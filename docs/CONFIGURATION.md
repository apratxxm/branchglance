# Configuration

Everything optional is off until you turn it on. Settings live in the widget (Insights → Settings); the flags below are environment variables read when the widget starts.

## Claude tracking

**Settings → Notifications → Claude tracking.** Turning it on records your consent, backs up Claude Code's `settings.json` and adds Branchglance's own hooks; your other hooks stay as they are. Hook events carry metadata only, not prompts, tool inputs or outputs, full paths or chat titles. Turning it off, or uninstalling, removes only those hooks. Coverage depends on the hook events Claude Code reports.

Hook installation (`claude_hook_install`) and transcript discovery (`providers`) prefer `CLAUDE_HOME`, then `CLAUDE_CONFIG_DIR`, then `~/.claude`. Identity (`claude_identity`), live usage (`claude_usage_api`), status-line connection and its hook preflight, and installer status-line handling use `CLAUDE_CONFIG_DIR` or `~/.claude`; they ignore `CLAUDE_HOME`. When both are set to different paths, those operations can target different configurations. Set only one, or set both to the same path.

## Claude live usage

Live usage is off by default, including for existing installations. Enable it in Settings alongside Claude tracking, or use `TOKEN_TRACKER_CLAUDE_LIVE_USAGE=1` with Claude collection granted. An explicit environment value overrides the saved setting, including `0`. When enabled, Branchglance reads Claude Code's OAuth token from `.credentials.json` in memory and asks `api.anthropic.com` for usage about every 180 seconds, with up to 15 seconds of jitter. A Claude Code usage cache younger than 180 seconds skips the poll. The account profile is fetched when binding. Branchglance never refreshes, logs or stores the token; it persists only a retry-after timestamp. The endpoint is undocumented and could break or be disallowed; Anthropic's terms restrict third-party use of subscription sign-ins. See [Privacy](PRIVACY.md).

Claude limits also use the account-bound Claude Code cache and status-line or CLI samples. Claude Desktop is not a limit source.

## Claude status line helper

Use **Connect Claude status line** in Settings to install the helper, and Disconnect to remove it. Connect refuses to replace an existing status line. To keep your own status line, configure your existing command manually to invoke the Branchglance helper as appropriate. Branchglance backs up existing settings and refuses observed concurrent edits; a write between its final comparison and replacement remains a possible race. Settings are resolved through `CLAUDE_CONFIG_DIR` or `~/.claude`.

## Usage ledger and By work

Off by default. Turn on By work in Settings to save usage locally. `TOKEN_TRACKER_LEDGER` overrides the saved setting; explicit `0` disables it. Delete a provider's saved history in the reset-action settings window.

## Reset actions and greetings

Off by default. A greeting runs the provider's own CLI to send a short message after a verified reset. Greetings recur while enabled, and for Claude may send a first greeting to establish a window; this keeps the 5-hour window rolling. A `continue` for selected Codex chats is one-shot.

- **Claude greeting** needs Claude tracking on (the setting or `TOKEN_TRACKER_CLAUDE_COLLECT=1`), and either the Claude greetings setting with the consent marker and policy file, or `TOKEN_TRACKER_CLAUDE_SEND=1`; it also needs an enabled greeting in reset settings and a signed-in Claude Code CLI. Policy changes apply at restart.
- **Codex greeting** needs `TOKEN_TRACKER_CODEX_SEND=1`, the legacy `TOKEN_TRACKER_LIMIT_RESET=1`, or a policy-file Codex send grant; the latter two grant greetings only. **`continue`** needs `TOKEN_TRACKER_CODEX_SEND=1`. Both need an enabled action in reset settings and Codex collection. On macOS the menu switch **Allow Codex Messages After Reset** sets the provider send flag for the server.

## Provider policy flags

Per provider, the collect, show and send flags below take `1`/`0` (or `true`/`false`, `on`/`off`). Defaults: Codex collects and shows; Claude does neither until granted; sending is off for both. Provider-specific explicit `0` vetoes that flag. `TOKEN_TRACKER_LIMIT_RESET=0` vetoes sending for both providers; an explicit provider send grant conflicts with it and fails closed. Show and send require collection. Invalid or conflicting policy flags fail closed. Settings → Providers shows the effective values read-only.

## Environment inventory

The inventory covers product, installer, packaging and macOS host reads. Privacy means a collection, disclosure or local data control; network means a control or credential for network activity. Other entries are paths, identity, build or operating-system inputs. Defaults shown apply when unset.

<!-- environment inventory -->
| Variable | Purpose and default | Privacy / network switch |
|---|---|---|
| `TOKEN_TRACKER_CODEX_COLLECT` | Codex collection; on by default. | Privacy |
| `TOKEN_TRACKER_CODEX_SHOW` | Codex display; on when collection is on by default. | Privacy |
| `TOKEN_TRACKER_CODEX_SEND` | Codex greeting and continue grant; off by default. | Network |
| `TOKEN_TRACKER_CLAUDE_COLLECT` | Claude collection; off by default. | Privacy |
| `TOKEN_TRACKER_CLAUDE_SHOW` | Claude display; off by default. | Privacy |
| `TOKEN_TRACKER_CLAUDE_SEND` | Claude greeting grant and feature override; off by default. | Network |
| `TOKEN_TRACKER_LIMIT_RESET` | Legacy Codex greeting grant or global send veto; unset by default. | Network |
| `TOKEN_TRACKER_CLAUDE_LIVE_USAGE` | Live usage override; saved setting or off by default, requires collection. | Network |
| `TOKEN_TRACKER_LEDGER` | Local usage ledger override; saved setting or off by default. Only exactly `1` enables it. | Privacy |
| `CLAUDE_HOME` | Hook and transcript config directory; falls back to `CLAUDE_CONFIG_DIR`, then `~/.claude`. | No |
| `CLAUDE_CONFIG_DIR` | Claude config directory; defaults to `~/.claude`, subject to the precedence above. | No |
| `CODEX_HOME` | Codex data directory; defaults to `~/.codex`. | No |
| `CLAUDE_CODE_SESSION_ID` | Associates a bg-codex receipt with its Claude session; absent means no receipt. | Privacy |
| `CLAUDE_CODE_GIT_BASH_PATH` | Preferred Git Bash path for Windows hooks; otherwise searches installed Git and PATH. | No |
| `BG_CODEX_LAUNCHER` | Identifies the bg-codex launcher for executable resolution; unset by default. | No |
| `BRANCHGLANCE_STATE_ROOT` | Source-mode state directory override; defaults to the product's local state directory, ignored in frozen builds. | No |
| `BRANCHGLANCE_POLICY_FILE` | Source-mode provider policy path; unset by default, with macOS host supplying its own path. Frozen builds use their product path. | Privacy / network policy path |
| `BRANCHGLANCE_BUILD` | Build identifier override; defaults to the embedded build ID (server fallback `dev`). | No |
| `BRANCHGLANCE_ROOT` | macOS host runtime root; defaults to the build's Info.plist root. | No |
| `BRANCHGLANCE_MACOS_HOST_SECRET` | Host-to-server proof secret, required and supplied by the macOS host. | No; authentication secret |
| `BRANCHGLANCE_SETTINGS_TOKEN` | Host-to-server settings token; absent by default and supplied by the macOS host. | No; authentication secret |
| `BRANCHGLANCE_APPLICATIONS` | macOS build/install destination and test isolation override; defaults to `/Applications`. | No |
| `BRANCHGLANCE_SIGN_IDENTITY` | macOS signing identity; defaults to ad hoc signing (`-`). | No |
| `BRANCHGLANCE_NOTARY_PROFILE` | macOS notarytool keychain profile; unset skips notarization. | Network |
| `BRANCHGLANCE_SOURCE` | Packaging spec source directory; required by build tooling. | No |
| `BRANCHGLANCE_ICON` | Packaging spec icon path; required by build tooling. | No |
| `BRANCHGLANCE_VERSION_INFO` | Packaging spec version resource path; required by build tooling. | No |
| `BRANCHGLANCE_INSTALLER_ROLE` | Installer packaging role; required (`setup` or `uninstall`). | No |
| `BRANCHGLANCE_PAYLOAD` | Installer packaging payload; unset by default. | No |
| `LOCALAPPDATA` | Windows local data root; defaults to platform home data location. | No |
| `APPDATA` | Windows roaming data root; defaults to `~/AppData/Roaming`. | No |
| `HOME` | macOS build home directory; normally supplied by the OS. | No |
| `USERPROFILE` | Installer user home; defaults to the current user's home. | No |
| `TEMP` | Installer temporary directory; defaults to `LOCALAPPDATA`. | No |
| `PATH` | Executable search path; inherited from the environment. | No |
| `SHELL` | macOS login shell used to obtain PATH; defaults to `/bin/zsh`. | No |
| `TZ` | Ledger timezone; defaults to the system local timezone. | No |
| `WINDIR` | Windows directory for the C# compiler; defaults to `C:\Windows`. | No |
| `ProgramFiles` | Git Bash search root; unset means skip that root. | No |
| `ProgramW6432` | Git Bash search root; unset means skip that root. | No |
| `ProgramFiles(x86)` | Git Bash search root; unset means skip that root. | No |
| `npm_config_user_agent` | Codex package-manager detection; unset is empty. | No |
| `npm_execpath` | Codex package-manager detection; unset is empty. | No |
<!-- end environment inventory -->

`bg-codex` nests Codex jobs under the Claude Code chat that started them; see the [integration guide](INTEGRATION.md).
