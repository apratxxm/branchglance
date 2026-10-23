# Changelog

## 1.0.0 — release candidate

### Added

- Compact Windows widget for Codex and opt-in Claude Code chats and hook-reported subagents.
- Codex blue and Claude orange source themes, with usage meters that count up to 100% used.
- Provider source switch and compact pop-up behavior that can bring the active provider into view.
- `is:hidden` search for chats hidden from the regular list.
- Optional greeting after a verified five-hour limit reset.
- Consent-gated Claude Code hooks, local session activity, and usage summaries.
- Per-user installer, Start menu entry, optional sign-in startup, uninstaller and previous-release rollback.

### Privacy and scope

- Claude Code hook collection requires explicit user consent; hooks are not installed before enablement.
- Monitoring reads local provider data. Branchglance does not upload transcripts to a Branchglance service. An enabled greeting invokes the provider CLI with a short request.
- Claude subagent coverage depends on hook events and has not been established across real sessions.

### Release blockers

- Current installer source does not create the requested Desktop shortcut.
- Current installer source has no refusal guard for Setup launched from Claude Desktop.
- The uninstaller does not revoke Claude Code hooks or consent; disable Claude collection before uninstalling.
- The private-material scan found machine-specific absolute paths in committed review/test material. See `docs/release/RELEASE_CHECKLIST.md`.
