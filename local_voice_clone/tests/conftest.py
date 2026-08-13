from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf


SERVICE_ROOT = Path(__file__).resolve().parents[1]
while str(SERVICE_ROOT) in sys.path:
    sys.path.remove(str(SERVICE_ROOT))
sys.path.insert(0, str(SERVICE_ROOT))


@pytest.fixture
def local_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point a test service at an isolated, disposable local data area."""

    monkeypatch.setenv("VOICE_ENGINE", "dummy")
    monkeypatch.setenv("DEFAULT_VOICE_ID", "default")
    monkeypatch.setenv("OUTPUT_DIR", str(tmp_path / "generated_audio"))
    monkeypatch.setenv("VOICE_DIR", str(tmp_path / "voices"))
    monkeypatch.setenv("CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setenv("LOG_DIR", str(tmp_path / "logs"))
    monkeypatch.setenv("TEMP_DIR", str(tmp_path / "temp"))
    monkeypatch.setenv("PRELOAD_MODEL", "false")
    monkeypatch.setenv("ENABLE_CACHE", "true")
    monkeypatch.setenv("MIN_REFERENCE_SECONDS", "0.5")
    monkeypatch.setenv("MAX_REFERENCE_SECONDS", "6")
    monkeypatch.setenv("REQUIRE_UPLOAD_PASSWORD", "true")
    monkeypatch.setenv("VOICE_UPLOAD_PASSWORD", "test-upload-password")
    from config.settings import get_settings

    get_settings.cache_clear()
    return tmp_path


@pytest.fixture
def reference_wav(tmp_path: Path) -> Path:
    path = tmp_path / "reference.wav"
    sample_rate = 24_000
    timeline = np.arange(sample_rate, dtype=np.float32) / sample_rate
    samples = 0.12 * np.sin(2 * np.pi * 220 * timeline)
    sf.write(path, samples, sample_rate, subtype="PCM_16")
    return path
