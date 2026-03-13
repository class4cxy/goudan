#!/usr/bin/env python3
"""
扬声器测试脚本
==============
测试内容：
  1. TTS 基础播放（输入文字，听效果）
  2. 中文 Neural 声音对比（晓晓 / 云扬 / 云希 / 云夏）
  3. 语速/音量调节对比
  4. interrupt 中断测试（验证队列清空和播放打断）
  5. 防回声回调验证（模拟 mute/unmute 联动）

运行方式：
  python3 speaker_test.py               # 交互菜单
  python3 speaker_test.py --speak "你好，机器人"  # 播放一段文字
  python3 speaker_test.py --voices      # 声音对比
  python3 speaker_test.py --rates       # 语速对比
  python3 speaker_test.py --interrupt   # interrupt 中断测试
  python3 speaker_test.py --callback    # 防回声回调验证
"""

import argparse
import asyncio
import sys
import time


# ── 依赖检查 ──────────────────────────────────────────────────────────

def _check_deps() -> bool:
    missing = []
    try:
        import edge_tts  # noqa: F401
    except ImportError:
        missing.append("edge-tts")
    try:
        import sounddevice  # noqa: F401
    except ImportError:
        missing.append("sounddevice")
    try:
        import soundfile  # noqa: F401
    except ImportError:
        missing.append("soundfile")

    if missing:
        print(f"❌  缺少依赖：{', '.join(missing)}")
        print(f"    请运行：pip install {' '.join(missing)}")
        return False
    return True


# ── 从本目录导入 Speaker ───────────────────────────────────────────────

def _import_speaker():
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).parent))
    from speaker import Speaker
    return Speaker


# ── 测试 1：基础播放 ───────────────────────────────────────────────────

async def test_speak(text: str, voice: str, rate: str = "+0%", volume: str = "+0%"):
    """TTS + 播放一段文字。"""
    Speaker = _import_speaker()

    print(f"\n{'─'*55}")
    print(f"  播放：{text!r}")
    print(f"  声音：{voice}  语速：{rate}  音量：{volume}")
    print(f"{'─'*55}")

    t0 = time.perf_counter()
    sp = Speaker(voice=voice, rate=rate, volume=volume)
    sp_task = asyncio.create_task(sp.start())

    await sp.enqueue(text)
    await asyncio.sleep(0.2)  # 等待入队

    # 等待队列清空
    await sp._queue.join()
    elapsed = time.perf_counter() - t0

    sp_task.cancel()
    try:
        await sp_task
    except (asyncio.CancelledError, Exception):
        pass

    print(f"  ✅  播放完成（总耗时含 TTS：{elapsed:.1f}s）")


# ── 测试 2：声音对比 ───────────────────────────────────────────────────

VOICES = [
    ("zh-CN-XiaoxiaoNeural", "晓晓（温柔女声，默认）"),
    ("zh-CN-YunxiNeural",    "云希（自然男声）"),
    ("zh-CN-YunjianNeural",  "云健（磁性男声）"),
    ("zh-CN-XiaoyiNeural",   "晓伊（活泼女声）"),
]

TEST_TEXT_VOICE = "你好，我是机器人，正在测试语音合成效果。"


async def test_voices():
    """依次用不同声音播放同一句话。"""
    Speaker = _import_speaker()

    print(f"\n{'═'*60}")
    print("  中文 Neural 声音对比")
    print(f"{'═'*60}")
    print(f"  测试文字：{TEST_TEXT_VOICE!r}\n")

    for i, (voice, label) in enumerate(VOICES):
        print(f"  [{i+1}/{len(VOICES)}] {label}")
        print(f"        {voice}")

        sp = Speaker(voice=voice)
        sp_task = asyncio.create_task(sp.start())

        await sp.enqueue(TEST_TEXT_VOICE)
        await asyncio.sleep(0.2)
        await sp._queue.join()

        sp_task.cancel()
        try:
            await sp_task
        except (asyncio.CancelledError, Exception):
            pass

        print(f"        ✅  播放完成")

        if i < len(VOICES) - 1:
            print("        （等待 1 秒后播放下一个）")
            await asyncio.sleep(1)

    print(f"\n{'═'*60}")
    print("  声音对比完成，请根据听感选择合适的声音")
    print(f"  修改 speaker.py 中的 DEFAULT_VOICE 即可")
    print("═" * 60)


# ── 测试 3：语速对比 ───────────────────────────────────────────────────

RATES = ["-30%", "-10%", "+0%", "+20%", "+50%"]
TEST_TEXT_RATE = "机器人正在以不同语速播放这段文字，请注意听差异。"


async def test_rates(voice: str):
    """依次用不同语速播放同一句话。"""
    Speaker = _import_speaker()

    print(f"\n{'═'*60}")
    print("  语速对比测试")
    print(f"{'═'*60}")
    print(f"  声音：{voice}")
    print(f"  文字：{TEST_TEXT_RATE!r}\n")

    for rate in RATES:
        label = "（慢）" if "-" in rate else ("（正常）" if rate == "+0%" else "（快）")
        print(f"  语速 {rate:>5} {label}", end=" ", flush=True)

        sp = Speaker(voice=voice, rate=rate)
        sp_task = asyncio.create_task(sp.start())

        t0 = time.perf_counter()
        await sp.enqueue(TEST_TEXT_RATE)
        await asyncio.sleep(0.2)
        await sp._queue.join()
        elapsed = time.perf_counter() - t0

        sp_task.cancel()
        try:
            await sp_task
        except (asyncio.CancelledError, Exception):
            pass

        print(f"→ 耗时 {elapsed:.1f}s  ✅")
        await asyncio.sleep(0.5)

    print(f"\n{'═'*60}")
    print("  语速对比完成")
    print("═" * 60)


# ── 测试 4：interrupt 中断测试 ────────────────────────────────────────

async def test_interrupt(voice: str):
    """
    入队 3 句话，2 秒后发 interrupt=True 的新指令。
    验证：之前的队列是否被清空、当前播放是否被打断。
    """
    Speaker = _import_speaker()

    print(f"\n{'═'*60}")
    print("  interrupt 中断测试")
    print(f"{'═'*60}")
    print("  步骤：")
    print("    1. 入队句子 A、B、C（依次排队播放）")
    print("    2. 2 秒后发送 interrupt=True 的句子 X")
    print("    3. 期望：A 可能被打断，B/C 被清空，X 立即播放")

    sp = Speaker(voice=voice)
    sp_task = asyncio.create_task(sp.start())

    await asyncio.sleep(0.2)

    # 入队 3 句
    sentences = [
        "这是第一句话，将会开始播放。",
        "这是第二句话，排在队列中等待。",
        "这是第三句话，也在队列中等待。",
    ]
    for s in sentences:
        await sp.enqueue(s)
        print(f"  [入队] {s!r}")

    # 2 秒后打断
    print(f"\n  等待 2 秒后发送 interrupt 指令...")
    await asyncio.sleep(2)

    interrupt_text = "interrupt 成功！前面的内容已被打断和清空。"
    print(f"  [interrupt] {interrupt_text!r}")
    await sp.enqueue(interrupt_text, interrupt=True)

    # 等播完
    await asyncio.sleep(0.5)
    await sp._queue.join()

    sp_task.cancel()
    try:
        await sp_task
    except (asyncio.CancelledError, Exception):
        pass

    print(f"\n  ✅  interrupt 测试完成")
    print(f"  如果你只听到「interrupt 成功」而没有听到第二、三句，则测试通过")
    print("═" * 60)


# ── 测试 5：防回声回调验证 ────────────────────────────────────────────

async def test_callback(voice: str):
    """
    注入 on_play_start / on_play_end 回调，验证它们在播放前后被正确调用。
    实际场景中这两个回调会调用 mic.mute() / mic.unmute()。
    """
    Speaker = _import_speaker()

    print(f"\n{'═'*60}")
    print("  防回声回调验证")
    print(f"{'═'*60}")
    print("  注入 on_play_start / on_play_end 回调（模拟 mute/unmute）\n")

    events: list[str] = []

    def on_start():
        ts = time.strftime("%H:%M:%S")
        events.append(f"{ts} on_play_start → mic.mute()")
        print(f"  [{ts}] 🔇  on_play_start 触发（麦克风应已静音）")

    def on_end():
        ts = time.strftime("%H:%M:%S")
        events.append(f"{ts} on_play_end → mic.unmute()")
        print(f"  [{ts}] 🎙  on_play_end 触发（麦克风已恢复）")

    sp = Speaker(voice=voice, on_play_start=on_start, on_play_end=on_end)
    sp_task = asyncio.create_task(sp.start())

    texts = ["第一句播放，验证回声控制。", "第二句播放，再次验证回声控制。"]
    for text in texts:
        await sp.enqueue(text)
        print(f"  [入队] {text!r}")

    await asyncio.sleep(0.5)
    await sp._queue.join()

    sp_task.cancel()
    try:
        await sp_task
    except (asyncio.CancelledError, Exception):
        pass

    print(f"\n  回调事件记录（共 {len(events)} 次）：")
    for e in events:
        print(f"    {e}")

    expected = len(texts) * 2
    if len(events) == expected:
        print(f"\n  ✅  回调次数正确（{expected} 次）：每句话对应一次 start + 一次 end")
    else:
        print(f"\n  ⚠️  回调次数异常，期望 {expected} 次，实际 {len(events)} 次")
    print("═" * 60)


# ── 交互菜单 ──────────────────────────────────────────────────────────

async def interactive_menu(voice: str):
    menu = """
╔══════════════════════════════════════════════╗
║         扬声器测试 — 交互菜单                ║
╠══════════════════════════════════════════════╣
║  1. 自定义文字播放                           ║
║  2. 中文 Neural 声音对比                     ║
║  3. 语速对比                                 ║
║  4. interrupt 中断测试                       ║
║  5. 防回声回调验证                           ║
║  q. 退出                                     ║
╚══════════════════════════════════════════════╝"""

    while True:
        print(menu)
        try:
            choice = input("请选择 > ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            break

        if choice == "q":
            break
        elif choice == "1":
            try:
                text = input("  请输入要播放的文字 > ").strip()
            except (EOFError, KeyboardInterrupt):
                continue
            if text:
                await test_speak(text, voice)
        elif choice == "2":
            await test_voices()
        elif choice == "3":
            await test_rates(voice)
        elif choice == "4":
            await test_interrupt(voice)
        elif choice == "5":
            await test_callback(voice)
        else:
            print("  无效选项，请重新输入")


# ── 主入口 ────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="扬声器测试脚本 — 验证 TTS 质量、中断控制、防回声回调",
        formatter_class=argparse.RawTextHelpFormatter,
    )
    parser.add_argument("--speak",     metavar="TEXT",  help="播放指定文字")
    parser.add_argument("--voices",    action="store_true", help="中文 Neural 声音对比")
    parser.add_argument("--rates",     action="store_true", help="语速对比")
    parser.add_argument("--interrupt", action="store_true", help="interrupt 中断测试")
    parser.add_argument("--callback",  action="store_true", help="防回声回调验证")
    parser.add_argument(
        "--voice", default="zh-CN-XiaoxiaoNeural",
        help="TTS 声音（默认：zh-CN-XiaoxiaoNeural 晓晓）",
    )
    args = parser.parse_args()

    if not _check_deps():
        sys.exit(1)

    print("=" * 60)
    print("  扬声器测试工具")
    print("=" * 60)
    print(f"  当前声音：{args.voice}")

    try:
        if args.speak:
            asyncio.run(test_speak(args.speak, args.voice))
        elif args.voices:
            asyncio.run(test_voices())
        elif args.rates:
            asyncio.run(test_rates(args.voice))
        elif args.interrupt:
            asyncio.run(test_interrupt(args.voice))
        elif args.callback:
            asyncio.run(test_callback(args.voice))
        else:
            asyncio.run(interactive_menu(args.voice))
    except KeyboardInterrupt:
        print("\n\n⚠️  用户中断，退出测试")


if __name__ == "__main__":
    main()
