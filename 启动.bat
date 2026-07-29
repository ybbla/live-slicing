@echo off
chcp 65001 >nul
cd /d "%~dp0"
title Live Slicing

echo ========================================
echo   Smart Live Stream Slicer
echo ========================================
echo.

REM Check Python
where python >nul 2>nul
if errorlevel 1 (
    echo [ERROR] Python not found. Please install Python 3.10+
    echo   https://www.python.org/downloads/
    echo.
    pause
    exit /b 1
)

REM Check dependencies
python -c "import flask, requests, openai, numpy, PIL" >nul 2>nul
if errorlevel 1 (
    echo [INFO] Installing dependencies...
    python -m pip install -r requirements.txt
    if errorlevel 1 (
        echo [ERROR] Failed to install deps. Run manually: pip install -r requirements.txt
        echo.
        pause
        exit /b 1
    )
    echo.
)

REM Check ffmpeg
where ffmpeg >nul 2>nul
if errorlevel 1 (
    echo [ERROR] ffmpeg not found. Install with:
    echo   winget install --id Gyan.FFmpeg -e
    echo.
    pause
    exit /b 1
)

REM Check .env
if not exist ".env" (
    echo [ERROR] .env file not found.
    echo   Copy .env.example to .env and fill in VOLC_APP_KEY and ARK_API_KEY.
    echo.
    pause
    exit /b 1
)

echo Project dir: %~dp0
echo Starting web server on http://localhost:5876
echo Close this window to stop.
echo.

REM Browser is opened automatically by Python (1.5s delay after Flask starts)
REM Start Flask
python web.py
set exit_code=%errorlevel%

if not %exit_code%==0 (
    echo.
    echo [ERROR] Server exited with code %exit_code%
    echo.
    pause
)
