from __future__ import annotations

import argparse
import copy
import ipaddress
import json
import logging
import os
import re
import shutil
import subprocess
import threading
import time
import webbrowser
from datetime import datetime, timezone
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer as _BaseThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse, urlsplit

from .providers import build_snapshot, live_state, today_usage, trim_chats


STATIC = Path(__file__).with_name("static")
THREAD_ID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
CHAT_LIMIT = 15
QUEUE_MESSAGE_LIMIT = 2000
QUEUE_TIMEOUT_SECONDS = 15
LIVE_FAILURE_LOG_INTERVAL = 30.0
LIVE_REFRESH_TTL_SECONDS = 1.0
LIVE_MAX_AGE_SECONDS = 4.0
LIVE_COLD_WAIT_SECONDS = 0.35
LIVE_RETRY_COOLDOWN_SECONDS = 1.0


def _codex_policy():
    """Read the immutable startup policy, treating malformed state as unavailable."""
    try:
        from .provider_policy import POLICIES, policy_for
        return policy_for("Codex", POLICIES)
    except Exception:
        return None


def _codex_allows(action: str) -> bool:
    policy = _codex_policy()
    try:
        return policy is not None and policy.allows(action) is True
    except Exception:
        return False


def _codex_queue_allowed() -> bool:
    """Queueing arbitrary text requires explicit provider SEND consent."""
    policy = _codex_policy()
    try:
        return (policy is not None and policy.allows("collect") is True
                and policy.allows("show") is True
                and policy.send is True and policy.send_origin == "provider_env")
    except Exception:
        return False


PRODUCT = "Branchglance"
BUILD = os.environ.get("BRANCHGLANCE_BUILD", "dev")


def _collected_codex_thread_ids() -> frozenset[str]:
    """Codex thread ids from the cached snapshot and live roster; Claude ids never qualify."""
    ids: set[str] = set()
    try:
        snapshot = CACHE.value
        for provider in (snapshot or {}).get("providers", []):
            if isinstance(provider, dict) and provider.get("name") == "Codex":
                ids.update(chat["id"] for chat in provider.get("chats", [])
                           if isinstance(chat, dict) and isinstance(chat.get("id"), str))
        live = LIVE_CACHE.value
        for chat in (live or {}).get("chats", []):
            if (isinstance(chat, dict) and isinstance(chat.get("id"), str)
                    and chat.get("key") == f"Codex:{chat['id']}"
                    and chat.get("provider", "Codex") == "Codex"):
                ids.add(chat["id"])
    except Exception:
        return frozenset()
    return frozenset(ids)


def _is_collected_codex_thread(thread: str) -> bool:
    return thread in _collected_codex_thread_ids()


def _identity_payload() -> dict:
    try:
        from .limit_reset import _runtime_dir
        state_root = str(_runtime_dir())
    except Exception:
        state_root = None
    try:
        from .provider_policy import public_policies
        policies = public_policies()
        summary = {"generation": policies["generation"],
                   "providers": {row["name"]: {"collect": row["collect"], "show": row["show"],
                                                "send": row["send"], "sendOrigin": row["sendOrigin"]}
                                 for row in policies["providers"]}}
    except Exception:
        summary = None
    return {"product": PRODUCT, "build": BUILD, "pid": os.getpid(), "stateRoot": state_root,
            "providerPolicies": summary}


def _without_codex_thread_ids(value):
    """Remove selected thread identifiers recursively from hidden-provider responses."""
    if isinstance(value, dict):
        return {key: _without_codex_thread_ids(item) for key, item in value.items()
                if str(key).casefold() not in {"threadid", "threadids", "selectedids"}}
    if isinstance(value, list):
        return [_without_codex_thread_ids(item) for item in value]
    return value


class ThreadingHTTPServer(_BaseThreadingHTTPServer):
    """Application server restricted to explicit IPv4 loopback bindings."""

    def __init__(self, server_address, RequestHandlerClass, bind_and_activate=True):
        if not isinstance(server_address, tuple) or len(server_address) != 2:
            raise ValueError("Token Tracker server address must be an IPv4 host and port pair")
        host, port = server_address
        if not isinstance(host, str):
            raise ValueError("Token Tracker server host must be 127.0.0.1 or localhost")
        if isinstance(port, bool) or not isinstance(port, int) or not 0 <= port <= 65535:
            raise ValueError("Token Tracker server port must be an integer from 0 to 65535")
        if host.lower() == "localhost":
            host = "127.0.0.1"
        try:
            address = ipaddress.ip_address(host)
        except ValueError as exc:
            raise ValueError("Token Tracker server accepts only literal 127.0.0.1 or localhost") from exc
        if isinstance(address, ipaddress.IPv6Address):
            raise ValueError("IPv6 is not supported; bind Token Tracker to a loopback IPv4 address")
        if address != ipaddress.IPv4Address("127.0.0.1"):
            raise ValueError("Only 127.0.0.1 or localhost are supported for the Token Tracker server")
        super().__init__((str(address), port), RequestHandlerClass, bind_and_activate=bind_and_activate)


_LIVE_FAILURE_LOG_LOCK = threading.Lock()
_LIVE_FAILURE_LOGGED_AT: dict[str, float] = {}


def _log_live_failure(exc: Exception) -> None:
    """Log each recurring live telemetry exception class at most every 30s."""
    error_type = type(exc).__name__
    now = time.monotonic()
    with _LIVE_FAILURE_LOG_LOCK:
        if now - _LIVE_FAILURE_LOGGED_AT.get(error_type, float("-inf")) < LIVE_FAILURE_LOG_INTERVAL:
            return
        _LIVE_FAILURE_LOGGED_AT[error_type] = now
    logging.getLogger(__name__).warning("Live telemetry failed (%s)", error_type)


class LiveCache:
    """Keep slow live-provider work out of HTTP request threads."""

    def __init__(self) -> None:
        self.condition = threading.Condition()
        self.value = None
        self.completed_at = 0.0
        self.building = False
        self.retry_after = 0.0

    def _refresh(self) -> None:
        try:
            value = live_state()
        except Exception as exc:
            _log_live_failure(exc)
            with self.condition:
                self.building = False
                self.retry_after = time.monotonic() + LIVE_RETRY_COOLDOWN_SECONDS
                self.condition.notify_all()
            return

        with self.condition:
            self.value = value
            self.completed_at = time.monotonic()
            self.building = False
            self.retry_after = 0.0
            self.condition.notify_all()

    def _start_refresh_locked(self, now: float) -> None:
        if self.building or now < self.retry_after:
            return
        self.building = True
        worker = threading.Thread(target=self._refresh, name="token-tracker-live", daemon=True)
        try:
            worker.start()
        except Exception as exc:
            self.building = False
            self.retry_after = time.monotonic() + LIVE_RETRY_COOLDOWN_SECONDS
            self.condition.notify_all()
            _log_live_failure(exc)

    def get(self):
        with self.condition:
            now = time.monotonic()
            age = now - self.completed_at if self.value is not None else float("inf")
            if self.value is not None and age <= LIVE_MAX_AGE_SECONDS:
                if age >= LIVE_REFRESH_TTL_SECONDS:
                    self._start_refresh_locked(now)
                return self.value

            # A cold caller gets only a short chance to receive the first result.
            self._start_refresh_locked(now)
            deadline = now + LIVE_COLD_WAIT_SECONDS
            while self.value is None and self.building:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self.condition.wait(remaining)

            now = time.monotonic()
            age = now - self.completed_at if self.value is not None else float("inf")
            if self.value is not None and age <= LIVE_MAX_AGE_SECONDS:
                return self.value
            return None


LIVE_CACHE = LiveCache()


class SnapshotCache:
    RETRY_COOLDOWN_SECONDS = 5.0

    def __init__(self, ttl: float = 120.0) -> None:
        self.ttl = ttl
        self.lock = threading.Lock()
        self.condition = threading.Condition(self.lock)
        self._value = None
        self._at = 0.0
        self.generation = 0
        self.built_at: str | None = None
        self.building = False
        self.retry_after = 0.0
        self.last_failure_at: str | None = None
        self.last_failure_category: str | None = None
        self._last_build_error: Exception | None = None
        self._attempt = 0
        self._completed_attempt = 0

    @property
    def value(self):
        """Compatibility inspection hook; callers receive a defensive copy."""
        with self.condition:
            value = self._value
        return copy.deepcopy(value)

    @value.setter
    def value(self, value) -> None:
        with self.condition:
            self._value = copy.deepcopy(value)

    @property
    def at(self) -> float:
        with self.condition:
            return self._at

    @at.setter
    def at(self, value: float) -> None:
        with self.condition:
            self._at = float(value)

    @staticmethod
    def _failure_category(exc: Exception, *, worker_start: bool = False) -> str:
        if worker_start:
            return "worker_start_failed"
        if isinstance(exc, TimeoutError):
            return "timeout"
        if isinstance(exc, OSError):
            return "io_error"
        if isinstance(exc, (TypeError, ValueError)):
            return "invalid_snapshot"
        return "build_failed"

    @staticmethod
    def _utc_now() -> str:
        return datetime.now(timezone.utc).isoformat()

    def _record_failure_locked(self, exc: Exception, *, worker_start: bool = False) -> None:
        self.building = False
        self.retry_after = time.monotonic() + self.RETRY_COOLDOWN_SECONDS
        self.last_failure_at = self._utc_now()
        self.last_failure_category = self._failure_category(exc, worker_start=worker_start)
        self._last_build_error = exc
        self._completed_attempt = self._attempt

    def _metadata_locked(self, now: float) -> dict:
        age = max(0.0, now - self._at) if self._value is not None else None
        return {
            "generation": self.generation,
            "builtAt": self.built_at,
            "ageSeconds": round(age, 3) if age is not None else None,
            "building": self.building,
            "stale": (self._value is None or age is None or age > self.ttl
                      or self.last_failure_category is not None),
            "lastFailureAt": self.last_failure_at,
            "lastFailureCategory": self.last_failure_category,
        }

    @staticmethod
    def _egress(value, metadata: dict):
        if value is None:
            return None
        result = copy.deepcopy(value)
        result["snapshotCache"] = metadata
        return result

    def _selected_locked(self):
        return self._value, self._metadata_locked(time.monotonic())

    def _build(self, *, background: bool = False, attempt: int | None = None):
        try:
            value = copy.deepcopy(build_snapshot())
            if not isinstance(value, dict):
                raise TypeError("snapshot builder returned a non-object")
        except Exception as exc:
            with self.condition:
                if attempt is not None:
                    self._attempt = max(self._attempt, attempt)
                self._record_failure_locked(exc)
                self.condition.notify_all()
            if background:
                logging.getLogger(__name__).warning("Snapshot refresh failed (%s)", type(exc).__name__)
                return None
            raise
        with self.condition:
            published_at = time.monotonic()
            self._value = value
            self._at = published_at
            self.generation += 1
            self.built_at = self._utc_now()
            self.building = False
            self.retry_after = 0.0
            self.last_failure_at = None
            self.last_failure_category = None
            self._last_build_error = None
            self._completed_attempt = attempt if attempt is not None else self._attempt
            selected = self._selected_locked()
            self.condition.notify_all()
        return self._egress(*selected)

    def _start_background_locked(self) -> None:
        self._attempt += 1
        attempt = self._attempt
        self.building = True
        try:
            worker = threading.Thread(target=self._build, kwargs={"background": True, "attempt": attempt}, daemon=True)
            worker.start()
        except Exception as exc:
            self._record_failure_locked(exc, worker_start=True)
            self.condition.notify_all()

    def get(self, force: bool = False, background: bool = False):
        with self.condition:
            while True:
                now = time.monotonic()
                stale = self._value is None or now - self._at > self.ttl
                if background and self._value is not None:
                    if (force or stale) and not self.building:
                        if now >= self.retry_after:
                            self._start_background_locked()
                    selected = self._selected_locked()
                    break

                if self.building:
                    request_generation = self.generation
                    request_attempt = self._attempt
                    self.condition.wait()
                    if self.generation != request_generation:
                        selected = self._selected_locked()
                        break
                    if self._completed_attempt >= request_attempt and self._last_build_error is not None:
                        if self._value is not None and not force:
                            selected = self._selected_locked()
                            break
                        error = self._last_build_error
                        raise error
                    continue

                if self._value is not None and not force and (not stale or now < self.retry_after):
                    selected = self._selected_locked()
                    break
                if self._value is None and (not force or background) and now < self.retry_after:
                    if self._last_build_error is not None:
                        raise self._last_build_error
                    raise RuntimeError("snapshot temporarily unavailable")

                self.building = True
                self._attempt += 1
                attempt = self._attempt
                break

        if "selected" in locals():
            return self._egress(*selected)
        # Never hold the cache lock while provider work performs disk or app IO.
        return self._build(attempt=attempt)


CACHE = SnapshotCache()


class Handler(SimpleHTTPRequestHandler):
    reset_service = None

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(STATIC), **kwargs)

    def do_GET(self):
        if not self._valid_local_host():
            self._send_json({"ok": False, "error": "local Host required"}, 403)
            return
        parsed = urlparse(self.path)
        if parsed.path == "/api/provider-policy":
            try:
                from .provider_policy import public_policies
                payload = public_policies()
            except Exception:
                self._send_json({"ok": False, "error": "Provider policy is unavailable."}, 503)
                return
            self._send_json(payload)
            return
        if parsed.path == "/api/identity":
            if not self._open_request_allowed():
                self._send_json({"ok": False, "error": "same-origin request required"}, 403)
                return
            self._send_json(_identity_payload())
            return
        if parsed.path == "/api/stats":
            query = parse_qs(parsed.query)
            force = query.get("refresh", [""])[0] == "1"
            if query.get("background", [""])[0] == "1":
                snapshot = CACHE.get(force=force, background=True)
            else:
                snapshot = CACHE.get(force=force)
            if "limit" in query:
                try:
                    limit = max(1, min(200, int(query["limit"][0])))
                except ValueError:
                    limit = CHAT_LIMIT
                pins = [pin for value in query.get("pins", []) for pin in value.split(",") if pin]
                snapshot = trim_chats(snapshot, limit, pins, query=query.get("q", [""])[0][:200],
                                      provider_filter=query.get("provider", ["all"])[0],
                                      status_filter=query.get("status", ["all"])[0],
                                      sort=query.get("sort", ["latest"])[0],
                                      days={"1d": 1, "7d": 7, "30d": 30}.get(query.get("range", ["7d"])[0], 7))
            self._send_json(snapshot)
            return
        if parsed.path == "/api/live":
            snapshot = LIVE_CACHE.get()
            if snapshot is None:
                self._send_json({"ok": False, "error": "Live telemetry unavailable"}, 503)
            else:
                self._send_json(snapshot)
            return
        if parsed.path == "/api/today":
            self._send_json(today_usage())
            return
        if parsed.path == "/api/open":
            # Jump to one chat in the Codex app. Only a strict thread id is accepted, and only the
            # codex://threads/ deep link is ever opened.
            if not self._open_request_allowed():
                self._send_json({"ok": False, "error": "same-origin request required"}, 403)
                return
            thread = parse_qs(parsed.query).get("thread", [""])[0]
            if not THREAD_ID.fullmatch(thread):
                self._send_json({"ok": False, "error": "invalid thread id"})
                return
            if not _codex_allows("show"):
                self._send_json({"ok": False, "error": "Codex is not enabled."}, 403)
                return
            if not _is_collected_codex_thread(thread):
                self._send_json({"ok": False, "error": "unknown Codex thread"}, 404)
                return
            try:
                os.startfile(f"codex://threads/{thread}")  # type: ignore[attr-defined]
                self._send_json({"ok": True})
            except (AttributeError, OSError) as exc:
                self._send_json({"ok": False, "error": str(exc)[:120]})
            return
        if parsed.path == "/api/avatars":
            # Codex's own subagent avatars, read from the local app install (cached per version).
            from .codex_avatars import codex_avatars
            from .limit_reset import _runtime_dir
            self._send_json({"avatars": codex_avatars(_runtime_dir())})
            return
        if parsed.path == "/api/limit-reset":
            if not _codex_allows("collect"):
                self._send_json({"ok": False, "error": "Codex collection is disabled or unavailable."}, 403)
                return
            status = self.reset_service.status() if self.reset_service else {"enabled": False, "events": []}
            if not _codex_allows("show") and isinstance(status, dict):
                status = dict(status)
                status["providers"] = {}
                status["events"] = []
                status["enabledProviders"] = []
                status = _without_codex_thread_ids(status)
            payload = json.dumps(status).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        if parsed.path == "/api/quota":
            if not self._open_request_allowed():
                self._send_json({"ok": False, "error": "same-origin request required"}, 403)
                return
            if not _codex_allows("collect"):
                self._send_json({"ok": False, "error": "Codex collection is disabled or unavailable."}, 403)
                return
            from .limit_reset import codex_quota_http_view
            payload = codex_quota_http_view()
            if not _codex_allows("show") and isinstance(payload, dict):
                payload = dict(payload)
                payload["providers"] = {}
                payload.pop("events", None)
            self._send_json(payload)
            return
        if parsed.path == "/api/reset-action":
            if not _codex_allows("collect"):
                self._send_json({"ok": False, "error": "Codex collection is disabled or unavailable."}, 403)
                return
            if not _codex_allows("show"):
                self._send_json({"ok": False, "error": "Codex display is disabled."}, 403)
                return
            service = self.reset_service
            if service is None:
                self._send_json({"ok": False, "error": "Reset action service is unavailable."}, 503)
                return
            try:
                action = service.get_reset_action()
            except Exception:
                self._send_json({"ok": False, "error": "Reset action settings could not be read."}, 503)
                return
            try:
                from .reset_delivery import list_candidates
                threads = list_candidates()
                self._send_json({"ok": True, "action": action, "threads": threads, "threadsAvailable": True})
            except Exception:
                self._send_json({"ok": True, "action": action, "threads": [], "threadsAvailable": False,
                                 "error": "Chat list is unavailable. Retry to refresh it."})
            return
        if parsed.path == "/healthz":
            payload = b'{"ok":true}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        if parsed.path == "/":
            self.path = "/index.html"
        super().do_GET()

    def do_POST(self):
        if not self._valid_local_host():
            self._send_json({"ok": False, "error": "local Host required"}, 403)
            return
        parsed = urlparse(self.path)
        if parsed.path not in {"/api/queue", "/api/reset-action"}:
            self._send_json({"ok": False, "error": "not found"}, 404)
            return
        if not self._same_origin_request():
            self._send_json({"ok": False, "error": "same-origin request required"}, 403)
            return
        if self.headers.get_content_type() != "application/json":
            self._send_json({"ok": False, "error": "application/json required"}, 415)
            return
        try:
            length = int(self.headers.get("Content-Length", "-1"))
        except ValueError:
            length = -1
        size_limit = QUEUE_MESSAGE_LIMIT + 512 if parsed.path == "/api/queue" else 4096
        if length < 0 or length > size_limit:
            self._send_json({"ok": False, "error": "invalid request size"}, 413)
            return
        try:
            body = json.loads(self.rfile.read(length))
        except (json.JSONDecodeError, UnicodeDecodeError):
            self._send_json({"ok": False, "error": "invalid JSON"}, 400)
            return
        if parsed.path == "/api/reset-action":
            service = self.reset_service
            if service is None:
                self._send_json({"ok": False, "error": "Reset action service is unavailable."}, 503)
                return
            if parsed.query or not isinstance(body, dict) or set(body) != {"mode", "threadIds"}:
                self._send_json({"ok": False, "error": "Expected only mode and threadIds."}, 400)
                return
            mode, thread_ids = body.get("mode"), body.get("threadIds")
            if (not isinstance(mode, str) or mode not in {"greet", "continue"} or not isinstance(thread_ids, list)
                    or len(thread_ids) > 20
                    or any(not isinstance(item, str) or not THREAD_ID.fullmatch(item) for item in thread_ids)
                    or (mode == "greet" and thread_ids) or (mode == "continue" and not thread_ids)):
                self._send_json({"ok": False, "error": "Invalid reset action mode or chat IDs."}, 400)
                return
            if not _codex_queue_allowed():
                self._send_json({"ok": False, "error": "Explicit Codex SEND consent is required."}, 403)
                return
            try:
                action = service.configure_reset_action(mode, thread_ids)
            except Exception as exc:
                from .provider_policy import ProviderPolicyError
                if isinstance(exc, ProviderPolicyError):
                    self._send_json({"ok": False, "error": "Codex policy does not allow this reset action."}, 403)
                    return
                if isinstance(exc, ValueError):
                    message = str(exc)[:180] if _codex_allows("show") else "Reset action settings are invalid."
                    self._send_json({"ok": False, "error": message}, 400)
                    return
                if isinstance(exc, RuntimeError):
                    status = 409 if "running" in str(exc).lower() or "in progress" in str(exc).lower() else 503
                    self._send_json({"ok": False, "error": ("A reset action is currently running. Try again shortly."
                                                                  if status == 409 else "Reset action settings could not be saved.")}, status)
                    return
                self._send_json({"ok": False, "error": "Reset action settings could not be saved."}, 503)
                return
            if not _codex_allows("show"):
                action = _without_codex_thread_ids(action)
            self._send_json({"ok": True, "action": action})
            return
        thread = body.get("thread") if isinstance(body, dict) else None
        message = body.get("message") if isinstance(body, dict) else None
        if not isinstance(thread, str) or not THREAD_ID.fullmatch(thread):
            self._send_json({"ok": False, "error": "invalid thread id"}, 400)
            return
        if not isinstance(message, str) or not message.strip() or len(message) > QUEUE_MESSAGE_LIMIT:
            self._send_json({"ok": False, "error": "message must be 1 to 2000 characters"}, 400)
            return
        if not _codex_queue_allowed():
            self._send_json({"ok": False, "error": "Explicit Codex SEND consent is required."}, 403)
            return
        if not _is_collected_codex_thread(thread):
            self._send_json({"ok": False, "error": "unknown Codex thread"}, 404)
            return
        try:
            queue_codex_message(thread, message)
        except FileNotFoundError:
            self._send_json({"ok": False, "error": "Codex CLI unavailable"}, 503)
            return
        except OSError:
            self._send_json({"ok": False, "error": "Could not start Codex CLI"}, 503)
            return
        except subprocess.TimeoutExpired:
            # The CLI may have accepted the message before timing out. Do not claim either failure
            # or delivery when the outcome is indeterminate.
            self._send_json({"ok": False, "status": "unknown", "error": "queue status unknown; check the Codex task"}, 504)
            return
        except subprocess.CalledProcessError:
            self._send_json({"ok": False, "error": "Codex CLI rejected the queue request"}, 502)
            return
        self._send_json({"ok": True, "status": "queued", "detail": "Queued for the next turn. This does not answer the pending question."})

    def _valid_local_host(self) -> bool:
        host = self.headers.get("Host", "")
        try:
            parsed = urlsplit("//" + host)
            return (bool(host) and "@" not in parsed.netloc and not parsed.path
                    and not parsed.query and not parsed.fragment
                    and parsed.hostname in {"127.0.0.1", "localhost", "::1"}
                    and parsed.port == self.server.server_port)
        except ValueError:
            return False

    def _open_request_allowed(self) -> bool:
        origin = self.headers.get("Origin")
        origin_valid = self._same_origin_request()
        if origin is not None and not origin_valid:
            return False

        referer = self.headers.get("Referer")
        referer_valid = False
        if referer is not None:
            try:
                parsed = urlsplit(referer)
                host = self.headers.get("Host", "")
                referer_valid = (parsed.scheme == "http" and parsed.netloc == host
                                 and "@" not in parsed.netloc
                                 and parsed.hostname in {"127.0.0.1", "localhost", "::1"}
                                 and parsed.port == self.server.server_port)
            except ValueError:
                referer_valid = False
            if not referer_valid:
                return False

        fetch_site = self.headers.get("Sec-Fetch-Site")
        if fetch_site is not None and fetch_site.lower() != "same-origin" and not (origin_valid or referer_valid):
            return False
        return True

    def _same_origin_request(self) -> bool:
        origin = self.headers.get("Origin", "")
        host = self.headers.get("Host", "")
        parsed = urlparse(origin)
        return (parsed.scheme == "http" and parsed.netloc == host and not parsed.path
                and parsed.hostname in {"127.0.0.1", "localhost", "::1"})

    def _send_json(self, value, status: int = 200) -> None:
        payload = json.dumps(value).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def end_headers(self):
        if self.path.endswith((".html", ".css", ".js")):
            self.send_header("Cache-Control", "no-store, max-age=0")
        super().end_headers()

    def log_message(self, fmt, *args):
        if args and str(args[0]).startswith(("GET /api", "POST /api")):
            return
        super().log_message(fmt, *args)


def queue_codex_message(thread: str, message: str) -> None:
    """Queue a user message for the next turn in an existing Codex task."""
    if not _codex_queue_allowed():
        from .provider_policy import ProviderPolicyError
        raise ProviderPolicyError("explicit Codex SEND consent is required to queue messages")
    executable = shutil.which("codex")
    if not executable:
        raise FileNotFoundError("codex")
    result = subprocess.run(
        [executable, "queue", "--thread", thread, "--message", message],
        stdin=subprocess.DEVNULL, capture_output=True, text=True,
        timeout=QUEUE_TIMEOUT_SECONDS, check=False,
        **({"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}),
    )
    if result.returncode:
        raise subprocess.CalledProcessError(result.returncode, result.args)

def main() -> None:
    parser = argparse.ArgumentParser(description="Run the local Token Tracker 2a widget")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-open", action="store_true")
    args = parser.parse_args()
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    url = f"http://{args.host}:{args.port}"
    print(f"Token Tracker running at {url}")
    if not args.no_open:
        threading.Timer(0.4, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
