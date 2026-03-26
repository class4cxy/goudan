"""
Costmap — 三层代价地图
======================
层级结构（优先级从高到低）：

  Layer 3: 动态障碍层（实时，TimeToLive 衰减）
    来源：实时 LiDAR 扫描中出现的、但原始地图里没有的障碍
    时效：每个动态障碍格子有时间戳，超过 ttl_s 秒未更新则自动清除
    用途：临时障碍（搬来的椅子、宠物、行人）的实时绕行

  Layer 2: 膨胀层（由 Layer 1 推导，静态不变）
    原理：障碍物向外扩展 inflation_radius_mm，防止机器车贴墙行驶
    实现：对静态二值地图做距离变换后阈值化

  Layer 1: 静态层（加载的 PGM 地图，不变）
    来源：breezyslam 保存的历史地图
    语义：LETHAL_OBSTACLE / FREE

代价值定义（与 ROS Nav2 兼容）：
  0          = FREE（自由通行）
  1–127      = INSCRIBED 膨胀区（可通行但有代价，路径规划会绕开）
  128–252    = 高代价区
  253        = INSCRIBED_INFLATED（机器车半径内的障碍）
  254        = LETHAL_OBSTACLE（绝对障碍，不可进入）
  255        = UNKNOWN（未探索）
"""

import os
import time
import threading
import math
import logging
from dataclasses import dataclass
from pathlib import Path

import numpy as np

logger = logging.getLogger(__name__)

# 代价常量
FREE             = 0
LETHAL_OBSTACLE  = 254
INSCRIBED        = 253
UNKNOWN          = 255


@dataclass
class CostmapConfig:
    # 地图参数（从 SLAM 地图元数据读取）
    map_size_pixels: int   = int(os.environ.get("SLAM_MAP_SIZE_PIXELS", "1000"))
    mm_per_pixel:    float = float(os.environ.get("COSTMAP_MM_PER_PIXEL", "20.0"))

    # 膨胀参数
    robot_radius_mm: float = float(os.environ.get("COSTMAP_ROBOT_RADIUS_MM", "180.0"))
    inflation_mm:    float = float(os.environ.get("COSTMAP_INFLATION_MM",    "60.0"))

    # 动态障碍层
    dynamic_ttl_s:   float = float(os.environ.get("COSTMAP_DYNAMIC_TTL_S",  "5.0"))


class Costmap:
    """
    三层代价地图，线程安全。

    典型用法：
        cm = Costmap(config)
        cm.load_static(map_bytes, map_size_pixels, mm_per_pixel)
        cm.update_dynamic(lidar_scan, robot_x_mm, robot_y_mm, robot_theta_deg)
        cost = cm.get_cost(px, py)
        grid = cm.get_grid()   # 供 A* 使用
    """

    def __init__(self, config: CostmapConfig | None = None) -> None:
        self._cfg   = config or CostmapConfig()
        self._lock  = threading.Lock()
        sz          = self._cfg.map_size_pixels

        self._static:  np.ndarray = np.full((sz, sz), UNKNOWN, dtype=np.uint8)
        self._inflated: np.ndarray = np.full((sz, sz), UNKNOWN, dtype=np.uint8)
        self._dynamic: np.ndarray = np.zeros((sz, sz), dtype=np.float64)  # timestamp
        self._combined: np.ndarray = np.full((sz, sz), UNKNOWN, dtype=np.uint8)
        self._is_loaded = False

    # ─── 静态层加载 ───────────────────────────────────────────────

    def load_static(
        self,
        map_bytes: bytes,
        map_size_pixels: int,
        mm_per_pixel: float,
    ) -> None:
        """从 breezyslam map_bytes 构建静态层 + 膨胀层。"""
        try:
            from scipy.ndimage import distance_transform_edt
        except ImportError:
            logger.error("[Costmap] 需要 scipy：pip install scipy")
            return

        arr = np.frombuffer(map_bytes, dtype=np.uint8).reshape(
            (map_size_pixels, map_size_pixels)
        )

        sz  = map_size_pixels
        mpp = mm_per_pixel

        # 静态层
        static = np.full((sz, sz), UNKNOWN, dtype=np.uint8)
        static[arr > 127]               = FREE
        static[(arr > 0) & (arr <= 127)] = LETHAL_OBSTACLE

        # 膨胀层：距离变换后按机器车半径 + 安全余量阈值化
        obstacle_mask = static == LETHAL_OBSTACLE
        dist_px = distance_transform_edt(~obstacle_mask).astype(np.float32)

        robot_px     = self._cfg.robot_radius_mm  / mpp
        inflation_px = self._cfg.inflation_mm     / mpp
        total_px     = robot_px + inflation_px

        inflated = static.copy()
        # 机器人半径内：LETHAL（机器人中心不可进入）
        inflated[(dist_px > 0) & (dist_px <= robot_px)] = INSCRIBED
        # 安全余量：线性代价 1–127
        zone = (dist_px > robot_px) & (dist_px <= total_px)
        cost_vals = (128 * (1.0 - (dist_px[zone] - robot_px) / inflation_px)).astype(np.uint8)
        inflated[zone] = np.clip(cost_vals, 1, 127)

        with self._lock:
            self._cfg = CostmapConfig(
                map_size_pixels=sz,
                mm_per_pixel=mpp,
                robot_radius_mm=self._cfg.robot_radius_mm,
                inflation_mm=self._cfg.inflation_mm,
                dynamic_ttl_s=self._cfg.dynamic_ttl_s,
            )
            self._static   = static
            self._inflated = inflated
            self._dynamic  = np.zeros((sz, sz), dtype=np.float64)
            self._rebuild_combined()
            self._is_loaded = True

        logger.info(
            f"[Costmap] 静态层已加载 {sz}×{sz} px | "
            f"机器车半径={self._cfg.robot_radius_mm}mm 膨胀={self._cfg.inflation_mm}mm"
        )

    # ─── 动态障碍层更新 ───────────────────────────────────────────

    def update_dynamic(
        self,
        scan,
        robot_x_mm: float,
        robot_y_mm: float,
        robot_theta_deg: float,
    ) -> None:
        """
        根据当前 LiDAR 扫描更新动态障碍层。
        仅标记原始静态地图为 FREE 但扫描显示有障碍的格子。
        """
        if not self._is_loaded:
            return

        mpp  = self._cfg.mm_per_pixel
        half = self._cfg.map_size_pixels / 2.0
        now  = time.monotonic()
        cos_t = math.cos(math.radians(robot_theta_deg))
        sin_t = math.sin(math.radians(robot_theta_deg))

        new_obstacles: list[tuple[int, int]] = []

        for pt in scan.points:
            if not pt.is_valid:
                continue
            a_rad = math.radians(pt.angle)
            ex = pt.distance * math.cos(a_rad)
            ey = pt.distance * math.sin(a_rad)
            # 机器人坐标系 → 地图 mm
            wx = robot_x_mm + ex * cos_t - ey * sin_t
            wy = robot_y_mm + ex * sin_t + ey * cos_t
            # mm → 像素
            px = int(wx / mpp + half)
            py = int(wy / mpp + half)
            sz = self._cfg.map_size_pixels
            if 0 <= px < sz and 0 <= py < sz:
                # 仅标记静态地图是自由空间的格子（真正的新障碍）
                if self._static[py, px] == FREE:
                    new_obstacles.append((px, py))

        # 清理过期动态障碍
        ttl = self._cfg.dynamic_ttl_s
        with self._lock:
            expired = self._dynamic > 0
            expired &= (now - self._dynamic) > ttl
            self._dynamic[expired] = 0.0

            for px, py in new_obstacles:
                self._dynamic[py, px] = now

            if new_obstacles or np.any(expired):
                self._rebuild_combined()

    def clear_dynamic(self) -> None:
        """清空动态障碍层（调试用）。"""
        with self._lock:
            sz = self._cfg.map_size_pixels
            self._dynamic = np.zeros((sz, sz), dtype=np.float64)
            self._rebuild_combined()

    # ─── 查询接口 ────────────────────────────────────────────────

    def get_cost(self, px: int, py: int) -> int:
        """查询像素 (px, py) 的代价值 0–255。"""
        sz = self._cfg.map_size_pixels
        if not (0 <= px < sz and 0 <= py < sz):
            return LETHAL_OBSTACLE
        with self._lock:
            return int(self._combined[py, px])

    def is_free(self, px: int, py: int) -> bool:
        """像素是否可通行（代价 < INSCRIBED）。"""
        return self.get_cost(px, py) < INSCRIBED

    def get_grid(self) -> np.ndarray:
        """返回合并代价栅格的副本（供 A* 使用），shape (H, W) uint8。"""
        with self._lock:
            return self._combined.copy()

    def mm_to_pixel(self, x_mm: float, y_mm: float) -> tuple[int, int]:
        """地图 mm 坐标 → 像素坐标。"""
        half = self._cfg.map_size_pixels / 2.0
        mpp  = self._cfg.mm_per_pixel
        return int(x_mm / mpp + half), int(y_mm / mpp + half)

    def pixel_to_mm(self, px: int, py: int) -> tuple[float, float]:
        """像素坐标 → 地图 mm 坐标。"""
        half = self._cfg.map_size_pixels / 2.0
        mpp  = self._cfg.mm_per_pixel
        return (px - half) * mpp, (py - half) * mpp

    @property
    def is_loaded(self) -> bool:
        return self._is_loaded

    @property
    def config(self) -> CostmapConfig:
        return self._cfg

    # ─── 内部 ────────────────────────────────────────────────────

    def _rebuild_combined(self) -> None:
        """合并所有层（在持锁状态下调用）。"""
        combined = self._inflated.copy()
        # 动态障碍：有时间戳的格子视为 LETHAL
        dyn_mask = self._dynamic > 0
        combined[dyn_mask] = LETHAL_OBSTACLE
        # 动态障碍的小范围膨胀（1 像素）
        from scipy.ndimage import binary_dilation
        try:
            inflated_dyn = binary_dilation(dyn_mask, iterations=1)
            zone = inflated_dyn & ~dyn_mask & (combined < INSCRIBED)
            combined[zone] = INSCRIBED
        except Exception:
            pass
        self._combined = combined
