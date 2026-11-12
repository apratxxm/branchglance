# Security policy

Branchglance reads local Codex and Claude Code files, can edit Claude Code's `settings.json` to add hooks (only after you turn Claude tracking on), and can read Claude Code's OAuth token in memory to fetch your usage from Anthropic. Security reports are taken seriously.

## Reporting a problem

Please report security problems privately through [GitHub's private vulnerability reporting](https://github.com/apratxxm/branchglance/security/advisories/new). Do not open a public issue.

Include what you found, how to reproduce it and what it affects. Expect a first reply within a few days.

## What Branchglance does with your data

- It has no account, server or telemetry, and uploads nothing to Branchglance.
- With Claude tracking on and Claude Code signed in, it sends Claude Code's OAuth token to `api.anthropic.com` to fetch your usage. It reads the token into memory from `.credentials.json`, and never refreshes, logs or stores it.
- If you enable a reset greeting or Codex sending, it runs the provider's own CLI to send a short message to that provider.

Full detail: [Privacy](docs/PRIVACY.md).

## Supported versions

Branchglance is in alpha. Only the latest release receives fixes.
