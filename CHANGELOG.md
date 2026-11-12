# Changelog

All notable changes to Branchglance are recorded here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html). Before 1.0, a minor version adds features or changes behaviour and a patch version only fixes bugs.

## [Unreleased]

Planned as 0.2.0.

### Added

- Live Claude usage. With Claude tracking on and Claude Code signed in, Branchglance reads the account's 5-hour and weekly usage from Anthropic about every 30 seconds, using Claude Code's existing sign-in. It never refreshes or stores the token, honours Anthropic's retry-after across restarts, and only trusts readings for the account that is currently signed in.
- Claude limits also refresh from the usage cache Claude Code keeps in its own config, so they stay current without Claude Desktop or the status line helper. Readings are used only when they match the signed-in account.
- Banked Codex limit resets are shown beside the weekly bar; ones that expire while the widget is open are dropped.
- Opt-in Claude greetings keep the 5-hour window rolling. Branchglance reads the limits Claude Code prints for each greeting.
- Large trees scroll within the expanded window, and a stopped chat stays visible for 15 minutes.

### Changed

- A Codex job nests under a Claude chat only when its parent is proven by a receipt or verified process lineage. Unverifiable candidates now stay unresolved instead of being guessed.
- Subagent rows keep their parent's timing and workspace columns.
- Claude readings are tied to the account they came from. Switching accounts no longer shows the old account's limits, and a reset armed on one account is not carried to another.
- Privacy text is exact: Branchglance does read Claude Code's OAuth token, in memory, for live usage. See [Privacy](docs/PRIVACY.md).

### Fixed

- With several agents running, the expanded tree no longer flips between pages. It pages only when the rows cannot fit at the window's maximum height, and keeps the same page as the window resizes.
- Claude incremental reads are no longer treated as rewrites on Python 3.12+ on Windows.
- Live Claude usage requests honour the TLS context and have a whole-request deadline, and no longer fail when the response socket path is absent.
- Shutdown stops background services in parallel under a bounded budget.
- Quiet upgrades wait up to 60 seconds for a slow old instance (the quiet budget is now 90 seconds) and always give the relaunch its own health window.
- Claude limits no longer go blank while a snapshot is being published.
- Reset actions are more reliable: saves survive restarts, queued batches are reserved so they are not duplicated, and a failure before a message starts is told apart from one whose outcome is unknown.
- A slow background snapshot can no longer overwrite newer data or limit peaks after a source change.
- A run whose Claude parent is gone is no longer retained again, and an exec row keeps its status when it cannot be linked.
- Limit-reset detection handles stale baselines and candidate readings more strictly.
- Setup and uninstall are safer on unusual folders and names: Unicode paths are handled byte-exactly and a folder is only removed once it is proven to be Branchglance's own.

### Security

- The installer deletes files through verified handles, and recovery fails closed when a folder's identity can't be proven.
- The launcher authenticates widget relaunch requests within one install, and release builds ship without the test report.
- The local avatar and limit-reset endpoints check the request origin.
- Codex processes Branchglance starts are contained from the moment they are created, and retention cleanup on macOS no longer follows links.

## [0.1.0] - 2026-11-03

First public alpha.

### Added

- Compact Windows widget showing Codex and opt-in Claude Code chats and hook-reported subagents in one tree, with 5-hour and weekly usage meters and reset countdowns.
- Codex jobs started from a Claude Code chat nest under that chat; `bg-codex` is a drop-in `codex` that does this for you.
- Needs-you detection with a toolbar count, search and `is:needs`, `is:running` and `is:failed` filters.
- Insights view: 24h, 7d, 30d and all ranges, tokens or cost, provider shares, plan multiples, model table, hourly and daily charts, and limit peaks. Cost names the unpriced models it leaves out.
- Settings tab, including a 12- or 24-hour clock.
- macOS notch app built from source, with Insights, the tree, menu bar controls, `bg-codex`, rollback and uninstall.
- Optional Claude Code status line helper for quota display.
- Opt-in local usage ledger (`TOKEN_TRACKER_LEDGER=1`) with a By work view and per-provider history deletion.
- Opt-in reset greetings after a verified 5-hour reset, with explicit send permission and a signed-in provider CLI.
- Per-user installer with Start menu and Desktop shortcuts, an optional sign-in shortcut, an uninstaller and previous-release rollback.

### Changed

- Usage bars use colour for usage left; the clock and timers are green.
- The collapsed widget is narrower (328 px at 100% scaling).
- The live tree starts sooner and fills in older chat details in the background; reopening the widget shows the last snapshot instantly.
- `/api/live` is gzip-compressed (about 387 KB to 82 KB) and idle CPU dropped from about 29% to about 2% of a core.
- Codex row titles drop brief-style prefixes and a leading word that only repeats the model name.
- Subagents beyond three fold behind a +N more control.
- Claude Code settings follow `CLAUDE_HOME` or `CLAUDE_CONFIG_DIR` when set.
- Upgrades keep the existing start-at-sign-in choice.
- Claude Opus 5.5, Sonnet 5.5, Haiku 5.5 and Fable 5.1 are priced from exact model ids; models without a known price are never given a guessed one.

### Fixed

- Codex subagent usage is attributed to the correct work in the ledger.
- 24-hour Insights labels follow real local hours across daylight-saving changes, including half-hour zones.
- Claude subagents keep their parent in the live view between snapshots.
- Right after launch, limit bars show each window's last unexpired reading, marked last-known, instead of staying blank.
- On macOS, limit peaks are kept, `bg-codex` receipts are read and Claude hook events identify the Claude process.
- A finished earlier run of a resumed Codex thread no longer blocks the thread's current link.
- Choosing "No" for Startup removes legacy shortcuts.

### Security

- Claude Code hooks are installed only after you consent, and uninstall revokes consent first, backs up settings, removes only Branchglance's hooks and keeps the rest. Unreadable settings are left untouched and reported.
- Setup refuses redirected or inconclusive AppData locations, and uninstall verifies the install before changing anything.
- Branchglance does not upload transcripts to a Branchglance service; an enabled greeting sends a short request through the provider's own CLI.
- Known limitation: a concurrent Claude Code settings write can race the hook edit, because nothing serializes those writes.

[Unreleased]: https://github.com/apratxxm/branchglance/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/apratxxm/branchglance/releases/tag/v0.1.0
