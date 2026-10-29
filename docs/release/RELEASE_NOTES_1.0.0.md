# Branchglance 1.0.0

Branchglance is a compact Windows widget for keeping Codex and Claude Code chats, subagents, limit usage and waiting work in view.

## Highlights

- Follow Codex chats and subagents in a live tree. Claude Code sessions can appear in the same view after you explicitly enable local hook collection.
- Show Codex and Claude in one tree: Codex rows use blue-violet accents and Claude rows orange. Usage meters count up to 100% used.
- Let the compact pop-up surface new work or a waiting question.
- Search chats by title, agent, model, workspace or latest request, with filters such as `is:needs`, `is:running` and `is:failed`.
- See five-hour and weekly usage limits and reset times when provider data is available.
- Optionally request a short greeting after a verified limit reset.
- Install per user with Start menu and Desktop shortcuts, optionally start at sign-in, uninstall, or roll back to the previous verified release. Shortcut locations come from Windows known folders, including redirected Desktop locations.
- Setup checks for AppData redirected into packaged-app storage and refuses visibly if redirection is detected or the check is inconclusive. When the widget launcher detects redirection, it reopens through File Explorer.
- On widget reopen, the last snapshot appears immediately marked stale while a fresh snapshot loads in the background. Stale data has no live statuses or limits; a first-ever launch loads normally.

## Claude Code consent and limits

Claude Code collection is disabled until you enable it in Reset Action Settings. Enabling installs Branchglance hooks in Claude Code settings after backing up the settings file. Uninstall revokes consent first, backs up settings, removes Branchglance-owned hooks and leaves other hooks alone. If settings are unreadable, they are left untouched and the cleanup result is reported. A simultaneous Claude Code settings write can race the uninstall edit because no lock serializes those writes. Claude subagent visibility depends on reported hook events; coverage across real sessions has not been established, and this release does not claim full parity.

## Privacy

Monitoring reads local Codex session/transcript and application-state data, local Claude Code session data, and Claude Desktop's local quota cache. Consent-gated Claude hooks record allowlisted event metadata. Branchglance has no hosted account or sync service and does not upload monitored transcripts to Branchglance. Provider usage collection relies on local provider integrations. If you enable greetings, Branchglance invokes the selected provider CLI with a short greeting request after it observes a genuine reset; that request is sent to the provider.

## Install

Download `BranchglanceSetup.exe` and run it from File Explorer or the Start menu as a standard user. Do not start Setup from another app's terminal or with administrator privileges. The target install folder is `%LOCALAPPDATA%\Programs\Branchglance`; optional sign-in startup is offered during setup.

Setup does not migrate a pre-1.0 Token Tracker install.

### Verify the download

In PowerShell, compare the SHA-256 hash with the published `BranchglanceSetup.exe.sha256` asset and check the signature status is `Valid` and the signer is SignPath Foundation:

```powershell
Get-FileHash .\BranchglanceSetup.exe -Algorithm SHA256
Get-AuthenticodeSignature .\BranchglanceSetup.exe | Format-List Status, SignerCertificate
```
