# Unified agent tree

Status: design accepted after Sol review r1/r2 (F1–F6, 25 Oct 2026). Mockup: `docs/mockups/unified-tree-claude-parent-bg.jpg`.

## Outcome
One tree of every agent on the machine instead of the Claude/Codex source switch. A Claude chat (e.g. an Opus lead) shows the Codex jobs it launched and its own subagents beneath it. No provider labels: colour carries identity (Claude rows warm orange with the warm parent background, OpenAI rows blue-violet). Both providers' 5-hour and weekly bands show together, each reset time in its provider colour. With only one provider present, only its rows and bands show (no empty slots). The widget keeps the Codex-state frame and glows. Clock is 12-hour. Model names: "Opus 5.5", "Sonnet 5.5", "Haiku 5.5", "GPT-6 Luna", "GPT-6.1 Sol" (OpenAI names with a bluer-violet glow). Every column (time, status, activity, workspace, tokens) is centred on one shared axis across parents and children.

## Linking (backend)
A Codex root row gets a Claude parent only on exact evidence; anything ambiguous stays a top-level row. A wrong parent is worse than no parent.

1. **Policy first.** Cross-provider capture runs only when both Claude and Codex collection are permitted (`_provider_collection_sets` / `provider_policy`). Otherwise read nothing for it: no process scan, no hook history, no receipts (zero-read). Edge publication is filtered again by show-policy: an edge is published only if both endpoints are visible.
2. **Process scan (each poll, only when 1 allows).** One Toolhelp32 snapshot per poll. Consider only processes owned by the current user's SID (fail closed if ownership can't be read) and named `codex.exe`. Read their command line with `NtQueryInformationProcess(ProcessCommandLineInformation)`; keep only the parsed thread uuid after `exec resume`. Never persist or log raw argv, cwd or environment.
3. **Ancestry.** Walk parents to the nearest `claude.exe`. Every edge must validate: parent creation time < child creation time, and each (pid, created) stable within the poll. Any missing/reused/unverifiable edge rejects the chain. Map the Claude (pid, created) to a session using time-scoped associations from hook records (keep every (pid, created) per session with first/last ts; not latest-only).
4. **Receipts.** `Branchglance.exe --link-codex <thread-uuid>` (for new `codex exec` runs whose thread id is only known after start) is called by the launcher as `--link-codex <thread-uuid> --codex-pid <pid>`. The helper reads `CLAUDE_CODE_SESSION_ID` from its own environment and records the Codex process's creation time itself (binding the receipt to that execution). It appends one bounded JSON line to a current-user-private inbox folder. Only the app reads the inbox and merges it. Receipts are display-only: they never set Running, grant collection or authorise anything. Strict uuid validation, size caps.
5. **Executions, not threads.** A run = (codex thread id, codex (pid, created), claude session id, first/last seen, method: `cmdline`|`receipt`). The run registry is written only by the running app instance (holds the instance mutex), atomic writes, validated on load, retention 7 days by last-seen, independent of snapshot filtering.
6. **Parent of a Codex row.** While a linked run is live: that run's session. When idle: the session of the most recent run **only if** no newer native execution of that thread exists without matching evidence (compare the rollout's latest turn start against the run's last-seen; a newer unlinked turn invalidates the historical parent → top-level). Concurrent conflicting runs → unlinked.
7. **Native lineage wins.** Codex subagent → Codex parent and Claude subagent → Claude session stay as today. Only Codex roots receive cross-provider parents. Cycles or missing parents → render as root.

## Snapshot contract
- Every row has canonical `key` = `<provider>:<id>` and `parentKey` (provider-qualified or null).
- `providers[].chats` projections stay for compatibility.
- Full and live payloads carry `graph: {revision, edges: {childKey: parentKey|null}}`; `null` removes an edge; the revision increments on any change.
- Limits: both providers' 5-hour and weekly windows with independent reset times; the UI hides a provider with no data.

## UI
- Single tree, no source switch. Group = Claude parent (warm background) with its children (Claude subagents, linked Codex jobs and their Codex subagents), then unlinked roots. Order by latest activity.
- Counts, waiting badge, jump-to-waiting and trimming work across providers via `key`/`parentKey`.
- Limit bands: top band 5-hour, bottom band weekly; each shows both providers (bar + % + reset countdown in provider colour); one provider absent → its half hidden.

## Acceptance
- Unit: F1 counterexample (unrelated same-cwd exec within 10 s) stays unlinked; PID reuse at an intermediate edge rejects; resume from a plain terminal invalidates the idle parent; conflicting concurrent runs unlinked; collection disabled → zero reads (process scan, hooks, inbox); other-user processes ignored; receipts can't set Running; graph null-removal; cycle → root.
- Perf: process step ≤ 5 ms median added per poll on this machine (measured before/after).
- Node: tree rendering with mixed providers, single-provider (Claude-only, Codex-only), column alignment, 12-hour clock, per-provider reset colours.
- Live: this machine's Opus session shows its running Luna/Sol `codex exec resume` jobs beneath it.
