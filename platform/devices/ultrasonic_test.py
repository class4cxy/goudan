"""
HC-SR04 超声波传感器真机测试脚本
================================
在树莓派上运行（Trig/Echo 接线完成后）。

默认接线（BCM）：
  Trig -> GPIO23
  Echo -> GPIO16

用法：
  python ultrasonic_test.py
  python ultrasonic_test.py --test 2
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from ultrasonic import Ultrasonic, UltrasonicConfig

DIVIDER = "─" * 60


def test_pin_config(trig_pin: int, echo_pin: int) -> None:
    print(f"\n{DIVIDER}")
    print("  测试 1 — 引脚配置检查")
    print(DIVIDER)
    print(f"  Trig: GPIO{trig_pin}")
    print(f"  Echo: GPIO{echo_pin}")
    print("  若接线不同，请使用 --trig-pin / --echo-pin 覆盖")
    print("  ✅ 配置检查完成")


def test_single_read(cfg: UltrasonicConfig) -> None:
    print(f"\n{DIVIDER}")
    print("  测试 2 — 单次测距")
    print(DIVIDER)
    sensor = Ultrasonic(cfg)
    sensor.start()
    try:
        time.sleep(0.1)
        reading = sensor.read_once()
        if reading is None:
            print("  ❌ 读数失败（回波超时或距离超量程）")
            return
        tag = "⚠️ TOO CLOSE" if reading.is_too_close else "OK"
        mode = "SIM" if sensor.is_simulation else "HW"
        print(f"  模式：{mode}")
        print(f"  距离：{reading.distance_cm:.2f} cm [{tag}]")
    finally:
        sensor.stop()


def test_continuous(cfg: UltrasonicConfig, duration_s: int = 20) -> None:
    print(f"\n{DIVIDER}")
    print(f"  测试 3 — 持续监测 {duration_s}s（Ctrl+C 提前退出）")
    print(DIVIDER)
    readings: list[float] = []

    def on_reading(r) -> None:
        readings.append(r.distance_cm)
        bar_n = int(max(0.0, min(100.0, 120 - r.distance_cm)) / 3)
        bar = "█" * bar_n
        flag = " ⚠️" if r.is_too_close else ""
        print(f"\r  {r.distance_cm:7.2f} cm |{bar:<40}|{flag}", end="", flush=True)

    sensor = Ultrasonic(cfg, on_reading=on_reading)
    sensor.start()
    try:
        time.sleep(duration_s)
    except KeyboardInterrupt:
        pass
    finally:
        sensor.stop()
    print()

    if readings:
        avg_d = sum(readings) / len(readings)
        min_d = min(readings)
        max_d = max(readings)
        print(f"\n  统计（{len(readings)} 次）：")
        print(f"  平均距离：{avg_d:.2f} cm")
        print(f"  最小距离：{min_d:.2f} cm")
        print(f"  最大距离：{max_d:.2f} cm")


def test_too_close_callback(cfg: UltrasonicConfig) -> None:
    print(f"\n{DIVIDER}")
    print("  测试 4 — 近距离告警回调")
    print(DIVIDER)
    print(f"  阈值：{cfg.too_close_threshold_cm:.1f} cm")
    print("  请将手掌/障碍物逐步靠近传感器，观察是否触发告警...")
    triggered = [0]

    def on_too_close(r) -> None:
        triggered[0] += 1
        print(f"\n  ✅ 触发告警：{r.distance_cm:.2f} cm (< {cfg.too_close_threshold_cm:.1f} cm)")

    sensor = Ultrasonic(cfg, on_too_close=on_too_close)
    sensor.start()
    try:
        time.sleep(10)
    except KeyboardInterrupt:
        pass
    finally:
        sensor.stop()

    if triggered[0] == 0:
        print("  ⚠️ 未触发告警，检查阈值设置或传感器朝向")
    else:
        print(f"  告警触发次数：{triggered[0]}")


def main() -> None:
    parser = argparse.ArgumentParser(description="HC-SR04 超声波传感器真机测试")
    parser.add_argument("--test", type=int, default=0, help="直接运行指定测试编号")
    parser.add_argument("--trig-pin", type=int, default=23)
    parser.add_argument("--echo-pin", type=int, default=16)
    parser.add_argument("--threshold-cm", type=float, default=25.0)
    args = parser.parse_args()

    cfg = UltrasonicConfig(
        trig_pin=args.trig_pin,
        echo_pin=args.echo_pin,
        too_close_threshold_cm=args.threshold_cm,
    )

    print("\n╔══════════════════════════════════════════════════════╗")
    print("║         HC-SR04 超声波传感器真机测试工具             ║")
    print("╚══════════════════════════════════════════════════════╝")

    tests = {
        1: lambda: test_pin_config(args.trig_pin, args.echo_pin),
        2: lambda: test_single_read(cfg),
        3: lambda: test_continuous(cfg),
        4: lambda: test_too_close_callback(cfg),
    }

    if args.test:
        tests.get(args.test, lambda: print("无效测试编号"))()
        return

    while True:
        print(f"\n{DIVIDER}")
        print("  [1] 引脚配置检查")
        print("  [2] 单次测距")
        print("  [3] 持续监测 20s")
        print("  [4] 近距离告警回调验证")
        print("  [0] 退出")
        print(DIVIDER)
        print("  请选择：", end="")
        c = input().strip()
        if c == "0":
            break
        if c in {"1", "2", "3", "4"}:
            tests[int(c)]()


if __name__ == "__main__":
    main()

