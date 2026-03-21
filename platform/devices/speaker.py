"""
Speaker — 扬声器硬件抽象层
============================
职责：
  1. TTS 文字转音频（edge-tts，支持中文 Neural 声音）
  2. 通过扬声器播放（sounddevice + soundfile 解码 MP3）
  3. 双队列流水线（文本队列 → 已合成音频队列），实现“边播边合成”
  4. interrupt 中断清空（取消当前播放 + 取消当前合成 + 丢弃旧队列）
  5. 通过回调通知上层播放开始/结束（用于防回声 mute 联动）

不含任何 WebSocket / Spine 逻辑，纯硬件操作。

播放方案说明：
  原方案用 sounddevice（PortAudio）的 OutputStream 进行播放。当麦克风的
  InputStream 与 Speaker 的 OutputStream 同时操作同一 USB ALSA hw:0,0 设备时，
  PortAudio 在流的 open/close 生命周期中会对共享 ALSA card 调用
  alsa_snd_pcm_drop，误伤 capture stream，导致麦克风回调静默死亡。

  现改为：soundfile 在内存中解码 MP3 → asyncio 子进程运行 aplay 播放原始 PCM。
  aplay 是独立 ALSA 客户端进程，与 Python 进程的 PortAudio 上下文完全隔离；
  USB audio class 设备的 ALSA 驱动天然支持全双工，input/output 在 ALSA 层互不干扰。
  中断时通过 proc.kill() 立即停止 aplay，无须等待音频播完。

依赖：edge-tts, soundfile, numpy, aplay（alsa-utils，树莓派默认已安装）
"""

import asyncio
import io
import logging
import os
from collections.abc import Callable

logger = logging.getLogger(__name__)

DEFAULT_VOICE = "zh-CN-XiaoxiaoNeural"  # 微软 Azure 晓晓，自然中文女声
DEFAULT_RATE = "+0%"
DEFAULT_VOLUME = "+100%"   # edge-tts 最大输出音量
SOFTWARE_GAIN = float(os.environ.get("SPEAKER_SOFTWARE_GAIN", "2"))  # 软件增益倍数，USB 声卡输出偏小时补偿；超过 1.0 会 clip 削波
# aplay 使用的 ALSA 输出设备；默认 "default" 走系统默认设备（通常含 plug/dmix 插件，支持自动重采样）
# 若默认设备不是 USB 声卡可改为 "plughw:0,0" 等；可通过 `aplay -L` 查看可用设备列表
ALSA_DEVICE = os.environ.get("SPEAKER_ALSA_DEVICE", "default")
# 单句播放超时（秒）：aplay 进程若超时将被 kill()，比 AudioEffector fallback 短 3s 以便日志定位
PLAY_TIMEOUT_S = float(os.environ.get("SPEAKER_PLAY_TIMEOUT_S", "27"))
# TTS 合成网络请求超时（秒）：edge-tts 依赖 Microsoft 服务器，网络抖动时需兜底
TTS_TIMEOUT_S = float(os.environ.get("SPEAKER_TTS_TIMEOUT_S", "15"))


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
        self._ready_queue: asyncio.Queue[dict] = asyncio.Queue()
        self._current_task: asyncio.Task | None = None
        self._current_tts_task: asyncio.Task | None = None
        self._generation = 0
        self._enqueue_lock = asyncio.Lock()

    # ─── 公共接口 ─────────────────────────────────────────────────────

    async def start(self) -> None:
        """启动双阶段流水线（阻塞，应在独立 task 中运行）。"""
        logger.info(f"[Speaker] 播放队列已启动（声音：{self._voice}）")
        tts_worker = asyncio.create_task(self._synthesis_loop(), name="speaker_tts_worker")
        play_worker = asyncio.create_task(self._playback_loop(), name="speaker_play_worker")
        await asyncio.gather(tts_worker, play_worker)

    async def enqueue(self, text: str, interrupt: bool = False) -> None:
        """
        将文字加入播放队列。

        Args:
            text:      要播放的文字
            interrupt: True 时先清空队列并取消当前播放，再插入新内容
        """
        normalized = text.strip()
        if not normalized:
            return

        async with self._enqueue_lock:
            if interrupt:
                self._interrupt_pipeline_locked()

            await self._queue.put({
                "text": normalized,
                "generation": self._generation,
            })

    def is_busy(self) -> bool:
        """是否仍有播放或待处理任务（含合成中/待播/播放中）。"""
        is_playing = bool(self._current_task and not self._current_task.done())
        is_synthesizing = bool(self._current_tts_task and not self._current_tts_task.done())
        return (
            is_playing
            or is_synthesizing
            or not self._queue.empty()
            or not self._ready_queue.empty()
        )

    def is_idle(self) -> bool:
        return not self.is_busy()

    # ─── 内部播放流程 ──────────────────────────────────────────────────

    def _interrupt_pipeline_locked(self) -> None:
        """切换代际并清空旧任务：用于 interrupt_current=true。"""
        self._generation += 1
        self._drain_queue(self._queue)
        self._drain_queue(self._ready_queue)

        if self._current_task and not self._current_task.done():
            self._current_task.cancel()
        if self._current_tts_task and not self._current_tts_task.done():
            self._current_tts_task.cancel()

    def _drain_queue(self, queue: asyncio.Queue[dict]) -> None:
        while not queue.empty():
            try:
                queue.get_nowait()
                queue.task_done()
            except asyncio.QueueEmpty:
                break

    async def _synthesis_loop(self) -> None:
        """消费文本队列并合成为音频，支持与播放并行。"""
        while True:
            item = await self._queue.get()
            try:
                generation = int(item.get("generation", -1))
                text = str(item.get("text", "")).strip()
                if not text:
                    continue
                if generation != self._generation:
                    continue

                self._current_tts_task = asyncio.create_task(self._tts(text))
                try:
                    audio_data = await self._current_tts_task
                except asyncio.CancelledError:
                    # interrupt 会主动取消当前合成；这是预期路径，不应让 worker 退出。
                    if generation != self._generation:
                        continue
                    raise
                finally:
                    self._current_tts_task = None

                if generation != self._generation:
                    continue
                if audio_data:
                    await self._ready_queue.put({
                        "audio_data": audio_data,
                        "generation": generation,
                    })
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"[Speaker] 合成流程异常：{e}")
            finally:
                self._queue.task_done()

    async def _playback_loop(self) -> None:
        """消费已合成音频队列并播放。"""
        while True:
            item = await self._ready_queue.get()
            started = False
            try:
                generation = int(item.get("generation", -1))
                audio_data = item.get("audio_data")
                if generation != self._generation:
                    continue
                if not isinstance(audio_data, (bytes, bytearray)) or len(audio_data) == 0:
                    continue

                if self._on_play_start:
                    self._on_play_start()
                started = True

                self._current_task = asyncio.create_task(self._play(bytes(audio_data)))
                await self._current_task
            except asyncio.CancelledError:
                logger.debug("[Speaker] 播放被中断")
            except Exception as e:
                logger.error(f"[Speaker] 播放失败：{e}")
            finally:
                self._current_task = None
                if started and self._on_play_end:
                    self._on_play_end()
                self._ready_queue.task_done()

    async def _tts(self, text: str) -> bytes | None:
        """调用 edge-tts 生成音频，返回 MP3 字节。

        edge-tts 依赖 Microsoft Azure 语音服务，网络异常时 stream() 可能长时间阻塞；
        用 asyncio.wait_for 兜底，超时后让上层合成循环继续处理下一句，而非永久卡住。
        """
        try:
            import edge_tts
        except ImportError:
            logger.error("[Speaker] 缺少依赖：edge-tts，请运行 pip install edge-tts")
            return None

        async def _do_tts() -> bytes:
            communicate = edge_tts.Communicate(
                text, voice=self._voice, rate=self._rate, volume=self._volume
            )
            chunks: list[bytes] = []
            async for chunk in communicate.stream():
                if chunk["type"] == "audio":
                    chunks.append(chunk["data"])
            return b"".join(chunks)

        try:
            return await asyncio.wait_for(_do_tts(), timeout=TTS_TIMEOUT_S)
        except asyncio.TimeoutError:
            logger.error("[Speaker] TTS 合成超时（%.0fs），跳过本句：%s", TTS_TIMEOUT_S, text[:30])
            return None
        except Exception as e:
            logger.error(f"[Speaker] TTS 失败：{e}")
            return None

    async def _play(self, mp3_data: bytes) -> None:
        """解码 MP3 并通过 aplay 子进程播放。

        将 MP3 在内存中解码为原始 S16_LE PCM（soundfile），再通过 aplay 子进程
        输出到 ALSA 设备。aplay 是独立的 ALSA 客户端进程，与 Python 进程的
        PortAudio（麦克风 InputStream）完全隔离，从根本上消除 PortAudio 内部
        alsa_snd_pcm_drop 误伤 capture stream 的问题。

        中断（CancelledError）时直接 proc.kill()，aplay 立即停止，音频马上静音。
        ALSA 的 plug 插件层负责自动重采样（24000Hz → 48000Hz），无需 Python 手动处理。
        """
        try:
            import soundfile as sf
            import numpy as np
        except ImportError:
            logger.error("[Speaker] 缺少依赖：soundfile / numpy")
            return

        loop = asyncio.get_event_loop()

        def _decode_to_pcm() -> tuple[bytes, int, int]:
            """在 executor 线程中解码 MP3 → S16_LE PCM（CPU 密集，不阻塞事件循环）。"""
            with io.BytesIO(mp3_data) as buf:
                data, sample_rate = sf.read(buf, dtype="float32")
            if SOFTWARE_GAIN != 1.0:
                data = np.clip(data * SOFTWARE_GAIN, -1.0, 1.0)
            channels = 1 if data.ndim == 1 else data.shape[1]
            pcm = (np.clip(data, -1.0, 1.0) * 32767).astype(np.int16)
            return pcm.tobytes(), int(sample_rate), channels

        try:
            pcm_bytes, sample_rate, channels = await loop.run_in_executor(None, _decode_to_pcm)
        except Exception as e:
            logger.error("[Speaker] MP3 解码失败：%s", e)
            return

        # aplay 参数：
        #   -q         静默模式（不打印进度）
        #   -D <dev>   ALSA 设备（默认 "default"，含 plug 层，支持自动重采样）
        #   -f S16_LE  有符号 16-bit 小端 PCM
        #   -r <rate>  采样率（edge-tts 输出 24000Hz，plug 层自动升采样至设备原生率）
        #   -c <ch>    声道数
        #   -          从 stdin 读取 PCM 数据
        cmd = [
            "aplay", "-q",
            "-D", ALSA_DEVICE,
            "-f", "S16_LE",
            "-r", str(sample_rate),
            "-c", str(channels),
            "-",
        ]

        proc: asyncio.subprocess.Process | None = None
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr_bytes = await asyncio.wait_for(
                proc.communicate(input=pcm_bytes),
                timeout=PLAY_TIMEOUT_S,
            )
            if proc.returncode != 0:
                logger.warning(
                    "[Speaker] aplay 退出码 %d: %s",
                    proc.returncode,
                    stderr_bytes.decode(errors="replace")[:200],
                )
            else:
                logger.debug("[Speaker] 播放完成：%d bytes，%.1fs", len(mp3_data), len(pcm_bytes) / (sample_rate * channels * 2))
        except asyncio.TimeoutError:
            if proc:
                proc.kill()
            logger.error("[Speaker] aplay 播放超时（%.0fs），已强制终止", PLAY_TIMEOUT_S)
        except asyncio.CancelledError:
            if proc:
                proc.kill()
            raise  # 必须重新抛出，让 _playback_loop 感知并触发 on_play_end
        except FileNotFoundError:
            logger.error("[Speaker] aplay 命令不存在，请安装：sudo apt install alsa-utils")
        except Exception as e:
            logger.error("[Speaker] 播放异常：%s", e)
