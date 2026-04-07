#!/usr/bin/env python3
"""
编码器电容效果验证脚本
======================
直接采集后轮编码器原始脉冲时间戳，分析 EMF 噪声 burst 是否减少，
验证焊在后电机电源两端的 100nF 贴片电容是否起效。

⚠️  运行前必须停止 Platform 服务，本脚本需独占电机 + 编码器 GPIO。
    sudo systemctl stop goudan
    或直接 Ctrl+C 停掉正在运行的 main.py

测试原理
--------
  EMF 噪声特征：电机换向瞬间，反电动势耦合进编码器线，
  在极短时间内（<2ms）产生多个假脉冲（物理上不可能是真实运动）。
  加电容后：motor 端高频尖峰被电容短路 → 辐射减弱 → 假脉冲减少。

  本脚本同时测试两相（A+B），仅后轮（M3 左后 + M4 右后）：
    左后编码器：A=GPIO23，B=GPIO16
    右后编码器：A=GPIO14，B=GPIO18

测试阶段
--------
  Phase 0（静止 3s）：电机 OFF，统计本底噪声（应为 0 burst）
  Phase 1（运行 10s）：电机以固定速度运行，统计 burst 事件

输出指标
--------
  burst_count   — burst 事件次数（每个 burst = ≥3 个间隔 < 2ms 的连续脉冲）
  noise_frac    — 噪声脉冲占比（%）
  clean_rate    — 有效脉冲频率（Hz），与理论值对比
  verdict       — ✅ 有效 / ⚠ 部分改善 / ❌ 效果不明显

用法
----
  # 默认：speed=50, 运行 10s
  python3 platform/encoder_cap_test.py

  # 自定义速度和时长
  python3 platform/encoder_cap_test.py --speed 60 --duration 15

  # 仅做静止本底测试（不跑电机，只看环境噪声）
  python3 platform/encoder_cap_test.py --static-only
"""

import argparse
import sys
import time
import threading
import collections

# ── 硬件引脚（BCM 编号） ──────────────────────────────────────────────
# 后轮编码器（A + B 两相均监测，与 encoder.py 一致）
_ENC_PINS = {
    "左后-A": 23,
    "左后-B": 16,
    "右后-A": 14,
    "右后-B": 18,
}

# 后轮电机驱动（直接 IN-PWM 模式，与 chassis.py DEFAULT_CONFIG 一致）
_M3_IN1, _M3_IN2 = 5,  6    # M3 左后
_M4_IN1, _M4_IN2 = 22, 9    # M4 右后

# ── 噪声判断阈值 ──────────────────────────────────────────────────────
_PWM_FREQ_HZ     = 1000     # 与 Motor.PWM_FREQ 一致
_BURST_THRESH_US = 2000     # 两相邻脉冲间隔 < 2ms → 噪声嫌疑
_BURST_MIN_SEQS  = 3        # 连续 ≥3 个"短间隔"才算一次 burst 事件

# 理论最小合法脉冲间隔（speed=80% 时，1:90 减速比，500线，4倍频）
# 实测电机约 800RPM_no_load → 80% ≈ 640RPM → 640/90≈7.1RPM_wheel ≈ 0.12rev/s
# 2000pulses/rev * 0.12 ≈ 236 pulse/s → 最短间隔 ≈ 4.2ms
# 保守取 2ms 作为阈值
_GEARBOX       = 90
_ENCODER_LINES = 500
_QUAD          = 4

DIVIDER = "─" * 65


# ── 脉冲采集 ─────────────────────────────────────────────────────────

class PulseCapture:
    """用 lgpio callback 采集编码器脉冲时间戳（单引脚）。"""

    def __init__(self):
        self.timestamps: list[int] = []   # µs，lgpio tick
        self._lock = threading.Lock()

    def callback(self, chip, gpio, level, tick):
        with self._lock:
            self.timestamps.append(tick)

    def snapshot(self) -> list[int]:
        with self._lock:
            return list(self.timestamps)

    def clear(self):
        with self._lock:
            self.timestamps.clear()


# ── 噪声分析 ─────────────────────────────────────────────────────────

def analyze(timestamps: list[int], label: str, duration_s: float,
            motor_speed: int) -> dict:
    """
    分析一组时间戳，返回 burst 统计。

    burst 定义：≥ _BURST_MIN_SEQS 个连续脉冲对，每对间隔 < _BURST_THRESH_US。
    """
    n = len(timestamps)
    if n < 2:
        return {
            "label": label, "total": n, "burst_events": 0,
            "burst_pulses": 0, "noise_frac": 0.0,
            "mean_interval_ms": 0.0, "min_interval_ms": 0.0,
            "effective_hz": 0.0,
        }

    intervals = [timestamps[i+1] - timestamps[i] for i in range(n - 1)]
    min_iv = min(intervals)

    # 检测 burst：连续 short_interval_count >= _BURST_MIN_SEQS
    is_short = [iv < _BURST_THRESH_US for iv in intervals]
    burst_events = 0
    burst_pulse_flags = [False] * n   # 标记哪些脉冲属于 burst

    i = 0
    while i < len(is_short):
        if is_short[i]:
            # 统计这段连续短间隔
            j = i
            while j < len(is_short) and is_short[j]:
                j += 1
            seq_len = j - i   # 连续短间隔数
            if seq_len >= _BURST_MIN_SEQS - 1:
                burst_events += 1
                # 标记涉及的脉冲（seq_len 个间隔 = seq_len+1 个脉冲）
                for k in range(i, j + 1):
                    if k < n:
                        burst_pulse_flags[k] = True
            i = j
        else:
            i += 1

    burst_pulses = sum(burst_pulse_flags)
    noise_frac   = burst_pulses / n if n > 0 else 0.0
    mean_iv_ms   = (sum(intervals) / len(intervals)) / 1000.0
    min_iv_ms    = min_iv / 1000.0

    # 有效脉冲率（扣除 burst 脉冲后的净率）
    clean_pulses = n - burst_pulses
    effective_hz = clean_pulses / duration_s if duration_s > 0 else 0.0

    # 理论脉冲率（粗估）
    # 这里不知道实际 RPM，只能用 burst 比例来判断效果
    return {
        "label":          label,
        "total":          n,
        "burst_events":   burst_events,
        "burst_pulses":   burst_pulses,
        "noise_frac":     noise_frac,
        "mean_interval_ms": mean_iv_ms,
        "min_interval_ms":  min_iv_ms,
        "effective_hz":   effective_hz,
    }


def print_result(r: dict, phase: str) -> None:
    n    = r["total"]
    be   = r["burst_events"]
    bp   = r["burst_pulses"]
    nf   = r["noise_frac"] * 100
    mean = r["mean_interval_ms"]
    mn   = r["min_interval_ms"]
    hz   = r["effective_hz"]

    print(f"\n  [{phase}] {r['label']}")
    print(f"    总脉冲数     : {n}")
    print(f"    burst 事件   : {be} 次")
    print(f"    噪声脉冲     : {bp} 个（占 {nf:.1f}%）")
    print(f"    均值间隔     : {mean:.2f} ms")
    print(f"    最小间隔     : {mn:.3f} ms")
    print(f"    有效脉冲率   : {hz:.1f} Hz（已剔除 burst 脉冲）")


def verdict(phase0_results: list[dict], phase1_results: list[dict]) -> None:
    """综合两个阶段的结果给出结论。"""
    print(f"\n{DIVIDER}")
    print("  综合诊断结论")
    print(DIVIDER)

    # 静止阶段：本应无任何脉冲
    p0_total  = sum(r["total"] for r in phase0_results)
    p0_bursts = sum(r["burst_events"] for r in phase0_results)
    print(f"\n  Phase 0（静止本底）")
    if p0_total == 0:
        print("  ✅ 完全安静，无任何脉冲（接线正常，无外部干扰）")
    elif p0_bursts == 0 and p0_total < 10:
        print(f"  ✅ 极少脉冲（{p0_total} 个，无 burst），接近理想")
    elif p0_bursts > 0:
        print(f"  ⚠️  静止时仍有 {p0_bursts} 次 burst（共 {p0_total} 个脉冲）")
        print("     → 可能存在外部干扰或 GPIO 上拉电阻缺失")
    else:
        print(f"  ℹ️  静止时有 {p0_total} 个散脉冲（无 burst），可接受")

    # 运行阶段：关键指标
    p1_total  = sum(r["total"] for r in phase1_results)
    p1_bursts = sum(r["burst_events"] for r in phase1_results)
    p1_bp     = sum(r["burst_pulses"] for r in phase1_results)
    p1_nf     = p1_bp / p1_total * 100 if p1_total > 0 else 0.0

    print(f"\n  Phase 1（电机运行）")
    print(f"    总脉冲 {p1_total}，burst 事件 {p1_bursts} 次，噪声占比 {p1_nf:.1f}%")

    if p1_bursts == 0:
        print("\n  ✅✅ 电容效果显著：运行期间 0 次 burst，EMF 噪声已被有效抑制！")
        print("     里程计精度应大幅提升，可将 LINES_PER_REV 恢复至 500 后重新标定。")
    elif p1_bursts <= 5 and p1_nf < 5.0:
        print(f"\n  ✅  电容有效：burst 极少（{p1_bursts} 次），噪声占比低（{p1_nf:.1f}%）")
        print("     建议：可尝试将 LINES_PER_REV 逐步从 125 上调（先试 200，再试 300）。")
    elif p1_bursts <= 30 and p1_nf < 30.0:
        print(f"\n  ⚠️  部分改善：burst 减少，但仍有 {p1_bursts} 次（噪声 {p1_nf:.1f}%）")
        print("     电机端电容有一定效果，但不够彻底。建议追加：")
        print("     → 在编码器信号线（A/B 相）对 GND 各焊一颗 100nF（需 4 颗）")
        print("     → 焊接位置：靠近树莓派 GPIO 引脚端（信号接收端）")
    else:
        print(f"\n  ❌  效果不明显：仍有 {p1_bursts} 次 burst（噪声 {p1_nf:.1f}%）")
        print("     motor 端电容对本机的 EMF 耦合路径改善有限，需进一步处理：")
        print("     1. 在编码器 A/B 信号线对 GND 各焊 100nF（4 颗，靠近 GPIO 端）")
        print("     2. 检查编码器线与电机线是否平行捆绑（分开走线可减少耦合）")
        print("     3. 确认电容已焊牢，无虚焊（用万用表量电机两端容值应约 100nF）")

    print()
    print("  ── 下一步建议 ──────────────────────────────────────────────")
    print("  使用 encoder_noise_diag.py 做多速度比值测试（需真实距离测量）")
    print("  以确认 LINES_PER_REV 的最终校正值。")
    print()


# ── 主程序 ────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="编码器电容效果验证（standalone）")
    parser.add_argument("--speed",       type=int, default=50,
                        help="电机 PWM 占空比 0-100（默认 50）")
    parser.add_argument("--duration",    type=float, default=10.0,
                        help="Phase 1 电机运行时长（秒，默认 10）")
    parser.add_argument("--static-only", action="store_true",
                        help="仅做静止本底测试，不运行电机")
    parser.add_argument("--use-api",     action="store_true",
                        help="通过 Platform HTTP API 控制电机（Platform 服务需已启动，"
                             "编码器引脚由本脚本用 lgpio 直接监测）")
    parser.add_argument("--base-url",    default="http://localhost:8001",
                        help="Platform 服务地址（--use-api 时有效，默认 http://localhost:8001）")
    parser.add_argument("--chip",        type=int, default=0,
                        help="lgpio chip 编号（RPi5 默认 0）")
    args = parser.parse_args()

    speed    = max(0, min(100, args.speed))
    duration = max(3.0, args.duration)

    print(f"\n{DIVIDER}")
    print("  编码器电容效果验证")
    print(DIVIDER)
    use_api = args.use_api

    if args.static_only:
        print(f"  模式     : 仅静止本底（不运行电机）")
    else:
        mode_str = f"API ({args.base_url})" if use_api else "直接 GPIO (lgpio tx_pwm)"
        print(f"  电机速度 : {speed}%  |  运行时长 : {duration:.0f}s  |  电机控制 : {mode_str}")
    print(f"  编码器引脚 : {_ENC_PINS}")
    print(f"  burst 阈值 : < {_BURST_THRESH_US/1000:.0f}ms 内 ≥{_BURST_MIN_SEQS} 个连续脉冲")
    print(DIVIDER)

    # ── 载入 lgpio ────────────────────────────────────────────────────
    try:
        import lgpio
    except ImportError:
        print("\n❌ lgpio 未安装。请执行：sudo apt install -y python3-lgpio")
        sys.exit(1)

    chip = lgpio.gpiochip_open(args.chip)
    if chip < 0:
        print(f"\n❌ 无法打开 gpiochip{args.chip}（错误码 {chip}）")
        print("   确认以 root 或 gpio 组身份运行（sudo python3 ...）")
        sys.exit(1)

    # ── 设置编码器输入引脚 + 回调 ────────────────────────────────────
    captures: dict[str, PulseCapture] = {}
    cb_handles = []

    def _make_cb(cap):
        def _cb(c, g, level, tick):
            cap.callback(c, g, level, tick)
        return _cb

    for name, pin in _ENC_PINS.items():
        ret = lgpio.gpio_claim_input(chip, pin, lgpio.SET_PULL_UP)
        if ret < 0:
            print(f"\n❌ GPIO{pin}（{name}）被占用（错误码 {ret}）。")
            print("   请先停止 Platform 服务（Ctrl+C 或 systemctl stop goudan）")
            lgpio.gpiochip_close(chip)
            sys.exit(1)
        cap = PulseCapture()
        captures[name] = cap
        handle = lgpio.callback(chip, pin, lgpio.BOTH_EDGES, _make_cb(cap))
        cb_handles.append(handle)

    # ── 电机控制函数（直接 GPIO 或 Platform API 二选一）────────────────
    motor_pins = [_M3_IN1, _M3_IN2, _M4_IN1, _M4_IN2]

    if use_api:
        # API 模式：Platform 服务负责电机，本脚本只做编码器监测
        import urllib.request, json as _json

        def _api_post(path: str, payload: dict) -> dict:
            url  = args.base_url.rstrip("/") + path
            data = _json.dumps(payload).encode()
            req  = urllib.request.Request(url, data=data, method="POST",
                                          headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=5) as r:
                return _json.loads(r.read())

        def motors_forward(pct: int) -> None:
            _api_post("/motor/command", {"command": "forward", "speed": pct})

        def motors_stop() -> None:
            try:
                _api_post("/motor/command", {"command": "stop"}, )
            except Exception:
                pass

        def _check(ret: int, op: str) -> None:
            pass   # API 模式下无 lgpio 返回值需要检查

        print("  ℹ️  API 模式：编码器回调由本脚本独立监测（需 Platform 服务正在运行）")

    else:
        # 直接 GPIO 模式：独占电机引脚
        if not args.static_only:
            for pin in motor_pins:
                ret = lgpio.gpio_claim_output(chip, pin, 0)
                if ret < 0:
                    print(f"\n❌ 电机引脚 GPIO{pin} 被占用（错误码 {ret}）。")
                    print("   请先停止 Platform 服务，或改用 --use-api 模式。")
                    for h in cb_handles:
                        h.cancel()
                    lgpio.gpiochip_close(chip)
                    sys.exit(1)

        def _check(ret: int, op: str) -> None:
            if ret < 0:
                raise RuntimeError(f"{op} 失败（lgpio 错误码 {ret}：{lgpio.error_text(ret)}）")

        def motors_forward(pct: int) -> None:
            """两后轮同速正转（直接 IN-PWM）。"""
            dc = max(0, min(100, pct))
            _check(lgpio.gpio_write(chip, _M3_IN2, 0), "M3_IN2 LOW")
            _check(lgpio.gpio_write(chip, _M4_IN2, 0), "M4_IN2 LOW")
            _check(lgpio.tx_pwm(chip, _M3_IN1, _PWM_FREQ_HZ, dc), f"M3 tx_pwm {dc}%")
            _check(lgpio.tx_pwm(chip, _M4_IN1, _PWM_FREQ_HZ, dc), f"M4 tx_pwm {dc}%")

        def motors_stop() -> None:
            for pin in [_M3_IN1, _M4_IN1]:
                lgpio.tx_pwm(chip, pin, _PWM_FREQ_HZ, 0)
            for pin in motor_pins:
                lgpio.gpio_write(chip, pin, 0)

    # ── Phase 0：静止本底（3s） ───────────────────────────────────────
    STATIC_DUR = 3.0
    print(f"\n  [Phase 0] 静止本底采集（{STATIC_DUR:.0f}s，电机 OFF）...")
    print("  请保持机器人静止，不要触碰。")

    for cap in captures.values():
        cap.clear()

    t_start = time.monotonic()
    while time.monotonic() - t_start < STATIC_DUR:
        remaining = STATIC_DUR - (time.monotonic() - t_start)
        print(f"    剩余 {remaining:.1f}s...", end="\r")
        time.sleep(0.5)
    print()

    phase0_results = []
    for name, cap in captures.items():
        ts = cap.snapshot()
        r  = analyze(ts, name, STATIC_DUR, 0)
        print_result(r, "Phase 0")
        phase0_results.append(r)

    if args.static_only:
        verdict(phase0_results, [])
        for h in cb_handles:
            h.cancel()
        lgpio.gpiochip_close(chip)
        return

    # ── Phase 1：电机运行（duration s） ──────────────────────────────
    print(f"\n  [Phase 1] 电机运行采集（speed={speed}%，时长={duration:.0f}s）...")
    print("  ⚠️  机器车将向前行驶，请确保前方有足够空间（>50cm）！")
    print()
    for i in range(3, 0, -1):
        print(f"  {i}...", end="\r")
        time.sleep(1.0)
    print("  启动！      ")

    for cap in captures.values():
        cap.clear()

    try:
        motors_forward(speed)
    except RuntimeError as e:
        print(f"\n  ❌ 电机启动失败：{e}")
        print("  排查：电机引脚是否被其他进程占用？Platform 服务是否已停止？")
        for h in cb_handles:
            h.cancel()
        lgpio.gpiochip_close(chip)
        sys.exit(1)

    # ── 早期脉冲验证（前 2s 检查编码器是否有任何响应）─────────────
    print("  验证编码器响应（2s）...", end="", flush=True)
    time.sleep(2.0)
    early_total = sum(len(cap.snapshot()) for cap in captures.values())
    if early_total == 0:
        motors_stop()
        print("\n\n  ❌ 电机运行 2s 后编码器仍为 0 脉冲，终止测试。")
        print()
        print("  可能原因及排查：")
        print("  A) 电机未转动：")
        print("     - 用手轻推车轮，若阻力正常说明 PWM 未到电机驱动板")
        print("     - 改用 Platform API 驱动：--use-api 模式（见下方）")
        print()
        print("  B) 编码器回调未触发：")
        print("     - 用 Platform 服务跑一次 /motor/drive，确认编码器线路正常")
        print("     - 尝试手动转动车轮，看 Phase 0 是否能采到脉冲")
        print()
        print("  建议：先跑 --use-api 模式绕过直接 GPIO 控制：")
        print(f"    确保 Platform 服务已启动，然后：")
        print(f"    python3 platform/encoder_cap_test.py --use-api --speed {speed}")
        for h in cb_handles:
            h.cancel()
        lgpio.gpiochip_close(chip)
        sys.exit(1)
    print(f" 已捕获 {early_total} 个脉冲 ✅")

    t_start = time.monotonic() - 2.0   # 把早期 2s 也计入总时长

    while time.monotonic() - t_start < duration:
        remaining = duration - (time.monotonic() - t_start)
        # 实时显示各引脚脉冲计数
        counts = {name: len(cap.snapshot()) for name, cap in captures.items()}
        count_str = "  ".join(f"{n}:{c}" for n, c in counts.items())
        print(f"    剩余 {remaining:5.1f}s  |  {count_str}", end="\r")
        time.sleep(0.2)

    motors_stop()
    print("\n  电机已停止。")
    time.sleep(0.3)   # 等余震脉冲散尽

    phase1_results = []
    for name, cap in captures.items():
        ts = cap.snapshot()
        r  = analyze(ts, name, duration, speed)
        print_result(r, "Phase 1")
        phase1_results.append(r)

    # ── 综合结论 ──────────────────────────────────────────────────────
    verdict(phase0_results, phase1_results)

    # ── 清理 ─────────────────────────────────────────────────────────
    for h in cb_handles:
        h.cancel()
    lgpio.gpiochip_close(chip)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n\n已中断。")
        sys.exit(0)
