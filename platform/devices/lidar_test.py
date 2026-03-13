"""
LD06 激光雷达真机测试脚本
===========================
在树莓派上运行（CP2102 USB-TTL 转接后插入 USB 口）。

用法：
  python lidar_test.py              # 自动探测串口，交互式菜单
  python lidar_test.py --port /dev/ttyUSB0    # 指定串口
  python lidar_test.py --port /dev/ttyUSB0 --test 2  # 直接运行指定测试

测试项：
  1. 列出可用串口
  2. 连接并打印原始扫描数据（5 圈）
  3. ASCII 极坐标可视化（实时刷新）
  4. 性能统计（扫描频率、点数、有效率）
  5. 保存扫描数据到 JSON 文件

前提：pip install pyserial
"""

import argparse
import json
import math
import os
import sys
import time
from pathlib import Path

# 直接导入同目录的 lidar 模块（无需安装）
sys.path.insert(0, str(Path(__file__).parent))
from lidar import Lidar, LidarConfig, LidarScan, LidarPoint

DIVIDER = "─" * 60


def list_serial_ports() -> list[str]:
    """列出所有可用串口。"""
    try:
        import serial.tools.list_ports
        ports = serial.tools.list_ports.comports()
        return [p.device for p in ports]
    except ImportError:
        print("❌ 未安装 pyserial，请运行：pip install pyserial")
        return []


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

def test_raw_data(port: str, num_scans: int = 5):
    print(f"\n{DIVIDER}")
    print(f"  测试 2 — 原始扫描数据（前 {num_scans} 圈）")
    print(f"  串口：{port}")
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
            # 打印首圈前 5 个点详情
            print("           ┌─ 前 5 个测距点：")
            for p in scan.points[:5]:
                print(f"           │  角度={p.angle:6.2f}°  距离={p.distance:5d}mm  "
                      f"置信度={p.confidence:3d}  {'✓' if p.is_valid else '✗'}")
        if len(received) >= num_scans:
            done[0] = True

    lidar = Lidar(LidarConfig(port=port), on_scan=on_scan)
    lidar.start()

    if lidar.is_simulation:
        print("  ❌ 串口不可用，进入模拟模式（无真实数据）")
        return

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

    if not received:
        print("  ❌ 超时未收到任何数据，请检查：")
        print("     1. 接线正确（LD06 Tx → CP2102 RXD，P5V → 5V，GND → GND）")
        print("     2. 串口设备名正确（当前：%s）" % port)
        print("     3. 用户是否有串口权限：sudo usermod -aG dialout $USER")
    else:
        print(f"\n  ✅ 成功接收 {len(received)} 圈数据")


# ── 测试 3：ASCII 极坐标可视化 ────────────────────────────────────

def test_ascii_viz(port: str, duration_s: int = 10):
    print(f"\n{DIVIDER}")
    print(f"  测试 3 — 实时 ASCII 极坐标可视化（{duration_s}s）")
    print(f"  串口：{port}")
    print(DIVIDER)

    WIDTH  = 61
    HEIGHT = 31
    MAX_DIST = 3000  # 可视化最大距离（mm），超出截断

    def render(scan: LidarScan):
        grid = [['·'] * WIDTH for _ in range(HEIGHT)]
        cx, cy = WIDTH // 2, HEIGHT // 2
        grid[cy][cx] = 'O'  # 机器人自身

        # 绘制方向标记
        grid[cy][0]        = '←'
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
        print(f"  LD06 实时扫描  RPM={scan.rpm:.1f}  点数={scan.point_count}  "
              f"有效={len(scan.valid_points)}  (Ctrl+C 退出)\n")
        print("  ┌" + "─" * WIDTH + "┐")
        for row in grid:
            print("  │" + "".join(row) + "│")
        print("  └" + "─" * WIDTH + "┘")
        print(f"  范围：中心 = 机器人，边缘 = {MAX_DIST}mm，方向与传感器安装方向一致")

    lidar = Lidar(LidarConfig(port=port), on_scan=render)
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


# ── 测试 4：性能统计 ──────────────────────────────────────────────

def test_performance(port: str, duration_s: int = 10):
    print(f"\n{DIVIDER}")
    print(f"  测试 4 — 性能统计（{duration_s}s）")
    print(f"  串口：{port}")
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

    lidar = Lidar(LidarConfig(port=port), on_scan=on_scan)
    lidar.start()

    if lidar.is_simulation:
        print("  ❌ 串口不可用")
        return

    print(f"  统计中... （{duration_s}s）")
    try:
        time.sleep(duration_s)
    except KeyboardInterrupt:
        pass
    finally:
        lidar.stop()

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

def test_save_json(port: str, output_path: str = "/tmp/lidar_scan.json"):
    print(f"\n{DIVIDER}")
    print(f"  测试 5 — 保存一圈扫描数据到 JSON")
    print(f"  串口：{port}  输出：{output_path}")
    print(DIVIDER)

    result: list[LidarScan] = []
    done = [False]

    def on_scan(scan: LidarScan):
        if not done[0]:
            result.append(scan)
            done[0] = True

    lidar = Lidar(LidarConfig(port=port), on_scan=on_scan)
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


# ── 主入口 ─────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="LD06 激光雷达真机测试")
    parser.add_argument("--port", default="", help="串口路径（如 /dev/ttyUSB0）")
    parser.add_argument("--test", type=int, default=0, help="直接运行指定测试（1-5）")
    args = parser.parse_args()

    print("\n╔══════════════════════════════════════════════════════╗")
    print("║         LD06 激光雷达真机测试工具                    ║")
    print("║  接线：LD06 Tx→RXD, P5V→5V, GND→GND (CP2102)       ║")
    print("╚══════════════════════════════════════════════════════╝")

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
        # 直接运行指定测试
        tests = {
            1: lambda: test_list_ports(),
            2: lambda: test_raw_data(port),
            3: lambda: test_ascii_viz(port),
            4: lambda: test_performance(port),
            5: lambda: test_save_json(port),
        }
        fn = tests.get(args.test)
        if fn:
            fn()
        else:
            print(f"  ❌ 无效测试编号：{args.test}（1-5）")
        return

    # 交互式菜单
    while True:
        print(f"\n{DIVIDER}")
        print(f"  串口：{port}")
        print("  [1] 列出可用串口")
        print("  [2] 连接并打印原始扫描数据")
        print("  [3] ASCII 极坐标实时可视化")
        print("  [4] 性能统计（扫描频率、点数、RPM）")
        print("  [5] 保存一圈数据到 JSON")
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
            test_raw_data(port)
        elif choice == "3":
            test_ascii_viz(port)
        elif choice == "4":
            test_performance(port)
        elif choice == "5":
            test_save_json(port)
        else:
            print(f"  无效选项：{choice}")


if __name__ == "__main__":
    main()
