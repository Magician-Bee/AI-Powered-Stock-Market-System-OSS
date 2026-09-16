#!/bin/bash

# Rotate only Stock AI's n8n callback secret. The n8n public API key is not
# changed. Existing workflows must be redeployed after this operation so
# their callback headers use the new wire token.
set -euo pipefail

PROJECT_ROOT="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"
source "$PROJECT_ROOT/scripts/n8n-runtime-root.sh" "$PROJECT_ROOT"
DATA_ROOT="$N8N_RUNTIME_ROOT/data"
GATEWAY_ENV_FILE="$DATA_ROOT/stock-ai-gateway.env"

[ -r "$GATEWAY_ENV_FILE" ] || {
  printf 'n8n gateway environment file is missing: %s\n' "$GATEWAY_ENV_FILE" >&2
  exit 1
}

new_secret="$(openssl rand -hex 32)"
umask 077
temporary_file="$(mktemp "$DATA_ROOT/.stock-ai-gateway.env.XXXXXX")"
trap 'rm -f "$temporary_file"' EXIT

while IFS='=' read -r name value; do
  [ "$name" = "N8N_AUTOMATION_CALLBACK_SECRET" ] && continue
  printf '%s=%s\n' "$name" "$value"
done < "$GATEWAY_ENV_FILE" > "$temporary_file"
printf 'N8N_AUTOMATION_CALLBACK_SECRET=%s\n' "$new_secret" >> "$temporary_file"
chmod 600 "$temporary_file"
mv "$temporary_file" "$GATEWAY_ENV_FILE"
trap - EXIT

printf 'Rotated N8N_AUTOMATION_CALLBACK_SECRET only. Redeploy existing workflows before execution.\n'
