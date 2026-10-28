# Changelog

## 1.0.0

### Added

- Compact Windows widget for Codex and opt-in Claude Code chats and hook-reported subagents.
- Codex blue and Claude orange source themes, with usage meters that count up to 100% used.
- Provider source switch and compact pop-up behavior that can bring the active provider into view.
- `is:hidden` search for chats hidden from the regular list.
- Optional greeting after a verified five-hour limit reset.
- Consent-gated Claude Code hooks, local session activity, and usage summaries.
- Per-user installer, Start menu and Desktop shortcuts, optional sign-in Startup shortcut, uninstaller and previous-release rollback.
- AppData redirection checks, visible Setup refusal for redirected or inconclusive locations, and Explorer relaunch for the widget when its launcher detects redirection.
- Instant stale display of the last snapshot on widget reopen while a fresh snapshot loads in the background.
- Insights view from the widget's Insights button: 24h, 7d, 30d and all ranges, a tokens or cost metric, daily limit peaks, and cost totals that name the unpriced models they leave out.
- Settings tab in the Insights view, with a 12-hour or 24-hour clock for the compact widget.
- Codex jobs started from a Claude Code chat nest under that chat in the unified tree.

### Changed

- The collapsed widget is narrower (328 px at 100% scaling).
- Insights opens as a widget-shaped panel between the 5-hour and weekly bands. Settings shows the installed version and build.
- Codex row titles drop brief-style prefixes (`# `, `[Project]`) and a leading word that only repeats the model name.
- The app icon has more margin around the mark.
- Claude Code settings follow `CLAUDE_HOME` or `CLAUDE_CONFIG_DIR` when set, for hooks, transcripts and uninstall.
- Upgrades keep the existing start-at-sign-in choice.
- Tree rows use shared column tracks. Titles, actions, workspace and status text wrap instead of truncating.
- `/api/live` is gzip-compressed (about 387 KB to about 82 KB).
- Cold builds are faster: snapshot about 35 s to 25 s, live about 19.7 s to 14 s (measured on copied data).
- Widget idle CPU is lower (about 29% to about 2% of a core): identical DOM writes are skipped and the edge sheen animates only on hover.
- Claude Opus 5.5, Sonnet 5.5, Haiku 5.5 and Fable 5.1 are priced from exact model ids. Models without a known price are never given a guessed price.
- Live tree polls send the full link map on every poll instead of a delta.

### Fixed

- A finished earlier run of a resumed Codex thread no longer blocks the thread's current link.
- A first paint without a chat graph is held for a bounded time before the tree is drawn without it.
- Limit peak journal writes are limited to the running widget instance. Journal dates are validated strictly.
- The 24h Insights range no longer falls back to day totals. Quota peaks use fresh observations only.

### Privacy and scope

- Claude Code hook collection requires explicit user consent; hooks are not installed before enablement.
- Monitoring reads local provider data. Branchglance does not upload transcripts to a Branchglance service. An enabled greeting invokes the provider CLI with a short request.
- Claude subagent coverage depends on hook events and has not been established across real sessions.
- Uninstall revokes Claude consent first, backs up settings, removes Branchglance-owned hooks and preserves other hooks. Unreadable settings remain untouched and are reported; a concurrent Claude Code settings write can race the edit because no lock serializes writes.
- User settings and history under `%LOCALAPPDATA%\Branchglance` are kept after uninstall.
