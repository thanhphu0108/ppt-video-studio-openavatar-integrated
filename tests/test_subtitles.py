from PIL import Image, ImageChops

from src.video_export import (
    VideoScene,
    _draw_subtitle,
    _subtitle_rgb,
    _subtitle_reveal_text,
    build_srt,
)


def test_burned_subtitle_reveals_words_over_speech_time() -> None:
    narration = "Một hai ba bốn năm sáu"
    first = _subtitle_reveal_text(narration, 0.0, 6.0)
    last = _subtitle_reveal_text(narration, 6.0, 6.0)

    assert first == "Một"
    assert "sáu" not in first
    assert last.endswith("sáu")


def test_burned_subtitle_uses_short_karaoke_windows_and_clears_after_speech() -> None:
    narration = "aa bb cc dd ee ff gg hh ii jj"

    first_window = _subtitle_reveal_text(narration, 7.9, 10.0)
    second_window = _subtitle_reveal_text(narration, 8.1, 10.0)
    after_speech = _subtitle_reveal_text(narration, 10.1, 10.0)

    assert "hh" in first_window
    assert "ii" not in first_window
    assert "ii" in second_window
    assert "aa" not in second_window
    assert after_speech == ""


def test_progressive_srt_uses_audio_duration() -> None:
    srt = build_srt(
        [VideoScene(title="Slide 1", narration="Một hai ba bốn")],
        [5.0],
        audio_durations=[3.0],
    )

    assert srt.count(" --> ") == 4
    assert "Một" in srt
    assert "bốn" in srt
    assert "00:00:03,000" in srt


def test_burned_subtitle_accepts_colors_width_and_corner_alignment() -> None:
    base = Image.new("RGB", (400, 200), "white")
    left = _draw_subtitle(
        base,
        "Màu chữ",
        progressive=False,
        background_color="#123456",
        text_color="#fedcba",
        box_width_percent=50,
        alignment="Góc trái",
    )
    centered = _draw_subtitle(
        base,
        "Màu chữ",
        progressive=False,
        background_color="#123456",
        text_color="#fedcba",
        box_width_percent=50,
        alignment="Canh giữa",
    )

    left_box = ImageChops.difference(base, left).getbbox()
    centered_box = ImageChops.difference(base, centered).getbbox()
    assert left_box is not None and centered_box is not None
    assert left_box[0] < centered_box[0]
    assert _subtitle_rgb("#123456", (0, 0, 0)) == (18, 52, 86)
    assert _subtitle_rgb("#fedcba", (0, 0, 0)) == (254, 220, 186)
