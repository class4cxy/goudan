#!/usr/bin/env python3
"""
舵机探针 — 摄像头水平云台调试。

MAKEROBO 扩展板舵机接口（已实测确认）：
  GPIO 13 = 水平轴 Pan（左右旋转，Pin 33 / PWM1）

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

PAN_PIN    = 13   # 水平轴（实测确认：Pin 33 / GPIO 13 / PWM1）
PWM_FREQ   = 50   # Hz，舵机标准频率
DUTY_MIN   = 2.5  # % → 0°
DUTY_MID   = 7.5  # % → 90°
DUTY_MAX   = 12.5 # % → 180°
STEP_DELAY = 0.017 # 秒/度，平滑扫描每步延迟（≈ 60°/s，与 servo.py speed_deg_per_s 一致）

# ── 实测物理标定值 ────────────────────────────────────────────────────
# 注意：此脚本直接操作物理 PWM 角度，与 Platform API 的「逻辑角度」不同。
# Platform 层：Pan invert=True → physical = min + max - logical
# Pan 逻辑 110° ↔ 物理 70°（待重新校准，当前为旧值）
PAN_CENTER  = 97.0  # 实测正前方（已校准）；全幅扫描范围：0°–180°

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
    """水平轴全幅扫描：0° → 180° → PAN_CENTER（正前）。"""
    print(f"\n  全幅扫描（GPIO {PAN_PIN}）：0° → 180° → {PAN_CENTER:.0f}°（正前）")
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
    print(f"\n  水平 Pan 手动定位（GPIO {PAN_PIN}）")
    print(f"  输入：角度数字（0–180）｜< 最左端 ｜> 最右端 ｜c 归中 ｜q 退出")
    pwm = _make_pwm(PAN_PIN, PAN_CENTER)
    _run_manual(pwm, 0.0, 180.0, PAN_CENTER)
    pwm.stop()
    GPIO.cleanup(PAN_PIN)


def calibrate_limits():
    """极限校准模式：直接输入占空比（%），找到舵机物理边界。

    部分舵机物理极限超出标准 2.5–12.5% 范围，此模式绕过角度换算，
    允许在 0.5–15.0% 之间自由调节，帮助找到真实的左右机械止点。
    找到极限值后可据此更新 DUTY_MIN / DUTY_MAX 常量。
    """
    print(f"\n  占空比极限校准（GPIO {PAN_PIN}）")
    print(f"  标准范围：{DUTY_MIN}%（0°）~ {DUTY_MAX}%（180°）")
    print(f"  允许输入：0.5% – 15.0%（超出标准范围的值可能碰到机械止点）")
    print(f"  输入：占空比数字（如 2.0）｜< 下限 0.5% ｜> 上限 15.0% ｜q 退出")

    GPIO.setup(PAN_PIN, GPIO.OUT)
    pwm = GPIO.PWM(PAN_PIN, PWM_FREQ)
    duty_start = _duty(PAN_CENTER)
    pwm.start(duty_start)
    time.sleep(0.3)
    current_duty = duty_start

    def _duty_to_angle(d: float) -> float:
        return (d - DUTY_MIN) / (DUTY_MAX - DUTY_MIN) * 180.0

    print(f"  当前占空比 {current_duty:.2f}%（≈ {_duty_to_angle(current_duty):.1f}°）\n")

    while True:
        try:
            raw = input(f"  {current_duty:.2f}% > ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            break
        if raw in ("q", "quit"):
            break
        if raw == "<":
            target = 0.5
        elif raw == ">":
            target = 15.0
        else:
            try:
                target = float(raw)
            except ValueError:
                print("  请输入占空比数值（如 2.5）或 < / > / q")
                continue

        target = max(0.5, min(15.0, target))
        pwm.ChangeDutyCycle(target)
        current_duty = target
        angle_equiv = _duty_to_angle(current_duty)
        print(f"  → {current_duty:.2f}%  （等效角度 {angle_equiv:.1f}°，标准范围外时仅供参考）")

    pwm.stop()
    GPIO.cleanup(PAN_PIN)
    print(f"\n  如需更新极限值，修改 servo_test.py 顶部：")
    print(f"    DUTY_MIN = <左端占空比>   # 对应 0°")
    print(f"    DUTY_MAX = <右端占空比>   # 对应 180°")
    print(f"  并同步修改 servo.py 中的 _DUTY_MIN / _DUTY_MAX。")


def _run_manual(pwm, hard_min: float, hard_max: float, start_angle: float = 90.0):
    """通用手动角度控制循环。

    输入：
      数字      — 绝对角度（钳位到 hard_min–hard_max）
      < 或 min  — 跳到最小值
      > 或 max  — 跳到最大值
      c 或 ctr  — 归中（PAN_CENTER）
      q         — 退出
    """
    current = start_angle
    while True:
        try:
            raw = input(f"  {current:.1f}° > ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            break
        if raw in ("q", "quit"):
            break
        if raw in ("<", "min"):
            angle = hard_min
        elif raw in (">", "max"):
            angle = hard_max
        elif raw in ("c", "ctr", "center"):
            angle = PAN_CENTER
        else:
            try:
                angle = float(raw)
            except ValueError:
                print("  请输入角度数字，或 < (最左) / > (最右) / c (归中) / q (退出)")
                continue
        angle = max(hard_min, min(hard_max, angle))
        _smooth_move(pwm, current, angle, step=2.0)
        current = angle
        print(f"  → {current:.1f}°  (占空比 {_duty(current):.2f}%)")


# ── 引脚扫描探针 ──────────────────────────────────────────────────────
#
# 树莓派 5 BCM → 物理引脚对照 + 当前项目占用情况
# 格式：BCM: (物理Pin, 说明, 是否安全测试)
#
#   安全等级：
#     "servo"  — 预期舵机引脚，首先测试
#     "free"   — 未使用/规划编码器，可安全测试
#     "motor"  — 电机引脚，测试时车轮会抖动（仍要测，但提醒用户区分）
#     "skip"   — 系统/特殊用途，跳过（I2C/SPI内核/蜂鸣器/超声波）
#
PIN_MAP: dict[int, tuple[int, str, str]] = {
     2: ( 3, "I2C SDA (INA219)",             "skip"),
     3: ( 5, "I2C SCL (INA219)",             "skip"),
     4: ( 7, "空闲 / 规划左后编码A",          "free"),
     5: (29, "电机 M3-IN1 左后正转",          "motor"),
     6: (31, "电机 M3-IN2 左后反转",          "motor"),
     7: (26, "SPI CE1 内核占用",              "skip"),
     8: (24, "SPI CE0 内核占用",              "skip"),
     9: (21, "电机 M4-IN2 右后反转",          "motor"),
    10: (19, "扩展板RGB灯SPI数据(禁用)",       "skip"),
    11: (23, "空闲 / 规划左后编码B",          "free"),
    12: (32, "PWM0 空闲（原规划Pan，实测非舵机）", "free"),
    13: (33, "舵机Pan槽位 (PWM1)【实测确认】",   "servo"),
    14: ( 8, "UART TX / 规划右前编码A",       "free"),
    15: (10, "UART RX (空闲)",               "free"),
    16: (36, "规划左前编码B",                 "free"),
    17: (11, "板载蜂鸣器 (实测确认)",          "skip"),
    18: (12, "板载蜂鸣器/右前编码B (禁用)",    "skip"),
    19: (35, "规划右后编码A",                 "free"),
    20: (38, "超声波 Trig",                  "skip"),
    21: (40, "超声波 Echo",                  "skip"),
    22: (15, "电机 M4-IN1 右后正转",          "motor"),
    23: (16, "规划左前编码A",                 "free"),
    24: (18, "电机 M1-IN1 左前正转",          "motor"),
    25: (22, "电机 M1-IN2 左前反转",          "motor"),
    26: (37, "电机 M2-IN2 右前反转",          "motor"),
    27: (13, "电机 M2-IN1 右前正转",          "motor"),
}

# 扫描顺序：servo → free → motor（从最可能到最不可能）
_SCAN_ORDER = (
    [bcm for bcm, (_, _, t) in PIN_MAP.items() if t == "servo"] +
    [bcm for bcm, (_, _, t) in PIN_MAP.items() if t == "free"] +
    [bcm for bcm, (_, _, t) in PIN_MAP.items() if t == "motor"]
)


def _pwm_sweep_pin(pin: int, hold_s: float = 0.8) -> None:
    """
    对指定引脚输出 50Hz 舵机 PWM 摆动序列。
    不调用 GPIO.cleanup()，避免 rpi-lgpio 在 RPi5 上重置 BCM 模式。
    """
    GPIO.setup(pin, GPIO.OUT)
    pwm = GPIO.PWM(pin, PWM_FREQ)
    pwm.start(_duty(90))
    time.sleep(hold_s * 0.4)
    pwm.ChangeDutyCycle(_duty(45))
    time.sleep(hold_s)
    pwm.ChangeDutyCycle(_duty(135))
    time.sleep(hold_s)
    pwm.ChangeDutyCycle(_duty(90))
    time.sleep(hold_s * 0.4)
    pwm.stop()


def probe_single_pin(pin: int) -> None:
    """对单个引脚持续慢摆 12 秒，用于热插拔诊断。"""
    phys = PIN_MAP.get(pin, (0, "未知", "free"))[0]
    desc = PIN_MAP.get(pin, (0, "未知", "free"))[1]
    print(f"\n  GPIO {pin} (Pin {phys})  {desc}")
    print("  持续输出 50Hz 舵机 PWM，来回慢摆 12 秒…")
    print("  现在把舵机信号线（橙/黄色）接到树莓派 Pin 33（GPIO 13）或其他引脚，")
    print("  看哪个位置让舵机动起来。")
    GPIO.setup(pin, GPIO.OUT)
    pwm = GPIO.PWM(pin, PWM_FREQ)
    pwm.start(_duty(90))
    deadline = time.time() + 12.0
    angle, direction = 90.0, 0.3
    while time.time() < deadline:
        angle += direction
        if angle >= 130:
            direction = -0.3
        elif angle <= 50:
            direction = 0.3
        pwm.ChangeDutyCycle(_duty(angle))
        time.sleep(0.02)
    pwm.stop()
    print("  完成。")


def probe_servo_pin() -> None:
    """
    全量引脚扫描：遍历所有可用 GPIO（按 servo→free→motor 顺序），
    输出 50Hz 舵机 PWM 摆动，确认哪个引脚实际连接了舵机。

    关键改进（v3）：
    - 不调用 GPIO.cleanup() 防止 rpi-lgpio 重置 BCM 模式
    - 扫描所有之前未测试的引脚（GPIO 4/11/14/15/23 等）
    - 电机引脚也纳入（带提醒），彻底排除遗漏
    - 每个引脚显示物理 Pin 编号，方便对照接线
    """
    print("\n" + "═" * 64)
    print("  舵机引脚全量扫描 v3")
    print("  判断标准：")
    print("    ✓ 有效 = 摄像头支架/云台旋转（或听到舵机嗡嗡声）")
    print("    ✗ 无效 = 车轮/电机抖动（不算，明确区分）")
    print("  注意：测试电机引脚时车轮会动，属正常现象，请只关注舵机。")
    print(f"  扫描数量：{len(_SCAN_ORDER)} 个引脚")
    print("  确认 platform 服务已停止，否则 GPIO 冲突会干扰结果。")
    print("═" * 64)

    # 打印引脚分组预览
    servo_pins = [p for p in _SCAN_ORDER if PIN_MAP[p][2] == "servo"]
    free_pins  = [p for p in _SCAN_ORDER if PIN_MAP[p][2] == "free"]
    motor_pins = [p for p in _SCAN_ORDER if PIN_MAP[p][2] == "motor"]
    print(f"\n  【舵机槽位】{servo_pins}")
    print(f"  【空闲引脚】{free_pins}")
    print(f"  【电机引脚】{motor_pins}（车轮会动，仍需测）")

    try:
        input("\n  准备好后按 Enter 开始扫描…")
    except (EOFError, KeyboardInterrupt):
        return

    hit_pins: list[int] = []

    for pin in _SCAN_ORDER:
        phys, desc, kind = PIN_MAP[pin]
        prefix = "⚠️ " if kind == "motor" else "   "
        print(f"\n  ── GPIO {pin:2d}  Pin {phys:2d}  {prefix}{desc} ──")
        if kind == "motor":
            print("     此为电机引脚，车轮可能会抖动，请只看云台是否转。")

        _pwm_sweep_pin(pin, hold_s=0.9)

        try:
            ans = input("  云台有转动？(y=有/n=没有/q=退出) > ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            ans = "q"

        if ans == "q":
            print("  扫描中止。")
            break
        elif ans == "y":
            hit_pins.append(pin)
            print(f"  ✓ GPIO {pin} (Pin {phys}) → 云台响应！")

    # ── 汇总 ──────────────────────────────────────────────────────────
    print("\n" + "═" * 64)
    print("  【扫描结果汇总】")
    if hit_pins:
        print()
        for p in hit_pins:
            phys, desc, kind = PIN_MAP[p]
            tag = "（电机引脚，可能是误报）" if kind == "motor" else ""
            print(f"  ✓ GPIO {p:2d}  Pin {phys:2d}  {desc} {tag}")
        real = [p for p in hit_pins if PIN_MAP[p][2] != "motor"]
        if real:
            p = real[0]
            phys = PIN_MAP[p][0]
            print(f"\n  → 确认舵机连接在 GPIO {p}（物理 Pin {phys}）")
            print(f"    请更新以下两处：")
            print(f"      servo_test.py 顶部：PAN_PIN = {p}")
            print(f"      servo.py DEFAULT_CAMERA_CONFIG：pin={p}")
        else:
            print("\n  所有响应引脚均为电机引脚，可能是车轮抖动误报。")
            print("  请重新扫描，仅在看到摄像头支架旋转时按 y。")
    else:
        print()
        print("  所有引脚均无云台响应。")
        print()
        print("  ▶ 最终诊断步骤：确认舵机本身是否正常")
        print()
        print("  1. 用万用表或 LED 测试舵机电源线（红线=VCC，棕/黑=GND）是否有电压")
        print("  2. 手动旋转云台时能感受到阻力吗？")
        print("     有阻力  → 舵机有电但信号线未接通，继续排查 SIG 线")
        print("     无阻力  → 舵机没有通电，检查 VCC/GND 接线")
        print()
        print("  3. 运行热插拔诊断（对 GPIO 13 持续输出 12s PWM）：")
        print("        python3 servo_test.py --live 13")
        print("     在这 12 秒内，把舵机信号线（橙/黄色）挨个插到")
        print("     树莓派 40pin 排针，哪个引脚让云台动了就是正确引脚。")
    print("═" * 64)


# ── 主菜单 ────────────────────────────────────────────────────────────

MENU = f"""
╔══════════════════════════════════════════════════╗
║       摄像头舵机探针 — 交互菜单                  ║
╠══════════════════════════════════════════════════╣
║  1. 全幅扫描（0° → 180° → {PAN_CENTER:.0f}°）              ║
║  2. 手动角度定位（< 最左 / > 最右 / c 归中）    ║
║  3. 占空比极限校准（找真实物理边界）            ║
║  p. 引脚全量扫描（找不到舵机时用）              ║
║  0. 归中（Pan {PAN_CENTER:.0f}°=正前）                     ║
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
            elif choice == "3":
                calibrate_limits()
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
