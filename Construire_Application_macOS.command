#!/bin/bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
DIST="$ROOT/dist"
APP="$DIST/AMS Lite Companion.app"
CONTENTS="$APP/Contents"
MACOS="$CONTENTS/MacOS"
RESOURCES="$CONTENTS/Resources"
ARCHIVE="$DIST/AMS-Lite-Companion-1.3.0-macOS.zip"
BUILD="$DIST/.build"

if [ "$(uname -s)" != "Darwin" ]; then
  echo "Cette construction doit être lancée sur macOS."
  exit 1
fi

if ! command -v xcrun >/dev/null 2>&1; then
  echo "Les outils Apple sont nécessaires. Lancez : xcode-select --install"
  exit 1
fi

mkdir -p "$DIST"
rm -rf "$APP"
rm -rf "$BUILD"
mkdir -p "$MACOS" "$RESOURCES" "$BUILD"

xcrun swiftc \
  -O \
  -target arm64-apple-macosx11.0 \
  -framework AppKit \
  -framework Foundation \
  -framework WebKit \
  -o "$BUILD/AMS-Lite-Companion-arm64" \
  "$ROOT/macos/AMSCompanionLauncher.swift"

xcrun swiftc \
  -O \
  -target x86_64-apple-macosx10.15 \
  -framework AppKit \
  -framework Foundation \
  -framework WebKit \
  -o "$BUILD/AMS-Lite-Companion-x86_64" \
  "$ROOT/macos/AMSCompanionLauncher.swift"

xcrun lipo -create \
  "$BUILD/AMS-Lite-Companion-arm64" \
  "$BUILD/AMS-Lite-Companion-x86_64" \
  -output "$MACOS/AMS-Lite-Companion"

cp "$ROOT/ams_companion.py" "$RESOURCES/ams_companion.py"
cp "$ROOT/macos/Info.plist" "$CONTENTS/Info.plist"
chmod 755 "$MACOS/AMS-Lite-Companion"
chmod 644 "$RESOURCES/ams_companion.py" "$CONTENTS/Info.plist"

codesign --force --deep --sign - "$APP"
codesign --verify --deep --strict --verbose=2 "$APP"

rm -f "$ARCHIVE"
ditto -c -k --sequesterRsrc --keepParent "$APP" "$ARCHIVE"
rm -rf "$BUILD"

echo
echo "Application créée :"
echo "$APP"
echo
echo "Archive GitHub créée :"
echo "$ARCHIVE"
echo
if [ -t 1 ]; then
  open "$DIST"
fi
