"""Claude Code rows in the Codex row contract, sourced from hook events.

Execution (running / waiting / compacting / terminal) comes only from the hook
event stream reduced by ``claude_events``. Transcripts supply identity, usage,
explicit terminal records and AskUserQuestion text, read at render time and
never persisted. A transcript alone can never produce Running.

Row titles (like Codex, whose titles come from the first prompt) are the
transcript's explicit custom title, else Claude's generated ai-title, with
path-like tokens removed and capped. They are read from the transcript at
render time only and never written to the hook event files.

Every row leaves through ``public_row``, an explicit allowlist: no objective,
preview, descriptions, errors, tool payloads or paths are ever exposed.
"""

from __future__ import annotations

import hashlib
import os
import re
import stat as stat_module
import threading
import time
from array import array
from copy import deepcopy
from datetime import date, datetime
from pathlib import Path
from typing import Any, Callable

from . import claude_events, claude_hook, claude_hook_install

HOOK_COVERAGE_SECONDS = 150
ROW_WINDOW_SECONDS = 24 * 3600
ENDED_WINDOW_SECONDS = 6 * 3600
CHILD_WINDOW_SECONDS = 30 * 60
FALLBACK_LIMIT = 60
_ROWS_TTL = 1.0

PUBLIC_FIELDS = frozenset({
    "id", "title", "model", "workspace", "status", "statusSource", "statusObservedAt",
    "telemetryUnavailable", "metricsUnavailable", "stateSince", "compactionStartedAt", "stateDetail",
    "executionStatus", "executionSource", "executionObservedAt", "executionSince",
    "executionCompactionStartedAt", "executionTerminalAt", "executionActivity", "activity",
    "activityUnavailable", "pendingQuestion", "pendingOptions", "pendingQuestionAt", "pendingQuestionId",
    "contextTokens", "contextWindow", "contextPercent", "contextObservedAt", "contextModel",
    "lastRequest", "tokens", "turns", "usageUpdatedAt", "parentId", "agentName", "updatedAt",
    "recordSource", "lastValidAt", "resumable",
})


def claude_home() -> Path:
    return claude_hook_install.claude_config_dir()


def projects_root() -> Path:
    return claude_home() / "projects"


_PATHLIKE = re.compile(r"""(?:[A-Za-z]:[\\/]|\\\\|~[\\/]|\.{0,2}/)\S*|\S*[\\/]\S*""")


def public_model(value: Any) -> str | None:
    """A model id in Claude's fixed format, else nothing (no free text or paths)."""
    return value if isinstance(value, str) and claude_hook._MODEL.fullmatch(value) else None


def public_agent_name(value: Any) -> str | None:
    return value if isinstance(value, str) and claude_hook._AGENT_TYPE.fullmatch(value) else None


def public_title(value: Any) -> str | None:
    """The explicit custom title with path-like tokens removed (render-time only)."""
    text = claude_hook.clean_text(value, 200)
    if not text:
        return None
    text = re.sub(r"\s+", " ", _PATHLIKE.sub(" ", text)).strip()
    return text[:80] or None


def row_title(transcript: dict[str, Any]) -> str | None:
    """Explicit custom title first, then Claude's generated ai-title."""
    return transcript.get("title") or transcript.get("ai_title")


def public_row(row: dict[str, Any]) -> dict[str, Any]:
    """The only projection of a Claude row that may reach the API."""
    return {key: value for key, value in row.items() if key in PUBLIC_FIELDS}


# --------------------------------------------------------------- transcripts

_PATHS: dict[str, str] = {}
_PATHS_LOCK = threading.Lock()
# Claude rewrites a transcript when it compacts, so for a moment the file has
# no title or model yet. Keep the last ones seen per session/agent.
_IDENTITY: dict[str, tuple[str | None, str | None]] = {}
_IDENTITY_LOCK = threading.Lock()
_IDENTITY_MAX = 512


def _remembered_identity(key: str, title: str | None, model: str | None) -> tuple[str | None, str | None]:
    with _IDENTITY_LOCK:
        known_title, known_model = _IDENTITY.pop(key, (None, None))
        title, model = title or known_title, model or known_model
        _IDENTITY[key] = (title, model)
        while len(_IDENTITY) > _IDENTITY_MAX:
            _IDENTITY.pop(next(iter(_IDENTITY)))
    return title, model


def find_transcript(session_id: str, agent_id: str = "", root: Path | None = None) -> Path | None:
    """Locate a session (or subagent) transcript by id. The location stays internal."""
    base = root or projects_root()
    cache_key = f"{base}|{session_id}|{agent_id}"
    with _PATHS_LOCK:
        cached = _PATHS.get(cache_key)
    if cached and os.path.isfile(cached):
        return Path(cached)
    found: Path | None = None
    try:
        with os.scandir(base) as projects:
            for project in projects:
                if not project.is_dir(follow_symlinks=False):
                    continue
                if agent_id:
                    candidate = Path(project.path) / session_id / "subagents" / f"agent-{agent_id}.jsonl"
                else:
                    candidate = Path(project.path) / f"{session_id}.jsonl"
                if candidate.is_file():
                    found = candidate
                    break
    except OSError:
        return None
    if found is not None:
        with _PATHS_LOCK:
            _PATHS[cache_key] = str(found)
    return found


def _epoch(value: Any) -> float | None:
    from . import providers
    return providers._timestamp_epoch(value)


def _new_transcript_state() -> dict[str, Any]:
    return {"model": None, "usage": None, "usage_ts": None, "cur_id": None, "cur_total": 0, "tokens": 0,
            "turns": 0, "title": None, "ai_title": None, "workspace": None, "last_conv_ts": None, "asks": {}, "open_asks": {},
            "explicit": None, "entrypoint": None, "interrupt_ts": None}


_INTERRUPT_MARKER_PREFIX = "[Request interrupted by user"


def _usage_numbers(usage: Any) -> dict[str, int] | None:
    if not isinstance(usage, dict):
        return None

    def number(name: str) -> int:
        value = usage.get(name)
        return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0

    creation = number("cache_creation_input_tokens")
    nested = usage.get("cache_creation")
    if isinstance(nested, dict):
        creation = max(creation, sum(v for v in nested.values() if isinstance(v, int) and v >= 0))
    return {"input": number("input_tokens"), "cached": number("cache_read_input_tokens"),
            "creation": creation, "output": number("output_tokens")}


def _reduce_transcript(state: dict[str, Any], item: Any) -> None:
    from . import providers
    if not isinstance(item, dict):
        return
    kind = item.get("type")
    stamp = _epoch(item.get("timestamp"))
    if state["workspace"] is None and isinstance(item.get("cwd"), str):
        state["workspace"] = claude_hook.workspace_label(item["cwd"])
    if state["entrypoint"] is None and isinstance(item.get("entrypoint"), str):
        state["entrypoint"] = item["entrypoint"][:16]
    if kind == "custom-title":
        title = public_title(item.get("customTitle"))
        if title:
            state["title"] = title
        return
    if kind == "ai-title":
        title = public_title(item.get("aiTitle"))
        if title:
            state["ai_title"] = title
        return
    if kind == "result":
        if stamp is not None:
            state["explicit"] = ("failed" if item.get("is_error") else "completed", stamp)
        return
    if kind not in {"user", "assistant"}:
        return
    if stamp is not None:
        state["last_conv_ts"] = stamp if state["last_conv_ts"] is None else max(state["last_conv_ts"], stamp)
    message = item.get("message") if isinstance(item.get("message"), dict) else {}
    content = message.get("content")
    blocks = [b for b in content if isinstance(b, dict)] if isinstance(content, list) else []
    if kind == "user":
        state["explicit"] = None
        interrupted = isinstance(content, str) and content.startswith(_INTERRUPT_MARKER_PREFIX)
        if not interrupted:
            interrupted = any(block.get("type") == "text" and isinstance(block.get("text"), str)
                              and block["text"].startswith(_INTERRUPT_MARKER_PREFIX) for block in blocks)
        if interrupted and stamp is not None:
            previous = state.get("interrupt_ts")
            state["interrupt_ts"] = stamp if previous is None else max(previous, stamp)
        for block in blocks:
            if block.get("type") == "tool_result":
                state["open_asks"].pop(block.get("tool_use_id"), None)
        return
    model = message.get("model")
    if isinstance(model, str) and model and not model.startswith("<"):
        state["model"] = model
    numbers = _usage_numbers(message.get("usage"))
    if numbers:
        total = sum(numbers.values())
        identity = message.get("id") if isinstance(message.get("id"), str) else None
        if identity is None or identity != state["cur_id"]:
            state["tokens"] += state["cur_total"]
            state["cur_id"], state["cur_total"] = identity, total
            state["turns"] += 1
        else:
            state["cur_total"] = max(state["cur_total"], total)
        state["usage"], state["usage_ts"] = numbers, stamp
    for block in blocks:
        if block.get("type") == "tool_use" and block.get("name") in {"AskUserQuestion", "ask_user_question"}:
            tuid = block.get("id")
            if isinstance(tuid, str):
                entry = {"q": providers._question_text(block.get("input")),
                         "opts": providers._question_options(block.get("input")), "ts": stamp}
                state["asks"][tuid] = entry
                state["open_asks"][tuid] = stamp
                while len(state["asks"]) > 8:
                    state["asks"].pop(next(iter(state["asks"])))
                while len(state["open_asks"]) > 8:
                    state["open_asks"].pop(next(iter(state["open_asks"])))
    if message.get("stop_reason") in {"end_turn", "stop_sequence"} and stamp is not None and not state["open_asks"]:
        state["explicit"] = ("completed", stamp)


# Only records whose message carries usage counts reach _reduce_usage's state.
_USAGE_NEEDLES = (b'"usage"',)


def transcript_summary(path: Path | None) -> dict[str, Any]:
    if path is None:
        return _new_transcript_state()
    from . import providers
    state = providers._incremental_jsonl_reduce(
        "claude-code-transcript", path, _new_transcript_state, _reduce_transcript,
        unavailable=_new_transcript_state())
    return state if isinstance(state, dict) else _new_transcript_state()


# ---------------------------------------------------------------------- rows

_EXPLICIT = {"completed": "idle", "failed": "failed"}
_STATUS = {"waiting": "needs input", "compacting": "compacting", "running": "running",
           "idle": "idle", "failed": "failed"}


def _context_fields(transcript: dict[str, Any], model: str | None, reset_at: float | None) -> dict[str, Any]:
    from . import providers
    usage, observed = transcript.get("usage"), transcript.get("usage_ts")
    empty = {"contextTokens": None, "contextWindow": None, "contextPercent": None,
             "contextObservedAt": None, "contextModel": None, "lastRequest": None}
    if not usage:
        return empty
    if reset_at is not None and (observed is None or observed < reset_at):
        return empty  # context resets after a compaction until the next recorded request
    tokens = usage["input"] + usage["cached"] + usage["creation"]
    model = public_model(transcript.get("model")) or model
    window = providers._claude_context_window(model) if model else None
    return {
        "contextTokens": tokens, "contextWindow": window,
        "contextPercent": min(100, round(tokens * 100 / window)) if window else None,
        "contextObservedAt": claude_events.iso(observed), "contextModel": model,
        "lastRequest": {"input": tokens, "cached": usage["cached"], "output": usage["output"]},
    }


def _hook_row(entry: dict[str, Any], root_entry: dict[str, Any] | None, now: float,
              projects: Path | None, events_path: str) -> dict[str, Any]:
    state = entry["state"]
    sid, aid = entry["sid"], entry["aid"]
    root_state = root_entry["state"] if root_entry else state
    path = find_transcript(sid, aid, projects)
    transcript = transcript_summary(path)
    execution = {k: entry[k] for k in (
        "executionStatus", "executionSource", "executionObservedAt", "executionSince",
        "executionCompactionStartedAt", "executionTerminalAt", "executionActivity", "stateDetail")}
    status = execution["executionStatus"]
    # Hook coverage: conversation records far newer than the last hook event
    # mean hooks stopped reporting. Fall back without inferring anything.
    last_conv = transcript.get("last_conv_ts")
    last_hook = max(state["last"] or 0, state["sub_activity"] or 0)
    uncovered = (last_conv is not None and last_conv - last_hook > HOOK_COVERAGE_SECONDS
                 and execution["stateDetail"] != "finished")
    asking = [tuid for tuid in transcript.get("open_asks", {}) if tuid in transcript.get("asks", {})]
    if uncovered:
        explicit = transcript.get("explicit")
        execution.update(executionStatus="unavailable", executionSource="unavailable",
                         executionObservedAt=None, executionSince=None, executionCompactionStartedAt=None,
                         executionTerminalAt=None, executionActivity=None, stateDetail=None)
        status = "unavailable"
    title, model = _remembered_identity(f"{sid}:{aid or ''}", None if aid else row_title(transcript),
                                        public_model(transcript.get("model")) or public_model(state.get("model")))
    row: dict[str, Any] = {
        "id": sid if not aid else f"{sid}:{aid}",
        "title": title,
        # Per-agent model facts: a child uses its own transcript or hook model,
        # never the parent's. Process/workspace stay session-wide (root_state).
        "model": model,
        "workspace": root_state.get("workspace") or transcript.get("workspace"),
        "recordSource": "hook",
        "updatedAt": claude_events.iso(max(state["last"] or 0, transcript.get("last_conv_ts") or 0) or None),
        "tokens": (transcript["tokens"] + transcript["cur_total"]) or None,
        "turns": transcript["turns"] or None,
        "usageUpdatedAt": claude_events.iso(transcript.get("usage_ts")),
        **execution,
        "activity": execution["executionActivity"],
        "activityUnavailable": status == "unavailable",
        "pendingQuestion": None, "pendingOptions": None, "pendingQuestionAt": None, "pendingQuestionId": None,
        "compactionStartedAt": execution["executionCompactionStartedAt"],
        "stateSince": execution["executionSince"] or execution["executionTerminalAt"],
        "statusObservedAt": execution["executionObservedAt"],
    }
    # `claude --resume <id>` only makes sense for a CLI-origin root session with a known id.
    row["resumable"] = (not aid) and transcript.get("entrypoint") == "cli"
    if aid:
        row["parentId"] = sid
        row["agentName"] = public_agent_name(state.get("atype"))
    if uncovered:
        explicit = transcript.get("explicit")
        fallback_waiting = bool(asking)
        row.update(status="needs input" if fallback_waiting else _EXPLICIT.get(explicit[0], "idle") if explicit else "unavailable",
                   statusSource="transcript" if (fallback_waiting or explicit) else "unavailable",
                   telemetryUnavailable=not (fallback_waiting or explicit), metricsUnavailable=False)
    else:
        row.update(status=_STATUS.get(status, "unavailable"),
                   statusSource="hook" if status != "unknown" else "unavailable",
                   telemetryUnavailable=status == "unknown", metricsUnavailable=False)
    if (status == "waiting" and entry.get("waitKind") == "ask") or (uncovered and asking):
        ask_id = asking[-1] if uncovered else entry.get("waitToolUseId")
        ask = transcript["asks"].get(ask_id)
        if ask is None and transcript["asks"] and not entry.get("waitToolUseId"):
            ask = list(transcript["asks"].values())[-1]
        if ask:
            row.update(pendingQuestion=ask["q"], pendingOptions=ask["opts"] or None,
                       pendingQuestionAt=claude_events.iso(ask["ts"]),
                       pendingQuestionId=ask_id)
    interrupt_ts = transcript.get("interrupt_ts")
    execution_since = _epoch(execution.get("executionSince"))
    prompt_ts = state.get("prompt")
    run_start = max((ts for ts in (execution_since, prompt_ts) if ts is not None), default=None)
    if status in {"running", "waiting", "compacting"} and interrupt_ts is not None \
            and run_start is not None and interrupt_ts >= run_start:
        stopped_at = claude_events.iso(interrupt_ts)
        execution.update(executionStatus="idle", executionSince=None, executionCompactionStartedAt=None,
                         executionTerminalAt=stopped_at, executionActivity=None, stateDetail="stopped")
        row.update(status="idle", executionStatus="idle", executionSince=None,
                   executionCompactionStartedAt=None, executionTerminalAt=stopped_at,
                   executionActivity=None, activity=None, stateDetail="stopped",
                   compactionStartedAt=None, stateSince=stopped_at,
                   pendingQuestion=None, pendingOptions=None, pendingQuestionAt=None, pendingQuestionId=None)
    row.update(_context_fields(transcript, row["model"], state.get("post_compact")))
    # Internal only (popped before any response): the transcript, or the hook
    # event file when the transcript cannot be found, so live polling still tracks it.
    row["_path"] = str(path) if path else events_path
    return row


def _fallback_row(path: Path, sid: str, mtime: float) -> dict[str, Any] | None:
    transcript = transcript_summary(path)
    if not (transcript["turns"] or row_title(transcript)):
        return None
    explicit = transcript.get("explicit")
    asking = [tuid for tuid in transcript["open_asks"] if tuid in transcript["asks"]]
    row: dict[str, Any] = {
        "id": sid, "title": row_title(transcript), "model": public_model(transcript.get("model")),
        "workspace": transcript.get("workspace"), "recordSource": "transcript",
        "updatedAt": claude_events.iso(max(mtime, transcript.get("last_conv_ts") or 0)),
        "tokens": (transcript["tokens"] + transcript["cur_total"]) or None,
        "turns": transcript["turns"] or None,
        "usageUpdatedAt": claude_events.iso(transcript.get("usage_ts")),
        "resumable": transcript.get("entrypoint") == "cli",
        "executionStatus": "unavailable", "executionSource": "unavailable", "executionObservedAt": None,
        "executionSince": None, "executionCompactionStartedAt": None, "executionTerminalAt": None,
        "executionActivity": None, "activity": None, "activityUnavailable": True, "stateDetail": None,
        "compactionStartedAt": None, "stateSince": None, "statusObservedAt": None, "metricsUnavailable": False,
        "pendingQuestion": None, "pendingOptions": None, "pendingQuestionAt": None, "pendingQuestionId": None,
    }
    if asking:
        ask = transcript["asks"][asking[-1]]
        row.update(status="needs input", statusSource="transcript", telemetryUnavailable=False,
                   pendingQuestion=ask["q"], pendingOptions=ask["opts"] or None,
                   pendingQuestionAt=claude_events.iso(ask["ts"]), pendingQuestionId=asking[-1])
    elif explicit:
        row.update(status=_EXPLICIT.get(explicit[0], "idle"), statusSource="transcript", telemetryUnavailable=False,
                   executionTerminalAt=None, stateDetail="completed" if explicit[0] == "completed" else None)
    else:
        row.update(status="unavailable", statusSource="unavailable", telemetryUnavailable=True)
    row.update(_context_fields(transcript, row["model"], None))
    row["_path"] = str(path)
    return row


def build_rows(now: float | None = None,
               alive_fn: Callable[[int | None, int | None], bool | None] | None = None,
               events_directory: str | os.PathLike[str] | None = None,
               projects: Path | None = None) -> list[dict[str, Any]]:
    """All Claude Code rows (roots and subagents), newest first, internal `_path` included."""
    now = time.time() if now is None else now
    states = claude_events.load_states(now, events_directory)
    entries = claude_events.project_all(states, alive_fn or claude_events.default_alive)
    events_path = str(Path(events_directory if events_directory is not None else claude_hook.events_dir())
                      / claude_events.day_names(now)[1])
    roots = {e["sid"]: e for e in entries if not e["aid"]}
    rows: list[dict[str, Any]] = []
    for entry in entries:
        state = entry["state"]
        age = now - (state["last"] or 0)
        root_entry = roots.get(entry["sid"])
        if entry["aid"]:
            if root_entry is None or now - (root_entry["state"]["last"] or 0) > ROW_WINDOW_SECONDS:
                continue
            if entry["executionStatus"] not in {"running", "waiting"} and age > CHILD_WINDOW_SECONDS:
                continue
        else:
            if age > ROW_WINDOW_SECONDS:
                continue
            if entry["stateDetail"] == "finished" and age > ENDED_WINDOW_SECONDS:
                continue
        rows.append(_hook_row(entry, root_entry, now, projects, events_path))
    covered = set(roots)
    base = projects or projects_root()
    fallback: list[tuple[float, Path, str]] = []
    try:
        with os.scandir(base) as scanned:
            for project in scanned:
                if not project.is_dir(follow_symlinks=False):
                    continue
                with os.scandir(project.path) as files:
                    for item in files:
                        if not item.name.endswith(".jsonl") or not item.is_file(follow_symlinks=False):
                            continue
                        sid = item.name[:-6]
                        if sid in covered:
                            continue
                        mtime = item.stat().st_mtime
                        if now - mtime <= ROW_WINDOW_SECONDS:
                            fallback.append((mtime, Path(item.path), sid))
    except OSError:
        pass
    for mtime, path, sid in sorted(fallback, reverse=True)[:FALLBACK_LIMIT]:
        row = _fallback_row(path, sid, mtime)
        if row:
            rows.append(row)
    rows.sort(key=lambda r: r.get("updatedAt") or "", reverse=True)
    return rows


# ------------------------------------------------------- 31-day aggregates

# Per-file usage, reduced incrementally (append-aware, checkpointed by the
# shared reader) into packed arrays so checkpoints stay small: one entry per
# response id with its local day, model and five token counts. No text kept.
TRACKING_DAYS = 31
# v2 adds per-response epochs; older checkpoints lack them and are rebuilt.
_USAGE_KIND = "claude-code-usage-v2"
_RECENT_IDS = 16


def _new_usage_state() -> dict[str, Any]:
    return {"ids": array("Q"), "days": array("I"), "models": array("H"), "values": array("Q"),
            "epochs": array("q"),
            "model_names": [], "recent": {}}


def _usage_values(usage: dict[str, Any]) -> tuple[int, int, int, int, int]:
    def number(source: Any, name: str) -> int:
        value = source.get(name) if isinstance(source, dict) else None
        return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 0

    creation = usage.get("cache_creation")
    if isinstance(creation, dict) and creation:
        write_5m, write_1h = number(creation, "ephemeral_5m_input_tokens"), number(creation, "ephemeral_1h_input_tokens")
    else:
        write_5m, write_1h = number(usage, "cache_creation_input_tokens"), 0
    return (number(usage, "input_tokens"), number(usage, "output_tokens"),
            number(usage, "cache_read_input_tokens"), write_5m, write_1h)


def _reduce_usage(state: dict[str, Any], item: Any) -> None:
    from . import providers
    if not isinstance(item, dict):
        return
    message = item.get("message") if isinstance(item.get("message"), dict) else {}
    usage = message.get("usage") if isinstance(message.get("usage"), dict) else None
    if not usage:
        return
    values = _usage_values(usage)
    if not any(values):
        return
    day = providers._local_date_from_timestamp(item.get("timestamp"))
    if not day:
        return
    identity = message.get("id") or item.get("requestId") or f"{item.get('timestamp')}|{values}"
    key = int.from_bytes(hashlib.blake2b(str(identity).encode("utf-8", "replace"), digest_size=8).digest(), "little")
    model = public_model(message.get("model")) or "unknown"
    names = state["model_names"]
    if model not in names:
        names.append(model)
    model_index = names.index(model)
    recent = state["recent"]
    index = recent.get(key)
    if index is not None:
        # One response is written once per content block; keep the most complete counts.
        base = index * 5
        for offset, value in enumerate(values):
            state["values"][base + offset] = max(state["values"][base + offset], value)
        return
    state["ids"].append(key)
    state["days"].append(date.fromisoformat(day).toordinal())
    state["models"].append(model_index)
    stamp = providers._timestamp_epoch(item.get("timestamp")) if item.get("timestamp") else None
    state["epochs"].append(int(stamp) if stamp else 0)
    state["values"].extend(values)
    recent[key] = len(state["ids"]) - 1
    while len(recent) > _RECENT_IDS:
        recent.pop(next(iter(recent)))


def _usage_files(base: Path, cutoff: float) -> list[tuple[float, str, Path]]:
    from . import providers
    found: list[tuple[float, str, Path]] = []
    try:
        candidates = list(base.rglob("*.jsonl"))
    except OSError:
        return found
    for path in candidates:
        try:
            info = path.lstat()
        except OSError:
            continue
        if not stat_module.S_ISREG(info.st_mode) or info.st_mtime < cutoff:
            continue  # a file last written before the window holds no in-window usage
        found.append((providers._created_at(info), str(path), path))
    found.sort()
    return found


def _insight_add(bucket: dict[str, dict[str, int]], model: str, record: dict[str, int]) -> None:
    target = bucket.setdefault(model, {})
    for name, value in record.items():
        target[name] = target.get(name, 0) + value


def _local_hour_key(epoch: int, tz: Any, cache: dict[int, str]) -> str:
    """Local clock-hour start as ISO text, the same key shape the Insights reader uses."""
    minute = epoch // 60
    key = cache.get(minute)
    if key is None:
        key = datetime.fromtimestamp(epoch, tz).replace(minute=0, second=0, microsecond=0).isoformat()
        cache[minute] = key
    return key


def usage_aggregates(now: float | None = None, root: Path | None = None,
                     days: int = TRACKING_DAYS) -> dict[str, Any]:
    """Provider-level 31-day totals, cache-hit rate, activity and model usage.

    Same shapes and window as the Claude Desktop/Codex providers. Responses
    copied into resumed or forked transcripts are credited once, to the
    earliest-created file, as the Desktop scan did.
    """
    from . import providers
    now = time.time() if now is None else now
    base = root or projects_root()
    today = datetime.fromtimestamp(now, providers._local_timezone()).date()
    window = {date.fromisoformat(d).toordinal() for d in providers._last_calendar_dates(days, today)}
    totals = {"input_tokens": 0, "output_tokens": 0, "cached_tokens": 0, "cache_5m_tokens": 0,
              "cache_1h_tokens": 0, "total_tokens": 0}
    models: dict[str, dict[str, int]] = {}
    model_usage: dict[str, dict[str, int]] = {}
    daily: dict[int, int] = {}
    seen: set[int] = set()
    tz = providers._local_timezone()
    hour_keys: dict[int, str] = {}
    insight_daily: dict[str, dict[str, dict[str, int]]] = {}
    insight_hourly: dict[str, dict[str, dict[str, int]]] = {}
    active_hours: set[str] = set()
    session_activity: dict[str, dict[str, Any]] = {}
    for _created, _name, path in _usage_files(base, now - (days + 1) * 86400):
        state = providers._incremental_jsonl_reduce(
            _USAGE_KIND, path, _new_usage_state, _reduce_usage,
            unavailable=None, needles=_USAGE_NEEDLES)
        if not isinstance(state, dict):
            continue
        subagent = "subagents" in path.parts
        ids, day_list, model_list, values = state["ids"], state["days"], state["models"], state["values"]
        for index, key in enumerate(ids):
            if key in seen:
                continue
            seen.add(key)
            day = day_list[index]
            if day not in window:
                continue
            fields = values[index * 5:index * 5 + 5]
            amount = sum(fields)
            model = state["model_names"][model_list[index]]
            bucket = models.setdefault(model, dict.fromkeys(totals, 0))
            for name, value in zip(("input_tokens", "output_tokens", "cached_tokens", "cache_5m_tokens",
                                    "cache_1h_tokens"), fields):
                totals[name] += value
                bucket[name] += value
            totals["total_tokens"] += amount
            bucket["total_tokens"] += amount
            daily[day] = daily.get(day, 0) + amount
            usage = model_usage.setdefault(model, {"tokens": 0, "observedRequests": 0,
                                                   "subagentTokens": 0, "subagentRequests": 0})
            usage["tokens"] += amount
            usage["observedRequests"] += 1
            if subagent:
                usage["subagentTokens"] += amount
                usage["subagentRequests"] += 1
            record = {"input_tokens": fields[0], "output_tokens": fields[1], "cached_tokens": fields[2],
                      "cache_5m_tokens": fields[3], "cache_1h_tokens": fields[4], "total_tokens": amount}
            _insight_add(insight_daily.setdefault(date.fromordinal(day).isoformat(), {}), model, record)
            epochs = state.get("epochs")
            epoch = epochs[index] if epochs is not None and index < len(epochs) else 0
            if epoch:
                hour = _local_hour_key(epoch, tz, hour_keys)
                _insight_add(insight_hourly.setdefault(hour, {}), model, record)
                active_hours.add(hour)
            session = session_activity.setdefault(
                path.stem, {"id": path.stem, "subagent": subagent, "lastActivityAt": None})
            if epoch and (session["lastActivityAt"] is None or epoch > session["lastActivityAt"]):
                session["lastActivityAt"] = epoch
    denominator = totals["input_tokens"] + totals["cached_tokens"] + totals["cache_5m_tokens"] + totals["cache_1h_tokens"]
    cache = {
        "percent": min(100, round(totals["cached_tokens"] * 100 / denominator)) if denominator else 0,
        "read": totals["cached_tokens"], "uncachedInput": totals["input_tokens"],
        "cacheCreation": totals["cache_5m_tokens"] + totals["cache_1h_tokens"],
        "output": totals["output_tokens"],
        **providers._costs(totals, models, providers.CLAUDE_PRICING, claude=True),
    } if denominator else None
    tracked = totals["total_tokens"]
    return {
        "cache": cache,
        "activity": [{"date": date.fromordinal(day).isoformat(), "value": value} for day, value in sorted(daily.items())],
        "activityNote": "Usage is grouped from recorded Claude Code transcript events in this 31-day window.",
        "modelUsage": [{"model": model, **usage}
                       for model, usage in sorted(model_usage.items(), key=lambda item: -item[1]["tokens"])],
        "modelUsageNote": ("Recorded transcript responses over the last 31 days, each response counted once; "
                           "subagent totals come from subagent transcripts."),
        "_insightsData": {"trackingWindowDays": days, "dailyModels": insight_daily,
                          "hourlyModels": insight_hourly, "activeHours": sorted(active_hours),
                          "sessions": list(session_activity.values())},
        "tracked31Days": tracked,
        "tracked31DaysLabel": providers._human_tokens(tracked),
        "trackingWindowDays": days,
        "lifetimeTokens": tracked,
    }


# --------------------------------------------------------------- integration

_MEMO: dict[str, Any] = {"at": 0.0, "rows": None}
_MEMO_LOCK = threading.Lock()


def reset_memo() -> None:
    with _MEMO_LOCK:
        _MEMO.update(at=0.0, rows=None)
    with _IDENTITY_LOCK:
        _IDENTITY.clear()


def _cached_rows() -> list[dict[str, Any]]:
    with _MEMO_LOCK:
        if _MEMO["rows"] is not None and time.monotonic() - _MEMO["at"] <= _ROWS_TTL:
            return _MEMO["rows"]
    rows = build_rows()
    with _MEMO_LOCK:
        _MEMO.update(at=time.monotonic(), rows=rows)
    return rows


def public_rows(rows: list[dict[str, Any]], keep_path: bool = False) -> list[dict[str, Any]]:
    out = []
    for row in rows:
        item = public_row(row)
        if keep_path and row.get("_path"):
            item["path"] = row["_path"]
        out.append(item)
    return out


def live_row(row: dict[str, Any], indexed: dict[str, Any]) -> dict[str, Any]:
    """Replace a live row's inferred fields with the hook-sourced projection.

    Called for Claude rows by ``providers.live_state``. The legacy transcript
    status (which infers running from any trailing record) is discarded.
    """
    identity = indexed.get("id") or row.get("id")
    match = next((r for r in _cached_rows() if r.get("id") == identity), None)
    keep = {"key", "activityAt"}
    base = {k: v for k, v in row.items() if k in keep}
    if match is None:
        base.update(id=identity, status="unavailable", statusSource="unavailable", telemetryUnavailable=True,
                    metricsUnavailable=False, executionStatus="unavailable", executionSource="unavailable",
                    executionObservedAt=None, executionSince=None, executionCompactionStartedAt=None,
                    executionTerminalAt=None, executionActivity=None, activity=None, activityUnavailable=True)
        return base
    base.update(public_row(match))
    return base


class ClaudeCodeProvider:
    """Full-snapshot provider for Claude: sessions from hooks, quota from Desktop caches."""

    def __init__(self, desktop_factory: Callable[[], Any] | None = None,
                 limits_factory: Callable[[], Any] | None = None) -> None:
        self._desktop_factory = desktop_factory
        self._limits_factory = limits_factory

    def _base(self) -> dict[str, Any] | None:
        from . import providers
        # The limits-only path reads the freshest local quota cache without
        # rescanning Desktop transcripts whose chat rows are discarded here.
        # An explicitly injected factory remains useful to isolated tests.
        factory = self._limits_factory or self._desktop_factory or providers.ClaudeLimitsProvider
        try:
            base = factory().snapshot()
        except Exception:
            return None
        return base if isinstance(base, dict) else None

    def snapshot(self) -> dict[str, Any]:
        base = deepcopy(self._base()) or {
            "connected": True, "name": "Claude", "accent": "#e8813f",
            "fiveHour": {"percent": None, "reset": None, "resetAt": None, "lastKnownResetAt": None},
            "weekly": {"percent": None, "reset": None, "resetAt": None, "lastKnownResetAt": None},
            "cache": None, "activity": [], "limitsUpdatedAt": None,
        }
        base["connected"] = True
        base["name"] = "Claude"
        base.pop("limitsOnly", None)
        # Sessions never come from the Desktop scan: its rows carry objectives,
        # previews and infer running from trailing records.
        base["chats"] = public_rows(build_rows(), keep_path=True)
        try:
            # Provider-level parity fields from the incremental transcript reader.
            base.update(usage_aggregates())
        except Exception:
            pass  # aggregates are advisory; quota and sessions still render
        base["source"] = "Claude Code hooks + transcripts; Claude Desktop quota cache"
        return base
