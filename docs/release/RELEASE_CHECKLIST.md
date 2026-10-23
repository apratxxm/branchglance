# Branchglance 1.0.0 release checklist

Manual end-to-end plan for a disposable Windows x64 VM and synthetic/test accounts. Record Windows build, WebView2 and .NET versions, Setup file hash, account/provider versions, results and evidence. Never capture private chat titles, prompt text, tokens, account identifiers or full paths in screenshots or logs.

## Current source blockers — branch `docs`, base `dbde83a`

These are findings from static source review; they are not passed manual tests. Clear them before calling the release accepted.

- [ ] **Desktop shortcut — BLOCKED:** `scripts/branchglance_installer/integration.py` creates a Start menu shortcut and optional sign-in shortcut, but no Desktop shortcut. The requested Desktop-shortcut acceptance cannot pass with this source.
- [ ] **Launch-context refusal — BLOCKED:** Setup accepts ordinary command-line invocation; no guard was found that detects or refuses launch from Claude Desktop. The required refusal test below must fail if Setup proceeds or mutates state. Do not claim this protection until the implementation and test exist.
- [ ] **Claude hooks on uninstall — BLOCKED:** installer source removes app-owned integrations/files but does not call the Claude hook uninstaller. Before uninstall, users must disable collection in Reset Action Settings. Test that workflow and consider blocking/unwinding direct uninstall while consent/hooks remain; no such pre-uninstall guard was found.
- [ ] **Private-material scan — FAIL:** a pattern-based scan of all 47 reachable commits found five tracked paths containing absolute developer-path references: `token_tracker/static/runway_review.js`, `tests/test_token_tracker_refined.mjs`, `tests/test_token_tracker_runway.mjs`, `tests/test_claude_hooks.py`, and `tests/test_claude_hooks_provider.py`. The static review fixture and tests include machine-specific paths; review material also contains private-looking conversation labels. No raw values are reproduced here. No credential-shaped strings were found. Email matches are `.test` fixtures in two test files. No internal handoff document is present in reachable history. Do not rewrite history as part of this documentation task; curate the source/build inputs in an authorized follow-up before public distribution.
- [ ] **Screenshots — PENDING:** README lists four screenshot targets; none is included. Capture only sanitized, synthetic data after manual acceptance.

## Test record

Use a clean, disposable Windows 10 build 19041+ or Windows 11 x64 VM. Confirm .NET Framework 4.8+ and WebView2 Runtime. Use a non-administrator account. Start from a VM snapshot and restore it after each provider scenario. For quota and greeting tests, use approved test accounts and stop if a real limit cannot be safely observed; do not force quota exhaustion or send a greeting without explicit test-owner authorization.

Record each item as **Pass**, **Fail**, **Blocked** or **Not run**, with a short result and evidence reference. No row is pre-passed by this draft.

### Clean install

- [ ] Download the candidate Setup package from the private release draft and verify its recorded hash against the build manifest.
- [ ] Launch Setup by opening it in File Explorer. Confirm the preflight accepts the clean VM and does not request administrator elevation.
- [ ] Complete installation. Confirm files are under `%LOCALAPPDATA%\Programs\Branchglance` and the Start menu shortcut opens the widget.
- [ ] Confirm Setup offers optional sign-in startup with the intended default. Select **No** on one clean snapshot; verify no startup shortcut/task is registered.
- [ ] Verify the requested Desktop shortcut is created and opens the widget. **Expected to fail on this source; release blocker above.**
- [ ] Confirm no user data or provider settings are changed by installation alone. Claude Code hooks must remain absent and consent disabled.

### Upgrade from release candidate

- [ ] Install the approved RC in a separate snapshot. Add synthetic local settings/history and record current version and integrations.
- [ ] Run 1.0 Setup from File Explorer over the Branchglance-owned RC install. Confirm it recognizes the owned location and preserves settings/history.
- [ ] Confirm the active release changes to 1.0.0 only after staging/self-check succeeds; interrupt at each approved failure point if the build provides a documented test hook, then verify the previous release remains launchable.
- [ ] Verify Start menu shortcut, Desktop shortcut, optional sign-in startup and uninstall registration after upgrade. Desktop link is expected to be missing in this source.

### Startup at sign-in

- [ ] From a fresh snapshot, install and choose **Yes** to automatic sign-in startup.
- [ ] Sign out and back in as the same standard user. Confirm one Branchglance instance opens in the background and the widget is available.
- [ ] Confirm a second sign-in does not create duplicate instances. Record whether a startup task or shortcut was used.

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

- [ ] In a disposable VM, start the Setup executable from Claude Desktop's terminal/launch context. Expected behavior: Setup refuses before writing files, registry entries, shortcuts, startup entries or provider settings, and tells the user to run Setup from File Explorer or the Start menu.
- [ ] **Expected to fail on this source:** no launch-context refusal guard was found in the installer. If Setup proceeds, mark Fail and block release; do not continue the install in this VM.
- [ ] Separately launch Setup from File Explorer and confirm it follows the clean-install flow.

### Rollback

- [ ] Install two verified releases in the disposable VM. Preserve synthetic settings and record the currently active version.
- [ ] Create a Windows shortcut to the packaged Setup executable with argument `--rollback`; launch it from File Explorer, not a terminal.
- [ ] Confirm rollback selects the previous verified release, reports its version, and preserves user settings/history and owned integrations.
- [ ] Confirm rollback refuses cleanly when no previous verified release exists or installation metadata is invalid; it must not remove the current installation.

### Uninstall

- [ ] With Claude collection enabled, open Reset Action Settings and disable collection first. Confirm consent is revoked and Branchglance-owned Claude hook entries are removed while unrelated hooks remain.
- [ ] Uninstall through Windows **Settings → Apps → Installed apps → Branchglance → Uninstall** as a standard user.
- [ ] Confirm owned program files, Start menu/startup shortcuts and uninstall registration are removed. Confirm there are no remaining Branchglance Claude hook entries; the app uninstaller does not remove them, so this check depends on the disable step above.
- [ ] Confirm user settings/history under `%LOCALAPPDATA%\Branchglance` remain, and Claude consent/hooks follow the documented uninstall behavior without removing unrelated hook entries.
- [ ] Confirm a second uninstall attempt is safe and does not remove unrelated files.

### Privacy and release content

- [ ] Confirm Codex and Claude monitoring are local reads and no transcript or hook event is uploaded to a Branchglance service.
- [ ] Confirm provider usage reads go through their local application integrations and that an enabled greeting invokes the provider CLI with a short request. Release copy must not say “nothing is ever sent anywhere.”
- [ ] Repeat the private-material scan on the exact source tree and release assets. Resolve all machine-specific and private-looking fixture hits before public distribution; do not rewrite history without a separate approved recovery plan.
- [ ] Add and review the four README screenshots using synthetic content. Inspect pixels and metadata for usernames, local paths, titles, tokens and account identifiers.
- [ ] Confirm release text and metadata do not credit a model or assistant as an author.
- [ ] Confirm release assets, version and checksum match the signed-off build. Verify the private GitHub release draft renders correctly.

## Sign-off

- Windows install/upgrade owner: __________  Date: __________
- Provider behavior reviewer: __________  Date: __________
- Privacy/material scan reviewer: __________  Date: __________
- Release owner: __________  Date: __________
- Final decision: **Blocked until current source blockers above are cleared.**
