# Branchglance 1.0.0 release checklist

Manual end-to-end plan for a disposable Windows x64 VM and synthetic/test accounts. Record Windows build, WebView2 and .NET versions, Setup file hash, account/provider versions, results and evidence. Never capture private chat titles, prompt text, tokens, account identifiers or full paths in screenshots or logs.

## Test record

Use a clean, disposable Windows 10 build 19041+ or Windows 11 x64 VM. Confirm .NET Framework 4.8+ and WebView2 Runtime. Use a non-administrator account. Start from a VM snapshot and restore it after each provider scenario. For quota and greeting tests, use approved test accounts and stop if a real limit cannot be safely observed; do not force quota exhaustion or send a greeting without explicit test-owner authorization.

Record each item as **Pass**, **Fail** or **Not run**, with a short result and evidence reference. No row is pre-passed by this draft.

### Clean install

- [ ] Download the candidate Setup package from the private release draft and verify its recorded hash against the build manifest.
- [ ] Launch Setup by opening it in File Explorer. Confirm the preflight accepts the clean VM and does not request administrator elevation.
- [ ] Complete installation. Confirm files are under `%LOCALAPPDATA%\Programs\Branchglance` and the Start menu shortcut opens the widget.
- [ ] Confirm Setup offers optional sign-in startup with the intended default. Select **No** on one clean snapshot; verify no Startup shortcut is registered.
- [ ] Verify Setup creates Start menu and Desktop shortcuts at Windows-known-folder locations, plus an optional Startup shortcut when selected. Confirm the Desktop link follows a OneDrive-relocated Desktop and all shortcuts show the Live Stack icon.
- [ ] Confirm no user data or provider settings are changed by installation alone. Claude Code hooks must remain absent and consent disabled.

### Upgrade from release candidate

- [ ] Install the approved RC in a separate snapshot. Add synthetic local settings/history and record current version and integrations.
- [ ] Run 1.0 Setup from File Explorer over the Branchglance-owned RC install. Confirm it recognizes the owned location and preserves settings/history.
- [ ] Confirm the active release changes to 1.0.0 only after staging/self-check succeeds; interrupt at each approved failure point if the build provides a documented test hook, then verify the previous release remains launchable.
- [ ] Verify Start menu and Desktop shortcuts, optional sign-in Startup shortcut and uninstall registration after upgrade. Confirm locations are resolved from Windows known folders and shortcuts use the Live Stack icon.

### Startup at sign-in

- [ ] From a fresh snapshot, install and choose **Yes** to automatic sign-in startup.
- [ ] Sign out and back in as the same standard user. Confirm one Branchglance instance opens in the background and the widget is available.
- [ ] Confirm a second sign-in does not create duplicate instances. Record whether a startup task or shortcut was used.
- [ ] Reopen the widget after it has a prior snapshot. Confirm it displays that snapshot immediately with a stale marker, without live statuses or limits, then refreshes in the background. Confirm a first-ever launch loads normally.

### Codex limits, tree and pop-up

- [ ] With Codex signed in, confirm five-hour and weekly values appear when fresh data is available. Verify both meters count **used** upward and cap at 100%; confirm unavailable/stale values are not presented as current.
- [ ] Confirm the Codex source uses the blue theme and the provider switch is available when both providers are enabled.
- [ ] Create a synthetic root chat with multiple child agents. Verify parent/child relationship, observed statuses, activity and elapsed time.
- [ ] Trigger a new-work or waiting-question event in the synthetic/test session. Confirm the compact pop-up surfaces and follows Codex when eligible; then manually select Claude and confirm that recent manual selection is respected.
- [ ] Hide one synthetic chat, confirm it leaves the normal list, search `is:hidden`, confirm the hidden chat can be found, then clear search and restore the test state.

### Claude limits, consent and tree

- [ ] Before consent, inspect Claude Code settings and local Branchglance state. Confirm Branchglance hooks are absent, no Claude hook events are collected, and Claude rows are not shown.
- [ ] Open Reset Action Settings and enable Claude Code collection. Confirm the settings backup exists, only Branchglance-owned hook entries are added, and consent status becomes enabled.
- [ ] Restart if prompted. In a synthetic/test Claude Code session, confirm the provider switch, orange theme, chat row and reported hook activity appear.
- [ ] Verify Claude Desktop's locally cached five-hour and weekly usage values and used-percent meters. Confirm stale/missing data remains unknown rather than becoming a fabricated current value.
- [ ] Trigger a synthetic hook-reported child agent if available. Record observed coverage; do not infer that all real Claude subagents are captured.
- [ ] Disable Claude collection. Confirm consent is revoked and only Branchglance-owned hook entries are removed; leave unrelated settings untouched.

### Search and active-provider behavior

- [ ] With both sources enabled, trigger new work and a waiting question in the currently unselected provider. Verify the source indicator marks attention and compact view follows the provider when no manual hold/search/popover interaction prevents it.
- [ ] Manually switch sources and verify theme, limit display and chat tree update together.
- [ ] Verify `is:hidden` finds hidden roots and children, and that ordinary search does not silently unhide them.

### Optional greeting

- [ ] Confirm greeting is off by default for both providers and no request is sent on install or when a countdown reaches zero without reset verification.
- [ ] Using a dedicated approved test account and explicit authorization, enable **Show greeting** for one provider. Observe a naturally available verified reset; confirm the selected provider CLI sends its short greeting request once and the status reflects its actual result.
- [ ] Confirm Branchglance does not claim the greeting changed allowance or that work started/completed. Disable greeting and verify no later request occurs.
- [ ] Do not force exhaustion, alter provider limits, or repeat a send after an unverified result.

### Setup launch context

- [ ] In a disposable VM, start Setup from Claude Desktop's terminal/launch context. Confirm it visibly refuses before writing files, registry entries, shortcuts, Startup entries or provider settings and directs the user to File Explorer or the Start menu.
- [ ] Cause the AppData probe to be inconclusive. Confirm Setup visibly refuses with the probe state/reason and makes no install changes.
- [ ] Start the widget launcher from a redirected app context. Confirm the launcher reopens through File Explorer before the widget starts and the background relaunch request is consumed once.
- [ ] Separately launch Setup from File Explorer and confirm it follows the clean-install flow.

### Rollback

- [ ] Install two verified releases in the disposable VM. Preserve synthetic settings and record the currently active version.
- [ ] Create a Windows shortcut to the packaged Setup executable with argument `--rollback`; launch it from File Explorer, not a terminal.
- [ ] Confirm rollback selects the previous verified release, reports its version, and preserves user settings/history and owned integrations.
- [ ] Confirm rollback refuses cleanly when no previous verified release exists or installation metadata is invalid; it must not remove the current installation.

### Uninstall

- [ ] Uninstall through Windows **Settings → Apps → Installed apps → Branchglance → Uninstall** as a standard user.
- [ ] Confirm owned program files, Start menu, Desktop and Startup shortcuts and uninstall registration are removed.
- [ ] With Claude Code hooks enabled, confirm uninstall backs up settings, removes Branchglance-owned hook entries and consent, and preserves all unrelated hooks.
- [ ] Make Claude settings unreadable in a disposable test account. Confirm uninstall leaves settings untouched and reports the failure/result visibly.
- [ ] Review the narrow race case: if Claude Code writes settings while uninstall is updating them, record the resulting backup/hook state. No settings lock serializes these writes.
- [ ] Confirm user settings/history under `%LOCALAPPDATA%\Branchglance` remain after uninstall.
- [ ] Confirm a second uninstall attempt is safe and does not remove unrelated files.

### Privacy and release content

- [ ] Confirm Codex and Claude monitoring are local reads and no transcript or hook event is uploaded to a Branchglance service.
- [ ] Confirm provider usage reads go through their local application integrations and that an enabled greeting invokes the provider CLI with a short request. Release copy must not say “nothing is ever sent anywhere.”
- [ ] Re-run the private-material scan on the exact final tree and release assets. Record any hits and their disposition.
- [ ] Review the three README screenshots (rendered from synthetic fixtures; no EXIF or text metadata). Inspect pixels and metadata for usernames, local paths, titles, tokens and account identifiers.
- [ ] Confirm release assets, version and checksum match the signed-off build. Verify the private GitHub release draft renders correctly.

## Sign-off

- Windows install/upgrade owner: __________  Date: __________
- Provider behavior reviewer: __________  Date: __________
- Privacy/material scan reviewer: __________  Date: __________
- Release owner: __________  Date: __________
- Final release decision: ____________________
