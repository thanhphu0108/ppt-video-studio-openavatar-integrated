from pathlib import Path

from .base import EngineUnavailableError, VoiceCloneEngine
from .dummy_engine import DummyVoiceCloneEngine
from .f5_tts_engine import F5TTSEngine


def create_engine(
    name: str,
    *,
    device: str = "auto",
    model: str = "F5TTS_v1_Base",
    allow_model_download: bool = False,
    model_cache_dir: str | Path | None = None,
) -> VoiceCloneEngine:
    normalized = name.strip().lower()
    if normalized in {"f5-tts", "f5tts", "f5_tts"}:
        return F5TTSEngine(
            device=device,
            model_name=model,
            allow_model_download=allow_model_download,
            model_cache_dir=model_cache_dir,
        )
    if normalized == "dummy":
        return DummyVoiceCloneEngine(device=device)
    raise EngineUnavailableError(
        f"Engine '{name}' chưa được hỗ trợ. Dùng f5-tts hoặc dummy."
    )


__all__ = [
    "EngineUnavailableError",
    "VoiceCloneEngine",
    "DummyVoiceCloneEngine",
    "F5TTSEngine",
    "create_engine",
]
