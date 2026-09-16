#!/bin/bash

set -u

PROJECT_ROOT="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
DEFAULT_PORT=8000
PORT="$DEFAULT_PORT"
UV_VERSION="0.11.28"
RUNTIME_ROOT_HELPER="${PROJECT_ROOT}/scripts/runtime-root.sh"
if [ -x "$RUNTIME_ROOT_HELPER" ]; then
  source "$RUNTIME_ROOT_HELPER" "$PROJECT_ROOT"
else
  printf 'Startup failed: runtime helper is missing.\n' >&2
  exit 1
fi
PROJECT_RUNTIME_ROOT="${PROJECT_ROOT}/.runtime"
N8N_RUNTIME_HELPER="${PROJECT_ROOT}/scripts/n8n-runtime-root.sh"
if [ -x "$N8N_RUNTIME_HELPER" ]; then
  source "$N8N_RUNTIME_HELPER" "$PROJECT_ROOT"
else
  printf 'Startup failed: n8n runtime helper is missing.\n' >&2
  exit 1
fi
RUNTIME_ROOT="$STOCK_AI_RUNTIME_ROOT"
DEVICE_ID="$(df -P "$PROJECT_ROOT" 2>/dev/null | awk 'END {print $1}')"
FILESYSTEM_TYPE="$(diskutil info "$DEVICE_ID" 2>/dev/null | awk -F: '/Type \(Bundle\)/ {gsub(/[[:space:]]/, "", $2); print tolower($2); exit}')"
case "$FILESYSTEM_TYPE" in
  exfat|msdos)
    PROJECT_ID="$(printf '%s' "$PROJECT_ROOT" | cksum | awk '{print $1}')"
    RUNTIME_ROOT="${HOME}/Library/Caches/StockAI-System/${PROJECT_ID}"
    ;;
esac
if [ "$RUNTIME_ROOT" = "$PROJECT_RUNTIME_ROOT" ]; then
  SYMLINK_TEST_TARGET="${PROJECT_RUNTIME_ROOT}/.symlink-test-target"
  SYMLINK_TEST_LINK="${PROJECT_RUNTIME_ROOT}/.symlink-test-link"
  mkdir -p "$PROJECT_RUNTIME_ROOT" >/dev/null 2>&1 || true
  rm -f "$SYMLINK_TEST_TARGET" "$SYMLINK_TEST_LINK"
  if ! touch "$SYMLINK_TEST_TARGET" >/dev/null 2>&1 ||
    ! ln -s "$SYMLINK_TEST_TARGET" "$SYMLINK_TEST_LINK" >/dev/null 2>&1 ||
    [ ! -L "$SYMLINK_TEST_LINK" ]; then
    PROJECT_ID="$(printf '%s' "$PROJECT_ROOT" | cksum | awk '{print $1}')"
    RUNTIME_ROOT="${HOME}/Library/Caches/StockAI-System/${PROJECT_ID}"
  fi
  rm -f "$SYMLINK_TEST_TARGET" "$SYMLINK_TEST_LINK"
fi

PROJECT_INSTANCE_ID="$STOCK_AI_RUNTIME_INSTANCE_ID"
# Keep the macOS App identity stable for this project copy across source
# rebuilds, while isolating it from other checkouts.  A global bundle ID makes
# macOS merge unrelated native windows and lets an old WebKit session call a
# newly launched project's server with a stale runtime token.
BUNDLE_ID="io.github.magicianbee.stockai.liquidglass.instance${PROJECT_INSTANCE_ID}"
# Keep this launcher ASCII-only so Finder can run it reliably under legacy
# locale settings. The runtime display name remains UTF-8 Chinese.
NATIVE_APP_DISPLAY_NAME="$(printf '\350\202\241\345\270\202\101\111\347\263\273\347\265\261\040\101\147\145\156\164\346\270\254\350\251\246\347\211\210')"
GIT_COMMIT="$(git -C "$PROJECT_ROOT" rev-parse --short=12 HEAD 2>/dev/null || printf 'working-copy')"
GIT_BRANCH="$(git -C "$PROJECT_ROOT" branch --show-current 2>/dev/null || printf 'unknown')"
[ -n "$GIT_BRANCH" ] || GIT_BRANCH="detached"
if command -v shasum >/dev/null 2>&1 &&
  git -C "$PROJECT_ROOT" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  WORKTREE_FINGERPRINT="$(
    git -C "$PROJECT_ROOT" ls-files -co --exclude-standard -- \
      src config macos docs/research/taiwan-market-impact-baseline.md pyproject.toml uv.lock open-stock-ai.sh build-macos-liquid-glass.sh |
      while IFS= read -r relative_path; do
        [ -f "${PROJECT_ROOT}/${relative_path}" ] &&
          shasum -a 256 "${PROJECT_ROOT}/${relative_path}"
      done |
      shasum -a 256 |
      awk '{print substr($1,1,16)}'
  )"
else
  WORKTREE_FINGERPRINT="$GIT_COMMIT"
fi

TOOLS_DIR="${RUNTIME_ROOT}/tools/macos"
CODEX_DIR="${RUNTIME_ROOT}/tools/codex"
CODEX_BIN="${CODEX_DIR}/codex"
NATIVE_APP="${RUNTIME_ROOT}/apps/Stock AI Liquid Glass.app"
NATIVE_APP_EXECUTABLE="${NATIVE_APP}/Contents/MacOS/StockAILiquidGlass"
NATIVE_APP_PLIST="${NATIVE_APP}/Contents/Info.plist"
NATIVE_APP_ARCHIVE="${PROJECT_ROOT}/macos/StockAILiquidGlass.app.zip"
NATIVE_APP_STAGING="${RUNTIME_ROOT}/apps/.stock-ai-native-install"
NATIVE_APP_ARCHIVE_BUNDLE="${NATIVE_APP_STAGING}/StockAILiquidGlass.app"
ARCH="$(uname -m)"
VENV_DIR="${RUNTIME_ROOT}/venv-macos-${ARCH}"
VENV_SITE_PACKAGES="${VENV_DIR}/lib/python3.12/site-packages"
CACHE_DIR="${RUNTIME_ROOT}/cache"
AGENT_DATA_ROOT="${RUNTIME_ROOT}/agent-data"
MARKET_DATA_DB="${RUNTIME_ROOT}/market-data.db"
N8N_GATEWAY_ENV_FILE="${N8N_RUNTIME_ROOT}/data/stock-ai-gateway.env"
N8N_START_SCRIPT="${PROJECT_ROOT}/scripts/start-n8n-headless.sh"
VENV_PYTHON="${VENV_DIR}/bin/python"
SERVER_PYTHON=""
SERVER_PYTHONPATH=""
SERVICE_SOURCE_ROOT="${RUNTIME_ROOT}/service-source"
LOG_DIR="${PROJECT_ROOT}/logs"
PID_FILE="${LOG_DIR}/stock-ai-server.pid"
PORT_FILE="${LOG_DIR}/stock-ai-server.port"
COMMIT_FILE="${LOG_DIR}/stock-ai-server.commit"
FINGERPRINT_FILE="${LOG_DIR}/stock-ai-server.source-fingerprint"
GATEWAY_GENERATION_FILE="${LOG_DIR}/stock-ai-server.gateway-generation"
ROOT_FILE="${LOG_DIR}/stock-ai-server.root"
INSTANCE_FILE="${LOG_DIR}/stock-ai-server.instance"
STDOUT_LOG="${LOG_DIR}/stock-ai-server.out.log"
STDERR_LOG="${LOG_DIR}/stock-ai-server.err.log"
# `nohup ... &` is sufficient when this script is launched from an interactive
# Terminal, but a Finder/automation-launched shell can be torn down with its
# whole process group.  Let launchd own this one, project-specific server so
# clicking the Finder launcher keeps the UI's local API alive after the
# launcher itself exits.
LAUNCH_AGENT_LABEL="io.github.magicianbee.stockai.${PROJECT_INSTANCE_ID}"

load_n8n_gateway_env() {
  # Load only the n8n gateway API key and independent callback secret produced
  # by the local bootstrap. The owner credentials remain private runtime data
  # and are never exported to the Stock AI server process.
  [ -r "$N8N_GATEWAY_ENV_FILE" ] || return 0
  while IFS='=' read -r N8N_ENV_NAME N8N_ENV_VALUE; do
    case "$N8N_ENV_NAME" in
      N8N_AUTOMATION_GATEWAY_URL|N8N_AUTOMATION_GATEWAY_TOKEN|N8N_AUTOMATION_CALLBACK_SECRET)
        export "$N8N_ENV_NAME=$N8N_ENV_VALUE"
        ;;
    esac
  done < "$N8N_GATEWAY_ENV_FILE"
}

n8n_gateway_generation() {
  [ -r "$N8N_GATEWAY_ENV_FILE" ] || {
    printf 'none'
    return 0
  }
  awk -F= '$1 == "N8N_AUTOMATION_GATEWAY_GENERATION" { print substr($0, index($0, "=") + 1); exit }' "$N8N_GATEWAY_ENV_FILE"
}

N8N_GATEWAY_GENERATION="none"

set_urls() {
  URL="http://127.0.0.1:${PORT}/"
  HEALTH_URL="${URL}health"
  CAPABILITIES_URL="${URL}api/codex/capabilities"
}

set_urls

step() {
  printf '\033[36m[Stock AI] %s\033[0m\n' "$1"
}

fail() {
  printf '\n\033[31mStartup failed: %s\033[0m\n' "$1" >&2
  printf 'Log: %s\n' "$STDERR_LOG" >&2
  printf 'Press Return to close...'
  read -r _unused
  exit 1
}

select_existing_codex_binary() {
  local applications_root="${1:-/Applications}" candidate
  if [ -n "${STOCK_AI_CODEX_BIN:-}" ]; then
    [ -f "$STOCK_AI_CODEX_BIN" ] && [ -x "$STOCK_AI_CODEX_BIN" ]
    return $?
  fi
  # Prefer the desktop runtime over an older portable or PATH installation.
  for candidate in \
    "${applications_root}/ChatGPT.app/Contents/Resources/codex" \
    "${applications_root}/Codex.app/Contents/Resources/codex" \
    "$CODEX_BIN" \
    "$(command -v codex 2>/dev/null || true)"; do
    if [ -f "$candidate" ] && [ -x "$candidate" ]; then
      export STOCK_AI_CODEX_BIN="$candidate"
      return 0
    fi
  done
  return 1
}

local_runtime_is_compatible() {
  [ -x "$VENV_PYTHON" ] || return 1
  # Importability alone lets an old SDK reject current app-server responses.
  # Check the response contract without starting Codex or requiring a network.
  "$VENV_PYTHON" -c '
import stock_ai, uvicorn
from openai_codex import AsyncCodex, AsyncThread, CodexConfig
from openai_codex.generated.v2_all import ReasoningEffort
if ReasoningEffort("ultra").value != "ultra":
    raise RuntimeError("Codex SDK does not preserve current reasoning efforts")
' >/dev/null 2>&1
}

ensure_n8n_execution_layer() {
  [ -x "$N8N_START_SCRIPT" ] || fail "The local n8n execution launcher is missing."
  step "Preparing the local n8n execution layer..."
  "$N8N_START_SCRIPT" || fail "The local n8n execution layer did not become deployment-ready."
  load_n8n_gateway_env
  N8N_GATEWAY_GENERATION="$(n8n_gateway_generation)"
  [ -n "$N8N_GATEWAY_GENERATION" ] || N8N_GATEWAY_GENERATION="none"
}

listener_pid_for_port() {
  lsof -nP -tiTCP:"$1" -sTCP:LISTEN 2>/dev/null | head -n 1
}

process_cwd() {
  local pid="$1"
  lsof -a -p "$pid" -d cwd -Fn 2>/dev/null | sed -n 's/^n//p' | head -n 1
}

project_server_cwd_is_owned() {
  case "$1" in
    "$PROJECT_ROOT"|"$SERVICE_SOURCE_ROOT") return 0 ;;
    *) return 1 ;;
  esac
}

server_is_ready() {
  HEALTH_RESPONSE="$(curl -fsS --max-time 2 "$HEALTH_URL" 2>/dev/null || true)"
  INDEX_RESPONSE="$(curl -fsS --max-time 2 "$URL" 2>/dev/null || true)"
  RUNTIME_SESSION_TOKEN="$(
    printf '%s' "$INDEX_RESPONSE" |
      sed -n 's/.*name="stock-ai-runtime-session" content="\([^"]*\)".*/\1/p' |
      head -n 1
  )"
  printf '%s' "$HEALTH_RESPONSE" | grep -F '"system_id":"stock-ai-system"' >/dev/null 2>&1 &&
    printf '%s' "$HEALTH_RESPONSE" | grep -F "\"build_commit\":\"${GIT_COMMIT}\"" >/dev/null 2>&1 &&
    printf '%s' "$HEALTH_RESPONSE" | grep -F "\"instance_id\":\"${PROJECT_INSTANCE_ID}\"" >/dev/null 2>&1 &&
    [ -n "$RUNTIME_SESSION_TOKEN" ] &&
    curl -fsS --max-time 2 \
      -H "X-Stock-AI-Session: ${RUNTIME_SESSION_TOKEN}" \
      "$CAPABILITIES_URL" >/dev/null 2>&1
}

tracked_server_is_ready() {
  [ -f "$PID_FILE" ] && [ -f "$PORT_FILE" ] && [ -f "$COMMIT_FILE" ] &&
    [ -f "$FINGERPRINT_FILE" ] && [ -f "$GATEWAY_GENERATION_FILE" ] && [ -f "$ROOT_FILE" ] &&
    [ -f "$INSTANCE_FILE" ] || return 1

  TRACKED_PID="$(tr -cd '0-9' < "$PID_FILE")"
  TRACKED_PORT="$(tr -cd '0-9' < "$PORT_FILE")"
  TRACKED_COMMIT="$(cat "$COMMIT_FILE" 2>/dev/null || true)"
  TRACKED_FINGERPRINT="$(cat "$FINGERPRINT_FILE" 2>/dev/null || true)"
  TRACKED_GATEWAY_GENERATION="$(cat "$GATEWAY_GENERATION_FILE" 2>/dev/null || true)"
  TRACKED_ROOT="$(cat "$ROOT_FILE" 2>/dev/null || true)"
  TRACKED_INSTANCE="$(cat "$INSTANCE_FILE" 2>/dev/null || true)"
  [ -n "$TRACKED_PID" ] && [ -n "$TRACKED_PORT" ] || return 1
  [ "$TRACKED_COMMIT" = "$GIT_COMMIT" ] || return 1
  [ "$TRACKED_FINGERPRINT" = "$WORKTREE_FINGERPRINT" ] || return 1
  [ "$TRACKED_GATEWAY_GENERATION" = "$N8N_GATEWAY_GENERATION" ] || return 1
  [ "$TRACKED_ROOT" = "$PROJECT_ROOT" ] || return 1
  [ "$TRACKED_INSTANCE" = "$PROJECT_INSTANCE_ID" ] || return 1
  kill -0 "$TRACKED_PID" >/dev/null 2>&1 || return 1

  TRACKED_COMMAND="$(ps -p "$TRACKED_PID" -o command= 2>/dev/null || true)"
  case "$TRACKED_COMMAND" in
    *uvicorn*stock_ai.main:app*) ;;
    *) return 1 ;;
  esac

  TRACKED_CWD="$(process_cwd "$TRACKED_PID")"
  project_server_cwd_is_owned "$TRACKED_CWD" || return 1
  [ "$(listener_pid_for_port "$TRACKED_PORT")" = "$TRACKED_PID" ] || return 1

  PORT="$TRACKED_PORT"
  set_urls
  server_is_ready
}

stop_verified_project_server() {
  local pid="$1" command cwd attempt
  [ -n "$pid" ] || return 1
  kill -0 "$pid" >/dev/null 2>&1 || return 0
  command="$(ps -p "$pid" -o command= 2>/dev/null || true)"
  cwd="$(process_cwd "$pid")"
  case "$command" in
    *uvicorn*stock_ai.main:app*)
      if project_server_cwd_is_owned "$cwd"; then
        kill "$pid" >/dev/null 2>&1 || true
        attempt=0
        while kill -0 "$pid" >/dev/null 2>&1 && [ "$attempt" -lt 50 ]; do
          sleep 0.1
          attempt=$((attempt + 1))
        done
        if kill -0 "$pid" >/dev/null 2>&1; then
          kill -KILL "$pid" >/dev/null 2>&1 || true
          attempt=0
          while kill -0 "$pid" >/dev/null 2>&1 && [ "$attempt" -lt 50 ]; do
            sleep 0.1
            attempt=$((attempt + 1))
          done
        fi
        ! kill -0 "$pid" >/dev/null 2>&1
        return
      fi
      ;;
  esac
  return 1
}

stop_stale_tracked_server() {
  [ -f "$PID_FILE" ] || return 0
  local pid
  pid="$(tr -cd '0-9' < "$PID_FILE")"
  [ -n "$pid" ] || return 0
  kill -0 "$pid" >/dev/null 2>&1 || return 0
  step "Stopping an older server from this project copy..."
  stop_verified_project_server "$pid" ||
    fail "The older server for this project could not be stopped safely."
}

stop_other_project_servers() {
  local pid
  for pid in $(pgrep -f 'uvicorn.*stock_ai\.main:app' 2>/dev/null || true); do
    kill -0 "$pid" >/dev/null 2>&1 || continue
    project_server_cwd_is_owned "$(process_cwd "$pid")" || continue
    step "Stopping a leftover server process from this project copy..."
    stop_verified_project_server "$pid" ||
      fail "A leftover server for this project could not be stopped safely."
  done
}

port_is_in_use() {
  lsof -nP -iTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1
}

select_available_port() {
  PORT="$DEFAULT_PORT"
  while [ "$PORT" -le 8999 ]; do
    if ! port_is_in_use; then
      set_urls
      return 0
    fi
    PORT=$((PORT + 1))
  done
  return 1
}

open_stock_ai_ui() {
  MACOS_MAJOR="$(sw_vers -productVersion 2>/dev/null | awk -F. '{print $1}')"
  APP_URL="${URL}?stock_ai_instance=${PROJECT_INSTANCE_ID}&stock_ai_commit=${GIT_COMMIT}&stock_ai_source=${WORKTREE_FINGERPRINT}&stock_ai_launch=$(date +%s)"
  if [ "${MACOS_MAJOR:-0}" -ge 26 ]; then
    if [ -f "$NATIVE_APP_ARCHIVE" ] &&
      { [ ! -x "$NATIVE_APP_EXECUTABLE" ] ||
        { [ "$NATIVE_APP_ARCHIVE" -nt "$NATIVE_APP_EXECUTABLE" ] &&
          [ "${PROJECT_ROOT}/macos/StockAILiquidGlass/main.swift" -ot "$NATIVE_APP_ARCHIVE" ] &&
          [ "${PROJECT_ROOT}/build-macos-liquid-glass.sh" -ot "$NATIVE_APP_ARCHIVE" ]; }; }; then
      step "Installing the bundled Apple Liquid Glass interface..."
      mkdir -p "$(dirname "$NATIVE_APP")" || fail "Could not create the native application folder."
      rm -rf "$NATIVE_APP_STAGING"
      mkdir -p "$NATIVE_APP_STAGING" ||
        fail "Could not create the native application staging folder."
      /usr/bin/ditto -x -k "$NATIVE_APP_ARCHIVE" "$NATIVE_APP_STAGING" ||
        fail "Could not install the bundled Apple Liquid Glass interface."
      [ -x "${NATIVE_APP_ARCHIVE_BUNDLE}/Contents/MacOS/StockAILiquidGlass" ] &&
        [ -f "${NATIVE_APP_ARCHIVE_BUNDLE}/Contents/Info.plist" ] ||
        fail "The bundled Apple Liquid Glass interface is incomplete."
      rm -rf "$NATIVE_APP"
      mv "$NATIVE_APP_ARCHIVE_BUNDLE" "$NATIVE_APP" ||
        fail "Could not activate the bundled Apple Liquid Glass interface."
      rm -rf "$NATIVE_APP_STAGING"
    elif [ ! -x "$NATIVE_APP_EXECUTABLE" ] ||
      [ "${PROJECT_ROOT}/macos/StockAILiquidGlass/main.swift" -nt "$NATIVE_APP_EXECUTABLE" ] ||
      [ "${PROJECT_ROOT}/build-macos-liquid-glass.sh" -nt "$NATIVE_APP_EXECUTABLE" ]; then
      step "Building the Apple Liquid Glass native interface..."
      /bin/bash "${PROJECT_ROOT}/build-macos-liquid-glass.sh" "$NATIVE_APP" "$APP_URL" >/dev/null ||
        fail "Apple Liquid Glass native interface build failed."
    fi
    [ -x "$NATIVE_APP_EXECUTABLE" ] && [ -f "$NATIVE_APP_PLIST" ] ||
      fail "The Apple Liquid Glass native interface is missing required bundle files."
    /usr/bin/xattr -dr com.apple.quarantine "$NATIVE_APP" >/dev/null 2>&1 || true
    plutil -replace CFBundleIdentifier -string "$BUNDLE_ID" "$NATIVE_APP_PLIST" ||
      fail "Could not isolate the Apple Liquid Glass project container."
    # Give this project copy a stable, human-visible identity as well as an
    # isolated bundle identifier.  This prevents a second desktop checkout
    # from being mistaken for the active local acceptance app.
    plutil -replace CFBundleDisplayName -string "$NATIVE_APP_DISPLAY_NAME" "$NATIVE_APP_PLIST" ||
      fail "Could not label the Apple Liquid Glass test interface."
    plutil -replace CFBundleName -string "$NATIVE_APP_DISPLAY_NAME" "$NATIVE_APP_PLIST" ||
      fail "Could not label the Apple Liquid Glass test interface."
    plutil -replace StockAIServerURL -string "$APP_URL" "$NATIVE_APP_PLIST" ||
      fail "Could not update the native interface server address."
    codesign --force --deep --sign - "$NATIVE_APP" >/dev/null ||
      fail "Could not validate the native interface."
    pkill -f "$NATIVE_APP_EXECUTABLE" >/dev/null 2>&1 || true
    sleep 0.4
    open -n "$NATIVE_APP" || fail "Could not open the Apple Liquid Glass native interface."
  else
    printf '\033[33mApple Liquid Glass requires macOS 26 or newer; opening the web interface.\033[0m\n'
    open "$APP_URL"
  fi
}

synchronize_launchd_source() {
  local source_dir source_file
  command -v rsync >/dev/null 2>&1 || fail "rsync is required to prepare the managed desktop service source."
  mkdir -p "$SERVICE_SOURCE_ROOT" || fail "Cannot create the managed service source folder."
  # Only mirror program files into the managed runtime. Runtime databases,
  # logs, credentials and user output remain outside this tree. --delete is
  # constrained to this launcher-owned destination so old code can never
  # survive a newer desktop build.
  for source_dir in src config external; do
    [ -d "$PROJECT_ROOT/$source_dir" ] || continue
    mkdir -p "$SERVICE_SOURCE_ROOT/$source_dir" || fail "Cannot prepare managed $source_dir source."
    rsync -a --delete "$PROJECT_ROOT/$source_dir/" "$SERVICE_SOURCE_ROOT/$source_dir/" ||
      fail "Cannot synchronize managed $source_dir source."
  done
  # This versioned research artifact is a runtime dependency of the impact
  # rules in config. Preserve its path and bytes for the strict SHA check.
  source_file="docs/research/taiwan-market-impact-baseline.md"
  mkdir -p "$SERVICE_SOURCE_ROOT/docs/research" || fail "Cannot prepare managed research artifacts."
  install -m 644 "$PROJECT_ROOT/$source_file" "$SERVICE_SOURCE_ROOT/$source_file" ||
    fail "Cannot synchronize the market-impact research artifact."
  if [ -f "$PROJECT_ROOT/.env" ]; then
    install -m 600 "$PROJECT_ROOT/.env" "$SERVICE_SOURCE_ROOT/.env" ||
      fail "Cannot synchronize the managed local environment file."
  fi
}

for relative_path in pyproject.toml uv.lock src/stock_ai/main.py; do
  [ -f "${PROJECT_ROOT}/${relative_path}" ] || fail "Project file is missing: ${relative_path}"
done

mkdir -p "$TOOLS_DIR" "$CACHE_DIR" "$AGENT_DATA_ROOT" "$LOG_DIR" || fail "Cannot create the local runtime folders."
cd "$PROJECT_ROOT" || fail "Cannot enter the project folder."

printf '\033[36m========================================\033[0m\n'
printf ' Stock AI System - Portable Launcher\n'
printf '\033[36m========================================\033[0m\n'
printf 'Project : %s\n' "$PROJECT_ROOT"
printf 'Branch  : %s\n' "$GIT_BRANCH"
printf 'Commit  : %s\n' "$GIT_COMMIT"
printf 'Source  : %s\n' "$WORKTREE_FINGERPRINT"
printf 'Instance: %s\n' "$PROJECT_INSTANCE_ID"
if [ "$RUNTIME_ROOT" != "$PROJECT_RUNTIME_ROOT" ]; then
  step "Using the managed macOS runtime outside the Desktop privacy domain."
fi

# Keep the automation execution layer alive even when this invocation reuses
# an already healthy Stock AI server. Otherwise a later n8n crash leaves the
# native UI looking healthy while every Automation is paused until the user
# discovers and runs a second command by hand.
ensure_n8n_execution_layer

if tracked_server_is_ready; then
  printf '\033[32mThis exact project instance is already running on port %s. Opening it...\033[0m\n' "$PORT"
  open_stock_ai_ui
  exit 0
fi

# Remove a completed/stale launchd submission before creating the exact
# instance below.  Do this only after the fast-path above, so a healthy server
# is never interrupted merely to reopen the native window.
launchctl remove "$LAUNCH_AGENT_LABEL" >/dev/null 2>&1 || true
stop_stale_tracked_server
stop_other_project_servers
rm -f "$PID_FILE" "$PORT_FILE" "$COMMIT_FILE" "$FINGERPRINT_FILE" "$GATEWAY_GENERATION_FILE" "$ROOT_FILE" "$INSTANCE_FILE"

PORT="$DEFAULT_PORT"
set_urls
if port_is_in_use; then
  OCCUPIED_PID="$(listener_pid_for_port "$PORT")"
  OCCUPIED_CWD="$(process_cwd "$OCCUPIED_PID")"
  step "Port ${PORT} belongs to another process${OCCUPIED_CWD:+ in ${OCCUPIED_CWD}}; selecting a separate port."
fi
select_available_port || fail "No available local port was found between 8000 and 8999."

if [ -x "${TOOLS_DIR}/uv" ]; then
  UV="${TOOLS_DIR}/uv"
elif command -v uv >/dev/null 2>&1; then
  UV="$(command -v uv)"
else
  step "Installing the portable environment manager..."
  command -v curl >/dev/null 2>&1 || fail "curl is required for the first-time setup."
  curl --proto '=https' --tlsv1.2 -LsSf "https://astral.sh/uv/${UV_VERSION}/install.sh" |
    env UV_UNMANAGED_INSTALL="$TOOLS_DIR" sh || fail "Could not install uv. Check the internet connection."
  UV="${TOOLS_DIR}/uv"
fi
[ -x "$UV" ] || fail "uv is not executable: $UV"

export UV_PROJECT_ENVIRONMENT="$VENV_DIR"
export UV_CACHE_DIR="$CACHE_DIR"
export UV_PYTHON_INSTALL_DIR="${RUNTIME_ROOT}/python"

step "Preparing Python 3.12 and project dependencies..."
"$UV" python install 3.12 --no-bin || fail "Python 3.12 setup failed."
if ! "$UV" sync --frozen --no-dev --python 3.12; then
  if local_runtime_is_compatible; then
    printf '\033[33mDependency sync could not reach the network; using the verified local runtime.\033[0m\n'
  else
    fail "Dependency setup failed; the local runtime is missing dependencies or has an incompatible Codex SDK."
  fi
fi

# TradingAgents is an explicit research-only opt-in.  Keep its sizeable
# LangGraph dependency tree out of the normal desktop startup, but install it
# after the canonical project sync whenever the operator requests the real
# vendored graph.  This prevents `uv sync --no-dev` from silently removing the
# dependencies that the isolated TradingAgents worker needs.
if [ "${STOCK_AI_ENABLE_TRADINGAGENTS_RUNTIME:-0}" = "1" ]; then
  step "Preparing the opt-in TradingAgents research runtime..."
  "$UV" pip install --python "$VENV_PYTHON" -e "${PROJECT_ROOT}/external/TradingAgents" ||
    fail "TradingAgents research runtime setup failed."
fi

# A launchd-owned process cannot reliably initialize Python through a virtual
# environment symlink located in Desktop-protected storage. Resolve the same
# base interpreter and pass only the verified venv site-packages plus project
# source explicitly. ``-S`` is required below: without it the base interpreter
# also imports Anaconda's global site-packages, mixing binary extensions built
# against a different NumPy ABI into this managed runtime.
SERVER_PYTHON="$(readlink "$VENV_PYTHON" 2>/dev/null || true)"
[ -x "$SERVER_PYTHON" ] || SERVER_PYTHON="$VENV_PYTHON"
[ -d "$VENV_SITE_PACKAGES" ] || fail "Python site-packages are missing: $VENV_SITE_PACKAGES"
synchronize_launchd_source
SERVER_PYTHONPATH="${VENV_SITE_PACKAGES}:${SERVICE_SOURCE_ROOT}/src"

step "Preparing the Codex connection..."
if select_existing_codex_binary; then
  :
elif [ -n "${STOCK_AI_CODEX_BIN:-}" ]; then
  fail "STOCK_AI_CODEX_BIN must point to an existing executable."
else
  mkdir -p "$CODEX_DIR" || fail "Cannot create the Codex runtime folder."
  if curl --proto '=https' --tlsv1.2 -LsSf https://github.com/openai/codex/releases/latest/download/install.sh |
    env CODEX_INSTALL_DIR="$CODEX_DIR" CODEX_NON_INTERACTIVE=1 sh; then
    if [ -x "$CODEX_BIN" ]; then
      export STOCK_AI_CODEX_BIN="$CODEX_BIN"
    else
      printf '\033[33mCodex installer finished without a usable executable; the stock system will still open.\033[0m\n'
    fi
  else
    printf '\033[33mCodex could not be downloaded. The stock system will open normally; Codex can be connected later from Settings.\033[0m\n'
  fi
fi

if [ ! -f "${PROJECT_ROOT}/.env" ] && [ -f "${PROJECT_ROOT}/.env.example" ]; then
  cp "${PROJECT_ROOT}/.env.example" "${PROJECT_ROOT}/.env" || fail "Could not create .env."
fi

rm -f "$STDOUT_LOG" "$STDERR_LOG"
step "Starting commit ${GIT_COMMIT} on port ${PORT}..."
# Use launchd instead of a shell-owned background process.  This keeps the
# local server alive when the .command file is opened from Finder, while the
# existing PID/cwd checks still ensure we touch only this project copy.
if ! launchctl submit -l "$LAUNCH_AGENT_LABEL" -o "$STDOUT_LOG" -e "$STDERR_LOG" -- \
  /bin/sh -c 'cd "$1" || exit 1; pid_file="$2"; shift 2; printf "%s\n" "$$" > "${pid_file}.tmp" && mv "${pid_file}.tmp" "$pid_file" || exit 1; exec "$@"' -- \
  "$SERVICE_SOURCE_ROOT" "$PID_FILE" \
  /usr/bin/env \
  STOCK_AI_CODEX_BIN="${STOCK_AI_CODEX_BIN:-}" \
  STOCK_AI_INSTANCE_ID="$PROJECT_INSTANCE_ID" \
  STOCK_AI_BUILD_COMMIT="$GIT_COMMIT" \
  STOCK_AI_PROJECT_ROOT="$SERVICE_SOURCE_ROOT" \
  STOCK_AI_AGENT_DATA_ROOT="$AGENT_DATA_ROOT" \
  STOCK_AI_AGENT_BACKGROUND_PAUSED="${STOCK_AI_AGENT_BACKGROUND_PAUSED:-0}" \
  STOCK_AI_OPERATOR_ALERT_SINKS="${STOCK_AI_OPERATOR_ALERT_SINKS:-desktop}" \
  STOCK_AI_MARKET_DATA_DB="$MARKET_DATA_DB" \
  VIRTUAL_ENV="$VENV_DIR" \
  PYTHONPATH="$SERVER_PYTHONPATH" \
  N8N_AUTOMATION_GATEWAY_URL="${N8N_AUTOMATION_GATEWAY_URL:-}" \
  N8N_AUTOMATION_GATEWAY_TOKEN="${N8N_AUTOMATION_GATEWAY_TOKEN:-}" \
  N8N_AUTOMATION_CALLBACK_SECRET="${N8N_AUTOMATION_CALLBACK_SECRET:-}" \
  "$SERVER_PYTHON" -S -m uvicorn stock_ai.main:app --host 127.0.0.1 --port "$PORT"; then
  # Some managed desktop sessions prohibit launchctl submission even though
  # the local project is otherwise runnable. Keep the .command launcher
  # usable there: the same explicit environment and logs are used, only the
  # process owner changes. Finder/macOS installations still take launchd.
  step "launchd is unavailable; using the local launcher fallback..."
  (
    cd "$SERVICE_SOURCE_ROOT" || exit 1
    exec /usr/bin/env \
      STOCK_AI_CODEX_BIN="${STOCK_AI_CODEX_BIN:-}" \
      STOCK_AI_INSTANCE_ID="$PROJECT_INSTANCE_ID" \
      STOCK_AI_BUILD_COMMIT="$GIT_COMMIT" \
      STOCK_AI_PROJECT_ROOT="$SERVICE_SOURCE_ROOT" \
      STOCK_AI_AGENT_DATA_ROOT="$AGENT_DATA_ROOT" \
      STOCK_AI_AGENT_BACKGROUND_PAUSED="${STOCK_AI_AGENT_BACKGROUND_PAUSED:-0}" \
      STOCK_AI_OPERATOR_ALERT_SINKS="${STOCK_AI_OPERATOR_ALERT_SINKS:-desktop}" \
      STOCK_AI_MARKET_DATA_DB="$MARKET_DATA_DB" \
      VIRTUAL_ENV="$VENV_DIR" \
      PYTHONPATH="$SERVER_PYTHONPATH" \
      N8N_AUTOMATION_GATEWAY_URL="${N8N_AUTOMATION_GATEWAY_URL:-}" \
      N8N_AUTOMATION_GATEWAY_TOKEN="${N8N_AUTOMATION_GATEWAY_TOKEN:-}" \
      N8N_AUTOMATION_CALLBACK_SECRET="${N8N_AUTOMATION_CALLBACK_SECRET:-}" \
      "$SERVER_PYTHON" -S -m uvicorn stock_ai.main:app --host 127.0.0.1 --port "$PORT"
  ) >"$STDOUT_LOG" 2>"$STDERR_LOG" &
fi
SERVER_PID=""
printf '%s\n' "$PORT" > "$PORT_FILE"
printf '%s\n' "$GIT_COMMIT" > "$COMMIT_FILE"
printf '%s\n' "$WORKTREE_FINGERPRINT" > "$FINGERPRINT_FILE"
printf '%s\n' "$N8N_GATEWAY_GENERATION" > "$GATEWAY_GENERATION_FILE"
printf '%s\n' "$PROJECT_ROOT" > "$ROOT_FILE"
printf '%s\n' "$PROJECT_INSTANCE_ID" > "$INSTANCE_FILE"

READY=0
ATTEMPT=0
# A multi-gigabyte durable Agent database can need several minutes after a
# cold start to rebuild SQLite schema caches. Keep the native window closed
# until the exact project instance and Codex capabilities endpoint have both
# passed the readiness contract, rather than falsely reporting startup failure
# while Uvicorn is still completing its application lifespan.
while [ "$ATTEMPT" -lt 600 ]; do
  if server_is_ready; then
    SERVER_PID="$(listener_pid_for_port "$PORT")"
    [ -n "$SERVER_PID" ] || break
    printf '%s\n' "$SERVER_PID" > "$PID_FILE"
    READY=1
    break
  fi
  ATTEMPT=$((ATTEMPT + 1))
  sleep 0.5
done

if [ "$READY" -ne 1 ]; then
  rm -f "$PID_FILE" "$PORT_FILE" "$COMMIT_FILE" "$FINGERPRINT_FILE" "$GATEWAY_GENERATION_FILE" "$ROOT_FILE" "$INSTANCE_FILE"
  if [ -f "$STDERR_LOG" ]; then
    tail -n 60 "$STDERR_LOG" >&2
  fi
  fail "The server did not become ready."
fi

printf '\033[32mReady: %s (PID %s, commit %s, instance %s)\033[0m\n' "$URL" "$SERVER_PID" "$GIT_COMMIT" "$PROJECT_INSTANCE_ID"
open_stock_ai_ui
exit 0
