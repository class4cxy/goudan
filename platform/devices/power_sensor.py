"""
PowerSensor — INA219 电流/电压传感器硬件抽象层
================================================
职责：
  1. 通过 I2C 持续读取 INA219 的电压、电流、功率数据
  2. 低电量检测：电压低于阈值时触发 on_low_battery 回调
  3. 通过 on_reading 回调向上层推送实时读数
  4. 非树莓派环境（或 INA219 未连接）自动降级为模拟模式

不含任何 WebSocket / FastAPI 逻辑，纯硬件操作。

接线：
  INA219 VCC → 树莓派 3.3V（Pin 1）
  INA219 GND → 树莓派 GND（Pin 6）
  INA219 SDA → 树莓派 GPIO2/SDA1（Pin 3）
  INA219 SCL → 树莓派 GPIO3/SCL1（Pin 5）
  绿色接线柱 Vin+ / Vin- 串联在供电回路中

依赖：pi-ina219（pip install pi-ina219）
I2C 须先通过 raspi-config 启用，默认地址 0x40
"""

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Callable

logger = logging.getLogger(__name__)


# ── 数据模型 ───────────────────────────────────────────────────────

@dataclass
class PowerReading:
    """单次电源采样结果。"""
    timestamp_ms: int    # 采样时间（Unix ms）
    voltage_v: float     # 总线电压（V）
    current_ma: float    # 电流（mA，正=放电，负=充电）
    power_mw: float      # 功率（mW）
    shunt_mv: float      # 分流电阻两端电压（mV，调试用）
    battery_pct: float   # 电量百分比（0–100，由电压线性估算）
    is_charging: bool    # 是否正在充电（电流 < -50mA 视为充电）

    def to_dict(self) -> dict:
        return {
            "timestamp_ms": self.timestamp_ms,
            "voltage_v":    round(self.voltage_v, 3),
            "current_ma":   round(self.current_ma, 1),
            "power_mw":     round(self.power_mw, 1),
            "shunt_mv":     round(self.shunt_mv, 2),
            "battery_pct":  round(self.battery_pct, 1),
            "is_charging":  self.is_charging,
        }


# ── 配置 ──────────────────────────────────────────────────────────

@dataclass
class PowerSensorConfig:
    shunt_ohms: float        = 0.1    # 板载分流电阻阻值（R100 = 0.1Ω）
    i2c_address: int         = 0x40   # INA219 I2C 地址（默认 0x40）
    max_expected_amps: float = 3.2    # 预期最大电流（A），INA219 + 0.1Ω 分流电阻硬件上限
    poll_interval_s: float   = 5.0    # 采样间隔（秒）
    battery_full_v: float    = 8.4    # 电池满电电压（V）：2S LiPo = 8.4V（4.2V×2）
    battery_empty_v: float   = 6.6    # 电池安全下限电压（V）：2S LiPo = 6.6V（3.3V×2）
    low_battery_pct: float   = 20.0   # 低电量报警阈值（%），低于此值触发告警

DEFAULT_POWER_CONFIG = PowerSensorConfig()


# ── PowerSensor 主类 ──────────────────────────────────────────────

class PowerSensor:
    """
    INA219 电流/电压传感器控制器（纯硬件层）。

    通过回调向上层推送读数，不依赖 WebSocket / 任何网络组件。

    Args:
        config:          PowerSensorConfig 配置
        on_reading:      每次采样后调用，参数为 PowerReading（在轮询线程中同步调用）
        on_low_battery:  电量低于 low_battery_pct% 时调用（同一线程，调用方需自行节流）
    """

    def __init__(
        self,
        config: PowerSensorConfig | None = None,
        on_reading: Callable[[PowerReading], None] | None = None,
        on_low_battery: Callable[[PowerReading], None] | None = None,
    ):
        self._cfg = config or DEFAULT_POWER_CONFIG
        self._on_reading = on_reading
        self._on_low_battery = on_low_battery

        self._ina = None          # INA219 实例（延迟初始化）
        self._is_simulation = False
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._latest: PowerReading | None = None

        # 低电量节流：避免回调过于频繁（充电中不触发）
        self._low_battery_last_alert: float = 0.0
        self._low_battery_interval_s: float = 60.0

    # ─── 公共接口 ─────────────────────────────────────────────────

    def start(self) -> None:
        """初始化 INA219 并启动后台轮询线程（非阻塞）。"""
        if not self._init_ina219():
            self._is_simulation = True
            logger.warning("[PowerSensor] INA219 初始化失败，进入模拟模式")
            return

        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._poll_loop,
            daemon=True,
            name="power_sensor_poll",
        )
        self._thread.start()
        logger.info(
            f"[PowerSensor] 已启动：地址=0x{self._cfg.i2c_address:02X}，"
            f"间隔={self._cfg.poll_interval_s}s，"
            f"低电量阈值={self._cfg.low_battery_pct}%"
        )

    def stop(self) -> None:
        """停止轮询线程。"""
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=5.0)
            self._thread = None
        logger.info("[PowerSensor] 已停止")

    @property
    def is_simulation(self) -> bool:
        return self._is_simulation

    @property
    def latest_reading(self) -> PowerReading | None:
        with self._lock:
            return self._latest

    @property
    def status(self) -> dict:
        reading = self.latest_reading
        return {
            "is_simulation":    self._is_simulation,
            "is_running":       self._thread is not None and self._thread.is_alive(),
            "i2c_address":      f"0x{self._cfg.i2c_address:02X}",
            "poll_interval_s":  self._cfg.poll_interval_s,
            "battery_full_v":   self._cfg.battery_full_v,
            "battery_empty_v":  self._cfg.battery_empty_v,
            "low_battery_pct":  self._cfg.low_battery_pct,
            "latest":           reading.to_dict() if reading else None,
            "is_low_battery":   (
                not reading.is_charging and reading.battery_pct < self._cfg.low_battery_pct
                if reading else False
            ),
        }

    # ─── 辅助计算 ─────────────────────────────────────────────────

    def _calc_battery_pct(self, voltage_v: float) -> float:
        """
        由总线电压线性估算剩余电量百分比（0–100）。
        在 battery_empty_v ~ battery_full_v 之间线性映射，两端钳位。
        """
        span = self._cfg.battery_full_v - self._cfg.battery_empty_v
        if span <= 0:
            return 100.0
        pct = (voltage_v - self._cfg.battery_empty_v) / span * 100.0
        return max(0.0, min(100.0, pct))

    # ─── INA219 初始化 ────────────────────────────────────────────

    def _init_ina219(self) -> bool:
        """
        尝试初始化 INA219。
        返回 True = 成功，False = 失败（库未安装或设备未连接）。
        """
        try:
            from ina219 import INA219
        except ImportError:
            logger.error("[PowerSensor] 缺少依赖：pi-ina219，请运行：pip install pi-ina219")
            return False

        try:
            self._ina = INA219(
                shunt_ohms=self._cfg.shunt_ohms,
                max_expected_amps=self._cfg.max_expected_amps,
                address=self._cfg.i2c_address,
                busnum=1,
            )
            self._ina.configure()
            logger.info(f"[PowerSensor] INA219 就绪（地址 0x{self._cfg.i2c_address:02X}）")
            return True
        except Exception as e:
            logger.warning(f"[PowerSensor] INA219 连接失败：{e}")
            return False

    # ─── 轮询循环 ────────────────────────────────────────────────

    def _poll_loop(self) -> None:
        """在后台线程中定期读取 INA219 数据。"""
        from ina219 import DeviceRangeError

        while not self._stop_event.is_set():
            try:
                voltage_v  = self._ina.voltage()
                current_ma = self._ina.current()
                reading = PowerReading(
                    timestamp_ms = int(time.time() * 1000),
                    voltage_v    = voltage_v,
                    current_ma   = current_ma,
                    power_mw     = self._ina.power(),
                    shunt_mv     = self._ina.shunt_voltage(),
                    battery_pct  = self._calc_battery_pct(voltage_v),
                    is_charging  = current_ma < -50.0,
                )
            except DeviceRangeError as e:
                logger.warning(f"[PowerSensor] 超量程：{e}（电流可能超过 {self._cfg.max_expected_amps}A）")
                self._stop_event.wait(self._cfg.poll_interval_s)
                continue
            except Exception as e:
                logger.error(f"[PowerSensor] 读取失败：{e}")
                self._stop_event.wait(self._cfg.poll_interval_s)
                continue

            with self._lock:
                self._latest = reading

            charge_tag = "充电中" if reading.is_charging else "放电中"
            logger.debug(
                f"[PowerSensor] {reading.voltage_v:.2f}V  "
                f"{reading.battery_pct:.0f}%  "
                f"{reading.current_ma:.0f}mA  {reading.power_mw:.0f}mW  {charge_tag}"
            )

            # 回调上层
            if self._on_reading:
                try:
                    self._on_reading(reading)
                except Exception as e:
                    logger.warning(f"[PowerSensor] on_reading 回调异常：{e}")

            # 低电量报警（充电中不触发；节流：最多每 60s 触发一次）
            if not reading.is_charging and reading.battery_pct < self._cfg.low_battery_pct:
                now = time.time()
                if now - self._low_battery_last_alert >= self._low_battery_interval_s:
                    self._low_battery_last_alert = now
                    logger.warning(
                        f"[PowerSensor] ⚠️  低电量！{reading.battery_pct:.0f}% < "
                        f"{self._cfg.low_battery_pct:.0f}%（{reading.voltage_v:.2f}V）"
                    )
                    if self._on_low_battery:
                        try:
                            self._on_low_battery(reading)
                        except Exception as e:
                            logger.warning(f"[PowerSensor] on_low_battery 回调异常：{e}")

            self._stop_event.wait(self._cfg.poll_interval_s)
