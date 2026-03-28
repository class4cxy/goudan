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
        """复位并唤醒 MPU6050/MPU6500，配置量程 & 采样率。"""
        with self._i2c_lock:
            # 先软复位，确保从干净状态启动（防止上次异常导致内部状态混乱）
            self._bus.write_byte_data(self._addr, _REG_PWR_MGMT_1, 0x80)  # DEVICE_RESET
            time.sleep(0.15)
            self._bus.write_byte_data(self._addr, _REG_PWR_MGMT_1, 0x00)  # 退出睡眠
            time.sleep(0.15)
            # 采样率 = 陀螺仪输出频率 / (SMPLRT_DIV + 1)
            # 配置低通滤波后陀螺仪输出频率 = 1000Hz，目标 100Hz → DIV = 9
            self._bus.write_byte_data(self._addr, _REG_SMPLRT_DIV,  0x09)
            self._bus.write_byte_data(self._addr, _REG_CONFIG,       0x03)  # 低通 44Hz
            self._bus.write_byte_data(self._addr, _REG_GYRO_CONFIG,  _GYRO_FS_250_DEG)
            self._bus.write_byte_data(self._addr, _REG_ACCEL_CONFIG, 0x00)  # ±2g

    def _read_raw(self) -> ImuReading:
        """读取 14 字节原始数据并转换为物理量（持有 _i2c_lock 期间调用）。"""
        data = self._bus.read_i2c_block_data(self._addr, _REG_ACCEL_XOUT_H, 14)

        def to_int16(hi: int, lo: int) -> int:
            v = (hi << 8) | lo
            return v - 65536 if v > 32767 else v

        ax = to_int16(data[0],  data[1])  / _ACCEL_SCALE
        ay = to_int16(data[2],  data[3])  / _ACCEL_SCALE
        az = to_int16(data[4],  data[5])  / _ACCEL_SCALE
        # data[6:8] = Temperature（跳过）
        gx = to_int16(data[8],  data[9])  / _GYRO_SCALE
        gy = to_int16(data[10], data[11]) / _GYRO_SCALE
        gz = to_int16(data[12], data[13]) / _GYRO_SCALE
        return ImuReading(gx, gy, gz, ax, ay, az, time.monotonic())

    def _calibrate_gyro(self) -> None:
        """静止 N 帧平均，估算并记录陀螺仪 Z 轴零偏。

        采样间隔 10ms（与 _sample_loop 错开），避免与采样线程竞争 _i2c_lock。
        """
        try:
            samples: list[float] = []
            for _ in range(_CALIBRATION_FRAMES):
                with self._i2c_lock:
                    samples.append(self._read_raw().gyro_z)
                time.sleep(0.01)   # 10ms 间隔，让采样线程有机会获锁
            self._gyro_bias_z = sum(samples) / len(samples)
        except Exception as e:
            logger.warning(f"[IMU] 零偏校准失败：{e}")
            self._gyro_bias_z = 0.0

    def _sample_loop(self) -> None:
        interval = 1.0 / self.SAMPLE_HZ
        consecutive_errors = 0
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
                # 连续失败 10 次（~0.1s）升级为 WARNING，避免静默冻结
                if consecutive_errors == 10:
                    logger.warning(f"[IMU] 采样连续失败 {consecutive_errors} 次，数据已冻结：{e}")
                elif consecutive_errors % 100 == 0:
                    logger.warning(f"[IMU] 采样持续失败（{consecutive_errors} 次），最后错误：{e}")
                else:
                    logger.debug(f"[IMU] 采样异常：{e}")
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
