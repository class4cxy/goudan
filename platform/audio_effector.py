"""
AudioEffector — 应用层（音频输出桥接）
=======================================
职责：
  - 实例化 Speaker 硬件层（aplay/pacat 后端，由 SPEAKER_BACKEND 环境变量决定）
  - 接收来自 WebSocket 的 action.speak 指令，转发给 Speaker 播放队列
  - 所有 TTS 句子播完后，向 Node.js 广播 sense.audio.speak_end 事件

Chat 模式（蓝牙外放）：
  - 无麦克风接入，移除了 mute/unmute 回声保护逻辑
  - Speaker 输出通过 pacat 路由到蓝牙 A2DP sink（SPEAKER_BACKEND=pulseaudio）

不包含任何 TTS / 播放逻辑，仅做 WebSocket 指令 → Speaker 的路由。
硬件层详见 devices/speaker.py。

speak_end：`AUDIO_SPEAK_END_FALLBACK_MS` 默认 45s；每句播完若 Speaker 仍 busy 会重置兜底计时，避免多句总时长超过旧 15s 窗口时误发 forced speak_end。
"""

import asyncio
import logging
import os
from datetime import datetime, timezone

from devices import Speaker

logger = logging.getLogger(__name__)


def _utc_iso_ms() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class AudioEffector:
    """WebSocket 指令到 Speaker 的桥接层（应用层）。"""

    def __init__(self, ws_manager=None):
        self._ws_manager = ws_manager
        self._loop: asyncio.AbstractEventLoop | None = None
        # 仅在「长时间无播放进度」时强制 speak_end；中间句播完会重置计时（见 _on_play_end）
        self._fallback_ms = int(os.environ.get("AUDIO_SPEAK_END_FALLBACK_MS", "45000"))
        self._idle_confirm_ms = int(os.environ.get("AUDIO_SPEAK_END_IDLE_CONFIRM_MS", "250"))
        self._speak_seq = 0
        self._last_emitted_seq = 0
        self._fallback_task: asyncio.Task | None = None
        self._idle_confirm_task: asyncio.Task | None = None
        self._speaker = Speaker(
            on_play_end=self._on_play_end,
        )

    def _on_play_end(self) -> None:
        """每句 TTS 播完后调用：仅在全部句子播完（is_idle）时才安排 speak_end 广播。

        句间有短暂空窗（LLM 下一句尚未入队），通过二次确认避免误判。
        若仍有后续句在合成/排队/播放，向后推迟强制 speak_end 的兜底计时，避免「多句快速入队但总播放很长」时误触发。
        """
        if not self._speaker.is_idle():
            if self._loop:
                self._schedule_fallback(self._speak_seq)
            return

        # 为避免句间短暂空窗（LLM 下一句尚未入队）导致误判，增加短延迟二次确认。
        if self._ws_manager and self._loop:
            seq = self._speak_seq
            if self._idle_confirm_task and not self._idle_confirm_task.done():
                self._idle_confirm_task.cancel()
            self._idle_confirm_task = self._loop.create_task(self._confirm_and_emit_speak_end(seq))

    async def start(self) -> None:
        """启动播放队列（委托给 Speaker，阻塞）。"""
        self._loop = asyncio.get_running_loop()
        await self._speaker.start()

    async def enqueue(self, text: str, interrupt: bool = False) -> None:
        """将播放指令加入队列（委托给 Speaker）。"""
        await self._speaker.enqueue(text, interrupt)
        self._speak_seq += 1
        t = text.strip()
        logger.info(
            "[AudioEffector] speak_enqueued ts=%s seq=%d chars=%d interrupt=%s preview=%s",
            _utc_iso_ms(),
            self._speak_seq,
            len(t),
            interrupt,
            (t[:36] + "…") if len(t) > 36 else t,
        )
        if self._idle_confirm_task and not self._idle_confirm_task.done():
            self._idle_confirm_task.cancel()
        self._schedule_fallback(self._speak_seq)

    async def _emit_speak_end(self, forced: bool, reason: str) -> None:
        """广播 speak_end；forced=True 表示通过兜底机制触发。"""
        if not self._ws_manager:
            return
        await self._ws_manager.broadcast({
            "type": "sense.audio.speak_end",
            "payload": {"forced": forced, "reason": reason},
        })
        logger.info("[AudioEffector] → sense.audio.speak_end 已广播（forced=%s, reason=%s）", forced, reason)

    async def _confirm_and_emit_speak_end(self, seq: int) -> None:
        """短延迟确认空闲，避免句间瞬时空队列误触发 speak_end。"""
        try:
            await asyncio.sleep(self._idle_confirm_ms / 1000)
            if seq != self._speak_seq:
                return
            if seq <= self._last_emitted_seq:
                return
            if not self._speaker.is_idle():
                return
            await self._emit_speak_end(forced=False, reason="queue_empty_confirmed")
            self._last_emitted_seq = seq
            if self._fallback_task and not self._fallback_task.done():
                self._fallback_task.cancel()
        except asyncio.CancelledError:
            return

    def _schedule_fallback(self, seq: int) -> None:
        """为每轮 speak 安排兜底回执，避免播放链路异常导致 Node 长时间卡在 SPEAKING。"""
        if not self._loop:
            return
        if self._fallback_task and not self._fallback_task.done():
            self._fallback_task.cancel()
        self._fallback_task = self._loop.create_task(self._fallback_emit(seq))

    async def _fallback_emit(self, seq: int) -> None:
        try:
            await asyncio.sleep(self._fallback_ms / 1000)
            if seq != self._speak_seq:
                return
            if seq <= self._last_emitted_seq:
                return

            if self._speaker.is_idle():
                return

            await self._emit_speak_end(forced=True, reason="fallback_timeout")
            self._last_emitted_seq = seq
        except asyncio.CancelledError:
            return
