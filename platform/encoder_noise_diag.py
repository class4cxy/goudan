#!/usr/bin/env python3
"""
编码器噪声诊断脚本
==================
通过在不同速度下跑同样距离，判断编码器误差是"纯标定问题"还是"EMF噪声问题"。

原理
----
  纯标定误差：encoder/actual 比值与速度无关（所有速度下比值相同）
  EMF 噪声  ：速度越高，encoder 读数越少（比值随速度下降）

用法
----
  python3 encoder_noise_diag.py --base-url http://localhost:8001

测试前准备
----------
  1. 在地板上贴一条起点胶带
  2. 每次车停后用尺子量实际走了多少
  3. 如果某个速度的测试 timeout，请如实输入 t（表示超时）
"""

import argparse
import json
import urllib.request
import sys

TEST_DISTANCE_MM = 300.0   # 固定测试距离
TEST_SPEEDS      = [20, 25, 30, 35]


def post_json(url: str, payload: dict, timeout_s: float = 120.0) -> dict:
    data = json.dumps(payload).encode()
    req  = urllib.request.Request(url, data=data, method="POST",
                                  headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout_s) as r:
        return json.loads(r.read())


def force_stop(base_url: str) -> None:
    try:
        post_json(f"{base_url}/motor/command", {"command": "stop"}, timeout_s=2.0)
    except Exception:
        pass


def run_one(base_url: str, speed: int, target_mm: float) -> dict:
    timeout_s = 90.0   # 始终用宽松超时，避免超时误判
    res = post_json(
        f"{base_url}/motor/drive",
        {"distance_mm": target_mm, "speed": speed, "timeout_s": timeout_s},
        timeout_s=timeout_s + 10,
    )
    return res


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://localhost:8001")
    parser.add_argument("--distance-mm", type=float, default=TEST_DISTANCE_MM)
    parser.add_argument(
        "--speeds", nargs="+", type=int, default=TEST_SPEEDS,
        help="要测试的速度列表，默认 20 25 30 35"
    )
    args = parser.parse_args()

    results = []

    print("=" * 60)
    print(" 编码器噪声诊断")
    print(f" 每轮固定行驶目标: {args.distance_mm:.0f}mm")
    print(f" 测试速度序列: {args.speeds}")
    print("=" * 60)
    print("每轮测试后请用尺子量实际距离。")
    print("如果超时（车还没停就到 timeout），请输入 t。")
    print()

    for speed in args.speeds:
        input(f"▶ 准备好后按 Enter 开始 speed={speed} 的测试（先回到起点）...")
        print(f"  执行中：target={args.distance_mm:.0f}mm  speed={speed}  timeout=90s")

        try:
            res = run_one(args.base_url, speed, args.distance_mm)
        except KeyboardInterrupt:
            force_stop(args.base_url)
            print("\n已中断。")
            sys.exit(0)
        except Exception as e:
            force_stop(args.base_url)
            print(f"  ❌ 接口错误: {e}")
            continue

        enc_mm      = float(res.get("traveled_mm", 0.0))
        timed_out   = bool(res.get("timeout", False))
        sensor      = res.get("sensor", "")

        if timed_out:
            print(f"  ⚠ timeout！encoder 仅读到 {enc_mm:.1f}mm（未到达目标）")
        else:
            print(f"  encoder 读数: {enc_mm:.1f}mm  (sensor={sensor})")

        raw = input("  请输入尺子实测距离（mm/cm/m，输入 t 表示超时/车没动）: ").strip().lower()

        if raw in ("t", "timeout", "超时"):
            print("  → 记录为：超时 / 车基本没动")
            results.append({
                "speed": speed,
                "enc_mm": enc_mm,
                "actual_mm": None,
                "timed_out": True,
            })
        else:
            try:
                if raw.endswith("m") and not raw.endswith("mm") and not raw.endswith("cm"):
                    actual_mm = float(raw[:-1]) * 1000
                elif raw.endswith("cm"):
                    actual_mm = float(raw[:-2]) * 10
                elif raw.endswith("mm"):
                    actual_mm = float(raw[:-2])
                else:
                    actual_mm = float(raw)
            except ValueError:
                print("  格式无法解析，跳过。")
                continue

            ratio = enc_mm / actual_mm if actual_mm > 0 else 0
            print(f"  → encoder/actual = {enc_mm:.1f}/{actual_mm:.1f} = {ratio:.2%}")
            results.append({
                "speed": speed,
                "enc_mm": enc_mm,
                "actual_mm": actual_mm,
                "timed_out": timed_out,
            })

        print()

    if not results:
        print("没有有效数据。")
        return

    # ── 汇总分析 ──────────────────────────────────────────────────
    print("=" * 60)
    print(" 汇总结果")
    print("=" * 60)
    print(f"{'speed':>6} {'enc_mm':>8} {'actual_mm':>10} {'比值':>8}  状态")
    print("-" * 60)

    valid = []
    for r in results:
        if r["timed_out"] or r["actual_mm"] is None:
            print(f"{r['speed']:>6} {r['enc_mm']:>8.1f} {'—':>10} {'—':>8}  ⚠ 超时")
        else:
            ratio = r["enc_mm"] / r["actual_mm"] if r["actual_mm"] > 0 else 0
            print(f"{r['speed']:>6} {r['enc_mm']:>8.1f} {r['actual_mm']:>10.1f} {ratio:>8.2%}  {'timeout' if r['timed_out'] else 'OK'}")
            valid.append((r["speed"], ratio))

    print()

    # 结论判断
    if len(valid) < 2:
        print("有效数据点不足，无法判断。建议至少跑完 3 个速度。")
        return

    ratios = [v[1] for v in valid]
    ratio_min = min(ratios)
    ratio_max = max(ratios)
    variation = ratio_max - ratio_min   # 比值波动

    print("── 诊断结论 ──────────────────────────────────────────────")
    if variation < 0.10:   # 10% 以内认为是纯标定误差
        avg = sum(ratios) / len(ratios)
        new_lines = round(500 * avg)
        print(f"✅ 比值在各速度下基本稳定（波动 {variation:.1%}）")
        print(f"   → 这是【纯标定误差】，不是 EMF 噪声")
        print(f"   → 编码器 encoder/actual ≈ {avg:.2%}，校正方式：")
        print(f"      ENCODER_LINES_PER_REV = {new_lines}  (当前 500)")
        print(f"      写入 .env 并重启 platform 即可")
    elif variation < 0.25:
        print(f"⚠  比值有明显速度依赖性（波动 {variation:.1%}），轻度 EMF 噪声")
        print(f"   低速（speed≤25）基本可用，高速（speed≥35）编码器不可靠")
        print(f"   建议：")
        print(f"   1. 在 .env 中设置 CLOSED_LOOP_MAX_SPEED=25")
        print(f"   2. 再焊电容彻底解决（见下方硬件方案）")
    else:
        print(f"❌ 比值随速度严重下降（波动 {variation:.1%}），确认为 EMF 噪声")
        print(f"   速度越高，EMF 越强，正交解码相消越严重，里程计完全失效")
        print()
        print("── 硬件解决方案 ───────────────────────────────────────")
        print("  原因：电机线圈产生的反电动势（EMF）耦合进编码器 A/B 信号线")
        print("        造成随机跳变（3-5ms 宽），去抖无法过滤")
        print()
        print("  方案 1（推荐，最简单）：加滤波电容")
        print("    每条编码器信号线对 GND 各焊一颗 100nF 陶瓷贴片电容（0603/0805）")
        print("    共需 4 颗：左A/左B/右A/右B 各一颗")
        print("    焊接位置：尽量靠近树莓派 GPIO 引脚端（信号接收端）")
        print("    效果：把噪声跳变从 3-5ms 压缩到 <0.1ms，软件去抖即可过滤")
        print("    购买：淘宝搜「贴片电容 100nF 0603」约 ¥5 一盒（100颗）")
        print()
        print("  方案 2：编码器线与电机线分开走线")
        print("    如果编码器和电机共用同一线束，把编码器线单独拉出来")
        print("    避免平行走线，减少 EMF 耦合距离")
        print()
        print("  方案 3（更彻底）：带磁编码器的直流减速电机")
        print("    更换为光电耦合隔离编码器输出的减速电机，噪声从源头隔离")

    # 有超时记录
    timeouts = [r for r in results if r["timed_out"]]
    if timeouts:
        print()
        print(f"⚠  speed={[r['speed'] for r in timeouts]} 发生 timeout，")
        print(f"   说明在该速度下编码器读数极低，完全不可信")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n已中断。")
