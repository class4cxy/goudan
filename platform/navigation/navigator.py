"""
Navigator — 导航总协调器
=========================
整合 AMCL（定位）、A*（全局规划）、DWA（局部规划）和 Costmap（代价地图），
提供高层导航接口：

  navigate_to(x_mm, y_mm)  → 机器车自主导航到目标点
  cancel()                  → 取消当前导航
  status                    → 当前导航状态

导航控制循环（10Hz）：
  ① AMCL 更新定位
  ② Costmap 更新动态障碍层
  ③ 若路径为空或障碍变化显著 → A* 重规划
  ④ DWA 计算当前速度指令
  ⑤ 发送速度指令给底盘 Chassis

状态机：
  IDLE → LOCALIZING → NAVIGATING → ARRIVED / FAILED
"""

import asyncio
import math
import os
import threading
import time
import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable

logger = logging.getLogger(__name__)


class NavigationStatus(str, Enum):
    IDLE        = "idle"
    LOCALIZING  = "localizing"    # AMCL 尚未收敛
    NAVIGATING  = "navigating"
    ARRIVED     = "arrived"
    FAILED      = "failed"
    CANCELLED   = "cancelled"


@dataclass
class NavigationGoal:
    x_mm: float
    y_mm: float
    label: str = ""


@dataclass
class NavigatorConfig:
    control_hz:          float = float(os.environ.get("NAV_CONTROL_HZ",       "10.0"))
    replan_every_s:      float = float(os.environ.get("NAV_REPLAN_EVERY_S",    "3.0"))
    arrived_tolerance_mm: float = float(os.environ.get("NAV_ARRIVED_TOL_MM", "150.0"))
    localize_timeout_s:  float = float(os.environ.get("NAV_LOCALIZE_TIMEOUT", "60.0"))
    max_nav_time_s:      float = float(os.environ.get("NAV_MAX_TIME_S",       "300.0"))


class Navigator:
    """
    导航总协调器。

    依赖注入：
        amcl     — AMCL 实例
        costmap  — Costmap 实例
        planner  — AStarPlanner 实例
        dwa      — DWAPlanner 实例
        odometry — Odometry 实例
        chassis  — Chassis 实例（执行速度指令）
        lidar    — LidarSensor 实例（用于动态层更新）
    """

    def __init__(
        self,
        amcl,
        costmap,
        planner,
        dwa,
        odometry,
        chassis,
        on_status_change: Callable[[NavigationStatus, dict], None] | None = None,
        config: NavigatorConfig | None = None,
    ) -> None:
        self._amcl     = amcl
        self._costmap  = costmap
        self._planner  = planner
        self._dwa      = dwa
        self._odom     = odometry
        self._chassis  = chassis
        self._on_status = on_status_change
        self._cfg      = config or NavigatorConfig()

        self._lock     = threading.Lock()
        self._status   = NavigationStatus.IDLE
        self._goal:    NavigationGoal | None = None
        self._path_mm: list[tuple[float, float]] = []
        self._path_idx: int  = 0
        self._curr_v:   float = 0.0
        self._curr_w:   float = 0.0

        self._thread:  threading.Thread | None = None
        self._running  = False
        self._last_replan = 0.0
        self._nav_start:  float = 0.0
        self._replan_fail_count: int = 0   # A* 连续失败次数，超阈值进入 FAILED

    # ─── 公共接口 ─────────────────────────────────────────────────

    def start(self) -> None:
        """启动控制循环线程。"""
        self._running = True
        self._thread  = threading.Thread(
            target=self._control_loop, daemon=True, name="navigator"
        )
        self._thread.start()
        logger.info("[Navigator] 控制循环已启动")

    def stop(self) -> None:
        self._running = False
        self._chassis_stop()
        if self._thread:
            self._thread.join(timeout=2.0)

    def navigate_to(self, x_mm: float, y_mm: float, label: str = "") -> dict:
        """
        发起导航任务。

        Returns:
            {"ok": bool, "message": str}
        """
        if not self._amcl.is_running:
            return {"ok": False, "message": "AMCL 未启动，请先调用 /localize/start"}
        if not self._costmap.is_loaded:
            return {"ok": False, "message": "地图未加载，请先调用 /localize/start"}

        with self._lock:
            self._goal    = NavigationGoal(x_mm, y_mm, label)
            self._path_mm = []
            self._path_idx = 0
            self._nav_start = time.monotonic()
            self._last_replan = 0.0
            self._replan_fail_count = 0
            self._set_status(NavigationStatus.LOCALIZING if not self._amcl.is_converged
                             else NavigationStatus.NAVIGATING)

        logger.info(f"[Navigator] 导航目标：({x_mm:.0f}, {y_mm:.0f}) {label}")
        return {"ok": True, "message": f"导航已启动，目标 ({x_mm:.0f}, {y_mm:.0f})"}

    def cancel(self) -> None:
        with self._lock:
            self._goal = None
            self._path_mm = []
            self._set_status(NavigationStatus.CANCELLED)
        self._chassis_stop()
        logger.info("[Navigator] 导航已取消")

    # ─── 控制循环 ────────────────────────────────────────────────

    def _control_loop(self) -> None:
        interval = 1.0 / self._cfg.control_hz
        while self._running:
            t0 = time.monotonic()
            try:
                self._step()
            except Exception as e:
                logger.error(f"[Navigator] 控制循环异常：{e}", exc_info=True)
            elapsed = time.monotonic() - t0
            time.sleep(max(0.0, interval - elapsed))

    def _step(self) -> None:
        with self._lock:
            goal   = self._goal
            status = self._status

        if goal is None or status in (
            NavigationStatus.IDLE, NavigationStatus.ARRIVED,
            NavigationStatus.FAILED, NavigationStatus.CANCELLED,
        ):
            return

        now = time.monotonic()

        # ── ① 获取 AMCL 位姿 ──────────────────────────────────────
        rx, ry, rt, confidence = self._amcl.get_pose()

        # ── ② LOCALIZING 阶段：等待 AMCL 收敛 ─────────────────────
        if status == NavigationStatus.LOCALIZING:
            if self._amcl.is_converged:
                with self._lock:
                    self._set_status(NavigationStatus.NAVIGATING)
            elif now - self._nav_start > self._cfg.localize_timeout_s:
                self._fail("AMCL 定位超时，请尝试缓慢移动机器车或手动重定位")
            else:
                # 缓慢原地旋转以帮助 AMCL 收敛
                self._chassis.turn_right(speed=30)
            return

        # ── ③ 超时检查 ────────────────────────────────────────────
        if now - self._nav_start > self._cfg.max_nav_time_s:
            self._fail("导航超时")
            return

        # ── ④ 到达检测 ────────────────────────────────────────────
        dist_to_goal = math.hypot(goal.x_mm - rx, goal.y_mm - ry)
        if dist_to_goal < self._cfg.arrived_tolerance_mm:
            self._chassis_stop()
            with self._lock:
                self._goal = None
                self._set_status(NavigationStatus.ARRIVED)
            logger.info(f"[Navigator] 已到达目标 ({goal.x_mm:.0f}, {goal.y_mm:.0f})")
            return

        # ── ⑤ A* 重规划（首次 or 定期 or 路径走完）────────────────
        with self._lock:
            need_replan = (
                len(self._path_mm) == 0
                or self._path_idx >= len(self._path_mm)
                or now - self._last_replan > self._cfg.replan_every_s
            )

        if need_replan:
            self._replan(rx, ry, goal.x_mm, goal.y_mm)

        # ── ⑥ 选取当前子目标 ──────────────────────────────────────
        with self._lock:
            if not self._path_mm or self._path_idx >= len(self._path_mm):
                # A* 连续失败超过阈值（约 30s）→ 进入 FAILED，避免静默卡住
                self._replan_fail_count += 1
                if self._replan_fail_count >= int(self._cfg.control_hz * 30):
                    self._fail(f"A* 持续规划失败 {self._replan_fail_count} 次，目标不可达或地图异常")
                return
            subgoal = self._path_mm[self._path_idx]

        # 子目标到达则前进到下一个
        d_sub = math.hypot(subgoal[0] - rx, subgoal[1] - ry)
        if d_sub < self._cfg.arrived_tolerance_mm * 0.5:
            with self._lock:
                self._path_idx += 1
            return

        # ── ⑦ DWA 局部规划 ────────────────────────────────────────
        grid = self._costmap.get_grid()
        cmd  = self._dwa.compute(
            robot_x=rx, robot_y=ry, robot_th=rt,
            curr_v=self._curr_v, curr_w=self._curr_w,
            goal_x=subgoal[0], goal_y=subgoal[1],
            grid=grid, mm_per_pixel=self._costmap.config.mm_per_pixel,
        )
        self._curr_v = cmd.v_mm_s
        self._curr_w = cmd.w_deg_s

        # ── ⑧ 发送速度指令给底盘 ──────────────────────────────────
        self._apply_velocity(cmd)

    def _replan(self, rx: float, ry: float, gx: float, gy: float) -> None:
        grid = self._costmap.get_grid()
        mpp  = self._costmap.config.mm_per_pixel

        s_px = self._costmap.mm_to_pixel(rx, ry)
        g_px = self._costmap.mm_to_pixel(gx, gy)

        result = self._planner.plan(grid, s_px, g_px, mpp)
        if result.success:
            with self._lock:
                self._path_mm  = result.path_mm
                self._path_idx = 0
                self._last_replan = time.monotonic()
                self._replan_fail_count = 0
            logger.info(f"[Navigator] A* 规划成功，路径点数：{len(result.path_mm)}")
        else:
            logger.warning(f"[Navigator] A* 规划失败：{result.message}")

    def _apply_velocity(self, cmd) -> None:
        """将 DWA 输出的 PWM 指令发送给底盘。"""
        lp = cmd.left_pwm
        rp = cmd.right_pwm
        if abs(lp) < 5 and abs(rp) < 5:
            self._chassis_stop()
            return
        spd = int(max(abs(lp), abs(rp)))
        if abs(lp - rp) < 10:
            self._chassis.forward(speed=spd)
        elif lp < rp:
            self._chassis.turn_right(speed=spd)
        else:
            self._chassis.turn_left(speed=spd)

    def _chassis_stop(self) -> None:
        try:
            self._chassis.stop()
        except Exception:
            pass
        self._curr_v = 0.0
        self._curr_w = 0.0

    def _fail(self, reason: str) -> None:
        logger.error(f"[Navigator] 导航失败：{reason}")
        self._chassis_stop()
        with self._lock:
            self._goal = None
            self._set_status(NavigationStatus.FAILED)

    def _set_status(self, status: NavigationStatus) -> None:
        """设置状态并触发回调（在持锁状态下调用）。"""
        self._status = status
        if self._on_status:
            try:
                self._on_status(status, self.status)
            except Exception:
                pass

    # ─── 状态查询 ────────────────────────────────────────────────

    @property
    def status(self) -> dict:
        with self._lock:
            goal   = self._goal
            status = self._status
            path   = self._path_mm
            idx    = self._path_idx

        amcl_x, amcl_y, amcl_t, conf = self._amcl.get_pose() if self._amcl.is_running else (0, 0, 0, 0)

        return {
            "status":          status.value,
            "goal":            {"x_mm": goal.x_mm, "y_mm": goal.y_mm, "label": goal.label} if goal else None,
            "pose":            {"x_mm": round(amcl_x, 1), "y_mm": round(amcl_y, 1), "theta_deg": round(amcl_t, 2)},
            "confidence":      round(conf, 3),
            "path_remaining":  max(0, len(path) - idx),
            "dist_to_goal_mm": round(math.hypot(goal.x_mm - amcl_x, goal.y_mm - amcl_y), 1) if goal else None,
            "curr_v_mm_s":     round(self._curr_v, 1),
            "curr_w_deg_s":    round(self._curr_w, 1),
        }
