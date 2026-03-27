#!/usr/bin/env python3
"""
舵机探针 — 摄像头水平云台调试。

MAKEROBO 扩展板舵机接口（已实测确认）：
  GPIO 12 = 水平轴 Pan（左右旋转）

舵机 PWM 参数（SG90 / MG90S）：
  频率：50 Hz（周期 20ms）
  占空比  2.5% → 0°   （脉宽 0.5ms）
  占空比  7.5% → 90°  （脉宽 1.5ms，中立/正前方）
  占空比 12.5% → 180° （脉宽 2.5ms）

用法：
  python3 servo_test.py              # 交互菜单
  python3 servo_test.py --center     # 水平轴归中后退出
  python3 servo_test.py --probe      # 引脚扫描：逐一激活候选引脚找到实际连接的舵机
"""

import argparse
import time

PAN_PIN    = 12   # 水平轴
PWM_FREQ   = 50   # Hz，舵机标准频率
DUTY_MIN   = 2.5  # % → 0°
DUTY_MID   = 7.5  # % → 90°
DUTY_MAX   = 12.5 # % → 180°
STEP_DELAY = 0.02 # 秒，平滑扫描每步延迟

# ── 实测物理标定值 ────────────────────────────────────────────────────
# 注意：此脚本直接操作物理 PWM 角度，与 Platform API 的「逻辑角度」不同。
# Platform 层：Pan invert=True → physical = min + max - logical
# Pan 逻辑 110° ↔ 物理 70°
PAN_CENTER  = 70.0  # 物理正前方（Platform API pan=110）

# ── GPIO 初始化 ───────────────────────────────────────────────────────
try:
    import RPi.GPIO as GPIO
    GPIO.setmode(GPIO.BCM)
    GPIO.setwarnings(False)
    REAL = True
except (ImportError, RuntimeError):
    print("⚠  模拟模式（非树莓派环境）\n")
    REAL = False

    class _FakeGPIO:
        BCM = "BCM"; OUT = "OUT"
        def setmode(self, *a): pass
        def setwarnings(self, *a): pass
        def setup(self, *a, **kw): pass
        def cleanup(self, *a): pass
        class PWM:
            def __init__(self, p, f): self._p = p
            def start(self, d): print(f"  [SIM] PWM GPIO{self._p} start {d:.2f}%")
            def ChangeDutyCycle(self, d): print(f"  [SIM] PWM GPIO{self._p} → {d:.2f}%")
            def stop(self): print(f"  [SIM] PWM GPIO{self._p} stop")
    GPIO = _FakeGPIO()


def _duty(angle: float) -> float:
    """角度（0–180°）→ 占空比（2.5–12.5%）。"""
    return DUTY_MIN + (DUTY_MAX - DUTY_MIN) * angle / 180.0


def _make_pwm(pin: int, init_angle: float = 90.0):
    """初始化引脚并设置初始角度。"""
    GPIO.setup(pin, GPIO.OUT)
    pwm = GPIO.PWM(pin, PWM_FREQ)
    pwm.start(_duty(init_angle))
    return pwm


def _smooth_move(pwm, from_angle: float, to_angle: float, step: float = 1.0):
    """平滑移动到目标角度。"""
    if from_angle == to_angle:
        return
    direction = 1 if to_angle > from_angle else -1
    step = abs(step) * direction
    angle = from_angle
    while (direction > 0 and angle < to_angle) or (direction < 0 and angle > to_angle):
        angle += step
        angle = max(0.0, min(180.0, angle))
        pwm.ChangeDutyCycle(_duty(angle))
        time.sleep(STEP_DELAY)


# ── 功能函数 ──────────────────────────────────────────────────────────

def center_pan():
    """水平轴归中：Pan → 70°（实测正前）。"""
    print(f"  水平轴归中（Pan {PAN_CENTER:.0f}°）...")
    GPIO.setup(PAN_PIN, GPIO.OUT)
    pwm = GPIO.PWM(PAN_PIN, PWM_FREQ)
    pwm.start(_duty(PAN_CENTER))
    time.sleep(0.5)
    pwm.stop()
    GPIO.cleanup(PAN_PIN)
    print("  完成。")


def sweep_pan():
    """水平轴全幅扫描：0° → 180° → PAN_CENTER。"""
    print(f"\n  水平 Pan（GPIO {PAN_PIN}）扫描：0° → 180° → {PAN_CENTER:.0f}°（正前）")
    pwm = _make_pwm(PAN_PIN, PAN_CENTER)
    time.sleep(0.3)
    _smooth_move(pwm, PAN_CENTER, 0)
    time.sleep(0.4)
    _smooth_move(pwm, 0, 180)
    time.sleep(0.4)
    _smooth_move(pwm, 180, PAN_CENTER)
    pwm.stop()
    GPIO.cleanup(PAN_PIN)
    print(f"  扫描完成，已归中（{PAN_CENTER:.0f}°）。")


def manual_pan():
    """手动输入角度控制水平轴。"""
    print(f"\n  水平 Pan 手动定位（GPIO {PAN_PIN}）— 输入角度 0–180，q 退出")
    pwm = _make_pwm(PAN_PIN, PAN_CENTER)
    _run_manual(pwm, 0.0, 180.0, PAN_CENTER)
    pwm.stop()
    GPIO.cleanup(PAN_PIN)


def _run_manual(pwm, hard_min: float, hard_max: float, start_angle: float = 90.0):
    """通用手动角度控制循环。"""
    current = start_angle
    while True:
        try:
            raw = input(f"  当前 {current:.1f}° → 输入角度 [0-180] > ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            break
        if raw in ("q", "quit"):
            break
        try:
            angle = float(raw)
            angle = max(hard_min, min(hard_max, angle))
            _smooth_move(pwm, current, angle, step=2.0)
            current = angle
            print(f"  → {current:.1f}°  (占空比 {_duty(current):.2f}%)")
        except ValueError:
            print("  请输入数字（0–180）或 q 退出")


# ── 引脚扫描探针 ──────────────────────────────────────────────────────

# 已知电机引脚（chassis.py 占用），不做舵机测试
MOTOR_PINS = {
    24: "M1-IN1(左前正转)", 25: "M1-IN2(左前反转)",
    27: "M2-IN1(右前正转)", 26: "M2-IN2(右前反转)",
     5: "M3-IN1(左后正转)",  6: "M3-IN2(左后反转)",
    22: "M4-IN1(右后正转)",  9: "M4-IN2(右后反转)",
}

# 全量候选引脚：跳过 I2C(2/3)、SPI(7/8)、UART(14/15)、电机引脚、蜂鸣器(17)
# 包含所有尚未排除的 GPIO（编码器规划脚也纳入，排查阶段优先于规划）
PROBE_PINS = [4, 10, 11, 12, 13, 14, 16, 17, 18, 19, 23]


def _pwm_sweep(pin: int, hold_s: float = 0.8):
    """对指定引脚输出舵机 PWM 摆动序列，hold_s 控制每个位置的驻留时长。"""
    GPIO.setup(pin, GPIO.OUT)
    pwm = GPIO.PWM(pin, PWM_FREQ)
    pwm.start(_duty(90))
    time.sleep(hold_s * 0.5)
    print("  → 45°（左）")
    pwm.ChangeDutyCycle(_duty(45))
    time.sleep(hold_s)
    print("  → 135°（右）")
    pwm.ChangeDutyCycle(_duty(135))
    time.sleep(hold_s)
    print("  → 90°（中立）")
    pwm.ChangeDutyCycle(_duty(90))
    time.sleep(hold_s * 0.5)
    pwm.stop()
    GPIO.cleanup(pin)
    # rpi-lgpio cleanup 可能重置 setmode，重新设置防止后续引脚失效
    try:
        GPIO.setmode(GPIO.BCM)
        GPIO.setwarnings(False)
    except Exception:
        pass


def probe_single_pin(pin: int):
    """对单个引脚持续输出 PWM 8 秒，用于手动核验接线。"""
    print(f"\n  对 GPIO {pin} 持续输出舵机 PWM 8 秒（45°→135° 来回慢摆）…")
    print("  此时用手将舵机信号线插到各接口，若云台动则说明该接口对应此引脚。")
    GPIO.setup(pin, GPIO.OUT)
    pwm = GPIO.PWM(pin, PWM_FREQ)
    pwm.start(_duty(90))
    deadline = time.time() + 8.0
    angle, direction = 90.0, 1.0
    while time.time() < deadline:
        angle += direction * 0.5
        if angle >= 135:
            direction = -1.0
        elif angle <= 45:
            direction = 1.0
        pwm.ChangeDutyCycle(_duty(angle))
        time.sleep(0.02)
    pwm.stop()
    GPIO.cleanup(pin)
    try:
        GPIO.setmode(GPIO.BCM)
        GPIO.setwarnings(False)
    except Exception:
        pass
    print("  完成。")


def probe_servo_pin():
    """
    全量引脚扫描：逐一激活候选 GPIO，输出 50Hz PWM 并来回摆动，
    找到实际连接舵机的引脚。

    判断标准：
      ✓ 有效响应 = 摄像头支架旋转（看到转动 或 听到舵机齿轮声/嗡嗡声）
      ✗ 无效响应 = 车轮/电机抖动（排除，不算）
    """
    print("\n" + "═" * 60)
    print("  舵机引脚全量扫描")
    print("  ✓ 有效 = 摄像头支架旋转 / 舵机发出嗡嗡声")
    print("  ✗ 无效 = 车轮/电机抖动（不要误报）")
    print(f"  候选引脚：{PROBE_PINS}")
    print("  请先停止 platform 服务，避免 GPIO 冲突。")
    print("═" * 60)

    try:
        input("\n  准备好后按 Enter 开始扫描...")
    except (EOFError, KeyboardInterrupt):
        return

    hit_pins: list[int] = []

    for pin in PROBE_PINS:
        print(f"\n  ── GPIO {pin:2d} ─────────────────────────────────")
        print(f"  → 发送 50Hz PWM …")
        _pwm_sweep(pin, hold_s=0.8)

        try:
            print("  云台/摄像头支架有旋转吗？")
            ans = input(f"  (y=转了 / n=没动 / q=退出) > ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            ans = "q"

        if ans == "q":
            print("  扫描中止。")
            break
        elif ans == "y":
            hit_pins.append(pin)
            print(f"  ✓ GPIO {pin} 有云台旋转！")

    # 汇总
    print("\n" + "═" * 60)
    print("  【扫描结果汇总】")
    if hit_pins:
        for p in hit_pins:
            print(f"  ✓ GPIO {p:2d} → 云台确认响应")
        if len(hit_pins) == 1:
            p = hit_pins[0]
            print(f"\n  建议更新：")
            print(f"    servo_test.py 顶部：PAN_PIN = {p}")
            print(f"    servo.py DEFAULT_CAMERA_CONFIG：pin={p}")
        else:
            print("\n  多个引脚有响应，请选择最明显的一个更新 PAN_PIN。")
    else:
        print("  所有引脚均无云台旋转响应。")
        print()
        print("  ── 下一步：手动热插拔诊断 ──────────────────────")
        print("  运行以下命令，脚本对 GPIO 12 持续输出 PWM 信号 8 秒：")
        print()
        print("    python3 servo_test.py --live 12")
        print()
        print("  在这 8 秒内，用手逐一把舵机信号线（橙/黄色）")
        print("  插到树莓派 40pin 排针的各个引脚，哪个引脚让云台动了")
        print("  就说明接线应该接在那里。")
        print()
        print("  如果 8 秒内完全没有任何响应，请检查：")
        print("  1. 舵机红线（VCC）和棕/黑线（GND）是否有电（用万用表量）")
        print("  2. 换一个舵机测试（排查舵机本身损坏）")
    print("═" * 60)


# ── 主菜单 ────────────────────────────────────────────────────────────

MENU = """
╔══════════════════════════════════════════════════╗
║       摄像头舵机探针 — 交互菜单                  ║
╠══════════════════════════════════════════════════╣
║  1. 水平 Pan  全幅扫描（0°→180°→70°）           ║
║  2. 水平 Pan  手动定位（起始 70°=正前）          ║
║  p. 引脚全量扫描（找不到舵机时用）              ║
║  0. 水平轴归中（Pan 70°）                        ║
║  q. 退出                                        ║
╚══════════════════════════════════════════════════╝"""


def main():
    parser = argparse.ArgumentParser(description="摄像头舵机探针")
    parser.add_argument("--center", action="store_true",
                        help="水平轴归中后退出")
    parser.add_argument("--probe", action="store_true",
                        help="引脚全量扫描：逐一激活候选引脚确认舵机接线")
    parser.add_argument("--live", type=int, metavar="PIN",
                        help="对指定引脚持续输出 PWM 8 秒（热插拔诊断用）")
    args = parser.parse_args()

    print("\n摄像头舵机探针 — MAKEROBO 扩展板")
    print(f"  水平 Pan  → GPIO {PAN_PIN}")
    print(f"  PWM {PWM_FREQ}Hz  |  占空比 {DUTY_MIN}%–{DUTY_MAX}%（对应 0°–180°）")

    try:
        if args.center:
            center_pan()
            return

        if args.probe:
            probe_servo_pin()
            return

        if args.live is not None:
            probe_single_pin(args.live)
            return

        while True:
            print(MENU)
            try:
                choice = input("请选择 > ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                break

            if choice == "q":
                break
            elif choice == "1":
                sweep_pan()
            elif choice == "2":
                manual_pan()
            elif choice == "p":
                probe_servo_pin()
            elif choice == "0":
                center_pan()
            else:
                print("  无效选项")

    except KeyboardInterrupt:
        print("\n  中断...")
    finally:
        GPIO.cleanup()
        print("  GPIO 已清理，退出。")


if __name__ == "__main__":
    main()
