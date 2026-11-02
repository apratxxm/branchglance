"""Budgeted live_state keeps the full roster, makes progress each round, and backs off without it."""

from __future__ import annotations

import copy
import json
import os
import sqlite3
import time
import types
from datetime import datetime, timezone

import pytest

from token_tracker import agent_links, claude_provider, providers, server
from token_tracker.provider_policy import load_policies


@pytest.fixture
def claude_live(tmp_path, monkeypatch):
    monkeypatch.setattr(providers, "ENABLED_PROVIDERS", ("Claude",))
    monkeypatch.setattr(providers, "LIMIT_ONLY_PROVIDERS", ())
    monkeypatch.setattr(providers, "POLICIES", load_policies({
        "TOKEN_TRACKER_CLAUDE_COLLECT": "true", "TOKEN_TRACKER_CLAUDE_SHOW": "true",
    }))
    monkeypatch.setattr(providers, "_LIVE_INDEX", {})
    monkeypatch.setattr(providers, "_LIVE_DETAILED", {})
    monkeypatch.setattr(providers, "_LIVE_INDEX_POLICY_GENERATION", None)

    def add(name, age_seconds, **fields):
        path = tmp_path / f"{name}.jsonl"
        path.write_text(json.dumps({"type": "assistant", "message": {"content": []}}), encoding="utf-8")
        stamp = time.time() - age_seconds
        os.utime(path, (stamp, stamp))
        return {"id": name, "path": str(path), **fields}

    def index(chats):
        providers._index_live_paths([{"name": "Claude", "chats": chats}])

    return types.SimpleNamespace(add=add, index=index, tmp=tmp_path)


def record_transcript_reads(monkeypatch):
    real = providers._claude_transcript_state
    order = []

    def recording(path):
        order.append(path.stem)
        return real(path)

    monkeypatch.setattr(providers, "_claude_transcript_state", recording)
    return order


def test_spent_budget_keeps_recent_chats_and_defers_all_but_one_old_chat(claude_live, monkeypatch):
    order = record_transcript_reads(monkeypatch)
    claude_live.index([claude_live.add("recent", 5 * 60), claude_live.add("old-newer", 2 * 3600),
                       claude_live.add("old-older", 3 * 3600)])
    live = providers.live_state(budget_seconds=-1)
    rows = {chat["id"]: chat for chat in live["chats"]}
    assert set(rows) == {"recent", "old-newer", "old-older"}  # the roster is complete
    assert sorted(order) == ["old-newer", "recent"]  # the first awaiting chat is always read
    assert rows["old-older"]["detailPending"] is True and rows["old-older"]["status"] == "idle"
    assert rows["old-older"]["statusSource"] == "transcript" and rows["old-older"]["metricsUnavailable"] is True
    assert "detailPending" not in rows["old-newer"]
    assert live["complete"] is False and live["pendingChats"] == 1


def test_budget_is_checked_newest_first_and_counts_deferred_chats(claude_live, monkeypatch):
    clock = [1000.0]
    shim = types.SimpleNamespace(**{name: getattr(time, name) for name in dir(time) if not name.startswith("__")})
    shim.monotonic = lambda: clock[0]
    monkeypatch.setattr(providers, "time", shim)
    real = providers._claude_transcript_state
    order = []

    def costly(path):
        order.append(path.stem)
        clock[0] += 10.0  # each full read spends ten of the fifteen budgeted seconds
        return real(path)

    monkeypatch.setattr(providers, "_claude_transcript_state", costly)
    claude_live.index([claude_live.add("two-hours", 2 * 3600), claude_live.add("one-minute", 60),
                       claude_live.add("half-hour", 30 * 60), claude_live.add("three-hours", 3 * 3600)])
    live = providers.live_state(budget_seconds=15)
    assert order == ["one-minute", "half-hour"]  # newest first, then the budget runs out
    rows = {chat["id"]: chat for chat in live["chats"]}
    assert set(rows) == {"two-hours", "one-minute", "half-hour", "three-hours"}
    assert {name for name, chat in rows.items() if chat.get("detailPending")} == {"two-hours", "three-hours"}
    assert live["complete"] is False and live["pendingChats"] == 2


def test_complete_budgeted_call_reports_no_pending_chats(claude_live):
    claude_live.index([claude_live.add("a", 60), claude_live.add("b", 3 * 3600)])
    live = providers.live_state(budget_seconds=60)
    assert live["complete"] is True and live["pendingChats"] == 0
    assert {chat["id"] for chat in live["chats"]} == {"a", "b"}
    assert not any(chat.get("detailPending") for chat in live["chats"])


def test_deferred_chats_leave_the_index_and_detail_memo_untouched(claude_live, monkeypatch):
    order = record_transcript_reads(monkeypatch)
    claude_live.index([claude_live.add("recent", 60), claude_live.add("old-newer", 2 * 3600),
                       claude_live.add("old-older", 3 * 3600)])
    index_before = copy.deepcopy(providers._LIVE_INDEX)
    providers.live_state(budget_seconds=-1)
    assert "old-older" not in order
    assert providers._LIVE_INDEX == index_before
    assert all("old-older" not in key for key in providers._LIVE_DETAILED)


def test_every_budgeted_round_completes_one_more_chat(claude_live):
    claude_live.index([claude_live.add("one", 2 * 3600), claude_live.add("two", 3 * 3600),
                       claude_live.add("three", 4 * 3600)])
    pending = []
    for _ in range(5):
        live = providers.live_state(budget_seconds=-1)  # the budget is spent before any chat is reached
        pending.append(live["pendingChats"])
        if live["complete"]:
            break
    assert pending == [2, 1, 0]  # strictly fewer awaiting detail each round


def test_skipped_parent_stays_in_the_roster_so_its_child_lineage_resolves(claude_live):
    claude_live.index([
        claude_live.add("child", 60),
        claude_live.add("filler", 20 * 60),
        claude_live.add("parent", 2 * 3600),
    ])
    live = providers.live_state(budget_seconds=-1)
    rows = {chat["id"]: chat for chat in live["chats"]}
    assert rows["parent"]["detailPending"] is True  # the parent is deferred, not omitted
    assert rows["parent"]["provider"] == "Claude" and rows["parent"]["id"] == "parent"
    # Lineage is validated against the roster: a child naming the deferred parent still links to it.
    from token_tracker.agent_links import snapshot_graph
    roster = [{"provider": "Codex", "id": "child", "parentId": "parent", "parentProvider": "Claude"}]
    roster += [{"provider": chat["provider"], "id": chat["id"]} for chat in live["chats"]]
    assert snapshot_graph(roster)["edges"]["Codex:child"] == "Claude:parent"


def test_unbudgeted_payload_matches_today_apart_from_the_two_new_keys(claude_live):
    claude_live.index([claude_live.add("a", 60), claude_live.add("b", 3 * 3600), claude_live.add("c", 600)])
    plain = providers.live_state()
    budgeted = providers.live_state(budget_seconds=3600)
    assert plain["complete"] is True and plain["pendingChats"] == 0
    assert [chat["key"] for chat in plain["chats"]] == list(providers._LIVE_INDEX)  # payload keeps index order
    for payload in (plain, budgeted):
        for field in ("complete", "pendingChats", "generatedAt"):
            payload.pop(field)
    assert plain == budgeted


class PartialLive:
    """Fake live_state: `pending` counts down by one per call until complete."""

    def __init__(self, partial_rounds):
        self.partial_rounds = partial_rounds
        self.budgets = []

    def __call__(self, budget_seconds=None):
        self.budgets.append(budget_seconds)
        pending = max(0, self.partial_rounds - len(self.budgets) + 1)
        return {"chats": [{"key": f"Codex:{len(self.budgets)}"}], "graph": {"edges": {}},
                "complete": pending == 0, "pendingChats": pending}


def test_live_cache_publishes_partial_then_completes_without_a_caller(monkeypatch):
    fake = PartialLive(partial_rounds=2)
    monkeypatch.setattr(server, "live_state", fake)
    cache = server.LiveCache()
    cache._refresh()  # the one caller; every later round is started by the cache itself
    with cache.condition:
        assert cache.condition.wait_for(lambda: cache.value is not None and cache.value["complete"] is True
                                        and not cache.building, timeout=5)
    assert fake.budgets == [server.LIVE_BUILD_BUDGET_SECONDS] * 3
    cache._refresh()  # a complete value switches later rounds to the unbudgeted call
    assert fake.budgets[-1] is None
    assert cache.get() == cache.value


class StalledLive:
    """Fake live_state whose partial result never gets any closer to complete."""

    def __init__(self):
        self.budgets = []

    def __call__(self, budget_seconds=None):
        self.budgets.append(budget_seconds)
        return {"chats": [], "graph": {"edges": {}}, "complete": False, "pendingChats": 1}


def test_live_cache_backs_off_when_a_round_makes_no_progress(monkeypatch):
    fake = StalledLive()
    monkeypatch.setattr(server, "live_state", fake)
    cache = server.LiveCache()
    cache._refresh()
    with cache.condition:
        assert cache.condition.wait_for(lambda: not cache.building, timeout=5)
    time.sleep(0.2)
    assert len(fake.budgets) == 2  # the first round, then one round that showed no progress
    assert cache.retry_after > time.monotonic()  # backed off with the existing cooldown
    cache.completed_at = time.monotonic() - server.LIVE_REFRESH_TTL_SECONDS - 0.1
    cache.retry_after = 0.0
    cache.get()  # a later poll resumes the rounds, and that round stalls again
    with cache.condition:
        assert cache.condition.wait_for(lambda: not cache.building and len(fake.budgets) == 3, timeout=5)


# ------------------------------------------------------------------ lineage parity (deferred vs detailed)

LINEAGE_POLICIES = {
    "TOKEN_TRACKER_CODEX_COLLECT": "true", "TOKEN_TRACKER_CODEX_SHOW": "true",
    "TOKEN_TRACKER_CLAUDE_COLLECT": "true", "TOKEN_TRACKER_CLAUDE_SHOW": "true",
}


@pytest.fixture
def lineage_roster(tmp_path, monkeypatch):
    """Codex and Claude chats, all old enough to be deferred, plus the registry that proves an old link."""
    now = time.time()
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    monkeypatch.setattr(providers, "ENABLED_PROVIDERS", ("Codex", "Claude"))
    monkeypatch.setattr(providers, "LIMIT_ONLY_PROVIDERS", ())
    monkeypatch.setattr(providers, "POLICIES", load_policies(LINEAGE_POLICIES))
    monkeypatch.setattr(providers, "_LIVE_INDEX", {})
    monkeypatch.setattr(providers, "_LIVE_DETAILED", {})
    monkeypatch.setattr(providers, "_LIVE_INDEX_POLICY_GENERATION", None)
    sessions = tmp_path / "sessions" / "2026" / "11" / "02"
    sessions.mkdir(parents=True)

    def iso(seconds_ago):
        return datetime.fromtimestamp(now - seconds_ago, timezone.utc).isoformat().replace("+00:00", "Z")

    def write(path, records, age):
        path.write_text("".join(json.dumps(record) + "\n" for record in records), encoding="utf-8")
        os.utime(path, (now - age, now - age))
        return path

    def turn(identity, at_seconds_ago, parent=None):
        meta = {"id": identity, **({"parent_thread_id": parent} if parent else {})}
        return [{"type": "session_meta", "payload": meta},
                {"type": "event_msg", "timestamp": iso(at_seconds_ago), "payload": {"type": "task_started"}},
                {"type": "event_msg", "timestamp": iso(at_seconds_ago - 5), "payload": {"type": "task_complete"}}]

    root = write(sessions / "native-root.jsonl", turn("native-root", 3 * 3600), age=3 * 3600)
    child = write(sessions / "native-child.jsonl", turn("native-child", 3 * 3600, parent="native-root"), age=3 * 3600)
    exec_old = write(sessions / "exec-old.jsonl", turn("exec-old", 33 * 60), age=50 * 60)
    claude_old = tmp_path / "claude-old.jsonl"
    write(claude_old, [{"type": "assistant", "message": {"content": []}}], age=3 * 3600)
    filler = tmp_path / "claude-filler.jsonl"
    write(filler, [{"type": "assistant", "message": {"content": []}}], age=20 * 60)
    # A hook-backed Claude subagent and its parent session: the hook rows, not the transcript, name the parent.
    session = tmp_path / "claude-session.jsonl"
    write(session, [{"type": "assistant", "message": {"content": []}}], age=3 * 3600)
    subagent = tmp_path / "claude-session-agent.jsonl"
    write(subagent, [{"type": "assistant", "message": {"content": []}}], age=2 * 3600)
    hook_rows = [{"id": "session:agent", "parentId": "session", "title": "Explore the parser", "recordSource": "hook",
                  "status": "idle", "agentName": "explore"}]
    monkeypatch.setattr(claude_provider, "_cached_rows", lambda: [dict(row) for row in hook_rows])

    database = tmp_path / "state_5.sqlite"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE IF NOT EXISTS threads (id, rollout_path, source, cwd, name, title, model, agent_path, archived, updated_at)")
        connection.executemany("INSERT OR REPLACE INTO threads VALUES (?,?,?,?,?,?,?,?,?,?)", [
            ("native-root", str(root), json.dumps("vscode"), str(tmp_path), "native-root", "native-root", None, None, 0, 1),
            # Native parent plus an older cached Claude parent: the native pair must win.
            ("native-child", str(child), json.dumps({"subagent": {"thread_spawn": {"parent_thread_id": "native-root"}}}),
             str(tmp_path), "native-child", "native-child", None, None, 0, 1),
            # An unparented exec run with a cached Claude parent.
            ("exec-old", str(exec_old), json.dumps("exec"), str(tmp_path), "exec-old", "exec-old", None, None, 0, 1),
        ])

    providers._index_live_paths([
        {"name": "Claude", "chats": [{"id": "old", "path": str(claude_old)}, {"id": "filler", "path": str(filler)},
                                    {"id": "session", "path": str(session)},
                                    {"id": "session:agent", "path": str(subagent), "parentId": "session"}]},
        {"name": "Codex", "chats": [
            {"id": "native-child", "path": str(child), "parentId": "old", "parentProvider": "Claude",
             "runStartedAt": iso(3 * 3600)},
            {"id": "exec-old", "path": str(exec_old), "parentId": "old", "parentProvider": "Claude",
             "runStartedAt": iso(33 * 60)},
        ]},
    ])
    runs = [{"thread_id": "exec-old", "codex_pid": 1, "codex_created": 1, "claude_session_id": "old",
             "last_seen": now - 50 * 60, "verified_at": 0, "method": "receipt"}]
    real_apply = agent_links.apply_exact_links
    monkeypatch.setattr(agent_links, "apply_exact_links", lambda grouped: real_apply(
        grouped, registry_path=tmp_path / "registry.json", registry_reader=lambda path: [dict(run) for run in runs],
        permission_check=lambda policies: True))
    return types.SimpleNamespace(index=providers._LIVE_INDEX)


def lineage(live):
    edges = live["graph"]["edges"]
    return {chat["key"]: (chat.get("parentId"), chat.get("parentProvider"), edges.get(chat["key"]))
            for chat in live["chats"]}


def test_native_parent_wins_over_older_cached_claude_parent_when_deferred(lineage_roster):
    live = providers.live_state(budget_seconds=-1)
    rows = {chat["key"]: chat for chat in live["chats"]}
    assert rows["Codex:native-child"]["detailPending"] is True
    assert (rows["Codex:native-child"]["parentId"], rows["Codex:native-child"]["parentProvider"]) == ("native-root", "Codex")
    assert live["graph"]["edges"]["Codex:native-child"] == "Codex:native-root"  # the native edge survives validation


def test_deferred_exec_with_an_old_cached_parent_matches_the_detailed_row(lineage_roster):
    # The run started 33 minutes ago, after the registry's 50-minute-old association: no parent.
    detailed = providers.live_state()
    assert lineage(detailed)["Codex:exec-old"] == (None, None, None)
    providers._LIVE_DETAILED.clear()
    deferred = providers.live_state(budget_seconds=-1)
    row = next(chat for chat in deferred["chats"] if chat["key"] == "Codex:exec-old")
    assert row.get("detailPending") is True, "the exec row must be deferred for this check to mean anything"
    assert lineage(deferred)["Codex:exec-old"] == (None, None, None)


def test_deferred_row_without_a_run_boundary_never_takes_the_historical_parent(lineage_roster):
    lineage_roster.index[("Codex:exec-old")]["identityRunStartedAt"] = None
    live = providers.live_state(budget_seconds=-1)
    row = next(chat for chat in live["chats"] if chat["key"] == "Codex:exec-old")
    assert row.get("detailPending") is True
    assert (row.get("parentId"), row.get("parentProvider")) == (None, None)


def test_deferred_and_detailed_lineage_agree_for_every_chat(lineage_roster):
    detailed = lineage(providers.live_state())
    providers._LIVE_DETAILED.clear()
    deferred_live = providers.live_state(budget_seconds=-1)
    deferred_rows = [chat["key"] for chat in deferred_live["chats"] if chat.get("detailPending")]
    assert len(deferred_rows) >= 3
    deferred = lineage(deferred_live)
    for key in deferred_rows:
        assert deferred[key] == detailed[key], key


def test_hook_backed_claude_subagent_keeps_its_parent_when_deferred(lineage_roster):
    detailed_live = providers.live_state()
    detailed = next(chat for chat in detailed_live["chats"] if chat["key"] == "Claude:session:agent")
    assert detailed["parentId"] == "session"
    providers._LIVE_DETAILED.clear()
    deferred_live = providers.live_state(budget_seconds=-1)
    deferred = next(chat for chat in deferred_live["chats"] if chat["key"] == "Claude:session:agent")
    assert deferred.get("detailPending") is True, "the subagent must be deferred for this check to mean anything"
    assert (deferred.get("parentId"), deferred.get("parentProvider")) == (detailed.get("parentId"), detailed.get("parentProvider"))
    assert deferred_live["graph"]["edges"]["Claude:session:agent"] == detailed_live["graph"]["edges"]["Claude:session:agent"] == "Claude:session"
