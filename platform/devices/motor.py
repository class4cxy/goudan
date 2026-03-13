"""
单个 DC 电机控制器（H 桥一路）

支持两种驱动模式，通过 MotorPins.en 自动切换：

  模式 A — EN + IN 模式（L298N 类驱动板）
    en >= 0：in1/in2 控制方向，en 引脚输出 PWM 控制速度
    接线：IN1 → in1, IN2 → in2, ENA/ENB → en

  模式 B — 直接 IN PWM 模式（DRV8833 / SW-6008 类驱动板）
    en = -1（默认）：对 in1/in2 直接输出 PWM，无需外部 EN 引脚
    MAKEROBO 功能扩展板使用此模式（EN 已内部接高电平）
    接线：IN1 → in1, IN2 → in2（无需接 EN）

转向逻辑（两种模式一致）：
  in1 PWM / HIGH，in2 LOW  → forward（正转）
  in1 LOW，in2 PWM / HIGH  → backward（反转）
  in1 LOW，in2 LOW          → stop（制动）
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .gpio_adapter import GPIO


@dataclass(frozen=True)
class MotorPins:
    """单个电机的 GPIO 引脚配置（BCM 编号）。

    en = -1（默认）：驱动板无需外部 EN 信号，速度通过 IN 引脚 PWM 控制。
    en >= 0：传统 L298N 接法，速度通过 EN 引脚 PWM 控制。
    """

    in1: int
    in2: int
    en: int = -1  # -1 = 不使用外部 EN 引脚


class Motor:
    """单个 DC 电机控制器，支持 EN 模式和直接 IN PWM 模式。"""

    PWM_FREQ: int = 1000  # Hz

    def __init__(self, position: str, pins: MotorPins) -> None:
        self.position = position
        self._in1 = pins.in1
        self._in2 = pins.in2
        self._en = pins.en

        # EN 模式
        self._pwm: object | None = None
        # 直接 IN PWM 模式
        self._pwm_fwd: object | None = None
        self._pwm_bwd: object | None = None

        self._current_speed: int = 0

    @property
    def _direct_in_mode(self) -> bool:
        """True = 直接 IN PWM 模式（en=-1）；False = 传统 EN 模式。"""
        return self._en < 0

    def setup(self) -> None:
        """初始化 GPIO 引脚并根据模式启动 PWM。"""
        GPIO.setup(self._in1, GPIO.OUT)
        GPIO.setup(self._in2, GPIO.OUT)
        GPIO.output(self._in1, False)
        GPIO.output(self._in2, False)

        if self._direct_in_mode:
            # 对 in1/in2 分别建立 PWM，起始占空比 0（停止）
            self._pwm_fwd = GPIO.PWM(self._in1, self.PWM_FREQ)
            self._pwm_bwd = GPIO.PWM(self._in2, self.PWM_FREQ)
            self._pwm_fwd.start(0)
            self._pwm_bwd.start(0)
        else:
            GPIO.setup(self._en, GPIO.OUT)
            self._pwm = GPIO.PWM(self._en, self.PWM_FREQ)
            self._pwm.start(0)

    def forward(self, speed: int = 60) -> None:
        """正转。speed 为占空比（0–100）。"""
        speed = _clamp(speed)
        if self._direct_in_mode:
            self._pwm_bwd.ChangeDutyCycle(0)
            GPIO.output(self._in2, False)
            self._pwm_fwd.ChangeDutyCycle(speed)
        else:
            GPIO.output(self._in1, True)
            GPIO.output(self._in2, False)
            self._pwm.ChangeDutyCycle(speed)
        self._current_speed = speed

    def backward(self, speed: int = 60) -> None:
        """反转。speed 为占空比（0–100）。"""
        speed = _clamp(speed)
        if self._direct_in_mode:
            self._pwm_fwd.ChangeDutyCycle(0)
            GPIO.output(self._in1, False)
            self._pwm_bwd.ChangeDutyCycle(speed)
        else:
            GPIO.output(self._in1, False)
            GPIO.output(self._in2, True)
            self._pwm.ChangeDutyCycle(speed)
        self._current_speed = -speed

    def stop(self) -> None:
        """制动停止（两个 IN 引脚均归零）。"""
        if self._direct_in_mode:
            self._pwm_fwd.ChangeDutyCycle(0)
            self._pwm_bwd.ChangeDutyCycle(0)
            GPIO.output(self._in1, False)
            GPIO.output(self._in2, False)
        else:
            GPIO.output(self._in1, False)
            GPIO.output(self._in2, False)
            self._pwm.ChangeDutyCycle(0)
        self._current_speed = 0

    def cleanup(self) -> None:
        """停止 PWM 输出，释放资源。"""
        if self._direct_in_mode:
            if self._pwm_fwd is not None:
                self._pwm_fwd.stop()
            if self._pwm_bwd is not None:
                self._pwm_bwd.stop()
        else:
            if self._pwm is not None:
                self._pwm.stop()

    @property
    def current_speed(self) -> int:
        """当前速度：正数=正转（0–100），负数=反转（-100–0），0=停止。"""
        return self._current_speed


def _clamp(speed: int) -> int:
    """将速度值钳位到 [0, 100]。"""
    return max(0, min(100, speed))
