from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import numpy as np
import soundfile as sf

from config.settings import get_settings
from services.voice_profile_service import VoiceProfileService


def test_voice_profile_loads_reference_and_transcript(local_env) -> None:
    service_root = Path(__file__).resolve().parents[1]
    registry_root = local_env / "registry_root"
    voice_dir = registry_root / "voices" / "default"
    voice_dir.mkdir(parents=True)
    reference = voice_dir / "reference.wav"
    sf.write(reference, 0.1 * np.ones(24_000, dtype=np.float32), 24_000, subtype="PCM_16")
    transcript = voice_dir / "transcript.txt"
    transcript.write_text("Xin chào quý anh chị.", encoding="utf-8")
    config_dir = registry_root / "config"
    config_dir.mkdir()
    (config_dir / "voices.json").write_text(
        json.dumps(
            {
                "default": {
                    "engine": "f5-tts",
                    "reference_audio": "voices/default/reference.wav",
                    "reference_text_file": "voices/default/transcript.txt",
                    "language": "vi",
                    "enabled": True,
                }
            }
        ),
        encoding="utf-8",
    )
    settings = replace(get_settings(), root=registry_root)
    profile = VoiceProfileService(settings).get("default")
    assert profile.available
    assert profile.reference_text == "Xin chào quý anh chị."
    assert profile.to_dict()["engine"] == "f5-tts"

