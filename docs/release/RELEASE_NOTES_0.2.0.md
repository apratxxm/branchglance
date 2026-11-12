# Branchglance 0.2.0 (alpha)

Live Claude limits, banked Codex resets and a more trustworthy agent tree.

## Highlights

- **Live Claude usage.** With Claude tracking on and Claude Code signed in, Branchglance reads your 5-hour and weekly usage from Anthropic about every 30 seconds, using Claude Code's existing sign-in. Without it, limits still come from the local caches.
- **Banked Codex resets** appear beside the weekly bar.
- **Claude greetings** (opt-in) keep the 5-hour window rolling.
- **A tree you can trust.** Parent links need proof, large trees page instead of overflowing, and stopped chats stay visible for 15 minutes.
- **Account-aware limits.** Claude readings are tied to the signed-in account, so switching accounts never shows the old account's numbers.
- **Safer installer.** Deletion goes through verified handles and fails closed when a folder can't be proven to be Branchglance's own.

Full list: [CHANGELOG](https://github.com/apratxxm/branchglance/blob/master/CHANGELOG.md).

## Install

- **Windows:** download `BranchglanceSetup.exe` and its `.sha256` file from this release, verify the hash, then run the installer as a normal user. Upgrading from 0.1.0 keeps your settings.
- **macOS:** clone the repository and run `./macos/build.sh install`. See the [macOS guide](https://github.com/apratxxm/branchglance/blob/master/macos/README.md).

## Known limitations

- Windows is the primary platform. macOS is available from source only, and does not use the Keychain, so live Claude usage there works only if Claude Code keeps a credentials file; otherwise Claude limits come from local caches.
- Claude subagent coverage depends on the hook events Claude Code reports.
- The usage ledger is off by default (`TOKEN_TRACKER_LEDGER=1` enables it).

## Privacy

Branchglance reads local Codex and Claude Code data and serves its UI over a local loopback HTTP server and has no Branchglance-hosted account, analytics or sync service. With Claude tracking and live usage enabled, it reads Claude Code's OAuth token in memory and asks `api.anthropic.com` for your usage; it never refreshes, logs or stores the token. Greetings and queued Codex messages are off by default and, when enabled, are sent through the provider's own CLI. Details: [Privacy](https://github.com/apratxxm/branchglance/blob/master/docs/PRIVACY.md).
