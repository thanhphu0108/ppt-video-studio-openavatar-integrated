from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

from .base import EngineStatus, VoiceCloneEngine


# The local service is often started with ``local_voice_clone`` as its current
# directory.  Reuse the adapter owned by the parent application instead of
# maintaining a second VieNeu SDK compatibility layer.
PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.vieneu_tts import (  # noqa: E402
    DEFAULT_STYLE,
    list_vieneu_voices,
    get_vieneu_engine,
    DEFAULT_VOICE_REGION,
    synthesize_vieneu_audio,
    vieneu_available,
    vieneu_install_hint,
)


class VieneuTTSEngine(VoiceCloneEngine):
    """VieNeu v3 Turbo preset and Vietnamese reference-clone engine.

    ``synthesize_with_voice`` keeps the built-in preset path, while the common
    ``synthesize`` contract passes a reference recording to v3 Turbo's
    zero-shot ``ref_audio`` argument.
    """

    id = "vieneu"

    def __init__(self, *, device: str = "auto") -> None:
        self.device = device
        self.backend = os.getenv("VIENEU_BACKEND", "").strip().lower() or None
        self._engine: Any | None = None

    def load(self) -> None:
        if self._engine is None:
            self._engine = get_vieneu_engine(backend=self.backend)

    def list_preset_voices(self) -> list[tuple[str, str]]:
        self.load()
        return list_vieneu_voices(backend=self.backend)

    def synthesize_with_voice(
        self,
        *,
        text: str,
        voice_id: str | None,
        style: str = DEFAULT_STYLE,
        output_path: str | Path,
        language: str = "vi",
        speed: float = 1.0,
    ) -> str:
        # VieNeu v3 currently controls language/speed through the model/preset
        # rather than the legacy clone-engine arguments.  Keep accepting the
        # common arguments so the API remains compatible; the adapter validates
        # and normalizes the text before inference.
        del language, speed
        self.load()
        generated = synthesize_vieneu_audio(
            text,
            output_path,
            voice=voice_id or "",
            style=style,
            backend=self.backend,
        )
        if generated is None:
            raise RuntimeError("VieNeu-TTS không nhận được nội dung cần đọc.")
        return str(generated)

    def synthesize(
        self,
        text: str,
        reference_audio: str | Path | None,
        reference_text: str | None,
        output_path: str | Path,
        language: str = "vi",
        speed: float = 1.0,
        voice_region: str = DEFAULT_VOICE_REGION,
    ) -> str:
        self.load()
        generated = synthesize_vieneu_audio(
            text,
            output_path,
            # A reference audio takes precedence over a preset voice.  This
            # is the Vietnamese zero-shot clone path used by model=
            # "vieneu-clone" in the API.
            voice="" if reference_audio is not None else (reference_text or ""),
            style=DEFAULT_STYLE,
            backend=self.backend,
            reference_audio=reference_audio,
            reference_text=reference_text,
            voice_region=voice_region,
        )
        if generated is None:
            raise RuntimeError("VieNeu-TTS không nhận được nội dung cần đọc.")
        return str(generated)

    def status(self) -> EngineStatus:
        available = vieneu_available()
        return EngineStatus(
            id=self.id,
            available=available,
            loaded=self._engine is not None,
            device=self.backend or self.device,
            model="VieNeu-TTS v3 Turbo",
            message="" if available else vieneu_install_hint(),
        )
