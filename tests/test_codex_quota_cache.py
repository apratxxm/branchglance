from __future__ import annotations

import json
import threading
import time
from http.client import HTTPConnection
from pathlib import Path

import pytest

from token_tracker import limit_reset, server
from token_tracker.limit_reset import CodexQuotaCache, LimitResetService, LimitSample


def _rates(five=12, weekly=None):
    primary = {} if five is None else {"usedPercent": five}
    secondary = {} if weekly is None else {"usedPercent": weekly}
    return {"rateLimits": {"primary": primary, "secondary": secondary}}


class FakeClient:
    def __init__(self, *results, block=False):
        self.results, self.calls = list(results), 0
        self.lock = threading.Lock()
        self.entered, self.release = threading.Event(), threading.Event()
        self.block = block

    def rate_limits(self):
        with self.lock:
            index = self.calls
            self.calls += 1
        self.entered.set()
        if self.block:
            assert self.release.wait(3)
        result = self.results[min(index, len(self.results) - 1)]
        if isinstance(result, Exception):
            raise result
        return result


def _wait_idle(cache):
    with cache.condition:
        assert cache.condition.wait_for(lambda: not cache.refreshing, timeout=2)


def test_quota_cache_tests_import_this_checkout():
    assert Path(limit_reset.__file__).resolve() == Path(__file__).resolve().parents[1] / "token_tracker" / "limit_reset.py"
    assert limit_reset._CODEX_QUOTA_CACHE.client is limit_reset._CODEX_LIMIT_CLIENT


def test_cold_http_get_is_nonblocking_and_action_joins_one_inflight_read():
    client = FakeClient(_rates(five=12), block=True)
    cache = CodexQuotaCache(client=client)
    waiting = threading.Event()
    original_wait = cache.condition.wait
    cache.condition.wait = lambda timeout=None: (waiting.set(), original_wait(timeout))[1]
    action_result = []
    try:
        first, second = cache.http_view(), None
        assert client.entered.wait(1)
        second = cache.http_view()
        assert first["quotaCache"]["ready"] is False
        assert first["quotaCache"]["refreshing"] is True
        assert "lastStatus" not in first["providers"]["Codex"]
        assert second["quotaCache"]["ready"] is False
        assert client.calls == 1
        action = threading.Thread(target=lambda: action_result.append(cache.read_for_action()))
        action.start()
        assert waiting.wait(1)
    finally:
        client.release.set()
    action.join(2)
    assert not action.is_alive() and client.calls == 1
    sample = action_result[0]
    view = cache.http_view()
    status = view["providers"]["Codex"]["lastStatus"]
    assert view["quotaCache"]["ready"] and view["quotaCache"]["fresh"]
    assert status["five_hour_percent"] == 12
    assert "weekly_percent" not in status
    assert status["observed_at"] == sample.observed_at
    assert status["checkedAt"] == limit_reset._iso_from_epoch(sample.observed_at)


def test_action_requires_new_generation_even_when_http_sample_is_fresh():
    client = FakeClient(_rates(five=10), _rates(five=20))
    cache = CodexQuotaCache(client=client)
    first = cache.read_for_action()
    assert cache.http_view()["quotaCache"]["fresh"]
    second = cache.read_for_action()
    assert (first.five_hour_percent, second.five_hour_percent, cache.generation, client.calls) == (10, 20, 2, 2)


def test_cached_failure_retains_original_timestamp_and_retries_after_cooldown():
    clock = [100.0]
    client = FakeClient(_rates(five=11), RuntimeError("private provider path"), _rates(five=22))
    cache = CodexQuotaCache(client=client, clock=lambda: clock[0])
    first = cache.read_for_action()
    checked = limit_reset._iso_from_epoch(first.observed_at)
    clock[0] += 3
    cache.http_view()
    _wait_idle(cache)
    failed = cache.http_view()
    assert failed["quotaCache"]["ageSeconds"] == 3.0
    assert failed["quotaCache"]["errorCategory"] == "read_failed"
    assert failed["providers"]["Codex"]["lastStatus"]["checkedAt"] == checked
    assert "private provider path" not in json.dumps(failed)
    cache.http_view()
    assert client.calls == 2
    clock[0] += limit_reset.CODEX_QUOTA_RETRY_SECONDS + 0.01
    cache.http_view()
    _wait_idle(cache)
    assert cache.http_view()["providers"]["Codex"]["lastStatus"]["five_hour_percent"] == 22
    assert cache.http_view()["quotaCache"]["errorCategory"] is None


def test_worker_start_failure_clears_refresh_and_cools_down():
    clock = [100.0]
    client = FakeClient(_rates(five=5))

    class FailedThread:
        def __init__(self, **_kwargs): pass
        def start(self): raise RuntimeError("private thread detail")

    cache = CodexQuotaCache(client=client, clock=lambda: clock[0], thread_factory=FailedThread)
    failed = cache.http_view()
    assert failed["quotaCache"]["errorCategory"] == "worker_start_failed"
    assert failed["quotaCache"]["refreshing"] is False
    cache.http_view()
    assert client.calls == 0
    with pytest.raises(RuntimeError):
        cache.read_for_action()
    clock[0] += limit_reset.CODEX_QUOTA_RETRY_SECONDS + .01
    cache.thread_factory = threading.Thread
    cache.http_view()
    _wait_idle(cache)
    assert cache.http_view()["quotaCache"]["ready"] is True


def test_watcher_preserves_codex_sample_checked_at(tmp_path):
    observed = time.time() - 60
    service = LimitResetService(sources={"Codex": lambda: LimitSample(50, 20, observed)}, senders={},
                               state_path=tmp_path / "state.json", log_path=tmp_path / "events.jsonl",
                               enabled=True, clock=lambda: observed + 120)
    service.check_provider("Codex", LimitSample(50, 20, observed))
    assert service._provider_state("Codex")["lastStatus"]["checkedAt"] == limit_reset._iso_from_epoch(observed)


def test_action_does_not_replay_stale_cache_to_verification(tmp_path, monkeypatch):
    now = time.time()
    cache = CodexQuotaCache(client=FakeClient(_rates(five=80)), clock=lambda: 50.0)
    cache.sample = LimitSample(80, 20, now - 30, now + 300, now + 1000)
    cache.sampled_at, cache.generation, cache.retry_after = 20.0, 4, 55.0
    cache.error_category, cache._last_error = "read_failed", RuntimeError("stale")
    monkeypatch.setattr(limit_reset, "_CODEX_QUOTA_CACHE", cache)
    service = LimitResetService(sources={"Codex": limit_reset.codex_limit_sample},
                                senders={"Codex": lambda _cwd: pytest.fail("must not send")},
                                state_path=tmp_path / "state.json", log_path=tmp_path / "events.jsonl",
                                enabled=True, clock=lambda: now)
    state = service._provider_state("Codex")
    state.update(awaitingVerification=True, verificationDeadline=now + 60,
                 preSendPercent=10, preSendResetAt=now + 100)
    original_status = state.get("lastStatus")
    monkeypatch.setattr(service, "_save_state", lambda: True)
    service.check_once()
    assert state["awaitingVerification"] is True
    assert state.get("lastVerifiedAt") is None and state.get("lastStatus") == original_status
    assert cache.client.calls == 0


@pytest.mark.parametrize("enabled", [False, True])
def test_quota_endpoint_is_origin_guarded_and_never_runs_watcher_actions(tmp_path, monkeypatch, enabled):
    client = FakeClient(_rates(five=12), block=True)
    cache = CodexQuotaCache(client=client)
    monkeypatch.setattr(limit_reset, "_CODEX_QUOTA_CACHE", cache)
    actions = []

    class Watcher:
        def status(self):
            actions.append("status")
            return {"enabled": enabled, "events": []}
        def check_once(self): actions.append("check")
        def check_provider(self, *_args): actions.append("arm")
        def _save_state(self): actions.append("persist")

    monkeypatch.setattr(server.Handler, "reset_service", Watcher())
    httpd = server.ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    worker = threading.Thread(target=httpd.serve_forever, daemon=True)
    worker.start()
    connection = HTTPConnection("127.0.0.1", httpd.server_port, timeout=2)
    origin = f"http://127.0.0.1:{httpd.server_port}"
    try:
        connection.request("GET", "/api/quota", headers={"Origin": origin, "Sec-Fetch-Site": "same-origin"})
        response = connection.getresponse()
        payload = json.loads(response.read())
        assert response.status == 200 and payload["quotaCache"]["ready"] is False
        assert "lastStatus" not in payload["providers"]["Codex"]
        assert client.entered.wait(1)
        client.release.set()
        _wait_idle(cache)

        connection.request("GET", "/api/quota", headers={"Origin": origin, "Sec-Fetch-Site": "same-origin"})
        response = connection.getresponse()
        payload = json.loads(response.read())
        assert response.status == 200 and payload["quotaCache"]["ready"]
        assert payload["providers"]["Codex"]["lastStatus"]["five_hour_percent"] == 12
        connection.request("GET", "/api/quota", headers={"Origin": "http://evil.example"})
        assert connection.getresponse().status == 403
        connection.request("POST", "/api/quota", body="{}", headers={
            "Origin": origin, "Content-Type": "application/json", "Content-Length": "2"})
        assert connection.getresponse().status == 404
    finally:
        client.release.set()
        connection.close()
        httpd.shutdown(); httpd.server_close(); worker.join(2)
    assert actions == [] and client.calls == 1
