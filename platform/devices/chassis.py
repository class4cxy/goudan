"""
4轮差速底盘控制器

硬件：2×L298N H 桥驱动板（各控制两路电机）

运动模型：差速转向（Skid Steering / Tank Drive）
坐标约定（俯视图，车头朝上）：
  ┌──────────────┐
  │ [FL]    [FR] │  ← 前轮
  │              │
  │ [RL]    [RR] │  ← 后轮
  └──────────────┘

差速转向逻辑：
  前进(forward)    → 四轮同方向正转
  后退(backward)   → 四轮同方向反转
  左转(turn_left)  → 左侧轮反转 / 右侧轮正转（原地左旋）
  右转(turn_right) → 右侧轮反转 / 左侧轮正转（原地右旋）
  停止(stop)       → 四轮制动

默认 GPIO 引脚（BCM 编号）见 DEFAULT_CONFIG，与 motor_test.py 保持一致。
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from .gpio_adapter import GPIO, SIMULATION
from .motor import Motor, MotorPins

logger = logging.getLogger(__name__)

# 有效运动指令集合（供外部校验）
VALID_COMMANDS: frozenset[str] = frozenset(
    {"forward", "backward", "turn_left", "turn_right", "stop"}
)

# 有效电机位置集合（供外部校验）
VALID_POSITIONS: frozenset[str] = frozenset(
    {"front_left", "front_right", "rear_left", "rear_right"}
)


@dataclass
class ChassisConfig:
    """底盘四路电机的 GPIO 引脚配置。"""

    front_left: MotorPins
    front_right: MotorPins
    rear_left: MotorPins
    rear_right: MotorPins
    default_speed: int = 60  # 全局默认速度（0–100）


# MAKEROBO 功能扩展板实测引脚（Phase A GPIO 探针确认，BCM 编号）
# 驱动芯片（SW-6008）EN 已内板接高电平，速度通过 IN 引脚 PWM 控制（en=-1）
# M4 右后轮 in2=17 为推测值（Phase A 显示"有动静但无法判断"），如不对请改为实测值
DEFAULT_CONFIG = ChassisConfig(
    front_left=MotorPins(in1=24, in2=25),   # M1 左前轮 — Phase A 确认
    front_right=MotorPins(in1=27, in2=26),  # M2 右前轮 — Phase A 确认
    rear_left=MotorPins(in1=5,  in2=6),     # M3 左后轮 — Phase A 确认
    rear_right=MotorPins(in1=22, in2=9),    # M4 右后轮 — 全部实测确认
)


class Chassis:
    """
    4轮差速底盘控制器。

    用法示例::

        chassis = Chassis()                  # 使用默认引脚配置
        chassis.forward(speed=70)            # 前进，速度 70%
        await chassis.execute_timed("forward", speed=60, duration=2.0)  # 前进 2 秒后自动停止
        chassis.stop()
        chassis.cleanup()                    # 程序退出前调用
    """

    def __init__(self, config: ChassisConfig = DEFAULT_CONFIG) -> None:
        GPIO.setmode(GPIO.BCM)
        GPIO.setwarnings(False)

        self._config = config
        self._default_speed = config.default_speed
        self._motors: dict[str, Motor] = {
            "front_left": Motor("front_left", config.front_left),
            "front_right": Motor("front_right", config.front_right),
            "rear_left": Motor("rear_left", config.rear_left),
            "rear_right": Motor("rear_right", config.rear_right),
        }
        for motor in self._motors.values():
            motor.setup()

        self._timed_task: asyncio.Task | None = None

        logger.info(
            "底盘初始化完成（%s）",
            "模拟模式" if SIMULATION else "GPIO 真实引脚",
        )

    # ── 整车运动指令 ──────────────────────────────────────────────

    def forward(self, speed: int | None = None) -> None:
        """四轮同步正转，车辆前进。"""
        s = self._resolve_speed(speed)
        for motor in self._motors.values():
            motor.forward(s)
        logger.debug("[底盘] 前进 speed=%d", s)

    def backward(self, speed: int | None = None) -> None:
        """四轮同步反转，车辆后退。"""
        s = self._resolve_speed(speed)
        for motor in self._motors.values():
            motor.backward(s)
        logger.debug("[底盘] 后退 speed=%d", s)

    def turn_left(self, speed: int | None = None) -> None:
        """原地左转：左侧轮反转，右侧轮正转。"""
        s = self._resolve_speed(speed)
        self._motors["front_left"].backward(s)
        self._motors["rear_left"].backward(s)
        self._motors["front_right"].forward(s)
        self._motors["rear_right"].forward(s)
        logger.debug("[底盘] 左转 speed=%d", s)

    def turn_right(self, speed: int | None = None) -> None:
        """原地右转：右侧轮反转，左侧轮正转。"""
        s = self._resolve_speed(speed)
        self._motors["front_left"].forward(s)
        self._motors["rear_left"].forward(s)
        self._motors["front_right"].backward(s)
        self._motors["rear_right"].backward(s)
        logger.debug("[底盘] 右转 speed=%d", s)

    def stop(self) -> None:
        """所有电机制动停止。"""
        for motor in self._motors.values():
            motor.stop()
        logger.debug("[底盘] 停止")

    # ── 定时动作 ──────────────────────────────────────────────────

    async def execute_timed(
        self,
        command: str,
        speed: int | None = None,
        duration: float | None = None,
    ) -> None:
        """
        执行运动指令，可选定时自动停止。

        新指令到来时会自动取消上一条未完成的定时停止任务。

        Args:
            command:  运动指令，取值范围见 VALID_COMMANDS
            speed:    速度 0–100，None 时使用 default_speed
            duration: 持续时间（秒）。None 表示持续运动，直到显式发送 stop
        Raises:
            ValueError: 未知 command
        """
        self._cancel_timed_task()
        self._dispatch(command, speed)

        if duration is not None and duration > 0 and command != "stop":
            self._timed_task = asyncio.create_task(self._deferred_stop(duration))

    # ── 单电机精细控制 ────────────────────────────────────────────

    def set_motor(
        self,
        position: str,
        direction: str,
        speed: int | None = None,
    ) -> None:
        """
        控制单个电机，用于调试或特殊动作。

        Args:
            position:  电机位置，取值范围见 VALID_POSITIONS
            direction: 方向 forward / backward / stop
            speed:     0–100，None 使用默认速度
        Raises:
            ValueError: 未知 position 或 direction
        """
        motor = self._motors.get(position)
        if motor is None:
            raise ValueError(
                f"未知电机位置 {position!r}，有效值：{sorted(VALID_POSITIONS)}"
            )

        s = self._resolve_speed(speed)
        if direction == "forward":
            motor.forward(s)
        elif direction == "backward":
            motor.backward(s)
        elif direction == "stop":
            motor.stop()
        else:
            raise ValueError(
                f"未知方向 {direction!r}，有效值：forward / backward / stop"
            )

    # ── 状态查询 ──────────────────────────────────────────────────

    @property
    def status(self) -> dict[str, int]:
        """
        返回各电机当前速度快照。

        返回值示例::
            {
                "front_left":  60,   # 正转 60%
                "front_right": 60,
                "rear_left":   -60,  # 反转 60%（负号表示反向）
                "rear_right":  -60,
            }
        """
        return {pos: motor.current_speed for pos, motor in self._motors.items()}

    @property
    def is_simulation(self) -> bool:
        """当前是否为模拟模式（非树莓派环境）。"""
        return SIMULATION

    # ── 资源清理 ──────────────────────────────────────────────────

    def cleanup(self) -> None:
        """停止所有电机并释放 GPIO 资源。程序退出前必须调用。"""
        self._cancel_timed_task()
        self.stop()
        for motor in self._motors.values():
            motor.cleanup()
        GPIO.cleanup()
        logger.info("底盘 GPIO 已清理")

    # ── 内部工具 ──────────────────────────────────────────────────

    def _resolve_speed(self, speed: int | None) -> int:
        """将 None 替换为默认速度，并钳位到 [0, 100]。"""
        s = speed if speed is not None else self._default_speed
        return max(0, min(100, s))

    def _dispatch(self, command: str, speed: int | None) -> None:
        """将字符串指令路由到对应方法。"""
        if command == "forward":
            self.forward(speed)
        elif command == "backward":
            self.backward(speed)
        elif command == "turn_left":
            self.turn_left(speed)
        elif command == "turn_right":
            self.turn_right(speed)
        elif command == "stop":
            self.stop()
        else:
            raise ValueError(
                f"未知运动指令 {command!r}，有效值：{sorted(VALID_COMMANDS)}"
            )

    def _cancel_timed_task(self) -> None:
        """取消尚未触发的定时停止任务。"""
        if self._timed_task and not self._timed_task.done():
            self._timed_task.cancel()
        self._timed_task = None

    async def _deferred_stop(self, delay: float) -> None:
        """等待 delay 秒后自动停止。"""
        await asyncio.sleep(delay)
        self.stop()
        logger.debug("[底盘] 定时停止（delay=%.2fs）", delay)
