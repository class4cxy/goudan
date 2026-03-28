"""
MPU6050 IMU 真机测试脚本
=========================
在树莓派上运行（MPU6050 通过 I2C 接线完成后）。

接线（见 docs/HARDWARE.md §7）：
  MPU6050 VCC → 树莓派 3.3V（Pin 1 或 Pin 17）
  MPU6050 GND → GND
  MPU6050 SDA → GPIO 2（I2C1 SDA，物理引脚 3）
  MPU6050 SCL → GPIO 3（I2C1 SCL，物理引脚 5）
  MPU6050 AD0 → GND（I2C 地址 0x68）

前提：
  1. sudo raspi-config → Interface Options → I2C → Enable
  2. pip install smbus2
  3. i2cdetect -y 1  （应看到 0x68）

用法：
  python3 imu_test.py              # 交互式菜单
  python3 imu_test.py --test 1    # 直接运行指定测试（1-6）
  python3 imu_test.py --stream    # 直接进入实时数据流（等同于 --test 3）

测试项：
  1. I2C 总线扫描（确认 MPU6050 存在）
  2. 单次原始寄存器读取（直接操作寄存器，不走 Imu 类）
  3. 实时数据流（连续滚动，Ctrl+C 停止）
  4. 静止零偏校准分析（采集 200 帧，输出均值/标准差）
  5. 振动响应测试（检测摇晃时加速度计是否变化）
  6. 偏航角积分演示（实时积分 gyro_z 显示转动角度）
"""

import argparse
import math
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))         # platform/devices/
sys.path.insert(0, str(Path(__file__).parent.parent))  # platform/
from imu import Imu, ImuReading

DIVIDER = "─" * 60
IMU_ADDR = 0x68   # AD0→GND 时的默认地址


# ── 测试 1：I2C 总线扫描 ──────────────────────────────────────────

def test_i2c_scan():
    print(f"\n{DIVIDER}")
    print("  测试 1 — I2C 总线扫描")
    print(DIVIDER)
    try:
        import smbus2
    except ImportError:
        print("  ❌ smbus2 未安装，请运行：pip install smbus2")
        return

    try:
        bus = smbus2.SMBus(1)
    except Exception as e:
        print(f"  ❌ 无法打开 I2C 总线：{e}")
        print("  检查：sudo raspi-config → Interface Options → I2C → Enable")
        return

    found: dict[int, str] = {}
    print("  扫描 I2C1 总线（0x03–0x77）...")
    for addr in range(0x03, 0x78):
        try:
            bus.read_byte(addr)
            label = ""
            if addr == 0x40:
                label = "  ← INA219 电源传感器"
            elif addr == 0x68:
                label = "  ← MPU6050 IMU ✅"
            elif addr == 0x69:
                label = "  ← MPU6050 IMU（AD0=HIGH）✅"
            found[addr] = label
        except Exception:
            pass
    bus.close()

    if found:
        print(f"  发现 {len(found)} 个设备：")
        for addr, label in sorted(found.items()):
            print(f"    0x{addr:02X}{label}")
        if IMU_ADDR in found or 0x69 in found:
            print(f"\n  ✅ MPU6050 已识别，地址 0x{IMU_ADDR:02X}")
        else:
            print(f"\n  ❌ MPU6050（0x{IMU_ADDR:02X}）未找到")
            print("  排查：VCC→3.3V，SDA→Pin3，SCL→Pin5，AD0→GND，检查虚焊")
    else:
        print("  ❌ 总线上未发现任何设备")
        print("  排查：1) I2C 已启用？  2) SDA/SCL 接线正确？  3) VCC 已供电？")


# ── 测试 2：单次原始寄存器读取 ───────────────────────────────────

def test_raw_register():
    print(f"\n{DIVIDER}")
    print("  测试 2 — 原始寄存器读取（不走 Imu 类）")
    print(DIVIDER)
    try:
        import smbus2
        bus = smbus2.SMBus(1)
    except Exception as e:
        print(f"  ❌ I2C 初始化失败：{e}")
        return

    try:
        # 读 WHO_AM_I 寄存器（0x75），MPU6050 应返回 0x68
        who = bus.read_byte_data(IMU_ADDR, 0x75)
        print(f"  WHO_AM_I  = 0x{who:02X}  {'✅ 正确（应为 0x68）' if who == 0x68 else '❌ 异常'}")

        # 唤醒设备（清除睡眠位）
        bus.write_byte_data(IMU_ADDR, 0x6B, 0x00)
        time.sleep(0.1)

        # 读 14 字节原始数据
        data = bus.read_i2c_block_data(IMU_ADDR, 0x3B, 14)

        def s16(hi, lo):
            v = (hi << 8) | lo
            return v - 65536 if v > 32767 else v

        ax_raw = s16(data[0],  data[1])
        ay_raw = s16(data[2],  data[3])
        az_raw = s16(data[4],  data[5])
        gx_raw = s16(data[8],  data[9])
        gy_raw = s16(data[10], data[11])
        gz_raw = s16(data[12], data[13])

        accel_scale = 16384.0   # ±2g
        gyro_scale  = 131.0     # ±250°/s

        print()
        print(f"  ── 加速度计（±2g，16384 LSB/g）")
        print(f"    accel_x = {ax_raw:7d} LSB  →  {ax_raw/accel_scale:+7.4f} g")
        print(f"    accel_y = {ay_raw:7d} LSB  →  {ay_raw/accel_scale:+7.4f} g")
        print(f"    accel_z = {az_raw:7d} LSB  →  {az_raw/accel_scale:+7.4f} g")
        az_g = az_raw / accel_scale
        # 静止平放时 az 应约为 ±1g（重力方向），accel_x/y 约为 0
        if abs(abs(az_g) - 1.0) < 0.2:
            print(f"    → 芯片平放，Z 轴感受到重力（{az_g:+.3f}g ≈ ±1g）✅")
        else:
            magnitude = math.sqrt(
                (ax_raw/accel_scale)**2 +
                (ay_raw/accel_scale)**2 +
                (az_raw/accel_scale)**2
            )
            print(f"    → 合加速度 = {magnitude:.3f}g（静止时应约 1.0g）")

        print()
        print(f"  ── 陀螺仪（±250°/s，131 LSB/(°/s)）")
        print(f"    gyro_x  = {gx_raw:7d} LSB  →  {gx_raw/gyro_scale:+7.3f} °/s")
        print(f"    gyro_y  = {gy_raw:7d} LSB  →  {gy_raw/gyro_scale:+7.3f} °/s")
        print(f"    gyro_z  = {gz_raw:7d} LSB  →  {gz_raw/gyro_scale:+7.3f} °/s")
        max_gyro = max(abs(gx_raw), abs(gy_raw), abs(gz_raw)) / gyro_scale
        if max_gyro < 5.0:
            print(f"    → 芯片静止，各轴角速度 < 5°/s ✅")
        else:
            print(f"    → 最大角速度 {max_gyro:.1f}°/s（静止时应 < 5°/s，请保持静止重试）")

        bus.close()
        print(f"\n  ✅ 寄存器读取正常")
    except Exception as e:
        print(f"  ❌ 读取失败：{e}")
        print("  排查：I2C 地址是否正确？接线是否牢固？")
        bus.close()


# ── 测试 3：实时数据流 ────────────────────────────────────────────

def test_stream(duration_s: int = 0):
    """连续打印 IMU 数据，duration_s=0 表示持续到 Ctrl+C。"""
    print(f"\n{DIVIDER}")
    label = f"实时数据流（{'Ctrl+C 停止' if duration_s == 0 else f'{duration_s}s'}）"
    print(f"  测试 3 — {label}")
    print(DIVIDER)

    imu = Imu()
    ok = imu.start()
    if not ok:
        print("  ❌ IMU 启动失败（模拟模式）")
        print("  排查：smbus2 已安装？I2C 已启用？接线正确？")
        return

    print("  采样中... Ctrl+C 停止")
    print()
    print(f"  {'时间(s)':>8}  {'gyro_x':>8}  {'gyro_y':>8}  {'gyro_z':>8}  "
          f"{'accel_x':>8}  {'accel_y':>8}  {'accel_z':>8}   合加速度")
    print(f"  {'':>8}  {'(°/s)':>8}  {'(°/s)':>8}  {'(°/s)':>8}  "
          f"{'(g)':>8}  {'(g)':>8}  {'(g)':>8}")
    print("  " + "─" * 80)

    start = time.monotonic()
    try:
        while True:
            r = imu.get_latest()
            if r:
                elapsed = time.monotonic() - start
                magnitude = math.sqrt(r.accel_x**2 + r.accel_y**2 + r.accel_z**2)
                print(
                    f"  {elapsed:>8.2f}  "
                    f"{r.gyro_x:>+8.2f}  {r.gyro_y:>+8.2f}  {r.gyro_z:>+8.2f}  "
                    f"{r.accel_x:>+8.4f}  {r.accel_y:>+8.4f}  {r.accel_z:>+8.4f}  "
                    f"  {magnitude:.4f}g"
                )
            if duration_s and time.monotonic() - start >= duration_s:
                break
            time.sleep(0.1)
    except KeyboardInterrupt:
        print()
    finally:
        imu.stop()
        print(f"\n  已停止。")


# ── 测试 4：静止零偏校准分析 ──────────────────────────────────────

def test_calibration():
    print(f"\n{DIVIDER}")
    print("  测试 4 — 静止零偏校准分析")
    print(DIVIDER)
    print("  ⚠  请将传感器放平静止，不要触碰，采集 200 帧约需 3 秒...")

    imu = Imu()
    ok = imu.start()
    if not ok:
        print("  ❌ IMU 启动失败（模拟模式），请检查接线和 I2C 配置")
        return

    # 等待启动稳定（imu.start 内部已 sleep 0.6s）
    time.sleep(0.5)

    samples: list[ImuReading] = []
    print("  采集中", end="", flush=True)
    deadline = time.monotonic() + 4.0
    while time.monotonic() < deadline:
        r = imu.get_latest()
        if r and (not samples or r.timestamp != samples[-1].timestamp):
            samples.append(r)
        time.sleep(0.01)
        if len(samples) % 40 == 0:
            print(".", end="", flush=True)
    imu.stop()

    n = len(samples)
    print(f"\n  采集到 {n} 帧\n")
    if n < 50:
        print("  ❌ 样本过少，请检查 IMU 是否正常工作")
        return

    def stats(vals):
        mean = sum(vals) / len(vals)
        var  = sum((v - mean) ** 2 for v in vals) / len(vals)
        return mean, math.sqrt(var)

    gx_m, gx_s = stats([s.gyro_x  for s in samples])
    gy_m, gy_s = stats([s.gyro_y  for s in samples])
    gz_m, gz_s = stats([s.gyro_z  for s in samples])
    ax_m, ax_s = stats([s.accel_x for s in samples])
    ay_m, ay_s = stats([s.accel_y for s in samples])
    az_m, az_s = stats([s.accel_z for s in samples])

    # 时间戳差分估算实际采样率
    dts = [samples[i+1].timestamp - samples[i].timestamp for i in range(len(samples)-1)]
    avg_dt = sum(dts) / len(dts)
    actual_hz = 1.0 / avg_dt if avg_dt > 0 else 0

    print(f"  ── 陀螺仪零偏（静止应接近 0°/s）")
    print(f"    gyro_x  均值={gx_m:+8.3f}°/s   标准差={gx_s:.4f}°/s")
    print(f"    gyro_y  均值={gy_m:+8.3f}°/s   标准差={gy_s:.4f}°/s")
    print(f"    gyro_z  均值={gz_m:+8.3f}°/s   标准差={gz_s:.4f}°/s  ← 偏航轴（imu.py 已校准）")
    print()
    print(f"  ── 加速度计（平放时：ax≈0，ay≈0，az≈±1g）")
    print(f"    accel_x 均值={ax_m:+8.4f}g   标准差={ax_s:.5f}g")
    print(f"    accel_y 均值={ay_m:+8.4f}g   标准差={ay_s:.5f}g")
    print(f"    accel_z 均值={az_m:+8.4f}g   标准差={az_s:.5f}g")
    print()
    print(f"  ── 采样率")
    print(f"    实测 {actual_hz:.1f} Hz（目标 100 Hz，允许误差 ±10 Hz）")

    ok_gyro = max(abs(gx_m), abs(gy_m), abs(gz_m)) < 5.0
    ok_accel = abs(abs(az_m) - 1.0) < 0.15 and abs(ax_m) < 0.1 and abs(ay_m) < 0.1
    ok_hz    = 85 <= actual_hz <= 115

    print()
    print("  ── 综合判断")
    print(f"    陀螺仪零偏   {'✅ 正常（< 5°/s）' if ok_gyro  else '⚠  偏大，可能未静止或需重新上电校准'}")
    print(f"    加速度计静止 {'✅ 正常（平放）'   if ok_accel else '⚠  az 偏离 1g，检查安装方向或接线'}")
    print(f"    采样率       {'✅ 正常'           if ok_hz    else f'⚠  {actual_hz:.1f}Hz 偏离目标，检查 I2C 时钟'}")

    if ok_gyro and ok_accel and ok_hz:
        print(f"\n  ✅ 校准分析通过，传感器工作正常")
    else:
        print(f"\n  ⚠  存在异常项，请对照提示排查后重新测试")


# ── 测试 5：振动响应测试 ──────────────────────────────────────────

def test_vibration():
    print(f"\n{DIVIDER}")
    print("  测试 5 — 振动响应测试")
    print(DIVIDER)
    print("  将在 10 秒内持续监测合加速度，")
    print("  请在提示后用力摇晃传感器，观察数值是否响应。")
    print()

    imu = Imu()
    ok = imu.start()
    if not ok:
        print("  ❌ IMU 启动失败（模拟模式）")
        return

    time.sleep(0.5)

    SHAKE_THRESHOLD = 1.5  # g，超过此值判定为摇晃
    baseline_samples: list[float] = []

    # 采集 1.5s 静止基线
    print("  [阶段 1/2] 静止基线采集（1.5s）...")
    deadline = time.monotonic() + 1.5
    while time.monotonic() < deadline:
        r = imu.get_latest()
        if r:
            m = math.sqrt(r.accel_x**2 + r.accel_y**2 + r.accel_z**2)
            baseline_samples.append(m)
        time.sleep(0.02)

    baseline = sum(baseline_samples) / len(baseline_samples) if baseline_samples else 1.0
    print(f"  静止合加速度基线 = {baseline:.4f}g")
    print()
    print("  [阶段 2/2] ★ 请现在用力摇晃传感器！（8 秒）★")
    print()
    print(f"  {'合加速度(g)':>12}  峰值(g)   状态")
    print("  " + "─" * 40)

    peak = baseline
    shake_count = 0
    deadline = time.monotonic() + 8.0
    try:
        while time.monotonic() < deadline:
            r = imu.get_latest()
            if r:
                m = math.sqrt(r.accel_x**2 + r.accel_y**2 + r.accel_z**2)
                if m > peak:
                    peak = m
                shaking = m > SHAKE_THRESHOLD
                if shaking:
                    shake_count += 1
                print(
                    f"  {m:>12.4f}g  {peak:>6.3f}g  "
                    f"{'★ 摇晃检测到！' if shaking else '静止'}",
                    end="\r"
                )
            time.sleep(0.05)
    except KeyboardInterrupt:
        pass
    finally:
        imu.stop()

    print()
    print()
    print(f"  测试结果：")
    print(f"    静止基线  = {baseline:.4f}g")
    print(f"    峰值      = {peak:.4f}g")
    print(f"    摇晃帧数  = {shake_count}")

    if peak > SHAKE_THRESHOLD:
        print(f"\n  ✅ 加速度计响应正常（峰值 {peak:.3f}g > 阈值 {SHAKE_THRESHOLD}g）")
    else:
        print(f"\n  ⚠  未检测到摇晃（峰值 {peak:.3f}g ≤ 阈值 {SHAKE_THRESHOLD}g）")
        print("  请确认摇晃幅度足够大，或检查 accel_z 基线是否正常（应约 1g）")


# ── 测试 6：偏航角积分演示 ────────────────────────────────────────

def test_yaw_integration():
    print(f"\n{DIVIDER}")
    print("  测试 6 — 偏航角积分演示（gyro_z 积分）")
    print(DIVIDER)
    print("  实时积分 gyro_z 显示转动角度（零偏已由 Imu 类校准）。")
    print("  ● 顺时针旋转传感器 → 角度增大")
    print("  ● 逆时针旋转       → 角度减小")
    print("  ● 按 Ctrl+C 停止并显示累计角度")
    print()

    imu = Imu()
    ok = imu.start()
    if not ok:
        print("  ❌ IMU 启动失败（模拟模式）")
        return

    time.sleep(0.5)
    yaw_deg = 0.0
    prev_ts: float | None = None
    frame_count = 0

    BAR_WIDTH = 40
    print(f"  {'角速度(°/s)':>12}  {'累计角度(°)':>13}  方向示意")
    print("  " + "─" * 60)

    try:
        while True:
            r = imu.get_latest()
            if r:
                if prev_ts is not None:
                    dt = r.timestamp - prev_ts
                    if 0 < dt < 0.5:
                        yaw_deg += r.gyro_z * dt
                prev_ts = r.timestamp
                frame_count += 1

                # 角度进度条：以 ±180° 为满量程
                clamped = max(-180.0, min(180.0, yaw_deg))
                filled = int(abs(clamped) / 180.0 * (BAR_WIDTH // 2))
                if clamped >= 0:
                    bar = " " * (BAR_WIDTH // 2) + "│" + "█" * filled + " " * (BAR_WIDTH // 2 - filled)
                else:
                    bar = " " * (BAR_WIDTH // 2 - filled) + "█" * filled + "│" + " " * (BAR_WIDTH // 2)

                print(
                    f"  {r.gyro_z:>+12.2f}°/s  {yaw_deg:>+13.2f}°  [{bar}]",
                    end="\r"
                )
            time.sleep(0.02)
    except KeyboardInterrupt:
        pass
    finally:
        imu.stop()

    print()
    print()
    print(f"  最终累计偏航角：{yaw_deg:+.2f}°")
    print(f"  共处理 {frame_count} 帧")
    if abs(yaw_deg) < 2.0 and frame_count > 50:
        print("  （传感器几乎未转动，零偏校准正常）✅")
    else:
        print("  （累计角度反映了您的实际操作）")


# ── 主菜单 ────────────────────────────────────────────────────────

MENU = """
╔══════════════════════════════════════════════════════╗
║         MPU6050 IMU 测试工具                         ║
╠══════════════════════════════════════════════════════╣
║  1. I2C 总线扫描（确认设备地址）                     ║
║  2. 原始寄存器读取（单次，不走 Imu 类）               ║
║  3. 实时数据流（连续滚动，Ctrl+C 停止）               ║
║  4. 静止零偏校准分析（采 200 帧，输出均值/标准差）    ║
║  5. 振动响应测试（摇晃传感器验证加速度计）            ║
║  6. 偏航角积分演示（gyro_z 积分，显示转动角度）       ║
║  q. 退出                                             ║
╚══════════════════════════════════════════════════════╝"""


def main():
    parser = argparse.ArgumentParser(description="MPU6050 IMU 测试工具")
    parser.add_argument("--test", type=int, default=0, metavar="N",
                        help="直接运行指定测试（1-6）")
    parser.add_argument("--stream", action="store_true",
                        help="直接进入实时数据流（等同于 --test 3）")
    args = parser.parse_args()

    print("\n╔══════════════════════════════════════════════════════╗")
    print("║         MPU6050 IMU 真机测试工具                     ║")
    print("║  接线：VCC→3.3V，GND→GND，SDA→Pin3，SCL→Pin5       ║")
    print("║  I2C 地址：0x68（AD0→GND）                          ║")
    print("╚══════════════════════════════════════════════════════╝")

    tests = {
        1: test_i2c_scan,
        2: test_raw_register,
        3: test_stream,
        4: test_calibration,
        5: test_vibration,
        6: test_yaw_integration,
    }

    if args.stream:
        test_stream()
        return

    if args.test:
        fn = tests.get(args.test)
        if fn:
            fn()
        else:
            print(f"  ❌ 无效测试编号：{args.test}（1-6）")
        return

    while True:
        print(MENU)
        try:
            choice = input("请选择 > ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            break

        if choice == "q":
            break
        elif choice.isdigit() and int(choice) in tests:
            tests[int(choice)]()
        else:
            print("  无效选项")

    print("  退出。")


if __name__ == "__main__":
    main()
