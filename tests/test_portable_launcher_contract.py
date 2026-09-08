from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def read_root_file(name: str) -> str:
    return (ROOT / name).read_text(encoding="utf-8-sig")


def read_windows_launcher() -> str:
    return read_root_file("open-stock-ai.ps1") + "\n" + read_root_file("open-stock-ai-core.ps1")


def test_windows_launcher_authenticates_protected_readiness_probe():
    launcher = read_windows_launcher()

    assert 'name=["\'\']stock-ai-runtime-session' in launcher
    assert '"X-Stock-AI-Session" = $runtimeSessionToken' in launcher
    assert "$capabilities.StatusCode -eq 200" in launcher
    assert "Invoke-WebRequest -Uri $CapabilitiesUrl -UseBasicParsing" not in launcher


def test_windows_launcher_binds_health_to_exact_project_instance():
    launcher = read_windows_launcher()

    for contract in (
        "$env:STOCK_AI_INSTANCE_ID = $ProjectInstanceId",
        "$env:STOCK_AI_BUILD_COMMIT = $GitCommit",
        "$env:STOCK_AI_PROJECT_ROOT = $ProjectRoot",
        "[string]$health.build_commit -ne $GitCommit",
        "[string]$health.instance_id -ne $ProjectInstanceId",
        "$CommitFile",
        "$RootFile",
        "$InstanceFile",
    ):
        assert contract in launcher


def test_windows_launcher_cleans_failed_and_stale_processes():
    launcher = read_windows_launcher()
    stop_launcher = read_root_file("stop-stock-ai.ps1")

    assert "function Stop-StaleTrackedServer" in launcher
    assert "Stop-Process -Id $process.Id -Force" in launcher
    assert "Clear-ServerState" in launcher
    assert "$trackedRoot -eq $ProjectRoot" in stop_launcher
    assert "stock_ai\\.main:app" in stop_launcher
    assert "$StateFiles" in stop_launcher


def test_windows_wrappers_enable_utf8_for_unicode_paths():
    start_wrapper = read_root_file("開啟股市AI系統.bat").lower()
    stop_wrapper = read_root_file("停止股市AI系統.bat").lower()
    launcher = read_windows_launcher()

    assert "chcp 65001" in start_wrapper
    assert "chcp 65001" in stop_wrapper
    assert 'set "pythonutf8=1"' in start_wrapper
    assert "$env:PYTHONUTF8 = \"1\"" in launcher
    assert "$env:PYTHONIOENCODING = \"utf-8\"" in launcher


def test_windows_runtime_is_local_and_not_reused_from_macos():
    launcher = read_windows_launcher()

    assert 'Join-Path $RuntimeRoot "venv-windows"' in launcher
    assert "$env:UV_PYTHON_INSTALL_DIR = $PythonInstallDir" in launcher
    assert "python install 3.12 --no-bin" in launcher


def test_macos_launcher_moves_runtime_and_service_source_out_of_desktop_privacy_domain():
    launcher = read_root_file("open-stock-ai.sh")
    helper = (ROOT / "scripts" / "runtime-root.sh").read_text(encoding="utf-8")

    assert 'source "$RUNTIME_ROOT_HELPER" "$PROJECT_ROOT"' in launcher
    assert 'RUNTIME_ROOT="$STOCK_AI_RUNTIME_ROOT"' in launcher
    assert 'rsync -a --delete "$PROJECT_ROOT/$source_dir/" "$SERVICE_SOURCE_ROOT/$source_dir/"' in launcher
    assert 'PYTHONPATH="$SERVER_PYTHONPATH"' in launcher
    assert '"$SERVER_PYTHON" -S -m uvicorn stock_ai.main:app' in launcher
    assert "mixing binary extensions built" in launcher
    assert "/bin/sh -c 'cd \"$1\" || exit 1; shift; exec \"$@\"' -- \"$SERVICE_SOURCE_ROOT\"" in launcher
    assert '"$PROJECT_ROOT"|"$SERVICE_SOURCE_ROOT"' in launcher
    assert '${HOME}/Library/Application Support/StockAI-System/' in helper
    assert 'mv "$STOCK_AI_LEGACY_RUNTIME_ROOT" "$STOCK_AI_RUNTIME_ROOT"' in helper
    assert 'ln -s "$STOCK_AI_RUNTIME_ROOT" "$STOCK_AI_LEGACY_RUNTIME_ROOT"' in helper


def test_windows_codex_launcher_rejects_shell_shims_and_invalid_pe_files():
    wrapper = read_root_file("open-stock-ai.ps1")

    assert "function Test-NativeCodexExecutable" in wrapper
    assert "$stream.ReadByte() -ne 0x4D" in wrapper
    assert "$stream.ReadByte() -ne 0x5A" in wrapper
    assert '$startInfo.Arguments = "--version"' in wrapper
    assert "Get-Command codex.exe" in wrapper
    assert '"codex.cmd", "codex.bat", "codex.ps1"' in wrapper
    assert "$env:STOCK_AI_CODEX_BIN = $nativeCodex" in wrapper
    assert "Get-ChildItem -LiteralPath $root -Filter codex.exe" in wrapper
    assert "& $CoreLauncher" in wrapper
