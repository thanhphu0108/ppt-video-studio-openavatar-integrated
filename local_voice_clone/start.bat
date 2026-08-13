@echo off
setlocal
cd /d "%~dp0"

if exist ".venv\Scripts\activate.bat" (
    call ".venv\Scripts\activate.bat"
)

set PYTHONUTF8=1
python app.py --local-voice-clone
set "VOICE_EXIT=%ERRORLEVEL%"
echo.
echo Local Voice Clone Service stopped with code %VOICE_EXIT%.
pause
exit /b %VOICE_EXIT%
