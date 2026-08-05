from __future__ import annotations

import io
import os
import json
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
from src.video_export import VideoScene, export_storyboard_video, synthesize_edge_tts_audio
from src.avatar_api import AvatarApiConfig, check_avatar_api, generate_talking_head
from src.local_gpu_bridge import local_gpu_bridge, decode_video_result

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

    if uploaded and (uploaded.name != st.session_state.ppt_name or not st.session_state.records):
        try:
            payload = uploaded.getvalue()
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
    else:
        st.subheader("Cấu hình slide mở đầu")
        intro_mode_label = st.radio("Nguồn mở đầu", ["Không thêm", "Chọn slide trong PowerPoint", "Slide mặc định", "Tải ảnh riêng"], horizontal=True, key="intro_mode_label")
        intro_mode = {"Không thêm":"none", "Chọn slide trong PowerPoint":"source_slide", "Slide mặc định":"system_default", "Tải ảnh riêng":"uploaded_image"}[intro_mode_label]
        intro_index = None
        intro_title = st.session_state.records[0]["title"]
        intro_subtitle = st.session_state.organization
        remove_intro = True
        if intro_mode == "source_slide":
            intro_index = st.selectbox("Chọn slide mở đầu", [r["slide"] for r in records], format_func=lambda n: f"Slide {n} — {records[n-1]['title']}")
            remove_intro = st.checkbox("Không lặp lại slide này ở vị trí cũ", value=True, key="remove_intro")
        elif intro_mode == "system_default":
            intro_title = st.text_input("Tiêu đề mở đầu", value=intro_title, key="intro_title")
            intro_subtitle = st.text_input("Phụ đề mở đầu", value=intro_subtitle, key="intro_subtitle")
        elif intro_mode == "uploaded_image":
            intro_file = st.file_uploader("Ảnh mở đầu", type=["png", "jpg", "jpeg"], key="intro_image")
            if intro_file:
                st.session_state.intro_upload = intro_file.getvalue()

        st.subheader("Cấu hình slide kết thúc")
        outro_mode_label = st.radio("Nguồn kết thúc", ["Không thêm", "Chọn slide trong PowerPoint", "Slide mặc định", "Tải ảnh riêng"], horizontal=True, key="outro_mode_label")
        outro_mode = {"Không thêm":"none", "Chọn slide trong PowerPoint":"source_slide", "Slide mặc định":"system_default", "Tải ảnh riêng":"uploaded_image"}[outro_mode_label]
        outro_index = None
        outro_title = "Trân trọng cảm ơn"
        outro_subtitle = st.session_state.organization
        remove_outro = True
        if outro_mode == "source_slide":
            outro_index = st.selectbox("Chọn slide kết thúc", [r["slide"] for r in records], index=len(records)-1, format_func=lambda n: f"Slide {n} — {records[n-1]['title']}")
            remove_outro = st.checkbox("Không lặp lại slide này ở vị trí cũ", value=True, key="remove_outro")
        elif outro_mode == "system_default":
            outro_title = st.text_input("Tiêu đề kết thúc", value=outro_title, key="outro_title")
            outro_subtitle = st.text_input("Phụ đề kết thúc", value=outro_subtitle, key="outro_subtitle")
        elif outro_mode == "uploaded_image":
            outro_file = st.file_uploader("Ảnh kết thúc", type=["png", "jpg", "jpeg"], key="outro_image")
            if outro_file:
                st.session_state.outro_upload = outro_file.getvalue()

        st.session_state.boundary = {
            "intro": asdict(BoundarySlideConfig(intro_mode, intro_index, remove_intro, intro_title, intro_subtitle, f"Xin chào quý anh chị. Nội dung trình bày hôm nay là {intro_title}.")),
            "outro": asdict(BoundarySlideConfig(outro_mode, outro_index, remove_outro, outro_title, outro_subtitle, "Nội dung trình bày xin được kết thúc tại đây. Trân trọng cảm ơn quý anh chị đã theo dõi.")),
        }

        st.divider()
        st.subheader("Biên tập storyboard")
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
            records[i]["pause_after"] = float(row["Nghỉ sau (giây)"])

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
        c1, c2, c3 = st.columns(3)
        voice_id = c1.selectbox("Giọng đọc", ["vi-VN-HoaiMyNeural", "vi-VN-NamMinhNeural"])
        voice_rate = c2.selectbox("Tốc độ", ["-10%", "-5%", "+0%", "+5%", "+10%"], index=2)
        fps = c3.selectbox("FPS", [12, 15, 24], index=1)
        burn_subtitles = st.checkbox("Đốt phụ đề vào video", value=True)
        subtitle_position = st.selectbox("Vị trí phụ đề", ["Dưới", "Giữa"])
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
                                    synthesize_edge_tts_audio(
                                        [VideoScene(title="Preview", narration=preview_text)],
                                        audio_path, voice=voice_id, rate=voice_rate,
                                    )
                                    if not audio_path.exists():
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
                    st.session_state.local_avatar_audio = {}
                    st.session_state.local_avatar_clips = {}
                    st.session_state.local_avatar_queue = []
                    prep_dir = Path(tempfile.mkdtemp(prefix="local_avatar_audio_"))
                    for rec in records:
                        if rec.get("skip") or not rec.get("narration", "").strip():
                            continue
                        audio_path = prep_dir / f"slide_{rec['slide']:03}.mp3"
                        text_value = apply_dictionary(rec["narration"], st.session_state.dictionary)
                        synthesize_edge_tts_audio([VideoScene(title=rec["title"], narration=text_value)], audio_path, voice=voice_id, rate=voice_rate)
                        if audio_path.exists():
                            st.session_state.local_avatar_audio[rec["slide"]] = audio_path.read_bytes()
                            st.session_state.local_avatar_queue.append(rec["slide"])
                    st.rerun()

                queue = st.session_state.local_avatar_queue
                if queue:
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
                elif st.session_state.local_avatar_audio:
                    done = len(st.session_state.local_avatar_clips)
                    total = len(st.session_state.local_avatar_audio)
                    st.success(f"Đã tạo {done}/{total} clip avatar bằng OpenAvatar Runtime.")

        if st.button("Tạo video", type="primary", use_container_width=True):
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
                        scenes.append(VideoScene(title=rec["title"], bullets=tuple(rec["bullets"]), narration=rec["narration"], slide_type=side, source_slide_number=rec["slide"]))
                        images.append(st.session_state.original_slide_images[rec["slide"] - 1].copy() if st.session_state.original_slide_images else build_content_slide(rec["title"], rec["bullets"], st.session_state.organization, side))
                    elif mode == "uploaded_image":
                        payload = st.session_state.intro_upload if side == "intro" else st.session_state.outro_upload
                        if payload:
                            scenes.append(VideoScene(title=side.title(), narration=cfg.get("narration", ""), slide_type=side))
                            images.append(Image.open(io.BytesIO(payload)).convert("RGB"))
                    else:
                        scenes.append(VideoScene(title=cfg.get("title", ""), subtitle=cfg.get("subtitle", ""), narration=cfg.get("narration", ""), slide_type=side))
                        images.append(build_boundary_slide(cfg.get("title", ""), cfg.get("subtitle", ""), st.session_state.organization, side))

                add_boundary("intro")
                for rec in records:
                    if rec["slide"] in excluded or rec.get("skip"):
                        continue
                    scenes.append(VideoScene(title=rec["title"], bullets=tuple(rec["bullets"]), narration=apply_dictionary(rec["narration"], st.session_state.dictionary), slide_number=rec["slide"], slide_type=rec["slide_type"], pause_after=rec.get("pause_after", 0.35), source_slide_number=rec["slide"]))
                    images.append(st.session_state.original_slide_images[rec["slide"] - 1].copy() if st.session_state.original_slide_images else build_content_slide(rec["title"], rec["bullets"], st.session_state.organization))
                add_boundary("outro")

                try:
                    with st.spinner("Đang tạo audio từng slide và ghép video..."):
                        temp_dir = Path(tempfile.mkdtemp(prefix="ppt_video_studio_"))
                        video_path = temp_dir / "ppt_video.mp4"
                        srt_path = temp_dir / "ppt_video.srt"
                        local_avatar_videos_for_export = None
                        if presenter_mode == "AI nhép môi bằng OpenAvatar Runtime":
                            local_avatar_videos_for_export = [st.session_state.local_avatar_clips.get(scene.source_slide_number) for scene in scenes]
                        output, _, srt_text = export_storyboard_video(
                            scenes, video_path, fps=fps, with_voice=True, voice_id=voice_id,
                            voice_rate=voice_rate, slide_images=images, burn_subtitles=burn_subtitles,
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
                            "boundary": boundary,
                            "dictionary": json.loads(dictionary_json(st.session_state.dictionary)),
                            "storyboard": records,
                        }
                        st.success("Đã tạo video.")
                        st.video(output.read_bytes())
                        d1, d2, d3, d4 = st.columns(4)
                        d1.download_button("Tải MP4", output.read_bytes(), "ppt_video.mp4", "video/mp4")
                        d2.download_button("Tải SRT", srt_text.encode("utf-8"), "ppt_video.srt", "text/plain")
                        script_text = "\n\n".join(f"Slide {r['slide']}: {r['narration']}" for r in records if not r.get("skip"))
                        d3.download_button("Tải script", script_text.encode("utf-8"), "narration.txt", "text/plain")
                        d4.download_button("Tải project", json.dumps(project, ensure_ascii=False, indent=2).encode("utf-8"), "ppt_video_project.json", "application/json")
                except Exception as exc:
                    st.error(f"Không tạo được video: {exc}")
