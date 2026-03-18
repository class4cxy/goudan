"""
AudioEffector — 应用层（音频输出桥接）
=======================================
职责：
  - 实例化 Speaker 硬件层，注入防回声回调（mute/unmute）
  - 接收来自 WebSocket 的 action.speak 指令，转发给 Speaker 播放队列
  - 所有 TTS 句子播完后，向 Node.js 广播 sense.audio.speak_end 事件

不包含任何 TTS / 播放逻辑，仅做 WebSocket 指令 → Speaker 的路由。
硬件层详见 devices/speaker.py。
"""

import asyncio
import logging
import os

from devices import Speaker

logger = logging.getLogger(__name__)


class AudioEffector:
    """WebSocket 指令到 Speaker 的桥接层（应用层）。"""

    def __init__(self, audio_sensor: "AudioSensor | None" = None, ws_manager=None):
        self._audio_sensor = audio_sensor
        self._ws_manager = ws_manager
        self._loop: asyncio.AbstractEventLoop | None = None
        self._fallback_ms = int(os.environ.get("AUDIO_SPEAK_END_FALLBACK_MS", "15000"))
        self._idle_confirm_ms = int(os.environ.get("AUDIO_SPEAK_END_IDLE_CONFIRM_MS", "250"))
        self._speak_seq = 0
        self._last_emitted_seq = 0
        self._fallback_task: asyncio.Task | None = None
        self._idle_confirm_task: asyncio.Task | None = None
        self._speaker = Speaker(
            on_play_start=audio_sensor.mute if audio_sensor else None,
            on_play_end=self._on_play_end,
        )

    def _on_play_end(self) -> None:
        """每句 TTS 播完后调用：先 unmute，再判断队列是否清空。"""
        if self._audio_sensor:
            self._audio_sensor.unmute()

        # 为避免句间短暂空窗（LLM 下一句尚未入队）导致误判，增加短延迟二次确认。
        if self._speaker.is_idle() and self._ws_manager and self._loop:
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

            if self._audio_sensor:
                self._audio_sensor.unmute()
            await self._emit_speak_end(forced=True, reason="fallback_timeout")
            self._last_emitted_seq = seq
        except asyncio.CancelledError:
            return
