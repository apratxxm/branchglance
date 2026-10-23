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

### Privacy and scope

- Claude Code hook collection requires explicit user consent; hooks are not installed before enablement.
- Monitoring reads local provider data. Branchglance does not upload transcripts to a Branchglance service. An enabled greeting invokes the provider CLI with a short request.
- Claude subagent coverage depends on hook events and has not been established across real sessions.
- Uninstall revokes Claude consent first, backs up settings, removes Branchglance-owned hooks and preserves other hooks. Unreadable settings remain untouched and are reported; a concurrent Claude Code settings write can race the edit because no lock serializes writes.
- User settings and history under `%LOCALAPPDATA%\Branchglance` are kept after uninstall.
