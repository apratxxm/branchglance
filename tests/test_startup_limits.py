from datetime import datetime, timezone
import threading
import time

import pytest

from token_tracker import claude_identity, limit_reset, provider_policy, providers, server
from token_tracker.provider_policy import load_policies


@pytest.fixture
def verified_claude(monkeypatch):
    identity = claude_identity.Identity("test-account", "test-org", "test-epoch", "Test account")
    monkeypatch.setattr(claude_identity, "current_identity", lambda: identity)
    monkeypatch.setattr(claude_identity.AUTHORITY, "capture_nonblocking", lambda: (identity.generation, identity))
    return identity


def test_restored_limits_are_display_only_and_fresh_build_merges_per_window(tmp_path, monkeypatch):
    now = time.time()
    stamp = datetime.now(timezone.utc).isoformat()
    def snapshot(five, weekly):
        return {"generatedAt": stamp, "enabledProviders": ["Codex"], "limitProviders": [],
                "providers": [{"name": "Codex", "connected": True, "chats": [],
                               "fiveHour": five, "weekly": weekly, "limitsUpdatedAt": stamp}]}

    original = snapshot({"percent": 0, "resetAt": now + 3600}, {"percent": 46, "resetAt": now + 7200})
    path = tmp_path / "stats-snapshot.json"
    monkeypatch.setattr(server.SnapshotCache, "_shown_provider_names", staticmethod(lambda: {"Codex"}))
    monkeypatch.setattr(server, "_record_insights_peaks", lambda value: None)
    monkeypatch.setattr(server, "build_snapshot", lambda: original)
    server.SnapshotCache(persist_path=path, compatibility="test").get()
    started, release = threading.Event(), threading.Event()
    fresh = snapshot({"percent": 12, "resetAt": now + 3600}, {"percent": None})
    def build():
        started.set()
        assert release.wait(10)
        return fresh
    monkeypatch.setattr(server, "build_snapshot", build)
    cache = server.SnapshotCache(persist_path=path, compatibility="test")
    shown = cache.get_background()
    assert started.wait(2)
    assert cache.value is None
    provider = shown["providers"][0]
    assert provider["fiveHour"]["percent"] == 0 and provider["fiveHour"]["lastKnown"] is True
    assert provider["weekly"]["percent"] == 46 and provider["weekly"]["lastKnown"] is True
    assert provider["limitsUpdatedAt"] == stamp
    release.set()
    with cache.condition:
        assert cache.condition.wait_for(lambda: not cache.building, timeout=10)
    provider = cache.get_background()["providers"][0]
    assert provider["fiveHour"]["percent"] == 12 and "lastKnown" not in provider["fiveHour"]
    assert provider["weekly"]["percent"] == 46 and provider["weekly"]["lastKnown"] is True
    assert cache.value["providers"][0]["weekly"]["percent"] is None
    monkeypatch.setattr(server, "CACHE", cache)
    assert server._insights_snapshot()["providers"][0]["weekly"]["percent"] is None
    restarted = server.SnapshotCache(persist_path=path, compatibility="test")
    restarted._ensure_restored()
    again = restarted._egress(restarted._value, {}, restored=True)["providers"][0]
    assert again["weekly"]["percent"] == 46 and again["weekly"]["lastKnown"]


def test_expired_and_unverifiable_restored_windows_are_unknown():
    now = time.time()
    stamp = datetime.now(timezone.utc).isoformat()
    snapshot = {"providers": [{"name": "Claude", "chats": [], "limitsUpdatedAt": stamp,
                               "fiveHour": {"percent": 80, "resetAt": now - 1},
                               "weekly": {"percent": 55, "resetAt": now + 3600}}]}
    result = server.SnapshotCache._egress(snapshot, {}, restored=True)["providers"][0]
    assert result["fiveHour"]["percent"] is None
    assert result["weekly"]["percent"] == 55 and result["weekly"]["lastKnown"]
    result["weekly"]["resetAt"] = now - 1
    assert server.SnapshotCache._egress({"providers": [result]}, {})["providers"][0]["weekly"]["percent"] is None


def test_greeter_uses_its_source_not_the_restored_snapshot(tmp_path, monkeypatch):
    now = time.time()
    stamp = datetime.now(timezone.utc).isoformat()
    restored = {"providers": [{"name": "Codex", "chats": [], "limitsUpdatedAt": stamp,
                                "fiveHour": {"percent": 100, "resetAt": now + 1},
                                "weekly": {"percent": 0, "resetAt": now + 100}}]}
    cache = server.SnapshotCache()
    cache._value = restored
    cache._restored = True
    monkeypatch.setattr(server, "CACHE", cache)
    calls = []
    policies = load_policies({"TOKEN_TRACKER_CODEX_COLLECT": "true", "TOKEN_TRACKER_CODEX_SHOW": "true", "TOKEN_TRACKER_CODEX_SEND": "true"})
    service = limit_reset.LimitResetService(
        sources={"Codex": lambda: limit_reset.LimitSample(None, None, now)},
        senders={"Codex": lambda cwd: calls.append(cwd)}, state_path=tmp_path / "state.json",
        log_path=tmp_path / "events.jsonl", cwd=tmp_path, enabled=True, clock=lambda: now,
        policies=policies)
    service.check_once()
    state = service.status()["providers"]["Codex"]
    assert not state["armed"] and state["nextGreetingAt"] is None and not calls


def test_restored_claude_uses_fresh_memory_sample_per_window(monkeypatch, verified_claude):
    class Policy:
        def allows(self, action):
            return action == "show"
    monkeypatch.setattr(provider_policy, "policy_for", lambda *args: Policy())
    now = time.time()
    old = datetime.fromtimestamp(now - 60, timezone.utc).isoformat()
    current = datetime.fromtimestamp(now, timezone.utc).isoformat()
    cache = server.FastClaudeLimits()
    cache.identity_generation = verified_claude.generation
    cache.value = {"fiveHour": {"percent": 12, "resetAt": now + 3600},
                   "weekly": {"percent": None}, "limitsUpdatedAt": current}
    cache.at = time.monotonic()
    snapshot = {"providers": [{"name": "Claude", "stale": True, "limitsUpdatedAt": old,
                               "fiveHour": {"percent": 90, "resetAt": now + 3600, "lastKnown": True},
                               "weekly": {"percent": 55, "resetAt": now + 7200, "lastKnown": True}}]}
    provider = cache.overlay(snapshot)["providers"][0]
    assert provider["fiveHour"]["percent"] == 12 and "lastKnown" not in provider["fiveHour"]
    assert provider["weekly"]["percent"] == 55 and provider["weekly"]["lastKnown"]
    assert provider["_quotaWindowObservedAt"] == {"fiveHour": current, "weekly": old}
    assert snapshot["providers"][0]["fiveHour"]["percent"] == 90


def test_restored_claude_starts_cold_read_without_waiting(tmp_path, monkeypatch, verified_claude):
    class Policy:
        def allows(self, action):
            return action == "show"
    monkeypatch.setattr(provider_policy, "policy_for", lambda *args: Policy())
    started, release = threading.Event(), threading.Event()
    now = time.time()
    stamp = datetime.fromtimestamp(now, timezone.utc).isoformat()
    class Provider:
        root = tmp_path
        def snapshot(self):
            started.set()
            assert release.wait(10)
            return {"identityVerified": True, "identityGeneration": verified_claude.generation,
                    "fiveHour": {"percent": 12, "resetAt": now + 3600}, "limitsUpdatedAt": stamp}
    monkeypatch.setattr(providers, "ClaudeLimitsProvider", Provider)
    (tmp_path / "plan-usage-history.json").write_text("{}", encoding="utf-8")
    snapshot = {"providers": [{"name": "Claude", "stale": True, "limitsUpdatedAt": stamp,
                               "fiveHour": {"percent": 90, "resetAt": now + 3600, "lastKnown": True}}]}
    cache = server.FastClaudeLimits()
    try:
        assert cache.overlay(snapshot)["providers"][0]["fiveHour"]["percent"] == 90
        assert started.wait(2)
    finally:
        release.set()
    with cache.condition:
        assert cache.condition.wait_for(lambda: not cache.loading, timeout=10)
    assert cache.overlay(snapshot)["providers"][0]["fiveHour"]["percent"] == 12


def test_restored_claude_thread_start_failure_keeps_fallback(monkeypatch):
    class Policy:
        def allows(self, action):
            return action == "show"
    class Thread:
        def __init__(self, **kwargs):
            pass
        def start(self):
            raise OSError("thread unavailable")
    monkeypatch.setattr(provider_policy, "policy_for", lambda *args: Policy())
    monkeypatch.setattr(server.threading, "Thread", Thread)
    snapshot = {"providers": [{"name": "Claude", "stale": True, "fiveHour": {"percent": 90, "lastKnown": True}}]}
    cache = server.FastClaudeLimits()
    assert cache.overlay(snapshot) is snapshot
    assert not cache.loading


def test_restored_claude_retains_sample_past_memo_ttl_during_refresh(tmp_path, monkeypatch, verified_claude):
    class Policy:
        def allows(self, action):
            return action == "show"
    monkeypatch.setattr(provider_policy, "policy_for", lambda *args: Policy())
    started, release = threading.Event(), threading.Event()
    now = time.time()
    old = datetime.fromtimestamp(now - 60, timezone.utc).isoformat()
    current = datetime.fromtimestamp(now, timezone.utc).isoformat()
    class Provider:
        root = tmp_path
        def snapshot(self):
            started.set()
            assert release.wait(10)
            return {"identityVerified": True, "identityGeneration": verified_claude.generation,
                    "fiveHour": {"percent": None}, "weekly": {"percent": None}, "limitsUpdatedAt": current}
    monkeypatch.setattr(providers, "ClaudeLimitsProvider", Provider)
    (tmp_path / "plan-usage-history.json").write_text("{}", encoding="utf-8")
    snapshot = {"providers": [{"name": "Claude", "stale": True, "limitsUpdatedAt": old,
                               "fiveHour": {"percent": 90, "resetAt": now + 3600, "lastKnown": True}}]}
    cache = server.FastClaudeLimits()
    cache.identity_generation = verified_claude.generation
    cache.value = {"fiveHour": {"percent": 12, "resetAt": now + 3600}, "limitsUpdatedAt": current}
    cache.at = time.monotonic() - 3
    try:
        assert cache.overlay(snapshot)["providers"][0]["fiveHour"]["percent"] == 12
        assert started.wait(2)
        assert cache.overlay(snapshot)["providers"][0]["fiveHour"]["percent"] == 12
    finally:
        release.set()
    with cache.condition:
        assert cache.condition.wait_for(lambda: not cache.loading, timeout=10)
    assert cache.overlay(snapshot)["providers"][0]["fiveHour"]["percent"] == 12
    normal = {"providers": [{"name": "Claude", "stale": False, "limitsUpdatedAt": current,
                              "identityVerified": True, "identityGeneration": verified_claude.generation,
                              "fiveHour": {"percent": None}}]}
    assert cache.overlay(normal)["providers"][0]["fiveHour"]["percent"] == 12
    cache.retained["fiveHour"]["value"]["resetAt"] = now - 1
    assert cache.overlay(normal)["providers"][0]["fiveHour"]["percent"] is None
    cache.value = {"fiveHour": {"percent": 12, "resetAt": now - 1}, "limitsUpdatedAt": current}
    assert cache.overlay(normal)["providers"][0]["fiveHour"]["percent"] is None


def test_expired_retained_window_cannot_block_fresh_null(monkeypatch, verified_claude):
    class Policy:
        def allows(self, action):
            return action == "show"
    monkeypatch.setattr(provider_policy, "policy_for", lambda *args: Policy())
    now = time.time()
    old = datetime.fromtimestamp(now - 60, timezone.utc).isoformat()
    fresh = datetime.fromtimestamp(now, timezone.utc).isoformat()
    old_epoch = datetime.fromisoformat(old).timestamp()
    cache = server.FastClaudeLimits()
    cache.identity_generation = verified_claude.generation
    cache.retained = {
        "fiveHour": {"value": {"percent": 90, "resetAt": now - 1}, "observed": old, "at": old_epoch},
        "weekly": {"value": {"percent": 55, "resetAt": now + 3600}, "observed": old, "at": old_epoch},
    }
    cache.value = {"fiveHour": {"percent": None}, "weekly": {"percent": None}, "limitsUpdatedAt": fresh}
    cache.at = time.monotonic()
    for marked in (False, True):
        five = {"percent": 90, "resetAt": now - 1}
        if marked:
            five["lastKnown"] = True
        snapshot = {"providers": [{"name": "Claude", "identityVerified": True,
                                   "identityGeneration": verified_claude.generation, "limitsUpdatedAt": old, "fiveHour": five,
                                   "weekly": {"percent": 55, "resetAt": now + 3600, "lastKnown": True}}]}
        result = cache.overlay(snapshot)["providers"][0]
        assert result["fiveHour"]["percent"] is None
        assert "lastKnown" not in result["fiveHour"]
        assert result["weekly"]["percent"] == 55 and "lastKnown" not in result["weekly"]
        assert "fiveHour" not in cache.retained and "weekly" in cache.retained


def test_fast_claude_load_without_history_calls_snapshot(tmp_path, monkeypatch, verified_claude):
    class Provider:
        root = tmp_path
        def snapshot(self):
            return {"name": "Claude", "identityVerified": True, "identityGeneration": verified_claude.generation,
                    "fiveHour": {"percent": 12}}
    monkeypatch.setattr(providers, "ClaudeLimitsProvider", Provider)
    cache = server.FastClaudeLimits()
    cache.identity_generation = verified_claude.generation
    assert cache._load()["fiveHour"]["percent"] == 12
