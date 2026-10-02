# run.ps1 — Launch the CareVoice AI FastAPI server on Windows
#
# Usage:
#   .\run.ps1                 # default: 0.0.0.0:8000 with hot-reload
#   .\run.ps1 -Port 9000      # custom port
#   .\run.ps1 -NoReload       # disable hot-reload (production)
#
# Uses the project virtualenv at ..\.venv if present, else falls back to `python`.

param (
    [string] $BindHost = "0.0.0.0",
    [int]    $Port     = 8000,
    [switch] $NoReload,
    [string] $LogLevel = "info"
)

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $ScriptDir

# Prefer the project venv python so dependencies resolve correctly.
$VenvPython = Join-Path $ScriptDir "..\.venv\Scripts\python.exe"
$Python = if (Test-Path $VenvPython) { $VenvPython } else { "python" }

Write-Host ""
Write-Host "  CareVoice AI - FastAPI Gateway" -ForegroundColor Cyan
Write-Host "  http://${BindHost}:${Port}/docs   (Swagger UI)"   -ForegroundColor Green
Write-Host "  http://${BindHost}:${Port}/health (health check)" -ForegroundColor Green
Write-Host ""

$args = @("-m", "uvicorn", "main:app", "--host", $BindHost, "--port", $Port, "--log-level", $LogLevel)
if (-not $NoReload) { $args += "--reload" }

& $Python @args
