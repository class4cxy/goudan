"""
摄像头拍照模块

支持 USB 摄像头（source=0 对应 /dev/video0）和 RTSP 网络摄像头（source 为 URL 字符串）。
采用 OpenCV 采集图像，输出 JPEG 格式。

依赖：
  pip install opencv-python-headless

基本用法：
  cam = Camera()
  jpeg = cam.capture()           # → bytes | None
  b64  = cam.capture_base64()    # → str   | None
  path = cam.capture_to_file()   # → str   | None（保存到文件，返回绝对路径）
  cam.cleanup()

注意事项：
  - capture() 是阻塞调用，在 asyncio 中请用 asyncio.to_thread(cam.capture) 包裹。
  - 首次调用 capture() 时自动打开摄像头，之后保持常驻打开以避免每次打开的延迟。
  - 在树莓派上优先使用 opencv-python-headless，避免安装 GUI 依赖。
  - Raspberry Pi OS Bookworm 上 OpenCV 4.x 的 V4L2 后端用整数索引枚举会跳过 USB UVC
    摄像头，需将整数索引转为 /dev/videoN 路径字符串并指定 CAP_V4L2 后端。
  - 若摄像头被其他进程（如 motion.service）占用，open() 会失败并打印警告。
"""

from __future__ import annotations

import base64
import logging
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path

# 抑制 OpenCV 内部后端（obsensor / GStreamer）的噪音日志
os.environ.setdefault("OPENCV_LOG_LEVEL", "ERROR")

logger = logging.getLogger(__name__)

try:
    import cv2  # type: ignore
    _CV2_AVAILABLE = True
except ImportError:
    _CV2_AVAILABLE = False
    logger.warning(
        "cv2 (opencv-python-headless) 未安装，Camera.capture() 将始终返回 None。\n"
        "  → 解决方式：pip install opencv-python-headless"
    )


# ── 配置 ──────────────────────────────────────────────────────────

@dataclass
class CaptureConfig:
    """摄像头采集参数配置。"""
    source: str = "/dev/video0"
    """摄像头来源：
       - str：设备路径（如 "/dev/video0"）或 RTSP / HTTP 流地址
       直接用路径字符串可绕过 OpenCV 4.x 在 Raspberry Pi OS Bookworm 上
       整数索引枚举跳过 USB UVC 摄像头的 bug。
    """
    width: int = 1280
    height: int = 960
    jpeg_quality: int = 85      # JPEG 编码质量，范围 1–100
    rotate: int = 180           # 旋转角度：0（不旋转）/ 90 / 180 / 270
    snapshot_dir: str = "/tmp/roborock_snapshots"   # 快照文件保存目录


DEFAULT_CAPTURE_CONFIG = CaptureConfig()


# ── 摄像头控制器 ──────────────────────────────────────────────────

class Camera:
    """USB / RTSP 摄像头拍照控制器。

    线程安全：capture() 是同步阻塞方法；在 asyncio 上下文中请用::

        data = await asyncio.to_thread(cam.capture)
    """

    def __init__(self, config: CaptureConfig = DEFAULT_CAPTURE_CONFIG) -> None:
        self._config = config
        self._cap: "cv2.VideoCapture | None" = None  # type: ignore
        self._snapshot_dir = Path(config.snapshot_dir)
        self._is_open = False

    # ── 打开 / 关闭 ───────────────────────────────────────────────

    def _apply_rotate(self, frame: "cv2.Mat") -> "cv2.Mat":  # type: ignore
        _ROTATE_MAP = {
            90:  cv2.ROTATE_90_CLOCKWISE,
            180: cv2.ROTATE_180,
            270: cv2.ROTATE_90_COUNTERCLOCKWISE,
        }
        code = _ROTATE_MAP.get(self._config.rotate)
        return cv2.rotate(frame, code) if code is not None else frame

    @staticmethod
    def _open_cap(source: int | str) -> "cv2.VideoCapture | None":
        """
        尝试打开摄像头，Linux 上优先 CAP_V4L2，失败再回退 CAP_ANY。
        """
        if sys.platform.startswith("linux"):
            cap = cv2.VideoCapture(source, cv2.CAP_V4L2)
            if cap.isOpened():
                return cap
            cap.release()
        cap = cv2.VideoCapture(source)
        return cap if cap.isOpened() else None

    def open(self) -> bool:
        """打开摄像头设备并配置分辨率，返回是否成功。"""
        if not _CV2_AVAILABLE:
            return False
        if self._is_open:
            return True

        cap = self._open_cap(self._config.source)
        if cap is None:
            logger.warning(
                "摄像头打开失败：source=%s。"
                "若设备被其他进程占用（如 motion.service），请先停止该服务。",
                self._config.source,
            )
            return False

        cap.set(cv2.CAP_PROP_FRAME_WIDTH,  self._config.width)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self._config.height)
        # 缓冲区只保留 1 帧，确保 capture() 取到的是最新帧
        cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)

        self._cap = cap
        self._is_open = True
        self._snapshot_dir.mkdir(parents=True, exist_ok=True)
        logger.info(
            "摄像头已打开：source=%s  分辨率=%dx%d",
            self._config.source, self._config.width, self._config.height,
        )
        return True

    def cleanup(self) -> None:
        """释放摄像头资源（程序退出前调用）。"""
        if self._cap is not None:
            self._cap.release()
            self._cap = None
        self._is_open = False
        logger.info("摄像头资源已释放")

    # ── 拍照 ──────────────────────────────────────────────────────

    def capture(self) -> bytes | None:
        """拍一张照片，返回 JPEG bytes。失败时返回 None。

        首次调用会自动 open()；若读帧失败会尝试重新打开摄像头。
        """
        if not self._is_open and not self.open():
            return None

        assert self._cap is not None

        # 丢弃缓存旧帧：grab() 不解码，overhead 极小
        self._cap.grab()
        ret, frame = self._cap.read()

        if not ret or frame is None:
            logger.warning("摄像头读帧失败，尝试重新打开")
            self.cleanup()
            if not self.open():
                return None
            ret, frame = self._cap.read()  # type: ignore
            if not ret:
                return None

        frame = self._apply_rotate(frame)

        params = [cv2.IMWRITE_JPEG_QUALITY, self._config.jpeg_quality]
        ok, buf = cv2.imencode(".jpg", frame, params)
        if not ok:
            logger.error("JPEG 编码失败")
            return None
        return buf.tobytes()

    def capture_base64(self) -> str | None:
        """拍照，返回 base64 编码的 JPEG 字符串（可直接嵌入 JSON）。"""
        data = self.capture()
        return base64.b64encode(data).decode() if data else None

    def capture_to_file(self, filename: str | None = None) -> str | None:
        """拍照并保存到 snapshot_dir，返回保存文件的绝对路径。

        filename=None 时自动以毫秒时间戳命名（snapshot_<ms>.jpg）。
        """
        data = self.capture()
        if data is None:
            return None

        name = filename or f"snapshot_{int(time.time() * 1000)}.jpg"
        path = self._snapshot_dir / name
        try:
            path.write_bytes(data)
            logger.debug("快照已保存：%s", path)
            return str(path)
        except OSError as e:
            logger.error("快照保存失败：%s", e)
            return None

    # ── 状态 ──────────────────────────────────────────────────────

    @property
    def is_available(self) -> bool:
        """返回摄像头是否可用（必要时尝试打开）。"""
        if self._is_open:
            return True
        return self.open()

    @property
    def status(self) -> dict:
        """返回当前摄像头状态（供 REST /camera/capture/status 使用）。"""
        return {
            "source":        self._config.source,
            "width":         self._config.width,
            "height":        self._config.height,
            "jpeg_quality":  self._config.jpeg_quality,
            "snapshot_dir":  str(self._snapshot_dir),
            "is_open":       self._is_open,
            "cv2_available": _CV2_AVAILABLE,
        }
