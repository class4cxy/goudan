"""
AStarPlanner — A* 全局路径规划
================================
在代价地图（Costmap）上搜索从起点到终点的最优路径。

特性：
  - 标准 A*，8 连通邻居（含对角线）
  - 代价地图感知：累计路径代价 = 移动代价 × 格子代价
  - 障碍（≥ INSCRIBED）不可穿越
  - 输出：像素坐标路径 + mm 坐标路径（经道格拉斯-普克算法抽稀）

时间复杂度：O(N log N)，1000×1000 地图典型耗时 < 200ms（RPi5）
"""

import heapq
import math
import logging
from dataclasses import dataclass

import numpy as np

logger = logging.getLogger(__name__)

LETHAL_OBSTACLE = 254
INSCRIBED       = 253


@dataclass
class PlanResult:
    success:      bool
    path_pixels:  list[tuple[int, int]]   # [(px, py), ...]
    path_mm:      list[tuple[float, float]]  # [(x_mm, y_mm), ...]
    cost:         float
    message:      str = ""


class AStarPlanner:
    """A* 全局路径规划器（工作在像素坐标系）。"""

    # 8 连通邻居及其移动代价（对角线 √2）
    _NEIGHBORS = [
        (-1,  0, 1.0), ( 1,  0, 1.0), ( 0, -1, 1.0), ( 0,  1, 1.0),
        (-1, -1, 1.414), (-1,  1, 1.414), ( 1, -1, 1.414), ( 1,  1, 1.414),
    ]

    def plan(
        self,
        grid: np.ndarray,
        start: tuple[int, int],
        goal:  tuple[int, int],
        mm_per_pixel: float = 20.0,
    ) -> PlanResult:
        """
        在代价栅格上搜索路径。

        Args:
            grid:         代价地图 uint8 数组，shape (H, W)
            start:        起点像素 (px, py)
            goal:         终点像素 (px, py)
            mm_per_pixel: 比例尺，用于将像素路径转为 mm 路径

        Returns:
            PlanResult
        """
        H, W = grid.shape
        sx, sy = start
        gx, gy = goal

        # 边界与障碍检查
        for label, (x, y) in [("起点", start), ("终点", goal)]:
            if not (0 <= x < W and 0 <= y < H):
                return PlanResult(False, [], [], 0.0, f"{label}超出地图范围：({x}, {y})")
            if grid[y, x] >= INSCRIBED:
                return PlanResult(False, [], [], 0.0, f"{label}位于障碍区域：({x}, {y})")

        if start == goal:
            return PlanResult(True, [start], [self._px_to_mm(sx, sy, grid.shape, mm_per_pixel)], 0.0)

        # ── A* ──────────────────────────────────────────────────
        h = lambda x, y: math.hypot(gx - x, gy - y)

        open_set: list[tuple[float, int, int]] = []
        heapq.heappush(open_set, (h(sx, sy), sx, sy))

        came_from: dict[tuple[int, int], tuple[int, int]] = {}
        g_score: dict[tuple[int, int], float] = {start: 0.0}

        while open_set:
            _, cx, cy = heapq.heappop(open_set)

            if (cx, cy) == goal:
                path = self._reconstruct_path(came_from, goal)
                path_mm = [
                    self._px_to_mm(px, py, grid.shape, mm_per_pixel)
                    for px, py in path
                ]
                path_mm = self._simplify_path(path_mm, tolerance_mm=2.0 * mm_per_pixel)
                return PlanResult(True, path, path_mm, g_score[goal])

            for dx, dy, move_cost in self._NEIGHBORS:
                nx, ny = cx + dx, cy + dy
                if not (0 <= nx < W and 0 <= ny < H):
                    continue
                cell_cost = int(grid[ny, nx])
                if cell_cost >= INSCRIBED:
                    continue

                # 格子代价：自由=1，膨胀区加权
                terrain = 1.0 + (cell_cost / 127.0) * 4.0

                tentative_g = g_score[(cx, cy)] + move_cost * terrain
                neighbor    = (nx, ny)

                if tentative_g < g_score.get(neighbor, float("inf")):
                    came_from[neighbor] = (cx, cy)
                    g_score[neighbor]   = tentative_g
                    f = tentative_g + h(nx, ny)
                    heapq.heappush(open_set, (f, nx, ny))

        return PlanResult(False, [], [], 0.0, "A* 未找到可行路径")

    # ─── 工具 ────────────────────────────────────────────────────

    @staticmethod
    def _reconstruct_path(
        came_from: dict[tuple[int, int], tuple[int, int]],
        goal: tuple[int, int],
    ) -> list[tuple[int, int]]:
        path = []
        cur = goal
        while cur in came_from:
            path.append(cur)
            cur = came_from[cur]
        path.append(cur)
        path.reverse()
        return path

    @staticmethod
    def _px_to_mm(
        px: int,
        py: int,
        shape: tuple[int, int],
        mm_per_pixel: float,
    ) -> tuple[float, float]:
        H, W = shape
        return (px - W / 2.0) * mm_per_pixel, (py - H / 2.0) * mm_per_pixel

    @staticmethod
    def _simplify_path(
        path: list[tuple[float, float]],
        tolerance_mm: float,
    ) -> list[tuple[float, float]]:
        """道格拉斯-普克算法抽稀路径，减少路径点数量。"""
        if len(path) < 3:
            return path

        def perp_dist(pt, line_start, line_end):
            lx, ly = line_end[0] - line_start[0], line_end[1] - line_start[1]
            ll = math.hypot(lx, ly)
            if ll < 1e-6:
                return math.hypot(pt[0] - line_start[0], pt[1] - line_start[1])
            return abs(lx * (line_start[1] - pt[1]) - ly * (line_start[0] - pt[0])) / ll

        def rdp(pts, tol):
            if len(pts) < 3:
                return pts
            dmax, idx = 0.0, 0
            for i in range(1, len(pts) - 1):
                d = perp_dist(pts[i], pts[0], pts[-1])
                if d > dmax:
                    dmax, idx = d, i
            if dmax > tol:
                left  = rdp(pts[:idx + 1], tol)
                right = rdp(pts[idx:],     tol)
                return left[:-1] + right
            return [pts[0], pts[-1]]

        return rdp(path, tolerance_mm)
