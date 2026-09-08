#!/bin/bash

# Bootstrap the loopback-only n8n instance used by Stock AI. Secrets are
# generated locally and written only below the ignored .runtime directory.
set -euo pipefail

PROJECT_ROOT="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"
source "$PROJECT_ROOT/scripts/n8n-runtime-root.sh" "$PROJECT_ROOT"
N8N_URL="http://127.0.0.1:5678"
DATA_ROOT="$N8N_RUNTIME_ROOT/data"
GATEWAY_ENV_FILE="$DATA_ROOT/stock-ai-gateway.env"
COOKIE_FILE="$DATA_ROOT/.stock-ai-bootstrap-cookie"
RESPONSE_FILE="$DATA_ROOT/.stock-ai-owner-response.json"

mkdir -p "$DATA_ROOT"
chmod 700 "$DATA_ROOT"

read_gateway_value() {
  local wanted="$1" name value
  [ -r "$GATEWAY_ENV_FILE" ] || return 1
  while IFS='=' read -r name value; do
    if [ "$name" = "$wanted" ]; then
      printf '%s' "$value"
      return 0
    fi
  done < "$GATEWAY_ENV_FILE"
  return 1
}

existing_token="$(read_gateway_value N8N_AUTOMATION_GATEWAY_TOKEN || true)"
existing_callback_secret="$(read_gateway_value N8N_AUTOMATION_CALLBACK_SECRET || true)"
existing_owner_email="$(read_gateway_value N8N_LOCAL_OWNER_EMAIL || true)"
existing_owner_password="$(read_gateway_value N8N_LOCAL_OWNER_PASSWORD || true)"
existing_gateway_generation="$(read_gateway_value N8N_AUTOMATION_GATEWAY_GENERATION || true)"

ensure_gateway_generation() {
  [ -n "$existing_gateway_generation" ] && return 0
  gateway_generation="$(date -u +%Y%m%dT%H%M%SZ)"
  umask 077
  printf 'N8N_AUTOMATION_GATEWAY_GENERATION=%s\n' "$gateway_generation" >> "$GATEWAY_ENV_FILE"
  chmod 600 "$GATEWAY_ENV_FILE"
}

gateway_key_is_ready() {
  [ -n "$1" ] &&
    curl -fsS --max-time 3 -H "X-N8N-API-KEY: $1" \
      "$N8N_URL/api/v1/workflows?limit=1&excludePinnedData=true" >/dev/null 2>&1 &&
    curl -fsS --max-time 3 -H "X-N8N-API-KEY: $1" \
      "$N8N_URL/api/v1/executions?limit=1&includeData=false" >/dev/null 2>&1
}

if gateway_key_is_ready "$existing_token"; then
  if [ -z "$existing_callback_secret" ]; then
    callback_secret="$(openssl rand -hex 32)"
    umask 077
    printf 'N8N_AUTOMATION_CALLBACK_SECRET=%s\n' "$callback_secret" >> "$GATEWAY_ENV_FILE"
    chmod 600 "$GATEWAY_ENV_FILE"
    printf 'n8n API key is ready; generated an independent callback secret.\n'
  else
    printf 'n8n local owner, scoped API key and independent callback secret are already ready.\n'
  fi
  ensure_gateway_generation
  exit 0
fi

settings_body="$(curl -fsS --max-time 3 "$N8N_URL/rest/settings")"
compact_settings="$(printf '%s' "$settings_body" | tr -d '[:space:]')"
case "$compact_settings" in
  *'"showSetupOnFirstLoad":true'*)
    owner_email="stock-ai-local@localhost.invalid"
    owner_password="$(openssl rand -hex 24)Aa1x"
    owner_payload="$(
      jq -n \
        --arg email "$owner_email" \
        --arg firstName Stock \
        --arg lastName AI \
        --arg password "$owner_password" \
        '{email:$email,firstName:$firstName,lastName:$lastName,password:$password}'
    )"
    setup_code="$(
      curl -sS -o "$RESPONSE_FILE" -w '%{http_code}' -c "$COOKIE_FILE" \
        -H 'Content-Type: application/json' \
        -X POST --data "$owner_payload" "$N8N_URL/rest/owner/setup"
    )"
    if [ "$setup_code" != "200" ]; then
      printf 'n8n owner setup failed with HTTP %s.\n' "$setup_code" >&2
      exit 1
    fi
    ;;
  *)
    if [ -z "$existing_owner_email" ] || [ -z "$existing_owner_password" ]; then
      printf 'n8n already has an owner, but Stock AI has no valid local API key or local owner recovery credentials.\n' >&2
      printf 'Create a scoped key in n8n and save it as N8N_AUTOMATION_GATEWAY_TOKEN in %s.\n' "$GATEWAY_ENV_FILE" >&2
      exit 1
    fi
    owner_email="$existing_owner_email"
    owner_password="$existing_owner_password"
    login_payload="$(
      jq -n \
        --arg email "$owner_email" \
        --arg password "$owner_password" \
        '{emailOrLdapLoginId:$email,password:$password}'
    )"
    login_code="$(
      curl -sS -o "$RESPONSE_FILE" -w '%{http_code}' -c "$COOKIE_FILE" \
        -H 'Content-Type: application/json' \
        -X POST --data "$login_payload" "$N8N_URL/rest/login"
    )"
    if [ "$login_code" != "200" ]; then
      printf 'n8n owner login for local gateway recovery failed with HTTP %s.\n' "$login_code" >&2
      exit 1
    fi
    printf 'n8n local gateway API key is invalid; authenticated the existing local owner to recover a scoped replacement.\n'
    ;;
esac

scopes_json="$(curl -fsS -b "$COOKIE_FILE" "$N8N_URL/rest/api-keys/scopes")"
scopes="$(
  printf '%s' "$scopes_json" |
    jq -c '[.. | strings | select(test("^(workflow:(create|list|read|update|activate|deactivate|delete)|execution:(list|read|delete))$"))] | unique'
)"
scope_count="$(printf '%s' "$scopes" | jq 'length')"
if [ "$scope_count" -lt 9 ]; then
  printf 'n8n did not expose the required workflow and execution API scopes.\n' >&2
  exit 1
fi

# n8n enforces a unique API-key label for each owner. A token may be
# unrecoverable after an n8n reset while its redacted record is still present,
# so replace only Stock AI's own, exact-label gateway key before creating the
# successor. The owner session above limits this list to the local owner.
existing_gateway_keys="$(curl -fsS -b "$COOKIE_FILE" "$N8N_URL/rest/api-keys?ownership=mine&label=Stock%20AI%20local%20gateway")"
existing_gateway_key_ids="$(
  printf '%s' "$existing_gateway_keys" |
    jq -r '(.data.items // .items // [])[] | select(.label == "Stock AI local gateway") | .id // empty'
)"
for existing_gateway_key_id in $existing_gateway_key_ids; do
  case "$existing_gateway_key_id" in
    *[!A-Za-z0-9_-]*|'')
      printf 'n8n returned an unsafe local gateway API-key identifier.\n' >&2
      exit 1
      ;;
  esac
  delete_code="$(
    curl -sS -o "$RESPONSE_FILE" -w '%{http_code}' -b "$COOKIE_FILE" \
      -X DELETE "$N8N_URL/rest/api-keys/$existing_gateway_key_id"
  )"
  case "$delete_code" in
    2??) ;;
    *)
      printf 'n8n local gateway API-key replacement failed while removing its stale key (HTTP %s).\n' "$delete_code" >&2
      exit 1
      ;;
  esac
done
if [ -n "$existing_gateway_key_ids" ]; then
  printf 'Removed stale Stock AI local gateway API key before replacement.\n'
fi

key_payload="$(
  jq -n \
    --arg label 'Stock AI local gateway' \
    --argjson scopes "$scopes" \
    '{label:$label,scopes:$scopes,expiresAt:null}'
)"
key_response="$(
  curl -fsS -b "$COOKIE_FILE" \
    -H 'Content-Type: application/json' \
    -X POST --data "$key_payload" "$N8N_URL/rest/api-keys"
)"
raw_key="$(printf '%s' "$key_response" | jq -r '.data.rawApiKey // .rawApiKey // empty')"
if [ -z "$raw_key" ]; then
  printf 'n8n API key creation returned no usable key.\n' >&2
  exit 1
fi

callback_secret="$existing_callback_secret"
if [ -z "$callback_secret" ]; then
  callback_secret="$(openssl rand -hex 32)"
fi
gateway_generation="$(date -u +%Y%m%dT%H%M%SZ)"

umask 077
printf 'N8N_AUTOMATION_GATEWAY_URL=%s\nN8N_AUTOMATION_GATEWAY_TOKEN=%s\nN8N_AUTOMATION_CALLBACK_SECRET=%s\nN8N_AUTOMATION_GATEWAY_GENERATION=%s\nN8N_LOCAL_OWNER_EMAIL=%s\nN8N_LOCAL_OWNER_PASSWORD=%s\n' \
  "$N8N_URL" "$raw_key" "$callback_secret" "$gateway_generation" "$owner_email" "$owner_password" > "$GATEWAY_ENV_FILE"
chmod 600 "$GATEWAY_ENV_FILE" "$COOKIE_FILE" "$RESPONSE_FILE"
printf 'n8n local owner, scoped API key and independent callback secret are ready.\n'
