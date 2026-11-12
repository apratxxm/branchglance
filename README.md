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

**Branchglance is a small always-on-top widget that shows what your Codex and Claude Code agents are doing: which are running, which are waiting on you, and how much of your usage limit is left.**

<p align="center">
  <img alt="Branchglance showing a tree of Codex and Claude Code chats with subagents, plus 5-hour and weekly limits" src="docs/images/widget.png" width="560">
</p>

All screenshots use synthetic sample data.

> These docs describe `master`. For the latest release, use the docs at its release tag; features in [Unreleased](CHANGELOG.md) require a source build.

## What it shows

Items marked **(0.2.0)** are in the development version.

- **A tree of your chats.** Codex and Claude Code chats with their subagents. Codex jobs started from a Claude chat nest under it. A status or parent link appears only when there is evidence for it.
- **A needs-you count.** Chats waiting for input are marked and counted. Click the count to jump to the next one.
- **Usage limits.** 5-hour and weekly limits for Codex and Claude side by side, with reset countdowns. Optional live Claude usage (off by default) refreshes about every 180 seconds **(0.2.0)**.
- **Insights.** Tokens, estimated cost, model breakdown and limit peaks, over 24 hours to all time. Models without a known price are listed, never guessed.
- **Reset actions (off by default).** A greeting that keeps Claude's 5-hour window rolling **(0.2.0)**, and a one-time `continue` queued for chats you pick.
- **Out of the way.** The widget collapses to a thin strip with a clock and limit bands. Hover over it, or press Ctrl+Shift+T, to open the tree.

## Supported tools

Branchglance currently works with Claude Code and Codex. Support for more agent harnesses is planned. If you use a different setup, you're welcome to fork Branchglance and add support. See [Adding a provider](docs/CONTRIBUTING.md#adding-a-provider).

## Platforms

| Platform | Install method | Native testing | Known limits |
|---|---|---|---|
| Windows | Alpha installer, or source | Recorded in each release record | v0.1.0 is unsigned; SmartScreen may warn |
| macOS | Source build | Not natively verified | No downloadable installer; live usage needs a credentials file |

Full requirements, signing status and verification steps are in the [install guide](docs/INSTALL.md).

## Install

**Windows**

1. Download `BranchglanceSetup.exe` from the [latest release](https://github.com/apratxxm/branchglance/releases).
2. Run it as your normal user, not as administrator.
3. Open **Branchglance** from the Start menu.

**macOS (from source):**

```sh
git clone https://github.com/apratxxm/branchglance.git
cd branchglance
./macos/build.sh install
```

See the [install guide](docs/INSTALL.md) for update, rollback and uninstall, and the [macOS guide](macos/README.md) for details.

## Privacy

Branchglance runs on your machine. It reads local Codex and Claude Code data, and a local loopback server serves its UI. There is no Branchglance-hosted account, analytics or sync service. Transcript files are never uploaded. Claude tracking, live usage, Claude hooks, the usage ledger, greetings and sending are all off until you turn them on. With live usage on, Branchglance sends Claude Code's OAuth token to `api.anthropic.com` to read your usage. Messages leave only when you enable reset greetings or Codex sending, and they go through the provider's own CLI. The full list of what is read, sent and stored is in [Privacy](docs/PRIVACY.md), and reporting security issues is covered in [SECURITY.md](SECURITY.md).

## Documentation

| | |
|---|---|
| [Walkthrough](docs/GUIDE.md) | Every control, and a day of use |
| [Install](docs/INSTALL.md) | Installer, verification, macOS, update and uninstall, troubleshooting |
| [Configuration](docs/CONFIGURATION.md) | Claude tracking, live usage, status line, ledger, greetings, environment flags |
| [Privacy](docs/PRIVACY.md) | Exactly what is read, sent and stored |
| [Architecture](docs/ARCHITECTURE.md), [Integration](docs/INTEGRATION.md) | How it works; nesting your own launchers |
| [Changelog](CHANGELOG.md), [Code signing policy](docs/CODE_SIGNING_POLICY.md) | History and signing |

The [documentation index](docs/README.md) lists everything, including the release process.

## Contributing and support

- [Contributing](docs/CONTRIBUTING.md): set up a development copy, run the checks, and open a pull request. CI runs the same checks on Windows and macOS for every pull request.
- [Support](SUPPORT.md): where to ask questions and report bugs.
- [Security policy](SECURITY.md): report vulnerabilities privately.
- [Code of conduct](CODE_OF_CONDUCT.md).

## License

[MIT](LICENSE). Third-party components keep their own licenses ([notices](THIRD_PARTY_NOTICES.md)). The Branchglance name and logo are not licensed as trademarks.
