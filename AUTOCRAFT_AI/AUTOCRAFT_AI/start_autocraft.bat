@echo off
SETLOCAL
cd /d "%~dp0"

echo ============================================================================
echo AUTOCRAFT AI - Autonomous Desktop Agent Startup
echo ============================================================================

IF NOT EXIST "venv\Scripts\python.exe" (
    echo [ERROR] Project virtual environment not found. Run setup_windows.bat first.
    exit /b 1
)

IF NOT EXIST "workspace" mkdir workspace

echo Starting FastAPI Backend & Dashboard at http://127.0.0.1:8000 ...
start "" http://127.0.0.1:8000
"venv\Scripts\python.exe" -m uvicorn backend.main:app --host 127.0.0.1 --port 8000
ENDLOCAL
