from __future__ import annotations

import hmac
import json
import shutil
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import quote

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse

from config.settings import Settings
from services.access_control import enforce_uploaded_voice_consent
from services.audio_service import SUPPORTED_REFERENCE_FORMATS
from services.errors import VoiceCloneServiceError
from services.synthesis_service import SynthesisResult, SynthesisService

from .compatibility import CompatibilitySynthesisInput, parse_compatibility_request
from .schemas import StoryboardSynthesisRequest


def _relative_to_root(path: Path, settings: Settings) -> str:
    try:
        return path.resolve().relative_to(settings.root.resolve()).as_posix()
    except ValueError:
        return path.name


def _response_for_result(result: SynthesisResult, request: Request, settings: Settings) -> dict[str, Any]:
    filename = quote(result.audio_path.name)
    body = result.to_dict(audio_url=f"{str(request.base_url).rstrip('/')}/audio/{filename}")
    body["audio_path"] = _relative_to_root(result.audio_path, settings)
    return body


def _assert_local_api_key(request: Request, settings: Settings, body_api_key: str = "") -> None:
    """Enforce an optional token without recording it in any log."""

    if not settings.local_api_key:
        return
    supplied = request.headers.get("x-api-key", "").strip() or body_api_key.strip()
    authorization = request.headers.get("authorization", "").strip()
    if authorization.lower().startswith("bearer "):
        supplied = authorization[7:].strip()
    if not supplied or not hmac.compare_digest(supplied, settings.local_api_key):
        raise VoiceCloneServiceError("UNAUTHORIZED", "Thiếu hoặc sai LOCAL_API_KEY.", status_code=401)


async def _persist_reference_upload(payload: CompatibilitySynthesisInput, settings: Settings) -> tuple[Path | None, Path | None]:
    """Copy an uploaded reference to a request-local temp folder.

    The caller owns the returned folder and must remove it once synthesis has
    completed.  A filename is never trusted as a path.
    """

    upload = payload.reference_audio
    if upload is None or not upload.filename:
        return None, None
    suffix = Path(upload.filename).suffix.lower()
    if suffix not in SUPPORTED_REFERENCE_FORMATS:
        allowed = ", ".join(sorted(SUPPORTED_REFERENCE_FORMATS))
        raise VoiceCloneServiceError("UNSUPPORTED_AUDIO_FORMAT", f"Chỉ hỗ trợ audio mẫu: {allowed}.")
    folder = settings.temp_dir / f"upload_{uuid.uuid4().hex}"
    folder.mkdir(parents=True, exist_ok=False)
    target = folder / f"reference{suffix}"
    total = 0
    try:
        with target.open("wb") as handle:
            while block := await upload.read(1_048_576):
                total += len(block)
                if total > 100 * 1024 * 1024:
                    raise VoiceCloneServiceError("REFERENCE_AUDIO_INVALID", "File giọng mẫu vượt quá 100 MB.")
                handle.write(block)
    except Exception:
        shutil.rmtree(folder, ignore_errors=True)
        raise
    finally:
        await upload.close()
    if total == 0:
        raise VoiceCloneServiceError("REFERENCE_AUDIO_INVALID", "File giọng mẫu rỗng.")
    return target, folder


def _ui_html() -> str:
    """A dependency-free local UI; no CDN, telemetry, or cloud request."""

    return """<!doctype html>
<html lang="vi"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Local Voice Clone</title><style>
body{font-family:system-ui,-apple-system,Segoe UI,sans-serif;background:#f5f7fb;color:#182233;margin:0}
main{max-width:850px;margin:32px auto;padding:28px;background:#fff;border-radius:14px;box-shadow:0 8px 30px #18223317}
h1{margin-top:0} label{display:block;font-weight:650;margin-top:15px} input,select,textarea,button{box-sizing:border-box;width:100%;padding:10px;margin-top:5px;border:1px solid #ccd4e0;border-radius:8px;font:inherit}
textarea{min-height:155px;resize:vertical} .grid{display:grid;grid-template-columns:1fr 1fr;gap:14px}.help{color:#536274;font-size:.9rem}.status{padding:10px;border-radius:8px;background:#edf5ff;margin:14px 0}.error{background:#fff0f0;color:#9e1c1c}.success{background:#effaf2;color:#176b36}button{background:#145ce6;color:white;border:0;font-weight:700;cursor:pointer;margin-top:20px}audio{width:100%;margin-top:14px}a{color:#145ce6}
</style></head><body><main>
<h1>Local Voice Clone</h1><p class="help">Chạy hoàn toàn trên máy này. Không tải audio hoặc nội dung lên cloud.</p>
<div id="health" class="status">Đang kiểm tra service…</div>
<form id="form"><div class="grid"><label>Model<select name="model"><option value="vieneu-clone">VieNeu v3 Turbo — clone tiếng Việt</option><option value="f5-tts">F5-TTS (Trung/Anh)</option><option value="dummy">Dummy (chỉ kiểm thử)</option></select></label>
<label>Voice profile<select id="voice" name="voice_id"><option value="">-- dùng audio upload --</option></select></label></div>
<label>Reference audio (WAV, MP3, M4A, AAC, OGG, FLAC)<input type="file" name="reference_audio" accept=".wav,.mp3,.m4a,.aac,.ogg,.flac,audio/*"></label>
<label>Mật khẩu để dùng giọng tải lên<input id="uploadPassword" type="password" autocomplete="current-password"></label>
<label class="help"><input name="voice_use_consent" type="checkbox" value="true" style="width:auto"> Tôi xác nhận có quyền sử dụng giọng nói mẫu này.</label>
<label>Reference transcript <span class="help">(khuyến nghị nhập đúng lời trong file mẫu)</span><textarea name="reference_text" style="min-height:72px"></textarea></label>
<label>Vùng giọng / phương ngữ<select name="voice_region"><option value="auto">Tự động — giữ vùng theo file mẫu</option><option value="nam">Miền Nam</option><option value="bac">Miền Bắc</option><option value="trung">Miền Trung</option></select></label>
<label>Text tiếng Việt cần đọc<textarea name="text" required placeholder="Kính thưa quý anh chị…"></textarea></label>
<div class="grid"><label>Speed<input name="speed" type="number" min="0.5" max="2" step="0.05" value="1"></label><label>Định dạng<select name="output_format"><option value="wav">WAV (khuyến nghị cho Wav2Lip)</option><option value="mp3">MP3 192 kbps</option></select></label></div>
<input name="language" type="hidden" value="vi"><button id="generate" type="submit">Generate local audio</button></form>
<div id="result"></div></main><script>
const health=document.getElementById('health'), voice=document.getElementById('voice'), result=document.getElementById('result');
async function init(){try{const [h,v]=await Promise.all([fetch('/health').then(x=>x.json()),fetch('/v1/voices').then(x=>x.json())]);const note=h.device?.warning?` ${h.device.warning}`:'';health.textContent=`Service ${h.status}; engine: ${h.engine_status?.loaded?'ready':'chưa nạp model'}.${note}`; if(!h.engine_status?.loaded)health.className='status error'; (v.voices||[]).forEach(x=>{const o=document.createElement('option');o.value=x.id;o.textContent=`${x.id}${x.available?'':' (thiếu reference.wav)'}`;voice.appendChild(o)})}catch(e){health.textContent='Không kết nối được service local: '+e;health.className='status error'}}
document.getElementById('form').addEventListener('submit',async(e)=>{e.preventDefault();result.innerHTML='';const b=document.getElementById('generate');b.disabled=true;b.textContent='Đang tổng hợp…';try{const response=await fetch('/v1/voice-clone/synthesize-upload',{method:'POST',headers:{'X-Voice-Upload-Password':document.getElementById('uploadPassword').value},body:new FormData(e.target)});const data=await response.json();if(!response.ok||!data.success)throw new Error(data.message||data.detail||'Synthesis failed');const msg=document.createElement('div');msg.className='status success';msg.textContent=`Xong: ${data.duration_seconds}s${data.cache_hit?' (cache)':''}`;const audio=document.createElement('audio');audio.controls=true;audio.src=data.audio_url;const link=document.createElement('a');link.href=data.audio_url;link.textContent='Tải audio';link.download='';result.append(msg,audio,link)}catch(err){const msg=document.createElement('div');msg.className='status error';msg.textContent='Lỗi: '+err.message;result.appendChild(msg)}finally{b.disabled=false;b.textContent='Generate local audio'}});init();
</script></body></html>"""


def create_router(service: SynthesisService, settings: Settings) -> APIRouter:
    router = APIRouter()

    @router.get("/", response_class=HTMLResponse, include_in_schema=False)
    def home() -> str:
        return _ui_html()

    @router.get("/health")
    def health(request: Request) -> dict[str, Any]:
        _assert_local_api_key(request, settings)
        engine = service.engine(settings.engine).status()
        return {
            "status": "ok",
            "service": "local-voice-clone",
            "engine": settings.engine,
            "engine_status": engine.to_dict(),
            "device": service.device_info(),
            "local_only": settings.host == "127.0.0.1",
        }

    @router.get("/v1/models")
    def models(request: Request) -> dict[str, Any]:
        _assert_local_api_key(request, settings)
        return {"data": service.model_infos()}

    @router.get("/v1/voices")
    def voices(request: Request) -> dict[str, Any]:
        _assert_local_api_key(request, settings)
        return {"voices": service.profiles.list(), "default_voice_id": settings.default_voice_id}

    @router.get("/v1/vieneu/voices")
    def vieneu_voices(request: Request) -> dict[str, Any]:
        """Return preset voices from the local VieNeu model.

        This endpoint is intentionally separate from ``/v1/voices``: the
        latter lists uploaded/reference profiles, while VieNeu voices are
        built into the local model and do not have a reference WAV.
        """

        _assert_local_api_key(request, settings)
        try:
            engine = service.engine("vieneu")
            voices = engine.list_preset_voices()  # type: ignore[attr-defined]
        except Exception as exc:
            raise VoiceCloneServiceError(
                "MODEL_NOT_LOADED",
                f"Không đọc được danh sách giọng VieNeu: {exc}",
                status_code=503,
            ) from exc
        return {
            "voices": [
                {"label": label, "id": voice_id, "available": True}
                for label, voice_id in voices
            ],
            "default_voice_id": voices[0][1] if voices else "",
            "model": "vieneu",
        }

    async def synthesize_common(request: Request) -> JSONResponse | FileResponse:
        payload = await parse_compatibility_request(request)
        _assert_local_api_key(request, settings, payload.api_key)
        if payload.reference_audio is not None:
            enforce_uploaded_voice_consent(
                consent=payload.voice_use_consent,
                password=request.headers.get("x-voice-upload-password", ""),
                required=settings.require_upload_password,
            )
        uploaded_reference, upload_folder = await _persist_reference_upload(payload, settings)
        try:
            result = service.synthesize(
                model=payload.model,
                voice_id=payload.voice_id,
                text=payload.text,
                reference_audio=uploaded_reference,
                reference_text=payload.reference_text,
                voice_style=payload.voice_style,
                voice_region=payload.voice_region,
                language=payload.language,
                speed=payload.speed,
                output_format=payload.output_format,
            )
        finally:
            if upload_folder:
                shutil.rmtree(upload_folder, ignore_errors=True)
        if payload.return_audio:
            media_type = "audio/wav" if result.output_format == "wav" else "audio/mpeg"
            return FileResponse(result.audio_path, media_type=media_type, filename=result.audio_path.name)
        return JSONResponse(_response_for_result(result, request, settings))

    @router.post("/v1/voice-clone/synthesize", tags=["voice-clone"], response_model=None)
    async def synthesize(request: Request):
        """Cloud-style synthesis; accepts either JSON voice_id or multipart upload."""

        return await synthesize_common(request)

    @router.post("/v1/voice-clone/synthesize-upload", tags=["voice-clone"], response_model=None)
    async def synthesize_upload(request: Request):
        """Explicit multipart alias for callers that upload a reference file."""

        return await synthesize_common(request)

    @router.post("/v1/storyboard/synthesize", tags=["storyboard"])
    def storyboard_synthesize(payload: StoryboardSynthesisRequest, request: Request) -> dict[str, Any]:
        _assert_local_api_key(request, settings)
        if not payload.slides:
            raise VoiceCloneServiceError("TEXT_EMPTY", "Storyboard chưa có slide.")
        if len({slide.slide for slide in payload.slides}) != len(payload.slides):
            raise VoiceCloneServiceError("MODEL_INFERENCE_ERROR", "Storyboard có Slide bị trùng.")
        run_id = str(uuid.uuid4())
        files: list[dict[str, Any]] = []
        for item in payload.slides:
            try:
                rendered = service.synthesize(
                    model=payload.model,
                    voice_id=payload.voice_id,
                    text=item.text,
                    reference_text=payload.reference_text,
                    voice_style=payload.voice_style,
                    voice_region=payload.voice_region,
                    language=payload.language,
                    speed=payload.speed,
                    output_format=payload.output_format,
                    output_name=f"slide_{item.slide:03d}",
                )
                files.append(
                    {
                        "slide": item.slide,
                        "path": _relative_to_root(rendered.audio_path, settings),
                        "audio_url": f"{str(request.base_url).rstrip('/')}/audio/{quote(rendered.audio_path.name)}",
                        "duration": round(rendered.duration_seconds, 3),
                        "pause_after": item.pause_after,
                        "status": "SUCCESS",
                        "warnings": rendered.warnings,
                    }
                )
            except VoiceCloneServiceError as exc:
                failure = {"slide": item.slide, "status": "FAILED", "error_code": exc.code, "message": exc.message}
                files.append(failure)
                if not payload.continue_on_error:
                    raise
        manifest = {
            "run_id": run_id,
            "voice_id": payload.voice_id,
            "voice_region": payload.voice_region,
            "model": payload.model,
            "slides": files,
        }
        manifest_path = settings.output_dir / f"storyboard_manifest_{run_id}.json"
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        return {"success": all(item["status"] == "SUCCESS" for item in files), "files": files, "manifest": _relative_to_root(manifest_path, settings)}

    @router.get("/audio/{filename}", include_in_schema=False)
    def audio(filename: str, request: Request) -> FileResponse:
        _assert_local_api_key(request, settings)
        if Path(filename).name != filename or Path(filename).suffix.lower() not in {".wav", ".mp3"}:
            raise VoiceCloneServiceError("REFERENCE_AUDIO_NOT_FOUND", "Tên audio không hợp lệ.", status_code=404)
        target = (settings.output_dir / filename).resolve()
        if target.parent != settings.output_dir.resolve() or not target.exists() or target.stat().st_size == 0:
            raise VoiceCloneServiceError("REFERENCE_AUDIO_NOT_FOUND", "Không tìm thấy audio đã sinh.", status_code=404)
        media_type = "audio/wav" if target.suffix.lower() == ".wav" else "audio/mpeg"
        return FileResponse(target, media_type=media_type, filename=target.name)

    return router
