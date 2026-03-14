#!/usr/bin/env python3
"""
麦克风测试脚本
==============
测试内容：
  1. 列出所有可用音频设备（含 USB 声卡自动检测结果）
  2. VAD 实时检测（打印语音开始/结束事件，显示时长）
  3. 静音控制验证（mute 后 VAD 是否停止触发）
  4. VAD 灵敏度对比（分别用 0/1/2/3 运行，观察误触发率）
  5. 采样率探测（显示设备支持的率及降采样策略）

运行方式：
  python3 microphone_test.py             # 交互菜单
  python3 microphone_test.py --list      # 列出所有音频设备
  python3 microphone_test.py --probe     # 探测 USB 设备采样率支持情况
  python3 microphone_test.py --vad       # VAD 实时检测（默认 15 秒）
  python3 microphone_test.py --vad --duration 30   # 指定检测时长
  python3 microphone_test.py --mute      # 静音控制验证
  python3 microphone_test.py --aggressiveness 3    # 指定 VAD 灵敏度（0-3）
  python3 microphone_test.py --device "USB Audio Device"  # 手动指定设备名

硬件：USB 免驱声卡（Type-C 接口）
  - 接入树莓派 USB-A 口，即插即用（无需安装驱动）
  - 若设备未被自动检测到，用 --list 查看设备名后通过 --device 手动指定
  - 若 ALSA 默认设备不是 USB 声卡，在 ~/.asoundrc 中设置：
      defaults.pcm.card 1
      defaults.ctl.card 1
    （card 编号通过 `aplay -l` 查看）
"""

import argparse
import asyncio
import sys
import time


# ── 依赖检查 ──────────────────────────────────────────────────────────

def _check_deps() -> bool:
    missing = []
    try:
        import sounddevice  # noqa: F401
    except ImportError:
        missing.append("sounddevice")
    try:
        import webrtcvad  # noqa: F401
    except ImportError:
        missing.append("webrtcvad")
    try:
        import numpy  # noqa: F401
    except ImportError:
        missing.append("numpy")

    if missing:
        print(f"❌  缺少依赖：{', '.join(missing)}")
        print(f"    请运行：pip install {' '.join(missing)}")
        return False
    return True


# ── 设备列表 ──────────────────────────────────────────────────────────

def list_devices():
    """列出所有可用音频输入设备，并标注自动检测到的 USB 设备。"""
    import sounddevice as sd
    import sys
    sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent))
    from microphone import find_usb_audio_device

    auto_detected = find_usb_audio_device()

    print("\n" + "═" * 68)
    print("  可用音频输入设备")
    print("═" * 68)

    devices = sd.query_devices()
    default_input = sd.default.device[0]

    input_devices = [(i, dev) for i, dev in enumerate(devices) if dev["max_input_channels"] > 0]

    if not input_devices:
        print("  ❌  未检测到任何音频输入设备")
        return

    print(f"  {'序号':<6} {'设备名':<38} {'声道':<5} {'采样率':<12} 备注")
    print(f"  {'─'*6} {'─'*38} {'─'*5} {'─'*12} {'─'*16}")
    for idx, dev in input_devices:
        marks = []
        if idx == default_input:
            marks.append("← ALSA默认")
        if auto_detected and dev["name"] == auto_detected:
            marks.append("← USB自动检测")
        print(
            f"  {idx:<6} {dev['name'][:38]:<38} "
            f"{dev['max_input_channels']:<5} "
            f"{int(dev['default_samplerate']):<12} "
            f"{'  '.join(marks)}"
        )

    print(f"\n  共找到 {len(input_devices)} 个输入设备")
    if auto_detected:
        print(f"  ✅  USB 声卡自动检测：{auto_detected!r}")
    else:
        print("  ⚠️  未自动检测到 USB 声卡，将使用 ALSA 默认设备")
        print("      若 USB 声卡已连接但未显示，请检查：")
        print("      1. lsusb 确认设备已识别")
        print("      2. arecord -l 确认 ALSA 已注册")
        print("      3. 可通过 --device 手动指定设备名")
    print("═" * 68)


# ── 采样率探测 ────────────────────────────────────────────────────────

def probe_device(device: str | None):
    """探测指定设备的采样率支持情况及降采样策略。"""
    import sounddevice as sd
    import sys
    sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent))
    from microphone import find_usb_audio_device, _probe_input_settings, SAMPLE_RATE, CHANNELS

    if device is None:
        device = find_usb_audio_device()

    print(f"\n{'═'*60}")
    print(f"  采样率探测：设备={device!r}")
    print(f"{'─'*60}")

    for rate in [8000, 16000, 32000, 44100, 48000]:
        try:
            sd.check_input_settings(device=device, channels=CHANNELS, dtype="int16", samplerate=rate)
            note = ""
            if rate == SAMPLE_RATE:
                note = "← webrtcvad 最佳"
            elif rate == 48000:
                note = "← 可 3:1 降采样至 16000Hz"
            elif rate == 44100:
                note = "← 降采样比非整数，暂不支持自动降采样"
            print(f"  {rate:>6} Hz  ✅  支持  {note}")
        except Exception:
            print(f"  {rate:>6} Hz  ✗   不支持")

    try:
        native_rate, downsample = _probe_input_settings(sd, device)
        print(f"\n  将使用：{native_rate}Hz 采集，降采样因子={downsample}，VAD 目标={SAMPLE_RATE}Hz")
    except RuntimeError as e:
        print(f"\n  ❌  {e}")
    print("═" * 60)


# ── VAD 实时检测 ──────────────────────────────────────────────────────

async def test_vad(duration_sec: int, aggressiveness: int, device: str | None):
    """运行 VAD，实时打印语音事件。"""
    import sys
    sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent))
    from microphone import Microphone

    speech_count = 0
    total_duration_ms = 0
    start_time = time.time()

    print(f"\n{'═'*60}")
    print(f"  VAD 实时检测（灵敏度={aggressiveness}，设备={device!r}，时长={duration_sec}s）")
    print(f"{'═'*60}")
    print("  请对着麦克风说话，Ctrl+C 可提前退出")
    print(f"  {'─'*58}")

    async def on_start():
        elapsed = time.time() - start_time
        print(f"  [{elapsed:6.1f}s] 🎙  语音开始...", flush=True)

    async def on_end(raw_pcm: bytes, sample_rate: int, duration_ms: int):
        nonlocal speech_count, total_duration_ms
        speech_count += 1
        total_duration_ms += duration_ms
        elapsed = time.time() - start_time
        pcm_kb = len(raw_pcm) / 1024
        print(
            f"  [{elapsed:6.1f}s] ✅  语音结束  "
            f"时长={duration_ms}ms  大小={pcm_kb:.1f}KB  "
            f"（累计 {speech_count} 次）"
        )

    mic = Microphone(
        on_speech_start=on_start,
        on_speech_end=on_end,
        vad_aggressiveness=aggressiveness,
        device=device,
    )

    mic_task = asyncio.create_task(mic.start())

    try:
        await asyncio.sleep(duration_sec)
    except asyncio.CancelledError:
        pass
    finally:
        mic_task.cancel()
        try:
            await mic_task
        except (asyncio.CancelledError, Exception):
            pass

    print(f"\n  {'─'*58}")
    print(f"  检测完成：共捕获 {speech_count} 段语音，累计 {total_duration_ms}ms")
    if speech_count == 0:
        print("  ⚠️  未检测到语音，请检查：")
        print("       - USB 声卡是否已插入（lsusb 确认）")
        print("       - arecord -l 确认 ALSA 已识别声卡")
        print("       - 用 --list 查看设备，再用 --device 手动指定")
        print("       - 可尝试降低灵敏度：--aggressiveness 1")
        print("       - 用 --probe 查看设备采样率支持情况")
    print("═" * 60)


# ── 静音控制验证 ──────────────────────────────────────────────────────

async def test_mute(aggressiveness: int, device: str | None):
    """
    验证 mute/unmute 控制。
    阶段 1：正常监听 5 秒（应能检测语音）
    阶段 2：静音 5 秒（不应检测到语音）
    阶段 3：恢复监听 5 秒（应能再次检测语音）
    """
    import sys
    sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent))
    from microphone import Microphone

    events: list[str] = []

    async def on_start():
        events.append("start")
        print("    🎙  语音开始", flush=True)

    async def on_end(raw_pcm: bytes, sample_rate: int, duration_ms: int):
        events.append(f"end({duration_ms}ms)")
        print(f"    ✅  语音结束 {duration_ms}ms", flush=True)

    mic = Microphone(
        on_speech_start=on_start,
        on_speech_end=on_end,
        vad_aggressiveness=aggressiveness,
        device=device,
    )

    mic_task = asyncio.create_task(mic.start())
    await asyncio.sleep(0.5)  # 等待麦克风初始化

    try:
        # 阶段 1：正常监听
        print(f"\n{'═'*60}")
        print("  【阶段 1/3】正常监听 5 秒 — 请对着麦克风说话")
        print(f"{'─'*60}")
        phase1_start = len(events)
        await asyncio.sleep(5)
        phase1_count = len(events) - phase1_start
        print(f"  阶段 1 捕获事件数：{phase1_count}")

        # 阶段 2：静音
        print(f"\n{'─'*60}")
        print("  【阶段 2/3】静音 5 秒 — 请继续说话（应无事件触发）")
        print(f"{'─'*60}")
        mic.mute()
        print(f"  mute() 已调用，is_muted={mic.is_muted}")
        phase2_start = len(events)
        await asyncio.sleep(5)
        phase2_count = len(events) - phase2_start
        mic.unmute()
        print(f"  unmute() 已调用，is_muted={mic.is_muted}")
        print(f"  阶段 2 捕获事件数：{phase2_count}  （期望：0）")

        # 阶段 3：恢复监听
        print(f"\n{'─'*60}")
        print("  【阶段 3/3】恢复监听 5 秒 — 请对着麦克风说话")
        print(f"{'─'*60}")
        phase3_start = len(events)
        await asyncio.sleep(5)
        phase3_count = len(events) - phase3_start
        print(f"  阶段 3 捕获事件数：{phase3_count}")

    finally:
        mic_task.cancel()
        try:
            await mic_task
        except (asyncio.CancelledError, Exception):
            pass

    # 汇总
    print(f"\n{'═'*60}")
    print("  静音控制验证结果")
    print(f"{'═'*60}")
    print(f"  阶段 1（正常）：{phase1_count} 个事件  {'✅' if phase1_count > 0 else '⚠️  无事件（麦克风可能未工作）'}")
    print(f"  阶段 2（静音）：{phase2_count} 个事件  {'✅ 静音有效' if phase2_count == 0 else '❌ 静音无效，仍有事件触发'}")
    print(f"  阶段 3（恢复）：{phase3_count} 个事件  {'✅' if phase3_count > 0 else '⚠️  无事件（说话了吗？）'}")
    print("═" * 60)


# ── 交互菜单 ──────────────────────────────────────────────────────────

async def interactive_menu(aggressiveness: int, device: str | None):
    menu = """
╔══════════════════════════════════════════╗
║        麦克风测试 — 交互菜单             ║
╠══════════════════════════════════════════╣
║  1. 列出音频设备（含 USB 自动检测）      ║
║  2. 探测设备采样率支持情况               ║
║  3. VAD 实时检测（15 秒）                ║
║  4. VAD 实时检测（30 秒）                ║
║  5. 静音控制验证（mute/unmute）          ║
║  q. 退出                                 ║
╚══════════════════════════════════════════╝"""

    while True:
        print(menu)
        try:
            choice = input("请选择 > ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            break

        if choice == "q":
            break
        elif choice == "1":
            list_devices()
        elif choice == "2":
            probe_device(device)
        elif choice == "3":
            await test_vad(15, aggressiveness, device)
        elif choice == "4":
            await test_vad(30, aggressiveness, device)
        elif choice == "5":
            await test_mute(aggressiveness, device)
        else:
            print("  无效选项，请重新输入")


# ── 主入口 ────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="麦克风测试脚本 — USB 免驱声卡，验证 VAD 检测、采样率、静音控制",
        formatter_class=argparse.RawTextHelpFormatter,
    )
    parser.add_argument("--list",  action="store_true", help="列出所有音频输入设备")
    parser.add_argument("--probe", action="store_true", help="探测设备采样率支持情况")
    parser.add_argument("--vad",   action="store_true", help="VAD 实时检测")
    parser.add_argument("--mute",  action="store_true", help="静音控制验证")
    parser.add_argument("--duration", type=int, default=15, help="VAD 检测时长（秒，默认 15）")
    parser.add_argument(
        "--aggressiveness", type=int, default=2, choices=[0, 1, 2, 3],
        help="VAD 灵敏度：0=最宽松，3=最严格（默认 2）",
    )
    parser.add_argument(
        "--device", default=None,
        help=(
            "音频设备名或序号（默认自动检测 USB 声卡）\n"
            "示例：--device 'USB Audio Device'  或  --device 1"
        ),
    )
    args = parser.parse_args()

    if not _check_deps():
        sys.exit(1)

    # 解析 --device：纯数字则转为 int（sounddevice 支持按序号指定）
    device = args.device
    if device is not None and device.isdigit():
        device = int(device)

    print("=" * 60)
    print("  麦克风测试工具 — USB 免驱声卡")
    print("=" * 60)
    print(f"  VAD 灵敏度：{args.aggressiveness}  设备：{repr(device) if device is not None else '自动检测'}")

    if args.list:
        list_devices()
        return

    if args.probe:
        probe_device(device)
        return

    try:
        if args.vad:
            asyncio.run(test_vad(args.duration, args.aggressiveness, device))
        elif args.mute:
            asyncio.run(test_mute(args.aggressiveness, device))
        else:
            asyncio.run(interactive_menu(args.aggressiveness, device))
    except KeyboardInterrupt:
        print("\n\n⚠️  用户中断，退出测试")


if __name__ == "__main__":
    main()
