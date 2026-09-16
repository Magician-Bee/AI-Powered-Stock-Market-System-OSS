from pathlib import Path
import os
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]


def test_macos_acceptance_commands_are_executable():
    import os
    import pytest

    if os.name == "nt":
        pytest.skip("POSIX executable permissions")
    for name in ("開啟股市AI系統.command", "停止股市AI系統.command", "驗證目前執行版本.command"):
        assert (ROOT / name).stat().st_mode & 0o111, name


def read_windows_launcher() -> str:
    return (
        (ROOT / "open-stock-ai.ps1").read_text(encoding="utf-8")
        + "\n"
        + (ROOT / "open-stock-ai-core.ps1").read_text(encoding="utf-8")
    )


def test_windows_launcher_is_encoding_safe_and_uses_current_project():
    launcher = read_windows_launcher()
    shortcuts = (ROOT / "create-shortcuts.ps1").read_text(encoding="utf-8")

    assert launcher.isascii()
    assert shortcuts.isascii()
    assert "StockAI_System" not in launcher
    assert "StockAI_System" not in shortcuts
    assert '$ProjectRoot = $PSScriptRoot' in launcher
    assert '.runtime' in launcher
    assert 'venv-windows' in launcher
    assert 'UV_PROJECT_ENVIRONMENT' in launcher
    assert 'https://astral.sh/uv/$UvVersion/install.ps1' in launcher
    assert '$UvVersion = "0.11.28"' in launcher
    assert 'UV_NO_MODIFY_PATH' in launcher
    assert '[Text.Encoding]::UTF8.GetString($installer.Content)' in launcher
    assert 'python install 3.12' in launcher
    assert 'sync --frozen --no-dev --python 3.12' in launcher
    assert 'github.com/openai/codex/releases/latest/download/install.ps1' in launcher
    assert '$env:CODEX_INSTALL_DIR = $CodexDir' in launcher
    assert '$env:STOCK_AI_CODEX_BIN = $CodexExe' in launcher
    assert 'stock-ai-system' in launcher
    assert 'stock_ai.main:app' in launcher
    assert 'Save-ServerState' in launcher
    assert 'Test-StockAiServer' in launcher
    assert 'Start-Process $Url' in launcher
    assert 'Select-AvailablePort' in launcher
    assert '8999' in launcher
    assert '$PortFile' in launcher


def test_batch_launchers_delegate_to_the_persistent_launcher():
    for filename in ("open-stock-ai.bat", "Open Stock AI System.bat", "開啟股市AI系統.bat"):
        content = (ROOT / filename).read_text(encoding="utf-8")
        assert 'open-stock-ai.ps1' in content
        assert 'uvicorn stock_ai.main:app' not in content


def test_macos_launcher_bootstraps_an_isolated_runtime_and_opens_native_app():
    launcher = (ROOT / "open-stock-ai.sh").read_text(encoding="utf-8")
    command = (ROOT / "開啟股市AI系統.command").read_text(encoding="utf-8")

    assert launcher.isascii()
    assert command.isascii()
    assert 'venv-macos-${ARCH}' in launcher
    assert 'UV_PROJECT_ENVIRONMENT' in launcher
    assert 'https://astral.sh/uv/${UV_VERSION}/install.sh' in launcher
    assert 'UV_VERSION="0.11.28"' in launcher
    assert 'UV_UNMANAGED_INSTALL="$TOOLS_DIR"' in launcher
    assert 'UV_PYTHON_INSTALL_DIR=' in launcher
    assert '.symlink-test-link' in launcher
    assert 'exfat|msdos' in launcher
    assert 'Library/Caches/StockAI-System' in launcher
    assert 'python install 3.12' in launcher
    assert 'sync --frozen --no-dev --python 3.12' in launcher
    assert 'github.com/openai/codex/releases/latest/download/install.sh' in launcher
    assert 'CODEX_INSTALL_DIR="$CODEX_DIR"' in launcher
    assert 'STOCK_AI_CODEX_BIN="$CODEX_BIN"' in launcher
    assert 'stock-ai-system' in launcher
    assert 'build-macos-liquid-glass.sh' in launcher
    assert 'Stock AI Liquid Glass.app' in launcher
    assert 'NATIVE_APP_STAGING=' in launcher
    assert 'NATIVE_APP_ARCHIVE_BUNDLE=' in launcher
    assert 'mv "$NATIVE_APP_ARCHIVE_BUNDLE" "$NATIVE_APP"' in launcher
    assert 'The bundled Apple Liquid Glass interface is incomplete.' in launcher
    assert 'The Apple Liquid Glass native interface is missing required bundle files.' in launcher
    assert '/usr/bin/ditto -x -k' in launcher
    assert 'codesign --force --deep --sign -' in launcher
    assert 'open_stock_ai_ui' in launcher
    assert 'stock_ai.main:app' in launcher
    assert 'open "$APP_URL"' in launcher
    assert 'open -n "$NATIVE_APP"' in launcher
    assert 'DEFAULT_PORT=8000' in launcher
    assert 'select_available_port' in launcher
    assert '8999' in launcher
    assert 'PORT_FILE=' in launcher
    assert 'pid_file="$2"; shift 2' in launcher
    assert '"$SERVICE_SOURCE_ROOT" "$PID_FILE"' in launcher
    assert 'while [ "$ATTEMPT" -lt 600 ]' in launcher
    assert 'open-stock-ai.sh' in command
    assert (ROOT / "macos" / "StockAILiquidGlass.app.zip").is_file()


@pytest.mark.skipif(os.name == "nt", reason="macOS launcher uses Bash")
@pytest.mark.parametrize("available,override,expected", [
    (("explicit", "chatgpt", "codex", "portable", "path"), "explicit", "explicit"),
    (("chatgpt", "codex", "portable", "path"), None, "chatgpt"),
    (("codex", "portable", "path"), None, "codex"),
    (("portable", "path"), None, "portable"),
    (("path",), None, "path"),
    (("chatgpt", "portable", "path"), "missing", None),
    ((), None, None),
])
def test_macos_launcher_codex_selection_preserves_override_and_prefers_app(
    tmp_path, available, override, expected,
):
    applications = tmp_path / "Applications"
    binaries = {
        "explicit": tmp_path / "custom runtime" / "codex",
        "chatgpt": applications / "ChatGPT.app" / "Contents" / "Resources" / "codex",
        "codex": applications / "Codex.app" / "Contents" / "Resources" / "codex",
        "portable": tmp_path / "portable" / "codex",
        "path": tmp_path / "bin" / "codex",
        "missing": tmp_path / "missing" / "codex",
    }
    for name in available:
        executable = binaries[name]
        executable.parent.mkdir(parents=True, exist_ok=True)
        executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        executable.chmod(0o755)
    launcher = (ROOT / "open-stock-ai.sh").read_text(encoding="utf-8")
    # Exercise only the pure selection function; never bootstrap or start a server.
    definition = "select_existing_codex_binary() {"
    function = definition + launcher.split(definition, 1)[1].split("\n}\n", 1)[0] + "\n}\n"
    environment = os.environ.copy()
    environment.pop("STOCK_AI_CODEX_BIN", None)
    environment["CODEX_BIN"] = str(binaries["portable"])
    environment["PATH"] = str(binaries["path"].parent)
    if override:
        environment["STOCK_AI_CODEX_BIN"] = str(binaries[override])
    result = subprocess.run(
        ["/bin/bash", "-c", function + '\nselect_existing_codex_binary "$1"\nselection_status=$?\nprintf "%s" "${STOCK_AI_CODEX_BIN:-}"\nexit "$selection_status"\n', "selection-test", str(applications)],
        env=environment, text=True, capture_output=True, check=False,
    )

    assert result.returncode == (0 if expected else 1), result.stderr
    if expected:
        assert result.stdout == str(binaries[expected])


def test_macos_launcher_passes_selected_codex_to_both_server_start_paths():
    launcher = (ROOT / "open-stock-ai.sh").read_text(encoding="utf-8")

    assert launcher.count('STOCK_AI_CODEX_BIN="${STOCK_AI_CODEX_BIN:-}"') == 2
    assert 'if select_existing_codex_binary; then' in launcher
    assert 'fail "STOCK_AI_CODEX_BIN must point to an existing executable."' in launcher


@pytest.mark.skipif(os.name == "nt", reason="macOS launcher uses Bash")
@pytest.mark.parametrize("sdk_contract", ["compatible", "legacy", "coerced", "missing_dependency"])
def test_macos_offline_fallback_requires_current_codex_response_contract(tmp_path, sdk_contract):
    # Execute only the fallback verifier against isolated import fixtures. A
    # legacy SDK is importable but raises on the app-server's `ultra` value.
    (tmp_path / "stock_ai.py").write_text("", encoding="utf-8")
    (tmp_path / "uvicorn.py").write_text(
        'raise ImportError("fixture missing dependency")\n' if sdk_contract == "missing_dependency" else "",
        encoding="utf-8",
    )
    sdk = tmp_path / "openai_codex"
    generated = sdk / "generated"
    generated.mkdir(parents=True)
    (sdk / "__init__.py").write_text(
        "class AsyncCodex: pass\nclass AsyncThread: pass\nclass CodexConfig: pass\n",
        encoding="utf-8",
    )
    (generated / "__init__.py").write_text("", encoding="utf-8")
    contract = 'from enum import Enum\nclass ReasoningEffort(str, Enum):\n    high = "high"\n'
    if sdk_contract in {"compatible", "missing_dependency"}:
        contract += (
            "    @classmethod\n    def _missing_(cls, value):\n"
            "        member = str.__new__(cls, value)\n"
            "        member._name_ = member._value_ = value\n"
            "        return member\n"
        )
    elif sdk_contract == "coerced":
        contract += "    @classmethod\n    def _missing_(cls, value):\n        return cls.high\n"
    (generated / "v2_all.py").write_text(contract, encoding="utf-8")
    launcher = (ROOT / "open-stock-ai.sh").read_text(encoding="utf-8")
    definition = "local_runtime_is_compatible() {"
    function = definition + launcher.split(definition, 1)[1].split("\n}\n", 1)[0] + "\n}\n"
    environment = os.environ.copy()
    environment.update({
        "VENV_PYTHON": sys.executable,
        "PYTHONPATH": str(tmp_path),
        "PYTHONDONTWRITEBYTECODE": "1",
    })
    result = subprocess.run(
        ["/bin/bash", "-c", function + "\nlocal_runtime_is_compatible\n"],
        cwd=tmp_path, env=environment, text=True, capture_output=True, check=False,
    )

    assert result.returncode == (0 if sdk_contract == "compatible" else 1), result.stderr
    assert 'if local_runtime_is_compatible; then' in launcher
    assert 'the local runtime is missing dependencies or has an incompatible Codex SDK' in launcher


def test_headless_n8n_launcher_is_local_pinned_and_refuses_low_disk_install():
    launcher = (ROOT / "scripts" / "start-n8n-headless.sh").read_text(encoding="utf-8")
    command = (ROOT / "啟動n8n執行層.command").read_text(encoding="utf-8")

    assert launcher.isascii()
    assert command.isascii()
    assert 'N8N_VERSION="2.33.7"' in launcher
    assert 'N8N_HOST="127.0.0.1"' in launcher
    assert 'N8N_PORT="5678"' in launcher
    assert 'READINESS_URL="${HEALTH_URL}/readiness"' in launcher
    assert 'readiness_is_ready()' in launcher
    assert 'wait_for_readiness()' in launcher
    assert 'wait_for_readiness "$(listener_pid)"' in launcher
    assert 'start_with_launchd' in launcher
    assert 'wait_for_readiness ""' in launcher
    assert 'if health_is_ready; then\n  print_readiness' not in launcher
    assert 'MIN_FREE_KB=4194304' in launcher
    assert 'n8n needs at least 4 GiB free' in launcher
    assert 'npm install --prefix "$PACKAGE_ROOT" --no-audit --no-fund "n8n@$N8N_VERSION"' in launcher
    assert 'N8N_ENCRYPTION_KEY=' in launcher
    assert 'start-n8n-headless.sh' in command


def test_stock_ai_launcher_exports_only_n8n_gateway_values_needed_by_callbacks():
    launcher = (ROOT / "open-stock-ai.sh").read_text(encoding="utf-8")

    assert "N8N_AUTOMATION_CALLBACK_SECRET" in launcher
    assert 'N8N_AUTOMATION_GATEWAY_URL|N8N_AUTOMATION_GATEWAY_TOKEN|N8N_AUTOMATION_CALLBACK_SECRET' in launcher
    assert 'N8N_AUTOMATION_CALLBACK_SECRET="${N8N_AUTOMATION_CALLBACK_SECRET:-}"' in launcher
    assert 'N8N_START_SCRIPT="${PROJECT_ROOT}/scripts/start-n8n-headless.sh"' in launcher
    assert 'ensure_n8n_execution_layer()' in launcher
    assert '"$N8N_START_SCRIPT" || fail "The local n8n execution layer did not become deployment-ready."' in launcher
    assert 'load_n8n_gateway_env' in launcher
    assert 'ensure_n8n_execution_layer\n\nif tracked_server_is_ready; then' in launcher
    assert 'if [ ! -f "${PROJECT_ROOT}/.env" ] && [ -f "${PROJECT_ROOT}/.env.example" ]; then\n  cp "${PROJECT_ROOT}/.env.example" "${PROJECT_ROOT}/.env" || fail "Could not create .env."\nfi\n\nrm -f "$STDOUT_LOG"' in launcher
    assert "N8N_LOCAL_OWNER_PASSWORD" not in launcher


def test_macos_launcher_never_reuses_a_server_from_another_project_copy():
    launcher = (ROOT / "open-stock-ai.sh").read_text(encoding="utf-8")

    assert 'PROJECT_INSTANCE_ID=' in launcher
    assert 'BUNDLE_ID="io.github.magicianbee.stockai.liquidglass.instance${PROJECT_INSTANCE_ID}"' in launcher
    assert 'GIT_COMMIT=' in launcher
    assert 'COMMIT_FILE=' in launcher
    assert 'FINGERPRINT_FILE=' in launcher
    assert 'GATEWAY_GENERATION_FILE=' in launcher
    assert 'ROOT_FILE=' in launcher
    assert 'INSTANCE_FILE=' in launcher
    assert 'process_cwd' in launcher
    assert '[ "$TRACKED_ROOT" = "$PROJECT_ROOT" ]' in launcher
    assert '[ "$TRACKED_COMMIT" = "$GIT_COMMIT" ]' in launcher
    assert '[ "$TRACKED_FINGERPRINT" = "$WORKTREE_FINGERPRINT" ]' in launcher
    assert '[ "$TRACKED_GATEWAY_GENERATION" = "$N8N_GATEWAY_GENERATION" ]' in launcher
    assert '[ "$TRACKED_INSTANCE" = "$PROJECT_INSTANCE_ID" ]' in launcher
    assert 'project_server_cwd_is_owned "$TRACKED_CWD" || return 1' in launcher
    assert 'if tracked_server_is_ready; then' in launcher
    assert 'tracked_server_is_ready || server_is_ready' not in launcher
    assert 'Port ${PORT} belongs to another process' in launcher
    assert 'stock_ai_instance=${PROJECT_INSTANCE_ID}' in launcher
    assert 'stock_ai_commit=${GIT_COMMIT}' in launcher
    assert 'stock_ai_source=${WORKTREE_FINGERPRINT}' in launcher
    assert 'STOCK_AI_BUILD_COMMIT="$GIT_COMMIT"' in launcher
    assert 'AGENT_DATA_ROOT="${RUNTIME_ROOT}/agent-data"' in launcher
    assert 'MARKET_DATA_DB="${RUNTIME_ROOT}/market-data.db"' in launcher
    assert 'STOCK_AI_AGENT_DATA_ROOT="$AGENT_DATA_ROOT"' in launcher
    assert launcher.count('STOCK_AI_AGENT_BACKGROUND_PAUSED="${STOCK_AI_AGENT_BACKGROUND_PAUSED:-0}"') == 2
    assert 'STOCK_AI_MARKET_DATA_DB="$MARKET_DATA_DB"' in launcher


def test_macos_launcher_waits_for_stale_project_servers_to_exit_before_restart():
    launcher = (ROOT / "open-stock-ai.sh").read_text(encoding="utf-8")

    assert "stop_verified_project_server()" in launcher
    assert "stop_other_project_servers()" in launcher
    assert "stop_stale_tracked_server\nstop_other_project_servers" in launcher
    assert 'kill -KILL "$pid"' in launcher
    assert '[ "$attempt" -lt 50 ]' in launcher
    assert "could not be stopped safely" in launcher


def test_macos_stop_script_only_stops_the_current_project_copy():
    stop_script = (ROOT / "stop-stock-ai.sh").read_text(encoding="utf-8")

    assert 'process_cwd' in stop_script
    assert 'source "$RUNTIME_ROOT_HELPER" "$PROJECT_ROOT"' in stop_script
    assert 'MANAGED_SERVICE_SOURCE="${RUNTIME_ROOT}/service-source"' in stop_script
    assert 'project_server_cwd_is_owned "$cwd" || return 1' in stop_script
    assert '"$PROJECT_ROOT"|"$MANAGED_SERVICE_SOURCE"' in stop_script
    assert 'stock-ai-server.commit' in stop_script
    assert 'stock-ai-server.source-fingerprint' in stop_script
    assert 'stock-ai-server.root' in stop_script
    assert 'stock-ai-server.instance' in stop_script
    assert 'launchctl remove "$LAUNCH_AGENT_LABEL"' in stop_script


def test_running_instance_verifier_accepts_only_this_projects_managed_service_source():
    verifier = (ROOT / "verify-stock-ai-instance.sh").read_text(encoding="utf-8")

    assert 'EXPECTED_INSTANCE=' in verifier
    assert 'MANAGED_SERVICE_SOURCE=' in verifier
    assert 'RECORDED_INSTANCE" != "$EXPECTED_INSTANCE' in verifier
    assert '"$PROCESS_CWD" != "$PROJECT_ROOT" ] && [ "$PROCESS_CWD" != "$MANAGED_SERVICE_SOURCE"' in verifier
    assert 'rsync -rcn --delete "${PROJECT_ROOT}/src/" "${MANAGED_SERVICE_SOURCE}/src/"' in verifier


def test_portable_runtime_is_ignored_and_python_is_pinned():
    gitignore = (ROOT / ".gitignore").read_text(encoding="utf-8")
    python_version = (ROOT / ".python-version").read_text(encoding="utf-8").strip()

    assert ".runtime/" in gitignore
    assert python_version == "3.12"


def test_windows_stop_launchers_only_delegate_to_verified_process_shutdown():
    stop_script = (ROOT / "stop-stock-ai.ps1").read_text(encoding="utf-8")
    assert "uvicorn\\s+stock_ai\\.main:app" in stop_script
    assert "Stop-VerifiedStockAiProcess" in stop_script

    for filename in ("stop-stock-ai.bat", "Stop Stock AI System.bat", "停止股市AI系統.bat"):
        content = (ROOT / filename).read_text(encoding="utf-8")
        assert "stop-stock-ai.ps1" in content
        assert "Stop-Process" not in content


@pytest.mark.skipif(os.name == "nt", reason="macOS launcher uses Bash")
def test_managed_source_copies_hash_bound_runtime_artifact_without_user_output(tmp_path):
    import hashlib
    import json
    import shutil
    import yaml

    if not shutil.which("rsync"):
        pytest.skip("rsync is required by the macOS launcher")
    project, managed = tmp_path / "project", tmp_path / "managed"
    for relative in ("src", "config", "external", "docs/research", "output"):
        (project / relative).mkdir(parents=True)
    artifact = "docs/research/taiwan-market-impact-baseline.md"
    for relative in (artifact, "config/taiwan_market_impact_rules.yaml"):
        shutil.copy2(ROOT / relative, project / relative)
    (project / "output/private.json").write_text(json.dumps({"fixture": "not a runtime dependency"}))
    (managed / "output").mkdir(parents=True)
    (managed / "output/existing.txt").write_text("preserve runtime account data")
    source = (ROOT / "open-stock-ai.sh").read_text()
    signature = "synchronize_launchd_source() {"
    function = signature + source.split(signature, 1)[1].split("\n}\n", 1)[0] + "\n}\n"
    env = {**os.environ, "PROJECT_ROOT": str(project), "SERVICE_SOURCE_ROOT": str(managed)}
    result = subprocess.run(["/bin/bash", "-c", 'fail() { exit 1; }\n' + function + "\nsynchronize_launchd_source\n"],
                            env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    rules = yaml.safe_load((managed / "config/taiwan_market_impact_rules.yaml").read_text())
    assert hashlib.sha256((managed / rules["calibration_artifact"]).read_bytes()).hexdigest() == rules["calibration_artifact_sha256"]
    assert (managed / "output/existing.txt").read_text() == "preserve runtime account data"
    assert not (managed / "output/private.json").exists()
    assert "src config macos docs/research/taiwan-market-impact-baseline.md" in source
