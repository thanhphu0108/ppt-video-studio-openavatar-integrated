from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from fastapi import HTTPException, Request
from starlette.datastructures import UploadFile


@dataclass(frozen=True)
class CompatibilitySynthesisInput:
    """Normalized input for JSON and multipart callers.

    This deliberately accepts the field names currently used by the PPT
    application (`reference_transcript`, `api_key`) as well as the documented
    local API names.  `api_key` is consumed by authentication and never logged.
    """

    model: str
    voice_id: str | None
    text: str
    reference_text: str | None
    language: str
    speed: float
    output_format: str
    reference_audio: UploadFile | None
    api_key: str
    return_audio: bool
    voice_use_consent: bool


def _string(value: Any, default: str = "") -> str:
    if value is None:
        return default
    return str(value).strip()


def _bool(value: Any) -> bool:
    return _string(value).lower() in {"1", "true", "yes", "on", "file", "audio"}


def _speed(value: Any) -> float:
    try:
        speed = float(value if value is not None else 1.0)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail="speed phải là số.") from exc
    if not 0.5 <= speed <= 2.0:
        raise HTTPException(status_code=422, detail="speed phải trong khoảng 0.5–2.0.")
    return speed


async def parse_compatibility_request(request: Request) -> CompatibilitySynthesisInput:
    content_type = request.headers.get("content-type", "").lower()
    upload: UploadFile | None = None
    if "application/json" in content_type:
        try:
            payload = await request.json()
        except ValueError as exc:
            raise HTTPException(status_code=422, detail="JSON không hợp lệ.") from exc
        if not isinstance(payload, dict):
            raise HTTPException(status_code=422, detail="JSON body phải là object.")
        values: dict[str, Any] = payload
    elif "multipart/form-data" in content_type or "application/x-www-form-urlencoded" in content_type:
        form = await request.form()
        values = dict(form)
        candidate = form.get("reference_audio")
        if isinstance(candidate, UploadFile):
            upload = candidate
    else:
        raise HTTPException(status_code=415, detail="Chỉ hỗ trợ application/json hoặc multipart/form-data.")

    output_format = _string(values.get("output_format"), "wav").lower().lstrip(".")
    if output_format not in {"wav", "mp3"}:
        raise HTTPException(status_code=422, detail="output_format chỉ có thể là wav hoặc mp3.")
    return CompatibilitySynthesisInput(
        model=_string(values.get("model"), "f5-tts"),
        voice_id=_string(values.get("voice_id")) or None,
        text=_string(values.get("text")),
        reference_text=_string(values.get("reference_text"))
        or _string(values.get("reference_transcript"))
        or None,
        language=_string(values.get("language"), "vi"),
        speed=_speed(values.get("speed")),
        output_format=output_format,
        reference_audio=upload,
        api_key=_string(values.get("api_key")),
        return_audio=_bool(values.get("return_audio")) or _string(values.get("response_mode")).lower() == "file",
        voice_use_consent=_bool(values.get("voice_use_consent")) or _bool(values.get("consent")),
    )
