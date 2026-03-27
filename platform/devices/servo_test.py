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

# 候选引脚：扩展板两路舵机接口 + 附近常被误接的引脚
# 标注已知电机引脚，防止误判电机抖动为舵机响应
PROBE_PINS = [12, 13, 16, 19, 18]  # 纯舵机候选（不含电机引脚）

# 已知电机引脚（chassis.py 占用），不做舵机测试
MOTOR_PINS = {
    24: "M1-IN1(左前正转)", 25: "M1-IN2(左前反转)",
    27: "M2-IN1(右前正转)", 26: "M2-IN2(右前反转)",
     5: "M3-IN1(左后正转)",  6: "M3-IN2(左后反转)",
    22: "M4-IN1(右后正转)",  9: "M4-IN2(右后反转)",
}


def probe_servo_pin():
    """
    逐一激活候选 GPIO 引脚，输出 50Hz PWM 并来回摆动，
    确认哪个引脚实际连接了舵机。

    判断标准（重要）：
      ✓ 舵机响应 = 摄像头支架/云台发生旋转，能看到或听到舵机齿轮声
      ✗ 轮子/电机抖动 ≠ 舵机响应，不要把车轮动误报为舵机

    每个引脚测试序列：90°→45°（左）→135°（右）→90°，约 2.5s
    """
    print("\n" + "═" * 60)
    print("  舵机引脚扫描探针 v2")
    print("  !! 判断标准 !!")
    print("  ✓ 有效响应 = 摄像头支架旋转（看到云台转动 或 听到舵机嗡嗡声）")
    print("  ✗ 无效响应 = 车轮/电机抖动（上次 GPIO26/GPIO6 就是电机误报）")
    print(f"  本次候选引脚（已排除电机引脚）：{PROBE_PINS}")
    print("  提示：确保 platform 服务已停止（避免 GPIO 冲突）。")
    print("═" * 60)

    try:
        input("\n  准备好后按 Enter 开始扫描...")
    except (EOFError, KeyboardInterrupt):
        return

    hit_pins: list[int] = []

    for pin in PROBE_PINS:
        motor_note = f"  ⚠ 注意：此引脚为电机引脚（{MOTOR_PINS[pin]}）！" if pin in MOTOR_PINS else ""
        print(f"\n  ── GPIO {pin:2d} ─────────────────────────────────")
        if motor_note:
            print(motor_note)
            print("     若看到车轮动，为正常现象，不算舵机响应。")
        print(f"  正在向 GPIO {pin} 发送舵机 PWM（50Hz）…")

        GPIO.setup(pin, GPIO.OUT)
        pwm = GPIO.PWM(pin, PWM_FREQ)

        pwm.start(_duty(90))
        time.sleep(0.4)
        print("  → 45°（左）")
        pwm.ChangeDutyCycle(_duty(45))
        time.sleep(0.7)
        print("  → 135°（右）")
        pwm.ChangeDutyCycle(_duty(135))
        time.sleep(0.7)
        print("  → 90°（中立）")
        pwm.ChangeDutyCycle(_duty(90))
        time.sleep(0.4)

        pwm.stop()
        GPIO.cleanup(pin)
        # cleanup 后重新设置 BCM 模式，防止 rpi-lgpio 重置 setmode
        try:
            GPIO.setmode(GPIO.BCM)
            GPIO.setwarnings(False)
        except Exception:
            pass

        try:
            print("  问：摄像头支架/云台有旋转吗？（不是车轮，是摄像头支架）")
            ans = input(f"  GPIO {pin:2d}：(y=云台转了 / n=没动 / q=退出) > ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            ans = "q"

        if ans == "q":
            print("  扫描中止。")
            break
        elif ans == "y":
            hit_pins.append(pin)
            print(f"  ✓ 记录：GPIO {pin} → 云台有旋转！")

    # 汇总
    print("\n" + "═" * 60)
    print("  【扫描结果汇总】")
    real_hits = [p for p in hit_pins if p not in MOTOR_PINS]
    if real_hits:
        for p in real_hits:
            print(f"  ✓ GPIO {p:2d}  → 云台舵机确认响应")
        if len(real_hits) == 1:
            p = real_hits[0]
            print(f"\n  → 请更新以下两处：")
            print(f"     servo_test.py  顶部：PAN_PIN = {p}")
            print(f"     servo.py       DEFAULT_CAMERA_CONFIG 中：pin={p}")
    else:
        print("  所有引脚均无云台旋转响应。")
        print()
        print("  ▶ 最可能的原因：舵机信号线没有插到树莓派 GPIO")
        print()
        print("  请按以下步骤检查物理接线：")
        print("  1. 找到 MAKEROBO 扩展板上标有「舵机」或「SERVO」的 3pin 接口")
        print("     （通常在板子一侧，有两排：VCC/GND/SIG）")
        print("  2. 确认舵机的 3 根线已插入：")
        print("     棕/黑 → GND")
        print("     红    → VCC（5V 或 3.3V，看扩展板标注）")
        print("     橙/黄/白 → SIG（信号线，必须接 GPIO）")
        print("  3. 若舵机已插入扩展板 SERVO 口但仍无响应：")
        print("     尝试换插第二个 SERVO 口（一个对应 GPIO12，另一个对应 GPIO13）")
        print("  4. 若扩展板没有 SERVO 标记接口：")
        print("     直接用杜邦线将舵机 SIG 线接到树莓派 GPIO 12（Pin 32）")
    print("═" * 60)


# ── 主菜单 ────────────────────────────────────────────────────────────

MENU = """
╔══════════════════════════════════════════════════╗
║       摄像头舵机探针 — 交互菜单                  ║
╠══════════════════════════════════════════════════╣
║  1. 水平 Pan  全幅扫描（0°→180°→70°）           ║
║  2. 水平 Pan  手动定位（起始 70°=正前）          ║
║  p. 引脚扫描（找不到舵机时用，扫描候选引脚）    ║
║  0. 水平轴归中（Pan 70°）                        ║
║  q. 退出                                        ║
╚══════════════════════════════════════════════════╝"""


def main():
    parser = argparse.ArgumentParser(description="摄像头舵机探针")
    parser.add_argument("--center", action="store_true",
                        help="水平轴归中后退出")
    parser.add_argument("--probe", action="store_true",
                        help="引脚扫描模式：逐一激活候选引脚确认舵机接线")
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
