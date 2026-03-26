"""
Encoder — 500线正交编码器驱动（lgpio 后端，支持树莓派 5）
============================================================
职责：
  - 通过 lgpio 读取两路正交编码器（A/B 两相）的硬件中断
  - 4 倍频解码：A/B 上升沿与下降沿均计数 → 每圈 2000 脉冲（500线）
  - 线程安全地累计脉冲，供 Odometry 定期读取并清零
  - lgpio 不可用时自动降级为模拟模式（返回 0）

接线说明（GMR 1:90 编码电机，6 线：M+/M− 接电机驱动口，另 4 线为编码器信号）：
  编码器 4 线：VCC(3.3V)、GND、A相、B相
  四轮底盘只需接左右各一路编码器（选后轮，受力更均匀）：
    左后轮编码器：A → ENCODER_LEFT_A（默认 GPIO4），B → ENCODER_LEFT_B（默认 GPIO11）
    右后轮编码器：A → ENCODER_RIGHT_A（默认 GPIO19），B → ENCODER_RIGHT_B（默认 GPIO7）
  引脚定稿见 docs/HARDWARE.md §7.1.1；GPIO13 已被舵机 Tilt 占用，禁止接编码器。
  编码器 VCC 接树莓派 3.3V（Pin 1 或 Pin 17），不要接 5V。

lgpio 安装：
  sudo apt install -y python3-lgpio

树莓派 5 GPIO 芯片编号：
  /dev/gpiochip4（RP1 芯片）—— 脚本自动检测，无需手动配置。
"""

import os
import threading
import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)

# 正交解码状态转换表
# key: (prev_a, prev_b, curr_a, curr_b) → delta (+1 / -1 / 0)
_QUAD_TABLE: dict[tuple[int, int, int, int], int] = {
    (0, 0, 0, 1): +1, (0, 1, 1, 1): +1, (1, 1, 1, 0): +1, (1, 0, 0, 0): +1,
    (0, 0, 1, 0): -1, (1, 0, 1, 1): -1, (1, 1, 0, 1): -1, (0, 1, 0, 0): -1,
}

# 自动检测 40pin GPIO 对应的 chip 编号（RPi 5 实测为 gpiochip0，描述含 pinctrl-rp1）。
# 可通过环境变量 GPIO_CHIP_NUM 手动覆盖。
def _detect_gpio_chip() -> int:
    override = os.environ.get("GPIO_CHIP_NUM")
    if override is not None:
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

_CHIP_NUM = _detect_gpio_chip()


@dataclass
class EncoderConfig:
    """双路编码器 GPIO 引脚配置（BCM 编号）。"""
    left_a:  int = int(os.environ.get("ENCODER_LEFT_A",   "4"))   # M3 左后 A
    left_b:  int = int(os.environ.get("ENCODER_LEFT_B",  "11"))  # M3 左后 B
    right_a: int = int(os.environ.get("ENCODER_RIGHT_A", "19"))  # M4 右后 A
    right_b: int = int(os.environ.get("ENCODER_RIGHT_B", "10"))  # M4 右后 B（GPIO7/8=spi0 CS强占）
    lines_per_rev: int = int(os.environ.get("ENCODER_LINES_PER_REV", "500"))

    @property
    def ticks_per_rev(self) -> int:
        """4 倍频后每圈脉冲总数。"""
        return self.lines_per_rev * 4


class _WheelEncoder:
    """
    单路正交编码器（A/B 两相），lgpio 轮询线程驱动。

    使用轮询线程代替 callback，避免 RPi.GPIO 与 lgpio 共享 gpiochip
    handle 时 callback 不触发的问题（树莓派 5 实测确认）。
    """

    def __init__(self, h: int, pin_a: int, pin_b: int) -> None:
        self._h       = h
        self._pin_a   = pin_a
        self._pin_b   = pin_b
        self._lock    = threading.Lock()
        self._ticks   = 0
        self._prev_a: int = 0
        self._prev_b: int = 0
        self._running = False
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        import lgpio
        for pin in (self._pin_a, self._pin_b):
            try:
                lgpio.gpio_free(self._h, pin)
            except Exception:
                pass
        lgpio.gpio_claim_input(self._h, self._pin_a, lgpio.SET_PULL_UP)
        lgpio.gpio_claim_input(self._h, self._pin_b, lgpio.SET_PULL_UP)
        self._prev_a  = lgpio.gpio_read(self._h, self._pin_a)
        self._prev_b  = lgpio.gpio_read(self._h, self._pin_b)
        self._running = True
        self._thread  = threading.Thread(
            target=self._poll_loop, daemon=True, name=f"enc-{self._pin_a}"
        )
        self._thread.start()

    def _poll_loop(self) -> None:
        import lgpio
        while self._running:
            curr_a = lgpio.gpio_read(self._h, self._pin_a)
            curr_b = lgpio.gpio_read(self._h, self._pin_b)
            if curr_a != self._prev_a or curr_b != self._prev_b:
                delta = _QUAD_TABLE.get(
                    (self._prev_a, self._prev_b, curr_a, curr_b), 0
                )
                if delta:
                    with self._lock:
                        self._ticks += delta
                self._prev_a = curr_a
                self._prev_b = curr_b

    def stop(self) -> None:
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

    def get_and_reset(self) -> int:
        """原子地读取并清零脉冲计数，返回自上次调用以来的脉冲增量。"""
        with self._lock:
            ticks = self._ticks
            self._ticks = 0
        return ticks


class Encoder:
    """
    双路正交编码器控制器（左轮 + 右轮）。

    使用 lgpio 读取硬件中断，支持树莓派 5（gpiochip4）及旧款（gpiochip0）。
    lgpio 不可用时自动降级为模拟模式（read_and_reset 始终返回 (0, 0)）。
    """

    def __init__(self, config: EncoderConfig | None = None) -> None:
        self._cfg           = config or EncoderConfig()
        self._h: int | None = None
        self._left:  _WheelEncoder | None = None
        self._right: _WheelEncoder | None = None
        self._is_simulation = False

    # ─── 生命周期 ─────────────────────────────────────────────────

    def start(self) -> bool:
        """
        启动编码器读取。

        Returns:
            True  = 真实硬件已就绪
            False = 降级为模拟模式
        """
        try:
            import lgpio
            self._h = lgpio.gpiochip_open(_CHIP_NUM)
            self._left  = _WheelEncoder(self._h, self._cfg.left_a,  self._cfg.left_b)
            self._right = _WheelEncoder(self._h, self._cfg.right_a, self._cfg.right_b)
            self._left.start()
            self._right.start()
            logger.info(
                f"[Encoder] 已启动（lgpio chip={_CHIP_NUM}）| "
                f"左轮 A/B={self._cfg.left_a}/{self._cfg.left_b} "
                f"右轮 A/B={self._cfg.right_a}/{self._cfg.right_b} "
                f"ticks/rev={self._cfg.ticks_per_rev}"
            )
            return True
        except Exception as e:
            logger.warning(f"[Encoder] 初始化失败，降级为模拟模式：{e}")
            self._is_simulation = True
            return False

    def stop(self) -> None:
        """停止编码器读取并释放 lgpio 资源。"""
        if self._left:
            self._left.stop()
        if self._right:
            self._right.stop()
        if self._h is not None:
            try:
                import lgpio
                lgpio.gpiochip_close(self._h)
            except Exception:
                pass
            self._h = None

    # ─── 数据读取 ────────────────────────────────────────────────

    def read_and_reset(self) -> tuple[int, int]:
        """
        原子地读取并清零左右轮脉冲增量。

        Returns:
            (left_ticks, right_ticks) — 自上次调用以来的脉冲数
            模拟模式下始终返回 (0, 0)
        """
        if self._is_simulation or self._left is None or self._right is None:
            return 0, 0
        return self._left.get_and_reset(), self._right.get_and_reset()

    # ─── 属性 ────────────────────────────────────────────────────

    @property
    def ticks_per_rev(self) -> int:
        return self._cfg.ticks_per_rev

    @property
    def is_simulation(self) -> bool:
        return self._is_simulation

    @property
    def status(self) -> dict:
        return {
            "is_simulation": self._is_simulation,
            "ticks_per_rev": self._cfg.ticks_per_rev,
            "gpio_chip":     _CHIP_NUM,
            "pins": {
                "left_a":  self._cfg.left_a,
                "left_b":  self._cfg.left_b,
                "right_a": self._cfg.right_a,
                "right_b": self._cfg.right_b,
            },
        }
