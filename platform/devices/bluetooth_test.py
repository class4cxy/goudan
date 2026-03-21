#!/usr/bin/env python3
"""
蓝牙模块测试脚本
================
测试内容：
  1. 环境检测 — bluetoothctl / pactl 可用性、蓝牙服务状态
  2. 扫描测试 — 扫描附近蓝牙设备并列出
  3. 已配对设备 — 列出已配对过的设备
  4. 连接测试 — 配对 → 信任 → 连接指定 MAC 地址
  5. Sink 测试 — 验证 PulseAudio/PipeWire 蓝牙 sink 已注册
  6. 音频测试 — 通过蓝牙播放 TTS 验证音（需先完成连接，需 edge-tts/soundfile）
  7. 断开测试 — 断开当前蓝牙设备

运行方式：
  python3 bluetooth_test.py                        # 交互菜单
  python3 bluetooth_test.py --check                # 环境检测
  python3 bluetooth_test.py --scan [--timeout 15]  # 扫描附近设备
  python3 bluetooth_test.py --paired               # 列出已配对设备
  python3 bluetooth_test.py --connect XX:XX:XX:XX:XX:XX  # 连接并验证
  python3 bluetooth_test.py --sink                 # 检查 PulseAudio sink
  python3 bluetooth_test.py --audio               # 蓝牙播放 TTS 测试（需先连接）
  python3 bluetooth_test.py --disconnect           # 断开当前设备
"""

import argparse
import asyncio
import subprocess
import sys
import time
from pathlib import Path

# 将 devices 目录加入路径，使 bluetooth.py 可以直接 import
sys.path.insert(0, str(Path(__file__).parent))

DIVIDER = "─" * 60
WIDE    = "═" * 60


# ── 工具函数 ────────────────────────────────────────────────────────


def _run(cmd: list[str], timeout: float = 5.0) -> tuple[int, str]:
    """同步运行命令，返回 (returncode, stdout+stderr)。"""
    try:
        r = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return r.returncode, (r.stdout + r.stderr).strip()
    except FileNotFoundError:
        return -1, f"命令不存在：{cmd[0]}"
    except subprocess.TimeoutExpired:
        return -2, "命令超时"
    except Exception as e:
        return -3, str(e)


def _ok(msg: str):
    print(f"  ✅  {msg}")


def _fail(msg: str):
    print(f"  ❌  {msg}")


def _warn(msg: str):
    print(f"  ⚠️   {msg}")


def _info(msg: str):
    print(f"  ℹ️   {msg}")


# ── 测试 1：环境检测 ────────────────────────────────────────────────


def test_check():
    """检测蓝牙环境：bluetoothctl / pactl / BlueZ 服务 / 蓝牙适配器。"""
    print(f"\n{WIDE}")
    print("  测试 1 — 蓝牙环境检测")
    print(WIDE)

    all_ok = True

    # bluetoothctl
    rc, out = _run(["bluetoothctl", "--version"])
    if rc == 0:
        _ok(f"bluetoothctl 已安装：{out}")
    else:
        _fail("bluetoothctl 不可用，请运行：sudo apt install bluez")
        all_ok = False

    # pactl（PulseAudio/PipeWire 兼容层）
    rc, out = _run(["pactl", "--version"])
    if rc == 0:
        _ok(f"pactl 已安装：{out.splitlines()[0] if out else repr(out)}")
    else:
        _warn("pactl 不可用（若使用 ALSA 直连可忽略）；蓝牙音频需要：sudo apt install pulseaudio-utils")

    # bluetooth 服务状态
    rc, out = _run(["systemctl", "is-active", "bluetooth"])
    if rc == 0 and out.strip() == "active":
        _ok("bluetooth 服务运行中")
    else:
        _fail(f"bluetooth 服务状态：{out}（请运行：sudo systemctl start bluetooth）")
        all_ok = False

    # 蓝牙适配器
    rc, out = _run(["bluetoothctl", "list"])
    if rc == 0 and out.strip():
        _ok(f"蓝牙适配器：{out.strip()}")
    else:
        _fail("未找到蓝牙适配器（树莓派 5 内置 BT5.0，检查是否被 rfkill 屏蔽）")
        rc2, out2 = _run(["rfkill", "list", "bluetooth"])
        if out2:
            print(f"      rfkill: {out2[:200]}")
        all_ok = False

    # 适配器是否上电
    rc, out = _run(["bluetoothctl", "show"])
    if rc == 0:
        powered = any("Powered: yes" in line for line in out.splitlines())
        if powered:
            _ok("蓝牙适配器已上电（Powered: yes）")
        else:
            _warn("蓝牙适配器未上电，运行：bluetoothctl power on")
            all_ok = False

    print(f"\n  {'✅  环境检测通过' if all_ok else '❌  存在问题，请修复后再测试'}")
    print(WIDE)
    return all_ok


# ── 测试 2：扫描附近设备 ────────────────────────────────────────────


async def test_scan(timeout_s: int = 10):
    """扫描附近蓝牙设备并列出。"""
    print(f"\n{WIDE}")
    print(f"  测试 2 — 扫描附近蓝牙设备（{timeout_s}s）")
    print(WIDE)
    print(f"  正在扫描，请稍候...")

    from bluetooth import BluetoothManager
    bt = BluetoothManager()
    await bt.probe()

    if bt.is_simulation:
        _warn("bluetoothctl 不可用，显示模拟结果")

    t0 = time.perf_counter()
    devices = await bt.scan(timeout_s=timeout_s)
    elapsed = time.perf_counter() - t0

    print(f"\n  扫描完成（耗时 {elapsed:.1f}s），发现 {len(devices)} 台设备：\n")
    if devices:
        print(f"  {'MAC 地址':<20}  名称")
        print(f"  {'─'*20}  {'─'*30}")
        for d in devices:
            print(f"  {d['mac']:<20}  {d['name']}")
        print()
        _ok(f"共发现 {len(devices)} 台设备")
        _info("使用 --connect XX:XX:XX:XX:XX:XX 连接目标设备")
    else:
        _warn("未发现任何设备（设备需处于可发现模式）")

    print(WIDE)
    return devices


# ── 测试 3：已配对设备 ──────────────────────────────────────────────


async def test_paired():
    """列出已配对（信任）的蓝牙设备。"""
    print(f"\n{WIDE}")
    print("  测试 3 — 已配对蓝牙设备")
    print(WIDE)

    from bluetooth import BluetoothManager
    bt = BluetoothManager()
    await bt.probe()

    devices = await bt.get_paired_devices()

    if devices:
        print(f"  已配对 {len(devices)} 台设备：\n")
        print(f"  {'MAC 地址':<20}  名称")
        print(f"  {'─'*20}  {'─'*30}")
        for d in devices:
            print(f"  {d['mac']:<20}  {d['name']}")
        print()
        _ok(f"共 {len(devices)} 台已配对设备")
        _info("可直接用 --connect <MAC> 重新连接已配对设备（无需重新扫描）")
    else:
        _warn("暂无已配对设备")
        _info("请先用 --scan 扫描并用 --connect <MAC> 连接")

    print(WIDE)
    return devices


# ── 测试 4：连接设备 ────────────────────────────────────────────────


async def test_connect(mac: str) -> bool:
    """配对 → 信任 → 连接指定 MAC 地址的蓝牙设备。"""
    print(f"\n{WIDE}")
    print(f"  测试 4 — 连接蓝牙设备：{mac}")
    print(WIDE)

    from bluetooth import BluetoothManager
    bt = BluetoothManager()
    await bt.probe()

    if bt.is_simulation:
        _warn("bluetoothctl 不可用，跳过真实连接")
        return False

    print("  步骤：pair → trust → connect → pactl set-default-sink\n")

    print(f"  [{1}/4] 配对（pair）...")
    t0 = time.perf_counter()
    ok = await bt.connect(mac)
    elapsed = time.perf_counter() - t0

    if ok:
        status = bt.status()
        _ok(f"连接成功！（耗时 {elapsed:.1f}s）")
        print(f"\n  设备信息：")
        print(f"    名称：{status['name']}")
        print(f"    MAC：{status['mac']}")
        _info("现在可以用 --audio 测试蓝牙音频输出")
        _info("同时设置 SPEAKER_BACKEND=pulseaudio 可让 TTS 通过蓝牙播放")
    else:
        _fail(f"连接失败（耗时 {elapsed:.1f}s）")
        _info("常见原因：")
        print("    1. 音箱未开机 / 未在配对模式")
        print("    2. 蓝牙适配器未上电（bluetoothctl power on）")
        print("    3. 音箱已连接到其他设备（需先在音箱上断开）")
        print("    4. A2DP 音频配置文件未加载（见 docs/HARDWARE.md）")

    print(WIDE)
    return ok


# ── 测试 5：PulseAudio Sink 检测 ───────────────────────────────────


def test_sink():
    """列出当前 PulseAudio/PipeWire sink，确认蓝牙 sink 已注册。"""
    print(f"\n{WIDE}")
    print("  测试 5 — PulseAudio/PipeWire Sink 列表")
    print(WIDE)

    rc, out = _run(["pactl", "list", "sinks", "short"], timeout=5.0)
    if rc != 0:
        _fail(f"pactl 不可用：{out}")
        _info("若使用 aplay（ALSA 直连）可跳过此测试")
        print(WIDE)
        return

    if not out.strip():
        _warn("当前无可用 sink（PipeWire/PulseAudio 未运行？）")
        print(WIDE)
        return

    lines = out.strip().splitlines()
    print(f"  共 {len(lines)} 个 sink：\n")
    bt_sinks = []
    for line in lines:
        is_bt = "bluez" in line.lower()
        marker = "🔵 BT " if is_bt else "     "
        print(f"  {marker}{line}")
        if is_bt:
            bt_sinks.append(line)

    print()
    if bt_sinks:
        _ok(f"发现 {len(bt_sinks)} 个蓝牙 sink（A2DP 已就绪）")

        # 检查默认 sink
        rc2, default_out = _run(["pactl", "get-default-sink"], timeout=3.0)
        if rc2 == 0:
            default_sink = default_out.strip()
            is_bt_default = "bluez" in default_sink.lower()
            if is_bt_default:
                _ok(f"当前默认 sink 是蓝牙设备：{default_sink}")
            else:
                _warn(f"当前默认 sink 不是蓝牙：{default_sink}")
                _info("运行 --connect <MAC> 会自动设置蓝牙为默认 sink")
    else:
        _warn("未发现蓝牙 sink（设备未连接？A2DP 未加载？）")
        _info("确认：1) 蓝牙已连接  2) 音箱支持 A2DP  3) PipeWire 已安装")

    print(WIDE)


# ── 测试 6：蓝牙音频输出测试 ──────────────────────────────────────


async def test_audio(mac: str | None = None):
    """
    通过蓝牙播放一段 TTS 测试音，验证端到端音频链路。
    需要先连接蓝牙设备（--connect），且 SPEAKER_BACKEND=pulseaudio。
    """
    print(f"\n{WIDE}")
    print("  测试 6 — 蓝牙音频播放测试（edge-tts + pacat）")
    print(WIDE)

    # 检查依赖
    missing = []
    try:
        import edge_tts  # noqa: F401
    except ImportError:
        missing.append("edge-tts")
    try:
        import soundfile  # noqa: F401
    except ImportError:
        missing.append("soundfile")
    try:
        import numpy  # noqa: F401
    except ImportError:
        missing.append("numpy")

    if missing:
        _fail(f"缺少依赖：{', '.join(missing)}")
        print(f"  请运行：pip install {' '.join(missing)}")
        print(WIDE)
        return False

    # 检查 pacat
    rc, _ = _run(["pacat", "--version"])
    if rc != 0:
        _warn("pacat 不可用，将尝试 aplay（ALSA，可能不走蓝牙）")
        import os
        os.environ["SPEAKER_BACKEND"] = "alsa"
    else:
        import os
        os.environ.setdefault("SPEAKER_BACKEND", "pulseaudio")

    from speaker import Speaker, SPEAKER_BACKEND
    _info(f"播放后端：{SPEAKER_BACKEND}")

    test_sentences = [
        "蓝牙音频测试，第一句。",
        "蓝牙音频测试，第二句，验证多句连续播放。",
        "测试完成，蓝牙外放工作正常。",
    ]

    print(f"\n  测试内容：{len(test_sentences)} 句话连续播放\n")

    start_times: list[float] = []
    end_times: list[float] = []

    def on_end():
        end_times.append(time.perf_counter())

    sp = Speaker(on_play_end=on_end)
    sp_task = asyncio.create_task(sp.start())
    await asyncio.sleep(0.1)

    t_total = time.perf_counter()
    for i, text in enumerate(test_sentences):
        print(f"  [{i+1}/{len(test_sentences)}] {text!r}", end=" ", flush=True)
        start_times.append(time.perf_counter())
        await sp.enqueue(text)
        print("（已入队）")

    # 等待所有句子播完
    while sp.is_busy():
        await asyncio.sleep(0.3)

    elapsed_total = time.perf_counter() - t_total

    sp_task.cancel()
    try:
        await sp_task
    except (asyncio.CancelledError, Exception):
        pass

    print(f"\n  总耗时：{elapsed_total:.1f}s（含 TTS 合成）")
    _ok(f"蓝牙音频输出正常，共播放 {len(test_sentences)} 句")
    _info("若听到声音：SPEAKER_BACKEND=pulseaudio 配置正确")
    _info("若无声音：检查默认 sink 是否为蓝牙（用 --sink 查看）")

    print(WIDE)
    return True


# ── 测试 7：断开设备 ────────────────────────────────────────────────


async def test_disconnect():
    """断开当前已连接的蓝牙设备。"""
    print(f"\n{WIDE}")
    print("  测试 7 — 断开蓝牙设备")
    print(WIDE)

    from bluetooth import BluetoothManager
    bt = BluetoothManager()

    # 用 bluetoothctl info 找当前已连接设备
    rc, out = _run(["bluetoothctl", "devices", "Connected"])
    connected_mac = None
    if rc == 0 and out.strip():
        import re
        # group(0) 是完整 MAC（group(1) 是最后捕获的分组）
        m = re.search(r"(?:[0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}", out)
        if m:
            connected_mac = m.group(0).upper()

    if not connected_mac:
        _warn("未检测到已连接的蓝牙设备")
        print(WIDE)
        return

    print(f"  当前已连接：{connected_mac}")
    bt._connected_mac = connected_mac  # 注入，使 disconnect() 可以找到目标

    ok = await bt.disconnect()
    if ok:
        _ok(f"已断开：{connected_mac}")
    else:
        _fail(f"断开失败：{connected_mac}")

    print(WIDE)


# ── 交互菜单 ────────────────────────────────────────────────────────


async def interactive_menu():
    menu = """
╔══════════════════════════════════════════════════════════╗
║              蓝牙模块测试 — 交互菜单                     ║
╠══════════════════════════════════════════════════════════╣
║  1. 环境检测（bluetoothctl / pactl / 服务 / 适配器）     ║
║  2. 扫描附近蓝牙设备                                     ║
║  3. 列出已配对设备                                       ║
║  4. 连接蓝牙设备（输入 MAC）                             ║
║  5. 检查 PulseAudio/PipeWire Sink                        ║
║  6. 蓝牙音频输出测试（TTS 播放）                         ║
║  7. 断开当前蓝牙设备                                     ║
║  q. 退出                                                 ║
╚══════════════════════════════════════════════════════════╝"""

    while True:
        print(menu)
        try:
            choice = input("请选择 > ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            break

        if choice == "q":
            break
        elif choice == "1":
            test_check()
        elif choice == "2":
            try:
                t = input("  扫描时长（秒，默认 10）> ").strip()
                timeout = int(t) if t.isdigit() else 10
            except (EOFError, KeyboardInterrupt):
                timeout = 10
            await test_scan(timeout)
        elif choice == "3":
            await test_paired()
        elif choice == "4":
            try:
                mac = input("  输入 MAC 地址（格式 XX:XX:XX:XX:XX:XX）> ").strip().upper()
            except (EOFError, KeyboardInterrupt):
                continue
            if mac:
                await test_connect(mac)
            else:
                print("  MAC 地址不能为空")
        elif choice == "5":
            test_sink()
        elif choice == "6":
            await test_audio()
        elif choice == "7":
            await test_disconnect()
        else:
            print("  无效选项，请重新输入")


# ── 主入口 ──────────────────────────────────────────────────────────


def main():
    parser = argparse.ArgumentParser(
        description="蓝牙模块测试脚本 — 验证硬件环境、扫描、配对、音频输出",
        formatter_class=argparse.RawTextHelpFormatter,
    )
    parser.add_argument("--check",      action="store_true", help="环境检测（bluetoothctl/pactl/服务/适配器）")
    parser.add_argument("--scan",       action="store_true", help="扫描附近蓝牙设备")
    parser.add_argument("--timeout",    type=int, default=10, metavar="秒", help="扫描超时（默认 10s）")
    parser.add_argument("--paired",     action="store_true", help="列出已配对设备")
    parser.add_argument("--connect",    metavar="MAC", help="连接蓝牙设备（格式 XX:XX:XX:XX:XX:XX）")
    parser.add_argument("--sink",       action="store_true", help="检查 PulseAudio/PipeWire Sink")
    parser.add_argument("--audio",      action="store_true", help="蓝牙音频播放测试（TTS，需先连接设备）")
    parser.add_argument("--disconnect", action="store_true", help="断开当前已连接的蓝牙设备")
    args = parser.parse_args()

    print("=" * 60)
    print("  蓝牙模块测试工具（RPi 5 内置 BT5.0 + PipeWire）")
    print("=" * 60)

    try:
        if args.check:
            test_check()
        elif args.scan:
            asyncio.run(test_scan(args.timeout))
        elif args.paired:
            asyncio.run(test_paired())
        elif args.connect:
            asyncio.run(test_connect(args.connect))
        elif args.sink:
            test_sink()
        elif args.audio:
            asyncio.run(test_audio())
        elif args.disconnect:
            asyncio.run(test_disconnect())
        else:
            asyncio.run(interactive_menu())
    except KeyboardInterrupt:
        print("\n\n⚠️  用户中断，退出测试")


if __name__ == "__main__":
    main()
