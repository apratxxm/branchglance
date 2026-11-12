# Configuration

Everything optional is off until you turn it on. Settings live in the widget (Insights → Settings); the flags below are environment variables read when the widget starts.

## Claude tracking

**Settings → Notifications → Claude tracking.** Turning it on records your consent, backs up Claude Code's `settings.json` and adds Branchglance's own hooks; your other hooks stay as they are. Hook events carry metadata only, not prompts, tool inputs or outputs, full paths or chat titles. Turning it off, or uninstalling, removes only those hooks. Coverage depends on the hook events Claude Code reports.

For hooks and transcripts, Branchglance finds Claude Code's config through `CLAUDE_HOME`, then `CLAUDE_CONFIG_DIR`, then `~/.claude`. Live usage reads `.credentials.json` from `CLAUDE_CONFIG_DIR` or `~/.claude` only.

## Claude live usage

Live usage is off by default, including for existing installations. Enable it in Settings alongside Claude tracking, or use `TOKEN_TRACKER_CLAUDE_LIVE_USAGE=1`. An explicit environment value overrides the saved setting, including `0`. When enabled, Branchglance reads Claude Code's OAuth token from `.credentials.json` in memory and asks `api.anthropic.com` for usage about every 180 seconds, with 15 seconds of jitter. A Claude Code usage cache younger than 180 seconds skips the poll. The account profile is fetched when binding. Branchglance never refreshes, logs or stores the token; it persists only a retry-after timestamp. The endpoint is undocumented and could break or be disallowed; Anthropic's terms restrict third-party use of subscription sign-ins. See [Privacy](PRIVACY.md).

Claude limits also use the account-bound Claude Code cache and status-line or CLI samples. Claude Desktop is not a limit source.

## Claude status line helper

Use **Connect Claude status line** in Settings to install the helper, and Disconnect to remove it. Connect refuses to replace an existing status line. To keep your own status line, configure your existing command manually to invoke the Branchglance helper as appropriate. Branchglance backs up existing settings and refuses observed concurrent edits; a write between its final comparison and replacement remains a possible race. Settings are resolved through `CLAUDE_CONFIG_DIR` or `~/.claude`.

## Usage ledger and By work

Off by default. Turn on By work in Settings to save usage locally. `TOKEN_TRACKER_LEDGER` overrides the saved setting; explicit `0` disables it. Delete a provider's saved history in the reset-action settings window.

## Reset actions and greetings

Off by default. A greeting runs the provider's own CLI to send a short message after a verified reset. Greetings recur while enabled, and for Claude may send a first greeting to establish a window; this keeps the 5-hour window rolling. A `continue` for selected Codex chats is one-shot.

- **Claude greeting** needs Claude tracking on, the Claude greetings setting (or `TOKEN_TRACKER_CLAUDE_SEND=1`), a restart after changing it, an enabled greeting in reset settings, and a signed-in Claude Code CLI.
- **Codex greeting and `continue`** need `TOKEN_TRACKER_CODEX_SEND=1` and an enabled action in reset settings. On macOS the menu switch **Allow Codex Messages After Reset** does this.

## Provider policy flags

Per provider, `TOKEN_TRACKER_<CODEX|CLAUDE>_<COLLECT|SHOW|SEND>` take `1`/`0` (or `true`/`false`, `on`/`off`). Defaults: Codex collects and shows; Claude does neither until you consent; sending is off for both. An explicit `0` always wins. Settings → Providers shows the effective values read-only.

## Other

| Variable | Use |
|---|---|
| `BRANCHGLANCE_STATE_ROOT` | Keep a source checkout's state in a throwaway folder |
| `CLAUDE_HOME`, `CLAUDE_CONFIG_DIR` | Claude Code config location |

`bg-codex` nests Codex jobs under the Claude Code chat that started them; see the [integration guide](INTEGRATION.md).
