from __future__ import annotations

import inspect
import io
import os
import sys
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from typing import Any

import numpy as np
import soundfile as sf

from .base import EngineStatus, EngineUnavailableError, VoiceCloneEngine


class F5TTSEngine(VoiceCloneEngine):
    """Thin adapter cho official `f5_tts.api.F5TTS` API."""

    id = "f5-tts"

    def __init__(
        self,
        *,
        device: str = "auto",
        model_name: str = "F5TTS_v1_Base",
        allow_model_download: bool = False,
        model_cache_dir: str | Path | None = None,
    ) -> None:
        self.requested_device = device
        self.model_name = model_name
        self.allow_model_download = allow_model_download
        self.model_cache_dir = Path(model_cache_dir).resolve() if model_cache_dir else None
        self._model: Any | None = None
        self._error = ""
        self._resolved_device = device

    def _device(self) -> str:
        if self.requested_device != "auto":
            return self.requested_device
        try:
            import torch

            return "cuda" if torch.cuda.is_available() else "cpu"
        except ImportError:
            return "cpu"

    @staticmethod
    def _configure_utf8_console() -> None:
        """Keep upstream F5 debug output from breaking on Windows code pages."""

        for stream in (sys.stdout, sys.stderr):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except (AttributeError, OSError):
                pass

    @staticmethod
    def _enable_soundfile_wav_fallback() -> None:
        """Avoid a TorchCodec/FFmpeg DLL issue for our already-normalized WAVs.

        F5-TTS currently calls ``torchaudio.load`` for the reference WAV.  On
        some Windows CPU environments recent torchaudio delegates this to
        TorchCodec, whose shared FFmpeg DLLs may not be loadable even when the
        standalone FFmpeg executable is installed.  This narrow fallback only
        handles local WAV paths that this service has already normalized; all
        other inputs retain torchaudio's normal behaviour.
        """

        try:
            import torch
            import torchaudio
        except ImportError:
            return
        if getattr(torchaudio.load, "_local_voice_clone_soundfile_fallback", False):
            return
        original_load = torchaudio.load

        def load_wav_with_soundfile(
            uri: Any,
            frame_offset: int = 0,
            num_frames: int = -1,
            normalize: bool = True,
            channels_first: bool = True,
            *args: Any,
            **kwargs: Any,
        ) -> tuple[Any, int]:
            path = Path(uri) if isinstance(uri, (str, Path)) else None
            if path is None or path.suffix.lower() != ".wav":
                return original_load(
                    uri,
                    frame_offset=frame_offset,
                    num_frames=num_frames,
                    normalize=normalize,
                    channels_first=channels_first,
                    *args,
                    **kwargs,
                )
            samples, sample_rate = sf.read(path, always_2d=True, dtype="float32")
            start = max(0, int(frame_offset))
            end = None if num_frames is None or int(num_frames) < 0 else start + int(num_frames)
            samples = np.ascontiguousarray(samples[start:end])
            tensor = torch.from_numpy(samples.T if channels_first else samples)
            return tensor, int(sample_rate)

        setattr(load_wav_with_soundfile, "_local_voice_clone_soundfile_fallback", True)
        torchaudio.load = load_wav_with_soundfile

    def load(self) -> None:
        if self._model is not None:
            return
        self._resolved_device = self._device()
        self._configure_utf8_console()
        self._enable_soundfile_wav_fallback()
        if not self.allow_model_download:
            # The upstream helper may otherwise fetch public checkpoints on
            # first use.  The service must never initiate a network transfer
            # of its own in local-only mode.
            os.environ["HF_HUB_OFFLINE"] = "1"
            os.environ["TRANSFORMERS_OFFLINE"] = "1"
        try:
            from f5_tts.api import F5TTS
        except ImportError as exc:
            self._error = "Chưa cài F5-TTS. Chạy `pip install -r requirements-f5.txt`."
            raise EngineUnavailableError(self._error) from exc

        try:
            signature = inspect.signature(F5TTS)
            kwargs: dict[str, Any] = {}
            if "model" in signature.parameters:
                kwargs["model"] = self.model_name
            if "device" in signature.parameters:
                kwargs["device"] = self._resolved_device
            if "hf_cache_dir" in signature.parameters and self.model_cache_dir is not None:
                self.model_cache_dir.mkdir(parents=True, exist_ok=True)
                kwargs["hf_cache_dir"] = str(self.model_cache_dir)
            self._model = F5TTS(**kwargs)
            self._error = ""
        except Exception as exc:
            self._error = f"Không tải được F5-TTS: {exc}"
            raise EngineUnavailableError(self._error) from exc

    def status(self) -> EngineStatus:
        if self._model is not None:
            return EngineStatus(
                id=self.id,
                available=True,
                loaded=True,
                device=self._resolved_device,
                model=self.model_name,
            )
        try:
            import f5_tts  # noqa: F401

            available = True
            message = self._error or "F5-TTS đã cài, chờ load model."
        except ImportError:
            available = False
            message = self._error or "Chưa cài F5-TTS."
        return EngineStatus(
            id=self.id,
            available=available,
            loaded=False,
            device=self._device(),
            model=self.model_name,
            message=message,
        )

    def synthesize(
        self,
        text: str,
        reference_audio: str | Path,
        reference_text: str | None,
        output_path: str | Path,
        language: str = "vi",
        speed: float = 1.0,
    ) -> str:
        self.load()
        if self._model is None:  # defensive for type checker and failed loads
            raise EngineUnavailableError(self._error or "F5-TTS chưa được load.")
        target = Path(output_path).resolve()
        target.parent.mkdir(parents=True, exist_ok=True)
        infer = self._model.infer
        signature = inspect.signature(infer)
        kwargs: dict[str, Any] = {
            "ref_file": str(Path(reference_audio).resolve()),
            "ref_text": reference_text or "",
            "gen_text": text,
            "file_wave": str(target),
        }
        if "speed" in signature.parameters:
            kwargs["speed"] = speed
        if "language" in signature.parameters:
            kwargs["language"] = language
        if "show_info" in signature.parameters:
            # Upstream emits ref/gen text through this callback.  On default
            # Windows CP1252 consoles that can raise UnicodeEncodeError for
            # Vietnamese before inference starts.  Request-level logging is
            # handled by SynthesisService instead, without recording text.
            kwargs["show_info"] = lambda *_args, **_kwargs: None
        try:
            # F5 currently prints reference and generated text internally,
            # bypassing `show_info`.  Keep it out of process logs/terminals so
            # LOG_FULL_TEXT=false genuinely protects narration/transcripts.
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                result = infer(**kwargs)
        except Exception as exc:
            self._error = f"F5-TTS inference lỗi: {exc}"
            raise

        if not target.exists() and isinstance(result, tuple) and len(result) >= 2:
            wav, sample_rate = result[0], result[1]
            sf.write(target, wav, sample_rate, subtype="PCM_16")
        if not target.exists() or target.stat().st_size == 0:
            raise RuntimeError("F5-TTS không tạo được file WAV.")
        return str(target)
