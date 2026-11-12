# Configuration

Everything optional is off until you turn it on. Settings live in the widget (Insights → Settings); the flags below are environment variables read when the widget starts.

## Claude tracking

**Settings → Notifications → Claude tracking.** Turning it on records your consent, backs up Claude Code's `settings.json` and adds Branchglance's own hooks; your other hooks stay as they are. Hook events carry metadata only, not prompts, tool inputs or outputs, full paths or chat titles. Turning it off, or uninstalling, removes only those hooks. Coverage depends on the hook events Claude Code reports.

For hooks and transcripts, Branchglance finds Claude Code's config through `CLAUDE_HOME`, then `CLAUDE_CONFIG_DIR`, then `~/.claude`. Live usage reads `.credentials.json` from `CLAUDE_CONFIG_DIR` or `~/.claude` only.

## Claude live usage

With Claude tracking on and Claude Code signed in, Branchglance reads Claude Code's OAuth token from `.credentials.json` in that config folder, in memory, and asks `api.anthropic.com` for your account's usage about every 30 seconds (the account profile is fetched when it first binds to an account, after a restart, and when the credentials or account change). It never refreshes, logs or stores the token; only a retry-after timestamp is saved. It does not use the macOS Keychain, so on macOS it works only when a credentials file exists; otherwise Claude limits come from the caches below.

Without it, Claude limits fall back to Claude Desktop's usage cache, the usage cache Claude Code keeps in `~/.claude.json`, and the status line helper. See [Privacy](PRIVACY.md).

## Claude status line helper

Supplies Claude Code quota readings. Add this to Claude Code's `settings.json` on Windows. It replaces any existing status line command, and the helper prints no status line of its own, so keep your existing command if you need it:

```json
"statusLine": {
  "type": "command",
  "command": "\"$LOCALAPPDATA/Programs/Branchglance/Branchglance.exe\" --statusline"
}
```

For macOS, use the command in the [macOS guide](../macos/README.md).

## Usage ledger and By work

Off by default. Set `TOKEN_TRACKER_LEDGER=1` in the widget's environment before starting it to save usage locally and enable the By work view. Delete a provider's saved history in the reset-action settings window.

## Reset actions and greetings

Off by default. A greeting runs the provider's own CLI to send a short message after a verified reset. Greetings recur while enabled, and for Claude may send a first greeting to establish a window; this keeps the 5-hour window rolling. A `continue` for selected Codex chats is one-shot.

- **Claude greeting** needs Claude tracking on, `TOKEN_TRACKER_CLAUDE_SEND=1`, an enabled greeting in reset settings, and a signed-in Claude Code CLI.
- **Codex greeting and `continue`** need `TOKEN_TRACKER_CODEX_SEND=1` and an enabled action in reset settings. On macOS the menu switch **Allow Codex Messages After Reset** does this.

## Provider policy flags

Per provider, `TOKEN_TRACKER_<CODEX|CLAUDE>_<COLLECT|SHOW|SEND>` take `1`/`0` (or `true`/`false`, `on`/`off`). Defaults: Codex collects and shows; Claude does neither until you consent; sending is off for both. An explicit `0` always wins. Settings → Providers shows the effective values read-only.

## Other

| Variable | Use |
|---|---|
| `BRANCHGLANCE_STATE_ROOT` | Keep a source checkout's state in a throwaway folder |
| `CLAUDE_HOME`, `CLAUDE_CONFIG_DIR` | Claude Code config location |

`bg-codex` nests Codex jobs under the Claude Code chat that started them; see the [integration guide](INTEGRATION.md).
