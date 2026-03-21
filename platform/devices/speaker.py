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

播放后端（SPEAKER_BACKEND 环境变量控制）：
  alsa        — aplay（默认，适合 USB 声卡直连，纯 ALSA 无需 PulseAudio）
  pulseaudio  — pacat（适合蓝牙音箱，通过 PulseAudio/PipeWire A2DP 路由）

  RPi 5 蓝牙外放时选 pulseaudio：先用 BluetoothManager.connect() 连接蓝牙音箱
  并设为默认 sink，之后 pacat 写入 default sink 时自动路由到蓝牙扬声器。

依赖：edge-tts, soundfile, numpy, aplay（alsa-utils）或 pacat（pulseaudio-utils）
      树莓派 OS Bookworm 默认均已安装
"""

import asyncio
import io
import logging
import os
from collections.abc import Callable
from datetime import datetime, timezone

logger = logging.getLogger(__name__)


def _utc_iso_ms() -> str:
    """UTC 时间戳（毫秒），与 Node `new Date().toISOString()` 对齐便于对日志。"""
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")

DEFAULT_VOICE = "zh-CN-XiaoxiaoNeural"  # 微软 Azure 晓晓，自然中文女声
DEFAULT_RATE = "+0%"
DEFAULT_VOLUME = "+100%"   # edge-tts 最大输出音量
SOFTWARE_GAIN = float(os.environ.get("SPEAKER_SOFTWARE_GAIN", "2"))  # 软件增益倍数，USB 声卡输出偏小时补偿；超过 1.0 会 clip 削波

# 播放后端：alsa（aplay，USB声卡默认）| pulseaudio（pacat，蓝牙/PipeWire）
SPEAKER_BACKEND = os.environ.get("SPEAKER_BACKEND", "pulseaudio").lower()

# ALSA 后端：aplay 输出设备（SPEAKER_BACKEND=alsa 时生效）
ALSA_DEVICE = os.environ.get("SPEAKER_ALSA_DEVICE", "default")

# 单句播放超时（秒）：包裹 pacat/aplay 的 proc.communicate（写 stdin 并等进程退出）。
# 若蓝牙/PipeWire 阻塞、子进程僵死，超过此时长则 kill。应大于「本句 PCM 实际播放时长」；
# 长段落可调大 SPEAKER_PLAY_TIMEOUT_S。与 AudioEffector 的 speak_end 兜底（AUDIO_SPEAK_END_FALLBACK_MS）相互独立。
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

                logger.info(
                    "[Speaker] tts_start ts=%s chars=%d preview=%s",
                    _utc_iso_ms(),
                    len(text),
                    (text[:36] + "…") if len(text) > 36 else text,
                )
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
                    logger.info(
                        "[Speaker] tts_ready ts=%s mp3_bytes=%d",
                        _utc_iso_ms(),
                        len(audio_data),
                    )
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

                logger.info(
                    "[Speaker] play_dequeue ts=%s mp3_bytes=%d",
                    _utc_iso_ms(),
                    len(audio_data),
                )

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
        """解码 MP3 并通过子进程播放。

        根据 SPEAKER_BACKEND 环境变量选择播放后端：
          - alsa（默认）：aplay，纯 ALSA 路径，适合 USB 声卡直连
          - pulseaudio：pacat，通过 PulseAudio/PipeWire 路由，适合蓝牙 A2DP

        两种后端均先将 MP3 在内存中解码为 S16_LE PCM（soundfile），
        再通过对应命令的 stdin 管道送入播放器，中断时直接 proc.kill()。
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

        est_play_s = len(pcm_bytes) / float(sample_rate * max(channels, 1) * 2)

        if SPEAKER_BACKEND == "pulseaudio":
            # pacat —— PulseAudio/PipeWire 兼容层
            # 写入 default sink；蓝牙连接后 BluetoothManager 已将其设为 BT sink
            #   --format=s16le  有符号 16-bit 小端 PCM
            #   --rate          采样率
            #   --channels      声道数
            #   --latency-msec  缓冲延迟（降低以减少播放延迟，过低会有噼啪声）
            cmd = [
                "pacat", "--playback",
                "--format=s16le",
                f"--rate={sample_rate}",
                f"--channels={channels}",
                "--latency-msec=100",
            ]
            backend_label = "pacat"
        else:
            # aplay —— 纯 ALSA 路径（USB 声卡默认）
            # plug 层自动重采样（24000Hz → 设备原生率），无需 Python 手动处理
            cmd = [
                "aplay", "-q",
                "-D", ALSA_DEVICE,
                "-f", "S16_LE",
                "-r", str(sample_rate),
                "-c", str(channels),
                "-",
            ]
            backend_label = "aplay"

        logger.info(
            "[Speaker] play_subproc ts=%s backend=%s est_audio_s=%.2f timeout_cap_s=%.0f",
            _utc_iso_ms(),
            backend_label,
            est_play_s,
            PLAY_TIMEOUT_S,
        )

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
                    "[Speaker] %s 退出码 %d: %s",
                    backend_label,
                    proc.returncode,
                    stderr_bytes.decode(errors="replace")[:200],
                )
            else:
                logger.debug(
                    "[Speaker] 播放完成（%s）：%d bytes，%.1fs",
                    backend_label,
                    len(mp3_data),
                    len(pcm_bytes) / (sample_rate * channels * 2),
                )
        except asyncio.TimeoutError:
            if proc:
                proc.kill()
            logger.error(
                "[Speaker] %s 播放超时（cap=%.0fs，本句 PCM 估算 %.2fs），已强制终止",
                backend_label,
                PLAY_TIMEOUT_S,
                est_play_s,
            )
        except asyncio.CancelledError:
            if proc:
                proc.kill()
            raise  # 必须重新抛出，让 _playback_loop 感知并触发 on_play_end
        except FileNotFoundError:
            if SPEAKER_BACKEND == "pulseaudio":
                logger.error("[Speaker] pacat 命令不存在，请安装：sudo apt install pulseaudio-utils")
            else:
                logger.error("[Speaker] aplay 命令不存在，请安装：sudo apt install alsa-utils")
        except Exception as e:
            logger.error("[Speaker] 播放异常（%s）：%s", backend_label, e)
