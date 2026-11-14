# Contributing

Thanks for your interest in Branchglance. Bug reports, fixes and focused features are all welcome. This guide covers setting up a development copy, running the tests, building the installer and opening a pull request. Everyone taking part is expected to follow the [Code of Conduct](../CODE_OF_CONDUCT.md).

Windows is the primary platform; the macOS notch app builds from source (see [the macOS guide](../macos/README.md)). Keep platform-specific code behind a clear boundary, and read [the architecture](ARCHITECTURE.md) and [the macOS port inventory](MAC_PORT.md) before touching it.

## Development setup

You need Python 3.12 or newer and Node.js 18 or newer. From a fresh clone, in PowerShell:

```powershell
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -r requirements-token-tracker.txt
```

Run the widget from source:

```powershell
.\.venv\Scripts\python.exe -m token_tracker.widget
```

A source checkout keeps its state in `%LOCALAPPDATA%\TokenTracker`, separate from an installed copy. To use a throwaway folder instead, set `BRANCHGLANCE_STATE_ROOT` before starting it. The installed widget and a source copy use the same local ports (8765 and 8766), so quit one before starting the other.

## Checks

Install the lint tool once (`python -m pip install ruff==0.14.4`), then run all checks from the repository root:

```text
python scripts/run_checks.py
```

The script runs the Python tests (pytest), every `tests/*.mjs` file under Node, Ruff and ESLint. Node.js and npm are required; the lint step runs pinned ESLint via `npx`. To run one part, use `--only python`, `--only node` or `--only lint`.

CI runs this same command on Windows and macOS for every pull request and every push to `master`. CI also runs CodeQL (static security analysis) and pip-audit (known-vulnerable dependencies), and Dependabot opens weekly update pull requests.

Rules for tests:

- Never let a test read or write your real `~/.claude`, `~/.codex` or `%LOCALAPPDATA%` data. Use temporary folders, injected settings paths and fake adapters.
- Don't make real provider requests from tests.
- Python tests cover the backend, providers, API and native contracts; `.mjs` tests cover the static UI.
- Guard tests that need native Windows tools by platform or tool availability, and keep everything else runnable anywhere.

## Building the installer

Release builds use a separate, pinned environment. Python 3.12 matches what CI uses:

```powershell
py -3.12 -m venv .build-venv
.\.build-venv\Scripts\python.exe -m pip install -r packaging/requirements-release.txt
.\.venv\Scripts\python.exe packaging/build.py --output build --python .build-venv\Scripts\python.exe
```

The output folder contains `BranchglanceSetup.exe`, the uninstaller, the launcher and a `releases/<version>/` folder. Check a build before installing it:

```powershell
.\build\releases\<version>\BranchglanceApp.exe --self-test
```

Local builds and v0.1.0 are unsigned, so Windows SmartScreen may warn. Release signing depends on workflow configuration; check each release for its signing status. See the [code signing policy](CODE_SIGNING_POLICY.md).

If you fork the project to ship your own build, change the product name and icons in `token_tracker/product.py` and `assets/brand/`, so your build doesn't install over an official one.

## Code conventions

- Keep observations explicit. A status, timestamp or link should come from evidence; when the evidence is missing, show unknown rather than inferring from silence.
- Keep reading, displaying and sending separate. Anything that sends to a provider must be opt-in and off by default.
- Never touch files or settings Branchglance doesn't own. Installer and hook changes need failure-path tests with temporary folders or fakes.
- Say in docs whether something is implemented or planned.

## Adding a provider

Branchglance builds one snapshot per refresh from a list of providers. Two exist today: Codex and Claude. This section describes the contract they follow, so a new agent tool can be added the same way. Read [the architecture](ARCHITECTURE.md) first.

**What a provider returns.** A provider is a class with a `snapshot()` method that returns a dict. `build_snapshot()` in `token_tracker/providers.py` calls it, and `_provider_snapshot()` in the same file keeps the last good data (marked `stale`) when a build fails. The dict has:

- `name` (the provider name, for example `"Claude"`), `connected` (bool) and `accent` (the colour used for its rows).
- `chats`: a list of row dicts. Each row needs a string `id`. `build_snapshot()` sets `provider` on each row, and `_dedupe_provider_chats()` removes duplicate ids. Optional `parentId` and `parentProvider` fields feed the tree.
- `fiveHour` and `weekly` limit objects, each with `percent`, `reset`, `resetAt` and `lastKnownResetAt`. The shape is visible in `ClaudeCodeProvider.snapshot()` in `token_tracker/claude_provider.py`.
- Optional: `cache`, `activity` (a list of `{date, value}`), `limitsUpdatedAt` and `source` (a short text describing where the data comes from).

Keep observations explicit: a status, timestamp or link should come from evidence, and show unknown when the evidence is missing.

**Where it is registered.**

1. `token_tracker/provider_policy.py`: add the name to `PROVIDERS`, its environment prefix to `_ENV_NAMES`, and any actions it supports to `_CAPABILITIES` (only `greet` and `continue` exist). The collect, show and send flags are derived from these names.
2. `token_tracker/providers.py`: add the name to `ENABLED_PROVIDERS` for full collection (chats and limits), or to `LIMIT_ONLY_PROVIDERS` for limits only. Then add its class to the `factories` dict (or `limit_factories`) inside `build_snapshot()`, with its accent colour.
3. Parent links: `snapshot_graph()` in `token_tracker/agent_links.py` links rows by `provider:id`. Cross-provider links are resolved only by `apply_exact_links()`, which `build_snapshot()` runs for Claude and Codex. Leave `parentId` empty unless the link is exact.
4. Environment flags: the `TOKEN_TRACKER_<NAME>_COLLECT`, `_SHOW` and `_SEND` flags are part of the inventory in [Configuration](CONFIGURATION.md). `tests/test_config_docs.py` fails until the new flags are listed there between the environment inventory markers.

**Tests to copy.** Use these as the starting point, and keep the suite isolated as described in the rules above:

- `tests/test_provider_policy.py` for policy names, flags and capabilities.
- `tests/test_claude_hooks_provider.py` for a provider fed by hook events, and `tests/test_claude_usage_api.py` if it reads limits from a remote API.
- `tests/test_agent_links.py` if rows can be parents of Codex jobs.
- `tests/test_config_docs.py` for the environment inventory.
- `tests/conftest.py` already isolates `LOCALAPPDATA`, the state root and `CLAUDE_CONFIG_DIR` for every test. Use injected factories and fake data rather than real sessions.

## Commits

Format: `Area: what changed`, written as a statement, no trailing period. Keep the first line under about 72 characters.

```
Widget: open only when a subagent starts, finishes or comes back
Installer: keep the newest bg-codex on rollback
```

- The area is the part of the app a user would name: `Widget`, `Insights`, `Settings`, `Installer`, `Codex`, `Claude`, `Docs`, `CI`, `macOS`.
- Add a body when the reason isn't obvious from the diff: what was wrong and why this fixes it.
- One logical change per commit. Don't mix a refactor with a behaviour change.
- No generated-by lines or co-author trailers for tools.

## Branches and pull requests

1. Branch from `master` with a short kebab-case name for the change: `insights-hover`, `bg-codex-path`.
2. Keep the pull request to one topic. Its title follows the commit format.
3. Fill in the template: what changes for the user, screenshots for anything visible, platform notes, changes to stored data or permissions, and the checks you ran.
4. Run `python scripts/run_checks.py` locally first. CI (`.github/workflows/ci.yml`) runs the same command on Windows and macOS for every pull request. Pull requests into `master` must pass it before merging.
5. Pull requests from contributors are squash-merged with a clean message. Branches from maintainers are merged with a merge commit, `Merge branch '<name>': <summary>`.

Platform-specific work is merged after review by someone who can test on that platform.

### Review comments

Say what to change and why, in one or two sentences. Separate blocking changes from optional ones by saying which is which. Reply to every blocking comment before merging, either with the change or with the reason not to.

## Versions and releases

Branchglance is in **alpha**: features are still being added and behaviour can change between releases. Versions follow [Semantic Versioning](https://semver.org) below 1.0:

| Change | Bump | Example |
|---|---|---|
| New feature or a visible behaviour change | minor | 0.1.0 → 0.2.0 |
| Bug fix only | patch | 0.2.0 → 0.2.1 |

- The stage moves to **beta** once the planned feature set is in and releases are signed, and to **1.0** once it has been stable for daily use.
- Every build has a build ID after a `+`, for example `0.2.0+910b6d3`. That's the commit it was built from.
- `CHANGELOG.md` keeps an `[Unreleased]` section. Add a line there in the same pull request as the change, under Added, Changed, Fixed or Security, in wording a user would understand. Don't put internal ticket or review IDs in it.

Maintainers: the tag, build, sign and publish steps are in [RELEASING](release/RELEASING.md).

## Reporting bugs

Open an issue with your Windows or macOS version, the Branchglance version (shown under **Settings** in the widget) and steps to reproduce. Please don't paste chat transcripts or tokens; describe the behaviour instead. Support and question routes are in [SUPPORT.md](../SUPPORT.md). Security problems go to [SECURITY.md](../SECURITY.md), not to a public issue.

## Attaching a debug report

Settings has a **Report a bug** section. Click **Preview debug report**, check the text, then click **Copy** and paste it into the issue form. **Open GitHub issue** opens the form with your version and OS filled in; it attaches nothing.

From a source checkout, run `.\.venv\Scripts\python.exe -m token_tracker.widget --debug-report` and optionally add a path. Without a path it writes `branchglance-debug-report.md` to the current folder and prints the full path. The command reads the running widget on port 8765. If that read fails, the file says "App not running" and has no agent tree.

From an installed copy, the same command is `%LOCALAPPDATA%\Programs\Branchglance\Branchglance.exe --debug-report` on Windows. It has no console, so it prints nothing: without a path, or with a relative one, the report goes to the `Downloads` folder in your user profile, or to the profile folder itself if there is no `Downloads` folder. On macOS, run `PYTHONPATH="$HOME/Library/Application Support/Branchglance/runtime" "$HOME/Library/Application Support/Branchglance/runtime/.venv/bin/python" -m token_tracker.debug_report` from the folder you want the report in; it uses the installed runtime and does not start the app.

The report has the app version and build, OS and architecture, Python version, install type, Settings switches and provider flags, Claude hook state, counts and depths of chats and subagents, status and state values, public model names, and warnings or errors as component, level, time and exception class name. Times are age buckets or offsets in seconds, never clock times.

It never contains chat titles, prompts or replies, file paths, workspace names, ids, account names, emails or tokens. Read the file before you attach it. Branchglance never sends a report; you do.
