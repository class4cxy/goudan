#!/usr/bin/env python3
"""
connect_speaker.py — 连接蓝牙外放音响
=======================================
将指定蓝牙音箱配对、信任、连接，并设为 PulseAudio/PipeWire 默认输出设备，
最后播放一段测试音确认链路正常。

首次使用（未配对）：
  python3 connect_speaker.py --scan              # 先扫描找到音箱 MAC
  python3 connect_speaker.py AA:BB:CC:DD:EE:FF   # 配对并连接

已配对后（每次开机）：
  python3 connect_speaker.py AA:BB:CC:DD:EE:FF   # 直接重连，无需重新扫描

其他选项：
  python3 connect_speaker.py --scan --timeout 15 # 扫描 15 秒
  python3 connect_speaker.py --status            # 查看当前连接状态
  python3 connect_speaker.py --disconnect        # 断开当前音箱
  python3 connect_speaker.py AA:BB:CC:DD:EE:FF --no-test   # 连接但不播测试音
  python3 connect_speaker.py AA:BB:CC:DD:EE:FF --test-only # 只播测试音（已连接）

环境变量：
  SPEAKER_MAC=AA:BB:CC:DD:EE:FF  # 可配置默认 MAC，运行时无需手动输入
"""

import argparse
import asyncio
import os
import sys
import time
from pathlib import Path

# 加入 devices 目录路径
sys.path.insert(0, str(Path(__file__).parent / "devices"))

DIVIDER = "─" * 55


def _banner(title: str):
    print(f"\n{'═' * 55}")
    print(f"  {title}")
    print(f"{'═' * 55}")


def _ok(msg: str):   print(f"  ✅  {msg}")
def _fail(msg: str): print(f"  ❌  {msg}")
def _warn(msg: str): print(f"  ⚠️   {msg}")
def _info(msg: str): print(f"  ℹ️   {msg}")
def _step(n: int, total: int, msg: str): print(f"  [{n}/{total}] {msg}...", flush=True)


# ── 扫描 ────────────────────────────────────────────────────────────

async def do_scan(timeout_s: int) -> list[dict]:
    from bluetooth import BluetoothManager
    _banner(f"扫描附近蓝牙设备（{timeout_s}s）")
    print("  请确保音箱处于配对/可发现模式...\n")

    bt = BluetoothManager()
    await bt.probe()

    t0 = time.perf_counter()
    devices = await bt.scan(timeout_s=timeout_s)
    elapsed = time.perf_counter() - t0

    if not devices:
        _warn(f"扫描完成（{elapsed:.1f}s），未发现任何设备")
        print("  提示：确认音箱已开机并进入配对模式（通常长按电源键）")
        return []

    print(f"  发现 {len(devices)} 台设备（耗时 {elapsed:.1f}s）：\n")
    print(f"  {'序号':<4}  {'MAC 地址':<20}  名称")
    print(f"  {'─'*4}  {'─'*20}  {'─'*28}")
    for i, d in enumerate(devices, 1):
        print(f"  {i:<4}  {d['mac']:<20}  {d['name']}")

    print()
    _info("找到目标音箱后，运行：")
    print(f"       python3 connect_speaker.py <MAC 地址>")
    return devices


# ── 连接 ────────────────────────────────────────────────────────────

async def do_connect(mac: str, play_test: bool = True) -> bool:
    from bluetooth import BluetoothManager
    _banner(f"连接蓝牙音箱：{mac}")

    bt = BluetoothManager()
    await bt.probe()

    if bt.is_simulation:
        _warn("bluetoothctl 不可用（开发机环境），模拟连接")
        return False

    steps = 4 if play_test else 3
    _step(1, steps, "配对 + 信任 + 连接（pair → trust → connect）")
    t0 = time.perf_counter()
    ok = await bt.connect(mac)
    elapsed = time.perf_counter() - t0

    if not ok:
        _fail(f"连接失败（{elapsed:.1f}s）")
        print()
        _info("常见解决方法：")
        print("    1. 确认音箱已开机，若支持配对模式请开启")
        print("    2. 检查蓝牙适配器是否上电：bluetoothctl power on")
        print("    3. 若音箱已连接到手机，请先在手机上断开")
        print("    4. 删除旧配对记录后重试：bluetoothctl remove <MAC>")
        return False

    status = bt.status()
    print(f"        → 已连接 {status['name']} ✅（{elapsed:.1f}s）")

    _step(2, steps, "将蓝牙设为 PulseAudio 默认输出 sink")
    # connect() 内部已调用 _set_default_sink，这里只做验证
    import subprocess
    r = subprocess.run(["pactl", "get-default-sink"], capture_output=True, text=True, timeout=3)
    default_sink = r.stdout.strip() if r.returncode == 0 else "（pactl 不可用）"
    is_bt = "bluez" in default_sink.lower()
    if is_bt:
        print(f"        → 默认 sink：{default_sink} ✅")
    else:
        print(f"        → 默认 sink：{default_sink}")
        _warn("默认 sink 不是蓝牙设备，TTS 可能输出到其他设备")
        _info("手动设置：pactl set-default-sink <sink_name>")
        _info("查看 sink 列表：pactl list sinks short | grep bluez")

    _step(3, steps, "检查 A2DP 音频配置文件")
    r2 = subprocess.run(
        ["bluetoothctl", "info", mac.upper()],
        capture_output=True, text=True, timeout=5
    )
    if r2.returncode == 0:
        has_a2dp = "a2dp" in r2.stdout.lower() or "Audio Sink" in r2.stdout
        if has_a2dp:
            print(f"        → A2DP 音频配置文件已加载 ✅")
        else:
            print(f"        → 未找到明确 A2DP 信息，继续测试...")
    else:
        print(f"        → 无法获取设备信息（正常，继续...）")

    if play_test:
        _step(4, steps, "播放测试音验证音频链路")
        await _play_test_tone()

    print()
    _ok(f"蓝牙音箱已就绪：{status['name']} ({mac})")
    print()
    _info("现在可以启动 Platform 服务（需设置环境变量）：")
    print(f"       SPEAKER_BACKEND=pulseaudio npm run platform")
    print()
    _info("或在 .env 中永久配置：")
    print(f"       SPEAKER_BACKEND=pulseaudio")
    print()
    _info("已配对设备下次开机自动重连：")
    print(f"       python3 connect_speaker.py {mac}")

    return True


# ── 播放测试音 ──────────────────────────────────────────────────────

async def _play_test_tone():
    """通过 pacat/aplay 播放一段 TTS 测试音。"""
    missing = []
    for pkg in ["edge_tts", "soundfile", "numpy"]:
        try:
            __import__(pkg)
        except ImportError:
            missing.append(pkg.replace("_", "-"))

    if missing:
        _warn(f"缺少依赖 {missing}，跳过音频测试（pip install {' '.join(missing)}）")
        return

    import subprocess
    rc = subprocess.run(["pacat", "--version"], capture_output=True).returncode
    backend = "pulseaudio" if rc == 0 else "alsa"
    os.environ["SPEAKER_BACKEND"] = backend

    from speaker import Speaker

    done_event = asyncio.Event()

    def on_end():
        done_event.set()

    sp = Speaker(on_play_end=on_end)
    sp_task = asyncio.create_task(sp.start())
    await asyncio.sleep(0.1)

    test_text = "蓝牙连接成功，外放音响已就绪，机器人语音输出正常。"
    print(f"        → 播放：{test_text!r}")

    await sp.enqueue(test_text)

    try:
        await asyncio.wait_for(done_event.wait(), timeout=20)
        print(f"        → 播放完成 ✅")
    except asyncio.TimeoutError:
        _warn("播放超时（20s），可能音频未路由到蓝牙设备")

    sp_task.cancel()
    try:
        await sp_task
    except (asyncio.CancelledError, Exception):
        pass


# ── 查看状态 ────────────────────────────────────────────────────────

async def do_status():
    _banner("当前蓝牙连接状态")

    import subprocess

    # 通过 bluetoothctl devices Connected 查当前已连接设备
    r = subprocess.run(
        ["bluetoothctl", "devices", "Connected"],
        capture_output=True, text=True, timeout=5
    )
    if r.returncode == 0 and r.stdout.strip():
        print(f"  已连接设备：\n")
        import re
        for line in r.stdout.strip().splitlines():
            m = re.search(r"([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}", line)
            mac = m.group(0) if m else "?"
            name = line.split(mac)[-1].strip() if mac != "?" else line
            print(f"    {mac}  {name}")
        print()
    else:
        _warn("当前无已连接蓝牙设备")

    # PulseAudio 默认 sink
    r2 = subprocess.run(["pactl", "get-default-sink"], capture_output=True, text=True, timeout=3)
    if r2.returncode == 0:
        sink = r2.stdout.strip()
        is_bt = "bluez" in sink.lower()
        tag = "🔵 蓝牙" if is_bt else "🖥  非蓝牙"
        print(f"  PulseAudio 默认 sink：{tag}")
        print(f"    {sink}")
    else:
        _warn("pactl 不可用，无法查询默认 sink")

    print()


# ── 断开 ────────────────────────────────────────────────────────────

async def do_disconnect():
    _banner("断开蓝牙音箱")

    import subprocess, re
    r = subprocess.run(
        ["bluetoothctl", "devices", "Connected"],
        capture_output=True, text=True, timeout=5
    )
    if r.returncode != 0 or not r.stdout.strip():
        _warn("未检测到已连接的蓝牙设备")
        return

    from bluetooth import BluetoothManager
    bt = BluetoothManager()

    macs = re.findall(r"(?:[0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}", r.stdout)
    for mac in macs:
        mac = mac.rstrip(":").upper()
        bt._connected_mac = mac
        ok = await bt.disconnect(mac)
        if ok:
            _ok(f"已断开：{mac}")
        else:
            _fail(f"断开失败：{mac}")

    print()


# ── 主入口 ──────────────────────────────────────────────────────────

def main():
    # 支持从环境变量读取默认 MAC
    default_mac = os.environ.get("SPEAKER_MAC", "")

    parser = argparse.ArgumentParser(
        description="连接蓝牙外放音响并设为系统默认音频输出",
        formatter_class=argparse.RawTextHelpFormatter,
        epilog=f"""
示例：
  python3 connect_speaker.py --scan                     扫描附近设备
  python3 connect_speaker.py AA:BB:CC:DD:EE:FF          配对并连接
  python3 connect_speaker.py AA:BB:CC:DD:EE:FF --no-test  跳过测试音
  python3 connect_speaker.py --status                   查看当前连接状态
  python3 connect_speaker.py --disconnect               断开音箱

环境变量：
  SPEAKER_MAC=AA:BB:CC:DD:EE:FF  设置默认 MAC，省略命令行参数
{"当前默认 MAC（SPEAKER_MAC）：" + default_mac if default_mac else ""}
""",
    )
    parser.add_argument(
        "mac", nargs="?", default=default_mac,
        metavar="MAC",
        help="蓝牙音箱 MAC 地址（格式 XX:XX:XX:XX:XX:XX）",
    )
    parser.add_argument("--scan",       action="store_true", help="扫描附近蓝牙设备")
    parser.add_argument("--timeout",    type=int, default=10, metavar="秒", help="扫描超时（默认 10s）")
    parser.add_argument("--status",     action="store_true", help="查看当前蓝牙连接状态")
    parser.add_argument("--disconnect", action="store_true", help="断开当前已连接音箱")
    parser.add_argument("--no-test",    action="store_true", help="连接后跳过测试音播放")
    parser.add_argument("--test-only",  action="store_true", help="只播测试音（设备已连接时使用）")
    args = parser.parse_args()

    print("=" * 55)
    print("  蓝牙音响连接工具 — goudan 机器人")
    print("=" * 55)

    try:
        if args.scan:
            asyncio.run(do_scan(args.timeout))

        elif args.status:
            asyncio.run(do_status())

        elif args.disconnect:
            asyncio.run(do_disconnect())

        elif args.test_only:
            print("\n  仅播放测试音（假设蓝牙已连接）...")
            asyncio.run(_play_test_tone())

        elif args.mac:
            mac = args.mac.upper().strip()
            # 简单格式校验
            import re
            if not re.fullmatch(r"([0-9A-F]{2}:){5}[0-9A-F]{2}", mac):
                print(f"  ❌  MAC 地址格式错误：{mac!r}")
                print("       正确格式：AA:BB:CC:DD:EE:FF")
                sys.exit(1)
            asyncio.run(do_connect(mac, play_test=not args.no_test))

        else:
            # 无参数：交互式引导
            print("\n  未指定操作，进入交互引导...\n")
            print("  选项：")
            print("    1. 扫描附近蓝牙设备（推荐首次使用）")
            print("    2. 输入 MAC 地址直接连接")
            print("    3. 查看当前连接状态")
            print("    q. 退出")
            try:
                choice = input("\n  请选择 > ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                return

            if choice == "1":
                try:
                    t = input("  扫描时长（秒，默认 10）> ").strip()
                    timeout = int(t) if t.isdigit() else 10
                except (EOFError, KeyboardInterrupt):
                    timeout = 10
                asyncio.run(do_scan(timeout))
                try:
                    mac = input("\n  输入要连接的 MAC 地址（直接回车跳过）> ").strip().upper()
                except (EOFError, KeyboardInterrupt):
                    mac = ""
                if mac:
                    asyncio.run(do_connect(mac))
            elif choice == "2":
                try:
                    mac = input("  输入 MAC 地址（格式 XX:XX:XX:XX:XX:XX）> ").strip().upper()
                except (EOFError, KeyboardInterrupt):
                    return
                if mac:
                    asyncio.run(do_connect(mac))
            elif choice == "3":
                asyncio.run(do_status())

    except KeyboardInterrupt:
        print("\n\n⚠️  用户中断")


if __name__ == "__main__":
    main()
