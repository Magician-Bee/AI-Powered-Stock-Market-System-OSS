#!/bin/bash

# Resolve the local n8n runtime outside the Desktop project folder. macOS
# launchd is intentionally denied access to Desktop-protected files, which
# makes a seemingly healthy n8n process disappear as soon as a Finder
# launcher exits. Keep one project-scoped runtime under Application Support
# and retain a legacy symlink so existing maintenance paths stay compatible.
set -euo pipefail

N8N_RUNTIME_PROJECT_ROOT="${1:-$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)}"
N8N_LEGACY_RUNTIME_ROOT="$N8N_RUNTIME_PROJECT_ROOT/.runtime/n8n"
if command -v shasum >/dev/null 2>&1; then
  N8N_RUNTIME_INSTANCE_ID="$(printf '%s' "$N8N_RUNTIME_PROJECT_ROOT" | shasum -a 256 | awk '{print substr($1,1,16)}')"
else
  N8N_RUNTIME_INSTANCE_ID="$(printf '%s' "$N8N_RUNTIME_PROJECT_ROOT" | cksum | awk '{print $1}')"
fi
N8N_RUNTIME_ROOT="${HOME}/Library/Application Support/StockAI-System/${N8N_RUNTIME_INSTANCE_ID}/n8n"

if [ -L "$N8N_LEGACY_RUNTIME_ROOT" ]; then
  N8N_LEGACY_TARGET="$(readlink "$N8N_LEGACY_RUNTIME_ROOT")"
  [ "$N8N_LEGACY_TARGET" = "$N8N_RUNTIME_ROOT" ] || {
    printf 'n8n runtime migration refused: legacy link points somewhere else: %s\n' "$N8N_LEGACY_RUNTIME_ROOT" >&2
    exit 1
  }
elif [ -e "$N8N_LEGACY_RUNTIME_ROOT" ] && [ ! -e "$N8N_RUNTIME_ROOT" ]; then
  mkdir -p "$(dirname "$N8N_RUNTIME_ROOT")"
  mv "$N8N_LEGACY_RUNTIME_ROOT" "$N8N_RUNTIME_ROOT"
elif [ -e "$N8N_LEGACY_RUNTIME_ROOT" ] && [ -e "$N8N_RUNTIME_ROOT" ]; then
  printf 'n8n runtime migration refused: both legacy and managed runtime directories exist.\n' >&2
  exit 1
fi

mkdir -p "$N8N_RUNTIME_ROOT"
if [ ! -L "$N8N_LEGACY_RUNTIME_ROOT" ]; then
  mkdir -p "$(dirname "$N8N_LEGACY_RUNTIME_ROOT")"
  ln -s "$N8N_RUNTIME_ROOT" "$N8N_LEGACY_RUNTIME_ROOT"
fi
chmod 700 "$N8N_RUNTIME_ROOT"

export N8N_RUNTIME_INSTANCE_ID N8N_RUNTIME_ROOT N8N_LEGACY_RUNTIME_ROOT
