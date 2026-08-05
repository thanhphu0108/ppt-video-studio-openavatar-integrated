from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import requests


@dataclass(frozen=True)
class AvatarApiConfig:
    base_url: str
    api_key: str = ""
    engine: str = "wav2lip"
    timeout_seconds: int = 300
    verify_ssl: bool = True

    @property
    def normalized_base_url(self) -> str:
        return self.base_url.rstrip("/")


def _headers(config: AvatarApiConfig) -> dict[str, str]:
    headers: dict[str, str] = {}
    if config.api_key:
        headers["Authorization"] = f"Bearer {config.api_key}"
    return headers


def check_avatar_api(config: AvatarApiConfig) -> tuple[bool, str]:
    if not config.base_url.strip():
        return False, "Chưa nhập GPU API URL."
    try:
        response = requests.get(
            f"{config.normalized_base_url}/health",
            headers=_headers(config),
            timeout=min(config.timeout_seconds, 30),
            verify=config.verify_ssl,
        )
        if response.ok:
            try:
                payload = response.json()
                detail = payload.get("message") or payload.get("status") or "GPU API sẵn sàng"
            except ValueError:
                detail = response.text.strip() or "GPU API sẵn sàng"
            return True, str(detail)
        return False, f"HTTP {response.status_code}: {response.text[:240]}"
    except requests.RequestException as exc:
        return False, str(exc)


def _download_result(url: str, output_path: Path, config: AvatarApiConfig) -> Path:
    response = requests.get(
        url,
        headers=_headers(config),
        timeout=config.timeout_seconds,
        verify=config.verify_ssl,
    )
    response.raise_for_status()
    output_path.write_bytes(response.content)
    return output_path


def generate_talking_head(
    config: AvatarApiConfig,
    image_path: str | Path,
    audio_path: str | Path,
    output_path: str | Path,
    *,
    preview_seconds: float | None = None,
) -> Path:
    """Call a generic GPU talking-head API.

    Expected endpoint: POST /generate as multipart/form-data with fields:
    - image: portrait image
    - audio: narration audio
    - engine: wav2lip | sadtalker | musetalk | liveportrait
    - preview_seconds: optional

    Accepted responses:
    1. Direct MP4 bytes.
    2. JSON {"video_url": "..."}.
    3. JSON {"status_url": "..."}; polling endpoint later returns video_url.
    """
    image_path = Path(image_path)
    audio_path = Path(audio_path)
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    data: dict[str, Any] = {"engine": config.engine}
    if preview_seconds is not None:
        data["preview_seconds"] = str(preview_seconds)

    with image_path.open("rb") as image_file, audio_path.open("rb") as audio_file:
        files = {
            "image": (image_path.name, image_file, "application/octet-stream"),
            "audio": (audio_path.name, audio_file, "application/octet-stream"),
        }
        response = requests.post(
            f"{config.normalized_base_url}/generate",
            headers=_headers(config),
            data=data,
            files=files,
            timeout=config.timeout_seconds,
            verify=config.verify_ssl,
        )
    response.raise_for_status()

    content_type = (response.headers.get("content-type") or "").lower()
    if "video/" in content_type or content_type == "application/octet-stream":
        output_path.write_bytes(response.content)
        return output_path

    try:
        payload = response.json()
    except ValueError as exc:
        raise RuntimeError("GPU API không trả video hoặc JSON hợp lệ.") from exc

    video_url = payload.get("video_url") or payload.get("result_url")
    if video_url:
        return _download_result(str(video_url), output_path, config)

    status_url = payload.get("status_url")
    if not status_url:
        raise RuntimeError(f"GPU API thiếu video_url/status_url: {json.dumps(payload, ensure_ascii=False)[:400]}")

    deadline = time.time() + config.timeout_seconds
    while time.time() < deadline:
        status_response = requests.get(
            str(status_url),
            headers=_headers(config),
            timeout=min(30, config.timeout_seconds),
            verify=config.verify_ssl,
        )
        status_response.raise_for_status()
        status_payload = status_response.json()
        state = str(status_payload.get("status", "")).lower()
        video_url = status_payload.get("video_url") or status_payload.get("result_url")
        if video_url:
            return _download_result(str(video_url), output_path, config)
        if state in {"failed", "error", "cancelled"}:
            raise RuntimeError(str(status_payload.get("error") or "GPU render thất bại."))
        time.sleep(2)

    raise TimeoutError("GPU API render quá thời gian chờ.")
