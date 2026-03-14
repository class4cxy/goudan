"""
platform/devices — 机器车硬件抽象层

公开接口：

  底盘（4轮差速驱动）
    Chassis            — 4轮底盘控制器
    ChassisConfig      — 底盘引脚配置数据类
    DEFAULT_CONFIG     — MAKEROBO 扩展板实测 GPIO 引脚映射
    Motor              — 单电机控制器（底层）
    MotorPins          — 单电机引脚数据类

  摄像头云台（双轴舵机）
    CameraMount        — 双轴云台控制器
    CameraConfig       — 云台配置数据类
    DEFAULT_CAMERA_CONFIG — MAKEROBO 扩展板实测配置（含垂直轴限位）
    Servo              — 单轴舵机控制器（底层）
    ServoConfig        — 单轴舵机配置数据类

  摄像头采集（USB / RTSP 拍照）
    Camera             — 摄像头拍照控制器
    CaptureConfig      — 采集参数配置数据类
    DEFAULT_CAPTURE_CONFIG — 默认配置（USB /dev/video0，640×480）

  麦克风（VAD 采集）
    Microphone         — 麦克风 VAD 采集器，通过回调推送语音事件

  扬声器（TTS 播放）
    Speaker            — TTS + 扬声器播放器，通过回调通知播放状态

  激光雷达（LD06，CP2102 USB-TTL 串口）
    Lidar              — LD06 激光雷达控制器，通过回调推送完整圈扫描数据
    LidarConfig        — 雷达串口配置数据类
    DEFAULT_LIDAR_CONFIG — 默认配置（/dev/ttyUSB0，230400bps）
    LidarScan          — 一圈扫描结果数据类
    LidarPoint         — 单个测距点数据类
"""

from .chassis import Chassis, ChassisConfig, DEFAULT_CONFIG
from .motor import Motor, MotorPins
from .servo import CameraMount, CameraConfig, DEFAULT_CAMERA_CONFIG, Servo, ServoConfig
from .camera import Camera, CaptureConfig, DEFAULT_CAPTURE_CONFIG
from .microphone import Microphone
from .speaker import Speaker
from .lidar import Lidar, LidarConfig, DEFAULT_LIDAR_CONFIG, LidarScan, LidarPoint
from .power_sensor import PowerSensor, PowerSensorConfig, DEFAULT_POWER_CONFIG, PowerReading

__all__ = [
    # 底盘
    "Chassis",
    "ChassisConfig",
    "DEFAULT_CONFIG",
    "Motor",
    "MotorPins",
    # 云台
    "CameraMount",
    "CameraConfig",
    "DEFAULT_CAMERA_CONFIG",
    "Servo",
    "ServoConfig",
    # 摄像头采集
    "Camera",
    "CaptureConfig",
    "DEFAULT_CAPTURE_CONFIG",
    # 音频
    "Microphone",
    "Speaker",
    # 激光雷达
    "Lidar",
    "LidarConfig",
    "DEFAULT_LIDAR_CONFIG",
    "LidarScan",
    "LidarPoint",
    # 电源传感器
    "PowerSensor",
    "PowerSensorConfig",
    "DEFAULT_POWER_CONFIG",
    "PowerReading",
]
