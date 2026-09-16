#!/bin/bash

set -u

PROJECT_ROOT="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
LOG_DIR="${PROJECT_ROOT}/logs"
PID_FILE="${LOG_DIR}/stock-ai-server.pid"
PORT_FILE="${LOG_DIR}/stock-ai-server.port"
COMMIT_FILE="${LOG_DIR}/stock-ai-server.commit"
ROOT_FILE="${LOG_DIR}/stock-ai-server.root"
INSTANCE_FILE="${LOG_DIR}/stock-ai-server.instance"
LOCAL_SCRIPT="${PROJECT_ROOT}/src/stock_ai/ui/static/paper-training.js"
MANAGED_SERVICE_SOURCE=""

line() {
  printf '%-18s %s\n' "$1" "$2"
}

hash_file() {
  if command -v shasum >/dev/null 2>&1; then
    shasum -a 256 "$1" | awk '{print $1}'
  else
    openssl dgst -sha256 "$1" | awk '{print $NF}'
  fi
}

printf '\n========================================\n'
printf ' Stock AI Running Instance Verification\n'
printf '========================================\n'

BRANCH="$(git -C "$PROJECT_ROOT" branch --show-current 2>/dev/null || printf 'unknown')"
COMMIT="$(git -C "$PROJECT_ROOT" rev-parse --short=12 HEAD 2>/dev/null || printf 'working-copy')"
line 'Project folder:' "$PROJECT_ROOT"
line 'Git branch:' "$BRANCH"
line 'Git commit:' "$COMMIT"

if [ ! -f "$PID_FILE" ] || [ ! -f "$PORT_FILE" ]; then
  printf '\nFAIL: This project copy has no running-server PID/port record.\n'
  printf 'Launch it with: %s\n' "${PROJECT_ROOT}/開啟股市AI系統.command"
  exit 1
fi

PID="$(tr -cd '0-9' < "$PID_FILE")"
PORT="$(tr -cd '0-9' < "$PORT_FILE")"
RECORDED_COMMIT="$(cat "$COMMIT_FILE" 2>/dev/null || printf 'missing')"
RECORDED_ROOT="$(cat "$ROOT_FILE" 2>/dev/null || printf 'missing')"
RECORDED_INSTANCE="$(cat "$INSTANCE_FILE" 2>/dev/null || printf 'missing')"
PROCESS_CWD="$(lsof -a -p "$PID" -d cwd -Fn 2>/dev/null | sed -n 's/^n//p' | head -n 1)"
COMMAND="$(ps -p "$PID" -o command= 2>/dev/null || true)"
URL="http://127.0.0.1:${PORT}"

# The desktop launcher deliberately mirrors executable source into this
# project-scoped Application Support runtime before launchd starts Uvicorn.
# Treat that exact managed source root as current; requiring the process to
# run directly from Desktop would incorrectly report every healthy native
# launch as stale. The instance ID is recomputed from the Desktop project path
# so a copied log file cannot authorize another project's runtime.
if command -v shasum >/dev/null 2>&1; then
  EXPECTED_INSTANCE="$(printf '%s' "$PROJECT_ROOT" | shasum -a 256 | awk '{print substr($1,1,16)}')"
else
  EXPECTED_INSTANCE="$(printf '%s' "$PROJECT_ROOT" | cksum | awk '{print $1}')"
fi
MANAGED_SERVICE_SOURCE="${HOME}/Library/Application Support/StockAI-System/${EXPECTED_INSTANCE}/runtime/service-source"

line 'Server PID:' "$PID"
line 'Server port:' "$PORT"
line 'Process cwd:' "${PROCESS_CWD:-not-running}"
line 'Recorded root:' "$RECORDED_ROOT"
line 'Recorded commit:' "$RECORDED_COMMIT"
line 'Instance ID:' "$RECORDED_INSTANCE"

FAILED=0
if [ "$PROCESS_CWD" != "$PROJECT_ROOT" ] && [ "$PROCESS_CWD" != "$MANAGED_SERVICE_SOURCE" ]; then
  printf '\nFAIL: The server process is running from another project folder or managed runtime.\n'
  FAILED=1
fi
if [ "$RECORDED_ROOT" != "$PROJECT_ROOT" ]; then
  printf '\nFAIL: The launcher root record does not match this folder.\n'
  FAILED=1
fi
if [ "$RECORDED_INSTANCE" != "$EXPECTED_INSTANCE" ]; then
  printf '\nFAIL: The launcher instance record does not match this project folder.\n'
  FAILED=1
fi
if [ "$RECORDED_COMMIT" != "$COMMIT" ]; then
  printf '\nFAIL: The running server commit is %s, but this folder is %s.\n' "$RECORDED_COMMIT" "$COMMIT"
  FAILED=1
fi
case "$COMMAND" in
  *uvicorn*stock_ai.main:app*) ;;
  *)
    printf '\nFAIL: PID %s is not the Stock AI uvicorn server.\n' "$PID"
    FAILED=1
    ;;
esac

TMP_SCRIPT="$(mktemp -t stock-ai-served-js.XXXXXX)"
trap 'rm -f "$TMP_SCRIPT"' EXIT
if ! curl -fsS --max-time 8 "${URL}/static/paper-training.js?verify=$(date +%s)" > "$TMP_SCRIPT"; then
  printf '\nFAIL: Could not download paper-training.js from %s.\n' "$URL"
  exit 1
fi

LOCAL_SHA="$(hash_file "$LOCAL_SCRIPT")"
SERVED_SHA="$(hash_file "$TMP_SCRIPT")"
line 'Local UI SHA:' "$LOCAL_SHA"
line 'Served UI SHA:' "$SERVED_SHA"

if [ "$LOCAL_SHA" != "$SERVED_SHA" ]; then
  printf '\nFAIL: The running server is not serving the UI file from this project copy.\n'
  FAILED=1
fi

# The UI check above catches stale static assets. Compare the complete managed
# Python source mirror as well, so a matching commit environment variable
# cannot mask a failed source synchronization before native UI acceptance.
if ! command -v rsync >/dev/null 2>&1; then
  printf '\nFAIL: rsync is required to verify the managed Python source mirror.\n'
  FAILED=1
elif [ "$PROCESS_CWD" = "$MANAGED_SERVICE_SOURCE" ] &&
  rsync -rcn --delete "${PROJECT_ROOT}/src/" "${MANAGED_SERVICE_SOURCE}/src/" | grep -q .; then
  printf '\nFAIL: The managed runtime Python source differs from this project copy.\n'
  FAILED=1
fi

for marker in "broker-v5" "uncachedPath" "renderAccount(returnedAccount" "syncNavigationMaterial"; do
  if ! grep -F "$marker" "$TMP_SCRIPT" >/dev/null 2>&1; then
    printf '\nFAIL: The served UI is missing latest marker: %s\n' "$marker"
    FAILED=1
  fi
done

if [ "$FAILED" -ne 0 ]; then
  printf '\nRESULT: WRONG OR STALE INSTANCE\n'
  exit 1
fi

printf '\nRESULT: VERIFIED CURRENT PROJECT INSTANCE\n'
printf 'Open URL: %s/?stock_ai_commit=%s\n' "$URL" "$COMMIT"
exit 0
