"""
Lidar — LD06 激光雷达硬件抽象层
=================================
职责：
  1. 通过串口（CP2102 USB-TTL，/dev/ttyUSB0）持续读取 LD06 原始数据包
  2. 解析 47 字节协议帧：速度 / 起止角度 / 12 个测距点 / CRC8 校验
  3. 在内部将多帧合并为一圈完整扫描（~37 帧 @ 10Hz）
  4. 每完成一圈通过 on_scan 回调推送 LidarScan 给上层
  5. 非树莓派环境自动降级为模拟模式（返回 None / 空数据）

不含任何 WebSocket / Spine / FastAPI 逻辑，纯硬件操作。

接线说明（LD06 → CP2102 → 树莓派 USB）：
  LD06 P5V  → CP2102 5V
  LD06 GND  → CP2102 GND
  LD06 Tx   → CP2102 RXD   ← 只需接收，发送方向
  LD06 PWM  → 悬空（内部调速模式，默认 10Hz）

依赖：pyserial（pip install pyserial）
"""

import logging
import struct
import threading
import time
from dataclasses import dataclass, field
from typing import Callable

logger = logging.getLogger(__name__)

# ── LD06 协议常量 ──────────────────────────────────────────────────
PACKET_HEADER  = 0x54   # 帧头
PACKET_VERLEN  = 0x2C   # VerLen 字节（12 个数据点）
PACKET_SIZE    = 47     # 每帧字节数
POINTS_PER_PKT = 12     # 每帧测距点数
ANGLE_UNIT     = 100.0  # 原始角度单位（0.01°）
BAUD_RATE      = 230400

# LD06 CRC8 查找表（多项式 0x4D，预计算）
_CRC_TABLE: list[int] = []

def _build_crc_table() -> None:
    global _CRC_TABLE
    for i in range(256):
        crc = i
        for _ in range(8):
            crc = ((crc << 1) ^ 0x4D) & 0xFF if (crc & 0x80) else (crc << 1) & 0xFF
        _CRC_TABLE.append(crc)

_build_crc_table()


def _crc8(data: bytes) -> int:
    crc = 0
    for b in data:
        crc = _CRC_TABLE[crc ^ b]
    return crc


# ── 数据模型 ───────────────────────────────────────────────────────

@dataclass
class LidarPoint:
    """单个测距点。"""
    angle: float      # 绝对角度（度，0.0–360.0）
    distance: int     # 距离（毫米，0 表示无效）
    confidence: int   # 置信度（0–255）

    @property
    def is_valid(self) -> bool:
        """distance 在有效范围内（20mm–12000mm）且置信度 > 10。"""
        return 20 <= self.distance <= 12000 and self.confidence > 10


@dataclass
class LidarScan:
    """一圈完整扫描结果（约 360°）。"""
    timestamp_ms: int                           # 采集完成时的系统时间戳（ms）
    rpm: float                                  # 电机转速（RPM）
    points: list[LidarPoint] = field(default_factory=list)

    @property
    def point_count(self) -> int:
        return len(self.points)

    @property
    def valid_points(self) -> list[LidarPoint]:
        return [p for p in self.points if p.is_valid]

    def to_dict(self) -> dict:
        return {
            "timestamp_ms": self.timestamp_ms,
            "rpm": round(self.rpm, 1),
            "point_count": self.point_count,
            "valid_count": len(self.valid_points),
            "points": [
                {
                    "angle": round(p.angle, 2),
                    "distance": p.distance,
                    "confidence": p.confidence,
                }
                for p in self.points
            ],
        }


# ── Lidar 配置 ─────────────────────────────────────────────────────

@dataclass
class LidarConfig:
    port: str = "/dev/ttyUSB0"          # 串口设备（CP2102 USB-TTL）
    baud_rate: int = BAUD_RATE          # 波特率（LD06 固定 230400）
    timeout: float = 1.0               # 读取超时（秒）
    broadcast_every_n_scans: int = 1   # 每 N 圈回调一次（降低 WebSocket 压力）
    mount_angle_deg: float = 0.0       # 安装偏移角（度）：
                                       #   0   = LD06 线缆接口朝向机器人正前方（默认）
                                       #   180 = 线缆接口朝向机器人正后方（装反了）
                                       #   90  = 线缆接口朝向机器人右侧
                                       # 修改后重启 Platform 生效，无需改硬件。
    mount_angle_deg: float = 0.0       # 雷达安装偏转角（度）：
                                        #   0   → 线缆朝前（默认）
                                        #   180 → 线缆朝后（装反了）
                                        #   90  → 线缆朝右
                                        #  -90  → 线缆朝左

DEFAULT_LIDAR_CONFIG = LidarConfig()


# ── Lidar 主类 ─────────────────────────────────────────────────────

class Lidar:
    """
    LD06 激光雷达控制器（纯硬件层）。

    通过 on_scan 回调向上层推送完整扫描帧，不依赖任何网络组件。

    Args:
        config:   LidarConfig，包含串口地址和波特率
        on_scan:  每圈完成时调用，参数为 LidarScan；在串口读取线程中同步调用
    """

    def __init__(
        self,
        config: LidarConfig | None = None,
        on_scan: Callable[[LidarScan], None] | None = None,
    ):
        self._config = config or DEFAULT_LIDAR_CONFIG
        self._on_scan = on_scan

        self._serial = None
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._is_simulation = False

        self._latest_scan: LidarScan | None = None
        self._lock = threading.Lock()

        # 帧累积状态（拼接多帧为一圈）
        self._scan_buffer: list[LidarPoint] = []
        self._scan_rpm_sum: float = 0.0
        self._scan_rpm_count: int = 0
        self._last_start_angle: float = -1.0
        self._completed_scans: int = 0

    # ─── 公共接口 ──────────────────────────────────────────────────

    def start(self) -> None:
        """打开串口并启动后台读取线程（非阻塞）。重复调用时若线程已在运行则直接返回。"""
        if self.is_running:
            logger.debug("[Lidar] 读取线程已在运行，跳过重复启动")
            return

        # 每次调用 start() 前重置模拟标志，允许硬件插入后重试
        self._is_simulation = False

        try:
            import serial
        except ImportError:
            logger.error("[Lidar] 缺少依赖 pyserial，请运行：pip install pyserial")
            self._is_simulation = True
            return

        try:
            self._serial = serial.Serial(
                port=self._config.port,
                baudrate=self._config.baud_rate,
                timeout=self._config.timeout,
            )
            logger.info(f"[Lidar] 串口已打开：{self._config.port} @ {self._config.baud_rate}bps")
        except Exception as e:
            logger.warning(f"[Lidar] 串口打开失败（{e}），进入模拟模式")
            self._is_simulation = True
            return

        self._stop_event.clear()
        self._thread = threading.Thread(target=self._read_loop, daemon=True, name="lidar_reader")
        self._thread.start()
        logger.info("[Lidar] 读取线程已启动")

    def stop(self) -> None:
        """停止读取线程并关闭串口。"""
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=3.0)
            self._thread = None
        if self._serial and self._serial.is_open:
            self._serial.close()
            logger.info("[Lidar] 串口已关闭")

    @property
    def is_simulation(self) -> bool:
        return self._is_simulation

    @property
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    @property
    def latest_scan(self) -> LidarScan | None:
        with self._lock:
            return self._latest_scan

    @property
    def status(self) -> dict:
        scan = self.latest_scan
        return {
            "port": self._config.port,
            "baud_rate": self._config.baud_rate,
            "is_simulation": self._is_simulation,
            "is_running": self.is_running,
            "completed_scans": self._completed_scans,
            "latest_scan": {
                "timestamp_ms": scan.timestamp_ms,
                "rpm": round(scan.rpm, 1),
                "point_count": scan.point_count,
                "valid_count": len(scan.valid_points),
            } if scan else None,
        }

    # ─── 串口读取循环 ──────────────────────────────────────────────

    def _read_loop(self) -> None:
        """在后台线程中持续读取串口数据并解析 LD06 协议帧。"""
        buf = bytearray()
        logger.info("[Lidar] 开始读取串口数据...")

        while not self._stop_event.is_set():
            try:
                chunk = self._serial.read(128)
            except Exception as e:
                if not self._stop_event.is_set():
                    logger.error(f"[Lidar] 串口读取错误：{e}")
                    time.sleep(0.5)
                continue

            if not chunk:
                continue

            buf.extend(chunk)

            # 在缓冲区中查找并处理完整帧
            while len(buf) >= PACKET_SIZE:
                idx = buf.find(PACKET_HEADER)
                if idx == -1:
                    buf.clear()
                    break
                if idx > 0:
                    del buf[:idx]
                if len(buf) < PACKET_SIZE:
                    break
                # 验证第 2 字节（VerLen = 0x2C）
                if buf[1] != PACKET_VERLEN:
                    del buf[0]
                    continue
                pkt = bytes(buf[:PACKET_SIZE])
                if _crc8(pkt[:-1]) != pkt[-1]:
                    logger.debug("[Lidar] CRC 校验失败，丢弃帧头")
                    del buf[0]
                    continue
                del buf[:PACKET_SIZE]
                self._process_packet(pkt)

    # ─── 协议解析 ──────────────────────────────────────────────────

    def _process_packet(self, pkt: bytes) -> None:
        """
        解析单个 47 字节 LD06 数据包，将测距点追加到当前扫描缓冲区。
        检测到新一圈起始时，触发完整圈回调。
        """
        # 帧结构（偏移量）：
        #   0     Header   (0x54)
        #   1     VerLen   (0x2C)
        #   2-3   Speed    uint16 LE（rpm）
        #   4-5   StartAngle uint16 LE（0.01°）
        #   6-41  12 × [Distance uint16 LE (mm), Confidence uint8]
        #   42-43 EndAngle   uint16 LE（0.01°）
        #   44-45 Timestamp  uint16 LE（ms）
        #   46    CRC8

        speed_raw    = struct.unpack_from("<H", pkt, 2)[0]
        start_raw    = struct.unpack_from("<H", pkt, 4)[0]
        end_raw      = struct.unpack_from("<H", pkt, 42)[0]

        rpm        = speed_raw / 360.0        # 单位 °/s → RPM：÷360×60
        rpm        = speed_raw * 60.0 / 36000.0  # speed_raw 单位为 °/s × 100，即 0.01°/s
        start_deg  = start_raw / ANGLE_UNIT   # 转换为度
        end_deg    = end_raw   / ANGLE_UNIT

        # 检测新一圈开始（起始角度小于上一帧的起始角度，说明过了 0°）
        if self._last_start_angle >= 0 and start_deg < self._last_start_angle:
            self._finalize_scan()

        self._last_start_angle = start_deg

        # 解析 12 个测距点，角度线性插值
        if end_deg < start_deg:
            # 跨 0° 包内，处理角度回卷
            end_deg += 360.0

        step = (end_deg - start_deg) / (POINTS_PER_PKT - 1) if POINTS_PER_PKT > 1 else 0.0

        for i in range(POINTS_PER_PKT):
            offset = 6 + i * 3
            dist   = struct.unpack_from("<H", pkt, offset)[0]
            conf   = pkt[offset + 2]
            # 应用安装偏移：将硬件角度旋转到机器人坐标系（0° = 正前方）
            angle  = (start_deg + i * step + self._config.mount_angle_deg) % 360.0
            self._scan_buffer.append(LidarPoint(angle=angle, distance=dist, confidence=conf))

        self._scan_rpm_sum   += rpm
        self._scan_rpm_count += 1

    def _finalize_scan(self) -> None:
        """将缓冲的测距点打包为 LidarScan，保存并触发回调。"""
        if not self._scan_buffer:
            return

        avg_rpm = (
            self._scan_rpm_sum / self._scan_rpm_count
            if self._scan_rpm_count > 0
            else 0.0
        )
        scan = LidarScan(
            timestamp_ms=int(time.time() * 1000),
            rpm=avg_rpm,
            points=list(self._scan_buffer),
        )

        with self._lock:
            self._latest_scan = scan

        self._completed_scans += 1

        # 按配置频率回调（降低上层处理压力）
        if self._on_scan and self._completed_scans % self._config.broadcast_every_n_scans == 0:
            try:
                self._on_scan(scan)
            except Exception as e:
                logger.warning(f"[Lidar] on_scan 回调异常：{e}")

        logger.debug(
            f"[Lidar] 第 {self._completed_scans} 圈完成："
            f"{scan.point_count} 点，{len(scan.valid_points)} 有效，"
            f"RPM={avg_rpm:.1f}"
        )

        # 重置缓冲区
        self._scan_buffer.clear()
        self._scan_rpm_sum   = 0.0
        self._scan_rpm_count = 0
