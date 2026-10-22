from __future__ import annotations

import threading
import time
from datetime import datetime
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
