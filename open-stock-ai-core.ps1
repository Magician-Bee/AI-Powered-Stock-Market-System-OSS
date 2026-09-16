$ErrorActionPreference = "Stop"

$Utf8NoBom = New-Object System.Text.UTF8Encoding -ArgumentList $false
[Console]::InputEncoding = $Utf8NoBom
[Console]::OutputEncoding = $Utf8NoBom
$OutputEncoding = $Utf8NoBom
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"

$ProjectRoot = $PSScriptRoot
$ProjectRoot = [IO.Path]::GetFullPath($ProjectRoot).TrimEnd([IO.Path]::DirectorySeparatorChar)
$DefaultPort = 8000
$Port = $DefaultPort
$UvVersion = "0.11.28"
$RuntimeRoot = Join-Path $ProjectRoot ".runtime"
$ToolsDir = Join-Path $RuntimeRoot "tools\windows"
$CodexDir = Join-Path $RuntimeRoot "tools\codex"
$CodexExe = Join-Path $CodexDir "codex.exe"
$VenvDir = Join-Path $RuntimeRoot "venv-windows"
$PythonInstallDir = Join-Path $RuntimeRoot "python"
$CacheDir = Join-Path $RuntimeRoot "cache"
$UvExe = Join-Path $ToolsDir "uv.exe"
$VenvPython = Join-Path $VenvDir "Scripts\python.exe"
$LogDir = Join-Path $ProjectRoot "logs"
$PidFile = Join-Path $LogDir "stock-ai-server.pid"
$PortFile = Join-Path $LogDir "stock-ai-server.port"
$CommitFile = Join-Path $LogDir "stock-ai-server.commit"
$RootFile = Join-Path $LogDir "stock-ai-server.root"
$InstanceFile = Join-Path $LogDir "stock-ai-server.instance"
$StdoutLog = Join-Path $LogDir "stock-ai-server.out.log"
$StderrLog = Join-Path $LogDir "stock-ai-server.err.log"

function Get-ProjectInstanceId {
    $normalizedRoot = $ProjectRoot.ToLowerInvariant()
    $sha256 = [Security.Cryptography.SHA256]::Create()
    try {
        $bytes = [Text.Encoding]::UTF8.GetBytes($normalizedRoot)
        $hash = $sha256.ComputeHash($bytes)
        return ([BitConverter]::ToString($hash).Replace("-", "").ToLowerInvariant().Substring(0, 16))
    } finally {
        $sha256.Dispose()
    }
}

function Get-GitMetadata {
    $commit = "working-copy"
    $branch = "archive-or-working-copy"
    $git = Get-Command git.exe -ErrorAction SilentlyContinue
    if (-not $git) {
        $git = Get-Command git -ErrorAction SilentlyContinue
    }
    if ($git -and (Test-Path -LiteralPath (Join-Path $ProjectRoot ".git"))) {
        try {
            $candidateCommit = (& $git.Source -C $ProjectRoot rev-parse --short=12 HEAD 2>$null | Select-Object -First 1)
            if ($LASTEXITCODE -eq 0 -and $candidateCommit) {
                $commit = [string]$candidateCommit
            }
            $candidateBranch = (& $git.Source -C $ProjectRoot branch --show-current 2>$null | Select-Object -First 1)
            if ($LASTEXITCODE -eq 0 -and $candidateBranch) {
                $branch = [string]$candidateBranch
            } elseif ($commit -ne "working-copy") {
                $branch = "detached"
            }
        } catch {
            # ZIP downloads and Git-less machines are supported working copies.
        }
    }
    return @{
        Commit = $commit.Trim()
        Branch = $branch.Trim()
    }
}

$ProjectInstanceId = Get-ProjectInstanceId
$GitMetadata = Get-GitMetadata
$GitCommit = $GitMetadata.Commit
$GitBranch = $GitMetadata.Branch

function Set-ServiceUrls {
    $script:Url = "http://127.0.0.1:$script:Port/"
    $script:HealthUrl = "${script:Url}health"
    $script:CapabilitiesUrl = "${script:Url}api/codex/capabilities"
}

Set-ServiceUrls

function Write-Step([string]$Message) {
    Write-Host "[Stock AI] $Message" -ForegroundColor Cyan
}

function Get-ProcessCommandLine([int]$ProcessId) {
    try {
        $process = Get-CimInstance Win32_Process -Filter "ProcessId=$ProcessId" -ErrorAction SilentlyContinue
        if ($process) {
            return [string]$process.CommandLine
        }
    } catch {
        return ""
    }
    return ""
}

function Test-LauncherOwnedProcess([int]$ProcessId) {
    if ($ProcessId -le 0) {
        return $false
    }
    $process = Get-Process -Id $ProcessId -ErrorAction SilentlyContinue
    if (-not $process) {
        return $false
    }
    $commandLine = Get-ProcessCommandLine $ProcessId
    return $commandLine -match '(?i)uvicorn\s+stock_ai\.main:app'
}

function Get-RuntimeSessionToken([string]$Html) {
    if ([string]::IsNullOrWhiteSpace($Html)) {
        return ""
    }
    $match = [regex]::Match(
        $Html,
        '<meta\s+name=["'']stock-ai-runtime-session["'']\s+content=["'']([^"'']+)["'']\s*/?>',
        [Text.RegularExpressions.RegexOptions]::IgnoreCase
    )
    if ($match.Success) {
        return $match.Groups[1].Value
    }
    return ""
}

function Test-StockAiServer {
    try {
        $health = Invoke-RestMethod -Uri $HealthUrl -UseBasicParsing -TimeoutSec 2
        if (
            $health.status -ne "ok" -or
            $health.system_id -ne "stock-ai-system" -or
            [string]$health.build_commit -ne $GitCommit -or
            [string]$health.instance_id -ne $ProjectInstanceId -or
            [IO.Path]::GetFullPath([string]$health.project_root).TrimEnd([IO.Path]::DirectorySeparatorChar) -ne $ProjectRoot
        ) {
            return $false
        }

        $index = Invoke-WebRequest -Uri $Url -UseBasicParsing -TimeoutSec 2
        $runtimeSessionToken = Get-RuntimeSessionToken ([string]$index.Content)
        if ([string]::IsNullOrWhiteSpace($runtimeSessionToken)) {
            return $false
        }

        $capabilities = Invoke-WebRequest `
            -Uri $CapabilitiesUrl `
            -Headers @{ "X-Stock-AI-Session" = $runtimeSessionToken } `
            -UseBasicParsing `
            -TimeoutSec 2
        return $capabilities.StatusCode -eq 200
    } catch {
        return $false
    }
}

function Test-PortInUse {
    $probe = $null
    try {
        $probe = [Net.Sockets.TcpListener]::new([Net.IPAddress]::Loopback, $Port)
        $probe.Start()
        return $false
    } catch [Net.Sockets.SocketException] {
        return $true
    } finally {
        if ($probe) {
            $probe.Stop()
        }
    }
}

function Read-FirstLine([string]$Path) {
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        return ""
    }
    return [string](Get-Content -LiteralPath $Path -TotalCount 1 -ErrorAction SilentlyContinue)
}

function Use-TrackedServerPort {
    foreach ($stateFile in @($PidFile, $PortFile, $CommitFile, $RootFile, $InstanceFile)) {
        if (-not (Test-Path -LiteralPath $stateFile -PathType Leaf)) {
            return $false
        }
    }

    try {
        $trackedPid = [int](Read-FirstLine $PidFile)
        $trackedPort = [int](Read-FirstLine $PortFile)
        $trackedCommit = Read-FirstLine $CommitFile
        $trackedRoot = Read-FirstLine $RootFile
        $trackedInstance = Read-FirstLine $InstanceFile

        if (
            $trackedPid -le 0 -or
            $trackedPort -lt 1 -or
            $trackedPort -gt 65535 -or
            $trackedCommit -ne $GitCommit -or
            $trackedRoot -ne $ProjectRoot -or
            $trackedInstance -ne $ProjectInstanceId -or
            -not (Test-LauncherOwnedProcess $trackedPid)
        ) {
            return $false
        }

        $script:Port = $trackedPort
        Set-ServiceUrls
        return (Test-StockAiServer)
    } catch {
        return $false
    }
}

function Stop-StaleTrackedServer {
    if (-not (Test-Path -LiteralPath $PidFile -PathType Leaf)) {
        return
    }

    $trackedRoot = Read-FirstLine $RootFile
    $pidText = Read-FirstLine $PidFile
    if ($trackedRoot -ne $ProjectRoot -or $pidText -notmatch '^\d+$') {
        return
    }

    $trackedPid = [int]$pidText
    if (Test-LauncherOwnedProcess $trackedPid) {
        Write-Step "Stopping an older server from this project copy..."
        Stop-Process -Id $trackedPid -Force -ErrorAction SilentlyContinue
        Start-Sleep -Milliseconds 500
    }
}

function Clear-ServerState {
    Remove-Item `
        -LiteralPath $PidFile, $PortFile, $CommitFile, $RootFile, $InstanceFile `
        -Force `
        -ErrorAction SilentlyContinue
}

function Select-AvailablePort {
    for ($candidate = $DefaultPort; $candidate -le 8999; $candidate++) {
        $script:Port = $candidate
        Set-ServiceUrls
        if (-not (Test-PortInUse)) {
            return $true
        }
    }
    return $false
}

function Find-Uv {
    if (Test-Path -LiteralPath $UvExe -PathType Leaf) {
        return $UvExe
    }

    $command = Get-Command uv.exe -ErrorAction SilentlyContinue
    if (-not $command) {
        $command = Get-Command uv -ErrorAction SilentlyContinue
    }
    if ($command) {
        return $command.Source
    }

    $candidates = @(
        (Join-Path $env:USERPROFILE ".local\bin\uv.exe"),
        (Join-Path $env:LOCALAPPDATA "Programs\uv\uv.exe")
    )
    foreach ($candidate in $candidates) {
        if (Test-Path -LiteralPath $candidate -PathType Leaf) {
            return $candidate
        }
    }
    return $null
}

function Install-ProjectUv {
    Write-Step "Installing the portable environment manager..."
    New-Item -ItemType Directory -Force -Path $ToolsDir | Out-Null
    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
    $previousInstallDir = $env:UV_INSTALL_DIR
    $previousNoModifyPath = $env:UV_NO_MODIFY_PATH
    try {
        $env:UV_INSTALL_DIR = $ToolsDir
        $env:UV_NO_MODIFY_PATH = "1"
        $installer = Invoke-WebRequest -Uri "https://astral.sh/uv/$UvVersion/install.ps1" -UseBasicParsing -TimeoutSec 60
        $installerScript = if ($installer.Content -is [byte[]]) {
            [Text.Encoding]::UTF8.GetString($installer.Content)
        } else {
            [string]$installer.Content
        }
        Invoke-Expression $installerScript
    } finally {
        $env:UV_INSTALL_DIR = $previousInstallDir
        $env:UV_NO_MODIFY_PATH = $previousNoModifyPath
    }
    if (-not (Test-Path -LiteralPath $UvExe -PathType Leaf)) {
        throw "uv installation completed but $UvExe was not created."
    }
    return $UvExe
}

function Assert-ProjectFiles {
    foreach ($relativePath in @("pyproject.toml", "uv.lock", "src\stock_ai\main.py")) {
        $path = Join-Path $ProjectRoot $relativePath
        if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
            throw "Project file is missing: $relativePath"
        }
    }
}

function Prepare-Codex {
    Write-Step "Preparing the Codex connection..."
    if (Test-Path -LiteralPath $CodexExe -PathType Leaf) {
        $env:STOCK_AI_CODEX_BIN = $CodexExe
        return
    }
    $systemCodex = Get-Command codex.exe -ErrorAction SilentlyContinue
    if (-not $systemCodex) {
        $systemCodex = Get-Command codex -ErrorAction SilentlyContinue
    }
    if ($systemCodex) {
        $env:STOCK_AI_CODEX_BIN = $systemCodex.Source
        return
    }
    New-Item -ItemType Directory -Force -Path $CodexDir | Out-Null
    $previousInstallDir = $env:CODEX_INSTALL_DIR
    $previousNonInteractive = $env:CODEX_NON_INTERACTIVE
    try {
        $env:CODEX_INSTALL_DIR = $CodexDir
        $env:CODEX_NON_INTERACTIVE = "1"
        try {
            $installer = Invoke-WebRequest `
                -Uri "https://github.com/openai/codex/releases/latest/download/install.ps1" `
                -UseBasicParsing `
                -TimeoutSec 120
            $installerScript = if ($installer.Content -is [byte[]]) {
                [Text.Encoding]::UTF8.GetString($installer.Content)
            } else {
                [string]$installer.Content
            }
            Invoke-Expression $installerScript
        } catch {
            Write-Host "Codex could not be downloaded. The stock system will open normally; Codex can be connected later from Settings." -ForegroundColor Yellow
            return
        }
    } finally {
        $env:CODEX_INSTALL_DIR = $previousInstallDir
        $env:CODEX_NON_INTERACTIVE = $previousNonInteractive
    }
    if (Test-Path -LiteralPath $CodexExe -PathType Leaf) {
        $env:STOCK_AI_CODEX_BIN = $CodexExe
    } else {
        Write-Host "Codex installer finished without a usable executable; the stock system will still open." -ForegroundColor Yellow
    }
}

function Sync-Runtime([string]$Uv) {
    Write-Step "Preparing Python 3.12 and project dependencies..."
    $env:UV_PROJECT_ENVIRONMENT = $VenvDir
    $env:UV_CACHE_DIR = $CacheDir
    $env:UV_PYTHON_INSTALL_DIR = $PythonInstallDir

    & $Uv python install 3.12 --no-bin
    if ($LASTEXITCODE -ne 0) {
        throw "Python 3.12 setup failed with exit code $LASTEXITCODE."
    }

    & $Uv sync --frozen --no-dev --python 3.12
    if ($LASTEXITCODE -ne 0) {
        if (Test-Path -LiteralPath $VenvPython -PathType Leaf) {
            & $VenvPython -c "import stock_ai, uvicorn"
            if ($LASTEXITCODE -eq 0) {
                Write-Host "Dependency sync could not reach the network; using the verified local runtime." -ForegroundColor Yellow
                return
            }
        }
        throw "Dependency setup failed with exit code $LASTEXITCODE."
    }
}

function Save-ServerState([int]$ProcessId) {
    $ProcessId | Set-Content -Encoding ascii -Path $PidFile
    $Port | Set-Content -Encoding ascii -Path $PortFile
    $GitCommit | Set-Content -Encoding utf8 -Path $CommitFile
    $ProjectRoot | Set-Content -Encoding utf8 -Path $RootFile
    $ProjectInstanceId | Set-Content -Encoding ascii -Path $InstanceFile
}

try {
    Set-Location -LiteralPath $ProjectRoot
    Assert-ProjectFiles
    New-Item -ItemType Directory -Force -Path $RuntimeRoot, $ToolsDir, $CacheDir, $PythonInstallDir, $LogDir | Out-Null

    Write-Host "========================================" -ForegroundColor DarkCyan
    Write-Host " Stock AI System - Portable Launcher" -ForegroundColor White
    Write-Host "========================================" -ForegroundColor DarkCyan
    Write-Host "Project : $ProjectRoot"
    Write-Host "Branch  : $GitBranch"
    Write-Host "Commit  : $GitCommit"
    Write-Host "Instance: $ProjectInstanceId"

    if (Use-TrackedServerPort) {
        Write-Host "This exact project instance is already running. Opening the browser..." -ForegroundColor Green
        Start-Process $Url
        exit 0
    }

    Stop-StaleTrackedServer
    Clear-ServerState

    $Port = $DefaultPort
    Set-ServiceUrls
    if (Test-PortInUse) {
        Write-Step "Port $Port is already in use; looking for an available port..."
    }
    if (-not (Select-AvailablePort)) {
        throw "No available local port was found between 8000 and 8999."
    }

    $uv = Find-Uv
    if (-not $uv) {
        $uv = Install-ProjectUv
    }
    Sync-Runtime $uv
    Prepare-Codex

    if (-not (Test-Path -LiteralPath (Join-Path $ProjectRoot ".env")) -and
        (Test-Path -LiteralPath (Join-Path $ProjectRoot ".env.example"))) {
        Copy-Item -LiteralPath (Join-Path $ProjectRoot ".env.example") -Destination (Join-Path $ProjectRoot ".env")
    }

    $env:STOCK_AI_INSTANCE_ID = $ProjectInstanceId
    $env:STOCK_AI_BUILD_COMMIT = $GitCommit
    $env:STOCK_AI_PROJECT_ROOT = $ProjectRoot

    Remove-Item -LiteralPath $StdoutLog, $StderrLog -Force -ErrorAction SilentlyContinue
    Write-Step "Starting commit $GitCommit on port $Port..."
    $process = Start-Process -FilePath $VenvPython `
        -ArgumentList @("-m", "uvicorn", "stock_ai.main:app", "--host", "127.0.0.1", "--port", "$Port") `
        -WorkingDirectory $ProjectRoot `
        -WindowStyle Hidden `
        -RedirectStandardOutput $StdoutLog `
        -RedirectStandardError $StderrLog `
        -PassThru

    Save-ServerState $process.Id

    $ready = $false
    for ($attempt = 0; $attempt -lt 120; $attempt++) {
        Start-Sleep -Milliseconds 500
        if (Test-StockAiServer) {
            $ready = $true
            break
        }
        if ($process.HasExited) {
            break
        }
    }

    if (-not $ready) {
        if (-not $process.HasExited) {
            Stop-Process -Id $process.Id -Force -ErrorAction SilentlyContinue
        }
        Clear-ServerState
        $errorTail = if (Test-Path -LiteralPath $StderrLog) {
            (Get-Content -LiteralPath $StderrLog -Tail 60 -ErrorAction SilentlyContinue) -join [Environment]::NewLine
        } else {
            "No server error log was produced."
        }
        throw "The server did not become ready.`n$errorTail"
    }

    Write-Host "Ready: $Url (PID $($process.Id), commit $GitCommit, instance $ProjectInstanceId)" -ForegroundColor Green
    Start-Process $Url
    exit 0
} catch {
    Write-Host ""
    Write-Host "Startup failed: $($_.Exception.Message)" -ForegroundColor Red
    Write-Host "Log: $StderrLog" -ForegroundColor Yellow
    exit 1
}
