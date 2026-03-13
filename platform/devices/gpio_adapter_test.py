#!/usr/bin/env python3
"""
GPIO 引脚探针 — 逐一激活候选引脚，目视确认与电机/舵机的连接关系。

运行：
  python3 probe_gpio.py          # 全量两阶段探测（推荐）
  python3 probe_gpio.py --a      # 只跑 Phase A（单引脚拉高）
  python3 probe_gpio.py --b      # 只跑 Phase B（配合 EN=13 做 IN 扫描）
  python3 probe_gpio.py --c      # 只跑 Phase C（PWM 扫描，找 EN 引脚）
  python3 probe_gpio.py --pin 17 # 只测试指定引脚（HIGH 模式）

两阶段逻辑
──────────
Phase A — 单引脚拉高扫描
  每个候选引脚单独拉高 0.8s，观察哪个轮子/舵机响应。
  适用于：电机驱动板 EN 已默认接高电平，只需 IN 信号即可转动的情况。
  → 能找到 IN1（出现正转）和 IN2（出现反转）引脚。

Phase B — 固定 EN=GPIO13，扫描 IN 引脚
  保持 GPIO 13 输出 PWM（已知能激活右前轮），
  再对每个候选引脚逐一拉高，观察哪个轮子响应。
  → 能找到所有使用 EN=13 的电机的 IN 引脚。

Phase C — 固定 IN=GPIO27（已知右前轮 IN1），扫描 EN 引脚
  保持 GPIO 27 拉高，对每个候选引脚逐一输出 PWM，
  看右前轮是否转动，同时观察其他轮子。
  → 验证 EN=13 并找出其他 EN 引脚。

最终：把打印出的汇总表告知 AI，即可生成正确的 ChassisConfig。
"""

import argparse
import time

# ── 候选引脚（跳过 I2C=2/3, SPI=7-11, UART=14/15, 预留=0/1）─────────
CANDIDATE_PINS = [4, 5, 6, 12, 13, 16, 17, 18, 19, 20, 21, 22, 23, 24, 25, 26, 27]

PULSE = 0.8   # 秒，每次脉冲持续时间
PWM_FREQ = 1000
PWM_DUTY = 75  # %

# ── GPIO 初始化 ───────────────────────────────────────────────────────
try:
    import RPi.GPIO as GPIO
    GPIO.setmode(GPIO.BCM)
    GPIO.setwarnings(False)
    REAL = True
except (ImportError, RuntimeError):
    print("⚠  未检测到 RPi.GPIO，进入模拟模式（不操作真实引脚）\n")
    REAL = False

    class _FakeGPIO:
        BCM = "BCM"; OUT = "OUT"
        def setmode(self, *a): pass
        def setwarnings(self, *a): pass
        def setup(self, *a, **kw): pass
        def output(self, pin, val):
            print(f"  [SIM] GPIO {pin:2d} = {'HIGH' if val else 'LOW '}")
        def cleanup(self, *a): print("  [SIM] cleanup")

        class PWM:
            def __init__(self, pin, freq): self._p = pin
            def start(self, d): print(f"  [SIM] PWM GPIO{self._p:2d} {d}%")
            def ChangeDutyCycle(self, d): pass
            def stop(self): print(f"  [SIM] PWM GPIO{self._p:2d} stop")

    GPIO = _FakeGPIO()

# ── 观察结果选项 ──────────────────────────────────────────────────────
CHOICES = {
    "0": "无反应",
    "1": "左前轮(M1)  正转",
    "2": "左前轮(M1)  反转",
    "3": "右前轮(M2)  正转",
    "4": "右前轮(M2)  反转",
    "5": "左后轮(M3)  正转",
    "6": "左后轮(M3)  反转",
    "7": "右后轮(M4)  正转",
    "8": "右后轮(M4)  反转",
    "s": "舵机声音（无轮转动）",
    "m": "多个轮子同时响应",
    "?": "有动静但无法判断",
}


def _ask(pin: int, mode: str) -> tuple[str, str]:
    """打印选项并读取用户输入。"""
    print(f"\n  GPIO {pin:2d} [{mode}] 激活 {PULSE}s 完毕，观察到了什么？")
    for k, v in CHOICES.items():
        print(f"    {k} = {v}")
    while True:
        try:
            ans = input("  输入编号 > ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            return "q", "用户中断"
        if ans in CHOICES:
            return ans, CHOICES[ans]
        if ans == "q":
            return "q", "用户中断"
        print("  无效输入，请重新输入")


def _setup_out(pin: int):
    GPIO.setup(pin, GPIO.OUT)
    GPIO.output(pin, False)


def _high_pulse(pin: int):
    """单引脚拉高 PULSE 秒后归零。"""
    GPIO.setup(pin, GPIO.OUT)
    GPIO.output(pin, True)
    time.sleep(PULSE)
    GPIO.output(pin, False)


def _pwm_pulse(pin: int) -> None:
    """单引脚 PWM PULSE 秒后停止。"""
    GPIO.setup(pin, GPIO.OUT)
    pwm = GPIO.PWM(pin, PWM_FREQ)
    pwm.start(PWM_DUTY)
    time.sleep(PULSE)
    pwm.stop()
    GPIO.output(pin, False)


# ── Phase A：单引脚拉高扫描 ───────────────────────────────────────────
def phase_a(pins: list[int]) -> dict[int, tuple[str, str]]:
    print("\n" + "═" * 62)
    print("  Phase A — 单引脚拉高扫描")
    print("  每个引脚单独拉高 0.8s，其他所有引脚保持 LOW。")
    print("  观察哪个轮子/舵机响应，记录正/反转。")
    print("═" * 62)

    results: dict[int, tuple[str, str]] = {}
    for pin in pins:
        print(f"\n  ── GPIO {pin:2d} ───────────────────────────")
        _high_pulse(pin)
        code, label = _ask(pin, "HIGH")
        if code == "q":
            break
        results[pin] = (code, label)
    return results


# ── Phase B：固定 EN=GPIO13，扫描 IN 引脚 ────────────────────────────
def phase_b(pins: list[int], en_pin: int = 13) -> dict[int, tuple[str, str]]:
    print("\n" + "═" * 62)
    print(f"  Phase B — 固定 EN=GPIO{en_pin} PWM，逐一拉高候选引脚")
    print(f"  保持 GPIO {en_pin} 持续输出 PWM（{PWM_DUTY}%），")
    print("  每个候选引脚单独拉高 0.8s，观察电机响应。")
    print("═" * 62)

    # 启动 EN PWM
    GPIO.setup(en_pin, GPIO.OUT)
    en_pwm = GPIO.PWM(en_pin, PWM_FREQ)
    en_pwm.start(PWM_DUTY)

    results: dict[int, tuple[str, str]] = {}
    for pin in pins:
        if pin == en_pin:
            continue
        print(f"\n  ── GPIO {pin:2d} ───────────────────────────")
        GPIO.setup(pin, GPIO.OUT)
        GPIO.output(pin, True)
        time.sleep(PULSE)
        GPIO.output(pin, False)
        code, label = _ask(pin, f"HIGH+EN{en_pin}")
        if code == "q":
            break
        results[pin] = (code, label)

    en_pwm.stop()
    GPIO.output(en_pin, False)
    return results


# ── Phase C：固定 IN=GPIO27，扫描 EN 引脚 ────────────────────────────
def phase_c(pins: list[int], in_pin: int = 27) -> dict[int, tuple[str, str]]:
    print("\n" + "═" * 62)
    print(f"  Phase C — 固定 IN=GPIO{in_pin} HIGH，逐一 PWM 扫描候选引脚")
    print(f"  保持 GPIO {in_pin} 拉高（已知可激活右前轮正转方向），")
    print("  每个候选引脚依次输出 PWM，观察哪个电机转动。")
    print("═" * 62)

    GPIO.setup(in_pin, GPIO.OUT)
    GPIO.output(in_pin, True)

    results: dict[int, tuple[str, str]] = {}
    for pin in pins:
        if pin == in_pin:
            continue
        print(f"\n  ── GPIO {pin:2d} ───────────────────────────")
        _pwm_pulse(pin)
        code, label = _ask(pin, f"PWM+IN{in_pin}")
        if code == "q":
            break
        results[pin] = (code, label)

    GPIO.output(in_pin, False)
    return results


# ── 打印汇总表 ────────────────────────────────────────────────────────
def print_summary(title: str, results: dict[int, tuple[str, str]]) -> None:
    if not results:
        return
    print(f"\n{'═'*62}")
    print(f"  {title}")
    print(f"{'═'*62}")
    print(f"  {'GPIO':>6}  {'代码':>4}  描述")
    print("  " + "─" * 50)
    for pin, (code, label) in sorted(results.items()):
        marker = "  ◀" if code not in ("0", "?", "q") else ""
        print(f"  GPIO {pin:2d}  {code:>4}  {label}{marker}")
    print()


# ── 主函数 ────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="GPIO 引脚探针 — MAKEROBO 电机控制引脚识别")
    parser.add_argument("--a", action="store_true", help="只跑 Phase A（单引脚拉高）")
    parser.add_argument("--b", action="store_true", help="只跑 Phase B（EN=13 配合 IN 扫描）")
    parser.add_argument("--c", action="store_true", help="只跑 Phase C（IN=27 配合 EN 扫描）")
    parser.add_argument("--pin", type=int, help="只测试指定 BCM 引脚（HIGH 模式）")
    args = parser.parse_args()

    print("\n" + "═" * 62)
    print("  GPIO 引脚探针 — MAKEROBO 电机控制引脚识别")
    print(f"  候选引脚（BCM）：{CANDIDATE_PINS}")
    print(f"  脉冲时长：{PULSE}s  |  PWM 占空比：{PWM_DUTY}%")
    print("  ！确保小车架空或放在安全位置，防止意外行驶！")
    print("═" * 62)

    try:
        if args.pin is not None:
            # 只测单个引脚
            print(f"\n  单引脚测试：GPIO {args.pin}")
            _high_pulse(args.pin)
            code, label = _ask(args.pin, "HIGH")
            print(f"\n  结果：GPIO {args.pin} → {label}")

        elif args.a:
            r = phase_a(CANDIDATE_PINS)
            print_summary("Phase A 汇总", r)

        elif args.b:
            r = phase_b(CANDIDATE_PINS)
            print_summary("Phase B 汇总（EN=GPIO13）", r)

        elif args.c:
            r = phase_c(CANDIDATE_PINS)
            print_summary("Phase C 汇总（IN=GPIO27）", r)

        else:
            # 全量：A → B → C
            ra = phase_a(CANDIDATE_PINS)
            print_summary("Phase A 汇总", ra)

            cont = input("继续 Phase B（固定EN=13扫IN引脚）？[y/N] ").strip().lower()
            rb: dict = {}
            if cont == "y":
                rb = phase_b(CANDIDATE_PINS)
                print_summary("Phase B 汇总（EN=GPIO13）", rb)

            cont = input("继续 Phase C（固定IN=27扫EN引脚）？[y/N] ").strip().lower()
            rc: dict = {}
            if cont == "y":
                rc = phase_c(CANDIDATE_PINS)
                print_summary("Phase C 汇总（IN=GPIO27）", rc)

            # 最终合并汇总
            all_pins = sorted(set(ra) | set(rb) | set(rc))
            if all_pins:
                print("═" * 62)
                print("  最终汇总（把这张表发给 AI，帮你生成正确的 ChassisConfig）")
                print("═" * 62)
                print(f"  {'GPIO':>6}  {'Phase A':>12}  {'Phase B(EN13)':>14}  {'Phase C(IN27)':>14}")
                print("  " + "─" * 58)
                for pin in all_pins:
                    a = ra.get(pin, ("-", "─"))[1][:20]
                    b = rb.get(pin, ("-", "─"))[1][:20]
                    c = rc.get(pin, ("-", "─"))[1][:20]
                    print(f"  GPIO {pin:2d}  {a:<20}  {b:<20}  {c:<20}")
                print()

    except KeyboardInterrupt:
        print("\n\n  中断，正在清理 GPIO...")
    finally:
        GPIO.cleanup()
        print("  GPIO 已清理，探针退出。")


if __name__ == "__main__":
    main()
