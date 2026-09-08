#!/bin/bash

PROJECT_ROOT="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
RUNTIME_ROOT_HELPER="${PROJECT_ROOT}/scripts/runtime-root.sh"
[ -x "$RUNTIME_ROOT_HELPER" ] || {
  printf 'Startup failed: runtime helper is missing.\n' >&2
  exit 1
}
source "$RUNTIME_ROOT_HELPER" "$PROJECT_ROOT"
NATIVE_APP_EXECUTABLE="${STOCK_AI_RUNTIME_ROOT}/apps/Stock AI Liquid Glass.app/Contents/MacOS/StockAILiquidGlass"
NATIVE_APP_PLIST="${STOCK_AI_RUNTIME_ROOT}/apps/Stock AI Liquid Glass.app/Contents/Info.plist"
BUNDLE_ID="com.choubee.stockai.liquidglass.instance${STOCK_AI_RUNTIME_INSTANCE_ID}"

# The original project copy and the Agent test copy use the same macOS app
# executable. Match the full executable path because macOS may truncate long
# process names when pkill -x is used.
pkill -f "$NATIVE_APP_EXECUTABLE" >/dev/null 2>&1 || true
sleep 0.4

# Clear only WebKit network/resource caches for the actual stable bundle ID.
# LocalStorage, UI settings, SQLite state and all project data are preserved.
# Using a synthetic path-derived ID here would leave the real native cache
# untouched and make acceptance tests load stale JavaScript.
rm -rf "${HOME}/Library/Caches/${BUNDLE_ID}" >/dev/null 2>&1 || true
rm -rf "${HOME}/Library/HTTPStorages/${BUNDLE_ID}" >/dev/null 2>&1 || true
WEBKIT_DATA="${HOME}/Library/WebKit/${BUNDLE_ID}/WebsiteData"
if [ -d "$WEBKIT_DATA" ]; then
  find "$WEBKIT_DATA" -maxdepth 3 \( -name NetworkCache -o -name CacheStorage -o -name ServiceWorkerRegistrations \) -exec rm -rf {} + >/dev/null 2>&1 || true
fi

exec /bin/bash "${PROJECT_ROOT}/open-stock-ai.sh"
