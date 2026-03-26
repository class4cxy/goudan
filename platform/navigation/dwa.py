"""
DWAPlanner — 动态窗口法局部路径规划器
=======================================
算法流程：
  1. 构建动态窗口：当前速度 ± 最大加速度 × dt，并与速度限制求交
  2. 采样速度对 (v, ω)：线速度 × 角速度的离散网格
  3. 轨迹仿真：以每个 (v, ω) 向前模拟 predict_time 秒
  4. 碰撞检测：轨迹上的位置在代价地图中检查是否有障碍
  5. 轨迹评分：heading（朝向目标） + dist（离障碍的余量） + velocity（速度奖励）
  6. 选最高分的 (v, ω) 输出

轨迹转为底盘指令：
  线速度 v_mm_s + 角速度 ω_deg_s → 左右轮速度（差速运动学逆解）
  left_speed  = (v - ω_rad * wheel_base / 2) / wheel_radius
  right_speed = (v + ω_rad * wheel_base / 2) / wheel_radius
  归一化到 [-100, 100] PWM 占空比
"""

import math
import os
import logging
from dataclasses import dataclass

import numpy as np

logger = logging.getLogger(__name__)

LETHAL_OBSTACLE = 254
INSCRIBED       = 253


@dataclass
class RobotConstraints:
    """差速底盘运动约束（需根据实际车辆标定）。"""
    max_v_mm_s:    float = float(os.environ.get("DWA_MAX_V_MM_S",    "300.0"))  # 最大线速度
    min_v_mm_s:    float = float(os.environ.get("DWA_MIN_V_MM_S",      "0.0"))  # 最小线速度（不倒退）
    max_w_deg_s:   float = float(os.environ.get("DWA_MAX_W_DEG_S",  "120.0"))  # 最大角速度
    max_acc_mm_s2: float = float(os.environ.get("DWA_MAX_ACC_MM_S2", "200.0"))  # 最大线加速度
    max_acc_w:     float = float(os.environ.get("DWA_MAX_ACC_W",      "90.0"))  # 最大角加速度（°/s²）
    wheel_base_mm: float = float(os.environ.get("ODOM_WHEEL_BASE_MM","160.0"))  # 轮距
    wheel_radius_mm: float = float(os.environ.get("ODOM_WHEEL_RADIUS_MM","33.0"))  # 轮径


@dataclass
class DWAConfig:
    dt:            float = float(os.environ.get("DWA_DT",           "0.1"))   # 控制周期（s）
    predict_time:  float = float(os.environ.get("DWA_PREDICT_TIME", "1.5"))   # 轨迹预测时长（s）
    v_samples:     int   = int(os.environ.get("DWA_V_SAMPLES",     "10"))    # 线速度采样数
    w_samples:     int   = int(os.environ.get("DWA_W_SAMPLES",     "20"))    # 角速度采样数

    # 评分权重
    w_heading:     float = float(os.environ.get("DWA_W_HEADING",   "0.4"))
    w_dist:        float = float(os.environ.get("DWA_W_DIST",      "0.2"))
    w_velocity:    float = float(os.environ.get("DWA_W_VELOCITY",  "0.4"))

    goal_tolerance_mm: float = float(os.environ.get("DWA_GOAL_TOL_MM", "150.0"))


@dataclass
class VelocityCmd:
    """输出速度指令。"""
    v_mm_s:     float   # 线速度（mm/s），正=前进
    w_deg_s:    float   # 角速度（°/s），正=左转（逆时针）
    left_pwm:   float   # 左轮 PWM [-100, 100]
    right_pwm:  float   # 右轮 PWM [-100, 100]

    @classmethod
    def stop(cls) -> "VelocityCmd":
        return cls(0.0, 0.0, 0.0, 0.0)


class DWAPlanner:
    """动态窗口法局部规划器。"""

    def __init__(
        self,
        constraints: RobotConstraints | None = None,
        config:      DWAConfig | None = None,
    ) -> None:
        self._rc  = constraints or RobotConstraints()
        self._cfg = config      or DWAConfig()

    def compute(
        self,
        robot_x:   float,
        robot_y:   float,
        robot_th:  float,
        curr_v:    float,
        curr_w:    float,
        goal_x:    float,
        goal_y:    float,
        grid:      np.ndarray,
        mm_per_pixel: float,
    ) -> VelocityCmd:
        """
        计算一步局部速度指令。

        Args:
            robot_x/y/th: 当前机器人位姿（mm, mm, °）
            curr_v/w:     当前线速度（mm/s）/ 角速度（°/s）
            goal_x/y:     当前子目标（mm）
            grid:         代价地图 numpy 数组 (H, W)
            mm_per_pixel: 地图比例尺

        Returns:
            VelocityCmd
        """
        dist_to_goal = math.hypot(goal_x - robot_x, goal_y - robot_y)
        if dist_to_goal < self._cfg.goal_tolerance_mm:
            return VelocityCmd.stop()

        dw = self._calc_dynamic_window(curr_v, curr_w)
        best_score = -float("inf")
        best_cmd   = VelocityCmd.stop()

        v_range = np.linspace(dw[0], dw[1], self._cfg.v_samples)
        w_range = np.linspace(dw[2], dw[3], self._cfg.w_samples)

        for v in v_range:
            for w in w_range:
                traj = self._simulate_trajectory(robot_x, robot_y, robot_th, v, w)
                if not self._is_trajectory_safe(traj, grid, mm_per_pixel):
                    continue
                score = self._score(traj, v, goal_x, goal_y, grid, mm_per_pixel)
                if score > best_score:
                    best_score = score
                    left_pwm, right_pwm = self._diff_drive(v, w)
                    best_cmd = VelocityCmd(v, w, left_pwm, right_pwm)

        return best_cmd

    # ─── 内部算法 ────────────────────────────────────────────────

    def _calc_dynamic_window(self, v: float, w: float) -> tuple[float, float, float, float]:
        """
        动态窗口 [v_min, v_max, w_min, w_max]。
        """
        rc  = self._rc
        cfg = self._cfg
        dt  = cfg.dt

        v_min = max(rc.min_v_mm_s, v - rc.max_acc_mm_s2 * dt)
        v_max = min(rc.max_v_mm_s, v + rc.max_acc_mm_s2 * dt)
        w_min = max(-rc.max_w_deg_s, w - rc.max_acc_w * dt)
        w_max = min( rc.max_w_deg_s, w + rc.max_acc_w * dt)
        return v_min, v_max, w_min, w_max

    def _simulate_trajectory(
        self, x: float, y: float, th: float, v: float, w: float
    ) -> list[tuple[float, float, float]]:
        """以 (v, ω) 向前仿真，返回轨迹点列表 [(x, y, th), ...]。"""
        dt      = self._cfg.dt
        n_steps = int(self._cfg.predict_time / dt)
        traj    = [(x, y, th)]
        for _ in range(n_steps):
            th += w * dt
            x  += v * math.cos(math.radians(th)) * dt
            y  += v * math.sin(math.radians(th)) * dt
            traj.append((x, y, th))
        return traj

    def _is_trajectory_safe(
        self,
        traj: list[tuple[float, float, float]],
        grid: np.ndarray,
        mpp:  float,
    ) -> bool:
        """轨迹上任一点在障碍区域则返回 False。"""
        H, W  = grid.shape
        half  = W / 2.0
        for x, y, _ in traj[1:]:
            px = int(x / mpp + half)
            py = int(y / mpp + half)
            if not (0 <= px < W and 0 <= py < H):
                return False
            if grid[py, px] >= INSCRIBED:
                return False
        return True

    def _score(
        self,
        traj:  list[tuple[float, float, float]],
        v:     float,
        gx:    float,
        gy:    float,
        grid:  np.ndarray,
        mpp:   float,
    ) -> float:
        """
        轨迹综合评分。
          heading:  终点朝向与目标方向的一致性
          dist:     轨迹上距最近障碍的最小距离（越大越好）
          velocity: 速度奖励（速度越快得分越高）
        """
        ex, ey, eth = traj[-1]
        H, W = grid.shape
        half = W / 2.0

        # heading 评分
        goal_angle = math.degrees(math.atan2(gy - ey, gx - ex))
        heading    = 1.0 - abs((goal_angle - eth + 180) % 360 - 180) / 180.0

        # dist 评分：轨迹上最近障碍
        min_dist_px = float("inf")
        for x, y, _ in traj:
            px = int(x / mpp + half)
            py = int(y / mpp + half)
            if 0 <= px < W and 0 <= py < H:
                c = int(grid[py, px])
                # 将代价值转为"距障碍的像素估计距离"
                if c >= INSCRIBED:
                    d = 0.0
                else:
                    d = (INSCRIBED - c) / float(INSCRIBED)
                min_dist_px = min(min_dist_px, d)
        dist = min_dist_px if min_dist_px < float("inf") else 1.0

        # velocity 评分
        velocity = v / max(self._rc.max_v_mm_s, 1.0)

        cfg = self._cfg
        return cfg.w_heading * heading + cfg.w_dist * dist + cfg.w_velocity * velocity

    def _diff_drive(self, v: float, w: float) -> tuple[float, float]:
        """
        差速逆解：(v_mm_s, w_deg_s) → (left_pwm, right_pwm) ∈ [-100, 100]。
        """
        w_rad  = math.radians(w)
        half_b = self._rc.wheel_base_mm / 2.0
        r      = self._rc.wheel_radius_mm

        left_v  = v - w_rad * half_b   # mm/s
        right_v = v + w_rad * half_b   # mm/s

        # 线速度 → 角速度（rad/s）→ 归一化
        max_wheel_v = self._rc.max_v_mm_s + math.radians(self._rc.max_w_deg_s) * half_b
        left_pwm  = max(-100.0, min(100.0, left_v  / max_wheel_v * 100.0))
        right_pwm = max(-100.0, min(100.0, right_v / max_wheel_v * 100.0))
        return left_pwm, right_pwm
