from __future__ import annotations

import http.client
import json
import io
import socket
import threading
import time
from email.message import Message
from http.server import ThreadingHTTPServer

import pytest

from token_tracker import claude_collection, server


@pytest.fixture
def server_errors():
    return []


@pytest.fixture
def api(server_errors):
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    httpd.handle_error = lambda request, address: server_errors.append((request, address))
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield httpd.server_port
    finally:
        server.set_settings_token(None)
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=2)


def request(port, method="GET", body=None, headers=None, origin=True, path="/api/claude-collection"):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    request_headers = {"Host": f"127.0.0.1:{port}"}
    if origin:
        request_headers["Origin"] = f"http://127.0.0.1:{port}"
    request_headers.update(headers or {})
    encoded = None if body is None else json.dumps(body).encode()
    if encoded is not None:
        request_headers.setdefault("Content-Type", "application/json")
        request_headers.setdefault("Content-Length", str(len(encoded)))
    conn.request(method, path, body=encoded, headers=request_headers)
    response = conn.getresponse()
    result = json.loads(response.read())
    conn.close()
    return response.status, result


def test_status_get_and_scoped_mutation_token_flow(api, monkeypatch):
    monkeypatch.setattr(claude_collection, "status", lambda: {
        "state": "off", "reason": None, "hooks": {"state": "missing"},
        "consent": False, "grant": False, "restartRequired": False})
    monkeypatch.setattr(claude_collection, "enable", lambda: {"ok": True, "state": "on"})
    monkeypatch.setattr(claude_collection, "disable", lambda: {"ok": True, "state": "off"})

    status, data = request(api)
    assert status == 200 and data["state"] == "off" and data["ok"] is True
    # A browser-shaped same-origin GET may omit Origin but supplies fetch metadata.
    status, data = request(api, headers={"Referer": f"http://127.0.0.1:{api}/reset_actions.html",
                                         "Sec-Fetch-Site": "same-origin"}, origin=False)
    assert status == 200 and data["state"] == "off"
    old_token = "window-token-one"
    server.set_settings_token(old_token)
    status, denied = request(api, "POST", {"action": "enable"})
    assert status == 403 and denied["error"] == "Open settings from the Branchglance widget to change this."
    status, denied = request(api, "POST", {"action": "enable"}, {"X-Branchglance-Settings-Token": "wrong"})
    assert status == 403 and denied["error"] == "Open settings from the Branchglance widget to change this."
    status, denied = request(api, "POST", {"action": "enable"},
                             {"Origin": "http://foreign.invalid",
                              "X-Branchglance-Settings-Token": old_token})
    assert status == 403 and denied["error"] == "Open settings from the Branchglance widget to change this."
    status, denied = request(api, "POST", {"action": "enable"}, path=f"/api/claude-collection?t={old_token}")
    assert status == 403 and denied["error"] == "Open settings from the Branchglance widget to change this."
    status, denied = request(api, "POST", {"action": "enable"},
                             {"X-Branchglance-Settings-Token": old_token, "Content-Type": "text/plain"})
    assert status == 403 and denied["error"] == "Open settings from the Branchglance widget to change this."
    status, data = request(api, "POST", {"action": "enable"},
                           {"X-Branchglance-Settings-Token": old_token})
    assert status == 200 and data["state"] == "on"

    server.set_settings_token(None)  # close and quit revoke the current capability
    status, denied = request(api, "POST", {"action": "disable"},
                             {"X-Branchglance-Settings-Token": old_token})
    assert status == 403 and denied["error"] == "Open settings from the Branchglance widget to change this."
    quit_token = "window-token-before-quit"
    server.set_settings_token(quit_token)
    server.set_settings_token(None)  # application quit clears the active window token
    status, _ = request(api, "POST", {"action": "disable"},
                        {"X-Branchglance-Settings-Token": quit_token})
    assert status == 403
    new_token = "window-token-two"
    server.set_settings_token(new_token)
    status, _ = request(api, "POST", {"action": "disable"},
                        {"X-Branchglance-Settings-Token": old_token})
    assert status == 403
    status, data = request(api, "POST", {"action": "disable"},
                           {"X-Branchglance-Settings-Token": new_token})
    assert status == 200 and data["state"] == "off"


def test_status_origin_guard(api):
    status, _ = request(api, headers={"Origin": "http://evil.invalid"})
    assert status == 403


def _rejection_handler(headers, chunks):
    handler = server.Handler.__new__(server.Handler)
    handler.headers = Message()
    for name, value in headers:
        handler.headers.add_header(name, value)
    class Connection:
        timeout = 7
        calls = []
        def gettimeout(self): return self.timeout
        def settimeout(self, value):
            self.calls.append(value)
            self.timeout = value
    class Reader:
        calls = []
        def read1(self, size):
            self.calls.append(size)
            result = chunks.pop(0)
            if isinstance(result, Exception): raise result
            return result
    handler.connection = Connection()
    handler.rfile = Reader()
    handler.wfile = io.BytesIO()
    handler.send_response = lambda status: None
    handler.send_header = lambda *args: None
    handler.end_headers = lambda: None
    return handler


@pytest.mark.parametrize("path,headers", [
    ("/unknown", [("Content-Length", "1")]),
    ("/api/queue", []),
    ("/api/queue", [("Content-Length", "1"), ("Content-Length", "1")]),
    ("/api/queue", [("Content-Length", "1, 1")]),
    ("/api/queue", [("Content-Length", "+1")]),
    ("/api/queue", [("Content-Length", "-1")]),
    ("/api/queue", [("Content-Length", "2513")]),
    ("/api/claude-collection", [("Content-Length", "4097")]),
    ("/api/queue", [("Content-Length", "1"), ("Transfer-Encoding", "identity")]),
])
def test_rejection_never_reads_invalid_framing(path, headers):
    handler = _rejection_handler(headers, [])
    handler._reject_post(path, {"ok": False}, 403)
    assert handler.rfile.calls == []
    assert handler.close_connection
    assert handler.connection.timeout == 7


@pytest.mark.parametrize("chunks,expected", [
    ([b"a", b"bc"], [3, 2]), ([b"a", b""], [3, 2]),
    ([OSError("closed")], [3]),
])
def test_rejection_bounded_read_and_restore(chunks, expected):
    handler = _rejection_handler([("Content-Length", "3")], chunks)
    handler._reject_post("/api/claude-collection", {"ok": False}, 403)
    assert handler.rfile.calls == expected
    assert handler.connection.timeout == 7


def test_rejection_zero_and_deadline(monkeypatch):
    handler = _rejection_handler([("Content-Length", "0")], [])
    handler._reject_post("/api/queue", {}, 403)
    assert handler.rfile.calls == [] and handler.connection.timeout == 7
    handler = _rejection_handler([("Content-Length", "3")], [b"a", b"b"])
    times = iter([0, 0.05, 0.15, 0.26])
    monkeypatch.setattr(server.time, "monotonic", lambda: next(times))
    handler._reject_post("/api/queue", {}, 403)
    assert handler.rfile.calls == [3, 2]
    assert handler.connection.calls == pytest.approx([0.20, 0.10, 7])
    assert handler.connection.timeout == 7


def test_denied_guards_and_no_mutation(api, monkeypatch):
    calls = []
    monkeypatch.setattr(claude_collection, "enable", lambda: calls.append("enable"))
    monkeypatch.setattr(claude_collection, "disable", lambda: calls.append("disable"))
    server.set_settings_token("valid")
    cases = [
        ("/api/claude-collection", {"Host": "evil", "Origin": "http://evil"}, 403, server._SETTINGS_AUTH_MESSAGE),
        ("/missing", {"Origin": "http://evil"}, 404, "not found"),
        ("/api/queue", {"Origin": "http://evil", "Content-Type": "text/plain"}, 403, "same-origin request required"),
        ("/api/claude-collection", {"X-Branchglance-Settings-Token": "wrong"}, 403, server._SETTINGS_AUTH_MESSAGE),
        ("/api/queue", {"Content-Type": "text/plain"}, 415, "application/json required"),
    ]
    for path, headers, status, error in cases:
        actual, body = request(api, "POST", {"action": "enable"}, headers, path=path)
        assert (actual, body) == (status, {"ok": False, "error": error})
    assert calls == []


def test_slow_drip_denial_has_total_deadline(api):
    with socket.create_connection(("127.0.0.1", api), timeout=2) as sock:
        sock.sendall((f"POST /api/claude-collection HTTP/1.1\r\nHost: 127.0.0.1:{api}\r\n"
                      f"Origin: http://127.0.0.1:{api}\r\nContent-Length: 100\r\n\r\n").encode())
        started = time.monotonic()
        sock.sendall(b"a")
        time.sleep(0.12)
        sock.sendall(b"b")
        response = sock.recv(4096)
        elapsed = time.monotonic() - started
    assert b"403" in response and b"Connection: close" in response
    assert 0.18 <= elapsed < 3.0


def test_complete_small_body_denials_loopback_stress(api, monkeypatch, server_errors):
    calls = []
    monkeypatch.setattr(claude_collection, "enable", lambda: calls.append("enable"))
    counts = {"denied": 0, "other": 0, "exception": 0}
    first_exception = None
    for _ in range(1000):
        try:
            status, body = request(api, "POST", {"action": "enable"})
            counts["denied" if status == 403 and body.get("error") == server._SETTINGS_AUTH_MESSAGE else "other"] += 1
        except (OSError, http.client.HTTPException, ValueError) as exc:
            counts["exception"] += 1
            if first_exception is None:
                first_exception = f"{type(exc).__name__}: {exc}"
    assert counts == {"denied": 1000, "other": 0, "exception": 0}, first_exception
    assert server_errors == []
    assert calls == []
