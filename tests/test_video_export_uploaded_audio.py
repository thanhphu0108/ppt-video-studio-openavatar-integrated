import io
import wave

from PIL import Image

from src.audio_assets import AudioAsset
from src.video_export import VideoScene, export_storyboard_video


def _silent_wav_bytes() -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(8000)
        writer.writeframes(b"\x00\x00" * 1600)
    return buffer.getvalue()


def test_export_storyboard_uses_uploaded_audio_for_a_scene(tmp_path):
    output, srt_path, srt_text = export_storyboard_video(
        [VideoScene(title="Slide 1", narration="Xin chào", source_slide_number=1)],
        tmp_path / "video.mp4",
        fps=1,
        voice_engine="uploaded",
        scene_audio_assets=[AudioAsset(_silent_wav_bytes(), "slide_001.wav")],
        slide_images=[Image.new("RGB", (64, 36), "navy")],
        burn_subtitles=False,
        srt_path=tmp_path / "video.srt",
    )

    assert output.exists() and output.stat().st_size > 0
    assert srt_path is not None and srt_path.exists()
    assert "Xin chào" in srt_text
