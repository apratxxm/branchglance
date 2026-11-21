import hashlib
import hmac
import http.client
import json
import os
import socket
import subprocess
import sys
import threading

import pytest
from token_tracker import macos_server
from token_tracker.macos_server import proof_handler
from token_tracker.server import ThreadingHTTPServer

KEY = bytes(range(32))
CHALLENGE = bytes(range(32, 64))
REQUEST_VECTOR = "ea0269979ba118089f201ff66842118af74fb1489435fcad9ee72cde8066177c"
RESPONSE_VECTOR = "416fc4718aeb6e4e4cd31d14c359ca6b1db354b6c415d53e2e4a0d0bfe72f9e5"


def signature(key, label, challenge):
    return hmac.new(key, b"branchglance-macos-proof-v1/" + label + b":" + challenge, hashlib.sha256).hexdigest()


def test_proof_framing_and_rejection(capsys):
    assert signature(KEY, b"request", CHALLENGE) == REQUEST_VECTOR
    assert signature(KEY, b"response", CHALLENGE) == RESPONSE_VECTOR
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), proof_handler(KEY))
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    port = httpd.server_port
    def request(path="/api/macos-host-proof", challenge=CHALLENGE.hex(), auth=REQUEST_VECTOR, extra=None):
        headers = {"Host": f"127.0.0.1:{port}", "X-Branchglance-Challenge": challenge,
                   "X-Branchglance-Auth": auth}
        headers = {key: value for key, value in headers.items() if value is not None}
        headers.update(extra or {})
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
        conn.request("GET", path, headers=headers)
        response = conn.getresponse()
        body = response.read()
        result = response.status, dict(response.getheaders()), body
        conn.close()
        return result
    try:
        status, headers, body = request()
        assert status == 200
        assert json.loads(body) == {"version": 1, "proof": RESPONSE_VECTOR}
        assert headers["Cache-Control"] == "no-store"
        assert not any(k.lower().startswith("access-control-") for k in headers)
        assert KEY.hex().encode() not in body and signature(KEY, b"request", CHALLENGE).encode() not in body
        invalid = [
            {"auth": signature(KEY, b"response", CHALLENGE)},
            {"auth": signature(bytes(32), b"request", CHALLENGE)},
            {"challenge": bytes(32).hex()}, {"challenge": "00"},
            {"challenge": None}, {"auth": None},
            {"challenge": CHALLENGE.hex().upper()},
            {"path": "/api/macos-host-proof?x=1"},
            {"extra": {"Origin": "http://127.0.0.1"}},
            {"extra": {"Host": f"example.com:{port}"}},
        ]
        for case in invalid:
            status, headers, body = request(**case)
            assert status == 403, case
            assert headers["Cache-Control"] == "no-store"
            assert not any(k.lower().startswith("access-control-") for k in headers)
            assert KEY.hex().encode() not in body and signature(KEY, b"request", CHALLENGE).encode() not in body
    finally:
        httpd.shutdown(); thread.join(timeout=3); httpd.server_close()
    logs = capsys.readouterr()
    for value in (KEY.hex(), REQUEST_VECTOR, RESPONSE_VECTOR):
        assert value not in logs.out + logs.err


def test_macos_host_keeps_known_open_http_gap_while_shared_handler_returns_404(monkeypatch):
    from token_tracker import server

    thread_id = "01234567-89ab-cdef-0123-456789abcdef"
    opened = []
    monkeypatch.setattr(server.Handler, "_open_request_allowed", lambda _self: True)
    monkeypatch.setattr(server, "_codex_allows", lambda _action: True)
    monkeypatch.setattr(server, "_is_collected_codex_thread", lambda _thread: True)
    monkeypatch.setattr(server.os, "startfile", lambda value: opened.append(value), raising=False)
    servers = [ThreadingHTTPServer(("127.0.0.1", 0), proof_handler(KEY)),
               ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)]
    threads = [threading.Thread(target=httpd.serve_forever, daemon=True) for httpd in servers]
    for worker in threads:
        worker.start()
    try:
        for index, expected in enumerate((200, 404)):
            httpd = servers[index]
            conn = http.client.HTTPConnection("127.0.0.1", httpd.server_port, timeout=2)
            conn.request("GET", f"/api/open?thread={thread_id}", headers={"Host": f"127.0.0.1:{httpd.server_port}"})
            response = conn.getresponse()
            body = response.read()
            assert response.status == expected
            if index == 0:
                assert json.loads(body) == {"ok": True}
            conn.close()
        assert opened == [f"codex://threads/{thread_id}"]
    finally:
        for httpd, worker in zip(servers, threads):
            httpd.shutdown(); worker.join(timeout=3); httpd.server_close()


def test_missing_secret_and_prebound_port(tmp_path):
    env = os.environ.copy()
    env.pop("BRANCHGLANCE_MACOS_HOST_SECRET", None)
    env["LOCALAPPDATA"] = str(tmp_path)
    env["BRANCHGLANCE_POLICY_FILE"] = str(tmp_path / "policy.json")
    missing = subprocess.run([sys.executable, "-m", "token_tracker.macos_server"], env=env, capture_output=True, timeout=5)
    assert missing.returncode != 0
    env["BRANCHGLANCE_MACOS_HOST_SECRET"] = "bad"
    malformed = subprocess.run([sys.executable, "-m", "token_tracker.macos_server"], env=env, capture_output=True, timeout=5)
    assert malformed.returncode != 0
    env["BRANCHGLANCE_MACOS_HOST_SECRET"] = KEY.hex()
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0)); listener.listen()
        port = listener.getsockname()[1]
        child = subprocess.run([sys.executable, "-m", "token_tracker.macos_server", "--port", str(port)], env=env, capture_output=True, timeout=5)
        assert child.returncode != 0
        with socket.create_connection(("127.0.0.1", port), timeout=1):
            pass
        assert KEY.hex().encode() not in child.stdout + child.stderr


def test_secret_removed_from_environment_before_server_start(monkeypatch, tmp_path):
    monkeypatch.setenv("BRANCHGLANCE_MACOS_HOST_SECRET", KEY.hex())
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.setenv("BRANCHGLANCE_POLICY_FILE", str(tmp_path / "policy.json"))
    monkeypatch.setattr(sys, "argv", ["macos_server"])
    def stop_after_check(*args):
        assert "BRANCHGLANCE_MACOS_HOST_SECRET" not in os.environ
        raise RuntimeError("checked")
    monkeypatch.setattr(macos_server.server, "ThreadingHTTPServer", stop_after_check)
    with pytest.raises(RuntimeError, match="checked"):
        macos_server.main()
