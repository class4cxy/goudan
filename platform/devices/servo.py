"""
摄像头云台控制模块（水平单轴舵机）

硬件：MAKEROBO 扩展板 PWM 舵机接口
  GPIO 12 = 水平轴 Pan（左右旋转）

舵机 PWM 参数（SG90 / MG90S 兼容）：
  频率：50 Hz（20ms 周期）
  脉宽 0.5ms → 占空比  2.5% → 0°
  脉宽 1.5ms → 占空比  7.5% → 90°（中立/正前方）
  脉宽 2.5ms → 占空比 12.5% → 180°
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
    default_angle: float = 90.0  # 上电默认角度（逻辑角度，归中位置）
    invert: bool = False         # True = 舵机安装方向与角度约定相反，自动镜像物理信号
    speed_deg_per_s: float = 120.0
    # 受控移动速度（度/秒）。SG90 机械最大速度约 600°/s。
    # 0 = 直接跳到目标（旧行为，最快）；典型值：30=慢，60=中，120=偏快。


@dataclass
class CameraConfig:
    """摄像头云台配置（水平单轴）。"""

    pan: ServoConfig   # 水平轴


# MAKEROBO 扩展板实测配置
# Pan 反向安装：invert=True，physical = min + max - logical
# Pan：0°=最左、110°=正前（物理 70°）、180°=最右
DEFAULT_CAMERA_CONFIG = CameraConfig(
    pan=ServoConfig(
        pin=13,                   # 实测确认：GPIO 13 / Pin 33 / PWM1
        min_angle=0.0,
        max_angle=180.0,
        default_angle=90.0,       # 物理中立位；如需偏移再通过 servo_test.py 校准后更新
        invert=False,             # 校准前先不反转，实测方向后再决定
        speed_deg_per_s=60.0,     # 60°/s：从中位到端点约 1.5s，平滑但不迟钝
    ),
)


# ── 单轴舵机 ──────────────────────────────────────────────────────────

class Servo:
    """单轴舵机控制器。

    PWM 常驻策略：setup() 后 PWM 信号持续输出，set_angle 直接 ChangeDutyCycle。
    原「脉冲到位即停」模式是为了消除多路软件 PWM 互相干扰，现仅一路硬件 PWM，
    rpi-lgpio stop()+start() 重启行为不稳定，改为常驻更可靠。
    """

    # 最终到位后额外稳定等待（秒）；渐进移动时已有步进延迟，此值仅用于末位稳定
    SETTLE_S: float = 0.05
    # 渐进移动每步角度（度）；越小越平滑，越小 CPU 调用越频繁
    _STEP_DEG: float = 1.0

    def __init__(self, name: str, config: ServoConfig) -> None:
        self.name = name
        self._pin = config.pin
        self._min = config.min_angle
        self._max = config.max_angle
        self._default = config.default_angle
        self._invert = config.invert
        self._speed = config.speed_deg_per_s
        self._pwm: object | None = None
        self._current_angle: float = config.default_angle

    def _to_physical(self, logical: float) -> float:
        """将逻辑角度转为物理 PWM 角度（invert=True 时镜像）。"""
        return (self._min + self._max - logical) if self._invert else logical

    def setup(self) -> None:
        """初始化 GPIO，将舵机移到默认角度，PWM 保持常驻输出。"""
        GPIO.setup(self._pin, GPIO.OUT)
        self._pwm = GPIO.PWM(self._pin, _PWM_FREQ)
        self._pwm.start(_angle_to_duty(self._to_physical(self._default)))
        time.sleep(self.SETTLE_S)   # 等待舵机到达默认位置
        logger.debug(
            "[舵机-%s] 初始化完成，默认角度=%.1f°（物理=%.1f°，invert=%s）",
            self.name, self._default, self._to_physical(self._default), self._invert,
        )

    def set_angle(self, angle: float) -> float:
        """
        移动到目标逻辑角度。

        当 speed_deg_per_s > 0 时以受控速度渐进移动，否则直接跳到目标。
        含阻塞 sleep，异步上下文中应通过 asyncio.to_thread 调用。

        Returns:
            实际设置的逻辑角度（钳位后）
        """
        clamped = max(self._min, min(self._max, angle))
        if abs(clamped - angle) > 0.01:
            logger.debug(
                "[舵机-%s] %.1f° 超出范围 [%.1f°, %.1f°]，钳位至 %.1f°",
                self.name, angle, self._min, self._max, clamped,
            )

        total_deg = abs(clamped - self._current_angle)
        if self._speed > 0 and total_deg > self._STEP_DEG:
            # 渐进移动：每步 _STEP_DEG 度，按 speed_deg_per_s 控速
            step_delay = self._STEP_DEG / self._speed
            direction = 1.0 if clamped > self._current_angle else -1.0
            pos = self._current_angle
            while abs(clamped - pos) > self._STEP_DEG:
                pos += direction * self._STEP_DEG
                self._pwm.ChangeDutyCycle(_angle_to_duty(self._to_physical(pos)))
                time.sleep(step_delay)
            logger.debug(
                "[舵机-%s] → %.1f°（物理=%.1f°，%.0f°/s，耗时约 %.2fs）",
                self.name, clamped, self._to_physical(clamped),
                self._speed, total_deg / self._speed,
            )

        # 最终精确落点
        self._pwm.ChangeDutyCycle(_angle_to_duty(self._to_physical(clamped)))
        self._current_angle = clamped
        time.sleep(self.SETTLE_S)
        return clamped

    def move_by(self, delta: float) -> float:
        """相对当前角度偏移，返回实际角度。"""
        return self.set_angle(self._current_angle + delta)

    def center(self) -> None:
        """归位到默认角度。"""
        self.set_angle(self._default)

    def cleanup(self) -> None:
        """归中后停止 PWM，释放 GPIO 资源。"""
        if self._pwm is not None:
            self.center()
            self._pwm.stop()

    @property
    def current_angle(self) -> float:
        """当前逻辑角度（度）。"""
        return self._current_angle

    @property
    def min_angle(self) -> float:
        return self._min

    @property
    def max_angle(self) -> float:
        return self._max


# ── 水平单轴云台 ──────────────────────────────────────────────────────

class CameraMount:
    """
    摄像头云台控制器（水平单轴 Pan）。

    逻辑角度约定（invert=True，物理 = min + max - logical）：
      Pan  0° = 最左  |  110° = 正前方（逻辑 → 物理 70°）  |  180° = 最右

    用法示例::

        cam = CameraMount()
        cam.pan_to(45)                       # 向左转 45°
        cam.pan_by(-10)                      # 向左偏移 10°
        await cam.sweep_pan(60, 120, step=3) # 水平扫描
        cam.center()                         # 归中
        cam.cleanup()                        # 程序退出前调用
    """

    def __init__(self, config: CameraConfig = DEFAULT_CAMERA_CONFIG) -> None:
        self._pan_servo = Servo("pan", config.pan)
        self._pan_servo.setup()
        logger.info(
            "摄像头云台初始化完成（%s）  Pan GPIO%d [%.0f°–%.0f°]",
            "模拟模式" if SIMULATION else "GPIO 模式",
            config.pan.pin, config.pan.min_angle, config.pan.max_angle,
        )

    # ── 绝对定位 ──────────────────────────────────────────────────

    def pan_to(self, angle: float) -> float:
        """水平转到指定角度（0°=最左，110°=正前，180°=最右）。"""
        actual = self._pan_servo.set_angle(angle)
        logger.debug("[云台] Pan → %.1f°", actual)
        return actual

    # ── 相对偏移 ──────────────────────────────────────────────────

    def pan_by(self, delta: float) -> float:
        """水平相对偏移（正=右，负=左）。"""
        return self._pan_servo.move_by(delta)

    def center(self) -> None:
        """归中（正视前方）。"""
        self._pan_servo.center()
        logger.debug("[云台] 归中")

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
            step = -step
        angle = from_angle
        while (step > 0 and angle <= to_angle) or (step < 0 and angle >= to_angle):
            self.pan_to(angle)
            await asyncio.sleep(delay)
            angle += step

    # ── 状态查询 ──────────────────────────────────────────────────

    @property
    def status(self) -> dict[str, float]:
        """当前水平角度。"""
        return {"pan": self._pan_servo.current_angle}

    @property
    def limits(self) -> dict[str, dict[str, float]]:
        """水平轴角度硬件限制范围。"""
        return {
            "pan": {"min": self._pan_servo.min_angle, "max": self._pan_servo.max_angle},
        }

    # ── 资源清理 ──────────────────────────────────────────────────

    def cleanup(self) -> None:
        """归中后释放 GPIO 资源。程序退出前必须调用。"""
        self._pan_servo.cleanup()
        logger.info("云台 GPIO 已清理")
