#!/usr/bin/env python3
"""
编码器里程精度测试（直接硬件控制，无需 HTTP 服务端，无需 .env）
=================================================================

用法：直接编辑下方「测试配置」区的常量，然后运行：
    python3 encoder_accuracy_test.py

测试流程：
  1. 初始化编码器 + 里程计 + 底盘
  2. 驱动机器人前进 TARGET_DISTANCE_MM（编码器脉冲闭环，超时则停）
  3. 打印左右轮 ticks、对称比、里程计估算距离
  4. 你用卷尺量实际行驶距离，回填后脚本给出 ENCODER_LINES_PER_REV 校准建议

TICK_ONLY_MODE=True 时为手推模式：不驱动底盘，手推车辆后看 ticks 与距离对应关系。
"""

from __future__ import annotations

import math
import re
import sys
import time
from pathlib import Path

# ══════════════════════════════════════════════════════════════════════════════
# 测试配置（修改这里，不依赖 .env）
# ══════════════════════════════════════════════════════════════════════════════

# ── 编码器 GPIO 引脚（BCM 编号）────────────────────────────────────────────────
#
# M3 左后轮（实物：白线=A，黄线=B）
LEFT_A:  int = 23   # M3 白线 A → GPIO23（Pin 16，扩展板 GP23）
LEFT_B:  int = 16   # M3 黄线 B → GPIO16（Pin 36，扩展板 GP16）
#
# M4 右后轮（实物：白线=A，黄线=B）
# ⚠️ 超声波 HC-SR04 原占用 GPIO20/21，已关闭超声波，把这两脚让给 M4 编码器。
RIGHT_A: int = 20   # M4 白线 A → GPIO20（Pin 38）
RIGHT_B: int = 21   # M4 黄线 B → GPIO21（Pin 40）

# ── 编码器参数 ────────────────────────────────────────────────────────────────

# 编码器标称线数。4 倍频后 ticks/rev = LINES_PER_REV × 4。
# 实测校准：目标500mm 走了1250mm，里程计读509mm → 204 = 500×(509/1250)
# 注：误差来源疑似 EMF 噪声漏脉冲，待 encoder_diag.py 进一步排查
LINES_PER_REV: int = 204

# 前进时某轮 ticks 为负时翻转极性（等效于对调 A/B 接线）。
# 实测：M4 右后轮前进时 ticks 为负（右侧电机镜像安装导致相位相反），需翻转。
LEFT_INVERT:  bool = False
RIGHT_INVERT: bool = True

# 去抖参数。检测到跳变后连续读 DEBOUNCE_READS 次，确认电平稳定才计数。
# 推荐值：1 次 × 20μs（默认，低噪声场景）
# 高噪声场景（PWM 耦合严重）可改为：3 次 × 500μs
DEBOUNCE_READS: int = 1    # 去抖读取次数
DEBOUNCE_US:    int = 20   # 去抖延时（微秒）

# ── 里程计参数 ────────────────────────────────────────────────────────────────

# 驱动轮半径（mm）。用卡尺量轮子直径后除以 2 填入。
WHEEL_RADIUS_MM: float = 34.0

# 两驱动轮（后轮）中心间距（mm）。用卡尺量后轮轮胎中心到中心的距离。
WHEEL_BASE_MM:   float = 160.0

# ── 测试参数 ──────────────────────────────────────────────────────────────────

# 目标行驶距离（mm）。正数=前进，负数=后退。
TARGET_DISTANCE_MM: float = 500.0

# 电机速度（0-100%）。建议 25-40；过高时编码器漏脉冲，里程偏小。
MOTOR_SPEED: int = 30

# 单次超时（秒）。编码器闭环未达目标时的强制停车时限。
TIMEOUT_S: float = 30.0

# 重复测试次数。大于 1 时取平均，评估一致性。每次需手动复位车辆起点。
REPEATS: int = 1

# 手推模式：True = 只读编码器，不驱动底盘（用于手推校验 ticks→距离换算）。
TICK_ONLY_MODE: bool = False

# ══════════════════════════════════════════════════════════════════════════════
# 以下无需修改
# ══════════════════════════════════════════════════════════════════════════════

_PLATFORM_DIR = Path(__file__).parent.resolve()
if str(_PLATFORM_DIR) not in sys.path:
    sys.path.insert(0, str(_PLATFORM_DIR))


def _parse_mm(text: str) -> float:
    s = text.strip().lower().replace(" ", "")
    m = re.fullmatch(r"([+-]?\d+(?:\.\d+)?)(mm|cm|m)?", s)
    if not m:
        raise ValueError("格式错误，示例：500mm / 100cm / 1m / -300mm")
    v, u = float(m.group(1)), m.group(2) or "mm"
    return v * (10.0 if u == "cm" else 1000.0 if u == "m" else 1.0)


def _sep(c: str = "─", w: int = 68) -> None:
    print(c * w)


# ── 手推模式 ───────────────────────────────────────────────────────────────────

def _tick_only(encoder, ticks_per_rev: int, wheel_circ_mm: float) -> None:
    print("\n[手推模式] 手动推动机器人，Ctrl+C 结束后查看结果。")
    _sep()

    total_l = total_r = 0
    last_t = time.monotonic()

    try:
        while True:
            dl, dr = encoder.read_and_reset()
            total_l += dl
            total_r += dr
            if time.monotonic() - last_t >= 1.0:
                dist_l = (total_l / ticks_per_rev) * wheel_circ_mm
                dist_r = (total_r / ticks_per_rev) * wheel_circ_mm
                avg    = (dist_l + dist_r) / 2.0
                sym    = (min(abs(dist_l), abs(dist_r)) / max(abs(dist_l), abs(dist_r))
                          if max(abs(dist_l), abs(dist_r)) > 0 else 1.0)
                print(f"  L: {total_l:+6d} tk={dist_l:+7.1f}mm"
                      f"  R: {total_r:+6d} tk={dist_r:+7.1f}mm"
                      f"  avg={avg:+7.1f}mm  sym={sym:.3f}")
                last_t = time.monotonic()
            time.sleep(0.01)
    except KeyboardInterrupt:
        pass

    dist_l = (total_l / ticks_per_rev) * wheel_circ_mm
    dist_r = (total_r / ticks_per_rev) * wheel_circ_mm
    avg    = (dist_l + dist_r) / 2.0
    _sep()
    print(f"  左轮: {total_l:+d} ticks ≈ {dist_l:+.1f} mm")
    print(f"  右轮: {total_r:+d} ticks ≈ {dist_r:+.1f} mm")
    print(f"  平均: {avg:+.1f} mm")

    try:
        raw = input("\n卷尺实测距离（mm/cm/m）: ").strip()
        actual = _parse_mm(raw)
        err_pct = (actual - avg) / max(abs(actual), 1e-9) * 100.0
        suggested = LINES_PER_REV * (abs(avg) / abs(actual)) if abs(actual) > 1e-3 else 0
        print(f"  实测: {actual:.1f}mm  误差: {actual-avg:+.1f}mm ({err_pct:+.1f}%)")
        if suggested:
            print(f"  建议 LINES_PER_REV: {suggested:.0f}（当前 {LINES_PER_REV}）")
    except (KeyboardInterrupt, ValueError):
        pass


# ── 单次行程 ───────────────────────────────────────────────────────────────────

def _run_trip(chassis, encoder, odometry,
              target_mm: float, speed: int, timeout_s: float,
              ticks_per_rev: int, wheel_circ_mm: float,
              run_idx: int) -> dict:
    direction  = "forward" if target_mm >= 0 else "backward"
    target_abs = abs(target_mm)

    odometry.get_and_reset_travel()
    snap_l0, snap_r0 = encoder.get_cumulative()

    t0 = time.monotonic()
    chassis._dispatch(direction, speed)

    traveled   = 0.0
    timed_out  = False
    deadline   = t0 + timeout_s
    while True:
        time.sleep(0.02)
        traveled += odometry.get_and_reset_travel()
        if traveled >= target_abs:
            break
        if time.monotonic() >= deadline:
            timed_out = True
            break

    elapsed = time.monotonic() - t0
    chassis.stop()
    time.sleep(0.15)

    snap_l1, snap_r1  = encoder.get_cumulative()
    left_ticks  = snap_l1 - snap_l0
    right_ticks = snap_r1 - snap_r0

    dist_l  = (left_ticks  / ticks_per_rev) * wheel_circ_mm
    dist_r  = (right_ticks / ticks_per_rev) * wheel_circ_mm
    odom_d  = (dist_l + dist_r) / 2.0
    max_abs = max(abs(dist_l), abs(dist_r))
    sym     = (min(abs(dist_l), abs(dist_r)) / max_abs) if max_abs > 1e-6 else 1.0

    print(f"\n  ── 第 {run_idx} 次 {'─' * 40}")
    print(f"  目标: {target_abs:.1f} mm  方向: {direction}  speed: {speed}%")
    print(f"  耗时: {elapsed:.2f} s  {'⚠ 超时停车' if timed_out else '✓ 正常停车'}")
    print(f"  左轮: {left_ticks:+6d} ticks ≈ {dist_l:+7.1f} mm")
    print(f"  右轮: {right_ticks:+6d} ticks ≈ {dist_r:+7.1f} mm")
    print(f"  里程计估算: {odom_d:+7.1f} mm   对称比: {sym:.3f}")

    if timed_out:
        print("  ⚠ 超时：里程未达目标，可能 EMF 噪声严重 → 先跑 encoder_diag.py")
    if sym < 0.90:
        # 走得少的那侧是"偏弱"侧：abs 更小 = 距离更短 = 偏弱
        weak = "右轮" if abs(dist_l) > abs(dist_r) else "左轮"
        print(f"  ⚠ 左右不对称 {sym:.2%}：{weak}偏弱，"
              "可在 .env 调 CHASSIS_LEFT/RIGHT_SCALE")

    return dict(target_mm=target_mm, left_ticks=left_ticks, right_ticks=right_ticks,
                dist_left=dist_l, dist_right=dist_r, odom_dist=odom_d,
                symmetry=sym, elapsed_s=elapsed, timed_out=timed_out)


# ── 主流程 ─────────────────────────────────────────────────────────────────────

def main() -> int:
    # 导入放在函数内，避免模块级 env 读取干扰 EncoderConfig 默认值
    try:
        from devices.encoder import Encoder, EncoderConfig
        from devices.chassis import Chassis, DEFAULT_CONFIG
        from odometry import Odometry, OdometryConfig
    except ImportError as e:
        print(f"❌ 导入失败：{e}")
        print("   请在 platform/ 目录下运行，且已激活 .venv")
        return 1

    print("\n正在初始化硬件...")

    # 用顶部常量显式构造 EncoderConfig，完全不读 env
    enc_cfg = EncoderConfig(
        left_a            = LEFT_A,
        left_b            = LEFT_B,
        right_a           = RIGHT_A,
        right_b           = RIGHT_B,
        lines_per_rev     = LINES_PER_REV,
        left_invert       = LEFT_INVERT,
        right_invert      = RIGHT_INVERT,
        debounce_reads    = DEBOUNCE_READS,
        debounce_delay_us = DEBOUNCE_US,
    )
    encoder = Encoder(config=enc_cfg)
    if not encoder.start():
        print("⚠ 编码器模拟模式（lgpio 不可用），数据将全为 0")
    else:
        print(f"✓ 编码器  左L=A{LEFT_A}/B{LEFT_B}  右R=A{RIGHT_A}/B{RIGHT_B}"
              f"  ticks/rev={encoder.ticks_per_rev}"
              f"  极性=左{'翻' if LEFT_INVERT else '正'}右{'翻' if RIGHT_INVERT else '正'}"
              f"  去抖={DEBOUNCE_READS}×{DEBOUNCE_US}μs")

    # 用顶部常量显式构造 OdometryConfig
    odom_cfg = OdometryConfig(
        wheel_radius_mm = WHEEL_RADIUS_MM,
        wheel_base_mm   = WHEEL_BASE_MM,
    )
    odometry = Odometry(encoder=encoder, imu=None, config=odom_cfg)
    odometry.start()

    circ_mm       = 2.0 * math.pi * WHEEL_RADIUS_MM
    ticks_per_rev = encoder.ticks_per_rev
    print(f"✓ 里程计  轮径={WHEEL_RADIUS_MM}mm  轮距={WHEEL_BASE_MM}mm"
          f"  周长={circ_mm:.2f}mm")

    # 手推模式
    if TICK_ONLY_MODE:
        try:
            _tick_only(encoder, ticks_per_rev, circ_mm)
        finally:
            odometry.stop()
            encoder.stop()
        return 0

    chassis = Chassis(DEFAULT_CONFIG)
    print(f"✓ 底盘    {'模拟' if chassis.is_simulation else 'GPIO 真实'}")

    _sep("═")
    print(f"测试参数：{TARGET_DISTANCE_MM:+.0f}mm  speed={MOTOR_SPEED}%"
          f"  timeout={TIMEOUT_S}s  重复={REPEATS}次")
    _sep("═")

    results = []
    try:
        for i in range(1, REPEATS + 1):
            if i > 1:
                time.sleep(2.0)
                input("  重新摆好机器人后按 Enter 继续... ")
            results.append(_run_trip(
                chassis, encoder, odometry,
                TARGET_DISTANCE_MM, MOTOR_SPEED, TIMEOUT_S,
                ticks_per_rev, circ_mm, i,
            ))
    except KeyboardInterrupt:
        print("\n已取消。")
        chassis.stop()
    finally:
        odometry.stop()
        encoder.stop()
        chassis.cleanup()

    if not results:
        return 130

    # 汇总
    n           = len(results)
    target_abs  = abs(TARGET_DISTANCE_MM)
    odom_avg    = sum(abs(r["odom_dist"]) for r in results) / n
    odom_std    = math.sqrt(sum((abs(r["odom_dist"]) - odom_avg) ** 2
                                for r in results) / n) if n > 1 else 0.0
    sym_avg     = sum(r["symmetry"] for r in results) / n
    n_timeout   = sum(1 for r in results if r["timed_out"])

    _sep("═")
    print(f"\n=== 汇总（{n} 次）===")
    print(f"  目标:       {target_abs:.1f} mm")
    print(f"  里程计均值: {odom_avg:.1f} mm   σ={odom_std:.1f} mm")
    err = odom_avg - target_abs
    print(f"  系统误差:   {err:+.1f} mm ({err/target_abs*100:+.1f}%)")
    print(f"  对称比均值: {sym_avg:.3f}")
    if n_timeout:
        print(f"  ⚠ {n_timeout}/{n} 次超时")

    # 人工测量与校准
    _sep()
    print("\n用卷尺量实际行驶距离，回填后自动计算校准建议")
    if REPEATS > 1:
        print(f"  请输入 {REPEATS} 次的算术平均值（mm）")
    try:
        raw = input("实测距离（mm/cm/m，直接回车跳过）: ").strip()
        if not raw:
            return 0
        actual     = abs(_parse_mm(raw))
        err_mm     = actual - odom_avg
        err_pct    = err_mm / max(odom_avg, 1e-9) * 100.0

        _sep()
        print(f"\n=== 校准结果 ===")
        print(f"  里程计: {odom_avg:.1f} mm    卷尺: {actual:.1f} mm")
        print(f"  偏差:   {err_mm:+.1f} mm ({err_pct:+.1f}%)"
              f"  → 里程计{'高估' if err_mm < 0 else '低估'}了实际距离")

        if abs(err_pct) < 3.0:
            print("  ✅ 精度良好（<3%），无需调整")
        elif abs(err_pct) < 10.0:
            print("  ⚠ 建议校准")
        else:
            print("  ❌ 误差过大，优先排查 EMF 噪声（encoder_diag.py）")

        if actual > 1e-3 and odom_avg > 1e-3:
            # 里程计低估 → 每 tick 对应距离偏小 → 需减小 ticks_per_rev → 减小 LINES_PER_REV
            # 里程计高估 → 每 tick 对应距离偏大 → 需增大 ticks_per_rev → 增大 LINES_PER_REV
            sugg = LINES_PER_REV * (odom_avg / actual)
            print(f"\n  校准公式: 新 LINES_PER_REV = {LINES_PER_REV} × ({odom_avg:.1f}/{actual:.1f})")
            print(f"  建议将顶部 LINES_PER_REV 改为: {sugg:.0f}")

        left_avg  = sum(abs(r["dist_left"])  for r in results) / n
        right_avg = sum(abs(r["dist_right"]) for r in results) / n
        if left_avg > 1e-3 and right_avg > 1e-3:
            ratio = right_avg / left_avg  # >1 说明右轮跑得更多（右快左慢）
            print(f"\n  左右对称: L={left_avg:.1f}mm  R={right_avg:.1f}mm  比={ratio:.3f}")
            if ratio < 0.93:
                # 右轮跑得少 → 右慢左快 → 调慢左轮
                print(f"  右轮偏慢 → .env: CHASSIS_LEFT_SCALE={ratio:.2f}")
            elif ratio > 1.07:
                # 右轮跑得多 → 右快左慢 → 调慢右轮
                print(f"  左轮偏慢 → .env: CHASSIS_RIGHT_SCALE={1/ratio:.2f}")
            else:
                print("  ✅ 对称性良好")

    except (KeyboardInterrupt, ValueError):
        print("\n已跳过校准。")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
