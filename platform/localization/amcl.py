"""
AMCL — 自适应蒙特卡洛定位（粒子滤波）
=======================================
算法流程（每次 LiDAR 扫描）：
  1. 运动更新：根据里程计增量将每个粒子向前移动，加高斯噪声
  2. 观测更新：用似然场模型计算每个粒子与当前扫描的匹配得分（权重）
  3. 重采样：低方差系统重采样，高权重粒子多复制，低权重粒子淘汰
  4. 绑架检测：若全局平均权重持续偏低 → 触发全局重定位（粒子全局散布）
  5. 估计输出：加权平均位姿作为当前定位结果

似然场模型（Likelihood Field）：
  - 预计算地图的距离变换（DistanceField）
  - 对每个粒子：将当前激光端点投影到地图，查询到最近障碍的距离 d
  - 得分 = exp(-d² / (2σ²))；d=0（命中障碍）得分最高
  - 采样 N_RAYS 条射线（不需要全部 360°，36 条已足够）

依赖：numpy（核心）、localization.distance_field（地图）
"""

import math
import os
import threading
import time
import logging
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from .distance_field import DistanceField

logger = logging.getLogger(__name__)


# ── 配置 ──────────────────────────────────────────────────────────

@dataclass
class AmclConfig:
    # 粒子数
    n_particles: int   = int(os.environ.get("AMCL_N_PARTICLES",   "500"))

    # 运动模型噪声（高斯标准差）
    alpha_xy_mm: float = float(os.environ.get("AMCL_ALPHA_XY_MM",   "30.0"))
    alpha_th_deg: float = float(os.environ.get("AMCL_ALPHA_TH_DEG",  "5.0"))

    # 似然场传感器模型 σ（mm）
    sigma_hit_mm: float = float(os.environ.get("AMCL_SIGMA_HIT_MM", "80.0"))

    # 每次更新使用的射线数（从 360 条中均匀采样）
    n_rays: int        = int(os.environ.get("AMCL_N_RAYS",          "36"))

    # 绑架检测：低权重帧累计超过此阈值触发全局重定位
    kidnap_threshold: float  = float(os.environ.get("AMCL_KIDNAP_THRESHOLD", "0.02"))
    kidnap_frames:    int    = int(os.environ.get("AMCL_KIDNAP_FRAMES",      "10"))

    # 全局重定位时随机粒子的比例（0–1）
    recovery_ratio:   float  = float(os.environ.get("AMCL_RECOVERY_RATIO", "0.5"))

    # 收敛判断：粒子 x/y 标准差低于此值视为已收敛
    converge_xy_mm:   float  = float(os.environ.get("AMCL_CONVERGE_XY_MM", "200.0"))


# ── AMCL ──────────────────────────────────────────────────────────

class AMCL:
    """
    自适应蒙特卡洛定位。

    生命周期：
        amcl = AMCL(distance_field, config)
        amcl.start_global()          # 全局定位（位置未知）
        # 或
        amcl.start_at(x, y, theta)  # 已知起点（如充电桩）

        amcl.update(scan, velocities)  # 每帧激光调用
        pose, confidence = amcl.get_pose()

        amcl.reset()  # 触发重定位（被搬走后手动调用）

    线程安全：update() 和 get_pose() 可从不同线程调用。
    """

    def __init__(self, distance_field: DistanceField, config: AmclConfig | None = None) -> None:
        self._df   = distance_field
        self._cfg  = config or AmclConfig()
        self._lock = threading.Lock()

        n  = self._cfg.n_particles
        sz = self._df.size

        # 粒子状态：shape (N, 3) = [x_mm, y_mm, theta_deg]
        self._particles = np.zeros((n, 3), dtype=np.float64)
        # 粒子权重：shape (N,)
        self._weights   = np.ones(n, dtype=np.float64) / n

        self._is_running   = False
        self._is_converged = False
        self._low_weight_count = 0

        # 对数似然缓存（节省 exp 计算）
        self._log_sigma_sq = 2.0 * (self._cfg.sigma_hit_mm / self._df.mm_per_pixel) ** 2

    # ─── 启动方式 ────────────────────────────────────────────────

    def start_global(self) -> None:
        """全局定位：粒子均匀撒在地图所有可通行格子。"""
        free_pixels = self._get_free_pixels()
        n = self._cfg.n_particles

        if len(free_pixels) == 0:
            logger.warning("[AMCL] 地图中无可通行区域，无法全局定位")
            return

        idx = np.random.choice(len(free_pixels), size=n, replace=True)
        chosen = free_pixels[idx]       # shape (N, 2) in pixel coords

        mpp = self._df.mm_per_pixel
        half = self._df.size / 2.0

        with self._lock:
            # 像素坐标 → mm 坐标（与 slam_engine 坐标系一致）
            self._particles[:, 0] = (chosen[:, 0] - half) * mpp   # x_mm
            self._particles[:, 1] = (chosen[:, 1] - half) * mpp   # y_mm
            self._particles[:, 2] = np.random.uniform(-180, 180, n)
            self._weights[:] = 1.0 / n
            self._is_running   = True
            self._is_converged = False
            self._low_weight_count = 0

        logger.info(f"[AMCL] 全局定位已启动，{n} 个粒子均匀散布")

    def start_at(self, x_mm: float, y_mm: float, theta_deg: float) -> None:
        """已知起点定位：粒子集中在给定位姿附近（小范围高斯散布）。"""
        n = self._cfg.n_particles
        with self._lock:
            self._particles[:, 0] = np.random.normal(x_mm, 50.0, n)
            self._particles[:, 1] = np.random.normal(y_mm, 50.0, n)
            self._particles[:, 2] = np.random.normal(theta_deg, 5.0, n)
            self._weights[:] = 1.0 / n
            self._is_running   = True
            self._is_converged = False
            self._low_weight_count = 0
        logger.info(f"[AMCL] 已知起点定位：({x_mm:.0f}, {y_mm:.0f}, {theta_deg:.1f}°)")

    def reset(self) -> None:
        """手动触发全局重定位（机器人被搬走时调用）。"""
        logger.info("[AMCL] 触发全局重定位（reset）")
        self.start_global()

    # ─── 核心更新 ────────────────────────────────────────────────

    def update(self, scan, velocities: tuple[float, float, float]) -> None:
        """
        用一圈激光扫描 + 里程计增量更新粒子滤波。

        Args:
            scan:       LidarScan 对象（含 valid_points）
            velocities: (dxy_mm, dtheta_deg, dt_s) 来自 Odometry.get_velocity_for_slam()
        """
        if not self._is_running or not self._df.is_loaded:
            return

        dxy_mm, dtheta_deg, _ = velocities

        with self._lock:
            # ── 1. 运动模型 ───────────────────────────────────────
            self._motion_update(dxy_mm, dtheta_deg)

            # ── 2. 观测模型 ───────────────────────────────────────
            ray_endpoints = self._sample_rays(scan)
            if len(ray_endpoints) > 0:
                self._sensor_update(ray_endpoints)

            # ── 3. 绑架检测 ───────────────────────────────────────
            mean_w = float(np.mean(self._weights))
            if mean_w < self._cfg.kidnap_threshold:
                self._low_weight_count += 1
            else:
                self._low_weight_count = 0

            if self._low_weight_count >= self._cfg.kidnap_frames:
                logger.warning("[AMCL] 绑架检测：权重持续偏低，注入随机粒子")
                self._inject_random_particles()
                self._low_weight_count = 0

            # ── 4. 重采样 ─────────────────────────────────────────
            self._resample()

            # ── 5. 收敛检测 ───────────────────────────────────────
            std_x = float(np.std(self._particles[:, 0]))
            std_y = float(np.std(self._particles[:, 1]))
            self._is_converged = (
                max(std_x, std_y) < self._cfg.converge_xy_mm
            )

    # ─── 位姿读取 ────────────────────────────────────────────────

    def get_pose(self) -> tuple[float, float, float, float]:
        """
        返回当前估计位姿与置信度。

        Returns:
            (x_mm, y_mm, theta_deg, confidence)
            confidence: 0.0–1.0，粒子收敛程度（1.0=完全收敛）
        """
        with self._lock:
            wx = float(np.average(self._particles[:, 0], weights=self._weights))
            wy = float(np.average(self._particles[:, 1], weights=self._weights))
            # 角度加权平均（避免 ±180° 跳变）
            sin_t = np.average(np.sin(np.radians(self._particles[:, 2])), weights=self._weights)
            cos_t = np.average(np.cos(np.radians(self._particles[:, 2])), weights=self._weights)
            wt = float(math.degrees(math.atan2(sin_t, cos_t)))

            std_x = float(np.std(self._particles[:, 0]))
            std_y = float(np.std(self._particles[:, 1]))
            # confidence: 粒子标准差越小越大，最大 1.0
            conv = self._cfg.converge_xy_mm
            confidence = max(0.0, min(1.0, 1.0 - max(std_x, std_y) / conv))

        return wx, wy, wt, confidence

    @property
    def is_converged(self) -> bool:
        with self._lock:
            return self._is_converged

    @property
    def is_running(self) -> bool:
        return self._is_running

    # ─── 内部算法 ────────────────────────────────────────────────

    def _motion_update(self, dxy_mm: float, dtheta_deg: float) -> None:
        """运动模型：给每个粒子施加带噪声的里程计增量。"""
        n  = self._cfg.n_particles
        a  = self._cfg.alpha_xy_mm
        at = self._cfg.alpha_th_deg

        noise_xy = np.random.normal(0, a,  n)
        noise_th = np.random.normal(0, at, n)

        theta_rad = np.radians(self._particles[:, 2])
        self._particles[:, 0] += (dxy_mm + noise_xy) * np.cos(theta_rad)
        self._particles[:, 1] += (dxy_mm + noise_xy) * np.sin(theta_rad)
        self._particles[:, 2] += dtheta_deg + noise_th
        self._particles[:, 2]  = (self._particles[:, 2] + 180) % 360 - 180

    def _sample_rays(self, scan) -> np.ndarray:
        """
        从 LidarScan 中均匀采样 N_RAYS 条有效射线，
        返回机器人坐标系下的端点 array，shape (M, 2)，单位 mm。
        """
        pts = [(p.angle, p.distance) for p in scan.points if p.is_valid]
        if not pts:
            return np.empty((0, 2))

        # 均匀采样
        n_rays = min(self._cfg.n_rays, len(pts))
        step   = max(1, len(pts) // n_rays)
        sampled = pts[::step][:n_rays]

        endpoints = np.array([
            [d * math.cos(math.radians(a)), d * math.sin(math.radians(a))]
            for a, d in sampled
        ])
        return endpoints  # shape (M, 2) in robot frame (mm)

    def _sensor_update(self, ray_endpoints: np.ndarray) -> None:
        """
        似然场传感器模型：计算每个粒子与当前扫描的匹配概率，更新权重。
        """
        mpp  = self._df.mm_per_pixel
        half = self._df.size / 2.0
        sig2 = self._log_sigma_sq   # 2σ²（像素²）

        n_pts = len(ray_endpoints)
        log_weights = np.zeros(len(self._particles))

        for i, (px, py, pt) in enumerate(self._particles):
            cos_t = math.cos(math.radians(pt))
            sin_t = math.sin(math.radians(pt))
            log_w = 0.0
            for ex, ey in ray_endpoints:
                # 机器人坐标系 → 地图坐标系（mm）
                wx = px + ex * cos_t - ey * sin_t
                wy = py + ex * sin_t + ey * cos_t
                # mm → 像素
                map_px = int(wx / mpp + half)
                map_py = int(wy / mpp + half)
                d = self._df.lookup(map_px, map_py)  # 到最近障碍距离（像素）
                log_w += -(d * d) / sig2
            log_weights[i] = log_w / max(n_pts, 1)

        # 数值稳定：减去最大值后再 exp
        log_weights -= log_weights.max()
        self._weights *= np.exp(log_weights)
        total = self._weights.sum()
        if total > 0:
            self._weights /= total
        else:
            self._weights[:] = 1.0 / len(self._weights)

    def _resample(self) -> None:
        """低方差系统重采样（Systematic Resampling）。"""
        n = len(self._particles)
        cumsum = np.cumsum(self._weights)
        cumsum[-1] = 1.0  # 防止浮点误差

        start = np.random.uniform(0, 1.0 / n)
        positions = (start + np.arange(n) / n)

        indices = np.searchsorted(cumsum, positions)
        self._particles = self._particles[indices]
        self._weights[:] = 1.0 / n

    def _inject_random_particles(self) -> None:
        """绑架恢复：将一部分粒子随机散布到可通行区域。"""
        free_pixels = self._get_free_pixels()
        if len(free_pixels) == 0:
            return

        n       = self._cfg.n_particles
        n_rand  = int(n * self._cfg.recovery_ratio)
        mpp     = self._df.mm_per_pixel
        half    = self._df.size / 2.0

        idx     = np.random.choice(len(free_pixels), size=n_rand, replace=True)
        chosen  = free_pixels[idx]

        start = n - n_rand
        self._particles[start:, 0] = (chosen[:, 0] - half) * mpp
        self._particles[start:, 1] = (chosen[:, 1] - half) * mpp
        self._particles[start:, 2] = np.random.uniform(-180, 180, n_rand)
        self._weights[:] = 1.0 / n

    def _get_free_pixels(self) -> np.ndarray:
        """返回地图中所有可通行像素坐标，shape (M, 2)。"""
        if self._df._field is None:
            return np.empty((0, 2), dtype=np.int32)
        ys, xs = np.where(self._df._field > 0)
        return np.stack([xs, ys], axis=1).astype(np.int32)

    @property
    def status(self) -> dict:
        if not self._is_running:
            return {"running": False}
        x, y, t, conf = self.get_pose()
        with self._lock:
            std_x = float(np.std(self._particles[:, 0]))
            std_y = float(np.std(self._particles[:, 1]))
        return {
            "running":     True,
            "converged":   self._is_converged,
            "pose":        {"x_mm": round(x, 1), "y_mm": round(y, 1), "theta_deg": round(t, 2)},
            "confidence":  round(conf, 3),
            "std_xy_mm":   round(max(std_x, std_y), 1),
            "n_particles": self._cfg.n_particles,
            "low_weight_count": self._low_weight_count,
        }
