"""
Microphone — 麦克风硬件抽象层
================================
职责：
  1. 持续采集 PCM 流（sounddevice）
  2. VAD 检测语音活动（webrtcvad）
  3. 通过回调通知上层：语音开始 / 语音结束（携带原始 PCM bytes）
  4. mute/unmute 控制（外放时暂停 VAD 防止回声）

不含任何 WebSocket / Spine 逻辑，纯硬件操作。

硬件：USB 免驱声卡（Type-C 接口），接入树莓派 USB-A 口后注册为 ALSA USB Audio 设备。
      优先使用 find_usb_audio_device() 自动检测，也可通过 device 参数手动指定。

采样率策略：
  优先尝试 16000Hz（webrtcvad 原生支持）；
  若设备不支持，自动回退到 48000Hz 并以 3:1 均值抽取降采样至 16000Hz。

依赖：sounddevice, webrtcvad, numpy
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable

import numpy as np

logger = logging.getLogger(__name__)

SAMPLE_RATE = 16000          # webrtcvad 目标采样率（Hz）
CHANNELS = 1                 # USB 音频模块麦克风为单声道
FRAME_DURATION_MS = 30       # webrtcvad 支持 10/20/30ms
FRAME_SIZE = int(SAMPLE_RATE * FRAME_DURATION_MS / 1000)  # 480 samples @ 16kHz

# 设为 None 则使用 find_usb_audio_device() 自动检测，未检测到时再退到 ALSA 默认设备
DEFAULT_DEVICE: str | None = None

SILENCE_THRESHOLD_MS = 800   # 静音多久判定说话结束
SILENCE_FRAMES = SILENCE_THRESHOLD_MS // FRAME_DURATION_MS

MIN_SPEECH_MS = 300          # 低于此时长的片段丢弃（过滤噪声）
MIN_SPEECH_FRAMES = MIN_SPEECH_MS // FRAME_DURATION_MS

# 采样率回退顺序：先尝试 16000Hz，若不支持则尝试 48000Hz（3:1 整数比，可无损降采样）
_PROBE_RATES = [16000, 48000]


# ─── 工具函数 ─────────────────────────────────────────────────────────

def find_usb_audio_device() -> str | None:
    """
    自动检测 USB 音频输入设备。

    遍历 sounddevice 设备列表，返回第一个名称含 "usb" 的输入设备名称；
    未找到时返回 None（调用方退到 ALSA 默认设备）。
    """
    try:
        import sounddevice as sd
        for dev in sd.query_devices():
            if dev["max_input_channels"] > 0 and "usb" in dev["name"].lower():
                logger.info(f"[Microphone] 自动检测到 USB 音频设备：{dev['name']!r}")
                return dev["name"]
    except Exception as e:
        logger.debug(f"[Microphone] USB 设备检测失败：{e}")
    return None


def _decimate(signal: np.ndarray, factor: int) -> np.ndarray:
    """
    均值抽取降采样（相当于一阶盒式低通滤波 + 整数倍抽取）。

    适用于 webrtcvad 对音质要求不高的场景（只需语音活动检测）。
    factor=3 时将 48000Hz 降为 16000Hz。
    """
    if factor == 1:
        return signal
    n = (len(signal) // factor) * factor
    return signal[:n].reshape(-1, factor).mean(axis=1).astype(np.int16)


def _probe_input_settings(sd, device: str | None) -> tuple[int, int]:
    """
    探测设备支持的采样率，返回 (native_rate, downsample_factor)。

    先尝试 16000Hz，再尝试 48000Hz，均失败则抛出 RuntimeError。
    """
    for rate in _PROBE_RATES:
        try:
            sd.check_input_settings(
                device=device,
                channels=CHANNELS,
                dtype="int16",
                samplerate=rate,
            )
            factor = rate // SAMPLE_RATE  # 16000→1，48000→3
            if rate != SAMPLE_RATE:
                logger.info(
                    f"[Microphone] 设备不支持 {SAMPLE_RATE}Hz，"
                    f"回退至 {rate}Hz（降采样因子 {factor}）"
                )
            return rate, factor
        except Exception:
            continue

    raise RuntimeError(
        f"[Microphone] 设备 {device!r} 不支持 {_PROBE_RATES}Hz，"
        f"请用 `python microphone_test.py --list` 查看支持的采样率"
    )


# ─── 主类 ─────────────────────────────────────────────────────────────

class Microphone:
    """
    麦克风 VAD 采集器（纯硬件层）。

    通过回调向上层推送语音事件，不依赖 WebSocket / 任何网络组件。

    Args:
        on_speech_start:    检测到语音开始时调用（异步）
        on_speech_end:      语音结束时调用，参数为 (raw_pcm: bytes, sample_rate: int, duration_ms: int)
        vad_aggressiveness: webrtcvad 灵敏度，0=最宽松，3=最严格，默认 2
        device:             音频输入设备名或 None。
                            None 时先用 find_usb_audio_device() 自动检测 USB 设备，
                            再退到 ALSA 系统默认设备。
    """

    def __init__(
        self,
        on_speech_start: Callable[[], Awaitable[None]] | None = None,
        on_speech_end: Callable[[bytes, int, int], Awaitable[None]] | None = None,
        vad_aggressiveness: int = 2,
        device: str | None = DEFAULT_DEVICE,
    ):
        self._on_speech_start = on_speech_start
        self._on_speech_end = on_speech_end
        self._vad_aggressiveness = vad_aggressiveness
        # device=None 时在 start() 内自动检测 USB 设备
        self._device = device

        self._is_muted = False
        self._is_speaking = False
        self._speech_buffer: list[bytes] = []
        self._silent_frames = 0
        self._loop: asyncio.AbstractEventLoop | None = None
        self._vad = None  # 延迟初始化，避免 import 时崩溃

    # ─── 公共控制接口 ─────────────────────────────────────────────────

    def mute(self) -> None:
        """外放时静音，防止扬声器声音触发 VAD（防回声）。"""
        self._is_muted = True
        logger.debug("[Microphone] 已静音")

    def unmute(self) -> None:
        """外放结束后恢复监听。"""
        self._is_muted = False
        logger.debug("[Microphone] 已恢复")

    @property
    def is_muted(self) -> bool:
        return self._is_muted

    # ─── 启动 ─────────────────────────────────────────────────────────

    async def start(self) -> None:
        """启动持续采集循环（阻塞，应在独立 task 中运行）。"""
        try:
            import sounddevice as sd
            import webrtcvad
        except ImportError as e:
            logger.error(f"[Microphone] 缺少依赖：{e}，请安装 sounddevice 和 webrtcvad")
            return

        self._vad = webrtcvad.Vad(self._vad_aggressiveness)
        self._loop = asyncio.get_event_loop()

        # 优先自动检测 USB 设备
        device = self._device
        if device is None:
            device = find_usb_audio_device()

        # 探测设备实际支持的采样率
        try:
            native_rate, downsample = _probe_input_settings(sd, device)
        except RuntimeError as e:
            logger.error(str(e))
            return

        native_blocksize = int(native_rate * FRAME_DURATION_MS / 1000)

        logger.info(
            f"[Microphone] 启动：设备={device!r}，"
            f"采集={native_rate}Hz，目标={SAMPLE_RATE}Hz，"
            f"降采样因子={downsample}，VAD 灵敏度={self._vad_aggressiveness}"
        )

        def _sd_callback(indata: np.ndarray, frames: int, time, status):
            if status:
                logger.warning(f"[Microphone] sounddevice 状态：{status}")
            if self._loop and self._loop.is_running():
                frame = indata[:, 0].copy().astype(np.int16)
                if downsample > 1:
                    frame = _decimate(frame, downsample)
                asyncio.run_coroutine_threadsafe(
                    self._process_frame(frame.tobytes()), self._loop
                )

        with sd.InputStream(
            samplerate=native_rate,
            channels=CHANNELS,
            dtype="int16",
            blocksize=native_blocksize,
            callback=_sd_callback,
            device=device,
        ):
            logger.info("[Microphone] 麦克风已开启，开始监听...")
            await asyncio.sleep(float("inf"))

    # ─── 内部 VAD 状态机 ──────────────────────────────────────────────

    async def _process_frame(self, pcm_bytes: bytes) -> None:
        if self._is_muted or self._vad is None:
            return

        # webrtcvad 要求帧长精确（480 bytes = 240 个 int16 samples @ 16kHz 30ms）
        if len(pcm_bytes) != FRAME_SIZE * 2:
            return

        try:
            is_speech = self._vad.is_speech(pcm_bytes, SAMPLE_RATE)
        except Exception:
            return

        if is_speech:
            if not self._is_speaking:
                self._is_speaking = True
                self._speech_buffer.clear()
                self._silent_frames = 0
                if self._on_speech_start:
                    await self._on_speech_start()
                logger.debug("[Microphone] → speech_start")

            self._speech_buffer.append(pcm_bytes)
            self._silent_frames = 0

        else:
            if self._is_speaking:
                self._silent_frames += 1
                self._speech_buffer.append(pcm_bytes)  # 保留尾部静音，避免截断

                if self._silent_frames >= SILENCE_FRAMES:
                    await self._flush_speech()

    async def _flush_speech(self) -> None:
        """将缓冲的语音块打包，回调上层，然后重置状态。"""
        self._is_speaking = False

        if len(self._speech_buffer) < MIN_SPEECH_FRAMES:
            logger.debug("[Microphone] 语音过短，丢弃")
            self._speech_buffer.clear()
            self._silent_frames = 0
            return

        raw_pcm = b"".join(self._speech_buffer)
        duration_ms = len(self._speech_buffer) * FRAME_DURATION_MS

        if self._on_speech_end:
            await self._on_speech_end(raw_pcm, SAMPLE_RATE, duration_ms)

        logger.debug(f"[Microphone] → speech_end ({duration_ms}ms, {len(raw_pcm)} bytes)")

        self._speech_buffer.clear()
        self._silent_frames = 0
