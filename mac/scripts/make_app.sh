#!/usr/bin/env bash
# Build "Air Quality.app" (universal: Apple Silicon + Intel) and zip it into mac/dist/.
set -euo pipefail
cd "$(dirname "$0")/.."

ARCHS=(--arch arm64 --arch x86_64)
swift build -c release "${ARCHS[@]}"
BIN="$(swift build -c release "${ARCHS[@]}" --show-bin-path)/AirQuality"

APP="dist/Air Quality.app"
rm -rf dist
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
cp "$BIN" "$APP/Contents/MacOS/AirQuality"

# App icon from the repo's icon.png
if [ -f ../icon.png ]; then
  ICONSET=dist/AppIcon.iconset
  mkdir -p "$ICONSET"
  for s in 16 32 128 256 512; do
    sips -z $s $s ../icon.png --out "$ICONSET/icon_${s}x${s}.png" >/dev/null
    sips -z $((s * 2)) $((s * 2)) ../icon.png --out "$ICONSET/icon_${s}x${s}@2x.png" >/dev/null
  done
  iconutil -c icns "$ICONSET" -o "$APP/Contents/Resources/AppIcon.icns"
  rm -rf "$ICONSET"
fi

VERSION="${VERSION:-1.0}"
cat > "$APP/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleName</key><string>Air Quality</string>
  <key>CFBundleDisplayName</key><string>Air Quality</string>
  <key>CFBundleIdentifier</key><string>io.github.air-quality-widget</string>
  <key>CFBundleExecutable</key><string>AirQuality</string>
  <key>CFBundleIconFile</key><string>AppIcon</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleShortVersionString</key><string>${VERSION}</string>
  <key>CFBundleVersion</key><string>${VERSION}</string>
  <key>LSMinimumSystemVersion</key><string>13.0</string>
  <key>LSUIElement</key><true/>
  <key>NSHighResolutionCapable</key><true/>
</dict>
</plist>
PLIST

# Ad-hoc signature so it launches on Apple Silicon (not notarized).
codesign --force --deep --sign - "$APP"
codesign --verify --verbose "$APP"

(cd dist && ditto -c -k --keepParent "Air Quality.app" AirQuality-mac.zip)
echo "Built mac/dist/AirQuality-mac.zip"
