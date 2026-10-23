# Branchglance

Branchglance is a compact Windows widget for keeping Codex and Claude Code chats, subagents, usage limits and waiting work in view.

The Codex source uses blue accents; Claude uses orange. Limit meters count upward to 100% used. In compact mode, new activity can bring the relevant provider into view; use the Codex | Claude switch to change sources yourself. A pop-up can surface new work or a waiting question. Search hidden chats with `is:hidden`; hiding a chat does not delete it.

Claude Code collection is off until you enable it in Reset Action Settings. It uses local hooks and Claude session data. Subagent rows reflect the hook events received; completeness across real sessions has not been established.

## Screenshots

No screenshots are included yet. Add reviewed, privacy-safe images at these paths before publishing; do not use real account data or private chat titles.

| Preview | Placeholder |
| --- | --- |
| Widget overview | `docs/release/screenshots/widget-overview.png` — pending |
| Codex limits and chat tree | `docs/release/screenshots/codex-view.png` — pending |
| Claude source and usage | `docs/release/screenshots/claude-view.png` — pending |
| Claude consent setting | `docs/release/screenshots/claude-consent.png` — pending |

## Install

1. Download `BranchglanceSetup.exe` from the 1.0.0 release assets.
2. Run Setup from File Explorer or the Start menu, not from another app's terminal. Run it as your standard Windows user; do not choose **Run as administrator**.
3. Follow the prompts. Automatic sign-in startup is optional and off unless you choose it.

Branchglance requires 64-bit Windows 10 build 19041 or newer, .NET Framework 4.8 or newer, and Microsoft Edge WebView2 Runtime. Setup checks these prerequisites and stops with an explanation when one is missing.

The per-user install location is `%LOCALAPPDATA%\Programs\Branchglance`. Setup creates a Start menu shortcut and an optional sign-in startup shortcut. **This source does not create a Desktop shortcut; the requested Desktop shortcut is a release blocker recorded in the checklist.**

## Open and use

- Open Branchglance from the Start menu. The widget stays in the Windows desktop session.
- Use the Codex | Claude switch when both providers are available. New provider activity can bring that provider into view; a manual selection takes priority briefly.
- Limit meters show used percentage and stop at 100%. Reset times are observations; reaching zero on a countdown alone does not confirm a reset.
- Open search and enter `is:hidden` to find chats you previously hid. Clear the query to return to the normal list.
- Open **Reset Action Settings** from the widget's quota status pop-up to configure an optional greeting after a verified limit reset or to enable Claude Code collection. Greetings are off by default. Enabling Claude collection adds Branchglance hooks to Claude Code settings after backing up the settings file; disabling removes Branchglance-owned hooks and revokes consent.

## Uninstall and rollback

To remove the app, open **Settings → Apps → Installed apps**, select **Branchglance**, and choose **Uninstall**. The uninstaller removes the owned install and integrations. It keeps Branchglance settings and history under `%LOCALAPPDATA%\Branchglance`.

If Claude Code collection is enabled, disable it in Reset Action Settings before uninstalling. Claude Code hook entries live in Claude's user settings outside the app install; the uninstaller does not remove them. Disabling collection first revokes consent and removes Branchglance-owned hooks while leaving other hook entries alone.

The setup program has a rollback mode that restores the previous verified release. Release reviewers should test `BranchglanceSetup.exe --rollback` using a reviewed Windows shortcut or other release-approved launch method. Do not launch Setup from another application's terminal.

## Privacy and provider access

Branchglance reads local Codex session/transcript and application-state data. For Claude, it reads local Claude Code session data and, only after explicit consent, allowlisted hook events. Hook records contain event and session identifiers, model/tool categories and limited state metadata; they do not contain prompt text, tool inputs or outputs, full paths or chat titles. Chat titles are read from local transcripts for display and are not stored in hook event files. Claude quota limits are read from Claude Desktop's local usage cache.

Branchglance has no Branchglance-hosted account or sync service, and monitoring does not upload chat transcripts to Branchglance. Provider usage is read through the local Codex integration and Claude Desktop cache. These integrations rely on the provider applications and their existing account connections.

The optional greeting is different: when explicitly enabled and a genuine reset is observed, Branchglance runs the installed Codex or Claude CLI with a short greeting request. That request goes to the selected provider through its CLI. Leave greetings off if you do not want that provider request. A greeting is not an allowance reset and does not prove work completed.

Runtime settings, hook events and history remain in the per-user application data folder. Uninstall keeps that folder. Review it separately if you want to remove local history after uninstalling.

## Troubleshooting

- **Setup says it cannot continue:** confirm 64-bit Windows 10 build 19041+, .NET Framework 4.8+, and WebView2 Runtime are installed. Run as a standard user, not as administrator.
- **Setup refuses an existing folder or shortcut:** it will not overwrite files it cannot identify as Branchglance-owned. Preserve the folder and contact the release maintainer; do not delete files to force an install.
- **Setup ran but Branchglance is not in the Start menu:** check whether Windows or endpoint protection blocked Setup, then rerun Setup from File Explorer. Do not launch it from another app's terminal.
- **Claude is unavailable:** open Reset Action Settings, enable Claude Code collection, and restart Branchglance if it asks. Check that Claude Code hooks are allowed by Claude Code settings. Only events Claude Code reports can appear; missing events do not prove a session is idle.
- **Uninstalling with Claude hooks enabled:** cancel uninstall, open Reset Action Settings, disable Claude Code collection, then uninstall. The uninstaller does not remove hooks stored in Claude's settings.
- **A hidden chat is missing:** search for `is:hidden`. Hiding is a local display choice, not deletion.
- **No greeting appeared at countdown zero:** the app waits for a verified reset sample; zero on the timer is not enough. Confirm the correct provider's greeting setting is enabled and that its CLI is signed in.

## Release readiness note

This README describes intended 1.0.0 behavior. The current `docs` branch has two installer acceptance gaps: it does not create a Desktop shortcut, and source inspection found no refusal guard for Setup launched from Claude Desktop. The committed-tree privacy scan also found machine-specific review/test material. See [the release checklist](docs/release/RELEASE_CHECKLIST.md); do not publish until its blockers are cleared and the screenshots are reviewed.

Branchglance is licensed under MIT. See [LICENSE](LICENSE). Bundled fonts (Inter, Instrument Sans, Geist, Red Hat Mono) keep their SIL Open Font License 1.1 notices in `token_tracker/static/fonts/*/OFL.txt`.
