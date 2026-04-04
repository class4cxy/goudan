#!/usr/bin/env python3
"""
角度单项测试入口（人工量角校准）
================================

流程：
1) 输入目标角度（正=左转，负=右转）
2) 调用 /motor/drive 执行
3) 你用量角器/地面标线读“实际转了多少”，回填结果
4) 脚本输出误差，并按返回 sensor 给出校准建议
"""

from __future__ import annotations

import argparse
import json
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


def main() -> int:
    parser = argparse.ArgumentParser(description="角度单项测试入口")
    parser.add_argument("--base-url", default="http://localhost:8001")
    parser.add_argument("--speed", type=int, default=35)
    parser.add_argument("--timeout-s", type=float, default=20.0)
    parser.add_argument("--wheel-base-mm", type=float, default=160.0)
    parser.add_argument("--target-deg", type=float, default=None)
    args = parser.parse_args()

    speed = max(0, min(100, args.speed))

    try:
        if args.target_deg is None:
            target_deg = float(input("请输入目标角度（正=左转，负=右转，例如 90 / -90）: ").strip())
        else:
            target_deg = float(args.target_deg)

        print(f"\n执行中：target={target_deg:.1f}deg speed={speed} timeout={args.timeout_s}s")
        res = post_json(
            f"{args.base_url}/motor/drive",
            {"angle_deg": target_deg, "speed": speed, "timeout_s": args.timeout_s},
            timeout_s=args.timeout_s + 10,
        )
        print("接口返回：", json.dumps(res, ensure_ascii=False))
        sensor = str(res.get("sensor", "unknown"))

        measured_deg = float(input("\n请输入实测角度（保持方向符号）: ").strip())
        target_abs = abs(target_deg)
        actual_abs = abs(measured_deg)
        signed_error = measured_deg - target_deg
        error_pct = signed_error / max(target_abs, 1e-9) * 100.0

        print("\n=== 人工评估结果 ===")
        print(f"目标: {target_deg:.1f} deg")
        print(f"实测: {measured_deg:.1f} deg")
        print(f"误差: {signed_error:+.1f} deg ({error_pct:+.1f}%)")
        print(f"传感器路径: {sensor}")

        if actual_abs > 1e-6 and sensor == "odometry_theta":
            suggested = args.wheel_base_mm * (target_abs / actual_abs)
            print(f"建议 ODOM_WHEEL_BASE_MM: {suggested:.1f} (当前 {args.wheel_base_mm:.1f})")
            print("应用公式: 新值 = 旧值 × (目标角度 / 实测角度)")
        elif sensor == "raw_imu":
            print("当前转向使用 raw_imu：无需调 wheel_base，请优先检查 IMU 接线/零偏。")
        else:
            print("未识别 sensor 路径，先确认 /motor/drive 返回字段 sensor。")
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

