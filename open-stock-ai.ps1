$ErrorActionPreference = "Stop"

$ProjectRoot = $PSScriptRoot
$CoreLauncher = Join-Path $ProjectRoot "open-stock-ai-core.ps1"
$PortableCodex = Join-Path $ProjectRoot ".runtime\tools\codex\codex.exe"

function Test-NativeCodexExecutable([string]$Path) {
    if ([string]::IsNullOrWhiteSpace($Path) -or
        -not (Test-Path -LiteralPath $Path -PathType Leaf) -or
        [IO.Path]::GetExtension($Path) -ne ".exe") {
        return $false
    }

    try {
        $stream = [IO.File]::Open($Path, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::ReadWrite)
        try {
            if ($stream.ReadByte() -ne 0x4D -or $stream.ReadByte() -ne 0x5A) {
                return $false
            }
        } finally {
            $stream.Dispose()
        }

        $startInfo = New-Object Diagnostics.ProcessStartInfo
        $startInfo.FileName = [IO.Path]::GetFullPath($Path)
        $startInfo.Arguments = "--version"
        $startInfo.UseShellExecute = $false
        $startInfo.CreateNoWindow = $true
        $startInfo.RedirectStandardOutput = $true
        $startInfo.RedirectStandardError = $true
        $process = [Diagnostics.Process]::Start($startInfo)
        if (-not $process) {
            return $false
        }
        try {
            if (-not $process.WaitForExit(15000)) {
                $process.Kill()
                return $false
            }
            return $process.ExitCode -eq 0
        } finally {
            $process.Dispose()
        }
    } catch {
        return $false
    }
}

function Get-NativeCodexCandidates {
    $values = New-Object Collections.Generic.List[string]

    if ($env:STOCK_AI_CODEX_BIN) {
        $values.Add($env:STOCK_AI_CODEX_BIN)
    }
    $values.Add($PortableCodex)

    $command = Get-Command codex.exe -ErrorAction SilentlyContinue
    if ($command -and $command.Source) {
        $values.Add($command.Source)
    }

    $searchRoots = New-Object Collections.Generic.List[string]
    if ($env:APPDATA) {
        $searchRoots.Add((Join-Path $env:APPDATA "npm\node_modules\@openai\codex"))
    }
    $searchRoots.Add((Join-Path $ProjectRoot ".runtime\venv-windows\Lib\site-packages\codex_cli_bin"))

    $npm = Get-Command npm.cmd -ErrorAction SilentlyContinue
    if (-not $npm) {
        $npm = Get-Command npm -ErrorAction SilentlyContinue
    }
    if ($npm) {
        try {
            $npmRoot = (& $npm.Source root -g 2>$null | Select-Object -First 1)
            if ($LASTEXITCODE -eq 0 -and $npmRoot) {
                $searchRoots.Add((Join-Path ([string]$npmRoot).Trim() "@openai\codex"))
            }
        } catch {
            # npm is optional; the Python wheel and native installer remain available.
        }
    }

    foreach ($root in $searchRoots | Select-Object -Unique) {
        if (-not (Test-Path -LiteralPath $root -PathType Container)) {
            continue
        }
        Get-ChildItem -LiteralPath $root -Filter codex.exe -File -Recurse -ErrorAction SilentlyContinue |
            ForEach-Object { $values.Add($_.FullName) }
    }

    return $values | Where-Object { $_ } | Select-Object -Unique
}

function Remove-InvalidCodexShimDirectoriesFromPath {
    $kept = New-Object Collections.Generic.List[string]
    foreach ($entry in ([string]$env:PATH -split ';')) {
        $directory = $entry.Trim().Trim('"')
        if ([string]::IsNullOrWhiteSpace($directory) -or -not (Test-Path -LiteralPath $directory -PathType Container)) {
            if (-not [string]::IsNullOrWhiteSpace($entry)) {
                $kept.Add($entry)
            }
            continue
        }

        $hasShim = @("codex", "codex.cmd", "codex.bat", "codex.ps1") |
            ForEach-Object { Test-Path -LiteralPath (Join-Path $directory $_) -PathType Leaf } |
            Where-Object { $_ } |
            Select-Object -First 1
        $native = Join-Path $directory "codex.exe"
        if ($hasShim -and -not (Test-NativeCodexExecutable $native)) {
            continue
        }
        $kept.Add($entry)
    }
    $env:PATH = $kept -join ';'
}

if (-not (Test-Path -LiteralPath $CoreLauncher -PathType Leaf)) {
    throw "Core launcher is missing: $CoreLauncher"
}

if ((Test-Path -LiteralPath $PortableCodex -PathType Leaf) -and
    -not (Test-NativeCodexExecutable $PortableCodex)) {
    Write-Host "[Stock AI] Removing an invalid portable Codex executable..." -ForegroundColor Yellow
    Remove-Item -LiteralPath $PortableCodex -Force -ErrorAction SilentlyContinue
}

$nativeCodex = $null
foreach ($candidate in Get-NativeCodexCandidates) {
    if (Test-NativeCodexExecutable $candidate) {
        $nativeCodex = [IO.Path]::GetFullPath($candidate)
        break
    }
}

if ($nativeCodex) {
    $nativeDirectory = Split-Path -Parent $nativeCodex
    $env:PATH = "$nativeDirectory;$env:PATH"
    $env:STOCK_AI_CODEX_BIN = $nativeCodex
    Write-Host "[Stock AI] Verified native Codex: $nativeCodex" -ForegroundColor DarkGray
} else {
    Remove-Item Env:STOCK_AI_CODEX_BIN -ErrorAction SilentlyContinue
    Remove-InvalidCodexShimDirectoriesFromPath
    Write-Host "[Stock AI] No verified native Codex was found; the portable installer or bundled runtime will be used." -ForegroundColor Yellow
}

& $CoreLauncher
exit $LASTEXITCODE
