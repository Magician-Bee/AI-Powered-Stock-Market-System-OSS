$ErrorActionPreference = "Stop"

$Utf8NoBom = New-Object System.Text.UTF8Encoding -ArgumentList $false
[Console]::InputEncoding = $Utf8NoBom
[Console]::OutputEncoding = $Utf8NoBom
$OutputEncoding = $Utf8NoBom

$ProjectRoot = $PSScriptRoot
$ProjectRoot = [IO.Path]::GetFullPath($ProjectRoot).TrimEnd([IO.Path]::DirectorySeparatorChar)
$LogDir = Join-Path $ProjectRoot "logs"
$PidFile = Join-Path $LogDir "stock-ai-server.pid"
$PortFile = Join-Path $LogDir "stock-ai-server.port"
$CommitFile = Join-Path $LogDir "stock-ai-server.commit"
$RootFile = Join-Path $LogDir "stock-ai-server.root"
$InstanceFile = Join-Path $LogDir "stock-ai-server.instance"
$StateFiles = @($PidFile, $PortFile, $CommitFile, $RootFile, $InstanceFile)
$Stopped = $false

function Read-FirstLine([string]$Path) {
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        return ""
    }
    return [string](Get-Content -LiteralPath $Path -TotalCount 1 -ErrorAction SilentlyContinue)
}

function Stop-VerifiedStockAiProcess([int]$ProcessId) {
    if ($ProcessId -le 0) {
        return $false
    }

    $process = Get-CimInstance Win32_Process -Filter "ProcessId=$ProcessId" -ErrorAction SilentlyContinue
    if (-not $process) {
        return $false
    }

    $commandLine = [string]$process.CommandLine
    if ($commandLine -notmatch '(?i)uvicorn\s+stock_ai\.main:app') {
        return $false
    }

    Stop-Process -Id $ProcessId -Force -ErrorAction Stop
    return $true
}

try {
    $trackedRoot = Read-FirstLine $RootFile
    $pidText = Read-FirstLine $PidFile

    if ($trackedRoot -eq $ProjectRoot -and $pidText -match '^\d+$') {
        $Stopped = Stop-VerifiedStockAiProcess ([int]$pidText)
    }

    Remove-Item -LiteralPath $StateFiles -Force -ErrorAction SilentlyContinue

    if ($Stopped) {
        Write-Host "Stock AI System stopped." -ForegroundColor Green
    } else {
        Write-Host "No Stock AI System process owned by this folder was running." -ForegroundColor Yellow
    }
    exit 0
} catch {
    Write-Host "Could not stop Stock AI System: $($_.Exception.Message)" -ForegroundColor Red
    exit 1
}
