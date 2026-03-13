"""
GPIO 适配层

在树莓派上使用真实 RPi.GPIO；在非树莓派环境（开发机、CI）
自动降级为 _FakeGPIO，所有操作以 DEBUG 日志输出，不操作任何真实引脚。

对外导出：
  GPIO        — GPIO 模块对象（真实或模拟）
  SIMULATION  — bool，当前是否为模拟模式
"""

import logging

logger = logging.getLogger(__name__)

try:
    import RPi.GPIO as GPIO  # type: ignore[import]
    SIMULATION = False
    logger.info("GPIO: 使用真实 RPi.GPIO")
except (ImportError, RuntimeError):
    logger.warning("GPIO: 未检测到 RPi.GPIO，进入模拟模式（不操作真实引脚）")
    SIMULATION = True

    class _FakeGPIO:  # noqa: N801
        BCM = "BCM"
        OUT = "OUT"

        def setmode(self, *_a) -> None:
            pass

        def setwarnings(self, *_a) -> None:
            pass

        def setup(self, *_a, **_kw) -> None:
            pass

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

__all__ = ["GPIO", "SIMULATION"]
