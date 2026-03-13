"""
AudioSensor — 应用层（音频输入桥接）
=====================================
职责：
  - 实例化 Microphone 硬件层，注入 WebSocket 广播回调
  - 将 Microphone 的语音事件（raw PCM）转换为 Spine 协议格式发布到 WebSocket

不包含任何硬件逻辑，仅做 Microphone → WebSocket 的数据路由。
硬件层详见 devices/microphone.py。

事件输出（发往 WebSocket → Spine）：
  sense.audio.speech_start  — 检测到有人开始说话（LOW 优先级）
  sense.audio.speech_end    — 说话结束，携带 base64 PCM 音频块（MEDIUM 优先级）
"""

import base64
import logging

from devices import Microphone

logger = logging.getLogger(__name__)


class AudioSensor:
    """Microphone 到 WebSocket 的桥接层（应用层）。"""

    def __init__(self, ws_manager: "ConnectionManager"):
        self._ws = ws_manager
        self._mic = Microphone(
            on_speech_start=self._on_speech_start,
            on_speech_end=self._on_speech_end,
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

    async def _on_speech_end(self, raw_pcm: bytes, sample_rate: int, duration_ms: int) -> None:
        await self._ws.broadcast({
            "type": "sense.audio.speech_end",
            "payload": {
                "audio_b64": base64.b64encode(raw_pcm).decode(),
                "sample_rate": sample_rate,
                "duration_ms": duration_ms,
            },
        })
