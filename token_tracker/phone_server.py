"""Paired, loopback-only HTTP service for the Token Tracker phone view.

Start explicitly with ``python -m token_tracker.phone_server``. This service
does not expose the desktop server's mutation or diagnostics routes.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import hashlib
import hmac
import html
import ipaddress
import json
import logging
import math
import os
import re
import secrets
import tempfile
import threading
import time
from collections import defaultdict, deque
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlsplit
from urllib.request import Request, urlopen

from . import product
from .phone_push import PhonePushService


DESKTOP_ORIGIN = "http://127.0.0.1:8765"
DEFAULT_ORIGIN = "http://127.0.0.1:8767"
COOKIE_NAME = "tt_phone_session"
BODY_LIMIT = 16 * 1024
SESSION_TTL = 30 * 24 * 60 * 60
PAIR_WINDOW = 5 * 60
PAIR_FAILURES = 8
PAIR_CLIENTS = 256
MAX_SESSIONS = 32
FORCED_REFRESH_INTERVAL = 10.0
STATIC_FILES = {
    "phone.html", "phone.css", "phone.js", "phone-sw.js", "manifest.webmanifest",
    "app.js", "runway.css", "phone-icon.svg", "phone-icon-192.png", "phone-icon-512.png",
    "apple-touch-icon.png",
}
ALLOWED_PUSH_HOSTS = {
    "web.push.apple.com", "fcm.googleapis.com", "updates.push.services.mozilla.com",
    "push.services.mozilla.com",
}
MOZILLA_REGION = re.compile(r"(?:us|eu|ap|sa|ca|au|cn)-(?:[a-z]+-)?[1-9][0-9]?\.push\.services\.mozilla\.com\Z", re.I)
_LOG = logging.getLogger("token_tracker.phone")


def _state_path() -> Path:
    return product.phone_state_path()


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _decode_b64url(value: object, expected: int | None = None) -> bytes | None:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,512}", value):
        return None
    try:
        decoded = base64.urlsafe_b64decode(value + "=" * ((4 - len(value) % 4) % 4))
    except (ValueError, binascii.Error):
        return None
    return decoded if expected is None or len(decoded) == expected else None


def _new_vapid() -> dict[str, str] | None:
    try:
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import ec

        private = ec.generate_private_key(ec.SECP256R1())
        private_pem = private.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ).decode("ascii")
        public = private.public_key().public_bytes(
            serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
        )
        return {"private": private_pem, "public": _b64url(public)}
    except ImportError:
        return None


class PhoneState:
    """Small synchronized persistent store; secrets never leave this process."""

    def __init__(self, path: Path):
        self.path = path
        self.lock = threading.RLock()
        self.data = self._load()
        changed = False
        if not self.data.get("pairingCode"):
            self.data["pairingCode"] = secrets.token_urlsafe(18)
            changed = True
        if not isinstance(self.data.get("vapid"), dict):
            self.data["vapid"] = _new_vapid()
            changed = True
        if not isinstance(self.data.get("subscriptions"), dict):
            self.data["subscriptions"] = {}
            changed = True
        if not isinstance(self.data.get("sessions"), dict):
            self.data["sessions"] = {}
            changed = True
        if changed:
            self._write_locked()

    def _load(self) -> dict:
        try:
            with self.path.open("r", encoding="utf-8") as stream:
                data = json.load(stream)
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _write_locked(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=".phone-state-", suffix=".tmp", dir=self.path.parent)
        try:
            if os.name != "nt":
                os.chmod(temporary, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(self.data, stream, separators=(",", ":"))
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
        finally:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass

    def pairing_code(self) -> str:
        with self.lock:
            return self.data["pairingCode"]

    def session(self, token: str) -> tuple[str, dict] | None:
        token_hash = hashlib.sha256(token.encode("ascii", "ignore")).hexdigest()
        now = time.time()
        with self.lock:
            record = self.data["sessions"].get(token_hash)
            try:
                valid_until = float(record.get("expires", 0)) if isinstance(record, dict) else 0
            except (TypeError, ValueError):
                valid_until = 0
            if not isinstance(record, dict) or valid_until <= now:
                if record:
                    self.data["sessions"].pop(token_hash, None)
                    self._write_locked()
                return None
            return token_hash, record

    def create_session(self) -> str:
        token = secrets.token_urlsafe(32)
        token_hash = hashlib.sha256(token.encode("ascii")).hexdigest()
        now = time.time()
        with self.lock:
            sessions = self.data["sessions"]
            for key, record in list(sessions.items()):
                if not isinstance(record, dict) or float(record.get("expires", 0)) <= now:
                    sessions.pop(key, None)
            while len(sessions) >= MAX_SESSIONS:
                oldest = min(sessions, key=lambda key: sessions[key].get("created", 0))
                sessions.pop(oldest, None)
            sessions[token_hash] = {"created": now, "expires": now + SESSION_TTL, "subscription": None}
            self._write_locked()
        return token

    def public_config(self) -> dict:
        with self.lock:
            vapid = self.data.get("vapid") or {}
            public = vapid.get("public")
            ready = bool(public and vapid.get("private") and _configured_vapid_subject() and _pywebpush_available())
            return {"pushReady": ready, "publicKey": public if ready else None}

    def push_private_key(self) -> str | None:
        with self.lock:
            return (self.data.get("vapid") or {}).get("private")

    def save_subscription(self, session_hash: str, subscription: dict) -> None:
        endpoint = subscription["endpoint"]
        key = hashlib.sha256(endpoint.encode("utf-8")).hexdigest()
        with self.lock:
            old = self.data["sessions"].get(session_hash, {}).get("subscription")
            if old and old != key:
                self.data["subscriptions"].pop(old, None)
            self.data["subscriptions"][key] = subscription
            self.data["sessions"][session_hash]["subscription"] = key
            self._write_locked()

    def remove_subscription(self, session_hash: str, supplied: dict) -> None:
        key = hashlib.sha256(supplied["endpoint"].encode("utf-8")).hexdigest()
        with self.lock:
            record = self.data["sessions"].get(session_hash)
            current = record.get("subscription") if isinstance(record, dict) else None
            if current and current == key:
                self.data["subscriptions"].pop(current, None)
                record["subscription"] = None
                self._write_locked()

    def subscriptions(self) -> list[dict]:
        with self.lock:
            return [dict(item) for item in self.data["subscriptions"].values() if isinstance(item, dict)][:20]

    def remove_endpoint(self, endpoint: str) -> None:
        key = hashlib.sha256(endpoint.encode("utf-8")).hexdigest()
        with self.lock:
            if self.data["subscriptions"].pop(key, None) is None:
                return
            for session in self.data["sessions"].values():
                if isinstance(session, dict) and session.get("subscription") == key:
                    session["subscription"] = None
            self._write_locked()


def _pywebpush_available() -> bool:
    try:
        import pywebpush  # noqa: F401
        return True
    except ImportError:
        return False


def _configured_vapid_subject() -> str:
    subject = os.environ.get("TOKEN_TRACKER_VAPID_SUBJECT", "").strip()
    if subject.startswith("mailto:"):
        address = subject[7:]
        return subject if re.fullmatch(r"[^\s@<>]+@[^\s@<>]+\.[^\s@<>]+", address) else ""
    if subject.startswith("https://"):
        parsed = urlsplit(subject)
        if parsed.hostname and not parsed.username and not parsed.password:
            return subject
    return ""


def valid_subscription(value: object) -> dict | None:
    if not isinstance(value, dict) or not {"endpoint", "keys"}.issubset(value) or set(value) - {"endpoint", "keys", "expirationTime"}:
        return None
    endpoint, keys = value.get("endpoint"), value.get("keys")
    if not isinstance(endpoint, str) or len(endpoint) > 4096 or not isinstance(keys, dict) or set(keys) != {"p256dh", "auth"}:
        return None
    expiration = value.get("expirationTime")
    if expiration is not None and (isinstance(expiration, bool) or not isinstance(expiration, (int, float)) or not math.isfinite(expiration)):
        return None
    try:
        parsed = urlsplit(endpoint)
        host = (parsed.hostname or "").lower().rstrip(".")
        if (parsed.scheme != "https" or parsed.username or parsed.password or parsed.port not in (None, 443)
                or parsed.fragment or not parsed.path.startswith("/") or not host):
            return None
        address = ipaddress.ip_address(host.strip("[]")) if ":" in host or re.fullmatch(r"[0-9.]+", host) else None
        if address is not None and (not address.is_global):
            return None
        if host not in ALLOWED_PUSH_HOSTS and not MOZILLA_REGION.fullmatch(host):
            return None
    except (ValueError, UnicodeError):
        return None
    if _decode_b64url(keys.get("p256dh"), 65) is None or _decode_b64url(keys.get("auth"), 16) is None:
        return None
    if not _valid_p256_point(_decode_b64url(keys["p256dh"])):
        return None
    normalized = {"endpoint": endpoint, "keys": {"p256dh": keys["p256dh"], "auth": keys["auth"]}}
    if expiration is not None:
        normalized["expirationTime"] = expiration
    return normalized


def _valid_p256_point(encoded: bytes | None) -> bool:
    if not encoded or len(encoded) != 65 or encoded[0] != 4:
        return False
    prime = 0xFFFFFFFF00000001000000000000000000000000FFFFFFFFFFFFFFFFFFFFFFFF
    coefficient_b = 0x5AC635D8AA3A93E7B3EBBD55769886BC651D06B0CC53B0F63BCE3C3E27D2604B
    x = int.from_bytes(encoded[1:33], "big")
    y = int.from_bytes(encoded[33:], "big")
    return x < prime and y < prime and (y * y - (x * x * x - 3 * x + coefficient_b)) % prime == 0


class PairingLimiter:
    def __init__(self):
        self.lock = threading.Lock()
        self.failures: dict[str, deque[float]] = defaultdict(deque)

    def allowed(self, client: str) -> bool:
        now = time.monotonic()
        with self.lock:
            events = self.failures.get(client)
            if events is None and len(self.failures) >= PAIR_CLIENTS:
                self.failures.pop(next(iter(self.failures)))
                events = deque()
                self.failures[client] = events
            if events is None:
                events = self.failures[client]
            while events and now - events[0] > PAIR_WINDOW:
                events.popleft()
            return len(events) < PAIR_FAILURES

    def failed(self, client: str) -> None:
        with self.lock:
            self.failures[client].append(time.monotonic())


class PhoneRefreshLimiter:
    """Allow one explicit phone snapshot refresh within each 10-second window."""

    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.lock = threading.Lock()
        self.last_refresh = float("-inf")

    def allow(self) -> bool:
        now = self.clock()
        with self.lock:
            if now - self.last_refresh < FORCED_REFRESH_INTERVAL:
                return False
            self.last_refresh = now
            return True


def _origin_parts(origin: str):
    parsed = urlsplit(origin)
    if (parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password
            or parsed.path not in {"", "/"} or parsed.query or parsed.fragment):
        raise ValueError("origin must be an exact HTTP(S) origin")
    try:
        parsed.port
    except ValueError as exc:
        raise ValueError("origin has an invalid port") from exc
    return parsed


def create_server(host: str = "127.0.0.1", port: int = 8767, *, origin: str = DEFAULT_ORIGIN,
                  state_path: Path | None = None, desktop_origin: str = DESKTOP_ORIGIN,
                  push_sender=None, start_monitor: bool = True) -> ThreadingHTTPServer:
    bind_ip = ipaddress.ip_address(host)
    if not bind_ip.is_loopback:
        raise ValueError("phone service must bind to a loopback address")
    if desktop_origin.rstrip("/") != DESKTOP_ORIGIN:
        raise ValueError("desktop telemetry origin is fixed to the local Token Tracker service")
    allowed_origin = _origin_parts(origin)
    expected_host = allowed_origin.netloc.lower()
    store = PhoneState(state_path or _state_path())
    limiter = PairingLimiter()
    refresh_limiter = PhoneRefreshLimiter()
    server = ThreadingHTTPServer((host, port), _handler_type(store, limiter, allowed_origin, expected_host, desktop_origin))
    server.daemon_threads = True
    server.phone_store = store
    server.phone_refresh_limiter = refresh_limiter
    server.phone_monitor = None
    if start_monitor:
        server.phone_monitor = PhonePushService(store, desktop_origin=desktop_origin, sender=push_sender,
                                                refresh_gate=refresh_limiter.allow)
        server.phone_monitor.start()
    return server


def _handler_type(store: PhoneState, limiter: PairingLimiter, origin, expected_host: str, desktop_origin: str):
    class Handler(BaseHTTPRequestHandler):
        server_version = "TokenTrackerPhone/1"

        def log_message(self, fmt, *args):
            # Request paths, cookies, endpoints, and body content are private.
            return

        def _send(self, status: int, body: bytes, content_type: str, extra: tuple[tuple[str, str], ...] = ()) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Length", str(len(body)))
            for key, value in extra:
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(body)

        def _json(self, status: int, value: dict, extra=()):
            self._send(status, json.dumps(value, separators=(",", ":")).encode(), "application/json; charset=utf-8", tuple(extra))

        def _host_ok(self) -> bool:
            return self.headers.get("Host", "").lower() == expected_host

        def _peer_loopback(self) -> bool:
            try:
                return ipaddress.ip_address(self.client_address[0]).is_loopback
            except ValueError:
                return False

        def _secure_cookie(self) -> bool:
            # Forwarded headers are ignored. The configured HTTPS origin plus a direct loopback peer
            # identifies the explicitly trusted local TLS proxy deployment.
            return origin.scheme == "https" and self._peer_loopback()

        def _same_origin(self) -> bool:
            return self.headers.get("Origin", "") == origin.geturl().rstrip("/") and self._host_ok() and self._peer_loopback()

        def _session(self):
            try:
                cookie = SimpleCookie(self.headers.get("Cookie", ""))
                token = cookie.get(COOKIE_NAME).value if cookie.get(COOKIE_NAME) else ""
                if not re.fullmatch(r"[A-Za-z0-9_-]{40,64}", token):
                    return None
                return store.session(token)
            except (ValueError, KeyError):
                return None

        def _body(self) -> bytes | None:
            try:
                length = int(self.headers.get("Content-Length", "-1"))
            except ValueError:
                return None
            if length < 0 or length > BODY_LIMIT:
                return None
            return self.rfile.read(length)

        def do_GET(self):
            if not self._host_ok() or not self._peer_loopback():
                return self._json(403, {"error": "forbidden"})
            path = urlsplit(self.path).path
            if path == "/phone.html" or path == "/":
                if not self._session():
                    return self._send(200, _pairing_page().encode(), "text/html; charset=utf-8")
                return self._static("phone.html")
            if path == "/api/phone/config":
                if not self._session():
                    return self._json(401, {"error": "pairing required"})
                return self._json(200, store.public_config())
            if path in {"/api/stats", "/api/live"}:
                if not self._session():
                    return self._json(401, {"error": "pairing required"})
                force = False
                if path == "/api/stats":
                    try:
                        query = parse_qs(urlsplit(self.path).query, keep_blank_values=True, max_num_fields=8)
                    except ValueError:
                        query = {}
                    # Filters and unknown keys are ignored. Only the exact refresh flag is forwarded.
                    if query == {"refresh": ["1"]}:
                        force = self.server.phone_refresh_limiter.allow()
                return self._proxy(path, force_refresh=force)
            name = path.removeprefix("/")
            if name in STATIC_FILES and self._session():
                return self._static(name)
            return self._json(404, {"error": "not found"})

        def do_POST(self):
            if not self._host_ok() or not self._peer_loopback():
                return self._json(403, {"error": "forbidden"})
            path = urlsplit(self.path).path
            if path == "/api/phone/pair":
                return self._pair()
            if not self._same_origin() or self.headers.get_content_type() != "application/json":
                return self._json(403, {"error": "same-origin JSON required"})
            session = self._session()
            if not session:
                return self._json(401, {"error": "pairing required"})
            if path != "/api/phone/subscription":
                return self._json(404, {"error": "not found"})
            raw = self._body()
            if raw is None:
                return self._json(413, {"error": "invalid request size"})
            try:
                body = json.loads(raw)
            except (ValueError, UnicodeDecodeError):
                return self._json(400, {"error": "invalid request"})
            subscription = valid_subscription(body.get("subscription") if isinstance(body, dict) and set(body) == {"subscription"} else None)
            if subscription is None:
                return self._json(400, {"error": "invalid subscription"})
            store.save_subscription(session[0], subscription)
            return self._json(200, {"ok": True})

        def do_DELETE(self):
            if not self._host_ok() or not self._peer_loopback() or not self._same_origin() or self.headers.get_content_type() != "application/json":
                return self._json(403, {"error": "same-origin JSON required"})
            session = self._session()
            if not session:
                return self._json(401, {"error": "pairing required"})
            if urlsplit(self.path).path != "/api/phone/subscription":
                return self._json(404, {"error": "not found"})
            raw = self._body()
            if raw is None:
                return self._json(413, {"error": "invalid request size"})
            try:
                body = json.loads(raw)
            except (ValueError, UnicodeDecodeError):
                return self._json(400, {"error": "invalid request"})
            supplied = valid_subscription(body.get("subscription") if isinstance(body, dict) and set(body) == {"subscription"} else None)
            if supplied is None:
                return self._json(400, {"error": "invalid subscription"})
            store.remove_subscription(session[0], supplied)
            return self._json(200, {"ok": True})

        def _pair(self):
            if not self._same_origin():
                return self._json(403, {"error": "same-origin pairing required"})
            content_type = self.headers.get_content_type()
            if content_type not in {"application/json", "application/x-www-form-urlencoded"}:
                return self._json(415, {"error": "pairing form or JSON required"})
            raw = self._body()
            if raw is None:
                return self._json(413, {"error": "invalid request size"})
            try:
                if content_type == "application/json":
                    body = json.loads(raw)
                    code = body.get("code") if isinstance(body, dict) and set(body) == {"code"} else None
                else:
                    body = parse_qs(raw.decode("utf-8"), strict_parsing=True, max_num_fields=4)
                    code = body.get("code", [None])[0] if set(body) == {"code"} else None
            except (ValueError, UnicodeDecodeError):
                code = None
            if not isinstance(code, str) or len(code) > 128:
                code = ""
            client = self.client_address[0]
            if not limiter.allowed(client):
                return self._json(429, {"error": "pairing temporarily limited"})
            if not hmac.compare_digest(code.encode("utf-8"), store.pairing_code().encode("utf-8")):
                limiter.failed(client)
                return self._pair_failure(content_type)
            token = store.create_session()
            cookie = f"{COOKIE_NAME}={token}; Path=/; HttpOnly; SameSite=Strict; Max-Age={SESSION_TTL}" + ("; Secure" if self._secure_cookie() else "")
            if content_type == "application/json":
                return self._json(200, {"paired": True}, (("Set-Cookie", cookie),))
            self.send_response(303)
            self.send_header("Location", "/phone.html")
            self.send_header("Set-Cookie", cookie)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", "0")
            self.end_headers()

        def _pair_failure(self, content_type):
            if content_type == "application/json":
                return self._json(401, {"error": "pairing code rejected"})
            page = _pairing_page("Pairing code was rejected.")
            return self._send(401, page.encode(), "text/html; charset=utf-8")

        def _static(self, name: str):
            if name not in STATIC_FILES:
                return self._json(404, {"error": "not found"})
            path = Path(__file__).with_name("static") / name
            try:
                payload = path.read_bytes()
            except OSError:
                return self._json(404, {"error": "not found"})
            kinds = {".html": "text/html; charset=utf-8", ".css": "text/css; charset=utf-8", ".js": "text/javascript; charset=utf-8", ".webmanifest": "application/manifest+json", ".svg": "image/svg+xml", ".png": "image/png"}
            return self._send(200, payload, kinds.get(path.suffix, "application/octet-stream"))

        def _proxy(self, path: str, *, force_refresh: bool = False):
            # Only a rate-limited exact refresh flag can bypass the desktop cache.
            try:
                target = desktop_origin.rstrip("/") + path
                if path == "/api/stats" and force_refresh:
                    target += "?refresh=1"
                timeout = 20 if path == "/api/stats" else 5
                request = Request(target, headers={"Accept": "application/json"}, method="GET")
                with urlopen(request, timeout=timeout) as response:
                    payload = response.read(4 * 1024 * 1024 + 1)
                    if len(payload) > 4 * 1024 * 1024:
                        raise ValueError("response too large")
                    parsed = json.loads(payload)
                    if not isinstance(parsed, dict):
                        raise ValueError("invalid response")
                    return self._send(200, payload, "application/json; charset=utf-8")
            except (HTTPError, URLError, TimeoutError, OSError, ValueError, json.JSONDecodeError):
                return self._json(503, {"error": "desktop telemetry unavailable"})

    return Handler


def _pairing_page(message: str = "") -> str:
    safe = html.escape(message, quote=True)
    return ("<!doctype html><html lang='en'><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>"
            "<title>Pair Token Tracker</title><style>body{font:16px system-ui;background:#080808;color:#eee;max-width:26rem;margin:15vh auto;padding:1.5rem}input,button{font:inherit;padding:.8rem;border-radius:.5rem}input{width:90%;margin:.5rem 0 1rem;background:#171717;color:#fff;border:1px solid #555}button{background:#eee;color:#111;border:0}</style>"
            "<h1>Pair this phone</h1><p>Enter the pairing code shown by the Token Tracker service.</p>"
            f"<p role='alert'>{safe}</p><form method='post' action='/api/phone/pair'><label>Pairing code<input name='code' autocomplete='one-time-code' required maxlength='128'></label><button>Pair phone</button></form></html>")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Run the private Token Tracker phone service")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8767)
    parser.add_argument("--origin", default=DEFAULT_ORIGIN, help="exact browser origin allowed through a trusted loopback proxy")
    parser.add_argument("--show-pairing-code", action="store_true", help="print the pairing code to this terminal")
    args = parser.parse_args(argv)
    server = create_server(args.host, args.port, origin=args.origin)
    if args.show_pairing_code:
        print(f"Phone pairing code: {server.phone_store.pairing_code()}")
    print(f"Token Tracker phone service listening at http://{args.host}:{args.port}")
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        if server.phone_monitor:
            server.phone_monitor.stop()
        server.server_close()


if __name__ == "__main__":
    main()
