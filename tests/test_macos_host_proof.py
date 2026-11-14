import hashlib
import hmac
import http.client
import json
import os
import socket
import subprocess
import sys
import time
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


def test_server_runs_the_services_the_windows_widget_runs(monkeypatch, tmp_path):
    from token_tracker import agent_links, claude_hook, claude_usage_api, ledger_service, limit_reset, product
    calls = []

    class Service:
        def __init__(self, name):
            self.name = name
        def start(self):
            calls.append(("start", self.name))
        def stop(self):
            calls.append(("stop", self.name))

    class Server:
        def __init__(self, *args):
            pass
        def serve_forever(self):
            calls.append(("serve", None))
        def shutdown(self):
            pass
        def server_close(self):
            pass

    monkeypatch.setenv("BRANCHGLANCE_MACOS_HOST_SECRET", KEY.hex())
    monkeypatch.setenv("BRANCHGLANCE_POLICY_FILE", str(tmp_path / "policy.json"))
    monkeypatch.setattr(sys, "argv", ["macos_server"])
    monkeypatch.setattr(product, "hook_data_root", lambda: tmp_path)
    monkeypatch.setattr(product, "acquire_macos_instance_lock", lambda: True)
    monkeypatch.setattr(product, "macos_instance_held", lambda: True)
    monkeypatch.setattr(macos_server.server, "ThreadingHTTPServer", Server)
    monkeypatch.setattr(agent_links, "initialize_receipt_inbox", lambda: None)
    monkeypatch.setattr(ledger_service, "LedgerService", lambda: Service("ledger"))
    monkeypatch.setattr(limit_reset, "LimitResetService", lambda **kwargs: Service("reset"))
    monkeypatch.setattr(claude_usage_api, "REFRESHER", Service("usage"))
    monkeypatch.setattr(claude_hook, "start_event_retention", lambda: calls.append(("start", "retention")))
    monkeypatch.setattr(macos_server, "_warm_snapshot", lambda: calls.append(("start", "warmup")))
    monkeypatch.setattr(macos_server.server.Handler, "reset_service", None)
    macos_server.main()
    deadline = time.monotonic() + 2  # the warmup runs on its own thread
    while ("start", "warmup") not in calls and time.monotonic() < deadline:
        time.sleep(0.01)
    started = {name for kind, name in calls if kind == "start"}
    assert started == {"ledger", "usage", "reset", "retention", "warmup"}
    assert macos_server.server.Handler.reset_service.name == "reset"
    assert {name for kind, name in calls if kind == "stop"} == {"ledger", "usage", "reset"}
