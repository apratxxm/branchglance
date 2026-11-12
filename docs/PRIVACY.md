# Privacy

Branchglance runs on your machine. There is no Branchglance account, server, sync service or telemetry, and nothing it reads is sent to its maintainers.

## What it reads

| Source | When |
|---|---|
| Codex session data and the local Codex app server (chats, subagents, limits) | Always, if Codex is installed |
| Claude Code transcripts and Branchglance's hook events | Only after you turn on Claude tracking |
| Claude Desktop's local usage cache, and the usage cache Claude Code keeps in `~/.claude.json` | Only after you turn on Claude tracking |
| Claude Code's OAuth token in `.credentials.json` (see below) | Only after you turn on Claude tracking, and only while Claude Code is signed in |

Transcripts are read for display (titles, status) and are not copied or uploaded. Hook events carry allowlisted metadata only: identifiers, model and tool categories, state, and the workspace drive and final folder name. They never include prompts, tool inputs or outputs, full paths or chat titles.

## What it sends

Exactly two things can leave your machine, and both go to the provider you already use:

1. **Claude live usage.** With Claude tracking on and Claude Code signed in, Branchglance reads the OAuth token from `.credentials.json` in Claude Code's config folder (`CLAUDE_HOME`, `CLAUDE_CONFIG_DIR` or `~/.claude`) into memory and sends it to `api.anthropic.com` to fetch the account's usage (about every 30 seconds) and profile (once per account). It never refreshes, logs or stores the token. The only value it persists is a retry-after timestamp. It does not use the macOS Keychain, so on macOS it falls back to the local caches. If Claude Code isn't signed in, nothing is sent.
2. **Reset greetings.** Off by default. If you enable one, Branchglance runs the provider's own CLI to send a short message after a verified limit reset. That request goes to the provider. For Claude, Branchglance reads the limits Claude Code prints for the greeting; it does not run `claude auth status`.

## What it stores

Settings, the last snapshot and (if you enable it) the usage ledger live under `%LOCALAPPDATA%\Branchglance` on Windows, or `~/Library/Application Support/Branchglance` on macOS. The uninstaller keeps them; delete the folder to remove them. A provider's saved ledger history can be deleted in the reset-action settings.

Claude tracking changes Claude Code's `settings.json` by adding hooks. Branchglance backs the file up first and removes only its own hooks when you turn tracking off or uninstall.

## Controls

Claude tracking, the usage ledger and greetings are all off until you enable them, and each can be turned off again. See [Configuration](CONFIGURATION.md). Security reports: [SECURITY.md](../SECURITY.md).
