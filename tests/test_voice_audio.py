from unittest.mock import Mock, patch

from src.audio_assets import AudioAsset, write_audio_asset
from src.video_export import VideoScene, synthesize_scene_audio
from src.voice_clone import (
    VoiceCloneConfig,
    _response_audio,
    is_loopback_voice_clone_endpoint,
    synthesize_voice_clone_audio,
)


def test_uploaded_scene_audio_is_written_with_original_extension(tmp_path):
    audio = AudioAsset(data=b"voice-bytes", filename="slide_001.wav")
    result = write_audio_asset(audio, tmp_path / "audio.mp3")

    assert result == tmp_path / "audio.wav"
    assert result.read_bytes() == b"voice-bytes"


def test_empty_uploaded_voice_is_not_written(tmp_path):
    assert write_audio_asset(AudioAsset(data=b"", filename="slide_001.mp3"), tmp_path / "audio.mp3") is None


def test_voice_clone_sends_reference_audio_and_writes_response(tmp_path):
    response = Mock()
    response.headers = {"content-type": "audio/mpeg"}
    response.content = b"cloned-audio"
    response.raise_for_status.return_value = None
    config = VoiceCloneConfig(
        endpoint="https://voice.example.test/synthesize",
        reference_audio=b"reference-audio",
        reference_filename="my_voice.wav",
        model="f5-tts",
        reference_transcript="Đây là giọng mẫu.",
    )

    with patch("src.voice_clone.requests.post", return_value=response) as post:
        result = synthesize_voice_clone_audio("Nội dung cần đọc", tmp_path / "voice.mp3", config=config)

    assert result.read_bytes() == b"cloned-audio"
    assert post.call_args.kwargs["data"]["text"] == "Nội dung cần đọc"
    assert post.call_args.kwargs["files"]["reference_audio"][0] == "my_voice.wav"


def test_voice_clone_accepts_base64_data_url():
    response = Mock()
    response.headers = {"content-type": "application/json"}
    response.json.return_value = {"audio_base64": "data:audio/mpeg;base64,YXVkaW8="}

    assert _response_audio(response, timeout_seconds=1, verify_ssl=True) == b"audio"


def test_loopback_detection_only_accepts_local_voice_service():
    assert is_loopback_voice_clone_endpoint("http://127.0.0.1:8009/v1/voice-clone/synthesize")
    assert not is_loopback_voice_clone_endpoint("https://voice.example.test/synthesize")


def test_local_voice_clone_requests_wav_source_for_lipsync(tmp_path):
    config = VoiceCloneConfig(
        endpoint="http://127.0.0.1:8009/v1/voice-clone/synthesize",
        reference_audio=b"reference-audio",
        reference_filename="my_voice.wav",
    )
    with patch("src.video_export.synthesize_voice_clone_audio", return_value=tmp_path / "voice.wav") as synthesize:
        result = synthesize_scene_audio(
            VideoScene(title="Slide 1", narration="Kính thưa quý anh chị."),
            tmp_path / "voice.mp3",
            voice_engine="voice_clone",
            voice_clone_config=config,
        )
    assert result.suffix == ".wav"
    assert synthesize.call_args.args[1].suffix == ".wav"
