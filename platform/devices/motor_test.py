"""
4轮编码电机测试脚本 — 适用于树莓派 + Makerobo 功能扩展板
=====================================================

硬件：GMR 1:90 编码电机 × 4，PH2.0-6P 接口（接线见 docs/HARDWARE.md §2.1）

  Makerobo 扩展板接口          对应电机位置
  ┌──────┬──────────────────┐
  │  M1  │ 左前轮 (front_left)  │
  │  M2  │ 右前轮 (front_right) │
  │  M3  │ 左后轮 (rear_left)   │
  │  M4  │ 右后轮 (rear_right)  │
  └──────┴──────────────────┘

  电机驱动引脚（BCM）：M1(24,25) M2(27,26) M3(5,6) M4(22,9)
  编码器引脚（BCM）：
    左前 M1: A=23  B=16     右前 M2: A=18  B=17
    左后 M3: A=4   B=11     右后 M4: A=19  B=7
  编码器 VCC → 树莓派 3.3V（Pin 1/17），GND → GND

用法：
  python motor_test.py              # 交互菜单模式
  python motor_test.py --all        # 自动运行全部电机测试
  python motor_test.py --motor 1    # 只测试指定编号电机 (1-4)
  python motor_test.py --verify     # 逐口目视验证 M1~M4 接线是否正确
  python motor_test.py --encoder    # 自动运行全部编码器测试
  python motor_test.py --enc-verify # 逐路交互验证编码器接线

注意：必须以 root 或 gpio 组身份运行才能访问 GPIO。
      编码器测试需要 pigpio 守护进程：sudo pigpiod
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

# ── 编码器引脚配置（BCM，定稿见 docs/HARDWARE.md §7.1.1）────────────
# 每路编码器：pin_a = A相，pin_b = B相
# VCC → 树莓派 3.3V（Pin 1 或 Pin 17），GND → GND
ENCODER_PINS: dict[str, dict[str, int]] = {
    "front_left":  {"pin_a": 23, "pin_b": 16},  # M1 左前
    "front_right": {"pin_a": 18, "pin_b": 17},  # M2 右前
    "rear_left":   {"pin_a":  4, "pin_b": 11},  # M3 左后（里程计左轮）
    "rear_right":  {"pin_a": 19, "pin_b": 10},  # M4 右后（里程计右轮）GPIO7/8 被 spi0 CS 强占
}

# 正交解码状态转换表（4倍频）
_QUAD_TABLE: dict[tuple[int, int, int, int], int] = {
    (0, 0, 0, 1): +1, (0, 1, 1, 1): +1, (1, 1, 1, 0): +1, (1, 0, 0, 0): +1,
    (0, 0, 1, 0): -1, (1, 0, 1, 1): -1, (1, 1, 0, 1): -1, (0, 1, 0, 0): -1,
}

# 自动检测 40pin GPIO chip 编号（RPi 5 实测为 gpiochip0，描述含 pinctrl-rp1）
import os as _os
import subprocess as _subprocess

def _detect_gpio_chip() -> int:
    override = _os.environ.get("GPIO_CHIP_NUM")
    if override is not None:
        return int(override)
    try:
        out = _subprocess.check_output(["gpiodetect"], text=True, timeout=3)
        for line in out.splitlines():
            if "pinctrl-rp1" in line or ("pinctrl" in line and "brcmstb" not in line):
                return int(line.split()[0].replace("gpiochip", ""))
    except Exception:
        pass
    return 0

_CHIP_NUM = _detect_gpio_chip()

# ── lgpio 初始化（编码器后端，支持树莓派 5）────────────────────────────
try:
    import lgpio as _lgpio
    _h = _lgpio.gpiochip_open(_CHIP_NUM)
    ENC_SIMULATION = False
except Exception as _e:
    print(f"⚠️  lgpio 不可用，编码器将进入模拟模式：{_e}")
    print(f"   安装方法：sudo apt install -y python3-lgpio")
    _lgpio = None  # type: ignore[assignment]
    _h = None
    ENC_SIMULATION = True

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


# ── 编码器计数器 ──────────────────────────────────────────────────
class _EncoderCounter:
    """
    单路正交编码器计数器（lgpio 轮询线程，支持树莓派 5）。

    使用轮询线程代替 callback，避免 RPi.GPIO 与 lgpio 共享 gpiochip
    handle 时 callback 无法触发的问题。原始 GPIO read 已验证正常。
    """

    def __init__(self, h: int, pin_a: int, pin_b: int):
        self._h       = h
        self._pin_a   = pin_a
        self._pin_b   = pin_b
        self._ticks   = 0
        self._prev_a  = 0
        self._prev_b  = 0
        self._running = False
        self._thread  = None

    def start(self) -> bool:
        """
        启动编码器轮询线程。
        Returns True 成功，False 表示引脚被占用（如 SPI0 未禁用）。
        """
        import lgpio
        import threading
        for pin in (self._pin_a, self._pin_b):
            try:
                lgpio.gpio_free(self._h, pin)
            except Exception:
                pass
        for pin, name in ((self._pin_a, "A"), (self._pin_b, "B")):
            try:
                lgpio.gpio_claim_input(self._h, pin, lgpio.SET_PULL_UP)
            except Exception as e:
                print(f"\n  ⚠️  GPIO{pin}({name}相) 无法声明：{e}")
                print(f"      → 可能原因：SPI0 未禁用。请执行：")
                print(f"        sudo raspi-config → Interface Options → SPI → No → 重启")
                return False
        self._prev_a  = lgpio.gpio_read(self._h, self._pin_a)
        self._prev_b  = lgpio.gpio_read(self._h, self._pin_b)
        self._running = True
        self._thread  = threading.Thread(target=self._poll_loop, daemon=True)
        self._thread.start()
        return True

    def _poll_loop(self):
        import lgpio
        while self._running:
            curr_a = lgpio.gpio_read(self._h, self._pin_a)
            curr_b = lgpio.gpio_read(self._h, self._pin_b)
            if curr_a != self._prev_a or curr_b != self._prev_b:
                delta = _QUAD_TABLE.get(
                    (self._prev_a, self._prev_b, curr_a, curr_b), 0
                )
                if delta:
                    self._ticks += delta
                self._prev_a = curr_a
                self._prev_b = curr_b

    def stop(self):
        self._running = False
        if self._thread:
            self._thread.join(timeout=1.0)
            self._thread = None
        import lgpio
        for pin in (self._pin_a, self._pin_b):
            try:
                lgpio.gpio_free(self._h, pin)
            except Exception:
                pass

    @property
    def ticks(self) -> int:
        return self._ticks

    def reset(self):
        self._ticks = 0


# ── 编码器测试函数 ──────────────────────────────────────────────────
_ENC_SPIN_DURATION = 2.0    # 秒：转动时长
_ENC_MIN_TICKS     = 50     # 判断"有脉冲"的最低阈值


def _enc_label(name: str) -> str:
    return MOTOR_LABELS[name]


def test_encoder_pulse(car: CarController, name: str) -> bool:
    """
    转动单个电机 _ENC_SPIN_DURATION 秒，检测对应编码器是否产生脉冲。
    返回 True 表示脉冲正常。
    """
    if ENC_SIMULATION:
        print(f"    [模拟] 跳过编码器脉冲检测（pigpio 不可用）")
        return True

    pins = ENCODER_PINS[name]
    enc = _EncoderCounter(_h, pins["pin_a"], pins["pin_b"])
    if not enc.start():
        return False
    try:
        label = _enc_label(name)
        print(f"    ▶ 正转 {_ENC_SPIN_DURATION}s，读取脉冲...", end="", flush=True)
        car.motor_forward(name, TEST_SPEED)
        time.sleep(_ENC_SPIN_DURATION)
        car.motor_stop(name)
        time.sleep(0.2)
        fwd_ticks = enc.ticks
        enc.reset()

        print(f" 正转脉冲={fwd_ticks:+d}")

        print(f"    ▶ 反转 {_ENC_SPIN_DURATION}s，读取脉冲...", end="", flush=True)
        car.motor_backward(name, TEST_SPEED)
        time.sleep(_ENC_SPIN_DURATION)
        car.motor_stop(name)
        time.sleep(0.2)
        bwd_ticks = enc.ticks
        print(f" 反转脉冲={bwd_ticks:+d}")

        ok_fwd = abs(fwd_ticks) >= _ENC_MIN_TICKS
        ok_bwd = abs(bwd_ticks) >= _ENC_MIN_TICKS
        ok_dir = (fwd_ticks > 0 and bwd_ticks < 0) or (fwd_ticks < 0 and bwd_ticks > 0)

        if not ok_fwd or not ok_bwd:
            print(f"    ❌ 脉冲数过少（阈值 {_ENC_MIN_TICKS}）→ 检查 VCC/GND/A/B 接线")
            return False
        if not ok_dir:
            print(f"    ⚠️  正反转脉冲方向相同 → A/B 相可能接反（里程计方向会出错）")
            return False
        print(f"    ✅ 脉冲正常，方向正确")
        return True
    finally:
        enc.stop()


def test_encoder_realtime(car: CarController, name: str):
    """
    实时显示编码器脉冲数（5 秒），供手动转动轮子或驱动电机时观察。
    """
    if ENC_SIMULATION:
        print(f"    [模拟] 跳过实时显示（pigpio 不可用）")
        return

    pins = ENCODER_PINS[name]
    enc = _EncoderCounter(_h, pins["pin_a"], pins["pin_b"])
    enc.start()
    try:
        label = _enc_label(name)
        print(f"    实时脉冲监视（{label}，5 秒，手动转轮或等电机旋转）：")
        for _ in range(50):
            print(f"\r    ticks = {enc.ticks:+6d}", end="", flush=True)
            time.sleep(0.1)
        print(f"\r    最终 ticks = {enc.ticks:+6d}  （5 秒总计）")
    finally:
        enc.stop()


def test_all_encoders(car: CarController) -> None:
    """依次对四路编码器做脉冲 + 方向验证，打印汇总报告。"""
    print("\n" + "═" * 56)
    print("  【编码器测试】四路脉冲 & 方向验证")
    if ENC_SIMULATION:
        print("  ⚠️  pigpio 不可用，以下结果均为模拟")
    print("═" * 56)

    results = []
    for name in MOTOR_NAMES.values():
        label = _enc_label(name)
        pins  = ENCODER_PINS[name]
        print(f"\n  [{label}]  A=GPIO{pins['pin_a']}  B=GPIO{pins['pin_b']}")
        ok = test_encoder_pulse(car, name)
        results.append((label, ok))
        time.sleep(0.5)

    print("\n" + "═" * 56)
    print("  【编码器测试汇总】")
    print("═" * 56)
    all_ok = True
    for label, ok in results:
        status = "✅ 正常" if ok else "❌ 异常"
        if not ok:
            all_ok = False
        print(f"  {label:<20} {status}")
    print("═" * 56)
    if all_ok:
        print("  结论：全部编码器脉冲与方向正常！\n")
    else:
        print("  结论：存在异常，请对照 docs/HARDWARE.md §2.1 检查接线。\n")


def diagnose_encoder_pins(name: str) -> None:
    """
    不转电机，直接读 A/B 相引脚原始电平，手动转轮观察变化。
    用于确认编码器信号是否到达 GPIO（排查 VCC/接线问题）。
    """
    if ENC_SIMULATION:
        print("    [模拟] lgpio 不可用，跳过")
        return

    import lgpio
    pins  = ENCODER_PINS[name]
    pin_a = pins["pin_a"]
    pin_b = pins["pin_b"]
    label = _enc_label(name)

    print(f"\n  【引脚诊断】{label}  A=GPIO{pin_a}  B=GPIO{pin_b}")
    print(f"  请用手慢慢转动对应轮子，观察 A/B 值是否在 0/1 之间跳变。")
    print(f"  若始终为 1/1 或 0/0 且不变 → 编码器 VCC/GND/A/B 接线有问题。")
    print(f"  按 Ctrl+C 结束诊断\n")

    for pin in (pin_a, pin_b):
        try:
            lgpio.gpio_free(_h, pin)
        except Exception:
            pass

    ok_a = ok_b = True
    try:
        lgpio.gpio_claim_input(_h, pin_a, lgpio.SET_PULL_UP)
    except Exception as e:
        print(f"  ❌ GPIO{pin_a} 无法声明：{e}（可能被内核占用，如 SPI/I2C）")
        ok_a = False
    try:
        lgpio.gpio_claim_input(_h, pin_b, lgpio.SET_PULL_UP)
    except Exception as e:
        print(f"  ❌ GPIO{pin_b} 无法声明：{e}（可能被内核占用，如 SPI/I2C）")
        ok_b = False

    try:
        prev = (-1, -1)
        while True:
            a = lgpio.gpio_read(_h, pin_a) if ok_a else "×"
            b = lgpio.gpio_read(_h, pin_b) if ok_b else "×"
            if (a, b) != prev:
                print(f"\r  A(GPIO{pin_a})={a}  B(GPIO{pin_b})={b}    ", flush=True)
                prev = (a, b)
            else:
                print(f"\r  A(GPIO{pin_a})={a}  B(GPIO{pin_b})={b}    ", end="", flush=True)
            import time as _t; _t.sleep(0.02)
    except KeyboardInterrupt:
        print("\n  诊断结束")
    finally:
        for pin in (pin_a, pin_b):
            try:
                lgpio.gpio_free(_h, pin)
            except Exception:
                pass


def verify_encoder_wiring(car: CarController) -> None:
    """
    交互式编码器接线验证：
    依次转动每个电机，显示实时脉冲，用户确认是否有信号。
    """
    print("\n" + "═" * 56)
    print("  【编码器接线验证】逐路交互确认")
    print("  每路电机会转动 3 秒，同时显示实时脉冲数。")
    print("  若脉冲数持续为 0，说明该路编码器接线有问题。")
    if ENC_SIMULATION:
        print("  ⚠️  pigpio 不可用，以下为模拟模式")
    print("═" * 56)

    results = []
    for name in MOTOR_NAMES.values():
        label = _enc_label(name)
        pins  = ENCODER_PINS[name]
        print(f"\n  ─── {label}  A=GPIO{pins['pin_a']}  B=GPIO{pins['pin_b']} ───")
        input(f"  按 Enter 开始转动 [{label}]...")

        if ENC_SIMULATION:
            print("  [模拟] 跳过")
            results.append((label, "跳过"))
            continue

        enc = _EncoderCounter(_h, pins["pin_a"], pins["pin_b"])
        if not enc.start():
            results.append((label, "⛔ GPIO busy（SPI0 未禁用？）"))
            continue
        car.motor_forward(name, TEST_SPEED)
        try:
            for _ in range(30):
                print(f"\r  ticks = {enc.ticks:+6d}", end="", flush=True)
                time.sleep(0.1)
        finally:
            car.motor_stop(name)
            enc.stop()

        final = enc.ticks
        print(f"\r  3 秒脉冲总计：{final:+d}")

        if abs(final) < _ENC_MIN_TICKS:
            print(f"  ❌ 脉冲过少（< {_ENC_MIN_TICKS}），可能接线有问题")
            verdict = "❌ 异常"
        else:
            print(f"  ✅ 脉冲正常")
            verdict = "✅ 正常"
        results.append((label, verdict))
        time.sleep(0.5)

    print("\n" + "═" * 56)
    print("  【编码器验证汇总】")
    for label, verdict in results:
        print(f"  {label:<20} {verdict}")
    print("═" * 56 + "\n")


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
    enc_status = "模拟模式（pigpio 不可用）" if ENC_SIMULATION else "已就绪（pigpiod 运行中）"
    menu = f"""
╔════════════════════════════════════════════╗
║     4轮编码电机测试 — 交互菜单             ║
║     编码器状态：{enc_status:<20}║
╠════════════════════════════════════════════╣
║  ── 电机测试 ──────────────────────────── ║
║  1. 测试前左电机 M1 (Front-Left)           ║
║  2. 测试前右电机 M2 (Front-Right)          ║
║  3. 测试后左电机 M3 (Rear-Left)            ║
║  4. 测试后右电机 M4 (Rear-Right)           ║
║  5. 逐一测试全部电机                       ║
║  6. 速度渐变测试                           ║
║  7. 整车运动模式测试                       ║
║  8. 运行全部电机测试                       ║
║  v. 目视验证 M1~M4 接线位置               ║
║  ── 编码器测试 ─────────────────────────  ║
║  e. 四路编码器脉冲 & 方向自动测试          ║
║  r. 实时脉冲显示（选单路，手动转轮）       ║
║  d. 引脚原始电平诊断（手动转轮看0/1跳变）  ║
║  w. 交互式编码器接线验证                   ║
║  ─────────────────────────────────────── ║
║  q. 退出                                   ║
╚════════════════════════════════════════════╝"""
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
        elif choice == "e":
            test_all_encoders(car)
        elif choice == "r":
            print("  选择要监视的编码器：1=左前 2=右前 3=左后 4=右后")
            try:
                sel = input("  > ").strip()
                if sel in ("1", "2", "3", "4"):
                    test_encoder_realtime(car, MOTOR_NAMES[int(sel)])
                else:
                    print("  无效选项")
            except (EOFError, KeyboardInterrupt):
                pass
        elif choice == "d":
            print("  选择要诊断的编码器：1=左前 2=右前 3=左后 4=右后")
            try:
                sel = input("  > ").strip()
                if sel in ("1", "2", "3", "4"):
                    diagnose_encoder_pins(MOTOR_NAMES[int(sel)])
                else:
                    print("  无效选项")
            except (EOFError, KeyboardInterrupt):
                pass
        elif choice == "w":
            verify_encoder_wiring(car)
        else:
            print("  无效选项，请重新输入")

        car.stop_all()

    car.stop_all()


# ── 主入口 ────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="4轮编码电机测试脚本")
    parser.add_argument("--all", action="store_true",
                        help="自动运行全部电机测试")
    parser.add_argument("--motor", type=int, choices=[1, 2, 3, 4],
                        help="只测试指定编号电机 (1=M1左前 2=M2右前 3=M3左后 4=M4右后)")
    parser.add_argument("--verify", action="store_true",
                        help="逐口目视验证模式：依次点动 M1~M4，确认电机接线位置")
    parser.add_argument("--encoder", action="store_true",
                        help="自动测试四路编码器脉冲与方向（需要 sudo pigpiod）")
    parser.add_argument("--enc-verify", action="store_true",
                        help="交互式编码器接线验证（逐路转动，实时显示脉冲）")
    parser.add_argument("--diagnose", type=int, choices=[1, 2, 3, 4],
                        help="引脚原始电平诊断，手动转轮观察0/1跳变 (1=左前 2=右前 3=左后 4=右后)")
    args = parser.parse_args()

    gpio_mode = "模拟" if SIMULATION else "真实 GPIO"
    enc_mode  = "模拟" if ENC_SIMULATION else f"lgpio chip{_CHIP_NUM}"
    print(f"\n4轮编码电机测试脚本")
    print(f"  GPIO 模式：{gpio_mode}  |  编码器模式：{enc_mode}")
    print(f"  测试速度：{TEST_SPEED}%  |  步骤时长：{TEST_DURATION}s\n")

    car = CarController()
    try:
        if args.verify:
            verify_motor_mapping(car)
        elif args.motor:
            test_single_motor(car, MOTOR_NAMES[args.motor])
        elif args.all:
            test_all(car)
        elif args.encoder:
            test_all_encoders(car)
        elif args.enc_verify:
            verify_encoder_wiring(car)
        elif args.diagnose:
            diagnose_encoder_pins(MOTOR_NAMES[args.diagnose])
        else:
            interactive_menu(car)
    except KeyboardInterrupt:
        print("\n\n⚠️  用户中断，正在停止电机并清理 GPIO...")
    finally:
        car.cleanup()
        if _h is not None and _lgpio is not None:
            _lgpio.gpiochip_close(_h)
        print("资源已释放，程序退出。")


if __name__ == "__main__":
    main()
