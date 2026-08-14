from __future__ import annotations

import asyncio
import math
import re
import shutil
import subprocess
import tempfile
import wave
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Sequence

import imageio.v2 as imageio
import imageio_ffmpeg
import numpy as np
from PIL import Image, ImageDraw, ImageFont, ImageOps

from .audio_assets import AudioAsset, write_audio_asset
from .avatar_api import AvatarApiConfig, generate_talking_head
from .voice_clone import (
    VoiceCloneConfig,
    is_loopback_voice_clone_endpoint,
    synthesize_voice_clone_audio,
)
from .vieneu_tts import synthesize_vieneu_audio


@dataclass(frozen=True)
class VideoScene:
    title: str
    subtitle: str = ""
    metrics: Sequence[tuple[str, str]] = field(default_factory=tuple)
    bullets: Sequence[str] = field(default_factory=tuple)
    narration: str = ""
    slide_number: int = 0
    slide_type: str = "content"
    transition: str = "fade"
    transition_seconds: float = 0.4
    pause_before: float = 0.1
    pause_after: float = 0.35
    subtitle_enabled: bool = True
    skip: bool = False
    chapter: str = ""
    audio_duration: float | None = None
    source_slide_number: int | None = None


def _font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    candidates = [
        "arialbd.ttf" if bold else "arial.ttf",
        "C:/Windows/Fonts/arialbd.ttf" if bold else "C:/Windows/Fonts/arial.ttf",
        "C:/Windows/Fonts/segoeuib.ttf" if bold else "C:/Windows/Fonts/segoeui.ttf",
    ]
    for candidate in candidates:
        try:
            return ImageFont.truetype(candidate, size)
        except OSError:
            continue
    return ImageFont.load_default()


def _wrap_text(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.ImageFont, max_width: int) -> list[str]:
    words = str(text).split()
    lines: list[str] = []
    current = ""
    for word in words:
        candidate = word if not current else f"{current} {word}"
        if draw.textbbox((0, 0), candidate, font=font)[2] <= max_width:
            current = candidate
        else:
            if current:
                lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines or [""]


def _draw_wrapped(
    draw: ImageDraw.ImageDraw,
    xy: tuple[int, int],
    text: str,
    font: ImageFont.ImageFont,
    fill: tuple[int, int, int],
    max_width: int,
    line_gap: int = 8,
) -> int:
    x, y = xy
    lines = _wrap_text(draw, text, font, max_width)
    for line in lines:
        draw.text((x, y), line, fill=fill, font=font)
        y += draw.textbbox((0, 0), line, font=font)[3] + line_gap
    return y


def make_slide(
    scene: VideoScene | str,
    subtitle: str = "",
    *,
    width: int = 1280,
    height: int = 720,
    bg: tuple[int, int, int] = (248, 250, 252),
) -> Image.Image:
    if isinstance(scene, str):
        scene = VideoScene(title=scene, subtitle=subtitle)

    img = Image.new("RGB", (width, height), bg)
    draw = ImageDraw.Draw(img)
    title_font = _font(42, bold=True)
    subtitle_font = _font(23)
    label_font = _font(19, bold=True)
    value_font = _font(25, bold=True)
    body_font = _font(24)
    small_font = _font(16)

    draw.rectangle((0, 0, width, 78), fill=(30, 64, 175))
    draw.text((52, 22), "Báo cáo giao ban bệnh viện", fill=(255, 255, 255), font=_font(24, bold=True))
    draw.text((width - 210, 25), "Tự động tổng hợp", fill=(219, 234, 254), font=small_font)

    y = _draw_wrapped(draw, (56, 108), scene.title, title_font, (15, 23, 42), width - 112, 6)
    if scene.subtitle:
        y = _draw_wrapped(draw, (58, y + 6), scene.subtitle, subtitle_font, (71, 85, 105), width - 116, 8)

    card_top = max(238, y + 18)
    metric_count = min(len(scene.metrics), 4)
    if metric_count:
        gap = 18
        card_width = math.floor((width - 112 - gap * (metric_count - 1)) / metric_count)
        for index, (label, value) in enumerate(scene.metrics[:4]):
            left = 56 + index * (card_width + gap)
            draw.rounded_rectangle(
                (left, card_top, left + card_width, card_top + 112),
                radius=8,
                fill=(255, 255, 255),
                outline=(203, 213, 225),
                width=1,
            )
            _draw_wrapped(draw, (left + 18, card_top + 18), label, label_font, (71, 85, 105), card_width - 36, 4)
            draw.text((left + 18, card_top + 62), value, fill=(15, 23, 42), font=value_font)
        body_top = card_top + 146
    else:
        body_top = card_top

    if scene.bullets:
        draw.text((58, body_top), "Nhận định và việc cần làm", fill=(15, 23, 42), font=_font(24, bold=True))
        y = body_top + 42
        for bullet in scene.bullets[:5]:
            draw.ellipse((62, y + 10, 72, y + 20), fill=(30, 64, 175))
            y = _draw_wrapped(draw, (88, y), bullet, body_font, (30, 41, 59), width - 150, 8) + 8

    draw.line((56, height - 44, width - 56, height - 44), fill=(226, 232, 240), width=1)
    draw.text(
        (56, height - 30),
        "Nguồn: dữ liệu dashboard HIS tại thời điểm xuất video",
        fill=(100, 116, 139),
        font=small_font,
    )
    return img


def _audio_duration(path: Path) -> float:
    with wave.open(str(path), "rb") as reader:
        frames = reader.getnframes()
        rate = reader.getframerate()
        return frames / float(rate or 1)


def _media_duration(path: Path) -> float | None:
    if path.suffix.lower() == ".wav":
        try:
            return _audio_duration(path)
        except wave.Error:
            pass

    ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    result = subprocess.run(
        [ffmpeg, "-i", str(path)],
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="ignore",
    )
    match = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", result.stderr or "")
    if not match:
        return None
    hours, minutes, seconds = match.groups()
    return int(hours) * 3600 + int(minutes) * 60 + float(seconds)


def _scene_durations(scenes: Sequence[VideoScene], audio_seconds: float | None) -> list[float]:
    if not scenes:
        return []
    if not audio_seconds:
        return [max(4.0, min(14.0, len(scene.narration or scene.title) / 13.0)) for scene in scenes]

    weights = [max(1, len(scene.narration or scene.title)) for scene in scenes]
    total_weight = sum(weights)
    raw = [audio_seconds * weight / total_weight for weight in weights]
    durations = [max(3.0, seconds) for seconds in raw]
    factor = audio_seconds / sum(durations)
    return [seconds * factor for seconds in durations]


def synthesize_edge_tts_audio(
    scenes: Sequence[VideoScene],
    output_path: str | Path,
    *,
    voice: str = "vi-VN-HoaiMyNeural",
    rate: str = "+0%",
) -> Path | None:
    text = "\n\n".join(scene.narration or f"{scene.title}. {scene.subtitle}" for scene in scenes)
    text = sanitize_tts_text(text)
    if not text.strip():
        return None

    try:
        import aiohttp.connector as aiohttp_connector
        import aiohttp.resolver as aiohttp_resolver

        aiohttp_resolver.DefaultResolver = aiohttp_resolver.ThreadedResolver
        aiohttp_connector.DefaultResolver = aiohttp_resolver.ThreadedResolver
        import edge_tts
    except ImportError:
        return None

    output_path = Path(output_path)

    async def save(target_voice: str, target_path: Path) -> None:
        communicate = edge_tts.Communicate(text, voice=target_voice, rate=rate)
        await communicate.save(str(target_path))

    voices = [voice]
    fallback_voice = "vi-VN-NamMinhNeural" if voice == "vi-VN-HoaiMyNeural" else "vi-VN-HoaiMyNeural"
    voices.append(fallback_voice)
    last_error: Exception | None = None
    for index, target_voice in enumerate(voices):
        target_path = output_path if index == 0 else output_path.with_name(f"{output_path.stem}_{index}{output_path.suffix}")
        try:
            asyncio.run(save(target_voice, target_path))
            if target_path.exists() and target_path.stat().st_size > 0:
                if target_path != output_path:
                    shutil.copyfile(target_path, output_path)
                return output_path
        except Exception as exc:
            last_error = exc
            continue

    if last_error:
        print(f"Không tạo được audio TTS: {last_error}")
    return None


def sanitize_tts_text(text: str, max_chars: int = 3200) -> str:
    text = re.sub(r"https?://\S+|www\.\S+", " liên kết tham khảo ", str(text or ""), flags=re.IGNORECASE)
    text = text.replace("\u200b", " ").replace("\ufeff", " ").replace("\x00", " ")
    text = re.sub(r"[\r\t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r"[^\S\n]+", " ", text)
    text = text.strip()
    if len(text) > max_chars:
        text = text[:max_chars].rsplit(" ", 1)[0].strip() + "."
    return text


def synthesize_narration_wav(
    scenes: Sequence[VideoScene],
    output_path: str | Path,
    *,
    rate: int = 165,
    voice_hint: str = "Vietnam",
) -> Path | None:
    text = "\n\n".join(scene.narration or f"{scene.title}. {scene.subtitle}" for scene in scenes)
    if not text.strip():
        return None

    try:
        import pyttsx3
    except ImportError:
        return None

    output_path = Path(output_path)
    engine = pyttsx3.init()
    engine.setProperty("rate", rate)
    for voice in engine.getProperty("voices") or []:
        label = " ".join(str(getattr(voice, attr, "")) for attr in ("id", "name", "languages"))
        if voice_hint.lower() in label.lower() or "vi" in label.lower():
            engine.setProperty("voice", voice.id)
            break
    engine.save_to_file(text, str(output_path))
    engine.runAndWait()
    return output_path if output_path.exists() and output_path.stat().st_size > 0 else None


def synthesize_scene_audio(
    scene: VideoScene,
    output_path: str | Path,
    *,
    voice_engine: str = "edge",
    voice_id: str = "vi-VN-HoaiMyNeural",
    voice_rate: str = "+0%",
    vieneu_style: str = "tu_nhien",
    voice_region: str = "auto",
    voice_clone_config: VoiceCloneConfig | None = None,
    uploaded_audio: AudioAsset | None = None,
) -> Path | None:
    """Chuẩn bị audio cho một cảnh từ TTS local, clone giọng hoặc bản thu thật."""

    output_path = Path(output_path)
    if voice_engine == "uploaded":
        if uploaded_audio is None:
            if scene.narration.strip():
                label = scene.source_slide_number or scene.slide_number or scene.slide_type
                raise ValueError(f"Thiếu bản thu lời đọc cho cảnh {label}.")
            return None
        target = write_audio_asset(uploaded_audio, output_path)
        return target if target is not None and target.exists() and target.stat().st_size > 0 else None

    text = scene.narration or f"{scene.title}. {scene.subtitle}"
    if not text.strip():
        return None
    if voice_engine == "voice_clone":
        if voice_clone_config is None:
            raise ValueError("Chưa cấu hình dịch vụ nhân bản giọng.")
        # Keep the local clone's source in WAV for OpenAvatar/Wav2Lip. Remote
        # clone providers preserve the requested suffix for compatibility.
        if is_loopback_voice_clone_endpoint(voice_clone_config.endpoint):
            output_path = output_path.with_suffix(".wav")
        return synthesize_voice_clone_audio(text, output_path, config=voice_clone_config)
    if voice_engine == "vieneu":
        return synthesize_vieneu_audio(
            text,
            output_path.with_suffix(".wav"),
            voice=voice_id,
            style=vieneu_style,
            voice_region=voice_region,
        )
    if voice_engine == "edge":
        return synthesize_edge_tts_audio(
            [scene], output_path, voice=voice_id, rate=voice_rate
        )
    return synthesize_narration_wav(
        [scene], output_path.with_suffix(".wav"), voice_hint=voice_id or "Vietnam"
    )


def export_video(
    slides: Iterable[Image.Image],
    output_path: str | Path,
    *,
    durations: Sequence[float] | None = None,
    seconds_per_slide: int = 4,
    fps: int = 24,
) -> Path:
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    slide_list = [slide.convert("RGB") for slide in slides]
    if not slide_list:
        raise ValueError("Cần ít nhất 1 slide để xuất video.")

    if durations is None:
        durations = [seconds_per_slide] * len(slide_list)
    if len(durations) != len(slide_list):
        raise ValueError("Số duration phải bằng số slide.")

    with imageio.get_writer(output_path, fps=fps, codec="libx264", quality=8) as writer:
        for slide, duration in zip(slide_list, durations):
            frame = np.asarray(slide)
            frame_count = max(1, int(round(duration * fps)))
            for _ in range(frame_count):
                writer.append_data(frame)
    return output_path


def _srt_timestamp(seconds: float) -> str:
    total_milliseconds = max(0, int(round(float(seconds) * 1000)))
    total_seconds, milliseconds = divmod(total_milliseconds, 1000)
    hours, remainder = divmod(total_seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours:02}:{minutes:02}:{secs:02},{milliseconds:03}"


def _subtitle_words(text: str) -> list[str]:
    return re.sub(r"\s+", " ", str(text or "")).strip().split()


def _subtitle_lines(words: Sequence[str], max_chars: int, max_lines: int) -> list[str]:
    lines: list[str] = []
    current = ""
    for word in words:
        candidate = word if not current else f"{current} {word}"
        if len(candidate) <= max_chars or not current:
            current = candidate
        else:
            lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines


def _subtitle_text(text: str, max_chars: int = 82, max_lines: int = 2) -> str:
    return "\n".join(_subtitle_lines(_subtitle_words(text), max_chars, max_lines)[:max_lines])


def _subtitle_word_end_times(words: Sequence[str], duration: float) -> list[float]:
    """Estimate word timings from text when the TTS engine has no alignment API."""

    if not words:
        return []
    safe_duration = max(0.05, float(duration))
    weights = []
    for word in words:
        letters = len(re.sub(r"[^\wÀ-ỹ]", "", word, flags=re.UNICODE))
        punctuation_pauses = len(re.findall(r"[,.;:!?…]", word))
        weights.append(max(1, letters) + punctuation_pauses * 2.5)
    total_weight = sum(weights) or 1.0
    elapsed = 0.0
    ends: list[float] = []
    for weight in weights:
        elapsed += safe_duration * weight / total_weight
        ends.append(min(safe_duration, elapsed))
    return ends


def _subtitle_window_text(
    words: Sequence[str],
    *,
    max_chars: int = 74,
    max_lines: int = 2,
) -> str:
    """Keep the newest spoken words visible in the two-line burned caption."""

    if not words:
        return ""
    selected: list[str] = []
    for word in reversed(words):
        candidate = [word, *selected]
        if selected and len(_subtitle_lines(candidate, max_chars, max_lines)) > max_lines:
            break
        selected = candidate
    return "\n".join(_subtitle_lines(selected, max_chars, max_lines)[:max_lines])


def _subtitle_chunks(
    words: Sequence[str],
    *,
    max_chars: int = 74,
    max_lines: int = 2,
    max_words: int = 8,
) -> list[list[str]]:
    """Split narration into short karaoke windows instead of one long scroll."""

    safe_max_lines = max(1, int(max_lines))
    safe_max_words = max(1, int(max_words))
    chunks: list[list[str]] = []
    current: list[str] = []

    for word in words:
        candidate = [*current, word]
        too_many_words = len(candidate) > safe_max_words
        too_many_lines = (
            bool(current)
            and len(_subtitle_lines(candidate, max_chars, safe_max_lines)) > safe_max_lines
        )
        if current and (too_many_words or too_many_lines):
            chunks.append(current)
            current = [word]
        else:
            current = candidate

    if current:
        chunks.append(current)
    return chunks


def _subtitle_reveal_text(
    text: str,
    elapsed_seconds: float,
    duration: float,
    *,
    max_chars: int = 74,
    max_lines: int = 2,
) -> str:
    """Reveal a short karaoke window, then clear it after speech ends."""

    words = _subtitle_words(text)
    if not words:
        return ""
    safe_duration = max(0.05, float(duration))
    elapsed = max(0.0, float(elapsed_seconds))
    if elapsed > safe_duration:
        return ""

    ends = _subtitle_word_end_times(words, safe_duration)
    visible_count = min(
        len(words),
        1 + sum(1 for end in ends[:-1] if elapsed >= end),
    )
    chunks = _subtitle_chunks(
        words,
        max_chars=max_chars,
        max_lines=max_lines,
    )

    word_offset = 0
    for chunk in chunks:
        chunk_end = word_offset + len(chunk)
        if visible_count <= chunk_end:
            visible_in_chunk = max(1, visible_count - word_offset)
            return "\n".join(
                _subtitle_lines(
                    chunk[:visible_in_chunk],
                    max_chars,
                    max_lines,
                )[:max_lines]
            )
        word_offset = chunk_end

    return "\n".join(_subtitle_lines(chunks[-1], max_chars, max_lines)[:max_lines])


def _progressive_subtitle_entries(
    text: str,
    duration: float,
    *,
    max_chars: int = 82,
    max_lines: int = 2,
) -> list[tuple[float, float, str]]:
    words = _subtitle_words(text)
    ends = _subtitle_word_end_times(words, duration)
    entries: list[tuple[float, float, str]] = []
    previous = 0.0
    word_offset = 0
    for chunk in _subtitle_chunks(
        words,
        max_chars=max_chars,
        max_lines=max_lines,
    ):
        chunk_ends = ends[word_offset:word_offset + len(chunk)]
        for local_index, end in enumerate(chunk_ends):
            caption = "\n".join(
                _subtitle_lines(
                    chunk[:local_index + 1],
                    max_chars,
                    max_lines,
                )[:max_lines]
            )
            if caption and end > previous:
                entries.append((previous, end, caption))
            previous = end
        word_offset += len(chunk)
    return entries


def build_srt(
    scenes: Sequence[VideoScene],
    durations: Sequence[float],
    *,
    audio_durations: Sequence[float | None] | None = None,
    max_chars: int = 82,
    progressive: bool = True,
) -> str:
    entries: list[str] = []
    current = 0.0
    index = 1
    for scene_index, (scene, duration) in enumerate(zip(scenes, durations)):
        if scene.subtitle_enabled and scene.narration.strip():
            audio_duration = (
                audio_durations[scene_index]
                if audio_durations is not None and scene_index < len(audio_durations)
                else None
            )
            speech_duration = (
                min(float(duration), float(audio_duration))
                if audio_duration and audio_duration > 0
                else float(duration)
            )
            if progressive:
                subtitle_entries = _progressive_subtitle_entries(
                    scene.narration,
                    speech_duration,
                    max_chars=max_chars,
                )
            else:
                subtitle_entries = [(0.0, float(duration), _subtitle_text(scene.narration, max_chars=max_chars))]
            for start, end, text in subtitle_entries:
                entries.append(
                    f"{index}\n{_srt_timestamp(current + start)} --> "
                    f"{_srt_timestamp(current + end)}\n{text}\n"
                )
                index += 1
        current += duration
    return "\n".join(entries)


def _subtitle_rgb(
    value: str | Sequence[int],
    fallback: tuple[int, int, int],
) -> tuple[int, int, int]:
    """Convert a color-picker value to an RGB tuple without breaking exports."""

    if isinstance(value, (tuple, list)) and len(value) >= 3:
        try:
            return tuple(max(0, min(255, int(channel))) for channel in value[:3])  # type: ignore[return-value]
        except (TypeError, ValueError):
            return fallback
    raw = str(value or "").strip().lstrip("#")
    if len(raw) == 3:
        raw = "".join(char * 2 for char in raw)
    if len(raw) >= 6 and re.fullmatch(r"[0-9a-fA-F]{6}", raw[:6]):
        return tuple(int(raw[index:index + 2], 16) for index in (0, 2, 4))  # type: ignore[return-value]
    return fallback


def _draw_subtitle(
    image: Image.Image,
    text: str,
    *,
    position: str = "Dưới",
    font_size: int = 28,
    max_lines: int = 2,
    elapsed_seconds: float = 0.0,
    reveal_duration: float | None = None,
    progressive: bool = True,
    background_color: str | Sequence[int] = "#000000",
    text_color: str | Sequence[int] = "#FFFFFF",
    box_width_percent: int = 85,
    alignment: str = "Canh giữa",
) -> Image.Image:
    if not text.strip():
        return image

    img = image.copy()
    draw = ImageDraw.Draw(img, "RGBA")
    font = _font(font_size, bold=True)
    try:
        width_percent = max(20, min(100, int(box_width_percent)))
    except (TypeError, ValueError):
        width_percent = 85
    side_margin = max(18, img.width // 36)
    box_width = max(120, int(img.width * width_percent / 100))
    if alignment in {"Góc trái", "Canh trái", "Trái"}:
        x1 = side_margin
        box_width = min(box_width, img.width - side_margin)
    elif alignment in {"Góc phải", "Canh phải", "Phải"}:
        box_width = min(box_width, img.width - side_margin)
        x1 = img.width - box_width - side_margin
    else:
        box_width = min(box_width, img.width)
        x1 = (img.width - box_width) // 2
    x2 = x1 + box_width
    horizontal_padding = max(16, font_size)
    max_chars = max(
        1,
        min(
            54,
            int((box_width - horizontal_padding * 2) / max(1.0, font_size * 0.62)),
        ),
    )
    subtitle = (
        _subtitle_reveal_text(
            text,
            elapsed_seconds,
            reveal_duration or 0.0,
            max_chars=max_chars,
            max_lines=max_lines,
        )
        if progressive
        else _subtitle_text(text, max_chars=max_chars, max_lines=max_lines)
    )
    # Hết lời thì bỏ cả chữ và nền, không để lại một khung đen rỗng trong
    # khoảng nghỉ cuối slide.
    if not subtitle.strip():
        return img
    lines = subtitle.splitlines()
    line_height = font_size + 8
    reserved_lines = max(1, int(max_lines))
    # Giữ chiều cao cố định để khung không nhảy lên khi từ dòng 1 sang dòng 2.
    box_height = line_height * reserved_lines + 28
    y = img.height - box_height - 36 if position == "Dưới" else (img.height - box_height) // 2
    background_rgb = _subtitle_rgb(background_color, (0, 0, 0))
    text_rgb = _subtitle_rgb(text_color, (255, 255, 255))
    draw.rounded_rectangle(
        (x1, y, x2, y + box_height),
        radius=8,
        fill=(*background_rgb, 150),
    )
    text_y = y + 14 + max(0, (reserved_lines - len(lines)) * line_height // 2)
    for line in lines:
        bbox = draw.textbbox((0, 0), line, font=font)
        line_width = bbox[2] - bbox[0]
        if alignment in {"Góc trái", "Canh trái", "Trái"}:
            text_x = x1 + horizontal_padding
        elif alignment in {"Góc phải", "Canh phải", "Phải"}:
            text_x = x2 - horizontal_padding - line_width
        else:
            text_x = x1 + (box_width - line_width) // 2
        draw.text((text_x, text_y), line, fill=(*text_rgb, 255), font=font)
        text_y += line_height
    return img


def _prepare_slide_frame(slide: Image.Image, width: int = 1280, height: int = 720) -> Image.Image:
    frame = slide.convert("RGB")
    frame.thumbnail((width, height))
    canvas = Image.new("RGB", (width, height), (0, 0, 0))
    left = (width - frame.width) // 2
    top = (height - frame.height) // 2
    canvas.paste(frame, (left, top))
    return canvas


def _zoom_frame(frame: Image.Image, progress: float, max_zoom: float = 1.03) -> Image.Image:
    zoom = 1.0 + (max_zoom - 1.0) * progress
    width, height = frame.size
    crop_w = max(1, int(width / zoom))
    crop_h = max(1, int(height / zoom))
    left = (width - crop_w) // 2
    top = (height - crop_h) // 2
    cropped = frame.crop((left, top, left + crop_w, top + crop_h))
    return cropped.resize((width, height))


def _fade_frame(frame: Image.Image, frame_index: int, total_frames: int, fade_frames: int) -> Image.Image:
    if fade_frames <= 0:
        return frame
    alpha = 1.0
    if frame_index < fade_frames:
        alpha = frame_index / max(1, fade_frames)
    elif frame_index >= total_frames - fade_frames:
        alpha = (total_frames - frame_index - 1) / max(1, fade_frames)
    if alpha >= 0.999:
        return frame
    black = Image.new("RGB", frame.size, (0, 0, 0))
    return Image.blend(black, frame, max(0.0, alpha))




def _prepare_avatar_image(avatar: Image.Image, size: int, shape: str = "Tròn") -> Image.Image:
    avatar = ImageOps.exif_transpose(avatar).convert("RGBA")
    avatar = ImageOps.fit(avatar, (size, size), method=Image.Resampling.LANCZOS)
    if shape == "Tròn":
        mask = Image.new("L", (size, size), 0)
        d = ImageDraw.Draw(mask)
        d.ellipse((0, 0, size - 1, size - 1), fill=255)
        avatar.putalpha(mask)
    else:
        mask = Image.new("L", (size, size), 0)
        d = ImageDraw.Draw(mask)
        d.rounded_rectangle((0, 0, size - 1, size - 1), radius=max(12, size // 10), fill=255)
        avatar.putalpha(mask)
    return avatar


def _overlay_avatar(
    frame: Image.Image,
    avatar: Image.Image,
    *,
    position: str,
    size_percent: int,
    shape: str,
    border_width: int,
    talking: bool,
    frame_index: int,
    fps: int,
) -> Image.Image:
    canvas = frame.convert("RGBA")
    size = max(96, int(canvas.width * size_percent / 100))
    pulse = 1.0
    if talking:
        pulse = 1.0 + 0.018 * (0.5 + 0.5 * math.sin(frame_index * 2 * math.pi * 4.2 / max(1, fps)))
    render_size = max(72, int(size * pulse))
    pip = _prepare_avatar_image(avatar, render_size, shape)

    margin = max(18, canvas.width // 64)
    positions = {
        "Trên trái": (margin, margin),
        "Trên phải": (canvas.width - render_size - margin, margin),
        "Dưới trái": (margin, canvas.height - render_size - margin),
        "Dưới phải": (canvas.width - render_size - margin, canvas.height - render_size - margin),
    }
    x, y = positions.get(position, positions["Dưới phải"])

    if border_width > 0:
        border_size = render_size + border_width * 2
        border = Image.new("RGBA", (border_size, border_size), (255, 255, 255, 0))
        d = ImageDraw.Draw(border)
        if shape == "Tròn":
            d.ellipse((0, 0, border_size - 1, border_size - 1), fill=(255, 255, 255, 235))
        else:
            d.rounded_rectangle((0, 0, border_size - 1, border_size - 1), radius=max(12, border_size // 10), fill=(255, 255, 255, 235))
        canvas.alpha_composite(border, (x - border_width, y - border_width))

    canvas.alpha_composite(pip, (x, y))
    return canvas.convert("RGB")


def _write_segment_video(
    slide: Image.Image,
    output_path: Path,
    duration: float,
    *,
    fps: int,
    scene: VideoScene,
    burn_subtitles: bool,
    subtitle_position: str,
    subtitle_font_size: int,
    subtitle_max_lines: int,
    subtitle_audio_duration: float | None = None,
    subtitle_progressive: bool = True,
    subtitle_background_color: str | Sequence[int] = "#000000",
    subtitle_text_color: str | Sequence[int] = "#FFFFFF",
    subtitle_box_width_percent: int = 85,
    subtitle_alignment: str = "Canh giữa",
    preserve_original_slide: bool = True,
    avatar_image: Image.Image | None = None,
    avatar_position: str = "Dưới phải",
    avatar_size_percent: int = 18,
    avatar_shape: str = "Tròn",
    avatar_border_width: int = 4,
    avatar_talking_effect: bool = False,
    ai_avatar_video_path: Path | None = None,
) -> Path:
    base = _prepare_slide_frame(slide)
    total_frames = max(1, int(round(duration * fps)))
    fade_frames = int(round(scene.transition_seconds * fps)) if scene.transition == "fade" else 0
    avatar_reader = imageio.get_reader(ai_avatar_video_path) if ai_avatar_video_path else None
    avatar_iterator = iter(avatar_reader) if avatar_reader is not None else None
    last_ai_avatar: Image.Image | None = None
    try:
        with imageio.get_writer(output_path, fps=fps, codec="libx264", quality=8) as writer:
            for frame_index in range(total_frames):
                progress = frame_index / max(1, total_frames - 1)
                if preserve_original_slide:
                    frame = base.copy()
                else:
                    frame = _zoom_frame(base, progress)
                    frame = _fade_frame(frame, frame_index, total_frames, fade_frames)
                current_avatar = avatar_image
                if avatar_iterator is not None:
                    try:
                        last_ai_avatar = Image.fromarray(next(avatar_iterator)).convert("RGBA")
                    except StopIteration:
                        pass
                    if last_ai_avatar is not None:
                        current_avatar = last_ai_avatar
                if current_avatar is not None:
                    frame = _overlay_avatar(
                        frame, current_avatar, position=avatar_position, size_percent=avatar_size_percent,
                        shape=avatar_shape, border_width=avatar_border_width,
                        talking=avatar_talking_effect and ai_avatar_video_path is None and bool(scene.narration.strip()),
                        frame_index=frame_index, fps=fps,
                    )
                if burn_subtitles and scene.subtitle_enabled:
                    frame = _draw_subtitle(
                        frame,
                        scene.narration,
                        position=subtitle_position,
                        font_size=subtitle_font_size,
                        max_lines=subtitle_max_lines,
                        elapsed_seconds=frame_index / max(1, fps),
                        reveal_duration=subtitle_audio_duration or duration,
                        progressive=subtitle_progressive,
                        background_color=subtitle_background_color,
                        text_color=subtitle_text_color,
                        box_width_percent=subtitle_box_width_percent,
                        alignment=subtitle_alignment,
                    )
                writer.append_data(np.asarray(frame))
    finally:
        if avatar_reader is not None:
            avatar_reader.close()
    return output_path


def _concat_videos(segment_paths: Sequence[Path], output_path: Path) -> Path:
    if not segment_paths:
        raise ValueError("Không có segment video để ghép.")
    if len(segment_paths) == 1:
        shutil.copyfile(segment_paths[0], output_path)
        return output_path

    list_path = output_path.parent / "segments.txt"
    list_path.write_text(
        "\n".join(f"file '{path.as_posix()}'" for path in segment_paths),
        encoding="utf-8",
    )
    ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    subprocess.run(
        [
            ffmpeg,
            "-y",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(list_path),
            "-c",
            "copy",
            str(output_path),
        ],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return output_path


def export_storyboard_video(
    scenes: Sequence[VideoScene],
    output_path: str | Path,
    *,
    fps: int = 24,
    with_voice: bool = True,
    voice_engine: str = "edge",
    voice_id: str = "vi-VN-HoaiMyNeural",
    voice_rate: str = "+0%",
    vieneu_style: str = "tu_nhien",
    voice_region: str = "auto",
    voice_clone_config: VoiceCloneConfig | None = None,
    scene_audio_assets: Sequence[AudioAsset | None] | None = None,
    slide_images: Sequence[Image.Image] | None = None,
    burn_subtitles: bool = True,
    subtitle_position: str = "Dưới",
    subtitle_font_size: int = 28,
    subtitle_max_lines: int = 2,
    subtitle_progressive: bool = True,
    subtitle_background_color: str | Sequence[int] = "#000000",
    subtitle_text_color: str | Sequence[int] = "#FFFFFF",
    subtitle_box_width_percent: int = 85,
    subtitle_alignment: str = "Canh giữa",
    preserve_original_slide: bool = True,
    pause_after: float = 0.35,
    srt_path: str | Path | None = None,
    avatar_image: Image.Image | None = None,
    avatar_position: str = "Dưới phải",
    avatar_size_percent: int = 18,
    avatar_shape: str = "Tròn",
    avatar_border_width: int = 4,
    avatar_talking_effect: bool = False,
    ai_avatar_config: AvatarApiConfig | None = None,
    local_avatar_videos: Sequence[bytes | None] | None = None,
) -> tuple[Path, Path | None, str]:
    active_scenes = [scene for scene in scenes if not scene.skip]
    if not active_scenes:
        raise ValueError("Không có slide nào được chọn để xuất video.")

    if scene_audio_assets is not None and len(scene_audio_assets) != len(scenes):
        raise ValueError("Số bản thu lời đọc phải bằng số cảnh trong storyboard.")

    if slide_images:
        active_images = [image for scene, image in zip(scenes, slide_images) if not scene.skip]
        if len(active_images) != len(active_scenes):
            raise ValueError("Số ảnh slide phải bằng số cảnh đang xuất.")
    else:
        active_images = [make_slide(scene) for scene in active_scenes]

    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="storyboard_video_") as tmp:
        tmp_dir = Path(tmp)
        avatar_source_path: Path | None = None
        if avatar_image is not None and ai_avatar_config is not None:
            avatar_source_path = tmp_dir / "avatar_source.png"
            avatar_image.convert("RGB").save(avatar_source_path)
        segment_paths: list[Path] = []
        durations: list[float] = []
        subtitle_audio_durations: list[float | None] = []

        active_local_videos = [video for scene, video in zip(scenes, local_avatar_videos or [None] * len(scenes)) if not scene.skip] if local_avatar_videos is not None else [None] * len(active_scenes)
        active_audio_assets = [asset for scene, asset in zip(scenes, scene_audio_assets or [None] * len(scenes)) if not scene.skip] if scene_audio_assets is not None else [None] * len(active_scenes)

        for index, (scene, image, local_video_bytes, uploaded_audio) in enumerate(zip(active_scenes, active_images, active_local_videos, active_audio_assets), start=1):
            audio_path: Path | None = None
            if with_voice:
                audio_path = synthesize_scene_audio(
                    scene,
                    tmp_dir / f"slide_{index:03}.mp3",
                    voice_engine=voice_engine,
                    voice_id=voice_id,
                    voice_rate=voice_rate,
                    vieneu_style=vieneu_style,
                    voice_region=voice_region,
                    voice_clone_config=voice_clone_config,
                    uploaded_audio=uploaded_audio,
                )

            audio_duration = _media_duration(audio_path) if audio_path else None
            duration = max(2.2, (audio_duration or len(scene.narration) / 13.0 or 4.0) + scene.pause_after + pause_after)
            durations.append(duration)
            subtitle_audio_durations.append(audio_duration)

            ai_avatar_video_path: Path | None = None
            if local_video_bytes:
                ai_avatar_video_path = tmp_dir / f"avatar_local_{index:03}.mp4"
                ai_avatar_video_path.write_bytes(local_video_bytes)
            elif ai_avatar_config is not None and avatar_source_path is not None and audio_path is not None:
                ai_avatar_video_path = generate_talking_head(
                    ai_avatar_config,
                    avatar_source_path,
                    audio_path,
                    tmp_dir / f"avatar_{index:03}.mp4",
                )

            silent_segment = tmp_dir / f"segment_{index:03}_silent.mp4"
            _write_segment_video(
                image,
                silent_segment,
                duration,
                fps=fps,
                scene=scene,
                burn_subtitles=burn_subtitles,
                subtitle_position=subtitle_position,
                subtitle_font_size=subtitle_font_size,
                subtitle_max_lines=subtitle_max_lines,
                subtitle_audio_duration=audio_duration,
                subtitle_progressive=subtitle_progressive,
                subtitle_background_color=subtitle_background_color,
                subtitle_text_color=subtitle_text_color,
                subtitle_box_width_percent=subtitle_box_width_percent,
                subtitle_alignment=subtitle_alignment,
                preserve_original_slide=preserve_original_slide,
                avatar_image=avatar_image,
                avatar_position=avatar_position,
                avatar_size_percent=avatar_size_percent,
                avatar_shape=avatar_shape,
                avatar_border_width=avatar_border_width,
                avatar_talking_effect=avatar_talking_effect,
                ai_avatar_video_path=ai_avatar_video_path,
            )

            if audio_path:
                segment_path = tmp_dir / f"segment_{index:03}.mp4"
                ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
                subprocess.run(
                    [
                        ffmpeg,
                        "-y",
                        "-i",
                        str(silent_segment),
                        "-i",
                        str(audio_path),
                        "-c:v",
                        "copy",
                        "-c:a",
                        "aac",
                        "-shortest",
                        str(segment_path),
                    ],
                    check=True,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            else:
                segment_path = silent_segment
            segment_paths.append(segment_path)

        srt_text = build_srt(
            active_scenes,
            durations,
            audio_durations=subtitle_audio_durations,
            progressive=subtitle_progressive,
        )
        written_srt_path = Path(srt_path) if srt_path else None
        if written_srt_path:
            written_srt_path.parent.mkdir(parents=True, exist_ok=True)
            written_srt_path.write_text(srt_text, encoding="utf-8")

        _concat_videos(segment_paths, output_path)

    return output_path, written_srt_path, srt_text


def export_report_video(
    scenes: Sequence[VideoScene],
    output_path: str | Path,
    *,
    fps: int = 24,
    with_voice: bool = True,
    voice_engine: str = "edge",
    voice_id: str = "vi-VN-HoaiMyNeural",
    voice_rate: str = "+0%",
    slide_images: Sequence[Image.Image] | None = None,
) -> Path:
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="dashboard_video_") as tmp:
        tmp_dir = Path(tmp)
        silent_path = tmp_dir / "silent.mp4"
        if with_voice and voice_engine == "edge":
            narration_path = synthesize_edge_tts_audio(
                scenes,
                tmp_dir / "narration.mp3",
                voice=voice_id,
                rate=voice_rate,
            )
        elif with_voice:
            narration_path = synthesize_narration_wav(
                scenes,
                tmp_dir / "narration.wav",
                voice_hint=voice_id or "Vietnam",
            )
        else:
            narration_path = None

        audio_seconds = _media_duration(narration_path) if narration_path else None
        durations = _scene_durations(scenes, audio_seconds)
        if slide_images:
            slides = [slide.convert("RGB").resize((1280, 720)) for slide in slide_images]
            if len(slides) != len(scenes):
                raise ValueError("Số ảnh slide phải bằng số cảnh thuyết trình.")
        else:
            slides = [make_slide(scene) for scene in scenes]
        export_video(slides, silent_path, durations=durations, fps=fps)

        if narration_path:
            ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
            command = [
                ffmpeg,
                "-y",
                "-i",
                str(silent_path),
                "-i",
                str(narration_path),
                "-c:v",
                "copy",
                "-c:a",
                "aac",
                "-shortest",
                str(output_path),
            ]
            subprocess.run(command, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        else:
            shutil.copyfile(silent_path, output_path)

    return output_path


if __name__ == "__main__":
    demo_scenes = [
        VideoScene(
            "Buồng lái an toàn người bệnh",
            "Tổng quan nguy cơ theo khoa",
            metrics=(("Ca nặng", "12"), ("Nằm lâu", "37"), ("CLS chờ", "86")),
            bullets=("Ưu tiên hội chẩn ca nặng.", "Đóng các phiếu còn thiếu trước giao ban chiều."),
            narration="Buồng lái an toàn người bệnh. Hôm nay ưu tiên ca nặng, nằm lâu và chỉ định cận lâm sàng còn chờ.",
        ),
        VideoScene(
            "Dược và tồn kho",
            "Theo kho, nhóm thuốc và thuốc xuất cho bệnh nhân",
            metrics=(("Hết kho", "24"), ("Xuất tồn 0", "9"), ("Kho KSDB", "128")),
            bullets=("Tách phiếu luân chuyển khỏi cảnh báo thiếu.", "Rà kho Kiểm Soát Đặc Biệt theo hạn dùng."),
            narration="Dược và tồn kho cần quản trị theo từng kho và từng thuốc, đặc biệt các mặt hàng xuất xong tồn bằng không.",
        ),
    ]
    path = export_report_video(demo_scenes, "exports/dashboard_demo.mp4")
    print(f"Đã tạo video: {path}")
