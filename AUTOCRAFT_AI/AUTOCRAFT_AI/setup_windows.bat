@echo off
SETLOCAL EnableDelayedExpansion
cd /d "%~dp0"

echo ============================================================================
echo AUTOCRAFT AI - Windows 10/11 Setup Script
echo ============================================================================

python --version >nul 2>&1
IF %ERRORLEVEL% NEQ 0 (
    echo [ERROR] Python 3.10+ is not installed or not on PATH.
    echo Please install Python from https://www.python.org/downloads/windows/
    exit /b 1
)

IF NOT EXIST "venv" (
    echo [1/5] Creating Python virtual environment in .\venv ...
    python -m venv venv
) ELSE (
    echo [1/5] Existing virtual environment found in .\venv
)

echo [2/5] Installing Python dependencies from requirements.txt ...
call venv\Scripts\activate.bat
python -m pip install --upgrade pip
pip install -r requirements.txt

echo [3/5] Installing Playwright browser drivers ...
playwright install chromium

IF NOT EXIST ".env" (
    echo [4/5] Creating .env from .env.example ...
    copy .env.example .env >nul
)
IF NOT EXIST "workspace" mkdir workspace

where npm >nul 2>&1
IF %ERRORLEVEL% EQU 0 (
    echo [5/5] Installing Frontend dependencies in .\frontend ...
    pushd frontend
    call npm install
    IF !ERRORLEVEL! NEQ 0 (
        popd
        echo [ERROR] Frontend dependency installation failed.
        exit /b 1
    )
    call npm run build
    IF !ERRORLEVEL! NEQ 0 (
        popd
        echo [ERROR] Frontend production build failed.
        exit /b 1
    )
    popd
) ELSE (
    echo [5/5] Node.js not detected on PATH; pre-built frontend bundle in .\frontend\dist will be served directly by FastAPI.
)

echo ============================================================================
echo Setup Complete! Run start_autocraft.bat to launch AUTOCRAFT AI.
echo ============================================================================
ENDLOCAL
