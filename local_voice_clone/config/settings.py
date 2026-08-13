from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover - optional during partial setup
    load_dotenv = None


ROOT = Path(__file__).resolve().parents[1]


def _load_local_env() -> None:
    """Load local_voice_clone/.env without overriding process-level values.

    `python-dotenv` remains the preferred loader. A minimal fallback is included
    so security-sensitive local settings such as VOICE_UPLOAD_PASSWORD still
    work if python-dotenv is missing from a secondary virtual environment.
    """

    env_path = ROOT / ".env"
    if not env_path.exists():
        return

    if load_dotenv is not None:
        load_dotenv(env_path, override=False)

    # Fallback / completeness pass. Existing process variables always win.
    # This parser intentionally supports the simple KEY=VALUE format used by
    # this project's .env file; it is not a replacement for python-dotenv.
    try:
        for raw_line in env_path.read_text(encoding="utf-8-sig").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue

            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip()

            if not key or key in os.environ:
                continue

            if (
                len(value) >= 2
                and value[0] == value[-1]
                and value[0] in {"'", '"'}
            ):
                value = value[1:-1]

            os.environ[key] = value
    except OSError:
        # Settings defaults still allow /health to start and report problems.
        pass


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except ValueError:
        return default


def _path_from_env(name: str, default: Path) -> Path:
    value = os.getenv(name, "").strip()
    path = Path(value) if value else default
    return path if path.is_absolute() else ROOT / path


@dataclass(frozen=True)
class Settings:
    root: Path
    host: str
    port: int
    engine: str
    default_voice_id: str
    device: str
    local_api_key: str
    enable_cache: bool
    output_dir: Path
    voice_dir: Path
    cache_dir: Path
    log_dir: Path
    temp_dir: Path
    model_cache_dir: Path
    max_chars_per_chunk: int
    sentence_pause_ms: int
    paragraph_pause_ms: int
    normalize_numbers: bool
    min_reference_seconds: float
    max_reference_seconds: float
    reference_sample_rate: int
    output_sample_rate: int
    wav2lip_endpoint: str
    log_full_text: bool
    f5_model: str
    preload_model: bool
    allow_model_download: bool
    require_upload_password: bool
    voice_upload_password: str

    @classmethod
    def from_env(cls) -> "Settings":
        _load_local_env()

        output_dir = _path_from_env("OUTPUT_DIR", ROOT / "generated_audio")
        requested_host = (
            os.getenv("VOICE_HOST", "127.0.0.1").strip() or "127.0.0.1"
        )

        # This service contains private voice material. Do not accidentally
        # expose it to LAN/Internet because of an inherited environment value.
        host = (
            "127.0.0.1"
            if requested_host != "127.0.0.1"
            else requested_host
        )

        return cls(
            root=ROOT,
            host=host,
            port=_env_int("VOICE_PORT", 8009),
            engine=(
                os.getenv("VOICE_ENGINE", "f5-tts").strip().lower()
                or "f5-tts"
            ),
            default_voice_id=(
                os.getenv("DEFAULT_VOICE_ID", "default").strip()
                or "default"
            ),
            device=os.getenv("DEVICE", "auto").strip().lower() or "auto",
            local_api_key=os.getenv("LOCAL_API_KEY", "").strip(),
            enable_cache=_env_bool("ENABLE_CACHE", True),
            output_dir=output_dir,
            voice_dir=_path_from_env("VOICE_DIR", ROOT / "voices"),
            cache_dir=_path_from_env("CACHE_DIR", ROOT / "cache"),
            log_dir=_path_from_env("LOG_DIR", ROOT / "logs"),
            temp_dir=_path_from_env("TEMP_DIR", ROOT / "temp"),
            model_cache_dir=_path_from_env(
                "MODEL_CACHE_DIR",
                ROOT / "models",
            ),
            max_chars_per_chunk=max(
                50,
                _env_int("MAX_CHARS_PER_CHUNK", 350),
            ),
            sentence_pause_ms=max(
                0,
                _env_int("SENTENCE_PAUSE_MS", 180),
            ),
            paragraph_pause_ms=max(
                0,
                _env_int("PARAGRAPH_PAUSE_MS", 350),
            ),
            normalize_numbers=_env_bool("NORMALIZE_NUMBERS", True),
            min_reference_seconds=max(
                0.5,
                _env_float("MIN_REFERENCE_SECONDS", 2.0),
            ),
            max_reference_seconds=max(
                3.0,
                _env_float("MAX_REFERENCE_SECONDS", 12.0),
            ),
            reference_sample_rate=max(
                8_000,
                _env_int("REFERENCE_SAMPLE_RATE", 24_000),
            ),
            output_sample_rate=max(
                8_000,
                _env_int("OUTPUT_SAMPLE_RATE", 24_000),
            ),
            wav2lip_endpoint=os.getenv(
                "WAV2LIP_ENDPOINT",
                "http://127.0.0.1:8008",
            ).rstrip("/"),
            log_full_text=_env_bool("LOG_FULL_TEXT", False),
            f5_model=(
                os.getenv("F5_MODEL", "F5TTS_v1_Base").strip()
                or "F5TTS_v1_Base"
            ),
            preload_model=_env_bool("PRELOAD_MODEL", True),
            allow_model_download=_env_bool(
                "ALLOW_MODEL_DOWNLOAD",
                False,
            ),
            require_upload_password=_env_bool(
                "REQUIRE_UPLOAD_PASSWORD",
                True,
            ),
            voice_upload_password=os.getenv(
                "VOICE_UPLOAD_PASSWORD",
                "",
            ),
        )

    @property
    def voices_registry_path(self) -> Path:
        return self.root / "config" / "voices.json"

    @property
    def ffmpeg(self) -> str | None:
        return shutil.which("ffmpeg")

    def ensure_directories(self) -> None:
        for folder in (
            self.output_dir,
            self.voice_dir,
            self.cache_dir,
            self.log_dir,
            self.temp_dir,
            self.model_cache_dir,
        ):
            folder.mkdir(parents=True, exist_ok=True)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    settings = Settings.from_env()
    settings.ensure_directories()
    return settings
