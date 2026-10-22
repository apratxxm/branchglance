from __future__ import annotations

import sys
import types

import pytest

from token_tracker import phone_push
from token_tracker import phone_server


class Store:
    def __init__(self, subscriptions):
        self.values = list(subscriptions)
        self.removed = []

    def subscriptions(self):
        return list(self.values)

    def remove_endpoint(self, endpoint):
        self.removed.append(endpoint)
        self.values = [item for item in self.values if item["endpoint"] != endpoint]


class Events:
    def __init__(self, values):
        self.values = list(values)
        self.calls = []

    def observe(self, chats, observed_at, healthy=True):
        self.calls.append((list(chats), observed_at, healthy))
        return self.values if healthy else []


def test_failed_or_incomplete_roster_never_produces_events(monkeypatch):
    store = Store([{"endpoint": "https://fcm.googleapis.com/send/x", "keys": {}}])
    engine = Events([{"kind": "completion"}])
    service = phone_push.PhonePushService(store, desktop_origin="http://127.0.0.1:8765", event_engine=engine)
    monkeypatch.setattr(phone_push, "_get_json", lambda url, timeout: None)

    assert service.poll_once(observed_at=1) == []
    assert engine.calls == [([], 1, False)]
    assert service.full_snapshot is None


def test_healthy_poll_normalizes_roster_and_dispatches_privacy_safe_event(monkeypatch):
    store = Store([{"endpoint": "https://fcm.googleapis.com/send/x", "keys": {}}])
    event = {"kind": "question", "title": "A chat needs your input", "body": "1 chat needs input.", "url": "/phone.html"}
    engine = Events([event])
    sent = []
    service = phone_push.PhonePushService(store, desktop_origin="http://127.0.0.1:8765", event_engine=engine,
                                          sender=lambda subscription, payload: sent.append((subscription, payload)) or 201,
                                          clock=lambda: 10)
    responses = {
        "http://127.0.0.1:8765/api/stats": {"providers": [{"name": "Codex", "chats": [{"id": "abc", "status": "running", "prompt": "private prompt"}]}]},
        "http://127.0.0.1:8765/api/live": {"chats": [{"key": "Codex:abc", "status": "needs input", "pendingQuestion": "private question"}]},
    }
    seen = []
    def get_json(url, timeout):
        seen.append((url, timeout))
        return responses[url]
    monkeypatch.setattr(phone_push, "_get_json", get_json)

    result = service.poll_once(observed_at="2026-10-07T00:00:00Z")
    assert result == [event]
    assert seen == [
        ("http://127.0.0.1:8765/api/stats", 20),
        ("http://127.0.0.1:8765/api/live", 5),
    ]
    chat = engine.calls[0][0][0]
    assert chat["key"] == "Codex:abc" and chat["status"] == "needs input"
    assert "private prompt" in chat["prompt"]
    assert sent[0][1] == event and "private question" not in str(sent)


def test_full_snapshot_cache_is_not_refreshed_before_120_seconds(monkeypatch):
    clock = [5.0]
    store = Store([{"endpoint": "https://fcm.googleapis.com/send/x", "keys": {}}])
    service = phone_push.PhonePushService(store, desktop_origin="http://127.0.0.1:8765", event_engine=Events([]),
                                          clock=lambda: clock[0])
    calls = []
    def get_json(url, timeout):
        calls.append(url)
        if url.endswith("/api/stats"):
            return {"providers": []}
        return {"chats": []}
    monkeypatch.setattr(phone_push, "_get_json", get_json)
    service.poll_once(observed_at=5)
    clock[0] += 119
    service.poll_once(observed_at=124)
    assert calls.count("http://127.0.0.1:8765/api/stats") == 1
    clock[0] += 1
    service.poll_once(observed_at=125)
    assert calls.count("http://127.0.0.1:8765/api/stats") == 2


def test_cold_full_snapshot_failure_retries_after_two_seconds(monkeypatch):
    clock = [0.0]
    store = Store([{"endpoint": "https://fcm.googleapis.com/send/x", "keys": {}}])
    service = phone_push.PhonePushService(store, desktop_origin="http://127.0.0.1:8765", event_engine=Events([]),
                                          clock=lambda: clock[0])
    stats_attempts = []
    def get_json(url, timeout):
        if url.endswith("/api/stats"):
            stats_attempts.append((clock[0], timeout))
            return None if len(stats_attempts) == 1 else {"providers": [], "structureStamp": "s1"}
        return {"chats": [], "structureStamp": "s1"}
    monkeypatch.setattr(phone_push, "_get_json", get_json)

    assert service.poll_once(observed_at=0) == []
    clock[0] = 1
    assert service.poll_once(observed_at=1) == []
    assert len(stats_attempts) == 1
    clock[0] = 2
    assert service.poll_once(observed_at=2) == []
    assert stats_attempts == [(0.0, 20), (2, 20)]


def test_repeated_full_snapshot_failures_back_off_two_four_eight_then_fifteen(monkeypatch):
    clock = [0.0]
    store = Store([{"endpoint": "https://fcm.googleapis.com/send/x", "keys": {}}])
    service = phone_push.PhonePushService(store, desktop_origin="http://127.0.0.1:8765", event_engine=Events([]),
                                          clock=lambda: clock[0])
    stats_attempts = []
    def get_json(url, timeout):
        if url.endswith("/api/stats"):
            stats_attempts.append(clock[0])
            return None
        return {"chats": []}
    monkeypatch.setattr(phone_push, "_get_json", get_json)

    for instant in (0, 1, 2, 5, 6, 13, 14, 28, 29, 44):
        clock[0] = instant
        service.poll_once(observed_at=instant)
    assert stats_attempts == [0, 2, 6, 14, 29, 44]


def test_structure_change_forces_snapshot_before_healthy_observation(monkeypatch):
    clock = [10.0]
    store = Store([{"endpoint": "https://fcm.googleapis.com/send/x", "keys": {}}])
    engine = Events([])
    forced = []
    def allow_refresh():
        forced.append(clock[0])
        return True
    service = phone_push.PhonePushService(store, desktop_origin="http://127.0.0.1:8765", event_engine=engine,
                                          clock=lambda: clock[0], refresh_gate=allow_refresh)
    responses = [
        {"providers": [], "structureStamp": "before"},
        {"chats": [], "structureStamp": "after"},
        {"providers": [{"name": "Codex", "chats": [{"id": "new", "status": "running"}]}], "structureStamp": "after"},
    ]
    seen = []
    def get_json(url, timeout):
        seen.append((url, timeout))
        return responses.pop(0)
    monkeypatch.setattr(phone_push, "_get_json", get_json)

    assert service.poll_once(observed_at=10) == []
    assert seen == [
        ("http://127.0.0.1:8765/api/stats", 20),
        ("http://127.0.0.1:8765/api/live", 5),
        ("http://127.0.0.1:8765/api/stats?refresh=1", 20),
    ]
    assert forced == [10.0]
    assert engine.calls[-1][2] is True
    assert engine.calls[-1][0][0]["id"] == "new"


def test_throttled_structure_change_does_not_repeat_full_fetch_or_mark_sample_healthy(monkeypatch):
    clock = [10.0]
    store = Store([{"endpoint": "https://fcm.googleapis.com/send/x", "keys": {}}])
    engine = Events([])
    seen = []
    service = phone_push.PhonePushService(store, desktop_origin="http://127.0.0.1:8765", event_engine=engine,
                                          clock=lambda: clock[0], refresh_gate=lambda: False)
    def get_json(url, timeout):
        seen.append(url)
        if url.endswith("/api/stats"):
            return {"providers": [], "structureStamp": "before"}
        return {"chats": [], "structureStamp": "after"}
    monkeypatch.setattr(phone_push, "_get_json", get_json)

    service.poll_once(observed_at=10)
    clock[0] += 2
    service.poll_once(observed_at=12)
    assert seen == [
        "http://127.0.0.1:8765/api/stats",
        "http://127.0.0.1:8765/api/live",
        "http://127.0.0.1:8765/api/live",
    ]
    assert [call[2] for call in engine.calls] == [False, False]


def test_expired_subscriptions_are_removed_and_provider_retry_is_bounded():
    subscriptions = [{"endpoint": "https://fcm.googleapis.com/send/expired", "keys": {}}]
    store = Store(subscriptions)
    waits = []
    outcomes = [503, 201]
    retry_store = Store([{"endpoint": "https://fcm.googleapis.com/send/retry", "keys": {}}])
    service = phone_push.PhonePushService(retry_store, desktop_origin="http://127.0.0.1:8765",
                                          sender=lambda *_: outcomes.pop(0), wait=lambda seconds: waits.append(seconds))
    service._dispatch({"kind": "failure"})
    assert waits == [1.0]

    expired = phone_push.PhonePushService(store, desktop_origin="http://127.0.0.1:8765", sender=lambda *_: 410)
    expired._dispatch({"kind": "failure"})
    assert store.removed == ["https://fcm.googleapis.com/send/expired"]


def test_retry_after_is_honored_but_capped():
    class Result:
        headers = {"Retry-After": "50"}
    assert phone_push._retry_after(Result()) == phone_push.MAX_RETRY_WAIT
    Result.headers = {"Retry-After": "0.25"}
    assert phone_push._retry_after(Result()) == 0.25


def test_push_work_stays_bounded_to_twenty_subscribers():
    subscriptions = [{"endpoint": f"https://fcm.googleapis.com/send/{index}", "keys": {}} for index in range(25)]
    sent = []
    service = phone_push.PhonePushService(Store(subscriptions), desktop_origin="http://127.0.0.1:8765",
                                          sender=lambda sub, event: sent.append(sub["endpoint"]) or 201)
    service._dispatch({"kind": "launch"})
    assert len(sent) == phone_push.SUBSCRIBER_LIMIT


def test_sender_uses_the_server_injected_store_key_without_network(monkeypatch):
    captured = {}
    class CustomStore(Store):
        def push_private_key(self):
            return "custom-store-private-key"

    def fake_webpush(**kwargs):
        captured.update(kwargs)
        return 201

    class FakeVapid:
        @classmethod
        def from_pem(cls, value):
            captured["loaded_pem"] = value
            return "vapid-instance-from-custom-store"

    monkeypatch.setenv("TOKEN_TRACKER_VAPID_SUBJECT", "mailto:phone@example.test")
    monkeypatch.setitem(sys.modules, "pywebpush", types.SimpleNamespace(webpush=fake_webpush))
    monkeypatch.setitem(sys.modules, "py_vapid", types.SimpleNamespace(Vapid=FakeVapid))
    subscription = {"endpoint": "https://fcm.googleapis.com/send/test", "keys": {"p256dh": "p", "auth": "a"}}
    event = {"kind": "completion", "body": "1 chat completed."}

    assert phone_push.send_web_push(CustomStore([]), subscription, event) == 201
    assert captured["loaded_pem"] == b"custom-store-private-key"
    assert captured["vapid_private_key"] == "vapid-instance-from-custom-store"
    assert captured["subscription_info"] == subscription
    assert captured["vapid_claims"] == {"sub": "mailto:phone@example.test"}


def test_real_pywebpush_encrypts_offline_with_store_vapid_key(tmp_path, monkeypatch):
    pywebpush = pytest.importorskip("pywebpush")
    py_vapid = pytest.importorskip("py_vapid")
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from token_tracker.phone_server import _b64url

    monkeypatch.setenv("TOKEN_TRACKER_VAPID_SUBJECT", "mailto:phone@example.test")
    store = phone_server.PhoneState(tmp_path / "custom-phone-state.json")
    config = store.public_config()
    assert config["pushReady"] is True
    vapid = py_vapid.Vapid.from_pem(store.push_private_key().encode("ascii"))
    public = vapid.public_key.public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    assert config["publicKey"] == _b64url(public)

    receiver = ec.generate_private_key(ec.SECP256R1())
    receiver_public = receiver.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    subscription = {"endpoint": "https://fcm.googleapis.com/send/offline-test", "keys": {
        "p256dh": _b64url(receiver_public), "auth": _b64url(b"a" * 16),
    }}
    event = {"title": "Tracker update", "body": "1 chat completed.", "url": "/phone.html", "tag": "tt-done"}
    plaintext = phone_push.json.dumps(event, separators=(",", ":")).encode()
    captured = {}

    class Response:
        status_code = 201
        text = ""
        reason = "Created"
        headers = {}

    def no_network_post(url, *, data, headers, timeout):
        captured.update(url=url, data=data, headers=headers, timeout=timeout)
        return Response()

    monkeypatch.setattr(pywebpush.requests, "post", no_network_post)
    response = phone_push.send_web_push(store, subscription, event)
    assert response.status_code == 201
    assert captured["url"] == subscription["endpoint"]
    assert captured["data"] != plaintext and isinstance(captured["data"], bytes)
    assert captured["headers"]["content-encoding"] == "aes128gcm"
    assert "Authorization" in captured["headers"]


def test_stopped_service_does_not_start_delivery_or_retry():
    calls = []
    service = phone_push.PhonePushService(
        Store([{"endpoint": "https://fcm.googleapis.com/send/test", "keys": {}}]),
        desktop_origin="http://127.0.0.1:8765",
        sender=lambda *_: calls.append("send") or 503,
    )
    service.stop_event.set()
    assert service._send_with_retry(service.store.subscriptions()[0], {"kind": "failure"}) is None
    service._dispatch({"kind": "failure"})
    assert calls == []
