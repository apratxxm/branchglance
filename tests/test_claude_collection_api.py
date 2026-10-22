from __future__ import annotations

import http.client
import json
import threading
from http.server import ThreadingHTTPServer

import pytest

from token_tracker import claude_collection, server


@pytest.fixture
def api():
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
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
