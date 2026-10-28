#!/bin/sh
# Build macos/build/Branchglance.app (runs the tracker from this checkout).
# `./macos/build.sh install` instead copies the tracker and a Python environment to
# ~/Library/Application Support/Branchglance/runtime, points the app there and moves it to
# /Applications. The installed app then no longer reads the checkout, which matters when the
# checkout is in Desktop/Documents: macOS asks for folder access again after every rebuild,
# and the server stalls until someone answers.
set -e
cd "$(dirname "$0")"
REPO="$(cd .. && pwd)"
ROOT="$REPO"
if [ "$1" = "install" ]; then
    ROOT="$HOME/Library/Application Support/Branchglance/runtime"
    mkdir -p "$ROOT"
    rsync -a --delete --exclude __pycache__ "$REPO/token_tracker" "$ROOT/"
    if [ ! -x "$ROOT/.venv/bin/python" ]; then
        python3 -m venv "$ROOT/.venv"
    fi
    "$ROOT/.venv/bin/python" -m pip install -q --disable-pip-version-check zstandard==0.25.0
fi
# Same version as the Windows release.
VERSION="$(sed -n 's/^RELEASE_VERSION = "\(.*\)"/\1/p' ../packaging/build.py)"
if [ "$1" = "install" ]; then
    # Settings > Version reads this, as in the Windows release build.
    printf 'VERSION = %s\nBUILD_ID = %s\n' "'$VERSION'" "'$(git -C "$REPO" rev-parse --short HEAD)'" \
        > "$ROOT/token_tracker/_build.py"
fi
APP=build/Branchglance.app
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
cp Resources/Branchglance.icns "$APP/Contents/Resources/"
swiftc -O -o "$APP/Contents/MacOS/Branchglance" Sources/*.swift \
    -framework Cocoa -framework WebKit -framework SwiftUI -framework CoreAudio -framework AudioToolbox -framework IOKit -framework ServiceManagement -framework CoreMediaIO -framework Accelerate -framework Carbon
cat > "$APP/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>CFBundleIdentifier</key><string>com.branchglance.notch</string>
  <key>CFBundleName</key><string>Branchglance</string>
  <key>CFBundleExecutable</key><string>Branchglance</string>
  <key>CFBundleIconFile</key><string>Branchglance</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleShortVersionString</key><string>$VERSION</string>
  <key>CFBundleVersion</key><string>$VERSION</string>
  <key>LSMinimumSystemVersion</key><string>14.0</string>
  <key>LSUIElement</key><true/>
  <key>NSAudioCaptureUsageDescription</key><string>Branchglance reads the music player's audio levels to animate the notch visualizer. Nothing is recorded.</string>
  <key>NSAppleEventsUsageDescription</key><string>Branchglance shows and controls what Spotify or Music is playing.</string>
  <key>BranchglanceRoot</key><string>$ROOT</string>
</dict></plist>
PLIST
codesign --force --sign - "$APP"
echo "Built $APP"

if [ "$1" = "install" ]; then
    osascript -e 'quit app id "com.branchglance.notch"' 2>/dev/null || true
    sleep 1
    rm -rf /Applications/Branchglance.app
    # Move rather than copy, so Spotlight and Launchpad see a single Branchglance.
    mv "$APP" /Applications/
    open /Applications/Branchglance.app
    echo "Installed /Applications/Branchglance.app"
fi
