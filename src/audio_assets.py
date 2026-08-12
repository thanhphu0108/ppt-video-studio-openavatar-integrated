from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class AudioAsset:
    """Bản thu lời đọc do người dùng tải lên cho một cảnh."""

    data: bytes
    filename: str = "narration.mp3"

    @property
    def suffix(self) -> str:
        suffix = Path(self.filename).suffix.lower()
        return suffix if suffix in {".mp3", ".wav", ".m4a", ".aac", ".ogg", ".flac"} else ".mp3"


def write_audio_asset(asset: AudioAsset, output_path: str | Path) -> Path | None:
    """Ghi bản thu với đúng phần mở rộng để FFmpeg nhận diện định dạng."""

    if not asset.data:
        return None
    target = Path(output_path).with_suffix(asset.suffix)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(asset.data)
    return target
