"""
AudioEffector — 应用层（音频输出桥接）
=======================================
职责：
  - 实例化 Speaker 硬件层，注入防回声回调（mute/unmute）
  - 接收来自 WebSocket 的 action.speak 指令，转发给 Speaker 播放队列

不包含任何 TTS / 播放逻辑，仅做 WebSocket 指令 → Speaker 的路由。
硬件层详见 devices/speaker.py。
"""

import logging

from devices import Speaker

logger = logging.getLogger(__name__)


class AudioEffector:
    """WebSocket 指令到 Speaker 的桥接层（应用层）。"""

    def __init__(self, audio_sensor: "AudioSensor | None" = None):
        self._speaker = Speaker(
            on_play_start=audio_sensor.mute if audio_sensor else None,
            on_play_end=audio_sensor.unmute if audio_sensor else None,
        )

    def set_sensor(self, sensor: "AudioSensor") -> None:
        """延迟注入 AudioSensor，解决循环依赖。"""
        self._speaker._on_play_start = sensor.mute
        self._speaker._on_play_end = sensor.unmute

    async def start(self) -> None:
        """启动播放队列（委托给 Speaker，阻塞）。"""
        await self._speaker.start()

    async def enqueue(self, text: str, interrupt: bool = False) -> None:
        """将播放指令加入队列（委托给 Speaker）。"""
        await self._speaker.enqueue(text, interrupt)
