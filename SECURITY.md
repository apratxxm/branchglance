# Security policy

Branchglance reads local Codex and Claude Code files, can edit Claude Code's `settings.json` to add hooks (only after you turn Claude tracking on), and can read Claude Code's OAuth token in memory to fetch your usage from Anthropic. Security reports are taken seriously.

## Supported versions

Branchglance is in alpha. Security fixes are made for the latest release and on `master`. Earlier releases don't receive fixes; update to the latest release to get them.

## Reporting a vulnerability

Report it privately through GitHub's private vulnerability reporting: open the repository's **Security** tab and choose **Report a vulnerability**, or use [this link](https://github.com/apratxxm/branchglance/security/advisories/new). Please don't open a public issue or discussion for a security problem.

Include:

- what you found and what an attacker could do with it;
- the steps to reproduce it, and a proof of concept if you have one;
- the affected version (shown under **Settings** in the widget) or commit, and your Windows or macOS version;
- whether the issue is already public anywhere.

Redact tokens, transcripts and private paths from anything you attach.

Branchglance is a small project with no guaranteed response times. The maintainer aims to acknowledge reports within a few days and will keep you informed while the issue is looked at and fixed.

## Scope

In scope: the Branchglance application and installer, the hooks and status line settings it writes, its local loopback server, and how it handles provider data and credentials.

Out of scope: vulnerabilities in Codex, Claude Code, Windows or macOS. Report those to their vendors.

## What Branchglance does with your data

- It serves its UI through a local loopback HTTP server, has no Branchglance-hosted account, analytics or sync service, and uploads nothing to Branchglance.
- With Claude tracking and live usage enabled and Claude Code signed in, it sends Claude Code's OAuth token to `api.anthropic.com` to fetch your usage. It reads the token into memory from `.credentials.json`, and never refreshes, logs or stores it.
- If you enable a reset greeting or Codex sending, it runs the provider's own CLI to send a short message to that provider.

Full detail: [Privacy](docs/PRIVACY.md).

Local builds and v0.1.0 are unsigned. Signing depends on release configuration; check each release for its signing status. See the [signing policy](docs/CODE_SIGNING_POLICY.md).
