#!/usr/bin/env python3
"""
编码器噪声诊断工具
==================
在树莓派上直接运行，持续监控每个 GPIO 引脚的跳变速率。
区分"电气噪声"和"真实编码器信号"。

运行方式：
    python3 encoder_diag.py

测试步骤：
    阶段1：电机断电/停止，静止观察 → 显示纯噪声基线
    阶段2：电机通电但不动（speed=0）→ 显示 PWM 干扰量
    阶段3：正常行驶 → 显示真实信号+噪声叠加
"""

import time
import sys

# ══════════════════════════════════════════════════════════════════════════════
# 硬件配置（修改这里，不依赖 .env）
# ══════════════════════════════════════════════════════════════════════════════

# 编码器 GPIO 引脚（BCM 编号）
# 左后轮（M3）：A→GPIO23（Pin 16），B→GPIO16（Pin 36）
LEFT_A  = 23
LEFT_B  = 16
# 右后轮（M4）：A→GPIO14（Pin 8，UART TX 已释放），B→GPIO18（Pin 12）
RIGHT_A = 14
RIGHT_B = 18

# 编码器标称线数（4 倍频后 ticks/rev = LINES_PER_REV × 4）
LINES_PER_REV = 500

# 轮子半径（mm），用于换算 mm/s 速度显示
WHEEL_RADIUS_MM = 34.0

# 噪声判定阈值（transitions/s per pin）
# 静止时超过此值视为噪声；正常行驶时会远超此值（属于真实信号）
NOISE_THRESHOLD = 50

# 每秒打印一次统计
REPORT_INTERVAL = 1.0

# ══════════════════════════════════════════════════════════════════════════════

import math

PINS = {"L_A": LEFT_A, "L_B": LEFT_B, "R_A": RIGHT_A, "R_B": RIGHT_B}
TICKS_PER_REV = LINES_PER_REV * 4
WHEEL_CIRC_MM = 2.0 * math.pi * WHEEL_RADIUS_MM


def detect_chip() -> int:
    """自动检测 GPIO chip 编号（RPi5 = 0，其他 = 0）。"""
    override = os.environ.get("GPIO_CHIP_NUM")
    if override:
        return int(override)
    try:
        import subprocess
        out = subprocess.check_output(["gpiodetect"], text=True, timeout=3)
        for line in out.splitlines():
            if "pinctrl-rp1" in line or ("pinctrl" in line and "brcmstb" not in line):
                return int(line.split()[0].replace("gpiochip", ""))
    except Exception:
        pass
    return 0





def run():
    try:
        import lgpio
    except ImportError:
        print("❌ lgpio 未安装，请运行：sudo apt install -y python3-lgpio")
        sys.exit(1)

    chip = detect_chip()
    print(f"📌 GPIO chip: gpiochip{chip}")
    print(f"📌 引脚：L_A=GPIO{LEFT_A}  L_B=GPIO{LEFT_B}  R_A=GPIO{RIGHT_A}  R_B=GPIO{RIGHT_B}")
    print(f"📌 ticks/rev={TICKS_PER_REV}  轮周长={WHEEL_CIRC_MM:.1f}mm")
    print()
    print("=" * 70)
    print("  时间  |  L_A  |  L_B  |  R_A  |  R_B  | 左轮mm/s | 右轮mm/s | 状态")
    print("=" * 70)

    h = lgpio.gpiochip_open(chip)
    for name, pin in PINS.items():
        try:
            lgpio.gpio_free(h, pin)
        except Exception:
            pass
        lgpio.gpio_claim_input(h, pin, lgpio.SET_PULL_UP)

    # 初始化状态
    prev = {name: lgpio.gpio_read(h, pin) for name, pin in PINS.items()}
    counts = {name: 0 for name in PINS}
    interval_counts = {name: 0 for name in PINS}

    # 也追踪正交解码的净位移（区分方向）
    QUAD_TABLE = {
        (0,0,0,1): +1, (0,1,1,1): +1, (1,1,1,0): +1, (1,0,0,0): +1,
        (0,0,1,0): -1, (1,0,1,1): -1, (1,1,0,1): -1, (0,1,0,0): -1,
    }
    left_ticks  = 0
    right_ticks = 0
    prev_la = prev["L_A"]
    prev_lb = prev["L_B"]
    prev_ra = prev["R_A"]
    prev_rb = prev["R_B"]

    start_total = time.monotonic()
    last_report = time.monotonic()
    total_counts = {name: 0 for name in PINS}

    print(f"\n[实时监控开始，Ctrl+C 停止]\n")

    try:
        while True:
            # 读取当前状态
            curr = {name: lgpio.gpio_read(h, pin) for name, pin in PINS.items()}

            # 逐引脚统计跳变
            for name in PINS:
                if curr[name] != prev[name]:
                    interval_counts[name] += 1
                    total_counts[name]    += 1

            # 正交解码左轮
            if curr["L_A"] != prev_la or curr["L_B"] != prev_lb:
                delta = QUAD_TABLE.get((prev_la, prev_lb, curr["L_A"], curr["L_B"]), 0)
                left_ticks += delta
            prev_la, prev_lb = curr["L_A"], curr["L_B"]

            # 正交解码右轮
            if curr["R_A"] != prev_ra or curr["R_B"] != prev_rb:
                delta = QUAD_TABLE.get((prev_ra, prev_rb, curr["R_A"], curr["R_B"]), 0)
                right_ticks += delta
            prev_ra, prev_rb = curr["R_A"], curr["R_B"]

            prev = curr

            # 定期报告
            now = time.monotonic()
            elapsed_interval = now - last_report
            if elapsed_interval >= REPORT_INTERVAL:
                elapsed_total = now - start_total

                # 计算速率（transitions/s per pin）
                rates = {name: interval_counts[name] / elapsed_interval for name in PINS}

                # 换算成 mm/s（正交解码结果）
                # left_ticks 和 right_ticks 是本次统计周期内的净位移
                left_mm_s  = (left_ticks  / TICKS_PER_REV) * WHEEL_CIRC_MM / elapsed_interval
                right_mm_s = (right_ticks / TICKS_PER_REV) * WHEEL_CIRC_MM / elapsed_interval

                any_noise = any(rates[n] > NOISE_THRESHOLD for n in PINS)
                status = "🔴 噪声严重" if any_noise else "🟢 信号正常"

                print(
                    f"  {elapsed_total:5.1f}s"
                    f" | {rates['L_A']:5.0f}"
                    f" | {rates['L_B']:5.0f}"
                    f" | {rates['R_A']:5.0f}"
                    f" | {rates['R_B']:5.0f}"
                    f" | {left_mm_s:+8.1f}"
                    f" | {right_mm_s:+8.1f}"
                    f" | {status}"
                )

                # 重置本区间计数
                interval_counts = {name: 0 for name in PINS}
                left_ticks  = 0
                right_ticks = 0
                last_report = now

    except KeyboardInterrupt:
        elapsed = time.monotonic() - start_total
        print("\n" + "=" * 70)
        print(f"总计运行 {elapsed:.1f}s")
        print("每引脚总跳变次数：")
        for name, count in total_counts.items():
            print(f"  GPIO{PINS[name]} ({name}): {count} 次 = {count/elapsed:.0f}/s")
        print("\n诊断结论：")
        total_rate = sum(total_counts.values()) / elapsed
        if total_rate > 200:
            print(f"  ❌ 噪声严重（平均 {total_rate:.0f} 跳变/s）")
            print("  ↳ 建议：在编码器 A/B 引脚对地各加 100nF 电容")
            print("  ↳ 或：将编码器信号线远离电机驱动线走线")
            print("  ↳ 软件缓解：已在 encoder.py 建议添加去抖延时")
        else:
            print(f"  ✅ 信号正常（平均 {total_rate:.0f} 跳变/s）")

    finally:
        lgpio.gpiochip_close(h)


if __name__ == "__main__":
    run()
