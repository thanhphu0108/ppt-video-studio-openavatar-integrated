from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from config.settings import Settings
from .errors import VoiceCloneServiceError


@dataclass(frozen=True)
class VoiceProfile:
    id: str
    engine: str
    reference_audio: Path
    reference_text: str | None
    language: str
    enabled: bool

    @property
    def available(self) -> bool:
        return self.enabled and self.reference_audio.exists() and self.reference_audio.stat().st_size > 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "engine": self.engine,
            "language": self.language,
            "enabled": self.enabled,
            "available": self.available,
            "reference_audio": str(self.reference_audio),
            "has_reference_text": bool(self.reference_text),
        }


class VoiceProfileService:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def _registry(self) -> dict[str, Any]:
        path = self.settings.voices_registry_path
        if not path.exists():
            return {}
        try:
            content = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise VoiceCloneServiceError("REFERENCE_AUDIO_INVALID", f"voices.json không hợp lệ: {exc}", status_code=500) from exc
        return content if isinstance(content, dict) else {}

    @staticmethod
    def _safe_id(voice_id: str) -> str:
        normalized = str(voice_id or "").strip()
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", normalized):
            raise VoiceCloneServiceError("REFERENCE_AUDIO_NOT_FOUND", "voice_id không hợp lệ.")
        return normalized

    def get(self, voice_id: str) -> VoiceProfile:
        safe_id = self._safe_id(voice_id)
        item = self._registry().get(safe_id)
        if not isinstance(item, dict):
            raise VoiceCloneServiceError("REFERENCE_AUDIO_NOT_FOUND", f"Không tìm thấy voice profile '{safe_id}'.")
        audio_value = str(item.get("reference_audio", "")).strip()
        if not audio_value:
            raise VoiceCloneServiceError("REFERENCE_AUDIO_NOT_FOUND", f"Profile '{safe_id}' chưa có reference_audio.")
        audio_path = Path(audio_value)
        if not audio_path.is_absolute():
            audio_path = self.settings.root / audio_path
        transcript = item.get("reference_text")
        text_file = str(item.get("reference_text_file", "")).strip()
        if not transcript and text_file:
            candidate = Path(text_file)
            if not candidate.is_absolute():
                candidate = self.settings.root / candidate
            if candidate.exists():
                transcript = candidate.read_text(encoding="utf-8").strip()
        return VoiceProfile(
            id=safe_id,
            engine=str(item.get("engine", self.settings.engine)),
            reference_audio=audio_path,
            reference_text=str(transcript).strip() if transcript else None,
            language=str(item.get("language", "vi")).strip() or "vi",
            enabled=bool(item.get("enabled", True)),
        )

    def list(self) -> list[dict[str, Any]]:
        profiles: list[dict[str, Any]] = []
        for voice_id in sorted(self._registry()):
            try:
                profiles.append(self.get(voice_id).to_dict())
            except VoiceCloneServiceError as exc:
                profiles.append({"id": voice_id, "available": False, "message": exc.message})
        return profiles
