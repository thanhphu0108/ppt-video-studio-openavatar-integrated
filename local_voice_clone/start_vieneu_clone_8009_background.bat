@echo off
setlocal
cd /d "%~dp0"

powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0start_vieneu_clone_8009_background.ps1"
set "EXITCODE=%ERRORLEVEL%"
if not "%EXITCODE%"=="0" (
    echo Khong the khoi dong VieNeu service nen. Exit code: %EXITCODE%
    exit /b %EXITCODE%
)

echo Khong giu terminal/IDE. Cho model nap trong process rieng.
exit /b 0
