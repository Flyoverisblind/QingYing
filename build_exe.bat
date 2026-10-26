@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo [QingYing] Creating virtual environment...
  python -m venv .venv
  ".venv\Scripts\python.exe" -m pip install --upgrade pip
  ".venv\Scripts\python.exe" -m pip install -r requirements-dev.txt
)
echo [QingYing] Building portable executable...
".venv\Scripts\python.exe" build_exe.py
if errorlevel 1 (
  echo.
  echo Build failed.
  pause
  exit /b 1
)
echo.
echo Done. See the dist folder.
pause
