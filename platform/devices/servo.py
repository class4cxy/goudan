"""
摄像头云台控制模块（双轴舵机）

硬件：MAKEROBO 扩展板两路 PWM 舵机接口
  GPIO 12 = 水平轴 Pan（左右旋转）
  GPIO 13 = 垂直轴 Tilt（上下俯仰）

舵机 PWM 参数（SG90 / MG90S 兼容）：
  频率：50 Hz（20ms 周期）
  脉宽 0.5ms → 占空比  2.5% → 0°
  脉宽 1.5ms → 占空比  7.5% → 90°（中立/正前方）
  脉宽 2.5ms → 占空比 12.5% → 180°

垂直轴硬件限制：
  摄像头支架有物理遮挡，可用范围约 ±15°（共 30°），
  默认安全区间 75°–105°，超出范围的角度请求会自动钳位，不会损伤舵机。
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass

from .gpio_adapter import GPIO, SIMULATION

logger = logging.getLogger(__name__)

_PWM_FREQ = 50    # Hz — 舵机标准频率
_DUTY_MIN  = 2.5  # % — 对应 0°
_DUTY_MAX  = 12.5 # % — 对应 180°


def _angle_to_duty(angle: float) -> float:
    """将角度（0–180°）换算为 PWM 占空比（2.5–12.5%）。"""
    return _DUTY_MIN + (_DUTY_MAX - _DUTY_MIN) * angle / 180.0


# ── 配置数据类 ────────────────────────────────────────────────────────

@dataclass
class ServoConfig:
    """单轴舵机配置。"""

    pin: int                   # BCM GPIO 引脚
    min_angle: float = 0.0     # 可用最小角度（度），硬件/机械限制
    max_angle: float = 180.0   # 可用最大角度（度），硬件/机械限制
    default_angle: float = 90.0  # 上电默认角度（归中位置）


@dataclass
class CameraConfig:
    """摄像头云台双轴配置。"""

    pan: ServoConfig   # 水平轴
    tilt: ServoConfig  # 垂直轴


# MAKEROBO 扩展板实测配置
DEFAULT_CAMERA_CONFIG = CameraConfig(
    pan=ServoConfig(
        pin=12,
        min_angle=0.0,
        max_angle=180.0,
        default_angle=90.0,    # 正前方
    ),
    tilt=ServoConfig(
        pin=13,
        min_angle=56.0,
        max_angle=106.0,
        default_angle=90.0,
    ),
)


# ── 单轴舵机 ──────────────────────────────────────────────────────────

class Servo:
    """单轴舵机控制器。"""

    def __init__(self, name: str, config: ServoConfig) -> None:
        self.name = name
        self._pin = config.pin
        self._min = config.min_angle
        self._max = config.max_angle
        self._default = config.default_angle
        self._pwm: object | None = None
        self._current_angle: float = config.default_angle

    def setup(self) -> None:
        """初始化 GPIO，启动 PWM 并归位到默认角度。"""
        GPIO.setup(self._pin, GPIO.OUT)
        self._pwm = GPIO.PWM(self._pin, _PWM_FREQ)
        self._pwm.start(_angle_to_duty(self._default))
        logger.debug("[舵机-%s] 初始化，默认角度=%.1f°", self.name, self._default)

    def set_angle(self, angle: float) -> float:
        """
        设置目标角度，超出范围自动钳位。

        Returns:
            实际设置的角度（钳位后）
        """
        clamped = max(self._min, min(self._max, angle))
        if abs(clamped - angle) > 0.01:
            logger.debug(
                "[舵机-%s] %.1f° 超出范围 [%.1f°, %.1f°]，钳位至 %.1f°",
                self.name, angle, self._min, self._max, clamped,
            )
        self._pwm.ChangeDutyCycle(_angle_to_duty(clamped))
        self._current_angle = clamped
        return clamped

    def move_by(self, delta: float) -> float:
        """相对当前角度偏移，返回实际角度。"""
        return self.set_angle(self._current_angle + delta)

    def center(self) -> None:
        """归位到默认角度。"""
        self.set_angle(self._default)

    def cleanup(self) -> None:
        """归位后停止 PWM，释放资源。"""
        if self._pwm is not None:
            self.center()
            time.sleep(0.3)   # 等待舵机到位
            self._pwm.stop()

    @property
    def current_angle(self) -> float:
        """当前角度（度）。"""
        return self._current_angle

    @property
    def min_angle(self) -> float:
        return self._min

    @property
    def max_angle(self) -> float:
        return self._max


# ── 双轴云台 ──────────────────────────────────────────────────────────

class CameraMount:
    """
    摄像头云台控制器（水平 Pan + 垂直 Tilt）。

    角度约定：
      Pan  0°   = 最左  |  90° = 正前方  |  180° = 最右
      Tilt 75°  = 最低  |  90° = 水平    |  105° = 最高（硬件限制）

    用法示例::

        cam = CameraMount()
        cam.pan_to(45)                       # 向左转 45°
        cam.tilt_to(95)                      # 轻微上仰
        cam.look_at(pan=90, tilt=90)         # 正视前方
        cam.pan_by(-10)                      # 向左偏移 10°
        await cam.sweep_pan(60, 120, step=3) # 水平扫描
        cam.center()                         # 双轴归中
        cam.cleanup()                        # 程序退出前调用
    """

    def __init__(self, config: CameraConfig = DEFAULT_CAMERA_CONFIG) -> None:
        self._pan_servo  = Servo("pan",  config.pan)
        self._tilt_servo = Servo("tilt", config.tilt)
        self._pan_servo.setup()
        self._tilt_servo.setup()
        logger.info(
            "摄像头云台初始化完成（%s）"
            "  Pan  GPIO%d [%.0f°–%.0f°]"
            "  Tilt GPIO%d [%.0f°–%.0f°]",
            "模拟模式" if SIMULATION else "GPIO 模式",
            config.pan.pin,  config.pan.min_angle,  config.pan.max_angle,
            config.tilt.pin, config.tilt.min_angle, config.tilt.max_angle,
        )

    # ── 单轴绝对定位 ──────────────────────────────────────────────

    def pan_to(self, angle: float) -> float:
        """水平转到指定角度（0°=最左，90°=正前，180°=最右）。"""
        actual = self._pan_servo.set_angle(angle)
        logger.debug("[云台] Pan → %.1f°", actual)
        return actual

    def tilt_to(self, angle: float) -> float:
        """
        垂直俯仰到指定角度（角度超出硬件范围时自动钳位）。

        Args:
            angle: 目标角度，安全范围 75°–105°
        """
        actual = self._tilt_servo.set_angle(angle)
        logger.debug("[云台] Tilt → %.1f°", actual)
        return actual

    # ── 单轴相对偏移 ──────────────────────────────────────────────

    def pan_by(self, delta: float) -> float:
        """水平相对偏移（正=右，负=左）。"""
        return self._pan_servo.move_by(delta)

    def tilt_by(self, delta: float) -> float:
        """垂直相对偏移（正=上仰，负=下俯）。"""
        return self._tilt_servo.move_by(delta)

    # ── 双轴联动 ──────────────────────────────────────────────────

    def look_at(self, pan: float, tilt: float) -> dict[str, float]:
        """同时设置水平和垂直角度，返回实际角度字典。"""
        return {
            "pan":  self.pan_to(pan),
            "tilt": self.tilt_to(tilt),
        }

    def center(self) -> None:
        """双轴归中（正视前方）。"""
        self._pan_servo.center()
        self._tilt_servo.center()
        logger.debug("[云台] 双轴归中")

    # ── 异步扫描 ──────────────────────────────────────────────────

    async def sweep_pan(
        self,
        from_angle: float = 0.0,
        to_angle: float = 180.0,
        step: float = 5.0,
        delay: float = 0.05,
    ) -> None:
        """
        水平扫描。

        Args:
            from_angle: 起始角度
            to_angle:   终止角度
            step:       步进（度），正数从左到右，负数从右到左
            delay:      每步等待时间（秒）
        """
        if step == 0:
            return
        if (to_angle - from_angle) * step < 0:
            step = -step   # 自动修正方向
        angle = from_angle
        while (step > 0 and angle <= to_angle) or (step < 0 and angle >= to_angle):
            self.pan_to(angle)
            await asyncio.sleep(delay)
            angle += step

    async def sweep_tilt(
        self,
        from_angle: float | None = None,
        to_angle: float | None = None,
        step: float = 3.0,
        delay: float = 0.05,
    ) -> None:
        """
        垂直扫描（默认在硬件安全范围内扫描）。

        Args:
            from_angle: 起始角度，None = 下限（75°）
            to_angle:   终止角度，None = 上限（105°）
            step:       步进（度）
            delay:      每步等待时间（秒）
        """
        fa = from_angle if from_angle is not None else self._tilt_servo.min_angle
        ta = to_angle   if to_angle   is not None else self._tilt_servo.max_angle
        if step == 0:
            return
        if (ta - fa) * step < 0:
            step = -step
        angle = fa
        while (step > 0 and angle <= ta) or (step < 0 and angle >= fa):
            self.tilt_to(angle)
            await asyncio.sleep(delay)
            angle += step

    # ── 状态查询 ──────────────────────────────────────────────────

    @property
    def status(self) -> dict[str, float]:
        """当前双轴角度。"""
        return {
            "pan":  self._pan_servo.current_angle,
            "tilt": self._tilt_servo.current_angle,
        }

    @property
    def limits(self) -> dict[str, dict[str, float]]:
        """双轴角度硬件限制范围。"""
        return {
            "pan":  {"min": self._pan_servo.min_angle,  "max": self._pan_servo.max_angle},
            "tilt": {"min": self._tilt_servo.min_angle, "max": self._tilt_servo.max_angle},
        }

    # ── 资源清理 ──────────────────────────────────────────────────

    def cleanup(self) -> None:
        """双轴归中后释放 GPIO 资源。程序退出前必须调用。"""
        self._pan_servo.cleanup()
        self._tilt_servo.cleanup()
        logger.info("云台 GPIO 已清理")
