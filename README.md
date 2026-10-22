# Orchestrator

Orchestrator is a compact Windows desktop companion for seeing what Codex chats
and subagents are doing. It provides orchestration visibility; it is not a full
agent execution harness.

## What it shows

- Active Codex chats in a parent-and-child tree; Claude Code chats and observed
  subagents when the user explicitly enables local hook collection.
- A Codex | Claude source switch for the same Runway view.
- Observed model, status, current tool activity and elapsed time.
- Context occupancy and cache details from the latest request when available.
- Pending attention, with a shortcut to the exact Codex chat.
- Five-hour and weekly quota data and reset countdowns when available.
- Claude Code 31-day usage totals, cache, activity and per-model usage when
  Claude collection is enabled. Real-session subagent coverage is unverified.
- A compact hover widget with drag, lock, monitor and recenter controls.
- Locally read Codex session, transcript and application-state metadata, plus
  consent-gated Claude Code hook events and local transcript data.

## Status and limitations

This is an unpublished release candidate. Source features are implemented; native
UI acceptance is pending. Orchestrator is Windows-focused. Codex monitoring is
the default; Claude Code hook collection requires explicit user consent. It does not
provide direct structured replies or arbitrary agent control. Figma
synchronization and analytics are deferred.

The source bundle retains experimental phone modules, assets and tests, plus the
optional `pywebpush` dependency after the `# Phone Web Push` marker. The desktop
installer reads only dependencies before that marker and does not launch the
phone service. Phone support is not part of the desktop distribution.

Orchestrator is licensed under MIT; see [LICENSE](LICENSE). Bundled fonts retain
their separate SIL Open Font License notices.

## Setup

Use Python 3.10 or newer on Windows. From this directory, run
`scripts/install_token_tracker.ps1` to create `.venv`, install the pinned
dependencies, and create the desktop shortcut. The optional `-StartAtSignIn`
switch registers the sign-in task.

To run the local server directly, use `scripts/start_token_tracker.ps1`. The
desktop widget can be launched with `.venv\Scripts\pythonw.exe -m
token_tracker.widget`.

The source tests are under `tests/`. Install the existing pinned pytest
dependency with `.venv\Scripts\python.exe -m pip install pytest==8.4.2`, then
run them with `.venv\Scripts\python.exe -m pytest`.

## Advanced quota actions

The standalone Reset Action Settings page is separate from the default widget
controls. Automatic greetings and one-time continuations are **off by default**.
To enable them, set this environment option before launching the widget:

```powershell
$env:TOKEN_TRACKER_LIMIT_RESET = "1"
.\.venv\Scripts\pythonw.exe -m token_tracker.widget
```

Only `1`, `true`, or `on` enable the option; missing, unknown and off values leave
it disabled. Existing history or a saved continuation selection does not enable it.
These defaults do not migrate another installation. The settings page can keep the
greeting or select existing Codex chats for a one-time `continue` at the next
eligible quota-availability cycle. Only freshly verified idle chats are eligible;
busy, compacting, waiting-for-input, archived or unverified chats are skipped.
A `queued` result confirms that a message was accepted for the next turn, not that
work started or finished. This is not a direct structured-reply feature or an
allowance reset API, and it does not purchase or redeem an allowance.

## Privacy and provider access

- Monitoring reads local Codex session/transcript and application-state metadata.
  Claude Code collection is off until the user explicitly enables it. When
  enabled, local Claude Code hooks record allowlisted events; the reducer uses
  those events and local transcripts to build observed chat and subagent rows.
  Subagent coverage depends on reported hook events and is unverified on real
  sessions. Hook event files do not persist chat titles. These local sources can
  contain private chat text, workspace paths and identifiers.
- The Claude hook installer backs up settings before changes and manages only
  Branchglance-owned hook entries. No Claude hook data is collected before
  consent is enabled.
- Provider usage checks and enabled auto-greetings can contact the corresponding
  provider through the installed CLI or account integration. Auto-greeting submits
  a small model request; it is not an offline-only feature or an allowance-reset API.
- By default, runtime state and logs use the per-user application-data location,
  outside this source checkout. Do not commit session stores, credentials, runtime
  logs, local databases, environment files or recordings.
- The previous candidate's Git history was not copied because it contains personal
  chat identifiers. This root contains no imported Git history. Do not import the
  previous history into a public repository; publication still requires a final
  history review and explicit authorization.
- The desktop server supports only `127.0.0.1` and `localhost`, which is
  canonicalized to `127.0.0.1`. Other bind addresses are rejected. The experimental
  phone service has a separate deployment/authentication policy; do not treat
  desktop loopback serving as authenticated remote access.
- Runway, the standalone index, and reset action settings pages use local font
  assets; these desktop routes do not request Google Fonts. This only covers
  font delivery, not the app's other network dependencies.

<details>
<summary>Bundled font assets and provenance</summary>

## Bundled font assets

Inter, Instrument Sans, Geist, and Red Hat Mono are licensed under SIL OFL 1.1;
their full unmodified licenses are in their respective `token_tracker/static/fonts/*/OFL.txt`
folders. The WOFF2 files below come from the Google Fonts CSS API and are
duplicated in both Token Tracker static roots. The desktop pages load their faces
from `token_tracker/static/fonts/localfonts.css`. The original requests specify
Inter weights 400, 500, 600, 650 and 700; Instrument Sans 400, 500, 600 and 700;
Geist 400, 500 and 600; and Red Hat Mono 500 and 600. Downloaded files expose
only a `wght` axis: Inter 100–900, Instrument Sans 400–700, Geist 100–900, and
Red Hat Mono 300–700. Each range covers the original requested weights.

| File | Google Fonts CDN source | SHA-256 |
| --- | --- | --- |
| `inter/Inter-cyrillic-ext.woff2` | `https://fonts.gstatic.com/s/inter/v20/UcC73FwrK3iLTeHuS_nVMrMxCp50SjIa2JL7SUc.woff2` | `ca157063339ac4ad418f214f3abfed119b0798ab4d377386ce5c9e5a7a435ebd` |
| `inter/Inter-cyrillic.woff2` | `https://fonts.gstatic.com/s/inter/v20/UcC73FwrK3iLTeHuS_nVMrMxCp50SjIa0ZL7SUc.woff2` | `71d5ee93cc1e9f1d520a3a8b66456de18c7879d8df09d57fcd2eaff75fef0075` |
| `inter/Inter-greek-ext.woff2` | `https://fonts.gstatic.com/s/inter/v20/UcC73FwrK3iLTeHuS_nVMrMxCp50SjIa2ZL7SUc.woff2` | `6e9e020a25f9b56d418f2c085b1d3c09725a4da23fe693a5b463064606732190` |
| `inter/Inter-greek.woff2` | `https://fonts.gstatic.com/s/inter/v20/UcC73FwrK3iLTeHuS_nVMrMxCp50SjIa1pL7SUc.woff2` | `1be3448e292fbf05ffe176fe1e43f135013d50b1e7d324ad1a558f623d3bb6f6` |
| `inter/Inter-vietnamese.woff2` | `https://fonts.gstatic.com/s/inter/v20/UcC73FwrK3iLTeHuS_nVMrMxCp50SjIa2pL7SUc.woff2` | `5c66f9e07e90c6d4ac4922cc68d60de26c17b1858e677fb5e603fce3952b3ff2` |
| `inter/Inter-latin-ext.woff2` | `https://fonts.gstatic.com/s/inter/v20/UcC73FwrK3iLTeHuS_nVMrMxCp50SjIa25L7SUc.woff2` | `34b9c504cab7a73e37b746343a449132e56cf7b5481af2cb81dc74dcff25c956` |
| `inter/Inter-latin.woff2` | `https://fonts.gstatic.com/s/inter/v20/UcC73FwrK3iLTeHuS_nVMrMxCp50SjIa1ZL7.woff2` | `3100e775e8616cd2611beecfa23a4263d7037586789b43f035236a2e6fbd4c62` |
| `instrument-sans/InstrumentSans-latin-ext.woff2` | `https://fonts.gstatic.com/s/instrumentsans/v4/pxiTypc9vsFDm051Uf6KVwgkfoSxQ0GsQv8ToedPibnr0She1YmV.woff2` | `c4fcfea41f2c1cfeea9211fa43679845454a1d0e0d7e95e069c7e73c4ae302d2` |
| `instrument-sans/InstrumentSans-latin.woff2` | `https://fonts.gstatic.com/s/instrumentsans/v4/pxiTypc9vsFDm051Uf6KVwgkfoSxQ0GsQv8ToedPibnr0SZe1Q.woff2` | `2ee17598a98d8a59e4df8152d015bec9ab8e4d5672cc0ab42bef806b568e3971` |
| `geist/Geist-cyrillic-ext.woff2` | `https://fonts.gstatic.com/s/geist/v5/gyByhwUxId8gMEwRGFWfOw.woff2` | `2317fa4bb293c9c0b110e18315d529235c47a0ddd3338cea3d8c7955e927899e` |
| `geist/Geist-cyrillic.woff2` | `https://fonts.gstatic.com/s/geist/v5/gyByhwUxId8gMEwYGFWfOw.woff2` | `6894439694946a589d157ece003086960a6a4013d74a813dab7602efdb3d8c09` |
| `geist/Geist-latin-ext.woff2` | `https://fonts.gstatic.com/s/geist/v5/gyByhwUxId8gMEwSGFWfOw.woff2` | `824f485b5d26e2f2da3c2b236132ece1bc8e4e43373452950bb0e40548b4313f` |
| `geist/Geist-latin.woff2` | `https://fonts.gstatic.com/s/geist/v5/gyByhwUxId8gMEwcGFU.woff2` | `19f9c92546aa300c312235e3125af1b81394d8db9a4bc4a425cd5b641d2d54e1` |
| `geist/Geist-vietnamese.woff2` | `https://fonts.gstatic.com/s/geist/v5/gyByhwUxId8gMEwTGFWfOw.woff2` | `8fa40e5d248247735eb97a0bd593b8852440430600d6ba01364c31fe0abc1fe1` |
| `red-hat-mono/RedHatMono-latin-ext.woff2` | `https://fonts.gstatic.com/s/redhatmono/v16/jVyN7nDnA2uf2zVvFAhhzEsUVuxSzgw.woff2` | `772e09e0b85fae466ecf6f2e337b856e6aa0a7b68447dfb91341ed7419353a52` |
| `red-hat-mono/RedHatMono-latin.woff2` | `https://fonts.gstatic.com/s/redhatmono/v16/jVyN7nDnA2uf2zVvFAhhzEsUWOxS.woff2` | `f4bef0a36dbd4f8a34ce60ba67af24babb1334de1637c0f2680e2e18c112c2b3` |

Original Google Fonts CSS requests: [`Inter`](https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;650;700&display=swap), [`Instrument Sans`](https://fonts.googleapis.com/css2?family=Instrument+Sans:wght@400;500;600;700&display=swap), and [`Geist` with `Red Hat Mono`](https://fonts.googleapis.com/css2?family=Geist:wght@400;500;600&family=Red+Hat+Mono:wght@500;600&display=swap). Official license copies: [Inter](https://raw.githubusercontent.com/google/fonts/main/ofl/inter/OFL.txt), [Instrument Sans](https://raw.githubusercontent.com/google/fonts/main/ofl/instrumentsans/OFL.txt), [Geist](https://raw.githubusercontent.com/google/fonts/main/ofl/geist/OFL.txt), and [Red Hat Mono](https://raw.githubusercontent.com/google/fonts/main/ofl/redhatmono/OFL.txt).

</details>
