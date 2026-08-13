from __future__ import annotations

import time
from pathlib import Path

import requests

from .errors import VoiceCloneServiceError


class LipSyncClient:
    """Adapter local cho OpenAvatar Runtime/Wav2Lip tại cổng cấu hình."""

    def __init__(self, endpoint: str, *, timeout_seconds: int = 1_800) -> None:
        self.endpoint = endpoint.rstrip("/")
        self.timeout_seconds = timeout_seconds

    def health(self) -> dict:
        try:
            response = requests.get(f"{self.endpoint}/health", timeout=10)
            response.raise_for_status()
            return response.json()
        except requests.RequestException as exc:
            raise VoiceCloneServiceError("WAV2LIP_UNAVAILABLE", f"Không kết nối được Wav2Lip/OpenAvatar: {exc}", status_code=503) from exc

    def synthesize_video(self, face_input: str | Path, audio_input: str | Path, output_path: str | Path, *, engine: str = "wav2lip") -> Path:
        face = Path(face_input)
        audio = Path(audio_input)
        target = Path(output_path)
        if not face.exists() or not audio.exists():
            raise VoiceCloneServiceError("WAV2LIP_INPUT_MISSING", "Thiếu ảnh/video mặt hoặc file audio.")
        try:
            with face.open("rb") as image_file, audio.open("rb") as audio_file:
                created = requests.post(
                    f"{self.endpoint}/avatar/generate",
                    files={
                        "image": (face.name, image_file, "application/octet-stream"),
                        "audio": (audio.name, audio_file, "audio/wav"),
                    },
                    data={"engine": engine},
                    timeout=60,
                )
            created.raise_for_status()
            job = created.json()
        except requests.RequestException as exc:
            raise VoiceCloneServiceError("WAV2LIP_UNAVAILABLE", f"Không tạo được Wav2Lip job: {exc}", status_code=503) from exc
        job_id = job.get("id")
        if not job_id:
            raise VoiceCloneServiceError("WAV2LIP_ERROR", "OpenAvatar Runtime không trả job id.", status_code=502)
        deadline = time.monotonic() + self.timeout_seconds
        while time.monotonic() < deadline:
            try:
                status_response = requests.get(f"{self.endpoint}/jobs/{job_id}", timeout=20)
                status_response.raise_for_status()
                status = status_response.json()
            except requests.RequestException as exc:
                raise VoiceCloneServiceError("WAV2LIP_UNAVAILABLE", f"Không đọc được trạng thái Wav2Lip: {exc}", status_code=503) from exc
            if status.get("status") == "completed":
                break
            if status.get("status") in {"failed", "cancelled"}:
                raise VoiceCloneServiceError("WAV2LIP_ERROR", status.get("error") or "Wav2Lip thất bại.", status_code=500)
            time.sleep(1)
        else:
            raise VoiceCloneServiceError("WAV2LIP_TIMEOUT", "Wav2Lip xử lý quá thời gian chờ.", status_code=504)
        try:
            download = requests.get(f"{self.endpoint}/jobs/{job_id}/download", timeout=120)
            download.raise_for_status()
        except requests.RequestException as exc:
            raise VoiceCloneServiceError("WAV2LIP_ERROR", f"Không tải được video Wav2Lip: {exc}", status_code=502) from exc
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(download.content)
        return target
