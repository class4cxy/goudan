#!/usr/bin/env python3
"""
距离单项测试入口（人工量尺校准）
================================

流程：
1) 输入目标距离（支持 1000 / 1000mm / 100cm / 1m，负数表示后退）
2) 调用 /motor/drive 执行
3) 你用尺子测“实际走了多少”，回填结果
4) 脚本输出误差并给出 ENCODER_LINES_PER_REV 建议值
"""

from __future__ import annotations

import argparse
import json
import os
import re
import urllib.request


def post_json(url: str, payload: dict, timeout_s: float = 60.0) -> dict:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url=url,
        data=data,
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout_s) as resp:
        return json.loads(resp.read().decode("utf-8"))


def force_stop(base_url: str) -> None:
    try:
        post_json(f"{base_url}/motor/command", {"command": "stop"}, timeout_s=2.0)
    except Exception:
        pass


def parse_distance_mm(text: str) -> float:
    s = text.strip().lower().replace(" ", "")
    m = re.fullmatch(r"([+-]?\d+(?:\.\d+)?)(mm|cm|m)?", s)
    if not m:
        raise ValueError("格式错误，示例：1000 / 1000mm / 100cm / 1m / -300mm")
    value = float(m.group(1))
    unit = m.group(2) or "mm"
    if unit == "mm":
        return value
    if unit == "cm":
        return value * 10.0
    return value * 1000.0


def main() -> int:
    parser = argparse.ArgumentParser(description="距离单项测试入口")
    parser.add_argument("--base-url", default="http://localhost:8001")
    parser.add_argument("--speed", type=int, default=35)
    parser.add_argument("--timeout-s", type=float, default=25.0)
    parser.add_argument(
        "--lines-per-rev",
        type=float,
        default=float(os.environ.get("ENCODER_LINES_PER_REV", "500")),
    )
    parser.add_argument("--target-mm", type=float, default=None)
    args = parser.parse_args()

    speed = max(0, min(100, args.speed))

    try:
        if args.target_mm is None:
            raw = input("请输入目标距离（如 1m / 100cm / 1000mm，负数=后退）: ")
            target_mm = parse_distance_mm(raw)
        else:
            target_mm = float(args.target_mm)

        print(f"\n执行中：target={target_mm:.1f}mm speed={speed} timeout={args.timeout_s}s")
        res = post_json(
            f"{args.base_url}/motor/drive",
            {"distance_mm": target_mm, "speed": speed, "timeout_s": args.timeout_s},
            timeout_s=args.timeout_s + 10,
        )
        print("接口返回：", json.dumps(res, ensure_ascii=False))
        drive_timeout = bool(res.get("timeout", False))
        traveled_mm = float(res.get("traveled_mm", 0.0))

        measured = input("\n请输入尺子实测距离（同样支持 mm/cm/m，保持方向符号）: ").strip()
        measured_mm = parse_distance_mm(measured)

        sign = 1.0 if target_mm >= 0 else -1.0
        target_abs = abs(target_mm)
        actual_abs = abs(measured_mm)
        signed_error = measured_mm - target_mm
        error_pct = signed_error / max(target_abs, 1e-9) * 100.0

        print("\n=== 人工评估结果 ===")
        print(f"目标: {target_mm:.1f} mm")
        print(f"实测: {measured_mm:.1f} mm")
        print(f"误差: {signed_error:+.1f} mm ({error_pct:+.1f}%)")
        print(f"闭环里程读数: {traveled_mm:.1f} mm, timeout={drive_timeout}")

        if drive_timeout:
            print("⚠️ 本次为超时停车（timeout=true），不应据此校准 ENCODER_LINES_PER_REV。")
            print("请先修复里程计读数链路，再做参数标定。")
        elif actual_abs > 1e-6:
            # 基于“目标距离 / 实测距离”给出编码器线数建议
            # 走得少（actual<target）→ lines_per_rev 应增大
            suggested = args.lines_per_rev * (target_abs / actual_abs)
            print(f"建议 ENCODER_LINES_PER_REV: {suggested:.0f} (当前 {args.lines_per_rev:.0f})")
            print("应用公式: 新值 = 旧值 × (目标距离 / 实测距离)")
        else:
            print("实测距离为 0，无法计算建议值。")
        return 0
    except KeyboardInterrupt:
        print("\n已取消。")
        return 130
    except Exception as e:
        print(f"\n❌ 测试失败: {e}")
        return 1
    finally:
        force_stop(args.base_url)


if __name__ == "__main__":
    raise SystemExit(main())

