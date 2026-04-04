#!/usr/bin/env python3
"""
运动能力测试脚本（基于 /motor/drive 闭环接口）
=========================================

用途：
  - 批量测试直线行走能力（distance_mm）
  - 批量测试旋转能力（angle_deg）
  - 连续多轮执行并输出误差统计，便于观察参数调整后的变化趋势

运行示例：
  python3 motion_capability_test.py
  python3 motion_capability_test.py --repeat 3 --speed 45
  python3 motion_capability_test.py --distance "300,500,-300" --angle "90,-90,180"
  python3 motion_capability_test.py --continuous
"""

from __future__ import annotations

import argparse
import json
import signal
import time
import urllib.request
import atexit
from dataclasses import dataclass


@dataclass
class CaseResult:
    kind: str                     # "distance" | "angle"
    target: float
    actual: float
    sensor: str
    timeout: bool
    elapsed_s: float
    ok: bool
    error: str = ""


def parse_csv_floats(text: str) -> list[float]:
    vals: list[float] = []
    for raw in text.split(","):
        s = raw.strip()
        if not s:
            continue
        vals.append(float(s))
    return vals


def post_json(url: str, payload: dict, timeout_s: float = 120.0) -> dict:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url=url,
        data=data,
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout_s) as resp:
        body = resp.read().decode("utf-8")
        return json.loads(body)


def force_stop(base_url: str, timeout_s: float = 2.0) -> None:
    """
    失效保护：无论测试状态如何，都尝试发送 stop。
    忽略异常，避免清理流程因网络问题中断。
    """
    payload = {"command": "stop"}
    try:
        post_json(f"{base_url}/motor/command", payload, timeout_s=timeout_s)
    except Exception:
        pass


def run_distance_case(base_url: str, target_mm: float, speed: int, timeout_s: float) -> CaseResult:
    t0 = time.time()
    payload = {
        "distance_mm": target_mm,
        "speed": speed,
        "timeout_s": timeout_s,
    }
    try:
        res = post_json(f"{base_url}/motor/drive", payload, timeout_s=timeout_s + 10)
        elapsed = time.time() - t0
        return CaseResult(
            kind="distance",
            target=float(target_mm),
            actual=float(res.get("traveled_mm", 0.0)),
            sensor=str(res.get("sensor", "unknown")),
            timeout=bool(res.get("timeout", False)),
            elapsed_s=elapsed,
            ok=bool(res.get("ok", False)),
        )
    except Exception as e:
        # /motor/drive 请求异常时，强制尝试停机，防止车持续运动。
        force_stop(base_url)
        return CaseResult(
            kind="distance",
            target=float(target_mm),
            actual=0.0,
            sensor="error",
            timeout=False,
            elapsed_s=time.time() - t0,
            ok=False,
            error=str(e),
        )


def run_angle_case(base_url: str, target_deg: float, speed: int, timeout_s: float) -> CaseResult:
    t0 = time.time()
    payload = {
        "angle_deg": target_deg,
        "speed": speed,
        "timeout_s": timeout_s,
    }
    try:
        res = post_json(f"{base_url}/motor/drive", payload, timeout_s=timeout_s + 10)
        elapsed = time.time() - t0
        return CaseResult(
            kind="angle",
            target=float(target_deg),
            actual=float(res.get("rotated_deg", 0.0)),
            sensor=str(res.get("sensor", "unknown")),
            timeout=bool(res.get("timeout", False)),
            elapsed_s=elapsed,
            ok=bool(res.get("ok", False)),
        )
    except Exception as e:
        # /motor/drive 请求异常时，强制尝试停机，防止车持续运动。
        force_stop(base_url)
        return CaseResult(
            kind="angle",
            target=float(target_deg),
            actual=0.0,
            sensor="error",
            timeout=False,
            elapsed_s=time.time() - t0,
            ok=False,
            error=str(e),
        )


def signed_err(target: float, actual: float) -> float:
    s = 1.0 if target >= 0 else -1.0
    return actual * s - abs(target)


def print_case_result(i: int, total: int, r: CaseResult) -> None:
    if not r.ok:
        print(f"[{i}/{total}] {r.kind:<8} target={r.target:>7.1f} -> ❌ {r.error}")
        return
    err = signed_err(r.target, r.actual)
    err_pct = (err / max(abs(r.target), 1e-9)) * 100.0
    flag = "⏱ timeout" if r.timeout else "ok"
    unit = "mm" if r.kind == "distance" else "deg"
    print(
        f"[{i}/{total}] {r.kind:<8} target={r.target:>7.1f}{unit:<3} "
        f"actual={r.actual:>8.1f}{unit:<3} err={err:>+8.1f} ({err_pct:+6.1f}%) "
        f"sensor={r.sensor:<12} t={r.elapsed_s:>5.2f}s {flag}"
    )


def print_summary(results: list[CaseResult], title: str) -> None:
    ok_results = [r for r in results if r.ok]
    if not ok_results:
        print(f"\n{title}: 无成功样本")
        return
    abs_pct = []
    abs_err = []
    timeouts = 0
    for r in ok_results:
        e = signed_err(r.target, r.actual)
        abs_err.append(abs(e))
        abs_pct.append(abs(e) / max(abs(r.target), 1e-9) * 100.0)
        if r.timeout:
            timeouts += 1
    mean_abs_err = sum(abs_err) / len(abs_err)
    mean_abs_pct = sum(abs_pct) / len(abs_pct)
    max_abs_pct = max(abs_pct)
    print(
        f"{title}: n={len(ok_results)} | "
        f"MAE={mean_abs_err:.1f} | MAPE={mean_abs_pct:.1f}% | "
        f"worst={max_abs_pct:.1f}% | timeout={timeouts}"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="运动能力测试（距离 + 角度）")
    parser.add_argument("--base-url", default="http://localhost:8001", help="Platform API 地址")
    parser.add_argument("--speed", type=int, default=40, help="电机功率 0-100")
    parser.add_argument("--timeout-s", type=float, default=20.0, help="/motor/drive 的超时时间")
    parser.add_argument("--pause-s", type=float, default=1.2, help="每个动作之间暂停秒数")
    parser.add_argument("--repeat", type=int, default=2, help="整套用例重复次数")
    parser.add_argument(
        "--distance",
        default="300,-300,600",
        help="距离用例列表（mm，逗号分隔，支持负值后退）",
    )
    parser.add_argument(
        "--angle",
        default="90,-90,180",
        help="角度用例列表（deg，逗号分隔，支持负值右转）",
    )
    parser.add_argument("--no-distance", action="store_true", help="不测距离")
    parser.add_argument("--no-angle", action="store_true", help="不测角度")
    parser.add_argument("--continuous", action="store_true", help="持续循环，直到 Ctrl+C")
    args = parser.parse_args()

    speed = max(0, min(100, args.speed))
    dist_cases = parse_csv_floats(args.distance) if not args.no_distance else []
    angle_cases = parse_csv_floats(args.angle) if not args.no_angle else []
    if not dist_cases and not angle_cases:
        print("未配置任何测试用例（distance/angle 均为空）")
        return 2

    print("== 运动能力测试启动 ==")
    print(f"base_url={args.base_url} speed={speed} timeout_s={args.timeout_s} pause_s={args.pause_s}")
    print(f"distance_cases={dist_cases}")
    print(f"angle_cases={angle_cases}")
    print("按 Ctrl+C 可随时停止。\n")

    # 进程退出和 Ctrl+C 时都执行一次 stop（双保险）。
    atexit.register(force_stop, args.base_url)

    def _handle_signal(_sig, _frame):
        print("\n捕获中断信号，发送 stop...")
        force_stop(args.base_url)
        raise KeyboardInterrupt

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    loop_idx = 0
    all_distance: list[CaseResult] = []
    all_angle: list[CaseResult] = []

    try:
        while True:
            loop_idx += 1
            print(f"\n===== Round {loop_idx} =====")
            round_results: list[CaseResult] = []

            for _ in range(args.repeat):
                for d in dist_cases:
                    r = run_distance_case(args.base_url, d, speed, args.timeout_s)
                    round_results.append(r)
                    all_distance.append(r)
                    print_case_result(len(round_results), len(dist_cases) * args.repeat + len(angle_cases) * args.repeat, r)
                    time.sleep(args.pause_s)

                for a in angle_cases:
                    r = run_angle_case(args.base_url, a, speed, args.timeout_s)
                    round_results.append(r)
                    all_angle.append(r)
                    print_case_result(len(round_results), len(dist_cases) * args.repeat + len(angle_cases) * args.repeat, r)
                    time.sleep(args.pause_s)

            print_summary([r for r in round_results if r.kind == "distance"], "本轮距离统计")
            print_summary([r for r in round_results if r.kind == "angle"], "本轮角度统计")

            if not args.continuous:
                break
    except KeyboardInterrupt:
        print("\n收到 Ctrl+C，停止测试。")
        force_stop(args.base_url)
    finally:
        # 正常结束也发送 stop，避免最后一条动作残留。
        force_stop(args.base_url)

    print("\n===== 全部统计 =====")
    print_summary(all_distance, "距离总统计")
    print_summary(all_angle, "角度总统计")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

