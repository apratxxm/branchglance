#!/bin/sh
# Build macos/build/Branchglance.app. Run it with: open macos/build/Branchglance.app
set -e
cd "$(dirname "$0")"
ROOT="$(cd .. && pwd)"
APP=build/Branchglance.app
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS"
swiftc -O -o "$APP/Contents/MacOS/Branchglance" Sources/*.swift \
    -framework Cocoa -framework WebKit -framework SwiftUI -framework CoreAudio -framework AudioToolbox -framework IOKit
cat > "$APP/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>CFBundleIdentifier</key><string>com.branchglance.notch</string>
  <key>CFBundleName</key><string>Branchglance</string>
  <key>CFBundleExecutable</key><string>Branchglance</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleShortVersionString</key><string>0.1.0</string>
  <key>LSMinimumSystemVersion</key><string>14.0</string>
  <key>LSUIElement</key><true/>
  <key>NSAppleEventsUsageDescription</key><string>Branchglance shows and controls what Spotify or Music is playing.</string>
  <key>BranchglanceRoot</key><string>$ROOT</string>
</dict></plist>
PLIST
codesign --force --sign - "$APP"
echo "Built $APP"
