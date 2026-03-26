"""
DistanceField — 占用栅格地图距离变换预计算
==========================================
职责：
  1. 读取 breezyslam 保存的 PGM 地图文件
  2. 将地图二值化（障碍 / 可通行）
  3. 计算距离变换（DT）：每个格子存储到最近障碍物的距离（像素单位）
  4. 缓存为 float32 numpy 数组，供 AMCL 似然场模型快速查询

距离变换原理：
  scipy.ndimage.distance_transform_edt 计算欧氏距离变换，时间复杂度 O(N)，
  1000×1000 地图约 50ms（RPi5 上），建图完成后一次性计算，不需要实时更新。

breezyslam 地图字节语义：
  0        = 未探索（视为障碍，不允许通行）
  1–127    = 障碍物（障碍）
  128–255  = 可通行（自由空间）
"""

import logging
from pathlib import Path

import numpy as np

logger = logging.getLogger(__name__)


class DistanceField:
    """
    PGM 地图 → 距离变换缓存。

    用法：
        df = DistanceField()
        df.load_from_pgm("platform/maps/mymap.pgm", map_size_pixels=1000)
        d = df.lookup(pixel_x, pixel_y)   # → float，到最近障碍的像素距离
    """

    def __init__(self) -> None:
        self._field: np.ndarray | None = None   # shape (H, W), float32
        self._size: int = 0
        self._mm_per_pixel: float = 20.0

    # ─── 加载 ─────────────────────────────────────────────────────

    def load_from_pgm(
        self,
        pgm_path: str | Path,
        map_size_pixels: int,
        mm_per_pixel: float = 20.0,
    ) -> bool:
        """
        从 PGM 文件加载地图并计算距离变换。

        Args:
            pgm_path:        PGM 文件路径
            map_size_pixels: 地图边长（像素），与 SlamConfig 一致
            mm_per_pixel:    每像素代表的毫米数

        Returns:
            True = 加载成功
        """
        try:
            from scipy.ndimage import distance_transform_edt
        except ImportError:
            logger.error("[DistanceField] 需要 scipy：pip install scipy")
            return False

        pgm_path = Path(pgm_path)
        if not pgm_path.exists():
            logger.error(f"[DistanceField] 文件不存在：{pgm_path}")
            return False

        try:
            raw = self._read_pgm(pgm_path)
        except Exception as e:
            logger.error(f"[DistanceField] 读取 PGM 失败：{e}")
            return False

        if len(raw) != map_size_pixels * map_size_pixels:
            logger.error(
                f"[DistanceField] PGM 大小不匹配：{len(raw)} ≠ {map_size_pixels ** 2}"
            )
            return False

        arr = np.frombuffer(raw, dtype=np.uint8).reshape((map_size_pixels, map_size_pixels))

        # 二值化：128–255 = 自由空间（True）；其余 = 障碍（False）
        free_mask = arr > 127

        # 距离变换：对"非障碍"格子求到最近障碍的欧氏距离（像素单位）
        # free_mask 中 True=自由，DT 计算的是到最近 False（障碍）的距离
        self._field = distance_transform_edt(free_mask).astype(np.float32)
        self._size  = map_size_pixels
        self._mm_per_pixel = mm_per_pixel

        obstacle_count = int(np.sum(~free_mask))
        free_count     = int(np.sum(free_mask))
        logger.info(
            f"[DistanceField] 距离变换已计算：{map_size_pixels}×{map_size_pixels} | "
            f"障碍={obstacle_count} 自由={free_count} px"
        )
        return True

    def load_from_bytes(
        self,
        map_bytes: bytes,
        map_size_pixels: int,
        mm_per_pixel: float = 20.0,
    ) -> bool:
        """
        直接从内存 bytes（breezyslam map_bytes）加载，无需 PGM 文件。
        """
        try:
            from scipy.ndimage import distance_transform_edt
        except ImportError:
            logger.error("[DistanceField] 需要 scipy：pip install scipy")
            return False

        arr = np.frombuffer(map_bytes, dtype=np.uint8).reshape(
            (map_size_pixels, map_size_pixels)
        )
        free_mask = arr > 127
        self._field       = distance_transform_edt(free_mask).astype(np.float32)
        self._size        = map_size_pixels
        self._mm_per_pixel = mm_per_pixel
        return True

    # ─── 查询 ─────────────────────────────────────────────────────

    def lookup(self, px: int, py: int) -> float:
        """
        查询像素 (px, py) 到最近障碍的距离（像素单位）。

        超出地图范围的点返回 0.0（视为障碍区域）。
        """
        if self._field is None:
            return 0.0
        if 0 <= px < self._size and 0 <= py < self._size:
            return float(self._field[py, px])
        return 0.0

    def lookup_mm(self, px: int, py: int) -> float:
        """查询到最近障碍的距离（毫米单位）。"""
        return self.lookup(px, py) * self._mm_per_pixel

    def is_free(self, px: int, py: int) -> bool:
        """像素 (px, py) 是否为可通行区域（距最近障碍 > 0 像素）。"""
        return self.lookup(px, py) > 0.0

    @property
    def is_loaded(self) -> bool:
        return self._field is not None

    @property
    def size(self) -> int:
        return self._size

    @property
    def mm_per_pixel(self) -> float:
        return self._mm_per_pixel

    # ─── 内部工具 ────────────────────────────────────────────────

    @staticmethod
    def _read_pgm(path: Path) -> bytes:
        """读取 P5（二进制灰度）PGM 文件，跳过头部，返回像素字节。"""
        with open(path, "rb") as f:
            # 跳过 magic、注释行、宽高、maxval 等头行
            while True:
                line = f.readline().decode("ascii", errors="ignore").strip()
                if not line or line.startswith("#"):
                    continue
                if line == "P5":
                    continue
                # 宽高行
                parts = line.split()
                if len(parts) == 2:
                    # 下一行是 maxval
                    f.readline()
                    break
            return f.read()
