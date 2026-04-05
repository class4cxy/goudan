"""
IMU — MPU6050/MPU6500 陀螺仪/加速度计驱动
==========================================
职责：
  - 通过 I2C（smbus2）读取 MPU6050/MPU6500 传感器寄存器
  - 后台线程 100Hz 持续采样，外部可随时读取最新数据
  - 启动时自动静止校准陀螺仪零偏（Z 轴）
  - 非树莓派或 I2C 不可用时自动降级为模拟模式

芯片兼容说明：
  MPU6050（WHO_AM_I=0x68）和 MPU6500（WHO_AM_I=0x70）寄存器兼容，
  GY-521 模块可能搭载其中任意一款，均可正常使用。

接线说明（MPU6050/6500 与树莓派5）：
  MPU VCC → 3.3V
  MPU GND → GND
  MPU SDA → GPIO 2（I2C1 SDA，物理引脚 3）
  MPU SCL → GPIO 3（I2C1 SCL，物理引脚 5）
  MPU AD0 → GND（I2C 地址 0x68；接 3.3V 则为 0x69）

启用 I2C：
  sudo raspi-config → Interface Options → I2C → Enable

安装依赖：
  pip install smbus2

主要输出：
  gyro_z  (°/s)   — 偏航角速度，供 Odometry 融合转向角
  accel_x/y (g)   — 加速度，可用于检测碰撞/斜坡（当前仅采集，未深度使用）
"""

import math
import os
import threading
import time
import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)

# ── MPU6050 寄存器地址 ─────────────────────────────────────────────
_REG_PWR_MGMT_1  = 0x6B
_REG_SMPLRT_DIV  = 0x19
_REG_CONFIG      = 0x1A
_REG_GYRO_CONFIG = 0x1B
_REG_ACCEL_CONFIG = 0x1C
_REG_ACCEL_XOUT_H = 0x3B   # Accel X/Y/Z + Temp + Gyro X/Y/Z = 14 bytes 连续

# ── 量程与灵敏度 ───────────────────────────────────────────────────
# GYRO_CONFIG = 0x00 → ±250°/s，灵敏度 131 LSB/(°/s)
_GYRO_FS_250_DEG  = 0x00
_GYRO_SCALE       = 131.0   # LSB per (°/s)
# ACCEL_CONFIG = 0x00 → ±2g，灵敏度 16384 LSB/g
_ACCEL_SCALE      = 16384.0  # LSB per g

# 校准采样帧数（启动后静止 ~0.5s）
_CALIBRATION_FRAMES = 50


@dataclass
class ImuReading:
    """单次 IMU 采样结果。"""
    gyro_x:    float   # °/s
    gyro_y:    float   # °/s
    gyro_z:    float   # °/s（已去零偏；顺时针为正，匹配底盘坐标系）
    accel_x:   float   # g
    accel_y:   float   # g
    accel_z:   float   # g
    timestamp: float   # time.monotonic()


class Imu:
    """
    MPU6050/MPU6500 驱动，后台 100Hz 采样。

    线程安全：
      - _i2c_lock：串行化所有 SMBus 操作，防止采样线程与校准主线程并发访问
        导致 I2C 总线冲突（Errno 121 Remote I/O error → Errno 110 总线锁死）。
      - _lock：保护 _latest 读写。
    非树莓派或 smbus2 未安装时自动降级，get_latest() 返回 None。
    """

    I2C_BUS   = 1       # RPi 默认 I2C 总线编号
    SAMPLE_HZ = 100

    def __init__(self, i2c_addr: int | None = None) -> None:
        addr_str = os.environ.get("IMU_I2C_ADDR", "0x68")
        self._addr = i2c_addr or int(addr_str, 16)
        self._bus            = None
        self._is_simulation  = False
        self._i2c_lock       = threading.Lock()   # 保护 SMBus 并发访问
        self._lock           = threading.Lock()   # 保护 _latest 读写
        self._latest: ImuReading | None = None
        self._thread: threading.Thread | None = None
        self._running        = False
        self._gyro_bias_z    = 0.0

    # ─── 生命周期 ─────────────────────────────────────────────────

    def start(self) -> bool:
        """
        初始化 I2C 并启动后台采样线程。

        Returns:
            True  = 真实硬件已就绪
            False = 降级为模拟模式
        """
        try:
            import smbus2
            self._bus = smbus2.SMBus(self.I2C_BUS)
            self._init_device()
            self._running = True
            self._thread  = threading.Thread(
                target=self._sample_loop,
                daemon=True,
                name="imu-sampler",
            )
            self._thread.start()
            # 等待采样稳定后校准零偏
            time.sleep(0.6)
            self._calibrate_gyro()
            logger.info(
                f"[IMU] MPU6050 已启动（0x{self._addr:02X}），"
                f"gyro_bias_z={self._gyro_bias_z:.3f}°/s"
            )
            return True
        except Exception as e:
            logger.warning(f"[IMU] 初始化失败，降级为模拟模式：{e}")
            self._is_simulation = True
            return False

    def stop(self) -> None:
        self._running = False
        if self._thread:
            self._thread.join(timeout=2.0)
        if self._bus:
            try:
                self._bus.close()
            except Exception:
                pass

    # ─── 内部：设备初始化 ─────────────────────────────────────────

    def _init_device(self) -> None:
        """唤醒 MPU6050/MPU6500，配置量程 & 采样率。

        跳过软复位（DEVICE_RESET 写入后芯片内部重载配置，会做极长 clock stretching，
        触发 RPi5 RP1 硬件 I2C 的 Errno 110 超时）。直接唤醒并配置寄存器即可。
        """
        with self._i2c_lock:
            self._bus.write_byte_data(self._addr, _REG_PWR_MGMT_1, 0x00)  # 退出睡眠
            time.sleep(0.05)
            # 采样率 = 陀螺仪输出频率 / (SMPLRT_DIV + 1)
            # 配置低通滤波后陀螺仪输出频率 = 1000Hz，目标 100Hz → DIV = 9
            self._bus.write_byte_data(self._addr, _REG_SMPLRT_DIV,  0x09)
            self._bus.write_byte_data(self._addr, _REG_CONFIG,       0x03)  # 低通 44Hz
            self._bus.write_byte_data(self._addr, _REG_GYRO_CONFIG,  _GYRO_FS_250_DEG)
            self._bus.write_byte_data(self._addr, _REG_ACCEL_CONFIG, 0x00)  # ±2g

    # 加速度计配置 ±2g，超过此值肯定是乱码（加 50% 裕量）
    _ACCEL_MAX_G  = 3.0
    # 陀螺仪配置 ±250°/s，超过此值肯定是乱码
    _GYRO_MAX_DPS = 280.0
    # 最大重试次数（每次 _read_raw 调用）
    _MAX_RETRIES  = 3

    def _read_raw(self) -> ImuReading:
        """读取加速度计和陀螺仪原始数据并转换为物理量（持有 _i2c_lock 期间调用）。

        拆分为两次独立的 6 字节读取，避免 MPU6050 在温度寄存器之后做 clock stretching
        导致 RPi5 RP1/bit-bang 驱动读到全 0xFF 的陀螺仪数据。
        每次读取最多重试 _MAX_RETRIES 次，并校验物理量范围以丢弃乱码帧。
        """
        def to_int16(hi: int, lo: int) -> int:
            v = (hi << 8) | lo
            return v - 65536 if v > 32767 else v

        last_exc: Exception | None = None
        for attempt in range(self._MAX_RETRIES):
            try:
                # 加速度计 6 字节（0x3B-0x40）
                accel = self._bus.read_i2c_block_data(self._addr, _REG_ACCEL_XOUT_H, 6)
                ax = to_int16(accel[0], accel[1]) / _ACCEL_SCALE
                ay = to_int16(accel[2], accel[3]) / _ACCEL_SCALE
                az = to_int16(accel[4], accel[5]) / _ACCEL_SCALE

                # 陀螺仪 6 字节（0x43-0x48，跳过 0x41-0x42 温度寄存器）
                gyro = self._bus.read_i2c_block_data(self._addr, 0x43, 6)
                gx = to_int16(gyro[0], gyro[1]) / _GYRO_SCALE
                gy = to_int16(gyro[2], gyro[3]) / _GYRO_SCALE
                gz = to_int16(gyro[4], gyro[5]) / _GYRO_SCALE

                # 范围校验：超出配置量程的值一定是乱码（I2C 位翻转/字节偏移）
                if (abs(ax) > self._ACCEL_MAX_G or abs(ay) > self._ACCEL_MAX_G
                        or abs(az) > self._ACCEL_MAX_G):
                    raise ValueError(
                        f"accel out of range: ax={ax:.3f} ay={ay:.3f} az={az:.3f}"
                    )
                if (abs(gx) > self._GYRO_MAX_DPS or abs(gy) > self._GYRO_MAX_DPS
                        or abs(gz) > self._GYRO_MAX_DPS):
                    raise ValueError(
                        f"gyro out of range: gx={gx:.1f} gy={gy:.1f} gz={gz:.1f}"
                    )

                return ImuReading(gx, gy, gz, ax, ay, az, time.monotonic())

            except Exception as e:
                last_exc = e
                if attempt < self._MAX_RETRIES - 1:
                    time.sleep(0.001)   # 1ms 让总线从错误状态恢复

        raise last_exc  # type: ignore[misc]

    def _calibrate_gyro(self) -> None:
        """静止 N 帧平均，估算并记录陀螺仪 Z 轴零偏。

        跳过单帧 I2C 错误，只要成功帧 >= _CALIBRATION_FRAMES // 2 就计算均值。
        采样间隔 10ms（与 _sample_loop 错开），避免与采样线程竞争 _i2c_lock。
        """
        samples: list[float] = []
        errors = 0
        for _ in range(_CALIBRATION_FRAMES):
            try:
                with self._i2c_lock:
                    samples.append(self._read_raw().gyro_z)
            except Exception:
                errors += 1
            time.sleep(0.01)

        min_samples = _CALIBRATION_FRAMES // 2
        if len(samples) >= min_samples:
            # 用中位数剔除偶发乱码帧（如 164°/s 的尖峰）后取均值
            samples.sort()
            trimmed = samples[len(samples) // 4 : len(samples) * 3 // 4]
            self._gyro_bias_z = sum(trimmed) / len(trimmed)
            logger.info(
                f"[IMU] 零偏校准完成：bias_z={self._gyro_bias_z:.3f}°/s"
                f"（成功 {len(samples)} 帧，跳过 {errors} 帧）"
            )
        else:
            logger.warning(
                f"[IMU] 零偏校准失败：仅获得 {len(samples)}/{_CALIBRATION_FRAMES} 帧"
                f"（需 >= {min_samples}），bias_z 保持 0"
            )
            self._gyro_bias_z = 0.0

    def _reinit_bus(self) -> None:
        """I2C 总线重置：关闭并重新打开 SMBus，重新初始化设备寄存器。"""
        try:
            if self._bus:
                self._bus.close()
        except Exception:
            pass
        try:
            import smbus2
            self._bus = smbus2.SMBus(self.I2C_BUS)
            self._init_device()
            logger.info("[IMU] I2C 总线已重置，设备重新初始化")
        except Exception as e:
            logger.warning(f"[IMU] I2C 重置失败：{e}")

    def _sample_loop(self) -> None:
        interval = 1.0 / self.SAMPLE_HZ
        consecutive_errors = 0
        # 连续失败超过此次数时尝试重新初始化 I2C（约 0.5s）
        _REINIT_THRESHOLD = 50
        while self._running:
            t0 = time.monotonic()
            try:
                with self._i2c_lock:
                    raw = self._read_raw()
                reading = ImuReading(
                    gyro_x=raw.gyro_x,
                    gyro_y=raw.gyro_y,
                    gyro_z=raw.gyro_z - self._gyro_bias_z,  # 去零偏
                    accel_x=raw.accel_x,
                    accel_y=raw.accel_y,
                    accel_z=raw.accel_z,
                    timestamp=raw.timestamp,
                )
                with self._lock:
                    self._latest = reading
                consecutive_errors = 0
            except Exception as e:
                consecutive_errors += 1
                if consecutive_errors == 10:
                    logger.warning(f"[IMU] 采样连续失败 {consecutive_errors} 次，数据已冻结：{e}")
                elif consecutive_errors == _REINIT_THRESHOLD:
                    logger.warning(f"[IMU] 采样持续失败 {consecutive_errors} 次，尝试重置 I2C 总线")
                    with self._i2c_lock:
                        self._reinit_bus()
                    consecutive_errors = 0
                else:
                    logger.debug(f"[IMU] 采样异常（第{consecutive_errors}次）：{e}")
            elapsed = time.monotonic() - t0
            time.sleep(max(0.0, interval - elapsed))

    # ─── 公共接口 ────────────────────────────────────────────────

    # 数据超过此秒数未更新则视为陈旧（I2C 断开后避免返回冻结数据）
    STALE_THRESHOLD_S = 1.0

    def get_latest(self) -> ImuReading | None:
        """
        返回最新一次采样数据（线程安全）。

        若超过 STALE_THRESHOLD_S 秒未刷新（I2C 故障导致数据冻结），返回 None。
        """
        with self._lock:
            if self._latest is None:
                return None
            age = time.monotonic() - self._latest.timestamp
            if age > self.STALE_THRESHOLD_S:
                return None
            return self._latest

    @property
    def is_simulation(self) -> bool:
        return self._is_simulation

    @property
    def status(self) -> dict:
        reading = self.get_latest()
        return {
            "is_simulation":  self._is_simulation,
            "i2c_addr":       f"0x{self._addr:02X}",
            "gyro_bias_z":    round(self._gyro_bias_z, 4),
            "latest": {
                "gyro_z_dps":  round(reading.gyro_z,  3),
                "accel_x_g":   round(reading.accel_x, 3),
                "accel_y_g":   round(reading.accel_y, 3),
            } if reading else None,
        }
