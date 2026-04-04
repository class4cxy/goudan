#!/usr/bin/env python3
"""
四电机输出能力诊断（API 版）
===========================

用途：
  - 逐个电机测试正转/反转输出能力
  - 优先使用 /power/status 电流值评估“出力是否明显偏低”
  - 辅助读取 /motor/sensor_test 的 traveled_mm（仅后轮编码器有参考意义）
  - 快速定位“个别电机无力/不转/方向异常”

说明：
  - 该脚本不直接占用 GPIO，只调用 platform HTTP API
  - 适合在 platform 服务运行时执行
"""

from __future__ import annotations

import argparse
import json
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from statistics import mean


POSITIONS = ["front_left", "front_right", "rear_left", "rear_right"]


@dataclass
class MotorDiagRow:
    position: str
    direction: str
    current_ma: float
    current_delta_ma: float
    current_valid: bool
    traveled_mm: float
    gyro_peak: float
    ok: bool
    error: str = ""


def request_json(url: str, method: str = "GET", payload: dict | None = None, timeout_s: float = 15.0) -> dict:
    data = None
    headers = {}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url=url, method=method, data=data, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout_s) as resp:
        return json.loads(resp.read().decode("utf-8"))


def motor_set(base_url: str, position: str, direction: str, speed: int) -> dict:
    return request_json(
        f"{base_url}/motor/set",
        method="POST",
        payload={"position": position, "direction": direction, "speed": speed},
        timeout_s=8.0,
    )


def motor_stop_all(base_url: str) -> None:
    try:
        request_json(
            f"{base_url}/motor/command",
            method="POST",
            payload={"command": "stop"},
            timeout_s=4.0,
        )
    except Exception:
        pass


def read_current_ma(base_url: str) -> tuple[float | None, int | None]:
    """
    从 /power/status 读取当前电流（mA）。
    若 power 传感器不可用或无数据，返回 None。
    """
    try:
        st = request_json(f"{base_url}/power/status", method="GET", timeout_s=3.0)
        latest = st.get("latest")
        if not latest:
            return None, None
        v = latest.get("current_ma")
        ts = latest.get("timestamp_ms")
        if v is None:
            return None, None
        return float(v), int(ts) if ts is not None else None
    except Exception:
        return None, None


def sample_current_ma(base_url: str, samples: int = 4, gap_s: float = 0.15) -> tuple[float | None, bool]:
    """
    多次采样电流，降低单点噪声。
    返回 (平均值, 是否有效样本>=2)。
    """
    values: list[float] = []
    for _ in range(samples):
        v, _ = read_current_ma(base_url)
        if v is not None:
            values.append(v)
        time.sleep(gap_s)
    if len(values) < 2:
        return (values[0] if values else None), False
    return mean(values), True


def clear_travel_accumulator(base_url: str) -> None:
    """
    清空 odometry travel 累计器，避免上一轮残留影响当前 case。
    通过短时 sensor_test 触发 get_and_reset_travel()。
    """
    try:
        q = urllib.parse.urlencode({"duration_s": "1.0"})
        request_json(f"{base_url}/motor/sensor_test?{q}", method="GET", timeout_s=8.0)
    except Exception:
        pass


def run_case(base_url: str, position: str, direction: str, speed: int, duration_s: float) -> MotorDiagRow:
    try:
        # 先全停，避免上一条命令残留
        motor_stop_all(base_url)
        time.sleep(0.25)
        clear_travel_accumulator(base_url)
        baseline_current, baseline_valid = sample_current_ma(base_url, samples=4, gap_s=0.12)

        # 启动单电机
        motor_set(base_url, position, direction, speed)
        time.sleep(0.2)
        active_current, active_valid = sample_current_ma(base_url, samples=4, gap_s=0.12)

        # 采样窗口：保持当前电机运行，读取 traveled_mm
        q = urllib.parse.urlencode({"duration_s": f"{duration_s:.2f}"})
        report = request_json(f"{base_url}/motor/sensor_test?{q}", method="GET", timeout_s=duration_s + 6.0)

        # 停当前电机
        motor_set(base_url, position, "stop", speed)

        traveled = float(report.get("encoder", {}).get("traveled_mm", 0.0))
        gyro_peak = float(report.get("imu", {}).get("gyro_z_peak_dps", 0.0))
        current_valid = (
            baseline_current is not None and active_current is not None
            and baseline_valid and active_valid
        )
        if current_valid:
            delta_ma = active_current - baseline_current
        else:
            delta_ma = 0.0
        return MotorDiagRow(
            position, direction, active_current or 0.0, delta_ma, current_valid, traveled, gyro_peak, True
        )
    except Exception as e:
        return MotorDiagRow(position, direction, 0.0, 0.0, False, 0.0, 0.0, False, str(e))
    finally:
        motor_stop_all(base_url)


def print_row(r: MotorDiagRow, ref_current: float, ref_travel: float, use_current: bool) -> None:
    if not r.ok:
        print(f"{r.position:>11} {r.direction:>8} | ❌ {r.error}")
        return
    ratio_current = (r.current_delta_ma / ref_current * 100.0) if (use_current and ref_current > 1e-9) else 0.0
    ratio_travel = (r.traveled_mm / ref_travel * 100.0) if ref_travel > 1e-9 else 0.0
    if use_current:
        flag = "⚠️电流偏低" if ref_current > 1e-9 and ratio_current < 65.0 else "OK"
    else:
        flag = "ℹ️电流无效"
    print(
        f"{r.position:>11} {r.direction:>8} | "
        f"I={r.current_ma:>7.1f}mA "
        f"ΔI={r.current_delta_ma:>7.1f}mA ({ratio_current:>6.1f}%) | "
        f"travel={r.traveled_mm:>6.1f}mm ({ratio_travel:>6.1f}%) | "
        f"{flag}"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="四电机输出能力诊断（逐个电机）")
    parser.add_argument("--base-url", default="http://localhost:8001", help="Platform 地址")
    parser.add_argument("--speed", type=int, default=35, help="测试速度 0-100")
    parser.add_argument("--duration-s", type=float, default=1.6, help="每路采样时长")
    args = parser.parse_args()

    speed = max(0, min(100, args.speed))
    duration_s = max(0.8, min(6.0, args.duration_s))
    rows: list[MotorDiagRow] = []

    print("== 电机输出能力诊断 ==")
    print(f"base_url={args.base_url} speed={speed} duration_s={duration_s}")
    print("提示：请把车架空（轮子离地）再执行，避免碰撞。\n")

    try:
        for pos in POSITIONS:
            for direction in ("forward", "backward"):
                print(f"测试 {pos}/{direction} ...")
                rows.append(run_case(args.base_url, pos, direction, speed, duration_s))
                time.sleep(0.25)
    finally:
        motor_stop_all(args.base_url)

    ok_rows = [r for r in rows if r.ok]
    if not ok_rows:
        print("\n❌ 全部测试失败，先检查 platform 服务是否在运行。")
        return 1

    valid_current_rows = [r for r in ok_rows if r.current_valid]
    use_current = len(valid_current_rows) >= 2
    ref_current = max((r.current_delta_ma for r in valid_current_rows), default=0.0)
    ref_travel = max((r.traveled_mm for r in ok_rows), default=0.0)
    if use_current:
        print("\n结果（主指标=电流增量 ΔI，与最大值对比）：")
    else:
        print("\n结果（电流指标无效，回退展示 traveled 辅助指标）：")
        print("提示：/power/status 更新太慢或无新样本，请提高 POWER_POLL_INTERVAL 频率后重测。")
    print("   position direction | metrics")
    for r in rows:
        print_row(r, ref_current, ref_travel, use_current)

    weak = (
        [r for r in valid_current_rows if ref_current > 1e-9 and (r.current_delta_ma / ref_current) < 0.65]
        if use_current else []
    )
    print("\n结论：")
    if not weak:
        print("✅ 未发现明显无力电机（<65% 阈值）。")
    else:
        print("⚠️ 疑似无力电机：")
        for r in weak:
            print(f"  - {r.position}/{r.direction}  ΔI={r.current_delta_ma:.0f}mA")
        print("建议：检查对应电机接线、驱动口、齿轮箱阻力，必要时互换驱动口复测。")
        print("注：travel 指标仅对后轮编码器更敏感，前轮低值不一定代表无力。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

