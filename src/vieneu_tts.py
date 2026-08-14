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
import sys
import threading
import unicodedata
from pathlib import Path
from typing import Any


DEFAULT_STYLE = "tu_nhien"
SUPPORTED_STYLES = {
    "tu_nhien": "Tự nhiên",
    "tin_tuc": "Tin tức",
    "doc_truyen": "Đọc truyện",
}

# VieNeu v3 Turbo does not expose a first-class dialect argument.  Its
# reference codes do, however, carry regional/prosodic information.  For a
# cloned voice we combine the user's speaker embedding with the reference
# codes of a natural regional preset.  The embedding keeps the user's timbre;
# the regional prompt nudges pronunciation/prosody toward the selected area.
DEFAULT_VOICE_REGION = "auto"
SUPPORTED_VOICE_REGIONS = {
    "auto": "Tự động — giữ vùng theo file mẫu",
    "nam": "Miền Nam",
    "bac": "Miền Bắc",
    "trung": "Miền Trung",
}
REGIONAL_PROMPT_VOICES = {
    "nam": "Xuân Vĩnh",
    "bac": "Phạm Tuyên",
    "trung": "Quang Sơn",
}


class VieNeuTTSError(RuntimeError):
    """Base error raised by the optional VieNeu adapter."""


class VieNeuUnavailableError(VieNeuTTSError):
    """Raised when the optional ``vieneu`` package is not installed."""


_ENGINE_LOCK = threading.RLock()
_ENGINE_CACHE: dict[str, Any] = {}
_AUDIO_RUNTIME_CONFIGURED = False
_DLL_DIRECTORY_HANDLES: list[Any] = []


def normalize_voice_region(region: str | None) -> str:
    """Normalize UI/API region values to ``auto``, ``nam``, ``bac`` or ``trung``."""

    raw = str(region or "").strip().lower()
    decomposed = unicodedata.normalize("NFD", raw)
    plain = "".join(char for char in decomposed if unicodedata.category(char) != "Mn")
    plain = re.sub(r"[^a-z]+", " ", plain).strip()
    aliases = {
        "": "auto",
        "auto": "auto",
        "tu dong": "auto",
        "giu giong mau": "auto",
        "nam": "nam",
        "mien nam": "nam",
        "south": "nam",
        "bac": "bac",
        "mien bac": "bac",
        "north": "bac",
        "trung": "trung",
        "mien trung": "trung",
        "central": "trung",
    }
    return aliases.get(plain, "auto")


def regional_prompt_voice_id(region: str | None) -> str:
    """Return the built-in natural prompt used for a regional clone."""

    return REGIONAL_PROMPT_VOICES.get(normalize_voice_region(region), "")


def _configure_vieneu_audio_runtime() -> None:
    """Make reference-WAV loading reliable on Windows.

    TorchAudio 2.11 routes ``torchaudio.load`` through TorchCodec.  That is
    unnecessary for the normalized WAV reference used by this service and is
    fragile on Windows when FFmpeg DLL dependencies are not on the DLL search
    path.  Add the repository's full-shared FFmpeg directory and use
    soundfile for this one read operation; the model still uses PyTorch/CUDA
    for speaker extraction and synthesis.
    """

    global _AUDIO_RUNTIME_CONFIGURED
    if _AUDIO_RUNTIME_CONFIGURED:
        return
    _AUDIO_RUNTIME_CONFIGURED = True

    if sys.platform == "win32":
        ffmpeg_bin = (
            Path(__file__).resolve().parents[1]
            / "local_voice_clone"
            / "third_party"
            / "ffmpeg8-shared"
            / "ffmpeg-8.1.1-full_build-shared"
            / "bin"
        )
        if ffmpeg_bin.is_dir():
            current_path = os.environ.get("PATH", "")
            path_entry = str(ffmpeg_bin)
            if path_entry.lower() not in {
                entry.strip().lower() for entry in current_path.split(os.pathsep) if entry.strip()
            }:
                os.environ["PATH"] = path_entry + os.pathsep + current_path
            add_dll_directory = getattr(os, "add_dll_directory", None)
            if callable(add_dll_directory):
                try:
                    _DLL_DIRECTORY_HANDLES.append(add_dll_directory(path_entry))
                except OSError:
                    # PATH is still useful on Windows versions without a
                    # compatible add_dll_directory setup.
                    pass

    if os.getenv("VIENEU_USE_SOUNDFILE_REFERENCE", "true").strip().lower() not in {
        "1",
        "true",
        "yes",
        "on",
    }:
        return

    try:
        import soundfile as sf
        import torch
        import torchaudio
    except Exception:
        # VieNeu will report its normal dependency error if the optional audio
        # stack cannot be imported.  Do not make preset voice listing fail.
        return

    if getattr(torchaudio, "_vieneu_soundfile_loader_installed", False):
        return
    native_load = getattr(torchaudio, "load", None)
    if not callable(native_load):
        return

    def _load_with_soundfile(
        uri: Any,
        frame_offset: int = 0,
        num_frames: int = -1,
        normalize: bool = True,
        channels_first: bool = True,
        format: str | None = None,
        buffer_size: int = 4096,
        backend: str | None = None,
    ) -> tuple[Any, int]:
        del normalize, format, buffer_size, backend
        waveform, sample_rate = sf.read(
            uri,
            dtype="float32",
            always_2d=True,
        )
        start = max(0, int(frame_offset))
        end = None if int(num_frames) < 0 else start + max(0, int(num_frames))
        waveform = waveform[start:end]
        if channels_first:
            waveform = waveform.T
        return torch.from_numpy(waveform.copy()), int(sample_rate)

    torchaudio.load = _load_with_soundfile
    torchaudio._vieneu_soundfile_loader_installed = True


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
    _configure_vieneu_audio_runtime()
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


def _infer(
    engine: Any,
    text: str,
    voice: str,
    style: str,
    *,
    reference_audio: str | Path | None = None,
    reference_text: str | None = None,
    voice_override: dict[str, Any] | None = None,
) -> Any:
    infer = getattr(engine, "infer", None)
    if not callable(infer):
        raise VieNeuTTSError("VieNeu-TTS engine không có phương thức infer().")

    kwargs: dict[str, Any] = {}
    if voice_override is not None:
        kwargs["voice"] = voice_override
    elif voice.strip():
        kwargs["voice"] = voice.strip()

    # ``style`` is supported by older VieNeu releases.  Current v3 Turbo
    # accepts it for compatibility but may ignore it because style is encoded
    # in the preset voice.  Bind only when the installed signature exposes it.
    try:
        parameters = inspect.signature(infer).parameters
    except (TypeError, ValueError):
        parameters = {}
    accepts_kwargs = any(
        parameter.kind == inspect.Parameter.VAR_KEYWORD
        for parameter in parameters.values()
    )
    if style and ("style" in parameters or not parameters):
        kwargs["style"] = style
    if reference_audio is not None:
        if "ref_audio" in parameters or accepts_kwargs:
            kwargs["ref_audio"] = str(Path(reference_audio).resolve())
        else:
            raise VieNeuTTSError(
                "Phiên bản VieNeu-TTS hiện tại không hỗ trợ ref_audio để clone giọng. "
                "Hãy cập nhật gói vieneu."
            )
    if reference_text and "ref_text" in parameters:
        kwargs["ref_text"] = reference_text.strip()

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


def _build_regional_clone_voice(
    engine: Any,
    reference_audio: str | Path,
    region: str,
) -> dict[str, Any]:
    """Combine the uploaded speaker embedding with a regional prompt.

    This uses only public VieNeu v3 methods.  It intentionally fails with a
    clear message when an older/unsupported backend cannot provide the two
    pieces, instead of silently returning the model's default northern voice.
    """

    prompt_id = regional_prompt_voice_id(region)
    get_preset_voice = getattr(engine, "get_preset_voice", None)
    encode_reference = getattr(engine, "encode_reference", None)
    if not prompt_id or not callable(get_preset_voice) or not callable(encode_reference):
        raise VieNeuTTSError(
            "Backend VieNeu hiện tại không hỗ trợ định hướng vùng giọng khi clone. "
            "Hãy dùng backend PyTorch của VieNeu v3 Turbo."
        )

    try:
        prompt = get_preset_voice(prompt_id)
        speaker_embedding, _ = encode_reference(str(Path(reference_audio).resolve()))
    except Exception as exc:
        raise VieNeuTTSError(
            f"Không chuẩn bị được prompt giọng {SUPPORTED_VOICE_REGIONS[region]}: {exc}"
        ) from exc

    regional_codes = prompt.get("codes") if isinstance(prompt, dict) else None
    if regional_codes is None:
        raise VieNeuTTSError(
            f"Preset prompt {prompt_id} không có reference codes để định hướng vùng giọng."
        )
    return {
        "speaker_emb": speaker_embedding,
        "codes": regional_codes,
    }


def synthesize_vieneu_audio(
    text: str,
    output_path: str | Path,
    *,
    voice: str = "",
    style: str = DEFAULT_STYLE,
    backend: str | None = None,
    reference_audio: str | Path | None = None,
    reference_text: str | None = None,
    voice_region: str = DEFAULT_VOICE_REGION,
) -> Path | None:
    """Generate one local VieNeu WAV file, optionally cloning a reference."""

    narration = sanitize_vieneu_text(text)
    if not narration:
        return None

    target = Path(output_path).with_suffix(".wav")
    target.parent.mkdir(parents=True, exist_ok=True)
    selected_style = style if style in SUPPORTED_STYLES else DEFAULT_STYLE
    selected_region = normalize_voice_region(voice_region)

    with _ENGINE_LOCK:
        engine = get_vieneu_engine(backend=backend)
        regional_voice = None
        if reference_audio is not None and selected_region != DEFAULT_VOICE_REGION:
            regional_voice = _build_regional_clone_voice(
                engine,
                reference_audio,
                selected_region,
            )
        try:
            audio = _infer(
                engine,
                narration,
                "" if reference_audio is not None else voice,
                selected_style,
                reference_audio=(None if regional_voice is not None else reference_audio),
                reference_text=reference_text,
                voice_override=regional_voice,
            )
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
