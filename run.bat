@echo off
setlocal
cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 (
  echo Python 3.10 or newer is required. Install it from https://python.org and try again.
  pause
  exit /b 1
)

python -c "import flask, yt_dlp, imageio_ffmpeg" >nul 2>nul
if errorlevel 1 (
  echo Installing required packages, this only happens once...
  python -m pip install --upgrade pip >nul
  python -m pip install -r requirements.txt
  if errorlevel 1 (
    echo Installation failed. Check your internet connection and try again.
    pause
    exit /b 1
  )
)

if not exist "downloads" mkdir "downloads"
if not exist ".tmp" mkdir ".tmp"

echo.
echo   Starting Audion Extractor. Your browser will open shortly.
echo   Keep this window open while you use the app. Press Ctrl+C to stop.
echo.
python app.py
pause
