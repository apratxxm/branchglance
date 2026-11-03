# Branchglance 0.1.0 alpha

This is the first public alpha. Expect rough edges.

## Highlights

- See Codex and opt-in Claude Code chats and subagents in one tree.
- Track five-hour and weekly limits, reset timers and chats waiting for input.
- Explore tokens, cost, provider share, models and usage charts in Insights.
- Enable the optional ledger for the By work view and per-provider history deletion.
- Use the optional Claude status line helper for quota readings.
- Enable reset greetings separately when you want the provider CLI to send a message.

## Install

- Windows: download `BranchglanceSetup.exe` and its `.sha256` file from this release. Verify the hash, then run the installer as a normal user.
- macOS: clone the repository and run `./macos/build.sh install`. See `macos/README.md` for requirements and signing details.
- Open Branchglance and enable Claude tracking in Settings if you want Claude collection.

## Known limitations

- Branchglance is Windows-first. macOS support is available from source.
- The usage ledger is off by default; set `TOKEN_TRACKER_LEDGER=1` before starting the widget to enable it.
- Windows alpha builds are unsigned and may trigger SmartScreen. Claude subagent coverage depends on hook events.
