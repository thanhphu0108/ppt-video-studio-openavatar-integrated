from __future__ import annotations

import io
import os
import json
import re
import tempfile
import hashlib
import time
import subprocess
import base64
import requests
import imageio_ffmpeg
from dataclasses import asdict
from pathlib import Path

import pandas as pd
import streamlit as st
from PIL import Image

from src.models import BoundarySlideConfig, PronunciationEntry
from src.moderation import ProfanityFilter
from src.ppt_reader import build_narration, read_pptx
from src.ppt_renderer import PowerPointRenderError, render_pptx_slides
from src.pronunciation import apply_dictionary, dictionary_json, load_dictionary, parse_dictionary_bytes, validate_entry
from src.slide_builder import build_boundary_slide
from src.storyboard_io import (
    apply_storyboard_updates,
    prepare_storyboard_updates,
    read_storyboard_upload,
    storyboard_csv_bytes,
    storyboard_xlsx_bytes,
)
from src.audio_assets import AudioAsset
from src.video_export import VideoScene, export_storyboard_video, synthesize_scene_audio
from src.avatar_api import AvatarApiConfig, check_avatar_api, generate_talking_head
from src.local_gpu_bridge import local_gpu_bridge, decode_video_result, decode_audio_result
from src.voice_clone import VoiceCloneConfig
from src.voice_access import verify_voice_upload_password
from src.vieneu_tts import (
    SUPPORTED_STYLES as VIENEU_STYLES,
    list_vieneu_voices,
    vieneu_available,
    vieneu_install_hint,
)

# OpenAvatar SDK chỉ dùng khi Streamlit chạy hoàn toàn trên máy local.
# Khi deploy trên Streamlit Cloud, phải dùng Browser Bridge vì Python server
# trên cloud không thể truy cập localhost của máy người dùng.
try:
    from openavatar_sdk import OpenAvatarClient
except ImportError:
    OpenAvatarClient = None

ROOT = Path(__file__).parent
LOCAL_PIPELINE_CACHE_DIR = ROOT / ".local_pipeline_cache"
LOCAL_AUDIO_CACHE_DIR = LOCAL_PIPELINE_CACHE_DIR / "audio"
LOCAL_AVATAR_CACHE_DIR = LOCAL_PIPELINE_CACHE_DIR / "avatar"
LOCAL_PIPELINE_MANIFEST = LOCAL_PIPELINE_CACHE_DIR / "manifest.json"
DEFAULT_DICTIONARY = ROOT / "config" / "pronunciation_default.json"
PROFANITY_CONFIG = ROOT / "config" / "profanity_vi.json"

st.set_page_config(page_title="PPT Video Studio", page_icon="🎬", layout="wide")
st.markdown("""
<style>
.block-container{padding-top:1.1rem;max-width:1500px}
[data-testid="stMetric"]{border:1px solid #e2e8f0;border-radius:12px;padding:12px;background:#fff}
.small-note{color:#64748b;font-size:.9rem}
</style>
""", unsafe_allow_html=True)


def init_state() -> None:
    if "dictionary" not in st.session_state:
        st.session_state.dictionary = load_dictionary(DEFAULT_DICTIONARY)
    if "records" not in st.session_state:
        st.session_state.records = []
    if "ppt_name" not in st.session_state:
        st.session_state.ppt_name = ""
    if "intro_upload" not in st.session_state:
        st.session_state.intro_upload = None
    if "outro_upload" not in st.session_state:
        st.session_state.outro_upload = None
    if "ppt_payload" not in st.session_state:
        st.session_state.ppt_payload = None
    if "original_slide_images" not in st.session_state:
        st.session_state.original_slide_images = []
    if "render_backend" not in st.session_state:
        st.session_state.render_backend = "Chưa render"
    if "render_warning" not in st.session_state:
        st.session_state.render_warning = ""
    if "avatar_upload" not in st.session_state:
        st.session_state.avatar_upload = None
    if "local_avatar_clips" not in st.session_state:
        st.session_state.local_avatar_clips = {}
    if "local_avatar_audio" not in st.session_state:
        st.session_state.local_avatar_audio = {}
    if "local_avatar_queue" not in st.session_state:
        st.session_state.local_avatar_queue = []
    if "local_voice_queue" not in st.session_state:
        st.session_state.local_voice_queue = []
    if "local_job_status" not in st.session_state:
        st.session_state.local_job_status = {}
    if "local_job_stage" not in st.session_state:
        st.session_state.local_job_stage = {}
    if "local_job_errors" not in st.session_state:
        st.session_state.local_job_errors = {}
    if "local_job_failed_stage" not in st.session_state:
        st.session_state.local_job_failed_stage = {}
    if "local_job_cache_source" not in st.session_state:
        st.session_state.local_job_cache_source = {}
    if "local_continue_avatar_targets" not in st.session_state:
        st.session_state.local_continue_avatar_targets = []
    if "local_audio_hashes" not in st.session_state:
        st.session_state.local_audio_hashes = {}
    if "local_avatar_hashes" not in st.session_state:
        st.session_state.local_avatar_hashes = {}
    if "local_batch_mode" not in st.session_state:
        st.session_state.local_batch_mode = False
    if "local_batch_auto_export" not in st.session_state:
        st.session_state.local_batch_auto_export = False
    if "auto_export_requested" not in st.session_state:
        st.session_state.auto_export_requested = False
    if "scene_audio_overrides" not in st.session_state:
        st.session_state.scene_audio_overrides = {}
    if "master_recording_payload" not in st.session_state:
        st.session_state.master_recording_payload = None
    if "master_recording_name" not in st.session_state:
        st.session_state.master_recording_name = ""
    if "master_recording_ranges" not in st.session_state:
        st.session_state.master_recording_ranges = {}


init_state()
profanity = ProfanityFilter(PROFANITY_CONFIG)


def check_runtime_from_python(base_url: str) -> tuple[bool, dict | str]:
    """Kiểm tra Runtime bằng Python SDK.

    Chỉ dùng khi app Streamlit chạy local trên cùng máy với OpenAvatar Runtime.
    Với Streamlit Cloud, dùng `local_gpu_bridge()` để request chạy trong browser.
    """
    if OpenAvatarClient is None:
        return False, (
            "Chưa cài openavatar-sdk. Cài bằng: "
            "`python -m pip install -e C:\\Phu\\openavatar-sdk\\python`"
        )
    try:
        client = OpenAvatarClient(base_url=base_url)
        return True, client.health()
    except Exception as exc:
        return False, str(exc)


def ensure_boundary_defaults(records: list[dict]) -> None:
    """Khởi tạo giá trị biểu mẫu boundary một lần để người dùng không mất lời thoại."""

    first_title = str(records[0].get("title", "")) if records else ""
    defaults = {
        "intro_title": first_title,
        "intro_subtitle": st.session_state.get("organization", ""),
        "intro_narration": f"Xin chào quý anh chị. Nội dung trình bày hôm nay là {first_title}.",
        "outro_title": "Trân trọng cảm ơn",
        "outro_subtitle": st.session_state.get("organization", ""),
        "outro_narration": "Nội dung trình bày xin được kết thúc tại đây. Trân trọng cảm ơn quý anh chị đã theo dõi.",
    }
    for key, value in defaults.items():
        if key not in st.session_state:
            st.session_state[key] = value


def reset_presentation_editing_state() -> None:
    """Bỏ dữ liệu gắn với PowerPoint cũ trước khi đọc một bài mới."""

    for key in (
        "intro_title",
        "intro_subtitle",
        "intro_narration",
        "outro_title",
        "outro_subtitle",
        "outro_narration",
        "intro_mode_label",
        "outro_mode_label",
        "intro_source_slide",
        "outro_source_slide",
        "remove_intro",
        "remove_outro",
        "intro_image",
        "outro_image",
        "storyboard_editor",
        "storyboard_import_file",
        "storyboard_import_notice",
        "recorded_voice_files",
        "voice_clone_reference",
        "voice_clone_endpoint",
        "voice_clone_model",
        "voice_clone_api_key",
        "voice_clone_transcript",
        "voice_clone_verify_ssl",
        "voice_clone_consent",
        "local_voice_upload_password",
        "vieneu_service_url",
        "vieneu_service_api_key",
        "vieneu_voice_id",
        "vieneu_voice_id_fallback",
        "vieneu_style",
    ):
        st.session_state.pop(key, None)
    st.session_state.intro_upload = None
    st.session_state.outro_upload = None
    st.session_state.local_avatar_clips = {}
    st.session_state.local_avatar_audio = {}
    st.session_state.local_avatar_queue = []
    st.session_state.local_voice_queue = []
    st.session_state.local_job_status = {}
    st.session_state.local_job_stage = {}
    st.session_state.local_job_errors = {}
    st.session_state.local_job_failed_stage = {}
    st.session_state.local_job_cache_source = {}
    st.session_state.local_continue_avatar_targets = []
    st.session_state.local_audio_hashes = {}
    st.session_state.local_avatar_hashes = {}
    st.session_state.local_batch_mode = False
    st.session_state.local_batch_auto_export = False
    st.session_state.auto_export_requested = False


def _require_original_ppt_slide(slide_number: int) -> Image.Image:
    """Return a real rendered PPT slide; never replace it with a text mockup."""

    images = st.session_state.get("original_slide_images", [])
    index = int(slide_number) - 1
    if index < 0 or index >= len(images):
        raise PowerPointRenderError(
            f"Chưa có ảnh render gốc cho slide {slide_number}. "
            "Hãy render lại PowerPoint trước khi xuất video."
        )
    return images[index].copy()


def configured_voice_upload_password() -> str | None:
    """Cho phép thay mật khẩu ngoài source qua environment hoặc Streamlit secrets."""

    from_environment = os.getenv("VOICE_UPLOAD_PASSWORD", "").strip()
    if from_environment:
        return from_environment
    try:
        from_secrets = str(st.secrets.get("VOICE_UPLOAD_PASSWORD", "")).strip()
    except Exception:
        from_secrets = ""
    return from_secrets or None


def configured_voice_clone_api_key() -> str:
    """Read the token used by the local Voice Clone service, if configured.

    ``start_f5_8009.bat`` names this token ``LOCAL_API_KEY`` while the
    Streamlit UI uses the less ambiguous ``VOICE_CLONE_API_KEY`` name.  The
    latter wins so a deployment can keep its own secret namespace.
    """

    for name in ("VOICE_CLONE_API_KEY", "LOCAL_API_KEY"):
        value = os.getenv(name, "").strip()
        if value:
            return value
    try:
        for name in ("VOICE_CLONE_API_KEY", "LOCAL_API_KEY"):
            value = str(st.secrets.get(name, "")).strip()
            if value:
                return value
    except Exception:
        pass
    return ""


def clear_voice_upload_session() -> None:
    """Khóa lại và bỏ các audio/mẫu giọng đang giữ trong session hiện tại."""

    for key in (
        "voice_upload_unlocked",
        "voice_upload_password",
        "recorded_voice_files",
        "voice_clone_reference",
        "voice_clone_endpoint",
        "voice_clone_model",
        "voice_clone_api_key",
        "voice_clone_transcript",
        "voice_clone_verify_ssl",
        "voice_clone_consent",
    ):
        st.session_state.pop(key, None)


def unlock_voice_uploads() -> bool:
    """Hiển thị cổng mật khẩu trước các chức năng có upload audio giọng."""

    if st.session_state.get("voice_upload_unlocked", False):
        unlocked_col, lock_col = st.columns([4, 1])
        unlocked_col.success("Đã mở khóa tính năng tải giọng cho phiên này.")
        if lock_col.button("Khóa lại", key="lock_voice_uploads", use_container_width=True):
            clear_voice_upload_session()
            st.rerun()
        return True

    password = st.text_input(
        "Mật khẩu để dùng giọng tải lên",
        type="password",
        key="voice_upload_password",
        help="Cần nhập mật khẩu trước khi tải bản thu thật hoặc mẫu giọng để nhân bản.",
    )
    if st.button("Mở khóa tải giọng", key="unlock_voice_uploads", use_container_width=True):
        if verify_voice_upload_password(
            password,
            configured_password=configured_voice_upload_password(),
        ):
            st.session_state.voice_upload_unlocked = True
            st.rerun()
        else:
            st.error("Mật khẩu không đúng.")
    return False


def uploaded_audio_assets(files: list[st.runtime.uploaded_file_manager.UploadedFile]) -> tuple[dict[int | str, AudioAsset], list[str]]:
    """Đọc bản thu theo quy ước slide_001.mp3, intro.mp3 hoặc outro.mp3."""

    assets: dict[int | str, AudioAsset] = {}
    errors: list[str] = []
    for uploaded_file in files:
        stem = Path(uploaded_file.name).stem.lower()
        normalized = re.sub(r"[^a-z0-9]+", "", stem)
        if normalized.startswith("intro") or normalized.startswith("modau"):
            slot: int | str = "intro"
        elif normalized.startswith("outro") or normalized.startswith("ketthuc"):
            slot = "outro"
        else:
            numbers = re.findall(r"\d+", stem)
            if not numbers:
                errors.append(
                    f"{uploaded_file.name}: đặt tên slide_001.mp3, intro.mp3 hoặc outro.mp3."
                )
                continue
            slot = int(numbers[-1])
        if slot in assets:
            errors.append(f"Có nhiều bản thu cùng cho {'slide ' if isinstance(slot, int) else ''}{slot}.")
            continue
        payload = uploaded_file.getvalue()
        if not payload:
            errors.append(f"{uploaded_file.name}: file audio rỗng.")
            continue
        assets[slot] = AudioAsset(data=payload, filename=uploaded_file.name)
    return assets, errors



def _parse_timecode(value: str) -> float:
    """Accept SS, MM:SS, MM:SS.mmm or HH:MM:SS.mmm and return seconds."""
    raw = str(value or "").strip().replace(",", ".")
    if not raw:
        raise ValueError("timecode trống")
    parts = raw.split(":")
    try:
        nums = [float(part) for part in parts]
    except ValueError as exc:
        raise ValueError(f"timecode không hợp lệ: {value}") from exc
    if len(nums) == 1:
        seconds = nums[0]
    elif len(nums) == 2:
        seconds = nums[0] * 60 + nums[1]
    elif len(nums) == 3:
        seconds = nums[0] * 3600 + nums[1] * 60 + nums[2]
    else:
        raise ValueError(f"timecode không hợp lệ: {value}")
    if seconds < 0:
        raise ValueError("timecode không được âm")
    return seconds


def _slice_master_audio(
    payload: bytes,
    filename: str,
    start_seconds: float,
    end_seconds: float,
    scene_key: int | str,
) -> AudioAsset:
    """Cut one scene from a long uploaded recording and normalize to WAV."""
    if not payload:
        raise ValueError("Chưa tải file thu âm tổng.")
    if end_seconds <= start_seconds:
        raise ValueError("Thời điểm kết thúc phải lớn hơn bắt đầu.")

    temp_dir = Path(tempfile.mkdtemp(prefix="ppt_audio_slice_"))
    suffix = Path(filename or "master_audio.wav").suffix or ".wav"
    source = temp_dir / f"master{suffix}"
    target = temp_dir / f"scene_{scene_key}.wav"
    source.write_bytes(payload)

    ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    command = [
        ffmpeg, "-y",
        "-ss", f"{start_seconds:.3f}",
        "-to", f"{end_seconds:.3f}",
        "-i", str(source),
        "-vn",
        "-ac", "1",
        "-ar", "48000",
        "-c:a", "pcm_s16le",
        str(target),
    ]
    completed = subprocess.run(
        command,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    if completed.returncode != 0 or not target.exists() or target.stat().st_size == 0:
        detail = (completed.stderr or "")[-1200:]
        raise RuntimeError(f"Không cắt được audio cho {scene_key}: {detail}")
    return AudioAsset(data=target.read_bytes(), filename=target.name)


def _effective_scene_audio(
    scene_key: int | str,
    recorded_assets: dict[int | str, AudioAsset],
) -> AudioAsset | None:
    """Priority: dashboard override/master slice -> ordinary uploaded per-slide file."""
    override = st.session_state.scene_audio_overrides.get(scene_key)
    if isinstance(override, AudioAsset):
        return override
    return recorded_assets.get(scene_key)


def audio_asset_for_scene(scene: VideoScene, assets: dict[int | str, AudioAsset]) -> AudioAsset | None:
    if scene.slide_type in {"intro", "outro"} and scene.slide_type in assets:
        return assets[scene.slide_type]
    if scene.source_slide_number is not None:
        return assets.get(scene.source_slide_number)
    if scene.slide_number:
        return assets.get(scene.slide_number)
    if scene.slide_type in {"intro", "outro"}:
        return assets.get(scene.slide_type)
    return None


def scene_label(scene: VideoScene) -> str:
    if scene.slide_type == "intro":
        return "mở đầu"
    if scene.slide_type == "outro":
        return "kết thúc"
    if scene.source_slide_number:
        return f"slide {scene.source_slide_number}"
    if scene.slide_number:
        return f"slide {scene.slide_number}"
    return scene.title


def _ensure_pipeline_cache_dirs() -> None:
    LOCAL_AUDIO_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    LOCAL_AVATAR_CACHE_DIR.mkdir(parents=True, exist_ok=True)


def _load_pipeline_manifest() -> dict:
    _ensure_pipeline_cache_dirs()
    if not LOCAL_PIPELINE_MANIFEST.exists():
        return {"version": 1, "audio": {}, "avatar": {}}
    try:
        data = json.loads(LOCAL_PIPELINE_MANIFEST.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("manifest invalid")
        data.setdefault("version", 1)
        data.setdefault("audio", {})
        data.setdefault("avatar", {})
        return data
    except Exception:
        return {"version": 1, "audio": {}, "avatar": {}}


def _save_pipeline_manifest(manifest: dict) -> None:
    _ensure_pipeline_cache_dirs()
    tmp = LOCAL_PIPELINE_MANIFEST.with_suffix(".tmp")
    tmp.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    tmp.replace(LOCAL_PIPELINE_MANIFEST)


def _cache_audio_path(cache_key: str) -> Path:
    return LOCAL_AUDIO_CACHE_DIR / f"{cache_key}.wav"


def _cache_avatar_path(cache_key: str) -> Path:
    return LOCAL_AVATAR_CACHE_DIR / f"{cache_key}.mp4"


def _restore_audio_from_disk(slide: int, cache_key: str) -> bool:
    path = _cache_audio_path(cache_key)
    if not path.exists() or path.stat().st_size == 0:
        return False
    st.session_state.local_avatar_audio[slide] = path.read_bytes()
    st.session_state.local_audio_hashes[slide] = cache_key
    return True


def _restore_avatar_from_disk(slide: int, cache_key: str) -> bool:
    path = _cache_avatar_path(cache_key)
    if not path.exists() or path.stat().st_size == 0:
        return False
    st.session_state.local_avatar_clips[slide] = path.read_bytes()
    st.session_state.local_avatar_hashes[slide] = cache_key
    return True


def _persist_audio_cache(slide: int, cache_key: str, audio_bytes: bytes) -> None:
    _ensure_pipeline_cache_dirs()
    path = _cache_audio_path(cache_key)
    path.write_bytes(audio_bytes)
    manifest = _load_pipeline_manifest()
    manifest["audio"][str(slide)] = {
        "hash": cache_key,
        "path": path.name,
        "size": len(audio_bytes),
        "updated_at": time.time(),
    }
    _save_pipeline_manifest(manifest)


def _persist_avatar_cache(slide: int, cache_key: str, video_bytes: bytes) -> None:
    _ensure_pipeline_cache_dirs()
    path = _cache_avatar_path(cache_key)
    path.write_bytes(video_bytes)
    manifest = _load_pipeline_manifest()
    manifest["avatar"][str(slide)] = {
        "hash": cache_key,
        "path": path.name,
        "size": len(video_bytes),
        "updated_at": time.time(),
    }
    _save_pipeline_manifest(manifest)


def _clear_pipeline_disk_cache() -> tuple[int, int]:
    _ensure_pipeline_cache_dirs()
    audio_count = 0
    avatar_count = 0
    for path in LOCAL_AUDIO_CACHE_DIR.glob("*.wav"):
        try:
            path.unlink()
            audio_count += 1
        except OSError:
            pass
    for path in LOCAL_AVATAR_CACHE_DIR.glob("*.mp4"):
        try:
            path.unlink()
            avatar_count += 1
        except OSError:
            pass
    try:
        LOCAL_PIPELINE_MANIFEST.unlink()
    except FileNotFoundError:
        pass
    return audio_count, avatar_count


def _pipeline_sha256(data: bytes | None) -> str:
    return hashlib.sha256(data or b"").hexdigest()


def _audio_job_cache_key(
    rec: dict,
    voice_clone_config: VoiceCloneConfig | None,
    voice_engine: str,
    voice_id: str,
    voice_rate: str,
    vieneu_style: str = "tu_nhien",
    vieneu_service_url: str = "",
) -> str:
    narration = apply_dictionary(rec.get("narration", ""), st.session_state.dictionary)
    payload = {
        "slide": int(rec.get("slide") or 0),
        "narration": narration,
        "voice_engine": voice_engine,
        "voice_id": voice_id,
        "voice_rate": voice_rate,
        "vieneu_style": vieneu_style,
        "vieneu_backend": os.getenv("VIENEU_BACKEND", "") if voice_engine == "vieneu" else "",
        "vieneu_service_url": vieneu_service_url if voice_engine == "vieneu" else "",
        "model": voice_clone_config.model if voice_clone_config else "",
        "reference_text": voice_clone_config.reference_transcript if voice_clone_config else "",
        "reference_audio_sha256": _pipeline_sha256(
            voice_clone_config.reference_audio if voice_clone_config else b""
        ),
    }
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()


def _avatar_job_cache_key(
    slide: int | str,
    audio_bytes: bytes,
    avatar_bytes: bytes | None,
    engine: str,
) -> str:
    payload = {
        "slide": str(slide),
        "audio_sha256": _pipeline_sha256(audio_bytes),
        "avatar_sha256": _pipeline_sha256(avatar_bytes),
        "engine": engine,
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True).encode("utf-8")
    ).hexdigest()


def _set_local_job(
    slide: int | str,
    status: str,
    stage: str,
    *,
    error: str = "",
    cache_source: str = "",
) -> None:
    st.session_state.local_job_status[slide] = status
    st.session_state.local_job_stage[slide] = stage
    if error:
        st.session_state.local_job_errors[slide] = error
    else:
        st.session_state.local_job_errors.pop(slide, None)
    if cache_source:
        st.session_state.local_job_cache_source[slide] = cache_source


def _pipeline_scene_items(records: list[dict]) -> list[dict]:
    """Build the same logical order used by export: intro -> PPT slides -> outro."""
    boundary = st.session_state.get(
        "boundary",
        {"intro": {"mode": "none"}, "outro": {"mode": "none"}},
    )
    items: list[dict] = []
    excluded: set[int] = set()

    for side in ("intro", "outro"):
        cfg = boundary.get(side, {}) or {}
        if (
            cfg.get("mode") == "source_slide"
            and cfg.get("remove_from_original_position")
            and cfg.get("source_slide_number")
        ):
            excluded.add(int(cfg["source_slide_number"]))

    def boundary_item(side: str) -> dict | None:
        cfg = boundary.get(side, {}) or {}
        mode = cfg.get("mode", "none")
        if mode == "none":
            return None

        narration = str(cfg.get("narration") or "").strip()
        source_slide = cfg.get("source_slide_number")
        if mode == "source_slide" and source_slide:
            rec = next(
                (r for r in records if int(r["slide"]) == int(source_slide)),
                None,
            )
            if rec is not None and not narration:
                narration = str(rec.get("narration") or "").strip()

        narration = apply_dictionary(narration, st.session_state.dictionary)
        return {
            "key": side,
            "label": "Mở đầu" if side == "intro" else "Kết thúc",
            "kind": side,
            "narration": narration,
            "needs_audio": bool(narration),
            "needs_lipsync": bool(narration),
            "source_slide": int(source_slide) if source_slide else None,
            "mode": mode,
        }

    intro = boundary_item("intro")
    if intro:
        items.append(intro)

    for rec in records:
        slide = int(rec["slide"])
        if slide in excluded or rec.get("skip"):
            continue
        narration = apply_dictionary(
            str(rec.get("narration") or "").strip(),
            st.session_state.dictionary,
        )
        items.append(
            {
                "key": slide,
                "label": f"Slide {slide}",
                "kind": "slide",
                "narration": narration,
                "needs_audio": bool(narration),
                "needs_lipsync": bool(narration),
                "source_slide": slide,
                "mode": "ppt",
            }
        )

    outro = boundary_item("outro")
    if outro:
        items.append(outro)

    return items


def _pipeline_item_map(items: list[dict]) -> dict:
    return {item["key"]: item for item in items}


def _pipeline_audio_cache_key(
    item: dict,
    voice_clone_config: VoiceCloneConfig | None,
    voice_engine: str,
    voice_id: str,
    voice_rate: str,
    vieneu_style: str = "tu_nhien",
    vieneu_service_url: str = "",
) -> str:
    payload = {
        "scene_key": str(item["key"]),
        "kind": item.get("kind", ""),
        "narration": item.get("narration", ""),
        "voice_engine": voice_engine,
        "voice_id": voice_id,
        "voice_rate": voice_rate,
        "vieneu_style": vieneu_style,
        "vieneu_backend": os.getenv("VIENEU_BACKEND", "") if voice_engine == "vieneu" else "",
        "vieneu_service_url": vieneu_service_url if voice_engine == "vieneu" else "",
        "model": voice_clone_config.model if voice_clone_config else "",
        "reference_text": (
            voice_clone_config.reference_transcript if voice_clone_config else ""
        ),
        "reference_audio_sha256": _pipeline_sha256(
            voice_clone_config.reference_audio if voice_clone_config else b""
        ),
    }
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()


def _scene_runtime_key(scene: VideoScene) -> int | str | None:
    # Boundary scenes get independent keys even when sourced from a PPT slide.
    if scene.slide_type in {"intro", "outro"}:
        return scene.slide_type
    if scene.source_slide_number is not None:
        return int(scene.source_slide_number)
    if scene.slide_number is not None:
        return int(scene.slide_number)
    return None


def _job_dashboard_frame(items: list[dict]) -> pd.DataFrame:
    rows = []
    for item in items:
        key = item["key"]
        has_text = bool(item.get("needs_audio"))
        if not has_text:
            status = "Done"
            stage = "Static / no narration"
        else:
            status = st.session_state.local_job_status.get(key, "Pending")
            stage = st.session_state.local_job_stage.get(key, "Audio")

        rows.append(
            {
                "Scene": item["label"],
                "Type": item["kind"],
                "Narration": "Có" if has_text else "Không",
                "Status": status,
                "Stage": stage,
                "Audio": (
                    "N/A"
                    if not has_text
                    else ("✓" if key in st.session_state.local_avatar_audio else "—")
                ),
                "Lip-sync": (
                    "N/A"
                    if not item.get("needs_lipsync")
                    else ("✓" if key in st.session_state.local_avatar_clips else "—")
                ),
                "Cache": st.session_state.local_job_cache_source.get(key, ""),
                "Error": st.session_state.local_job_errors.get(key, ""),
            }
        )
    return pd.DataFrame(rows)



def _direct_voice_clone_synthesize(
    *,
    endpoint: str,
    reference_audio: bytes,
    reference_filename: str,
    text: str,
    reference_text: str,
    voice_id: str,
    model: str,
    api_key: str,
    upload_password: str,
    voice_use_consent: bool,
    request_id: str,
) -> dict | None:
    """Call Local Voice Clone directly from Python.

    This is the preferred path when Streamlit is running on the same Windows
    machine as port 8009. It avoids the Streamlit browser component being
    re-rendered and accidentally issuing the same very expensive Vira request
    multiple times.

    Return None only when Python cannot connect to the endpoint, allowing the
    existing Browser Bridge to remain as a Cloud fallback.
    """
    read_timeout = max(
        60,
        int(os.getenv("VOICE_CLONE_TIMEOUT_SECONDS", "7200")),
    )

    headers: dict[str, str] = {}
    if api_key:
        headers["X-API-Key"] = api_key
    if upload_password:
        headers["X-Voice-Upload-Password"] = upload_password

    files = {
        "reference_audio": (
            reference_filename or "reference.wav",
            reference_audio,
            "audio/wav",
        )
    }
    data = {
        "text": text,
        "reference_text": reference_text or "",
        "voice_id": voice_id or "default",
        "model": model or "f5-tts",
        "language": "vi",
        "speed": "1.0",
        "output_format": "wav",
        "voice_use_consent": "true" if voice_use_consent else "false",
        "return_audio": "true",
    }

    try:
        response = requests.post(
            endpoint,
            headers=headers,
            files=files,
            data=data,
            timeout=(8, read_timeout),
        )
    except requests.exceptions.ConnectionError:
        # Typical Streamlit-Cloud case: localhost:8009 belongs to the browser
        # machine, not the Python server. Fall back to Browser Bridge.
        return None
    except requests.exceptions.Timeout as exc:
        return {
            "ok": False,
            "error": (
                f"Voice Clone timeout sau {read_timeout}s: {exc}. "
                "Audio đã tạo ở 8009 có thể vẫn còn trong cache."
            ),
            "request_id": request_id,
        }
    except requests.RequestException as exc:
        return {
            "ok": False,
            "error": f"Không gọi được Voice Clone bằng Python: {exc}",
            "request_id": request_id,
        }

    if not response.ok:
        detail = ""
        try:
            payload = response.json()
            detail = (
                payload.get("message")
                or payload.get("detail")
                or payload.get("error")
                or str(payload)
            )
        except Exception:
            detail = response.text[-2000:]
        if response.status_code == 401:
            detail = (
                f"{detail} Hãy nhập đúng LOCAL_API_KEY của service 8009 "
                "vào ô API key; đây không phải mật khẩu mở khóa upload."
            ).strip()
        return {
            "ok": False,
            "error": f"Voice Clone HTTP {response.status_code}: {detail}",
            "request_id": request_id,
        }

    content_type = (response.headers.get("content-type") or "").lower()

    if (
        content_type.startswith("audio/")
        or "application/octet-stream" in content_type
        or response.content[:4] == b"RIFF"
    ):
        return {
            "ok": True,
            "kind": "audio",
            "audio_base64": base64.b64encode(response.content).decode("ascii"),
            "content_type": content_type or "audio/wav",
            "request_id": request_id,
            "transport": "python-direct",
            "cache_hit": False,
        }

    try:
        payload = response.json()
    except Exception as exc:
        return {
            "ok": False,
            "error": f"Voice Clone trả response không đọc được: {exc}",
            "request_id": request_id,
        }

    if payload.get("audio_base64"):
        return {
            "ok": True,
            "kind": "audio",
            "audio_base64": payload["audio_base64"],
            "content_type": payload.get("content_type", "audio/wav"),
            "payload": payload,
            "request_id": request_id,
            "transport": "python-direct",
            "cache_hit": bool(payload.get("cache_hit")),
        }

    if payload.get("audio_url"):
        try:
            audio_headers = {"X-API-Key": api_key} if api_key else {}
            audio_response = requests.get(
                payload["audio_url"],
                headers=audio_headers,
                timeout=(8, 300),
            )
            audio_response.raise_for_status()
        except requests.RequestException as exc:
            if getattr(exc.response, "status_code", None) == 401:
                return {
                    "ok": False,
                    "error": (
                        "Không tải được WAV đã synthesize: HTTP 401. "
                        "Service 8009 yêu cầu LOCAL_API_KEY khi tải audio."
                    ),
                    "request_id": request_id,
                }
            return {
                "ok": False,
                "error": f"Không tải được WAV đã synthesize: {exc}",
                "request_id": request_id,
            }

        return {
            "ok": True,
            "kind": "audio",
            "audio_base64": base64.b64encode(
                audio_response.content
            ).decode("ascii"),
            "content_type": (
                audio_response.headers.get("content-type")
                or "audio/wav"
            ),
            "payload": payload,
            "request_id": request_id,
            "transport": "python-direct",
            "cache_hit": bool(payload.get("cache_hit")),
        }

    return {
        "ok": False,
        "error": "Voice Clone không trả audio binary, audio_base64 hoặc audio_url.",
        "request_id": request_id,
    }


def _queue_unique(items: list) -> list:
    seen = set()
    result = []
    for item in items:
        if item not in seen:
            result.append(item)
            seen.add(item)
    return result

st.title("🎬 PPT Video Studio")
st.caption("Chuyển PowerPoint thành video thuyết minh tiếng Việt; hỗ trợ OpenAvatar Runtime để nhép môi bằng GPU local khi app chạy trên Streamlit Cloud.")

tab_upload, tab_story, tab_dict, tab_export = st.tabs([
    "1. PowerPoint", "2. Storyboard", "3. Từ điển", "4. Xuất video"
])

with tab_upload:
    left, right = st.columns([1, 1])
    with left:
        uploaded = st.file_uploader("Tải file PowerPoint", type=["pptx"], help="Phiên bản cloud hỗ trợ .pptx.")
        style = st.selectbox("Phong cách thuyết minh", ["Thuyết trình chuyên nghiệp", "Điều hành", "Đọc gần nguyên bản"])
        organization = st.text_input("Tên đơn vị", value=st.session_state.get("organization", ""))
        st.session_state.organization = organization
    with right:
        st.info("App dùng python-pptx để đọc nội dung và ưu tiên Microsoft PowerPoint COM trên Windows để render nguyên hình slide; nếu không có thì dùng LibreOffice. Nền, ảnh, biểu đồ, SmartArt và bố cục được giữ ở dạng tĩnh; animation và video nhúng không được phát lại.")

    if uploaded:
        payload = uploaded.getvalue()
        needs_reload = (
            uploaded.name != st.session_state.ppt_name
            or payload != st.session_state.ppt_payload
            or not st.session_state.records
        )
        if needs_reload:
            try:
                if uploaded.name != st.session_state.ppt_name or payload != st.session_state.ppt_payload:
                    reset_presentation_editing_state()
                records = read_pptx(payload)
                for record in records:
                    record["narration"] = apply_dictionary(build_narration(record, style), st.session_state.dictionary)
                    record["skip"] = False
                    record["pause_after"] = 0.35
                st.session_state.records = records
                st.session_state.ppt_name = uploaded.name
                st.session_state.ppt_payload = payload
                st.session_state.original_slide_images = []
                st.session_state.render_warning = ""
                try:
                    rendered, backend = render_pptx_slides(payload, uploaded.name)
                    if len(rendered) != len(records):
                        raise PowerPointRenderError(
                            f"Số ảnh render ({len(rendered)}) khác số slide ({len(records)})."
                        )
                    st.session_state.original_slide_images = rendered
                    st.session_state.render_backend = backend
                except Exception as render_exc:
                    st.session_state.render_backend = "Không có ảnh gốc"
                    st.session_state.render_warning = str(render_exc)
                st.success(f"Đã phân tích {len(records)} slide từ {uploaded.name}.")
            except Exception as exc:
                st.error(f"Không đọc được PowerPoint: {exc}")

    if st.session_state.records:
        c1, c2, c3, c4 = st.columns(4)
        records = st.session_state.records
        c1.metric("Tổng slide", len(records))
        c2.metric("Slide rỗng/ít nội dung", sum(1 for r in records if r["word_count"] <= 2))
        c3.metric("Slide có liên kết", sum(1 for r in records if r["links"]))
        c4.metric("Tổng số từ", sum(r["word_count"] for r in records))
        st.write(f"**Nguồn hình slide:** {st.session_state.render_backend}")
        if st.session_state.render_warning:
            st.error(
                "Không render được hình gốc. App sẽ không dùng slide dựng lại từ text "
                "để tránh làm sai bố cục PowerPoint. "
                f"Chi tiết: {st.session_state.render_warning}"
            )
            st.info(
                "Nếu đang chạy Windows, hãy khởi động lại Streamlit rồi tải lại PPT "
                "để PowerPoint COM được khởi tạo đúng thread. Nếu máy không có "
                "Microsoft PowerPoint, hãy cài LibreOffice; app có thể dùng PyMuPDF "
                "nên không bắt buộc phải cài thêm Poppler ở máy local."
            )
        elif st.session_state.original_slide_images:
            st.success("Đã render nguyên hình slide để dùng trong video.")
            preview_cols = st.columns(min(4, len(st.session_state.original_slide_images)))
            for index, image in enumerate(st.session_state.original_slide_images[:4]):
                preview_cols[index].image(image, caption=f"Slide {index + 1}", use_container_width=True)
        preview_df = pd.DataFrame([{
            "Slide": r["slide"], "Loại": r["slide_type"], "Tiêu đề": r["title"],
            "Số từ": r["word_count"], "Liên kết": len(r["links"])
        } for r in records])
        st.dataframe(preview_df, use_container_width=True, hide_index=True)

with tab_story:
    records = st.session_state.records
    if not records:
        st.warning("Hãy tải PowerPoint ở bước 1.")
        st.download_button(
            "Tải mẫu Storyboard CSV",
            storyboard_csv_bytes(),
            "storyboard_mau.csv",
            "text/csv",
            help="Sau khi tải PowerPoint, dùng chính file mẫu theo số slide của bài trình bày để nhập lại.",
        )
    else:
        ensure_boundary_defaults(records)
        st.subheader("Cấu hình slide mở đầu")
        intro_mode_label = st.radio("Nguồn mở đầu", ["Không thêm", "Chọn slide trong PowerPoint", "Slide mặc định", "Tải ảnh riêng"], horizontal=True, key="intro_mode_label")
        intro_mode = {"Không thêm":"none", "Chọn slide trong PowerPoint":"source_slide", "Slide mặc định":"system_default", "Tải ảnh riêng":"uploaded_image"}[intro_mode_label]
        intro_index = None
        intro_title = st.session_state.intro_title
        intro_subtitle = st.session_state.intro_subtitle
        remove_intro = True
        if intro_mode == "source_slide":
            intro_index = st.selectbox("Chọn slide mở đầu", [r["slide"] for r in records], format_func=lambda n: f"Slide {n} — {records[n-1]['title']}", key="intro_source_slide")
            remove_intro = st.checkbox("Không lặp lại slide này ở vị trí cũ", value=True, key="remove_intro")
        elif intro_mode == "system_default":
            intro_title = st.text_input("Tiêu đề mở đầu", key="intro_title")
            intro_subtitle = st.text_input("Phụ đề mở đầu", key="intro_subtitle")
        elif intro_mode == "uploaded_image":
            intro_file = st.file_uploader("Ảnh mở đầu", type=["png", "jpg", "jpeg"], key="intro_image")
            if intro_file:
                st.session_state.intro_upload = intro_file.getvalue()
            if st.session_state.intro_upload:
                st.image(st.session_state.intro_upload, caption="Ảnh mở đầu", width=260)
        if intro_mode != "none":
            intro_narration = st.text_area(
                "Lời thuyết minh mở đầu",
                key="intro_narration",
                height=96,
                help="Dùng cho cả slide PowerPoint, slide mặc định và ảnh riêng.",
            )
        else:
            intro_narration = st.session_state.intro_narration

        st.subheader("Cấu hình slide kết thúc")
        outro_mode_label = st.radio("Nguồn kết thúc", ["Không thêm", "Chọn slide trong PowerPoint", "Slide mặc định", "Tải ảnh riêng"], horizontal=True, key="outro_mode_label")
        outro_mode = {"Không thêm":"none", "Chọn slide trong PowerPoint":"source_slide", "Slide mặc định":"system_default", "Tải ảnh riêng":"uploaded_image"}[outro_mode_label]
        outro_index = None
        outro_title = st.session_state.outro_title
        outro_subtitle = st.session_state.outro_subtitle
        remove_outro = True
        if outro_mode == "source_slide":
            outro_index = st.selectbox("Chọn slide kết thúc", [r["slide"] for r in records], index=len(records)-1, format_func=lambda n: f"Slide {n} — {records[n-1]['title']}", key="outro_source_slide")
            remove_outro = st.checkbox("Không lặp lại slide này ở vị trí cũ", value=True, key="remove_outro")
        elif outro_mode == "system_default":
            outro_title = st.text_input("Tiêu đề kết thúc", key="outro_title")
            outro_subtitle = st.text_input("Phụ đề kết thúc", key="outro_subtitle")
        elif outro_mode == "uploaded_image":
            outro_file = st.file_uploader("Ảnh kết thúc", type=["png", "jpg", "jpeg"], key="outro_image")
            if outro_file:
                st.session_state.outro_upload = outro_file.getvalue()
            if st.session_state.outro_upload:
                st.image(st.session_state.outro_upload, caption="Ảnh kết thúc", width=260)
        if outro_mode != "none":
            outro_narration = st.text_area(
                "Lời thuyết minh kết thúc",
                key="outro_narration",
                height=96,
                help="Dùng cho cả slide PowerPoint, slide mặc định và ảnh riêng.",
            )
        else:
            outro_narration = st.session_state.outro_narration

        st.session_state.boundary = {
            "intro": asdict(BoundarySlideConfig(
                mode=intro_mode,
                source_slide_number=intro_index,
                remove_from_original_position=remove_intro,
                title=intro_title,
                subtitle=intro_subtitle,
                narration=intro_narration,
            )),
            "outro": asdict(BoundarySlideConfig(
                mode=outro_mode,
                source_slide_number=outro_index,
                remove_from_original_position=remove_outro,
                title=outro_title,
                subtitle=outro_subtitle,
                narration=outro_narration,
            )),
        }

        st.divider()
        st.subheader("Biên tập storyboard")
        st.caption("Tải file mẫu, điền lại các cột cần sửa rồi nhập CSV, Excel hoặc project JSON đã xuất trước đó.")
        template_col, import_col = st.columns(2)
        with template_col:
            st.download_button(
                "Tải mẫu Storyboard CSV",
                storyboard_csv_bytes(records),
                "storyboard_mau.csv",
                "text/csv",
                use_container_width=True,
            )
        with import_col:
            try:
                st.download_button(
                    "Tải mẫu Storyboard Excel",
                    storyboard_xlsx_bytes(records),
                    "storyboard_mau.xlsx",
                    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    use_container_width=True,
                )
            except ImportError:
                st.caption("Cài `openpyxl` để dùng file mẫu Excel.")

        storyboard_file = st.file_uploader(
            "Nhập dữ liệu Storyboard",
            type=["csv", "tsv", "xlsx", "xls", "json"],
            key="storyboard_import_file",
            help="Khớp theo cột Slide. Có thể nhập Tiêu đề, Lời thuyết minh, Xuất và Nghỉ sau (giây).",
        )
        storyboard_import_notice = st.session_state.pop("storyboard_import_notice", None)
        if storyboard_import_notice:
            st.success(storyboard_import_notice)
        if storyboard_file:
            try:
                imported_storyboard = read_storyboard_upload(
                    storyboard_file.getvalue(), storyboard_file.name
                )
                st.caption(f"Đã đọc {len(imported_storyboard)} dòng từ {storyboard_file.name}. Kiểm tra trước khi áp dụng.")
                st.dataframe(imported_storyboard.head(20), use_container_width=True, hide_index=True)
                if st.button("Kiểm tra và áp dụng dữ liệu import", key="apply_storyboard_import", use_container_width=True):
                    updates = prepare_storyboard_updates(records, imported_storyboard)
                    blocked = [
                        f"Slide {update.slide}"
                        for update in updates
                        if update.narration is not None and profanity.contains_profanity(update.narration)
                    ]
                    if blocked:
                        st.error("Không thể nhập lời thuyết minh chứa từ ngữ không phù hợp: " + ", ".join(blocked))
                    else:
                        changed = apply_storyboard_updates(records, updates)
                        st.session_state.pop("storyboard_editor", None)
                        st.session_state.storyboard_import_notice = f"Đã áp dụng dữ liệu import cho {changed}/{len(updates)} slide."
                        st.rerun()
            except Exception as exc:
                st.error(f"Không thể nhập Storyboard: {exc}")

        edited = st.data_editor(
            pd.DataFrame([{
                "Xuất": not r.get("skip", False), "Slide": r["slide"], "Loại": r["slide_type"],
                "Tiêu đề": r["title"], "Lời thuyết minh": r["narration"], "Nghỉ sau (giây)": r.get("pause_after", 0.35)
            } for r in records]),
            hide_index=True,
            use_container_width=True,
            disabled=["Slide", "Loại"],
            column_config={"Lời thuyết minh": st.column_config.TextColumn(width="large"), "Xuất": st.column_config.CheckboxColumn()},
            key="storyboard_editor",
        )
        for i, row in edited.iterrows():
            records[i]["skip"] = not bool(row["Xuất"])
            records[i]["title"] = str(row["Tiêu đề"])
            narration = str(row["Lời thuyết minh"])
            if profanity.contains_profanity(narration):
                st.error(f"Lời thuyết minh slide {records[i]['slide']} chứa từ ngữ không phù hợp.")
            else:
                records[i]["narration"] = narration
            try:
                records[i]["pause_after"] = float(row["Nghỉ sau (giây)"])
            except (TypeError, ValueError):
                st.error(f"Khoảng nghỉ sau slide {records[i]['slide']} phải là một số.")

with tab_dict:
    st.subheader("Từ điển phát âm")
    st.caption("Từ điển mặc định nằm trong GitHub. Các thay đổi của người dùng được giữ trong phiên và có thể xuất thành JSON để dùng lại.")
    col_a, col_b = st.columns([2, 1])
    with col_a:
        source = st.text_input("Từ gốc", placeholder="Ví dụ: BHYT")
        pronunciation = st.text_input("Cách đọc", placeholder="Ví dụ: bảo hiểm y tế")
    with col_b:
        category = st.text_input("Nhóm", value="Khác")
        case_sensitive = st.checkbox("Phân biệt hoa thường", value=True)
        whole_word = st.checkbox("Khớp nguyên từ", value=True)
    if st.button("Thêm vào từ điển", type="primary"):
        entry = PronunciationEntry(source.strip(), pronunciation.strip(), category.strip() or "Khác", True, whole_word, case_sensitive)
        errors = validate_entry(entry, st.session_state.dictionary, profanity)
        if errors:
            for error in errors:
                st.error(error)
        else:
            st.session_state.dictionary.append(entry)
            st.success("Đã thêm thuật ngữ.")
            st.rerun()

    upload_dict = st.file_uploader("Nhập từ điển JSON", type=["json"], key="dict_upload")
    if upload_dict and st.button("Kiểm tra và nhập JSON"):
        try:
            imported = parse_dictionary_bytes(upload_dict.getvalue())
            valid: list[PronunciationEntry] = []
            errors: list[str] = []
            for entry in imported:
                item_errors = validate_entry(entry, st.session_state.dictionary + valid, profanity)
                if item_errors:
                    errors.extend([f"{entry.source or '(trống)'}: {e}" for e in item_errors])
                else:
                    valid.append(entry)
            if errors:
                st.error("Không thể nhập vì JSON có dữ liệu không hợp lệ.")
                st.code("\n".join(errors))
            else:
                st.session_state.dictionary.extend(valid)
                st.success(f"Đã nhập {len(valid)} thuật ngữ.")
                st.rerun()
        except Exception as exc:
            st.error(f"JSON không hợp lệ: {exc}")

    dict_df = pd.DataFrame([asdict(entry) for entry in st.session_state.dictionary])
    st.dataframe(dict_df, use_container_width=True, hide_index=True)
    c1, c2 = st.columns(2)
    c1.download_button("Tải từ điển JSON", dictionary_json(st.session_state.dictionary).encode("utf-8"), "pronunciation_dictionary.json", "application/json")
    if c2.button("Khôi phục từ điển mặc định"):
        st.session_state.dictionary = load_dictionary(DEFAULT_DICTIONARY)
        st.rerun()

with tab_export:
    records = st.session_state.records
    if not records:
        st.warning("Hãy tải và biên tập PowerPoint trước khi xuất.")
    else:
        st.subheader("Giọng đọc")
        voice_source = st.radio(
            "Nguồn giọng đọc",
            [
                "AI tiếng Việt",
                "VieNeu-TTS local",
                "Bản thu thật theo từng slide",
                "Nhân bản giọng từ mẫu (API riêng)",
            ],
            horizontal=True,
            key="voice_source",
        )
        voice_engine = "edge"
        voice_id = "vi-VN-HoaiMyNeural"
        voice_rate = "+0%"
        vieneu_style = "tu_nhien"
        vieneu_service_url = os.getenv(
            "VIENEU_SERVICE_URL", "http://127.0.0.1:8009"
        ).strip().rstrip("/")
        vieneu_service_api_key = (
            os.getenv("VIENEU_SERVICE_API_KEY", "").strip()
            or configured_voice_clone_api_key()
        )
        vieneu_direct_python = (
            os.getenv("VIENEU_DIRECT_PYTHON", "false").strip().lower()
            in {"1", "true", "yes", "on"}
        )
        voice_clone_config = None
        voice_clone_consent = False
        recorded_voice_assets: dict[int | str, AudioAsset] = {}
        voice_upload_unlocked = voice_source in {"AI tiếng Việt", "VieNeu-TTS local"}

        if not voice_upload_unlocked:
            voice_upload_unlocked = unlock_voice_uploads()

        if voice_source == "AI tiếng Việt":
            voice_col, rate_col = st.columns(2)
            voice_id = voice_col.selectbox(
                "Giọng AI",
                ["vi-VN-HoaiMyNeural", "vi-VN-NamMinhNeural"],
            )
            voice_rate = rate_col.selectbox(
                "Tốc độ",
                ["-10%", "-5%", "+0%", "+5%", "+10%"],
                index=2,
            )
            st.caption("Dùng Edge TTS; audio được tạo riêng cho từng slide để khớp phụ đề và avatar.")
        elif voice_source == "VieNeu-TTS local":
            voice_engine = "vieneu"
            st.caption(
                "Browser Bridge sẽ gọi VieNeu-TTS trên service local, không chạy model "
                "trong Streamlit Cloud. Nếu service 8009 chạy bằng "
                "start_f5_8009.bat, nhập LOCAL_API_KEY ở ô dưới."
            )
            vieneu_service_url = st.text_input(
                "VieNeu local service URL",
                value=vieneu_service_url,
                placeholder="http://127.0.0.1:8009",
                key="vieneu_service_url",
                help=(
                    "Service phải chạy trên cùng máy với trình duyệt. Mặc định dùng "
                    "local_voice_clone tại cổng 8009."
                ),
            ).strip().rstrip("/")
            vieneu_service_api_key = st.text_input(
                "VieNeu service API key (nếu có)",
                value=vieneu_service_api_key,
                type="password",
                key="vieneu_service_api_key",
                help=(
                    "Phải trùng LOCAL_API_KEY của service 8009 nếu bạn chạy "
                    "start_f5_8009.bat; có thể đặt VIENEU_SERVICE_API_KEY "
                    "hoặc VOICE_CLONE_API_KEY trong Streamlit secrets."
                ),
            ).strip()
            vieneu_style = st.selectbox(
                "Phong cách đọc",
                options=list(VIENEU_STYLES),
                format_func=lambda style: VIENEU_STYLES[style],
                key="vieneu_style",
                help=(
                    "Một số bản VieNeu v3 Turbo đã mã hóa phong cách trong giọng preset "
                    "và có thể bỏ qua tùy chọn này."
                ),
            )
            voice_list_result = local_gpu_bridge(
                action="vieneu_voices",
                agent_url=vieneu_service_url or "http://127.0.0.1:8009",
                api_key=vieneu_service_api_key,
                request_id="vieneu-voices",
                key="vieneu_voice_list",
            )
            vieneu_voices: list[tuple[str, str]] = []
            if isinstance(voice_list_result, dict) and voice_list_result.get("ok"):
                payload = voice_list_result.get("payload") or {}
                voice_items = payload.get("voices", []) if isinstance(payload, dict) else []
                for item in voice_items:
                    if not isinstance(item, dict):
                        continue
                    voice_key = str(item.get("id") or item.get("voice_id") or "").strip()
                    label = str(item.get("label") or voice_key).strip()
                    if voice_key:
                        vieneu_voices.append((label, voice_key))

            # Direct Python is an explicit opt-in for a fully local Streamlit
            # deployment. It is disabled by default so Cloud cannot load the
            # VieNeu model accidentally on the Streamlit server.
            if not vieneu_voices and vieneu_direct_python:
                if not vieneu_available():
                    st.warning("Chưa cài VieNeu-TTS trong môi trường Streamlit local.")
                    st.code(vieneu_install_hint())
                else:
                    try:
                        vieneu_voices = list_vieneu_voices()
                    except Exception as exc:
                        st.error(f"Không đọc được danh sách giọng VieNeu-TTS: {exc}")

            if vieneu_voices:
                voice_options = [voice_key for _, voice_key in vieneu_voices]
                voice_labels = {voice_key: label for label, voice_key in vieneu_voices}
                env_voice = os.getenv("VIENEU_VOICE", "").strip()
                default_index = (
                    voice_options.index(env_voice)
                    if env_voice in voice_options
                    else 0
                )
                voice_id = st.selectbox(
                    "Giọng VieNeu-TTS",
                    options=voice_options,
                    index=default_index,
                    format_func=lambda selected: voice_labels.get(selected, selected),
                    key="vieneu_voice_id",
                )
                st.caption(f"Đã nhận {len(vieneu_voices)} giọng preset từ VieNeu-TTS local.")
            else:
                voice_id = st.text_input(
                    "Voice ID VieNeu-TTS (tùy chọn)",
                    value=os.getenv("VIENEU_VOICE", ""),
                    key="vieneu_voice_id_fallback",
                    help="Để trống để dùng giọng mặc định của model.",
                ).strip()
                if isinstance(voice_list_result, dict) and voice_list_result.get("ok") is False:
                    vieneu_error = str(
                        voice_list_result.get("error", "Không có chi tiết lỗi.")
                    )
                    if any(
                        marker in vieneu_error.lower()
                        for marker in ("hub", "checkpoint", "model", "cache")
                    ):
                        st.warning(
                            "Đã gọi được service 8009 nhưng model VieNeu chưa sẵn sàng. "
                            "Khởi động lại start_f5_8009.bat sau khi tải model rồi tải lại trang."
                        )
                    else:
                        st.warning(
                            "Chưa kết nối được VieNeu local service. Hãy chạy "
                            "local_voice_clone/start_f5_8009.bat rồi tải lại trang."
                        )
                    st.caption(vieneu_error)
                elif not vieneu_direct_python:
                    st.info(
                        "Chưa nhận danh sách giọng. Chạy service local tại "
                        f"{vieneu_service_url or 'http://127.0.0.1:8009'}; "
                        "có thể nhập Voice ID thủ công trong lúc chờ kết nối."
                    )
        elif voice_source == "Bản thu thật theo từng slide":
            voice_engine = "uploaded"
            if voice_upload_unlocked:
                audio_files = st.file_uploader(
                    "Tải các bản thu lời đọc",
                    type=["mp3", "wav", "m4a", "aac", "ogg", "flac"],
                    accept_multiple_files=True,
                    key="recorded_voice_files",
                    help="Đặt tên slide_001.mp3, slide_002.wav…; dùng intro.mp3 hoặc outro.mp3 cho ảnh/slide mở đầu và kết thúc.",
                )
                recorded_voice_assets, audio_file_errors = uploaded_audio_assets(audio_files or [])
                for error in audio_file_errors:
                    st.error(error)
                if recorded_voice_assets:
                    slots = [f"slide {slot}" if isinstance(slot, int) else str(slot) for slot in recorded_voice_assets]
                    st.success("Đã nhận bản thu cho: " + ", ".join(slots))
                st.info(
                    "Chế độ này dùng nguyên giọng thật trong các file bạn tải lên; không gửi giọng đi để nhân bản. "
                    "Cần có file cho mọi slide/cảnh có lời thuyết minh được xuất."
                )
            else:
                st.info("Nhập đúng mật khẩu ở trên để tải các bản thu lời đọc.")
        else:
            voice_engine = "voice_clone"
            if voice_upload_unlocked:
                clone_sample = st.file_uploader(
                    "Tải mẫu giọng (khuyến nghị 15–60 giây, rõ tiếng, một người nói)",
                    type=["mp3", "wav", "m4a", "aac", "ogg", "flac"],
                    key="voice_clone_reference",
                )
                clone_col, model_col = st.columns([2, 1])
                clone_endpoint = clone_col.text_input(
                    "Voice-clone API endpoint",
                    value=os.getenv("VOICE_CLONE_API_URL", "http://127.0.0.1:8009/v1/voice-clone/synthesize"),
                    placeholder="http://127.0.0.1:8009/v1/voice-clone/synthesize",
                    help="Mặc định là Local Voice Clone Service trên máy này. Endpoint nhận mẫu giọng và text rồi trả audio.",
                    key="voice_clone_endpoint",
                )
                clone_model = model_col.text_input(
                    "Model",
                    value=os.getenv("VOICE_CLONE_MODEL", "f5-tts"),
                    help="Đang dùng start_f5_8009.bat nên chọn f5-tts; chỉ chọn vira-tts khi service Vira đang chạy.",
                    key="voice_clone_model",
                )
                clone_api_key = st.text_input(
                    "LOCAL_API_KEY của service 8009",
                    value=configured_voice_clone_api_key(),
                    type="password",
                    key="voice_clone_api_key",
                    help=(
                        "Phải trùng LOCAL_API_KEY trong start_f5_8009.bat "
                        "(hoặc biến môi trường của service). Không nhập mật "
                        "khẩu mở khóa upload vào ô này."
                    ),
                )
                local_voice_upload_password = st.text_input(
                    "Mật khẩu Local Voice Clone (8009)",
                    value=os.getenv("LOCAL_VOICE_UPLOAD_PASSWORD", ""),
                    type="password",
                    key="local_voice_upload_password",
                    help=(
                        "Mật khẩu mà service 127.0.0.1:8009 dùng để cho phép "
                        "reference_audio. Đây là mật khẩu riêng, không nhất thiết "
                        "giống mật khẩu mở khóa upload trên Streamlit."
                    ),
                )
                clone_transcript = st.text_area(
                    "Nội dung của mẫu giọng (không bắt buộc)",
                    height=74,
                    help="Một số model clone giọng dùng transcript này để tăng độ chính xác.",
                    key="voice_clone_transcript",
                )
                clone_verify_ssl = st.checkbox("Xác minh SSL", value=True, key="voice_clone_verify_ssl")
                voice_clone_consent = st.checkbox(
                    "Tôi xác nhận mình sở hữu giọng này hoặc có sự đồng ý rõ ràng của người sở hữu giọng.",
                    key="voice_clone_consent",
                )
                if clone_sample:
                    st.audio(clone_sample.getvalue(), format=clone_sample.type or "audio/mpeg")
                if clone_sample and voice_clone_consent:
                    voice_clone_config = VoiceCloneConfig(
                        endpoint=clone_endpoint,
                        reference_audio=clone_sample.getvalue(),
                        reference_filename=clone_sample.name,
                        model=clone_model,
                        api_key=clone_api_key,
                        reference_transcript=clone_transcript,
                        voice_use_consent=voice_clone_consent,
                        upload_password=str(local_voice_upload_password or ""),
                        verify_ssl=clone_verify_ssl,
                    )
                if ":8009" in clone_endpoint and not clone_api_key.strip():
                    st.warning(
                        "Service 8009 đang yêu cầu LOCAL_API_KEY. Nếu để trống "
                        "ô trên, bước tạo audio sẽ trả HTTP 401."
                    )
                st.info(
                    "Local Voice Clone Service mặc định chạy tại 127.0.0.1:8009: audio và text không rời máy. "
                    "Mẫu giọng chỉ được giữ trong phiên xuất và không được đưa vào file project tải xuống."
                )
            else:
                st.info("Nhập đúng mật khẩu ở trên để tải mẫu giọng và cấu hình nhân bản giọng.")

        render_col, subtitle_col, fps_col = st.columns(3)
        burn_subtitles = render_col.checkbox("Đốt phụ đề vào video", value=True)
        subtitle_position = subtitle_col.selectbox(
            "Độ cao phụ đề",
            ["Dưới", "Giữa"],
            help="Chọn phụ đề nằm phía dưới hoặc giữa khung hình.",
        )
        fps = fps_col.selectbox("FPS", [12, 15, 24], index=1)
        subtitle_font_size = st.slider("Cỡ chữ phụ đề", 10, 60, 28)
        subtitle_background_color = "#000000"
        subtitle_text_color = "#FFFFFF"
        subtitle_box_width_percent = 85
        subtitle_alignment = "Canh giữa"
        if burn_subtitles:
            subtitle_style_cols = st.columns(4)
            subtitle_background_color = subtitle_style_cols[0].color_picker(
                "Màu nền khung",
                value="#000000",
                key="subtitle_background_color",
            )
            subtitle_text_color = subtitle_style_cols[1].color_picker(
                "Màu chữ",
                value="#FFFFFF",
                key="subtitle_text_color",
            )
            subtitle_box_width_percent = subtitle_style_cols[2].slider(
                "Độ rộng khung (%)",
                min_value=40,
                max_value=100,
                value=85,
                key="subtitle_box_width_percent",
                help="Tỷ lệ chiều rộng khung phụ đề so với video.",
            )
            subtitle_alignment = subtitle_style_cols[3].selectbox(
                "Căn khung",
                ["Canh giữa", "Góc trái", "Góc phải"],
                key="subtitle_alignment",
                help="Canh giữa hoặc đặt khung về góc trái/góc phải.",
            )
        st.caption(
            "Phụ đề chạy kiểu karaoke theo từng cụm ngắn, không dồn cả đoạn; "
            "hết lời sẽ ẩn khỏi khung hình. Có thể chọn màu, độ rộng và căn giữa/góc. "
            "Cỡ chữ từ 10 trở lên."
        )
        preserve_original_slide = st.checkbox(
            "Giữ nguyên khung slide PPT (không zoom/crop/fade)",
            value=True,
            help=(
                "Giữ nguyên hình ảnh và mép slide gốc cho mọi nguồn audio. "
                "Bỏ chọn nếu muốn dùng hiệu ứng zoom/fade nhẹ."
            ),
        )

        st.divider()
        st.subheader("Người dẫn ở góc video")
        presenter_mode = st.radio(
            "Chế độ người dẫn",
            ["Không sử dụng", "Ảnh tĩnh", "Hiệu ứng nói nhẹ", "AI nhép môi qua GPU API", "AI nhép môi bằng OpenAvatar Runtime"],
            horizontal=True,
        )
        avatar_enabled = presenter_mode != "Không sử dụng"
        avatar_image = None
        ai_avatar_config = None
        avatar_position = "Dưới phải"
        avatar_size_percent = 18
        avatar_shape = "Tròn"
        avatar_border_width = 4
        avatar_talking_effect = False
        if avatar_enabled:
            avatar_file = st.file_uploader("Tải ảnh chân dung người dẫn", type=["png", "jpg", "jpeg", "webp"], key="avatar_image")
            if avatar_file:
                st.session_state.avatar_upload = avatar_file.getvalue()
            if st.session_state.avatar_upload:
                avatar_image = Image.open(io.BytesIO(st.session_state.avatar_upload)).convert("RGBA")
                st.image(avatar_image, caption="Ảnh người dẫn", width=180)
            a1, a2, a3 = st.columns(3)
            avatar_position = a1.selectbox("Góc hiển thị", ["Trên trái", "Trên phải", "Dưới trái", "Dưới phải"], index=3)
            avatar_shape = a2.selectbox("Khung ảnh", ["Tròn", "Bo góc"])
            avatar_size_percent = a3.slider("Kích thước (% chiều rộng)", 10, 32, 18)
            avatar_border_width = st.slider("Độ dày viền", 0, 12, 4)
            avatar_talking_effect = presenter_mode == "Hiệu ứng nói nhẹ"
            if presenter_mode == "AI nhép môi bằng OpenAvatar Runtime":
                st.caption("Trình duyệt gửi ảnh và audio trực tiếp tới OpenAvatar Runtime tại máy đang mở app. File không đi qua GPU của Streamlit Cloud.")
                l1, l2 = st.columns([2, 1])
                openavatar_runtime_url = l1.text_input(
                    "OpenAvatar Runtime URL",
                    value="http://127.0.0.1:8008",
                    help="Runtime chạy trên máy đang mở trình duyệt."
                )
                avatar_engine = l2.selectbox(
                    "Engine local",
                    ["wav2lip", "sadtalker", "musetalk", "liveportrait"],
                    key="local_engine",
                )

                # Bắt buộc dùng Browser Bridge khi app deploy trên Streamlit Cloud.
                health_result = local_gpu_bridge(
                    action="health",
                    agent_url=openavatar_runtime_url,
                    request_id="health",
                    key="openavatar_runtime_health",
                )
                if isinstance(health_result, dict):
                    if health_result.get("ok"):
                        payload = health_result.get("payload", {})
                        gpu_name = payload.get("gpu", "OpenAvatar Runtime")
                        free_vram = payload.get("vram_free_gb")
                        driver = payload.get("driver")
                        detail = f"Đã kết nối: {gpu_name}"
                        if free_vram is not None:
                            detail += f" | VRAM trống: {free_vram} GB"
                        if driver:
                            detail += f" | Driver: {driver}"
                        st.success(detail)

                        engine_items = payload.get("engines") or []
                        selected = next(
                            (
                                item for item in engine_items
                                if isinstance(item, dict) and item.get("id") == avatar_engine
                            ),
                            None,
                        )
                        if selected and not selected.get("available"):
                            st.warning(
                                f"Engine {avatar_engine} chưa sẵn sàng: "
                                f"{selected.get('message') or selected.get('missing')}"
                            )
                    else:
                        st.error(
                            health_result.get(
                                "error",
                                "Không kết nối được OpenAvatar Runtime.",
                            )
                        )

                with st.expander("Kiểm tra bằng Python SDK khi chạy Streamlit local"):
                    st.caption(
                        "Không dùng mục này trên Streamlit Cloud. "
                        "Python SDK chỉ truy cập được localhost khi Streamlit và Runtime "
                        "cùng chạy trên một máy."
                    )
                    if st.button("Kiểm tra Runtime bằng openavatar-sdk"):
                        ok, sdk_result = check_runtime_from_python(
                            openavatar_runtime_url
                        )
                        if ok:
                            st.success("Python SDK đã kết nối Runtime.")
                            st.json(sdk_result)
                        else:
                            st.error(str(sdk_result))

                st.info(
                    "OpenAvatar chỉ nhận audio từ Nguồn giọng đọc ở phía trên để nhép môi; "
                    "không cần Voice Clone API nếu bạn chọn Edge, bản thu hoặc VieNeu local. "
                    "Trước khi tạo avatar, chạy OpenAvatar Runtime bằng "
                    "`installer/start_agent.cmd`, rồi kiểm tra "
                    "`http://127.0.0.1:8008/health`."
                )
                avatar_talking_effect = False
            elif presenter_mode == "AI nhép môi qua GPU API":
                st.caption("App gửi ảnh và audio từng slide tới GPU API, nhận video talking-head rồi ghép vào góc slide.")
                g1, g2 = st.columns([2, 1])
                default_api_url = os.getenv("AVATAR_API_URL", "")
                gpu_api_url = g1.text_input("GPU API URL", value=default_api_url, placeholder="https://your-gpu-worker.example.com")
                avatar_engine = g2.selectbox("Engine", ["wav2lip", "sadtalker", "musetalk", "liveportrait"])
                api_key_default = os.getenv("AVATAR_API_KEY", "")
                gpu_api_key = st.text_input("API key", value=api_key_default, type="password", help="Có thể để trống khi API riêng không yêu cầu xác thực.")
                verify_ssl = st.checkbox("Xác minh SSL", value=True)
                ai_avatar_config = AvatarApiConfig(
                    base_url=gpu_api_url.strip(),
                    api_key=gpu_api_key.strip(),
                    engine=avatar_engine,
                    timeout_seconds=3600,
                    verify_ssl=verify_ssl,
                ) if gpu_api_url.strip() else None

                t1, t2 = st.columns(2)
                if t1.button("Kiểm tra kết nối GPU", use_container_width=True):
                    if ai_avatar_config is None:
                        st.error("Hãy nhập GPU API URL.")
                    else:
                        ok, message = check_avatar_api(ai_avatar_config)
                        (st.success if ok else st.error)(message)

                if t2.button("Tạo thử tối đa 5 giây", use_container_width=True):
                    if ai_avatar_config is None or avatar_image is None:
                        st.error("Cần ảnh người dẫn và GPU API URL.")
                    else:
                        preview_record = next((r for r in records if not r.get("skip") and r.get("narration", "").strip()), None)
                        if preview_record is None:
                            st.error("Không có lời thuyết minh để tạo thử.")
                        else:
                            try:
                                with st.spinner("Đang tạo audio và gọi GPU API..."):
                                    preview_dir = Path(tempfile.mkdtemp(prefix="avatar_preview_"))
                                    image_path = preview_dir / "avatar.png"
                                    audio_path = preview_dir / "preview.mp3"
                                    result_path = preview_dir / "preview.mp4"
                                    avatar_image.convert("RGB").save(image_path)
                                    preview_text = apply_dictionary(preview_record["narration"], st.session_state.dictionary)[:380]
                                    preview_scene = VideoScene(
                                        title="Preview",
                                        narration=preview_text,
                                        source_slide_number=preview_record["slide"],
                                    )
                                    audio_path = synthesize_scene_audio(
                                        preview_scene,
                                        audio_path,
                                        voice_engine=voice_engine,
                                        voice_id=voice_id,
                                        voice_rate=voice_rate,
                                        vieneu_style=vieneu_style,
                                        voice_clone_config=voice_clone_config,
                                        uploaded_audio=audio_asset_for_scene(preview_scene, recorded_voice_assets),
                                    )
                                    if audio_path is None or not audio_path.exists():
                                        raise RuntimeError("Không tạo được audio preview.")
                                    generate_talking_head(
                                        ai_avatar_config, image_path, audio_path, result_path, preview_seconds=5.0
                                    )
                                    st.video(result_path.read_bytes())
                            except Exception as exc:
                                st.error(f"Không tạo được preview AI: {exc}")
            elif presenter_mode == "Ảnh tĩnh":
                avatar_talking_effect = False
            else:
                st.info("Hiệu ứng nói nhẹ chạy trực tiếp trên Streamlit, không cần GPU API.")

        local_avatar_videos_for_export = None
        if presenter_mode == "AI nhép môi bằng OpenAvatar Runtime":
            if avatar_image is None:
                st.warning("Hãy tải ảnh người dẫn để dùng OpenAvatar Runtime.")
            else:
                st.markdown("#### Resume Job Dashboard")
                st.caption(
                    "Pipeline theo scene thực tế: Mở đầu → slide PowerPoint → Kết thúc. "
                    "Scene có lời mới tạo audio/nhép môi; scene không có lời chỉ giữ hình tĩnh."
                )

                pipeline_items = _pipeline_scene_items(records)
                item_by_key = _pipeline_item_map(pipeline_items)
                narration_keys = [
                    item["key"] for item in pipeline_items if item["needs_audio"]
                ]
                lipsync_keys = [
                    item["key"] for item in pipeline_items if item["needs_lipsync"]
                ]

                # Synchronize dashboard.
                for item in pipeline_items:
                    key = item["key"]
                    if not item["needs_audio"]:
                        _set_local_job(key, "Done", "Static / no narration")
                        continue
                    if key in st.session_state.local_avatar_clips:
                        if st.session_state.local_job_status.get(key) not in {
                            "Cached", "Failed"
                        }:
                            _set_local_job(key, "Done", "Complete")
                    elif key in st.session_state.local_avatar_audio:
                        if st.session_state.local_job_status.get(key) not in {
                            "Cached", "Failed", "Processing"
                        }:
                            _set_local_job(key, "Done", "Audio ready")
                    else:
                        st.session_state.local_job_status.setdefault(key, "Pending")
                        st.session_state.local_job_stage.setdefault(key, "Audio")

                dashboard = _job_dashboard_frame(pipeline_items)
                if not dashboard.empty:
                    counts = dashboard["Status"].value_counts().to_dict()
                    m1, m2, m3, m4, m5 = st.columns(5)
                    m1.metric("Cached", counts.get("Cached", 0))
                    m2.metric("Pending", counts.get("Pending", 0))
                    m3.metric("Processing", counts.get("Processing", 0))
                    m4.metric("Done", counts.get("Done", 0))
                    m5.metric("Failed", counts.get("Failed", 0))
                    st.dataframe(
                        dashboard,
                        use_container_width=True,
                        hide_index=True,
                        column_config={
                            "Error": st.column_config.TextColumn(width="large"),
                        },
                    )

                retry_col, continue_col, info_col = st.columns([1, 1, 2])
                retry_failed = retry_col.button(
                    "Retry failed only",
                    use_container_width=True,
                    disabled=not bool(st.session_state.local_job_failed_stage),
                )

                # Continue from PPT slide N; intro is handled independently as a scene.
                ppt_slide_numbers = [
                    int(item["key"])
                    for item in pipeline_items
                    if item["kind"] == "slide"
                ]
                min_slide = min(ppt_slide_numbers) if ppt_slide_numbers else 1
                max_slide = max(ppt_slide_numbers) if ppt_slide_numbers else 1
                continue_from = continue_col.number_input(
                    "Continue from slide N",
                    min_value=min_slide,
                    max_value=max_slide,
                    value=min_slide,
                    step=1,
                    key="resume_continue_from_slide",
                )
                continue_clicked = continue_col.button(
                    "Continue",
                    use_container_width=True,
                    disabled=not bool(ppt_slide_numbers),
                )
                info_col.caption(
                    "Mở đầu/Kết thúc có lời được xử lý như scene riêng. "
                    "Nếu không có lời: Audio = N/A, Lip-sync = N/A và không tốn GPU."
                )

                with st.expander("🎙️ Audio ngoài theo từng slide / cắt từ một bản thu dài", expanded=False):
                    st.caption(
                        "Có thể thay audio AI cho từng scene. Ưu tiên: audio chọn ở đây → "
                        "audio upload theo tên slide → engine giọng đang chọn. "
                        "Timecode nhận MM:SS hoặc HH:MM:SS."
                    )

                    master_file = st.file_uploader(
                        "Bản thu âm dài (tùy chọn)",
                        type=["mp3", "wav", "m4a", "aac", "ogg", "flac"],
                        key="dashboard_master_recording",
                        help="Ví dụ một file thu 30 phút; sau đó khai báo Slide 1 từ 00:15 đến 01:05, Slide 2 từ 01:05 đến 02:10...",
                    )
                    if master_file is not None:
                        payload = master_file.getvalue()
                        if (
                            payload
                            and (
                                st.session_state.master_recording_name != master_file.name
                                or st.session_state.master_recording_payload != payload
                            )
                        ):
                            st.session_state.master_recording_payload = payload
                            st.session_state.master_recording_name = master_file.name

                    if st.session_state.master_recording_payload:
                        st.audio(st.session_state.master_recording_payload)

                    range_rows = []
                    for item in pipeline_items:
                        if not item["needs_audio"]:
                            continue
                        old_range = st.session_state.master_recording_ranges.get(
                            item["key"], {}
                        )
                        range_rows.append(
                            {
                                "Scene": item["label"],
                                "Key": str(item["key"]),
                                "Dùng bản thu dài": bool(old_range.get("enabled", False)),
                                "Bắt đầu": str(old_range.get("start", "")),
                                "Kết thúc": str(old_range.get("end", "")),
                            }
                        )

                    if range_rows:
                        range_df = st.data_editor(
                            pd.DataFrame(range_rows),
                            hide_index=True,
                            use_container_width=True,
                            disabled=["Scene", "Key"],
                            column_config={
                                "Dùng bản thu dài": st.column_config.CheckboxColumn(),
                                "Bắt đầu": st.column_config.TextColumn(
                                    help="MM:SS hoặc HH:MM:SS.mmm"
                                ),
                                "Kết thúc": st.column_config.TextColumn(
                                    help="MM:SS hoặc HH:MM:SS.mmm"
                                ),
                            },
                            key="master_audio_range_editor",
                        )

                        if st.button(
                            "✂️ Áp dụng timecode vào các slide",
                            use_container_width=True,
                            key="apply_master_audio_ranges",
                        ):
                            if not st.session_state.master_recording_payload:
                                st.error("Hãy tải bản thu âm dài trước.")
                            else:
                                key_lookup = {
                                    str(item["key"]): item["key"]
                                    for item in pipeline_items
                                }
                                applied = 0
                                errors = []
                                for _, row in range_df.iterrows():
                                    scene_key = key_lookup.get(str(row["Key"]))
                                    if scene_key is None:
                                        continue
                                    enabled = bool(row["Dùng bản thu dài"])
                                    start_raw = str(row["Bắt đầu"] or "").strip()
                                    end_raw = str(row["Kết thúc"] or "").strip()
                                    st.session_state.master_recording_ranges[scene_key] = {
                                        "enabled": enabled,
                                        "start": start_raw,
                                        "end": end_raw,
                                    }
                                    if not enabled:
                                        continue
                                    try:
                                        start_s = _parse_timecode(start_raw)
                                        end_s = _parse_timecode(end_raw)
                                        asset = _slice_master_audio(
                                            st.session_state.master_recording_payload,
                                            st.session_state.master_recording_name,
                                            start_s,
                                            end_s,
                                            scene_key,
                                        )
                                        st.session_state.scene_audio_overrides[scene_key] = asset
                                        st.session_state.local_avatar_audio[scene_key] = asset.data
                                        # Audio changed: any old lip-sync clip is now stale.
                                        st.session_state.local_avatar_clips.pop(scene_key, None)
                                        st.session_state.local_audio_hashes.pop(scene_key, None)
                                        st.session_state.local_avatar_hashes.pop(scene_key, None)
                                        _set_local_job(
                                            scene_key,
                                            "Done",
                                            "External audio ready",
                                            cache_source="master-recording",
                                        )
                                        applied += 1
                                    except Exception as exc:
                                        errors.append(
                                            f"{item_by_key[scene_key]['label']}: {exc}"
                                        )
                                if errors:
                                    st.error("\n".join(errors))
                                if applied:
                                    st.success(f"Đã cắt và gán audio cho {applied} scene.")
                                    st.rerun()

                    st.markdown("**Upload/đổi audio riêng cho một slide**")
                    override_scene = st.selectbox(
                        "Scene cần thay audio",
                        [item["key"] for item in pipeline_items if item["needs_audio"]],
                        format_func=lambda key: item_by_key[key]["label"],
                        key="dashboard_audio_override_scene",
                    )
                    override_file = st.file_uploader(
                        "Chọn file audio cho scene này",
                        type=["mp3", "wav", "m4a", "aac", "ogg", "flac"],
                        key=f"dashboard_audio_override_file_{override_scene}",
                    )
                    oc1, oc2 = st.columns(2)
                    if oc1.button(
                        "Dùng audio này",
                        use_container_width=True,
                        disabled=override_file is None,
                        key="dashboard_apply_audio_override",
                    ):
                        payload = override_file.getvalue() if override_file else b""
                        if payload:
                            asset = AudioAsset(
                                data=payload,
                                filename=override_file.name,
                            )
                            st.session_state.scene_audio_overrides[override_scene] = asset
                            st.session_state.local_avatar_audio[override_scene] = payload
                            st.session_state.local_avatar_clips.pop(override_scene, None)
                            st.session_state.local_audio_hashes.pop(override_scene, None)
                            st.session_state.local_avatar_hashes.pop(override_scene, None)
                            _set_local_job(
                                override_scene,
                                "Done",
                                "External audio ready",
                                cache_source="manual-upload",
                            )
                            st.rerun()

                    if oc2.button(
                        "Bỏ audio ngoài của scene",
                        use_container_width=True,
                        key="dashboard_clear_audio_override",
                    ):
                        st.session_state.scene_audio_overrides.pop(override_scene, None)
                        st.session_state.local_avatar_audio.pop(override_scene, None)
                        st.session_state.local_avatar_clips.pop(override_scene, None)
                        st.session_state.local_audio_hashes.pop(override_scene, None)
                        st.session_state.local_avatar_hashes.pop(override_scene, None)
                        _set_local_job(override_scene, "Pending", "Audio")
                        st.rerun()

                a1, a2, a3, a4 = st.columns(4)
                create_audio = a1.button(
                    "1. Tạo / cập nhật audio",
                    use_container_width=True,
                    disabled=not bool(narration_keys),
                )
                preview_one = a2.button(
                    "2. Preview nhép môi 1 scene",
                    use_container_width=True,
                    disabled=not bool(st.session_state.local_avatar_audio),
                )
                create_lipsync = a3.button(
                    "3. Tạo / cập nhật nhép môi",
                    use_container_width=True,
                    disabled=not bool(st.session_state.local_avatar_audio),
                )
                batch_all = a4.button(
                    "🚀 Chạy hàng loạt + xuất video",
                    use_container_width=True,
                    disabled=not bool(narration_keys),
                    type="primary",
                )

                if st.session_state.local_batch_mode:
                    st.info(
                        "Đang chạy hàng loạt tự động: Audio → OpenAvatar → Xuất video. "
                        "Không cần bấm sang slide kế tiếp."
                    )

                if batch_all:
                    if voice_engine == "voice_clone" and voice_clone_config is None:
                        st.error(
                            "Bạn đang chọn nguồn giọng ‘Nhân bản giọng từ mẫu (API riêng)’ "
                            "nhưng chưa tải mẫu giọng và xác nhận quyền sử dụng."
                        )
                        st.info(
                            "OpenAvatar chỉ làm nhép môi, không tự tạo giọng. "
                            "Muốn dùng VieNeu, chọn ‘VieNeu-TTS local’ ở mục Nguồn giọng đọc; "
                            "muốn dùng F5 clone thì tải mẫu giọng và cấu hình service 8009."
                        )
                        st.stop()

                    targets = list(lipsync_keys)
                    st.session_state.local_batch_mode = True
                    st.session_state.local_batch_auto_export = True
                    st.session_state.auto_export_requested = False
                    st.session_state.local_continue_avatar_targets = targets

                    # Restore valid audio cache from disk first.
                    for key in targets:
                        item = item_by_key.get(key)
                        if item is None or not item.get("needs_audio"):
                            continue
                        audio_key = _pipeline_audio_cache_key(
                            item,
                            voice_clone_config,
                            voice_engine,
                            voice_id,
                            voice_rate,
                            vieneu_style,
                            vieneu_service_url,
                        )
                        if key not in st.session_state.local_avatar_audio:
                            try:
                                if _restore_audio_from_disk(key, audio_key):
                                    _set_local_job(
                                        key,
                                        "Cached",
                                        "Audio ready",
                                        cache_source="disk",
                                    )
                            except Exception:
                                pass

                    audio_pending = [
                        key
                        for key in targets
                        if key not in st.session_state.local_avatar_audio
                    ]
                    st.session_state.local_voice_queue = _queue_unique(audio_pending)

                    if not st.session_state.local_voice_queue:
                        avatar_pending = [
                            key
                            for key in targets
                            if key in st.session_state.local_avatar_audio
                            and key not in st.session_state.local_avatar_clips
                        ]
                        st.session_state.local_avatar_queue = _queue_unique(
                            avatar_pending
                        )
                        st.session_state.local_continue_avatar_targets = []

                        if not st.session_state.local_avatar_queue:
                            st.session_state.local_batch_mode = False
                            st.session_state.auto_export_requested = True

                    for key in targets:
                        if key in st.session_state.local_voice_queue:
                            _set_local_job(key, "Pending", "Audio")
                        elif key in st.session_state.local_avatar_queue:
                            _set_local_job(key, "Pending", "Lip-sync")

                    st.rerun()

                if retry_failed:
                    failed_audio = []
                    failed_avatar = []
                    for key, stage in list(
                        st.session_state.local_job_failed_stage.items()
                    ):
                        if key not in item_by_key:
                            continue
                        if stage == "audio":
                            failed_audio.append(key)
                        elif stage == "avatar":
                            failed_avatar.append(key)
                        st.session_state.local_job_errors.pop(key, None)
                        _set_local_job(
                            key,
                            "Pending",
                            "Audio" if stage == "audio" else "Lip-sync",
                        )

                    st.session_state.local_voice_queue = _queue_unique(failed_audio)
                    st.session_state.local_avatar_queue = _queue_unique(
                        [
                            key
                            for key in failed_avatar
                            if key in st.session_state.local_avatar_audio
                        ]
                    )
                    for key in failed_audio + failed_avatar:
                        st.session_state.local_job_failed_stage.pop(key, None)
                    st.rerun()

                if continue_clicked:
                    # Preserve intro/outro. "Continue from N" applies to PPT scenes N+.
                    targets = [
                        item["key"]
                        for item in pipeline_items
                        if (
                            item["kind"] == "slide"
                            and int(item["key"]) >= int(continue_from)
                            and item["needs_audio"]
                        )
                    ]
                    audio_pending = [
                        key
                        for key in targets
                        if key not in st.session_state.local_avatar_audio
                    ]
                    st.session_state.local_continue_avatar_targets = targets
                    st.session_state.local_voice_queue = _queue_unique(audio_pending)

                    for key in targets:
                        if key in audio_pending:
                            _set_local_job(key, "Pending", "Audio")
                        elif key not in st.session_state.local_avatar_clips:
                            _set_local_job(key, "Pending", "Lip-sync")

                    if not st.session_state.local_voice_queue:
                        st.session_state.local_avatar_queue = _queue_unique(
                            [
                                key
                                for key in targets
                                if key in st.session_state.local_avatar_audio
                                and key not in st.session_state.local_avatar_clips
                            ]
                        )
                        st.session_state.local_continue_avatar_targets = []
                    st.rerun()

                if create_audio:
                    if voice_engine == "voice_clone" and voice_clone_config is None:
                        st.error(
                            "Bạn đang chọn nguồn giọng ‘Nhân bản giọng từ mẫu (API riêng)’ "
                            "nhưng chưa tải mẫu giọng và xác nhận quyền sử dụng."
                        )
                        st.info(
                            "Nếu không muốn dùng Voice Clone, đổi Nguồn giọng đọc sang "
                            "‘VieNeu-TTS local’ hoặc ‘AI tiếng Việt’."
                        )
                    else:
                        pending = []
                        for key in narration_keys:
                            if key not in st.session_state.local_avatar_audio:
                                pending.append(key)
                                _set_local_job(key, "Pending", "Audio")
                        st.session_state.local_voice_queue = _queue_unique(pending)
                        st.rerun()

                # Audio synthesis queue. VieNeu and voice-clone requests use the
                # browser bridge by default so Streamlit Cloud never touches the
                # user's local GPU service. Edge/uploaded audio stays in Python.
                voice_queue = st.session_state.local_voice_queue
                if voice_queue:
                    current_key = voice_queue[0]
                    current_item = item_by_key.get(current_key)
                    if current_item is None or not current_item["needs_audio"]:
                        st.session_state.local_voice_queue = voice_queue[1:]
                        st.rerun()

                    _set_local_job(current_key, "Processing", "Audio")

                    clone_endpoint = ""
                    voice_base_url = ""
                    if voice_engine == "voice_clone" and voice_clone_config is not None:
                        clone_endpoint = voice_clone_config.endpoint.rstrip("/")
                        marker = "/v1/voice-clone/synthesize"
                        voice_base_url = (
                            clone_endpoint[:-len(marker)]
                            if clone_endpoint.endswith(marker)
                            else clone_endpoint
                        )

                    done = sum(
                        1
                        for key in narration_keys
                        if key in st.session_state.local_avatar_audio
                    )
                    st.progress(
                        done / max(1, len(narration_keys)),
                        text=(
                            f"VieNeu-TTS ({voice_id or 'mặc định'}): {current_item['label']}"
                            if voice_engine == "vieneu"
                            else f"{(voice_clone_config.model if voice_clone_config else 'voice-clone')}: {current_item['label']}"
                        ),
                    )

                    audio_cache_key = _pipeline_audio_cache_key(
                        current_item,
                        voice_clone_config,
                        voice_engine,
                        voice_id,
                        voice_rate,
                        vieneu_style,
                        vieneu_service_url,
                    )
                    # Re-check persistent disk cache immediately before making
                    # an expensive GPU request. This protects against Streamlit
                    # reruns/restarts and against the same scene being queued twice.
                    restored_now = False
                    if current_key not in st.session_state.local_avatar_audio:
                        try:
                            restored_now = _restore_audio_from_disk(
                                current_key,
                                audio_cache_key,
                            )
                        except Exception:
                            restored_now = False

                    if restored_now:
                        _set_local_job(
                            current_key,
                            "Cached",
                            "Audio ready",
                            cache_source="disk",
                        )
                        st.session_state.local_voice_queue = list(voice_queue[1:])

                        if (
                            not st.session_state.local_voice_queue
                            and st.session_state.local_continue_avatar_targets
                        ):
                            targets = list(
                                st.session_state.local_continue_avatar_targets
                            )
                            st.session_state.local_avatar_queue = _queue_unique(
                                [
                                    key
                                    for key in targets
                                    if key in st.session_state.local_avatar_audio
                                    and key not in st.session_state.local_avatar_clips
                                ]
                            )
                            st.session_state.local_continue_avatar_targets = []

                        st.rerun()

                    # Direct voice-clone Python is kept for a fully local app.
                    # VieNeu has a separate explicit opt-in below; its default
                    # path is always the browser bridge.
                    use_direct_clone_python = (
                        os.getenv("VOICE_CLONE_DIRECT_PYTHON", "true")
                        .strip()
                        .lower()
                        in {"1", "true", "yes", "on"}
                    )

                    voice_result = None
                    if voice_engine == "voice_clone":
                        if use_direct_clone_python:
                            voice_result = _direct_voice_clone_synthesize(
                                endpoint=clone_endpoint,
                                reference_audio=voice_clone_config.reference_audio,
                                reference_filename=voice_clone_config.reference_filename,
                                text=current_item["narration"],
                                reference_text=voice_clone_config.reference_transcript or "",
                                voice_id="default",
                                model=voice_clone_config.model or "f5-tts",
                                api_key=voice_clone_config.api_key or "",
                                upload_password=voice_clone_config.upload_password or "",
                                voice_use_consent=bool(
                                    voice_clone_config.voice_use_consent
                                ),
                                request_id=f"voice-scene-{current_key}",
                            )

                        # Cloud fallback: Python cannot reach the user's
                        # localhost, therefore let the browser call 8009.
                        if voice_result is None:
                            voice_result = local_gpu_bridge(
                                action="voice_synthesize",
                                agent_url=voice_base_url,
                                reference_audio_bytes=voice_clone_config.reference_audio,
                                reference_audio_filename=voice_clone_config.reference_filename,
                                text=current_item["narration"],
                                reference_text=voice_clone_config.reference_transcript or "",
                                voice_id="default",
                                model=voice_clone_config.model or "f5-tts",
                                api_key=voice_clone_config.api_key or "",
                                voice_use_consent=bool(
                                    voice_clone_config.voice_use_consent
                                ),
                                upload_password=voice_clone_config.upload_password or "",
                                request_id=f"voice-scene-{current_key}",
                                cache_key=audio_cache_key,
                                key=f"voice_clone_scene_{current_key}",
                            )
                    elif voice_engine == "vieneu" and not vieneu_direct_python:
                        voice_result = local_gpu_bridge(
                            action="voice_synthesize",
                            agent_url=vieneu_service_url or "http://127.0.0.1:8009",
                            api_key=vieneu_service_api_key,
                            text=current_item["narration"],
                            voice_id=voice_id or "default",
                            model="vieneu",
                            voice_style=vieneu_style,
                            request_id=f"voice-scene-{current_key}",
                            cache_key=audio_cache_key,
                            key=f"vieneu_scene_{current_key}",
                        )
                    else:
                        # Edge/uploaded audio, plus the explicit
                        # VIENEU_DIRECT_PYTHON opt-in, run in this Streamlit
                        # process.
                        try:
                            item_key = current_item["key"]
                            is_boundary = current_item.get("kind") in {"intro", "outro"}
                            numeric_key = None if is_boundary else int(item_key)
                            local_scene = VideoScene(
                                title=current_item["label"],
                                narration=current_item["narration"],
                                slide_number=numeric_key or 0,
                                source_slide_number=numeric_key,
                                slide_type=current_item.get("kind", "content"),
                            )
                            uploaded_asset = (
                                audio_asset_for_scene(local_scene, recorded_voice_assets)
                                if voice_engine == "uploaded"
                                else None
                            )
                            with tempfile.TemporaryDirectory(prefix="local_tts_scene_") as temp_dir:
                                generated_path = synthesize_scene_audio(
                                    local_scene,
                                    Path(temp_dir) / "narration.wav",
                                    voice_engine=voice_engine,
                                    voice_id=voice_id,
                                    voice_rate=voice_rate,
                                    vieneu_style=vieneu_style,
                                    uploaded_audio=uploaded_asset,
                                )
                                audio_bytes = generated_path.read_bytes() if generated_path else b""
                            voice_result = {
                                "ok": bool(audio_bytes),
                                "kind": "audio",
                                "audio_base64": base64.b64encode(audio_bytes).decode("ascii"),
                                "content_type": "audio/wav",
                                "transport": f"{voice_engine}-python",
                                "request_id": f"voice-scene-{current_key}",
                            }
                        except Exception as exc:
                            voice_result = {
                                "ok": False,
                                "error": str(exc),
                                "request_id": f"voice-scene-{current_key}",
                            }

                    if isinstance(voice_result, dict) and voice_result.get("ok"):
                        # Browser Bridge may return the service request_id instead of
                        # our UI request_id. Do not block queue advancement on that
                        # metadata field; the component key already identifies scene.
                        audio_bytes = decode_audio_result(voice_result)
                        if audio_bytes:
                            st.session_state.local_avatar_audio[current_key] = audio_bytes
                            st.session_state.local_audio_hashes[current_key] = audio_cache_key
                            try:
                                _persist_audio_cache(
                                    current_key,
                                    audio_cache_key,
                                    audio_bytes,
                                )
                            except Exception as cache_exc:
                                st.warning(
                                    f"Không ghi được audio cache cho {current_item['label']}: "
                                    f"{cache_exc}"
                                )

                            if voice_result.get("cache_hit"):
                                _set_local_job(
                                    current_key,
                                    "Cached",
                                    "Audio ready",
                                    cache_source=voice_result.get(
                                        "cache_source", "cache"
                                    ),
                                )
                            else:
                                transport = voice_result.get("transport", "")
                                _set_local_job(
                                    current_key,
                                    "Done",
                                    "Audio ready",
                                    cache_source=transport,
                                )

                            st.session_state.local_job_failed_stage.pop(
                                current_key, None
                            )

                            # IMPORTANT: always pop the completed scene.
                            st.session_state.local_voice_queue = list(voice_queue[1:])

                            # When the last audio finishes, automatically start
                            # OpenAvatar for every target scene.
                            if (
                                not st.session_state.local_voice_queue
                                and st.session_state.local_continue_avatar_targets
                            ):
                                targets = list(
                                    st.session_state.local_continue_avatar_targets
                                )
                                st.session_state.local_avatar_queue = _queue_unique(
                                    [
                                        key
                                        for key in targets
                                        if key in st.session_state.local_avatar_audio
                                        and key not in st.session_state.local_avatar_clips
                                    ]
                                )
                                st.session_state.local_continue_avatar_targets = []

                                if (
                                    st.session_state.local_batch_mode
                                    and not st.session_state.local_avatar_queue
                                ):
                                    st.session_state.local_batch_mode = False
                                    if st.session_state.local_batch_auto_export:
                                        st.session_state.auto_export_requested = True

                            st.rerun()
                        else:
                            message = (
                                "Bộ tổng hợp giọng báo thành công nhưng không trả audio."
                            )
                            _set_local_job(
                                current_key, "Failed", "Audio", error=message
                            )
                            st.session_state.local_job_failed_stage[current_key] = "audio"
                            st.session_state.local_voice_queue = list(voice_queue[1:])
                            st.session_state.local_batch_mode = False
                            st.error(message)
                            st.rerun()

                    elif (
                        isinstance(voice_result, dict)
                        and voice_result.get("ok") is False
                    ):
                        message = voice_result.get(
                            "error", "Bộ tổng hợp giọng local xử lý thất bại"
                        )
                        _set_local_job(
                            current_key, "Failed", "Audio", error=message
                        )
                        st.session_state.local_job_failed_stage[current_key] = "audio"
                        st.session_state.local_voice_queue = list(voice_queue[1:])
                        st.session_state.local_batch_mode = False
                        st.rerun()

                # Review audio by scene.
                ready_audio_keys = [
                    key
                    for key in narration_keys
                    if key in st.session_state.local_avatar_audio
                ]
                if ready_audio_keys and not st.session_state.local_voice_queue:
                    review_key = st.selectbox(
                        "Nghe thử audio",
                        ready_audio_keys,
                        format_func=lambda key: item_by_key[key]["label"],
                        key="local_audio_review_scene",
                    )
                    st.audio(
                        st.session_state.local_avatar_audio[review_key],
                        format="audio/wav",
                    )

                if preview_one and ready_audio_keys:
                    preview_key = st.session_state.get(
                        "local_audio_review_scene", ready_audio_keys[0]
                    )
                    st.session_state.local_avatar_queue = [preview_key]
                    _set_local_job(preview_key, "Pending", "Lip-sync")
                    st.rerun()

                if create_lipsync:
                    missing_audio = [
                        key
                        for key in lipsync_keys
                        if key not in st.session_state.local_avatar_audio
                    ]
                    if missing_audio:
                        st.error(
                            "Chưa có audio cho: "
                            + ", ".join(item_by_key[key]["label"] for key in missing_audio[:12])
                        )
                    else:
                        targets = [
                            key
                            for key in lipsync_keys
                            if key not in st.session_state.local_avatar_clips
                        ]
                        st.session_state.local_avatar_queue = _queue_unique(targets)
                        for key in targets:
                            _set_local_job(key, "Pending", "Lip-sync")
                        st.rerun()

                # OpenAvatar queue.
                queue = st.session_state.local_avatar_queue
                if not st.session_state.local_voice_queue and queue:
                    current_key = queue[0]
                    current_item = item_by_key.get(current_key)
                    audio_bytes = st.session_state.local_avatar_audio.get(current_key)

                    if current_item is None or not current_item["needs_lipsync"]:
                        st.session_state.local_avatar_queue = queue[1:]
                        st.rerun()

                    if not audio_bytes:
                        message = "Thiếu audio để chạy OpenAvatar."
                        _set_local_job(
                            current_key, "Failed", "Lip-sync", error=message
                        )
                        st.session_state.local_job_failed_stage[current_key] = "avatar"
                        st.session_state.local_avatar_queue = queue[1:]
                        st.rerun()

                    _set_local_job(current_key, "Processing", "Lip-sync")

                    avatar_cache_key = _avatar_job_cache_key(
                        current_key,
                        audio_bytes,
                        st.session_state.avatar_upload,
                        avatar_engine,
                    )
                    result = local_gpu_bridge(
                        action="generate",
                        agent_url=openavatar_runtime_url,
                        image_bytes=st.session_state.avatar_upload,
                        audio_bytes=audio_bytes,
                        engine=avatar_engine,
                        request_id=f"scene-{current_key}",
                        cache_key=avatar_cache_key,
                        key=f"openavatar_generate_scene_{current_key}",
                    )

                    if isinstance(result, dict) and result.get("ok"):
                        # As with voice_synthesize, scene identity is guaranteed by
                        # the Streamlit component key. Do not stall the queue merely
                        # because Runtime rewrites request_id.
                        clip = decode_video_result(result)
                        if clip:
                            st.session_state.local_avatar_clips[current_key] = clip
                            st.session_state.local_avatar_hashes[current_key] = avatar_cache_key
                            try:
                                _persist_avatar_cache(
                                    current_key,
                                    avatar_cache_key,
                                    clip,
                                )
                            except Exception as cache_exc:
                                st.warning(
                                    f"Không ghi được avatar cache cho {current_item['label']}: "
                                    f"{cache_exc}"
                                )

                            if result.get("cache_hit"):
                                _set_local_job(
                                    current_key,
                                    "Cached",
                                    "Complete",
                                    cache_source=result.get(
                                        "cache_source", "cache"
                                    ),
                                )
                            else:
                                _set_local_job(current_key, "Done", "Complete")

                            st.session_state.local_job_failed_stage.pop(
                                current_key, None
                            )

                            # IMPORTANT: always advance to the next scene.
                            st.session_state.local_avatar_queue = list(queue[1:])

                            if (
                                not st.session_state.local_avatar_queue
                                and st.session_state.local_batch_mode
                            ):
                                st.session_state.local_batch_mode = False
                                if st.session_state.local_batch_auto_export:
                                    st.session_state.auto_export_requested = True

                            st.rerun()
                        else:
                            message = (
                                "OpenAvatar báo thành công nhưng Browser Bridge "
                                "không trả video_base64."
                            )
                            _set_local_job(
                                current_key, "Failed", "Lip-sync", error=message
                            )
                            st.session_state.local_job_failed_stage[current_key] = "avatar"
                            st.session_state.local_avatar_queue = list(queue[1:])
                            st.session_state.local_batch_mode = False
                            st.error(message)
                            st.rerun()

                    elif isinstance(result, dict) and result.get("ok") is False:
                        message = result.get(
                            "error", "OpenAvatar Runtime xử lý thất bại"
                        )
                        _set_local_job(
                            current_key, "Failed", "Lip-sync", error=message
                        )
                        st.session_state.local_job_failed_stage[current_key] = "avatar"
                        st.session_state.local_avatar_queue = list(queue[1:])
                        st.session_state.local_batch_mode = False
                        st.rerun()

                ready_clip_keys = [
                    key
                    for key in lipsync_keys
                    if key in st.session_state.local_avatar_clips
                ]
                if ready_clip_keys and not st.session_state.local_avatar_queue:
                    clip_key = st.selectbox(
                        "Xem thử clip nhép môi",
                        ready_clip_keys,
                        format_func=lambda key: item_by_key[key]["label"],
                        key="local_avatar_review_scene",
                    )
                    st.video(st.session_state.local_avatar_clips[clip_key])

        create_video_clicked = st.button(
            "Tạo video",
            type="primary",
            use_container_width=True,
        )
        auto_export_now = bool(
            st.session_state.get("auto_export_requested", False)
        )
        if auto_export_now:
            st.success(
                "Đã hoàn tất audio + nhép môi hàng loạt. "
                "Đang tự động ghép video cuối..."
            )

        if create_video_clicked or auto_export_now:
            if len(st.session_state.get("original_slide_images", [])) != len(records):
                st.error(
                    "Chưa có đủ ảnh render gốc của PowerPoint nên không thể xuất video. "
                    "Audio VieNeu/F5/Edge/bản thu không thay đổi slide; hãy sửa lỗi render "
                    "ở tab PowerPoint rồi tải lại file."
                )
                st.stop()
            if voice_engine in {"uploaded", "voice_clone"} and not voice_upload_unlocked:
                st.error("Cần nhập mật khẩu để dùng giọng tải lên.")
                st.stop()
            if voice_engine == "voice_clone":
                if not voice_clone_consent:
                    st.error("Cần xác nhận quyền sử dụng giọng trước khi nhân bản giọng.")
                    st.stop()
                if voice_clone_config is None or not voice_clone_config.endpoint.strip():
                    st.error(
                        "Nguồn giọng đang là ‘Nhân bản giọng từ mẫu (API riêng)’. "
                        "Hãy tải mẫu giọng và nhập endpoint 8009; hoặc đổi Nguồn giọng đọc "
                        "sang VieNeu-TTS local nếu không muốn dùng Voice Clone."
                    )
                    st.stop()
            if presenter_mode == "AI nhép môi qua GPU API" and (ai_avatar_config is None or avatar_image is None):
                st.error("Chế độ AI nhép môi cần ảnh người dẫn và GPU API URL.")
                st.stop()
            if presenter_mode == "AI nhép môi bằng OpenAvatar Runtime":
                export_items = _pipeline_scene_items(records)
                narrated_export_items = [
                    item for item in export_items if item["needs_audio"]
                ]

                missing_audio_keys = [
                    item["key"]
                    for item in narrated_export_items
                    if item["key"] not in st.session_state.local_avatar_audio
                ]
                if missing_audio_keys:
                    item_map = _pipeline_item_map(export_items)
                    st.error(
                        "Chưa có audio cho: "
                        + ", ".join(
                            item_map[key]["label"] for key in missing_audio_keys[:10]
                        )
                        + ". Hãy dùng Resume Job Dashboard để tạo/khôi phục audio."
                    )
                    st.stop()

                missing_clip_keys = [
                    item["key"]
                    for item in narrated_export_items
                    if item["key"] not in st.session_state.local_avatar_clips
                ]
                if missing_clip_keys:
                    item_map = _pipeline_item_map(export_items)
                    st.error(
                        "Chưa tạo đủ clip OpenAvatar cho: "
                        + ", ".join(
                            item_map[key]["label"] for key in missing_clip_keys[:10]
                        )
                    )
                    st.stop()

            invalid = [r["slide"] for r in records if profanity.contains_profanity(r.get("narration", ""))]
            if invalid:
                st.error(f"Không thể render. Lời thuyết minh chứa nội dung không phù hợp tại slide: {invalid}")
            else:
                scenes: list[VideoScene] = []
                images: list[Image.Image] = []
                boundary = st.session_state.get("boundary", {"intro": {"mode":"none"}, "outro": {"mode":"none"}})
                excluded = set()
                for side in ("intro", "outro"):
                    cfg = boundary[side]
                    if cfg.get("mode") == "source_slide" and cfg.get("remove_from_original_position"):
                        excluded.add(cfg.get("source_slide_number"))

                def add_boundary(side: str) -> None:
                    cfg = boundary[side]
                    mode = cfg.get("mode", "none")
                    if mode == "none":
                        return
                    if mode == "source_slide":
                        rec = records[int(cfg["source_slide_number"]) - 1]
                        scenes.append(VideoScene(
                            title=rec["title"],
                            bullets=tuple(rec["bullets"]),
                            narration=apply_dictionary(
                                cfg.get("narration") or rec["narration"],
                                st.session_state.dictionary,
                            ),
                            slide_type=side,
                            source_slide_number=rec["slide"],
                        ))
                        images.append(_require_original_ppt_slide(rec["slide"]))
                    elif mode == "uploaded_image":
                        payload = st.session_state.intro_upload if side == "intro" else st.session_state.outro_upload
                        if payload:
                            scenes.append(VideoScene(
                                title=side.title(),
                                narration=apply_dictionary(cfg.get("narration", ""), st.session_state.dictionary),
                                slide_type=side,
                            ))
                            images.append(Image.open(io.BytesIO(payload)).convert("RGB"))
                    else:
                        scenes.append(VideoScene(
                            title=cfg.get("title", ""),
                            subtitle=cfg.get("subtitle", ""),
                            narration=apply_dictionary(cfg.get("narration", ""), st.session_state.dictionary),
                            slide_type=side,
                        ))
                        images.append(build_boundary_slide(cfg.get("title", ""), cfg.get("subtitle", ""), st.session_state.organization, side))

                add_boundary("intro")
                for rec in records:
                    if rec["slide"] in excluded or rec.get("skip"):
                        continue
                    scenes.append(VideoScene(title=rec["title"], bullets=tuple(rec["bullets"]), narration=apply_dictionary(rec["narration"], st.session_state.dictionary), slide_number=rec["slide"], slide_type=rec["slide_type"], pause_after=rec.get("pause_after", 0.35), source_slide_number=rec["slide"]))
                    images.append(_require_original_ppt_slide(rec["slide"]))
                add_boundary("outro")

                missing_boundary_images = [
                    side
                    for side in ("intro", "outro")
                    if boundary[side].get("mode") == "uploaded_image"
                    and not (st.session_state.intro_upload if side == "intro" else st.session_state.outro_upload)
                ]
                if missing_boundary_images:
                    st.error("Chưa tải ảnh cho slide " + " và ".join(missing_boundary_images) + ".")
                    st.stop()

                # Non-OpenAvatar exports still need local VieNeu audio. Use one
                # Browser Bridge storyboard request so the Cloud Streamlit
                # process never falls back to ``synthesize_scene_audio``.
                browser_vieneu_assets: dict[str, AudioAsset] = {}
                if (
                    voice_engine == "vieneu"
                    and presenter_mode != "AI nhép môi bằng OpenAvatar Runtime"
                    and not vieneu_direct_python
                ):
                    batch_items = [
                        {"key": f"scene-{index}", "text": scene.narration}
                        for index, scene in enumerate(scenes)
                        if scene.narration.strip()
                    ]
                    batch_result = local_gpu_bridge(
                        action="voice_synthesize_batch",
                        agent_url=vieneu_service_url or "http://127.0.0.1:8009",
                        api_key=vieneu_service_api_key,
                        voice_id=voice_id or "default",
                        model="vieneu",
                        voice_style=vieneu_style,
                        storyboard=batch_items,
                        request_id="vieneu-export-batch",
                        key="vieneu_export_batch",
                    )
                    if not isinstance(batch_result, dict):
                        st.session_state.auto_export_requested = True
                        st.info(
                            "Đang gửi storyboard tới VieNeu local service trong trình duyệt. "
                            "Giữ tab này mở; audio sẽ được trả về trước khi ghép video."
                        )
                        st.stop()
                    if batch_result.get("ok") is not True:
                        st.session_state.auto_export_requested = False
                        st.error(
                            "VieNeu local service không tạo được audio: "
                            + str(batch_result.get("error", "Không có chi tiết lỗi."))
                        )
                        st.stop()
                    for item in batch_result.get("items", []):
                        if not isinstance(item, dict) or not item.get("key"):
                            continue
                        try:
                            audio_bytes = base64.b64decode(item.get("audio_base64", ""))
                        except (TypeError, ValueError):
                            audio_bytes = b""
                        if audio_bytes:
                            browser_vieneu_assets[str(item["key"])] = AudioAsset(
                                data=audio_bytes,
                                filename=f"{item['key']}.wav",
                            )
                    missing_browser_audio = [
                        scene_label(scene)
                        for index, scene in enumerate(scenes)
                        if scene.narration.strip()
                        and f"scene-{index}" not in browser_vieneu_assets
                    ]
                    if missing_browser_audio:
                        st.session_state.auto_export_requested = False
                        st.error(
                            "VieNeu local service trả thiếu audio cho: "
                            + ", ".join(missing_browser_audio[:12])
                        )
                        st.stop()

                # IMPORTANT:
                # Với voice clone/VieNeu + OpenAvatar, audio đã chuẩn bị trong
                # Resume Job Dashboard phải được dùng lại ở bước xuất. Không
                # synthesize lại từ Streamlit server sau khi Browser/CPU local đã
                # tạo xong audio.
                if (
                    presenter_mode == "AI nhép môi bằng OpenAvatar Runtime"
                    and voice_engine in {"voice_clone", "vieneu"}
                ):
                    scene_audio_assets = []
                    for scene in scenes:
                        slide_key = _scene_runtime_key(scene)
                        override_asset = (
                            st.session_state.scene_audio_overrides.get(slide_key)
                            if slide_key is not None
                            else None
                        )
                        audio_bytes = (
                            override_asset.data
                            if isinstance(override_asset, AudioAsset)
                            else (
                                st.session_state.local_avatar_audio.get(slide_key)
                                if slide_key is not None
                                else None
                            )
                        )

                        # Boundary scene có thể có bản thu upload riêng.
                        if audio_bytes is None:
                            uploaded_asset = audio_asset_for_scene(
                                scene, recorded_voice_assets
                            )
                            if uploaded_asset is not None:
                                scene_audio_assets.append(uploaded_asset)
                                continue

                        scene_audio_assets.append(
                            AudioAsset(
                                data=audio_bytes,
                                filename=(
                                    f"{slide_key}.wav"
                                    if isinstance(slide_key, str)
                                    else f"slide_{int(slide_key):03d}.wav"
                                ),
                            )
                            if audio_bytes
                            else None
                        )

                    missing_local_audio = [
                        scene_label(scene)
                        for scene, audio_asset in zip(scenes, scene_audio_assets)
                        if scene.narration.strip() and audio_asset is None
                    ]
                    if missing_local_audio:
                        st.error(
                            "Chưa có audio local cho: "
                            + ", ".join(missing_local_audio[:12])
                            + ". Hãy chạy Resume Job Dashboard để tạo/khôi phục audio trước khi Tạo video."
                        )
                        st.stop()
                elif browser_vieneu_assets:
                    scene_audio_assets = [
                        browser_vieneu_assets.get(f"scene-{index}")
                        or audio_asset_for_scene(scene, recorded_voice_assets)
                        for index, scene in enumerate(scenes)
                    ]
                else:
                    scene_audio_assets = [
                        audio_asset_for_scene(scene, recorded_voice_assets)
                        for scene in scenes
                    ]

                if voice_engine == "uploaded":
                    missing_audio = [
                        scene_label(scene)
                        for scene, audio_asset in zip(scenes, scene_audio_assets)
                        if scene.narration.strip() and audio_asset is None
                    ]
                    if missing_audio:
                        st.error(
                            "Thiếu bản thu thật cho: "
                            + ", ".join(missing_audio[:12])
                            + ". Đặt tên file theo slide_001.mp3 hoặc intro.mp3/outro.mp3."
                        )
                        st.stop()

                invalid_scene_narrations = [
                    scene_label(scene)
                    for scene in scenes
                    if profanity.contains_profanity(scene.narration)
                ]
                if invalid_scene_narrations:
                    st.error(
                        "Không thể render. Lời thuyết minh chứa nội dung không phù hợp tại: "
                        + ", ".join(invalid_scene_narrations[:12])
                    )
                    st.stop()

                try:
                    if (
                        presenter_mode == "AI nhép môi bằng OpenAvatar Runtime"
                        and voice_engine in {"voice_clone", "vieneu"}
                    ):
                        st.caption(
                            "Export mode: pre-generated local audio — không gọi lại engine local từ Streamlit Cloud."
                        )

                    with st.spinner(
                        "Đang ghép video từ audio/clip đã chuẩn bị..."
                        if presenter_mode == "AI nhép môi bằng OpenAvatar Runtime"
                        else "Đang tạo audio từng slide và ghép video..."
                    ):
                        temp_dir = Path(tempfile.mkdtemp(prefix="ppt_video_studio_"))
                        video_path = temp_dir / "ppt_video.mp4"
                        srt_path = temp_dir / "ppt_video.srt"
                        local_avatar_videos_for_export = None
                        if presenter_mode == "AI nhép môi bằng OpenAvatar Runtime":
                            local_avatar_videos_for_export = [
                                st.session_state.local_avatar_clips.get(
                                    _scene_runtime_key(scene)
                                )
                                for scene in scenes
                            ]
                        # Export MUST NOT call localhost:8009 from Streamlit Cloud.
                        # In OpenAvatar + voice_clone mode, every narrated scene already
                        # has a browser-generated AudioAsset. Tell the exporter to treat
                        # those assets as uploaded/pre-generated audio so it never invokes
                        # the voice-clone client again.
                        export_voice_engine = voice_engine
                        export_voice_clone_config = voice_clone_config
                        if (
                            presenter_mode == "AI nhép môi bằng OpenAvatar Runtime"
                            and voice_engine in {"voice_clone", "vieneu"}
                        ) or (
                            voice_engine == "vieneu"
                            and not vieneu_direct_python
                            and bool(browser_vieneu_assets)
                        ):
                            export_voice_engine = "uploaded"
                            export_voice_clone_config = None

                        output, _, srt_text = export_storyboard_video(
                            scenes, video_path, fps=fps, with_voice=True,
                            voice_engine=export_voice_engine, voice_id=voice_id,
                            voice_rate=voice_rate,
                            vieneu_style=vieneu_style,
                            voice_clone_config=export_voice_clone_config,
                            scene_audio_assets=scene_audio_assets,
                            slide_images=images, burn_subtitles=burn_subtitles,
                            subtitle_position=subtitle_position, subtitle_font_size=subtitle_font_size,
                            subtitle_background_color=subtitle_background_color,
                            subtitle_text_color=subtitle_text_color,
                            subtitle_box_width_percent=subtitle_box_width_percent,
                            subtitle_alignment=subtitle_alignment,
                            preserve_original_slide=preserve_original_slide,
                            srt_path=srt_path, avatar_image=avatar_image,
                            avatar_position=avatar_position, avatar_size_percent=avatar_size_percent,
                            avatar_shape=avatar_shape, avatar_border_width=avatar_border_width,
                            avatar_talking_effect=avatar_talking_effect,
                            ai_avatar_config=ai_avatar_config,
                            local_avatar_videos=local_avatar_videos_for_export,
                        )
                        project = {
                            "project_version": 1,
                            "source_file_name": st.session_state.ppt_name,
                            "organization": st.session_state.organization,
                            "voice": {
                                "source": voice_engine,
                                "voice_id": voice_id if voice_engine in {"edge", "vieneu"} else None,
                                "voice_rate": voice_rate if voice_engine == "edge" else None,
                                "voice_style": vieneu_style if voice_engine == "vieneu" else None,
                                "voice_clone_model": voice_clone_config.model if voice_clone_config else None,
                                "has_recorded_audio": bool(recorded_voice_assets),
                            },
                            "boundary": boundary,
                            "dictionary": json.loads(dictionary_json(st.session_state.dictionary)),
                            "storyboard": records,
                        }
                        st.session_state.auto_export_requested = False
                        st.session_state.local_batch_auto_export = False
                        st.session_state.local_batch_mode = False
                        st.success("Đã tạo video tổng hợp hoàn chỉnh.")
                        st.video(output.read_bytes())
                        d1, d2, d3, d4 = st.columns(4)
                        d1.download_button("Tải MP4", output.read_bytes(), "ppt_video.mp4", "video/mp4")
                        d2.download_button("Tải SRT", srt_text.encode("utf-8"), "ppt_video.srt", "text/plain")
                        script_text = "\n\n".join(
                            f"{scene_label(scene).capitalize()}: {scene.narration}"
                            for scene in scenes
                            if scene.narration.strip()
                        )
                        d3.download_button("Tải script", script_text.encode("utf-8"), "narration.txt", "text/plain")
                        d4.download_button("Tải project", json.dumps(project, ensure_ascii=False, indent=2).encode("utf-8"), "ppt_video_project.json", "application/json")
                except Exception as exc:
                    st.error(f"Không tạo được video: {exc}")
                    if auto_export_now:
                        st.warning(
                            "All-in-One vẫn đang chờ xuất video. "
                            "Sửa lỗi cấu hình rồi app sẽ thử xuất lại; "
                            "audio/clip đã render không bị mất."
                        )
