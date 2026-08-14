from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class SynthesisRequest(BaseModel):
    """JSON request accepted by the cloud-style compatibility endpoint."""

    model: str = "f5-tts"
    voice_id: str | None = None
    voice_region: Literal["auto", "nam", "bac", "trung"] = "auto"
    text: str
    reference_text: str | None = None
    # The existing PPT app calls this field `reference_transcript`; keep it
    # alongside the documented name instead of forcing callers to migrate.
    reference_transcript: str | None = None
    language: str = "vi"
    speed: float = Field(default=1.0, ge=0.5, le=2.0)
    output_format: Literal["wav", "mp3"] = "wav"
    return_audio: bool = False

    @property
    def effective_reference_text(self) -> str | None:
        return self.reference_text if self.reference_text is not None else self.reference_transcript


class StoryboardSlide(BaseModel):
    slide: int = Field(ge=1)
    text: str
    pause_after: float = Field(default=0.0, ge=0.0, le=30.0)


class StoryboardSynthesisRequest(BaseModel):
    model: str = "f5-tts"
    voice_id: str = "default"
    voice_style: str = "tu_nhien"
    voice_region: Literal["auto", "nam", "bac", "trung"] = "auto"
    reference_text: str | None = None
    language: str = "vi"
    speed: float = Field(default=1.0, ge=0.5, le=2.0)
    output_format: Literal["wav", "mp3"] = "wav"
    continue_on_error: bool = False
    slides: list[StoryboardSlide]
