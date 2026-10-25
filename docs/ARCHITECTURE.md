# Architecture

Branchglance is a Windows desktop widget with a Python data and HTTP backend, a pywebview host, and a static vanilla JavaScript interface. `token_tracker/widget.py` starts the window and local servers; `token_tracker/server.py` owns API routes and snapshot caches; `token_tracker/providers.py` builds provider snapshots; `token_tracker/static/` renders them.

## Data flow

1. Provider adapters read local provider data and produce normalized rows, quota meters, usage summaries and provenance fields.
2. `providers.build_snapshot()` combines the enabled providers into a snapshot. `SnapshotCache` in `server.py` serves a compatible persisted snapshot while a fresh build runs and keeps live state in a separate cache.
3. The widget embeds the local `runway.html` view. `app.js` fetches `/api/stats`, `/api/live`, `/api/today` and supporting routes; it renders provider trees, meters, activity, settings and analytics. The server binds to loopback and also exposes a separate authenticated control channel used by the Windows installer and launcher.

## Codex provider

`CodexAppServer` in `token_tracker/codex_client.py` resolves the local Codex executable, starts `codex app-server --stdio`, and requests thread lists, thread details and rate-limit observations. `CodexProvider` in `providers.py` joins app-server rows with the local Codex state database and session rollout JSONL files. The rollout reducers extract token/context samples, current execution facts, activity summaries and session insights; they do not treat old transcript activity as proof of a current run. Execution status, source, observation time and terminal time are kept separately from older activity/status fields.

Agent lineage is recovered from session metadata and `thread_spawn` records, including `parent_thread_id`; catalog identities and rollout paths are joined to build the parent/child tree. Missing, conflicting or stale lineage is filtered rather than guessed. Session insight reduction adds bounded model, tool, compaction and agent-message metadata where observed.

Codex data roots are discovered from the user's Codex installation and home directory by `providers.py` and `codex_client.py`; local session JSONL and `state_*.sqlite` are read-only provider inputs. The app-server integration uses the user's existing Codex sign-in. Branchglance does not provide a separate Codex account or transcript sync service.

## Claude providers

Claude data is split across three inputs:

- `ClaudeCodeProvider` in `claude_provider.py` reads `~/.claude/projects` transcripts for identity, titles, usage and context.
- With explicit consent, `claude_collection.py` installs Branchglance-owned hooks in `~/.claude/settings.json`. The launcher or Python hook receives allowlisted Claude Code events and appends bounded records to the Branchglance `claude-events` JSONL directory. Event records omit prompts, tool inputs/outputs, full paths and chat titles.
- `ClaudeDesktopProvider` in `providers.py` reads Claude Desktop's local quota cache. This is quota input; it is not Claude Code transcript state.

`claude_events.py` reduces hook JSONL into execution and wait state. `Stop` and `StopFailure` are turn-scoped terminal markers; `SubagentStart` and `SubagentStop` delimit subagent activity. Prompt identifiers and event times prevent an older stop marker or notification from ending a newer turn. `claude_provider.py` joins hook state with transcript rows; if hook evidence is missing, execution can be unavailable rather than inferred from silence. Hook collection and display are disabled until the user enables Claude Code collection.

## Limits and reset actions

Codex and Claude expose five-hour and weekly usage meters when their local sources provide valid observations. The UI displays observed percentages and reset times; a countdown reaching zero is not itself confirmation of a reset. Live and historical freshness are tracked separately.

`LimitResetService` samples quota state and coordinates optional actions. `reset_actions.py` stores a versioned, one-shot choice: a greeting or an explicitly selected Codex continuation. Actions are disabled unless policy permits them; fresh reset/state checks and durable delivery claims prevent repeats. Greetings invoke the provider CLI only when explicitly enabled. A queued continuation is distinct from answering a question or proving work started.

## Widget, notch modes and window management

The frontend offers compact idle/rest/control modes and larger inspection/dashboard views. `widget.py` owns placement, lock and keep-open state, drag handling, monitor choice, recentering, size transitions, DPI conversion and the native window region. Placement and monitor identities are persisted locally. A global inspection shortcut and tray menu are also registered by the Windows host.

The host is a frameless, transparent, resizable, always-on-top pywebview window with a loopback URL. A child settings window is used for reset-action configuration. These window behaviours currently rely on Windows-specific APIs and event semantics.

## Phone companion status

`phone_server.py` is a separate, explicitly launched loopback service serving the phone web app. It supports pairing, authenticated phone state, limited roster refresh and optional Web Push subscriptions; `phone_push.py` and `phone_events.py` handle notification polling and delivery. The desktop widget does not launch this service. The phone feature and push path exist in source but require separate configuration and user action; they are not a hosted service.

## Packaging and lifecycle

`packaging/build.py` creates the Windows app release payload, native launcher, setup and uninstall executables from PyInstaller specs. `scripts/branchglance_installer/` stages verified releases, manages owned shortcuts and integrations, performs install/uninstall/rollback, and preserves user data. `scripts/launcher/Branchglance.cs` is the stable launcher used across versioned app releases and Claude hook invocation. `scripts/cutover_branchglance.py` contains the separate legacy-install cutover path. The PowerShell scripts at `scripts/` also include the older source-checkout installer and starter.

There is no in-app network updater described here: update and rollback are handled through the release payload and installer lifecycle. Setup, uninstall and cutover code are Windows-specific.

## Storage

`token_tracker/product.py` centralizes product paths. A frozen Windows build stores application state under `%LOCALAPPDATA%\Branchglance\state`, with other product-owned files below `%LOCALAPPDATA%\Branchglance`. Development uses the legacy `%LOCALAPPDATA%\TokenTracker` state root unless an explicit test/development override is supplied. Claude hook consent and `claude-events` live in the stable product data root so hooks can write outside the frozen app process. The phone companion has a separate phone-state file. WebView storage is placed under product data; legacy pywebview storage is under `%APPDATA%\pywebview` and is considered during migration/redirect checks.

Provider input remains in provider-owned locations: Codex home/session/state files, Claude Code's `~/.claude` tree, and Claude Desktop's local cache. Uninstall preserves Branchglance user data.

## Tests

`tests/test_*.py` covers providers, JSONL reducers, cache freshness, server routes, policy and reset delivery, Claude hook installation/collection, phone service, widget/window behaviour and installer lifecycle. Installer tests use temporary directories and fake native adapters where possible; some compile or inspect Windows-only components. `tests/*.mjs` exercises the static UI and its polling, rendering, consent and layout logic with Node's built-in test/assertion APIs. There is no npm package manifest or build step for the frontend.
