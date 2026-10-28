# Client integration

What a launcher, orchestrator script or other client has to do so that Branchglance can show its work correctly: Codex jobs nested under the Claude chat that started them, subagents under their parents, live statuses and clean titles.

Branchglance links rows only on exact evidence. A wrong parent is worse than no parent, so anything it can't prove is shown as a separate top-level chat. Most "this job shows up as its own parent" problems come from a client that didn't leave that evidence.

See [design/UNIFIED_TREE.md](design/UNIFIED_TREE.md) for the linking design, and [ARCHITECTURE.md](ARCHITECTURE.md) for the data flow.

## Checklist

| You want | The client must | Fails closed when |
|---|---|---|
| Claude chats, statuses and subagents | Enable Claude Code collection in Branchglance (consent), and let Claude Code run its hooks | Collection is off, hooks are disabled, or a hook event never arrives |
| Any cross-provider link (Codex under Claude) | Have **both** Claude and Codex collection allowed by policy | Either provider's `collect` is off: the cross-provider process scan and receipt reading are skipped (each provider can still collect on its own) |
| A resumed Codex job under its Claude chat | Start it as `codex exec resume <thread-uuid>` from a process that descends from the Claude Code process | The thread id isn't the argument right after `exec resume`, the process chain is broken, or the session can't be matched |
| A **new** `codex exec` job under its Claude chat | Write a link receipt once the thread id is known: `Branchglance.exe --link-codex <thread-uuid> --codex-pid <pid>` | No receipt, `CLAUDE_CODE_SESSION_ID` missing, or the pid isn't that live `codex.exe` |
| Codex subagents under their Codex parent | Nothing. Codex records `thread_spawn` and `parent_thread_id` itself | Lineage records are missing or conflict |
| Clean titles | Name jobs with the task first (see [Titles](#titles)) | — |

## Claude Code hooks

Collection is off until the user enables it in Branchglance (Settings → Notifications → Claude tracking). Enabling it backs up Claude Code's `settings.json` and adds Branchglance-owned hook entries that run `Branchglance.exe --claude-hook`.

- **Config folder:** Branchglance uses `CLAUDE_HOME` if set, then `CLAUDE_CONFIG_DIR`, then `~/.claude` (`claude_hook_install.claude_config_dir`). Transcripts are read from `<config dir>/projects`. If you switch config folders after enabling collection, enable it again so the hooks are in the folder Claude Code actually uses.
- **Events:** `SessionStart`, `SessionEnd`, `UserPromptSubmit`, `PreToolUse`, `PostToolUse`, `PostToolUseFailure`, `PermissionRequest`, `Notification`, `Stop`, `StopFailure`, `SubagentStart`, `SubagentStop`, `PreCompact`, `PostCompact` (`claude_hook._EVENTS`).
  - `UserPromptSubmit` starts a turn. Tool events record the current activity.
  - `Stop` (completed) and `StopFailure` (failed) end that turn. So does a `Notification` of type `idle_prompt`.
  - `PermissionRequest`, a `Notification` of type `permission_prompt` or `elicitation_dialog`, and a `PreToolUse` for a question tool mark the turn waiting. `SessionEnd` ends the session.
  - `SubagentStart` and `SubagentStop` bracket subagent rows.
  - Prompt ids and event times stop an older stop marker from ending a newer turn (`claude_events.py`).
- **Not recorded:** prompts, tool inputs or outputs, full paths and chat titles. Titles come from the local transcript, for display only.
- **Process associations:** when the Claude process can be identified, a hook event also records it as (pid, creation time), with first and last seen times. This is what lets the process scan match a `codex.exe` back to a Claude session. A session that hasn't fired any hook yet can't be matched.

## Linking Codex jobs to a Claude chat

There are two ways to link (`agent_links.py`). Both are display-only: they never mark a row as running and never grant collection.

### Process ancestry (for `codex exec resume`)

When it has new or unverified live runs to check, Branchglance looks at the current user's `codex.exe` processes and links one when all of these hold:

1. Its command line parses as `codex exec resume <thread-uuid>`, with the uuid as the argument right after `resume` (`parse_exec_invocation`). Prompt text is never used as an identity, even if it contains a uuid.
2. Walking its parent processes reaches a `claude.exe` within 16 steps. Every parent must be older than its child, and none can be missing, reused or owned by another user.
3. Exactly one Claude session was associated with that Claude process when the Codex process started.

**What this means for clients:**
- Run `codex exec resume <thread-uuid> …` directly, or through wrappers (`cmd`, PowerShell, `codex.cmd`) that stay in the same process tree.
- Detaching the job breaks the chain: `Start-Process` through Explorer, a scheduled task or a service all lose the ancestry.
- Wrapping the uuid in your own argument (for example `script.ps1 -Thread <uuid>`) is fine, as long as the final `codex.exe` command line is `exec resume <uuid>`.

### Link receipts (for new `codex exec` runs)

A new `codex exec` has no thread id on its command line, so ancestry can't identify it. Once you know the thread id (from the `thread.started` event in `codex exec --json` output), call:

```
"%LOCALAPPDATA%\Programs\Branchglance\Branchglance.exe" --link-codex <thread-uuid> --codex-pid <codex.exe pid>
```

**The helper** (`write_receipt`):
- Reads `CLAUDE_CODE_SESSION_ID` from its own environment, so call it from inside the Claude Code session.
- Checks the pid is a live `codex.exe` owned by the same user. For a resume, the command line must name the same thread.
- Records the process creation time, so the receipt binds to that run and not a later one that reuses the pid.
- Appends one bounded line to `%LOCALAPPDATA%\Branchglance\codex-link-receipts\receipts.jsonl`. Only the running widget reads that file, and only while both providers allow collection.
- Always exits with code 0, even when no receipt was written. Don't treat the exit code as confirmation.

Call it while the Codex process is alive. A receipt can't be added for a run that has already ended.

> **Known issue (1.0.0):** from a Claude desktop app session, AppData is redirected into Claude's app package. The launcher handles `--link-codex` like a normal launch: it notices the redirection and relaunches itself through File Explorer, which drops `CLAUDE_CODE_SESSION_ID`. So the receipt is never written. Hooks are unaffected because `--claude-hook` has its own launch path. Until this is fixed, rely on ancestry: jobs resumed with `codex exec resume` from the session still link.

### Why a job shows up as its own parent

- It's a new `codex exec` with no receipt, or the receipt failed (see the known issue above).
- The Claude session hadn't fired a hook yet, or Claude collection is off.
- Either provider's `collect` policy is off.
- The process chain to `claude.exe` is broken or more than 16 steps long.
- The thread had a newer turn that wasn't linked (for example, it was resumed from a plain terminal). That cancels the older parent.
- Two runs of the same thread claimed different Claude sessions at the same time.
- It was started by another client (another Claude session or a tool) that doesn't leave this evidence.

## Codex subagents

Codex writes the parent of a spawned thread itself (`thread_spawn` records and `parent_thread_id` in session metadata). Branchglance joins those to build the subtree. Clients don't need to do anything. Missing, conflicting or stale lineage is filtered rather than guessed.

## Titles

Branchglance shows the Codex thread name, or the first prompt line when there's no name. It tidies brief-style prefixes (`_tidy_codex_title`):

- A leading Markdown heading marker (`# `) and one bracketed tag of up to 40 characters (`[Project] `) are dropped.
- A leading word followed by ` · ` is dropped when that word is part of the model name. For example, `Terra · ` is dropped for `gpt-6.1-terra`.
- Anything else is kept, including `#123`, `#include` and short words that aren't part of the model name.

**Best practice:** make the first line of a brief the task itself in a few words (`Perf attribution`). Put the goal on the next line. Don't repeat the project or model name, since the row already shows workspace and model.

## Worked example: a resume script

A PowerShell launcher that resumes a Codex thread from inside a Claude Code session:

```powershell
param([string]$Thread)
$codex = "$HOME\.claude\tools\codex\node_modules\.bin\codex.cmd"
$link  = "$env:LOCALAPPDATA\Programs\Branchglance\Branchglance.exe"

# Ancestry already links a resume. A receipt is a backup in case the chain is
# broken. Write it once codex.exe is running.
Start-Process powershell -WindowStyle Hidden -ArgumentList '-NoProfile', '-Command', @"
for (`$i = 0; `$i -lt 60; `$i++) {
  `$p = Get-CimInstance Win32_Process -Filter "Name='codex.exe'" |
       Where-Object { `$_.CommandLine -match 'exec resume $Thread' } | Select-Object -First 1
  if (`$p) { & '$link' --link-codex $Thread --codex-pid `$p.ProcessId; break }
  Start-Sleep -Seconds 2
}
"@

Get-Content brief.md -Raw | & $codex exec resume $Thread --json -
```

- The background helper inherits `CLAUDE_CODE_SESSION_ID`, because it's started from the same session.
- The example takes the first `codex.exe` whose command line names the thread. If you can run the same thread twice at once, capture the pid of the process you started instead.
- For a new `codex exec`, read the thread id from the first `thread.started` line of the `--json` output instead, then make the same call.

## Client-side requirements log

Running list of things clients must do, or must avoid, that we found in practice. Add new findings here.

- **2026-10-28.** Jobs launched by our orchestration scripts showed as separate parents. New `codex exec` runs had no receipt. Resumed runs linked only when the ancestry held. Fix: write receipts from the launcher (see the worked example). Product fix pending for the MSIX receipt issue above.
- **2026-10-28.** Brief titles like `# [Orchestrator] Terra · row layout` showed raw on the live view. Fixed in the product (tidying now applies on the live and indexed paths too). Clients should still put the task first in titles.
- **2026-10-28.** `CLAUDE_CONFIG_DIR` changes after enabling collection leave hooks in the old folder. Enable collection again after switching, and check the old folder for leftover hooks. Since 942f13f, enabling records the exact settings file, and turning tracking off or uninstalling edits only that file.
