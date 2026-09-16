$ErrorActionPreference = "Stop"
$ProjectRoot = $PSScriptRoot
$Url = "http://127.0.0.1:8000/"
$Health = "http://127.0.0.1:8000/health"
$LogDir = Join-Path $ProjectRoot "logs"
$PidFile = Join-Path $LogDir "stock-ai-server.pid"
$LogFile = Join-Path $LogDir "stock-ai-server.log"

New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
Set-Location -LiteralPath $ProjectRoot

function Test-StockAiServer {
    try {
        $res = Invoke-WebRequest -Uri $Health -UseBasicParsing -TimeoutSec 2
        return ($res.StatusCode -eq 200)
    } catch {
        return $false
    }
}

Write-Host "=== Stock AI System Launcher ===" -ForegroundColor Cyan
Write-Host "Project: $ProjectRoot"

if (Test-StockAiServer) {
    Write-Host "Server is already running. Opening browser..." -ForegroundColor Green
    Start-Process $Url
    exit 0
}

Write-Host "Installing/syncing dependencies..." -ForegroundColor Cyan
uv sync --extra dev

Write-Host "Starting backend server..." -ForegroundColor Cyan
$proc = Start-Process -FilePath "uv" `
    -ArgumentList @("run", "python", "-m", "uvicorn", "stock_ai.main:app", "--host", "127.0.0.1", "--port", "8000") `
    -WorkingDirectory $ProjectRoot `
    -WindowStyle Minimized `
    -RedirectStandardOutput $LogFile `
    -RedirectStandardError $LogFile `
    -PassThru

$proc.Id | Set-Content -Encoding ascii -Path $PidFile

Write-Host "Waiting for server..." -ForegroundColor Cyan
$ready = $false
for ($i = 0; $i -lt 30; $i++) {
    Start-Sleep -Milliseconds 500
    if (Test-StockAiServer) {
        $ready = $true
        break
    }
}

if (-not $ready) {
    Write-Host "Startup failed. Log: $LogFile" -ForegroundColor Red
    if (Test-Path $LogFile) { Get-Content $LogFile -Tail 40 }
    Read-Host "Press Enter to close"
    exit 1
}

Write-Host "Started. Opening browser..." -ForegroundColor Green
Start-Process $Url
Write-Host "GUI: $Url" -ForegroundColor Green
Write-Host "API Docs: http://127.0.0.1:8000/docs" -ForegroundColor Green
Start-Sleep -Seconds 2
