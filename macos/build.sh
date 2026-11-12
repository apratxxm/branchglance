#!/bin/sh
# Build macos/build/Branchglance.app (runs the tracker from this checkout).
# `./macos/build.sh install` instead copies the tracker and a Python environment to
# ~/Library/Application Support/Branchglance/runtime, points the app there and moves it to
# /Applications. The installed app then no longer reads the checkout, which matters when the
# checkout is in Desktop/Documents: macOS asks for folder access again after every rebuild,
# and the server stalls until someone answers.
# `rollback` puts back the app and tracker from before the last install (the Windows release
# keeps the previous release the same way). `uninstall` removes the app, its runtime and its
# Claude hooks; `uninstall --purge` also deletes Branchglance's own data and preferences.
set -e
cd "$(dirname "$0")"
REPO="$(cd .. && pwd)"
ROOT="$REPO"
SUPPORT="$HOME/Library/Application Support/Branchglance"
PREVIOUS="$SUPPORT/previous"
APPS="${BRANCHGLANCE_APPLICATIONS:-/Applications}"  # overridable for testing

# A test run (BRANCHGLANCE_APPLICATIONS set) leaves the real app alone.
quit_app() {
    [ -z "$BRANCHGLANCE_APPLICATIONS" ] || return 0
    osascript -e 'quit app id "com.branchglance.notch"' 2>/dev/null || true
    sleep 1
}
launch_app() {
    [ -z "$BRANCHGLANCE_APPLICATIONS" ] || return 0
    open "$APPS/Branchglance.app"
}

# Check the interpreter that will run the server before changing the installation.
require_python() {
    if ! "$1" -c 'import sys; sys.exit(sys.version_info < (3, 10))' 2>/dev/null; then
        echo "Error: $1 is missing, unusable, or older than Python 3.10; install Python 3.10 or newer and recreate the runtime virtual environment." >&2
        exit 1
    fi
}

if [ "$1" = "rollback" ]; then
    if [ ! -d "$PREVIOUS/Branchglance.app" ] || [ ! -d "$PREVIOUS/token_tracker" ]; then
        echo "No previous install to roll back to." >&2
        exit 1
    fi
    quit_app
    rsync -a --delete "$PREVIOUS/token_tracker/" "$SUPPORT/runtime/token_tracker/"
    rm -rf "$APPS/Branchglance.app"
    cp -R "$PREVIOUS/Branchglance.app" "$APPS/"
    launch_app
    echo "Rolled back to the previous install"
    exit 0
fi

if [ "$1" = "uninstall" ]; then
    quit_app
    # Hooks first: the consent marker goes before the settings edit, so they are inert even if
    # that edit fails. Only Branchglance's own entries are removed.
    PY="$SUPPORT/runtime/.venv/bin/python"; PYROOT="$SUPPORT/runtime"
    [ -x "$PY" ] || { PY="$REPO/.venv/bin/python"; PYROOT="$REPO"; }
    [ -x "$PY" ] || PY=python3
    PYTHONPATH="$PYROOT" "$PY" -m token_tracker.claude_hook_install uninstall >/dev/null || \
        echo "Could not remove the Claude hooks; check ~/.claude/settings.json" >&2
    rm -rf "$APPS/Branchglance.app" "$SUPPORT/runtime" "$PREVIOUS"
    if [ "$2" = "--purge" ]; then
        rm -rf "$SUPPORT" "$HOME/Library/Application Support/TokenTracker"
        # Preferences live per user, not under $HOME, so a test run must not touch them.
        [ -n "$BRANCHGLANCE_APPLICATIONS" ] || defaults delete com.branchglance.notch 2>/dev/null || true
        echo "Uninstalled Branchglance and deleted its data"
    else
        echo "Uninstalled Branchglance. Data kept in $SUPPORT; run with --purge to delete it."
    fi
    exit 0
fi

if [ "$1" = "install" ]; then
    ROOT="$SUPPORT/runtime"
    if [ -x "$ROOT/.venv/bin/python" ]; then
        require_python "$ROOT/.venv/bin/python"
    else
        require_python python3
    fi
    if [ -n "$BRANCHGLANCE_ROOT" ] && [ "$BRANCHGLANCE_ROOT" != "$ROOT" ]; then
        if [ -x "$BRANCHGLANCE_ROOT/.venv/bin/python" ]; then
            require_python "$BRANCHGLANCE_ROOT/.venv/bin/python"
        else
            require_python /usr/bin/python3
        fi
    fi
    mkdir -p "$ROOT"
    # Keep what is installed now, for `rollback`.
    if [ -d "$ROOT/token_tracker" ] && [ -d "$APPS/Branchglance.app" ]; then
        rm -rf "$PREVIOUS"
        mkdir -p "$PREVIOUS"
        cp -R "$ROOT/token_tracker" "$PREVIOUS/"
        cp -R "$APPS/Branchglance.app" "$PREVIOUS/"
    fi
    rsync -a --delete --exclude __pycache__ "$REPO/token_tracker" "$ROOT/"
    if [ ! -x "$ROOT/.venv/bin/python" ]; then
        python3 -m venv "$ROOT/.venv"
    fi
    "$ROOT/.venv/bin/python" -m pip install -q --disable-pip-version-check zstandard==0.25.0
    # bg-codex, as on Windows: add this folder to PATH, or call it by its full path.
    mkdir -p "$ROOT/bin"
    cat > "$ROOT/bin/bg-codex" <<LAUNCHER
#!/bin/sh
BG_CODEX_LAUNCHER="\$0" PYTHONPATH="$ROOT" exec "$ROOT/.venv/bin/python" -m token_tracker.bg_codex "\$@"
LAUNCHER
    chmod 755 "$ROOT/bin/bg-codex"
    # Claude Code status line helper (Branchglance.exe --statusline on Windows): set
    # "statusLine": {"type": "command", "command": "<this path>"} in ~/.claude/settings.json.
    cat > "$ROOT/bin/branchglance-statusline" <<LAUNCHER
#!/bin/sh
PYTHONPATH="$ROOT" exec "$ROOT/.venv/bin/python" -c 'from token_tracker.claude_statusline import main; raise SystemExit(main())'
LAUNCHER
    chmod 755 "$ROOT/bin/branchglance-statusline"
else
    HOST_ROOT="${BRANCHGLANCE_ROOT:-$REPO}"
    if [ -x "$HOST_ROOT/.venv/bin/python" ]; then
        require_python "$HOST_ROOT/.venv/bin/python"
    else
        require_python /usr/bin/python3
    fi
fi
# Same version as the Windows release.
VERSION="$(sed -n 's/^__version__ = "\(.*\)"/\1/p' ../token_tracker/_version.py)"
if [ -z "$VERSION" ]; then
    echo "Error: could not read __version__ from token_tracker/_version.py." >&2
    exit 1
fi
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
IDENTITY="${BRANCHGLANCE_SIGN_IDENTITY:--}"
if [ "$IDENTITY" = "-" ]; then
    # Ad hoc: runs on this Mac; other Macs need a Developer ID build (see macos/README.md).
    codesign --force --sign - "$APP"
else
    codesign --force --options runtime --timestamp \
        --entitlements Resources/Branchglance.entitlements --sign "$IDENTITY" "$APP"
    codesign --verify --strict "$APP"
    if [ -n "$BRANCHGLANCE_NOTARY_PROFILE" ]; then
        ditto -c -k --keepParent "$APP" build/Branchglance-notarize.zip
        xcrun notarytool submit build/Branchglance-notarize.zip \
            --keychain-profile "$BRANCHGLANCE_NOTARY_PROFILE" --wait
        xcrun stapler staple "$APP"
        rm -f build/Branchglance-notarize.zip
    fi
fi
echo "Built $APP"

if [ "$1" = "install" ]; then
    quit_app
    rm -rf "$APPS/Branchglance.app"
    # Move rather than copy, so Spotlight and Launchpad see a single Branchglance.
    mv "$APP" "$APPS/"
    launch_app
    echo "Installed $APPS/Branchglance.app"
fi
