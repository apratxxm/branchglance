from __future__ import annotations

import threading
import time
import json
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import pytest

from token_tracker import server


def test_snapshot_cache_tests_import_this_checkout():
    expected = Path(__file__).resolve().parents[1] / "token_tracker" / "server.py"
    assert Path(server.__file__).resolve() == expected


def _wait_idle(cache: server.SnapshotCache) -> None:
    with cache.condition:
        assert cache.condition.wait_for(lambda: not cache.building, timeout=2)


def test_concurrent_forced_builds_publish_one_generation(monkeypatch):
    cache = server.SnapshotCache()
    started, release = threading.Event(), threading.Event()
    calls = []

    def build():
        calls.append(1)
        started.set()
        assert release.wait(2)
        return {"generatedAt": "provider-time", "nested": {"items": [1]}}

    monkeypatch.setattr(server, "build_snapshot", build)
    results, errors = [], []
    waiter_count = 4
    waiting = threading.Event()
    waiting_count = 0
    waiting_lock = threading.Lock()
    original_wait = cache.condition.wait

    def observed_wait(timeout=None):
        nonlocal waiting_count
        with waiting_lock:
            waiting_count += 1
            if waiting_count == waiter_count:
                waiting.set()
        return original_wait(timeout)

    cache.condition.wait = observed_wait

    def get_forced():
        try:
            results.append(cache.get(force=True))
        except Exception as exc:  # make worker-thread failures visible
            errors.append(exc)

    first = threading.Thread(target=get_forced)
    waiters = [threading.Thread(target=get_forced) for _ in range(waiter_count)]
    first.start()
    assert started.wait(1)
    for waiter in waiters:
        waiter.start()
    assert waiting.wait(1)
    release.set()
    first.join(2)
    for waiter in waiters:
        waiter.join(2)

    assert not first.is_alive() and all(not waiter.is_alive() for waiter in waiters)
    assert errors == []
    assert calls == [1]
    assert len(results) == 5
    assert {item["snapshotCache"]["generation"] for item in results} == {1}
    assert {item["generatedAt"] for item in results} == {"provider-time"}


def test_background_failure_keeps_last_good_generation_and_marks_stale(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(server.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(server, "build_snapshot", lambda: {"nested": {"value": 1}})
    cache = server.SnapshotCache(ttl=10)
    original = cache.get()
    original_built_at = original["snapshotCache"]["builtAt"]
    clock[0] = 111.0
    failure_calls = []

    def fail():
        failure_calls.append(1)
        raise OSError("private path and provider details")

    monkeypatch.setattr(server, "build_snapshot", fail)
    cache.get(force=True, background=True)
    _wait_idle(cache)
    failed = cache.get(background=True)

    assert failed["nested"] == {"value": 1}
    assert datetime.fromisoformat(failed["snapshotCache"]["lastFailureAt"]).tzinfo is not None
    assert failed["snapshotCache"] == {
        "generation": 1,
        "builtAt": original_built_at,
        "ageSeconds": 11.0,
        "building": False,
        "stale": True,
        "lastFailureAt": failed["snapshotCache"]["lastFailureAt"],
        "lastFailureCategory": "io_error",
    }
    assert "private path" not in repr(failed)
    assert cache.generation == 1 and cache.at == 100.0

    # Automatic stale reads during the retry window do not start another build.
    cache.get(background=True)
    assert failure_calls == [1]
    assert cache.generation == 1


def test_repeated_forced_background_refreshes_respect_retry_cooldown(monkeypatch):
    clock = [200.0]
    monkeypatch.setattr(server.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(server, "build_snapshot", lambda: {"generationData": "first"})
    cache = server.SnapshotCache(ttl=5)
    initial = cache.get()
    clock[0] = 206.0
    failure_calls = []

    def fail_once():
        failure_calls.append(1)
        raise RuntimeError("private provider detail")

    monkeypatch.setattr(server, "build_snapshot", fail_once)
    first_stale = cache.get(force=True, background=True)
    _wait_idle(cache)
    second_stale = cache.get(force=True, background=True)
    clock[0] = 208.0
    third_stale = cache.get(force=True, background=True)

    assert failure_calls == [1]
    for result in (first_stale, second_stale, third_stale):
        assert result["generationData"] == "first"
        assert result["snapshotCache"]["generation"] == initial["snapshotCache"]["generation"]
        assert result["snapshotCache"]["builtAt"] == initial["snapshotCache"]["builtAt"]
        assert result["snapshotCache"]["stale"] is True
    assert third_stale["snapshotCache"]["ageSeconds"] == 8.0

    clock[0] = 211.0  # five seconds after the failed attempt
    monkeypatch.setattr(server, "build_snapshot", lambda: {"generationData": "recovered"})
    cache.get(force=True, background=True)
    _wait_idle(cache)
    recovered = cache.get(background=True)
    assert recovered["generationData"] == "recovered"
    assert recovered["snapshotCache"]["generation"] == 2
    assert recovered["snapshotCache"]["stale"] is False
    assert recovered["snapshotCache"]["lastFailureCategory"] is None


def test_cold_failure_raises_and_recovery_clears_failure(monkeypatch):
    cache = server.SnapshotCache()
    failure_calls = []

    def fail():
        failure_calls.append(1)
        raise RuntimeError("private provider detail")

    monkeypatch.setattr(server, "build_snapshot", fail)
    with pytest.raises(RuntimeError, match="private provider detail"):
        cache.get()
    assert cache.value is None
    assert cache.building is False
    assert cache.last_failure_category == "build_failed"
    with pytest.raises(RuntimeError, match="private provider detail"):
        cache.get(force=True, background=True)
    assert failure_calls == [1]
    with pytest.raises(RuntimeError, match="private provider detail"):
        cache.get()

    monkeypatch.setattr(server, "build_snapshot", lambda: {"generatedAt": "provider-time"})
    recovered = cache.get(force=True)
    assert recovered["snapshotCache"]["generation"] == 1
    assert recovered["snapshotCache"]["stale"] is False
    assert recovered["snapshotCache"]["lastFailureAt"] is None
    assert recovered["snapshotCache"]["lastFailureCategory"] is None
    assert recovered["generatedAt"] == "provider-time"


def test_background_thread_start_failure_is_stale_and_cooldown_limited(monkeypatch):
    monkeypatch.setattr(server, "build_snapshot", lambda: {"nested": {"value": 1}})
    cache = server.SnapshotCache()
    good = cache.get()
    clock = [cache.at + cache.ttl + 1]
    monkeypatch.setattr(server.time, "monotonic", lambda: clock[0])
    thread_class = threading.Thread

    class FailedThread:
        def __init__(self, **_kwargs):
            pass

        def start(self):
            raise RuntimeError("private thread start detail")

    monkeypatch.setattr(server.threading, "Thread", FailedThread)
    stale = cache.get(background=True, force=True)
    assert stale["nested"] == good["nested"]
    assert stale["snapshotCache"]["generation"] == good["snapshotCache"]["generation"]
    assert stale["snapshotCache"]["building"] is False
    assert stale["snapshotCache"]["stale"] is True
    assert stale["snapshotCache"]["lastFailureCategory"] == "worker_start_failed"
    assert "private thread start detail" not in repr(stale)

    cache.get(background=True)
    assert cache.generation == 1
    clock[0] += cache.RETRY_COOLDOWN_SECONDS + 0.01
    monkeypatch.setattr(server.threading, "Thread", thread_class)
    recovered = cache.get(background=True)
    _wait_idle(cache)
    assert recovered["snapshotCache"]["generation"] == 1
    assert cache.generation == 2
    assert cache.last_failure_category is None


def test_caller_and_builder_mutations_cannot_change_published_cache(monkeypatch):
    built = {"nested": {"items": ["kept"]}}
    monkeypatch.setattr(server, "build_snapshot", lambda: built)
    cache = server.SnapshotCache()
    first = cache.get()
    built["nested"]["items"].append("builder mutation")
    first["nested"]["items"].append("caller mutation")

    second = cache.get()
    assert second["nested"]["items"] == ["kept"]
    second["snapshotCache"]["generation"] = 999
    assert cache.get()["snapshotCache"]["generation"] == 1


def test_age_and_generation_do_not_advance_on_repeated_cached_reads(monkeypatch):
    clock = [20.0]
    monkeypatch.setattr(server.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(server, "build_snapshot", lambda: {"generatedAt": "provider-time"})
    cache = server.SnapshotCache(ttl=100)
    first = cache.get()
    clock[0] += 7.25
    second = cache.get()

    assert first["generatedAt"] == second["generatedAt"] == "provider-time"
    assert first["snapshotCache"]["generation"] == second["snapshotCache"]["generation"] == 1
    assert first["snapshotCache"]["builtAt"] == second["snapshotCache"]["builtAt"]
    assert second["snapshotCache"]["ageSeconds"] == 7.25
    assert second["snapshotCache"]["stale"] is False


def _persistable_snapshot(*, status="running", pending="Choose?"):
    return {
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "enabledProviders": ["Codex"],
        "limitProviders": [],
        "providers": [{
            "name": "Codex", "connected": True,
            "chats": [{
                "id": "persisted-only", "status": status, "statusSource": "app",
                "executionStatus": status, "executionSource": "app",
                "executionObservedAt": datetime.now(timezone.utc).isoformat(),
                "pendingQuestion": pending, "pendingOptions": ["yes", "no"],
                "latestRequest": "private old request", "tokens": 42,
            }],
        }],
    }


def test_persisted_snapshot_is_display_only_stale_and_refreshes(monkeypatch, tmp_path):
    path = tmp_path / "stats-snapshot.json"
    compatibility = "policy-and-source-a"
    original = _persistable_snapshot()
    monkeypatch.setattr(server, "build_snapshot", lambda: original)
    writer = server.SnapshotCache(persist_path=path, compatibility=compatibility)
    built = writer.get()
    assert built["snapshotCache"]["stale"] is False
    assert path.is_file()

    started, release = threading.Event(), threading.Event()
    refreshed = _persistable_snapshot(status="idle", pending=None)

    def slow_build():
        started.set()
        assert release.wait(2)
        return refreshed

    monkeypatch.setattr(server, "build_snapshot", slow_build)
    restored = server.SnapshotCache(persist_path=path, compatibility=compatibility)
    assert restored.value is None  # Cached chat IDs cannot authorize actions.
    shown = restored.get_background()
    assert started.wait(1)
    assert shown["snapshotCache"]["stale"] is True
    assert shown["snapshotCache"]["building"] is True
    assert shown["providers"][0]["stale"] is True
    chat = shown["providers"][0]["chats"][0]
    assert chat["status"] == "unknown" and chat["telemetryUnavailable"] is True
    assert chat["executionStatus"] == "unavailable"
    assert chat["pendingQuestion"] is None and chat["pendingOptions"] == []
    assert "latestRequest" not in chat

    release.set()
    _wait_idle(restored)
    assert restored.value["providers"][0]["chats"][0]["status"] == "idle"
    assert restored.get_background()["snapshotCache"]["stale"] is False


def test_disk_snapshot_rejects_mismatched_compatibility_and_malformed_data(tmp_path):
    path = tmp_path / "stats-snapshot.json"
    snapshot = _persistable_snapshot()
    envelope = {
        "schemaVersion": server.SnapshotCache.PERSISTED_SCHEMA_VERSION,
        "compatibility": "source-a", "generation": 4,
        "builtAt": datetime.now(timezone.utc).isoformat(), "snapshot": snapshot,
    }
    path.write_text(json.dumps(envelope), encoding="utf-8")
    assert server.SnapshotCache(persist_path=path, compatibility="source-b").value is None

    envelope["compatibility"] = "source-a"
    envelope["snapshot"] = {"providers": "not-a-list"}
    path.write_text(json.dumps(envelope), encoding="utf-8")
    assert server.SnapshotCache(persist_path=path, compatibility="source-a").value is None


def test_restored_codex_id_does_not_authorize_open(monkeypatch, tmp_path):
    snapshot = _persistable_snapshot(status="idle", pending=None)
    snapshot["providers"][0]["chats"][0]["id"] = "12345678-1234-1234-1234-123456789abc"
    path = tmp_path / "stats-snapshot.json"
    monkeypatch.setattr(server, "build_snapshot", lambda: snapshot)
    server.SnapshotCache(persist_path=path, compatibility="source-a").get()
    restored = server.SnapshotCache(persist_path=path, compatibility="source-a")
    monkeypatch.setattr(server, "CACHE", restored)
    monkeypatch.setattr(server, "_codex_allows", lambda _action: True)
    monkeypatch.setattr(server.LIVE_CACHE, "value", None)
    httpd = server.ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        request = urllib.request.Request(
            f"http://127.0.0.1:{httpd.server_port}/api/open?thread=12345678-1234-1234-1234-123456789abc"
        )
        with pytest.raises(urllib.error.HTTPError) as response:
            urllib.request.urlopen(request, timeout=2)
        assert response.value.code == 404
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_failed_refresh_keeps_prior_persisted_snapshot(monkeypatch, tmp_path):
    path = tmp_path / "stats-snapshot.json"
    compatibility = "source-a"
    original = _persistable_snapshot(status="idle", pending=None)
    monkeypatch.setattr(server, "build_snapshot", lambda: original)
    server.SnapshotCache(persist_path=path, compatibility=compatibility).get()
    before = path.read_bytes()

    def fail():
        raise OSError("temporary source failure")

    monkeypatch.setattr(server, "build_snapshot", fail)
    restored = server.SnapshotCache(persist_path=path, compatibility=compatibility)
    assert restored.get_background()["snapshotCache"]["stale"] is True
    _wait_idle(restored)
    assert path.read_bytes() == before
    assert server.SnapshotCache(persist_path=path, compatibility=compatibility).value is None


def test_unavailable_provider_does_not_replace_cached_snapshot(monkeypatch, tmp_path):
    path = tmp_path / "stats-snapshot.json"
    compatibility = "source-a"
    original = _persistable_snapshot(status="idle", pending=None)
    monkeypatch.setattr(server, "build_snapshot", lambda: original)
    server.SnapshotCache(persist_path=path, compatibility=compatibility).get()
    before = path.read_bytes()

    degraded = _persistable_snapshot(status="unknown", pending=None)
    degraded["providers"][0]["connected"] = False
    monkeypatch.setattr(server, "build_snapshot", lambda: degraded)
    restored = server.SnapshotCache(persist_path=path, compatibility=compatibility)
    result = restored.get(force=True)
    assert result["providers"][0]["connected"] is False
    assert path.read_bytes() == before


def test_fast_claude_limits_overlay_only_quota_fields(monkeypatch, tmp_path):
    from token_tracker import provider_policy, providers

    class EnabledClaude:
        def allows(self, action):
            return action in {"collect", "show"}

    monkeypatch.setattr(provider_policy, "policy_for", lambda name, policies=None: EnabledClaude())
    calls = []

    def limits_snapshot():
        calls.append(1)
        return {"fiveHour": {"percent": 17}, "weekly": {"percent": 8},
                "limitsUpdatedAt": "observed", "chats": []}

    class FakeLimitsProvider:
        def __init__(self):
            self.root = tmp_path

        def snapshot(self):
            return limits_snapshot()

    history = tmp_path / "plan-usage-history.json"
    # The bounded reader checks this metadata before entering the provider.
    history.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(providers, "ClaudeLimitsProvider", FakeLimitsProvider)
    cache = server.FastClaudeLimits()
    snapshot = {"providers": [{"name": "Claude", "fiveHour": None, "weekly": None,
                               "limitsUpdatedAt": None, "chats": [{"id": "chat"}]}]}
    first = cache.overlay(snapshot)
    second = cache.overlay(snapshot)
    provider = first["providers"][0]
    assert provider["fiveHour"] == {"percent": 17}
    assert provider["weekly"] == {"percent": 8}
    assert provider["limitsUpdatedAt"] == "observed"
    assert provider["chats"] == [{"id": "chat"}]
    assert second["providers"][0]["fiveHour"] == {"percent": 17}
    assert calls == [1]


def test_cold_background_stats_and_quota_requests_do_not_wait_for_full_build(monkeypatch, tmp_path):
    from token_tracker import limit_reset

    cache = server.SnapshotCache(persist_path=tmp_path / "stats-snapshot.json", compatibility="test")
    monkeypatch.setattr(server, "CACHE", cache)
    started, release = threading.Event(), threading.Event()

    def slow_build():
        started.set()
        assert release.wait(3)
        return _persistable_snapshot(status="idle", pending=None)

    monkeypatch.setattr(server, "build_snapshot", slow_build)
    monkeypatch.setattr(limit_reset, "codex_quota_http_view", lambda: {"providers": {"Codex": {}}, "quotaCache": {"ready": False}})
    httpd = server.ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{httpd.server_port}"
    try:
        began = time.perf_counter()
        with pytest.raises(urllib.error.HTTPError) as pending:
            urllib.request.urlopen(base + "/api/stats?background=1", timeout=2)
        assert pending.value.code == 503
        stats_elapsed = time.perf_counter() - began
        assert started.wait(1)

        began = time.perf_counter()
        with urllib.request.urlopen(base + "/api/quota", timeout=2) as response:
            quota = json.loads(response.read())
        quota_elapsed = time.perf_counter() - began
        assert response.status == 200 and quota["quotaCache"]["ready"] is False
        assert stats_elapsed < 1 and quota_elapsed < 1
        assert cache.building is True
    finally:
        release.set()
        httpd.shutdown()
        httpd.server_close()
