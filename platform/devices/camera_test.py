#!/usr/bin/env python3
"""
摄像头测试脚本

测试内容：
  1. 摄像头能否打开
  2. 实际输出分辨率（可能与设置分辨率不同）
  3. 拍照延迟（首帧 / 后续帧）
  4. JPEG 文件大小
  5. 清晰度评分（Laplacian 方差，越高越清晰）
  6. 多分辨率对比
  7. 保存样张到当前目录，方便 scp 回来肉眼检查

运行方式：
  python3 camera_test.py               # 默认测试 /dev/video0
  python3 camera_test.py --source 1    # 指定设备号
  python3 camera_test.py --source rtsp://192.168.1.100/stream  # RTSP 流
  python3 camera_test.py --quick       # 快速单张测试
  python3 camera_test.py --resolutions # 多分辨率对比
"""

import argparse
import os
import sys
import time
from pathlib import Path

# ── 检查 cv2 ─────────────────────────────────────────────────────
try:
    import cv2
    import numpy as np
except ImportError:
    print("❌  缺少依赖：请先安装 opencv-python-headless")
    print("    pip install opencv-python-headless")
    sys.exit(1)

# 抑制 OpenCV 内部后端（obsensor/gstreamer）的噪音日志
os.environ.setdefault("OPENCV_LOG_LEVEL", "ERROR")


# ── 工具函数 ──────────────────────────────────────────────────────

def sharpness(frame) -> float:
    """
    用 Laplacian 方差评估图像清晰度。
    > 500  非常清晰
    100–500 正常
    < 100   模糊
    < 20    严重模糊 / 失焦
    """
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    return float(cv2.Laplacian(gray, cv2.CV_64F).var())


def brightness(frame) -> float:
    """平均亮度（0–255）。"""
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    return float(np.mean(gray))


def capture_frame(cap, flush: int = 1):
    """
    从已打开的 VideoCapture 读帧。
    flush=1：丢弃 1 帧缓存再读（保证取最新帧）。
    返回 (frame, elapsed_ms) 或 (None, -1)。
    """
    for _ in range(flush):
        cap.grab()
    t0 = time.perf_counter()
    ret, frame = cap.read()
    elapsed = (time.perf_counter() - t0) * 1000
    return (frame if ret else None), elapsed


def rotate_frame(frame, degrees: int):
    """旋转帧，degrees 为 0/90/180/270，其他值原样返回。"""
    _map = {
        90:  cv2.ROTATE_90_CLOCKWISE,
        180: cv2.ROTATE_180,
        270: cv2.ROTATE_90_COUNTERCLOCKWISE,
    }
    code = _map.get(degrees)
    return cv2.rotate(frame, code) if code is not None else frame


def encode_jpeg(frame, quality: int = 85) -> bytes:
    ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, quality])
    return buf.tobytes() if ok else b""


def save_sample(frame, name: str, quality: int = 85) -> str:
    path = Path(name)
    data = encode_jpeg(frame, quality)
    path.write_bytes(data)
    return str(path.resolve())


def fmt_bytes(n: int) -> str:
    if n < 1024:
        return f"{n} B"
    elif n < 1024 ** 2:
        return f"{n / 1024:.1f} KB"
    else:
        return f"{n / 1024 ** 2:.2f} MB"


def sharpness_label(score: float) -> str:
    if score >= 500:
        return "非常清晰"
    elif score >= 100:
        return "正常"
    elif score >= 20:
        return "模糊"
    else:
        return "严重模糊"


def list_available_cameras(max_index: int = 36) -> list[int]:
    """
    扫描并返回能被打开的摄像头索引列表。
    只用 isOpened() 判断（不做 read），避免因 USB 摄像头
    需要热身帧而误判为不可用。
    Linux 优先从 /dev/video* 取索引，其他平台探测 0..max_index-1。
    """
    backend = cv2.CAP_V4L2 if sys.platform.startswith("linux") else cv2.CAP_ANY

    video_devs = sorted(Path("/dev").glob("video*")) if Path("/dev").exists() else []
    candidates = []
    if video_devs:
        for dev in video_devs:
            try:
                candidates.append(int(dev.name.replace("video", "")))
            except ValueError:
                pass
    else:
        candidates = list(range(max_index))

    available = []
    for idx in candidates:
        # Linux 用路径字符串打开，绕过 OpenCV 整数索引枚举的兼容问题
        dev = f"/dev/video{idx}" if sys.platform.startswith("linux") and Path(f"/dev/video{idx}").exists() else idx
        cap = cv2.VideoCapture(dev, backend)
        if not cap.isOpened():
            cap.release()
            cap = cv2.VideoCapture(dev)
        if cap.isOpened():
            available.append(idx)
        cap.release()
    return available


def _try_open(source, backend) -> "cv2.VideoCapture | None":
    cap = cv2.VideoCapture(source, backend)
    if not cap.isOpened():
        cap.release()
        return None
    return cap


def _to_linux_source(source):
    """
    在 Linux 上将整数索引转为设备路径字符串。
    OpenCV 4.x 的 V4L2 后端在 Raspberry Pi OS Bookworm 等系统上
    用整数枚举时会跳过 USB UVC 摄像头，改用路径可绕过此问题。
    """
    if isinstance(source, int) and sys.platform.startswith("linux"):
        return f"/dev/video{source}"
    return source


def open_camera(source, width: int, height: int):
    """
    打开摄像头，返回 cap 或 None。
    Linux 上将整数索引转为 /dev/videoN 路径，用 CAP_V4L2 后端直接打开。
    """
    if sys.platform.startswith("linux"):
        path_source = _to_linux_source(source)
        cap = _try_open(path_source, cv2.CAP_V4L2) or _try_open(path_source, cv2.CAP_ANY)
    else:
        cap = cv2.VideoCapture(source)
        if not cap.isOpened():
            cap.release()
            cap = None

    if cap is None:
        return None

    cap.set(cv2.CAP_PROP_FRAME_WIDTH,  width)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, height)
    cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
    return cap


# ── 测试模式 ──────────────────────────────────────────────────────

def test_quick(source, width: int, height: int, quality: int, rotate: int = 0):
    """快速单张测试。"""
    print(f"\n{'─' * 55}")
    print("  快速拍照测试")
    print(f"{'─' * 55}")

    print(f"  打开摄像头：source={source!r}  设定分辨率={width}×{height} ...")
    t0 = time.perf_counter()
    cap = open_camera(source, width, height)
    open_ms = (time.perf_counter() - t0) * 1000

    if cap is None:
        print(f"  ❌  摄像头打开失败（source={source!r}）")
        available = list_available_cameras()
        if available:
            print(f"  ℹ️   检测到可用摄像头索引：{available}")
            print(f"       请用 --source {available[0]} 重试")
        else:
            print("  ℹ️   未检测到任何摄像头，请检查：")
            print("       - USB 摄像头是否已插入 / 驱动是否加载")
            print("       - ls /dev/video* 确认设备存在")
            print("       - source 参数是否正确（0, 1, 2... 或 RTSP URL）")
        return

    print(f"  ✅  摄像头打开成功（耗时 {open_ms:.0f} ms）")

    actual_w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    actual_h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    fps_prop  = cap.get(cv2.CAP_PROP_FPS)
    print(f"  实际分辨率：{actual_w}×{actual_h}  （设定：{width}×{height}）")
    print(f"  摄像头 FPS：{fps_prop:.1f}")

    # 首帧（热身）
    print("\n  ── 拍照 ────────────────────────────────────────────────")
    frame1, ms1 = capture_frame(cap, flush=2)
    if frame1 is None:
        print("  ❌  读帧失败")
        cap.release()
        return

    print(f"  首帧耗时：{ms1:.1f} ms")

    # 第二帧（稳定后）
    frame2, ms2 = capture_frame(cap, flush=1)
    if frame2 is None:
        frame2 = frame1
    else:
        print(f"  次帧耗时：{ms2:.1f} ms")

    frame2 = rotate_frame(frame2, rotate)

    # 分析
    sharp = sharpness(frame2)
    bright = brightness(frame2)
    jpeg_data = encode_jpeg(frame2, quality)
    print(f"\n  清晰度（Laplacian 方差）：{sharp:.1f}  → {sharpness_label(sharp)}")
    print(f"  平均亮度：{bright:.1f} / 255")
    print(f"  JPEG 大小（quality={quality}）：{fmt_bytes(len(jpeg_data))}")

    # 保存样张
    sample_name = f"camera_sample_{actual_w}x{actual_h}.jpg"
    path = save_sample(frame2, sample_name, quality)
    print(f"\n  ✅  样张已保存：{path}")
    print(f"       可 scp 到本机查看：scp pi@<树莓派IP>:{path} .")

    cap.release()


def test_burst(source, width: int, height: int, count: int = 5):
    """连拍延迟测试。"""
    print(f"\n{'─' * 55}")
    print(f"  连拍测试（{count} 张）")
    print(f"{'─' * 55}")

    cap = open_camera(source, width, height)
    if cap is None:
        print("  ❌  摄像头打开失败")
        return

    latencies = []
    sharp_scores = []

    # 热身 2 帧
    cap.grab(); cap.grab()

    for i in range(count):
        frame, ms = capture_frame(cap, flush=0)
        if frame is None:
            print(f"  帧 {i+1}：读取失败")
            continue
        s = sharpness(frame)
        latencies.append(ms)
        sharp_scores.append(s)
        print(f"  帧 {i+1:2d}：耗时 {ms:6.1f} ms  清晰度 {s:8.1f}  {sharpness_label(s)}")
        time.sleep(0.1)

    if latencies:
        print(f"\n  平均耗时：{sum(latencies)/len(latencies):.1f} ms")
        print(f"  最大耗时：{max(latencies):.1f} ms")
        print(f"  平均清晰度：{sum(sharp_scores)/len(sharp_scores):.1f}")

    cap.release()


def test_resolutions(source):
    """多分辨率对比测试。"""
    resolutions = [
        (320,  240),
        (640,  480),
        (1280, 720),
        (1280, 960),
    ]
    print(f"\n{'─' * 65}")
    print("  多分辨率对比测试")
    print(f"{'─' * 65}")
    print(f"  {'设定分辨率':<14} {'实际分辨率':<14} {'清晰度':>8} {'亮度':>6} {'JPEG大小':>10}  样张文件")
    print(f"  {'─'*14} {'─'*14} {'─'*8} {'─'*6} {'─'*10}  {'─'*30}")

    results = []

    for w, h in resolutions:
        cap = open_camera(source, w, h)
        if cap is None:
            print(f"  {w}×{h:<10} ❌ 打开失败")
            continue

        actual_w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        actual_h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))

        # 热身
        cap.grab(); cap.grab()
        frame, _ = capture_frame(cap, flush=1)
        cap.release()

        if frame is None:
            print(f"  {w}×{h:<10} ❌ 读帧失败")
            continue

        sharp = sharpness(frame)
        bright = brightness(frame)
        jpeg_data = encode_jpeg(frame, 85)
        sample_name = f"camera_res_{actual_w}x{actual_h}.jpg"
        save_sample(frame, sample_name, 85)

        print(
            f"  {w}×{h:<14} {actual_w}×{actual_h:<14} "
            f"{sharp:>8.1f} {bright:>6.1f} {fmt_bytes(len(jpeg_data)):>10}  {sample_name}"
        )
        results.append((actual_w, actual_h, sharp))

    if results:
        best = max(results, key=lambda r: r[2])
        print(f"\n  → 清晰度最高分辨率：{best[0]}×{best[1]}（Laplacian={best[2]:.1f}）")
        print("    所有样张已保存到当前目录，可批量 scp 查看")


def test_quality(source, width: int, height: int):
    """JPEG 压缩质量对比。"""
    qualities = [50, 70, 85, 95]
    print(f"\n{'─' * 55}")
    print("  JPEG 质量对比")
    print(f"{'─' * 55}")

    cap = open_camera(source, width, height)
    if cap is None:
        print("  ❌  摄像头打开失败")
        return

    cap.grab(); cap.grab()
    frame, _ = capture_frame(cap, flush=1)
    cap.release()

    if frame is None:
        print("  ❌  读帧失败")
        return

    print(f"  {'质量':>6}  {'文件大小':>10}  样张文件")
    print(f"  {'─'*6}  {'─'*10}  {'─'*30}")
    for q in qualities:
        data = encode_jpeg(frame, q)
        name = f"camera_q{q}_{width}x{height}.jpg"
        save_sample(frame, name, q)
        print(f"  {q:>6}  {fmt_bytes(len(data)):>10}  {name}")

    print("\n    所有质量对比样张已保存，可自行比较画质与文件大小")


# ── 主入口 ────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="摄像头测试脚本 — 验证分辨率、清晰度、延迟",
        formatter_class=argparse.RawTextHelpFormatter,
    )
    parser.add_argument(
        "--source", default="0",
        help="摄像头来源：设备号（0, 1...）或 RTSP URL\n默认：0 (/dev/video0)",
    )
    parser.add_argument("--width",   type=int, default=1280, help="设定宽度（默认 1280）")
    parser.add_argument("--height",  type=int, default=960,  help="设定高度（默认 960）")
    parser.add_argument("--quality", type=int, default=85,  help="JPEG 质量（默认 85）")
    parser.add_argument("--rotate",  type=int, default=180, choices=[0, 90, 180, 270],
                        help="旋转角度（默认 180，即纠正倒置摄像头）")
    parser.add_argument("--quick",   action="store_true",   help="仅做快速单张测试")
    parser.add_argument("--resolutions", action="store_true", help="多分辨率对比测试")
    parser.add_argument("--burst",   action="store_true",   help="连拍延迟测试")
    parser.add_argument("--all",     action="store_true",   help="跑所有测试")
    parser.add_argument("--list",    action="store_true",   help="列出所有可用摄像头索引")
    args = parser.parse_args()

    if args.list:
        print("正在扫描可用摄像头...")
        cams = list_available_cameras()
        if cams:
            print(f"✅  找到摄像头索引：{cams}")
            print(f"    运行示例：python3 {Path(__file__).name} --source {cams[0]}")
        else:
            print("❌  未找到任何摄像头")
        sys.exit(0)

    # source 类型处理：纯数字 → int，否则保留字符串（RTSP URL）
    source: int | str = int(args.source) if args.source.isdigit() else args.source

    print("=" * 55)
    print("  摄像头测试工具")
    print("=" * 55)
    print(f"  cv2 版本  ：{cv2.__version__}")
    print(f"  source    ：{source!r}")
    print(f"  设定分辨率：{args.width}×{args.height}")
    print(f"  JPEG 质量  ：{args.quality}")

    # 默认：运行快速测试 + 连拍测试
    run_quick      = args.quick or args.all or not any([args.resolutions, args.burst])
    run_burst      = args.burst or args.all
    run_resolutions = args.resolutions or args.all

    if run_quick:
        test_quick(source, args.width, args.height, args.quality, args.rotate)

    if run_burst:
        test_burst(source, args.width, args.height)

    if run_resolutions:
        test_quality(source, args.width, args.height)
        test_resolutions(source)

    print(f"\n{'=' * 55}")
    print("  测试完成")
    print("=" * 55)


if __name__ == "__main__":
    main()
