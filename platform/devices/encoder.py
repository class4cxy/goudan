"""
Encoder — 500线正交编码器驱动（pigpio 后端）
============================================
职责：
  - 通过 pigpio 守护进程读取两路正交编码器（A/B 两相）
  - 4 倍频解码：A/B 上升沿与下降沿均计数 → 每圈 2000 脉冲（500线）
  - 线程安全地累计脉冲，供 Odometry 定期读取并清零
  - 非树莓派或 pigpiod 未启动时自动降级为模拟模式（返回 0）

接线说明（GMR 1:90 编码电机，6 线：M+/M− 接电机驱动口，另 4 线为编码器信号）：
  编码器 4 线：VCC(5V)、GND、A相、B相
  四轮底盘只需接左右各一路编码器（选后轮，受力更均匀）：
    左后轮编码器：A → ENCODER_LEFT_A（默认 GPIO4），B → ENCODER_LEFT_B（默认 GPIO11）
    右后轮编码器：A → ENCODER_RIGHT_A（默认 GPIO19），B → ENCODER_RIGHT_B（默认 GPIO7）
  引脚定稿见 docs/HARDWARE.md §7.1.1；GPIO13 已被舵机 Tilt 占用，禁止接编码器。

pigpio 安装与启动：
  pip install pigpio
  sudo pigpiod               # 启动守护进程（每次开机）
  sudo systemctl enable pigpiod  # 或设置开机自启
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


@dataclass
class EncoderConfig:
    """双路编码器 GPIO 引脚配置（BCM 编号）。"""
    left_a:  int = int(os.environ.get("ENCODER_LEFT_A",   "4"))
    left_b:  int = int(os.environ.get("ENCODER_LEFT_B",  "11"))
    right_a: int = int(os.environ.get("ENCODER_RIGHT_A", "19"))
    right_b: int = int(os.environ.get("ENCODER_RIGHT_B",  "7"))
    lines_per_rev: int = int(os.environ.get("ENCODER_LINES_PER_REV", "500"))

    @property
    def ticks_per_rev(self) -> int:
        """4 倍频后每圈脉冲总数。"""
        return self.lines_per_rev * 4


class _WheelEncoder:
    """单路正交编码器（A/B 两相），由 pigpio 中断驱动。"""

    def __init__(self, pi, pin_a: int, pin_b: int) -> None:
        self._pi    = pi
        self._pin_a = pin_a
        self._pin_b = pin_b
        self._lock  = threading.Lock()
        self._ticks = 0
        self._prev_a: int = 0
        self._prev_b: int = 0
        self._cb_a = None
        self._cb_b = None

    def start(self) -> None:
        import pigpio
        self._pi.set_mode(self._pin_a, pigpio.INPUT)
        self._pi.set_mode(self._pin_b, pigpio.INPUT)
        self._pi.set_pull_up_down(self._pin_a, pigpio.PUD_UP)
        self._pi.set_pull_up_down(self._pin_b, pigpio.PUD_UP)
        self._prev_a = self._pi.read(self._pin_a)
        self._prev_b = self._pi.read(self._pin_b)
        self._cb_a = self._pi.callback(self._pin_a, pigpio.EITHER_EDGE, self._on_edge)
        self._cb_b = self._pi.callback(self._pin_b, pigpio.EITHER_EDGE, self._on_edge)

    def stop(self) -> None:
        if self._cb_a:
            self._cb_a.cancel()
        if self._cb_b:
            self._cb_b.cancel()

    def _on_edge(self, gpio: int, level: int, tick: int) -> None:
        """pigpio 中断回调（在 pigpio C 线程中执行，极低延迟）。"""
        curr_a = self._pi.read(self._pin_a)
        curr_b = self._pi.read(self._pin_b)
        delta = _QUAD_TABLE.get((self._prev_a, self._prev_b, curr_a, curr_b), 0)
        if delta:
            with self._lock:
                self._ticks += delta
        self._prev_a = curr_a
        self._prev_b = curr_b

    def get_and_reset(self) -> int:
        """原子地读取并清零脉冲计数，返回自上次调用以来的脉冲增量。"""
        with self._lock:
            ticks = self._ticks
            self._ticks = 0
        return ticks


class Encoder:
    """
    双路正交编码器控制器（左轮 + 右轮）。

    使用 pigpio 守护进程读取高频脉冲信号。
    非树莓派或 pigpiod 未运行时自动降级为模拟模式（read_and_reset 始终返回 (0, 0)）。
    """

    def __init__(self, config: EncoderConfig | None = None) -> None:
        self._cfg          = config or EncoderConfig()
        self._pi           = None
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
            import pigpio
            self._pi = pigpio.pi()
            if not self._pi.connected:
                raise RuntimeError(
                    "pigpiod 守护进程未运行，请执行：sudo pigpiod"
                )
            self._left  = _WheelEncoder(self._pi, self._cfg.left_a,  self._cfg.left_b)
            self._right = _WheelEncoder(self._pi, self._cfg.right_a, self._cfg.right_b)
            self._left.start()
            self._right.start()
            logger.info(
                f"[Encoder] 已启动 | 左轮 A/B={self._cfg.left_a}/{self._cfg.left_b} "
                f"右轮 A/B={self._cfg.right_a}/{self._cfg.right_b} "
                f"ticks/rev={self._cfg.ticks_per_rev}"
            )
            return True
        except Exception as e:
            logger.warning(f"[Encoder] 初始化失败，降级为模拟模式：{e}")
            self._is_simulation = True
            return False

    def stop(self) -> None:
        """停止编码器读取并释放 pigpio 资源。"""
        if self._left:
            self._left.stop()
        if self._right:
            self._right.stop()
        if self._pi:
            self._pi.stop()
            self._pi = None

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
            "pins": {
                "left_a":  self._cfg.left_a,
                "left_b":  self._cfg.left_b,
                "right_a": self._cfg.right_a,
                "right_b": self._cfg.right_b,
            },
        }
