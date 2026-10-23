from __future__ import annotations

import base64
import http.client
import json
import threading
from urllib.parse import urlsplit
import pytest

from token_tracker import phone_server


@pytest.fixture
def service(tmp_path):
    server = phone_server.create_server(port=0, state_path=tmp_path / "phone-state.json", start_monitor=False)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    try:
        yield server, host, port
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def request(service, method, path, *, body=None, headers=None):
    _, host, port = service
    connection = http.client.HTTPConnection(host, port, timeout=3)
    request_headers = {"Host": urlsplit(phone_server.DEFAULT_ORIGIN).netloc, **(headers or {})}
    connection.request(method, path, body=body, headers=request_headers)
    response = connection.getresponse()
    payload = response.read()
    result = (response.status, dict(response.getheaders()), payload)
    connection.close()
    return result


def pair(service):
    server, _, _ = service
    body = json.dumps({"code": server.phone_store.pairing_code()})
    status, headers, payload = request(
        service, "POST", "/api/phone/pair", body=body,
        headers={"Content-Type": "application/json", "Content-Length": str(len(body)), "Origin": phone_server.DEFAULT_ORIGIN},
    )
    assert status == 200, payload
    return headers["Set-Cookie"].split(";", 1)[0]


def _valid_subscription(endpoint="https://fcm.googleapis.com/fcm/send/opaque"):
    x = bytes.fromhex("6b17d1f2e12c4247f8bce6e563a440f277037d812deb33a0f4a13945d898c296")
    y = bytes.fromhex("4fe342e2fe1a7f9b8ee7eb4a7c0f9e162bce33576b315ececbb6406837bf51f5")
    return {"endpoint": endpoint, "keys": {
        "p256dh": base64.urlsafe_b64encode(b"\x04" + x + y).rstrip(b"=").decode(),
        "auth": base64.urlsafe_b64encode(b"b" * 16).rstrip(b"=").decode(),
    }}


def test_unpaired_client_sees_only_pair_form_and_no_api_data(service):
    status, _, _ = request(service, "GET", "/phone.html", headers={"Host": "127.0.0.1:1"})
    assert status == 403
    status, _, body = request(service, "GET", "/phone.html")
    assert status == 200 and b"Pair this phone" in body
    for route in ("/api/stats", "/api/live", "/api/phone/config"):
        status, _, body = request(service, "GET", route)
        assert status == 401
        assert b"providers" not in body and b"chats" not in body
    for route in ("/api/limit-reset", "/api/open?thread=x", "/api/queue"):
        status, _, _ = request(service, "GET", route)
        assert status == 404


def test_pair_uses_http_only_strict_cookie_and_pair_code_never_returns(service):
    server, _, _ = service
    code = server.phone_store.pairing_code()
    cookie = pair(service)
    assert cookie.startswith(phone_server.COOKIE_NAME + "=")
    assert "pairingCode" not in cookie and code not in cookie
    status, headers, page = request(service, "GET", "/phone.html", headers={"Cookie": cookie})
    assert status == 200 and b"phone-roster" in page
    assert "HttpOnly" in headers.get("Set-Cookie", "") or "Set-Cookie" not in headers


def test_pairing_code_compare_is_rate_limited_and_form_pairing_works(service):
    wrong = "code=incorrect"
    for _ in range(phone_server.PAIR_FAILURES):
        status, _, _ = request(service, "POST", "/api/phone/pair", body=wrong,
                               headers={"Content-Type": "application/x-www-form-urlencoded", "Origin": phone_server.DEFAULT_ORIGIN})
        assert status == 401
    status, _, _ = request(service, "POST", "/api/phone/pair", body=wrong,
                           headers={"Content-Type": "application/x-www-form-urlencoded", "Origin": phone_server.DEFAULT_ORIGIN})
    assert status == 429


def test_pair_rejects_cross_site_and_missing_origin_but_accepts_same_origin_form(service):
    server, _, _ = service
    body = "code=" + server.phone_store.pairing_code()
    headers = {"Content-Type": "application/x-www-form-urlencoded", "Content-Length": str(len(body))}
    for origin in (None, "https://evil.example"):
        supplied = dict(headers)
        if origin is not None:
            supplied["Origin"] = origin
        status, _, _ = request(service, "POST", "/api/phone/pair", body=body, headers=supplied)
        assert status == 403
    status, headers, _ = request(service, "POST", "/api/phone/pair", body=body,
                                 headers={**headers, "Origin": phone_server.DEFAULT_ORIGIN})
    assert status == 303
    assert headers["Set-Cookie"].startswith(phone_server.COOKIE_NAME + "=")


def test_authenticated_roster_proxy_sanitizes_and_throttles_refresh_query(monkeypatch, service):
    cookie = pair(service)
    seen = []

    class Response:
        def __enter__(self): return self
        def __exit__(self, *args): return False
        def read(self, limit): return b'{"providers":[]}'

    def fake_open(req, timeout):
        seen.append((req.full_url, timeout))
        return Response()

    monkeypatch.setattr(phone_server, "urlopen", fake_open)
    status, _, body = request(service, "GET", "/api/stats?refresh=1", headers={"Cookie": cookie})
    assert status == 200 and body == b'{"providers":[]}'
    assert seen == [(phone_server.DESKTOP_ORIGIN + "/api/stats?refresh=1", 20)]

    status, _, _ = request(service, "GET", "/api/stats?refresh=1", headers={"Cookie": cookie})
    status, _, _ = request(service, "GET", "/api/stats?refresh=1&limit=1", headers={"Cookie": cookie})
    assert status == 200
    assert seen[-2:] == [
        (phone_server.DESKTOP_ORIGIN + "/api/stats", 20),
        (phone_server.DESKTOP_ORIGIN + "/api/stats", 20),
    ]
    status, _, _ = request(service, "GET", "/api/stats?limit=1&provider=Codex", headers={"Cookie": cookie})
    assert status == 200 and seen[-1] == (phone_server.DESKTOP_ORIGIN + "/api/stats", 20)
    status, _, _ = request(service, "GET", "/api/live", headers={"Cookie": cookie})
    assert status == 200 and seen[-1] == (phone_server.DESKTOP_ORIGIN + "/api/live", 5)

    def failed(req, timeout):
        raise OSError("upstream path and data must not leak")

    monkeypatch.setattr(phone_server, "urlopen", failed)
    status, _, body = request(service, "GET", "/api/live", headers={"Cookie": cookie})
    assert status == 503 and b"upstream path" not in body
    assert b'"error":"desktop telemetry unavailable"' in body


def test_origin_json_and_allowlist_are_required_for_subscription(service):
    cookie = pair(service)
    body = json.dumps({"subscription": _valid_subscription()})
    headers = {"Cookie": cookie, "Content-Type": "application/json", "Content-Length": str(len(body))}
    status, _, _ = request(service, "POST", "/api/phone/subscription", body=body,
                           headers={**headers, "Origin": "https://evil.example"})
    assert status == 403
    status, _, _ = request(service, "POST", "/api/phone/subscription", body=body,
                           headers={**headers, "Origin": phone_server.DEFAULT_ORIGIN})
    assert status == 200

    invalid = json.dumps({"subscription": _valid_subscription("https://127.0.0.1/private")})
    status, _, payload = request(service, "POST", "/api/phone/subscription", body=invalid,
                                 headers={**headers, "Content-Length": str(len(invalid)), "Origin": phone_server.DEFAULT_ORIGIN})
    assert status == 400 and b"invalid subscription" in payload


def test_delete_removes_only_the_authenticated_phones_matching_subscription(service):
    server, _, _ = service
    cookie = pair(service)
    original = _valid_subscription()
    body = json.dumps({"subscription": original})
    headers = {"Cookie": cookie, "Origin": phone_server.DEFAULT_ORIGIN, "Content-Type": "application/json",
               "Content-Length": str(len(body))}
    status, _, _ = request(service, "POST", "/api/phone/subscription", body=body, headers=headers)
    assert status == 200
    assert len(server.phone_store.subscriptions()) == 1

    mismatch = _valid_subscription("https://fcm.googleapis.com/send/another-device")
    mismatch_body = json.dumps({"subscription": mismatch})
    mismatch_headers = {**headers, "Content-Length": str(len(mismatch_body))}
    status, _, _ = request(service, "DELETE", "/api/phone/subscription", body=mismatch_body, headers=mismatch_headers)
    assert status == 200 and len(server.phone_store.subscriptions()) == 1

    status, _, _ = request(service, "DELETE", "/api/phone/subscription", body=body, headers=headers)
    assert status == 200 and server.phone_store.subscriptions() == []


def test_no_mutation_route_or_static_traversal_and_body_limit(service):
    cookie = pair(service)
    headers = {"Cookie": cookie, "Origin": phone_server.DEFAULT_ORIGIN, "Content-Type": "application/json"}
    for route in ("/api/queue", "/api/open", "/api/today", "/api/avatars", "/api/limit-reset", "/%2e%2e/server.py"):
        status, _, _ = request(service, "POST", route, body="{}", headers={**headers, "Content-Length": "2"})
        assert status in {403, 404}
    status, _, _ = request(service, "POST", "/api/phone/subscription", body="x" * (phone_server.BODY_LIMIT + 1),
                           headers={**headers, "Content-Length": str(phone_server.BODY_LIMIT + 1)})
    assert status == 413


def test_forced_refresh_limiter_allows_at_most_one_per_ten_seconds():
    now = [25.0]
    limiter = phone_server.PhoneRefreshLimiter(clock=lambda: now[0])
    assert limiter.allow() is True
    assert limiter.allow() is False
    now[0] += phone_server.FORCED_REFRESH_INTERVAL - 0.01
    assert limiter.allow() is False
    now[0] += 0.01
    assert limiter.allow() is True


def test_session_expiry_invalidates_cookie(tmp_path, monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(phone_server.time, "time", lambda: now[0])
    store = phone_server.PhoneState(tmp_path / "state.json")
    token = store.create_session()
    assert store.session(token) is not None
    now[0] += phone_server.SESSION_TTL + 1
    assert store.session(token) is None


@pytest.mark.parametrize("endpoint", [
    "http://fcm.googleapis.com/send/x", "https://user:pass@example.test/send/x",
    "https://localhost/send/x", "https://192.168.1.1/send/x", "https://unknown.example/send/x",
    "https://evil.push.services.mozilla.com/send/x",
])
def test_rejects_untrusted_push_endpoints(endpoint):
    assert phone_server.valid_subscription(_valid_subscription(endpoint)) is None


def test_accepts_known_push_hosts_and_rejects_bad_key_shapes():
    assert phone_server.valid_subscription(_valid_subscription()) is not None
    value = _valid_subscription()
    value["keys"]["auth"] = "short"
    assert phone_server.valid_subscription(value) is None
    value = _valid_subscription()
    value["keys"]["p256dh"] = base64.urlsafe_b64encode(b"\x04" + b"a" * 64).rstrip(b"=").decode()
    assert phone_server.valid_subscription(value) is None
    browser_shape = {**_valid_subscription(), "expirationTime": None}
    assert phone_server.valid_subscription(browser_shape) == _valid_subscription()
    browser_shape["expirationTime"] = float("inf")
    assert phone_server.valid_subscription(browser_shape) is None


def test_vapid_state_is_private_and_only_public_config_is_exposed(tmp_path, monkeypatch):
    monkeypatch.setenv("TOKEN_TRACKER_VAPID_SUBJECT", "mailto:operator@example.test")
    store = phone_server.PhoneState(tmp_path / "state.json")
    state_text = (tmp_path / "state.json").read_text(encoding="utf-8")
    assert store.pairing_code() in state_text
    config = store.public_config()
    assert set(config) == {"pushReady", "publicKey"}
    assert "private" not in json.dumps(config)
