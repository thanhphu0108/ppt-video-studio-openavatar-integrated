"""Optional local adapter for VieNeu-TTS.

VieNeu is intentionally loaded lazily.  The main Streamlit app can therefore
still start on machines that only have the lightweight Edge-TTS dependencies
installed, while local users can opt into the on-device VieNeu engine.
"""

from __future__ import annotations

import importlib
import importlib.util
import inspect
import os
import re
import threading
from pathlib import Path
from typing import Any


DEFAULT_STYLE = "tu_nhien"
SUPPORTED_STYLES = {
    "tu_nhien": "Tự nhiên",
    "tin_tuc": "Tin tức",
    "doc_truyen": "Đọc truyện",
}


class VieNeuTTSError(RuntimeError):
    """Base error raised by the optional VieNeu adapter."""


class VieNeuUnavailableError(VieNeuTTSError):
    """Raised when the optional ``vieneu`` package is not installed."""


_ENGINE_LOCK = threading.RLock()
_ENGINE_CACHE: dict[str, Any] = {}


def _backend_from_environment() -> str:
    backend = os.getenv("VIENEU_BACKEND", "").strip().lower()
    if backend in {"auto", "onnx", "pytorch"}:
        return backend
    return ""


def vieneu_install_hint() -> str:
    """Return the recommended local install command for this project."""

    return (
        "Local Voice Clone (cổng 8009): "
        r".\local_voice_clone\.venv\Scripts\python.exe -m pip install vieneu"
        "; nếu chạy VieNeu trực tiếp trong Streamlit thì dùng "
        "python -m pip install vieneu."
    )


def vieneu_available() -> bool:
    """Return whether the SDK can be found without importing its model."""

    try:
        return importlib.util.find_spec("vieneu") is not None
    except (ImportError, ModuleNotFoundError, ValueError):
        return False


def _vieneu_class() -> Any:
    try:
        module = importlib.import_module("vieneu")
    except (ImportError, ModuleNotFoundError) as exc:
        raise VieNeuUnavailableError(
            "Chưa cài VieNeu-TTS trong môi trường đang gọi service. "
            f"{vieneu_install_hint()}"
        ) from exc

    vieneu_class = getattr(module, "Vieneu", None)
    if vieneu_class is None:
        raise VieNeuUnavailableError(
            "Gói vieneu đã cài nhưng không có lớp Vieneu tương thích. "
            "Hãy cập nhật bằng `python -m pip install -U vieneu`."
        )
    return vieneu_class


def get_vieneu_engine(*, backend: str | None = None) -> Any:
    """Return one process-local, lazily initialized VieNeu engine.

    Streamlit reruns the script for every widget interaction.  Reusing this
    object avoids reloading the model for each rerun and also serializes model
    access in :func:`synthesize_vieneu_audio`.
    """

    requested_backend = (backend or _backend_from_environment()).strip().lower()
    if requested_backend not in {"", "auto", "onnx", "pytorch"}:
        raise ValueError("VIENEU_BACKEND phải là auto, onnx hoặc pytorch.")
    cache_key = requested_backend or "auto"

    with _ENGINE_LOCK:
        if cache_key in _ENGINE_CACHE:
            return _ENGINE_CACHE[cache_key]

        vieneu_class = _vieneu_class()
        kwargs: dict[str, Any] = {}
        if requested_backend and requested_backend != "auto":
            kwargs["backend"] = requested_backend

        try:
            engine = vieneu_class(**kwargs)
        except TypeError:
            # Older VieNeu releases did not expose ``backend``.  Falling back
            # to their default keeps the integration compatible without
            # hiding real model initialization errors.
            if kwargs:
                engine = vieneu_class()
            else:
                raise
        _ENGINE_CACHE[cache_key] = engine
        return engine


def _normalize_voice_entry(entry: Any) -> tuple[str, str] | None:
    """Normalize the SDK's ``(label, voice_id)`` voice specification."""

    if isinstance(entry, dict):
        voice_id = entry.get("voice_id") or entry.get("id") or entry.get("name")
        label = entry.get("label") or entry.get("description") or voice_id
    elif isinstance(entry, (tuple, list)) and len(entry) >= 2:
        label, voice_id = entry[0], entry[1]
    else:
        label = voice_id = entry

    label = str(label or "").strip()
    voice_id = str(voice_id or "").strip()
    if not voice_id:
        return None
    return label or voice_id, voice_id


def list_vieneu_voices(*, backend: str | None = None) -> list[tuple[str, str]]:
    """List built-in voices as ``[(display_label, voice_id), ...]``."""

    engine = get_vieneu_engine(backend=backend)
    list_fn = getattr(engine, "list_preset_voices", None)
    if not callable(list_fn):
        raise VieNeuTTSError(
            "Phiên bản VieNeu-TTS hiện tại không hỗ trợ list_preset_voices()."
        )

    result: list[tuple[str, str]] = []
    seen: set[str] = set()
    for entry in list_fn() or []:
        normalized = _normalize_voice_entry(entry)
        if normalized is None or normalized[1] in seen:
            continue
        result.append(normalized)
        seen.add(normalized[1])
    return result


def sanitize_vieneu_text(text: str, max_chars: int = 3200) -> str:
    """Keep URLs/control characters from reaching the speech model."""

    text = re.sub(
        r"https?://\S+|www\.\S+",
        " liên kết tham khảo ",
        str(text or ""),
        flags=re.IGNORECASE,
    )
    text = text.replace("\u200b", " ").replace("\ufeff", " ").replace("\x00", " ")
    text = re.sub(r"[\r\t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r"[^\S\n]+", " ", text).strip()
    if len(text) > max_chars:
        text = text[:max_chars].rsplit(" ", 1)[0].strip() + "."
    return text


def _infer(engine: Any, text: str, voice: str, style: str) -> Any:
    infer = getattr(engine, "infer", None)
    if not callable(infer):
        raise VieNeuTTSError("VieNeu-TTS engine không có phương thức infer().")

    kwargs: dict[str, Any] = {}
    if voice.strip():
        kwargs["voice"] = voice.strip()

    # ``style`` is supported by older VieNeu releases.  Current v3 Turbo
    # accepts it for compatibility but may ignore it because style is encoded
    # in the preset voice.  Bind only when the installed signature exposes it.
    try:
        parameters = inspect.signature(infer).parameters
    except (TypeError, ValueError):
        parameters = {}
    if style and ("style" in parameters or not parameters):
        kwargs["style"] = style

    try:
        return infer(text=text, **kwargs)
    except TypeError as exc:
        # A few early package builds used a positional text argument or did not
        # yet expose ``style``.  Retry narrowly for those compatibility cases.
        if "style" in kwargs and "style" in str(exc).lower():
            kwargs.pop("style", None)
            return infer(text, **kwargs)
        if "text" in str(exc).lower() and "unexpected keyword" in str(exc).lower():
            return infer(text, **kwargs)
        raise


def synthesize_vieneu_audio(
    text: str,
    output_path: str | Path,
    *,
    voice: str = "",
    style: str = DEFAULT_STYLE,
    backend: str | None = None,
) -> Path | None:
    """Generate one local VieNeu WAV file and return its path."""

    narration = sanitize_vieneu_text(text)
    if not narration:
        return None

    target = Path(output_path).with_suffix(".wav")
    target.parent.mkdir(parents=True, exist_ok=True)
    selected_style = style if style in SUPPORTED_STYLES else DEFAULT_STYLE

    with _ENGINE_LOCK:
        engine = get_vieneu_engine(backend=backend)
        try:
            audio = _infer(engine, narration, voice, selected_style)
            save = getattr(engine, "save", None)
            if not callable(save):
                raise VieNeuTTSError("VieNeu-TTS engine không có phương thức save().")
            save(audio, str(target))
        except VieNeuTTSError:
            raise
        except Exception as exc:
            raise VieNeuTTSError(f"VieNeu-TTS không tạo được audio: {exc}") from exc

    if not target.exists() or target.stat().st_size == 0:
        raise VieNeuTTSError("VieNeu-TTS không tạo được file WAV hợp lệ.")
    return target


def clear_vieneu_engine_cache() -> None:
    """Clear the process-local cache; primarily useful for tests and upgrades."""

    with _ENGINE_LOCK:
        _ENGINE_CACHE.clear()
