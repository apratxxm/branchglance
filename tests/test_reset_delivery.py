from __future__ import annotations

import json
import sqlite3
import subprocess
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from token_tracker import codex_client, reset_delivery
from token_tracker.reset_actions import ResetActionDispatcher
from token_tracker.provider_policy import load_policies


TEST_POLICIES = load_policies({
    "TOKEN_TRACKER_CODEX_COLLECT": "true",
    "TOKEN_TRACKER_CODEX_SHOW": "true",
    "TOKEN_TRACKER_CODEX_SEND": "true",
})


THREAD = "01a0cad8-0000-7000-8000-000000000001"


class FakeClient:
    def __init__(self, thread=None, threads=None, error=None):
        self.thread = thread or {}
        self.threads = threads or []
        self.error = error
        self.read_calls = []

    def read_thread(self, thread_id, *, include_turns=True):
        self.read_calls.append((thread_id, include_turns))
        if self.error:
            raise self.error
        return self.thread

    def latest_turn(self, thread_id):
        turns = self.thread.get("turns") or []
        return turns[-1] if turns else None

    def list_threads(self, limit=100):
        return self.threads[:limit]


def _metadata_db(tmp_path: Path, *, archived=False, rollout=True, include_name=True):
    codex_home = tmp_path / "codex"
    codex_home.mkdir(exist_ok=True)
    rollout_path = codex_home / "rollout.jsonl"
    if rollout:
        rollout_path.write_text("\n".join([
            json.dumps({"type": "session_meta", "payload": {"id": THREAD}}),
            json.dumps({"type": "event_msg", "payload": {"type": "task_complete"}}),
        ]) + "\n", encoding="utf-8")
    connection = sqlite3.connect(codex_home / "state_5.sqlite")
    connection.execute("DROP TABLE IF EXISTS threads")
    if include_name:
        connection.execute(
            "CREATE TABLE threads (id TEXT, rollout_path TEXT, title TEXT, name TEXT, archived INTEGER, updated_at INTEGER)"
        )
        connection.execute(
            "INSERT INTO threads VALUES (?, ?, ?, ?, ?, ?)",
            (THREAD, str(rollout_path) if rollout else None, "Task title", None, int(archived), 1),
        )
    else:
        connection.execute(
            "CREATE TABLE threads (id TEXT, rollout_path TEXT, title TEXT, archived INTEGER, updated_at INTEGER)"
        )
        connection.execute(
            "INSERT INTO threads VALUES (?, ?, ?, ?, ?)",
            (THREAD, str(rollout_path) if rollout else None, "Task title", int(archived), 1),
        )
    connection.commit()
    connection.close()
    return codex_home, rollout_path


def _projection(monkeypatch, *, status="idle", question=None, question_ids=None, resolution=None,
                unavailable=False, question_unknown=False):
    def project(row):
        return {
            "status": status,
            "statusSource": "unavailable" if unavailable else "app",
            "telemetryUnavailable": unavailable,
            "pendingQuestion": question,
            "pendingQuestionIds": question_ids or [],
            "questionResolutionAt": resolution,
            "questionCorrelationUnavailable": question_unknown,
        }

    monkeypatch.setattr(reset_delivery.providers.CodexProvider, "_thread", staticmethod(project))


def test_read_thread_uses_persistent_initialized_rpc(monkeypatch):
    fake = FakeAppServer()
    monkeypatch.setattr(codex_client.subprocess, "Popen", lambda *args, **kwargs: fake)
    client = codex_client.CodexAppServer(executable="codex")

    thread = client.read_thread(THREAD)

    request = next(message for message in fake.requests if message.get("method") == "thread/read")
    assert request["params"] == {"threadId": THREAD, "includeTurns": False}
    assert thread["id"] == THREAD
    assert fake.methods.count("initialize") == 1
    latest = client.latest_turn(THREAD)
    turn_request = next(message for message in fake.requests if message.get("method") == "thread/turns/list")
    assert turn_request["params"] == {
        "threadId": THREAD, "limit": 1, "sortDirection": "desc", "itemsView": "notLoaded"
    }
    assert latest["status"] == "completed"
    client.close()


def test_list_threads_is_bounded_and_rejects_oversized_limit(monkeypatch):
    fake = FakeAppServer()
    monkeypatch.setattr(codex_client.subprocess, "Popen", lambda *args, **kwargs: fake)
    client = codex_client.CodexAppServer(executable="codex")

    rows = client.list_threads(10)

    request = next(message for message in fake.requests if message.get("method") == "thread/list")
    assert request["params"]["limit"] == 10
    assert "sourceKinds" in request["params"]
    assert [row["id"] for row in rows] == [THREAD]
    with pytest.raises(ValueError):
        client.list_threads(101)
    client.close()


class FakeAppServer:
    def __init__(self):
        import queue

        self.lines = queue.Queue()
        self.requests = []
        self.methods = []
        outer = self

        class Stdin:
            def write(self, text):
                for line in text.splitlines():
                    outer._answer(json.loads(line))

            def flush(self):
                pass

            def close(self):
                outer.lines.put(None)

        self.stdin = Stdin()
        self.stderr = None

    @property
    def stdout(self):
        while (line := self.lines.get()) is not None:
            yield line

    def _answer(self, request):
        self.requests.append(request)
        method, ident = request.get("method"), request.get("id")
        self.methods.append(method)
        if ident is None:
            return
        if method == "initialize":
            result = {"protocolVersion": "v2"}
        elif method == "thread/read":
            result = {"thread": {"id": request["params"]["threadId"], "status": {"type": "notLoaded"}, "turns": []}}
        elif method == "thread/turns/list":
            result = {"data": [{"status": "completed"}]}
        elif method == "thread/list":
            result = {"data": [{"id": THREAD, "status": {"type": "idle"}, "preview": "App title"}]}
        else:
            result = {}
        self.lines.put(json.dumps({"jsonrpc": "2.0", "id": ident, "result": result}) + "\n")

    def wait(self, timeout=None):
        return 0

    def poll(self):
        return None

    def terminate(self):
        self.lines.put(None)

    def kill(self):
        self.lines.put(None)


def test_candidates_require_nonarchived_app_and_metadata_records(tmp_path, monkeypatch):
    _metadata_db(tmp_path)
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))
    client = FakeClient(threads=[{"id": THREAD, "status": {"type": "idle"}, "preview": "App title"}])

    assert reset_delivery.list_candidates(client=client) == [{"id": THREAD, "title": "Task title", "status": "idle"}]
    assert reset_delivery.list_candidates(limit=100, client=FakeClient(threads=[{"id": "other", "status": "idle"}])) == []
    with pytest.raises(ValueError):
        reset_delivery.list_candidates(limit=101, client=client)


@pytest.mark.parametrize("turn_status,expected", [("completed", True), ("interrupted", True), ("failed", True), ("inProgress", False), (None, False)])
def test_not_loaded_requires_terminal_turn_and_no_question(tmp_path, monkeypatch, turn_status, expected):
    _metadata_db(tmp_path)
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))
    _projection(monkeypatch)
    turns = [{"status": turn_status}] if turn_status else []
    client = FakeClient(thread={"id": THREAD, "status": {"type": "notLoaded"}, "turns": turns})

    result = reset_delivery.inspect_target(THREAD, client=client)

    assert result["knownIdle"] is expected
    assert result["reason"] == ("not_loaded_terminal_turn" if expected else "not_loaded_without_terminal_turn")
    assert client.read_calls == [(THREAD, False)]


@pytest.mark.parametrize(
    "unfinished,reason,running,waiting",
    [
        ([{"type": "event_msg", "payload": {"type": "task_started"}},
          {"type": "response_item", "payload": {"type": "function_call", "name": "exec_command", "call_id": "cmd-1", "arguments": "{}"}}],
         "unfinished_rollout", True, False),
        ([{"type": "event_msg", "payload": {"type": "compaction_started"}}],
         "unfinished_rollout", True, False),
        ([{"type": "response_item", "payload": {"type": "function_call", "name": "request_user_input_async", "call_id": "ask-1", "arguments": json.dumps({"questions": [{"title": "Which option?"}]})}}],
         "waiting_for_input", False, True),
    ],
)
def test_not_loaded_terminal_turn_does_not_override_unfinished_rollout(
    tmp_path, monkeypatch, unfinished, reason, running, waiting
):
    codex_home, rollout_path = _metadata_db(tmp_path)
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    old = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat().replace("+00:00", "Z")
    records = [
        {"type": "session_meta", "payload": {"id": THREAD}},
        *[{**record, "timestamp": old} for record in unfinished],
    ]
    rollout_path.write_text("\n".join(json.dumps(record) for record in records) + "\n", encoding="utf-8")
    client = FakeClient(thread={"id": THREAD, "status": {"type": "notLoaded"}, "turns": [{"status": "completed"}]})

    result = reset_delivery.inspect_target(THREAD, client=client)

    assert result["knownIdle"] is False
    assert result["reason"] == reason
    assert result["running"] is running
    assert result["waiting"] is waiting


@pytest.mark.parametrize(
    "status,projection,expected_reason",
    [
        ({"type": "active", "activeFlags": []}, {}, "active"),
        ({"type": "active", "activeFlags": ["waitingOnUserInput"]}, {}, "waiting_for_input"),
        ({"type": "active", "activeFlags": ["waitingOnApproval"]}, {}, "waiting_for_input"),
        ({"type": "systemError"}, {}, "system_error"),
        ({"type": "mystery"}, {}, "unknown_status"),
        ({"type": "idle"}, {"question": "Need a choice"}, "waiting_for_input"),
        ({"type": "idle"}, {"unavailable": True}, "question_projection_unavailable"),
        ({"type": "idle"}, {"question_unknown": True}, "question_state_unknown"),
    ],
)
def test_inspector_withholds_busy_waiting_and_unknown_states(tmp_path, monkeypatch, status, projection, expected_reason):
    _metadata_db(tmp_path)
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))
    _projection(monkeypatch, **projection)
    client = FakeClient(thread={"id": THREAD, "status": status, "turns": []})

    result = reset_delivery.inspect_target(THREAD, client=client)

    assert result["knownIdle"] is False
    assert result["reason"] == expected_reason
    if status["type"] == "active":
        assert result["running"] is True


def test_archived_and_missing_rollout_metadata_fail_closed(tmp_path, monkeypatch):
    _metadata_db(tmp_path, archived=True)
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))
    client = FakeClient()
    archived = reset_delivery.inspect_target(THREAD, client=client)
    assert archived["archived"] is True
    assert archived["reason"] == "archived"
    assert client.read_calls == []

    _metadata_db(tmp_path, archived=False, rollout=False)
    missing = reset_delivery.inspect_target(THREAD, client=client)
    assert missing["knownIdle"] is False
    assert missing["reason"] == "rollout_metadata_missing"


def test_idle_with_transcript_question_and_resolution_rules(tmp_path, monkeypatch):
    _metadata_db(tmp_path)
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))
    client = FakeClient(thread={"id": THREAD, "status": {"type": "idle"}, "turns": [{"status": "completed"}]})
    _projection(monkeypatch, status="needs input", question="Choose one")
    waiting = reset_delivery.inspect_target(THREAD, client=client)
    assert waiting["waiting"] is True
    assert waiting["knownIdle"] is False

    _projection(monkeypatch, question="Old resolved question", resolution="2026-10-16T00:00:00+00:00")
    resolved = reset_delivery.inspect_target(THREAD, client=client)
    assert resolved["knownIdle"] is True


def test_idle_app_status_is_withheld_for_real_rollout_question(tmp_path, monkeypatch):
    codex_home, rollout_path = _metadata_db(tmp_path)
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    question = {
        "type": "response_item",
        "payload": {
            "type": "function_call",
            "name": "request_user_input_async",
            "call_id": "question-1",
            "arguments": json.dumps({"questions": [{"title": "Which option?"}]}),
        },
    }
    rollout_path.write_text(json.dumps(question) + "\n", encoding="utf-8")
    client = FakeClient(thread={"id": THREAD, "status": {"type": "idle"}, "turns": []})

    result = reset_delivery.inspect_target(THREAD, client=client)

    assert result["knownIdle"] is False
    assert result["waiting"] is True
    assert result["reason"] == "waiting_for_input"


def test_metadata_without_name_column_passes_real_dispatch_boundary(tmp_path, monkeypatch):
    codex_home, _ = _metadata_db(tmp_path, include_name=False)
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    client = FakeClient(thread={"id": THREAD, "status": {"type": "idle"}, "turns": []},
                        threads=[{"id": THREAD, "status": {"type": "idle"}}])
    state = {"schemaVersion": 2, "mode": "greet", "threadIds": [],
             "armed": False, "oneShot": True, "lastRun": None}
    queued = []
    dispatcher = ResetActionDispatcher(
        list_candidates=lambda limit=100: reset_delivery.list_candidates(limit=limit, client=client),
        inspect_target=lambda thread_id: reset_delivery.inspect_target(thread_id, client=client),
        queue_continue=lambda thread_id: queued.append(thread_id) or {"status": "queued"},
        clock=time.time,
        save=lambda: True,
        action_state=lambda: state,
        policies=TEST_POLICIES,
    )

    dispatcher.configure("continue", [THREAD])
    result = dispatcher.dispatch("reset-cycle-1")

    assert queued == [THREAD]
    assert result["targets"] == [{"threadId": THREAD, "status": "queued", "reason": None}]
    assert isinstance(state["lastRun"]["at"], float)


def test_queue_continue_uses_exact_message_without_context_overrides():
    calls = []

    def runner(*args, **kwargs):
        calls.append((args, kwargs))
        return type("Result", (), {"returncode": 0})()

    assert reset_delivery.queue_continue(THREAD, executable="codex.exe", runner=runner,
                                        policies=TEST_POLICIES) == {"status": "queued"}
    args, kwargs = calls[0]
    assert args[0] == ["codex.exe", "queue", "--thread", THREAD, "--message", "continue"]
    assert kwargs["shell"] is False
    assert "--model" not in args[0]
    assert "--sandbox" not in args[0]


def test_queue_timeout_is_unknown_without_retry_and_failure_is_rejected():
    calls = []

    def timeout(*args, **kwargs):
        calls.append(args)
        raise subprocess.TimeoutExpired(args[0], kwargs["timeout"])

    result = reset_delivery.queue_continue(THREAD, executable="codex.exe", runner=timeout,
                                           policies=TEST_POLICIES)
    assert result["status"] == "unknown"
    assert len(calls) == 1

    rejected = reset_delivery.queue_continue(
        THREAD, executable="codex.exe", runner=lambda *args, **kwargs: type("Result", (), {"returncode": 1})(),
        policies=TEST_POLICIES,
    )
    assert rejected["status"] == "rejected"


def test_missing_cli_and_invalid_id_never_invoke_runner(monkeypatch):
    called = []
    monkeypatch.setattr(reset_delivery.shutil, "which", lambda command: None)
    assert reset_delivery.queue_continue(THREAD, runner=lambda *a, **k: called.append(a),
                                         policies=TEST_POLICIES) == {
        "status": "rejected", "error": "Codex CLI is unavailable"
    }
    assert reset_delivery.queue_continue("not-a-thread", executable="codex.exe", runner=lambda *a, **k: called.append(a))["status"] == "rejected"
    assert called == []


def test_hidden_or_uncollected_candidates_are_denied_before_metadata_read(monkeypatch):
    denied = load_policies({"TOKEN_TRACKER_CODEX_COLLECT": "true", "TOKEN_TRACKER_CODEX_SHOW": "false"})
    monkeypatch.setattr(reset_delivery, "_metadata", lambda **_kwargs: pytest.fail("metadata read while hidden"))
    with pytest.raises(ValueError):
        reset_delivery.list_candidates(policies=denied)


def test_queue_requires_explicit_send_capability_before_runner():
    called = []
    denied = load_policies({})
    result = reset_delivery.queue_continue(
        THREAD, executable="codex.exe", runner=lambda *args, **kwargs: called.append(args), policies=denied,
    )
    assert result["status"] == "rejected"
    assert called == []


def test_denied_queue_does_not_discover_executable(monkeypatch):
    discovered = []
    monkeypatch.setattr(reset_delivery.shutil, "which", lambda name: discovered.append(name))
    result = reset_delivery.queue_continue(THREAD, policies=load_policies({}))
    assert result["status"] == "rejected"
    assert discovered == []
