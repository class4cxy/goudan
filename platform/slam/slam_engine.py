"""
SlamEngine — breezyslam 封装层
================================
职责：
  1. 将 LidarScan 重采样为 breezyslam 所需的等角度距离数组
  2. 驱动 RMHC_SLAM 算法：更新地图 + 估算机器人位姿
  3. 线程安全地暴露当前位姿和地图（串口线程写，AsyncIO 线程读）
  4. 地图持久化：保存/加载 PGM（ROS 兼容格式）+ JSON 元数据
  5. 渲染地图为 PNG（用于 WebSocket 广播和 REST 返回）

不含任何 WebSocket / FastAPI 逻辑，纯算法层。

breezyslam 安装：pip install breezyslam
breezyslam 原理：RMHC（随机重启爬山）+ 扫描匹配，无需里程计

地图字节语义（breezyslam 约定）：
  0        = 未探索（未知区域）
  1–127    = 障碍物（值越小 = 置信度越高 = 越深色）
  128–255  = 可通行（值越大 = 置信度越高 = 越浅色）

依赖：breezyslam, numpy, opencv-python-headless（已在 requirements.txt）
"""

import base64
import json
import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable

import numpy as np

logger = logging.getLogger(__name__)

# 地图文件保存目录（相对于 bridge/）
DEFAULT_MAPS_DIR = Path(__file__).parent.parent / "maps"


# ── 传感器规格（breezyslam Laser 子类）────────────────────────────

def _make_ld06_laser():
    """
    构造 LD06 的 breezyslam Laser 规格对象。
    延迟 import，允许在 breezyslam 未安装时模块仍可加载。
    """
    from breezyslam.sensors import Laser

    class LD06Laser(Laser):
        SCAN_SIZE = 360          # 重采样后每圈点数（每 1° 一个点）
        SCAN_RATE_HZ = 10.0
        DETECTION_ANGLE = 360.0  # 全圆
        MAX_DIST_MM = 12000
        OFFSET_MM = 0

        def __init__(self):
            Laser.__init__(
                self,
                scan_size=self.SCAN_SIZE,
                scan_rate_hz=self.SCAN_RATE_HZ,
                detection_angle_degrees=self.DETECTION_ANGLE,
                distance_no_detection_mm=self.MAX_DIST_MM,
                detection_margin=0,
                offset_mm=self.OFFSET_MM,
            )

    return LD06Laser()


# ── 配置 ──────────────────────────────────────────────────────────

@dataclass
class SlamConfig:
    # 地图尺寸
    map_size_pixels: int   = 500     # 地图分辨率（正方形边长，像素）
    map_size_meters: float = 10.0    # 地图覆盖的物理范围（米），决定精度

    # breezyslam 算法参数
    map_quality: int       = 50      # 地图更新强度 0–255，越大越快但越噪
    hole_width_mm: float   = 600.0   # 最小可通行孔宽（mm），影响路径规划
    sigma_xy_mm: float     = 100.0   # 位置不确定度（mm），值越大探索越激进
    sigma_theta_deg: float = 20.0    # 角度不确定度（度）

    # 广播频率
    pose_broadcast_every: int = 10   # 每 N 圈广播一次位姿（10 圈 = 1Hz）
    map_broadcast_every: int  = 50   # 每 N 圈广播一次地图 PNG（50 圈 = 5s）

    # 存储
    maps_dir: Path = field(default_factory=lambda: DEFAULT_MAPS_DIR)

    @property
    def mm_per_pixel(self) -> float:
        return (self.map_size_meters * 1000) / self.map_size_pixels


DEFAULT_SLAM_CONFIG = SlamConfig()


# ── SlamEngine ────────────────────────────────────────────────────

class SlamEngine:
    """
    breezyslam RMHC_SLAM 封装。

    线程模型：
      - process_scan() 由 LiDAR 串口线程调用（写操作）
      - get_pose() / get_map_*() 由 AsyncIO 线程调用（读操作）
      - 使用 threading.Lock 保护共享状态

    Args:
        config:      SlamConfig 配置
        on_update:   每次 SLAM 更新后的回调 (pose, scan_count)，
                     在串口线程中同步调用
    """

    def __init__(
        self,
        config: SlamConfig | None = None,
        on_update: Callable[[tuple, int], None] | None = None,
    ):
        self._cfg = config or DEFAULT_SLAM_CONFIG
        self._on_update = on_update

        # 内部状态（Lock 保护）
        self._lock = threading.Lock()
        self._slam = None                    # RMHC_SLAM 实例，start_mapping() 后初始化
        self._map_bytes = bytearray(
            self._cfg.map_size_pixels ** 2
        )
        self._pose: tuple[float, float, float] = (0.0, 0.0, 0.0)  # x_mm, y_mm, theta°

        # 状态标志
        self._is_mapping = False
        self._is_available = False           # breezyslam 是否安装
        self._scan_count = 0                 # 累计处理圈数
        self._session_start: float | None = None

        # 地图目录
        self._cfg.maps_dir.mkdir(parents=True, exist_ok=True)

        # 检查 breezyslam 是否可用
        try:
            from breezyslam.algorithms import RMHC_SLAM  # noqa: F401
            self._is_available = True
            logger.info("[SLAM] breezyslam 已检测到，可以建图")
        except ImportError:
            logger.warning("[SLAM] breezyslam 未安装，请运行：pip install breezyslam")

    # ─── 生命周期 ────────────────────────────────────────────────

    def start_mapping(self) -> bool:
        """
        初始化（或重置）SLAM 算法，开始接受扫描帧。

        Returns:
            True  = 成功启动
            False = breezyslam 未安装
        """
        if not self._is_available:
            logger.error("[SLAM] breezyslam 未安装，无法开始建图")
            return False

        from breezyslam.algorithms import RMHC_SLAM

        with self._lock:
            self._slam = RMHC_SLAM(
                laser=_make_ld06_laser(),
                map_size_pixels=self._cfg.map_size_pixels,
                map_size_meters=self._cfg.map_size_meters,
                map_quality=self._cfg.map_quality,
                hole_width_mm=self._cfg.hole_width_mm,
                sigma_xy_mm=self._cfg.sigma_xy_mm,
                sigma_theta_degrees=self._cfg.sigma_theta_deg,
            )
            self._map_bytes = bytearray(self._cfg.map_size_pixels ** 2)
            self._pose = (0.0, 0.0, 0.0)
            self._scan_count = 0
            self._is_mapping = True
            self._session_start = time.time()

        logger.info(
            f"[SLAM] 建图已开始："
            f"{self._cfg.map_size_pixels}×{self._cfg.map_size_pixels} 像素，"
            f"{self._cfg.map_size_meters}m×{self._cfg.map_size_meters}m，"
            f"精度={self._cfg.mm_per_pixel:.0f}mm/像素"
        )
        return True

    def stop_mapping(self) -> None:
        """冻结地图更新（停止接受新扫描帧，保留当前状态）。"""
        with self._lock:
            self._is_mapping = False
        logger.info(f"[SLAM] 建图已停止，共处理 {self._scan_count} 圈")

    def reset(self) -> None:
        """完全重置 SLAM 状态（清空地图和位姿）。"""
        with self._lock:
            self._slam = None
            self._map_bytes = bytearray(self._cfg.map_size_pixels ** 2)
            self._pose = (0.0, 0.0, 0.0)
            self._scan_count = 0
            self._is_mapping = False
            self._session_start = None
        logger.info("[SLAM] 已重置")

    # ─── 核心：处理扫描帧 ────────────────────────────────────────

    def process_scan(self, scan) -> None:
        """
        接收一圈 LidarScan，驱动 SLAM 更新。
        在 LiDAR 串口线程中调用（同步，非异步）。
        """
        with self._lock:
            if not self._is_mapping or self._slam is None:
                return
            distances = _resample_scan(scan, self._cfg)
            self._slam.update(distances)
            x, y, theta = self._slam.getpos()
            self._pose = (x, y, theta)
            self._slam.getmap(self._map_bytes)
            self._scan_count += 1
            count = self._scan_count

        if self._on_update:
            try:
                self._on_update(self._pose, count)
            except Exception as e:
                logger.warning(f"[SLAM] on_update 回调异常：{e}")

    # ─── 数据读取（线程安全）─────────────────────────────────────

    def get_pose(self) -> tuple[float, float, float]:
        """返回当前机器人位姿 (x_mm, y_mm, theta_degrees)。"""
        with self._lock:
            return self._pose

    def pose_to_pixel(self, x_mm: float, y_mm: float) -> tuple[int, int]:
        """将 mm 坐标转换为地图像素坐标。"""
        half = self._cfg.map_size_pixels / 2
        px = int(x_mm / self._cfg.mm_per_pixel + half)
        py = int(y_mm / self._cfg.mm_per_pixel + half)
        return (
            max(0, min(px, self._cfg.map_size_pixels - 1)),
            max(0, min(py, self._cfg.map_size_pixels - 1)),
        )

    def get_map_png_b64(self, draw_robot: bool = True) -> str | None:
        """
        将当前地图渲染为 PNG，返回 base64 编码字符串。
        未探索区域=灰色，障碍=黑色，可通行=白色，机器人=蓝点。
        """
        try:
            import cv2
        except ImportError:
            logger.warning("[SLAM] opencv 未安装，无法生成地图 PNG")
            return None

        with self._lock:
            raw = bytes(self._map_bytes)
            pose = self._pose
            size = self._cfg.map_size_pixels

        arr = np.frombuffer(raw, dtype=np.uint8).reshape((size, size))

        # 颜色映射：breezyslam 字节语义
        #   0        → 未探索 → 灰色 (192,192,192)
        #   1–127    → 障碍物 → 黑 → 深灰
        #   128–255  → 可通行 → 浅灰 → 白
        rgb = np.zeros((size, size, 3), dtype=np.uint8)
        unexplored = arr == 0
        occupied   = (arr > 0) & (arr <= 127)
        free       = arr > 127

        rgb[unexplored] = [192, 192, 192]
        # 障碍：arr=1→黑(0)，arr=127→深灰(64)
        occ_brightness = (arr[occupied].astype(np.int32) * 64 // 127).astype(np.uint8)
        rgb[occupied] = np.stack([occ_brightness, occ_brightness, occ_brightness], axis=-1)
        # 可通行：arr=128→浅灰(128)，arr=255→白(255)
        free_brightness = arr[free]
        rgb[free] = np.stack([free_brightness, free_brightness, free_brightness], axis=-1)

        # 绘制机器人位置（蓝色圆点）
        if draw_robot:
            rx, ry = self.pose_to_pixel(pose[0], pose[1])
            cv2.circle(rgb, (rx, ry), 5, (220, 80, 20), -1)   # 蓝色点（BGR）
            cv2.circle(rgb, (rx, ry), 5, (255, 255, 255), 1)  # 白色边框

            # 绘制朝向箭头
            theta_rad = np.radians(pose[2])
            ax = int(rx + 15 * np.sin(theta_rad))
            ay = int(ry - 15 * np.cos(theta_rad))
            cv2.arrowedLine(rgb, (rx, ry), (ax, ay), (220, 80, 20), 2, tipLength=0.4)

        _, buf = cv2.imencode(".png", rgb)
        return base64.b64encode(buf).decode()

    # ─── 地图持久化 ──────────────────────────────────────────────

    def save_map(self, name: str = "") -> dict | None:
        """
        保存当前地图到 maps/ 目录。

        文件格式：
          {name}.pgm  — 灰度图（ROS 兼容格式）
          {name}.json — 元数据（分辨率、原点、位姿、时间）

        Returns:
            {"pgm": path, "json": path, "name": name} 或 None（地图为空）
        """
        with self._lock:
            if not any(self._map_bytes):
                logger.warning("[SLAM] 地图为空，跳过保存")
                return None
            raw = bytes(self._map_bytes)
            pose = self._pose
            size = self._cfg.map_size_pixels
            count = self._scan_count

        if not name:
            name = f"map_{datetime.now().strftime('%Y%m%d_%H%M%S')}"

        pgm_path  = self._cfg.maps_dir / f"{name}.pgm"
        json_path = self._cfg.maps_dir / f"{name}.json"

        # 写 PGM（P5 二进制灰度）
        arr = np.frombuffer(raw, dtype=np.uint8).reshape((size, size))
        with open(pgm_path, "wb") as f:
            f.write(f"P5\n# breezyslam map\n{size} {size}\n255\n".encode())
            f.write(arr.tobytes())

        # 写 JSON 元数据
        meta = {
            "name": name,
            "created_at": datetime.now().isoformat(),
            "map_size_pixels": size,
            "map_size_meters": self._cfg.map_size_meters,
            "mm_per_pixel": self._cfg.mm_per_pixel,
            "scan_count": count,
            "final_pose": {"x_mm": pose[0], "y_mm": pose[1], "theta_deg": pose[2]},
        }
        json_path.write_text(json.dumps(meta, indent=2, ensure_ascii=False))

        logger.info(f"[SLAM] 地图已保存：{pgm_path}")
        return {"name": name, "pgm": str(pgm_path), "json": str(json_path)}

    def load_map(self, name: str) -> bool:
        """
        从 maps/ 目录加载已保存的地图（恢复 map_bytes，不恢复位姿）。

        Returns:
            True = 加载成功
        """
        pgm_path  = self._cfg.maps_dir / f"{name}.pgm"
        json_path = self._cfg.maps_dir / f"{name}.json"

        if not pgm_path.exists():
            logger.error(f"[SLAM] 地图文件不存在：{pgm_path}")
            return False

        try:
            # 读 PGM
            with open(pgm_path, "rb") as f:
                # 跳过 PGM 头（3 行注释/参数）
                for _ in range(3):
                    f.readline()
                data = f.read()
            size = self._cfg.map_size_pixels
            expected = size * size
            if len(data) != expected:
                logger.error(f"[SLAM] PGM 大小不匹配：{len(data)} ≠ {expected}")
                return False

            with self._lock:
                self._map_bytes = bytearray(data)

            logger.info(f"[SLAM] 地图已加载：{pgm_path}")
            return True

        except Exception as e:
            logger.error(f"[SLAM] 加载地图失败：{e}")
            return False

    def list_maps(self) -> list[dict]:
        """列出 maps/ 目录下所有已保存的地图（读 JSON 元数据）。"""
        result = []
        for json_path in sorted(self._cfg.maps_dir.glob("*.json")):
            try:
                meta = json.loads(json_path.read_text())
                meta["pgm_exists"] = (self._cfg.maps_dir / f"{meta['name']}.pgm").exists()
                result.append(meta)
            except Exception:
                continue
        return result

    # ─── 状态查询 ────────────────────────────────────────────────

    @property
    def is_available(self) -> bool:
        return self._is_available

    @property
    def is_mapping(self) -> bool:
        with self._lock:
            return self._is_mapping

    @property
    def scan_count(self) -> int:
        with self._lock:
            return self._scan_count

    @property
    def status(self) -> dict:
        with self._lock:
            x, y, theta = self._pose
            elapsed = (
                time.time() - self._session_start
                if self._session_start else 0
            )
        return {
            "available": self._is_available,
            "is_mapping": self._is_mapping,
            "scan_count": self._scan_count,
            "elapsed_s": round(elapsed, 1),
            "pose": {"x_mm": round(x, 1), "y_mm": round(y, 1), "theta_deg": round(theta, 2)},
            "map_size_pixels": self._cfg.map_size_pixels,
            "map_size_meters": self._cfg.map_size_meters,
            "mm_per_pixel": round(self._cfg.mm_per_pixel, 1),
        }


# ── 扫描重采样 ────────────────────────────────────────────────────

def _resample_scan(scan, cfg: SlamConfig) -> list[int]:
    """
    将 LidarScan（~450 个不等角度点）重采样为 360 个等角度距离值。

    breezyslam 要求输入是 scan_size 个均匀分布的距离（mm），
    index i 对应角度 i × (360 / scan_size)。

    策略：
      - 每个 1° 桶取最小距离（最近障碍优先）
      - 无读数的桶填 0（breezyslam 解释为"无探测"）
      - 无效点（distance=0 或超限）跳过
    """
    scan_size = 360  # 与 LD06Laser.SCAN_SIZE 保持一致
    result = [0] * scan_size

    for point in scan.points:
        if not point.is_valid:
            continue
        idx = int(point.angle * scan_size / 360.0) % scan_size
        if result[idx] == 0 or point.distance < result[idx]:
            result[idx] = point.distance

    return result
