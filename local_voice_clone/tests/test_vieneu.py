from __future__ import annotations

from pathlib import Path

import numpy as np
import soundfile as sf

from config.settings import get_settings
from engines.base import EngineStatus
from services.synthesis_service import SynthesisService


class FakeVieNeuEngine:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str]] = []

    def load(self) -> None:
        return None

    def status(self) -> EngineStatus:
        return EngineStatus(
            id="vieneu",
            available=True,
            loaded=True,
            device="cpu",
            model="fake-vieneu",
        )

    def synthesize_with_voice(
        self,
        *,
        text: str,
        voice_id: str | None,
        style: str,
        output_path: str | Path,
        language: str,
        speed: float,
    ) -> str:
        del language, speed
        self.calls.append((text, voice_id or "", style))
        sample_rate = 24_000
        timeline = np.arange(sample_rate // 4, dtype=np.float32) / sample_rate
        sf.write(output_path, 0.1 * np.sin(2 * np.pi * 220 * timeline), sample_rate)
        return str(output_path)


def test_vieneu_preset_voice_does_not_require_reference_and_hits_cache(local_env, monkeypatch) -> None:
    service = SynthesisService(get_settings())
    fake = FakeVieNeuEngine()
    monkeypatch.setattr(service, "engine", lambda name: fake)

    first = service.synthesize(
        model="vieneu",
        voice_id="female-northern",
        voice_style="tin_tuc",
        text="Kính thưa quý anh chị, đây là giọng đọc VieNeu local.",
        output_name="scene_001",
    )
    second = service.synthesize(
        model="vieneu",
        voice_id="female-northern",
        voice_style="tin_tuc",
        text="Kính thưa quý anh chị, đây là giọng đọc VieNeu local.",
        output_name="scene_001",
    )

    assert first.audio_path.exists()
    assert first.duration_seconds > 0
    assert first.voice_id == "female-northern"
    assert fake.calls[0][1:] == ("female-northern", "tin_tuc")
    assert second.cache_hit is True

