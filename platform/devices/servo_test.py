#!/usr/bin/env python3
"""
舵机探针 — 摄像头云台调试与垂直限位校准。

MAKEROBO 扩展板舵机接口（已实测确认）：
  GPIO 12 = 水平轴 Pan（左右旋转）
  GPIO 13 = 垂直轴 Tilt（上下俯仰，有硬件遮挡，需校准安全范围）

舵机 PWM 参数（SG90 / MG90S）：
  频率：50 Hz（周期 20ms）
  占空比  2.5% → 0°   （脉宽 0.5ms）
  占空比  7.5% → 90°  （脉宽 1.5ms，中立/正前方）
  占空比 12.5% → 180° （脉宽 2.5ms）

用法：
  python3 probe_servo.py              # 交互菜单
  python3 probe_servo.py --calibrate  # 直接进入垂直限位校准
  python3 probe_servo.py --center     # 双轴归中后退出
"""

import argparse
import time

PAN_PIN    = 12   # 水平轴
TILT_PIN   = 13   # 垂直轴
PWM_FREQ   = 50   # Hz，舵机标准频率
DUTY_MIN   = 2.5  # % → 0°
DUTY_MID   = 7.5  # % → 90°
DUTY_MAX   = 12.5 # % → 180°
STEP_DELAY = 0.02 # 秒，平滑扫描每步延迟

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

def center_all():
    """双轴归中到 90°。"""
    print("  双轴归中（90°）...")
    for pin in (PAN_PIN, TILT_PIN):
        GPIO.setup(pin, GPIO.OUT)
        pwm = GPIO.PWM(pin, PWM_FREQ)
        pwm.start(_duty(90))
        time.sleep(0.5)
        pwm.stop()
        GPIO.cleanup(pin)
    print("  完成。")


def sweep_pan():
    """水平轴全幅扫描：0° → 180° → 90°。"""
    print(f"\n  水平 Pan（GPIO {PAN_PIN}）扫描：0° → 180° → 90°")
    pwm = _make_pwm(PAN_PIN)
    time.sleep(0.3)
    _smooth_move(pwm, 90, 0)
    time.sleep(0.4)
    _smooth_move(pwm, 0, 180)
    time.sleep(0.4)
    _smooth_move(pwm, 180, 90)
    pwm.stop()
    GPIO.cleanup(PAN_PIN)
    print("  扫描完成，已归中。")


def sweep_tilt_safe(min_angle: float = 75.0, max_angle: float = 105.0):
    """垂直轴在安全范围内扫描：min → max → 90°。"""
    print(f"\n  垂直 Tilt（GPIO {TILT_PIN}）安全范围扫描：{min_angle:.0f}° → {max_angle:.0f}° → 90°")
    pwm = _make_pwm(TILT_PIN)
    time.sleep(0.3)
    _smooth_move(pwm, 90, min_angle)
    time.sleep(0.4)
    _smooth_move(pwm, min_angle, max_angle)
    time.sleep(0.4)
    _smooth_move(pwm, max_angle, 90)
    pwm.stop()
    GPIO.cleanup(TILT_PIN)
    print("  扫描完成，已归中。")


def manual_pan():
    """手动输入角度控制水平轴。"""
    print(f"\n  水平 Pan 手动定位（GPIO {PAN_PIN}）— 输入角度 0–180，q 退出")
    pwm = _make_pwm(PAN_PIN)
    _run_manual(pwm, 0.0, 180.0)
    pwm.stop()
    GPIO.cleanup(PAN_PIN)


def manual_tilt():
    """手动输入角度控制垂直轴（不做范围限制，用于校准）。"""
    print(f"\n  垂直 Tilt 手动定位（GPIO {TILT_PIN}）— 输入角度 0–180，q 退出")
    print("  提示：此模式不限位，请小心不要磕碰遮挡物")
    pwm = _make_pwm(TILT_PIN)
    _run_manual(pwm, 0.0, 180.0)
    pwm.stop()
    GPIO.cleanup(TILT_PIN)


def _run_manual(pwm, hard_min: float, hard_max: float):
    """通用手动角度控制循环。"""
    current = 90.0
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


# ── 垂直限位校准 ──────────────────────────────────────────────────────

def calibrate_tilt():
    """
    逐步移动垂直舵机，找出上下两侧的安全边界角度。

    流程：
      1. 从 90°（水平正视）开始，每按一次 Enter 向下移动 STEP 度
      2. 感觉到机械遮挡时输入 s，记录下限
      3. 归回 90°，再向上探测，同样方式找上限
      4. 打印校准结果，给出 servo.py 的配置代码
    """
    STEP      = 3    # 每步移动度数（较小步更安全）
    PAUSE     = 0.15 # 每步移动后停顿（秒），让用户看清楚

    print("\n" + "═" * 60)
    print("  垂直轴（Tilt）限位校准")
    print("  从 90°（水平）开始，逐步向两侧探测安全边界。")
    print("  操作说明：")
    print(f"    按 Enter      → 继续移动（每步 {STEP}°）")
    print("    输入角度数字  → 跳转到指定角度")
    print("    输入 s        → 当前位置标记为边界，停止本方向探测")
    print("  ！请确保手扶机械结构，防止磕碰损坏！")
    print("═" * 60)

    input("\n  准备好后按 Enter 开始...")

    pwm = _make_pwm(TILT_PIN, init_angle=90.0)
    time.sleep(0.5)

    # ── Phase 1：向下探测（角度减小）────────────────────────────────
    print("\n  ── Phase 1：向下探测（90° → 减小）──────────────")
    print("  说明：角度减小通常对应摄像头向下俯仰（取决于安装方向）")
    current   = 90.0
    min_angle = 0.0   # 最终记录的下限

    while current > 0:
        target = max(0.0, current - STEP)
        _smooth_move(pwm, current, target, step=1.0)
        current = target
        time.sleep(PAUSE)

        try:
            raw = input(f"  [{current:6.1f}°] Enter=继续  s=到边界了  数字=跳转 > ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            raw = "s"

        if raw == "s":
            # 留 2° 安全余量
            min_angle = current + 2.0
            print(f"  ✓ 下限记录：{min_angle:.1f}°（含 2° 安全余量）")
            break
        elif raw == "":
            continue
        else:
            try:
                jump = max(0.0, min(90.0, float(raw)))
                _smooth_move(pwm, current, jump, step=2.0)
                current = jump
            except ValueError:
                pass
    else:
        min_angle = current + 2.0
        print(f"  已到 0°，下限记录：{min_angle:.1f}°")

    # 归回 90°
    print(f"  归回 90°...")
    _smooth_move(pwm, current, 90.0, step=2.0)
    current = 90.0
    time.sleep(0.5)

    # ── Phase 2：向上探测（角度增大）────────────────────────────────
    print("\n  ── Phase 2：向上探测（90° → 增大）──────────────")
    print("  说明：角度增大通常对应摄像头向上仰起")
    max_angle = 180.0

    while current < 180:
        target = min(180.0, current + STEP)
        _smooth_move(pwm, current, target, step=1.0)
        current = target
        time.sleep(PAUSE)

        try:
            raw = input(f"  [{current:6.1f}°] Enter=继续  s=到边界了  数字=跳转 > ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            raw = "s"

        if raw == "s":
            max_angle = current - 2.0
            print(f"  ✓ 上限记录：{max_angle:.1f}°（含 2° 安全余量）")
            break
        elif raw == "":
            continue
        else:
            try:
                jump = max(90.0, min(180.0, float(raw)))
                _smooth_move(pwm, current, jump, step=2.0)
                current = jump
            except ValueError:
                pass
    else:
        max_angle = current - 2.0
        print(f"  已到 180°，上限记录：{max_angle:.1f}°")

    # 归中
    print(f"  归回 90°...")
    _smooth_move(pwm, current, 90.0, step=2.0)
    pwm.stop()
    GPIO.cleanup(TILT_PIN)

    # ── 校准报告 ─────────────────────────────────────────────────────
    total = max_angle - min_angle
    center = (min_angle + max_angle) / 2
    print("\n" + "═" * 60)
    print("  【校准结果】")
    print(f"  安全范围：{min_angle:.1f}° – {max_angle:.1f}°（共 {total:.1f}°）")
    print(f"  中心点  ：{center:.1f}°")
    print()
    print("  将以下配置更新到 platform/devices/servo.py 的 DEFAULT_CAMERA_CONFIG：")
    print()
    print(f"    tilt=ServoConfig(")
    print(f"        pin=13,")
    print(f"        min_angle={min_angle:.1f},")
    print(f"        max_angle={max_angle:.1f},")
    print(f"        default_angle=90.0,")
    print(f"    ),")
    print("═" * 60)


# ── 主菜单 ────────────────────────────────────────────────────────────

MENU = """
╔══════════════════════════════════════════════════╗
║       摄像头舵机探针 — 交互菜单                  ║
╠══════════════════════════════════════════════════╣
║  1. 水平 Pan  全幅扫描（0°→180°→90°）           ║
║  2. 垂直 Tilt 安全范围扫描（75°→105°→90°）      ║
║  3. 水平 Pan  手动定位                          ║
║  4. 垂直 Tilt 手动定位（无限位，校准用）         ║
║  c. 垂直 Tilt 限位校准（找上下安全边界）         ║
║  0. 双轴归中（90°）                             ║
║  q. 退出                                        ║
╚══════════════════════════════════════════════════╝"""


def main():
    parser = argparse.ArgumentParser(description="摄像头舵机探针")
    parser.add_argument("--calibrate", action="store_true",
                        help="直接进入垂直轴限位校准模式")
    parser.add_argument("--center", action="store_true",
                        help="双轴归中（90°）后退出")
    args = parser.parse_args()

    print("\n摄像头舵机探针 — MAKEROBO 扩展板")
    print(f"  水平 Pan  → GPIO {PAN_PIN}   垂直 Tilt → GPIO {TILT_PIN}")
    print(f"  PWM {PWM_FREQ}Hz  |  占空比 {DUTY_MIN}%–{DUTY_MAX}%（对应 0°–180°）")

    try:
        if args.center:
            center_all()
            return

        if args.calibrate:
            calibrate_tilt()
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
                sweep_tilt_safe()
            elif choice == "3":
                manual_pan()
            elif choice == "4":
                manual_tilt()
            elif choice == "c":
                calibrate_tilt()
            elif choice == "0":
                center_all()
            else:
                print("  无效选项")

    except KeyboardInterrupt:
        print("\n  中断...")
    finally:
        GPIO.cleanup()
        print("  GPIO 已清理，退出。")


if __name__ == "__main__":
    main()
