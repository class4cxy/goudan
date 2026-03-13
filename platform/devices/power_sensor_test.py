"""
INA219 电源传感器真机测试脚本
==============================
在树莓派上运行（INA219 通过 I2C 接线完成后）。

前提：
  1. raspi-config → Interface Options → I2C → Enable
  2. pip install pi-ina219
  3. 接线：VCC→3.3V，GND→GND，SDA→GPIO2，SCL→GPIO3
  4. 绿色接线柱串联在供电回路中

用法：
  python power_sensor_test.py            # 交互式菜单
  python power_sensor_test.py --test 2   # 直接运行指定测试
"""

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from power_sensor import PowerSensor, PowerSensorConfig

DIVIDER = "─" * 60


# ── 测试 1：I2C 扫描确认 INA219 存在 ─────────────────────────────

def test_i2c_scan():
    print(f"\n{DIVIDER}")
    print("  测试 1 — I2C 总线扫描")
    print(DIVIDER)
    try:
        import smbus2
        bus = smbus2.SMBus(1)
        found = []
        for addr in range(0x40, 0x50):  # INA219 地址范围 0x40–0x4F
            try:
                bus.read_byte(addr)
                found.append(f"0x{addr:02X}")
            except Exception:
                pass
        bus.close()
        if found:
            print(f"  ✅ 发现设备：{', '.join(found)}")
            print(f"  INA219 默认地址为 0x40，{'已找到' if '0x40' in found else '未在列表中，检查接线'}")
        else:
            print("  ❌ 未发现任何 I2C 设备（0x40–0x4F）")
            print("  检查：1) I2C 已启用？  2) SDA/SCL 接线正确？  3) VCC 已供电？")
    except ImportError:
        print("  smbus2 未安装，直接尝试初始化 INA219...")
        _try_direct_init()


def _try_direct_init():
    try:
        from ina219 import INA219
        ina = INA219(shunt_ohms=0.1, address=0x40)
        ina.configure()
        v = ina.voltage()
        print(f"  ✅ INA219 直接初始化成功，当前电压：{v:.3f}V")
    except ImportError:
        print("  ❌ pi-ina219 未安装，请运行：pip install pi-ina219")
    except Exception as e:
        print(f"  ❌ 初始化失败：{e}")


# ── 测试 2：单次读数 ──────────────────────────────────────────────

def test_single_read():
    print(f"\n{DIVIDER}")
    print("  测试 2 — 单次读数")
    print(DIVIDER)
    try:
        from ina219 import INA219, DeviceRangeError
        ina = INA219(shunt_ohms=0.1, max_expected_amps=2.0, address=0x40)
        ina.configure()

        print("  参数           数值          说明")
        print("  " + "─" * 50)
        v = ina.voltage()
        i = ina.current()
        p = ina.power()
        s = ina.shunt_voltage()
        print(f"  总线电压       {v:8.3f} V     供电电压")
        print(f"  电流           {i:8.1f} mA    正=放电，负=充电")
        print(f"  功率           {p:8.1f} mW    = 电压 × 电流")
        print(f"  分流电压       {s:8.2f} mV    用于计算电流")
        print()

        # 判断是否合理
        if v < 1.0:
            print("  ⚠️  电压极低（<1V），检查绿色接线柱是否串联在供电回路中")
        elif v > 25.0:
            print("  ⚠️  电压超出 INA219 量程（>26V），硬件可能损坏")
        else:
            print(f"  ✅ 数据正常")

    except ImportError:
        print("  ❌ pi-ina219 未安装：pip install pi-ina219")
    except Exception as e:
        print(f"  ❌ 读取失败：{e}")


# ── 测试 3：持续监测（实时刷新）─────────────────────────────────

def test_continuous(duration_s: int = 20):
    print(f"\n{DIVIDER}")
    print(f"  测试 3 — 持续监测 {duration_s}s（Ctrl+C 提前退出）")
    print(DIVIDER)
    readings = []

    def on_reading(r):
        readings.append(r)
        bar_len = int(r.current_ma / 50)  # 每 50mA 一格
        bar = "█" * min(bar_len, 40)
        flag = " ⚠️ LOW" if r.voltage_v < 6.8 else ""
        print(
            f"\r  {r.voltage_v:6.3f}V  {r.current_ma:7.1f}mA  "
            f"{r.power_mw:7.1f}mW  |{bar:<40}|{flag}",
            end="",
            flush=True,
        )

    sensor = PowerSensor(
        config=PowerSensorConfig(poll_interval_s=0.5),
        on_reading=on_reading,
    )
    sensor.start()

    if sensor.is_simulation:
        print("  ❌ INA219 不可用，无法持续监测")
        return

    print("  电压        电流          功率")
    try:
        time.sleep(duration_s)
    except KeyboardInterrupt:
        pass
    finally:
        sensor.stop()

    print()
    if readings:
        avg_v = sum(r.voltage_v for r in readings) / len(readings)
        avg_i = sum(r.current_ma for r in readings) / len(readings)
        max_i = max(r.current_ma for r in readings)
        print(f"\n  统计（{len(readings)} 次采样）：")
        print(f"  平均电压：{avg_v:.3f}V  平均电流：{avg_i:.1f}mA  峰值电流：{max_i:.1f}mA")


# ── 测试 4：低电量报警验证 ────────────────────────────────────────

def test_low_battery_alert(threshold_v: float = 99.0):
    """
    将阈值设为极高（99V）使其立刻触发，验证回调机制是否正常。
    """
    print(f"\n{DIVIDER}")
    print("  测试 4 — 低电量报警机制验证")
    print(f"  （临时将阈值设为 {threshold_v}V，任何正常读数都会触发）")
    print(DIVIDER)

    triggered = [False]

    def on_low(r):
        triggered[0] = True
        print(f"  ✅ 低电量回调触发：{r.voltage_v:.3f}V < {threshold_v}V")

    sensor = PowerSensor(
        config=PowerSensorConfig(poll_interval_s=1.0, low_battery_v=threshold_v),
        on_low_battery=on_low,
    )
    sensor.start()

    if sensor.is_simulation:
        print("  ❌ INA219 不可用")
        return

    print("  等待首次采样...")
    time.sleep(3)
    sensor.stop()

    if not triggered[0]:
        print("  ❌ 未触发（可能 INA219 没有读到有效电压）")


# ── 主入口 ─────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="INA219 电源传感器真机测试")
    parser.add_argument("--test", type=int, default=0)
    args = parser.parse_args()

    print("\n╔══════════════════════════════════════════════════════╗")
    print("║         INA219 电源传感器真机测试工具                ║")
    print("║  接线：VCC→3.3V，GND→GND，SDA→GPIO2，SCL→GPIO3      ║")
    print("╚══════════════════════════════════════════════════════╝")

    if args.test:
        {1: test_i2c_scan, 2: test_single_read,
         3: test_continuous, 4: test_low_battery_alert}.get(
            args.test, lambda: print("无效测试编号")
        )()
        return

    while True:
        print(f"\n{DIVIDER}")
        print("  [1] I2C 扫描（确认 INA219 地址）")
        print("  [2] 单次读数（电压/电流/功率）")
        print("  [3] 持续监测 20s（实时刷新）")
        print("  [4] 低电量报警机制验证")
        print("  [0] 退出")
        print(DIVIDER)
        print("  请选择：", end="")
        c = input().strip()
        if c == "0":
            break
        elif c == "1":
            test_i2c_scan()
        elif c == "2":
            test_single_read()
        elif c == "3":
            test_continuous()
        elif c == "4":
            test_low_battery_alert()


if __name__ == "__main__":
    main()
