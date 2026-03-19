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

质量门控（_flush_speech_sync）：
  - voiced_ratio：实时 VAD 语音帧占语音段（不含尾部静音）的比例，低于阈值丢弃
  - rms_dbfs：片段整体能量，低于阈值（过静）丢弃
  - strict_voiced_ratio：片段结束后用更严格 VAD 二次复检，低于阈值丢弃
  三项门控均以 WARNING 记录，便于在生产日志中直接看到被丢弃的原因。

性能说明：
  VAD 状态机在 sounddevice 音频回调线程中同步执行，仅在语音开始/结束事件时才通过
  run_coroutine_threadsafe 跨线程通知 asyncio 事件循环。
  避免了每帧（33次/秒）跨线程投递协程导致的事件循环积压和 input overflow。

依赖：sounddevice, webrtcvad, numpy
"""

import asyncio
import logging
import os
import time
from collections.abc import Awaitable, Callable

import numpy as np

logger = logging.getLogger(__name__)

SAMPLE_RATE = 16000          # webrtcvad 目标采样率（Hz）
CHANNELS = 1                 # USB 音频模块麦克风为单声道
FRAME_DURATION_MS = 30       # webrtcvad 支持 10/20/30ms
FRAME_SIZE = int(SAMPLE_RATE * FRAME_DURATION_MS / 1000)  # 480 samples @ 16kHz

# sounddevice 回调块大小：300ms 回调一次（vs 30ms 时 33次/秒），大幅降低 GIL 争抢导致的 input overflow
# 内部将 300ms 块拆分为多个 30ms VAD 帧逐帧处理
BLOCK_DURATION_MS = 300

# 设为 None 则使用 find_usb_audio_device() 自动检测，未检测到时再退到 ALSA 默认设备
DEFAULT_DEVICE: str | None = None

SILENCE_THRESHOLD_MS = 1500   # 静音多久判定说话结束
SILENCE_FRAMES = SILENCE_THRESHOLD_MS // FRAME_DURATION_MS

MIN_SPEECH_MS = 300          # 低于此时长的片段丢弃（过滤噪声）
MIN_SPEECH_FRAMES = MIN_SPEECH_MS // FRAME_DURATION_MS

# 语音片段质量门控：
# 1) voiced_ratio：被 VAD 判为语音的帧占比
# 2) rms_dbfs：片段整体能量（dBFS，越接近 0 越响）
MIN_VOICED_RATIO = float(os.environ.get("MIC_MIN_VOICED_RATIO", "0.20"))
MIN_CLIP_RMS_DBFS = float(os.environ.get("MIC_MIN_CLIP_RMS_DBFS", "-50"))
MAX_SPEECH_MS = int(os.environ.get("MIC_MAX_SPEECH_MS", "9000"))
MAX_SPEECH_FRAMES = max(1, MAX_SPEECH_MS // FRAME_DURATION_MS)
POST_VAD_ENABLED = os.environ.get("MIC_POST_VAD_ENABLED", "1") != "0"
POST_VAD_AGGRESSIVENESS = int(os.environ.get("MIC_POST_VAD_AGGRESSIVENESS", "3"))
POST_MIN_VOICED_RATIO = float(os.environ.get("MIC_POST_MIN_VOICED_RATIO", "0.35"))
# unmute 后的混响保护期：该窗口内开始的语音片段被视为 TTS 混响丢弃（防止扬声器回声进入 STT）
POST_UNMUTE_GRACE_MS = int(os.environ.get("MIC_POST_UNMUTE_GRACE_MS", "800"))
# mute 超时：静音超过此时长强制 unmute，防止 Speaker 异常/卡死导致录音永久休眠
MUTE_TIMEOUT_S = float(os.environ.get("MIC_MUTE_TIMEOUT_S", "25"))

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
        on_speech_end:      语音结束时调用，参数为 (raw_pcm, sample_rate, duration_ms, **kwargs)，可选 kwargs 含 vad_flush_ms
        vad_aggressiveness: webrtcvad 灵敏度，0=最宽松，3=最严格，默认 2
        device:             音频输入设备名或 None。
                            None 时先用 find_usb_audio_device() 自动检测 USB 设备，
                            再退到 ALSA 系统默认设备。
    """

    def __init__(
        self,
        on_speech_start: Callable[[], Awaitable[None]] | None = None,
        on_speech_end: Callable[..., Awaitable[None]] | None = None,
        vad_aggressiveness: int = 1,
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
        self._voiced_frames = 0
        self._loop: asyncio.AbstractEventLoop | None = None
        self._vad = None  # 延迟初始化，避免 import 时崩溃
        self._post_vad = None
        self._unmute_at: float = 0.0       # monotonic 时间戳，上次 unmute() 的时刻
        self._muted_at: float = 0.0        # monotonic 时间戳，上次 mute() 的时刻
        self._speech_start_at: float = 0.0  # monotonic 时间戳，当前语音段开始时刻

    # ─── 公共控制接口 ─────────────────────────────────────────────────

    def mute(self) -> None:
        """外放时静音，防止扬声器声音触发 VAD（防回声）。"""
        self._is_muted = True
        self._muted_at = time.monotonic()
        logger.debug("[Microphone] 已静音")

    def unmute(self) -> None:
        """外放结束后恢复监听，同时清除静音期间可能残留的 VAD 脏状态。"""
        self._is_muted = False
        self._unmute_at = time.monotonic()
        # 静音期间 VAD 状态机被冻结，若 _is_speaking 仍为 True 则说明有未 flush 的脏缓冲，
        # 直接丢弃，防止 TTS 回声数据被当作用户语音发给 STT。
        if self._is_speaking:
            logger.debug(f"[Microphone] unmute 时丢弃脏缓冲 {len(self._speech_buffer)} 帧")
            self._is_speaking = False
            self._speech_buffer.clear()
            self._silent_frames = 0
            self._voiced_frames = 0
        logger.info("[Microphone] 已恢复监听")

    @property
    def is_muted(self) -> bool:
        return self._is_muted

    # ─── 启动 ─────────────────────────────────────────────────────────

    async def start(self) -> None:
        """启动持续采集循环（阻塞，应在独立 task 中运行）。"""
        logger.info("[Microphone] start() 被调用")
        try:
            import sounddevice as sd
            import webrtcvad
        except (ImportError, OSError) as e:
            logger.error(f"[Microphone] 缺少依赖或系统库：{e}，请安装 sounddevice/webrtcvad 及 libportaudio2")
            return

        try:
            await self._run(sd, webrtcvad)
        except Exception as e:
            logger.error(f"[Microphone] 采集任务异常退出：{e}", exc_info=True)

    async def _run(self, sd, webrtcvad) -> None:
        """实际采集逻辑（从 start 分离，方便统一捕获异常）。"""
        self._vad = webrtcvad.Vad(self._vad_aggressiveness)
        if POST_VAD_ENABLED:
            # 二次门控只用于片段收尾复检，不参与实时状态机，避免引入 speech_start/speech_end 卡死问题。
            self._post_vad = webrtcvad.Vad(max(0, min(3, POST_VAD_AGGRESSIVENESS)))
        self._loop = asyncio.get_running_loop()

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

        # 用更大的回调块（100ms）降低回调频率，减少 GIL 争抢和 input overflow
        native_blocksize = int(native_rate * BLOCK_DURATION_MS / 1000)
        # 每个 100ms 块解码后（降采样至 16kHz）包含的 30ms VAD 帧数
        vad_frames_per_block = BLOCK_DURATION_MS // FRAME_DURATION_MS

        logger.info(
            f"[Microphone] 启动：设备={device!r}，"
            f"采集={native_rate}Hz，目标={SAMPLE_RATE}Hz，"
            f"降采样因子={downsample}，块大小={BLOCK_DURATION_MS}ms，"
            f"VAD 灵敏度={self._vad_aggressiveness}，"
            f"二次门控={'on' if POST_VAD_ENABLED else 'off'}"
        )

        # 用于在回调线程积累 overflow 计数，每秒最多打印一次，避免 I/O 卡回调
        _overflow_count = [0]

        def _sd_callback(indata: np.ndarray, frames: int, time, status):
            # 回调运行在 sounddevice 专用音频线程，必须快速返回。
            # 仅累计 overflow 计数；VAD 状态机同步完成；只在语音事件时才跨线程通知 asyncio。
            if status and status.input_overflow:
                _overflow_count[0] += 1

            frame = indata[:, 0].copy().astype(np.int16)
            if downsample > 1:
                frame = _decimate(frame, downsample)

            # 将 100ms 块拆分为 30ms VAD 帧逐帧处理
            for i in range(vad_frames_per_block):
                chunk = frame[i * FRAME_SIZE : (i + 1) * FRAME_SIZE]
                if len(chunk) == FRAME_SIZE:
                    self._process_frame_sync(chunk.tobytes())

        def _flush_overflow_log():
            """定期把积累的 overflow 计数打印为一条 warning，避免在音频线程做 I/O。"""
            count = _overflow_count[0]
            if count > 0:
                logger.warning(f"[Microphone] input overflow ×{count}（过去 5s）")
                _overflow_count[0] = 0

        with sd.InputStream(
            samplerate=native_rate,
            channels=CHANNELS,
            dtype="int16",
            blocksize=native_blocksize,
            latency="high",        # 更大的内部缓冲，进一步防止 overflow
            callback=_sd_callback,
            device=device,
        ):
            logger.info("[Microphone] 麦克风已开启，开始监听...")
            while True:
                await asyncio.sleep(5)
                _flush_overflow_log()
                # mute 超时兜底：防止 Speaker 异常导致麦克风永久静音
                if self._is_muted and self._muted_at > 0 and MUTE_TIMEOUT_S > 0:
                    elapsed = time.monotonic() - self._muted_at
                    if elapsed >= MUTE_TIMEOUT_S:
                        logger.warning(
                            "[Microphone] mute 超时（%.0fs >= %.0fs），强制 unmute，避免录音休眠",
                            elapsed, MUTE_TIMEOUT_S,
                        )
                        self._is_muted = False
                        self._unmute_at = time.monotonic()

    # ─── 内部 VAD 状态机（同步，运行在音频回调线程）────────────────────

    def _process_frame_sync(self, pcm_bytes: bytes) -> None:
        """
        在音频回调线程中同步运行 VAD 状态机。

        仅在语音开始/结束事件时通过 run_coroutine_threadsafe 通知 asyncio，
        而非每帧都投递协程，从而消除事件循环积压和 input overflow。
        """
        if self._is_muted or self._vad is None:
            return

        # webrtcvad 要求帧长精确（FRAME_SIZE * 2 bytes）
        if len(pcm_bytes) != FRAME_SIZE * 2:
            return

        try:
            is_speech = self._vad.is_speech(pcm_bytes, SAMPLE_RATE)
        except Exception:
            return

        if is_speech:
            if not self._is_speaking:
                self._is_speaking = True
                self._speech_start_at = time.monotonic()
                self._speech_buffer.clear()
                self._silent_frames = 0
                self._voiced_frames = 0
                if self._on_speech_start and self._loop:
                    asyncio.run_coroutine_threadsafe(
                        self._on_speech_start(), self._loop
                    )
                logger.debug("[Microphone] → speech_start")

            self._speech_buffer.append(pcm_bytes)
            self._silent_frames = 0
            self._voiced_frames += 1
            if len(self._speech_buffer) >= MAX_SPEECH_FRAMES:
                # 避免环境噪声把 speaking 状态拖成超长片段（几十秒）。
                logger.debug(f"[Microphone] 片段达到上限 {MAX_SPEECH_MS}ms，强制切段")
                self._flush_speech_sync()

        else:
            if self._is_speaking:
                self._silent_frames += 1
                self._speech_buffer.append(pcm_bytes)  # 保留尾部静音，避免截断

                if self._silent_frames >= SILENCE_FRAMES:
                    self._flush_speech_sync()

    def _flush_speech_sync(self) -> None:
        """打包语音块，通过 run_coroutine_threadsafe 提交 on_speech_end 回调，然后重置状态。"""
        t0 = time.perf_counter()
        self._is_speaking = False

        # 混响保护：语音在 unmute 后 POST_UNMUTE_GRACE_MS 内开始的片段，视为 TTS 扬声器混响丢弃
        if self._unmute_at > 0 and POST_UNMUTE_GRACE_MS > 0:
            since_unmute_ms = (self._speech_start_at - self._unmute_at) * 1000
            if 0 <= since_unmute_ms < POST_UNMUTE_GRACE_MS:
                logger.warning(
                    "[Microphone] 丢弃 unmute 后混响片段（语音起始距 unmute=%.0fms < %dms）",
                    since_unmute_ms, POST_UNMUTE_GRACE_MS,
                )
                self._speech_buffer.clear()
                self._silent_frames = 0
                self._voiced_frames = 0
                return

        if len(self._speech_buffer) < MIN_SPEECH_FRAMES:
            logger.debug("[Microphone] 语音过短，丢弃（%d 帧 < %d 帧）", len(self._speech_buffer), MIN_SPEECH_FRAMES)
            self._speech_buffer.clear()
            self._silent_frames = 0
            self._voiced_frames = 0
            return

        raw_pcm = b"".join(self._speech_buffer)
        duration_ms = len(self._speech_buffer) * FRAME_DURATION_MS

        # voiced_ratio 分母只算语音帧（排除尾部静音帧），避免短句被误判丢弃
        speech_frames_count = len(self._speech_buffer) - self._silent_frames
        speech_frames_count = max(speech_frames_count, 1)
        speech_only_pcm = b"".join(self._speech_buffer[:speech_frames_count])
        voiced_ratio = self._voiced_frames / max(speech_frames_count, 1)
        rms_dbfs = _compute_rms_dbfs(raw_pcm)

        if voiced_ratio < MIN_VOICED_RATIO:
            logger.warning(
                "[Microphone] 丢弃低语音占比片段（voiced_ratio=%.2f < %.2f，时长=%dms，rms=%.1fdBFS）",
                voiced_ratio, MIN_VOICED_RATIO, duration_ms, rms_dbfs,
            )
            self._speech_buffer.clear()
            self._silent_frames = 0
            self._voiced_frames = 0
            return

        if rms_dbfs < MIN_CLIP_RMS_DBFS:
            logger.warning(
                "[Microphone] 丢弃低能量片段（rms_dbfs=%.1f < %.1f，时长=%dms，voiced_ratio=%.2f）",
                rms_dbfs, MIN_CLIP_RMS_DBFS, duration_ms, voiced_ratio,
            )
            self._speech_buffer.clear()
            self._silent_frames = 0
            self._voiced_frames = 0
            return

        if POST_VAD_ENABLED and self._post_vad is not None:
            strict_voiced_ratio = _compute_vad_voiced_ratio(speech_only_pcm, self._post_vad)
            if strict_voiced_ratio < POST_MIN_VOICED_RATIO:
                logger.warning(
                    "[Microphone] 丢弃二次VAD低占比片段（strict_voiced_ratio=%.2f < %.2f，时长=%dms，voiced_ratio=%.2f，rms=%.1fdBFS）",
                    strict_voiced_ratio, POST_MIN_VOICED_RATIO, duration_ms, voiced_ratio, rms_dbfs,
                )
                self._speech_buffer.clear()
                self._silent_frames = 0
                self._voiced_frames = 0
                return

        vad_flush_ms = int((time.perf_counter() - t0) * 1000)
        if self._on_speech_end and self._loop:
            asyncio.run_coroutine_threadsafe(
                self._on_speech_end(raw_pcm, SAMPLE_RATE, duration_ms, vad_flush_ms=vad_flush_ms),
                self._loop,
            )

        logger.info(
            "[Microphone] → speech_end（时长=%dms，voiced_ratio=%.2f，rms=%.1fdBFS）",
            duration_ms, voiced_ratio, rms_dbfs,
        )

        self._speech_buffer.clear()
        self._silent_frames = 0
        self._voiced_frames = 0


def _compute_rms_dbfs(raw_pcm: bytes) -> float:
    """
    计算 PCM 片段 RMS 能量（dBFS）。

    dBFS 范围通常在 [-90, 0]，数值越大（越接近 0）表示声音越强。
    """
    if not raw_pcm:
        return -90.0
    samples = np.frombuffer(raw_pcm, dtype=np.int16).astype(np.float32) / 32768.0
    rms = float(np.sqrt(np.mean(samples * samples))) if samples.size else 0.0
    rms = max(rms, 1e-6)
    return float(20 * np.log10(rms))


def _compute_vad_voiced_ratio(raw_pcm: bytes, vad) -> float:
    """
    使用指定 VAD 复检片段语音占比（仅统计完整 30ms 帧）。
    """
    frame_bytes = FRAME_SIZE * 2
    total_frames = len(raw_pcm) // frame_bytes
    if total_frames <= 0:
        return 0.0

    voiced_frames = 0
    for i in range(total_frames):
        frame = raw_pcm[i * frame_bytes : (i + 1) * frame_bytes]
        try:
            if vad.is_speech(frame, SAMPLE_RATE):
                voiced_frames += 1
        except Exception:
            continue

    return voiced_frames / total_frames


