@echo off
setlocal
cd /d "%~dp0"

if not exist "voice_clone.pid" (
    echo No voice_clone.pid found. The service may already be stopped.
    exit /b 0
)

set /p VOICE_PID=<"voice_clone.pid"
echo %VOICE_PID%| findstr /r "^[0-9][0-9]*$" >nul
if errorlevel 1 (
    echo Invalid PID file. It was not used to stop any process.
    exit /b 1
)

REM Verify this is the local service before terminating one explicit PID.
powershell -NoProfile -Command "$p=Get-CimInstance Win32_Process -Filter 'ProcessId = %VOICE_PID%'; if ($null -eq $p) { exit 0 }; if ($p.CommandLine -notmatch '--local-voice-clone') { Write-Error 'PID does not belong to Local Voice Clone; refusing to stop it.'; exit 1 }; Stop-Process -Id $p.ProcessId -Force"
if errorlevel 1 exit /b 1

del /q "voice_clone.pid" 2>nul
echo Local Voice Clone Service stopped.
exit /b 0
