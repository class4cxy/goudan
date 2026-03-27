"""
LD06 激光雷达真机测试脚本
===========================
在树莓派上运行（CP2102 USB-TTL 转接后插入 USB 口）。

接线（见 docs/HARDWARE.md §3/§4）：
  LD06 Pin4 (P5V) → CP2102 5V
  LD06 Pin3 (GND) → CP2102 GND
  LD06 Pin1 (Tx)  → CP2102 RXD
  LD06 Pin2 (PWM) → 树莓派 GPIO18（Pin 12）← 电机转速控制，必须直连 GPIO！
  CP2102 USB-A    → 树莓派 USB-A 口

用法：
  python lidar_test.py                              # 自动探测串口，交互式菜单
  python lidar_test.py --port /dev/ttyUSB0          # 指定串口
  python lidar_test.py --motor-pin 18               # 指定电机控制 GPIO（默认 18）
  python lidar_test.py --no-motor                   # 不控制电机（PWM 悬空，常转）
  python lidar_test.py --test 2                     # 直接运行指定测试

测试项：
  1. 列出可用串口
  2. 连接并打印原始扫描数据（5 圈）
  3. ASCII 极坐标可视化（实时刷新）
  4. 性能统计（扫描频率、点数、RPM）
  5. 保存扫描数据到 JSON 文件
  6. 电机控制专项测试（启动→扫描→停止验证）

前提：pip install pyserial
"""

import argparse
import json
import math
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))         # platform/devices/ → import lidar
sys.path.insert(0, str(Path(__file__).parent.parent))  # platform/ → import devices.gpio_adapter
from lidar import Lidar, LidarConfig, LidarScan, LidarPoint

DIVIDER = "─" * 60

# ── 默认电机控制引脚（与 HARDWARE.md §4 保持一致）─────────────────
# GPIO15（Pin 10，UART RX）：唯一空闲引脚，UART 控制台已移除，可正常用作普通 GPIO
# 禁止使用 GPIO18（蜂鸣器）、GPIO12（左后编码A）、GPIO20/21（超声波）
DEFAULT_MOTOR_PIN = 15
# lgpio 软件 PWM 上限 10kHz；LD06 规格要求 20~50kHz。
# 若 lgpio 报 'bad PWM frequency'，改用 pigpio（DMA PWM，无频率上限）：
#   sudo apt install pigpiod && sudo systemctl enable --now pigpiod
#   pip install pigpio
DEFAULT_PWM_FREQ  = 1000    # Hz：先用 1kHz 验证 LD06 是否响应，再视情况提频
DEFAULT_PWM_DUTY  = 60.0    # %，约 10Hz 扫描速率


def make_config(port: str, motor_pin: int,
                pwm_freq: int = 0,
                pwm_duty: float = DEFAULT_PWM_DUTY) -> LidarConfig:
    """构造 LidarConfig，电机引脚 -1 表示悬空（常转，内部调速）。
    pwm_freq=0 表示使用当前 DEFAULT_PWM_FREQ（支持运行时 --pwm-freq 覆盖）。
    """
    return LidarConfig(
        port=port,
        motor_pin=motor_pin,
        motor_pwm_freq=pwm_freq or DEFAULT_PWM_FREQ,
        motor_pwm_duty=pwm_duty,
    )


def list_serial_ports() -> list[str]:
    """列出所有可用串口。"""
    try:
        import serial.tools.list_ports
        ports = serial.tools.list_ports.comports()
        return [p.device for p in ports]
    except ImportError:
        print("❌ 未安装 pyserial，请运行：pip install pyserial")
        return []


def _motor_label(motor_pin: int) -> str:
    if motor_pin < 0:
        return "悬空（常转，内部调速 10Hz）"
    return f"GPIO{motor_pin}（PWM {DEFAULT_PWM_FREQ//1000}kHz @ {DEFAULT_PWM_DUTY:.0f}%，stop() 后停转）"


# ── 测试 1：列出串口 ───────────────────────────────────────────────

def test_list_ports():
    print(f"\n{DIVIDER}")
    print("  测试 1 — 可用串口")
    print(DIVIDER)
    ports = list_serial_ports()
    if not ports:
        print("  未发现任何串口设备")
        print("  提示：CP2102 需要安装驱动（macOS Big Sur+ 免驱，Linux 通常无需驱动）")
    else:
        for i, p in enumerate(ports, 1):
            print(f"  [{i}] {p}")
    return ports


# ── 测试 2：连接并打印原始数据 ────────────────────────────────────

def test_raw_data(port: str, motor_pin: int, num_scans: int = 5):
    print(f"\n{DIVIDER}")
    print(f"  测试 2 — 原始扫描数据（前 {num_scans} 圈）")
    print(f"  串口：{port}  电机：{_motor_label(motor_pin)}")
    print(DIVIDER)

    received: list[LidarScan] = []
    done = [False]

    def on_scan(scan: LidarScan):
        received.append(scan)
        idx = len(received)
        valid = scan.valid_points
        print(
            f"  圈 {idx:3d} | 时间戳 {scan.timestamp_ms} ms | "
            f"RPM={scan.rpm:6.1f} | 总点数={scan.point_count:4d} | "
            f"有效={len(valid):4d} ({100*len(valid)//max(scan.point_count,1)}%)"
        )
        if idx == 1:
            print("           ┌─ 前 5 个测距点：")
            for p in scan.points[:5]:
                print(f"           │  角度={p.angle:6.2f}°  距离={p.distance:5d}mm  "
                      f"置信度={p.confidence:3d}  {'✓' if p.is_valid else '✗'}")
        if len(received) >= num_scans:
            done[0] = True

    lidar = Lidar(make_config(port, motor_pin), on_scan=on_scan)
    lidar.start()

    if lidar.is_simulation:
        print("  ❌ 串口不可用，进入模拟模式（无真实数据）")
        return

    if motor_pin >= 0:
        print(f"  电机已通过 GPIO{motor_pin} 启动，等待稳定...")
    print("  等待数据... （Ctrl+C 中断）")
    try:
        timeout = 15.0
        start = time.time()
        while not done[0] and time.time() - start < timeout:
            time.sleep(0.1)
    except KeyboardInterrupt:
        pass
    finally:
        lidar.stop()
        if motor_pin >= 0:
            print(f"  电机已停止（GPIO{motor_pin} → LOW）")

    if not received:
        print("  ❌ 超时未收到任何数据，请检查：")
        print("     1. 接线正确（LD06 Tx→CP2102 RXD，P5V→5V，GND→GND）")
        print("     2. 电机控制线：LD06 PWM → GPIO%d" % motor_pin if motor_pin >= 0 else
              "     2. PWM 悬空时电机应自转，若无数据检查串口线")
        print("     3. 串口设备名正确（当前：%s）" % port)
        print("     4. 用户是否有串口权限：sudo usermod -aG dialout $USER")
    else:
        print(f"\n  ✅ 成功接收 {len(received)} 圈数据")


# ── 测试 3：ASCII 极坐标可视化 ────────────────────────────────────

def test_ascii_viz(port: str, motor_pin: int, duration_s: int = 10):
    print(f"\n{DIVIDER}")
    print(f"  测试 3 — 实时 ASCII 极坐标可视化（{duration_s}s）")
    print(f"  串口：{port}  电机：{_motor_label(motor_pin)}")
    print(DIVIDER)

    WIDTH  = 61
    HEIGHT = 31
    MAX_DIST = 3000  # 可视化最大距离（mm），超出截断

    def render(scan: LidarScan):
        grid = [['·'] * WIDTH for _ in range(HEIGHT)]
        cx, cy = WIDTH // 2, HEIGHT // 2
        grid[cy][cx] = 'O'

        grid[cy][0]         = '←'
        grid[cy][WIDTH - 1] = '→'
        grid[0][cx]         = '↑'
        grid[HEIGHT - 1][cx] = '↓'

        for p in scan.valid_points:
            if p.distance > MAX_DIST:
                continue
            rad   = math.radians(p.angle)
            scale = min(p.distance / MAX_DIST, 1.0)
            dx    = math.sin(rad) * scale * (WIDTH  // 2 - 2)
            dy    = -math.cos(rad) * scale * (HEIGHT // 2 - 2)
            gx    = int(cx + dx)
            gy    = int(cy + dy)
            if 0 <= gx < WIDTH and 0 <= gy < HEIGHT:
                grid[gy][gx] = '█'

        os.system("clear")
        motor_info = f"GPIO{motor_pin}" if motor_pin >= 0 else "悬空"
        print(f"  LD06 实时扫描  RPM={scan.rpm:.1f}  点数={scan.point_count}  "
              f"有效={len(scan.valid_points)}  电机={motor_info}  (Ctrl+C 退出)\n")
        print("  ┌" + "─" * WIDTH + "┐")
        for row in grid:
            print("  │" + "".join(row) + "│")
        print("  └" + "─" * WIDTH + "┘")
        print(f"  范围：中心 = 机器人，边缘 = {MAX_DIST}mm，方向与传感器安装方向一致")

    lidar = Lidar(make_config(port, motor_pin), on_scan=render)
    lidar.start()

    if lidar.is_simulation:
        print("  ❌ 串口不可用")
        return

    try:
        time.sleep(duration_s)
    except KeyboardInterrupt:
        pass
    finally:
        lidar.stop()
        if motor_pin >= 0:
            print(f"\n  电机已停止（GPIO{motor_pin} → LOW）")


# ── 测试 4：性能统计 ──────────────────────────────────────────────

def test_performance(port: str, motor_pin: int, duration_s: int = 10):
    print(f"\n{DIVIDER}")
    print(f"  测试 4 — 性能统计（{duration_s}s）")
    print(f"  串口：{port}  电机：{_motor_label(motor_pin)}")
    print(DIVIDER)

    stats = {
        "count": 0, "total_points": 0, "total_valid": 0,
        "rpm_sum": 0.0, "t_start": 0.0,
    }

    def on_scan(scan: LidarScan):
        if stats["count"] == 0:
            stats["t_start"] = time.time()
        stats["count"]        += 1
        stats["total_points"] += scan.point_count
        stats["total_valid"]  += len(scan.valid_points)
        stats["rpm_sum"]      += scan.rpm

    lidar = Lidar(make_config(port, motor_pin), on_scan=on_scan)
    lidar.start()

    if lidar.is_simulation:
        print("  ❌ 串口不可用")
        return

    if motor_pin >= 0:
        print(f"  电机已通过 GPIO{motor_pin} 启动")
    print(f"  统计中... （{duration_s}s，Ctrl+C 提前结束）")
    try:
        time.sleep(duration_s)
    except KeyboardInterrupt:
        pass
    finally:
        lidar.stop()
        if motor_pin >= 0:
            print(f"  电机已停止（GPIO{motor_pin} → LOW）")

    n = stats["count"]
    if n == 0:
        print("  ❌ 未收到任何数据")
        return

    elapsed  = time.time() - stats["t_start"]
    scan_hz  = n / elapsed if elapsed > 0 else 0
    avg_pts  = stats["total_points"] / n
    avg_rpm  = stats["rpm_sum"] / n
    valid_r  = 100 * stats["total_valid"] // stats["total_points"] if stats["total_points"] else 0

    print(f"\n  ─── 性能报告 ─────────────────────────────")
    print(f"  采集时长   : {elapsed:.1f}s")
    print(f"  完整圈数   : {n}")
    print(f"  扫描频率   : {scan_hz:.2f} Hz（规格：5–13 Hz）")
    print(f"  平均转速   : {avg_rpm:.1f} RPM")
    print(f"  每圈点数   : {avg_pts:.0f}（规格：约 450）")
    print(f"  有效点比例 : {valid_r}%")
    if scan_hz < 4 or scan_hz > 15:
        print(f"  ⚠️  扫描频率异常，检查接线和串口波特率（应为 230400）")
    else:
        print(f"  ✅ 性能正常")


# ── 测试 5：保存 JSON ─────────────────────────────────────────────

def test_save_json(port: str, motor_pin: int, output_path: str = "/tmp/lidar_scan.json"):
    print(f"\n{DIVIDER}")
    print(f"  测试 5 — 保存一圈扫描数据到 JSON")
    print(f"  串口：{port}  输出：{output_path}  电机：{_motor_label(motor_pin)}")
    print(DIVIDER)

    result: list[LidarScan] = []
    done = [False]

    def on_scan(scan: LidarScan):
        if not done[0]:
            result.append(scan)
            done[0] = True

    lidar = Lidar(make_config(port, motor_pin), on_scan=on_scan)
    lidar.start()

    if lidar.is_simulation:
        print("  ❌ 串口不可用")
        return

    print("  等待一圈完整数据...")
    try:
        timeout = 10.0
        start = time.time()
        while not done[0] and time.time() - start < timeout:
            time.sleep(0.1)
    except KeyboardInterrupt:
        pass
    finally:
        lidar.stop()
        if motor_pin >= 0:
            print(f"  电机已停止（GPIO{motor_pin} → LOW）")

    if not result:
        print("  ❌ 超时，未采集到数据")
        return

    scan = result[0]
    data = scan.to_dict()
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    print(f"  ✅ 已保存：{output_path}")
    print(f"     圈数据：{scan.point_count} 点，{len(scan.valid_points)} 有效，RPM={scan.rpm:.1f}")
    print(f"     文件大小：{Path(output_path).stat().st_size / 1024:.1f} KB")


# ── 测试 6：电机控制专项 ──────────────────────────────────────────

def test_motor_control(port: str, motor_pin: int):
    """
    专项测试电机启停：
      阶段 1 — 启动电机，等待 3s 采样 RPM，确认电机在转
      阶段 2 — 停止（lidar.stop()），确认 RPM 归零（无数据输入）
    motor_pin < 0 时无法测试，给出提示退出。
    """
    print(f"\n{DIVIDER}")
    print("  测试 6 — 电机控制专项（启动 → 扫描 → 停止验证）")
    print(f"  串口：{port}")
    print(DIVIDER)

    if motor_pin < 0:
        print("  ❌ 电机引脚未配置（--no-motor 模式），跳过此测试")
        print(f"     重新运行时添加：--motor-pin {DEFAULT_MOTOR_PIN}")
        return

    print(f"  电机控制引脚：GPIO{motor_pin}")
    print(f"  PWM 参数：{DEFAULT_PWM_FREQ//1000}kHz @ {DEFAULT_PWM_DUTY:.0f}%")
    print()

    # ─── 阶段 1：启动并采样 ───────────────────────────────────────
    print("  [阶段 1] 启动电机，采集 3s 数据...")

    phase1_rpms: list[float] = []
    phase1_scans = [0]

    def on_scan_phase1(scan: LidarScan):
        phase1_rpms.append(scan.rpm)
        phase1_scans[0] += 1
        print(f"           圈 {phase1_scans[0]:3d}  RPM={scan.rpm:6.1f}  "
              f"有效点={len(scan.valid_points):4d}")

    lidar = Lidar(make_config(port, motor_pin), on_scan=on_scan_phase1)
    lidar.start()

    if lidar.is_simulation:
        print("  ❌ 串口不可用，无法执行电机控制测试")
        return

    print(f"           GPIO{motor_pin} PWM 已输出，电机应开始旋转...")
    try:
        time.sleep(3.0)
    except KeyboardInterrupt:
        lidar.stop()
        print(f"\n  中断：电机已停止（GPIO{motor_pin} → LOW）")
        return

    avg_rpm_running = sum(phase1_rpms) / len(phase1_rpms) if phase1_rpms else 0.0
    got_data = phase1_scans[0] > 0

    print()
    if got_data:
        print(f"  [阶段 1] ✅ 采到 {phase1_scans[0]} 圈，平均 RPM = {avg_rpm_running:.1f}")
    else:
        print("  [阶段 1] ❌ 未采到数据，检查串口和接线")

    # ─── 阶段 2：停止并验证 ───────────────────────────────────────
    print()
    print(f"  [阶段 2] 调用 lidar.stop()，停止电机...")
    lidar.stop()
    print(f"           GPIO{motor_pin} → LOW，PWM 已关闭")

    # 等待 2s，确认不再收到新数据
    post_stop_scans = [0]
    post_stop_done = [False]

    def on_scan_post(_scan: LidarScan):
        post_stop_scans[0] += 1

    lidar2 = Lidar(make_config(port, -1), on_scan=on_scan_post)  # -1：不控电机，只监听
    lidar2.start()
    if not lidar2.is_simulation:
        print("           监听 2s，观察电机停转后是否还有数据...")
        try:
            time.sleep(2.0)
        except KeyboardInterrupt:
            pass
        finally:
            lidar2.stop()

        if post_stop_scans[0] == 0:
            print(f"  [阶段 2] ✅ 停止后 2s 内未收到新扫描圈 → 电机已停转")
        else:
            print(f"  [阶段 2] ⚠️  停止后仍收到 {post_stop_scans[0]} 圈 → 电机可能仍在旋转")
            print(f"           检查：GPIO{motor_pin} 是否已拉低，接线是否松动")
    else:
        print("           （串口监听失败，跳过停止验证）")

    # ─── 总结 ────────────────────────────────────────────────────
    print()
    print(f"  ─── 电机控制测试总结 ──────────────────────")
    print(f"  电机引脚   : GPIO{motor_pin}  (BCM)")
    print(f"  PWM 频率   : {DEFAULT_PWM_FREQ//1000}kHz")
    print(f"  PWM 占空比 : {DEFAULT_PWM_DUTY:.0f}%")
    print(f"  运行阶段   : {'✅ 有数据' if got_data else '❌ 无数据'}"
          f"  平均 RPM = {avg_rpm_running:.1f}")
    print(f"  停止阶段   : {'✅ 已停转' if post_stop_scans[0] == 0 else '⚠️ 未完全停止'}")
    if got_data and post_stop_scans[0] == 0:
        print(f"  结论       : 电机控制正常 ✅")
    elif not got_data:
        print(f"  结论       : 未采到数据，检查串口线和 CP2102")
    else:
        print(f"  结论       : 停止后仍有数据，检查 PWM 线或 GPIO 配置")


# ── 测试 7：GPIO 连通性验证（不依赖 PWM）────────────────────────────

def test_gpio_connection(port: str, motor_pin: int):
    """
    纯 GPIO.output() 方式验证 PWM 线是否接对引脚。
    不使用 PWM —— 规避 'bad PWM frequency' 错误，直接用高低电平观察 LD06 电机转速变化。

    原理：
      LD06 PWM 引脚 HIGH（3.3V）→ 占空比 100%，电机超速（RPM > 默认 10Hz）
      LD06 PWM 引脚 LOW（0V）  → 占空比 0%，电机减速或停转
      若 RPM 在 HIGH/LOW 切换间 **没有任何变化**，说明线未接到该引脚。
    """
    print(f"\n{DIVIDER}")
    print("  测试 7 — GPIO 连通性验证（不依赖 PWM）")
    print(f"  串口：{port}  待测引脚：GPIO{motor_pin}（Pin {_bcm_to_pin(motor_pin)}）")
    print(DIVIDER)

    if motor_pin < 0:
        print("  ❌ 电机引脚未配置，请加 --motor-pin 参数")
        return

    try:
        from devices.gpio_adapter import GPIO
    except Exception as e:
        print(f"  ❌ GPIO 初始化失败：{e}")
        return

    # ─── 步骤 1：设置引脚为输出，先拉 LOW ─────────────────────────
    try:
        GPIO.setmode(GPIO.BCM)
        GPIO.setwarnings(False)
        GPIO.setup(motor_pin, GPIO.OUT)
        GPIO.output(motor_pin, GPIO.LOW)
        print(f"  GPIO{motor_pin} 已设为 OUTPUT，当前 LOW（0V）")
    except Exception as e:
        print(f"  ❌ GPIO.setup 失败：{e}")
        print(f"     可能原因：RPi5 需要安装 rpi-lgpio：pip install rpi-lgpio")
        return

    # ─── 步骤 2：扫描 3s 采基线 RPM（LOW 状态）─────────────────────
    print()
    print("  [LOW 阶段] GPIO → LOW，采集 3s 基线 RPM...")
    low_rpms: list[float] = []

    def on_low(scan: LidarScan):
        low_rpms.append(scan.rpm)

    lidar = Lidar(LidarConfig(port=port, motor_pin=-1), on_scan=on_low)
    lidar.start()

    if lidar.is_simulation:
        print("  ❌ 串口不可用")
        try:
            GPIO.cleanup()
        except Exception:
            pass
        return

    try:
        time.sleep(3.0)
    except KeyboardInterrupt:
        lidar.stop()
        return
    lidar.stop()

    avg_low = sum(low_rpms) / len(low_rpms) if low_rpms else 0.0
    print(f"           LOW 平均 RPM = {avg_low:.1f}  ({len(low_rpms)} 圈)")

    # ─── 步骤 3：切换 HIGH，再采 3s ───────────────────────────────
    print()
    print("  [HIGH 阶段] GPIO → HIGH（3.3V），采集 3s RPM...")
    GPIO.output(motor_pin, GPIO.HIGH)

    high_rpms: list[float] = []

    def on_high(scan: LidarScan):
        high_rpms.append(scan.rpm)

    lidar2 = Lidar(LidarConfig(port=port, motor_pin=-1), on_scan=on_high)
    lidar2.start()

    try:
        time.sleep(3.0)
    except KeyboardInterrupt:
        lidar2.stop()
        GPIO.output(motor_pin, GPIO.LOW)
        return
    lidar2.stop()

    avg_high = sum(high_rpms) / len(high_rpms) if high_rpms else 0.0
    print(f"           HIGH 平均 RPM = {avg_high:.1f}  ({len(high_rpms)} 圈)")

    # ─── 步骤 4：复位 LOW ────────────────────────────────────────
    GPIO.output(motor_pin, GPIO.LOW)
    print(f"\n  GPIO{motor_pin} 已复位 → LOW")

    # ─── 步骤 5：判断连通性 ───────────────────────────────────────
    print()
    print(f"  ─── 连通性判断 ──────────────────────────")
    rpm_delta = abs(avg_high - avg_low)
    print(f"  LOW  平均 RPM : {avg_low:.1f}")
    print(f"  HIGH 平均 RPM : {avg_high:.1f}")
    print(f"  RPM 差值      : {rpm_delta:.1f}")
    print()

    if avg_low == 0.0 and avg_high == 0.0:
        print("  ❌ 两段均无数据，检查串口连接")
    elif rpm_delta < 30:
        print("  ⚠️  HIGH/LOW 切换后 RPM 几乎无变化（差 < 30）")
        print(f"     → GPIO{motor_pin} 可能未连接到 LD06 PWM 引脚")
        print(f"     → 请确认导线插在 物理 Pin {_bcm_to_pin(motor_pin)} 上（从左上角数）")
        print(f"     → 另一端插在 LD06 ZH1.5T-4P 连接器的 Pin 2（PWM）")
    else:
        print(f"  ✅ HIGH/LOW 切换 RPM 差值 {rpm_delta:.0f}，GPIO{motor_pin} 已正确连接到 LD06 PWM！")
        print(f"     下一步：修复 'bad PWM frequency' 错误（见下方说明）")
        print()
        print("  PWM 修复方法（任选其一）：")
        print("   A. 安装 rpi-lgpio（推荐，RPi5 官方兼容层）：")
        print("      pip install rpi-lgpio")
        print("   B. 安装 pigpio（DMA PWM，支持任意引脚任意频率）：")
        print("      pip install pigpio  &&  sudo pigpiod")


def _bcm_to_pin(bcm: int) -> str:
    """BCM 编号 → 物理引脚编号（常用引脚）。"""
    _map = {
        2:3, 3:5, 4:7, 5:29, 6:31, 7:26, 8:24, 9:21,
        10:19, 11:23, 12:32, 13:33, 14:8, 15:10, 16:36,
        17:11, 18:12, 19:35, 20:38, 21:40, 22:15, 23:16,
        24:18, 25:22, 26:37, 27:13,
    }
    return str(_map.get(bcm, "?"))


# ── 主入口 ─────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="LD06 激光雷达真机测试")
    parser.add_argument("--port", default="", help="串口路径（如 /dev/ttyUSB0）")
    parser.add_argument("--test", type=int, default=0, help="直接运行指定测试（1-7）")
    parser.add_argument(
        "--motor-pin", type=int, default=DEFAULT_MOTOR_PIN,
        help=f"电机控制 GPIO 引脚 BCM 编号（默认 {DEFAULT_MOTOR_PIN}）",
    )
    parser.add_argument(
        "--no-motor", action="store_true",
        help="不控制电机（PWM 悬空，电机常转，内部自动调速）",
    )
    parser.add_argument(
        "--pwm-freq", type=int, default=DEFAULT_PWM_FREQ,
        help=f"PWM 频率 Hz（默认 {DEFAULT_PWM_FREQ}；lgpio 软件 PWM 上限 10000）",
    )
    args = parser.parse_args()

    motor_pin = -1 if args.no_motor else args.motor_pin

    # 运行时覆盖模块级默认频率，使所有 make_config() 调用生效
    global DEFAULT_PWM_FREQ
    DEFAULT_PWM_FREQ = args.pwm_freq

    print("\n╔══════════════════════════════════════════════════════════╗")
    print("║          LD06 激光雷达真机测试工具                       ║")
    print("║  串口：LD06 Tx→CP2102 RXD，P5V→5V，GND→GND             ║")
    print("║  电机：LD06 PWM → 树莓派 GPIO（BCM）直连，不经 CP2102   ║")
    print("╚══════════════════════════════════════════════════════════╝")

    motor_desc = _motor_label(motor_pin)
    print(f"  电机控制：{motor_desc}")

    # 确定串口
    port = args.port
    if not port:
        ports = test_list_ports()
        if not ports:
            print("\n未找到串口，请先确认 CP2102 已插入并安装驱动")
            return
        if len(ports) == 1:
            port = ports[0]
            print(f"\n  自动选择唯一串口：{port}")
        else:
            print("\n  请输入串口编号（回车选择第一个）：", end="")
            choice = input().strip()
            idx = int(choice) - 1 if choice.isdigit() else 0
            port = ports[max(0, min(idx, len(ports) - 1))]
            print(f"  选择：{port}")

    if args.test:
        tests = {
            1: lambda: test_list_ports(),
            2: lambda: test_raw_data(port, motor_pin),
            3: lambda: test_ascii_viz(port, motor_pin),
            4: lambda: test_performance(port, motor_pin),
            5: lambda: test_save_json(port, motor_pin),
            6: lambda: test_motor_control(port, motor_pin),
            7: lambda: test_gpio_connection(port, motor_pin),
        }
        fn = tests.get(args.test)
        if fn:
            fn()
        else:
            print(f"  ❌ 无效测试编号：{args.test}（1-7）")
        return

    # 交互式菜单
    while True:
        print(f"\n{DIVIDER}")
        print(f"  串口：{port}  电机引脚：{'GPIO%d' % motor_pin if motor_pin >= 0 else '悬空（常转）'}")
        print("  [1] 列出可用串口")
        print("  [2] 连接并打印原始扫描数据")
        print("  [3] ASCII 极坐标实时可视化")
        print("  [4] 性能统计（扫描频率、点数、RPM）")
        print("  [5] 保存一圈数据到 JSON")
        print("  [6] 电机控制专项测试（启动→扫描→停止验证）")
        print("  [7] GPIO 连通性验证（不依赖 PWM，先跑这个！）")
        print("  [0] 退出")
        print(DIVIDER)
        print("  请选择：", end="")
        choice = input().strip()

        if choice == "0":
            print("  退出。")
            break
        elif choice == "1":
            test_list_ports()
        elif choice == "2":
            test_raw_data(port, motor_pin)
        elif choice == "3":
            test_ascii_viz(port, motor_pin)
        elif choice == "4":
            test_performance(port, motor_pin)
        elif choice == "5":
            test_save_json(port, motor_pin)
        elif choice == "6":
            test_motor_control(port, motor_pin)
        elif choice == "7":
            test_gpio_connection(port, motor_pin)
        else:
            print(f"  无效选项：{choice}")


if __name__ == "__main__":
    main()
