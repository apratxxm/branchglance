# macOS notch app

The Swift host in `Sources/` shows the same web UI as the Windows widget, in the notch, and runs `token_tracker.macos_server` beside it. Windows behaviour is unchanged; Mac-only Python sits behind `sys.platform == "darwin"`.

## Build and install

```sh
./macos/build.sh            # build macos/build/Branchglance.app, running from this checkout
./macos/build.sh install    # install to /Applications with its own copy of the tracker
./macos/build.sh rollback   # put back the app and tracker from before the last install
./macos/build.sh uninstall  # remove the app, its runtime and its Claude hooks; keep data
./macos/build.sh uninstall --purge   # also delete Branchglance data and preferences
```

`install` also adds `branchglance-statusline` in `runtime/bin`, the macOS form of `Branchglance.exe --statusline`. To have Claude Code pass its quota to Branchglance, set it as the status line in `~/.claude/settings.json`:

```json
"statusLine": {"type": "command", "command": "\"$HOME/Library/Application Support/Branchglance/runtime/bin/branchglance-statusline\""}
```

`install` keeps the previous app and tracker in `~/Library/Application Support/Branchglance/previous` for `rollback`. It also installs `bg-codex` in `runtime/bin` (see [the integration guide](../docs/INTEGRATION.md#use-bg-codex)).

## Signing and notarization

Without a Developer ID the app is signed ad hoc, which runs on the machine that built it. To produce a build other Macs open without a warning, set:

| Variable | Value |
|---|---|
| `BRANCHGLANCE_SIGN_IDENTITY` | `Developer ID Application: Name (TEAMID)` from `security find-identity -p codesigning` |
| `BRANCHGLANCE_NOTARY_PROFILE` | a `notarytool` keychain profile, created once with `xcrun notarytool store-credentials` |

With the identity set, the build signs with the hardened runtime. With the profile also set, it submits the app to Apple, waits for the result and staples the ticket.

## Menu switches

- **Allow Codex Messages After Reset** grants what `TOKEN_TRACKER_CODEX_SEND=1` grants on Windows: Settings can then turn on the greeting and "continue" for Codex. Off by default; changing it restarts the app.
