# Contributing

Thanks for helping with Branchglance. The current desktop host, installer and release packaging support Windows. Keep platform-specific changes behind a clear platform boundary; macOS support is not implemented yet. See [the architecture](ARCHITECTURE.md) and [the macOS port inventory](MAC_PORT.md) before changing platform behaviour.

## Development setup

From a fresh clone, use Python 3.10 or newer and a current Node.js installation. In PowerShell:

```powershell
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -r requirements-token-tracker.txt
```

For local release builds, use a virtual environment with the release dependencies installed:

```powershell
.\.venv\Scripts\python.exe -m pip install -r packaging/requirements-release.txt
```

Run the widget from source with:

```powershell
.\.venv\Scripts\python.exe -m token_tracker.widget
```

The `scripts/start_token_tracker.ps1` helper starts the HTTP server directly; use the module command above when you want the desktop widget. Source launch still uses the Windows desktop/webview integration.

Run the Python tests with:

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

Run all standalone frontend checks from PowerShell:

```powershell
Get-ChildItem tests -Filter '*.mjs' | Sort-Object Name | ForEach-Object {
    node $_.FullName
    if ($LASTEXITCODE -ne 0) { throw "Node check failed: $($_.Name)" }
}
```

There is no npm install or package script. Each `.mjs` file is run directly by Node. On other shells, iterate over `tests/*.mjs` and stop at the first nonzero exit.

## Tests and local provider settings

Tests that exercise Claude hook installation must inject a temporary settings path. Never let a test write to the real `~/.claude/settings.json`. Hook installation and removal must add, update or remove only entries that Branchglance can positively identify as its own; preserve unrelated Claude hooks and settings. Use temporary homes, transcripts, app data roots and fake adapters for tests. Do not make real provider requests from tests.

Keep tests alongside the source concern: Python tests cover backend/provider/API/native contracts; `.mjs` files cover static UI behaviour. Guard native Windows integration tests by platform or tool availability, while keeping platform-neutral reducer and adapter-contract tests runnable everywhere.

## Code and documentation conventions

- Keep provider observations, timestamps, provenance and unavailable/null states explicit. Do not infer a running or waiting state from silence.
- Keep collection, display and permission to send separate. Queuing work is not an answer and is not evidence that work started.
- Preserve user-owned files and unrelated settings. Installer and hook changes need failure-path coverage using temporary directories or fakes.
- Document whether behaviour is implemented or planned. Do not describe mocked tests as native macOS/Windows acceptance.
- Use clear commits with a concise summary. Do not add AI attribution to commit messages or source.

## Branch and pull request flow

Create a focused topic branch from `master`, keep changes scoped, and open a pull request targeting `master`. Describe user-visible behaviour, platform impact, storage or permission changes, and the checks you ran. Link related issues when available. Do not merge platform-specific work until the relevant platform owner has reviewed it and platform-gated checks are passing.
