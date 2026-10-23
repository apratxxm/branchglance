# Branchglance 1.0.0

Branchglance is a compact Windows widget for keeping Codex and Claude Code chats, subagents, limit usage and waiting work in view.

## Highlights

- Follow Codex chats and subagents in a live tree. Claude Code sessions can appear in the same view after you explicitly enable local hook collection.
- Switch between Codex's blue theme and Claude's orange theme. Usage meters count up to 100% used.
- Let the compact pop-up bring a provider with new work into view, or switch sources manually.
- Search chats, including hidden ones with `is:hidden`.
- See five-hour and weekly usage limits and reset times when provider data is available.
- Optionally request a short greeting after a verified limit reset.
- Install per user, open from the Start menu, optionally start at sign-in, uninstall, or roll back to the previous verified release.

## Claude Code consent and limits

Claude Code collection is disabled until you enable it in Reset Action Settings. Enabling installs Branchglance hooks in Claude Code settings after backing up the settings file. Disable collection there to revoke consent and remove Branchglance-owned hooks. Claude subagent visibility depends on reported hook events; coverage across real sessions has not been established, and this release does not claim full parity.

## Privacy

Monitoring reads local Codex session/transcript and application-state data, local Claude Code session data, and Claude Desktop's local quota cache. Consent-gated Claude hooks record allowlisted event metadata. Branchglance has no hosted account or sync service and does not upload monitored transcripts to Branchglance. Provider usage collection relies on local provider integrations. If you enable greetings, Branchglance invokes the selected provider CLI with a short greeting request after it observes a genuine reset; that request is sent to the provider.

## Install

Download `BranchglanceSetup.exe` and run it from File Explorer or the Start menu as a standard user. Do not start Setup from another app's terminal or with administrator privileges. The target install folder is `%LOCALAPPDATA%\Programs\Branchglance`; optional sign-in startup is offered during setup.

> Publication gate: this release note is a draft. The release checklist records outstanding shortcut, launch-context and privacy-scan blockers. Do not publish this release until they are resolved and all manual checks pass.
