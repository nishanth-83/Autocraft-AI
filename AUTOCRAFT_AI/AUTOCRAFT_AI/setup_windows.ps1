# ============================================================================
# AUTOCRAFT AI - Windows 10/11 PowerShell Setup Script
# ============================================================================
$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $projectRoot

Write-Host "============================================================================" -ForegroundColor Cyan
Write-Host "AUTOCRAFT AI - Windows 10/11 PowerShell Setup" -ForegroundColor Cyan
Write-Host "============================================================================" -ForegroundColor Cyan

if (-not (Get-Command python -ErrorAction SilentlyContinue)) {
    Write-Error "Python 3.10+ is required on PATH. Download from https://www.python.org/"
    exit 1
}

if (-not (Test-Path ".\venv")) {
    Write-Host "[1/5] Creating Python virtual environment (.\venv)..."
    python -m venv venv
}

Write-Host "[2/5] Installing Python dependencies..."
& .\venv\Scripts\python.exe -m pip install --upgrade pip
& .\venv\Scripts\pip.exe install -r requirements.txt

Write-Host "[3/5] Installing Playwright browser binaries..."
& .\venv\Scripts\playwright.exe install chromium

if (-not (Test-Path ".\.env")) {
    Write-Host "[4/5] Initializing .env from .env.example..."
    Copy-Item ".\.env.example" ".\.env"
}

if (-not (Test-Path ".\workspace")) {
    New-Item -ItemType Directory -Path ".\workspace" | Out-Null
}

if (Get-Command npm -ErrorAction SilentlyContinue) {
    Write-Host "[5/5] Installing frontend packages..."
    Push-Location .\frontend
    npm install
    if ($LASTEXITCODE -ne 0) {
        Pop-Location
        throw "Frontend dependency installation failed."
    }
    npm run build
    if ($LASTEXITCODE -ne 0) {
        Pop-Location
        throw "Frontend production build failed."
    }
    Pop-Location
} else {
    Write-Host "[5/5] Pre-built dashboard assets in .\frontend\dist are ready to serve."
}

Write-Host "Setup complete! Execute .\start_autocraft.bat or .\start_autocraft.ps1 to start." -ForegroundColor Green
