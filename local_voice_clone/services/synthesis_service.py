from __future__ import annotations

import shutil
import tempfile
import threading
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from config.settings import Settings
from engines import EngineUnavailableError, VoiceCloneEngine, create_engine
from .audio_service import AudioInspection, AudioService, SUPPORTED_OUTPUT_FORMATS
from .cache_service import CacheService
from .errors import VoiceCloneServiceError
from .logging_service import configure_logging
from .text_normalizer import VietnameseTextNormalizer, chunk_text
from .voice_profile_service import VoiceProfile, VoiceProfileService

from src.vieneu_tts import regional_prompt_voice_id


@dataclass(frozen=True)
class SynthesisResult:
    request_id: str
    model: str
    voice_id: str
    audio_path: Path
    output_format: str
    duration_seconds: float
    sample_rate: int
    warnings: list[str]
    cache_hit: bool
    inference_seconds: float
    normalized_text: str
    queue_wait_seconds: float = 0.0
    total_seconds: float = 0.0
    voice_region: str = "auto"

    def to_dict(self, *, audio_url: str | None = None) -> dict[str, Any]:
        output = {
            "success": True,
            "request_id": self.request_id,
            "model": self.model,
            "voice_id": self.voice_id,
            "audio_path": str(self.audio_path),
            "duration_seconds": round(self.duration_seconds, 3),
            "sample_rate": self.sample_rate,
            "output_format": self.output_format,
            "warnings": self.warnings,
            "cache_hit": self.cache_hit,
            "inference_seconds": round(self.inference_seconds, 3),
            "queue_wait_seconds": round(self.queue_wait_seconds, 3),
            "total_seconds": round(self.total_seconds, 3),
            "voice_region": self.voice_region,
        }
        if audio_url:
            output["audio_url"] = audio_url
        return output


class SynthesisService:
    """Orchestrates local preprocessing, cloning, cache and output validation."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.settings.ensure_directories()
        self.audio = AudioService(settings)
        self.cache = CacheService(settings.cache_dir, enabled=settings.enable_cache)
        self.profiles = VoiceProfileService(settings)
        self.normalizer = VietnameseTextNormalizer(normalize_numbers=settings.normalize_numbers)
        self.logger = configure_logging(settings.log_dir)
        self._engines: dict[str, VoiceCloneEngine] = {}
        self._engine_errors: dict[str, str] = {}
        # F5 model access and its upstream stdout handling are process-global;
        # serialize actual inference to avoid GPU contention and text/log races.
        self._inference_lock = threading.RLock()

    @staticmethod
    def _engine_key(model: str | None, profile: VoiceProfile | None, default_engine: str) -> str:
        raw = (model or (profile.engine if profile else default_engine) or default_engine).strip().lower()
        if raw in {"f5-tts", "f5tts", "f5_tts", "f5tts_v1_base", "f5-tts-v1-base"}:
            return "f5-tts"
        if raw in {
            "vieneu",
            "vieneu-tts",
            "vieneu_tts",
            "vieneu-clone",
            "vieneu_clone",
        }:
            return "vieneu"
        return raw

    def engine(self, name: str) -> VoiceCloneEngine:
        if name not in self._engines:
            self._engines[name] = create_engine(
                name,
                device=self.settings.device,
                model=self.settings.f5_model,
                allow_model_download=self.settings.allow_model_download,
                model_cache_dir=self.settings.model_cache_dir,
            )
        return self._engines[name]

    def warm_up(self) -> None:
        if not self.settings.preload_model:
            return
        try:
            self.engine(self.settings.engine).load()
            self._engine_errors.pop(self.settings.engine, None)
            self.logger.info("engine_loaded engine=%s", self.settings.engine)
        except Exception as exc:
            self._engine_errors[self.settings.engine] = str(exc)
            self.logger.warning("engine_not_loaded engine=%s error=%s", self.settings.engine, exc)

    def device_info(self) -> dict[str, Any]:
        try:
            import torch

            if torch.cuda.is_available():
                properties = torch.cuda.get_device_properties(0)
                return {
                    "device": "cuda",
                    "gpu": properties.name,
                    "vram_total_gb": round(properties.total_memory / 1024**3, 2),
                    "cuda_available": True,
                }
            return {
                "device": "cpu",
                "cuda_available": False,
                "warning": "CPU inference may be slow.",
            }
        except ImportError:
            return {
                "device": "cpu",
                "cuda_available": False,
                "warning": "PyTorch chưa cài; CPU inference may be slow.",
            }

    def model_infos(self) -> list[dict[str, Any]]:
        output: list[dict[str, Any]] = []
        for name in ("f5-tts", "vieneu", "dummy"):
            try:
                engine = self.engine(name)
                info = engine.model_info()
                if name in self._engine_errors:
                    info["message"] = self._engine_errors[name]
                output.append(info)
            except Exception as exc:
                output.append({"id": name, "available": False, "loaded": False, "message": str(exc)})
        return output

    def _profile_or_reference(
        self,
        *,
        voice_id: str | None,
        reference_audio: str | Path | None,
        reference_text: str | None,
        language: str,
    ) -> tuple[VoiceProfile | None, Path, str | None, str, str]:
        profile: VoiceProfile | None = None
        if reference_audio:
            source = Path(reference_audio)
            effective_voice_id = voice_id or "uploaded"
            return profile, source, reference_text, language or "vi", effective_voice_id
        chosen_voice_id = voice_id or self.settings.default_voice_id
        profile = self.profiles.get(chosen_voice_id)
        if not profile.enabled:
            raise VoiceCloneServiceError("REFERENCE_AUDIO_NOT_FOUND", f"Voice profile '{profile.id}' đang tắt.")
        return (
            profile,
            profile.reference_audio,
            reference_text if reference_text is not None else profile.reference_text,
            language or profile.language,
            profile.id,
        )

    @staticmethod
    def _safe_output_path(output_dir: Path, output_name: str, output_format: str) -> Path:
        filename = Path(output_name).name
        if not filename or filename in {".", ".."}:
            raise VoiceCloneServiceError("MODEL_INFERENCE_ERROR", "Tên output không hợp lệ.", status_code=500)
        target = (output_dir / filename).with_suffix(f".{output_format}").resolve()
        root = output_dir.resolve()
        if root not in target.parents and target != root:
            raise VoiceCloneServiceError("MODEL_INFERENCE_ERROR", "Đường dẫn output không hợp lệ.", status_code=500)
        return target

    def _render_chunks(
        self,
        *,
        engine: VoiceCloneEngine,
        chunks: list[tuple[str, bool]],
        reference_audio: Path,
        reference_text: str | None,
        language: str,
        speed: float,
        temporary_dir: Path,
        voice_region: str = "auto",
    ) -> Path:
        temporary_dir.mkdir(parents=True, exist_ok=True)
        paths: list[Path] = []
        for index, (chunk, _) in enumerate(chunks, start=1):
            target = temporary_dir / f"chunk_{index:03}.wav"
            synthesize_kwargs = {
                "language": language,
                "speed": speed,
            }
            if voice_region != "auto":
                # Only the VieNeu clone engine consumes this optional
                # argument. F5/Vira callers keep the legacy contract.
                synthesize_kwargs["voice_region"] = voice_region
            engine.synthesize(
                chunk,
                reference_audio,
                reference_text,
                target,
                **synthesize_kwargs,
            )
            if not target.exists() or target.stat().st_size == 0:
                raise RuntimeError(f"Engine không sinh được chunk {index}.")
            paths.append(target)
        return self.audio.concatenate_wavs(
            paths,
            temporary_dir / "combined.wav",
            sentence_pause_ms=self.settings.sentence_pause_ms,
            paragraph_pause_ms=self.settings.paragraph_pause_ms,
            paragraph_after=[marker for _, marker in chunks],
        )

    def _render_vieneu_chunks(
        self,
        *,
        engine: VoiceCloneEngine,
        chunks: list[tuple[str, bool]],
        voice_id: str,
        style: str,
        language: str,
        speed: float,
        temporary_dir: Path,
    ) -> Path:
        temporary_dir.mkdir(parents=True, exist_ok=True)
        synthesize_with_voice = getattr(engine, "synthesize_with_voice", None)
        if not callable(synthesize_with_voice):
            raise RuntimeError("VieNeu engine không hỗ trợ preset voice.")

        paths: list[Path] = []
        for index, (chunk, _) in enumerate(chunks, start=1):
            target = temporary_dir / f"chunk_{index:03}.wav"
            synthesize_with_voice(
                text=chunk,
                voice_id=voice_id or None,
                style=style,
                output_path=target,
                language=language,
                speed=speed,
            )
            if not target.exists() or target.stat().st_size == 0:
                raise RuntimeError(f"VieNeu-TTS không sinh được chunk {index}.")
            paths.append(target)
        return self.audio.concatenate_wavs(
            paths,
            temporary_dir / "combined.wav",
            sentence_pause_ms=self.settings.sentence_pause_ms,
            paragraph_pause_ms=self.settings.paragraph_pause_ms,
            paragraph_after=[marker for _, marker in chunks],
        )

    @staticmethod
    def _is_oom(error: Exception) -> bool:
        message = str(error).lower()
        return "out of memory" in message or "cuda oom" in message or "cuda error: out" in message

    @staticmethod
    def _clear_cuda_cache() -> None:
        try:
            import torch

            if torch.cuda.is_available():
                torch.cuda.empty_cache()
        except ImportError:
            pass

    def _synthesize_vieneu(
        self,
        *,
        normalized_text: str,
        voice_id: str | None,
        reference_audio: str | Path | None,
        reference_text: str | None,
        voice_style: str,
        voice_region: str,
        language: str,
        speed: float,
        selected_format: str,
        output_dir: str | Path | None,
        output_name: str | None,
    ) -> SynthesisResult:
        """Synthesize a VieNeu preset or a Vietnamese reference clone.

        VieNeu v3 Turbo accepts ``ref_audio`` directly.  Keeping this path in
        the VieNeu-specific method is important: it lets preset voices keep
        their existing API while avoiding the generic F5/Vira profile path for
        a clone request.
        """

        selected_engine = "vieneu"
        clone_mode = reference_audio is not None
        requested_region = str(voice_region or "auto").strip().lower()
        regional_default_voice = (
            regional_prompt_voice_id(requested_region)
            if requested_region != "auto"
            else ""
        )
        effective_voice_id = (
            "uploaded"
            if clone_mode
            else (
                regional_default_voice
                if regional_default_voice and (not voice_id or voice_id.lower() in {"default", "auto"})
                else (voice_id or "default")
            )
        )
        effective_style = (voice_style or "tu_nhien").strip() or "tu_nhien"
        effective_language = language or "vi"
        request_id = str(uuid.uuid4())
        destination_dir = Path(output_dir).resolve() if output_dir else self.settings.output_dir.resolve()
        destination_dir.mkdir(parents=True, exist_ok=True)
        destination = self._safe_output_path(destination_dir, output_name or request_id, selected_format)
        warnings: list[str] = []
        started_at = time.perf_counter()

        reference_source: Path | None = None
        if clone_mode:
            reference_source = Path(reference_audio).expanduser().resolve()
            if not reference_source.is_file():
                raise VoiceCloneServiceError(
                    "REFERENCE_AUDIO_NOT_FOUND",
                    f"Không tìm thấy file mẫu giọng: {reference_source}",
                )

        try:
            with self._inference_lock:
                engine = self.engine(selected_engine)
                engine.load()
        except EngineUnavailableError as exc:
            raise VoiceCloneServiceError("MODEL_NOT_LOADED", str(exc), status_code=503) from exc
        except Exception as exc:
            raise VoiceCloneServiceError("MODEL_NOT_LOADED", f"Không tải được VieNeu-TTS: {exc}", status_code=503) from exc

        temporary_dir = Path(tempfile.mkdtemp(prefix="vieneu_", dir=self.settings.temp_dir))
        try:
            prepared_reference: Path | None = None
            if reference_source is not None:
                prepared_reference = temporary_dir / "reference.wav"
                self.audio.preprocess_reference(reference_source, prepared_reference)

            cache_key = self.cache.build_key(
                {
                    "engine": selected_engine,
                    "mode": "clone" if clone_mode else "preset",
                    "voice_id": effective_voice_id,
                    "voice_style": effective_style,
                    "voice_region": voice_region,
                    "reference_audio_hash": (
                        self.cache.hash_file(prepared_reference)
                        if prepared_reference is not None
                        else ""
                    ),
                    "reference_text": reference_text or "",
                    "text": normalized_text,
                    "language": effective_language,
                    "speed": float(speed),
                    "output_format": selected_format,
                    "service_version": "1.0.0-vieneu",
                }
            )
            cached_metadata = self.cache.restore(cache_key, selected_format, destination)
            if cached_metadata is not None:
                elapsed = time.perf_counter() - started_at
                return SynthesisResult(
                    request_id=request_id,
                    model=selected_engine,
                    voice_id=effective_voice_id,
                    audio_path=destination,
                    output_format=selected_format,
                    duration_seconds=float(cached_metadata.get("duration_seconds", 0.0)),
                    sample_rate=int(cached_metadata.get("sample_rate", self.settings.output_sample_rate)),
                    warnings=list(dict.fromkeys(warnings + list(cached_metadata.get("warnings", [])))),
                    cache_hit=True,
                    inference_seconds=0.0,
                    normalized_text=normalized_text,
                    queue_wait_seconds=0.0,
                    total_seconds=elapsed,
                    voice_region=voice_region,
                )

            # VieNeu v3 Turbo performs its own reference-aware chunking.  A
            # single service chunk avoids re-encoding the sample voice for
            # every sentence and is substantially faster for one slide.
            chunks = (
                [(normalized_text, False)]
                if clone_mode
                else chunk_text(normalized_text, self.settings.max_chars_per_chunk)
            )
            if not chunks:
                raise VoiceCloneServiceError("TEXT_EMPTY", "Không tách được text thành chunk.")

            queue_started = time.perf_counter()
            with self._inference_lock:
                queue_wait_seconds = time.perf_counter() - queue_started
                inference_started = time.perf_counter()
                try:
                    if clone_mode:
                        generated_wav = self._render_chunks(
                            engine=engine,
                            chunks=chunks,
                            reference_audio=prepared_reference,
                            reference_text=reference_text,
                            language=effective_language,
                            speed=float(speed),
                            temporary_dir=temporary_dir,
                            voice_region=voice_region,
                        )
                    else:
                        generated_wav = self._render_vieneu_chunks(
                            engine=engine,
                            chunks=chunks,
                            voice_id=effective_voice_id,
                            style=effective_style,
                            language=effective_language,
                            speed=float(speed),
                            temporary_dir=temporary_dir,
                        )
                except Exception as exc:
                    if not self._is_oom(exc):
                        raise
                    self._clear_cuda_cache()
                    smaller_chunks = chunk_text(
                        normalized_text,
                        max(50, self.settings.max_chars_per_chunk // 2),
                    )
                    try:
                        if clone_mode:
                            generated_wav = self._render_chunks(
                                engine=engine,
                                chunks=smaller_chunks,
                                reference_audio=prepared_reference,
                                reference_text=reference_text,
                                language=effective_language,
                                speed=float(speed),
                                temporary_dir=temporary_dir / "oom_retry",
                                voice_region=voice_region,
                            )
                        else:
                            generated_wav = self._render_vieneu_chunks(
                                engine=engine,
                                chunks=smaller_chunks,
                                voice_id=effective_voice_id,
                                style=effective_style,
                                language=effective_language,
                                speed=float(speed),
                                temporary_dir=temporary_dir / "oom_retry",
                            )
                        warnings.append("GPU_OOM_RETRIED_WITH_SMALLER_CHUNKS")
                    except Exception as retry_exc:
                        raise VoiceCloneServiceError(
                            "GPU_OUT_OF_MEMORY",
                            f"GPU hết bộ nhớ sau lần thử lại: {retry_exc}",
                            status_code=503,
                        ) from retry_exc
                inference_seconds = time.perf_counter() - inference_started

            inspection: AudioInspection = self.audio.inspect(generated_wav)
            warnings.extend(self.audio.quality_warnings(inspection))
            if selected_format == "wav":
                shutil.copyfile(generated_wav, destination)
            else:
                self.audio.convert_to_mp3(generated_wav, destination)
            if not destination.exists() or destination.stat().st_size == 0:
                raise VoiceCloneServiceError("MODEL_INFERENCE_ERROR", "Không ghi được output audio.", status_code=500)

            total_seconds = time.perf_counter() - started_at
            metadata = {
                "duration_seconds": inspection.duration_seconds,
                "sample_rate": inspection.sample_rate,
                "warnings": list(dict.fromkeys(warnings)),
                "model": selected_engine,
                "voice_id": effective_voice_id,
                "voice_style": effective_style,
                "voice_region": voice_region,
                "output_format": selected_format,
            }
            self.cache.store(cache_key, selected_format, destination, metadata)
            return SynthesisResult(
                request_id=request_id,
                model=selected_engine,
                voice_id=effective_voice_id,
                audio_path=destination,
                output_format=selected_format,
                duration_seconds=inspection.duration_seconds,
                sample_rate=inspection.sample_rate,
                warnings=list(dict.fromkeys(warnings)),
                cache_hit=False,
                inference_seconds=inference_seconds,
                normalized_text=normalized_text,
                queue_wait_seconds=queue_wait_seconds,
                total_seconds=total_seconds,
                voice_region=voice_region,
            )
        except VoiceCloneServiceError:
            raise
        except Exception as exc:
            self.logger.exception("vieneu_synthesis_failed voice_id=%s error=%s", effective_voice_id, exc)
            raise VoiceCloneServiceError("MODEL_INFERENCE_ERROR", f"VieNeu-TTS inference lỗi: {exc}", status_code=500) from exc
        finally:
            shutil.rmtree(temporary_dir, ignore_errors=True)

    def synthesize(
        self,
        *,
        text: str,
        model: str | None = None,
        voice_id: str | None = None,
        reference_audio: str | Path | None = None,
        reference_text: str | None = None,
        voice_style: str = "tu_nhien",
        voice_region: str = "auto",
        language: str = "vi",
        speed: float = 1.0,
        output_format: str = "wav",
        output_dir: str | Path | None = None,
        output_name: str | None = None,
    ) -> SynthesisResult:
        if not str(text or "").strip():
            raise VoiceCloneServiceError("TEXT_EMPTY", "Text cần đọc không được để trống.")
        if not 0.5 <= float(speed) <= 2.0:
            raise VoiceCloneServiceError("MODEL_INFERENCE_ERROR", "speed phải trong khoảng 0.5–2.0.")
        selected_format = output_format.strip().lower().lstrip(".")
        if selected_format not in SUPPORTED_OUTPUT_FORMATS:
            raise VoiceCloneServiceError("UNSUPPORTED_OUTPUT_FORMAT", "Chỉ hỗ trợ output wav hoặc mp3.")

        normalized_text = self.normalizer.normalize(text)
        if not normalized_text:
            raise VoiceCloneServiceError("TEXT_EMPTY", "Text trống sau khi chuẩn hóa.")
        requested_engine = self._engine_key(model, None, self.settings.engine)
        if requested_engine == "vieneu":
            return self._synthesize_vieneu(
                normalized_text=normalized_text,
                voice_id=voice_id,
                reference_audio=reference_audio,
                reference_text=reference_text,
                voice_style=voice_style,
                voice_region=voice_region,
                language=language,
                speed=float(speed),
                selected_format=selected_format,
                output_dir=output_dir,
                output_name=output_name,
            )
        profile, source_reference, effective_reference_text, effective_language, effective_voice_id = self._profile_or_reference(
            voice_id=voice_id,
            reference_audio=reference_audio,
            reference_text=reference_text,
            language=language,
        )
        selected_engine = self._engine_key(model, profile, self.settings.engine)
        try:
            with self._inference_lock:
                engine = self.engine(selected_engine)
                engine.load()
        except EngineUnavailableError as exc:
            raise VoiceCloneServiceError("MODEL_NOT_LOADED", str(exc), status_code=503) from exc
        except Exception as exc:
            raise VoiceCloneServiceError("MODEL_NOT_LOADED", f"Không tải được model: {exc}", status_code=503) from exc

        request_id = str(uuid.uuid4())
        destination_dir = Path(output_dir).resolve() if output_dir else self.settings.output_dir.resolve()
        destination_dir.mkdir(parents=True, exist_ok=True)
        destination = self._safe_output_path(destination_dir, output_name or request_id, selected_format)
        warnings: list[str] = []
        if not effective_reference_text:
            warnings.append("REFERENCE_TRANSCRIPT_MISSING")

        started_at = time.perf_counter()
        temporary_dir = Path(tempfile.mkdtemp(prefix="voice_clone_", dir=self.settings.temp_dir))
        try:
            prepared_reference = temporary_dir / "reference.wav"
            self.audio.preprocess_reference(source_reference, prepared_reference)
            cache_key = self.cache.build_key(
                {
                    "engine": selected_engine,
                    "model": model or self.settings.f5_model,
                    "reference_audio_hash": self.cache.hash_file(prepared_reference),
                    "reference_text": effective_reference_text or "",
                    "text": normalized_text,
                    "language": effective_language,
                    "speed": float(speed),
                    "output_format": selected_format,
                    "service_version": "1.0.0",
                }
            )
            cached_metadata = self.cache.restore(cache_key, selected_format, destination)
            if cached_metadata is not None:
                cached_warnings = list(cached_metadata.get("warnings", []))
                elapsed = time.perf_counter() - started_at
                self.logger.info(
                    "synthesis request_id=%s model=%s voice_id=%s cache_hit=true duration=%s seconds=%.3f",
                    request_id,
                    selected_engine,
                    effective_voice_id,
                    cached_metadata.get("duration_seconds", 0),
                    elapsed,
                )
                return SynthesisResult(
                    request_id=request_id,
                    model=selected_engine,
                    voice_id=effective_voice_id,
                    audio_path=destination,
                    output_format=selected_format,
                    duration_seconds=float(cached_metadata.get("duration_seconds", 0.0)),
                    sample_rate=int(cached_metadata.get("sample_rate", self.settings.output_sample_rate)),
                    warnings=list(dict.fromkeys(warnings + cached_warnings)),
                    cache_hit=True,
                    inference_seconds=0.0,
                    normalized_text=normalized_text,
                    queue_wait_seconds=0.0,
                    total_seconds=elapsed,
                )

            # Vira ULTRA V3 owns its chunking internally. Passing the full
            # normalized narration as one service-level chunk prevents
            # double chunking (service -> engine -> internal chunks).
            if selected_engine == "vira-tts":
                chunks = [(normalized_text, False)]
            else:
                chunks = chunk_text(normalized_text, self.settings.max_chars_per_chunk)

            if not chunks:
                raise VoiceCloneServiceError("TEXT_EMPTY", "Không tách được text thành chunk.")

            queue_started = time.perf_counter()
            with self._inference_lock:
                queue_wait_seconds = time.perf_counter() - queue_started
                inference_started = time.perf_counter()
                try:
                    generated_wav = self._render_chunks(
                        engine=engine,
                        chunks=chunks,
                        reference_audio=prepared_reference,
                        reference_text=effective_reference_text,
                        language=effective_language,
                        speed=float(speed),
                        temporary_dir=temporary_dir,
                    )
                except Exception as exc:
                    if not self._is_oom(exc):
                        raise

                    self._clear_cuda_cache()

                    # Vira has its own internal chunk/retry strategy. Do not
                    # introduce a second service-level chunking layer.
                    if selected_engine == "vira-tts":
                        raise VoiceCloneServiceError(
                            "GPU_OUT_OF_MEMORY",
                            f"GPU hết bộ nhớ khi chạy Vira-TTS: {exc}",
                            status_code=503,
                        ) from exc

                    smaller_chunks = chunk_text(
                        normalized_text,
                        max(50, self.settings.max_chars_per_chunk // 2),
                    )
                    try:
                        generated_wav = self._render_chunks(
                            engine=engine,
                            chunks=smaller_chunks,
                            reference_audio=prepared_reference,
                            reference_text=effective_reference_text,
                            language=effective_language,
                            speed=float(speed),
                            temporary_dir=temporary_dir / "oom_retry",
                        )
                        warnings.append("GPU_OOM_RETRIED_WITH_SMALLER_CHUNKS")
                    except Exception as retry_exc:
                        raise VoiceCloneServiceError(
                            "GPU_OUT_OF_MEMORY",
                            f"GPU hết bộ nhớ sau lần thử lại: {retry_exc}",
                            status_code=503,
                        ) from retry_exc

                inference_seconds = time.perf_counter() - inference_started

            inspection: AudioInspection = self.audio.inspect(generated_wav)
            warnings.extend(self.audio.quality_warnings(inspection))
            if selected_format == "wav":
                shutil.copyfile(generated_wav, destination)
            else:
                self.audio.convert_to_mp3(generated_wav, destination)
            if not destination.exists() or destination.stat().st_size == 0:
                raise VoiceCloneServiceError("MODEL_INFERENCE_ERROR", "Không ghi được output audio.", status_code=500)
            total_seconds = time.perf_counter() - started_at
            metadata = {
                "duration_seconds": inspection.duration_seconds,
                "sample_rate": inspection.sample_rate,
                "warnings": list(dict.fromkeys(warnings)),
                "model": selected_engine,
                "voice_id": effective_voice_id,
                "output_format": selected_format,
            }
            self.cache.store(cache_key, selected_format, destination, metadata)
            self.logger.info(
                "synthesis request_id=%s model=%s voice_id=%s text_length=%d ref=%s "
                "cache_hit=false duration=%.3f inference_seconds=%.3f "
                "queue_wait_seconds=%.3f total_seconds=%.3f device=%s",
                request_id,
                selected_engine,
                effective_voice_id,
                len(normalized_text),
                prepared_reference.name,
                inspection.duration_seconds,
                inference_seconds,
                queue_wait_seconds,
                total_seconds,
                engine.status().device,
            )
            return SynthesisResult(
                request_id=request_id,
                model=selected_engine,
                voice_id=effective_voice_id,
                audio_path=destination,
                output_format=selected_format,
                duration_seconds=inspection.duration_seconds,
                sample_rate=inspection.sample_rate,
                warnings=list(dict.fromkeys(warnings)),
                cache_hit=False,
                inference_seconds=inference_seconds,
                normalized_text=normalized_text,
                queue_wait_seconds=queue_wait_seconds,
                total_seconds=total_seconds,
            )
        except VoiceCloneServiceError:
            raise
        except Exception as exc:
            self.logger.exception("synthesis_failed model=%s voice_id=%s error=%s", selected_engine, effective_voice_id, exc)
            raise VoiceCloneServiceError("MODEL_INFERENCE_ERROR", f"Model inference lỗi: {exc}", status_code=500) from exc
        finally:
            shutil.rmtree(temporary_dir, ignore_errors=True)
