"""
Speaker — 扬声器硬件抽象层
============================
职责：
  1. TTS 文字转音频（edge-tts，支持中文 Neural 声音）
  2. 通过扬声器播放（sounddevice + soundfile 解码 MP3）
  3. 播放队列管理（顺序排队 / interrupt 中断清空）
  4. 通过回调通知上层播放开始/结束（用于防回声 mute 联动）

不含任何 WebSocket / Spine 逻辑，纯硬件操作。

依赖：edge-tts, sounddevice, soundfile
"""

import asyncio
import io
import logging
from collections.abc import Callable

logger = logging.getLogger(__name__)

DEFAULT_VOICE = "zh-CN-XiaoxiaoNeural"  # 微软 Azure 晓晓，自然中文女声
DEFAULT_RATE = "+0%"
DEFAULT_VOLUME = "+100%"   # edge-tts 最大输出音量
SOFTWARE_GAIN = 2.0        # 软件增益倍数，USB 声卡输出偏小时补偿；超过 1.0 会 clip 削波


class Speaker:
    """
    TTS + 扬声器播放器（纯硬件层）。

    通过回调通知上层播放开始/结束，不依赖 WebSocket / AudioSensor。

    Args:
        voice:          edge-tts 声音名，默认晓晓（zh-CN-XiaoxiaoNeural）
        rate:           语速，如 "+10%" / "-20%"，默认 "+0%"
        volume:         音量，如 "+10%" / "-20%"，默认 "+0%"
        on_play_start:  开始播放时调用（同步），用于通知麦克风 mute
        on_play_end:    播放结束时调用（同步），用于通知麦克风 unmute
    """

    def __init__(
        self,
        voice: str = DEFAULT_VOICE,
        rate: str = DEFAULT_RATE,
        volume: str = DEFAULT_VOLUME,
        on_play_start: Callable[[], None] | None = None,
        on_play_end: Callable[[], None] | None = None,
    ):
        self._voice = voice
        self._rate = rate
        self._volume = volume
        self._on_play_start = on_play_start
        self._on_play_end = on_play_end

        self._queue: asyncio.Queue[dict] = asyncio.Queue()
        self._current_task: asyncio.Task | None = None

    # ─── 公共接口 ─────────────────────────────────────────────────────

    async def start(self) -> None:
        """消费播放队列（阻塞，应在独立 task 中运行）。"""
        logger.info(f"[Speaker] 播放队列已启动（声音：{self._voice}）")
        while True:
            command = await self._queue.get()
            await self._do_speak(command["text"])
            self._queue.task_done()

    async def enqueue(self, text: str, interrupt: bool = False) -> None:
        """
        将文字加入播放队列。

        Args:
            text:      要播放的文字
            interrupt: True 时先清空队列并取消当前播放，再插入新内容
        """
        if interrupt:
            while not self._queue.empty():
                try:
                    self._queue.get_nowait()
                    self._queue.task_done()
                except asyncio.QueueEmpty:
                    break
            if self._current_task and not self._current_task.done():
                self._current_task.cancel()

        await self._queue.put({"text": text})

    # ─── 内部播放流程 ──────────────────────────────────────────────────

    async def _do_speak(self, text: str) -> None:
        if not text.strip():
            return

        if self._on_play_start:
            self._on_play_start()

        try:
            audio_data = await self._tts(text)
            if audio_data:
                self._current_task = asyncio.create_task(self._play(audio_data))
                await self._current_task
        except asyncio.CancelledError:
            logger.debug("[Speaker] 播放被中断")
        except Exception as e:
            logger.error(f"[Speaker] 播放失败：{e}")
        finally:
            if self._on_play_end:
                self._on_play_end()

    async def _tts(self, text: str) -> bytes | None:
        """调用 edge-tts 生成音频，返回 MP3 字节。"""
        try:
            import edge_tts
        except ImportError:
            logger.error("[Speaker] 缺少依赖：edge-tts，请运行 pip install edge-tts")
            return None

        try:
            communicate = edge_tts.Communicate(
                text, voice=self._voice, rate=self._rate, volume=self._volume
            )
            chunks: list[bytes] = []
            async for chunk in communicate.stream():
                if chunk["type"] == "audio":
                    chunks.append(chunk["data"])
            return b"".join(chunks)
        except Exception as e:
            logger.error(f"[Speaker] TTS 失败：{e}")
            return None

    async def _play(self, mp3_data: bytes) -> None:
        """解码 MP3 并通过 sounddevice 播放。"""
        try:
            import sounddevice as sd
            import soundfile as sf
            import numpy as np
        except ImportError:
            logger.error("[Speaker] 缺少依赖：sounddevice / soundfile / numpy")
            return

        loop = asyncio.get_event_loop()

        def _blocking_play():
            with io.BytesIO(mp3_data) as buf:
                data, sample_rate = sf.read(buf, dtype="float32")

            # 获取输出设备原生采样率，edge-tts 输出 24000Hz 而 USB 声卡通常只支持 44100/48000Hz
            device_info = sd.query_devices(kind="output")
            native_rate = int(device_info["default_samplerate"])

            if sample_rate != native_rate:
                n_orig = len(data)
                n_target = int(n_orig * native_rate / sample_rate)
                x_orig = np.arange(n_orig)
                x_target = np.linspace(0, n_orig - 1, n_target)

                if data.ndim == 1:
                    data = np.interp(x_target, x_orig, data).astype(np.float32)
                else:
                    data = np.column_stack([
                        np.interp(x_target, x_orig, data[:, i]).astype(np.float32)
                        for i in range(data.shape[1])
                    ])

                logger.debug(f"[Speaker] 重采样：{sample_rate}Hz → {native_rate}Hz")
                sample_rate = native_rate

            if SOFTWARE_GAIN != 1.0:
                data = np.clip(data * SOFTWARE_GAIN, -1.0, 1.0)

            sd.play(data, samplerate=sample_rate)
            sd.wait()

        await loop.run_in_executor(None, _blocking_play)
        logger.debug(f"[Speaker] 播放完成：{len(mp3_data)} bytes")
