from __future__ import annotations

import importlib
import importlib.util
import sys
from pathlib import Path

from fastapi.testclient import TestClient


def _client() -> TestClient:
    from config.settings import get_settings

    get_settings.cache_clear()
    module_name = "local_voice_clone_test_app"
    sys.modules.pop(module_name, None)
    app_path = Path(__file__).resolve().parents[1] / "app.py"
    spec = importlib.util.spec_from_file_location(module_name, app_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return TestClient(module.app)


def test_health_models_and_ui_work_in_dummy_mode(local_env) -> None:
    with _client() as client:
        health = client.get("/health")
        models = client.get("/v1/models")
        voices = client.get("/v1/voices")
        ui = client.get("/")
    assert health.status_code == 200
    assert health.json()["status"] == "ok"
    assert health.json()["engine"] == "dummy"
    assert models.status_code == 200
    assert any(item["id"] == "dummy" for item in models.json()["data"])
    assert voices.status_code == 200
    assert ui.status_code == 200
    assert "Local Voice Clone" in ui.text


def test_multipart_synthesis_requires_consent_and_returns_audio(local_env, reference_wav) -> None:
    form = {
        "model": "dummy",
        "text": "Kính thưa quý anh chị, hôm nay chúng ta cùng trao đổi.",
        "reference_transcript": "Xin chào quý anh chị.",
        "language": "vi",
        "speed": "1.0",
        "output_format": "wav",
    }
    audio_file = ("reference.wav", reference_wav.read_bytes(), "audio/wav")
    with _client() as client:
        denied = client.post(
            "/v1/voice-clone/synthesize-upload",
            data=form,
            files={"reference_audio": audio_file},
            headers={"X-Voice-Upload-Password": "test-upload-password"},
        )
        accepted = client.post(
            "/v1/voice-clone/synthesize",
            data={**form, "voice_use_consent": "true"},
            files={"reference_audio": audio_file},
            headers={"X-Voice-Upload-Password": "test-upload-password"},
        )
        payload = accepted.json()
        audio = client.get(payload["audio_url"])
    assert denied.status_code == 403
    assert denied.json()["error_code"] == "VOICE_USE_CONSENT_REQUIRED"
    assert accepted.status_code == 200
    assert payload["success"] is True
    assert payload["audio_url"].endswith(".wav")
    assert audio.status_code == 200
    assert len(audio.content) > 512
