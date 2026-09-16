#!/bin/bash

set -u

PROJECT_ROOT="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
RUNTIME_ROOT_HELPER="${PROJECT_ROOT}/scripts/runtime-root.sh"
[ -x "$RUNTIME_ROOT_HELPER" ] || {
  printf 'Shutdown failed: runtime helper is missing.\n' >&2
  exit 1
}
source "$RUNTIME_ROOT_HELPER" "$PROJECT_ROOT"
if command -v shasum >/dev/null 2>&1; then
  PROJECT_INSTANCE_ID="$(printf '%s' "$PROJECT_ROOT" | shasum -a 256 | awk '{print substr($1,1,16)}')"
else
  PROJECT_INSTANCE_ID="$(printf '%s' "$PROJECT_ROOT" | cksum | awk '{print $1}')"
fi
LAUNCH_AGENT_LABEL="io.github.magicianbee.stockai.${PROJECT_INSTANCE_ID}"
PROJECT_RUNTIME_ROOT="${PROJECT_ROOT}/.runtime"
RUNTIME_ROOT="$STOCK_AI_RUNTIME_ROOT"
DEVICE_ID="$(df -P "$PROJECT_ROOT" 2>/dev/null | awk 'END {print $1}')"
FILESYSTEM_TYPE="$(diskutil info "$DEVICE_ID" 2>/dev/null | awk -F: '/Type \(Bundle\)/ {gsub(/[[:space:]]/, "", $2); print tolower($2); exit}')"
case "$FILESYSTEM_TYPE" in
  exfat|msdos)
    PROJECT_ID="$(printf '%s' "$PROJECT_ROOT" | cksum | awk '{print $1}')"
    RUNTIME_ROOT="${HOME}/Library/Caches/StockAI-System/${PROJECT_ID}"
    ;;
esac
if [ "$RUNTIME_ROOT" = "$PROJECT_RUNTIME_ROOT" ] && [ ! -d "$PROJECT_RUNTIME_ROOT" ]; then
  RUNTIME_ROOT="$PROJECT_RUNTIME_ROOT"
fi
MANAGED_SERVICE_SOURCE="${RUNTIME_ROOT}/service-source"

PID_FILE="${PROJECT_ROOT}/logs/stock-ai-server.pid"
PORT_FILE="${PROJECT_ROOT}/logs/stock-ai-server.port"
COMMIT_FILE="${PROJECT_ROOT}/logs/stock-ai-server.commit"
FINGERPRINT_FILE="${PROJECT_ROOT}/logs/stock-ai-server.source-fingerprint"
ROOT_FILE="${PROJECT_ROOT}/logs/stock-ai-server.root"
INSTANCE_FILE="${PROJECT_ROOT}/logs/stock-ai-server.instance"
NATIVE_APP_EXECUTABLE="${RUNTIME_ROOT}/apps/Stock AI Liquid Glass.app/Contents/MacOS/StockAILiquidGlass"
STOPPED=0

# A launchctl-submitted process is supervised and will be started again after
# a plain kill. Remove the exact project job before terminating its PID.
launchctl remove "$LAUNCH_AGENT_LABEL" >/dev/null 2>&1 || true

process_cwd() {
  local pid="$1"
  lsof -a -p "$pid" -d cwd -Fn 2>/dev/null | sed -n 's/^n//p' | head -n 1
}

project_server_cwd_is_owned() {
  case "$1" in
    "$PROJECT_ROOT"|"$MANAGED_SERVICE_SOURCE") return 0 ;;
    *) return 1 ;;
  esac
}

stop_verified_pid() {
  local pid="$1" command cwd attempt
  [ -n "$pid" ] || return 1
  kill -0 "$pid" >/dev/null 2>&1 || return 1
  command="$(ps -p "$pid" -o command= 2>/dev/null || true)"
  cwd="$(process_cwd "$pid")"
  case "$command" in
    *uvicorn*stock_ai.main:app*)
      project_server_cwd_is_owned "$cwd" || return 1
      kill "$pid" >/dev/null 2>&1 || true
      attempt=0
      while kill -0 "$pid" >/dev/null 2>&1 && [ "$attempt" -lt 30 ]; do
        sleep 0.1
        attempt=$((attempt + 1))
      done
      if kill -0 "$pid" >/dev/null 2>&1; then
        kill -KILL "$pid" >/dev/null 2>&1 || true
      fi
      return 0
      ;;
  esac
  return 1
}

if [ -f "$PID_FILE" ]; then
  PID="$(tr -cd '0-9' < "$PID_FILE")"
  if stop_verified_pid "$PID"; then
    STOPPED=1
  fi
fi

# Recover from a stale or missing PID file without touching servers from another copy.
for PID in $(pgrep -f 'uvicorn.*stock_ai\.main:app' 2>/dev/null || true); do
  if stop_verified_pid "$PID"; then
    STOPPED=1
  fi
done

if [ -x "$NATIVE_APP_EXECUTABLE" ]; then
  pkill -f "$NATIVE_APP_EXECUTABLE" >/dev/null 2>&1 || true
fi

rm -f "$PID_FILE" "$PORT_FILE" "$COMMIT_FILE" "$FINGERPRINT_FILE" "$ROOT_FILE" "$INSTANCE_FILE"

if [ "$STOPPED" -eq 1 ]; then
  printf 'Stock AI System stopped for: %s\n' "$PROJECT_ROOT"
else
  printf 'No Stock AI System process owned by this folder was running.\n'
fi
sleep 1
