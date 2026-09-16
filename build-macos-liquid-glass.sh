#!/bin/bash

set -euo pipefail

# Native build revision: official-appkit-single-glass-v8.
PROJECT_ROOT="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
SOURCE="${PROJECT_ROOT}/macos/StockAILiquidGlass/main.swift"
LOCALIZATION_PATCHER="${PROJECT_ROOT}/macos/StockAILiquidGlass/patch-localization.py"
ICON_RESOURCES="${PROJECT_ROOT}/macos/StockAILiquidGlass/Resources"
APP_DIR="${1:?Pass the output .app directory as the first argument.}"
SERVER_URL="${2:-http://127.0.0.1:8000/}"
CONTENTS_DIR="${APP_DIR}/Contents"
MACOS_DIR="${CONTENTS_DIR}/MacOS"
RESOURCES_DIR="${CONTENTS_DIR}/Resources"
EXECUTABLE="${MACOS_DIR}/StockAILiquidGlass"
PLIST="${CONTENTS_DIR}/Info.plist"
BUILD_DIR="${APP_DIR}.build"
PATCHED_SOURCE="${BUILD_DIR}/main.localized.swift"
SWIFTC="${STOCK_AI_SWIFTC:-/Library/Developer/CommandLineTools/usr/bin/swiftc}"
MACOS_SDK="${STOCK_AI_MACOS_SDK:-/Library/Developer/CommandLineTools/SDKs/MacOSX26.0.sdk}"

[ -f "$SOURCE" ] || {
  printf 'Missing native Liquid Glass source: %s\n' "$SOURCE" >&2
  exit 1
}
[ -f "$LOCALIZATION_PATCHER" ] || {
  printf 'Missing native localization patcher: %s\n' "$LOCALIZATION_PATCHER" >&2
  exit 1
}

[ -x "$SWIFTC" ] || {
  printf 'Swift 6.2 compiler not found: %s\n' "$SWIFTC" >&2
  exit 1
}
[ -d "$MACOS_SDK" ] || {
  printf 'macOS 26 SDK not found: %s\n' "$MACOS_SDK" >&2
  exit 1
}
command -v python3 >/dev/null 2>&1 || {
  printf 'Python 3 is required to prepare the native Liquid Glass source.\n' >&2
  exit 1
}

mkdir -p "$MACOS_DIR" "$RESOURCES_DIR" "$BUILD_DIR"
python3 "$LOCALIZATION_PATCHER" "$SOURCE" "$PATCHED_SOURCE"
cp -X "${ICON_RESOURCES}/StockAIIcon.icns" "$RESOURCES_DIR/StockAIIcon.icns"
cp -X "${ICON_RESOURCES}/StockAIIconLight.png" "$RESOURCES_DIR/StockAIIconLight.png"
cp -X "${ICON_RESOURCES}/StockAIIconDark.png" "$RESOURCES_DIR/StockAIIconDark.png"

ARM_EXECUTABLE="${BUILD_DIR}/StockAILiquidGlass-arm64"
INTEL_EXECUTABLE="${BUILD_DIR}/StockAILiquidGlass-x86_64"

"$SWIFTC" \
  "$PATCHED_SOURCE" \
  -O \
  -sdk "$MACOS_SDK" \
  -target arm64-apple-macos26.0 \
  -framework AppKit \
  -framework WebKit \
  -o "$ARM_EXECUTABLE"
"$SWIFTC" \
  "$PATCHED_SOURCE" \
  -O \
  -sdk "$MACOS_SDK" \
  -target x86_64-apple-macos26.0 \
  -framework AppKit \
  -framework WebKit \
  -o "$INTEL_EXECUTABLE"
/usr/bin/lipo -create "$ARM_EXECUTABLE" "$INTEL_EXECUTABLE" -output "$EXECUTABLE"
chmod +x "$EXECUTABLE"

plutil -create xml1 "$PLIST"
plutil -insert CFBundleDevelopmentRegion -string zh_TW "$PLIST"
plutil -insert CFBundleDisplayName -string '股市 AI 系統' "$PLIST"
plutil -insert CFBundleExecutable -string StockAILiquidGlass "$PLIST"
plutil -insert CFBundleIdentifier -string io.github.magicianbee.stockai.liquidglass "$PLIST"
plutil -insert CFBundleIconFile -string StockAIIcon "$PLIST"
plutil -insert CFBundleInfoDictionaryVersion -string 6.0 "$PLIST"
plutil -insert CFBundleName -string StockAILiquidGlass "$PLIST"
plutil -insert CFBundlePackageType -string APPL "$PLIST"
plutil -insert CFBundleShortVersionString -string 1.0 "$PLIST"
plutil -insert CFBundleVersion -string 1 "$PLIST"
plutil -insert LSMinimumSystemVersion -string 26.0 "$PLIST"
plutil -insert NSHighResolutionCapable -bool true "$PLIST"
plutil -insert StockAIServerURL -string "$SERVER_URL" "$PLIST"

/usr/bin/xattr -cr "$APP_DIR"
codesign --force --deep --sign - "$APP_DIR" >/dev/null
rm -rf "$BUILD_DIR"

printf '%s\n' "$APP_DIR"
