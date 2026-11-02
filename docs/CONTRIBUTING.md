# Contributing

Thanks for your interest in Branchglance. Bug reports, fixes and focused features are all welcome. This guide covers setting up a development copy, running the tests, building the installer and opening a pull request.

Branchglance currently runs on Windows only. Keep platform-specific code behind a clear boundary. If you're interested in macOS, read [the macOS port inventory](MAC_PORT.md) and [the architecture](ARCHITECTURE.md) first.

## Development setup

You need Python 3.10 or newer and Node.js 18 or newer. From a fresh clone, in PowerShell:

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

## Tests

Python tests:

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

UI tests. There is no npm install; each `.mjs` file runs directly under Node:

```powershell
Get-ChildItem tests -Filter *.mjs | Sort-Object Name | ForEach-Object {
    node $_.FullName
    if ($LASTEXITCODE -ne 0) { throw "UI test failed: $($_.Name)" }
}
```

Both suites run on every push and pull request (see `.github/workflows/ci.yml`).

Rules for tests:

- Never let a test read or write your real `~/.claude`, `~/.codex` or `%LOCALAPPDATA%` data. Use temporary folders, injected settings paths and fake adapters.
- Don't make real provider requests from tests.
- Python tests cover the backend, providers, API and native contracts; `.mjs` tests cover the static UI.
- Guard tests that need native Windows tools by platform or tool availability, and keep everything else runnable anywhere.

## Building the installer

Release builds use a separate, pinned environment. Python 3.10 matches what CI uses:

```powershell
py -3.10 -m venv .build-venv
.\.build-venv\Scripts\python.exe -m pip install -r packaging/requirements-release.txt
.\.venv\Scripts\python.exe packaging/build.py --output build --python .build-venv\Scripts\python.exe
```

The output folder contains `BranchglanceSetup.exe`, the uninstaller, the launcher and a `releases/<version>/` folder. Check a build before installing it:

```powershell
.\build\releases\<version>\BranchglanceApp.exe --self-test
```

Local builds are unsigned, so Windows SmartScreen may warn when you run them. Official releases are built and signed by the `Windows release` workflow; see the [code signing policy](CODE_SIGNING_POLICY.md).

If you fork the project to ship your own build, change the product name and icons in `token_tracker/product.py` and `assets/brand/`, so your build doesn't install over an official one.

## Code conventions

- Keep observations explicit. A status, timestamp or link should come from evidence; when the evidence is missing, show unknown rather than inferring from silence.
- Keep reading, displaying and sending separate. Anything that sends to a provider must be opt-in and off by default.
- Never touch files or settings Branchglance doesn't own. Installer and hook changes need failure-path tests with temporary folders or fakes.
- Say in docs whether something is implemented or planned.

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
4. CI must pass. Run both test suites locally first.
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
- `CHANGELOG.md` keeps an `Unreleased` section. Add a line there in the same pull request as the change, under Added, Changed or Fixed.

To cut a release:

1. Set `RELEASE_VERSION` in `packaging/build.py`.
2. Rename `Unreleased` in `CHANGELOG.md` to the version and date, and start a new empty `Unreleased`.
3. Tag the release commit `v<version>` and push the tag. The `Windows release` workflow builds and signs it.
4. Publish the GitHub release marked as a pre-release while the stage is alpha or beta, with the changelog section as its notes.

## Reporting bugs

Open an issue with your Windows version, the Branchglance version (shown under **Settings** in the widget) and steps to reproduce. Please don't paste chat transcripts or tokens; describe the behaviour instead.
