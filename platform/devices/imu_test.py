"""
BNO055 IMU 真机测试脚本
========================
在树莓派上运行（BNO055 通过 CP2102 USB-UART 适配器接入后）。

接线（见 docs/HARDWARE.md §7）：
  BNO055 VCC  → CP2102 3.3V（或树莓派 3.3V Pin 1）
  BNO055 GND  → GND
  BNO055 ATX  → CP2102 RXD
  BNO055 LRX  → CP2102 TXD
  CP2102 USB  → 树莓派 USB → /dev/ttyUSB1（LD06 雷达占 /dev/ttyUSB0）

UART 模式切换（必须）：
  模块背面 S0 焊盘短接（PS0=HIGH）→ 切换为 UART 模式
  出厂默认 I2C，不短接 S0 则 BNO055 不响应 UART

前提：
  1. pip install pyserial（激光雷达已安装，通常已满足）
  2. 短接 BNO055 模块背面 S0 焊盘
  3. 插入 CP2102，确认 /dev/ttyUSB1 出现

用法：
  python3 imu_test.py              # 交互式菜单
  python3 imu_test.py --test 1    # 直接运行指定测试（1-6）
  python3 imu_test.py --stream    # 直接进入实时数据流（等同于 --test 3）
  python3 imu_test.py --port /dev/ttyUSB2  # 指定串口

测试项：
  1. 串口设备检测（列出 /dev/ttyUSB*，确认设备节点）
  2. BNO055 CHIP_ID 验证（原始 UART，不走 Imu 类）
  3. 实时数据流（连续滚动，Ctrl+C 停止）
  4. 静止零偏校准分析（采集 200 帧，输出均值/标准差）
  5. 振动响应测试（检测摇晃时加速度计是否变化）
  6. 偏航角积分演示（实时积分 gyro_z 显示转动角度）
"""

import argparse
import glob
import math
import sys
import time
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).parent))         # platform/devices/
sys.path.insert(0, str(Path(__file__).parent.parent))  # platform/
from imu import Imu, ImuReading

DIVIDER  = "─" * 60
BAUD     = 115200
PORT_PRIORITY = "/dev/ttyUSB0"

# BNO055 UART 协议常量（测试 2 直接使用原始串口）
_START, _READ, _WRITE = 0xAA, 0x01, 0x00
_RESP_READ, _RESP_WRITE = 0xBB, 0xEE
_MODE_CONFIG, _MODE_IMU = 0x00, 0x08
_REG_CHIP_ID, _REG_OPR_MODE, _REG_UNIT_SEL = 0x00, 0x3D, 0x3B
_REG_ACC_DATA, _REG_GYR_DATA = 0x08, 0x14


# ── 工具函数 ──────────────────────────────────────────────────────

def _s16(lo: int, hi: int) -> int:
    v = (hi << 8) | lo
    return v - 65536 if v > 32767 else v


def _uart_read(ser, reg: int, length: int) -> bytes:
    ser.reset_input_buffer()
    ser.write(bytes([_START, _READ, reg, length]))
    header = ser.read(2)
    if len(header) < 2 or header[0] != _RESP_READ:
        raise RuntimeError(f"响应头异常：{header.hex() if header else '空'}")
    data = ser.read(length)
    if len(data) < length:
        raise TimeoutError(f"数据不足（期望 {length}，实际 {len(data)}）")
    return data


def _uart_write(ser, reg: int, value: int) -> None:
    ser.reset_input_buffer()
    ser.write(bytes([_START, _WRITE, reg, 0x01, value]))
    resp = ser.read(2)
    if len(resp) < 2 or resp[0] != _RESP_WRITE or resp[1] != 0x01:
        raise RuntimeError(f"写失败：resp={resp.hex() if resp else '空'}")


def _candidate_ports() -> list[str]:
    ports = sorted(glob.glob("/dev/ttyUSB*") + glob.glob("/dev/ttyACM*"))
    out: list[str] = []
    for p in [PORT_PRIORITY, *ports]:
        if p and p not in out:
            out.append(p)
    return out


def _detect_bno_port() -> Optional[str]:
    """探测可通过 BNO055 CHIP_ID 握手的串口。"""
    try:
        import serial
    except ImportError:
        return None

    for port in _candidate_ports():
        try:
            ser = serial.Serial(port, baudrate=BAUD, timeout=0.35)
        except Exception:
            continue
        try:
            time.sleep(0.70)
            ser.reset_input_buffer()
            _uart_write(ser, _REG_OPR_MODE, _MODE_CONFIG)
            time.sleep(0.02)
            chip_id = _uart_read(ser, _REG_CHIP_ID, 1)[0]
            if chip_id == 0xA0:
                return port
        except Exception:
            pass
        finally:
            ser.close()
    return None


def _install_read_retry_for_test(imu: Imu, retries: int = 2, base_delay_s: float = 0.002) -> None:
    """
    仅在测试脚本中启用的临时补丁：
    给 Imu._read_reg 增加可恢复错误自动重试，避免 test 6 被偶发 UART 错误刷屏。
    """
    original_read = imu._read_reg

    def patched_read(reg: int, length: int) -> bytes:
        last_err: Exception | None = None
        for attempt in range(retries + 1):
            try:
                return original_read(reg, length)
            except (TimeoutError, RuntimeError) as e:
                msg = str(e)
                retriable = (
                    isinstance(e, TimeoutError)
                    or "0x07" in msg                  # BUS_OVER_RUN / 帧中断
                    or "数据不足" in msg
                    or "响应头异常" in msg
                )
                last_err = e
                if retriable and attempt < retries:
                    # 轻微递增退避，尽快恢复会话
                    time.sleep(base_delay_s * (attempt + 1))
                    continue
                raise
        raise last_err if last_err else RuntimeError("未知读取错误")

    imu._read_reg = patched_read


# ── 测试 1：串口设备检测 ──────────────────────────────────────────

def test_port_detect():
    print(f"\n{DIVIDER}")
    print("  测试 1 — 串口设备检测")
    print(DIVIDER)

    usb_ports = sorted(glob.glob("/dev/ttyUSB*"))
    acm_ports = sorted(glob.glob("/dev/ttyACM*"))
    all_ports  = usb_ports + acm_ports

    if not all_ports:
        print("  ❌ 未发现任何 /dev/ttyUSB* 或 /dev/ttyACM* 设备")
        print("  排查：CP2102 是否插入 USB？驱动已加载？（lsmod | grep cp210x）")
        return

    print(f"  发现 {len(all_ports)} 个串口设备：")
    for p in all_ports:
        hint = ""
        if "USB0" in p:
            hint = "← 通常为 LD06 激光雷达"
        elif "USB1" in p:
            hint = "← 通常为 BNO055 IMU（本脚本目标）"
        print(f"    {p}  {hint}")

    print()
    if PORT_PRIORITY in all_ports:
        print(f"  ✅ 优先端口 {PORT_PRIORITY} 已就绪")
    else:
        print(f"  ⚠  优先端口 {PORT_PRIORITY} 不存在")
        print("  提示：可使用 --port /dev/ttyUSBx 指定，或直接让脚本自动探测 BNO055 端口")


# ── 测试 2：BNO055 CHIP_ID 验证 ──────────────────────────────────

def test_chip_id(port: str):
    print(f"\n{DIVIDER}")
    print("  测试 2 — BNO055 CHIP_ID 验证（原始 UART，不走 Imu 类）")
    print(DIVIDER)

    try:
        import serial
    except ImportError:
        print("  ❌ pyserial 未安装：pip install pyserial")
        return

    print(f"  打开 {port} @ {BAUD}bps...")
    try:
        ser = serial.Serial(port, baudrate=BAUD, timeout=0.5)
    except Exception as e:
        print(f"  ❌ 串口打开失败：{e}")
        print("  排查：设备节点存在？CP2102 已插入？有读写权限（sudo usermod -aG dialout $USER）？")
        return

    try:
        print("  等待 BNO055 上电就绪（700ms）...")
        time.sleep(0.70)
        ser.reset_input_buffer()

        # 切到 CONFIG 模式
        _uart_write(ser, _REG_OPR_MODE, _MODE_CONFIG)
        time.sleep(0.02)

        # 读 CHIP_ID
        chip_id = _uart_read(ser, _REG_CHIP_ID, 1)[0]
        print(f"\n  CHIP_ID = 0x{chip_id:02X}", end="  ")
        if chip_id == 0xA0:
            print("← BNO055 ✅")
        else:
            print(f"← ❌ 非预期值（期望 0xA0）")
            print("  排查：S0 焊盘已短接？ATX/LRX 接线是否交叉（BNO055 ATX→CP2102 RXD）？")
            return

        # 读加速度计原始值（默认模式下即可读取）
        print()
        print("  切换至 IMU 融合模式...")
        _uart_write(ser, _REG_UNIT_SEL, 0x00)
        _uart_write(ser, _REG_OPR_MODE, _MODE_IMU)
        time.sleep(0.02)

        accel = _uart_read(ser, _REG_ACC_DATA, 6)
        gyro  = _uart_read(ser, _REG_GYR_DATA, 6)

        ACCEL_SCALE = 100.0 * 9.80665
        GYRO_SCALE  = 16.0

        ax = _s16(accel[0], accel[1]) / ACCEL_SCALE
        ay = _s16(accel[2], accel[3]) / ACCEL_SCALE
        az = _s16(accel[4], accel[5]) / ACCEL_SCALE
        gx = _s16(gyro[0],  gyro[1])  / GYRO_SCALE
        gy = _s16(gyro[2],  gyro[3])  / GYRO_SCALE
        gz = _s16(gyro[4],  gyro[5])  / GYRO_SCALE

        print()
        print(f"  ── 加速度计（m/s² 转 g，平放时 az ≈ ±1g）")
        print(f"    accel_x = {ax:+8.4f} g")
        print(f"    accel_y = {ay:+8.4f} g")
        print(f"    accel_z = {az:+8.4f} g")
        mag = math.sqrt(ax**2 + ay**2 + az**2)
        print(f"    合加速度 = {mag:.4f}g  {'✅（≈1g，静止）' if abs(mag - 1.0) < 0.2 else '⚠ 偏离1g，检查安装方向'}")

        print()
        print(f"  ── 陀螺仪（°/s，静止时应接近 0）")
        print(f"    gyro_x = {gx:+8.3f} °/s")
        print(f"    gyro_y = {gy:+8.3f} °/s")
        print(f"    gyro_z = {gz:+8.3f} °/s")

        print(f"\n  ✅ BNO055 通信正常，数据读取成功")

    except Exception as e:
        print(f"  ❌ 通信失败：{e}")
        print("  排查：S0 焊盘已短接（UART 模式）？接线 ATX→RXD / LRX→TXD 是否交叉？")
    finally:
        ser.close()


# ── 测试 3：实时数据流 ────────────────────────────────────────────

def test_stream(port: str, duration_s: int = 0):
    """连续打印 IMU 数据，duration_s=0 表示持续到 Ctrl+C。"""
    print(f"\n{DIVIDER}")
    label = f"实时数据流（{'Ctrl+C 停止' if duration_s == 0 else f'{duration_s}s'}）"
    print(f"  测试 3 — {label}")
    print(DIVIDER)

    imu = Imu(port)
    ok  = imu.start()
    if not ok:
        print("  ❌ IMU 启动失败（模拟模式）")
        print("  排查：串口存在？BNO055 上电？S0 已短接？")
        return

    print("  采样中... Ctrl+C 停止")
    print()
    print(f"  {'时间(s)':>8}  {'gyro_x':>8}  {'gyro_y':>8}  {'gyro_z':>8}  "
          f"{'accel_x':>8}  {'accel_y':>8}  {'accel_z':>8}  合加速度")
    print(f"  {'':>8}  {'(°/s)':>8}  {'(°/s)':>8}  {'(°/s)':>8}  "
          f"{'(g)':>8}  {'(g)':>8}  {'(g)':>8}")
    print("  " + "─" * 84)

    start = time.monotonic()
    try:
        while True:
            r = imu.get_latest()
            if r:
                elapsed = time.monotonic() - start
                mag = math.sqrt(r.accel_x**2 + r.accel_y**2 + r.accel_z**2)
                print(
                    f"  {elapsed:>8.2f}  "
                    f"{r.gyro_x:>+8.2f}  {r.gyro_y:>+8.2f}  {r.gyro_z:>+8.2f}  "
                    f"{r.accel_x:>+8.4f}  {r.accel_y:>+8.4f}  {r.accel_z:>+8.4f}  "
                    f"  {mag:.4f}g"
                )
            if duration_s and time.monotonic() - start >= duration_s:
                break
            time.sleep(0.1)
    except KeyboardInterrupt:
        print()
    finally:
        imu.stop()
        print("  已停止。")


# ── 测试 4：静止零偏校准分析 ──────────────────────────────────────

def test_calibration(port: str):
    print(f"\n{DIVIDER}")
    print("  测试 4 — 静止零偏校准分析")
    print(DIVIDER)
    print("  ⚠  请将传感器放平静止，不要触碰，采集 200 帧约需 3 秒...")

    imu = Imu(port)
    ok  = imu.start()
    if not ok:
        print("  ❌ IMU 启动失败（模拟模式），请检查接线和串口配置")
        return

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
        std  = math.sqrt(sum((v - mean) ** 2 for v in vals) / len(vals))
        return mean, std

    gx_m, gx_s = stats([s.gyro_x  for s in samples])
    gy_m, gy_s = stats([s.gyro_y  for s in samples])
    gz_m, gz_s = stats([s.gyro_z  for s in samples])
    ax_m, ax_s = stats([s.accel_x for s in samples])
    ay_m, ay_s = stats([s.accel_y for s in samples])
    az_m, az_s = stats([s.accel_z for s in samples])

    dts      = [samples[i+1].timestamp - samples[i].timestamp for i in range(len(samples)-1)]
    avg_dt   = sum(dts) / len(dts) if dts else 1
    actual_hz = 1.0 / avg_dt if avg_dt > 0 else 0

    print(f"  ── 陀螺仪零偏（静止应接近 0°/s）")
    print(f"    gyro_x  均值={gx_m:+8.3f}°/s   标准差={gx_s:.4f}°/s")
    print(f"    gyro_y  均值={gy_m:+8.3f}°/s   标准差={gy_s:.4f}°/s")
    print(f"    gyro_z  均值={gz_m:+8.3f}°/s   标准差={gz_s:.4f}°/s  ← 偏航轴（已去偏）")
    print()
    print(f"  ── 加速度计（平放时：ax≈0，ay≈0，az≈±1g）")
    print(f"    accel_x 均值={ax_m:+8.4f}g   标准差={ax_s:.5f}g")
    print(f"    accel_y 均值={ay_m:+8.4f}g   标准差={ay_s:.5f}g")
    print(f"    accel_z 均值={az_m:+8.4f}g   标准差={az_s:.5f}g")
    print()
    print(f"  ── 采样率")
    print(f"    实测 {actual_hz:.1f} Hz（目标 100 Hz，允许误差 ±15 Hz）")

    ok_gz  = abs(gz_m) < 2.0
    ok_acc = abs(abs(az_m) - 1.0) < 0.15 and abs(ax_m) < 0.1 and abs(ay_m) < 0.1
    ok_hz  = 85 <= actual_hz <= 115

    print()
    print("  ── 综合判断")
    print(f"    gyro_z 零偏  {'✅ 正常（< 2°/s）'   if ok_gz  else '⚠  偏大，请保持静止后重测'}"
          f"  ← 偏航轴（关键）")
    print(f"    加速度计静止 {'✅ 正常（平放）'      if ok_acc else '⚠  az 偏离 1g，检查安装方向'}")
    print(f"    采样率       {'✅ 正常'              if ok_hz  else f'⚠  {actual_hz:.1f}Hz 偏离目标'}")

    if ok_gz and ok_acc and ok_hz:
        print(f"\n  ✅ 校准分析通过，传感器工作正常，可集成到里程计")
    else:
        print(f"\n  ⚠  存在异常项，请对照提示排查后重新测试")


# ── 测试 5：振动响应测试 ──────────────────────────────────────────

def test_vibration(port: str):
    print(f"\n{DIVIDER}")
    print("  测试 5 — 振动响应测试")
    print(DIVIDER)
    print("  将在 10 秒内持续监测合加速度，")
    print("  请在提示后用力摇晃传感器，观察数值是否响应。")

    imu = Imu(port)
    ok  = imu.start()
    if not ok:
        print("  ❌ IMU 启动失败（模拟模式）")
        return

    time.sleep(0.5)
    SHAKE_THRESHOLD = 1.5
    baseline_samples: list[float] = []

    print("\n  [阶段 1/2] 静止基线采集（1.5s）...")
    deadline = time.monotonic() + 1.5
    while time.monotonic() < deadline:
        r = imu.get_latest()
        if r:
            baseline_samples.append(math.sqrt(r.accel_x**2 + r.accel_y**2 + r.accel_z**2))
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
                peak = max(peak, m)
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
        print("  请确认摇晃幅度足够大，或检查接线")


# ── 测试 6：偏航角积分演示 ────────────────────────────────────────

def test_yaw_integration(port: str):
    print(f"\n{DIVIDER}")
    print("  测试 6 — 偏航角积分演示（gyro_z 积分）")
    print(DIVIDER)
    print("  实时积分 gyro_z 显示转动角度（零偏已由 Imu 类校准）。")
    print("  ● 顺时针旋转传感器 → 角度增大")
    print("  ● 逆时针旋转       → 角度减小")
    print("  ● 按 Ctrl+C 停止并显示累计角度")

    imu = Imu(port)
    _install_read_retry_for_test(imu, retries=2, base_delay_s=0.002)
    ok  = imu.start()
    if not ok:
        print("  ❌ IMU 启动失败（模拟模式）")
        return

    time.sleep(0.5)
    yaw_deg  = 0.0
    prev_ts: float | None = None
    frame_count = 0
    BAR_WIDTH   = 40

    print()
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

                clamped = max(-180.0, min(180.0, yaw_deg))
                filled  = int(abs(clamped) / 180.0 * (BAR_WIDTH // 2))
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
║         BNO055 IMU 测试工具（UART via CP2102）       ║
╠══════════════════════════════════════════════════════╣
║  1. 串口设备检测（列出 /dev/ttyUSB*）                ║
║  2. CHIP_ID 验证（原始 UART，不走 Imu 类）           ║
║  3. 实时数据流（连续滚动，Ctrl+C 停止）               ║
║  4. 静止零偏校准分析（采 200 帧，输出均值/标准差）    ║
║  5. 振动响应测试（摇晃传感器验证加速度计）            ║
║  6. 偏航角积分演示（gyro_z 积分，显示转动角度）       ║
║  q. 退出                                             ║
╚══════════════════════════════════════════════════════╝"""


def main():
    parser = argparse.ArgumentParser(description="BNO055 IMU 测试工具（UART via CP2102）")
    parser.add_argument("--test",   type=int, default=0, metavar="N",
                        help="直接运行指定测试（1-6）")
    parser.add_argument("--stream", action="store_true",
                        help="直接进入实时数据流（等同于 --test 3）")
    parser.add_argument("--port",   type=str, default="",
                        help="串口设备节点（不填则自动探测 BNO055 端口）")
    args = parser.parse_args()

    port = args.port.strip() or _detect_bno_port() or PORT_PRIORITY
    auto_note = "（自动探测）" if not args.port.strip() else "（手动指定）"

    print("\n╔══════════════════════════════════════════════════════╗")
    print("║         BNO055 IMU 真机测试工具                      ║")
    print("║  接线：ATX→CP2102 RXD，LRX→CP2102 TXD，VCC→3.3V    ║")
    print(f"║  串口：{(port + auto_note):<44} ║")
    print("╚══════════════════════════════════════════════════════╝")

    tests = {
        1: lambda: test_port_detect(),
        2: lambda: test_chip_id(port),
        3: lambda: test_stream(port),
        4: lambda: test_calibration(port),
        5: lambda: test_vibration(port),
        6: lambda: test_yaw_integration(port),
    }

    if args.stream:
        test_stream(port)
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
