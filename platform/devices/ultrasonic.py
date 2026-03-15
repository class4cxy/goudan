"""
Ultrasonic — HC-SR04 超声波测距传感器硬件抽象层
================================================
职责：
  1. 通过 GPIO Trig/Echo 进行单次测距（厘米）
  2. 可选后台轮询，持续更新 latest_reading
  3. 在距离过近时触发 on_too_close 回调
  4. 非树莓派环境自动降级为模拟模式

不含任何 WebSocket / FastAPI 逻辑，纯硬件操作。

接线（默认 BCM 引脚）：
  Trig -> GPIO20
  Echo -> GPIO21
  VCC  -> 5V
  GND  -> GND

注意：
  HC-SR04 Echo 为 5V 电平。若扩展板未做电平转换，必须串联分压后再接入树莓派 GPIO。
"""

from __future__ import annotations

import logging
import math
import threading
import time
from dataclasses import dataclass
from typing import Callable

try:
    from .gpio_adapter import GPIO, SIMULATION
except ImportError:
    from gpio_adapter import GPIO, SIMULATION

logger = logging.getLogger(__name__)


@dataclass
class UltrasonicReading:
    """单次超声波测距读数。"""

    timestamp_ms: int
    distance_cm: float
    is_too_close: bool

    def to_dict(self) -> dict:
        return {
            "timestamp_ms": self.timestamp_ms,
            "distance_cm": round(self.distance_cm, 2),
            "is_too_close": self.is_too_close,
        }


@dataclass
class UltrasonicConfig:
    """HC-SR04 配置。"""

    trig_pin: int = 20
    echo_pin: int = 21
    poll_interval_s: float = 0.2
    timeout_s: float = 0.03
    min_distance_cm: float = 2.0
    max_distance_cm: float = 450.0
    speed_of_sound_cm_s: float = 34300.0
    too_close_threshold_cm: float = 25.0
    too_close_cooldown_s: float = 1.5


DEFAULT_ULTRASONIC_CONFIG = UltrasonicConfig()


class Ultrasonic:
    """HC-SR04 控制器（纯硬件层）。"""

    def __init__(
        self,
        config: UltrasonicConfig | None = None,
        on_reading: Callable[[UltrasonicReading], None] | None = None,
        on_too_close: Callable[[UltrasonicReading], None] | None = None,
    ):
        self._cfg = config or DEFAULT_ULTRASONIC_CONFIG
        self._on_reading = on_reading
        self._on_too_close = on_too_close

        self._is_simulation = SIMULATION
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._latest: UltrasonicReading | None = None
        self._last_too_close_at = 0.0
        self._sim_phase = 0.0

    def start(self) -> None:
        """初始化 GPIO 并启动后台轮询线程（非阻塞）。"""
        if self._is_simulation:
            logger.warning("[Ultrasonic] GPIO 模拟模式，测距将返回模拟数据")
        else:
            try:
                GPIO.setwarnings(False)
                GPIO.setmode(GPIO.BCM)
                GPIO.setup(self._cfg.trig_pin, GPIO.OUT)
                GPIO.setup(self._cfg.echo_pin, GPIO.IN)
                GPIO.output(self._cfg.trig_pin, False)
                time.sleep(0.05)
                logger.info(
                    "[Ultrasonic] 已初始化 Trig=GPIO%s Echo=GPIO%s",
                    self._cfg.trig_pin,
                    self._cfg.echo_pin,
                )
            except Exception as e:
                raise RuntimeError(
                    "初始化 GPIO 失败："
                    f"Trig=GPIO{self._cfg.trig_pin}, Echo=GPIO{self._cfg.echo_pin}。"
                    "请检查：1) 当前用户 GPIO 权限；2) 引脚是否被 SPI/I2S/其他进程占用；"
                    "3) 线序与电平转换。原始错误："
                    f"{e}"
                ) from e

        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._poll_loop,
            daemon=True,
            name="ultrasonic_poll",
        )
        self._thread.start()
        logger.info("[Ultrasonic] 轮询线程已启动（interval=%.2fs）", self._cfg.poll_interval_s)

    def stop(self) -> None:
        """停止轮询线程。"""
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=3.0)
            self._thread = None
        logger.info("[Ultrasonic] 已停止")

    @property
    def is_simulation(self) -> bool:
        return self._is_simulation

    @property
    def latest_reading(self) -> UltrasonicReading | None:
        with self._lock:
            return self._latest

    @property
    def status(self) -> dict:
        latest = self.latest_reading
        return {
            "is_simulation": self._is_simulation,
            "is_running": self._thread is not None and self._thread.is_alive(),
            "trig_pin": self._cfg.trig_pin,
            "echo_pin": self._cfg.echo_pin,
            "poll_interval_s": self._cfg.poll_interval_s,
            "too_close_threshold_cm": self._cfg.too_close_threshold_cm,
            "latest": latest.to_dict() if latest else None,
        }

    def read_once(self) -> UltrasonicReading | None:
        """同步执行一次测距，失败返回 None。"""
        if self._is_simulation:
            return self._simulate_reading()

        distance = self._measure_distance_cm()
        if distance is None:
            return None

        reading = UltrasonicReading(
            timestamp_ms=int(time.time() * 1000),
            distance_cm=distance,
            is_too_close=distance < self._cfg.too_close_threshold_cm,
        )
        with self._lock:
            self._latest = reading
        return reading

    def _poll_loop(self) -> None:
        while not self._stop_event.is_set():
            reading = self.read_once()
            if reading:
                self._emit_callbacks(reading)
            self._stop_event.wait(self._cfg.poll_interval_s)

    def _emit_callbacks(self, reading: UltrasonicReading) -> None:
        if self._on_reading:
            try:
                self._on_reading(reading)
            except Exception as e:
                logger.warning("[Ultrasonic] on_reading 回调异常：%s", e)

        if reading.is_too_close and self._on_too_close:
            now = time.time()
            if now - self._last_too_close_at >= self._cfg.too_close_cooldown_s:
                self._last_too_close_at = now
                try:
                    self._on_too_close(reading)
                except Exception as e:
                    logger.warning("[Ultrasonic] on_too_close 回调异常：%s", e)

    def _simulate_reading(self) -> UltrasonicReading:
        # 在 20cm~110cm 区间内轻微抖动，便于本地联调。
        self._sim_phase = (self._sim_phase + 0.35) % 6.28
        distance = 65.0 + 45.0 * math.sin(self._sim_phase)
        distance = max(20.0, min(110.0, distance))

        reading = UltrasonicReading(
            timestamp_ms=int(time.time() * 1000),
            distance_cm=distance,
            is_too_close=distance < self._cfg.too_close_threshold_cm,
        )
        with self._lock:
            self._latest = reading
        return reading

    def _measure_distance_cm(self) -> float | None:
        GPIO.output(self._cfg.trig_pin, False)
        time.sleep(0.000002)
        GPIO.output(self._cfg.trig_pin, True)
        time.sleep(0.00001)
        GPIO.output(self._cfg.trig_pin, False)

        echo_start = self._wait_for_level(1, self._cfg.timeout_s)
        if echo_start is None:
            return None
        echo_end = self._wait_for_level(0, self._cfg.timeout_s)
        if echo_end is None:
            return None

        pulse_duration = max(0.0, echo_end - echo_start)
        distance_cm = pulse_duration * self._cfg.speed_of_sound_cm_s / 2.0
        if distance_cm < self._cfg.min_distance_cm or distance_cm > self._cfg.max_distance_cm:
            return None
        return distance_cm

    def _wait_for_level(self, level: int, timeout_s: float) -> float | None:
        deadline = time.perf_counter() + timeout_s
        while time.perf_counter() < deadline:
            if GPIO.input(self._cfg.echo_pin) == level:
                return time.perf_counter()
        return None

