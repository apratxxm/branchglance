# Privacy

Branchglance runs on your machine. There is no Branchglance account, server, sync service or telemetry, and nothing it reads is sent to its maintainers.

## What it reads

| Source | When |
|---|---|
| Codex session data and the local Codex app server (chats, subagents, limits) | Always, if Codex is installed |
| Claude Code transcripts and Branchglance's hook events | Only after you turn on Claude tracking |
| The usage cache Claude Code keeps in `~/.claude.json` | Only after you turn on Claude tracking |
| Claude Code's OAuth token in `.credentials.json` (see below) | Only when both Claude tracking and live usage are enabled, and only while Claude Code is signed in |

Complete transcript files are never copied or uploaded. Branchglance does keep a snapshot cache of what it displays, which can include chat titles and short prompt or update excerpts, even without the ledger (see [What it stores](#what-it-stores)). Hook events carry allowlisted metadata only: identifiers, model and tool categories, state, and the workspace drive and final folder name. They never include prompts, tool inputs or outputs, full paths or chat titles.

## What it sends

Only three things can leave your machine, and all go to a provider you already use:

1. **Claude live usage.** With Claude tracking and live usage enabled and Claude Code signed in, Branchglance reads the OAuth token from `.credentials.json` in Claude Code's config folder (`CLAUDE_CONFIG_DIR`, otherwise `~/.claude`) into memory and sends it to `api.anthropic.com` to fetch the account's usage (about every 180 seconds, with up to 15 seconds of jitter; a fresh Claude Code cache skips a poll) and profile (when it first binds to an account, after a restart, and when the credentials or account change). It never refreshes, logs or stores the token. The only value it persists is a retry-after timestamp. It does not use the macOS Keychain; on macOS it works only if a credentials file exists, and otherwise falls back to the local caches. If Claude Code isn't signed in, nothing is sent.
2. **Reset greetings.** Off by default. If you enable one, Branchglance runs the provider's own CLI to send a short message after a verified limit reset, and repeats it while enabled to keep the window rolling; for Claude it may also send a first greeting to establish a window. That request goes to the provider. For Claude, Branchglance reads the limits Claude Code prints for the greeting; it does not run `claude auth status`.
3. **Queued Codex messages.** Off until you enable Codex sending (`TOKEN_TRACKER_CODEX_SEND=1`). Then a choice you queue on a Codex question, or the `continue` you arm for chosen chats after a reset, is sent to that chat through the local Codex CLI.

## What it stores

Settings, the last snapshot and (if you enable it) the usage ledger live under `%LOCALAPPDATA%\Branchglance` on Windows. On macOS, the app and settings are in `~/Library/Application Support/Branchglance` and saved state in `~/Library/Application Support/TokenTracker`. The uninstaller keeps them; on Windows delete the folder, and on macOS run `./macos/build.sh uninstall --purge`, to remove them. A provider's saved ledger history can be deleted in the reset-action settings.

Claude tracking changes Claude Code's `settings.json` by adding hooks. Connecting the status line is a separate explicit action and is refused if a status line already exists. Branchglance backs up existing settings before editing and removes only its own hooks and status line. It refuses observed concurrent edits; a write between its final comparison and replacement remains a possible race.

## Controls

Claude tracking, live usage, the usage ledger and greetings are all off until you enable them, and each can be turned off again. See [Configuration](CONFIGURATION.md). Security reports: [SECURITY.md](../SECURITY.md).
