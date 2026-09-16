#!/bin/bash

# Keep executable/runtime state out of the macOS Desktop privacy domain. A
# launchd service cannot safely traverse Desktop files, so it must own its
# project-scoped runtime under Application Support. The Desktop path remains a
# symlink for CLI tools and backwards-compatible maintenance commands.
set -euo pipefail

STOCK_AI_PROJECT_ROOT="${1:-$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)}"
STOCK_AI_LEGACY_RUNTIME_ROOT="$STOCK_AI_PROJECT_ROOT/.runtime"
if command -v shasum >/dev/null 2>&1; then
  STOCK_AI_RUNTIME_INSTANCE_ID="$(printf '%s' "$STOCK_AI_PROJECT_ROOT" | shasum -a 256 | awk '{print substr($1,1,16)}')"
else
  STOCK_AI_RUNTIME_INSTANCE_ID="$(printf '%s' "$STOCK_AI_PROJECT_ROOT" | cksum | awk '{print $1}')"
fi
STOCK_AI_RUNTIME_ROOT="${HOME}/Library/Application Support/StockAI-System/${STOCK_AI_RUNTIME_INSTANCE_ID}/runtime"

if [ -L "$STOCK_AI_LEGACY_RUNTIME_ROOT" ]; then
  STOCK_AI_LEGACY_TARGET="$(readlink "$STOCK_AI_LEGACY_RUNTIME_ROOT")"
  [ "$STOCK_AI_LEGACY_TARGET" = "$STOCK_AI_RUNTIME_ROOT" ] || {
    printf 'runtime migration refused: the legacy runtime link points somewhere else: %s\n' "$STOCK_AI_LEGACY_RUNTIME_ROOT" >&2
    exit 1
  }
elif [ -e "$STOCK_AI_LEGACY_RUNTIME_ROOT" ] && [ ! -e "$STOCK_AI_RUNTIME_ROOT" ]; then
  mkdir -p "$(dirname "$STOCK_AI_RUNTIME_ROOT")"
  mv "$STOCK_AI_LEGACY_RUNTIME_ROOT" "$STOCK_AI_RUNTIME_ROOT"
elif [ -e "$STOCK_AI_LEGACY_RUNTIME_ROOT" ] && [ -e "$STOCK_AI_RUNTIME_ROOT" ]; then
  printf 'runtime migration refused: both legacy and managed runtime directories exist.\n' >&2
  exit 1
fi

mkdir -p "$STOCK_AI_RUNTIME_ROOT"
if [ ! -L "$STOCK_AI_LEGACY_RUNTIME_ROOT" ]; then
  ln -s "$STOCK_AI_RUNTIME_ROOT" "$STOCK_AI_LEGACY_RUNTIME_ROOT"
fi
chmod 700 "$STOCK_AI_RUNTIME_ROOT"

export STOCK_AI_RUNTIME_ROOT STOCK_AI_RUNTIME_INSTANCE_ID STOCK_AI_LEGACY_RUNTIME_ROOT
