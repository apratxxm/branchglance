# Branchglance walkthrough

A guide to every part of Branchglance and how it fits a day of running Codex and Claude Code in parallel. It is written for power users: people who keep several chats going at once, delegate to subagents, and hit usage limits.

Behaviour below matches release 1.0.0. Where Branchglance only reports what it has observed, the guide says so.

## The idea

When work is spread across many chats and subagents, checking progress means opening each one. Branchglance keeps a small widget at the top of your screen and answers three questions at a glance:

1. **What is running?** Every Codex chat and its subagents, as a tree.
2. **What needs me?** Chats waiting for an answer are pulled to the top, with a shortcut to the exact chat.
3. **How much room do I have?** Five-hour and weekly limits and their reset times, for the provider you are looking at.

It reads local data only. It never answers for you, and never starts or stops work on its own. The one exception is reset actions, which you have to switch on yourself (see [Reset actions](#reset-actions)).

## A day with Branchglance

**Morning.** Open Branchglance from the Start menu, or let it start at sign-in. The collapsed widget shows the clock, live chat counts and both limit bands. If it was open before, the last snapshot appears immediately with a stale marker while fresh data loads. Stale data never shows live statuses or current limits.

**Starting work.** Kick off a few Codex chats and let them spawn workers. Each root chat appears with its subagents nested under it. Codex jobs launched from a Claude Code chat nest under that Claude chat.

**Heads-down.** Leave the widget collapsed. When something new starts or a chat asks a question, the widget can expand and bring that provider into view, so you notice without switching windows.

**A chat needs you.** It shows in the **Needs you** strip at the top. Open the question, pick a choice, and Branchglance opens that chat in Codex and copies the choice for you. You answer in Codex.

**Limit check.** Glance at the 5-hour and weekly bands before starting something big. When you are close to a limit, open Insights to see which models and days used it.

**End of the day.** Hide finished chats to keep the tree clean; they are still searchable with `is:hidden`. If you are near the five-hour limit, a reset action can send a greeting or a one-time `continue` to selected chats after the next real reset.

## The widget

### Collapsed

The collapsed widget is 328 px wide at 100% scaling. It is a thin strip with:

- the local time (12- or 24-hour, set in Settings)
- live chat counts
- the **5-hour** limit band, and the **weekly** band beneath it

Hover over the widget, or press the inspection shortcut (**Ctrl+Shift+T** by default), to expand it. Move the pointer away and it collapses again, unless **Keep open** is on.

### Expanded

The toolbar runs left to right:

| Control | What it does |
|---|---|
| Clock | Local time |
| Lock | Pins the widget in place so it can't be dragged |
| Keep open | Stops the widget from collapsing automatically |
| Sync age (`Now`) | Shows how fresh the data is. Click it to refresh |
| Monitor | Moves the widget to the next display. The label shows Laptop or Monitor |
| Insights | Opens the Insights and Settings panel |
| Counts | Live chat counts |
| Search | Opens search. You can also press `/` or `Ctrl+K` |
| Minimize, Fit, Maximize, Close | Window controls. Fit sizes the widget to the screen. Close hides it to the tray |

### Sources and themes

Branchglance shows one provider at a time: **Codex** in blue, **Claude** in orange. Use the Codex | Claude switch to change it. When new work appears for the other provider, the widget can switch to it; a choice you make yourself takes priority for a short while.

### Tray and shortcut

The tray icon menu has **Show / hide**, **Inspection shortcut** (Ctrl+Shift+T, Ctrl+Alt+T or off), **Refresh** and **Quit**. Closing the widget hides it to the tray; it keeps monitoring.

## The chat tree

Each root chat is a row. Its subagents are nested under it, in the order they were spawned. Rows share column tracks, so title, model, activity, workspace and elapsed time line up down the tree. Long text wraps rather than being cut off.

**What a row shows**

- the title, tidied: brief-style prefixes like `# ` and `[Project]`, and a leading word that just repeats the model name, are dropped, so `# [Orchestrator] Release fixes` reads as `Release fixes`
- a status glyph: running (breathing), waiting, idle, failed or finished
- the model, current tool activity and time spent in the current state, when available
- context and cache figures from the last observed request

**Status is only what was observed.** Branchglance shows a status only when it has seen evidence for it. Missing or old evidence is shown as unknown, never as "stopped".

**Lineage is strict.** A subagent is nested under a chat only when its parent link is exact. A Codex job started from a Claude Code session (`codex exec resume <thread>`) nests under that Claude chat only on exact ancestry or a receipt bound to that session and process. Links that conflict, are stale or can't be verified are left out, so you never see a wrong tree. If your own scripts start Codex jobs, see [INTEGRATION.md](INTEGRATION.md) for what they need to do so the jobs nest correctly.

**Row actions** (right-click or the row menu):

- **Pin** keeps a chat at the top.
- **Copy resume** copies the CLI command to resume that chat in a terminal.
- **Hide** removes a chat from the normal list. Nothing is deleted.
- **Open** opens the chat in Codex.

## Needs you

Chats waiting on you collect in the **Needs you** strip above the tree. Click **more** to see them all. A waiting question lists its choices:

- **Open in Codex** opens that chat at its question prompt and copies the choice. You select it in Codex.
- **Queue** saves the choice as a later message, sent after the chat's current turn rather than as the answer to the prompt.

Branchglance never submits a structured answer itself. This keeps you in control when a question needs real judgment.

## Search

Press `/` or `Ctrl+K`, or click the search icon. Plain text matches titles, agent names, models and workspaces. Filters:

| Filter | Shows |
|---|---|
| `is:needs` | chats waiting for input |
| `is:running` | chats running now |
| `is:failed` | failed runs |
| `is:hidden` | chats you've hidden |

Clear the query, or press Escape, to go back to the tree.

## Limits

Each provider has a **5-hour** band and a **weekly** band. Both show percent used, from 0 to 100%. Turning amber at 50% and red at 70%, they give you time to plan before you hit a limit.

- The countdown shows when the provider said the limit resets. Zero on the countdown does not mean the limit has reset. Branchglance waits for a real sample that shows the reset.
- Codex limits come from the local Codex app server. Claude limits come from Claude Desktop's local usage cache.
- The dot next to the Codex 5-hour label shows reset action status. Click it for details.

## Insights

Open **Insights** from the toolbar. It opens as a panel the shape of the widget, between the 5-hour and weekly bands.

- **Range:** 24h, 7d, 30d (default) or All.
- **Metric:** tokens or cost.
- Daily usage, per-model breakdown, cache reads and daily limit peaks (the highest 5-hour and weekly percentages seen each day).
- **Cost** covers only models with a known price, and the models left out are named. A model without a price is never given a guessed one. Cost is an estimate for comparison, not a bill.

Use it to work out what is eating your allowance: a single long chat, a model you didn't mean to use, or low cache reuse.

## Settings

The **Settings** tab sits next to Insights.

- **General:** lock widget position, keep the widget open, 12- or 24-hour clock.
- **Providers:** the show, collect and send flags from your policy file. These are read-only here.
- **Notifications:** reset actions and Claude tracking (see below).
- **Display:** the monitor the widget opens on, and **Recenter**, which brings the widget back to that display.
- **Version:** the installed version and build, for example `1.0.0+63c443c`.

## Reset actions

Off by default. Configure them from Settings or the reset-action status pop-up. Each is a **one-shot** choice for the next genuine reset:

- **Greeting:** sends a short greeting through the provider's own CLI once a real reset is observed. This sends a request to that provider. A greeting does not reset your allowance and does not prove work finished.
- **Continue selected chats once:** queues the exact message `continue` for chats you pick, delivered after the next genuine reset. Use it when you hit the limit mid-task and want work to resume as soon as allowance returns.

Before acting, Branchglance checks again that the reset is real and records each delivery, so an action never runs twice.

## Claude Code

Claude collection is off until you turn it on in **Claude tracking**.

- Turning it on backs up your Claude Code settings file, then adds Branchglance-owned hooks. Your other hooks stay as they are.
- The hooks record event metadata only: event and session ids, model and tool categories, and state. They never record prompts, tool inputs or outputs, full paths or chat titles. Titles are read from local transcripts for display only.
- Branchglance follows `CLAUDE_HOME` and `CLAUDE_CONFIG_DIR` if you set them. Otherwise it uses `~/.claude`.
- Claude rows and subagents appear only when Claude Code reports hook events for them. Coverage across real sessions has not been fully established yet.
- Turning it off removes Branchglance's hooks and revokes consent.

## Phone companion

A separate phone web app with pairing and optional Web Push alerts exists in the source code. The desktop widget never starts it; you have to launch and set it up yourself. It is not a hosted service.

## Install, update and remove

- **Install:** run `BranchglanceSetup.exe` from File Explorer or the Start menu, as your normal user (not as administrator). It installs to `%LOCALAPPDATA%\Programs\Branchglance` and adds Start menu and Desktop shortcuts. It can also start Branchglance at sign-in; that is off unless you choose it, and an upgrade keeps your earlier choice.
- **Update:** run the newer Setup. It installs the new release next to the old one, self-tests it, and only then switches over.
- **Rollback:** `BranchglanceSetup.exe --rollback` restores the previous verified release.
- **Uninstall:** go to Settings → Apps → Installed apps → Branchglance. The uninstaller checks the install first and makes no changes if anything looks wrong. It then revokes Claude consent, backs up Claude settings, removes only Branchglance's hooks, shortcuts and files, and keeps your history under `%LOCALAPPDATA%\Branchglance`.
- **Verify a download:** see [Verify the download](../README.md#verify-the-download).

## Power-user tips

- **Keep the widget collapsed.** The limit bands and counts are enough most of the time, and the widget expands when something needs you.
- **Bind the shortcut.** Ctrl+Shift+T opens the full tree without touching the mouse. If it clashes with another app, switch to Ctrl+Alt+T from the tray menu.
- **Search with filters.** `is:needs` gives you a queue of chats to answer. `is:failed` after a long run shows what to retry.
- **Pin the root chat** you are working in, and **hide** chats that are finished but that you might want later.
- **Use Copy resume** to pick a chat up in a terminal.
- **Plan around the weekly band,** not just the 5-hour one. Check Insights → 7d before a big parallel run.
- **Queue `continue` before a limit hits.** Work then picks up after the reset, even if you are away.
- **Lock the widget** on a multi-monitor setup, and use Recenter after docking or undocking a laptop.

## Limits of what it knows

- Statuses are observed, not inferred. Silence is not shown as idle.
- Reset times come from the provider; a countdown alone proves nothing.
- Cost is estimated from known prices only.
- Claude subagent coverage depends on hook events.
- Windows only for now. See [MAC_PORT.md](MAC_PORT.md) for the macOS inventory.

For how it works inside, see [ARCHITECTURE.md](ARCHITECTURE.md).
