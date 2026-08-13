@echo off
setlocal
cd /d "%~dp0"

python -m venv .venv
if errorlevel 1 goto :failed
call ".venv\Scripts\activate.bat"
python -m pip install --upgrade pip
if errorlevel 1 goto :failed
pip install -r requirements.txt
if errorlevel 1 goto :failed

echo.
echo Core service installed.
echo Before installing F5-TTS, install a PyTorch CPU/CUDA build compatible with this PC.
echo Then run: pip install -r requirements-f5.txt
echo See README.md for the offline model-cache setup.
pause
exit /b 0

:failed
echo Installation failed. See the command output above.
pause
exit /b 1

