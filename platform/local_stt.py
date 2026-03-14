"""
LocalSTT — 本地语音识别引擎（faster-whisper）
==============================================
职责：
  - 封装 faster-whisper 模型的加载与推理
  - 提供线程安全的 transcribe() 方法
  - 优雅降级：faster-whisper 未安装时 is_available=False，不影响其他模块

依赖：pip install faster-whisper
模型选择（由环境变量 LOCAL_STT_MODEL 控制，默认 base）：
  tiny   ~39MB  CPU 推理约 0.5s  适合对速度要求极高的场景，中文准确率略低
  base   ~74MB  CPU 推理约 1-2s  速度与精度均衡，推荐默认值
  small  ~244MB CPU 推理约 3-5s  精度更高，但树莓派 5 略慢
"""

import io
import wave
import base64
import logging
import threading
import os

logger = logging.getLogger(__name__)


class LocalSTT:
    """基于 faster-whisper 的本地语音识别引擎（线程安全）。"""

    def __init__(self) -> None:
        model_size    = os.environ.get("LOCAL_STT_MODEL", "base")
        device        = os.environ.get("LOCAL_STT_DEVICE", "cpu")
        compute_type  = os.environ.get("LOCAL_STT_COMPUTE", "int8")

        self._model_size   = model_size
        self._device       = device
        self._compute_type = compute_type
        self._model        = None
        self._lock         = threading.Lock()
        self.is_available  = False

    def load(self) -> bool:
        """
        加载 Whisper 模型（首次运行自动下载，base 模型约 74MB）。
        返回 True 表示加载成功，False 表示 faster-whisper 未安装或加载失败。
        """
        try:
            from faster_whisper import WhisperModel  # type: ignore
            logger.info(
                f"[LocalSTT] 加载模型：{self._model_size} "
                f"device={self._device} compute={self._compute_type} ..."
            )
            self._model = WhisperModel(
                self._model_size,
                device=self._device,
                compute_type=self._compute_type,
            )
            self.is_available = True
            logger.info("[LocalSTT] 模型加载完成，本地 STT 就绪")
            return True
        except ImportError:
            logger.warning(
                "[LocalSTT] faster-whisper 未安装，本地 STT 不可用。"
                "安装：pip install faster-whisper"
            )
            return False
        except Exception as e:
            logger.error(f"[LocalSTT] 模型加载失败：{e}")
            return False

    def transcribe(self, audio_b64: str, sample_rate: int) -> str:
        """
        将 base64 编码的原始 PCM（16-bit mono）转为中文文字。

        Args:
            audio_b64:   base64 编码的 PCM 原始字节
            sample_rate: 采样率（Hz），通常为 16000

        Returns:
            识别出的文字，空音频返回空字符串。

        Raises:
            RuntimeError: 模型未加载或不可用
        """
        if not self.is_available or self._model is None:
            raise RuntimeError("LocalSTT 不可用，请检查 faster-whisper 是否已安装")

        pcm_bytes = base64.b64decode(audio_b64)
        wav_bytes = _pcm_to_wav(pcm_bytes, sample_rate)

        with self._lock:
            segments, _ = self._model.transcribe(
                io.BytesIO(wav_bytes),
                language="zh",
                beam_size=5,
                vad_filter=True,       # 内置 VAD 过滤静音段
                vad_parameters=dict(
                    min_silence_duration_ms=300,
                ),
            )
            return "".join(seg.text for seg in segments).strip()

    @property
    def status(self) -> dict:
        return {
            "available":    self.is_available,
            "model":        self._model_size,
            "device":       self._device,
            "compute_type": self._compute_type,
        }


def _pcm_to_wav(pcm: bytes, sample_rate: int) -> bytes:
    """将原始 PCM（16-bit mono）封装为 WAV 格式（供 faster-whisper 读取）。"""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)          # 16-bit
        wf.setframerate(sample_rate)
        wf.writeframes(pcm)
    return buf.getvalue()
