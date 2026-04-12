"""
IMU — BNO055 UART 驱动（CP2102 USB-UART 适配器）
==================================================
职责：
  - 通过 UART（pyserial）与 BNO055 通信，读取陀螺仪与加速度计数据
  - 后台线程 100Hz 持续采样，外部可随时读取最新数据
  - 启动时自动静止校准陀螺仪零偏（Z 轴）
  - 串口不可用或 BNO055 未响应时自动降级为模拟模式

硬件接线（BNO055 模块 via CP2102 USB-UART 适配器）：
  BNO055 VCC  → 3.3V（CP2102 3.3V 引脚 或 树莓派 3.3V Pin 1/17）
  BNO055 GND  → GND
  BNO055 ATX  → CP2102 RXD
  BNO055 LRX  → CP2102 TXD
  CP2102 USB  → 树莓派 USB 口 → /dev/ttyUSB1

UART 模式切换（模块背面焊盘）：
  S0 焊盘短接（PS0=HIGH）→ UART 模式
  S1 焊盘不短接（PS1=LOW）
  默认出厂为 I2C 模式，必须手动短接 S0 焊盘

晶振（可选）：
  将配件 32.768kHz 晶振焊入模块正面 XTAL 焊盘，提升融合算法时序精度

安装依赖：
  pip install pyserial
  （pyserial 已作为激光雷达依赖引入，无需额外安装）

工作模式：IMU Mode（加速度计 + 陀螺仪，无磁力计，适合室内机器人）

主要输出：
  gyro_z  (°/s)   — 偏航角速度（已去零偏），供 Odometry 融合转向角
  accel_x/y (g)   — 加速度（含重力），可用于检测碰撞/斜坡
"""

import glob
import threading
import time
import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)

# ── BNO055 Page 0 寄存器 ───────────────────────────────────────────
_REG_CHIP_ID  = 0x00   # 固定值 0xA0，验证连接
_REG_ACC_DATA = 0x08   # 加速度计 X/Y/Z，各 2 字节 LSB-first，共 6 字节
_REG_GYR_DATA = 0x14   # 陀螺仪 X/Y/Z，各 2 字节 LSB-first，共 6 字节
_REG_UNIT_SEL = 0x3B   # 单位选择
_REG_OPR_MODE = 0x3D   # 工作模式

# ── 工作模式 ───────────────────────────────────────────────────────
_MODE_CONFIG  = 0x00   # 配置模式（写寄存器前必须切换至此）
_MODE_IMU     = 0x08   # IMU 融合模式（加速度计 + 陀螺仪，无需磁力计校准）

# ── 物理量换算 ─────────────────────────────────────────────────────
# UNIT_SEL=0x00（默认）：加速度 m/s²（100 LSB/m/s²），陀螺仪 °/s（16 LSB/(°/s)）
_G            = 9.80665
_ACCEL_SCALE  = 100.0 * _G   # LSB → g（raw/100 = m/s²，再 /9.80665 = g）
_GYRO_SCALE   = 16.0          # LSB → °/s

# ── BNO055 UART 协议字节 ───────────────────────────────────────────
_START        = 0xAA
_READ         = 0x01
_WRITE        = 0x00
_RESP_READ    = 0xBB   # 读响应头
_RESP_WRITE   = 0xEE   # 写响应头（status=0x01 表示成功）

# ── 容错与时序 ─────────────────────────────────────────────────────
_CHIP_ID_VAL        = 0xA0
_BAUD               = 115200
_BOOT_WAIT_S        = 0.70    # BNO055 上电到就绪至少 650ms
_MODE_SWITCH_S      = 0.020   # 模式切换后等待稳定
_READ_TIMEOUT_S     = 0.10    # 单次响应超时
_CALIBRATION_FRAMES = 100     # 零偏校准采样帧数
_ACCEL_MAX_G        = 20.0    # 超限则丢弃（BNO055 默认 ±4g，给 20g 裕量应对冲击）
_GYRO_MAX_DPS       = 2000.0  # 超限则丢弃
_REINIT_THRESHOLD   = 50      # 连续失败超过此次数时重置串口
_READ_RETRIES       = 2       # 读寄存器可恢复错误重试次数（总尝试=1+重试）
_RETRY_BASE_DELAY_S = 0.002   # 读重试退避基线
_WRITE_RETRIES      = 2       # 写寄存器在 BUS_OVER_RUN(0x07) 时的重试次数

# ── 默认串口 ────────────────────────────────────────────────────────
# 串口固定：BNO055 使用 /dev/ttyUSB0（当前实测映射）。
_DEFAULT_PORT = "/dev/ttyUSB0"


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
    BNO055 UART 驱动，后台 100Hz 采样。

    线程安全：
      - _serial_lock：串行化所有 UART 读写（pyserial 非线程安全）
      - _lock：保护 _latest 读写

    串口不可用或 BNO055 未响应时自动降级，get_latest() 返回 None。
    """

    SAMPLE_HZ         = 100
    STALE_THRESHOLD_S = 1.0

    def __init__(self, port: str | None = None) -> None:
        self._port          = port or _DEFAULT_PORT
        self._port_locked   = port is not None
        self._ser           = None
        self._is_simulation = False
        self._serial_lock   = threading.Lock()
        self._lock          = threading.Lock()
        self._latest: ImuReading | None = None
        self._thread: threading.Thread | None = None
        self._running       = False
        self._gyro_bias_z   = 0.0

    # ─── 生命周期 ─────────────────────────────────────────────────

    def start(self) -> bool:
        """
        打开串口并启动后台采样线程。

        Returns:
            True  = 真实硬件已就绪
            False = 降级为模拟模式
        """
        try:
            import serial as _serial
        except Exception as e:
            logger.warning("[IMU] pyserial 不可用，降级为模拟模式：%s", e)
            self._is_simulation = True
            self._close_serial()
            return False

        last_error: Exception | None = None
        for port in self._candidate_ports():
            try:
                self._open_and_init(_serial, port)
                self._calibrate_gyro()
                self._running = True
                self._thread = threading.Thread(
                    target=self._sample_loop,
                    daemon=True,
                    name="imu-sampler",
                )
                self._thread.start()
                logger.info(
                    "[IMU] BNO055 已启动（%s），gyro_bias_z=%.3f°/s",
                    self._port, self._gyro_bias_z,
                )
                return True
            except Exception as e:
                last_error = e
                logger.warning("[IMU] 端口 %s 初始化失败：%s", port, e)
                self._close_serial()

        logger.warning("[IMU] 初始化失败，降级为模拟模式：%s", last_error)
        self._is_simulation = True
        self._close_serial()
        return False

    def stop(self) -> None:
        self._running = False
        if self._thread:
            self._thread.join(timeout=2.0)
        self._close_serial()

    def _close_serial(self) -> None:
        if self._ser:
            try:
                self._ser.close()
            except Exception:
                pass
            self._ser = None

    # ─── 内部：设备初始化（调用时无并发或调用方已持 _serial_lock）───

    def _candidate_ports(self) -> list[str]:
        """
        生成候选串口列表。
        - 显式指定 port 时：只尝试该端口
        - 默认模式：优先 /dev/ttyUSB0，再尝试其他 ttyUSB/ttyACM（去重）
        """
        if self._port_locked:
            return [self._port]

        discovered = sorted(glob.glob("/dev/ttyUSB*") + glob.glob("/dev/ttyACM*"))
        out: list[str] = []
        for p in [self._port, *discovered]:
            if p and p not in out:
                out.append(p)
        return out

    def _open_and_init(self, serial_mod, port: str) -> None:
        """打开指定串口并完成 BNO055 初始化握手。"""
        self._port = port
        self._ser = serial_mod.Serial(
            self._port,
            baudrate=_BAUD,
            bytesize=serial_mod.EIGHTBITS,
            parity=serial_mod.PARITY_NONE,
            stopbits=serial_mod.STOPBITS_ONE,
            timeout=_READ_TIMEOUT_S,
        )
        time.sleep(_BOOT_WAIT_S)
        self._ser.reset_input_buffer()
        self._init_device()

    def _init_device(self) -> None:
        """验证 CHIP_ID，设置单位，切换至 IMU 融合模式。"""
        self._write_reg(_REG_OPR_MODE, _MODE_CONFIG)
        time.sleep(_MODE_SWITCH_S)

        chip_id = self._read_reg(_REG_CHIP_ID, 1)[0]
        if chip_id != _CHIP_ID_VAL:
            raise RuntimeError(
                f"BNO055 CHIP_ID 不匹配：期望 0x{_CHIP_ID_VAL:02X}，"
                f"实际 0x{chip_id:02X}（检查 S0 焊盘是否短接？）"
            )

        # UNIT_SEL=0x00：加速度 m/s²，陀螺仪 °/s（均为默认值，显式写入确保一致）
        self._write_reg(_REG_UNIT_SEL, 0x00)
        # 切换至 IMU 融合模式
        self._write_reg(_REG_OPR_MODE, _MODE_IMU)
        time.sleep(_MODE_SWITCH_S)

    # ─── 内部：UART 协议（调用前须持 _serial_lock 或处于单线程初始化阶段）

    @staticmethod
    def _uart_error_name(code: int) -> str:
        """BNO055 UART 错误码可读化（仅列出常见项）。"""
        mapping = {
            0x01: "WRITE_FAIL",
            0x02: "READ_FAIL",
            0x03: "REGMAP_INVALID_ADDRESS",
            0x04: "REGMAP_WRITE_DISABLED",
            0x05: "WRONG_START_BYTE",
            0x06: "BUS_READ_OVER_RUN_ERROR",
            0x07: "BUS_OVER_RUN_ERROR",
            0x08: "MAX_LENGTH_ERROR",
            0x09: "MIN_LENGTH_ERROR",
            0x0A: "RECEIVE_CHARACTER_TIMEOUT",
        }
        return mapping.get(code, "UNKNOWN")

    @staticmethod
    def _is_retriable_read_error(err: Exception) -> bool:
        """
        判定是否属于可恢复读错误。
        0x07（BUS_OVER_RUN_ERROR）和超时/短包/头异常可通过短暂退避后重试恢复。
        """
        if isinstance(err, TimeoutError):
            return True
        msg = str(err)
        if "数据不足" in msg or "响应头异常" in msg:
            return True
        return "BNO055 返回设备错误：0x07" in msg

    def _read_reg_once(self, reg: int, length: int) -> bytes:
        """发送一次读请求，返回 length 字节数据。"""
        cmd = bytes([_START, _READ, reg, length])
        self._ser.reset_input_buffer()
        self._ser.write(cmd)

        header = self._ser.read(2)
        if len(header) < 2:
            raise TimeoutError(f"寄存器 0x{reg:02X} 读响应头超时")
        if header[0] == _RESP_WRITE:
            code = header[1]
            name = self._uart_error_name(code)
            raise RuntimeError(
                f"BNO055 返回设备错误：0x{code:02X}（{name}，寄存器 0x{reg:02X}）"
            )
        if header[0] != _RESP_READ:
            raise RuntimeError(
                f"响应头异常：0x{header[0]:02X} 0x{header[1]:02X}（期望 0x{_RESP_READ:02X}）"
            )

        data = self._ser.read(length)
        if len(data) < length:
            raise TimeoutError(
                f"寄存器 0x{reg:02X} 数据不足（期望 {length} 字节，实际 {len(data)}）"
            )
        return data

    def _read_reg(self, reg: int, length: int) -> bytes:
        """发送读请求（含可恢复错误自动重试）。"""
        last_err: Exception | None = None
        for attempt in range(_READ_RETRIES + 1):
            try:
                return self._read_reg_once(reg, length)
            except Exception as e:
                last_err = e
                if self._is_retriable_read_error(e) and attempt < _READ_RETRIES:
                    try:
                        self._ser.reset_input_buffer()
                    except Exception:
                        pass
                    time.sleep(_RETRY_BASE_DELAY_S * (attempt + 1))
                    continue
                raise
        raise last_err if last_err else RuntimeError("读取寄存器失败")

    def _write_reg(self, reg: int, value: int) -> None:
        """写单字节寄存器（仅对 0x07 过载错误做有限重试）。"""
        cmd = bytes([_START, _WRITE, reg, 0x01, value])
        last_detail = "空"
        for attempt in range(_WRITE_RETRIES + 1):
            self._ser.reset_input_buffer()
            self._ser.write(cmd)

            resp = self._ser.read(2)
            if len(resp) >= 2 and resp[0] == _RESP_WRITE and resp[1] == 0x01:
                return

            last_detail = resp.hex() if resp else "空"
            # 0xEE 0x07 = BUS_OVER_RUN_ERROR，BNO055 短暂忙时常见，可恢复
            if (
                len(resp) >= 2
                and resp[0] == _RESP_WRITE
                and resp[1] == 0x07
                and attempt < _WRITE_RETRIES
            ):
                try:
                    self._ser.reset_input_buffer()
                except Exception:
                    pass
                time.sleep(0.01 * (attempt + 1))
                continue

            raise RuntimeError(
                f"写寄存器 0x{reg:02X}=0x{value:02X} 失败：resp={last_detail}"
            )

    # ─── 内部：采样 ───────────────────────────────────────────────

    def _read_raw(self) -> ImuReading:
        """
        读取加速度计和陀螺仪原始数据（调用前须持 _serial_lock）。

        BNO055 字节序：LSB 在前。
        """
        def s16(lo: int, hi: int) -> int:
            v = (hi << 8) | lo
            return v - 65536 if v > 32767 else v

        acc = self._read_reg(_REG_ACC_DATA, 6)
        gyr = self._read_reg(_REG_GYR_DATA, 6)

        ax = s16(acc[0], acc[1]) / _ACCEL_SCALE
        ay = s16(acc[2], acc[3]) / _ACCEL_SCALE
        az = s16(acc[4], acc[5]) / _ACCEL_SCALE
        gx = s16(gyr[0], gyr[1]) / _GYRO_SCALE
        gy = s16(gyr[2], gyr[3]) / _GYRO_SCALE
        gz = s16(gyr[4], gyr[5]) / _GYRO_SCALE

        if abs(ax) > _ACCEL_MAX_G or abs(ay) > _ACCEL_MAX_G or abs(az) > _ACCEL_MAX_G:
            raise ValueError(f"accel 超限：ax={ax:.2f} ay={ay:.2f} az={az:.2f}")
        if abs(gx) > _GYRO_MAX_DPS or abs(gy) > _GYRO_MAX_DPS or abs(gz) > _GYRO_MAX_DPS:
            raise ValueError(f"gyro 超限：gx={gx:.1f} gy={gy:.1f} gz={gz:.1f}")

        return ImuReading(gx, gy, gz, ax, ay, az, time.monotonic())

    def _calibrate_gyro(self) -> None:
        """静止 N 帧平均，估算陀螺仪 Z 轴零偏（启动时单线程调用）。"""
        samples: list[float] = []
        errors = 0
        for _ in range(_CALIBRATION_FRAMES):
            try:
                with self._serial_lock:
                    raw = self._read_raw()
                samples.append(raw.gyro_z)
            except Exception:
                errors += 1
            time.sleep(0.01)

        min_ok = _CALIBRATION_FRAMES // 2
        if len(samples) >= min_ok:
            samples.sort()
            trimmed = samples[len(samples) // 4 : len(samples) * 3 // 4]
            self._gyro_bias_z = sum(trimmed) / len(trimmed)
            logger.info(
                "[IMU] 零偏校准完成：bias_z=%.3f°/s（成功 %d 帧，跳过 %d 帧）",
                self._gyro_bias_z, len(samples), errors,
            )
        else:
            logger.warning(
                "[IMU] 零偏校准帧数不足（%d/%d），bias_z 保持 0",
                len(samples), _CALIBRATION_FRAMES,
            )
            self._gyro_bias_z = 0.0

    def _reinit_serial(self) -> None:
        """重新打开串口并重新初始化设备（调用方须持 _serial_lock）。"""
        self._close_serial()
        try:
            import serial as _serial
            self._ser = _serial.Serial(
                self._port, baudrate=_BAUD, timeout=_READ_TIMEOUT_S,
            )
            time.sleep(_BOOT_WAIT_S)
            self._ser.reset_input_buffer()
            self._init_device()
            logger.info("[IMU] 串口已重置，设备重新初始化")
        except Exception as e:
            logger.warning("[IMU] 串口重置失败：%s", e)

    def _sample_loop(self) -> None:
        interval = 1.0 / self.SAMPLE_HZ
        consecutive_errors = 0

        while self._running:
            t0 = time.monotonic()
            try:
                with self._serial_lock:
                    raw = self._read_raw()
                reading = ImuReading(
                    gyro_x=raw.gyro_x,
                    gyro_y=raw.gyro_y,
                    gyro_z=raw.gyro_z - self._gyro_bias_z,
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
                    logger.warning("[IMU] 采样连续失败 %d 次：%s", consecutive_errors, e)
                elif consecutive_errors == _REINIT_THRESHOLD:
                    logger.warning("[IMU] 持续失败 %d 次，尝试重置串口", consecutive_errors)
                    with self._serial_lock:
                        self._reinit_serial()
                    consecutive_errors = 0
                else:
                    logger.debug("[IMU] 采样异常（第 %d 次）：%s", consecutive_errors, e)

            elapsed = time.monotonic() - t0
            time.sleep(max(0.0, interval - elapsed))

    # ─── 公共接口 ────────────────────────────────────────────────

    def get_latest(self) -> ImuReading | None:
        """
        返回最新一次采样数据（线程安全）。

        超过 STALE_THRESHOLD_S 未刷新（串口故障导致数据冻结）时返回 None。
        """
        with self._lock:
            if self._latest is None:
                return None
            if time.monotonic() - self._latest.timestamp > self.STALE_THRESHOLD_S:
                return None
            return self._latest

    @property
    def is_simulation(self) -> bool:
        return self._is_simulation

    @property
    def status(self) -> dict:
        reading = self.get_latest()
        return {
            "is_simulation": self._is_simulation,
            "serial_port":   self._port,
            "gyro_bias_z":   round(self._gyro_bias_z, 4),
            "latest": {
                "gyro_z_dps": round(reading.gyro_z,  3),
                "accel_x_g":  round(reading.accel_x, 3),
                "accel_y_g":  round(reading.accel_y, 3),
            } if reading else None,
        }
