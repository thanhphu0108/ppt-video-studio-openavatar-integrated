from __future__ import annotations

import base64
from pathlib import Path
from typing import Any

import streamlit.components.v1 as components

_COMPONENT = components.declare_component(
    "ppt_video_openavatar_bridge",
    path=str(Path(__file__).resolve().parent.parent / "local_gpu_component"),
)


def bytes_to_data_url(data: bytes, mime: str) -> str:
    return f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"


def decode_video_result(value: Any) -> bytes | None:
    if not isinstance(value, dict):
        return None
    encoded = value.get("video_base64")
    if not encoded:
        return None
    return base64.b64decode(encoded)


def decode_audio_result(value: Any) -> bytes | None:
    if not isinstance(value, dict):
        return None
    encoded = value.get("audio_base64")
    if not encoded:
        return None
    return base64.b64decode(encoded)


def local_gpu_bridge(
    *,
    action: str,
    agent_url: str = "http://127.0.0.1:8008",
    image_bytes: bytes | None = None,
    audio_bytes: bytes | None = None,
    reference_audio_bytes: bytes | None = None,
    reference_audio_filename: str = "reference.wav",
    text: str = "",
    reference_text: str = "",
    voice_id: str = "default",
    model: str = "f5-tts",
    api_key: str = "",
    engine: str = "wav2lip",
    preview_seconds: float | None = None,
    request_id: str = "default",
    key: str | None = None,
) -> Any:
    """Browser bridge for OpenAvatar (8008) and Local Voice Clone (8009)."""
    return _COMPONENT(
        action=action,
        agent_url=agent_url.rstrip("/"),
        image_data_url=bytes_to_data_url(image_bytes, "image/png") if image_bytes else "",
        audio_data_url=bytes_to_data_url(audio_bytes, "audio/mpeg") if audio_bytes else "",
        reference_audio_data_url=(
            bytes_to_data_url(reference_audio_bytes, "audio/wav")
            if reference_audio_bytes else ""
        ),
        reference_audio_filename=reference_audio_filename,
        text=text,
        reference_text=reference_text,
        voice_id=voice_id,
        model=model,
        api_key=api_key,
        engine=engine,
        preview_seconds=preview_seconds,
        request_id=request_id,
        key=key,
        default=None,
    )
