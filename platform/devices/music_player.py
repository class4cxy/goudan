"""
MusicPlayer — 音乐播放器硬件抽象层
=====================================
职责：
  1. 在线音乐搜索（yt-dlp 搜索 YouTube，获取音频流 URL + 标题）
  2. 本地音乐文件播放（MUSIC_DIR 目录，支持 MP3/FLAC/WAV/AAC/OGG/M4A）
  3. 异步播放（ffplay 单进程，支持 SIGSTOP/SIGCONT 暂停/恢复）
  4. 播放队列管理（当前曲目 + 待播列表）
  5. TTS 闪避（pause_for_tts / resume_after_tts），TTS 播放时自动暂停音乐

依赖（系统命令）：
  - ffplay（ffmpeg 套件，sudo apt install ffmpeg）
  - yt-dlp（pip install yt-dlp，在线搜索时才需要）

环境变量：
  MUSIC_DIR         本地音乐目录，默认 /home/pi/Music
  YTDLP_PATH        yt-dlp 可执行文件路径，默认 yt-dlp
  FFPLAY_PATH       ffplay 可执行文件路径，默认 ffplay
  MUSIC_VOLUME      默认音量（0.0–2.0），默认 1.5
  YTDLP_TIMEOUT_S   yt-dlp 解析超时，默认 30s

播放方案说明：
  使用 ffplay -nodisp -autoexit 播放，音频输出走系统默认 PulseAudio sink
  （BluetoothManager 已将默认 sink 设为蓝牙音箱，TTS 和音乐共享同一路由）。

  暂停/恢复使用 POSIX SIGSTOP/SIGCONT 信号，对 ffplay 单进程有效，
  音频会有短暂缓冲区溢出点击声，属于预期行为。
"""

import asyncio
import logging
import os
import signal
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Callable

logger = logging.getLogger(__name__)

MUSIC_DIR = Path(os.environ.get("MUSIC_DIR", "/home/pi/Music"))
YTDLP_PATH = os.environ.get("YTDLP_PATH", "yt-dlp")
FFPLAY_PATH = os.environ.get("FFPLAY_PATH", "ffplay")
DEFAULT_VOLUME = float(os.environ.get("MUSIC_VOLUME", "1.5"))
YTDLP_TIMEOUT_S = float(os.environ.get("YTDLP_TIMEOUT_S", "30"))

SUPPORTED_EXTENSIONS = {".mp3", ".flac", ".wav", ".aac", ".ogg", ".m4a", ".opus"}


class MusicState(str, Enum):
    IDLE = "idle"
    LOADING = "loading"
    PLAYING = "playing"
    PAUSED = "paused"


@dataclass
class Track:
    query: str
    url: str | None = None
    title: str | None = None
    is_local: bool = False

    def to_dict(self) -> dict:
        return {
            "title": self.title or self.query,
            "query": self.query,
            "is_local": self.is_local,
        }


StateChangeCallback = Callable[[MusicState, Track | None], None]


class MusicPlayer:
    """
    异步音乐播放器（硬件层）。

    不含任何 WebSocket / Spine 逻辑，通过回调通知上层状态变化。

    Args:
        on_state_change: 状态变化回调（新状态，当前曲目），供上层广播 WebSocket 事件
    """

    def __init__(self, on_state_change: StateChangeCallback | None = None):
        self._state = MusicState.IDLE
        self._queue: deque[Track] = deque()
        self._current: Track | None = None
        self._proc: asyncio.subprocess.Process | None = None
        self._play_task: asyncio.Task | None = None
        self._on_state_change = on_state_change
        self._volume = DEFAULT_VOLUME
        self._paused_for_tts = False

    # ─── 公共接口 ─────────────────────────────────────────────────────

    @property
    def state(self) -> MusicState:
        return self._state

    @property
    def current(self) -> Track | None:
        return self._current

    async def play(self, query: str, interrupt: bool = True) -> dict:
        """
        播放音乐。

        Args:
            query:     搜索词（在线）或文件名（本地）或 HTTP URL（直链）
            interrupt: True 时打断当前播放，False 时加入队列末尾
        """
        track = self._make_track(query)

        if interrupt:
            await self._cancel_play_task()
            self._queue.clear()
            self._queue.appendleft(track)
        else:
            self._queue.append(track)

        if self._play_task is None or self._play_task.done():
            self._play_task = asyncio.create_task(self._play_loop(), name="music_play_loop")

        return {
            "queued": query,
            "title": track.title,
            "mode": "interrupt" if interrupt else "queue",
            "is_local": track.is_local,
        }

    async def enqueue(self, queries: list[str]) -> dict:
        """将多首歌曲加入播放队列末尾，不打断当前播放。"""
        for q in queries:
            self._queue.append(self._make_track(q))

        if self._play_task is None or self._play_task.done():
            self._play_task = asyncio.create_task(self._play_loop(), name="music_play_loop")

        return {"queued_count": len(queries)}

    async def pause(self) -> dict:
        """暂停当前播放（SIGSTOP）。"""
        if self._state == MusicState.PLAYING and self._proc:
            try:
                os.kill(self._proc.pid, signal.SIGSTOP)
                self._set_state(MusicState.PAUSED)
                return {"ok": True, "state": "paused"}
            except (ProcessLookupError, OSError) as e:
                return {"ok": False, "error": str(e)}
        return {"ok": False, "error": f"非播放状态（当前：{self._state}）"}

    async def resume(self) -> dict:
        """恢复播放（SIGCONT）。"""
        if self._state == MusicState.PAUSED and self._proc:
            try:
                os.kill(self._proc.pid, signal.SIGCONT)
                self._set_state(MusicState.PLAYING)
                return {"ok": True, "state": "playing"}
            except (ProcessLookupError, OSError) as e:
                return {"ok": False, "error": str(e)}
        return {"ok": False, "error": f"非暂停状态（当前：{self._state}）"}

    async def stop(self) -> dict:
        """停止播放，清空队列。"""
        await self._cancel_play_task()
        self._queue.clear()
        await self._kill_proc()
        self._current = None
        self._paused_for_tts = False
        self._set_state(MusicState.IDLE)
        return {"ok": True, "state": "idle"}

    async def next(self) -> dict:
        """跳到下一曲。若队列为空，则停止。"""
        if self._state in (MusicState.PLAYING, MusicState.PAUSED, MusicState.LOADING):
            if self._state == MusicState.PAUSED and self._proc:
                try:
                    os.kill(self._proc.pid, signal.SIGCONT)
                except (ProcessLookupError, OSError):
                    pass
            await self._kill_proc()
            return {"ok": True, "message": "已跳至下一曲"}
        return {"ok": False, "error": "没有正在播放的曲目"}

    def set_volume(self, volume: float) -> dict:
        """设置音量（0.0–2.0，1.0 为原始音量）。注意：对当前播放生效需重新启动曲目。"""
        self._volume = max(0.0, min(2.0, volume))
        return {"ok": True, "volume": self._volume}

    def status(self) -> dict:
        """获取当前播放状态。"""
        queue_preview = [t.title or t.query for t in list(self._queue)[:5]]
        return {
            "state": self._state.value,
            "current": self._current.to_dict() if self._current else None,
            "queue_length": len(self._queue),
            "queue_preview": queue_preview,
            "volume": self._volume,
        }

    def list_local(self) -> list[str]:
        """列出本地音乐目录中的音频文件。"""
        if not MUSIC_DIR.exists():
            return []
        return sorted(
            f.name
            for f in MUSIC_DIR.iterdir()
            if f.is_file() and f.suffix.lower() in SUPPORTED_EXTENSIONS
        )

    # ─── TTS 闪避 ────────────────────────────────────────────────────

    async def pause_for_tts(self) -> None:
        """TTS 开始时调用：若正在播放，则暂停音乐并记录标记。"""
        if self._state == MusicState.PLAYING:
            self._paused_for_tts = True
            await self.pause()
            logger.debug("[MusicPlayer] TTS 开始，音乐已暂停（闪避）")

    async def resume_after_tts(self) -> None:
        """TTS 结束时调用：若是因 TTS 而暂停，则恢复播放。"""
        if self._state == MusicState.PAUSED and self._paused_for_tts:
            self._paused_for_tts = False
            await self.resume()
            logger.debug("[MusicPlayer] TTS 结束，音乐已恢复")

    # ─── 内部逻辑 ────────────────────────────────────────────────────

    def _make_track(self, query: str) -> Track:
        """根据查询字符串生成 Track 对象（本地文件 / HTTP URL / 在线搜索）。"""
        if query.startswith("http://") or query.startswith("https://"):
            return Track(query=query, url=query, title=query)

        local_path = MUSIC_DIR / query
        if local_path.exists() and local_path.suffix.lower() in SUPPORTED_EXTENSIONS:
            return Track(query=query, url=str(local_path), title=local_path.stem, is_local=True)

        return Track(query=query)

    def _set_state(self, state: MusicState) -> None:
        self._state = state
        if self._on_state_change:
            try:
                self._on_state_change(state, self._current)
            except Exception as e:
                logger.error("[MusicPlayer] on_state_change 回调异常：%s", e)

    async def _cancel_play_task(self) -> None:
        if self._play_task and not self._play_task.done():
            self._play_task.cancel()
            try:
                await self._play_task
            except asyncio.CancelledError:
                pass
        self._play_task = None

    async def _kill_proc(self) -> None:
        """强制停止 ffplay 进程。"""
        if self._proc and self._proc.returncode is None:
            if self._state == MusicState.PAUSED:
                try:
                    os.kill(self._proc.pid, signal.SIGCONT)
                except (ProcessLookupError, OSError):
                    pass
            try:
                self._proc.kill()
                await asyncio.wait_for(self._proc.wait(), timeout=3.0)
            except (ProcessLookupError, OSError, asyncio.TimeoutError):
                pass
        self._proc = None

    async def _play_loop(self) -> None:
        """播放队列循环：依次取出 Track，解析 URL，播放，循环直到队列为空。"""
        try:
            while self._queue:
                track = self._queue.popleft()
                self._current = track
                self._set_state(MusicState.LOADING)

                resolved = await self._resolve_track(track)
                if not resolved:
                    logger.error("[MusicPlayer] 无法解析曲目，跳过：%s", track.query)
                    continue

                self._set_state(MusicState.PLAYING)
                logger.info("[MusicPlayer] 开始播放：%s", track.title or track.query)

                try:
                    await self._play_single(track)
                except asyncio.CancelledError:
                    await self._kill_proc()
                    raise
                except Exception as e:
                    logger.error("[MusicPlayer] 播放异常：%s", e)
        except asyncio.CancelledError:
            pass
        finally:
            await self._kill_proc()
            self._current = None
            self._set_state(MusicState.IDLE)

    async def _resolve_track(self, track: Track) -> bool:
        """
        解析 Track 的流 URL 和标题。

        - 本地文件 / 直链：直接使用，无需解析
        - 在线搜索：调用 yt-dlp 获取 URL 和标题
        """
        if track.url:
            if not track.title:
                track.title = Path(track.url).stem if track.is_local else track.query
            return True

        try:
            search_term = f"ytsearch1:{track.query}"

            proc_url = await asyncio.create_subprocess_exec(
                YTDLP_PATH,
                "--get-url",
                "-f", "bestaudio/best",
                "--no-playlist",
                "--no-warnings",
                search_term,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            url_bytes, _ = await asyncio.wait_for(proc_url.communicate(), timeout=YTDLP_TIMEOUT_S)
            urls = url_bytes.decode().strip().split("\n")
            track.url = next((u for u in urls if u.startswith("http")), None)

            if not track.url:
                return False

            proc_title = await asyncio.create_subprocess_exec(
                YTDLP_PATH,
                "--print", "title",
                "--no-playlist",
                "--no-warnings",
                search_term,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            title_bytes, _ = await asyncio.wait_for(proc_title.communicate(), timeout=YTDLP_TIMEOUT_S)
            raw_title = title_bytes.decode().strip().split("\n")[0]
            track.title = raw_title or track.query

            logger.info("[MusicPlayer] yt-dlp 解析完成：%s → %s", track.query, track.title)
            return True

        except asyncio.TimeoutError:
            logger.error("[MusicPlayer] yt-dlp 解析超时（%.0fs）：%s", YTDLP_TIMEOUT_S, track.query)
            return False
        except FileNotFoundError:
            logger.error("[MusicPlayer] yt-dlp 未找到，请安装：pip install yt-dlp")
            return False
        except Exception as e:
            logger.error("[MusicPlayer] yt-dlp 解析异常：%s", e)
            return False

    async def _play_single(self, track: Track) -> None:
        """
        启动 ffplay 进程播放单首曲目，等待播放完成。

        ffplay 直接连接 PulseAudio 默认 sink，无需额外配置。
        SIGSTOP/SIGCONT 实现暂停/恢复，ffplay 单进程可安全使用。
        """
        cmd = [
            FFPLAY_PATH,
            "-nodisp",
            "-autoexit",
            "-loglevel", "quiet",
            "-af", f"volume={self._volume}",
            track.url,
        ]

        try:
            self._proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
        except FileNotFoundError:
            logger.error("[MusicPlayer] ffplay 未找到，请安装：sudo apt install ffmpeg")
            return

        try:
            await self._proc.wait()
            if self._proc.returncode not in (0, -9, -15):
                logger.warning("[MusicPlayer] ffplay 非正常退出，码：%d，曲目：%s",
                               self._proc.returncode, track.title)
        except asyncio.CancelledError:
            await self._kill_proc()
            raise
        finally:
            self._proc = None
