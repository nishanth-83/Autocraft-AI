# ============================================================================
# AUTOCRAFT AI - Windows 10/11 PowerShell Startup Script
# ============================================================================
$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $projectRoot

Write-Host "Starting AUTOCRAFT AI on http://127.0.0.1:8000 ..." -ForegroundColor Cyan

$pythonExe = Join-Path $projectRoot "venv\Scripts\python.exe"
if (-not (Test-Path $pythonExe)) {
    throw "Project virtual environment not found. Run .\setup_windows.ps1 first."
}

Start-Process "http://127.0.0.1:8000"
& $pythonExe -m uvicorn backend.main:app --host 127.0.0.1 --port 8000
