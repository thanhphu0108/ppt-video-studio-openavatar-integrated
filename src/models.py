from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class PronunciationEntry:
    source: str
    pronunciation: str
    category: str = "Khác"
    enabled: bool = True
    whole_word: bool = True
    case_sensitive: bool = False
    created_by: str = "user"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class BoundarySlideConfig:
    mode: str = "none"  # none | source_slide | system_default | uploaded_image
    source_slide_number: int | None = None
    remove_from_original_position: bool = True
    title: str = ""
    subtitle: str = ""
    narration: str = ""


@dataclass
class ProjectSettings:
    project_name: str = ""
    organization_name: str = ""
    presenter_name: str = ""
    voice_id: str = "vi-VN-HoaiMyNeural"
    voice_rate: str = "+0%"
    burn_subtitles: bool = True
    subtitle_position: str = "Dưới"
    subtitle_font_size: int = 28
    fps: int = 15
    intro: BoundarySlideConfig = field(default_factory=BoundarySlideConfig)
    outro: BoundarySlideConfig = field(default_factory=BoundarySlideConfig)
