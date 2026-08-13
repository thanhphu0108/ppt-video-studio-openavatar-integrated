from __future__ import annotations

import base64
import json
import mimetypes
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

import requests


@dataclass(frozen=True)
class VoiceCloneConfig:
    """Kết nối tới dịch vụ nhân bản giọng do người dùng lựa chọn.

    Endpoint nhận multipart/form-data gồm `reference_audio`, `text`, `model`,
    `reference_transcript` và `output_format`; phản hồi audio thô hoặc JSON với
    trường `audio_base64`/`audio_url`.
    """

    endpoint: str
    reference_audio: bytes
    reference_filename: str
    model: str = "default"
    api_key: str = ""
    reference_transcript: str = ""
    voice_use_consent: bool = False
    upload_password: str = ""
    timeout_seconds: int = 300
    verify_ssl: bool = True


class VoiceCloneError(RuntimeError):
    pass


def is_loopback_voice_clone_endpoint(endpoint: str) -> bool:
    """True only for the local service; avoids sending a private password elsewhere."""

    host = (urlparse(str(endpoint or "").strip()).hostname or "").lower()
    return host in {"127.0.0.1", "localhost", "::1"}


def sanitize_voice_text(text: str, max_chars: int = 3200) -> str:
    text = re.sub(r"https?://\S+|www\.\S+", " liên kết tham khảo ", str(text or ""), flags=re.IGNORECASE)
    text = text.replace("\u200b", " ").replace("\ufeff", " ").replace("\x00", " ")
    text = re.sub(r"[\r\t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r"[^\S\n]+", " ", text).strip()
    if len(text) > max_chars:
        text = text[:max_chars].rsplit(" ", 1)[0].strip() + "."
    return text


def _response_audio(response: requests.Response, *, timeout_seconds: int, verify_ssl: bool) -> bytes:
    content_type = (response.headers.get("content-type") or "").lower()
    if "json" not in content_type:
        return response.content

    try:
        payload = response.json()
    except json.JSONDecodeError as exc:
        raise VoiceCloneError("Dịch vụ clone giọng trả về JSON không hợp lệ.") from exc
    if not isinstance(payload, dict):
        raise VoiceCloneError("Dịch vụ clone giọng không trả về audio hợp lệ.")
    if payload.get("audio_base64"):
        try:
            encoded = str(payload["audio_base64"])
            if encoded.startswith("data:") and "," in encoded:
                encoded = encoded.split(",", 1)[1]
            return base64.b64decode(encoded, validate=True)
        except (ValueError, TypeError) as exc:
            raise VoiceCloneError("audio_base64 từ dịch vụ clone giọng không hợp lệ.") from exc
    if payload.get("audio_url"):
        try:
            downloaded = requests.get(
                str(payload["audio_url"]),
                timeout=timeout_seconds,
                verify=verify_ssl,
            )
            downloaded.raise_for_status()
        except requests.RequestException as exc:
            raise VoiceCloneError(f"Không tải được audio từ dịch vụ clone giọng: {exc}") from exc
        return downloaded.content
    detail = payload.get("error") or payload.get("message") or "không có audio_base64 hoặc audio_url"
    raise VoiceCloneError(f"Dịch vụ clone giọng không tạo được audio: {detail}")


def synthesize_voice_clone_audio(
    text: str,
    output_path: str | Path,
    *,
    config: VoiceCloneConfig,
) -> Path | None:
    """Tạo audio từ giọng mẫu người dùng qua endpoint clone giọng đã cấu hình."""

    narration = sanitize_voice_text(text)
    if not narration:
        return None
    if not config.endpoint.strip():
        raise VoiceCloneError("Hãy nhập URL endpoint nhân bản giọng.")
    if not config.reference_audio:
        raise VoiceCloneError("Hãy tải tệp giọng mẫu để nhân bản.")
    if len(config.reference_audio) > 25 * 1024 * 1024:
        raise VoiceCloneError("Tệp giọng mẫu vượt quá giới hạn 25 MB.")

    output_path = Path(output_path)
    suffix = output_path.suffix.lower() or ".mp3"
    output_format = suffix.removeprefix(".")
    filename = config.reference_filename or f"voice_sample{suffix}"
    mime_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
    headers = {"Accept": "audio/*, application/json"}
    if config.api_key.strip():
        headers["Authorization"] = f"Bearer {config.api_key.strip()}"
    if config.upload_password and is_loopback_voice_clone_endpoint(config.endpoint):
        # Local service uses this separate header for protected reference voice
        # uploads. It is intentionally not an API key and is never logged.
        headers["X-Voice-Upload-Password"] = config.upload_password
    data = {
        "text": narration,
        "model": config.model.strip() or "default",
        "reference_transcript": config.reference_transcript.strip(),
        "output_format": output_format,
        "voice_use_consent": "true" if config.voice_use_consent else "false",
    }

    try:
        response = requests.post(
            config.endpoint.strip(),
            headers=headers,
            data=data,
            files={"reference_audio": (filename, config.reference_audio, mime_type)},
            timeout=config.timeout_seconds,
            verify=config.verify_ssl,
        )
        response.raise_for_status()
    except requests.RequestException as exc:
        raise VoiceCloneError(f"Không gọi được dịch vụ clone giọng: {exc}") from exc

    audio = _response_audio(
        response,
        timeout_seconds=config.timeout_seconds,
        verify_ssl=config.verify_ssl,
    )
    if not audio:
        raise VoiceCloneError("Dịch vụ clone giọng trả về audio rỗng.")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_bytes(audio)
    return output_path
