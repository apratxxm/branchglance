"""Hook collector and event reducer (synthetic event files only)."""

import json
import tempfile
import time
from pathlib import Path

import pytest

from token_tracker import claude_events, claude_hook

NOW = time.mktime((2026, 10, 22, 12, 0, 0, 0, 0, -1))
SID = "11111111-aaaa-bbbb-cccc-000000000001"


def ev(ts, event, sid=SID, **kw):
    if isinstance(kw.get("tool_name"), str):
        raw = kw["tool_name"]
        kw["tool_name"] = "mcp" if raw.startswith("mcp__") else claude_hook._TOOL_CATEGORIES.get(raw, "other")
    return {"ts": ts, "event": event, "session_id": sid, "claude_pid": 4242, "claude_pid_created": 777, **kw}


def write(directory: Path, name: str, records, tail: str = ""):
    directory.mkdir(parents=True, exist_ok=True)
    text = "".join(json.dumps(r) + "\n" for r in records) + tail
    (directory / name).write_text(text, encoding="utf-8")


def today(now=NOW):
    return claude_events.day_names(now)[1]


def yesterday(now=NOW):
    return claude_events.day_names(now)[0]


def project(tmp_path, records, now=NOW, alive=True, **write_kw):
    # Each call describes a separate synthetic history, not an in-place rewrite
    # of the append-only hook log used by the incremental production reader.
    event_dir = Path(tempfile.mkdtemp(dir=tmp_path))
    write(event_dir, today(now), records, **write_kw)
    states = claude_events.load_states(now, event_dir)
    rows = claude_events.project_all(states, lambda pid, created: alive)
    return {(r["sid"], r["aid"]): r for r in rows}


def root(rows, sid=SID):
    return rows[(sid, "")]


# ------------------------------------------------------------------ hook

@pytest.fixture()
def data_env(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    monkeypatch.setattr(claude_hook, "_claude_process", lambda: (4242, 777))
    return tmp_path / "local" / "Branchglance"


PAYLOAD = {
    "hook_event_name": "PreToolUse", "session_id": SID, "tool_name": "Write", "tool_use_id": "toolu_01",
    "cwd": "C:\\Users\\dev\\secret-client\\project",
    "tool_input": {"file_path": "C:/Users/dev/passwords.txt", "content": "TOP-SECRET-CONTENT"},
    "prompt": "my private prompt", "transcript_path": "C:/Users/dev/.claude/projects/x/y.jsonl",
    "session_title": "TOP SECRET PROMPT MUST NEVER BE STORED",
    "model": "C:/private/model.json",
    "agent_type": "C:/private/agent-name.txt",
    "source": "C:/private/source.txt",
    "reason": "C:/private/reason.txt",
    "trigger": "$(touch /private/marker)",
    "notification_type": "C:/private/notification.txt",
}


def test_consent_absent_writes_nothing(data_env):
    assert claude_hook.handle(json.dumps(PAYLOAD).encode(), NOW) is False
    assert not data_env.exists()


def test_bom_prefixed_payload_is_accepted(data_env):
    data_env.mkdir(parents=True)
    (data_env / claude_hook.CONSENT_NAME).write_text("{}")
    assert claude_hook.handle(b"\xef\xbb\xbf" + json.dumps(PAYLOAD).encode(), NOW) is True


def test_consent_present_writes_one_allowlisted_line(data_env):
    data_env.mkdir(parents=True)
    (data_env / claude_hook.CONSENT_NAME).write_text("{}")
    assert claude_hook.handle(json.dumps(PAYLOAD).encode(), NOW) is True
    files = list((data_env / claude_hook.EVENTS_DIR_NAME).glob("*.jsonl"))
    assert len(files) == 1
    lines = files[0].read_text().splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    allowed = {"ts", "event", "session_id", "agent_id", "prompt_id", "agent_type", "tool_name", "tool_use_id",
               "notification_type", "source", "reason", "trigger", "is_interrupt", "model",
               "claude_pid", "claude_pid_created", "ask", "workspace"}
    assert set(record) <= allowed
    text = lines[0]
    for forbidden in ("TOP-SECRET", "passwords", "private prompt", "dev", "secret-client", "transcript",
                      "TOP SECRET PROMPT", "private/model.json", "agent-name.txt", "source.txt", "reason.txt",
                      "notification.txt", "marker"):
        assert forbidden not in text
    assert record["workspace"] == "C:/project"
    assert "session_title" not in record and "model" not in record and "agent_type" not in record
    assert record["source"] == record["reason"] == record["notification_type"] == "other"
    assert record["trigger"] == "other" and record["tool_name"] == "edit"
    assert record["claude_pid"] == 4242


def test_ask_flag_and_garbage(data_env):
    data_env.mkdir(parents=True)
    (data_env / claude_hook.CONSENT_NAME).write_text("{}")
    assert claude_hook.handle(json.dumps({**PAYLOAD, "tool_name": "AskUserQuestion"}).encode(), NOW)
    assert claude_hook.handle(b"not json", NOW) is False
    assert claude_hook.handle(json.dumps({"hook_event_name": "Stop"}).encode(), NOW) is False  # no session
    assert claude_hook.handle(json.dumps({"hook_event_name": "Stop", "session_id": "../../x y"}).encode(), NOW) is False
    line = next((data_env / claude_hook.EVENTS_DIR_NAME).glob("*.jsonl")).read_text().splitlines()
    assert len(line) == 1 and json.loads(line[0])["ask"] is True


def test_fixed_categories_and_validated_fields(data_env):
    data_env.mkdir(parents=True)
    (data_env / claude_hook.CONSENT_NAME).write_text("{}")
    raw = {"hook_event_name": "PreToolUse", "session_id": SID, "tool_name": "mcp__local__secret_tool",
           "tool_use_id": "toolu_02", "model": "claude-sonnet-4-5", "agent_type": "valid_agent-1",
           "source": "resume", "reason": "logout", "trigger": "auto", "notification_type": "auth_success"}
    assert claude_hook.handle(json.dumps(raw).encode(), NOW)
    record = json.loads(next((data_env / claude_hook.EVENTS_DIR_NAME).glob("*.jsonl")).read_text())
    assert record["tool_name"] == "mcp" and record["model"] == "claude-sonnet-4-5"
    assert record["agent_type"] == "valid_agent-1" and record["source"] == "resume"
    assert record["reason"] == "logout" and record["trigger"] == "auto"
    assert record["notification_type"] == "auth_success"


def test_oversize_and_day_cap_drop_without_rename(data_env, monkeypatch):
    data_env.mkdir(parents=True)
    (data_env / claude_hook.CONSENT_NAME).write_text("{}")
    monkeypatch.setattr(claude_hook, "MAX_LINE_BYTES", 60)
    assert claude_hook.handle(json.dumps(PAYLOAD).encode(), NOW) is False
    monkeypatch.setattr(claude_hook, "MAX_LINE_BYTES", 4096)
    monkeypatch.setattr(claude_hook, "MAX_DAY_BYTES", 10)
    assert claude_hook.handle(json.dumps(PAYLOAD).encode(), NOW) is False
    directory = data_env / claude_hook.EVENTS_DIR_NAME
    names = sorted(p.name for p in directory.iterdir())
    assert names == sorted([claude_hook.DROP_LOG_NAME, claude_hook._day_name(NOW)])
    assert (directory / claude_hook._day_name(NOW)).read_text() == ""
    reasons = [json.loads(x)["reason"] for x in (directory / claude_hook.DROP_LOG_NAME).read_text().splitlines()]
    assert reasons == ["oversize", "day-cap"]


def test_main_never_raises_or_prints(data_env, monkeypatch, capsys):
    import io
    data_env.mkdir(parents=True)
    (data_env / claude_hook.CONSENT_NAME).write_text("{}")

    class Stdin:
        buffer = io.BytesIO(b"\xff\xfe garbage")

    monkeypatch.setattr("sys.stdin", Stdin())
    assert claude_hook.main([]) == 0
    monkeypatch.setattr(claude_hook, "handle", lambda raw: (_ for _ in ()).throw(RuntimeError("boom")))
    Stdin.buffer = io.BytesIO(b"{}")
    assert claude_hook.main([]) == 0
    out = capsys.readouterr()
    assert out.out == "" and out.err == ""


def test_main_returns_when_stdin_stays_open(data_env, monkeypatch):
    import io
    import threading

    class HeldOpen:
        def read(self, _limit):
            threading.Event().wait(10)
            return b"{}"

    class Stdin:
        buffer = HeldOpen()

    data_env.mkdir(parents=True)
    (data_env / claude_hook.CONSENT_NAME).write_text("{}")
    monkeypatch.setattr("sys.stdin", Stdin())
    monkeypatch.setattr(claude_hook, "STDIN_TIMEOUT_SECONDS", 0.01)
    assert claude_hook.main([]) == 0


def test_nearest_claude_ancestor():
    table = {10: (9, "python.exe"), 9: (8, "bash.exe"), 8: (7, "bash.exe"), 7: (6, "claude.exe"),
             6: (5, "claude.exe"), 5: (1, "explorer.exe")}
    assert claude_hook.nearest_claude_ancestor(table, 10) == 7
    assert claude_hook.nearest_claude_ancestor({10: (9, "python.exe")}, 10) is None
    assert claude_hook.nearest_claude_ancestor({1: (2, "a"), 2: (1, "b")}, 1) is None  # cycle safe


def test_workspace_label_is_drive_and_leaf():
    assert claude_hook.workspace_label("D:\\work\\orchestrator") == "D:/orchestrator"
    assert claude_hook.workspace_label("A:\\") == "A:"
    assert claude_hook.workspace_label("") is None
    # Non-absolute values are not a working directory; shell/control characters never pass.
    assert claude_hook.workspace_label("my private prompt") is None
    assert claude_hook.workspace_label("../../etc") is None
    assert claude_hook.workspace_label("C:\\work\\$(touch x)") == "C:"
    assert claude_hook.workspace_label("/home/dev/proj") == "proj"


# --------------------------------------------------------------- reducer

def test_running_until_stop_then_new_prompt(tmp_path):
    rows = project(tmp_path, [ev(NOW - 50, "SessionStart", source="startup"), ev(NOW - 40, "UserPromptSubmit")])
    r = root(rows)
    assert r["executionStatus"] == "running" and r["executionSource"] == "hook"
    assert r["executionSince"] == claude_events.iso(NOW - 40)
    assert r["executionObservedAt"] == claude_events.iso(NOW - 40)
    rows = project(tmp_path, [ev(NOW - 40, "UserPromptSubmit"), ev(NOW - 10, "Stop")])
    r = root(rows)
    assert r["executionStatus"] == "idle" and r["stateDetail"] == "completed"
    assert r["executionTerminalAt"] == claude_events.iso(NOW - 10)
    rows = project(tmp_path, [ev(NOW - 40, "UserPromptSubmit"), ev(NOW - 10, "Stop"), ev(NOW - 5, "UserPromptSubmit")])
    assert root(rows)["executionStatus"] == "running"


def test_stop_failure_session_end_and_idle_prompt(tmp_path):
    base = [ev(NOW - 40, "UserPromptSubmit")]
    assert root(project(tmp_path, base + [ev(NOW - 5, "StopFailure")]))["executionStatus"] == "failed"
    r = root(project(tmp_path, base + [ev(NOW - 5, "SessionEnd", reason="logout")]))
    assert r["executionStatus"] == "idle" and r["stateDetail"] == "finished"
    r = root(project(tmp_path, base + [ev(NOW - 5, "Notification", notification_type="idle_prompt")]))
    assert r["executionStatus"] == "idle"
    r = root(project(tmp_path, [ev(NOW - 50, "SessionStart", source="startup")]))
    assert r["executionStatus"] == "idle"  # nothing ran yet


def test_no_prompt_means_no_running(tmp_path):
    # Hooks installed mid-turn: tools but no prompt seen -> unknown, never running.
    rows = project(tmp_path, [ev(NOW - 5, "PreToolUse", tool_name="Bash", tool_use_id="t1")])
    r = root(rows)
    assert r["executionStatus"] == "unknown" and r["executionSource"] == "unavailable"


def test_esc_interrupt_stops_run_and_next_prompt_resets(tmp_path):
    recs = [ev(NOW - 40, "UserPromptSubmit"), ev(NOW - 30, "PreToolUse", tool_name="Bash", tool_use_id="t1"),
            ev(NOW - 20, "PostToolUseFailure", tool_name="Bash", tool_use_id="t1", is_interrupt=True)]
    r = root(project(tmp_path, recs))
    assert r["executionStatus"] == "idle" and r["stateDetail"] == "stopped"
    recs.append(ev(NOW - 10, "UserPromptSubmit"))
    assert root(project(tmp_path, recs))["executionStatus"] == "running"
    # A non-interrupt tool failure does not stop the run.
    recs = [ev(NOW - 40, "UserPromptSubmit"),
            ev(NOW - 20, "PostToolUseFailure", tool_name="Bash", tool_use_id="t1", is_interrupt=False)]
    assert root(project(tmp_path, recs))["executionStatus"] == "running"


def test_crash_dead_pid_clears_running_and_waiting(tmp_path):
    recs = [ev(NOW - 40, "UserPromptSubmit"), ev(NOW - 30, "PermissionRequest", tool_name="Bash")]
    live = root(project(tmp_path, recs, alive=True))
    assert live["executionStatus"] == "waiting"
    dead = root(project(tmp_path, recs, alive=False))
    assert dead["executionStatus"] == "idle" and dead["stateDetail"] == "finished"
    assert dead["executionTerminalAt"] == claude_events.iso(NOW - 30)
    unknown = root(project(tmp_path, recs, alive=None))
    assert unknown["executionStatus"] == "waiting"  # liveness unknown never ends a session


def test_permission_prompt_then_deny_and_approve(tmp_path):
    recs = [ev(NOW - 40, "UserPromptSubmit"), ev(NOW - 35, "PreToolUse", tool_name="Bash", tool_use_id="t1"),
            ev(NOW - 30, "PermissionRequest", tool_name="Bash"),
            ev(NOW - 29, "Notification", notification_type="permission_prompt")]
    r = root(project(tmp_path, recs))
    assert r["executionStatus"] == "waiting" and r["waitKind"] == "permission"
    # An unrelated notification does not clear the wait.
    recs.append(ev(NOW - 25, "Notification", notification_type="auth_success"))
    assert root(project(tmp_path, recs))["executionStatus"] == "waiting"
    # Deny: the tool fails, then Claude carries on in the same turn.
    deny = recs + [ev(NOW - 20, "PostToolUseFailure", tool_name="Bash", tool_use_id="t1", is_interrupt=False)]
    assert root(project(tmp_path, deny))["executionStatus"] == "running"
    # Deny ending the turn.
    assert root(project(tmp_path, deny + [ev(NOW - 10, "Stop")]))["executionStatus"] == "idle"
    # Approve: next tool event clears.
    approve = recs + [ev(NOW - 20, "PostToolUse", tool_name="Bash", tool_use_id="t1")]
    assert root(project(tmp_path, approve))["executionStatus"] == "running"


def test_ask_user_question_waits_until_its_result(tmp_path):
    recs = [ev(NOW - 40, "UserPromptSubmit"),
            ev(NOW - 30, "PreToolUse", tool_name="AskUserQuestion", tool_use_id="ask1", ask=True)]
    r = root(project(tmp_path, recs))
    assert r["executionStatus"] == "waiting" and r["waitKind"] == "ask" and r["waitToolUseId"] == "ask1"
    recs.append(ev(NOW - 20, "PostToolUse", tool_name="AskUserQuestion", tool_use_id="ask1", ask=True))
    assert root(project(tmp_path, recs))["executionStatus"] == "running"


def test_compaction_window_and_terminal(tmp_path):
    recs = [ev(NOW - 40, "UserPromptSubmit"), ev(NOW - 30, "PreCompact", trigger="auto")]
    r = root(project(tmp_path, recs))
    assert r["executionStatus"] == "compacting"
    assert r["executionCompactionStartedAt"] == claude_events.iso(NOW - 30)
    recs.append(ev(NOW - 20, "PostCompact", trigger="auto"))
    assert root(project(tmp_path, recs))["executionStatus"] == "running"
    # A turn that ends during compaction is not still compacting.
    r = root(project(tmp_path, [ev(NOW - 40, "UserPromptSubmit"), ev(NOW - 30, "PreCompact"), ev(NOW - 20, "Stop")]))
    assert r["executionStatus"] == "idle"
    # Manual /compact while idle.
    r = root(project(tmp_path, [ev(NOW - 40, "Stop"), ev(NOW - 30, "PreCompact", trigger="manual")]))
    assert r["executionStatus"] == "compacting"
    r = root(project(tmp_path, [ev(NOW - 40, "UserPromptSubmit", prompt_id="p1"),
                                ev(NOW - 30, "PreCompact"),
                                ev(NOW - 10, "UserPromptSubmit", prompt_id="p2")]))
    assert r["executionStatus"] == "running" and r["executionCompactionStartedAt"] is None


def test_activity_kinds_have_no_file_names(tmp_path):
    cases = {"Read": "reading", "Edit": "editing", "Write": "editing", "MultiEdit": "editing",
             "NotebookEdit": "editing", "Grep": "searching", "Glob": "searching", "Bash": "running_command",
             "PowerShell": "running_command", "WebSearch": "searching_web", "WebFetch": "inspecting",
             "Task": "coordinating", "Agent": "coordinating", "TodoWrite": "using_tool",
             "mcp__x__y": "using_tool", "Whatever": "using_tool"}
    for tool, kind in cases.items():
        recs = [ev(NOW - 40, "UserPromptSubmit"), ev(NOW - 5, "PreToolUse", tool_name=tool, tool_use_id="t1")]
        activity = root(project(tmp_path, recs))["executionActivity"]
        assert activity["kind"] == kind and activity["files"] == [], tool
        assert activity["observedAt"] == claude_events.iso(NOW - 5)
    recs.append(ev(NOW - 4, "PostToolUse", tool_name="Whatever", tool_use_id="t1"))
    assert root(project(tmp_path, recs))["executionActivity"] is None


def test_out_of_order_pairs_and_late_events(tmp_path):
    # Post written before Pre (async hooks): the tool is closed, not open.
    recs = [ev(NOW - 40, "UserPromptSubmit"), ev(NOW - 5, "PostToolUse", tool_name="Read", tool_use_id="t1"),
            ev(NOW - 6, "PreToolUse", tool_name="Read", tool_use_id="t1")]
    r = root(project(tmp_path, recs))
    assert r["executionStatus"] == "running" and r["executionActivity"] is None
    # Processing timestamps can put the delayed start after its completion.
    recs = [ev(NOW - 40, "UserPromptSubmit"),
            ev(NOW - 10, "PostToolUse", tool_name="Read", tool_use_id="late-start"),
            ev(NOW - 5, "PreToolUse", tool_name="Read", tool_use_id="late-start")]
    assert root(project(tmp_path, recs))["executionActivity"] is None
    # A late-arriving older event cannot resurrect a finished run.
    recs = [ev(NOW - 40, "UserPromptSubmit"), ev(NOW - 10, "Stop"), ev(NOW - 20, "PreToolUse", tool_name="Bash", tool_use_id="t2")]
    r = root(project(tmp_path, recs))
    assert r["executionStatus"] == "idle" and r["executionActivity"] is None
    # Stop recorded before the prompt line (reordered): ts decides.
    recs = [ev(NOW - 10, "Stop"), ev(NOW - 40, "UserPromptSubmit")]
    assert root(project(tmp_path, recs))["executionStatus"] == "idle"
    # Prompt identity rejects a delayed terminal from the prior turn.
    recs = [ev(NOW - 40, "UserPromptSubmit", prompt_id="prompt-old"),
            ev(NOW - 20, "UserPromptSubmit", prompt_id="prompt-new"),
            ev(NOW - 5, "Stop", prompt_id="prompt-old")]
    assert root(project(tmp_path, recs))["executionStatus"] == "running"


def test_subagents_attach_to_session_root(tmp_path):
    recs = [ev(NOW - 40, "UserPromptSubmit"), ev(NOW - 35, "PreToolUse", tool_name="Agent", tool_use_id="t1"),
            ev(NOW - 30, "SubagentStart", agent_id="agentA", agent_type="sonnet-builder"),
            ev(NOW - 29, "SubagentStart", agent_id="agentB", agent_type="haiku-builder"),
            ev(NOW - 20, "PreToolUse", agent_id="agentA", agent_type="sonnet-builder", tool_name="Read", tool_use_id="c1"),
            ev(NOW - 10, "SubagentStop", agent_id="agentB", agent_type="haiku-builder")]
    rows = project(tmp_path, recs)
    a, b = rows[(SID, "agentA")], rows[(SID, "agentB")]
    assert a["executionStatus"] == "running" and a["executionActivity"]["kind"] == "reading"
    assert b["executionStatus"] == "idle" and b["stateDetail"] == "completed"
    assert a["state"]["atype"] == "sonnet-builder"
    root_row = root(rows)
    assert root_row["executionStatus"] == "running"
    # Child tool events do not make the root's own activity change or clear its waits.
    assert root_row["executionActivity"]["kind"] == "coordinating"
    # Session ends: children stop with it.
    ended = project(tmp_path, recs + [ev(NOW - 5, "Stop")])
    assert ended[(SID, "agentA")]["executionStatus"] == "running"  # parent completion doesn't end a background child
    assert root(ended)["executionStatus"] == "idle"


def test_child_events_do_not_clear_root_wait(tmp_path):
    recs = [ev(NOW - 40, "UserPromptSubmit"), ev(NOW - 30, "PermissionRequest", tool_name="Bash"),
            ev(NOW - 20, "PreToolUse", agent_id="agentA", agent_type="x", tool_name="Read", tool_use_id="c1")]
    assert root(project(tmp_path, recs))["executionStatus"] == "waiting"


def test_child_without_start_never_runs(tmp_path):
    recs = [ev(NOW - 20, "PreToolUse", agent_id="agentA", agent_type="x", tool_name="Read", tool_use_id="c1")]
    assert project(tmp_path, recs)[(SID, "agentA")]["executionStatus"] == "unknown"


def test_session_clear_ends_replaced_session(tmp_path):
    old, new = SID, "22222222-aaaa-bbbb-cccc-000000000002"
    recs = [ev(NOW - 40, "UserPromptSubmit", sid=old), ev(NOW - 20, "PreToolUse", sid=old, tool_name="Bash", tool_use_id="t1"),
            ev(NOW - 10, "SessionStart", sid=new, source="clear")]
    rows = project(tmp_path, recs)
    assert root(rows, old)["executionStatus"] == "idle" and root(rows, old)["stateDetail"] == "finished"
    assert root(rows, new)["executionStatus"] == "idle"
    # Resume of an ended session re-links to the same row.
    recs = [ev(NOW - 40, "UserPromptSubmit"), ev(NOW - 30, "SessionEnd", reason="other"),
            ev(NOW - 20, "SessionStart", source="resume"), ev(NOW - 10, "UserPromptSubmit")]
    assert root(project(tmp_path, recs))["executionStatus"] == "running"


def test_midnight_rollover_keeps_session_state(tmp_path):
    now = NOW + 3600  # 13:00; build "midnight" by choosing now just after a day boundary
    midnight = time.mktime((2026, 10, 23, 0, 5, 0, 0, 0, -1))
    before = time.mktime((2026, 10, 22, 23, 58, 0, 0, 0, -1))
    write(tmp_path, claude_events.day_names(before)[1], [ev(before, "UserPromptSubmit"),
                                                          ev(before + 30, "PreToolUse", tool_name="Bash", tool_use_id="t1")])
    write(tmp_path, claude_events.day_names(midnight)[1], [ev(midnight - 60, "PostToolUse", tool_name="Bash", tool_use_id="t1")])
    states = claude_events.load_states(midnight, tmp_path)
    rows = {(r["sid"], r["aid"]): r for r in claude_events.project_all(states, lambda p, c: True)}
    r = rows[(SID, "")]
    assert r["executionStatus"] == "running"
    assert r["executionSince"] == claude_events.iso(before)
    assert r["executionActivity"] is None  # tool closed by the next day's file
    # And a Stop on the new day ends the run begun yesterday.
    with (tmp_path / claude_events.day_names(midnight)[1]).open("a") as handle:
        handle.write(json.dumps(ev(midnight - 30, "Stop")) + "\n")
    states = claude_events.load_states(midnight, tmp_path)
    assert claude_events.project_all(states, lambda p, c: True)[0]["executionStatus"] == "idle"


def test_late_yesterday_change_rebuilds_todays_seed(tmp_path):
    midnight = time.mktime((2026, 10, 23, 0, 5, 0, 0, 0, -1))
    before = time.mktime((2026, 10, 22, 23, 58, 0, 0, 0, -1))
    yesterday_path = tmp_path / claude_events.day_names(midnight)[0]
    today_path = tmp_path / claude_events.day_names(midnight)[1]
    write(tmp_path, yesterday_path.name, [ev(before, "UserPromptSubmit")])
    write(tmp_path, today_path.name, [ev(midnight - 30, "PreToolUse", tool_name="Read", tool_use_id="t1")])
    assert claude_events.project_all(claude_events.load_states(midnight, tmp_path), lambda p, c: True)[0]["executionStatus"] == "running"
    with yesterday_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(ev(before + 60, "Stop")) + "\n")
    rows = claude_events.project_all(claude_events.load_states(midnight, tmp_path), lambda p, c: True)
    assert rows[0]["executionStatus"] == "idle"


def test_torn_trailing_line_is_ignored_then_picked_up(tmp_path):
    first = json.dumps(ev(NOW - 40, "UserPromptSubmit"))
    stop = json.dumps(ev(NOW - 10, "Stop"))
    path = tmp_path / today()
    tmp_path.mkdir(exist_ok=True)
    path.write_text(first + "\n" + stop[:25], encoding="utf-8")
    states = claude_events.load_states(NOW, tmp_path)
    assert claude_events.project_all(states, lambda p, c: True)[0]["executionStatus"] == "running"
    with path.open("a", encoding="utf-8") as handle:
        handle.write(stop[25:] + "\n")
    states = claude_events.load_states(NOW, tmp_path)
    assert claude_events.project_all(states, lambda p, c: True)[0]["executionStatus"] == "idle"


def test_oversized_and_garbage_lines_do_not_poison_the_stream(tmp_path):
    path = tmp_path / today()
    tmp_path.mkdir(exist_ok=True)
    big = '{"ts": 1, "event": "UserPromptSubmit", "session_id": "' + "x" * (3 * 1024 * 1024) + '"}'
    lines = [json.dumps(ev(NOW - 40, "UserPromptSubmit")), "not json", big, "[1,2]", '{"ts":"x"}',
             json.dumps({"ts": float("nan"), "event": "Stop", "session_id": SID}),
             json.dumps(ev(NOW - 5, "PreToolUse", tool_name="Read", tool_use_id="t1"))]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    states = claude_events.load_states(NOW, tmp_path)
    rows = {(r["sid"], r["aid"]): r for r in claude_events.project_all(states, lambda p, c: True)}
    assert rows[(SID, "")]["executionStatus"] == "running"
    assert len(rows) == 2  # the 3 MB line created a (valid) session of its own, nothing else


def test_hook_bound_keeps_observed_time_of_last_event(tmp_path):
    # The reducer never extends freshness: the UI's 150 s bound applies to the last hook event.
    recs = [ev(NOW - 900, "UserPromptSubmit"), ev(NOW - 800, "PreToolUse", tool_name="Bash", tool_use_id="t1")]
    r = root(project(tmp_path, recs))
    assert r["executionStatus"] == "running"
    assert r["executionObservedAt"] == claude_events.iso(NOW - 800)


# ------------------------------------------------- review fixes F1, F6-F10

HOSTILE = "SECRET prompt text"
HOSTILE_PATH = "C:/Users/dev/private/notes.txt"


@pytest.mark.parametrize("field", ["agent_id", "prompt_id", "tool_use_id", "agent_type", "model",
                                   "source", "reason", "trigger", "notification_type", "tool_name",
                                   "session_title", "cwd"])
@pytest.mark.parametrize("value", [HOSTILE, HOSTILE_PATH, "notes.txt", "../../etc/passwd",
                                   "claude-opus C:/x", "$(touch pwned)", "a\nb", "x" * 5000])
def test_hostile_stdin_leaves_no_text_or_path_in_any_field(data_env, field, value):
    """F1: every allowlisted field is format/enum checked, not just character-cleaned."""
    data_env.mkdir(parents=True, exist_ok=True)
    (data_env / claude_hook.CONSENT_NAME).write_text("{}")
    payload = {"hook_event_name": "PreToolUse", "session_id": SID, "tool_name": "Read",
               "tool_use_id": "toolu_01ABCdef", field: value}
    claude_hook.handle(json.dumps(payload).encode(), NOW)
    text = "".join(p.read_text() for p in (data_env / claude_hook.EVENTS_DIR_NAME).glob("*.jsonl"))
    fragments = ["SECRET", "dev", "private", "notes", ".txt", "passwd", "..", "pwned", "C:/x",
                 "x" * 64, "session_title"]
    if field == "cwd" and value == HOSTILE_PATH:
        # The agreed workspace exposure: drive plus the last folder name only.
        assert json.loads(text)["workspace"] == "C:/notes.txt"
        fragments = [f for f in fragments if f not in {"notes", ".txt"}]
    for fragment in fragments:
        assert fragment not in text, (field, value)
    for line in text.splitlines():
        record = json.loads(line)
        for key in claude_hook._ENUMS:
            if key in record:
                assert record[key] in claude_hook._ENUMS[key]
        if "workspace" in record:  # drive + leaf only, never a deeper path
            assert record["workspace"].count("/") <= 1


def test_unknown_event_or_non_uuid_session_is_not_written(data_env):
    data_env.mkdir(parents=True)
    (data_env / claude_hook.CONSENT_NAME).write_text("{}")
    assert claude_hook.handle(json.dumps({"hook_event_name": HOSTILE, "session_id": SID}).encode(), NOW) is False
    assert claude_hook.handle(json.dumps({"hook_event_name": "Stop", "session_id": "notes.txt"}).encode(), NOW) is False
    assert claude_hook.handle(json.dumps({"hook_event_name": "Stop", "session_id": HOSTILE}).encode(), NOW) is False
    events = data_env / claude_hook.EVENTS_DIR_NAME
    assert not events.exists() or all(p.read_text() == "" for p in events.glob("*.jsonl"))


def test_mcp_and_unknown_tools_store_only_fixed_categories(data_env):
    data_env.mkdir(parents=True)
    (data_env / claude_hook.CONSENT_NAME).write_text("{}")
    for tool in ("mcp__private_server__read_secret_file", "SomeNewTool", "Read"):
        claude_hook.handle(json.dumps({"hook_event_name": "PreToolUse", "session_id": SID,
                                       "tool_name": tool}).encode(), NOW)
    lines = next((data_env / claude_hook.EVENTS_DIR_NAME).glob("*.jsonl")).read_text().splitlines()
    assert [json.loads(x)["tool_name"] for x in lines] == ["mcp", "other", "read"]
    assert "private_server" not in "".join(lines) and "SomeNewTool" not in "".join(lines)


def _hook_process(tmp_path, code=None):
    import os
    import subprocess
    import sys
    source_root = Path(claude_hook.__file__).resolve().parents[1]
    env = {**os.environ, "LOCALAPPDATA": str(tmp_path / "local"), "PYTHONPATH": str(source_root)}
    data = tmp_path / "local" / "Branchglance"
    data.mkdir(parents=True, exist_ok=True)
    (data / claude_hook.CONSENT_NAME).write_text("{}")
    args = [sys.executable, "-c", code] if code else [sys.executable, "-m", "token_tracker.claude_hook"]
    proc = subprocess.Popen(args, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            cwd=str(tmp_path), env=env)
    return proc, data


def test_hook_process_exits_zero_quietly_when_stdin_is_held_open(tmp_path):
    """F6: a producer that never closes stdin cannot keep the hook alive."""
    started = time.monotonic()
    proc, data = _hook_process(tmp_path)
    try:
        proc.stdin.write(b'{"hook_event_name": "Stop", "session_id": "')  # partial; pipe stays open
        proc.stdin.flush()
        code = proc.wait(timeout=20)
    finally:
        if proc.poll() is None:
            proc.kill()
    assert code == 0 and time.monotonic() - started < 15
    assert proc.stdout.read() == b"" and proc.stderr.read() == b""
    proc.stdin.close()
    events = data / claude_hook.EVENTS_DIR_NAME
    assert not events.exists() or not list(events.glob("*.jsonl"))


def test_hook_process_malformed_bytes_exit_zero_quietly(tmp_path):
    proc, _data = _hook_process(tmp_path)
    out, err = proc.communicate(b"\xff\xfe\x00 not json \x00" * 100, timeout=30)
    assert proc.returncode == 0 and out == b"" and err == b""


def test_hook_deadline_is_enforced_even_if_processing_blocks(tmp_path):
    """F6: the watchdog ends a hook stuck after reading stdin (for example in file IO)."""
    code = ("import time\n"
            "from token_tracker import claude_hook\n"
            "claude_hook.PROCESS_DEADLINE_SECONDS = 0.5\n"
            "claude_hook.handle = lambda raw: time.sleep(60)\n"
            "claude_hook.run([])\n")
    started = time.monotonic()
    proc, _data = _hook_process(tmp_path, code)
    try:
        out, err = proc.communicate(b"{}", timeout=20)
    finally:
        if proc.poll() is None:
            proc.kill()
    assert proc.returncode == 0 and time.monotonic() - started < 15
    assert out == b"" and err == b""


def test_completion_dominates_delayed_start_whatever_the_order(tmp_path):
    """F7: a tool completion wins over its start in either arrival or clock order."""
    for recs in (
        [ev(NOW - 40, "UserPromptSubmit"), ev(NOW - 10, "PostToolUse", tool_name="Read", tool_use_id="t9"),
         ev(NOW - 2, "PreToolUse", tool_name="Read", tool_use_id="t9")],
        [ev(NOW - 40, "UserPromptSubmit"), ev(NOW - 2, "PreToolUse", tool_name="Read", tool_use_id="t9"),
         ev(NOW - 10, "PostToolUse", tool_name="Read", tool_use_id="t9")],
    ):
        r = root(project(tmp_path, recs))
        assert r["executionStatus"] == "running" and r["executionActivity"] is None


def test_turn_stop_correlated_by_prompt_id_regardless_of_order(tmp_path):
    """F7: a turn's own Stop ends it even when stamped and written before its prompt."""
    recs = [ev(NOW - 50, "UserPromptSubmit", prompt_id="p-a"), ev(NOW - 45, "Stop", prompt_id="p-a"),
            ev(NOW - 12, "Stop", prompt_id="p-b"), ev(NOW - 10, "UserPromptSubmit", prompt_id="p-b")]
    r = root(project(tmp_path, recs))
    assert r["executionStatus"] == "idle" and r["stateDetail"] == "completed"
    assert r["executionTerminalAt"] == claude_events.iso(NOW - 10)
    # Delayed previous-turn Stop, StopFailure and interrupt never end a newer turn.
    recs = [ev(NOW - 50, "UserPromptSubmit", prompt_id="p-a"), ev(NOW - 10, "UserPromptSubmit", prompt_id="p-b"),
            ev(NOW - 3, "StopFailure", prompt_id="p-a"), ev(NOW - 2, "Stop", prompt_id="p-a"),
            ev(NOW - 1, "PostToolUseFailure", tool_name="Bash", tool_use_id="old", prompt_id="p-a",
               is_interrupt=True)]
    assert root(project(tmp_path, recs))["executionStatus"] == "running"
    # A delayed older prompt line does not displace the current turn's identity.
    recs = [ev(NOW - 10, "UserPromptSubmit", prompt_id="p-b"), ev(NOW - 5, "Stop", prompt_id="p-b"),
            ev(NOW - 50, "UserPromptSubmit", prompt_id="p-a")]
    assert root(project(tmp_path, recs))["executionStatus"] == "idle"
    # The current turn's interrupt still stops it.
    recs = [ev(NOW - 10, "UserPromptSubmit", prompt_id="p-b"),
            ev(NOW - 5, "PostToolUseFailure", tool_name="Bash", tool_use_id="t1", prompt_id="p-b", is_interrupt=True)]
    r = root(project(tmp_path, recs))
    assert r["executionStatus"] == "idle" and r["stateDetail"] == "stopped"


def test_unfinished_compaction_does_not_leak_into_newer_turn(tmp_path):
    """F8: run-epoch scoping holds for every arrival order."""
    orders = [
        [ev(NOW - 40, "UserPromptSubmit", prompt_id="p1"), ev(NOW - 30, "PreCompact", prompt_id="p1"),
         ev(NOW - 10, "UserPromptSubmit", prompt_id="p2")],
        [ev(NOW - 10, "UserPromptSubmit", prompt_id="p2"), ev(NOW - 30, "PreCompact", prompt_id="p1"),
         ev(NOW - 40, "UserPromptSubmit", prompt_id="p1")],
        # Without prompt ids, timestamps alone define the epoch.
        [ev(NOW - 10, "UserPromptSubmit"), ev(NOW - 30, "PreCompact")],
        # A delayed old-turn PreCompact stamped after the new prompt.
        [ev(NOW - 40, "UserPromptSubmit", prompt_id="p1"), ev(NOW - 10, "UserPromptSubmit", prompt_id="p2"),
         ev(NOW - 5, "PreCompact", prompt_id="p1")],
    ]
    for recs in orders:
        r = root(project(tmp_path, recs))
        assert r["executionStatus"] == "running", recs
        assert r["executionCompactionStartedAt"] is None
    # The current turn's own compaction still shows.
    recs = [ev(NOW - 10, "UserPromptSubmit", prompt_id="p2"), ev(NOW - 5, "PreCompact", prompt_id="p2")]
    assert root(project(tmp_path, recs))["executionStatus"] == "compacting"


def test_child_terminal_is_monotonic_and_parent_completion_does_not_complete_it(tmp_path):
    """F9: SubagentStop joins the max-timestamp terminal selection; nothing is inferred from the parent."""
    start = [ev(NOW - 40, "UserPromptSubmit"), ev(NOW - 30, "SubagentStart", agent_id="kid", agent_type="x")]
    stop = ev(NOW - 17, "SubagentStop", agent_id="kid", agent_type="x")
    fail = ev(NOW - 15, "StopFailure", agent_id="kid", agent_type="x")
    for recs in (start + [stop, fail], start + [fail, stop]):
        kid = project(tmp_path, recs)[(SID, "kid")]
        assert kid["executionStatus"] == "failed"
        assert kid["executionTerminalAt"] == claude_events.iso(NOW - 15)
    late_stop = ev(NOW - 5, "SubagentStop", agent_id="kid", agent_type="x")
    kid = project(tmp_path, start + [fail, late_stop])[(SID, "kid")]
    assert kid["executionStatus"] == "idle" and kid["executionTerminalAt"] == claude_events.iso(NOW - 5)
    # The parent turn ends while a background child keeps working: the child is not completed.
    rows = project(tmp_path, start + [ev(NOW - 10, "Stop"),
                                      ev(NOW - 5, "PreToolUse", agent_id="kid", tool_name="Read", tool_use_id="k1")])
    assert rows[(SID, "kid")]["executionStatus"] == "running"
    assert rows[(SID, "kid")]["executionTerminalAt"] is None
    assert root(rows)["executionStatus"] == "idle"


def test_late_yesterday_change_after_today_checkpoint_is_seen_repeatedly(tmp_path):
    """F10: today's cached state is rebuilt whenever yesterday's file changes."""
    midnight = time.mktime((2026, 10, 23, 0, 5, 0, 0, 0, -1))
    before = time.mktime((2026, 10, 22, 23, 58, 0, 0, 0, -1))
    names = claude_events.day_names(midnight)
    write(tmp_path, names[0], [ev(before, "UserPromptSubmit", prompt_id="p1")])
    write(tmp_path, names[1], [ev(midnight - 40, "PreToolUse", tool_name="Read", tool_use_id="t1")])

    def status():
        rows = claude_events.project_all(claude_events.load_states(midnight, tmp_path), lambda p, c: True)
        return root({(r["sid"], r["aid"]): r for r in rows})["executionStatus"]

    assert status() == "running"
    assert status() == "running"  # served from the checkpoint
    with (tmp_path / names[0]).open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(ev(before + 30, "Stop", prompt_id="p1")) + "\n")
    assert status() == "idle"
    # Today's file still appends incrementally on top of the rebuilt seed.
    with (tmp_path / names[1]).open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(ev(midnight - 20, "UserPromptSubmit", prompt_id="p2")) + "\n")
    assert status() == "running"
    # Yesterday's file disappearing rebuilds too, without keeping a stale seed.
    (tmp_path / names[0]).unlink()
    assert status() == "running"


# ------------------------------------------------------- round 2: G1, G3, G9

_WRITER = """
import sys
from token_tracker import claude_hook
worker, count, now = int(sys.argv[1]), int(sys.argv[2]), float(sys.argv[3])
for i in range(count):
    record = {"ts": now, "event": "PreToolUse", "session_id": "11111111-aaaa-bbbb-cccc-000000000001",
              "tool_use_id": "w%02d_%05d_" % (worker, i) + "p" * (40 + (i * 37 + worker * 11) % 900)}
    assert claude_hook.append_record(record, now)
"""


def test_concurrent_processes_append_without_lost_or_torn_lines(tmp_path):
    """G1: many writer processes, one daily file: every line arrives whole, exactly once."""
    import os
    import subprocess
    import sys
    workers, count = 12, 300
    source_root = Path(claude_hook.__file__).resolve().parents[1]
    env = {**os.environ, "LOCALAPPDATA": str(tmp_path / "local"), "PYTHONPATH": str(source_root)}
    script = tmp_path / "writer.py"
    script.write_text(_WRITER, encoding="utf-8")
    procs = [subprocess.Popen([sys.executable, str(script), str(w), str(count), str(NOW)], env=env, cwd=str(tmp_path),
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE) for w in range(workers)]
    for proc in procs:
        _out, err = proc.communicate(timeout=180)
        assert proc.returncode == 0, err.decode(errors="replace")
    path = tmp_path / "local" / "Branchglance" / claude_hook.EVENTS_DIR_NAME / claude_hook._day_name(NOW)
    raw = path.read_bytes()
    assert raw.endswith(b"\n")
    lines = raw.split(b"\n")[:-1]
    seen = set()
    for line in lines:
        record = json.loads(line)  # a torn or interleaved line fails here
        seen.add(record["tool_use_id"][:10])
    assert len(lines) == workers * count  # nothing lost or duplicated
    assert seen == {"w%02d_%05d_" % (w, i) for w in range(workers) for i in range(count)}
    assert not (path.parent / claude_hook.DROP_LOG_NAME).exists()


def test_append_line_reports_cap_without_writing(tmp_path):
    path = str(tmp_path / "day.jsonl")
    assert claude_hook.append_line(path, b"abc\n", 100) == "ok"
    assert claude_hook.append_line(path, b"x" * 97 + b"\n", 100) == "cap"
    assert (tmp_path / "day.jsonl").read_bytes() == b"abc\n"


def test_late_idle_prompt_of_previous_turn_does_not_end_new_turn(tmp_path):
    """G3 probe scenario: P1 Stop -> P2 prompt -> late idle_prompt(P1) -> P2 PreToolUse is running."""
    recs = [ev(NOW - 60, "UserPromptSubmit", prompt_id="P1"), ev(NOW - 50, "Stop", prompt_id="P1"),
            ev(NOW - 20, "UserPromptSubmit", prompt_id="P2"),
            ev(NOW - 10, "Notification", notification_type="idle_prompt", prompt_id="P1"),
            ev(NOW - 5, "PreToolUse", tool_name="Read", tool_use_id="t2", prompt_id="P2")]
    r = root(project(tmp_path, recs))
    assert r["executionStatus"] == "running" and r["executionActivity"]["kind"] == "reading"
    # The current turn's own idle_prompt ends it, whatever its stamp.
    own = recs[:3] + [ev(NOW - 25, "Notification", notification_type="idle_prompt", prompt_id="P2")]
    assert root(project(tmp_path, own))["executionStatus"] == "idle"
    # Without a prompt id: admitted only when stamped at or after the current prompt.
    early = recs[:3] + [ev(NOW - 30, "Notification", notification_type="idle_prompt")]
    assert root(project(tmp_path, early))["executionStatus"] == "running"
    late = recs[:3] + [ev(NOW - 20, "Notification", notification_type="idle_prompt")]
    assert root(project(tmp_path, late))["executionStatus"] == "idle"


def test_fork_source_is_kept(data_env):
    """G9."""
    data_env.mkdir(parents=True)
    (data_env / claude_hook.CONSENT_NAME).write_text("{}")
    claude_hook.handle(json.dumps({"hook_event_name": "SessionStart", "session_id": SID, "source": "fork"}).encode(), NOW)
    record = json.loads(next((data_env / claude_hook.EVENTS_DIR_NAME).glob("*.jsonl")).read_text())
    assert record["source"] == "fork"
