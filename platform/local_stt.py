"""
LocalSTT — 本地语音识别引擎
==============================
支持两种后端，通过环境变量 LOCAL_STT_BACKEND 切换：

  LOCAL_STT_BACKEND=whisper  (默认) faster-whisper，纯 Python，开箱即用
  LOCAL_STT_BACKEND=qwen     antirez/qwen-asr 纯 C 实现，精度更高，需要先编译

Whisper 配置：
  LOCAL_STT_MODEL=base       模型大小：tiny / base / small
  LOCAL_STT_DEVICE=cpu
  LOCAL_STT_COMPUTE=int8

Qwen 配置：
  LOCAL_STT_QWEN_BIN=~/qwen-asr/qwen_asr          二进制路径
  LOCAL_STT_QWEN_MODEL=~/qwen-asr/qwen3-asr-0.6b  模型目录
"""

import io
import os
import wave
import base64
import logging
import threading
import subprocess
import time
from pathlib import Path

logger = logging.getLogger(__name__)


# ─── 工具函数 ─────────────────────────────────────────────────────────────────

def _pcm_to_wav(pcm: bytes, sample_rate: int) -> bytes:
    """将原始 PCM（16-bit mono）封装为 WAV 格式。"""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(pcm)
    return buf.getvalue()


# ─── Whisper 后端 ─────────────────────────────────────────────────────────────

class _WhisperBackend:
    name = "whisper"

    def __init__(self) -> None:
        self._model_size  = os.environ.get("LOCAL_STT_MODEL", "base")
        self._device      = os.environ.get("LOCAL_STT_DEVICE", "cpu")
        self._compute     = os.environ.get("LOCAL_STT_COMPUTE", "int8")
        self._model       = None
        self._lock        = threading.Lock()

    def load(self) -> bool:
        try:
            from faster_whisper import WhisperModel  # type: ignore
            logger.info(f"[LocalSTT/whisper] 加载模型 {self._model_size} ({self._device}/{self._compute})...")
            self._model = WhisperModel(self._model_size, device=self._device, compute_type=self._compute)
            logger.info("[LocalSTT/whisper] 模型加载完成")
            return True
        except ImportError:
            logger.warning("[LocalSTT/whisper] faster-whisper 未安装")
            return False
        except Exception as e:
            logger.error(f"[LocalSTT/whisper] 加载失败：{e}")
            return False

    def transcribe(self, audio_b64: str, sample_rate: int) -> str:
        if self._model is None:
            raise RuntimeError("Whisper 模型未加载")
        pcm = base64.b64decode(audio_b64)
        wav = _pcm_to_wav(pcm, sample_rate)
        with self._lock:
            segments, _ = self._model.transcribe(
                io.BytesIO(wav),
                language="zh",
                beam_size=5,
                vad_filter=False,
                initial_prompt="以下是普通话语音，使用简体中文输出。",
                no_speech_threshold=0.6,
                log_prob_threshold=-1.0,
                compression_ratio_threshold=2.4,
            )
            return "".join(seg.text for seg in segments).strip()

    @property
    def info(self) -> dict:
        return {"backend": "whisper", "model": self._model_size,
                "device": self._device, "compute_type": self._compute}


# ─── Qwen ASR 后端 ────────────────────────────────────────────────────────────

class _QwenBackend:
    name = "qwen"

    def __init__(self) -> None:
        bin_raw   = os.environ.get("LOCAL_STT_QWEN_BIN",   "~/qwen-asr/qwen_asr")
        model_raw = os.environ.get("LOCAL_STT_QWEN_MODEL", "~/qwen-asr/qwen3-asr-0.6b")
        self._bin   = str(Path(bin_raw).expanduser())
        self._model = str(Path(model_raw).expanduser())
        self._lock  = threading.Lock()

    def load(self) -> bool:
        if not Path(self._bin).exists():
            logger.warning(f"[LocalSTT/qwen] 二进制不存在：{self._bin}，请先编译 antirez/qwen-asr")
            return False
        if not Path(self._model).exists():
            logger.warning(f"[LocalSTT/qwen] 模型目录不存在：{self._model}，请先下载模型")
            return False
        logger.info(f"[LocalSTT/qwen] 就绪：{self._bin}  模型：{self._model}")
        return True

    def transcribe(self, audio_b64: str, sample_rate: int) -> str:
        pcm = base64.b64decode(audio_b64)
        wav = _pcm_to_wav(pcm, sample_rate)

        cmd = [
            self._bin,
            "-d", self._model,
            "--stdin",
            "--language", "Chinese",
            "--silent",
        ]

        with self._lock:
            result = subprocess.run(
                cmd,
                input=wav,
                capture_output=True,
                timeout=60,
            )
            if result.returncode != 0:
                err = result.stderr.decode(errors="replace").strip()
                raise RuntimeError(f"qwen_asr 退出码={result.returncode}：{err[:200]}")
            return result.stdout.decode("utf-8", errors="replace").strip()

    @property
    def info(self) -> dict:
        return {"backend": "qwen", "bin": self._bin, "model": self._model}


# ─── 统一入口 ─────────────────────────────────────────────────────────────────

class LocalSTT:
    """本地 STT 统一入口，根据 LOCAL_STT_BACKEND 选择后端。"""

    def __init__(self) -> None:
        backend_name = os.environ.get("LOCAL_STT_BACKEND", "whisper").lower()
        if backend_name == "qwen":
            self._backend = _QwenBackend()
        else:
            self._backend = _WhisperBackend()
        self.is_available = False

    def load(self) -> bool:
        self.is_available = self._backend.load()
        return self.is_available

    def transcribe(self, audio_b64: str, sample_rate: int) -> str:
        if not self.is_available:
            raise RuntimeError("LocalSTT 不可用")
        return self._backend.transcribe(audio_b64, sample_rate)

    @property
    def status(self) -> dict:
        return {"available": self.is_available, **self._backend.info}
