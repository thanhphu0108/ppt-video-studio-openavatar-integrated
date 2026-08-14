@echo off
setlocal
cd /d "%~dp0"

title Local Voice Clone - VieNeu v3 Turbo Vietnamese Clone - Port 8009

echo ============================================================
echo Local Voice Clone - VieNeu v3 Turbo Vietnamese Clone
echo Host   : http://127.0.0.1:8009
echo Python : .venv
echo GPU    : PyTorch CUDA / GTX 1070 Ti
echo ============================================================
echo.

if not exist ".venv\Scripts\python.exe" (
    echo ERROR: Khong tim thay .venv\Scripts\python.exe
    echo Thu muc hien tai: %CD%
    pause
    exit /b 2
)

REM TorchCodec/FFmpeg on Windows needs the full-shared DLL directory on the
REM process search path. The repository already carries this local runtime.
set "FFMPEG_BIN=%~dp0third_party\ffmpeg8-shared\ffmpeg-8.1.1-full_build-shared\bin"
if exist "%FFMPEG_BIN%\ffmpeg.exe" (
    set "PATH=%FFMPEG_BIN%;%PATH%"
    echo FFmpeg DLLs         : %FFMPEG_BIN%
) else (
    echo WARNING: Khong tim thay FFmpeg full-shared tai %FFMPEG_BIN%
)

REM ============================================================
REM Authentication
REM ============================================================
set "LOCAL_API_KEY=123456"
set "REQUIRE_UPLOAD_PASSWORD=true"
set "VOICE_UPLOAD_PASSWORD=123456"

REM ============================================================
REM Service
REM ============================================================
set "VOICE_ENGINE=vieneu"
set "DEVICE=auto"
set "PRELOAD_MODEL=true"
set "ENABLE_CACHE=true"
set "LOG_FULL_TEXT=false"

REM VieNeu v3 Turbo cloning needs the PyTorch backend.  Set this to auto
REM when testing on a machine without a working CUDA installation.
set "VIENEU_BACKEND=pytorch"
set "VIENEU_USE_SOUNDFILE_REFERENCE=true"

REM ============================================================
REM Audio / local runtime
REM ============================================================
set "REFERENCE_SAMPLE_RATE=24000"
set "OUTPUT_SAMPLE_RATE=48000"
set "MIN_REFERENCE_SECONDS=2"
set "MAX_REFERENCE_SECONDS=12"
set "WAV2LIP_ENDPOINT=http://127.0.0.1:8008"

echo Engine                : %VOICE_ENGINE%
echo VieNeu backend        : %VIENEU_BACKEND%
echo API key/password      : configured
echo.
echo Reference: dung mau giong tieng Viet ro, khoang 3-8 giay.
echo Model se duoc nap mot lan va tai su dung cho cac slide tiep theo.
echo Neu lan dau can tai checkpoint tu Hugging Face, hay cho service tai xong.
echo.
echo Starting VieNeu clone service on port 8009...
echo.

".venv\Scripts\python.exe" app.py
set "EXITCODE=%ERRORLEVEL%"

echo.
echo VieNeu clone service da dung. Exit code: %EXITCODE%
pause
exit /b %EXITCODE%
