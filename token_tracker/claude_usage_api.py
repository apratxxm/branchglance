"""Account-bound, read-only Claude usage polling."""
from __future__ import annotations

import hashlib
import json
import math
import os
import random
import http.client
import socket
import ssl
import threading
import time
import urllib.error
import urllib.request
from email.utils import parsedate_to_datetime
from pathlib import Path

from . import claude_identity, claude_statusline, product, features
from .provider_policy import policy_for

_MAX_BODY = 1024 * 1024
_REQUEST_DEADLINE = 10.0  # seconds for the whole request: DNS, connect, TLS, headers and body
_transport_lock = threading.RLock()
_transport_generation = 0
_active_connections = set()


def _transport_admit():
    with _transport_lock:
        if not live_usage_allowed():
            raise RuntimeError("Claude live usage disabled")
        return _transport_generation


def _transport_current(generation):
    with _transport_lock:
        return generation == _transport_generation and live_usage_allowed()


def _revoke_transports():
    global _transport_generation
    with _transport_lock:
        _transport_generation += 1
        connections = tuple(_active_connections)
    for connection in connections:
        try:
            connection.cancel()
        except OSError:
            pass


def live_usage_allowed():
    return policy_for("Claude").allows("collect") and features.enabled("live_usage")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def _opener(handler=None):
    handlers = [urllib.request.ProxyHandler({}), _NoRedirect()]
    if handler is not None:
        handlers.append(handler)
    return urllib.request.build_opener(*handlers)


def _credentials():
    # Serialize admission with feature saves, so a completed disable cannot be
    # followed by a credential open that was admitted under the old setting.
    with features._lock:
        if not live_usage_allowed():
            return None
        return _read_credentials()


def _read_credentials():
    # Keychain access is deliberately deferred until a noninteractive read is verified.
    config = os.environ.get("CLAUDE_CONFIG_DIR")
    path = (Path(config).expanduser() if config else Path.home() / ".claude") / ".credentials.json"
    doc = claude_identity._read_passive_document(path)
    value = doc.get("claudeAiOauth") if isinstance(doc, dict) else None
    if not isinstance(value, dict):
        return None
    token, expires, scopes = value.get("accessToken"), value.get("expiresAt"), value.get("scopes")
    if (not isinstance(token, str) or not token or type(expires) not in (int, float)
            or not isinstance(scopes, list) or not all(isinstance(s, str) for s in scopes)):
        return None
    try:
        if not math.isfinite(expires):
            return None
        content_hash = hashlib.sha256(json.dumps(doc, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()).digest()
    except (OverflowError, ValueError, TypeError, RecursionError):
        return None
    return token, expires, tuple(scopes), content_hash


def _fingerprint(credentials):
    # Account rejection is reset by any relevant credential content change.
    return credentials[3]


class _DeadlineConnection(http.client.HTTPSConnection):
    """Bound DNS, connection, TLS, headers and body by one deadline."""
    def __init__(self, *args, deadline, generation=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.deadline = deadline
        self.cancelled = threading.Event()
        self._active_socket = None
        self.generation = _transport_admit() if generation is None else generation

    def send(self, data):
        # HTTPConnection.send ordinarily connects lazily, after the caller's
        # admission check. Gate again after DNS/TLS, immediately before sendall.
        if self.sock is None:
            self.connect()
        if self.cancelled.is_set() or not _transport_current(self.generation):
            raise OSError("Claude usage request revoked")
        return super().send(data)

    def cancel(self):
        self.cancelled.set()
        sock = self._active_socket
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            sock.close()

    def connect(self):
        done = threading.Event()
        addresses = []
        def resolve():
            try:
                addresses.extend(socket.getaddrinfo(self.host, self.port, type=socket.SOCK_STREAM))
            except OSError:
                pass
            finally:
                done.set()
        threading.Thread(target=resolve, daemon=True).start()
        if not done.wait(max(0, self.deadline - time.monotonic())):
            raise TimeoutError()
        for family, kind, proto, _, address in addresses:
            remaining = self.deadline - time.monotonic()
            if remaining <= 0 or self.cancelled.is_set():
                raise TimeoutError()
            sock = socket.socket(family, kind, proto)
            self._active_socket = sock
            try:
                sock.settimeout(remaining)
                sock.connect(address)
                sock.settimeout(max(0.001, self.deadline - time.monotonic()))
                self.sock = self._context.wrap_socket(sock, server_hostname=self.host, do_handshake_on_connect=False)
                self._active_socket = self.sock
                if self.cancelled.is_set():
                    self.cancel()
                    raise TimeoutError()
                self.sock.do_handshake()
                return
            except OSError:
                sock.close()
        raise OSError("connection failed")


def _bound_read(response, connections, remaining):
    """Shrink the socket timeout to the time left on the whole-request deadline.

    The response's raw socket is a private http.client detail. If it is not where
    it was, fall back to the public ``sock`` of the deadline connections; if that
    fails too, the whole-request timer in ``_request`` still cancels the active socket, so
    a read can never outlive the deadline.
    """
    try:
        response.fp.raw._sock.settimeout(remaining)
        return
    except (AttributeError, TypeError):
        pass
    for connection in connections:
        sock = getattr(connection, "sock", None)
        if sock is not None:
            try:
                sock.settimeout(remaining)
            except (AttributeError, OSError):
                pass


def _request(path, token):
    """Return status, headers and bounded body; exceptions never escape with secrets."""
    generation = _transport_admit()
    request = urllib.request.Request("https://api.anthropic.com" + path, headers={
        "Authorization": "Bearer " + token, "anthropic-beta": "oauth-2025-04-20",
        "Content-Type": "application/json", "User-Agent": "Branchglance/" + product.VERSION,
    })
    started = time.monotonic()
    deadline = started + _REQUEST_DEADLINE
    connections = []
    class Handler(urllib.request.HTTPSHandler):
        def https_open(self, req):
            def connection(host, **kwargs):
                obj = _DeadlineConnection(host, deadline=deadline, generation=generation, **kwargs)
                with _transport_lock:
                    connections.append(obj)
                    _active_connections.add(obj)
                    if generation != _transport_generation:
                        obj.cancel()
                return obj
            return self.do_open(connection, req, context=self._context)
    opener = _opener(Handler(context=ssl.create_default_context()))
    def cancel():
        for conn in connections:
            conn.cancel()
    timer = threading.Timer(_REQUEST_DEADLINE, cancel)
    timer.daemon = True
    timer.start()
    try:
        try:
            if not _transport_current(generation):
                raise RuntimeError("Claude live usage disabled")
            response = opener.open(request, timeout=_REQUEST_DEADLINE)
        except urllib.error.HTTPError as exc:
            response = exc
        with response:
            if not _transport_current(generation):
                raise RuntimeError("Claude usage request revoked")
            status, headers = response.status, response.headers
            if status != 200:
                if not _transport_current(generation):
                    raise RuntimeError("Claude usage request revoked")
                return status, headers, None
            body = bytearray()
            while len(body) <= _MAX_BODY:
                remaining = _REQUEST_DEADLINE - (time.monotonic() - started)
                if remaining <= 0:
                    raise TimeoutError()
                # urllib's socket timeout otherwise applies separately to each read.
                _bound_read(response, connections, remaining)
                chunk = response.read1(min(65536, _MAX_BODY + 1 - len(body)))
                if not chunk:
                    break
                body.extend(chunk)
            if len(body) > _MAX_BODY:
                raise ValueError()
            result = json.loads(body) if body else None
            if time.monotonic() >= deadline or not _transport_current(generation):
                raise TimeoutError()
            return status, headers, result
    except Exception:
        raise RuntimeError("Claude usage request failed") from None
    finally:
        timer.cancel()
        with _transport_lock:
            _active_connections.difference_update(connections)


def _retry_after(value, now):
    try:
        delay = float(value)
    except (TypeError, ValueError):
        try:
            delay = parsedate_to_datetime(value).timestamp() - now
        except (TypeError, ValueError, OverflowError):
            delay = 30
    return max(0, min(3600, delay)) if math.isfinite(delay) else 30


class ClaudeUsageRefresher:
    def __init__(self, root=None, *, clock=time.time, request=None):
        self.root = Path(root) if root is not None else product.state_root()
        self.clock = clock
        self.request = request or _request
        self._lock = threading.RLock()
        self._poll_lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = None
        self._owner = None
        self._owner_context = None
        self._fingerprint = None
        self._rejected = None
        self._binding = None
        self._sample = {}
        self._failures = 0
        self._revocation = 0
        self._next = 0.0
        self._retry_until = self._read_cooldown()

    def _read_cooldown(self):
        doc = claude_identity._read_passive_document(self.root / "claude-usage-cooldown.json")
        value = doc.get("retryAfterUntil") if isinstance(doc, dict) else None
        return float(value) if type(value) in (int, float) and math.isfinite(value) else 0.0

    def _save_cooldown(self):
        import tempfile
        with claude_statusline._guard_directories(self.root, create=True):
            target = self.root / "claude-usage-cooldown.json"
            claude_statusline._check_path(target, allow_missing=True, directory=False)
            name = None
            try:
                with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=self.root, delete=False) as f:
                    name = f.name
                    json.dump({"retryAfterUntil": self._retry_until}, f)
                os.replace(name, target)
                name = None
            finally:
                if name:
                    os.unlink(name)

    def acquire(self):
        if self._owner is not None:
            return True
        try:
            with claude_statusline._guard_directories(self.root, create=True):
                context = claude_statusline._open_checked(self.root / "claude-usage-poll.lock", write=True, create=True)
                stream = context.__enter__()
                try:
                    if os.name == "nt":
                        import msvcrt
                        stream.seek(0)
                        msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BaseException:
                    context.__exit__(None, None, None)
                    raise
                self._owner = stream
                self._owner_context = context
                # A previous owner may have persisted a newer Retry-After after we were built.
                self._retry_until = max(self._retry_until, self._read_cooldown())
                return True
        except OSError:
            return False

    def _observe(self, credentials):
        fingerprint = _fingerprint(credentials) if credentials else None
        if fingerprint != self._fingerprint:
            self._fingerprint, self._binding, self._sample = fingerprint, None, {}
            self._rejected = None
        return fingerprint

    def read(self, identity):
        if not live_usage_allowed():
            return {}
        credentials = _credentials()
        with self._lock:
            fingerprint = self._observe(credentials)
            if (not fingerprint or not identity or not credentials or credentials[1] <= (self.clock() + 60) * 1000
                    or "user:profile" not in credentials[2] or self._rejected == fingerprint
                    or self._binding != (fingerprint, identity.generation)
                    or not live_usage_allowed()):
                self._sample = {}
                return {}
            current = claude_identity.current_identity()
            if (not current or current.generation != identity.generation
                    or any(window["receivedAt"] < current.changed_at for window in self._sample.values())):
                self._sample = {}
                self._binding = None
                return {}
            return {key: dict(value) for key, value in self._sample.items()}

    def poll(self):
        if not live_usage_allowed() or self._owner is None or not self._poll_lock.acquire(blocking=False):
            return
        try:
            self._poll()
        finally:
            self._poll_lock.release()

    def _poll(self):
        now = self.clock()
        with self._lock:
            revocation = self._revocation
        if not live_usage_allowed():
            with self._lock:
                self._sample, self._binding = {}, None
            return
        identity = claude_identity.current_identity()
        if not identity:
            with self._lock:
                self._sample, self._binding = {}, None
            return
        code_usage = claude_identity.read_claude_code_usage(identity, now=now)
        if fresh(code_usage, 180, now):
            with self._lock:
                if now >= max(self._next, self._retry_until):
                    self._next = now + random.uniform(165, 195)
            return
        if not live_usage_allowed():
            return
        credentials = _credentials()
        with self._lock:
            fingerprint = self._observe(credentials)
            if (not credentials or credentials[1] <= (now + 60) * 1000 or "user:profile" not in credentials[2]
                    or fingerprint == self._rejected):
                self._sample, self._binding = {}, None
                return
            if now < max(self._next, self._retry_until):
                return
        started = self.clock()
        try:
            with self._lock:
                bound = self._binding == (fingerprint, identity.generation)
            if not bound:
                if not live_usage_allowed():
                    return
                status, headers, body = self.request("/api/oauth/profile", credentials[0])
                if not live_usage_allowed() or revocation != self._revocation:
                    return
                if status != 200:
                    self._error(status, headers, fingerprint)
                    return
                account = body.get("account") if isinstance(body, dict) else None
                org = body.get("organization") if isinstance(body, dict) else None
                if (not isinstance(account, dict) or not isinstance(org, dict)
                        or (account.get("uuid"), org.get("uuid")) != (identity.account_uuid, identity.org_id)):
                    with self._lock:
                        self._binding, self._sample = None, {}
                    self._error(0, {}, fingerprint)
                    return
                latest = claude_identity.current_identity()
                latest_credentials = _credentials() if live_usage_allowed() else None
                if (not latest or latest.generation != identity.generation or latest.changed_at > started
                        or not latest_credentials or _fingerprint(latest_credentials) != fingerprint
                        or latest_credentials[1] <= (self.clock() + 60) * 1000):
                    with self._lock:
                        self._binding, self._sample = None, {}
                    return
                with self._lock:
                    self._binding, self._sample = (fingerprint, identity.generation), {}
                if self._stop.is_set():
                    return
            if not live_usage_allowed() or revocation != self._revocation:
                return
            status, headers, body = self.request("/api/oauth/usage", credentials[0])
            if not live_usage_allowed() or revocation != self._revocation:
                return
            if status != 200:
                self._error(status, headers, fingerprint)
                return
            received = self.clock()
            parsed = claude_statusline.parse_live_usage(body, observed_at=received)
            sample = {name: {k: window[k] for k in ("used_percentage", "resets_at", "receivedAt")}
                      for name, window in parsed.items() if window.get("crossCheckPassed")}
            if not sample:
                self._error(0, {}, fingerprint)
                return
            latest = claude_identity.current_identity()
            latest_credentials = _credentials() if live_usage_allowed() else None
            with self._lock:
                if (not latest or latest.generation != identity.generation or latest.changed_at > started
                        or not latest_credentials or _fingerprint(latest_credentials) != fingerprint
                        or latest_credentials[1] <= (received + 60) * 1000
                        or not live_usage_allowed() or revocation != self._revocation):
                    self._binding, self._sample = None, {}
                    return
                self._binding = (fingerprint, identity.generation)
                self._sample = sample
                self._failures = 0
                self._next = received + random.uniform(165, 195)
        except Exception:
            self._error(0, {}, fingerprint)

    def _error(self, status, headers, fingerprint):
        with self._lock:
            if status in (401, 403):
                self._rejected, self._binding, self._sample = fingerprint, None, {}
            self._failures += 1
            self._next = self.clock() + min(300, 5 * 2 ** min(self._failures - 1, 6))
            if status == 429:
                self._retry_until = self.clock() + _retry_after(headers.get("Retry-After"), self.clock())
                try:
                    self._save_cooldown()
                except (OSError, ValueError):
                    pass

    def start(self):
        if not live_usage_allowed():
            return
        if self._thread and self._thread.is_alive():
            return
        if not self.acquire():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="claude-usage", daemon=True)
        self._thread.start()

    def _run(self):
        if self._stop.wait(random.uniform(0, 5)):
            return
        while not self._stop.is_set():
            self.poll()
            self._stop.wait(1)

    def stop(self):
        self._stop.set()
        _revoke_transports()
        with self._lock:
            self._revocation += 1
            self._binding, self._sample = None, {}
        if self._thread and self._thread is not threading.current_thread():
            self._thread.join(timeout=21)
        # Keep the OS lock until process exit; no second in-process server may poll.


REFRESHER = ClaudeUsageRefresher()


def fresh(sample, max_age, now=None):
    """True when a bound live sample has a window young enough to count as current evidence."""
    now = time.time() if now is None else now
    return any(isinstance(window, dict) and type(window.get("receivedAt")) in (int, float)
               and 0 <= now - window["receivedAt"] <= max_age for window in (sample or {}).values())


def read(identity):
    return REFRESHER.read(identity)
