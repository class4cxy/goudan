"""
openWakeWord 可行性测试脚本
============================
用途：在当前硬件上验证 openWakeWord 的准确率、误唤醒率和推理延迟，
      无需启动完整的 Platform 服务，直接接管麦克风运行。

安装依赖：
    pip install openwakeword sounddevice numpy
    # openWakeWord 首次运行会自动下载预训练模型（~50MB）

用法：
    # 1. 列出所有内置预训练模型
    python wake_word_test.py --list

    # 2. 使用预训练英文模型快速验证流程（说 "hey jarvis"）
    python wake_word_test.py --model hey_jarvis

    # 3. 指定阈值（默认 0.5，越高越严格）
    python wake_word_test.py --model hey_jarvis --threshold 0.6

    # 4. 使用自定义中文模型（训练完成后）
    python wake_word_test.py --model /path/to/gou_dan.onnx

    # 5. 静默模式，只打印命中行
    python wake_word_test.py --model hey_jarvis --quiet

注意：
    - 预训练模型均为英文，中文唤醒词需自行训练（见 README）
    - 准确率受麦克风距离、环境噪声和发音影响，建议多测几次取平均
    - 误唤醒：保持安静，看 5 分钟内是否有误触发
"""

import argparse
import queue
import sys
import time

import numpy as np

SAMPLE_RATE = 16000
CHANNELS = 1
OWW_CHUNK_MS = 80                                          # OWW 推荐帧长
OWW_CHUNK_SIZE = int(SAMPLE_RATE * OWW_CHUNK_MS / 1000)   # 1280 samples
BLOCK_MS = 32                                              # sounddevice 回调块（~2.5块/chunk）
BLOCK_SIZE = int(SAMPLE_RATE * BLOCK_MS / 1000)            # 512 samples
DEFAULT_THRESHOLD = 0.5
COOLDOWN_S = 1.5   # 命中后冷却，防止同一次说话重复计数


# ─── 工具函数 ─────────────────────────────────────────────────────────────────

def _get_pretrained_models() -> list[str]:
    """返回 openWakeWord 内置的预训练模型名称列表。"""
    try:
        import openwakeword
        paths = openwakeword.utils.get_pretrained_model_paths()
        # 路径形如 /path/to/hey_jarvis.onnx → 取 stem
        import pathlib
        return sorted({pathlib.Path(p).stem for p in paths})
    except Exception as e:
        print(f"[错误] 无法获取模型列表：{e}")
        return []


def _load_model(model_name: str):
    """加载 OWW 模型，返回 Model 实例。"""
    try:
        from openwakeword.model import Model
    except ImportError:
        print("[错误] 未安装 openwakeword，请运行：pip install openwakeword")
        sys.exit(1)

    print(f"[加载] 模型：{model_name!r} （首次运行可能需要下载，请稍候…）")
    try:
        oww = Model(wakeword_models=[model_name], inference_framework="onnx")
        print(f"[加载] 完成，模型输出名：{list(oww.models.keys())}")
        return oww
    except Exception as e:
        print(f"[错误] 模型加载失败：{e}")
        print("       如使用内置模型名，请先运行 --list 确认名称是否正确")
        sys.exit(1)


def _find_usb_mic() -> str | None:
    """自动检测 USB 麦克风设备名。"""
    try:
        import sounddevice as sd
        for dev in sd.query_devices():
            if dev["max_input_channels"] > 0 and "usb" in dev["name"].lower():
                return dev["name"]
    except Exception:
        pass
    return None


# ─── 列出模型 ──────────────────────────────────────────────────────────────────

def cmd_list() -> None:
    models = _get_pretrained_models()
    if not models:
        print("未找到内置预训练模型（请确认 openwakeword 已安装）")
        return
    print(f"内置预训练模型（共 {len(models)} 个）：")
    for m in models:
        print(f"  {m}")
    print()
    print("提示：预训练模型均为英文。中文唤醒词请参考官方训练指南：")
    print("      https://github.com/dscripka/openWakeWord#training-new-models")


# ─── 运行测试 ──────────────────────────────────────────────────────────────────

def cmd_test(model_name: str, threshold: float, quiet: bool) -> None:
    try:
        import sounddevice as sd
    except ImportError:
        print("[错误] 未安装 sounddevice，请运行：pip install sounddevice")
        sys.exit(1)

    oww = _load_model(model_name)
    audio_q: queue.SimpleQueue[np.ndarray] = queue.SimpleQueue()

    # ── 自动检测麦克风 ────────────────────────────────────────────────────────
    device = _find_usb_mic()
    if device:
        print(f"[麦克风] 自动检测到 USB 设备：{device!r}")
    else:
        print("[麦克风] 未检测到 USB 设备，使用系统默认麦克风")

    # ── 探测采样率 ────────────────────────────────────────────────────────────
    native_rate = SAMPLE_RATE
    downsample = 1
    for rate in [16000, 48000]:
        try:
            sd.check_input_settings(device=device, channels=1, dtype="int16", samplerate=rate)
            native_rate = rate
            downsample = rate // SAMPLE_RATE
            if rate != SAMPLE_RATE:
                print(f"[麦克风] 设备不支持 16kHz，使用 {rate}Hz（自动降采样 {downsample}×）")
            break
        except Exception:
            continue

    native_block = int(native_rate * BLOCK_MS / 1000)

    def _cb(indata: np.ndarray, frames: int, time_info, status):
        if status:
            print(f"[sounddevice] {status}", flush=True)
        frame = indata[:, 0].copy().astype(np.int16)
        if downsample > 1:
            n = (len(frame) // downsample) * downsample
            frame = frame[:n].reshape(-1, downsample).mean(axis=1).astype(np.int16)
        audio_q.put_nowait(frame)

    # ── 打印表头 ──────────────────────────────────────────────────────────────
    if not quiet:
        print()
        print(f"  阈值：{threshold}   冷却：{COOLDOWN_S}s   Ctrl+C 退出")
        print(f"  {'时间':>7}  {'模型':>20}  {'得分':>6}  状态")
        print("  " + "─" * 48)

    start_ts = time.time()
    detect_count = 0
    last_detect_ts = 0.0
    oww_buf = np.array([], dtype=np.int16)

    try:
        with sd.InputStream(
            samplerate=native_rate,
            channels=CHANNELS,
            dtype="int16",
            blocksize=native_block,
            device=device,
            callback=_cb,
        ):
            print("[监听中] 请对着麦克风说唤醒词…", flush=True)
            while True:
                # 排空队列，积累到 OWW 缓冲
                while True:
                    try:
                        oww_buf = np.concatenate([oww_buf, audio_q.get(timeout=0.05)])
                    except queue.Empty:
                        break

                # 逐块喂给 OWW（每 80ms 一次）
                while len(oww_buf) >= OWW_CHUNK_SIZE:
                    feed = oww_buf[:OWW_CHUNK_SIZE]
                    oww_buf = oww_buf[OWW_CHUNK_SIZE:]

                    t_infer = time.perf_counter()
                    try:
                        preds = oww.predict(feed)
                    except Exception as e:
                        print(f"[错误] 推理失败：{e}")
                        continue
                    infer_ms = int((time.perf_counter() - t_infer) * 1000)

                    now = time.time()
                    elapsed = now - start_ts

                    for word, score in preds.items():
                        in_cooldown = (now - last_detect_ts) < COOLDOWN_S
                        hit = score >= threshold and not in_cooldown

                        if hit:
                            detect_count += 1
                            last_detect_ts = now
                            marker = f"  ✓ 命中！（第 {detect_count} 次，推理={infer_ms}ms）"
                            print(f"  {elapsed:>7.1f}s  {word:>20}  {score:>6.3f}{marker}",
                                  flush=True)
                        elif not quiet and score > 0.05:
                            print(f"  {elapsed:>7.1f}s  {word:>20}  {score:>6.3f}",
                                  flush=True)

    except KeyboardInterrupt:
        elapsed = time.time() - start_ts
        print()
        print("─" * 52)
        print(f"[结果] 运行时长：{elapsed:.0f}s")
        print(f"[结果] 命中次数：{detect_count}")
        if elapsed > 0:
            rate = detect_count / (elapsed / 60)
            print(f"[结果] 误唤醒估算：{rate:.1f} 次/分钟（安静环境下应趋近 0）")
        print()


# ─── 入口 ─────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="openWakeWord 可行性测试",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--model", "-m",
        default="hey_jarvis",
        help="预训练模型名（如 hey_jarvis）或自定义 .onnx/.tflite 文件路径",
    )
    parser.add_argument(
        "--threshold", "-t",
        type=float,
        default=DEFAULT_THRESHOLD,
        help=f"检测阈值，0~1，越高越严格（默认 {DEFAULT_THRESHOLD}）",
    )
    parser.add_argument(
        "--quiet", "-q",
        action="store_true",
        help="只打印命中行，省略低分输出",
    )
    parser.add_argument(
        "--list", "-l",
        action="store_true",
        help="列出所有内置预训练模型名称后退出",
    )
    args = parser.parse_args()

    if args.list:
        cmd_list()
        return

    cmd_test(args.model, args.threshold, args.quiet)


if __name__ == "__main__":
    main()
