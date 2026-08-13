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
    """Production-local adapter for official ``f5_tts.api.F5TTS``.

    Safety / runtime rules:
    - Local-only by default: no implicit Hugging Face download.
    - Requires an explicit reference transcript in offline mode.
    - Uses local Vocos when available.
    - Prefers explicit local F5 checkpoint/vocab when the installed API exposes
      compatible constructor parameters.
    - Normalizes generated WAV if peak level risks clipping.
    """

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

        root = Path(__file__).resolve().parents[1]
        self.model_dir = Path(
            os.getenv(
                "F5_TTS_MODEL_DIR",
                str(root / "models" / "F5-TTS" / self.model_name),
            )
        ).resolve()

        self.ckpt_file = Path(
            os.getenv(
                "F5_TTS_CKPT_FILE",
                str(self.model_dir / "model_1250000.safetensors"),
            )
        ).resolve()

        self.vocab_file = Path(
            os.getenv(
                "F5_TTS_VOCAB_FILE",
                str(self.model_dir / "vocab.txt"),
            )
        ).resolve()
        self.vocoder_dir = Path(
            os.getenv(
                "F5_TTS_VOCODER_DIR",
                str(root / "models" / "vocos-mel-24khz"),
            )
        ).resolve()

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
        for stream in (sys.stdout, sys.stderr):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except (AttributeError, OSError):
                pass

    @staticmethod
    def _enable_soundfile_wav_fallback() -> None:
        """Read normalized local WAV files with SoundFile.

        This is deliberately narrow. Non-WAV sources retain torchaudio's normal
        loading path.
        """
        try:
            import torch
            import torchaudio
        except ImportError:
            return

        if getattr(
            torchaudio.load,
            "_local_voice_clone_soundfile_fallback",
            False,
        ):
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

            samples, sample_rate = sf.read(
                path,
                always_2d=True,
                dtype="float32",
            )

            start = max(0, int(frame_offset))
            end = (
                None
                if num_frames is None or int(num_frames) < 0
                else start + int(num_frames)
            )
            samples = np.ascontiguousarray(samples[start:end])

            tensor = torch.from_numpy(
                samples.T if channels_first else samples
            )
            return tensor, int(sample_rate)

        setattr(
            load_wav_with_soundfile,
            "_local_voice_clone_soundfile_fallback",
            True,
        )
        torchaudio.load = load_wav_with_soundfile

    def _validate_local_assets(self) -> None:
        if self.allow_model_download:
            return

        vocos_config = self.vocoder_dir / "config.yaml"
        vocos_model = self.vocoder_dir / "pytorch_model.bin"

        if not vocos_config.exists():
            raise EngineUnavailableError(
                f"Thiếu Vocos config local: {vocos_config}"
            )
        if not vocos_model.exists():
            raise EngineUnavailableError(
                f"Thiếu Vocos model local: {vocos_model}"
            )

        # F5 checkpoint is validated only when we intend to bind it explicitly.
        # Some official F5 versions resolve the checkpoint from the HF cache
        # using the model name even in offline mode.
        local_ckpt = self.ckpt_file
        local_vocab = self.vocab_file

        if not local_ckpt.exists():
            raise EngineUnavailableError(
                f"Thiếu F5-TTS checkpoint local: {local_ckpt}"
            )
        if not local_vocab.exists():
            raise EngineUnavailableError(
                f"Thiếu F5-TTS vocab local: {local_vocab}"
            )

    def load(self) -> None:
        if self._model is not None:
            return

        self._resolved_device = self._device()
        self._configure_utf8_console()
        self._enable_soundfile_wav_fallback()

        if not self.allow_model_download:
            os.environ["HF_HUB_OFFLINE"] = "1"
            os.environ["TRANSFORMERS_OFFLINE"] = "1"

        try:
            from f5_tts.api import F5TTS
        except ImportError as exc:
            self._error = (
                "Chưa cài F5-TTS. "
                "Chạy `pip install -r requirements-f5.txt`."
            )
            raise EngineUnavailableError(self._error) from exc

        try:
            self._validate_local_assets()

            signature = inspect.signature(F5TTS)
            kwargs: dict[str, Any] = {}

            if "model" in signature.parameters:
                kwargs["model"] = self.model_name

            if "device" in signature.parameters:
                kwargs["device"] = self._resolved_device

            if (
                "hf_cache_dir" in signature.parameters
                and self.model_cache_dir is not None
            ):
                self.model_cache_dir.mkdir(
                    parents=True,
                    exist_ok=True,
                )
                kwargs["hf_cache_dir"] = str(self.model_cache_dir)

            # Use local Vocos so synthesis never needs to fetch it.
            if "vocoder_local_path" in signature.parameters:
                kwargs["vocoder_local_path"] = str(self.vocoder_dir)

            # Different F5-TTS releases expose different local checkpoint
            # parameter names. Bind only when the installed API supports them.
            ckpt = self.ckpt_file
            vocab = self.vocab_file

            if "ckpt_file" in signature.parameters:
                kwargs["ckpt_file"] = str(ckpt)
            elif "ckpt_path" in signature.parameters:
                kwargs["ckpt_path"] = str(ckpt)

            if "vocab_file" in signature.parameters:
                kwargs["vocab_file"] = str(vocab)

            self._model = F5TTS(**kwargs)
            self._error = ""

        except Exception as exc:
            self._error = (
                f"Không tải được F5-TTS: {exc} | "
                f"model={self.model_name} | "
                f"ckpt={self.ckpt_file} | "
                f"vocab={self.vocab_file}"
            )
            raise EngineUnavailableError(self._error) from exc

    def status(self) -> EngineStatus:
        if self._model is not None:
            return EngineStatus(
                id=self.id,
                available=True,
                loaded=True,
                device=self._resolved_device,
                model=self.model_name,
                message=(
                    f"checkpoint={self.ckpt_file.name}; "
                    f"vocab={self.vocab_file.name}"
                ),
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

    @staticmethod
    def _normalize_if_clipping_risk(
        path: Path,
        target_peak: float = 0.891250938,  # -1 dBFS
    ) -> None:
        """Apply conservative peak normalization only when needed."""
        audio, sr = sf.read(
            path,
            always_2d=False,
            dtype="float32",
        )

        if audio.size == 0:
            return

        peak = float(np.max(np.abs(audio)))
        if peak <= 0:
            return

        # Normalize only when the waveform is close to full scale.
        if peak >= 0.98:
            gain = target_peak / peak
            audio = np.clip(audio * gain, -1.0, 1.0)
            sf.write(
                path,
                audio,
                sr,
                subtype="PCM_16",
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

        if self._model is None:
            raise EngineUnavailableError(
                self._error or "F5-TTS chưa được load."
            )

        if not reference_text or not reference_text.strip():
            raise EngineUnavailableError(
                "Thiếu reference_text. Chế độ local/offline yêu cầu "
                "transcript đúng với reference audio; không tự gọi ASR "
                "hoặc Hugging Face."
            )

        target = Path(output_path).resolve()
        target.parent.mkdir(parents=True, exist_ok=True)

        infer = self._model.infer
        signature = inspect.signature(infer)

        kwargs: dict[str, Any] = {
            "ref_file": str(Path(reference_audio).resolve()),
            "ref_text": reference_text.strip(),
            "gen_text": text,
            "file_wave": str(target),
        }

        if "speed" in signature.parameters:
            kwargs["speed"] = speed
        if "language" in signature.parameters:
            kwargs["language"] = language
        if "show_info" in signature.parameters:
            kwargs["show_info"] = lambda *_args, **_kwargs: None

        try:
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                result = infer(**kwargs)
        except Exception as exc:
            self._error = f"F5-TTS inference lỗi: {exc}"
            raise

        if (
            not target.exists()
            and isinstance(result, tuple)
            and len(result) >= 2
        ):
            wav, sample_rate = result[0], result[1]
            sf.write(
                target,
                wav,
                sample_rate,
                subtype="PCM_16",
            )

        if not target.exists() or target.stat().st_size == 0:
            raise RuntimeError("F5-TTS không tạo được file WAV.")

        self._normalize_if_clipping_risk(target)
        return str(target)
