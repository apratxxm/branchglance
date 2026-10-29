# Branchglance 0.1.0 release checklist

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
- [ ] Confirm the active release changes to 0.1.0 only after staging/self-check succeeds; interrupt at each approved failure point if the build provides a documented test hook, then verify the previous release remains launchable.
- [ ] Verify Start menu and Desktop shortcuts, optional sign-in Startup shortcut and uninstall registration after upgrade. Confirm locations are resolved from Windows known folders and shortcuts use the Live Stack icon.

### Startup at sign-in

- [ ] From a fresh snapshot, install and choose **Yes** to automatic sign-in startup.
- [ ] Sign out and back in as the same standard user. Confirm one Branchglance instance opens in the background and the widget is available.
- [ ] Confirm a second sign-in does not create duplicate instances. Record whether a startup task or shortcut was used.
- [ ] Reopen the widget after it has a prior snapshot. Confirm it displays that snapshot immediately with a stale marker, without live statuses or limits, then refreshes in the background. Confirm a first-ever launch loads normally.

### Codex limits, tree and pop-up

- [ ] With Codex signed in, confirm 5-hour and weekly values appear when fresh data is available. Verify both meters count **used** upward and cap at 100%, and that a stale value shows its age in the tooltip and a grey value, never as current.
- [ ] Confirm the collapsed strip is 328 px wide at 100% scaling and the countdown never overlaps the percentage, including when a value is last-known.
- [ ] Create a synthetic root chat with several child agents. Verify parent/child nesting, observed statuses, activity and elapsed time, and that tree lines stay unbroken at rest and while scrolling with several roots.
- [ ] Scroll a long subtree. Confirm the parent row sticks with a shadow and its children don't show through it.
- [ ] Trigger new work or a waiting question in the synthetic session. Confirm the compact pop-up surfaces it and the waiting chat appears under **Needs you**.

### Claude limits, consent and tree

- [ ] Before consent, inspect Claude Code settings and local Branchglance state. Confirm Branchglance hooks are absent, no Claude hook events are collected, and Claude rows are not shown.
- [ ] Enable Claude tracking in **Settings → Notifications**. Confirm the settings backup exists, only Branchglance-owned hook entries are added, the consent record names the settings file, and Claude rows appear in the same tree as Codex with orange accents.
- [ ] Verify Claude Desktop's cached 5-hour and weekly values. Confirm stale or missing data stays unknown rather than becoming a fabricated current value.
- [ ] Trigger a synthetic hook-reported child agent if available. Record observed coverage; do not infer that all real Claude subagents are captured.
- [ ] From a synthetic Claude Code session, run `codex exec resume <thread>` and confirm the Codex job nests under that Claude chat while it runs and after it finishes.
- [ ] From the same session, start a new `codex exec`, then call `Branchglance.exe --link-codex <thread> --codex-pid <pid>` while it runs. Confirm a receipt lands in `%LOCALAPPDATA%\Branchglance\codex-link-receipts\receipts.jsonl` and the job nests under the chat.
- [ ] Disable Claude tracking. Confirm consent is revoked and only Branchglance-owned hook entries are removed from the recorded settings file; leave unrelated settings untouched.

### Search

- [ ] Verify plain search matches titles, agents, models and workspaces, and that `is:needs`, `is:running` and `is:failed` filter as described. Clear the search and confirm the full tree returns.

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
- [ ] Review the README screenshots (rendered by `scripts/screenshots` from synthetic data; no EXIF or text metadata). Inspect pixels and metadata for usernames, local paths, titles, tokens and account identifiers.
- [ ] Confirm release assets, version and checksum match the signed-off build. Verify the private GitHub release draft renders correctly.

## Sign-off

- Windows install/upgrade owner: __________  Date: __________
- Provider behavior reviewer: __________  Date: __________
- Privacy/material scan reviewer: __________  Date: __________
- Release owner: __________  Date: __________
- Final release decision: ____________________
