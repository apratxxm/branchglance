# Branchglance code signing policy

**Free code signing provided by SignPath.io, certificate by SignPath Foundation.**

This policy describes the release-signing controls for Branchglance. Signing is enabled only after SignPath Foundation approves the project and the repository is configured. Each signing request requires manual approval in SignPath.

## Team roles and access

- **Committer:** apratxxm
- **Reviewer:** apratxxm
- **Signing approver:** apratxxm

The same maintainer currently performs all three roles; the team will be extended as additional maintainers join. GitHub and SignPath accounts used for these roles must have multi-factor authentication enabled. API tokens are stored as GitHub Actions secrets and are never committed to this repository.

## Build and signing rules

1. Releases are built on a GitHub-hosted Windows runner from the tagged source using the checked-in build scripts and pinned Python dependencies.
2. Python and JavaScript test suites must pass before the build artifact is eligible for signing.
3. The SignPath artifact configuration signs only Branchglance executables and checks their ProductName and ProductVersion metadata. Third-party runtime files are not signed with the Branchglance certificate.
4. Every request is reviewed and manually approved by the signing approver. CI waits for completion; it does not approve requests automatically.
5. The signed release is assembled only after its signed application binaries are re-hashed into the release manifest and embedded into the final setup executable. The final setup executable is submitted for its own signing request.
6. Material changes to the build, signing workflow, team roles, or data handling are announced in the release notes and this policy is updated.

The project name in signed version resources is **Branchglance**. The ProductVersion matches the release version. The publisher displayed by Authenticode is **SignPath Foundation**.

## Privacy and data handled

Branchglance reads data locally to show the user's own activity. This includes Codex session metadata and transcripts through the locally installed Codex app-server, Claude Code session data, Claude Desktop's local usage cache, and local application settings. Claude Code hook events are collected only after the user explicitly enables collection; the hook stores event metadata locally and does not record prompt text or tool inputs and outputs.

Branchglance has no Branchglance-hosted account, analytics, or synchronization service. The desktop app does not upload transcripts or history to Branchglance. The local Codex app-server and optional provider greeting commands use the user's installed, signed-in provider tools; those tools may communicate with their respective providers under the user's account. Claude Desktop obtains its own usage data; Branchglance reads that local cache.

## Changes made to Windows and Claude settings

Setup installs per user in `%LOCALAPPDATA%\Programs\Branchglance`, creates a Windows uninstall registration, and creates Start menu and Desktop shortcuts. A Startup shortcut is created only when the user opts into starting Branchglance at sign-in. Local state is kept in `%LOCALAPPDATA%\Branchglance`.

Claude Code hook entries are added to the user's `~/.claude/settings.json` only after explicit consent. Branchglance backs up the file and edits only entries it owns. Disabling collection or uninstalling removes Branchglance-owned entries and revokes the consent marker; unrelated Claude settings are preserved.

## Uninstall

Use **Settings → Apps → Installed apps → Branchglance → Uninstall**. The uninstaller removes the owned program files, shortcuts, Startup entry, uninstall registration, and Branchglance-owned Claude hooks. User data and history under `%LOCALAPPDATA%\Branchglance` are retained, except for the Claude consent marker. The user may review and remove that retained data separately.
