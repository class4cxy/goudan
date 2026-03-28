"""
AudioSensor — 应用层（音频输入桥接）
=====================================
职责：
  - 实例化 Microphone 硬件层，注入 WebSocket 广播回调
  - 将 Microphone 的语音事件（raw PCM）转换为 Spine 协议格式发布到 WebSocket
  - 可选：接入 openWakeWord 本地唤醒词检测，命中后广播 sense.audio.keyword

不包含任何硬件逻辑，仅做 Microphone → WebSocket 的数据路由。
硬件层详见 devices/microphone.py。

事件输出（发往 WebSocket → Spine）：
  sense.audio.speech_start  — 检测到有人开始说话（LOW 优先级）
  sense.audio.speech_end    — 说话结束，携带 base64 PCM 音频块（MEDIUM 优先级）
  sense.audio.keyword       — OWW 命中唤醒词（仅当 WAKE_WORD_MODEL 配置时发出）

OWW 配置（环境变量）：
  WAKE_WORD_MODEL     模型名或 .onnx/.tflite 路径；空字符串 = 禁用（默认）
  WAKE_WORD_THRESHOLD 检测阈值，0~1（默认 0.5）
  WAKE_WORD_COOLDOWN_S 命中冷却秒数（默认 1.5）
"""

import base64
import logging
import os
from uuid import uuid4

from devices import Microphone

logger = logging.getLogger(__name__)

_WAKE_WORD_MODEL = os.environ.get("WAKE_WORD_MODEL", "").strip()


class AudioSensor:
    """Microphone 到 WebSocket 的桥接层（应用层）。"""

    def __init__(self, ws_manager: "ConnectionManager"):
        self._ws = ws_manager

        oww_enabled = bool(_WAKE_WORD_MODEL)
        if oww_enabled:
            logger.info("[AudioSensor] openWakeWord 已启用，模型=%r", _WAKE_WORD_MODEL)
        else:
            logger.info("[AudioSensor] openWakeWord 未配置（WAKE_WORD_MODEL 为空），使用 Whisper 文本匹配唤醒")

        self._mic = Microphone(
            on_speech_start=self._on_speech_start,
            on_speech_end=self._on_speech_end,
            on_wake_word=self._on_wake_word if oww_enabled else None,
            wake_word_model=_WAKE_WORD_MODEL if oww_enabled else None,
        )

    def mute(self) -> None:
        """外放时静音，防止回声触发 VAD（委托给 Microphone）。"""
        self._mic.mute()

    def unmute(self) -> None:
        """外放结束后恢复监听（委托给 Microphone）。"""
        self._mic.unmute()

    async def start(self) -> None:
        """启动麦克风采集（委托给 Microphone，阻塞）。"""
        await self._mic.start()

    # ─── 内部回调：Microphone 事件 → WebSocket 广播 ──────────────────

    async def _on_speech_start(self) -> None:
        await self._ws.broadcast({
            "type": "sense.audio.speech_start",
            "payload": {},
        })

    async def _on_wake_word(self, word: str, score: float) -> None:
        """OWW 命中唤醒词 → 广播 sense.audio.keyword（与 Whisper 文本匹配结果结构相同）。"""
        logger.info("[AudioSensor] → keyword  word=%r  score=%.3f", word, score)
        await self._ws.broadcast({
            "type": "sense.audio.keyword",
            "payload": {"keyword": word, "score": score, "source": "oww"},
        })

    async def _on_speech_end(
        self, raw_pcm: bytes, sample_rate: int, duration_ms: int, **kwargs
    ) -> None:
        trace_id = uuid4().hex[:8]
        payload: dict = {
            "trace_id": trace_id,
            "audio_b64": base64.b64encode(raw_pcm).decode(),
            "sample_rate": sample_rate,
            "duration_ms": duration_ms,
        }
        if "vad_flush_ms" in kwargs:
            payload["platform_vad_flush_ms"] = kwargs["vad_flush_ms"]
        logger.info("[AudioSensor] → speech_end  trace=%s  时长=%dms  大小=%dKB",
                    trace_id, duration_ms, len(raw_pcm) // 1024)
        await self._ws.broadcast({"type": "sense.audio.speech_end", "payload": payload})
