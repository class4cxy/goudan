"""
HC-SR04 超声波传感器真机测试脚本
================================
在树莓派上运行（Trig/Echo 接线完成后）。

默认接线（BCM）：
  Trig -> GPIO20
  Echo -> GPIO21

用法：
  python ultrasonic_test.py
  python ultrasonic_test.py --test 2
  python ultrasonic_test.py --scan
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from ultrasonic import Ultrasonic, UltrasonicConfig

DIVIDER = "─" * 60

DEFAULT_SCAN_PINS = [20, 21, 23, 16, 18, 19, 4, 17, 11, 10]


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
    if not _safe_start(sensor, cfg):
        return
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
    if not _safe_start(sensor, cfg):
        return
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
    if not _safe_start(sensor, cfg):
        return
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


def scan_pin_pairs(
    candidate_pins: list[int],
    attempts_per_pair: int = 3,
    min_ok_distance: float = 2.0,
    max_ok_distance: float = 400.0,
) -> None:
    """
    扫描候选 Trig/Echo 组合，找出最可能的接线。
    建议测试时在传感器正前方放置平整障碍物（20~80cm）。
    """
    print(f"\n{DIVIDER}")
    print("  测试 5 — 自动扫描 Trig/Echo 组合")
    print(DIVIDER)
    print(f"  候选引脚：{candidate_pins}")
    print(f"  每组合尝试次数：{attempts_per_pair}")
    print("  提示：请在传感器前方保持稳定障碍物，避免误判")

    if len(candidate_pins) < 2:
        print("  ❌ 候选引脚数量不足（至少 2 个）")
        return

    scored: list[tuple[int, int, int, float]] = []
    total_pairs = len(candidate_pins) * (len(candidate_pins) - 1)
    tested = 0

    for trig in candidate_pins:
        for echo in candidate_pins:
            if trig == echo:
                continue
            tested += 1
            print(f"\r  扫描进度：{tested}/{total_pairs}  当前 Trig={trig} Echo={echo}", end="", flush=True)
            ok_count = 0
            distances: list[float] = []

            cfg = UltrasonicConfig(trig_pin=trig, echo_pin=echo)
            sensor = Ultrasonic(cfg)
            try:
                sensor.start()
            except Exception:
                # 单个组合初始化失败（如引脚被占用）时跳过，继续扫描其它组合。
                scored.append((trig, echo, 0, 0.0))
                continue
            try:
                time.sleep(0.03)
                for _ in range(attempts_per_pair):
                    reading = sensor.read_once()
                    if reading and min_ok_distance <= reading.distance_cm <= max_ok_distance:
                        ok_count += 1
                        distances.append(reading.distance_cm)
                    time.sleep(0.03)
            finally:
                sensor.stop()

            avg_d = (sum(distances) / len(distances)) if distances else 0.0
            scored.append((trig, echo, ok_count, avg_d))

    print()
    ranked = sorted(scored, key=lambda x: (x[2], -abs(80.0 - x[3])), reverse=True)
    good = [x for x in ranked if x[2] > 0]

    if not good:
        print("  ❌ 未找到可用 Trig/Echo 组合")
        print("  请检查：供电、GND 共地、Echo 电平转换、候选引脚范围")
        return

    print("\n  候选结果（按成功次数排序）：")
    print("  Trig  Echo  成功次数  平均距离(cm)")
    print("  " + "-" * 36)
    for trig, echo, ok_count, avg_d in good[:8]:
        print(f"  {trig:>4}  {echo:>4}  {ok_count:>8}  {avg_d:>11.2f}")

    best = good[0]
    print("\n  ✅ 最可能接线：")
    print(f"    Trig=GPIO{best[0]}, Echo=GPIO{best[1]}（成功 {best[2]}/{attempts_per_pair}）")
    print(f"    可直接复测：python3 ultrasonic_test.py --test 2 --trig-pin {best[0]} --echo-pin {best[1]}")


def main() -> None:
    parser = argparse.ArgumentParser(description="HC-SR04 超声波传感器真机测试")
    parser.add_argument("--test", type=int, default=0, help="直接运行指定测试编号")
    parser.add_argument("--scan", action="store_true", help="自动扫描 Trig/Echo 组合")
    parser.add_argument(
        "--scan-pins",
        type=str,
        default=",".join(str(x) for x in DEFAULT_SCAN_PINS),
        help="扫描候选 BCM 引脚，逗号分隔，例如 23,16,18,19",
    )
    parser.add_argument("--scan-attempts", type=int, default=3, help="每个引脚组合尝试次数")
    parser.add_argument("--trig-pin", type=int, default=20)
    parser.add_argument("--echo-pin", type=int, default=21)
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
        5: lambda: scan_pin_pairs(_parse_scan_pins(args.scan_pins), args.scan_attempts),
    }

    if args.scan:
        scan_pin_pairs(_parse_scan_pins(args.scan_pins), args.scan_attempts)
        return

    if args.test:
        tests.get(args.test, lambda: print("无效测试编号"))()
        return

    while True:
        print(f"\n{DIVIDER}")
        print("  [1] 引脚配置检查")
        print("  [2] 单次测距")
        print("  [3] 持续监测 20s")
        print("  [4] 近距离告警回调验证")
        print("  [5] 自动扫描 Trig/Echo 组合")
        print("  [0] 退出")
        print(DIVIDER)
        print("  请选择：", end="")
        c = input().strip()
        if c == "0":
            break
        if c in {"1", "2", "3", "4", "5"}:
            tests[int(c)]()


def _parse_scan_pins(raw: str) -> list[int]:
    pins: list[int] = []
    for part in raw.split(","):
        p = part.strip()
        if not p:
            continue
        try:
            pins.append(int(p))
        except ValueError:
            pass
    dedup = []
    seen = set()
    for pin in pins:
        if pin not in seen:
            dedup.append(pin)
            seen.add(pin)
    return dedup or list(DEFAULT_SCAN_PINS)


def _safe_start(sensor: Ultrasonic, cfg: UltrasonicConfig) -> bool:
    try:
        sensor.start()
        return True
    except Exception as e:
        print("  ❌ 传感器初始化失败")
        print(f"  Trig=GPIO{cfg.trig_pin}, Echo=GPIO{cfg.echo_pin}")
        print(f"  详情：{e}")
        print("  排查建议：")
        print("    1) 关闭占用引脚的功能（常见：SPI1 占用 GPIO20/21）")
        print("    2) 执行 `raspi-gpio get 20` 和 `raspi-gpio get 21` 查看复用状态")
        print("    3) 执行 `sudo lsof /dev/gpiochip0 /dev/gpiochip4` 检查占用进程")
        print("    4) 若仍失败，改用其它空闲引脚并重新 --scan")
        return False


if __name__ == "__main__":
    main()

