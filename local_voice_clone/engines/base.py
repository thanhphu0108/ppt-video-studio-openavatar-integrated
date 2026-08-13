from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class EngineUnavailableError(RuntimeError):
    """Engine chưa cài dependency hoặc chưa tải được model."""


@dataclass(frozen=True)
class EngineStatus:
    id: str
    available: bool
    loaded: bool
    device: str
    message: str = ""
    model: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "available": self.available,
            "loaded": self.loaded,
            "device": self.device,
            "message": self.message,
            "model": self.model,
        }


class VoiceCloneEngine(ABC):
    """Contract để thay F5-TTS bằng OpenVoice/XTTS/Fish Speech sau này."""

    id: str = "base"

    @abstractmethod
    def load(self) -> None:
        """Load model đúng một lần cho process."""

    @abstractmethod
    def synthesize(
        self,
        text: str,
        reference_audio: str | Path,
        reference_text: str | None,
        output_path: str | Path,
        language: str = "vi",
        speed: float = 1.0,
    ) -> str:
        """Sinh WAV và trả về đường dẫn tuyệt đối."""

    @abstractmethod
    def status(self) -> EngineStatus:
        """Trạng thái không làm crash health check."""

    def model_info(self) -> dict[str, Any]:
        status = self.status()
        return {
            "id": status.id,
            "available": status.available,
            "loaded": status.loaded,
            "device": status.device,
            "message": status.message,
            "model": status.model,
        }
