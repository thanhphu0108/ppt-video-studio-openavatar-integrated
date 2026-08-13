from __future__ import annotations

import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

import uvicorn
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from api.routes import create_router
from config.settings import get_settings
from services.errors import VoiceCloneServiceError
from services.synthesis_service import SynthesisService


settings = get_settings()
service = SynthesisService(settings)
PID_PATH = settings.root / "voice_clone.pid"


def _previous_pid_from_file() -> str:
    """Preserve another service PID while a nested local TestClient runs.

    Windows does not reliably support ``os.kill(pid, 0)`` as a liveness probe.
    `stop.bat` independently verifies command line ownership before acting on
    this PID, so retaining a syntactically valid prior value is safer here.
    """

    try:
        candidate = PID_PATH.read_text(encoding="utf-8").strip()
        pid = int(candidate)
        if pid > 0 and pid != os.getpid():
            return candidate
    except (OSError, ValueError):
        pass
    return ""


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    """Load the selected engine once and retain a PID for stop.bat.

    A missing model is intentionally non-fatal: `/health`, `/docs`, the UI,
    and configuration endpoints remain usable and report the corrective error.
    """

    previous_live_pid = _previous_pid_from_file()
    PID_PATH.write_text(str(os.getpid()), encoding="utf-8")
    service.warm_up()
    service.logger.info("voice_clone_started host=%s port=%s device=%s", settings.host, settings.port, service.device_info())
    try:
        yield
    finally:
        try:
            if PID_PATH.exists() and PID_PATH.read_text(encoding="utf-8").strip() == str(os.getpid()):
                if previous_live_pid:
                    PID_PATH.write_text(previous_live_pid, encoding="utf-8")
                else:
                    PID_PATH.unlink()
        finally:
            service.logger.info("voice_clone_stopped")


app = FastAPI(
    title="Local Voice Clone Service",
    description="Offline-only Vietnamese voice cloning with local F5-TTS and Wav2Lip hook.",
    version="1.0.0",
    lifespan=lifespan,
)

# Permit only loopback web apps.  This is useful when the PPT app is run
# locally through Streamlit, without making the FastAPI service network-wide.
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://127.0.0.1:8501",
        "http://localhost:8501",
        "http://127.0.0.1:8009",
        "http://localhost:8009",
    ],
    allow_credentials=False,
    allow_methods=["GET", "POST"],
    allow_headers=["Authorization", "Content-Type", "X-API-Key", "X-Voice-Upload-Password"],
)


@app.exception_handler(VoiceCloneServiceError)
async def voice_clone_error(_: Request, exc: VoiceCloneServiceError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content={"success": False, "error_code": exc.code, "message": exc.message},
    )


@app.exception_handler(RequestValidationError)
async def validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
    return JSONResponse(
        status_code=422,
        content={"success": False, "error_code": "VALIDATION_ERROR", "message": "Request không hợp lệ.", "details": exc.errors()},
    )


app.include_router(create_router(service, settings))


if __name__ == "__main__":
    uvicorn.run(app, host=settings.host, port=settings.port, log_level="info")
