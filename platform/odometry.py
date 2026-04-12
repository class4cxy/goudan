"""
Odometry — 差速轮式里程计
==========================
职责：
  1. 以 50Hz 定期读取编码器脉冲增量（read_and_reset）
  2. 融合 IMU 陀螺仪 Z 轴，通过互补滤波补偿车轮打滑误差
  3. 基于差速运动学计算每步增量（dx_mm, dy_mm, dtheta_deg）
  4. 累积绝对位姿（x_mm, y_mm, theta_deg），坐标原点为启动位置
  5. 提供 get_velocity_for_slam()：读取并清零增量，breezyslam update() 所需的 velocities 三元组
  6. 提供 peek_velocity()：只读不清零的增量快照（AMCL 等第二消费者调用，避免双消费竞态）

关键参数（需实测后填入 .env）：
  ODOM_WHEEL_RADIUS_MM  车轮半径（mm）默认 33.0，需卡尺实测
  ODOM_WHEEL_BASE_MM    两驱动轮中心距（mm）默认 160.0，需实测
  ODOM_IMU_WEIGHT       IMU 融合权重 0.0=纯编码器 1.0=纯IMU 默认 0.3

坐标系约定（右手坐标系，俯视）：
  X 轴 → 机器车正前方
  Y 轴 → 机器车左侧
  theta → 相对 X 轴正方向的偏航角（逆时针为正，单位 °）
"""

import math
import threading
import time
import logging
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


@dataclass
class OdometryConfig:
    # 轮子半径：卡尺实测直径 68mm → 半径 34mm
    wheel_radius_mm: float = 34.0
    # 两驱动轮（后轮）中心间距：卡尺实测后填入
    wheel_base_mm:   float = 160.0
    # IMU 融合权重：0.0=纯编码器，1.0=纯IMU；IMU 不可用时自动降级到纯编码器
    imu_weight:      float = 0.3
    # 里程计更新频率（Hz）
    update_hz:       int   = 50


@dataclass
class OdometryPose:
    x_mm:      float = 0.0
    y_mm:      float = 0.0
    theta_deg: float = 0.0


class Odometry:
    """
    差速里程计。后台线程以 update_hz 速率运行，外部线程安全读取。

    使用方式：
        odom = Odometry(encoder, imu)
        odom.start()
        ...
        vel = odom.get_velocity_for_slam()   # 每次 SLAM 帧前调用
        slam.process_scan(scan, vel)
        ...
        odom.stop()
    """

    def __init__(self, encoder, imu=None, config: OdometryConfig | None = None) -> None:
        self._encoder = encoder
        self._imu     = imu
        self._cfg     = config or OdometryConfig()

        self._lock    = threading.Lock()
        self._pose    = OdometryPose()

        # SLAM / AMCL 消费的增量（读取后清零）
        self._slam_dxy_mm:     float = 0.0
        self._slam_dtheta_deg: float = 0.0
        self._slam_dt_s:       float = 0.0

        # 独立行驶路程累计器（供闭环运动控制专用，读取后清零）
        self._travel_mm: float = 0.0

        self._running = False
        self._thread: threading.Thread | None = None
        self._last_t  = 0.0

    # ─── 生命周期 ─────────────────────────────────────────────────

    def start(self) -> None:
        self._last_t  = time.monotonic()
        self._running = True
        self._thread  = threading.Thread(
            target=self._update_loop,
            daemon=True,
            name="odometry",
        )
        self._thread.start()
        r  = self._cfg.wheel_radius_mm
        wb = self._cfg.wheel_base_mm
        logger.info(
            f"[Odometry] 已启动 | 轮径={r}mm 轮距={wb}mm "
            f"IMU权重={self._cfg.imu_weight} "
            f"encoder={'真实' if not self._encoder.is_simulation else '模拟'} "
            f"imu={'真实' if self._imu and not self._imu.is_simulation else '模拟/无'}"
        )

    def stop(self) -> None:
        self._running = False
        if self._thread:
            self._thread.join(timeout=2.0)

    # ─── 内部：更新循环 ───────────────────────────────────────────

    def _update_loop(self) -> None:
        interval = 1.0 / self._cfg.update_hz
        while self._running:
            t0 = time.monotonic()
            self._step()
            elapsed = time.monotonic() - t0
            time.sleep(max(0.0, interval - elapsed))

    def _step(self) -> None:
        now = time.monotonic()
        dt  = now - self._last_t
        self._last_t = now
        if dt <= 0:
            return

        # ── 1. 编码器脉冲 → 轮子线位移 ──────────────────────────────
        left_ticks, right_ticks = self._encoder.read_and_reset()
        tpr  = self._encoder.ticks_per_rev
        circ = 2.0 * math.pi * self._cfg.wheel_radius_mm
        left_dist  = (left_ticks  / tpr) * circ  # mm
        right_dist = (right_ticks / tpr) * circ  # mm

        # ── 2. 差速运动学：线位移 + 转向角 ──────────────────────────
        dxy_mm         = (left_dist + right_dist) * 0.5
        dtheta_enc_deg = math.degrees(
            (right_dist - left_dist) / self._cfg.wheel_base_mm
        )

        # ── 3. IMU 互补滤波（补偿打滑，0.3 权重给 IMU）──────────────
        dtheta_deg = dtheta_enc_deg
        if self._imu and not self._imu.is_simulation:
            reading = self._imu.get_latest()
            if reading:
                dtheta_imu_deg = reading.gyro_z * dt
                w = self._cfg.imu_weight
                dtheta_deg = (1.0 - w) * dtheta_enc_deg + w * dtheta_imu_deg

        # ── 4. 绝对位姿积分 ──────────────────────────────────────────
        with self._lock:
            theta_rad = math.radians(self._pose.theta_deg)
            self._pose.x_mm      += dxy_mm * math.cos(theta_rad)
            self._pose.y_mm      += dxy_mm * math.sin(theta_rad)
            theta_new             = self._pose.theta_deg + dtheta_deg
            self._pose.theta_deg  = (theta_new + 180.0) % 360.0 - 180.0

            # 累积 SLAM/AMCL 用的增量
            # 线位移保留幅度；角度必须保留符号，否则左/右转会被折叠成同方向，
            # 扫描匹配的初始猜测会失真，地图容易出现重影和放射状毛刺。
            self._slam_dxy_mm     += abs(dxy_mm)
            self._slam_dtheta_deg += dtheta_deg
            self._slam_dt_s       += dt

            # 累积行驶路程（闭环运动控制专用，不受 SLAM 消费影响）
            self._travel_mm += abs(dxy_mm)

    # ─── 公共接口 ─────────────────────────────────────────────────

    def get_pose(self) -> OdometryPose:
        """返回当前累积位姿（线程安全）。"""
        with self._lock:
            return OdometryPose(
                self._pose.x_mm,
                self._pose.y_mm,
                self._pose.theta_deg,
            )

    def get_velocity_for_slam(self) -> tuple[float, float, float]:
        """
        读取并清零自上次调用以来的增量，返回 breezyslam velocities 格式：
            (dxy_mm, dtheta_degrees, dt_seconds)

        每次 slam.process_scan() 之前调用（唯一清零消费者）。
        AMCL 等第二消费者请使用 peek_velocity()。
        """
        with self._lock:
            v = (self._slam_dxy_mm, self._slam_dtheta_deg, self._slam_dt_s)
            self._slam_dxy_mm     = 0.0
            self._slam_dtheta_deg = 0.0
            self._slam_dt_s       = 0.0
        return v

    def get_and_reset_travel(self) -> float:
        """
        读取并清零行驶路程累计器（mm）。

        仅供 /motor/drive 闭环距离控制使用，独立于 SLAM 消费者。
        返回自上次调用以来车轮走过的总路程（取绝对值，不含旋转）。
        """
        with self._lock:
            v = self._travel_mm
            self._travel_mm = 0.0
        return v

    def peek_velocity(self) -> tuple[float, float, float]:
        """
        只读不清零：返回当前增量快照，不影响 get_velocity_for_slam() 的后续清零。

        用于 AMCL 等需要与 SLAM 消费同一帧增量的场景，避免双消费竞态。
        调用时序：
            vel = odometry.peek_velocity()   # 先快照
            original_on_scan(scan)           # SLAM 内部会 get+clear
            amcl.update(scan, vel)           # AMCL 使用快照
        """
        with self._lock:
            return (self._slam_dxy_mm, self._slam_dtheta_deg, self._slam_dt_s)

    def reset_pose(
        self,
        x_mm: float = 0.0,
        y_mm: float = 0.0,
        theta_deg: float = 0.0,
    ) -> None:
        """重置绝对位姿（AMCL 定位收敛后同步里程计）。"""
        with self._lock:
            self._pose = OdometryPose(x_mm, y_mm, theta_deg)
        logger.info(f"[Odometry] 位姿已重置：({x_mm:.1f}, {y_mm:.1f}, {theta_deg:.1f}°)")

    @property
    def status(self) -> dict:
        pose = self.get_pose()
        return {
            "x_mm":              round(pose.x_mm, 1),
            "y_mm":              round(pose.y_mm, 1),
            "theta_deg":         round(pose.theta_deg, 2),
            "encoder_sim":       self._encoder.is_simulation,
            "imu_sim":           (self._imu is None or self._imu.is_simulation),
            "wheel_radius_mm":   self._cfg.wheel_radius_mm,
            "wheel_base_mm":     self._cfg.wheel_base_mm,
            "imu_weight":        self._cfg.imu_weight,
        }
