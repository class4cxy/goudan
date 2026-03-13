"""
4轮电机测试脚本 — 适用于树莓派 + Makerobo 功能扩展板 / L298N 驱动板
=====================================================

接线说明（使用 BCM GPIO 编号）：

  Makerobo 扩展板接口          对应电机位置
  ┌──────┬──────────────────┐
  │  M1  │ 左前轮 (front_left)  │
  │  M2  │ 右前轮 (front_right) │
  │  M3  │ 左后轮 (rear_left)   │
  │  M4  │ 右后轮 (rear_right)  │
  └──────┴──────────────────┘

  电机驱动引脚（BCM 编号，GPIO 探针 Phase A 实测）：
  M1: IN1=24  IN2=25  （无外部 EN，驱动芯片 EN 已内部接高电平）
  M2: IN1=27  IN2=26
  M3: IN1=5   IN2=6
  M4: IN1=22  IN2=9   （GPIO9 = SPI_MISO 复用，全部引脚已实测确认）

  注意：该板使用 SW-6008 驱动芯片，EN 引脚内部已接 3.3V/5V，
        速度控制通过对 IN1/IN2 引脚直接 PWM 实现（非 EN 引脚 PWM）。

用法：
  python motor_test.py             # 交互菜单模式
  python motor_test.py --all       # 自动运行全部测试
  python motor_test.py --motor 1   # 只测试指定编号电机 (1-4)
  python motor_test.py --verify    # 逐口目视验证 M1~M4 接线是否正确

注意：必须以 root 或 gpio 组身份运行才能访问 GPIO。
"""

import argparse
import time

# ── GPIO 引脚配置（BCM 编号，根据实际接线修改）──────────────────────
# 如果只有一块 L298N，后轮引脚与前轮相同，改为同一组引脚即可
MOTOR_PINS: dict[str, dict[str, int]] = {
    "front_left": {
        "in1": 24,   # 方向控制 A（正转）— Phase A 探针确认
        "in2": 25,   # 方向控制 B（反转）— Phase A 探针确认
        "en":  -1,   # 无外部 EN 引脚（SW-6008 驱动，EN 内部已接高电平）
    },
    "front_right": {
        "in1": 27,   # 方向控制 A（正转）— Phase A 探针确认
        "in2": 26,   # 方向控制 B（反转）— Phase A 探针确认
        "en":  -1,
    },
    "rear_left": {
        "in1": 5,    # 方向控制 A（正转）— Phase A 探针确认
        "in2": 6,    # 方向控制 B（反转）— Phase A 探针确认
        "en":  -1,
    },
    "rear_right": {
        "in1": 22,   # 方向控制 A（正转）— Phase A 探针确认
        "in2": 9,    # 方向控制 B（反转）— 实测确认（SPI_MISO 复用为 GPIO）
        "en":  -1,
    },
}

MOTOR_NAMES = {
    1: "front_left",
    2: "front_right",
    3: "rear_left",
    4: "rear_right",
}

MOTOR_LABELS = {
    "front_left":  "前左 (Motor 1)",
    "front_right": "前右 (Motor 2)",
    "rear_left":   "后左 (Motor 3)",
    "rear_right":  "后右 (Motor 4)",
}

PWM_FREQ = 1000   # Hz，PWM 频率
TEST_SPEED = 60   # %，测试速度（0–100）
TEST_DURATION = 1.5  # 秒，每步持续时间

# ── GPIO 初始化 ───────────────────────────────────────────────────
try:
    import RPi.GPIO as GPIO
    SIMULATION = False
except (ImportError, RuntimeError):
    print("⚠️  未检测到 RPi.GPIO，进入模拟模式（不会操作真实引脚）")
    SIMULATION = True

    class _FakeGPIO:
        BCM = "BCM"
        OUT = "OUT"

        def setmode(self, *a): pass
        def setwarnings(self, *a): pass
        def setup(self, *a, **kw): pass
        def output(self, *a): pass
        def cleanup(self): pass

        class PWM:
            def __init__(self, pin, freq): pass
            def start(self, dc): print(f"    [SIM] PWM start duty={dc}%")
            def ChangeDutyCycle(self, dc): print(f"    [SIM] PWM duty={dc}%")
            def stop(self): print("    [SIM] PWM stop")

    GPIO = _FakeGPIO()  # type: ignore[assignment]


# ── 电机控制类 ────────────────────────────────────────────────────
class Motor:
    """单个 DC 电机控制器（H 桥一路）。

    支持两种模式：
      en >= 0 → 传统 L298N 模式：in1/in2 控制方向，en 引脚 PWM 控制速度
      en = -1 → 直接 IN PWM 模式：对 in1/in2 直接 PWM，无需外部 EN 引脚
                （MAKEROBO SW-6008 扩展板使用此模式）
    """

    def __init__(self, name: str, pins: dict[str, int]):
        self.name = name
        self.label = MOTOR_LABELS[name]
        self._in1 = pins["in1"]
        self._in2 = pins["in2"]
        self._en  = pins["en"]          # -1 表示无外部 EN
        self._pwm     = None            # EN 模式 PWM
        self._pwm_fwd = None            # 直接 IN 模式：正转 PWM（在 in1）
        self._pwm_bwd = None            # 直接 IN 模式：反转 PWM（在 in2）

    @property
    def _direct(self) -> bool:
        return self._en < 0

    def setup(self):
        GPIO.setup(self._in1, GPIO.OUT)
        GPIO.setup(self._in2, GPIO.OUT)
        GPIO.output(self._in1, False)
        GPIO.output(self._in2, False)
        if self._direct:
            self._pwm_fwd = GPIO.PWM(self._in1, PWM_FREQ)
            self._pwm_bwd = GPIO.PWM(self._in2, PWM_FREQ)
            self._pwm_fwd.start(0)
            self._pwm_bwd.start(0)
        else:
            GPIO.setup(self._en, GPIO.OUT)
            self._pwm = GPIO.PWM(self._en, PWM_FREQ)
            self._pwm.start(0)

    def forward(self, speed: int = TEST_SPEED):
        if self._direct:
            self._pwm_bwd.ChangeDutyCycle(0)
            GPIO.output(self._in2, False)
            self._pwm_fwd.ChangeDutyCycle(speed)
        else:
            GPIO.output(self._in1, True)
            GPIO.output(self._in2, False)
            self._pwm.ChangeDutyCycle(speed)

    def backward(self, speed: int = TEST_SPEED):
        if self._direct:
            self._pwm_fwd.ChangeDutyCycle(0)
            GPIO.output(self._in1, False)
            self._pwm_bwd.ChangeDutyCycle(speed)
        else:
            GPIO.output(self._in1, False)
            GPIO.output(self._in2, True)
            self._pwm.ChangeDutyCycle(speed)

    def stop(self):
        if self._direct:
            self._pwm_fwd.ChangeDutyCycle(0)
            self._pwm_bwd.ChangeDutyCycle(0)
            GPIO.output(self._in1, False)
            GPIO.output(self._in2, False)
        else:
            GPIO.output(self._in1, False)
            GPIO.output(self._in2, False)
            self._pwm.ChangeDutyCycle(0)

    def cleanup(self):
        if self._direct:
            if self._pwm_fwd:
                self._pwm_fwd.stop()
            if self._pwm_bwd:
                self._pwm_bwd.stop()
        else:
            if self._pwm:
                self._pwm.stop()


class CarController:
    """4轮小车控制器。"""

    def __init__(self):
        GPIO.setmode(GPIO.BCM)
        GPIO.setwarnings(False)
        self.motors: dict[str, Motor] = {
            name: Motor(name, pins)
            for name, pins in MOTOR_PINS.items()
        }
        for m in self.motors.values():
            m.setup()

    # ── 单电机操作 ────────────────────────────────────────────────
    def motor_forward(self, name: str, speed: int = TEST_SPEED):
        self.motors[name].forward(speed)

    def motor_backward(self, name: str, speed: int = TEST_SPEED):
        self.motors[name].backward(speed)

    def motor_stop(self, name: str):
        self.motors[name].stop()

    # ── 整车动作 ──────────────────────────────────────────────────
    def all_forward(self, speed: int = TEST_SPEED):
        for m in self.motors.values():
            m.forward(speed)

    def all_backward(self, speed: int = TEST_SPEED):
        for m in self.motors.values():
            m.backward(speed)

    def turn_left(self, speed: int = TEST_SPEED):
        """左转：右侧轮前进，左侧轮后退。"""
        self.motors["front_left"].backward(speed)
        self.motors["rear_left"].backward(speed)
        self.motors["front_right"].forward(speed)
        self.motors["rear_right"].forward(speed)

    def turn_right(self, speed: int = TEST_SPEED):
        """右转：左侧轮前进，右侧轮后退。"""
        self.motors["front_left"].forward(speed)
        self.motors["rear_left"].forward(speed)
        self.motors["front_right"].backward(speed)
        self.motors["rear_right"].backward(speed)

    def stop_all(self):
        for m in self.motors.values():
            m.stop()

    def cleanup(self):
        self.stop_all()
        for m in self.motors.values():
            m.cleanup()
        GPIO.cleanup()


# ── 测试用例 ──────────────────────────────────────────────────────
def _step(label: str, action, duration: float = TEST_DURATION):
    print(f"  ▶ {label} ({duration:.1f}s)...", end="", flush=True)
    action()
    time.sleep(duration)
    print(" ✓")


def test_single_motor(car: CarController, name: str):
    """逐步测试单个电机：正转 → 停止 → 反转 → 停止。"""
    label = MOTOR_LABELS[name]
    print(f"\n{'─'*50}")
    print(f"  测试电机：{label}")
    print(f"{'─'*50}")
    _step("正转", lambda: car.motor_forward(name))
    _step("停止", lambda: car.motor_stop(name), 0.5)
    _step("反转", lambda: car.motor_backward(name))
    _step("停止", lambda: car.motor_stop(name), 0.5)
    print(f"  ✅ {label} 测试完成")


def test_all_motors_individually(car: CarController):
    """逐一测试 4 个电机。"""
    print("\n" + "═"*50)
    print("  【测试 1】逐一测试各电机")
    print("═"*50)
    for name in MOTOR_NAMES.values():
        test_single_motor(car, name)


def test_speed_sweep(car: CarController):
    """测试速度渐变：0% → 100% → 0%，验证 PWM 调速。"""
    print("\n" + "═"*50)
    print("  【测试 2】速度渐变（全轮同步）")
    print("═"*50)
    print("  ▶ 加速 0 → 100%...", end="", flush=True)
    for speed in range(0, 101, 10):
        car.all_forward(speed)
        time.sleep(0.1)
    print(" ✓")

    print("  ▶ 减速 100 → 0%...", end="", flush=True)
    for speed in range(100, -1, -10):
        car.all_forward(speed)
        time.sleep(0.1)
    print(" ✓")
    car.stop_all()
    print("  ✅ 速度渐变测试完成")


def test_movement_patterns(car: CarController):
    """测试整车运动模式：前进 → 后退 → 左转 → 右转。"""
    print("\n" + "═"*50)
    print("  【测试 3】整车运动模式")
    print("═"*50)
    _step("全速前进", car.all_forward)
    _step("停止",     car.stop_all, 0.5)
    _step("全速后退", car.all_backward)
    _step("停止",     car.stop_all, 0.5)
    _step("原地左转", car.turn_left)
    _step("停止",     car.stop_all, 0.5)
    _step("原地右转", car.turn_right)
    _step("停止",     car.stop_all, 0.5)
    print("  ✅ 运动模式测试完成")


def test_all(car: CarController):
    """运行全部测试。"""
    test_all_motors_individually(car)
    test_speed_sweep(car)
    test_movement_patterns(car)
    print("\n" + "🎉 全部测试完成！\n")


# ── 接线验证模式 ───────────────────────────────────────────────────
_WHEEL_OPTIONS = {
    "1": "左前轮 (Front-Left)",
    "2": "右前轮 (Front-Right)",
    "3": "左后轮 (Rear-Left)",
    "4": "右后轮 (Rear-Right)",
    "0": "没有轮子转动 / 无法判断",
}

# M1~M4 的预期位置（按扩展板丝印顺序）
_PORT_EXPECTED = [
    ("M1", "front_left",  "左前轮"),
    ("M2", "front_right", "右前轮"),
    ("M3", "rear_left",   "左后轮"),
    ("M4", "rear_right",  "右后轮"),
]


def verify_motor_mapping(car: CarController):
    """
    逐口目视验证：依次点动 M1~M4，让用户观察哪个轮子在转，
    最终打印实际接线映射表，与预期对比。
    """
    print("\n" + "═" * 56)
    print("  【接线验证模式】逐口目视确认 M1~M4 对应哪个轮子")
    print("  提示：每个接口会正转 1.5 秒，请目视观察哪个轮子在转。")
    print("═" * 56)

    results: list[tuple[str, str, str, str]] = []  # (port, expected_key, expected_label, actual_label)

    for port, expected_key, expected_label in _PORT_EXPECTED:
        print(f"\n  ──────────────────────────────────────────────")
        print(f"  正在点动接口 [{port}]（预期：{expected_label}）")
        print(f"  ──────────────────────────────────────────────")

        # 点动：正转 1.5s → 停止
        car.motor_forward(expected_key)
        time.sleep(1.5)
        car.motor_stop(expected_key)
        time.sleep(0.3)

        # 询问用户观察结果
        print("  请问刚才哪个轮子转动了？")
        for k, v in _WHEEL_OPTIONS.items():
            print(f"    {k} → {v}")

        while True:
            try:
                ans = input("  请输入编号 > ").strip()
            except (EOFError, KeyboardInterrupt):
                print("\n  验证被中断。")
                return
            if ans in _WHEEL_OPTIONS:
                break
            print("  无效输入，请输入 0~4")

        actual_label = _WHEEL_OPTIONS[ans]
        results.append((port, expected_key, expected_label, actual_label))
        print(f"  已记录：[{port}] → {actual_label}")

    # 汇总报告
    print("\n" + "═" * 56)
    print("  【验证结果汇总】")
    print("═" * 56)
    print(f"  {'接口':<6} {'预期轮位':<18} {'实际轮位':<24} {'结论'}")
    print(f"  {'─'*6} {'─'*18} {'─'*24} {'─'*4}")

    all_pass = True
    for port, _, expected_label, actual_label in results:
        match = expected_label in actual_label
        status = "✅ 正确" if match else "❌ 不符"
        if not match:
            all_pass = False
        print(f"  {port:<6} {expected_label:<18} {actual_label:<24} {status}")

    print("═" * 56)
    if all_pass:
        print("  结论：全部接线正确！M1=左前 M2=右前 M3=左后 M4=右后")
    else:
        print("  结论：存在接线不符，请根据上表调整电机接口或修改 MOTOR_PINS 配置。")
    print()


# ── 交互菜单 ──────────────────────────────────────────────────────
def interactive_menu(car: CarController):
    menu = """
╔══════════════════════════════════════╗
║      4轮电机测试 — 交互菜单          ║
╠══════════════════════════════════════╣
║  1. 测试前左电机 M1 (Front-Left)     ║
║  2. 测试前右电机 M2 (Front-Right)    ║
║  3. 测试后左电机 M3 (Rear-Left)      ║
║  4. 测试后右电机 M4 (Rear-Right)     ║
║  5. 逐一测试全部电机                 ║
║  6. 速度渐变测试                     ║
║  7. 整车运动模式测试                 ║
║  8. 运行全部测试                     ║
║  v. 接线验证（目视确认 M1~M4 位置）  ║
║  q. 退出                             ║
╚══════════════════════════════════════╝"""
    while True:
        print(menu)
        try:
            choice = input("请选择 > ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            break

        if choice == "q":
            break
        elif choice in ("1", "2", "3", "4"):
            test_single_motor(car, MOTOR_NAMES[int(choice)])
        elif choice == "5":
            test_all_motors_individually(car)
        elif choice == "6":
            test_speed_sweep(car)
        elif choice == "7":
            test_movement_patterns(car)
        elif choice == "8":
            test_all(car)
        elif choice == "v":
            verify_motor_mapping(car)
        else:
            print("  无效选项，请重新输入")

        car.stop_all()

    car.stop_all()


# ── 主入口 ────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="4轮电机测试脚本")
    parser.add_argument("--all", action="store_true", help="自动运行全部测试")
    parser.add_argument("--motor", type=int, choices=[1, 2, 3, 4],
                        help="只测试指定编号电机 (1=M1左前 2=M2右前 3=M3左后 4=M4右后)")
    parser.add_argument("--verify", action="store_true",
                        help="逐口目视验证模式：依次点动 M1~M4，确认接线是否正确")
    args = parser.parse_args()

    mode = "模拟" if SIMULATION else "真实 GPIO"
    print(f"\n4轮电机测试脚本 — 运行模式：{mode}")
    print(f"测试速度：{TEST_SPEED}%  |  步骤时长：{TEST_DURATION}s")
    print(f"GPIO 引脚（BCM）：{MOTOR_PINS}\n")

    car = CarController()
    try:
        if args.verify:
            verify_motor_mapping(car)
        elif args.motor:
            test_single_motor(car, MOTOR_NAMES[args.motor])
        elif args.all:
            test_all(car)
        else:
            interactive_menu(car)
    except KeyboardInterrupt:
        print("\n\n⚠️  用户中断，正在停止电机并清理 GPIO...")
    finally:
        car.cleanup()
        print("GPIO 已清理，程序退出。")


if __name__ == "__main__":
    main()
