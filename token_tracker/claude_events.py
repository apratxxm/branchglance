"""Reducer from Claude Code hook events to per-session execution state.

Input is the allowlisted event stream written by ``claude_hook``. State is keyed
by (session_id, agent_id or ""). Every field is a max-timestamp so the result
does not depend on arrival order: async hooks can be written out of order, and
a PostToolUse may be seen before its PreToolUse.

Nothing here infers that a session is running. "running" requires a
UserPromptSubmit later than every terminal event, liveness of the recorded
Claude process, and (at the UI) an event within the single 150 s bound.
"""

from __future__ import annotations

import math
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from . import claude_hook

TERMINAL_KINDS = ("failed", "finished", "stopped", "completed")  # tie priority, high to low
_TOOL_LIMIT = 128
_TURN_LIMIT = 16
_ACTIVITY = {
    "read": "reading", "edit": "editing", "search": "searching", "command": "running_command",
    "web_search": "searching_web", "web_fetch": "inspecting", "agent": "coordinating", "ask": "waiting",
    "mcp": "using_tool", "other": "using_tool",
}
_LABELS = {
    "reading": "Reading file", "editing": "Editing file", "searching": "Searching files",
    "running_command": "Running command", "searching_web": "Searching web",
    "inspecting": "Inspecting page", "coordinating": "Coordinating agents",
    "waiting": "Waiting for result", "using_tool": "Using tool",
}
_NON_CLEARING = {"Notification", "PermissionRequest"}


def activity_kind(tool_name: Any) -> str:
    return _ACTIVITY.get(tool_name, "using_tool") if isinstance(tool_name, str) else "using_tool"


def iso(ts: float | None) -> str | None:
    if ts is None:
        return None
    try:
        return datetime.fromtimestamp(ts, timezone.utc).isoformat()
    except (OverflowError, OSError, ValueError):
        return None


# ------------------------------------------------------------------ reducer

def new_file_state() -> dict[str, Any]:
    return {"keys": {}}


def _new_key(sid: str, aid: str) -> dict[str, Any]:
    return {
        "sid": sid, "aid": aid, "atype": None, "first": None, "last": None, "sub_activity": None,
        "pid": None, "pid_created": None, "pid_ts": -1.0,
        "workspace": None, "model": None, "model_ts": -1.0,
        "start": None, "start_source": None, "prompt": None, "prompt_id": None, "stop": None, "fail": None, "end": None,
        "end_reason": None, "interrupt": None, "idle": None,
        "wait": None, "wait_kind": None, "wait_tuid": None, "clear": None,
        "pre_compact": None, "post_compact": None, "compact_prompt_id": None,
        "sub_start": None, "sub_stop": None,
        "tools": {},
        # prompt_id -> {terminal kind: latest ts}; lets a turn's own Stop win
        # over arrival order and keeps a previous turn's Stop off a newer turn.
        "turns": {},
    }


def _max(current: float | None, value: float) -> float:
    return value if current is None or value > current else current


def _prune_tools(tools: dict[str, list[Any]], now: float) -> None:
    if len(tools) <= 32:
        return
    for tuid in [t for t, (_n, start, end, _prompt) in tools.items() if end is not None and end < now - 60]:
        tools.pop(tuid, None)
    while len(tools) > _TOOL_LIMIT:
        oldest = min(tools, key=lambda t: max(x for x in tools[t][1:3] if x is not None))
        tools.pop(oldest, None)


def _turn_terminal(s: dict[str, Any], prompt_id: str, kind: str, ts: float) -> None:
    turns = s["turns"]
    entry = turns.pop(prompt_id, None) or {}
    entry[kind] = _max(entry.get(kind), ts)
    turns[prompt_id] = entry  # most recently touched last
    while len(turns) > _TURN_LIMIT:
        turns.pop(next(iter(turns)))


def _record_ts(record: dict[str, Any]) -> float | None:
    value = record.get("ts")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    ts = float(value)
    return ts if math.isfinite(ts) and ts > 0 else None


def reduce_event(file_state: dict[str, Any], record: Any) -> None:
    if not isinstance(record, dict):
        return
    ts = _record_ts(record)
    sid, event = record.get("session_id"), record.get("event")
    if ts is None or not isinstance(sid, str) or not sid or not isinstance(event, str):
        return
    agent = record.get("agent_id")
    aid = agent if isinstance(agent, str) else ""
    keys = file_state["keys"]
    root = keys.get((sid, ""))
    if root is None:
        root = keys[(sid, "")] = _new_key(sid, "")
    s = root
    if aid:
        s = keys.get((sid, aid))
        if s is None:
            s = keys[(sid, aid)] = _new_key(sid, aid)
        if isinstance(record.get("agent_type"), str):
            s["atype"] = record["agent_type"]
        root["sub_activity"] = _max(root["sub_activity"], ts)
    s["first"] = ts if s["first"] is None else min(s["first"], ts)
    s["last"] = _max(s["last"], ts)
    if not aid:
        root["last"] = s["last"]
    # Process/workspace facts are session-wide; model facts belong to each agent.
    if isinstance(record.get("claude_pid"), int) and isinstance(record.get("claude_pid_created"), int) \
            and ts >= root["pid_ts"]:
        root.update(pid=record["claude_pid"], pid_created=record["claude_pid_created"], pid_ts=ts)
    if isinstance(record.get("workspace"), str):
        root["workspace"] = record["workspace"]
    if isinstance(record.get("model"), str) and ts >= s["model_ts"]:
        s["model"], s["model_ts"] = record["model"], ts

    ask = record.get("ask") is True or record.get("tool_name") == "ask"
    prompt_id = record.get("prompt_id") if isinstance(record.get("prompt_id"), str) else None
    tuid = record.get("tool_use_id") if isinstance(record.get("tool_use_id"), str) else None
    name = record.get("tool_name") if isinstance(record.get("tool_name"), str) else None

    if event == "SessionStart":
        s["start"] = _max(s["start"], ts)
        s["start_source"] = record.get("source") if isinstance(record.get("source"), str) else None
        if s["start_source"] == "clear" and not aid and root["pid"] is not None:
            for other in keys.values():
                if (other is not root and not other["aid"] and other["pid"] == root["pid"]
                        and other["pid_created"] == root["pid_created"]
                        and (other["last"] or 0) <= ts):
                    other["end"], other["end_reason"] = _max(other["end"], ts), "clear"
    elif event == "SessionEnd":
        # Session-wide, not turn-scoped.
        if s["end"] is None or ts >= s["end"]:
            reason = record.get("reason")
            s["end_reason"] = reason if isinstance(reason, str) else s["end_reason"]
        s["end"] = _max(s["end"], ts)
    elif event == "UserPromptSubmit":
        # The current turn is the latest prompt by timestamp, not by arrival.
        if s["prompt"] is None or ts >= s["prompt"]:
            s["prompt_id"] = prompt_id
        s["prompt"] = _max(s["prompt"], ts)
    elif event in {"Stop", "StopFailure"}:
        kind = "completed" if event == "Stop" else "failed"
        if prompt_id is not None:
            _turn_terminal(s, prompt_id, kind, ts)
        elif event == "Stop":
            s["stop"] = _max(s["stop"], ts)
        else:
            s["fail"] = _max(s["fail"], ts)
    elif event == "Notification":
        kind = record.get("notification_type")
        if kind == "idle_prompt":
            # Turn-scoped like Stop: with a prompt id it ends only that turn;
            # without one, plain timestamps decide (admitted only when ts >=
            # the current prompt, which _terminal/project_key enforce).
            if prompt_id is not None:
                _turn_terminal(s, prompt_id, "completed", ts)
            else:
                s["idle"] = _max(s["idle"], ts)
        elif kind in {"permission_prompt", "elicitation_dialog"} and (s["wait"] is None or ts >= s["wait"]):
            _set_wait(s, ts, "permission", None)
    elif event == "PermissionRequest":
        if s["wait"] is None or ts >= s["wait"]:
            _set_wait(s, ts, "ask" if ask else "permission", tuid if ask else None)
    elif event in {"PreToolUse", "PostToolUse", "PostToolUseFailure"} and tuid:
        tool = s["tools"].get(tuid)
        if tool is None:
            tool = s["tools"][tuid] = [name, None, None, prompt_id]
        if name:
            tool[0] = name
        if prompt_id is not None:
            tool[3] = prompt_id
        if event == "PreToolUse":
            if tool[2] is None:
                tool[1] = _max(tool[1], ts)
            if ask and (s["wait"] is None or ts >= s["wait"]):
                _set_wait(s, ts, "ask", tuid)
        else:
            tool[2] = _max(tool[2], ts)
            if event == "PostToolUseFailure" and record.get("is_interrupt") is True:
                if prompt_id is not None:
                    _turn_terminal(s, prompt_id, "stopped", ts)
                else:
                    s["interrupt"] = _max(s["interrupt"], ts)
        _prune_tools(s["tools"], ts)
    elif event == "PreCompact":
        # Scoped to its run epoch at projection time (timestamp and prompt id),
        # so a delayed or unfinished compaction cannot leak into a newer turn.
        if s["pre_compact"] is None or ts >= s["pre_compact"]:
            s["compact_prompt_id"] = prompt_id
        s["pre_compact"] = _max(s["pre_compact"], ts)
    elif event == "PostCompact":
        s["post_compact"] = _max(s["post_compact"], ts)
    elif event == "SubagentStart" and aid:
        s["sub_start"] = _max(s["sub_start"], ts)
    elif event == "SubagentStop" and aid:
        s["sub_stop"] = _max(s["sub_stop"], ts)
    # A later event that is not itself a wait signal means the wait was answered.
    if event not in _NON_CLEARING and not (event == "PreToolUse" and ask):
        s["clear"] = _max(s["clear"], ts)


def _set_wait(s: dict[str, Any], ts: float, kind: str, tuid: str | None) -> None:
    """Record a wait signal without losing an open question.

    Claude follows an AskUserQuestion call with a PermissionRequest (no tool id)
    and a permission_prompt Notification for the same wait. Those must not turn
    the open question into a plain permission prompt or drop its tool id.
    """
    open_ask = (s["wait_kind"] == "ask" and s["wait"] is not None
                and (s["clear"] is None or s["wait"] > s["clear"]))
    s["wait"] = ts
    if open_ask and (kind != "ask" or tuid is None):
        return
    s["wait_kind"], s["wait_tuid"] = kind, tuid


# ------------------------------------------------------------------- loading

def day_names(now: float) -> tuple[str, str]:
    today = claude_hook._day_name(now)
    yesterday = claude_hook._day_name(now - 86400)
    return yesterday, today


def _day_fingerprint(path: Path) -> tuple[int, ...] | None:
    try:
        from . import providers
        return providers._stat_fingerprint(path)
    except OSError:
        return None


_SEED_KINDS: dict[str, str] = {}
_SEED_LOCK = threading.Lock()


def load_states(now: float | None = None, directory: str | os.PathLike[str] | None = None) -> dict[tuple[str, str], dict[str, Any]]:
    """Incrementally reduce yesterday's and today's event files.

    Today's reducer starts from yesterday's final state, so a session spanning
    midnight keeps its history. Torn trailing lines are left for the next read
    by the shared incremental reader.
    """
    from . import providers  # late: providers is the shared reader's home

    now = time.time() if now is None else now
    base = Path(directory) if directory is not None else Path(claude_hook.events_dir())
    yesterday_name, today_name = day_names(now)
    seed_fingerprint = _day_fingerprint(base / yesterday_name)
    previous = providers._incremental_jsonl_reduce(
        "claude-hook-events", base / yesterday_name, new_file_state, reduce_event, unavailable=None)
    seed = previous if isinstance(previous, dict) else None

    def initial() -> dict[str, Any]:
        from copy import deepcopy
        return deepcopy(seed) if seed else new_file_state()

    # Today's checkpoint is valid only for the seed it was built from. The
    # seed fingerprint is part of the checkpoint kind, so a late append to
    # yesterday's file rebuilds today's state from the new seed. (The shared
    # reader's fingerprint_extra does not invalidate same-file checkpoints.)
    kind = "claude-hook-events-seeded:" + (
        "none" if seed_fingerprint is None else "-".join(str(part) for part in seed_fingerprint))
    today_key = str((base / today_name).resolve(strict=False))
    with _SEED_LOCK:
        stale = _SEED_KINDS.pop(today_key, None)
        _SEED_KINDS[today_key] = kind
        while len(_SEED_KINDS) > 16:
            _SEED_KINDS.pop(next(iter(_SEED_KINDS)))
    if stale is not None and stale != kind:
        providers._jsonl_checkpoint_remove((stale, today_key))
    current = providers._incremental_jsonl_reduce(
        kind, base / today_name, initial, reduce_event, unavailable=None)
    chosen = current if isinstance(current, dict) else seed
    return dict(chosen["keys"]) if chosen else {}


# ---------------------------------------------------------------- projection

def default_alive(pid: int | None, created: int | None) -> bool | None:
    """True/False when liveness is proven, None when it cannot be told."""
    if pid is None or created is None:
        return None
    status, current = claude_hook.probe_process(pid)
    if status == "gone":
        return False
    if status == "alive":
        return current == created
    return None


def _terminal(s: dict[str, Any]) -> tuple[float | None, str | None]:
    """Latest terminal event and its kind; ties favour the stronger kind.

    Turn-scoped terminals (carrying a prompt id) count only for the current
    turn, and there they dominate its prompt whatever the arrival-clock order.
    Without a known current prompt id they fall back to plain timestamps.
    """
    best: tuple[float, int, str] | None = None
    start = s["start"]
    candidates = [(s["stop"], "completed"), (s["idle"], "completed"), (s["fail"], "failed"),
                  (s["interrupt"], "stopped")]
    current = s.get("prompt_id")
    for turn_id, kinds in (s.get("turns") or {}).items():
        if current is not None and turn_id != current:
            continue
        for kind, ts in kinds.items():
            if current is not None and s["prompt"] is not None:
                ts = max(ts, s["prompt"])
            candidates.append((ts, kind))
    if s.get("aid"):
        candidates.append((s.get("sub_stop"), "completed"))
    if s["end"] is not None and (start is None or s["end"] >= start):
        candidates.append((s["end"], "finished"))
    for ts, kind in candidates:
        if ts is None:
            continue
        rank = len(TERMINAL_KINDS) - TERMINAL_KINDS.index(kind)
        if best is None or (ts, rank) > best[:2]:
            best = (ts, rank, kind)
    return (best[0], best[2]) if best else (None, None)


def _open_tool(s: dict[str, Any], since: float | None) -> tuple[str, float] | None:
    latest: tuple[float, str] | None = None
    for tool_name, start, end, prompt_id in s["tools"].values():
        if start is None or end is not None:
            continue
        if since is not None and start < since:
            continue
        if s.get("prompt_id") is not None and prompt_id is not None and prompt_id != s["prompt_id"]:
            continue
        if latest is None or start > latest[0]:
            latest = (start, tool_name or "")
    return (latest[1], latest[0]) if latest else None


def project_key(s: dict[str, Any], *, alive: bool | None, parent: dict[str, Any] | None = None,
                parent_terminal: float | None = None) -> dict[str, Any]:
    """Execution fields for one key. ``parent`` is the root state for a subagent."""
    last = s["last"]
    dead = alive is False
    child = bool(s["aid"])
    term_ts, term_kind = _terminal(s)
    if child:
        started, stopped = s["sub_start"], s["sub_stop"]
        run_start = started
        active = started is not None and (term_ts is None or started > term_ts)
    else:
        run_start = s["prompt"]
        active = run_start is not None and (term_ts is None or run_start > term_ts)
    if dead:
        active = False
        term_ts, term_kind = last, "finished"
    pre, compact_turn = s["pre_compact"], s.get("compact_prompt_id")
    compacting = (not dead and pre is not None
                  and (s["post_compact"] is None or pre > s["post_compact"])
                  and (term_ts is None or pre > term_ts)
                  # Run epoch: a newer prompt invalidates an older unfinished compaction.
                  and (child or run_start is None or pre >= run_start)
                  and (child or compact_turn is None or s.get("prompt_id") is None
                       or compact_turn == s["prompt_id"]))
    waiting = (not dead and s["wait"] is not None and (s["clear"] is None or s["wait"] > s["clear"])
               and (term_ts is None or s["wait"] > term_ts))
    observed = last
    if not child and active and s["sub_activity"] is not None:
        observed = max(observed, s["sub_activity"])  # a live subagent proves the session process is working

    out: dict[str, Any] = {
        "executionSource": "hook", "executionObservedAt": iso(observed), "executionSince": None,
        "executionCompactionStartedAt": None, "executionTerminalAt": None, "executionActivity": None,
        "stateDetail": None, "waitKind": None, "waitToolUseId": None,
    }
    if waiting:
        out.update(executionStatus="waiting", executionSince=iso(run_start) if active else None,
                   waitKind=s["wait_kind"], waitToolUseId=s["wait_tuid"],
                   executionActivity={"kind": "waiting", "label": _LABELS["waiting"], "files": [],
                                      "observedAt": iso(s["wait"])})
    elif compacting:
        out.update(executionStatus="compacting", executionSince=iso(run_start) if active else None,
                   executionCompactionStartedAt=iso(s["pre_compact"]))
    elif active:
        out.update(executionStatus="running", executionSince=iso(run_start))
        tool = _open_tool(s, run_start)
        if tool:
            kind = activity_kind(tool[0])
            out["executionActivity"] = {"kind": kind, "label": _LABELS[kind], "files": [], "observedAt": iso(tool[1])}
    elif term_ts is not None and (run_start is None or term_ts >= run_start):
        failed = term_kind == "failed"
        out.update(executionStatus="failed" if failed else "idle", executionTerminalAt=iso(term_ts),
                   stateDetail=None if failed else term_kind)
    elif not child and s["start"] is not None and (run_start is None or s["start"] >= run_start):
        out.update(executionStatus="idle", executionTerminalAt=iso(s["start"]))
    else:
        out.update(executionStatus="unknown", executionSource="unavailable", executionObservedAt=None)
    return out


def project_all(states: dict[tuple[str, str], dict[str, Any]],
                alive_fn: Callable[[int | None, int | None], bool | None] = default_alive) -> list[dict[str, Any]]:
    """One projection per key (root first), with internal identity fields."""
    verdicts: dict[tuple[Any, Any], bool | None] = {}
    children: dict[str, list[tuple[str, dict[str, Any]]]] = {}
    for (sid, aid), s in states.items():
        if aid:
            children.setdefault(sid, []).append((aid, s))
    rows: list[dict[str, Any]] = []
    for (sid, aid), s in states.items():
        if aid:
            continue
        pid_key = (s["pid"], s["pid_created"])
        if pid_key not in verdicts:
            verdicts[pid_key] = alive_fn(*pid_key)
        alive = verdicts[pid_key]
        rows.append({"sid": sid, "aid": "", "state": s, "alive": alive, **project_key(s, alive=alive)})
        term_ts, _kind = _terminal(s)
        parent_terminal = term_ts if alive is not False else s["last"]
        for caid, child in children.get(sid, []):
            rows.append({"sid": sid, "aid": caid, "state": child, "alive": alive,
                         **project_key(child, alive=alive, parent=s, parent_terminal=parent_terminal)})
    return rows
