@echo off
setlocal
cd /d %~dp0

if not exist ".venv\Scripts\activate.bat" (
  echo [ERROR] Khong tim thay .venv
  pause
  exit /b 1
)

call ".venv\Scripts\activate.bat"

set "VOICE_ENGINE=f5-tts"
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"

set "FFMPEG_SHARED_BIN=%CD%\third_party\ffmpeg8-shared\ffmpeg-8.1.1-full_build-shared\bin"
if not exist "%FFMPEG_SHARED_BIN%\ffmpeg.exe" (
  echo [ERROR] Khong tim thay FFmpeg 8 shared:
  echo %FFMPEG_SHARED_BIN%
  pause
  exit /b 1
)
set "PATH=%FFMPEG_SHARED_BIN%;%PATH%"

set "F5_TTS_MODEL_DIR=%CD%\models\F5-TTS\F5TTS_v1_Base"
set "F5_TTS_VOCODER_DIR=%CD%\models\vocos-mel-24khz"

set "HF_HUB_OFFLINE=1"
set "TRANSFORMERS_OFFLINE=1"

echo ============================================================
echo Local Voice Clone
echo Engine : %VOICE_ENGINE%
echo Host   : http://127.0.0.1:8009
echo FFmpeg : %FFMPEG_SHARED_BIN%
echo ============================================================

python app.py
set EXITCODE=%ERRORLEVEL%

echo.
echo Voice Clone service da dung. Exit code: %EXITCODE%
pause
exit /b %EXITCODE%
