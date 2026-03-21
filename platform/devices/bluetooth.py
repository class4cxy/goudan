"""
BluetoothManager — 蓝牙设备管理
================================
职责：
  - 通过 bluetoothctl 子进程管理蓝牙设备的配对、信任与连接/断开
  - 提供设备扫描（asyncio 超时控制）与状态查询接口
  - 连接成功后通过 pactl 将蓝牙 sink 设为 PulseAudio/PipeWire 默认输出设备
    （使得 pacat 写入 default sink 时自动路由到蓝牙扬声器）

RPi 5 注意事项（Bookworm / PipeWire）：
  - 蓝牙 sink 名通常为 bluez_output.XX_XX_XX_XX_XX_XX.1
  - PipeWire 兼容 PulseAudio API，pactl / pacat 均可正常使用
  - A2DP 立体声问题（仅出现单声道）的修复方式见 docs/HARDWARE.md

依赖：bluetoothctl（bluez），pactl（pulseaudio-utils/pipewire-pulse）
      树莓派 OS Bookworm 默认均已安装；开发机（非 RPi）会降级为模拟模式。
"""

import asyncio
import logging
import re

logger = logging.getLogger(__name__)

_MAC_RE = re.compile(r"([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}")


class BluetoothManager:
    """
    蓝牙设备管理器（bluetoothctl / pactl 封装）。

    所有操作均为异步，通过 asyncio.create_subprocess_exec 调用系统命令，
    无需常驻守护进程。当系统缺少 bluetoothctl 时自动进入模拟模式，
    所有操作返回安全的降级结果。
    """

    def __init__(self) -> None:
        self._connected_mac: str | None = None
        self._connected_name: str | None = None
        self._simulation = False  # 开发机无 bluetoothctl 时设为 True

    # ─── 公共接口 ──────────────────────────────────────────────────────

    async def probe(self) -> None:
        """检测 bluetoothctl 是否可用，不可用时进入模拟模式。"""
        out = await self._run_cmd(["bluetoothctl", "--version"])
        if not out:
            self._simulation = True
            logger.warning("[BT] bluetoothctl 不可用，进入模拟模式（开发机环境）")
        else:
            logger.info("[BT] bluetoothctl 已就绪：%s", out.strip())

    @property
    def is_simulation(self) -> bool:
        return self._simulation

    async def scan(self, timeout_s: int = 10) -> list[dict]:
        """
        扫描附近蓝牙设备，返回 [{mac, name}] 列表。

        实现方式：以 stdin 管道交互模式启动 bluetoothctl，发送 "scan on" 命令后
        持续读取 stdout 中的设备发现事件，到达 timeout_s 后发送 "scan off\\nquit"
        并返回结果。

        注意：`bluetoothctl scan on` 作为独立子进程（无 stdin 管道）时，打印
        "Discovery started" 后 stdout 即关闭，无法接收后续设备事件——必须走
        交互模式（stdin pipe）才能收到 [NEW] Device 行。
        """
        if self._simulation:
            return [{"mac": "00:11:22:33:44:55", "name": "模拟蓝牙音箱（simulation）"}]

        logger.info("[BT] 开始扫描（%ds）...", timeout_s)
        found: dict[str, str] = {}

        # 以交互模式（stdin pipe）运行 bluetoothctl，保持进程存活接收事件
        proc = await asyncio.create_subprocess_exec(
            "bluetoothctl",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )

        # 发送 scan on 命令
        try:
            assert proc.stdin
            proc.stdin.write(b"scan on\n")
            await proc.stdin.drain()
        except Exception as e:
            logger.warning("[BT] 发送 scan on 失败：%s", e)

        async def _read() -> None:
            assert proc.stdout
            async for line in proc.stdout:
                text = line.decode(errors="replace").strip()
                # 过滤控制字符（bluetoothctl 输出含 ANSI 颜色码）
                text = re.sub(r"\x1b\[[0-9;]*m|\x1b\[K", "", text)
                m = _MAC_RE.search(text)
                if not m:
                    continue
                mac = m.group(0).upper()
                name_m = re.search(r"Device\s+[\dA-Fa-f:]+\s+(.+)$", text)
                name = name_m.group(1).strip() if name_m else ""
                if name and not name.startswith("#"):
                    found[mac] = name
                elif mac not in found:
                    found[mac] = mac
                logger.debug("[BT] 发现设备：%s  %s", mac, found.get(mac, ""))

        try:
            await asyncio.wait_for(_read(), timeout=timeout_s)
        except asyncio.TimeoutError:
            pass
        finally:
            # 先礼貌退出，再强杀
            try:
                assert proc.stdin
                proc.stdin.write(b"scan off\nquit\n")
                await proc.stdin.drain()
            except Exception:
                pass
            await asyncio.sleep(0.3)
            try:
                proc.kill()
            except Exception:
                pass
            try:
                await asyncio.wait_for(proc.wait(), timeout=2.0)
            except Exception:
                pass

        # 补充：将 bluetoothctl devices 的缓存结果合并进来
        # （scan 期间发现的无名设备，可能在 devices 列表里有名字）
        cached = await self._get_cached_devices()
        for d in cached:
            mac = d["mac"]
            if mac not in found:
                found[mac] = d["name"]
            elif found[mac] == mac and d["name"] != mac:
                found[mac] = d["name"]

        result = [{"mac": mac, "name": name} for mac, name in found.items()]
        logger.info("[BT] 扫描完成，发现 %d 台设备", len(result))
        return result

    async def _get_cached_devices(self) -> list[dict]:
        """从 bluetoothctl devices 获取 BlueZ 缓存的设备列表（无需扫描）。"""
        out = await self._run_cmd(["bluetoothctl", "devices"])
        devices = []
        for line in out.splitlines():
            m = _MAC_RE.search(line)
            if not m:
                continue
            mac = m.group(0).upper()
            name_m = re.search(r"Device\s+[\dA-Fa-f:]+\s+(.+)$", line)
            name = name_m.group(1).strip() if name_m else mac
            devices.append({"mac": mac, "name": name})
        return devices

    async def get_paired_devices(self) -> list[dict]:
        """返回已配对设备列表 [{mac, name}]。"""
        if self._simulation:
            return []
        out = await self._run_cmd(["bluetoothctl", "paired-devices"])
        devices = []
        for line in out.splitlines():
            m = _MAC_RE.search(line)
            if not m:
                continue
            mac = m.group(0).upper()
            name_m = re.search(r"Device\s+\S+\s+(.+)$", line)
            name = name_m.group(1).strip() if name_m else mac
            devices.append({"mac": mac, "name": name})
        return devices

    async def connect(self, mac: str) -> bool:
        """
        配对 → 信任 → 连接，成功后将蓝牙设备设为默认 PulseAudio sink。

        若设备已配对则跳过配对步骤；失败返回 False，不抛出异常。
        """
        if self._simulation:
            self._connected_mac = mac.upper()
            self._connected_name = "模拟音箱"
            logger.info("[BT][sim] 模拟连接：%s", mac)
            return True

        mac = mac.upper()
        logger.info("[BT] 正在连接：%s", mac)

        await self._run_cmd(["bluetoothctl", "pair", mac])
        await self._run_cmd(["bluetoothctl", "trust", mac])
        out = await self._run_cmd(["bluetoothctl", "connect", mac], timeout_s=15.0)

        success = (
            "Connection successful" in out
            or "already connected" in out.lower()
        )

        if success:
            self._connected_mac = mac
            # 获取设备名
            info = await self._run_cmd(["bluetoothctl", "info", mac])
            name_m = re.search(r"Name:\s+(.+)$", info, re.MULTILINE)
            self._connected_name = name_m.group(1).strip() if name_m else mac
            logger.info("[BT] ✅ 已连接：%s (%s)", self._connected_name, mac)
            # A2DP sink 注册需要 1-2s，稍等后再设置，最多重试 5 次
            await self._set_default_sink_with_retry(mac)
        else:
            logger.warning("[BT] 连接失败，返回信息：%s", out[:300])

        return success

    async def disconnect(self, mac: str | None = None) -> bool:
        """断开指定设备（默认断开当前已连接设备）。"""
        target = (mac or self._connected_mac or "").upper()
        if not target:
            logger.warning("[BT] 无已连接设备可断开")
            return False

        if self._simulation:
            self._connected_mac = None
            self._connected_name = None
            logger.info("[BT][sim] 模拟断开")
            return True

        out = await self._run_cmd(["bluetoothctl", "disconnect", target])
        success = "Successful disconnected" in out or "not connected" in out.lower()
        if success and target == self._connected_mac:
            self._connected_mac = None
            self._connected_name = None
        logger.info("[BT] 断开 %s：%s", target, "成功" if success else "失败（%s）" % out[:100])
        return success

    def status(self) -> dict:
        """返回当前连接状态快照。"""
        return {
            "connected": self._connected_mac is not None,
            "mac": self._connected_mac,
            "name": self._connected_name,
            "simulation": self._simulation,
        }

    # ─── 内部工具 ──────────────────────────────────────────────────────

    async def _run_cmd(self, cmd: list[str], timeout_s: float = 10.0) -> str:
        """运行系统命令，返回 stdout 文本；超时或命令不存在时返回空字符串。"""
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
            return stdout.decode(errors="replace")
        except asyncio.TimeoutError:
            logger.warning("[BT] 命令超时（%.0fs）：%s", timeout_s, " ".join(cmd))
            return ""
        except FileNotFoundError:
            logger.debug("[BT] 命令不存在：%s", cmd[0])
            return ""
        except Exception as e:
            logger.warning("[BT] 命令异常：%s → %s", " ".join(cmd), e)
            return ""

    async def _set_default_sink_with_retry(
        self, mac: str, max_retries: int = 5, interval_s: float = 1.5
    ) -> bool:
        """
        等待 PipeWire 注册 A2DP sink 后将其设为默认输出，最多重试 max_retries 次。

        A2DP profile 协商完成后，PipeWire 需要约 1-2s 才会注册 bluez_output sink，
        立即调用 pactl set-default-sink 会因 sink 尚不存在而失败。
        """
        mac_underscore = mac.replace(":", "_")
        for attempt in range(1, max_retries + 1):
            await asyncio.sleep(interval_s)
            sink = await self._find_bt_sink(mac_underscore)
            if not sink:
                logger.debug("[BT] 第 %d/%d 次：sink 尚未注册，继续等待...", attempt, max_retries)
                continue
            out = await self._run_cmd(["pactl", "set-default-sink", sink])
            if "Failure" not in out:
                logger.info("[BT] ✅ PulseAudio 默认输出已设为：%s（第 %d 次尝试）", sink, attempt)
                return True
            logger.debug("[BT] 第 %d/%d 次 set-default-sink 失败：%s", attempt, max_retries, out.strip())

        logger.warning(
            "[BT] 设置默认 sink 失败（重试 %d 次），请手动运行：\n"
            "       pactl list sinks short | grep bluez\n"
            "       pactl set-default-sink <sink_name>",
            max_retries,
        )
        return False

    async def _find_bt_sink(self, mac_underscore: str) -> str | None:
        """从 pactl list sinks short 中找到与 MAC 匹配的蓝牙 sink 名。"""
        out = await self._run_cmd(["pactl", "list", "sinks", "short"])
        mac_lower = mac_underscore.lower()
        for line in out.splitlines():
            line_lower = line.lower()
            if mac_lower in line_lower or "bluez" in line_lower:
                parts = line.split()
                if len(parts) >= 2:
                    return parts[1]
        return None

    async def _set_default_sink(self, mac: str) -> None:
        """单次尝试设置默认 sink（不重试）。内部使用，外部调用请用 _set_default_sink_with_retry。"""
        mac_underscore = mac.replace(":", "_")
        sink_name = f"bluez_output.{mac_underscore}.1"
        out = await self._run_cmd(["pactl", "set-default-sink", sink_name])
        if "Failure" in out:
            logger.warning("[BT] 设置默认 sink 失败（%s）", out.strip())
        else:
            logger.info("[BT] PulseAudio 默认输出已设为：%s", sink_name)
