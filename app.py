from __future__ import annotations

import io
import os
import json
import re
import tempfile
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
from src.slide_builder import build_boundary_slide, build_content_slide
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

# OpenAvatar SDK chỉ dùng khi Streamlit chạy hoàn toàn trên máy local.
# Khi deploy trên Streamlit Cloud, phải dùng Browser Bridge vì Python server
# trên cloud không thể truy cập localhost của máy người dùng.
try:
    from openavatar_sdk import OpenAvatarClient
except ImportError:
    OpenAvatarClient = None

ROOT = Path(__file__).parent
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
    ):
        st.session_state.pop(key, None)
    st.session_state.intro_upload = None
    st.session_state.outro_upload = None
    st.session_state.local_avatar_clips = {}
    st.session_state.local_avatar_audio = {}
    st.session_state.local_avatar_queue = []


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
        st.info("App dùng python-pptx để lấy nội dung thuyết minh và dùng LibreOffice để render nguyên hình slide. Nền, ảnh, biểu đồ, SmartArt và bố cục được giữ ở dạng tĩnh; animation và video nhúng không được phát lại.")

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
                    st.session_state.render_backend = "Dựng lại từ text"
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
            st.warning(
                "Không render được hình gốc nên app đang dựng lại slide từ text. "
                f"Chi tiết: {st.session_state.render_warning}"
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
            ["AI tiếng Việt", "Bản thu thật theo từng slide", "Nhân bản giọng từ mẫu (API riêng)"],
            horizontal=True,
            key="voice_source",
        )
        voice_engine = "edge"
        voice_id = "vi-VN-HoaiMyNeural"
        voice_rate = "+0%"
        voice_clone_config = None
        voice_clone_consent = False
        recorded_voice_assets: dict[int | str, AudioAsset] = {}
        voice_upload_unlocked = voice_source == "AI tiếng Việt"

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
                    help="Dùng f5-tts cho Local Voice Clone Service.",
                    key="voice_clone_model",
                )
                clone_api_key = st.text_input(
                    "API key (nếu có)",
                    value=os.getenv("VOICE_CLONE_API_KEY", ""),
                    type="password",
                    key="voice_clone_api_key",
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
                        upload_password=str(st.session_state.get("voice_upload_password", "")),
                        verify_ssl=clone_verify_ssl,
                    )
                st.info(
                    "Local Voice Clone Service mặc định chạy tại 127.0.0.1:8009: audio và text không rời máy. "
                    "Mẫu giọng chỉ được giữ trong phiên xuất và không được đưa vào file project tải xuống."
                )
            else:
                st.info("Nhập đúng mật khẩu ở trên để tải mẫu giọng và cấu hình nhân bản giọng.")

        render_col, subtitle_col, fps_col = st.columns(3)
        burn_subtitles = render_col.checkbox("Đốt phụ đề vào video", value=True)
        subtitle_position = subtitle_col.selectbox("Vị trí phụ đề", ["Dưới", "Giữa"])
        fps = fps_col.selectbox("FPS", [12, 15, 24], index=1)
        subtitle_font_size = st.slider("Cỡ chữ phụ đề", 20, 40, 28)

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
                    timeout_seconds=600,
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
                if st.button("Chuẩn bị audio cho OpenAvatar Runtime", use_container_width=True):
                    try:
                        st.session_state.local_avatar_audio = {}
                        st.session_state.local_avatar_clips = {}
                        st.session_state.local_avatar_queue = []
                        st.session_state.local_voice_queue = []

                        selected_slides = [
                            rec["slide"]
                            for rec in records
                            if not rec.get("skip") and rec.get("narration", "").strip()
                        ]

                        if voice_engine == "voice_clone":
                            if voice_clone_config is None:
                                raise RuntimeError("Chưa cấu hình Voice Clone.")
                            # IMPORTANT: do not synthesize on Streamlit Cloud.
                            # Queue browser-side calls to localhost:8009 instead.
                            st.session_state.local_voice_queue = selected_slides
                        else:
                            prep_dir = Path(tempfile.mkdtemp(prefix="local_avatar_audio_"))
                            for rec in records:
                                if rec.get("skip") or not rec.get("narration", "").strip():
                                    continue
                                audio_path = prep_dir / f"slide_{rec['slide']:03}.mp3"
                                text_value = apply_dictionary(rec["narration"], st.session_state.dictionary)
                                source_scene = VideoScene(
                                    title=rec["title"],
                                    narration=text_value,
                                    source_slide_number=rec["slide"],
                                )
                                prepared_audio = synthesize_scene_audio(
                                    source_scene,
                                    audio_path,
                                    voice_engine=voice_engine,
                                    voice_id=voice_id,
                                    voice_rate=voice_rate,
                                    voice_clone_config=voice_clone_config,
                                    uploaded_audio=audio_asset_for_scene(source_scene, recorded_voice_assets),
                                )
                                if prepared_audio and prepared_audio.exists():
                                    st.session_state.local_avatar_audio[rec["slide"]] = prepared_audio.read_bytes()

                            st.session_state.local_avatar_queue = selected_slides

                        st.rerun()
                    except Exception as exc:
                        st.error(f"Không chuẩn bị được audio cho OpenAvatar Runtime: {exc}")

                # Voice Clone must run in the user's browser when Streamlit is on Cloud,
                # because localhost:8009 belongs to the user's PC, not Streamlit Cloud.
                voice_queue = st.session_state.local_voice_queue
                if voice_queue:
                    current_slide = voice_queue[0]
                    current_rec = next(
                        (rec for rec in records if rec["slide"] == current_slide),
                        None,
                    )
                    if current_rec is None:
                        st.session_state.local_voice_queue = voice_queue[1:]
                        st.rerun()

                    clone_endpoint = voice_clone_config.endpoint.rstrip("/")
                    marker = "/v1/voice-clone/synthesize"
                    voice_base_url = (
                        clone_endpoint[:-len(marker)]
                        if clone_endpoint.endswith(marker)
                        else clone_endpoint
                    )

                    voice_progress_done = len(st.session_state.local_avatar_audio)
                    voice_progress_total = voice_progress_done + len(voice_queue)
                    st.progress(
                        voice_progress_done / max(1, voice_progress_total),
                        text=f"F5-TTS local đang tạo audio cho slide {current_slide}",
                    )

                    voice_result = local_gpu_bridge(
                        action="voice_synthesize",
                        agent_url=voice_base_url,
                        reference_audio_bytes=voice_clone_config.reference_audio,
                        reference_audio_filename=voice_clone_config.reference_filename,
                        text=apply_dictionary(
                            current_rec["narration"],
                            st.session_state.dictionary,
                        ),
                        reference_text=voice_clone_config.reference_transcript or "",
                        voice_id="default",
                        model=voice_clone_config.model or "f5-tts",
                        api_key=voice_clone_config.api_key or "",
                        request_id=f"voice-slide-{current_slide}",
                        key=f"voice_clone_{current_slide}",
                    )

                    if (
                        isinstance(voice_result, dict)
                        and voice_result.get("ok")
                        and voice_result.get("request_id") == f"voice-slide-{current_slide}"
                    ):
                        audio_bytes = decode_audio_result(voice_result)
                        if audio_bytes:
                            st.session_state.local_avatar_audio[current_slide] = audio_bytes
                            st.session_state.local_voice_queue = voice_queue[1:]
                            if not st.session_state.local_voice_queue:
                                st.session_state.local_avatar_queue = list(
                                    st.session_state.local_avatar_audio.keys()
                                )
                            st.rerun()
                    elif isinstance(voice_result, dict) and voice_result.get("ok") is False:
                        st.error(
                            voice_result.get(
                                "error",
                                "Local Voice Clone xử lý thất bại",
                            )
                        )

                queue = st.session_state.local_avatar_queue
                if not st.session_state.local_voice_queue and queue:
                    current_slide = queue[0]
                    st.progress((len(st.session_state.local_avatar_clips)) / max(1, len(st.session_state.local_avatar_audio)), text=f"OpenAvatar Runtime đang tạo avatar cho slide {current_slide}")
                    result = local_gpu_bridge(
                        action="generate", agent_url=openavatar_runtime_url,
                        image_bytes=st.session_state.avatar_upload,
                        audio_bytes=st.session_state.local_avatar_audio[current_slide],
                        engine=avatar_engine, request_id=f"slide-{current_slide}",
                        key=f"openavatar_generate_{current_slide}",
                    )
                    if isinstance(result, dict) and result.get("ok") and result.get("request_id") == f"slide-{current_slide}":
                        clip = decode_video_result(result)
                        if clip:
                            st.session_state.local_avatar_clips[current_slide] = clip
                            st.session_state.local_avatar_queue = queue[1:]
                            st.rerun()
                    elif isinstance(result, dict) and result.get("ok") is False:
                        st.error(result.get("error", "OpenAvatar Runtime xử lý thất bại"))
                elif (
                    not st.session_state.local_voice_queue
                    and st.session_state.local_avatar_audio
                    and not st.session_state.local_avatar_queue
                ):
                    done = len(st.session_state.local_avatar_clips)
                    total = len(st.session_state.local_avatar_audio)
                    st.success(f"Đã tạo {done}/{total} clip avatar bằng OpenAvatar Runtime.")

        if st.button("Tạo video", type="primary", use_container_width=True):
            if voice_engine in {"uploaded", "voice_clone"} and not voice_upload_unlocked:
                st.error("Cần nhập mật khẩu để dùng giọng tải lên.")
                st.stop()
            if voice_engine == "voice_clone":
                if not voice_clone_consent:
                    st.error("Cần xác nhận quyền sử dụng giọng trước khi nhân bản giọng.")
                    st.stop()
                if voice_clone_config is None or not voice_clone_config.endpoint.strip():
                    st.error("Cần tải mẫu giọng và nhập Voice-clone API endpoint trước khi tạo video.")
                    st.stop()
            if presenter_mode == "AI nhép môi qua GPU API" and (ai_avatar_config is None or avatar_image is None):
                st.error("Chế độ AI nhép môi cần ảnh người dẫn và GPU API URL.")
                st.stop()
            if presenter_mode == "AI nhép môi bằng OpenAvatar Runtime":
                expected = [r["slide"] for r in records if not r.get("skip") and r.get("narration", "").strip()]
                missing = [slide for slide in expected if slide not in st.session_state.local_avatar_clips]
                if missing:
                    st.error(f"Chưa tạo đủ clip OpenAvatar Runtime. Còn thiếu slide: {missing[:10]}")
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
                        images.append(st.session_state.original_slide_images[rec["slide"] - 1].copy() if st.session_state.original_slide_images else build_content_slide(rec["title"], rec["bullets"], st.session_state.organization, side))
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
                    images.append(st.session_state.original_slide_images[rec["slide"] - 1].copy() if st.session_state.original_slide_images else build_content_slide(rec["title"], rec["bullets"], st.session_state.organization))
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

                scene_audio_assets = [
                    audio_asset_for_scene(scene, recorded_voice_assets) for scene in scenes
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
                    with st.spinner("Đang tạo audio từng slide và ghép video..."):
                        temp_dir = Path(tempfile.mkdtemp(prefix="ppt_video_studio_"))
                        video_path = temp_dir / "ppt_video.mp4"
                        srt_path = temp_dir / "ppt_video.srt"
                        local_avatar_videos_for_export = None
                        if presenter_mode == "AI nhép môi bằng OpenAvatar Runtime":
                            local_avatar_videos_for_export = [st.session_state.local_avatar_clips.get(scene.source_slide_number) for scene in scenes]
                        output, _, srt_text = export_storyboard_video(
                            scenes, video_path, fps=fps, with_voice=True,
                            voice_engine=voice_engine, voice_id=voice_id,
                            voice_rate=voice_rate, voice_clone_config=voice_clone_config,
                            scene_audio_assets=scene_audio_assets,
                            slide_images=images, burn_subtitles=burn_subtitles,
                            subtitle_position=subtitle_position, subtitle_font_size=subtitle_font_size,
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
                                "voice_id": voice_id if voice_engine == "edge" else None,
                                "voice_rate": voice_rate if voice_engine == "edge" else None,
                                "voice_clone_model": voice_clone_config.model if voice_clone_config else None,
                                "has_recorded_audio": bool(recorded_voice_assets),
                            },
                            "boundary": boundary,
                            "dictionary": json.loads(dictionary_json(st.session_state.dictionary)),
                            "storyboard": records,
                        }
                        st.success("Đã tạo video.")
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
