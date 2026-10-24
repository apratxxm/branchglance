"""Claude Code rows from hook events plus transcripts (synthetic files)."""

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from token_tracker import claude_events, claude_hook, claude_provider, providers
from token_tracker.provider_policy import load_policies

NOW = time.time()
SID = "33333333-aaaa-bbbb-cccc-000000000003"
SECRET = "SECRET-PROMPT-TEXT"
SECRET_FILE = "passwords-do-not-leak.txt"


def stamp(epoch):
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat().replace("+00:00", "Z")


def ev(ts, event, sid=SID, **kw):
    if isinstance(kw.get("tool_name"), str):
        raw = kw["tool_name"]
        kw["tool_name"] = "mcp" if raw.startswith("mcp__") else claude_hook._TOOL_CATEGORIES.get(raw, "other")
    return {"ts": ts, "event": event, "session_id": sid, "claude_pid": 4242, "claude_pid_created": 777, **kw}


def usage(inp, cached, creation, out):
    return {"input_tokens": inp, "cache_read_input_tokens": cached, "cache_creation_input_tokens": creation,
            "output_tokens": out}


def assistant(ts, mid, model="claude-opus-5", use=None, blocks=None, stop=None, entry="cli"):
    return {"type": "assistant", "entrypoint": entry, "timestamp": stamp(ts), "cwd": "C:\\Users\\dev\\client-secret\\proj",
            "message": {"id": mid, "model": model, "usage": use or usage(10, 900, 90, 50),
                        "content": blocks or [{"type": "text", "text": SECRET}], "stop_reason": stop}}


def user(ts, text=SECRET):
    return {"type": "user", "timestamp": stamp(ts), "message": {"role": "user", "content": text}}


@pytest.fixture()
def world(tmp_path, monkeypatch):
    home = tmp_path / "claude"
    projects = home / "projects" / "A--proj"
    projects.mkdir(parents=True)
    events = tmp_path / "events"
    events.mkdir()
    monkeypatch.setenv("CLAUDE_HOME", str(home))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    claude_provider.reset_memo()

    class World:
        pass

    w = World()
    w.projects, w.events, w.tmp = projects, events, tmp_path

    def transcript(sid, records):
        path = projects / f"{sid}.jsonl"
        path.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")
        return path

    def hooks(records, now=NOW):
        name = claude_events.day_names(now)[1]
        (events / name).write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")

    def rows(now=NOW, alive=True):
        return {r["id"]: r for r in claude_provider.build_rows(
            now, lambda pid, created: alive, events, home / "projects")}

    w.transcript, w.hooks, w.rows = transcript, hooks, rows
    return w


def test_hook_row_follows_codex_contract(world):
    world.transcript(SID, [{"type": "custom-title", "customTitle": "Ship the parser"},
                           user(NOW - 100), assistant(NOW - 60, "m1")])
    world.hooks([ev(NOW - 90, "UserPromptSubmit"), ev(NOW - 20, "PreToolUse", tool_name="Read", tool_use_id="t1")])
    row = world.rows()[SID]
    assert row["title"] == "Ship the parser"
    assert row["status"] == "running" and row["statusSource"] == "hook"
    assert row["executionStatus"] == "running" and row["executionSource"] == "hook"
    assert row["executionObservedAt"] == claude_events.iso(NOW - 20)
    assert row["executionSince"] == claude_events.iso(NOW - 90)
    assert row["executionActivity"]["kind"] == "reading" and row["activity"]["kind"] == "reading"
    assert row["model"] == "claude-opus-5"
    assert row["workspace"] == "C:/proj"
    assert row["contextTokens"] == 10 + 900 + 90
    assert row["contextWindow"] == 1_000_000 and row["contextPercent"] == 0
    assert row["lastRequest"] == {"input": 1000, "cached": 900, "output": 50}
    assert row["tokens"] == 1050 and row["turns"] == 1
    assert row["telemetryUnavailable"] is False
    assert row["resumable"] is True


def test_public_rows_expose_only_allowlisted_fields_and_no_private_text(world):
    world.transcript(SID, [
        {"type": "custom-title", "customTitle": "Safe title"},
        user(NOW - 100, SECRET),
        assistant(NOW - 60, "m1", blocks=[
            {"type": "text", "text": SECRET},
            {"type": "tool_use", "id": "t1", "name": "Write", "input": {"file_path": f"C:/x/{SECRET_FILE}", "content": SECRET}}]),
        {"type": "last-prompt", "lastPrompt": SECRET}, {"type": "ai-title", "aiTitle": SECRET}])
    # Hook lines go through the real writer's allowlist from hostile payloads.
    hostile = {"session_id": SID, "session_title": SECRET + " C:/private/transcript.txt",
               "prompt": SECRET, "cwd": "C:\\Users\\dev\\client-secret\\proj",
               "tool_input": {"file_path": f"C:/x/{SECRET_FILE}"}, "model": f"C:/x/{SECRET_FILE}",
               "agent_type": SECRET_FILE, "source": SECRET, "reason": SECRET_FILE}
    world.hooks([claude_hook.build_record({**hostile, "hook_event_name": "UserPromptSubmit"}, NOW - 90, 4242, 777),
                 claude_hook.build_record({**hostile, "hook_event_name": "PreToolUse", "tool_name": "Write",
                                           "tool_use_id": "t1"}, NOW - 20, 4242, 777)])
    rows = claude_provider.public_rows(claude_provider.build_rows(
        NOW, lambda p, c: True, world.events, world.projects.parent))
    blob = json.dumps(rows)
    for forbidden in (SECRET, SECRET_FILE, "client-secret", "Users", ".jsonl"):
        assert forbidden not in blob
    assert rows[0]["title"] == "Safe title"
    for row in rows:
        assert set(row) <= claude_provider.PUBLIC_FIELDS | {"path"}
        assert "objective" not in row and "preview" not in row
    # Hook files contain no text either.
    hook_text = "".join(p.read_text() for p in world.events.iterdir())
    for forbidden in (SECRET, SECRET_FILE, "client-secret", "transcript", "session_title", "private"):
        assert forbidden not in hook_text
    assert rows[0]["executionActivity"]["kind"] == "editing"


def test_ask_user_question_read_from_transcript_at_render_time(world):
    ask = {"type": "tool_use", "id": "ask1", "name": "AskUserQuestion",
           "input": {"questions": [{"question": "Which database?", "options": [{"label": "Postgres"}, {"label": "SQLite"}]}]}}
    world.transcript(SID, [user(NOW - 100), assistant(NOW - 30, "m1", blocks=[ask])])
    world.hooks([ev(NOW - 90, "UserPromptSubmit"),
                 ev(NOW - 29, "PreToolUse", tool_name="AskUserQuestion", tool_use_id="ask1", ask=True)])
    row = world.rows()[SID]
    assert row["status"] == "needs input" and row["executionStatus"] == "waiting"
    assert row["pendingQuestion"] and "Which database?" in row["pendingQuestion"]
    assert row["pendingOptions"] == ["Postgres", "SQLite"]
    assert row["pendingQuestionId"] == "ask1"
    # Answered: no question, back to running.
    world.hooks([ev(NOW - 90, "UserPromptSubmit"),
                 ev(NOW - 29, "PreToolUse", tool_name="AskUserQuestion", tool_use_id="ask1", ask=True),
                 ev(NOW - 10, "PostToolUse", tool_name="AskUserQuestion", tool_use_id="ask1", ask=True)])
    row = world.rows()[SID]
    assert row["status"] == "running" and row["pendingQuestion"] is None
    # The question text never reaches the hook files.
    assert "Which database" not in "".join(p.read_text() for p in world.events.iterdir())


def test_permission_wait_has_no_invented_question(world):
    world.transcript(SID, [user(NOW - 100)])
    world.hooks([ev(NOW - 90, "UserPromptSubmit"), ev(NOW - 20, "PermissionRequest", tool_name="Bash")])
    row = world.rows()[SID]
    assert row["status"] == "needs input" and row["pendingQuestion"] is None


def test_transcript_only_session_is_never_running(world):
    # The legacy reducer calls this "running" (trailing user record). It must not.
    world.transcript(SID, [user(NOW - 30), assistant(NOW - 20, "m1")])
    row = world.rows()[SID]
    assert row["status"] != "running"
    assert row["executionStatus"] == "unavailable" and row["executionSource"] == "unavailable"
    assert row["telemetryUnavailable"] is True and row["executionActivity"] is None
    assert row["contextTokens"] == 1000  # usage is still reported


def test_transcript_fallback_reports_only_explicit_terminal_and_questions(world):
    world.transcript(SID, [user(NOW - 30), assistant(NOW - 20, "m1", stop="end_turn")])
    row = world.rows()[SID]
    assert row["status"] == "idle" and row["statusSource"] == "transcript"
    assert row["executionStatus"] == "unavailable"
    ask = {"type": "tool_use", "id": "ask9", "name": "AskUserQuestion", "input": {"question": "Proceed?"}}
    other = "44444444-aaaa-bbbb-cccc-000000000004"
    world.transcript(other, [user(NOW - 30), assistant(NOW - 20, "m2", blocks=[ask])])
    row = world.rows()[other]
    assert row["status"] == "needs input" and row["pendingQuestion"] == "Proceed?"
    assert row["executionStatus"] == "unavailable"


def test_hook_coverage_gap_falls_back_instead_of_running(world):
    world.transcript(SID, [user(NOW - 300), assistant(NOW - 5, "m1")])
    world.hooks([ev(NOW - 290, "UserPromptSubmit"), ev(NOW - 280, "PreToolUse", tool_name="Bash", tool_use_id="t1")])
    row = world.rows()[SID]
    assert row["executionStatus"] == "unavailable" and row["status"] != "running"
    assert row["executionActivity"] is None
    # With hooks covering the transcript it is running as usual.
    world.hooks([ev(NOW - 290, "UserPromptSubmit"), ev(NOW - 6, "PreToolUse", tool_name="Bash", tool_use_id="t1")])
    assert world.rows()[SID]["executionStatus"] == "running"


def test_hook_coverage_fallback_keeps_transcript_question(world):
    ask = {"type": "tool_use", "id": "ask-gap", "name": "AskUserQuestion",
           "input": {"question": "Choose safely?", "options": [{"label": "yes"}]}}
    world.transcript(SID, [user(NOW - 300), assistant(NOW - 5, "m1", blocks=[ask])])
    world.hooks([ev(NOW - 290, "UserPromptSubmit"), ev(NOW - 280, "PreToolUse", tool_name="Bash", tool_use_id="b1")])
    row = world.rows()[SID]
    assert row["executionStatus"] == "unavailable" and row["executionSource"] == "unavailable"
    assert row["status"] == "needs input" and row["pendingQuestion"] == "Choose safely?"
    assert row["pendingOptions"] == ["yes"]


def test_context_resets_after_compaction_until_next_request(world):
    world.transcript(SID, [user(NOW - 100), assistant(NOW - 80, "m1")])
    world.hooks([ev(NOW - 90, "UserPromptSubmit"), ev(NOW - 70, "PreCompact"), ev(NOW - 60, "PostCompact")])
    row = world.rows()[SID]
    assert row["contextTokens"] is None and row["lastRequest"] is None
    assert row["executionStatus"] == "running"
    world.transcript(SID, [user(NOW - 100), assistant(NOW - 80, "m1"), assistant(NOW - 50, "m2", use=usage(5, 100, 0, 10))])
    assert world.rows()[SID]["contextTokens"] == 105


def test_context_unknown_model_is_null_and_usage_dedupes_by_message(world):
    world.transcript(SID, [
        assistant(NOW - 60, "m1", model="mystery-model", use=usage(1, 2, 3, 1)),
        assistant(NOW - 59, "m1", model="mystery-model", use=usage(1, 2, 3, 40)),
        assistant(NOW - 50, "m2", model="mystery-model", use=usage(2, 4, 6, 8))])
    world.hooks([ev(NOW - 70, "UserPromptSubmit"), ev(NOW - 5, "Stop")])
    row = world.rows()[SID]
    assert row["contextTokens"] == 12 and row["contextWindow"] is None and row["contextPercent"] is None
    assert row["tokens"] == (1 + 2 + 3 + 40) + (2 + 4 + 6 + 8) and row["turns"] == 2


def test_subagent_rows_attach_to_session(world):
    world.transcript(SID, [user(NOW - 100), assistant(NOW - 60, "m1")])
    child_dir = world.projects / SID / "subagents"
    child_dir.mkdir(parents=True)
    (child_dir / "agent-agentA.jsonl").write_text(
        json.dumps(assistant(NOW - 30, "c1", model="claude-haiku-4-5", use=usage(1, 1, 1, 1))) + "\n", encoding="utf-8")
    world.hooks([ev(NOW - 90, "UserPromptSubmit"),
                 ev(NOW - 40, "SubagentStart", agent_id="agentA", agent_type="haiku-builder"),
                 ev(NOW - 20, "PreToolUse", agent_id="agentA", agent_type="haiku-builder", tool_name="Grep", tool_use_id="c1")])
    rows = world.rows()
    child = rows[f"{SID}:agentA"]
    assert child["parentId"] == SID and child["agentName"] == "haiku-builder"
    assert child["executionStatus"] == "running" and child["executionActivity"]["kind"] == "searching"
    assert child["model"] == "claude-haiku-4-5" and child["title"] is None
    assert child["contextModel"] == "claude-haiku-4-5" and child["contextTokens"] == 3
    assert rows[SID]["executionStatus"] == "running" and "parentId" not in rows[SID]
    assert child["resumable"] is False  # subagents are not resumable sessions


def test_dead_process_ends_rows(world):
    world.transcript(SID, [user(NOW - 100), assistant(NOW - 60, "m1")])
    world.hooks([ev(NOW - 90, "UserPromptSubmit"), ev(NOW - 60, "PermissionRequest", tool_name="Bash")])
    row = world.rows(alive=False)[SID]
    assert row["executionStatus"] == "idle" and row["status"] == "idle" and row["stateDetail"] == "finished"
    assert row["pendingQuestion"] is None


def test_snapshot_survives_missing_desktop_quota(world):
    world.transcript(SID, [user(NOW - 100), assistant(NOW - 60, "m1")])
    world.hooks([ev(NOW - 90, "UserPromptSubmit")], now=time.time())

    def broken():
        raise RuntimeError("no Claude Desktop")

    monkey_events = world.events
    original = claude_hook.events_dir
    claude_hook.events_dir = lambda: str(monkey_events)
    try:
        snapshot = claude_provider.ClaudeCodeProvider(broken, broken).snapshot()
    finally:
        claude_hook.events_dir = original
    assert snapshot["connected"] is True and snapshot["name"] == "Claude"
    assert snapshot["fiveHour"]["percent"] is None and snapshot["weekly"]["percent"] is None
    assert [c["id"] for c in snapshot["chats"]] == [SID]
    assert all(c["path"] for c in snapshot["chats"])  # internal path for the live index only


def test_snapshot_keeps_quota_but_replaces_desktop_chats(world, monkeypatch):
    monkeypatch.setattr(claude_hook, "events_dir", lambda: str(world.events))
    world.transcript(SID, [user(NOW - 100), assistant(NOW - 60, "m1")])
    world.hooks([ev(NOW - 90, "UserPromptSubmit")], now=time.time())

    class Desktop:
        def snapshot(self):
            return {"connected": True, "name": "Claude", "fiveHour": {"percent": 42}, "weekly": {"percent": 7},
                    "cache": {"percent": 80}, "activity": [{"date": "2026-10-22", "value": 5}],
                    "chats": [{"id": "legacy", "objective": SECRET, "preview": SECRET, "path": "x"}]}

    snapshot = claude_provider.ClaudeCodeProvider(Desktop).snapshot()
    assert snapshot["fiveHour"]["percent"] == 42 and snapshot["weekly"]["percent"] == 7
    # Parity: provider-level cache/activity come from the transcript reader, not the base.
    assert snapshot["cache"] == claude_provider.usage_aggregates()["cache"]
    assert snapshot["activity"] != [{"date": "2026-10-22", "value": 5}]
    assert [c["id"] for c in snapshot["chats"]] == [SID]
    assert SECRET not in json.dumps(snapshot["chats"])


def test_default_provider_uses_limits_only_base(world, monkeypatch):
    class Limits:
        def snapshot(self):
            return {"connected": True, "name": "Claude", "fiveHour": {"percent": 51},
                    "weekly": {"percent": 12}, "cache": None, "activity": [], "chats": []}

    def desktop_unexpected():
        raise AssertionError("legacy Desktop provider must not scan transcripts")

    monkeypatch.setattr(providers, "ClaudeDesktopProvider", desktop_unexpected)
    monkeypatch.setattr(providers, "ClaudeLimitsProvider", Limits)
    monkeypatch.setattr(claude_hook, "events_dir", lambda: str(world.events))
    snapshot = claude_provider.ClaudeCodeProvider().snapshot()
    assert snapshot["fiveHour"]["percent"] == 51 and snapshot["weekly"]["percent"] == 12


def test_live_row_discards_inferred_status(world):
    world.transcript(SID, [user(NOW - 30), assistant(NOW - 20, "m1")])
    world.hooks([ev(NOW - 25, "UserPromptSubmit"), ev(NOW - 3, "Stop")], now=time.time())
    legacy = {"key": f"Claude:{SID}", "status": "running", "statusSource": None, "pendingQuestion": "stale",
              "stateDetail": "x", "objective": SECRET}
    import token_tracker.claude_provider as cp
    original = cp.build_rows
    cp.build_rows = lambda *a, **k: original(NOW, lambda p, c: True, world.events, world.projects.parent)
    try:
        claude_provider.reset_memo()
        row = claude_provider.live_row(legacy, {"id": SID})
    finally:
        cp.build_rows = original
    assert row["key"] == f"Claude:{SID}"
    assert row["status"] == "idle" and row["executionStatus"] == "idle" and row["executionSource"] == "hook"
    assert row["pendingQuestion"] is None and "objective" not in row
    missing = claude_provider.live_row({"key": "Claude:nope", "status": "running"}, {"id": "nope"})
    assert missing["status"] == "unavailable" and missing["executionStatus"] == "unavailable"


def test_live_state_keeps_hook_execution_for_claude(world, monkeypatch):
    monkeypatch.setattr(claude_hook, "events_dir", lambda: str(world.events))
    policies = load_policies({"TOKEN_TRACKER_CLAUDE_COLLECT": "1", "TOKEN_TRACKER_CLAUDE_SHOW": "1",
                              "TOKEN_TRACKER_CODEX_COLLECT": "0"})
    monkeypatch.setattr(providers, "POLICIES", policies)
    monkeypatch.setattr(claude_events, "default_alive", lambda pid, created: True)
    monkeypatch.setattr(providers.ClaudeDesktopProvider, "_usage", lambda self: (_ for _ in ()).throw(RuntimeError("none")))
    now = time.time()
    world.transcript(SID, [user(now - 30), assistant(now - 20, "m1")])
    world.hooks([ev(now - 25, "UserPromptSubmit"), ev(now - 3, "PreToolUse", tool_name="Edit", tool_use_id="t1")], now=now)
    claude_provider.reset_memo()
    snapshot = claude_provider.ClaudeCodeProvider().snapshot()
    providers._index_live_paths([snapshot], providers.public_policies(policies)["generation"])
    claude_provider.reset_memo()
    live = providers.live_state()
    chat = next(c for c in live["chats"] if c["id"] == SID)
    assert chat["executionStatus"] == "running" and chat["executionSource"] == "hook"
    assert chat["executionActivity"]["kind"] == "editing"
    assert chat["status"] == "running" and chat["statusSource"] == "hook"
    blob = json.dumps(live)
    assert SECRET not in blob and ".jsonl" not in blob and "path" not in chat


def test_desktop_origin_session_is_not_resumable(world):
    world.transcript(SID, [user(NOW - 30), assistant(NOW - 20, "m1", entry="claude-desktop")])
    world.hooks([ev(NOW - 25, "UserPromptSubmit")])
    assert world.rows()[SID]["resumable"] is False


# ----------------------------------------------------- review fixes F1, F11, F12

def test_public_title_model_and_agent_values_are_restricted(world):
    """F1: render-time title, model and agent values cannot carry paths or free text."""
    world.transcript(SID, [
        {"type": "custom-title", "customTitle": f"Fix parser C:\\Users\\dev\\{SECRET_FILE} and ~/notes/{SECRET_FILE}"},
        user(NOW - 100), assistant(NOW - 60, "m1", model=f"C:/models/{SECRET_FILE}")])
    child_dir = world.projects / SID / "subagents"
    child_dir.mkdir(parents=True)
    (child_dir / "agent-kid.jsonl").write_text(
        json.dumps(assistant(NOW - 30, "c1", model=SECRET)) + "\n", encoding="utf-8")
    # agent_type reaching the reducer through a hand-written line (not the hook) is still checked.
    world.hooks([ev(NOW - 90, "UserPromptSubmit"),
                 ev(NOW - 40, "SubagentStart", agent_id="kid", agent_type=f"../{SECRET_FILE}")])
    rows = world.rows()
    blob = json.dumps(claude_provider.public_rows(list(rows.values())))
    assert SECRET_FILE not in blob and SECRET not in blob and "Users" not in blob and "notes/" not in blob
    assert rows[SID]["title"] == "Fix parser and"
    assert rows[SID]["model"] is None and rows[SID]["contextModel"] is None
    assert rows[f"{SID}:kid"]["agentName"] is None and rows[f"{SID}:kid"]["model"] is None
    assert claude_provider.public_title("C:/only/a/path.txt") is None
    assert claude_provider.public_title("Ship it") == "Ship it"


def test_child_model_comes_from_child_evidence_not_parent(world):
    """F12: an Opus parent's Haiku child reports Haiku; with no child evidence, no model."""
    world.transcript(SID, [user(NOW - 100), assistant(NOW - 60, "m1", model="claude-opus-5")])
    world.hooks([ev(NOW - 95, "SessionStart", source="startup", model="claude-opus-5", workspace="C:/proj"),
                 ev(NOW - 90, "UserPromptSubmit"),
                 ev(NOW - 40, "SubagentStart", agent_id="hk", agent_type="haiku-builder", model="claude-haiku-4-5"),
                 ev(NOW - 39, "SubagentStart", agent_id="nm", agent_type="general-purpose")])
    rows = world.rows()
    assert rows[SID]["model"] == "claude-opus-5"
    assert rows[f"{SID}:hk"]["model"] == "claude-haiku-4-5"
    assert rows[f"{SID}:nm"]["model"] is None  # never inherits the parent's model
    # Session-wide facts are still shared with the child.
    assert rows[f"{SID}:hk"]["workspace"] == rows[SID]["workspace"]


def test_coverage_loss_keeps_explicit_terminal_and_never_runs(world):
    """F11: without hook coverage, transcript terminal/question projection is kept; Running is not."""
    world.transcript(SID, [user(NOW - 300), assistant(NOW - 5, "m1", stop="end_turn")])
    world.hooks([ev(NOW - 290, "UserPromptSubmit"), ev(NOW - 280, "PreToolUse", tool_name="Bash", tool_use_id="b1")])
    row = world.rows()[SID]
    assert row["executionStatus"] == "unavailable" and row["executionActivity"] is None
    assert row["status"] == "idle" and row["statusSource"] == "transcript"
    # An unanswered question wins over the terminal, and is the transcript's own text.
    ask = {"type": "tool_use", "id": "ask-x", "name": "AskUserQuestion",
           "input": {"question": "Deploy now?", "options": [{"label": "no"}]}}
    world.transcript(SID, [user(NOW - 300), assistant(NOW - 5, "m2", blocks=[ask])])
    row = world.rows()[SID]
    assert row["status"] == "needs input" and row["pendingQuestion"] == "Deploy now?"
    assert row["pendingQuestionId"] == "ask-x" and row["executionStatus"] != "running"


# ------------------------------------------------------------- round 2: G6

def test_ai_title_is_the_row_title_when_no_custom_title(world, monkeypatch):
    """G6: custom-title first, then ai-title; path-stripped, capped, render-time only."""
    world.transcript(SID, [user(NOW - 100), {"type": "ai-title", "aiTitle": f"Refactor C:/x/{SECRET_FILE} parser " + "y" * 200},
                           assistant(NOW - 60, "m1")])
    world.hooks([ev(NOW - 90, "UserPromptSubmit")])
    title = world.rows()[SID]["title"]
    assert title.startswith("Refactor parser ") and len(title) <= 80 and SECRET_FILE not in title
    world.transcript(SID, [{"type": "ai-title", "aiTitle": "Generated"}, {"type": "custom-title", "customTitle": "Mine"},
                           user(NOW - 100), assistant(NOW - 60, "m1")])
    assert world.rows()[SID]["title"] == "Mine"
    # Transcript-only fallback rows use the same rule.
    other = "44444444-aaaa-bbbb-cccc-000000000004"
    world.transcript(other, [{"type": "ai-title", "aiTitle": "Fallback title"}, user(NOW - 50), assistant(NOW - 40, "m9")])
    assert world.rows()[other]["title"] == "Fallback title"
    # Never persisted: the hook writer has no title field at all.
    record = claude_hook.build_record({"hook_event_name": "UserPromptSubmit", "session_id": SID,
                                       "session_title": "Generated", "ai_title": "Generated"}, NOW, 1, 2)
    assert "Generated" not in json.dumps(record)


def test_title_and_model_survive_the_transcript_rewrite_during_compaction(world):
    """Claude rewrites the transcript on compaction; the row keeps its last title and model meanwhile."""
    world.transcript(SID, [{"type": "custom-title", "customTitle": "Ship the parser"},
                           user(NOW - 100), assistant(NOW - 60, "m1")])
    world.hooks([ev(NOW - 90, "UserPromptSubmit")])
    assert world.rows()[SID]["title"] == "Ship the parser"
    world.transcript(SID, [{"type": "system", "subtype": "compact_boundary", "timestamp": stamp(NOW - 5)}])
    world.hooks([ev(NOW - 90, "UserPromptSubmit"), ev(NOW - 5, "PreCompact")])
    row = world.rows()[SID]
    assert row["status"] == "compacting"
    assert row["title"] == "Ship the parser" and row["model"] == "claude-opus-5"
    world.transcript(SID, [{"type": "custom-title", "customTitle": "Renamed"}, user(NOW - 3)])
    assert world.rows()[SID]["title"] == "Renamed"


# ------------------------------------------------- parity: provider aggregates

def _local_day(epoch):
    return datetime.fromtimestamp(epoch, providers._local_timezone()).date().isoformat()


def _usage5(inp, out, read, w5, w1):
    return {"input_tokens": inp, "output_tokens": out, "cache_read_input_tokens": read,
            "cache_creation": {"ephemeral_5m_input_tokens": w5, "ephemeral_1h_input_tokens": w1}}


def test_aggregates_match_desktop_shapes_and_window(world):
    """Parity: 31-day totals, cache-hit rate, activity and model usage from transcripts."""
    old = NOW - 40 * 86400
    first = world.transcript(SID, [
        assistant(NOW - 3600, "r1", use=_usage5(10, 5, 80, 4, 1)),
        assistant(NOW - 3599, "r1", use=_usage5(10, 20, 80, 4, 1)),   # same response, later block: max kept
        assistant(NOW - 86400 * 2, "r2", model="claude-haiku-4-5", use=_usage5(1, 1, 0, 0, 0)),
        assistant(old, "r-old", use=_usage5(999, 999, 0, 0, 0)),       # outside the window
    ])
    os.utime(first, (NOW - 3000, NOW - 3000))
    # A resumed transcript copies r1 again: counted once.
    resumed = world.transcript("55555555-aaaa-bbbb-cccc-000000000005", [
        assistant(NOW - 3600, "r1", use=_usage5(10, 20, 80, 4, 1)),
        assistant(NOW - 600, "r3", use=_usage5(2, 3, 50, 0, 0))])
    child_dir = world.projects / SID / "subagents"
    child_dir.mkdir(parents=True)
    (child_dir / "agent-kid.jsonl").write_text(
        json.dumps(assistant(NOW - 300, "c1", model="claude-haiku-4-5", use=_usage5(1, 1, 8, 0, 0))) + "\n",
        encoding="utf-8")
    agg = claude_provider.usage_aggregates(NOW, world.projects.parent)
    r1, r2, r3, c1 = 10 + 20 + 80 + 4 + 1, 2, 2 + 3 + 50, 1 + 1 + 8
    assert agg["tracked31Days"] == r1 + r2 + r3 + c1 and agg["trackingWindowDays"] == 31
    assert agg["lifetimeTokens"] == agg["tracked31Days"]
    assert agg["tracked31DaysLabel"] == providers._human_tokens(agg["tracked31Days"])
    cache = agg["cache"]
    read, uncached, created = 80 + 50 + 8, 10 + 1 + 2 + 1, 5
    assert (cache["read"], cache["uncachedInput"], cache["cacheCreation"]) == (read, uncached, created)
    assert cache["percent"] == round(read * 100 / (read + uncached + created))
    assert cache["output"] == 20 + 1 + 3 + 1
    assert {"apiEquivalentCost", "cacheSavings", "pricingCoverage", "estimated", "priceBasis"} <= set(cache)
    days = {row["date"]: row["value"] for row in agg["activity"]}
    assert _local_day(old) not in days
    assert days[_local_day(NOW - 86400 * 2)] >= r2
    assert sum(days.values()) == agg["tracked31Days"]
    assert [row["date"] for row in agg["activity"]] == sorted(days)
    usage = {row["model"]: row for row in agg["modelUsage"]}
    assert usage["claude-haiku-4-5"]["tokens"] == r2 + c1 and usage["claude-haiku-4-5"]["subagentTokens"] == c1
    assert usage["claude-opus-5"]["observedRequests"] == 2 and usage["claude-haiku-4-5"]["subagentRequests"] == 1
    del resumed


def test_aggregates_are_incremental_not_rescans(world, monkeypatch):
    path = world.transcript(SID, [assistant(NOW - 100 - i, f"m{i}", use=_usage5(1, 1, 1, 0, 0)) for i in range(20)])
    calls = {"n": 0}
    real = claude_provider._reduce_usage

    def counting(state, item):
        calls["n"] += 1
        real(state, item)

    monkeypatch.setattr(claude_provider, "_reduce_usage", counting)
    first = claude_provider.usage_aggregates(NOW, world.projects.parent)["tracked31Days"]
    assert calls["n"] == 20 and first == 60
    calls["n"] = 0
    assert claude_provider.usage_aggregates(NOW, world.projects.parent)["tracked31Days"] == 60
    assert calls["n"] == 0  # unchanged file: served from the checkpoint
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(assistant(NOW - 5, "m-new", use=_usage5(1, 1, 1, 0, 0))) + "\n")
    assert claude_provider.usage_aggregates(NOW, world.projects.parent)["tracked31Days"] == 63
    assert calls["n"] == 1  # only the appended line was reduced


def test_aggregates_hold_no_text_and_empty_data_is_null(world):
    assert claude_provider.usage_aggregates(NOW, world.projects.parent)["cache"] is None
    world.transcript(SID, [user(NOW - 50), assistant(NOW - 40, "m1", model=f"C:/x/{SECRET_FILE}")])
    agg = claude_provider.usage_aggregates(NOW, world.projects.parent)
    blob = json.dumps(agg)
    assert SECRET not in blob and SECRET_FILE not in blob
    assert [row["model"] for row in agg["modelUsage"]] == ["unknown"]
    state = claude_provider._new_usage_state()
    claude_provider._reduce_usage(state, assistant(NOW - 40, "m1"))
    assert SECRET not in repr(state)


def test_snapshot_carries_aggregates_with_limits_base(world, monkeypatch):
    class Limits:
        def snapshot(self):
            return {"connected": True, "name": "Claude", "fiveHour": {"percent": 3}, "weekly": {"percent": 4},
                    "cache": None, "activity": [], "chats": [], "limitsOnly": True}

    monkeypatch.setattr(providers, "ClaudeLimitsProvider", Limits)
    monkeypatch.setattr(claude_hook, "events_dir", lambda: str(world.events))
    world.transcript(SID, [user(NOW - 50), assistant(NOW - 40, "m1")])
    snapshot = claude_provider.ClaudeCodeProvider().snapshot()
    assert snapshot["cache"]["percent"] is not None and snapshot["activity"]
    assert snapshot["tracked31Days"] == 10 + 900 + 90 + 50 and snapshot["modelUsage"]
    assert snapshot["fiveHour"]["percent"] == 3 and "limitsOnly" not in snapshot
    # A failure in aggregation never breaks the snapshot.
    monkeypatch.setattr(claude_provider, "usage_aggregates", lambda: (_ for _ in ()).throw(RuntimeError("x")))
    assert claude_provider.ClaudeCodeProvider().snapshot()["fiveHour"]["percent"] == 3
