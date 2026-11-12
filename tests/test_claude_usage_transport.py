"""Offline transport tests for the Claude usage HTTPS client and refresher lock."""
from __future__ import annotations

import contextlib
import datetime
import json
import os
import queue
import shutil
import socket
import ssl
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from token_tracker import claude_identity as identity, claude_usage_api as api, product

REPO = Path(__file__).resolve().parents[1]
HOST = "api.anthropic.com"
TOKEN = "SENTINEL_TOKEN_abc"

_OPENSSL_CONFIG = """[req]
distinguished_name = dn
x509_extensions = v3_req
prompt = no
[dn]
CN = api.anthropic.com
[v3_req]
basicConstraints = critical,CA:TRUE
keyUsage = digitalSignature,keyEncipherment
subjectAltName = DNS:api.anthropic.com
"""


def _cert_with_cryptography(directory):
    try:
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.x509.oid import NameOID
    except ImportError:
        return False
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, HOST)])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
            .public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(minutes=5))
            .not_valid_after(now + datetime.timedelta(days=2))
            .add_extension(x509.SubjectAlternativeName([x509.DNSName(HOST)]), critical=False)
            .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
            .sign(key, hashes.SHA256()))
    (directory / "key.pem").write_bytes(key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL,
        serialization.NoEncryption()))
    (directory / "cert.pem").write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    return True


def _cert_with_openssl(directory):
    config = directory / "openssl.cnf"
    config.write_text(_OPENSSL_CONFIG)
    env = dict(os.environ, MSYS_NO_PATHCONV="1")
    for exe in (shutil.which("openssl"), r"C:\Program Files\Git\usr\bin\openssl.exe"):
        if not exe or not os.path.exists(exe):
            continue
        try:
            subprocess.run([exe, "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "2",
                            "-config", str(config), "-extensions", "v3_req",
                            "-keyout", str(directory / "key.pem"), "-out", str(directory / "cert.pem")],
                           check=True, capture_output=True, timeout=60, env=env)
        except (OSError, subprocess.SubprocessError):
            continue
        if (directory / "cert.pem").exists() and (directory / "key.pem").exists():
            return True
    return False


@pytest.fixture(scope="module")
def tls_material(tmp_path_factory):
    directory = tmp_path_factory.mktemp("tls")
    if not (_cert_with_cryptography(directory) or _cert_with_openssl(directory)):
        pytest.skip("no way to make a test certificate")
    return SimpleNamespace(cert=str(directory / "cert.pem"), key=str(directory / "key.pem"),
                           pem=(directory / "cert.pem").read_text())


@contextlib.contextmanager
def tls_server(material, behavior):
    """Accept TLS on 127.0.0.1; after the request head is read, call behavior(conn, stop)."""
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(material.cert, material.key)
    listener = socket.create_server(("127.0.0.1", 0))
    listener.settimeout(0.1)
    stop = threading.Event()
    server = SimpleNamespace(port=listener.getsockname()[1], requests=[],
                             request_seen=threading.Event())
    workers = []

    def serve(raw):
        try:
            conn = context.wrap_socket(raw, server_side=True)
        except OSError:  # handshake failures are the client's choice, not a test error
            raw.close()
            return
        try:
            conn.settimeout(5)
            data = b""
            while b"\r\n\r\n" not in data:
                chunk = conn.recv(4096)
                if not chunk:
                    break
                data += chunk
            if data:
                server.requests.append(data)
                server.request_seen.set()
                behavior(conn, stop)
        except OSError:
            pass
        finally:
            conn.close()

    def accept_loop():
        while not stop.is_set():
            try:
                raw, _ = listener.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            worker = threading.Thread(target=serve, args=(raw,), daemon=True)
            workers.append(worker)
            worker.start()

    acceptor = threading.Thread(target=accept_loop, daemon=True)
    acceptor.start()
    try:
        yield server
    finally:
        stop.set()
        acceptor.join(5)
        listener.close()
        for worker in workers:
            worker.join(5)


def _drip(payload, interval):
    def behavior(conn, stop):
        for index in range(len(payload)):
            conn.sendall(payload[index:index + 1])
            if stop.wait(interval):
                return
    return behavior


def _drip_body(interval):
    def behavior(conn, stop):
        conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 5000\r\n\r\n")
        while not stop.wait(interval):
            conn.sendall(b" ")
    return behavior


def _hang(conn, stop):
    stop.wait(5)


@pytest.fixture
def route_to(monkeypatch):
    """Send api.anthropic.com to a local port, with the deadline shortened to 1.5 s."""
    real_getaddrinfo = socket.getaddrinfo
    real_context = ssl.create_default_context

    def route(port, *, trust_cert_pem=None):
        def getaddrinfo(host, *args, **kwargs):
            if host == HOST:
                return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", port))]
            return real_getaddrinfo(host, *args, **kwargs)
        monkeypatch.setattr(api.socket, "getaddrinfo", getaddrinfo)
        monkeypatch.setattr(api, "_REQUEST_DEADLINE", 1.5)
        if trust_cert_pem is not None:
            trusting = lambda *a, **k: real_context(cadata=trust_cert_pem)
            monkeypatch.setattr(api.ssl, "create_default_context", trusting)
    return route


def test_slow_drip_headers_hit_the_whole_request_deadline(tls_material, route_to):
    payload = b"HTTP/1.1 200 OK\r\nX-Slow: " + b"x" * 400 + b"\r\n\r\n"
    with tls_server(tls_material, _drip(payload, 0.15)) as server:
        route_to(server.port, trust_cert_pem=tls_material.pem)
        started = time.monotonic()
        with pytest.raises(RuntimeError) as error:
            api._request("/api/oauth/usage", TOKEN)
        elapsed = time.monotonic() - started
    assert server.request_seen.is_set()
    assert 1.0 < elapsed < api._REQUEST_DEADLINE + 2
    assert TOKEN not in str(error.value)


def test_slow_drip_body_hits_the_whole_request_deadline(tls_material, route_to):
    with tls_server(tls_material, _drip_body(0.1)) as server:
        route_to(server.port, trust_cert_pem=tls_material.pem)
        started = time.monotonic()
        with pytest.raises(RuntimeError) as error:
            api._request("/api/oauth/usage", TOKEN)
        elapsed = time.monotonic() - started
    assert server.request_seen.is_set()
    assert 1.0 < elapsed < api._REQUEST_DEADLINE + 2
    assert TOKEN not in str(error.value)


def test_untrusted_certificate_is_rejected_before_the_token_is_sent(tls_material, route_to):
    with tls_server(tls_material, _hang) as server:
        route_to(server.port)  # real default trust store: the self-signed certificate must fail
        started = time.monotonic()
        with pytest.raises(RuntimeError) as error:
            api._request("/api/oauth/usage", TOKEN)
        elapsed = time.monotonic() - started
    assert elapsed < 5
    assert TOKEN not in str(error.value)
    assert server.requests == []
    assert not server.request_seen.is_set()


def _fake_identity(tmp_path, monkeypatch, now):
    config = tmp_path / "claude-config"
    config.mkdir(exist_ok=True)
    (config / ".credentials.json").write_text(json.dumps({"claudeAiOauth": {
        "accessToken": TOKEN, "expiresAt": (now + 3600) * 1000, "scopes": ["user:profile"]}}))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config))
    ident = identity.Identity("account", "org", "generation", changed_at=now - 100)
    monkeypatch.setattr(identity, "current_identity", lambda: ident)
    monkeypatch.setattr(product, "state_root", lambda: tmp_path / "state")
    monkeypatch.setattr(api, "policy_for",
                        lambda name: type("Policy", (), {"allows": lambda self, action: True})())
    return ident


def test_stop_during_an_in_flight_request_returns_within_the_deadline(tls_material, route_to, tmp_path, monkeypatch):
    ident = _fake_identity(tmp_path, monkeypatch, time.time())
    monkeypatch.setattr(api.random, "uniform", lambda a, b: 0)
    refresher = api.ClaudeUsageRefresher(tmp_path / "state", request=lambda path, token: api._request(path, token))
    with tls_server(tls_material, _hang) as server:
        route_to(server.port, trust_cert_pem=tls_material.pem)
        try:
            refresher.start()
            assert server.request_seen.wait(5)
            started = time.monotonic()
            refresher.stop()
            elapsed = time.monotonic() - started
            assert elapsed < api._REQUEST_DEADLINE + 3
            assert not refresher._thread.is_alive()
            assert refresher.read(ident) == {}
            assert refresher._sample == {}
        finally:
            if refresher._owner_context is not None:
                refresher._owner_context.__exit__(None, None, None)


@pytest.mark.parametrize("terminate", [True, False], ids=["terminate", "graceful"])
def test_polling_lock_is_released_when_the_owning_process_exits(tmp_path, terminate):
    root = tmp_path / "state"
    script = (
        "import sys\n"
        "import token_tracker.claude_usage_api as api\n"
        "refresher = api.ClaudeUsageRefresher(sys.argv[1], request=lambda *a: None)\n"
        "print('locked' if refresher.acquire() else 'busy', flush=True)\n"
        "sys.stdin.read()\n"
    )
    env = dict(os.environ, PYTHONPATH=str(REPO))
    proc = subprocess.Popen([sys.executable, "-c", script, str(root)], cwd=str(REPO), env=env,
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    lines = queue.Queue()
    threading.Thread(target=lambda: [lines.put(line) for line in iter(proc.stdout.readline, "")],
                     daemon=True).start()
    try:
        assert lines.get(timeout=20).strip() == "locked"
        other = api.ClaudeUsageRefresher(root)
        contested = other.acquire()
        if contested:
            other._owner_context.__exit__(None, None, None)
        assert not contested
        if terminate:
            proc.kill()
        else:
            proc.stdin.close()
        proc.wait(timeout=10)
        deadline = time.monotonic() + 5
        while True:
            contender = api.ClaudeUsageRefresher(root)
            if contender.acquire():
                break
            assert time.monotonic() < deadline, "polling lock not released after the owner exited"
            time.sleep(0.05)
        contender._owner_context.__exit__(None, None, None)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(10)
        with contextlib.suppress(OSError):
            proc.stdin.close()
        proc.stdout.close()


class _Sock:
    def __init__(self):
        self.timeouts = []

    def settimeout(self, value):
        self.timeouts.append(value)


def test_bound_read_without_private_raw_socket_uses_connection_socket():
    response = SimpleNamespace(fp=SimpleNamespace(raw=object()))
    recorder = _Sock()
    api._bound_read(response, [SimpleNamespace(sock=None), SimpleNamespace(sock=recorder)], 0.7)
    assert recorder.timeouts == [0.7]


def test_bound_read_without_any_socket_is_a_no_op():
    response = SimpleNamespace(fp=SimpleNamespace(raw=object()))
    api._bound_read(response, [SimpleNamespace(sock=None), object()], 0.7)


def test_missing_raw_socket_still_enforces_the_deadline(tls_material, route_to, monkeypatch):
    monkeypatch.setattr(api, "_bound_read", lambda *a, **k: None)
    with tls_server(tls_material, _drip_body(0.1)) as server:
        route_to(server.port, trust_cert_pem=tls_material.pem)
        started = time.monotonic()
        with pytest.raises(RuntimeError) as error:
            api._request("/api/oauth/usage", TOKEN)
        elapsed = time.monotonic() - started
    assert server.request_seen.is_set()
    assert elapsed < api._REQUEST_DEADLINE + 2
    assert TOKEN not in str(error.value)


def test_response_without_raw_socket_returns_valid_json(monkeypatch):
    class Response:
        status = 200
        headers = {}
        fp = object()

        def __init__(self, body):
            self.remaining = body

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read1(self, length):
            result, self.remaining = self.remaining[:length], self.remaining[length:]
            return result

    class Opener:
        def open(self, *args, **kwargs):
            return Response(b'{"ok": true}')

    monkeypatch.setattr(api.urllib.request, "build_opener", lambda *args: Opener())
    assert api._request("/api/oauth/usage", TOKEN) == (200, {}, {"ok": True})
