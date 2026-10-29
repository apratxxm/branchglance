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
- Write commit messages as a short summary of what changed and why.

## Pull requests

1. Create a branch from `master` and keep the change focused.
2. Run both test suites.
3. Open a pull request against `master`. Describe the user-visible change, anything platform-specific, any change to stored data or permissions, and the checks you ran.

Platform-specific work is merged after review by someone who can test on that platform.

## Reporting bugs

Open an issue with your Windows version, the Branchglance version (shown under **Settings** in the widget) and steps to reproduce. Please don't paste chat transcripts or tokens; describe the behaviour instead.
