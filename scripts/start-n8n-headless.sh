#!/bin/bash

# Start a project-local n8n instance as a loopback-only execution service.
# Stock AI never turns off n8n authentication and never writes user API keys
# or credentials into source control. The Agent's semantic compiler still
# owns goals and plans; n8n receives only approved, sealed execution work.
set -euo pipefail

PROJECT_ROOT="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"
source "$PROJECT_ROOT/scripts/n8n-runtime-root.sh" "$PROJECT_ROOT"
N8N_VERSION="2.33.7"
N8N_HOST="127.0.0.1"
N8N_PORT="5678"
RUNTIME_ROOT="$N8N_RUNTIME_ROOT"
PACKAGE_ROOT="$RUNTIME_ROOT/package"
DATA_ROOT="$RUNTIME_ROOT/data"
GATEWAY_ENV_FILE="$DATA_ROOT/stock-ai-gateway.env"
SETUP_SCRIPT="$PROJECT_ROOT/scripts/setup-n8n-local-owner.sh"
LOG_DIR="$RUNTIME_ROOT/logs"
PID_FILE="$LOG_DIR/stock-ai-n8n.pid"
LOG_FILE="$LOG_DIR/stock-ai-n8n.out.log"
N8N_BIN="$PACKAGE_ROOT/node_modules/.bin/n8n"
N8N_CLI="$PACKAGE_ROOT/node_modules/n8n/bin/n8n"
N8N_LAUNCH_AGENT_LABEL="com.choubee.stockai.n8n.${N8N_RUNTIME_INSTANCE_ID}"
HEALTH_URL="http://$N8N_HOST:$N8N_PORT/healthz"
READINESS_URL="${HEALTH_URL}/readiness"
MIN_FREE_KB=4194304

load_gateway_env() {
  local name value
  [ -r "$GATEWAY_ENV_FILE" ] || return 0
  while IFS='=' read -r name value; do
    case "$name" in
      N8N_AUTOMATION_GATEWAY_URL|N8N_AUTOMATION_GATEWAY_TOKEN|N8N_AUTOMATION_CALLBACK_SECRET)
        export "$name=$value"
        ;;
    esac
  done < "$GATEWAY_ENV_FILE"
}

fail() {
  printf 'n8n startup failed: %s\n' "$1" >&2
  printf 'Log: %s\n' "$LOG_FILE" >&2
  exit 1
}

listener_pid() {
  { lsof -nP -tiTCP:"$N8N_PORT" -sTCP:LISTEN 2>/dev/null || true; } | head -n 1
}

health_is_ready() {
  health_body="$(curl -fsS --max-time 2 "$HEALTH_URL" 2>/dev/null || true)"
  [ "$health_body" = '{"status":"ok"}' ]
}

readiness_is_ready() {
  # n8n exposes /healthz as soon as its HTTP listener exists.  At that point
  # migrations, the REST controllers and the public API can still be absent,
  # so owner/key setup would see a temporary 404 and misdiagnose it as an
  # invalid credential.  Only the readiness endpoint proves deployment APIs
  # are eligible to receive a request.
  readiness_body="$(curl -fsS --max-time 2 "$READINESS_URL" 2>/dev/null || true)"
  [ "$readiness_body" = '{"status":"ok"}' ]
}

api_is_ready() {
  [ -n "${N8N_AUTOMATION_GATEWAY_TOKEN:-}" ] || return 1
  curl -fsS --max-time 3 \
    -H "X-N8N-API-KEY: $N8N_AUTOMATION_GATEWAY_TOKEN" \
    "http://$N8N_HOST:$N8N_PORT/api/v1/workflows?limit=1&excludePinnedData=true" \
    >/dev/null 2>&1 &&
  curl -fsS --max-time 3 \
    -H "X-N8N-API-KEY: $N8N_AUTOMATION_GATEWAY_TOKEN" \
    "http://$N8N_HOST:$N8N_PORT/api/v1/executions?limit=1&includeData=false" \
    >/dev/null 2>&1
}

print_readiness() {
  load_gateway_env
  printf 'n8n process readiness: ready (%s)\n' "$READINESS_URL"
  if ! api_is_ready || [ -z "${N8N_AUTOMATION_CALLBACK_SECRET:-}" ]; then
    # This bootstrap is intentionally idempotent.  When an n8n reset has
    # invalidated only Stock AI's scoped gateway key, it authenticates the
    # project-local owner and replaces only that exact key label.  It also
    # restores a missing independent callback secret without exposing it.
    "$SETUP_SCRIPT"
    load_gateway_env
  fi
  if api_is_ready && [ -n "${N8N_AUTOMATION_CALLBACK_SECRET:-}" ]; then
    printf 'n8n public API: authenticated and deployment-ready\n'
  else
    fail "local owner bootstrap did not produce a valid scoped API key and callback secret."
  fi
}

wait_for_readiness() {
  local pid="$1" attempt=0
  while [ "$attempt" -lt 90 ]; do
    if readiness_is_ready; then
      return 0
    fi
    if [ -n "$pid" ] && ! kill -0 "$pid" >/dev/null 2>&1; then
      fail "n8n exited before its deployment readiness endpoint became ready."
    fi
    sleep 1
    attempt=$((attempt + 1))
  done
  fail "Timed out waiting for n8n deployment readiness endpoint."
}

start_with_launchd() {
  local node_bin launch_command
  node_bin="$(command -v node || true)"
  [ -x "$node_bin" ] || fail "node is required to run the pinned n8n runtime."
  [ -f "$N8N_CLI" ] || fail "the pinned n8n executable is missing: $N8N_CLI"

  # The command receives only paths. It reads the encryption key after
  # launchd has started, so no secret is ever stored in process arguments or
  # visible through launchctl inspection.
  launch_command='data_root="$1"; encryption_key="$2"; node_bin="$3"; n8n_cli="$4"; log_file="$5"
cd "$data_root" || exit 78
export PATH="$(dirname "$node_bin"):${PATH:-/usr/bin:/bin}"
export N8N_USER_FOLDER="$data_root"
export N8N_HOST="127.0.0.1" N8N_PORT="5678" N8N_LISTEN_ADDRESS="127.0.0.1" N8N_PROTOCOL="http"
export N8N_SECURE_COOKIE="false" N8N_PUBLIC_API_DISABLED="false"
export N8N_DIAGNOSTICS_ENABLED="false" N8N_PERSONALIZATION_ENABLED="false" N8N_TEMPLATES_ENABLED="false"
export N8N_BLOCK_ENV_ACCESS_IN_NODE="true" NODE_FUNCTION_ALLOW_BUILTIN="crypto"
export N8N_ENFORCE_SETTINGS_FILE_PERMISSIONS="true"
export EXECUTIONS_DATA_SAVE_ON_ERROR="none"
export EXECUTIONS_DATA_SAVE_ON_SUCCESS="none"
export EXECUTIONS_DATA_SAVE_ON_PROGRESS="false"
export EXECUTIONS_DATA_SAVE_MANUAL_EXECUTIONS="false"
export EXECUTIONS_DATA_PRUNE="true"
export EXECUTIONS_DATA_MAX_AGE="168"
export EXECUTIONS_DATA_PRUNE_MAX_COUNT="2000"
export EXECUTIONS_DATA_HARD_DELETE_BUFFER="1"
export EXECUTIONS_DATA_MAX_DISPLAY_SIZE="1048576"
N8N_ENCRYPTION_KEY="$(tr -d "\r\n" < "$encryption_key")"; export N8N_ENCRYPTION_KEY
exec "$node_bin" "$n8n_cli" start >> "$log_file" 2>&1'

  launchctl remove "$N8N_LAUNCH_AGENT_LABEL" >/dev/null 2>&1 || true
  launchctl submit -l "$N8N_LAUNCH_AGENT_LABEL" -o "$LOG_FILE" -e "$LOG_FILE" -- \
    /bin/sh -c "$launch_command" -- "$DATA_ROOT" "$DATA_ROOT/encryption.key" "$node_bin" "$N8N_CLI" "$LOG_FILE" ||
    fail "macOS could not register the local n8n execution service."
}

mkdir -p "$RUNTIME_ROOT" "$PACKAGE_ROOT" "$DATA_ROOT" "$LOG_DIR"
chmod 700 "$RUNTIME_ROOT" "$PACKAGE_ROOT" "$DATA_ROOT"
chmod +x "$SETUP_SCRIPT"
load_gateway_env

if health_is_ready; then
  wait_for_readiness "$(listener_pid)"
  print_readiness
  exit 0
fi

available_kb="$(df -Pk "$PROJECT_ROOT" | awk 'NR == 2 {print $4}')"
if [ -z "$available_kb" ] || [ "$available_kb" -lt "$MIN_FREE_KB" ]; then
  available_mb=$((available_kb / 1024))
  fail "n8n needs at least 4 GiB free for its pinned runtime and install workspace; only $available_mb MiB is available."
fi

existing_pid="$(listener_pid)"
if [ -n "$existing_pid" ]; then
  fail "Port $N8N_PORT is in use by PID $existing_pid; refusing to stop another process."
fi

installed_version=""
if [ -x "$N8N_BIN" ]; then
  installed_version="$($N8N_BIN --version 2>/dev/null || true)"
fi
if [ "$installed_version" != "$N8N_VERSION" ]; then
  command -v npm >/dev/null 2>&1 || fail "npm is required to install the pinned n8n runtime."
  printf 'Installing n8n %s into project runtime...\n' "$N8N_VERSION"
  npm install --prefix "$PACKAGE_ROOT" --no-audit --no-fund "n8n@$N8N_VERSION"
fi

if [ ! -s "$DATA_ROOT/encryption.key" ]; then
  umask 077
  openssl rand -hex 32 > "$DATA_ROOT/encryption.key"
  chmod 600 "$DATA_ROOT/encryption.key"
fi

start_with_launchd
wait_for_readiness ""
n8n_pid="$(listener_pid)"
[ -n "$n8n_pid" ] || fail "n8n did not expose its loopback listener after launchd registration."
printf '%s\n' "$n8n_pid" > "$PID_FILE"
printf 'n8n headless execution layer is supervised by macOS with PID %s.\n' "$n8n_pid"
print_readiness
printf 'Use a scoped n8n API key with workflow read/create/update/activate/deactivate/delete permissions; never commit it.\n'
