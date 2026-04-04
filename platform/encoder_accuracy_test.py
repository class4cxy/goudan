#!/usr/bin/env python3
"""
编码器里程精度测试（直接硬件控制，无需 HTTP 服务端）
======================================================

测试流程：
  1. 直接初始化底盘 + 编码器 + 里程计（跳过 FastAPI，直接控制硬件）
  2. 驱动机器人前进指定距离（编码器脉冲闭环，或超时停止）
  3. 打印每次运行的左右轮 ticks、对称比、里程计计算距离
  4. 全部重复完成后，你用卷尺量实际总位移，脚本给出校准建议

用途对比（与 motion_distance_test.py 的区别）：
  - motion_distance_test.py：通过 HTTP /motor/drive，需要服务端已启动
  - encoder_accuracy_test.py：直接控制硬件，更底层，显示原始 tick 级细节

运行方式：
  cd /path/to/goudan/platform
  python3 encoder_accuracy_test.py                      # 交互输入距离
  python3 encoder_accuracy_test.py --distance 500mm     # 指定距离
  python3 encoder_accuracy_test.py --distance 1m --speed 30 --repeats 3
  python3 encoder_accuracy_test.py --tick-only          # 只计数 ticks，不驱动底盘（手推测试）

参数说明：
  --distance DIST   目标距离，支持 mm / cm / m，负数=后退（默认 500mm）
  --speed PCT       电机速度 0-100（建议 25-40，过高精度差，默认 30）
  --timeout S       单次超时秒数（默认 30s）
  --repeats N       重复次数，取算术平均（默认 1）
  --tick-only       纯计数模式：只读编码器，不驱动底盘（用于手推校验）
  --no-imu          强制禁用 IMU（里程计只用编码器）
"""

from __future__ import annotations

import argparse
import math
import os
import re
import sys
import time
from pathlib import Path

# ── 把 platform/ 加入 Python 路径，使 devices.* 可导入 ───────────────────────
_PLATFORM_DIR = Path(__file__).parent.resolve()
if str(_PLATFORM_DIR) not in sys.path:
    sys.path.insert(0, str(_PLATFORM_DIR))


# ──────────────────────────────────────────────────────────────────────────────
# 工具函数
# ──────────────────────────────────────────────────────────────────────────────

def parse_distance_mm(text: str) -> float:
    s = text.strip().lower().replace(" ", "")
    m = re.fullmatch(r"([+-]?\d+(?:\.\d+)?)(mm|cm|m)?", s)
    if not m:
        raise ValueError("格式错误，示例：500mm / 100cm / 1m / -300mm")
    value = float(m.group(1))
    unit = m.group(2) or "mm"
    if unit == "cm":
        return value * 10.0
    if unit == "m":
        return value * 1000.0
    return value


def fmt_mm(v: float) -> str:
    return f"{v:+.1f} mm"


def print_sep(char: str = "─", width: int = 68) -> None:
    print(char * width)


# ──────────────────────────────────────────────────────────────────────────────
# 主测试逻辑
# ──────────────────────────────────────────────────────────────────────────────

def run_tick_only_mode(encoder, ticks_per_rev: int, wheel_circ_mm: float) -> None:
    """
    手推模式：只读编码器 ticks，不驱动底盘。
    推动机器人一段已知距离后按 Ctrl+C 查看结果。
    """
    print("\n[手推模式] 请手动推动机器人，完成后按 Ctrl+C 查看结果。")
    print_sep()

    start_l = start_r = 0
    total_l = total_r = 0
    report_interval = 1.0
    last_report = time.monotonic()

    try:
        while True:
            dl, dr = encoder.read_and_reset()
            total_l += dl
            total_r += dr

            now = time.monotonic()
            if now - last_report >= report_interval:
                dist_l = (total_l / ticks_per_rev) * wheel_circ_mm
                dist_r = (total_r / ticks_per_rev) * wheel_circ_mm
                avg    = (dist_l + dist_r) / 2.0
                sym    = (min(abs(dist_l), abs(dist_r)) / max(abs(dist_l), abs(dist_r))) if max(abs(dist_l), abs(dist_r)) > 0 else 1.0
                print(
                    f"  左轮: {total_l:+6d} ticks = {dist_l:+7.1f} mm"
                    f"  右轮: {total_r:+6d} ticks = {dist_r:+7.1f} mm"
                    f"  平均: {avg:+7.1f} mm  对称: {sym:.3f}"
                )
                last_report = now
            time.sleep(0.01)
    except KeyboardInterrupt:
        pass

    dist_l = (total_l / ticks_per_rev) * wheel_circ_mm
    dist_r = (total_r / ticks_per_rev) * wheel_circ_mm
    avg    = (dist_l + dist_r) / 2.0

    print_sep()
    print(f"\n最终统计：")
    print(f"  左轮 总 ticks = {total_l:+d}  ≈ {dist_l:+.1f} mm")
    print(f"  右轮 总 ticks = {total_r:+d}  ≈ {dist_r:+.1f} mm")
    print(f"  里程计估算平均行程 = {avg:+.1f} mm")

    try:
        raw = input("\n请输入卷尺实测距离（mm/cm/m，保持方向符号）: ").strip()
        actual_mm = parse_distance_mm(raw)
        error_mm  = actual_mm - avg
        error_pct = error_mm / max(abs(actual_mm), 1e-9) * 100.0
        lines_per_rev = int(os.environ.get("ENCODER_LINES_PER_REV", "500"))
        print(f"\n  实测: {actual_mm:.1f} mm  误差: {error_mm:+.1f} mm ({error_pct:+.1f}%)")
        if abs(actual_mm) > 1e-3:
            suggested = lines_per_rev * (abs(avg) / abs(actual_mm))
            print(f"  建议 ENCODER_LINES_PER_REV: {suggested:.0f}（当前 {lines_per_rev}）")
    except (KeyboardInterrupt, ValueError):
        pass


def run_single_trip(
    chassis,
    encoder,
    odometry,
    target_mm: float,
    speed: int,
    timeout_s: float,
    ticks_per_rev: int,
    wheel_circ_mm: float,
    run_idx: int,
) -> dict:
    """
    执行一次行程，返回原始测量数据。

    Tick 统计使用 encoder.get_cumulative() 快照（不清零），
    避免与里程计的 read_and_reset() 竞争同一份数据。
    """
    direction = "forward" if target_mm >= 0 else "backward"
    target_abs = abs(target_mm)

    # 里程计清零
    odometry.get_and_reset_travel()

    # 记录 cumulative ticks 起始快照（无竞争，不清零）
    snap_left_0, snap_right_0 = encoder.get_cumulative()

    # 启动电机
    t_start = time.monotonic()
    chassis._dispatch(direction, speed)

    # 闭环监控（20ms 轮询，仅里程计消费 read_and_reset）
    traveled = 0.0
    timed_out = False
    deadline = t_start + timeout_s

    while True:
        time.sleep(0.02)
        traveled += odometry.get_and_reset_travel()
        if traveled >= target_abs:
            break
        if time.monotonic() >= deadline:
            timed_out = True
            break

    elapsed = time.monotonic() - t_start
    chassis.stop()
    time.sleep(0.15)  # 等待电机完全停止

    # 取行程结束时的 cumulative 快照，差值即本次行程 ticks（无竞争）
    snap_left_1, snap_right_1 = encoder.get_cumulative()
    left_ticks  = snap_left_1  - snap_left_0
    right_ticks = snap_right_1 - snap_right_0

    dist_left  = (left_ticks  / ticks_per_rev) * wheel_circ_mm
    dist_right = (right_ticks / ticks_per_rev) * wheel_circ_mm
    odom_dist  = (dist_left + dist_right) / 2.0

    # 左右对称比（越接近 1.0 越好）
    max_abs = max(abs(dist_left), abs(dist_right))
    symmetry = (min(abs(dist_left), abs(dist_right)) / max_abs) if max_abs > 1e-6 else 1.0

    print(f"\n  ── 第 {run_idx} 次 {'─' * 40}")
    print(f"  目标:      {target_abs:.1f} mm  方向: {direction}  speed: {speed}")
    print(f"  耗时:      {elapsed:.2f} s  {'⚠ 超时停车' if timed_out else '✓ 正常停车'}")
    print(f"  左轮 ticks: {left_ticks:+6d}  ≈ {dist_left:+7.1f} mm")
    print(f"  右轮 ticks: {right_ticks:+6d}  ≈ {dist_right:+7.1f} mm")
    print(f"  里程计估算: {odom_dist:+7.1f} mm   对称比: {symmetry:.3f}")

    if timed_out:
        print(f"  ⚠ 超时：编码器 ticks 可能不可靠（EMF 噪声？），建议查阅 encoder_diag.py")

    # 对称比警告
    if symmetry < 0.90:
        asym_side = "左轮" if abs(dist_left) > abs(dist_right) else "右轮"
        print(f"  ⚠ 左右不对称（{symmetry:.2%}）：{asym_side}输出偏弱，"
              f"可调整 CHASSIS_LEFT_SCALE / CHASSIS_RIGHT_SCALE")

    return {
        "run_idx":    run_idx,
        "target_mm":  target_mm,
        "left_ticks": left_ticks,
        "right_ticks":right_ticks,
        "dist_left":  dist_left,
        "dist_right": dist_right,
        "odom_dist":  odom_dist,
        "symmetry":   symmetry,
        "elapsed_s":  elapsed,
        "timed_out":  timed_out,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="编码器里程精度直接测试（无需 HTTP 服务端）")
    parser.add_argument("--distance", default="500mm",
                        help="目标距离，支持 mm/cm/m，负数=后退（默认 500mm）")
    parser.add_argument("--speed",    type=int,   default=30,
                        help="电机速度 0-100（建议 25-40，默认 30）")
    parser.add_argument("--timeout",  type=float, default=30.0,
                        help="单次超时秒数（默认 30s）")
    parser.add_argument("--repeats",  type=int,   default=1,
                        help="重复次数（默认 1）")
    parser.add_argument("--tick-only", action="store_true",
                        help="只计数 ticks，不驱动底盘（手推校验模式）")
    parser.add_argument("--no-imu",  action="store_true",
                        help="强制禁用 IMU（里程计只用编码器）")
    args = parser.parse_args()

    # ── 解析目标距离 ────────────────────────────────────────────────────────
    try:
        target_mm = parse_distance_mm(args.distance)
    except ValueError as e:
        print(f"❌ {e}")
        return 1

    speed = max(0, min(100, args.speed))

    # ── 导入硬件模块 ────────────────────────────────────────────────────────
    print("\n正在初始化硬件...")
    try:
        from devices.encoder import Encoder, EncoderConfig
        from devices.chassis import Chassis, DEFAULT_CONFIG
        from odometry import Odometry, OdometryConfig
    except ImportError as e:
        print(f"❌ 导入平台模块失败：{e}")
        print("   请确认在 platform/ 目录下运行，且 .venv 已安装依赖。")
        return 1

    # ── 初始化编码器 ────────────────────────────────────────────────────────
    encoder = Encoder()
    hw_ok = encoder.start()
    if not hw_ok:
        print("⚠ 编码器以模拟模式启动（lgpio 不可用），测试数据将全为 0。")
        print("   请在树莓派上运行并安装 lgpio：sudo apt install -y python3-lgpio")
    else:
        print(f"✓ 编码器就绪（ticks/rev={encoder.ticks_per_rev}）")

    # ── 初始化里程计 ────────────────────────────────────────────────────────
    imu = None
    if not args.no_imu:
        try:
            from devices.imu import IMU
            imu = IMU()
            imu.start()
            if imu.is_simulation:
                imu = None
        except Exception:
            imu = None

    odom_cfg = OdometryConfig()
    odometry = Odometry(encoder=encoder, imu=imu, config=odom_cfg)
    odometry.start()

    wheel_circ_mm = 2.0 * math.pi * odom_cfg.wheel_radius_mm
    ticks_per_rev = encoder.ticks_per_rev

    print(f"✓ 里程计就绪（轮径={odom_cfg.wheel_radius_mm}mm  轮距={odom_cfg.wheel_base_mm}mm"
          f"  IMU={'真实' if imu else '无'}）")
    print(f"  轮周长 = {wheel_circ_mm:.2f} mm  ticks/rev = {ticks_per_rev}")

    # ── 手推模式 ────────────────────────────────────────────────────────────
    if args.tick_only:
        try:
            run_tick_only_mode(encoder, ticks_per_rev, wheel_circ_mm)
        finally:
            odometry.stop()
            encoder.stop()
        return 0

    # ── 初始化底盘 ──────────────────────────────────────────────────────────
    chassis = Chassis(DEFAULT_CONFIG)
    print(f"✓ 底盘就绪（{'模拟' if chassis.is_simulation else 'GPIO 真实'}）")

    # ── 开始测试 ────────────────────────────────────────────────────────────
    print_sep("═")
    print(f"测试参数：距离={target_mm:+.1f}mm  速度={speed}%  "
          f"超时={args.timeout}s  重复={args.repeats}次")
    print_sep("═")

    results = []
    try:
        for i in range(1, args.repeats + 1):
            if i > 1:
                pause = 2.0
                print(f"\n  [间隔 {pause}s，让电机冷却并重新摆放机器人...]")
                time.sleep(pause)
                input("  按 Enter 继续下一次测试... ")

            result = run_single_trip(
                chassis    = chassis,
                encoder    = encoder,
                odometry   = odometry,
                target_mm  = target_mm,
                speed      = speed,
                timeout_s  = args.timeout,
                ticks_per_rev = ticks_per_rev,
                wheel_circ_mm = wheel_circ_mm,
                run_idx    = i,
            )
            results.append(result)

    except KeyboardInterrupt:
        print("\n已取消。")
        chassis.stop()
    finally:
        odometry.stop()
        encoder.stop()
        chassis.cleanup()

    if not results:
        return 130

    # ── 汇总统计 ────────────────────────────────────────────────────────────
    n = len(results)
    target_abs   = abs(target_mm)
    odom_vals    = [r["odom_dist"] for r in results]
    odom_avg     = sum(abs(v) for v in odom_vals) / n
    odom_std     = math.sqrt(sum((abs(v) - odom_avg) ** 2 for v in odom_vals) / n) if n > 1 else 0.0
    sym_avg      = sum(r["symmetry"] for r in results) / n
    timed_out_n  = sum(1 for r in results if r["timed_out"])

    print_sep("═")
    print(f"\n=== 汇总（共 {n} 次）===")
    print(f"  目标距离:         {target_abs:.1f} mm")
    print(f"  里程计均值:       {odom_avg:.1f} mm   σ={odom_std:.1f} mm")
    odom_error = odom_avg - target_abs
    print(f"  里程计系统误差:   {odom_error:+.1f} mm ({odom_error/target_abs*100:+.1f}%)")
    print(f"  左右对称比均值:   {sym_avg:.3f}（1.000=完美）")
    if timed_out_n:
        print(f"  ⚠ {timed_out_n}/{n} 次超时停车，编码器数据可能不可靠")

    # ── 人工测量与校准建议 ───────────────────────────────────────────────────
    print_sep()
    print("\n请用卷尺测量机器人实际行驶距离（从起点到停止位置）：")
    if args.repeats > 1:
        print(f"  若每次都重置起点，请输入 {args.repeats} 次的算术平均值；")
        print("  若机器人累计走了多次，请输入总位移。")

    try:
        raw = input("实测距离（mm/cm/m，保持方向符号，直接回车跳过）: ").strip()
        if not raw:
            print("已跳过人工校准。")
            return 0

        actual_mm      = parse_distance_mm(raw)
        actual_abs     = abs(actual_mm)
        error_mm       = actual_abs - odom_avg
        error_pct      = error_mm / max(odom_avg, 1e-9) * 100.0
        lines_per_rev  = int(os.environ.get("ENCODER_LINES_PER_REV", "500"))

        print_sep()
        print(f"\n=== 人工校准结果 ===")
        print(f"  里程计估算：{odom_avg:.1f} mm")
        print(f"  卷尺实测：  {actual_abs:.1f} mm")
        print(f"  偏差：      {error_mm:+.1f} mm（里程计 {'高估' if error_mm < 0 else '低估'}了实际距离）")

        print(f"\n  【误差方向解读】")
        if abs(error_pct) < 3.0:
            print(f"  ✅ 误差 {error_pct:+.1f}%，里程计精度良好（<3%），无需调整。")
        elif abs(error_pct) < 10.0:
            print(f"  ⚠ 误差 {error_pct:+.1f}%，建议校准。")
        else:
            print(f"  ❌ 误差 {error_pct:+.1f}%，里程计严重不准，建议优先排查编码器噪声（encoder_diag.py）。")

        print(f"\n  【校准建议】")
        if actual_abs > 1e-3 and odom_avg > 1e-3:
            # 公式推导：里程计距离 = (ticks/tpr) * 2πR，若实测=actual、估算=odom
            # 要让里程计=actual，等效于把 tpr 缩放 odom/actual 倍
            # 即 new_lines_per_rev = old_lines_per_rev * (actual / odom)
            suggested_lpr = lines_per_rev * (actual_abs / odom_avg)
            print(f"  当前 ENCODER_LINES_PER_REV = {lines_per_rev}")
            print(f"  建议 ENCODER_LINES_PER_REV = {suggested_lpr:.0f}")
            print(f"  校准公式: 新值 = 旧值 × (卷尺实测 / 里程计估算)")
            print(f"           = {lines_per_rev} × ({actual_abs:.1f} / {odom_avg:.1f})")
            print(f"           = {suggested_lpr:.0f}")
            print(f"\n  在 .env 中更新后重启 platform：")
            print(f"    ENCODER_LINES_PER_REV={suggested_lpr:.0f}")

        # 左右对称性建议
        left_avg  = sum(abs(r["dist_left"])  for r in results) / n
        right_avg = sum(abs(r["dist_right"]) for r in results) / n
        if left_avg > 1e-3 and right_avg > 1e-3:
            ratio = right_avg / left_avg
            print(f"\n  【左右对称性】左轮平均={left_avg:.1f}mm  右轮平均={right_avg:.1f}mm  比值={ratio:.3f}")
            if ratio < 0.93:
                suggested_rscale = round(ratio, 2)
                print(f"  右轮偏慢，建议设置 CHASSIS_RIGHT_SCALE={suggested_rscale}")
            elif ratio > 1.07:
                suggested_lscale = round(1.0 / ratio, 2)
                print(f"  左轮偏慢，建议设置 CHASSIS_LEFT_SCALE={suggested_lscale}")
            else:
                print(f"  ✅ 左右对称性良好（比值 {ratio:.3f}）")

    except KeyboardInterrupt:
        print("\n已跳过校准。")
    except ValueError as e:
        print(f"❌ 距离解析失败：{e}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
