from __future__ import annotations

import json
import shutil
from dataclasses import replace
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.routes import create_router
from config.settings import get_settings
from services.synthesis_service import SynthesisService


def test_storyboard_endpoint_renders_registered_default_profile(local_env, reference_wav) -> None:
    root = local_env / "service_root"
    reference_target = root / "voices" / "default" / "reference.wav"
    reference_target.parent.mkdir(parents=True)
    shutil.copyfile(reference_wav, reference_target)
    transcript = reference_target.with_name("transcript.txt")
    transcript.write_text("Xin chào quý anh chị.", encoding="utf-8")
    registry_dir = root / "config"
    registry_dir.mkdir()
    (registry_dir / "voices.json").write_text(
        json.dumps(
            {
                "default": {
                    "engine": "dummy",
                    "reference_audio": "voices/default/reference.wav",
                    "reference_text_file": "voices/default/transcript.txt",
                    "language": "vi",
                    "enabled": True,
                }
            }
        ),
        encoding="utf-8",
    )
    settings = replace(
        get_settings(),
        root=root,
        voice_dir=root / "voices",
        output_dir=root / "generated_audio",
        cache_dir=root / "cache",
        log_dir=root / "logs",
        temp_dir=root / "temp",
        model_cache_dir=root / "models",
    )
    settings.ensure_directories()
    service = SynthesisService(settings)
    app = FastAPI()
    app.include_router(create_router(service, settings))

    with TestClient(app) as client:
        response = client.post(
            "/v1/storyboard/synthesize",
            json={
                "model": "dummy",
                "voice_id": "default",
                "output_format": "wav",
                "slides": [
                    {"slide": 1, "text": "Kính thưa quý anh chị."},
                    {"slide": 2, "text": "Xin cảm ơn quý anh chị."},
                ],
            },
        )
    payload = response.json()
    assert response.status_code == 200
    assert payload["success"] is True
    assert [item["path"] for item in payload["files"]] == [
        "generated_audio/slide_001.wav",
        "generated_audio/slide_002.wav",
    ]
    assert (root / "generated_audio" / "slide_001.wav").exists()
    assert (root / "generated_audio" / "slide_002.wav").exists()
    assert (root / payload["manifest"]).exists()

