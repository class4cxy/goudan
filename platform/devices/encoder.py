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
  四轮底盘只需接左右各一路编码器，当前定稿为后轮对称接法（M3/M4）：
    左后轮编码器（M3）：A → ENCODER_LEFT_A（默认 GPIO23），B → ENCODER_LEFT_B（默认 GPIO16）
    右后轮编码器（M4）：A → ENCODER_RIGHT_A（默认 GPIO14），B → ENCODER_RIGHT_B（默认 GPIO18）
  前轮 M1/M2 编码器线当前不参与里程计读取。
  编码器 VCC 接树莓派 3.3V（Pin 1 或 Pin 17），不要接 5V。

lgpio 安装：
  sudo apt install -y python3-lgpio

树莓派 5 GPIO 芯片编号：
  lgpio 枚举从 0 开始，RPi5 的 RP1 控制器实测为 gpiochip0（描述含 pinctrl-rp1）。
  旧版内核/第三方文档可能写作 gpiochip4，但 lgpio API 下编号是 0。
  脚本自动检测，可通过 GPIO_CHIP_NUM 环境变量手动覆盖。
"""

import os
import threading
import time
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
    """双路编码器 GPIO 引脚配置（BCM 编号）。

    所有默认值均为硬编码常量，反映当前实物接线；
    调用方可在构造时直接传参覆盖，无需依赖环境变量。
    """
    # M3 左后轮：白线A → GPIO23（Pin 16），黄线B → GPIO16（Pin 36）
    left_a:  int = 23
    left_b:  int = 16
    # M4 右后轮：白线A → GPIO20（Pin 38），黄线B → GPIO21（Pin 40）
    # 原 HC-SR04 超声波占用 GPIO20/21，已禁用超声波，改接 M4 编码器
    right_a: int = 20
    right_b: int = 21
    # 编码器标称线数；4 倍频后 ticks/rev = lines_per_rev × 4
    # 出厂标称 500 线。焊接后电机两端 100nF 电容后 EMF 噪声消除，恢复真实值 500。
    # 之前设 125 是数学补偿"75% 脉冲丢失"的临时方案，现已废弃。
    lines_per_rev: int = 500
    # 极性翻转：实测 M4 右后轮镜像安装，前进时 ticks 为负，需翻转
    left_invert:  bool = False
    right_invert: bool = True
    # 去抖：连续读 debounce_reads 次确认电平稳定，间隔 debounce_delay_us 微秒
    # 去抖延时权衡：脉冲间隔 5.6μs（89 RPM×500线×4倍频）
    # 3μs 可过滤 EMF 毛刺同时不丢真实脉冲；0 会引入噪声，20 会漏真实脉冲
    debounce_reads:    int = 1
    debounce_delay_us: int = 3

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

    def __init__(
        self,
        h: int,
        pin_a: int,
        pin_b: int,
        invert: bool = False,
        debounce_reads: int = 1,
        debounce_delay_us: int = 20,
    ) -> None:
        self._h       = h
        self._pin_a   = pin_a
        self._pin_b   = pin_b
        self._invert  = invert
        self._debounce_reads   = max(1, debounce_reads)
        self._debounce_delay_s = max(0.0, debounce_delay_us) / 1_000_000.0
        self._lock    = threading.Lock()
        self._ticks         = 0     # 消耗性计数（read_and_reset 用）
        self._total_ticks   = 0     # 非消耗性累计（get_cumulative 用，仅增不减）
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

    # 无跳变时的空闲轮询间隔（100μs）。
    # 500rpm × 90(减速比) × 4(倍频) / 60 ≈ 3000 ticks/s → 采样需 >6000Hz → 100μs = 10000Hz，不漏脉冲。
    _IDLE_SLEEP_S = 0.0001

    def _poll_loop(self) -> None:
        import lgpio
        while self._running:
            curr_a = lgpio.gpio_read(self._h, self._pin_a)
            curr_b = lgpio.gpio_read(self._h, self._pin_b)
            if curr_a != self._prev_a or curr_b != self._prev_b:
                # 去抖：重复读取确认电平稳定，排除 PWM 噪声毛刺
                stable = True
                for _ in range(self._debounce_reads):
                    time.sleep(self._debounce_delay_s)
                    if (lgpio.gpio_read(self._h, self._pin_a) != curr_a or
                            lgpio.gpio_read(self._h, self._pin_b) != curr_b):
                        stable = False
                        break
                if not stable:
                    # 毛刺，丢弃此次跳变，重新读取真实电平
                    curr_a = lgpio.gpio_read(self._h, self._pin_a)
                    curr_b = lgpio.gpio_read(self._h, self._pin_b)
                    self._prev_a = curr_a
                    self._prev_b = curr_b
                    continue

                delta = _QUAD_TABLE.get(
                    (self._prev_a, self._prev_b, curr_a, curr_b), 0
                )
                if delta:
                    if self._invert:
                        delta = -delta
                    with self._lock:
                        self._ticks       += delta
                        self._total_ticks += delta
                self._prev_a = curr_a
                self._prev_b = curr_b
            else:
                time.sleep(self._IDLE_SLEEP_S)

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

    def get_cumulative(self) -> int:
        """
        读取自 start() 以来的总脉冲（不清零）。

        用于测试/监控场景，不与 read_and_reset（里程计）竞争。
        """
        with self._lock:
            return self._total_ticks


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
            self._left  = _WheelEncoder(
                self._h, self._cfg.left_a,  self._cfg.left_b,
                invert=self._cfg.left_invert,
                debounce_reads=self._cfg.debounce_reads,
                debounce_delay_us=self._cfg.debounce_delay_us,
            )
            self._right = _WheelEncoder(
                self._h, self._cfg.right_a, self._cfg.right_b,
                invert=self._cfg.right_invert,
                debounce_reads=self._cfg.debounce_reads,
                debounce_delay_us=self._cfg.debounce_delay_us,
            )
            self._left.start()
            self._right.start()
            logger.info(
                f"[Encoder] 已启动（lgpio chip={_CHIP_NUM}）| "
                f"左轮 A/B={self._cfg.left_a}/{self._cfg.left_b} inv={self._cfg.left_invert} "
                f"右轮 A/B={self._cfg.right_a}/{self._cfg.right_b} inv={self._cfg.right_invert} "
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

    def get_cumulative(self) -> tuple[int, int]:
        """
        读取自 start() 以来左右轮的累计脉冲（不清零）。

        用于测试/监控，不与里程计的 read_and_reset() 竞争。
        模拟模式下返回 (0, 0)。
        """
        if self._is_simulation or self._left is None or self._right is None:
            return 0, 0
        return self._left.get_cumulative(), self._right.get_cumulative()

    # ─── 属性 ────────────────────────────────────────────────────

    @property
    def ticks_per_rev(self) -> int:
        return self._cfg.ticks_per_rev

    @property
    def is_simulation(self) -> bool:
        return self._is_simulation

    @property
    def status(self) -> dict:
        left_total, right_total = self.get_cumulative()
        return {
            "is_simulation": self._is_simulation,
            "ticks_per_rev": self._cfg.ticks_per_rev,
            "gpio_chip":     _CHIP_NUM,
            "pins": {
                "left_a":       self._cfg.left_a,
                "left_b":       self._cfg.left_b,
                "left_invert":  self._cfg.left_invert,
                "right_a":      self._cfg.right_a,
                "right_b":      self._cfg.right_b,
                "right_invert": self._cfg.right_invert,
            },
            "cumulative_ticks": {
                "left":  left_total,
                "right": right_total,
            },
        }
