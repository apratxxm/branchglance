<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="assets/brand/lockup-dark.svg">
    <source media="(prefers-color-scheme: light)" srcset="assets/brand/lockup-light.svg">
    <img alt="Branchglance" src="assets/brand/lockup-dark.svg" width="520">
  </picture>
</p>

<p align="center">
  <a href="LICENSE"><img alt="License: MIT" src="https://img.shields.io/badge/license-MIT-blue"></a>
  <a href="https://github.com/apratxxm/branchglance/releases"><img alt="Latest release" src="https://img.shields.io/github/v/release/apratxxm/branchglance?include_prereleases&label=release"></a>
  <a href="https://github.com/apratxxm/branchglance/actions/workflows/ci.yml"><img alt="CI" src="https://img.shields.io/github/actions/workflow/status/apratxxm/branchglance/ci.yml?branch=master&label=CI"></a>
  <img alt="Platform: Windows and macOS" src="https://img.shields.io/badge/platform-Windows%20%7C%20macOS-lightgrey">
  <img alt="Python 3.10+" src="https://img.shields.io/badge/python-3.10%2B-3776AB">
</p>

**A small always-on-top widget that shows what all your Codex and Claude Code agents are doing: which are running, which are waiting on you, and how much of your usage limits is left.**

<p align="center">
  <img alt="Branchglance showing a tree of Codex and Claude Code chats with subagents, plus 5-hour and weekly limits" src="docs/images/widget.png" width="560">
</p>

## Why

Running several agent chats in parallel means constantly checking which one finished, which one is stuck on a question, and how close you are to a limit. Branchglance puts that in one glanceable window, built from data already on your machine. There is no account to create and no Branchglance server.

## What it does

1. **One tree for both providers.** Every Codex and Claude Code chat with its subagents; Codex jobs started from a Claude chat nest under that chat.
2. **Needs-you queue.** Chats waiting for input are marked and counted. Click the count to jump to the next one.
3. **Limits with countdowns.** 5-hour and weekly usage for Codex and Claude side by side. Claude usage can refresh about every 30 seconds from Anthropic, using Claude Code's existing sign-in (with your consent; see [Privacy](#privacy)).
4. **Routing alerts.** A heads-up when one provider's limit runs low, so you can send work to the other.
5. **Reset handling.** Banked Codex resets shown beside the weekly bar. Optional, off by default: a greeting that keeps Claude's 5-hour window rolling, and a one-time `continue` queued for chats you pick.
6. **Insights.** Tokens, estimated cost, model breakdown, plan multiples and limit peaks over 24 hours, 7 days, 30 days or all time. Unpriced models are listed, never guessed.
7. **Honest status.** A status or parent link is shown only when there is evidence for it; otherwise it shows as unknown.
8. **Out of the way.** It collapses to a thin strip with a clock and limit bands; hover or press Ctrl+Shift+T for the tree.

<p align="center">
  <img alt="Insights view with token and cost totals, provider shares and limit peaks" src="docs/images/insights.png" width="560">
</p>

All screenshots use synthetic sample data.

## Quick start

**Windows (about a minute)**

1. Download `BranchglanceSetup.exe` from the [latest release](https://github.com/apratxxm/branchglance/releases).
2. Run it as your normal user, not as administrator.
3. Open **Branchglance** from the Start menu.

**macOS (from source):** `git clone https://github.com/apratxxm/branchglance.git && cd branchglance && ./macos/build.sh install`. See the [macOS guide](macos/README.md).

Details, verifying the download and uninstalling: [Install guide](docs/INSTALL.md).

## Requirements

- Windows 10 (build 19041) or 11, 64-bit, with Edge WebView2 and .NET Framework 4.8 (both ship with current Windows). The installer checks and tells you if one is missing.
- Codex signed in, for Codex chats and limits.
- Optional: Claude Code, for Claude chats and limits.
- macOS: Python 3 and the Xcode command line tools.

## Privacy

- Everything is read from local files on your machine: Codex session data, Claude Code transcripts and hook events, and the usage caches Claude Desktop and Claude Code keep. Transcripts are never uploaded.
- **One network call, only with Claude tracking on:** Branchglance reads Claude Code's OAuth token from `~/.claude/.credentials.json` in memory and asks `api.anthropic.com` for your account's usage. It never refreshes, logs or stores the token. On macOS it does not read the Keychain and falls back to the local caches.
- No Branchglance account, server or telemetry.
- Claude tracking, Claude hooks, the usage ledger and greetings are all off until you enable them.

Full details: [Privacy](docs/PRIVACY.md) and [SECURITY.md](SECURITY.md).

## Documentation

| | |
|---|---|
| [Walkthrough](docs/GUIDE.md) | Every control, and a day of use |
| [Install](docs/INSTALL.md) | Installer, verification, macOS, uninstall, troubleshooting |
| [Configuration](docs/CONFIGURATION.md) | Claude tracking, status line, ledger, greetings, environment flags |
| [Privacy](docs/PRIVACY.md) | Exactly what is read, what is sent, what is stored |
| [Architecture](docs/ARCHITECTURE.md), [Integration](docs/INTEGRATION.md) | How it works; nesting your own launchers |
| [Contributing](docs/CONTRIBUTING.md), [Releasing](docs/release/RELEASING.md) | Build from source, tests, cutting a release |
| [Changelog](CHANGELOG.md), [Code signing policy](docs/CODE_SIGNING_POLICY.md) | History and signing |

## License

[MIT](LICENSE). Third-party components keep their own licenses ([notices](THIRD_PARTY_NOTICES.md)). The Branchglance name and logo are not licensed as trademarks.
