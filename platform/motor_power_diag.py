#!/usr/bin/env python3
"""
四电机输出能力诊断（API 版）
===========================

用途：
  - 逐个电机测试正转/反转输出能力
  - 使用 /motor/sensor_test 的 traveled_mm 作为统一量化指标
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


POSITIONS = ["front_left", "front_right", "rear_left", "rear_right"]


@dataclass
class MotorDiagRow:
    position: str
    direction: str
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


def run_case(base_url: str, position: str, direction: str, speed: int, duration_s: float) -> MotorDiagRow:
    try:
        # 先全停，避免上一条命令残留
        motor_stop_all(base_url)
        time.sleep(0.15)

        # 启动单电机
        motor_set(base_url, position, direction, speed)
        time.sleep(0.05)

        # 采样窗口：保持当前电机运行，读取 traveled_mm
        q = urllib.parse.urlencode({"duration_s": f"{duration_s:.2f}"})
        report = request_json(f"{base_url}/motor/sensor_test?{q}", method="GET", timeout_s=duration_s + 6.0)

        # 停当前电机
        motor_set(base_url, position, "stop", speed)

        traveled = float(report.get("encoder", {}).get("traveled_mm", 0.0))
        gyro_peak = float(report.get("imu", {}).get("gyro_z_peak_dps", 0.0))
        return MotorDiagRow(position, direction, traveled, gyro_peak, True)
    except Exception as e:
        return MotorDiagRow(position, direction, 0.0, 0.0, False, str(e))
    finally:
        motor_stop_all(base_url)


def print_row(r: MotorDiagRow, ref: float) -> None:
    if not r.ok:
        print(f"{r.position:>11} {r.direction:>8} | ❌ {r.error}")
        return
    ratio = (r.traveled_mm / ref * 100.0) if ref > 1e-9 else 0.0
    flag = "⚠️偏低" if ref > 1e-9 and ratio < 65.0 else "OK"
    print(
        f"{r.position:>11} {r.direction:>8} | "
        f"travel={r.traveled_mm:>7.1f} mm | "
        f"gyro_peak={r.gyro_peak:>6.1f} dps | "
        f"{ratio:>6.1f}% {flag}"
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

    ref = max((r.traveled_mm for r in ok_rows), default=0.0)
    print("\n结果（按 traveled_mm 与最大值对比）：")
    print("   position direction | metrics")
    for r in rows:
        print_row(r, ref)

    weak = [r for r in ok_rows if ref > 1e-9 and (r.traveled_mm / ref) < 0.65]
    print("\n结论：")
    if not weak:
        print("✅ 未发现明显无力电机（<65% 阈值）。")
    else:
        print("⚠️ 疑似无力电机：")
        for r in weak:
            print(f"  - {r.position}/{r.direction}  traveled_mm={r.traveled_mm:.1f}")
        print("建议：检查对应电机接线、驱动口、齿轮箱阻力，必要时互换驱动口复测。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

