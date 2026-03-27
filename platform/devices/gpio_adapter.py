"""
GPIO 适配层

优先级（自动降级）：
  1. rpi-lgpio  — RPi5（RP1 GPIO 控制器）推荐，pip install rpi-lgpio
  2. RPi.GPIO   — RPi4 及更早版本
  3. _FakeGPIO  — 非树莓派环境（开发机、CI），所有操作以 DEBUG 日志输出

对外导出：
  GPIO        — GPIO 模块对象（真实或模拟）
  SIMULATION  — bool，当前是否为模拟模式
  GPIO_BACKEND — str，实际使用的后端名称（"rpi-lgpio" / "RPi.GPIO" / "simulation"）
"""

import logging

logger = logging.getLogger(__name__)

SIMULATION = False
GPIO_BACKEND = "simulation"

# ── 尝试 rpi-lgpio（RPi5 兼容层，优先）────────────────────────────
# rpi-lgpio 以 RPi.GPIO 兼容 API 包装 lgpio，支持 RPi5 RP1 GPIO 控制器。
# 安装：pip install rpi-lgpio
try:
    import lgpio as _lgpio_check  # noqa: F401 — 仅确认 lgpio 库存在
    import importlib
    _rpi_lgpio = importlib.import_module("RPi.GPIO")  # rpi-lgpio 注册为 RPi.GPIO
    GPIO = _rpi_lgpio
    SIMULATION = False
    GPIO_BACKEND = "rpi-lgpio"
    logger.info("GPIO: 使用 rpi-lgpio（RPi5 兼容，RP1 GPIO 控制器）")
except Exception:
    # ── 尝试 RPi.GPIO（RPi4 及更早）────────────────────────────────
    try:
        import RPi.GPIO as GPIO  # type: ignore[import]
        SIMULATION = False
        GPIO_BACKEND = "RPi.GPIO"
        logger.info("GPIO: 使用 RPi.GPIO")
    except (ImportError, RuntimeError):
        SIMULATION = True
        GPIO_BACKEND = "simulation"
        logger.warning(
            "GPIO: 未检测到 RPi.GPIO / rpi-lgpio，进入模拟模式（不操作真实引脚）\n"
            "      RPi5 请运行：pip install rpi-lgpio\n"
            "      RPi4 请运行：pip install RPi.GPIO"
        )

        class _FakeGPIO:  # noqa: N801
            BCM = "BCM"
            OUT = "OUT"
            IN = "IN"
            HIGH = 1
            LOW = 0

            def setmode(self, *_a) -> None:
                pass

            def setwarnings(self, *_a) -> None:
                pass

            def setup(self, *_a, **_kw) -> None:
                pass

            def input(self, pin: int) -> int:
                logger.debug("[GPIO-SIM] read pin=%s -> LOW", pin)
                return self.LOW

            def output(self, pin: int, value: bool) -> None:
                logger.debug("[GPIO-SIM] pin=%s value=%s", pin, value)

            def cleanup(self) -> None:
                logger.debug("[GPIO-SIM] cleanup")

            class PWM:
                def __init__(self, pin: int, freq: int) -> None:
                    self._pin = pin

                def start(self, dc: float) -> None:
                    logger.debug("[GPIO-SIM] PWM pin=%s start duty=%.1f%%", self._pin, dc)

                def ChangeDutyCycle(self, dc: float) -> None:  # noqa: N802
                    logger.debug("[GPIO-SIM] PWM pin=%s duty=%.1f%%", self._pin, dc)

                def stop(self) -> None:
                    logger.debug("[GPIO-SIM] PWM pin=%s stop", self._pin)

        GPIO = _FakeGPIO()  # type: ignore[assignment]

__all__ = ["GPIO", "SIMULATION", "GPIO_BACKEND"]
