from __future__ import annotations

import math
from pathlib import Path

import numpy as np
import soundfile as sf

from .base import EngineStatus, VoiceCloneEngine


class DummyVoiceCloneEngine(VoiceCloneEngine):
    """Engine test-only: tạo audio nhịp nhẹ, không clone hoặc phát tiếng nói thật."""

    id = "dummy"

    def __init__(self, *, device: str = "cpu") -> None:
        self.device = "cpu" if device == "auto" else device
        self._loaded = False

    def load(self) -> None:
        self._loaded = True

    def status(self) -> EngineStatus:
        return EngineStatus(
            id=self.id,
            available=True,
            loaded=self._loaded,
            device=self.device,
            model="deterministic-test-tone",
            message="Chỉ dùng kiểm thử API; không tạo giọng nói.",
        )

    def synthesize(
        self,
        text: str,
        reference_audio: str | Path,
        reference_text: str | None,
        output_path: str | Path,
        language: str = "vi",
        speed: float = 1.0,
    ) -> str:
        self.load()
        sample_rate = 24_000
        seconds = min(8.0, max(0.6, len(text.strip()) / max(8.0, 17.0 * speed)))
        samples = int(sample_rate * seconds)
        timeline = np.arange(samples, dtype=np.float32) / sample_rate
        seed = sum(ord(char) for char in (text + str(reference_audio))) % 80
        frequency = 180 + seed
        envelope = np.minimum(1.0, timeline * 8.0) * np.minimum(1.0, (seconds - timeline) * 8.0)
        wave = (0.04 * np.sin(2 * math.pi * frequency * timeline) * envelope).astype(np.float32)
        target = Path(output_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        sf.write(target, wave, sample_rate, subtype="PCM_16")
        return str(target.resolve())
