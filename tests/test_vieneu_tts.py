from types import SimpleNamespace
from unittest.mock import patch

import pytest

from src.vieneu_tts import (
    VieNeuUnavailableError,
    clear_vieneu_engine_cache,
    list_vieneu_voices,
    synthesize_vieneu_audio,
)
from src.video_export import VideoScene, synthesize_scene_audio


class _FakeVieNeu:
    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.infer_calls = []

    def list_preset_voices(self):
        return [("Bắc — giọng nữ", "BacNu"), {"label": "Nam", "id": "Nam"}]

    def infer(self, text, voice="", style=""):
        self.infer_calls.append((text, voice, style))
        return b"audio"

    @staticmethod
    def save(audio, path):
        assert audio == b"audio"
        open(path, "wb").write(b"RIFF-fake-wav")


def setup_function():
    clear_vieneu_engine_cache()


def test_vieneu_lists_and_synthesizes_preset_voice(tmp_path):
    module = SimpleNamespace(Vieneu=_FakeVieNeu)
    with patch("src.vieneu_tts.importlib.import_module", return_value=module):
        assert list_vieneu_voices() == [
            ("Bắc — giọng nữ", "BacNu"),
            ("Nam", "Nam"),
        ]
        result = synthesize_vieneu_audio(
            "Xin chào\tViệt Nam",
            tmp_path / "speech.mp3",
            voice="BacNu",
            style="tin_tuc",
        )

    assert result == tmp_path / "speech.wav"
    assert result.read_bytes() == b"RIFF-fake-wav"


def test_vieneu_missing_package_has_actionable_error(tmp_path):
    with patch(
        "src.vieneu_tts.importlib.import_module",
        side_effect=ModuleNotFoundError("vieneu"),
    ):
        with pytest.raises(VieNeuUnavailableError, match="pip install vieneu"):
            synthesize_vieneu_audio("Xin chào", tmp_path / "speech.wav")


def test_video_export_routes_vieneu_style(tmp_path):
    generated = tmp_path / "speech.wav"
    generated.write_bytes(b"audio")
    with patch(
        "src.video_export.synthesize_vieneu_audio",
        return_value=generated,
    ) as synthesize:
        result = synthesize_scene_audio(
            VideoScene(title="Slide 1", narration="Xin chào"),
            tmp_path / "speech.mp3",
            voice_engine="vieneu",
            voice_id="BacNu",
            vieneu_style="doc_truyen",
        )

    assert result == generated
    assert synthesize.call_args.kwargs["voice"] == "BacNu"
    assert synthesize.call_args.kwargs["style"] == "doc_truyen"
