from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import numpy as np
import soundfile as sf

from config.settings import Settings
from .errors import VoiceCloneServiceError


SUPPORTED_REFERENCE_FORMATS = {".wav", ".mp3", ".m4a", ".aac", ".ogg", ".flac"}
SUPPORTED_OUTPUT_FORMATS = {"wav", "mp3"}


@dataclass(frozen=True)
class AudioInspection:
    path: Path
    duration_seconds: float
    sample_rate: int
    channels: int
    peak: float
    silence_ratio: float
    size_bytes: int


def _as_mono(samples: np.ndarray) -> np.ndarray:
    if samples.ndim == 1:
        return samples.astype(np.float32)
    return samples.mean(axis=1, dtype=np.float32)


def _resample(samples: np.ndarray, source_rate: int, target_rate: int) -> np.ndarray:
    if source_rate == target_rate or len(samples) <= 1:
        return samples.astype(np.float32)
    source_index = np.linspace(0.0, 1.0, num=len(samples), endpoint=False)
    target_length = max(1, round(len(samples) * target_rate / source_rate))
    target_index = np.linspace(0.0, 1.0, num=target_length, endpoint=False)
    return np.interp(target_index, source_index, samples).astype(np.float32)


def _trim_silence(samples: np.ndarray, threshold: float = 0.003, keep_samples: int = 2_400) -> np.ndarray:
    active = np.flatnonzero(np.abs(samples) >= threshold)
    if not len(active):
        return samples
    start = max(0, int(active[0]) - keep_samples)
    end = min(len(samples), int(active[-1]) + keep_samples + 1)
    return samples[start:end]


class AudioService:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def inspect(self, path: str | Path) -> AudioInspection:
        source = Path(path)
        try:
            samples, sample_rate = sf.read(source, always_2d=True, dtype="float32")
        except Exception as exc:
            raise VoiceCloneServiceError(
                "REFERENCE_AUDIO_INVALID",
                f"Không đọc được audio: {source.name} ({exc})",
            ) from exc
        if samples.size == 0 or sample_rate <= 0:
            raise VoiceCloneServiceError("REFERENCE_AUDIO_INVALID", "Audio rỗng hoặc sample rate không hợp lệ.")
        mono = _as_mono(samples)
        peak = float(np.max(np.abs(mono))) if len(mono) else 0.0
        silence_ratio = float(np.mean(np.abs(mono) < 0.003)) if len(mono) else 1.0
        return AudioInspection(
            path=source,
            duration_seconds=len(mono) / float(sample_rate),
            sample_rate=int(sample_rate),
            channels=int(samples.shape[1]),
            peak=peak,
            silence_ratio=silence_ratio,
            size_bytes=source.stat().st_size,
        )

    def _require_ffmpeg(self) -> str:
        ffmpeg = self.settings.ffmpeg
        if not ffmpeg:
            raise VoiceCloneServiceError(
                "FFMPEG_NOT_FOUND",
                "Không tìm thấy FFmpeg trong PATH. Cài FFmpeg rồi mở lại service.",
                status_code=503,
            )
        return ffmpeg

    def _decode_with_ffmpeg(self, source: Path, target: Path) -> None:
        ffmpeg = self._require_ffmpeg()
        target.parent.mkdir(parents=True, exist_ok=True)
        command = [
            ffmpeg,
            "-y",
            "-i",
            str(source),
            "-vn",
            "-t",
            str(self.settings.max_reference_seconds),
            "-ac",
            "1",
            "-ar",
            str(self.settings.reference_sample_rate),
            "-c:a",
            "pcm_s16le",
            str(target),
        ]
        completed = subprocess.run(
            command,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        if completed.returncode != 0 or not target.exists():
            raise VoiceCloneServiceError(
                "REFERENCE_AUDIO_INVALID",
                f"FFmpeg không chuyển đổi được audio mẫu: {(completed.stderr or '')[-800:]}",
            )

    def preprocess_reference(self, source: str | Path, target: str | Path) -> AudioInspection:
        source_path = Path(source)
        target_path = Path(target)
        if not source_path.exists() or not source_path.is_file():
            raise VoiceCloneServiceError("REFERENCE_AUDIO_NOT_FOUND", "Không tìm thấy file giọng mẫu.")
        if source_path.suffix.lower() not in SUPPORTED_REFERENCE_FORMATS:
            allowed = ", ".join(sorted(SUPPORTED_REFERENCE_FORMATS))
            raise VoiceCloneServiceError("UNSUPPORTED_AUDIO_FORMAT", f"Chỉ hỗ trợ: {allowed}.")
        if source_path.stat().st_size == 0:
            raise VoiceCloneServiceError("REFERENCE_AUDIO_INVALID", "File giọng mẫu rỗng.")

        if source_path.suffix.lower() != ".wav":
            self._decode_with_ffmpeg(source_path, target_path)
        else:
            try:
                samples, source_rate = sf.read(source_path, always_2d=True, dtype="float32")
            except Exception as exc:
                raise VoiceCloneServiceError("REFERENCE_AUDIO_INVALID", f"Không đọc được WAV: {exc}") from exc
            mono = _as_mono(samples)
            mono = _resample(mono, int(source_rate), self.settings.reference_sample_rate)
            target_path.parent.mkdir(parents=True, exist_ok=True)
            sf.write(target_path, mono, self.settings.reference_sample_rate, subtype="PCM_16")

        samples, sample_rate = sf.read(target_path, dtype="float32")
        mono = _as_mono(np.asarray(samples))
        mono = _trim_silence(mono, keep_samples=max(1, sample_rate // 10))
        if not len(mono) or float(np.max(np.abs(mono))) < 0.001:
            raise VoiceCloneServiceError("REFERENCE_AUDIO_INVALID", "Giọng mẫu gần như im lặng.")
        max_samples = int(self.settings.max_reference_seconds * sample_rate)
        mono = mono[:max_samples]
        peak = float(np.max(np.abs(mono)))
        if peak > 0:
            mono = np.clip(mono * min(0.85 / peak, 4.0), -0.99, 0.99)
        sf.write(target_path, mono, sample_rate, subtype="PCM_16")
        inspection = self.inspect(target_path)
        if inspection.duration_seconds < self.settings.min_reference_seconds:
            raise VoiceCloneServiceError(
                "REFERENCE_AUDIO_INVALID",
                f"Giọng mẫu phải dài ít nhất {self.settings.min_reference_seconds:g} giây.",
            )
        return inspection

    def concatenate_wavs(
        self,
        paths: Iterable[str | Path],
        output_path: str | Path,
        *,
        sentence_pause_ms: int,
        paragraph_pause_ms: int,
        paragraph_after: Iterable[bool],
    ) -> Path:
        source_paths = [Path(path) for path in paths]
        markers = list(paragraph_after)
        if not source_paths:
            raise VoiceCloneServiceError("MODEL_INFERENCE_ERROR", "Không có chunk audio để ghép.", status_code=500)
        rendered: list[np.ndarray] = []
        target_rate = self.settings.output_sample_rate
        for index, source in enumerate(source_paths):
            samples, source_rate = sf.read(source, dtype="float32")
            mono = _resample(_as_mono(np.asarray(samples)), int(source_rate), target_rate)
            rendered.append(mono)
            if index < len(source_paths) - 1:
                pause_ms = paragraph_pause_ms if markers[index] else sentence_pause_ms
                if pause_ms:
                    rendered.append(np.zeros(round(target_rate * pause_ms / 1_000), dtype=np.float32))
        combined = np.concatenate(rendered)
        target = Path(output_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        sf.write(target, combined, target_rate, subtype="PCM_16")
        return target

    def convert_to_mp3(self, wav_path: str | Path, output_path: str | Path) -> Path:
        ffmpeg = self._require_ffmpeg()
        source = Path(wav_path)
        target = Path(output_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        completed = subprocess.run(
            [
                ffmpeg,
                "-y",
                "-i",
                str(source),
                "-codec:a",
                "libmp3lame",
                "-b:a",
                "192k",
                str(target),
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        if completed.returncode != 0 or not target.exists() or target.stat().st_size == 0:
            raise VoiceCloneServiceError(
                "MODEL_INFERENCE_ERROR",
                f"Không chuyển được WAV sang MP3: {(completed.stderr or '')[-800:]}",
                status_code=500,
            )
        return target

    def quality_warnings(self, inspection: AudioInspection) -> list[str]:
        warnings: list[str] = []
        if inspection.duration_seconds < 0.25:
            warnings.append("SYNTHESIS_QUALITY_WARNING: audio quá ngắn.")
        if inspection.peak >= 0.995:
            warnings.append("SYNTHESIS_QUALITY_WARNING: audio có nguy cơ clipping.")
        if inspection.silence_ratio > 0.80:
            warnings.append("SYNTHESIS_QUALITY_WARNING: tỉ lệ im lặng cao.")
        if inspection.size_bytes < 512:
            warnings.append("SYNTHESIS_QUALITY_WARNING: file audio quá nhỏ.")
        return warnings
