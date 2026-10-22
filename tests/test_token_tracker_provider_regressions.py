import json
import math
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from token_tracker import providers
from token_tracker.providers import CodexProvider


def _stamp(epoch):
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat().replace("+00:00", "Z")


def _record(kind, at=None, payload=None, **extra):
    row = {"type": kind, **extra}
    if at is not None:
        row["timestamp"] = at
    if payload is not None:
        row["payload"] = payload
    return row


def _token(total, context, at):
    return _record("event_msg", at, {"type": "token_count", "info": {
        "total_token_usage": {"total_tokens": total},
        "last_token_usage": {"total_tokens": context},
        "model_context_window": 100,
    }})


def _rollout(path, rows):
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    return path


def _base_rows(now):
    at = _stamp(now - 10)
    return [
        _record("turn_context", at, {"model": "model-a"}),
        _record("event_msg", at, {"type": "task_started"}),
        _token(10, 8, at),
    ]


def _identity_record(identity, path=None, parent=None, *, archived=0, updated=0,
                     title=None, name=None, model="db-model-sentinel", status="failed",
                     observed="2999-01-01T00:00:00Z", pinned=1):
    source = ({"subagent": {"thread_spawn": {
        "parent_thread_id": parent, "agent_path": name or "Worker",
    }}} if parent else "vscode")
    return (identity, path, json.dumps(source), "A:/workspace", name, title, name,
            archived, updated, model, status, observed, pinned)


def _create_identity_db(root, rows):
    root.mkdir(parents=True, exist_ok=True)
    database = root / "state_5.sqlite"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE threads (id TEXT PRIMARY KEY, rollout_path TEXT, source TEXT, cwd TEXT, name TEXT, title TEXT, agent_path TEXT, archived INTEGER, updated_at INTEGER, model TEXT, status TEXT, statusObservedAt TEXT, is_pinned INTEGER)")
        connection.executemany("INSERT INTO threads VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
    return database


def _write_runtime_session(root, identity, status, *, parent=None, now=None, model="transcript-model"):
    at = _stamp(now if now is not None else datetime.now(timezone.utc).timestamp())
    path = root / "sessions" / f"{identity}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"id": identity}
    if parent:
        payload["parent_thread_id"] = parent
    rows = [_record("session_meta", at, payload)]
    if status == "needs input":
        rows.append(_record("response_item", at, {"type": "function_call", "name": "request_user_input",
                                                   "call_id": "ask-" + identity,
                                                   "arguments": json.dumps({"questions": [{"question": "Choose an option?"}]})}))
    else:
        rows.extend([
            _record("turn_context", at, {"model": model}),
            _record("event_msg", at, {"type": "task_started"}),
        ])
        if status == "idle":
            rows.append(_record("event_msg", at, {"type": "task_complete"}))
    return _rollout(path, rows)


def _make_runtime_session_with_context(root, identity, *, now, model, total, context):
    at = _stamp(now)
    path = root / "sessions" / f"{identity}-new.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    return _rollout(path, [
        _record("session_meta", at, {"id": identity}),
        _record("turn_context", at, {"model": model}),
        _record("event_msg", at, {"type": "task_started"}),
        _token(total, context, at),
    ])


def _make_runtime_thread(root, identity, status, *, parent=None, now=None, model="transcript-model"):
    at = _stamp(now if now is not None else datetime.now(timezone.utc).timestamp())
    path = root / "sessions" / f"{identity}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"id": identity}
    if parent:
        payload["parent_thread_id"] = parent
    records = [_record("session_meta", at, payload), _record("turn_context", at, {"model": model})]
    if status == "needs input":
        records.append(_record("response_item", at, {"type": "function_call", "name": "request_user_input",
                                                      "call_id": "ask-" + identity,
                                                      "arguments": json.dumps({"questions": [{"question": "Choose an option?"}]})}))
    else:
        records.append(_record("event_msg", at, {"type": "task_started"}))
        if status == "idle":
            records.append(_record("event_msg", at, {"type": "task_complete"}))
        elif status == "failed":
            records.append(_record("event_msg", at, {"type": "task_failed", "message": "failed"}))
        elif status == "running":
            records.append(_token(10, 4, at))
    return _rollout(path, records)


def test_last_good_snapshot_and_three_stale_fallbacks_are_isolated_and_idempotent(tmp_path, monkeypatch):
    monkeypatch.setattr(providers, "_LAST_GOOD", {})
    monkeypatch.setattr(providers, "_LIVE_INDEX", {})
    fixed_epoch = 1_800_000_000.0
    monkeypatch.setattr(providers.time, "time", lambda: fixed_epoch)
    path = str(tmp_path / "thread.jsonl")
    now = _stamp(fixed_epoch - 1)

    class Good:
        def snapshot(self):
            return {"name": "Codex", "connected": True, "chats": [
                {"id": "thread", "path": path, "status": "running", "statusSource": "app",
                 "appStatus": "running", "statusObservedAt": now, "nested": {"value": 1}},
                {"id": "thread", "path": path, "status": "idle", "nested": {"value": 2}},
            ]}

    class Failed:
        def snapshot(self):
            return {"name": "Codex", "connected": False, "error": "temporary"}

    good = providers._provider_snapshot(Good, "Codex", "blue")
    assert len(good["chats"]) == 1
    providers._index_live_paths([good])
    cached = providers._LAST_GOOD["Codex"]
    assert cached["chats"][0]["path"] == path
    assert cached["chats"][0]["nested"] == {"value": 1}
    good["chats"][0]["nested"]["value"] = 88
    assert providers._LAST_GOOD["Codex"]["chats"][0]["nested"] == {"value": 1}

    for _ in range(3):
        stale = providers._provider_snapshot(Failed, "Codex", "blue")
        assert stale["stale"] is True
        assert len(stale["chats"]) == 1
        stale["chats"][0]["nested"]["value"] = 99
        providers._index_live_paths([stale])
        assert len(stale["chats"]) == 1
        assert stale["chats"][0]["id"] == "thread"
        assert providers._LAST_GOOD["Codex"]["chats"][0]["path"] == path
        assert providers._LAST_GOOD["Codex"]["chats"][0]["nested"] == {"value": 1}


def test_fresh_epoch_rejects_future_and_expires_after_150_seconds():
    now = 1_800_000_000.0
    assert not providers._fresh_epoch(now + 0.001, now=now)
    assert providers._fresh_epoch(now, now=now)
    assert providers._fresh_epoch(now - 150, now=now)
    assert not providers._fresh_epoch(now - 150.001, now=now)


def test_live_index_path_change_keeps_valid_nulls_and_drops_old_epoch(tmp_path, monkeypatch):
    monkeypatch.setattr(providers, "_LIVE_INDEX", {})
    old = str(tmp_path / "old.jsonl")
    new = str(tmp_path / "new.jsonl")
    providers._index_live_paths([{"name": "Codex", "chats": [{
        "id": "thread", "path": old, "status": "running", "statusSource": "app",
        "statusObservedAt": datetime.now(timezone.utc).isoformat(), "contextTokens": 40,
    }]}])
    providers._index_live_paths([{"name": "Codex", "chats": [{
        "id": "thread", "path": new, "status": "idle", "statusSource": "transcript",
        "contextTokens": None, "contextObservedAt": None,
    }]}])
    row = providers._LIVE_INDEX["Codex:thread"]
    assert row["path"] == new
    assert row["contextTokens"] is None
    assert row["contextObservedAt"] is None
    assert row["status"] == "idle"


def test_provider_chat_dedupe_does_not_collapse_missing_or_empty_ids():
    provider = {"name": "Codex", "chats": [
        {"title": "missing one"}, {"title": "missing two"},
        {"id": "", "title": "empty one"}, {"id": "", "title": "empty two"},
    ]}
    providers._dedupe_provider_chats(provider)
    assert [row["title"] for row in provider["chats"]] == [
        "missing one", "missing two", "empty one", "empty two",
    ]


def test_stale_identity_expires_without_mutating_last_good(tmp_path, monkeypatch):
    monkeypatch.setattr(providers, "_LAST_GOOD", {})
    monkeypatch.setattr(providers, "_LIVE_INDEX", {})
    old = datetime.now(timezone.utc) - timedelta(minutes=10)
    path = str(tmp_path / "old-thread.jsonl")
    result = {"name": "Codex", "connected": True, "chats": [{
        "id": "old", "path": path, "status": "running", "statusSource": "app",
        "appStatus": "running", "statusObservedAt": old.isoformat(),
    }]}
    providers._LAST_GOOD["Codex"] = {**result, "builtAt": old.isoformat(),
                                     "_policyGeneration": providers.public_policies(providers.POLICIES)["generation"]}
    stale = providers._provider_snapshot(lambda: type("Failure", (), {
        "snapshot": lambda self: {"name": "Codex", "connected": False, "error": "offline"}
    })(), "Codex", "blue")
    providers._index_live_paths([stale])
    assert stale["chats"] == []
    assert providers._LAST_GOOD["Codex"]["chats"][0]["path"] == path
    assert providers._LIVE_INDEX == {}


def test_future_token_sample_matures_without_file_append(tmp_path, monkeypatch):
    now = [1_791_618_000.0]
    monkeypatch.setattr(providers.time, "time", lambda: now[0])
    future = _stamp(now[0] + 5)
    path = _rollout(tmp_path / "future-token.jsonl", _base_rows(now[0]) + [
        _record("turn_context", future, {"model": "model-b"}),
        _token(20, 18, future),
    ])
    before = path.stat()
    first = CodexProvider._last_token_event(str(path))
    assert first["total_token_usage"]["total_tokens"] == 10
    now[0] += 6
    second = CodexProvider._last_token_event(str(path))
    assert second["model"] == "model-b"
    assert second["total_token_usage"]["total_tokens"] == 20
    assert path.stat().st_size == before.st_size and path.stat().st_mtime_ns == before.st_mtime_ns


@pytest.mark.parametrize("kind,extra,payload", [
    ("event_msg", {}, {"type": "context_compaction_start"}),
    ("compacted", {}, {}),
    ("system", {"subtype": "compact_boundary"}, {}),
])
def test_same_model_future_compaction_invalidates_context_at_maturity(tmp_path, monkeypatch, kind, extra, payload):
    now = [1_791_618_000.0]
    monkeypatch.setattr(providers.time, "time", lambda: now[0])
    future = _stamp(now[0] + 5)
    path = _rollout(tmp_path / f"future-{kind}.jsonl", _base_rows(now[0]) + [
        _record(kind, future, payload, **extra),
    ])
    before = path.stat()
    first = CodexProvider._last_token_event(str(path))
    assert first["context_valid"] is True
    now[0] += 6
    second = CodexProvider._last_token_event(str(path))
    assert second["context_valid"] is False
    assert path.stat().st_size == before.st_size and path.stat().st_mtime_ns == before.st_mtime_ns


def test_future_deadlines_advance_one_event_at_a_time(tmp_path, monkeypatch):
    now = [1_791_618_000.0]
    monkeypatch.setattr(providers.time, "time", lambda: now[0])
    first_deadline, second_deadline = _stamp(now[0] + 5), _stamp(now[0] + 10)
    path = _rollout(tmp_path / "multiple-deadlines.jsonl", _base_rows(now[0]) + [
        _record("turn_context", first_deadline, {"model": "model-b"}),
        _token(20, 18, first_deadline),
        _record("event_msg", second_deadline, {"type": "context_compaction_start"}),
    ])
    first = CodexProvider._last_token_event(str(path))
    assert first["total_token_usage"]["total_tokens"] == 10
    now[0] += 6
    second = CodexProvider._last_token_event(str(path))
    assert second["total_token_usage"]["total_tokens"] == 20
    assert second["context_valid"] is True
    now[0] += 5
    third = CodexProvider._last_token_event(str(path))
    assert third["context_valid"] is False


def test_future_model_changes_away_and_back_invalidate_without_a_new_token(tmp_path, monkeypatch):
    now = [1_791_618_000.0]
    monkeypatch.setattr(providers.time, "time", lambda: now[0])
    path = _rollout(tmp_path / "future-model-return.jsonl", _base_rows(now[0]) + [
        _record("turn_context", _stamp(now[0] + 5), {"model": "model-b"}),
        _record("turn_context", _stamp(now[0] + 10), {"model": "model-a"}),
    ])
    calls = []
    original = CodexProvider._read_last_token_projection
    monkeypatch.setattr(CodexProvider, "_read_last_token_projection",
                        staticmethod(lambda item: calls.append(item) or original(item)))

    first = CodexProvider._last_token_event(str(path))
    assert first["context_valid"] is True
    now[0] += 6
    second = CodexProvider._last_token_event(str(path))
    assert second["context_valid"] is False
    assert len(calls) == 2
    now[0] += 5
    third = CodexProvider._last_token_event(str(path))
    assert third["model"] == "model-a"
    assert third["context_valid"] is False
    assert len(calls) == 3


def test_tail_unknown_model_uses_conservative_deadlines_for_future_return(tmp_path, monkeypatch):
    now = [1_791_618_000.0]
    monkeypatch.setattr(providers.time, "time", lambda: now[0])
    at = _stamp(now[0] - 10)
    path = _rollout(tmp_path / "tail-unknown-model.jsonl", [
        _record("turn_context", at, {"model": "model-a"}),
        _record("event_msg", at, {"type": "task_started"}),
        _record("response_item", at, {"type": "other", "content": "x" * 525_000}),
        _token(10, 8, at),
        _record("turn_context", _stamp(now[0] + 5), {"model": "model-b"}),
        _record("turn_context", _stamp(now[0] + 10), {"model": "model-a"}),
    ])
    before = path.stat()
    calls = []
    original = CodexProvider._read_last_token_projection
    monkeypatch.setattr(CodexProvider, "_read_last_token_projection",
                        staticmethod(lambda item: calls.append(item) or original(item)))

    first = CodexProvider._last_token_event(str(path))
    assert first["model"] == "model-a" and first["context_valid"] is True
    now[0] += 6
    second = CodexProvider._last_token_event(str(path))
    assert second["model"] == "model-a" and second["context_valid"] is False
    now[0] += 5
    third = CodexProvider._last_token_event(str(path))
    assert third["model"] == "model-a" and third["context_valid"] is False
    assert len(calls) == 3
    assert path.stat().st_size == before.st_size and path.stat().st_mtime_ns == before.st_mtime_ns


def test_same_future_model_does_not_expire_existing_context(tmp_path, monkeypatch):
    now = [1_791_618_000.0]
    monkeypatch.setattr(providers.time, "time", lambda: now[0])
    path = _rollout(tmp_path / "same-future-model.jsonl", _base_rows(now[0]) + [
        _record("turn_context", _stamp(now[0] + 5), {"model": "model-a"}),
    ])
    calls = []
    original = CodexProvider._read_last_token_projection
    monkeypatch.setattr(CodexProvider, "_read_last_token_projection",
                        staticmethod(lambda item: calls.append(item) or original(item)))

    first = CodexProvider._last_token_event(str(path))
    now[0] += 6
    second = CodexProvider._last_token_event(str(path))
    assert first["context_valid"] is True and second["context_valid"] is True
    assert len(calls) == 1


def test_irrelevant_malformed_and_far_future_events_have_bounded_memo_behavior(tmp_path, monkeypatch):
    now = [1_791_618_000.0]
    monkeypatch.setattr(providers.time, "time", lambda: now[0])
    malformed = _rollout(tmp_path / "malformed.jsonl", [
        _record("event_msg", "not-a-timestamp", {"type": "context_compaction_start"}),
    ])
    reads = []
    original = CodexProvider._read_last_token_projection
    monkeypatch.setattr(CodexProvider, "_read_last_token_projection",
                        staticmethod(lambda path: reads.append(path) or original(path)))
    assert CodexProvider._last_token_event(str(malformed)) is None
    now[0] += 20
    assert CodexProvider._last_token_event(str(malformed)) is None
    assert len(reads) == 1

    far_future = datetime(3000, 1, 1, tzinfo=timezone.utc).isoformat().replace("+00:00", "Z")
    far = _rollout(tmp_path / "far-future.jsonl", _base_rows(now[0]) + [
        _record("compacted", far_future, {}),
    ])
    projection = original(far)
    assert isinstance(projection, providers._TimedProjection)
    assert math.isfinite(projection.valid_until)
    assert projection.valid_until > now[0]


def test_irrelevant_future_event_does_not_expire_last_token_memo(tmp_path, monkeypatch):
    now = [1_791_618_000.0]
    monkeypatch.setattr(providers.time, "time", lambda: now[0])
    path = _rollout(tmp_path / "irrelevant-future.jsonl", _base_rows(now[0]) + [
        _record("event_msg", _stamp(now[0] + 12), {"type": "task_started"}),
    ])
    calls = []
    original = CodexProvider._read_last_token_projection
    monkeypatch.setattr(CodexProvider, "_read_last_token_projection",
                        staticmethod(lambda item: calls.append(item) or original(item)))
    first = CodexProvider._last_token_event(str(path))
    now[0] += 13
    second = CodexProvider._last_token_event(str(path))
    assert first["timestamp"] == second["timestamp"]
    assert len(calls) == 1


def test_unchanged_ordinary_token_projection_is_memoized(tmp_path, monkeypatch):
    now = [1_791_618_000.0]
    monkeypatch.setattr(providers.time, "time", lambda: now[0])
    path = _rollout(tmp_path / "ordinary.jsonl", _base_rows(now[0]))
    calls = []
    original = CodexProvider._read_last_token_projection
    monkeypatch.setattr(CodexProvider, "_read_last_token_projection",
                        staticmethod(lambda item: calls.append(item) or original(item)))
    first = CodexProvider._last_token_event(str(path))
    second = CodexProvider._last_token_event(str(path))
    assert first == second and len(calls) == 1


def test_full_snapshot_recovers_old_waiting_root_beyond_both_catalog_caps(tmp_path, monkeypatch):
    now = datetime.now(timezone.utc).timestamp()
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    waiting_path = _make_runtime_thread(tmp_path, "waiting-root", "needs input", now=now)
    finished_path = _make_runtime_thread(tmp_path, "finished-root", "idle", now=now - 5)
    rows = [_identity_record(f"newer-{index:03d}", f"missing-{index:03d}.jsonl",
                             updated=1000 + index, title=f"Newer {index}")
            for index in range(205)]
    rows.extend([
        _identity_record("waiting-root", str(waiting_path), updated=1, title="Waiting root",
                         model="db-model-sentinel", status="failed", observed="2999-01-01T00:00:00Z"),
        _identity_record("finished-root", str(finished_path), updated=0, title="Finished root"),
    ])
    _create_identity_db(tmp_path, rows)
    api_rows = [{"id": f"api-page-{index}", "title": f"API page {index}", "status": "idle"}
                for index in range(100)]

    chats = CodexProvider()._with_agents(api_rows)
    waiting = [chat for chat in chats if chat.get("id") == "waiting-root"]
    assert len(waiting) == 1
    assert waiting[0]["status"] == "needs input"
    assert waiting[0]["pendingQuestion"] == "Choose an option?"
    assert waiting[0]["statusSource"] == "transcript"
    assert waiting[0]["model"] == "transcript-model"
    assert waiting[0].get("statusObservedAt") is None
    identity = providers._codex_identity_snapshot(["waiting-root"], limit=0)["by_id"]["waiting-root"]
    assert not {"status", "model", "statusObservedAt", "updated_at", "is_pinned"}.intersection(identity)
    assert not any(chat.get("id") == "finished-root" for chat in chats)


def test_archived_roots_children_and_ancestors_and_finished_children_are_not_resurrected(tmp_path, monkeypatch):
    now = datetime.now(timezone.utc).timestamp()
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    paths = {
        "archived-root": _make_runtime_thread(tmp_path, "archived-root", "needs input", now=now),
        "valid-root": _make_runtime_thread(tmp_path, "valid-root", "running", now=now),
        "archived-child": _make_runtime_thread(tmp_path, "archived-child", "running", parent="valid-root", now=now),
        "archived-ancestor": _make_runtime_thread(tmp_path, "archived-ancestor", "needs input", now=now),
        "idle-middle": _make_runtime_thread(tmp_path, "idle-middle", "idle", parent="archived-ancestor", now=now),
        "grandchild": _make_runtime_thread(tmp_path, "grandchild", "running", parent="idle-middle", now=now),
        "finished-child": _make_runtime_thread(tmp_path, "finished-child", "idle", parent="valid-root", now=now),
    }
    rows = [
        _identity_record(identity, str(path), parent=parent, archived=int(identity in {
            "archived-root", "archived-child", "archived-ancestor",
        }), updated=now, title=identity)
        for identity, path, parent in [
            ("archived-root", paths["archived-root"], None),
            ("valid-root", paths["valid-root"], None),
            ("archived-child", paths["archived-child"], "valid-root"),
            ("archived-ancestor", paths["archived-ancestor"], None),
            ("idle-middle", paths["idle-middle"], "archived-ancestor"),
            ("grandchild", paths["grandchild"], "idle-middle"),
            ("finished-child", paths["finished-child"], "valid-root"),
        ]
    ]
    _create_identity_db(tmp_path, rows)

    chats = CodexProvider()._with_agents([])
    ids = {chat.get("id") for chat in chats}
    assert "valid-root" in ids
    assert not ids.intersection({"archived-root", "archived-child", "archived-ancestor", "idle-middle", "grandchild", "finished-child"})


def test_idle_parent_and_intermediate_anchor_survive_for_active_grandchild(tmp_path, monkeypatch):
    now = datetime.now(timezone.utc).timestamp()
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    rows = []
    for identity, status, parent in [
        ("root", "idle", None), ("middle", "idle", "root"), ("grandchild", "running", "middle"),
    ]:
        path = _make_runtime_thread(tmp_path, identity, status, parent=parent, now=now)
        rows.append(_identity_record(identity, str(path), parent=parent, updated=0, title=identity))
    _create_identity_db(tmp_path, rows)

    chats = {chat["id"]: chat for chat in CodexProvider()._with_agents([])}
    assert set(chats) == {"root", "middle", "grandchild"}
    assert chats["root"]["status"] == "idle"
    assert chats["middle"]["status"] == "idle"
    assert chats["grandchild"]["status"] == "running"
    assert chats["middle"]["parentId"] == "root"
    assert chats["grandchild"]["parentId"] == "middle"


@pytest.mark.parametrize("catalog_available", [False, True])
@pytest.mark.parametrize("with_prior_descendants", [False, True])
def test_descendant_discovery_requires_available_archive_eligibility(tmp_path, monkeypatch, catalog_available, with_prior_descendants):
    now = 1_800_000_000.0
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    monkeypatch.setattr(providers.time, "time", lambda: now)
    monkeypatch.setattr(providers, "_LIVE_INDEX", {})
    child = _make_runtime_thread(tmp_path, "child", "running", parent="root", now=now - 2)
    grandchild = _make_runtime_thread(tmp_path, "grandchild", "running", parent="child", now=now - 1)
    if with_prior_descendants:
        providers._LIVE_INDEX.update({
            "Codex:" + identity: {
                "id": identity, "path": str(path), "parentId": parent,
                "status": "running", "statusSource": "transcript", "lastValidAt": _stamp(now - 1),
            }
            for identity, path, parent in [("child", child, "root"), ("grandchild", grandchild, "child")]
        })
    _create_identity_db(tmp_path, [
        _identity_record("root"),
        _identity_record("child", str(child), parent="root", archived=int(not catalog_available)),
        _identity_record("grandchild", str(grandchild), parent="child"),
    ])
    if not catalog_available:
        def unavailable_catalog(*_args, **_kwargs):
            raise sqlite3.OperationalError("catalog unavailable")
        monkeypatch.setattr(providers.sqlite3, "connect", unavailable_catalog)

    chats = {row["id"]: row for row in CodexProvider()._with_agents([
        {"id": "root", "status": "running", "tokens": 0},
    ])}

    if catalog_available:
        assert set(chats) == {"root", "child", "grandchild"}
        assert chats["child"]["status"] == chats["grandchild"]["status"] == "running"
        assert chats["child"]["parentId"] == "root"
        assert chats["grandchild"]["parentId"] == "child"
    else:
        assert set(chats) == {"root"}, "archive-unverified descendants must not be shown or counted"
        assert chats["root"]["tokens"] == 0


@pytest.mark.parametrize("with_parent", [False, True])
def test_offline_normalized_rows_never_promote_app_observations_and_expire(tmp_path, monkeypatch, with_parent):
    fixed_now = [1_800_000_000.0]
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    monkeypatch.setattr(providers.time, "time", lambda: fixed_now[0])
    monkeypatch.setattr(providers, "_LIVE_INDEX", {})
    parent = "root" if with_parent else None
    path = _make_runtime_thread(tmp_path, "child", "running", parent=parent,
                                now=fixed_now[0] - 149)
    records = [_identity_record("child", str(path), parent=parent, title="Child")]
    if with_parent:
        records.append(_identity_record("root", None, title="Root"))
    _create_identity_db(tmp_path, records)
    fallback_row = {
        "id": "child", "path": str(path), "parentId": parent, "title": "Child",
        "status": "running", "statusSource": "app", "appStatus": "running",
        "statusObservedAt": _stamp(fixed_now[0]), "model": "untrusted-normalized-model",
        "contextTokens": 900,
    }
    provider = CodexProvider()
    chats = {chat["id"]: chat for chat in provider._with_agents([fallback_row], appserver_rows=False)}
    child = chats["child"]
    assert child["status"] == "running" and child["statusSource"] == "transcript"
    assert child.get("statusObservedAt") is None and child.get("appStatus") is None
    assert child.get("model") != "untrusted-normalized-model"
    assert child.get("contextTokens") != 900

    fixed_now[0] += 2
    chats = {chat["id"]: chat for chat in provider._with_agents([fallback_row], appserver_rows=False)}
    if with_parent:
        assert "child" not in chats
    else:
        child = chats["child"]
        assert child["status"] == "unavailable"
        assert child["statusSource"] != "app" and child.get("statusObservedAt") is None


@pytest.mark.parametrize("parent_state", ["fresh", "expired", "archived"])
def test_active_child_can_restore_only_fresh_failed_idle_parent_anchor(tmp_path, monkeypatch, parent_state):
    now = 1_800_000_000.0
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    monkeypatch.setattr(providers.time, "time", lambda: now)
    child_path = _make_runtime_thread(tmp_path, "child", "running", parent="parent", now=now - 2)
    child_row = {
        "id": "child", "path": str(child_path), "parentThreadId": "parent",
        "source": {"subagent": {"thread_spawn": {"parent_thread_id": "parent"}}},
        "archived": False, "title": "Child", "name": None, "cwd": None,
    }
    parent_path = str(tmp_path / "parent.jsonl")
    prior_time = now - (151 if parent_state == "expired" else 10)
    prior = {
        "id": "parent", "path": parent_path, "status": "idle", "statusSource": "transcript",
        "statusObservedAt": _stamp(prior_time), "lastValidAt": _stamp(prior_time),
        "parentId": None, "title": "Parent",
    }
    monkeypatch.setattr(providers, "_LIVE_INDEX", {"Codex:parent": prior})

    def identity_snapshot(_ids, limit=200):
        by_id = {"child": child_row}
        failed = {"parent"}
        if parent_state == "archived":
            by_id["parent"] = {"id": "parent", "path": parent_path, "parentThreadId": None, "archived": True}
            failed = set()
        return {"status": "partial" if failed else "ok", "catalog": [], "by_id": by_id,
                "no_match_ids": set(), "failed_ids": failed}

    monkeypatch.setattr(providers, "_codex_identity_snapshot", identity_snapshot)
    chats = {chat["id"]: chat for chat in CodexProvider()._with_agents([])}
    if parent_state == "fresh":
        assert set(chats) == {"parent", "child"}
        anchor = chats["parent"]
        assert anchor["status"] == "unavailable" and anchor["previousStatus"] == "idle"
        assert anchor["statusObservedAt"] == _stamp(prior_time)
        assert anchor["lastValidAt"] == _stamp(prior_time)
    else:
        assert "parent" not in chats and "child" not in chats


def test_identity_batch_distinguishes_partial_failure_from_no_match(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    _create_identity_db(tmp_path, [_identity_record("found", "found.jsonl", updated=1)])
    monkeypatch.setattr(providers, "_sqlite_variable_limit", lambda _connection: 1)
    original = providers._codex_identity_batch

    def fail_one(connection, ids):
        if ids == ["unreadable"]:
            raise sqlite3.OperationalError("synthetic batch failure")
        return original(connection, ids)

    monkeypatch.setattr(providers, "_codex_identity_batch", fail_one)
    snapshot = providers._codex_identity_snapshot(["found", "missing", "unreadable"], limit=0)
    assert snapshot["status"] == "partial"
    assert set(snapshot["by_id"]) == {"found"}
    assert snapshot["failed_ids"] == {"unreadable"}
    assert snapshot["no_match_ids"] == {"missing"}


def test_cli_only_install_can_discover_transcript_proven_descendants(tmp_path, monkeypatch):
    now = 1_800_000_090.0
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    monkeypatch.setattr(providers.time, "time", lambda: now)
    monkeypatch.setattr(providers, "_LIVE_INDEX", {})
    _make_runtime_thread(tmp_path, "child", "running", parent="root", now=now - 2)
    _make_runtime_thread(tmp_path, "grandchild", "running", parent="child", now=now - 1)
    assert not (tmp_path / "state_5.sqlite").exists()

    chats = {row["id"]: row for row in CodexProvider()._with_agents([
        {"id": "root", "status": "idle"},
    ])}

    assert set(chats) == {"root", "child", "grandchild"}
    assert chats["child"]["status"] == chats["grandchild"]["status"] == "running"
    assert chats["child"]["parentId"] == "root"
    assert chats["grandchild"]["parentId"] == "child"


@pytest.mark.parametrize("lookup_failure", ["unreadable_database", "per_id_batch"])
def test_catalog_failures_keep_only_the_confirmed_root(tmp_path, monkeypatch, lookup_failure):
    now = 1_800_000_100.0
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    monkeypatch.setattr(providers.time, "time", lambda: now)
    monkeypatch.setattr(providers, "_LIVE_INDEX", {})
    child = _make_runtime_thread(tmp_path, "child", "running", parent="root", now=now - 2)
    grandchild = _make_runtime_thread(tmp_path, "grandchild", "running", parent="child", now=now - 1)
    database = tmp_path / "state_5.sqlite"
    if lookup_failure == "unreadable_database":
        database.write_bytes(b"not a sqlite database")
    else:
        _create_identity_db(tmp_path, [
            _identity_record("root"),
            _identity_record("child", str(child), parent="root"),
            _identity_record("grandchild", str(grandchild), parent="child"),
        ])
        monkeypatch.setattr(providers, "_sqlite_variable_limit", lambda _connection: 1)
        original = providers._codex_identity_batch

        def fail_child(connection, ids):
            if ids == ["child"]:
                raise sqlite3.OperationalError("synthetic per-id failure")
            return original(connection, ids)

        monkeypatch.setattr(providers, "_codex_identity_batch", fail_child)

    chats = {row["id"]: row for row in CodexProvider()._with_agents([
        {"id": "root", "status": "running"},
    ])}

    assert set(chats) == {"root"}


@pytest.mark.parametrize("query_state", ["partial", "unavailable"])
def test_partial_identity_failure_retains_recent_truth_but_not_expired_or_unknown_rows(tmp_path, monkeypatch, query_state):
    now = datetime.now(timezone.utc)
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    _make_runtime_thread(tmp_path, "new-root", "running", now=now.timestamp())
    monkeypatch.setattr(providers, "_LIVE_INDEX", {
        "Codex:prior": {"id": "prior", "path": str(tmp_path / "prior.jsonl"),
                        "status": "running", "statusSource": "app",
                        "statusObservedAt": (now - timedelta(seconds=10)).isoformat(),
                        "lastValidAt": (now - timedelta(seconds=10)).isoformat()},
        "Codex:expired": {"id": "expired", "path": str(tmp_path / "expired.jsonl"),
                          "status": "running", "statusSource": "app",
                          "statusObservedAt": (now - timedelta(minutes=10)).isoformat(),
                          "lastValidAt": (now - timedelta(minutes=10)).isoformat()},
    })

    def partial(ids, limit=200):
        return {"status": query_state, "catalog": [], "by_id": {}, "no_match_ids": set(),
                "failed_ids": set(ids)}

    monkeypatch.setattr(providers, "_codex_identity_snapshot", partial)
    api = {"id": "api-current", "title": "Current", "status": "running",
           "statusSource": "app", "statusObservedAt": now.isoformat()}
    chats = CodexProvider()._with_agents([api])
    rows = {chat["id"]: chat for chat in chats}
    assert rows["api-current"]["status"] == "running"
    assert rows["prior"]["status"] == "unavailable"
    assert rows["prior"]["previousStatus"] == "running"
    assert rows["prior"]["lastValidAt"] == (now - timedelta(seconds=10)).isoformat()
    assert "new-root" not in rows and "expired" not in rows


@pytest.mark.parametrize("appserver_available", [True, False])
def test_snapshot_runs_the_same_identity_join_after_server_success_or_fallback(monkeypatch, appserver_available):
    class Client:
        def snapshot(self):
            if not appserver_available:
                raise providers.CodexUnavailable("offline")
            return type("Snapshot", (), {
                "rate_limits": {"rateLimits": {}},
                "usage": {"summary": {}, "dailyUsageBuckets": []},
                "threads": [],
            })()

    provider = CodexProvider(client=Client())
    monkeypatch.setattr(provider, "_local_usage", lambda **_kwargs: {
        "daily": {}, "totals": {}, "models": {}, "rate": None,
        "rateUpdatedAt": None, "paths": [],
    })
    fallback_rows = [{"id": "fallback-root", "status": "running"}]
    monkeypatch.setattr(provider, "_local_threads", lambda **_kwargs: fallback_rows)
    calls = []

    def join(chats, *, appserver_rows=True, session_paths=None):
        calls.append((appserver_rows, [chat.get("id") for chat in chats]))
        return chats

    monkeypatch.setattr(provider, "_with_agents", join)
    snapshot = provider.snapshot()
    assert calls == [(appserver_available, [] if appserver_available else ["fallback-root"])]
    assert snapshot["chats"] == ([] if appserver_available else fallback_rows)


def test_resumed_identity_uses_newest_rollout_epoch_and_discards_old_context(tmp_path, monkeypatch):
    now = datetime.now(timezone.utc).timestamp()
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    old = _write_runtime_session(tmp_path, "resumed", "running", now=now - 300, model="old-model")
    new = _make_runtime_session_with_context(tmp_path, "resumed", now=now, model="new-model", total=20, context=7)
    _create_identity_db(tmp_path, [_identity_record("resumed", str(new), updated=int(now), title="Resumed")])
    monkeypatch.setattr(providers, "_LIVE_INDEX", {
        "Codex:resumed": {"id": "resumed", "path": str(old), "status": "running",
                          "statusSource": "app", "appStatus": "running",
                          "statusObservedAt": _stamp(now), "contextTokens": 99,
                          "contextWindow": 100, "contextModel": "old-model",
                          "lastValidAt": _stamp(now)},
    })

    chats = CodexProvider()._with_agents([])
    providers._index_live_paths([{"name": "Codex", "chats": chats}])
    row = providers._LIVE_INDEX["Codex:resumed"]
    assert row["path"] == str(new)
    assert row["contextTokens"] == 7
    assert row["contextModel"] == "new-model"


def test_live_poll_does_not_walk_transcript_tree(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    monkeypatch.setattr(providers, "ENABLED_PROVIDERS", ("Codex",))
    monkeypatch.setattr(providers, "_LIVE_INDEX", {})
    (tmp_path / "sessions").mkdir()
    _create_identity_db(tmp_path, [])
    original_glob = Path.glob

    def reject_transcript_walk(path, pattern):
        if pattern == "**/*.jsonl":
            pytest.fail("live polling must not walk the transcript tree")
        return original_glob(path, pattern)

    monkeypatch.setattr(Path, "glob", reject_transcript_walk)
    providers.live_state()


def test_recovered_root_keeps_existing_pin_key_for_trim():
    snapshot = {"providers": [{"name": "Codex", "chats": [
        {"id": "recovered", "title": "Recovered active root", "status": "needs input", "updatedAt": "2026-10-21T00:00:00Z"},
        {"id": "other", "title": "Other", "status": "idle", "updatedAt": "2026-10-20T23:00:00Z"},
    ]}]}
    trimmed = providers.trim_chats(snapshot, limit=1, pinned=["Codex:recovered"])
    assert [chat["id"] for chat in trimmed["providers"][0]["chats"]] == ["recovered"]


def test_async_question_keeps_fresh_execution_tool_evidence(tmp_path, monkeypatch):
    now = 1_800_000_050.0
    monkeypatch.setattr(providers.time, "time", lambda: now)
    path = _rollout(tmp_path / "async-activity.jsonl", [
        _record("event_msg", _stamp(now - 8), {"type": "task_started"}),
        _record("response_item", _stamp(now - 7), {
            "type": "function_call", "name": "read_file", "call_id": "read-1",
            "arguments": json.dumps({"path": "src/app.js"}),
        }),
        _record("response_item", _stamp(now - 4), {
            "type": "function_call", "name": "request_user_input_async", "call_id": "ask-1",
            "arguments": json.dumps({"questions": [{"question": "Continue?"}]}),
        }),
    ])

    state = providers._codex_rollout_state(str(path))

    assert state["executionStatus"] == "running"
    assert state["executionObservedAt"] == _stamp(now - 7)
    assert state["executionActivity"]["kind"] == "reading"
    assert state["executionActivity"]["files"] == ["src/app.js"]


@pytest.mark.parametrize("message_offset,append_read", [(-25, False), (-5, True)])
def test_user_message_clears_only_a_current_execution_blocker(tmp_path, monkeypatch, message_offset, append_read):
    now = 1_800_000_070.0
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    monkeypatch.setattr(providers.time, "time", lambda: now)
    path = _rollout(tmp_path / "old-blocker.jsonl", [
        _record("event_msg", _stamp(now - 100), {"type": "task_started"}),
        _record("event_msg", _stamp(now - 10), {"type": "task_complete"}),
    ])
    completed = providers._codex_rollout_state(str(path))
    terminal = _stamp(now - 10)
    assert completed["executionStatus"] == "idle" and completed["executionTerminalAt"] == terminal

    def append(record):
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record) + "\n")

    append(_record("response_item", _stamp(now - 30), {
        "type": "function_call", "name": "request_user_input", "call_id": "delayed-ask",
        "arguments": json.dumps({"questions": [{"question": "Old blocker?"}]}),
    }))
    append(_record("event_msg", _stamp(now + message_offset), {
        "type": "user_message", "message": "Continue",
    }))
    if append_read:
        append(_record("response_item", _stamp(now - 1), {
            "type": "function_call", "name": "read_file", "call_id": "post-terminal-read",
            "arguments": json.dumps({"path": "src/app.js"}),
        }))

    state = providers._codex_rollout_state(str(path))
    assert state["executionStatus"] == "idle"
    assert state["executionTerminalAt"] == terminal
    assert state["executionObservedAt"] == terminal
    assert state["executionActivity"] is None


def test_terminal_admission_respects_run_and_resume_boundaries(tmp_path, monkeypatch):
    now = 1_800_000_080.0
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    monkeypatch.setattr(providers.time, "time", lambda: now)

    path = _rollout(tmp_path / "terminal-before-run.jsonl", [
        _record("event_msg", _stamp(now - 20), {"type": "task_started"}),
    ])
    started = providers._codex_rollout_state(str(path))
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(_record("event_msg", _stamp(now - 50), {"type": "task_complete"})) + "\n")
    stale_terminal = providers._codex_rollout_state(str(path))
    assert stale_terminal["executionStatus"] == "running"
    assert stale_terminal["executionTerminalAt"] is None
    assert stale_terminal["executionObservedAt"] == started["executionObservedAt"]
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(_record("response_item", _stamp(now - 1), {
            "type": "function_call", "name": "read_file", "call_id": "read-after-stale-end",
            "arguments": json.dumps({"path": "src/app.js"}),
        })) + "\n")
    resumed = providers._codex_rollout_state(str(path))
    assert resumed["executionStatus"] == "running"
    assert resumed["executionActivity"]["kind"] == "reading"

    blocked = _rollout(tmp_path / "terminal-before-resume.jsonl", [
        _record("event_msg", _stamp(now - 20), {"type": "task_started"}),
        _record("response_item", _stamp(now - 10), {
            "type": "function_call", "name": "request_user_input", "call_id": "ask-before-resume",
            "arguments": json.dumps({"questions": [{"question": "Continue?"}]}),
        }),
    ])
    assert providers._codex_rollout_state(str(blocked))["executionStatus"] == "waiting"
    with blocked.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(_record("event_msg", _stamp(now - 5), {
            "type": "user_message", "message": "Continue",
        })) + "\n")
    cleared = providers._codex_rollout_state(str(blocked))
    assert cleared["executionStatus"] == "unknown"
    with blocked.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(_record("event_msg", _stamp(now - 8), {"type": "task_complete"})) + "\n")
    delayed_terminal = providers._codex_rollout_state(str(blocked))
    assert delayed_terminal["executionStatus"] == "unknown"
    assert delayed_terminal["executionTerminalAt"] is None
    with blocked.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(_record("response_item", _stamp(now - 1), {
            "type": "function_call", "name": "read_file", "call_id": "read-after-resume",
            "arguments": json.dumps({"path": "src/app.js"}),
        })) + "\n")
    after_boundary = providers._codex_rollout_state(str(blocked))
    assert after_boundary["executionStatus"] == "running"
    assert after_boundary["executionActivity"]["kind"] == "reading"


@pytest.mark.parametrize("delayed_terminal", [
    {"type": "event_msg", "payload": {"type": "task_complete"}},
    {"type": "event_msg", "payload": {"type": "task_failed"}},
    {"type": "response_item", "payload": {"type": "message", "role": "assistant", "channel": "final"}},
])
def test_delayed_lifecycle_records_cannot_resurrect_execution_across_polls(tmp_path, monkeypatch, delayed_terminal):
    now = 1_800_000_060.0
    monkeypatch.setattr(providers.time, "time", lambda: now)
    path = _rollout(tmp_path / "delayed-lifecycle.jsonl", [
        _record("event_msg", _stamp(now - 100), {"type": "task_started"}),
        _record("event_msg", _stamp(now - 10), {"type": "task_complete"}),
    ])
    first = providers._codex_rollout_state(str(path))
    assert first["executionStatus"] == "idle"
    assert first["executionTerminalAt"] == _stamp(now - 10)

    with path.open("a", encoding="utf-8") as handle:
        for record in [
            {**delayed_terminal, "timestamp": _stamp(now - 50)},
            _record("event_msg", _stamp(now - 20), {"type": "task_started"}),
            _record("response_item", _stamp(now - 1), {
                "type": "function_call", "name": "read_file", "call_id": "delayed-read",
                "arguments": json.dumps({"path": "src/app.js"}),
            }),
        ]:
            handle.write(json.dumps(record) + "\n")

    second = providers._codex_rollout_state(str(path))
    assert second["executionStatus"] == "idle"
    assert second["executionTerminalAt"] == first["executionTerminalAt"]
    assert second["executionObservedAt"] == _stamp(now - 10)
    assert second["executionActivity"] is None
    assert providers._codex_execution_projection(second, "running", True, _stamp(now), now)["executionStatus"] == "idle"


@pytest.mark.parametrize("unqualified_offset", [-6, -5])
def test_user_message_clears_blocker_and_requires_work_after_resume_boundary(tmp_path, monkeypatch, unqualified_offset):
    now = 1_800_000_060.0
    monkeypatch.setattr(providers.time, "time", lambda: now)
    path = _rollout(tmp_path / "user-clears-blocker.jsonl", [
        _record("event_msg", _stamp(now - 20), {"type": "task_started"}),
        _record("response_item", _stamp(now - 10), {
            "type": "function_call", "name": "request_user_input", "call_id": "blocking-ask",
            "arguments": json.dumps({"questions": [{"question": "Choose?"}]}),
        }),
    ])
    waiting = providers._codex_rollout_state(str(path))
    assert waiting["executionStatus"] == "waiting" and waiting["question"] == "Choose?"

    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(_record("event_msg", _stamp(now - 5), {
            "type": "user_message", "message": "Continue",
        })) + "\n")
    cleared = providers._codex_rollout_state(str(path))
    assert cleared["question"] is None
    assert cleared["executionStatus"] == "unknown"
    assert cleared["executionObservedAt"] is None and cleared["executionActivity"] is None
    assert providers._codex_execution_projection(cleared, "running", True, _stamp(now), now)["executionStatus"] == "unavailable"

    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(_record("response_item", _stamp(now + unqualified_offset), {
            "type": "function_call", "name": "read_file", "call_id": "before-resume",
        })) + "\n")
    assert providers._codex_rollout_state(str(path))["executionStatus"] == "unknown"

    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(_record("response_item", _stamp(now - 1), {
            "type": "function_call", "name": "read_file", "call_id": "after-resume",
            "arguments": json.dumps({"path": "src/app.js"}),
        })) + "\n")
    resumed = providers._codex_rollout_state(str(path))
    assert resumed["question"] is None
    assert resumed["executionStatus"] == "running"
    assert resumed["executionSince"] == _stamp(now - 5)
    assert resumed["executionObservedAt"] == _stamp(now - 1)
    assert resumed["executionActivity"]["kind"] == "reading"


def test_resolved_blocking_question_clears_wait_without_inventing_running_state(tmp_path, monkeypatch):
    now = 1_800_000_060.0
    monkeypatch.setattr(providers.time, "time", lambda: now)
    path = _rollout(tmp_path / "blocking-answer.jsonl", [
        _record("event_msg", _stamp(now - 8), {"type": "task_started"}),
        _record("response_item", _stamp(now - 7), {
            "type": "function_call", "name": "read_file", "call_id": "read-1",
            "arguments": json.dumps({"path": "src/app.js"}),
        }),
        _record("response_item", _stamp(now - 4), {
            "type": "function_call", "name": "request_user_input", "call_id": "ask-sync",
            "arguments": json.dumps({"questions": [{"question": "Choose?"}]}),
        }),
        _record("response_item", _stamp(now - 1), {
            "type": "function_call_output", "call_id": "ask-sync",
            "output": json.dumps({"accepted": False, "answer": "Continue"}),
        }),
    ])

    state = providers._codex_rollout_state(str(path))

    assert state["executionStatus"] == "unknown"
    assert state["executionObservedAt"] is None
    assert state["executionActivity"] is None


@pytest.mark.parametrize("scenario", [
    "current", "before-run-floor", "future", "after-terminal", "blocked", "compacting", "before-latest",
])
def test_reasoning_updates_only_proven_current_running_execution(tmp_path, monkeypatch, scenario):
    now = 1_800_000_080.0
    monkeypatch.setattr(providers.time, "time", lambda: now)
    records = [_record("event_msg", _stamp(now - 8), {"type": "task_started"})]
    reasoning_at = now - 7
    expected_status, expected_at, expected_activity = "running", _stamp(now - 7), "Reasoning"
    if scenario == "before-run-floor":
        reasoning_at = now - 9
        expected_at, expected_activity = _stamp(now - 8), None
    elif scenario == "future":
        reasoning_at = now + 5
        expected_at, expected_activity = _stamp(now - 8), None
    elif scenario == "after-terminal":
        records.append(_record("event_msg", _stamp(now - 4), {"type": "task_complete"}))
        reasoning_at = now - 3
        expected_status, expected_at, expected_activity = "idle", _stamp(now - 4), None
    elif scenario == "blocked":
        records.append(_record("response_item", _stamp(now - 4), {
            "type": "function_call", "name": "request_user_input", "call_id": "ask",
            "arguments": json.dumps({"questions": [{"question": "Choose?"}]}),
        }))
        reasoning_at = now - 3
        expected_status, expected_at, expected_activity = "waiting", _stamp(now - 4), None
    elif scenario == "compacting":
        records.append(_record("event_msg", _stamp(now - 4), {"type": "compaction_started"}))
        reasoning_at = now - 3
        expected_status, expected_at, expected_activity = "compacting", _stamp(now - 4), None
    elif scenario == "before-latest":
        records.append(_record("response_item", _stamp(now - 6), {
            "type": "function_call", "name": "read_file", "call_id": "read-current",
            "arguments": json.dumps({"path": "src/app.js"}),
        }))
        reasoning_at = now - 7
        expected_at, expected_activity = _stamp(now - 6), "reading"
    records.append(_record("response_item", _stamp(reasoning_at), {
        "type": "message", "role": "assistant", "channel": "analysis",
        "content": [{"type": "reasoning", "text": "private reasoning text"}],
    }))
    path = _rollout(tmp_path / f"reasoning-{scenario}.jsonl", records)

    state = providers._codex_rollout_state(str(path))

    assert state["executionStatus"] == expected_status
    assert state["executionObservedAt"] == expected_at
    if expected_activity is None:
        assert state["executionActivity"] is None
    elif expected_activity == "Reasoning":
        assert state["executionActivity"] == {
            "kind": "thinking", "label": "Reasoning", "files": [], "observedAt": _stamp(reasoning_at),
        }
        assert "private reasoning text" not in str(state["executionActivity"])
        assert state["executionSince"] == _stamp(now - 8)
    else:
        assert state["executionActivity"]["kind"] == expected_activity


def test_timestamp_less_context_tool_and_reasoning_do_not_refresh_execution(tmp_path, monkeypatch):
    now = 1_800_000_065.0
    monkeypatch.setattr(providers.time, "time", lambda: now)
    path = _rollout(tmp_path / "untimed-execution-inputs.jsonl", [
        _record("event_msg", _stamp(now - 8), {"type": "task_started"}),
        _record("event_msg", _stamp(now - 4), {"type": "task_complete"}),
        {"type": "turn_context", "payload": {"model": "metadata-only"}},
        {"type": "response_item", "payload": {
            "type": "function_call", "name": "read_file", "call_id": "untimed-call",
            "arguments": json.dumps({"path": "src/app.js"}),
        }},
        {"type": "response_item", "payload": {
            "type": "message", "role": "assistant", "channel": "analysis",
            "content": [{"type": "reasoning", "text": "private reasoning text"}],
        }},
    ])

    state = providers._codex_rollout_state(str(path))

    assert state["executionStatus"] == "idle"
    assert state["executionTerminalAt"] == _stamp(now - 4)
    assert state["executionObservedAt"] == _stamp(now - 4)
    assert state["executionActivity"] is None


def test_execution_projection_reconciles_stale_transcript_with_app_terminal_in_snapshot_and_live(tmp_path, monkeypatch):
    now = 1_800_000_090.0
    monkeypatch.setattr(providers.time, "time", lambda: now)
    stale = providers._codex_execution_projection({
        "executionStatus": "running", "executionObservedAt": _stamp(now - 151),
        "executionSince": _stamp(now - 180), "executionActivity": {"kind": "reading"},
    }, None, False, None, now=now)
    assert stale["executionStatus"] == "unavailable"
    assert stale["executionObservedAt"] is None and stale["executionActivity"] is None

    path = _rollout(tmp_path / "app-terminal.jsonl", [
        _record("event_msg", _stamp(now - 10), {"type": "task_started"}),
    ])
    app_time = _stamp(now - 1)
    snapshot = CodexProvider._thread({
        "id": "thread", "path": str(path), "status": {"type": "idle"},
        "statusObservedAt": app_time,
    })
    assert snapshot["executionStatus"] == "idle"
    assert snapshot["executionSource"] == "app"
    assert snapshot["executionTerminalAt"] == app_time

    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    monkeypatch.setattr(providers, "ENABLED_PROVIDERS", ("Codex",))
    monkeypatch.setattr(providers, "_persisted_codex_threads", lambda limit=200: [])
    monkeypatch.setattr(providers, "_LIVE_INDEX", {"Codex:thread": {
        "id": "thread", "path": str(path), "status": "idle", "statusSource": "app",
        "appStatus": "idle", "statusObservedAt": app_time,
    }})
    live = providers.live_state()
    chat = next(row for row in live["chats"] if row["key"] == "Codex:thread")
    assert chat["executionStatus"] == "idle"
    assert chat["executionSource"] == "app"
    assert chat["executionTerminalAt"] == app_time


def test_codex_snapshot_reuses_one_immutable_session_inventory(tmp_path, monkeypatch):
    paths = (tmp_path / "one.jsonl", tmp_path / "two.jsonl")
    inventory_calls = []
    seen = []

    def inventory():
        inventory_calls.append(True)
        return paths

    monkeypatch.setattr(providers, "_codex_session_inventory", inventory)

    class OfflineClient:
        def snapshot(self):
            raise providers.CodexUnavailable("offline")

    provider = CodexProvider(client=OfflineClient())
    monkeypatch.setattr(provider, "_local_usage", lambda **kwargs: seen.append(("usage", kwargs["session_paths"])) or {
        "daily": {}, "totals": {}, "models": {}, "rate": None, "rateUpdatedAt": None, "paths": [],
    })
    monkeypatch.setattr(provider, "_local_threads", lambda **kwargs: seen.append(("fallback", kwargs["session_paths"])) or [])
    monkeypatch.setattr(provider, "_with_agents", lambda chats, **kwargs: seen.append(("ownership", kwargs["session_paths"])) or chats)

    provider.snapshot()

    assert len(inventory_calls) == 1
    assert [name for name, _paths in seen] == ["usage", "fallback", "ownership"]
    assert all(selected is paths for _name, selected in seen)


def test_meta_cache_reloads_ownership_after_same_path_replacement(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    child_path = tmp_path / "sessions" / "same-path.jsonl"
    child_path.parent.mkdir(parents=True)
    _create_identity_db(tmp_path, [_identity_record("parent", None, title="Parent")])

    def write_child(identity):
        rows = [
            _record("session_meta", _stamp(datetime.now(timezone.utc).timestamp()),
                    {"id": identity, "parent_thread_id": "parent"}),
            _record("turn_context", _stamp(datetime.now(timezone.utc).timestamp()), {"model": "test-model"}),
            _record("event_msg", _stamp(datetime.now(timezone.utc).timestamp()), {"type": "task_started"}),
        ]
        _rollout(child_path, rows)

    provider = CodexProvider()
    write_child("first-child")
    first = {row["id"] for row in provider._with_agents([])}
    assert "first-child" in first

    write_child("replacement-child-with-longer-id")
    second = {row["id"] for row in provider._with_agents([])}
    assert "replacement-child-with-longer-id" in second
    assert "first-child" not in second


# ---- incremental reducer fast path / build-scoped memo ----

def _count_reduce(path, kind="perf-test-count", project=None):
    def reduce_record(state, item):
        state["n"] += 1
        state["items"].append(item.get("v"))

    return providers._incremental_jsonl_reduce(
        kind, path, lambda: {"n": 0, "items": []}, reduce_record, project, unavailable={})


def _write_lines(path, values):
    path.write_text("".join(json.dumps({"v": value}) + "\n" for value in values), encoding="utf-8")


def test_unchanged_checkpoint_does_not_open_file(tmp_path, monkeypatch):
    path = tmp_path / "a.jsonl"
    _write_lines(path, [1, 2, 3])
    assert _count_reduce(path)["n"] == 3
    opened = []
    real_open = Path.open
    monkeypatch.setattr(Path, "open", lambda self, *a, **k: (opened.append(self), real_open(self, *a, **k))[1])
    guards = []
    monkeypatch.setattr(providers, "_jsonl_guards", lambda *a: guards.append(a) or (b"", b""))
    assert _count_reduce(path)["items"] == [1, 2, 3]
    assert opened == [] and guards == []


def test_same_size_rewrite_with_new_mtime_invalidates(tmp_path):
    import os
    path = tmp_path / "b.jsonl"
    _write_lines(path, [1, 2, 3])
    assert _count_reduce(path)["items"] == [1, 2, 3]
    size = path.stat().st_size
    _write_lines(path, [7, 8, 9])
    assert path.stat().st_size == size
    stat = path.stat()
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 5_000_000_000))
    assert _count_reduce(path)["items"] == [7, 8, 9]


def test_append_is_incremental_but_guarded(tmp_path):
    path = tmp_path / "c.jsonl"
    _write_lines(path, [1, 2, 3])
    _count_reduce(path)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"v": 4}) + "\n")
    assert _count_reduce(path)["items"] == [1, 2, 3, 4]
    # Rewrite that grows the file but changes already-checkpointed bytes.
    _write_lines(path, [9, 9, 9, 9, 9, 9])
    assert _count_reduce(path)["items"] == [9] * 6


def test_truncation_and_replacement_invalidate(tmp_path):
    path = tmp_path / "d.jsonl"
    _write_lines(path, [1, 2, 3, 4])
    _count_reduce(path)
    _write_lines(path, [1, 2])
    assert _count_reduce(path)["items"] == [1, 2]
    other = tmp_path / "other.jsonl"
    _write_lines(other, [5, 6])
    other.replace(path)
    assert _count_reduce(path)["items"] == [5, 6]
    path.unlink()
    assert _count_reduce(path) == {}


def test_caller_mutation_never_reaches_next_call(tmp_path):
    path = tmp_path / "e.jsonl"
    _write_lines(path, [1, 2])
    first = _count_reduce(path)
    first["items"].append("poison")
    first["n"] = 99
    second = _count_reduce(path)
    assert second == {"n": 2, "items": [1, 2]}
    second["items"].clear()
    assert _count_reduce(path) == {"n": 2, "items": [1, 2]}
    projected = _count_reduce(path, kind="perf-test-proj", project=lambda s: s)
    projected["items"].append("x")
    assert _count_reduce(path, kind="perf-test-proj", project=lambda s: s)["items"] == [1, 2]


def test_build_scope_stats_and_resolves_each_path_once(tmp_path, monkeypatch):
    path = tmp_path / "f.jsonl"
    _write_lines(path, [1])
    _count_reduce(path)
    calls = {"stat": 0, "resolve": 0}
    real_stat, real_resolve = Path.stat, Path.resolve

    def stat(self, *a, **k):
        calls["stat"] += 1
        return real_stat(self, *a, **k)

    def resolve(self, *a, **k):
        calls["resolve"] += 1
        return real_resolve(self, *a, **k)

    monkeypatch.setattr(Path, "stat", stat)
    monkeypatch.setattr(Path, "resolve", resolve)
    with providers._build_scope():
        for _ in range(5):
            _count_reduce(path)
            _count_reduce(path, kind="perf-test-other")
    assert calls["resolve"] == 1
    assert calls["stat"] <= 3  # one pre-read stat; the second kind's first read adds post-read checks
    assert providers._BUILD_MEMO.get() is None
    with providers._build_scope():
        pass
    before = dict(calls)
    _count_reduce(path)
    assert calls["resolve"] == before["resolve"] + 1  # no cross-build memo


def test_stale_memoized_stat_is_refreshed_not_treated_as_rewrite(tmp_path):
    path = tmp_path / "g.jsonl"
    _write_lines(path, [1, 2])
    with providers._build_scope():
        assert _count_reduce(path, kind="perf-test-stale")["n"] == 2
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({"v": 3}) + "\n")
        # Memo holds the old stat, but a changed file must still be read fully, not as empty.
        assert _count_reduce(path, kind="perf-test-stale-2")["items"] == [1, 2, 3]
