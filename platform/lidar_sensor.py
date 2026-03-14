"""
LidarSensor — 应用层（激光雷达输入桥接）
==========================================
职责：
  - 实例化 Lidar 硬件层，注入扫描回调
  - 将每圈扫描同时路由至两个下游：
      1. WebSocket 广播（sense.lidar.scan）— 原始点云，供前端可视化
      2. SlamEngine.process_scan()         — 驱动 SLAM 建图
  - 按配置频率广播 SLAM 派生事件：
      sense.slam.pose       — 当前机器人位姿（轻量，1Hz）
      sense.slam.map_update — 当前地图 PNG（重量，默认 5s 一次）

不包含任何 SLAM 算法逻辑，仅做数据路由。
硬件层详见 devices/lidar.py，算法层详见 slam/slam_engine.py。

事件输出（发往 WebSocket → Spine）：
  sense.lidar.scan       — 每圈点云（频率受 LIDAR_BROADCAST_EVERY 控制）
  sense.slam.pose        — 机器人位姿 (x_mm, y_mm, theta_deg)（建图时才发）
  sense.slam.map_update  — 地图 PNG base64（建图时才发，频率较低）
"""

import asyncio
import logging
import os

from devices import Lidar, LidarConfig, LidarScan
from slam import SlamEngine

logger = logging.getLogger(__name__)


class LidarSensor:
    """Lidar + SlamEngine → WebSocket 的桥接层（应用层）。"""

    def __init__(self, ws_manager: "ConnectionManager", slam_engine: SlamEngine):
        self._ws = ws_manager
        self._slam = slam_engine
        self._loop: asyncio.AbstractEventLoop | None = None

        # LiDAR 硬件实例（配置来自环境变量）
        self._device = Lidar(
            config=LidarConfig(
                port=os.environ.get("LIDAR_PORT", "/dev/ttyUSB0"),
                broadcast_every_n_scans=1,   # 每圈都触发回调（由应用层自己控制广播频率）
                # LIDAR_MOUNT_ANGLE：雷达安装偏移角（度）
                #   0   = 线缆接口朝前（默认）
                #   180 = 线缆接口朝后（装反时设置）
                #   90  = 线缆接口朝右
                mount_angle_deg=float(os.environ.get("LIDAR_MOUNT_ANGLE", "0")),
            ),
            on_scan=self._on_scan,
        )

        # 广播节流计数器
        self._scan_count = 0
        self._lidar_broadcast_every = int(os.environ.get("LIDAR_BROADCAST_EVERY", "1"))

    # ─── 公共接口 ────────────────────────────────────────────────

    @property
    def device(self) -> Lidar:
        """暴露底层 Lidar 设备供 REST 接口使用。"""
        return self._device

    def start(self, loop: asyncio.AbstractEventLoop | None = None) -> None:
        """启动串口读取线程（非阻塞）。在 asyncio 线程中通过 to_thread 调用。

        loop 必须从调用方的 asyncio 上下文中传入，不能在 ThreadPoolExecutor 内部
        调用 asyncio.get_event_loop()（Python 3.10+ 在子线程中无当前事件循环）。
        """
        self._loop = loop
        self._device.start()
        if self._device.is_simulation:
            logger.warning("⚠️  激光雷达未连接，以模拟模式运行")
        else:
            logger.info(f"🔵 激光雷达已启动：{self._device._config.port}")

    def stop(self) -> None:
        """停止串口读取线程。"""
        self._device.stop()

    # ─── 内部回调：Lidar 串口线程 → 路由 ─────────────────────────

    def _on_scan(self, scan: LidarScan) -> None:
        """
        每圈扫描完成时由串口线程同步调用。
        注意：此函数在非 asyncio 线程中执行，
        需用 run_coroutine_threadsafe 提交 coroutine。
        """
        self._scan_count += 1
        loop = self._loop
        if loop is None or not loop.is_running():
            return

        # ── 1. 原始扫描广播（受 LIDAR_BROADCAST_EVERY 节流）────────
        if self._scan_count % self._lidar_broadcast_every == 0:
            asyncio.run_coroutine_threadsafe(
                self._ws.broadcast({
                    "type": "sense.lidar.scan",
                    "payload": scan.to_dict(),
                }),
                loop,
            )

        # ── 2. 喂给 SLAM（建图中才有效）────────────────────────────
        if self._slam.is_mapping:
            self._slam.process_scan(scan)
            pose = self._slam.get_pose()

            # ── 2a. 位姿广播（1Hz）──────────────────────────────────
            if self._scan_count % self._slam._cfg.pose_broadcast_every == 0:
                asyncio.run_coroutine_threadsafe(
                    self._ws.broadcast({
                        "type": "sense.slam.pose",
                        "payload": {
                            "x_mm":      round(pose[0], 1),
                            "y_mm":      round(pose[1], 1),
                            "theta_deg": round(pose[2], 2),
                            "scan_count": self._slam.scan_count,
                        },
                    }),
                    loop,
                )

            # ── 2b. 地图 PNG 广播（低频，默认 ~5s）──────────────────
            if self._scan_count % self._slam._cfg.map_broadcast_every == 0:
                asyncio.run_coroutine_threadsafe(
                    self._broadcast_map(),
                    loop,
                )

    async def _broadcast_map(self) -> None:
        """在 asyncio 线程中生成地图 PNG 并广播（PNG 编码是 CPU 密集，offload 到线程池）。"""
        png_b64 = await asyncio.to_thread(self._slam.get_map_png_b64)
        if png_b64 is None:
            return
        pose = self._slam.get_pose()
        rx, ry = self._slam.pose_to_pixel(pose[0], pose[1])
        await self._ws.broadcast({
            "type": "sense.slam.map_update",
            "payload": {
                "image_b64":        png_b64,
                "width":            self._slam._cfg.map_size_pixels,
                "height":           self._slam._cfg.map_size_pixels,
                "mm_per_pixel":     round(self._slam._cfg.mm_per_pixel, 1),
                "robot_pixel":      {"x": rx, "y": ry},
                "pose":             {
                    "x_mm":      round(pose[0], 1),
                    "y_mm":      round(pose[1], 1),
                    "theta_deg": round(pose[2], 2),
                },
                "scan_count":       self._slam.scan_count,
            },
        })
        logger.debug(f"[LidarSensor] 地图已广播，scan_count={self._slam.scan_count}")
