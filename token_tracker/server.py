from __future__ import annotations

import argparse
import copy
import gzip
import hashlib
import hmac
import ipaddress
import json
import logging
import math
import os
import re
import secrets
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
import webbrowser
from datetime import datetime, timedelta, timezone
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer as _BaseThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse, urlsplit

from . import product, providers as provider_module
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
LIVE_BUILD_BUDGET_SECONDS = 1.5
_CLAUDE_COLLECTION_LOCK = threading.Lock()
_SETTINGS_TOKEN_LOCK = threading.Lock()
_SETTINGS_TOKEN: str | None = None
_SETTINGS_AUTH_MESSAGE = "Open settings from the Branchglance widget to change this."


def set_settings_token(token: str | None) -> None:
    """Register or clear the current reset-settings window capability in memory."""
    global _SETTINGS_TOKEN
    if token is not None and (not isinstance(token, str) or not token):
        raise ValueError("settings token must be a non-empty string or None")
    with _SETTINGS_TOKEN_LOCK:
        _SETTINGS_TOKEN = token


def _settings_token_matches(provided: str | None) -> bool:
    with _SETTINGS_TOKEN_LOCK:
        return _settings_token_matches_locked(provided)


def _settings_token_matches_locked(provided: str | None) -> bool:
    expected = _SETTINGS_TOKEN
    return bool(expected and provided and hmac.compare_digest(expected, provided))


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
    """UI-facing identity (product.identity() plus policy summary). Cutover never trusts
    this unauthenticated route; it uses the control channel's authenticated health."""
    try:
        from . import product
        who = product.identity()
    except Exception:
        who = {"product": PRODUCT, "build": BUILD, "pid": os.getpid(), "stateRoot": None, "policyWarnings": []}
    try:
        from .provider_policy import public_policies
        policies = public_policies()
        summary = {"generation": policies["generation"],
                   "providers": {row["name"]: {"collect": row["collect"], "show": row["show"],
                                                "send": row["send"], "sendOrigin": row["sendOrigin"]}
                                 for row in policies["providers"]}}
    except Exception:
        summary = None
    return dict(who, providerPolicies=summary)


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


def _live_complete(value: object) -> bool:
    return not (isinstance(value, dict) and value.get("complete") is False)


def _live_progressed(previous: object, value: dict) -> bool:
    """A partial round makes progress when it leaves fewer chats awaiting detail."""
    if not isinstance(previous, dict) or _live_complete(previous):
        return True
    return value.get("pendingChats", 0) < previous.get("pendingChats", 0)


class LiveCache:
    """Keep slow live-provider work out of HTTP request threads."""

    def __init__(self) -> None:
        self.condition = threading.Condition()
        self.value = None
        self._encoded_value = None
        self._plain = None
        self._gzip = None
        self.completed_at = 0.0
        self.building = False
        self.retry_after = 0.0

    def _refresh(self) -> None:
        with self.condition:
            # Until a complete value exists, each round is budgeted; memoized chats make later rounds cheap.
            previous = self.value
            budget = None if previous is not None and _live_complete(previous) else LIVE_BUILD_BUDGET_SECONDS
        try:
            value = live_state() if budget is None else live_state(budget_seconds=budget)
            plain = json.dumps(value).encode("utf-8")
        except Exception as exc:
            _log_live_failure(exc)
            with self.condition:
                self.building = False
                self.retry_after = time.monotonic() + LIVE_RETRY_COOLDOWN_SECONDS
                self.condition.notify_all()
            return

        try:
            compressed = gzip.compress(plain, compresslevel=1, mtime=0)
        except Exception:
            compressed = None

        with self.condition:
            self.value = value
            self._encoded_value = value
            self._plain = plain
            self._gzip = compressed
            self.completed_at = time.monotonic()
            self.building = False
            self.retry_after = 0.0
            if not _live_complete(value):
                if _live_progressed(previous, value):
                    # Keep building until complete; no caller or TTL is needed.
                    self._start_refresh_locked(self.completed_at)
                else:
                    # No chat finished this round: back off; the next poll resumes after the cooldown.
                    self.retry_after = self.completed_at + LIVE_RETRY_COOLDOWN_SECONDS
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

    def _selected_locked(self, encoded: bool):
        if not encoded:
            return self.value
        if self._encoded_value is not self.value:
            return self.value, None, None
        return self.value, self._plain, self._gzip

    def get(self, *, encoded: bool = False):
        with self.condition:
            now = time.monotonic()
            age = now - self.completed_at if self.value is not None else float("inf")
            if self.value is not None and age <= LIVE_MAX_AGE_SECONDS:
                if age >= LIVE_REFRESH_TTL_SECONDS:
                    self._start_refresh_locked(now)
                return self._selected_locked(encoded)

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
                return self._selected_locked(encoded)
            return None


LIVE_CACHE = LiveCache()


def _live_encodings(header: str) -> tuple[bool, bool]:
    """Return whether gzip and identity are acceptable for this request."""
    choices: dict[str, float] = {}
    for item in header.split(","):
        parts = [part.strip() for part in item.split(";")]
        name = parts[0].lower()
        if not name:
            continue
        quality = 1.0
        for parameter in parts[1:]:
            key, sep, raw = parameter.partition("=")
            if key.strip().lower() != "q" or not sep or not re.fullmatch(r"(?:0(?:\.\d{0,3})?|1(?:\.0{0,3})?)", raw.strip()):
                quality = 0.0
                break
            quality = min(quality, float(raw.strip()))
        choices[name] = min(choices.get(name, 1.0), quality)
    wildcard = choices.get("*", 0.0)
    return choices.get("gzip", wildcard) > 0, choices.get("identity", 0.0 if choices.get("*") == 0 else 1.0) > 0


class SnapshotCache:
    RETRY_COOLDOWN_SECONDS = 5.0
    REQUEST_TIMEOUT_SECONDS = 10.0
    PERSISTED_SCHEMA_VERSION = 1
    PERSISTED_MAX_BYTES = 32 * 1024 * 1024
    PERSISTED_KEY_NAME = "snapshot-cache.key"
    PERSISTED_KEY_BYTES = 32

    def __init__(self, ttl: float = 120.0, *, persist_path: Path | None = None,
                 compatibility: str | None = None) -> None:
        self.ttl = ttl
        self.persist_path = Path(persist_path) if persist_path is not None else None
        # Compatibility reads provider policy, which the app may load only after its
        # Install gate; the key and the disk restore are therefore resolved on first use.
        self._compatibility = compatibility
        self._restore_once = threading.Lock()
        self._restore_pending = self.persist_path is not None
        self.lock = threading.Lock()
        self.condition = threading.Condition(self.lock)
        # Commit I/O never holds the request condition. Epoch changes share
        # this lock with durable publication and peak recording.
        self._publication_lock = threading.RLock()
        self._value = None
        # Only a snapshot built in this process may authorize chat actions.
        self._trusted_value = None
        self._at = 0.0
        self._restored_age = 0.0
        self._restored_at = 0.0
        self._restored = False
        self.generation = 0
        self.built_at: str | None = None
        self.building = False
        self.retry_after = 0.0
        self.last_failure_at: str | None = None
        self.last_failure_category: str | None = None
        self._last_build_error: Exception | None = None
        self._attempt = 0
        self._completed_attempt = 0
        # Future identity changes must increment this before accepting samples.
        self._publication_epoch = 0

    @property
    def compatibility(self) -> str | None:
        if self._compatibility is None and self.persist_path is not None:
            self._compatibility = _snapshot_cache_compatibility()
        return self._compatibility

    def _ensure_restored(self) -> None:
        if not self._restore_pending:
            return
        # First-use callers wait here until the restore has finished, so none can
        # publish a fresh build that the restore would then overwrite.
        with self._restore_once:
            if self._restore_pending:
                try:
                    self._restore_persisted()
                finally:
                    self._restore_pending = False

    @property
    def value(self):
        """Return only a current-process build for action authorization."""
        self._ensure_restored()
        with self.condition:
            value = self._trusted_value
            if value is None and not self._restored:
                # Preserve compatibility with in-memory test and integration seeding.
                value = self._value
        return copy.deepcopy(value)

    @value.setter
    def value(self, value) -> None:
        display, trusted = copy.deepcopy(value), copy.deepcopy(value)
        with self._publication_lock:
            # Rotate the authentication key before changing the epoch. A crash
            # then leaves any previous disk record unverifiable on restore.
            if self.persist_path is not None and self.persist_path.exists():
                self._ensure_persistence_key()
                self._atomic_write(self._key_path, secrets.token_bytes(self.PERSISTED_KEY_BYTES))
            with self.condition:
                self._publication_epoch += 1
                self._value = display
                self._trusted_value = trusted
                self._restored = False

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

    @staticmethod
    def _parse_utc(value: object) -> datetime | None:
        if not isinstance(value, str):
            return None
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                return None
            return parsed.astimezone(timezone.utc)
        except (OverflowError, ValueError, TypeError):
            return None

    @staticmethod
    def _authenticated_bytes(envelope: dict) -> bytes:
        body = {key: value for key, value in envelope.items() if key != "mac"}
        return json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")

    @classmethod
    def _snapshot_mac(cls, envelope: dict, key: bytes) -> str:
        return hmac.new(key, cls._authenticated_bytes(envelope), hashlib.sha256).hexdigest()

    @property
    def _key_path(self) -> Path | None:
        return self.persist_path.parent / self.PERSISTED_KEY_NAME if self.persist_path is not None else None

    def _read_persistence_key(self) -> bytes:
        path = self._key_path
        if path is None:
            raise OSError("snapshot persistence is disabled")
        info = os.lstat(path)
        reparse = getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
        if not stat.S_ISREG(info.st_mode) or reparse or info.st_size != self.PERSISTED_KEY_BYTES:
            raise ValueError("snapshot key is not a regular 32-byte file")
        key = path.read_bytes()
        if len(key) != self.PERSISTED_KEY_BYTES:
            raise ValueError("snapshot key is not a regular 32-byte file")
        return key

    @staticmethod
    def _atomic_write(path: Path, payload: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
        temp_path = Path(temp_name)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_path, path)
        finally:
            try:
                temp_path.unlink(missing_ok=True)
            except OSError:
                pass

    def _ensure_persistence_key(self) -> bytes:
        try:
            return self._read_persistence_key()
        except FileNotFoundError:
            pass
        except ValueError:
            path = self._key_path
            if path is None:
                raise
            info = os.lstat(path)
            reparse = getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
            if not stat.S_ISREG(info.st_mode) or reparse:
                raise OSError("snapshot key path is not a regular file")
        key = secrets.token_bytes(self.PERSISTED_KEY_BYTES)
        self._atomic_write(self._key_path, key)
        return key

    def _delete_persisted(self) -> None:
        if self.persist_path is not None:
            try:
                self.persist_path.unlink()
            except OSError:
                pass

    @staticmethod
    def _shown_provider_names() -> set[str]:
        from .provider_policy import POLICIES, PROVIDERS, policy_for
        return {name for name in PROVIDERS if policy_for(name, POLICIES).allows("show")}

    @staticmethod
    def _safe_snapshot_shape(snapshot: object) -> bool:
        if not isinstance(snapshot, dict) or not isinstance(snapshot.get("providers"), list):
            return False
        if not isinstance(snapshot.get("enabledProviders"), list):
            return False
        if SnapshotCache._parse_utc(snapshot.get("generatedAt")) is None:
            return False
        return all(
            isinstance(provider, dict) and isinstance(provider.get("name"), str)
            and isinstance(provider.get("chats"), list)
            for provider in snapshot["providers"]
        )

    def _restore_persisted(self) -> None:
        with self._publication_lock:
            self._restore_persisted_guarded()

    def _restore_persisted_guarded(self) -> None:
        path = self.persist_path
        if path is None:
            return
        try:
            info = os.lstat(path)
            reparse = getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
            if (not stat.S_ISREG(info.st_mode) or reparse or info.st_size <= 0
                    or info.st_size > self.PERSISTED_MAX_BYTES):
                return
            envelope = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(envelope, dict):
                self._delete_persisted()
                return
            try:
                key = self._read_persistence_key()
            except (OSError, ValueError):
                self._delete_persisted()
                return
            mac = envelope.get("mac")
            if not isinstance(mac, str) or len(mac) != hashlib.sha256().digest_size * 2:
                self._delete_persisted()
                return
            try:
                bytes.fromhex(mac)
                expected_mac = self._snapshot_mac(envelope, key)
            except (TypeError, ValueError, RecursionError):
                self._delete_persisted()
                return
            if not hmac.compare_digest(mac, expected_mac):
                self._delete_persisted()
                return
            if (envelope.get("schemaVersion") != self.PERSISTED_SCHEMA_VERSION
                    or envelope.get("compatibility") != self.compatibility
                    or type(envelope.get("generation")) is not int or envelope["generation"] < 1):
                return
            built_at = self._parse_utc(envelope.get("builtAt"))
            snapshot = envelope.get("snapshot")
            now = datetime.now(timezone.utc)
            if (built_at is None or built_at > now + timedelta(seconds=1)
                    or not self._safe_snapshot_shape(snapshot)):
                return
            provider_names = {provider["name"] for provider in snapshot["providers"]}
            if not provider_names <= self._shown_provider_names():
                self._delete_persisted()
                return
            snapshot = copy.deepcopy(snapshot)
            snapshot.pop("snapshotCache", None)
            with self.condition:
                if self._value is not None:
                    return  # a fresh build won; disk data must never replace it
                self._value = snapshot
                self._trusted_value = None
                self._at = time.monotonic()
                self._restored_age = max(0.0, (now - built_at).total_seconds())
                self._restored_at = self._at
                self._restored = True
                self.generation = envelope["generation"]
                self.built_at = built_at.isoformat()
        except (OSError, OverflowError, ValueError, TypeError, RecursionError):
            # A missing or damaged disk cache only loses the warm display path.
            return

    def _persistable(self, snapshot: dict) -> bool:
        if self.persist_path is None or self.compatibility is None or not self._safe_snapshot_shape(snapshot):
            return False
        expected = set(snapshot.get("enabledProviders") or []) | set(snapshot.get("limitProviders") or [])
        providers = {row.get("name"): row for row in snapshot.get("providers", [])}
        return bool(expected) and all(
            name in providers and providers[name].get("connected") is True
            and not providers[name].get("stale")
            for name in expected
        )

    def _write_persisted(self, snapshot: dict, generation: int, built_at: str) -> None:
        path = self.persist_path
        if path is None:
            return
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            try:
                existing = os.lstat(path)
            except FileNotFoundError:
                existing = None
            if existing is not None:
                reparse = getattr(existing, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
                if not stat.S_ISREG(existing.st_mode) or reparse:
                    raise OSError("snapshot cache path is not a regular file")
            key = self._ensure_persistence_key()
            envelope = {
                "schemaVersion": self.PERSISTED_SCHEMA_VERSION,
                "compatibility": self.compatibility,
                "generation": generation,
                "builtAt": built_at,
                "snapshot": snapshot,
            }
            envelope["mac"] = self._snapshot_mac(envelope, key)
            payload = json.dumps(envelope, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            if len(payload) > self.PERSISTED_MAX_BYTES:
                raise OSError("snapshot cache exceeds size limit")
            self._atomic_write(path, payload)
        except OSError as exc:
            logging.getLogger(__name__).warning("Snapshot cache persistence failed (%s)", type(exc).__name__)

    def _record_failure_locked(self, exc: Exception, *, worker_start: bool = False,
                               clear_owner: bool = True) -> None:
        if clear_owner:
            self.building = False
        self.retry_after = time.monotonic() + self.RETRY_COOLDOWN_SECONDS
        self.last_failure_at = self._utc_now()
        self.last_failure_category = self._failure_category(exc, worker_start=worker_start)
        self._last_build_error = exc
        self._completed_attempt = self._attempt

    def _metadata_locked(self, now: float) -> dict:
        age = (max(0.0, self._restored_age + now - self._restored_at) if self._restored
               else max(0.0, now - self._at) if self._value is not None else None)
        return {
            "generation": self.generation,
            "builtAt": self.built_at,
            "ageSeconds": round(age, 3) if age is not None else None,
            "building": self.building,
            "stale": (self._value is None or age is None or self._restored or age > self.ttl
                      or self.last_failure_category is not None),
            "lastFailureAt": self.last_failure_at,
            "lastFailureCategory": self.last_failure_category,
            "identityGeneration": None,
        }

    @staticmethod
    def _valid_last_known(quota, observed_at, now):
        observed = SnapshotCache._parse_utc(observed_at)
        if not isinstance(quota, dict) or observed is None or observed.timestamp() > now + 60:
            return False
        percent, reset = quota.get("percent"), quota.get("resetAt")
        try:
            return (type(percent) in (int, float) and math.isfinite(percent) and 0 <= percent <= 100
                    and type(reset) in (int, float) and math.isfinite(reset) and reset > now)
        except OverflowError:
            return False

    @staticmethod
    def _egress(value, metadata: dict, *, restored: bool = False):
        if value is None:
            return None
        result = copy.deepcopy(value)
        from . import claude_identity
        _, identity = claude_identity.AUTHORITY.capture_nonblocking()
        metadata = dict(metadata)
        for provider in result.get("providers", []):
            if isinstance(provider, dict):
                if provider.get("name") == "Claude":
                    verified = bool(identity and not restored and provider.get("identityVerified") is True
                                    and provider.get("identityGeneration") == identity.generation)
                    provider["identityVerified"] = verified
                    provider["identityLabel"] = identity.label if verified else "Account unverified"
                    if verified:
                        metadata["identityGeneration"] = identity.generation
                times = provider.get("_quotaWindowObservedAt")
                times = times if isinstance(times, dict) else {}
                for field in ("fiveHour", "weekly"):
                    quota = provider.get(field)
                    if restored or isinstance(quota, dict) and quota.get("lastKnown") is True:
                        if not isinstance(quota, dict):
                            quota = {}
                            provider[field] = quota
                        observed = times.get(field, provider.get("limitsUpdatedAt"))
                        if SnapshotCache._valid_last_known(quota, observed, time.time()):
                            quota["lastKnown"] = True
                        else:
                            quota["percent"] = None
                            quota.pop("lastKnown", None)
        if restored:
            for provider in result.get("providers", []):
                if not isinstance(provider, dict):
                    continue
                provider["stale"] = True
                if not any((provider.get(field) or {}).get("lastKnown") for field in ("fiveHour", "weekly")):
                    provider["limitsUpdatedAt"] = None
                    provider.pop("_quotaWindowObservedAt", None)
                for chat in provider.get("chats", []):
                    if not isinstance(chat, dict):
                        continue
                    previous = chat.get("status")
                    chat.update(
                        status="unknown", previousStatus=previous, statusSource="unavailable",
                        telemetryUnavailable=True, metricsUnavailable=True,
                        executionStatus="unavailable", executionSource="unavailable",
                        executionObservedAt=None, executionSince=None,
                        executionCompactionStartedAt=None, executionTerminalAt=None,
                        executionActivity=None, pendingQuestion=None, pendingQuestionAt=None,
                        pendingOptions=[], pendingQuestionId=None, pendingQuestionIds=[],
                    )
                    for field in ("latestRequest", "latestRequestAt", "latestUpdate", "latestUpdateAt",
                                  "trackerUpdate", "trackerUpdateAt", "lastRequest", "activity"):
                        chat.pop(field, None)
        result["snapshotCache"] = metadata
        return result

    def _selected_locked(self):
        return self._value, self._metadata_locked(time.monotonic()), self._restored

    def _build(self, *, background: bool = False, attempt: int | None = None):
        # A worker owns the build until it actually exits, even if every caller
        # has timed out. Publication failures must release waiters as well.
        try:
            with self.condition:
                publication_epoch = self._publication_epoch
            self._ensure_restored()
            with self.condition:
                self.condition.notify_all()
            return self._build_inner(background=background, attempt=attempt, publication_epoch=publication_epoch)
        except BaseException as exc:
            with self.condition:
                self._record_failure_locked(exc, clear_owner=False)
            logging.getLogger(__name__).warning("Snapshot refresh failed (%s)", type(exc).__name__)
        finally:
            with self.condition:
                self.building = False
                self.condition.notify_all()

    def _build_inner(self, *, background: bool = False, attempt: int | None = None,
                     publication_epoch: int = 0):
        from . import claude_identity
        try:
            identity = claude_identity.current_identity()
        except Exception:
            identity = None
        identity_generation = identity.generation if identity else None
        value = copy.deepcopy(build_snapshot())
        if not isinstance(value, dict):
            raise TypeError("snapshot builder returned a non-object")
        trusted = copy.deepcopy(value)
        built_at = self._utc_now()
        with self.condition:
            published_at = time.monotonic()
            generation = self.generation + 1
            if publication_epoch != self._publication_epoch:
                raise RuntimeError("snapshot publication superseded")
            prior_value, prior_restored = self._value, self._restored
        display = copy.deepcopy(value)
        if prior_value is not None:
            prior = self._egress(prior_value, {}, restored=prior_restored)
            previous = {p.get("name"): p for p in prior.get("providers", []) if isinstance(p, dict)}
            for provider in display.get("providers", []):
                old = previous.get(provider.get("name"), {})
                if provider.get("name") == "Claude" and (
                        not provider.get("identityVerified") or not old.get("identityVerified")
                        or provider.get("identityGeneration") != old.get("identityGeneration")):
                    continue
                for field in ("fiveHour", "weekly"):
                    current, saved = provider.get(field), old.get(field)
                    if (not isinstance(current, dict) or current.get("percent") is None) and isinstance(saved, dict) and saved.get("lastKnown"):
                        provider[field] = copy.deepcopy(saved)
                        times = provider.setdefault("_quotaWindowObservedAt", {})
                        times[field] = (old.get("_quotaWindowObservedAt") or {}).get(field, old.get("limitsUpdatedAt"))
        try:
            latest_identity = claude_identity.current_identity()
        except Exception:
            latest_identity = None
        identity_current = bool(identity_generation and latest_identity and latest_identity.generation == identity_generation)
        with self._publication_lock, claude_identity.AUTHORITY.guard(identity_generation) as guarded_identity:
            with self.condition:
                if publication_epoch != self._publication_epoch:
                    raise RuntimeError("snapshot publication superseded")
            if not identity_current or guarded_identity is None:
                for snapshot in (value, display, trusted):
                    for provider in snapshot.get("providers", []):
                        if isinstance(provider, dict) and provider.get("name") == "Claude":
                            provider["identityVerified"] = False
                            provider["identityLabel"] = "Account unverified"
                            provider.pop("identityGeneration", None)
            _record_insights_peaks(value)
            if self._persistable(value):
                self._write_persisted(display, generation, built_at)
            with self.condition:
                self._value = display
                self._trusted_value = trusted
                self._at = published_at
                self.generation = generation
                self.built_at = built_at
                self._restored = False
                self._restored_age = 0.0
                self._restored_at = 0.0
                self.retry_after = 0.0
                self.last_failure_at = None
                self.last_failure_category = None
                self._last_build_error = None
                self._completed_attempt = attempt if attempt is not None else self._attempt
        return None

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
        deadline = time.monotonic() + self.REQUEST_TIMEOUT_SECONDS
        with self.condition:
            while True:
                now = time.monotonic()
                stale = self._restored or self._value is None or now - self._at > self.ttl
                if self._restored and self._value is not None and not force:
                    if not self.building and now >= self.retry_after:
                        self._start_background_locked()
                    selected = self._selected_locked()
                    break
                if background and self._value is None:
                    if self.building:
                        return None
                    if now < self.retry_after:
                        if force and self._last_build_error is not None:
                            raise self._last_build_error
                        return None
                    self._start_background_locked()
                    return None
                if background and self._value is not None:
                    if (force or stale) and not self.building:
                        if now >= self.retry_after:
                            self._start_background_locked()
                    selected = self._selected_locked()
                    break

                if self.building:
                    request_generation = self.generation
                    request_attempt = self._attempt
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        selected = self._selected_locked()
                        metadata = dict(selected[1], stale=True, staleReason="request_timeout",
                                        identityGeneration=None)
                        selected = (selected[0], metadata, True)
                        break
                    self.condition.wait(remaining)
                    if self.generation != request_generation and (not force or not self.building):
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

                self._start_background_locked()
                if not self.building and self._last_build_error is not None:
                    raise self._last_build_error
                continue

        if "selected" in locals():
            return self._egress(selected[0], selected[1], restored=selected[2])
        return None

    def get_background(self, force: bool = False):
        """Return cached data immediately, or start a cold build and return no snapshot."""
        with self.condition:
            now = time.monotonic()
            if self._value is not None:
                stale = self._restored or now - self._at > self.ttl
                if (force or stale) and not self.building and now >= self.retry_after:
                    self._start_background_locked()
                selected = self._selected_locked()
            else:
                if not self.building and now >= self.retry_after:
                    self._start_background_locked()
                if self._restore_pending:
                    self.condition.wait_for(lambda: not self._restore_pending, timeout=0.1)
                if self._value is None:
                    return None
                selected = self._selected_locked()
        return self._egress(selected[0], selected[1], restored=selected[2])


def _insights_allowed_names() -> set[str]:
    from .provider_policy import POLICIES, policy_for
    full, limit_only = provider_module._provider_collection_sets(POLICIES)
    return {name for name in (*full, *limit_only) if policy_for(name, POLICIES).allows("show")}


def _record_insights_peaks(snapshot: dict) -> None:
    """Sample limit peaks on every snapshot refresh; the store itself is owner-gated."""
    try:
        from . import insights
        from . import claude_identity
        _, identity = claude_identity.AUTHORITY.capture()
        safe = copy.deepcopy(snapshot)
        safe["providers"] = [p for p in safe.get("providers", []) if isinstance(p, dict) and
                             (p.get("name") != "Claude" or
                             (identity and p.get("identityVerified") is True
                              and p.get("identityGeneration") == identity.generation))]
        insights.record_limit_peaks(safe, allowed_names=_insights_allowed_names())
    except Exception:
        return


def _snapshot_cache_compatibility() -> str:
    from .claude_hook_install import claude_config_dir
    from .provider_policy import POLICIES, public_policies
    full, limits = provider_module._provider_collection_sets(POLICIES)
    roots = [
        os.environ.get("CODEX_HOME") or str(Path.home() / ".codex"),
        str(claude_config_dir()),
        os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local"),
    ]
    normalized = []
    for root in roots:
        try:
            value = str(Path(root).resolve(strict=False))
        except OSError:
            value = str(Path(root).absolute())
        normalized.append(value.casefold() if os.name == "nt" else value)
    material = {
        "policy": public_policies(POLICIES)["generation"],
        "full": full,
        "limits": limits,
        "roots": normalized,
    }
    return hashlib.sha256(json.dumps(material, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


class FastClaudeLimits:
    """Bound repeated reads of Claude's account-bound quota sources."""

    TTL_SECONDS = 2.0

    def __init__(self) -> None:
        self.condition = threading.Condition()
        self.value = None
        self.at = 0.0
        self.loading = False
        self.retained = {}
        self.identity_generation = None

    def _prune_retained_locked(self):
        now = time.time()
        for field, sample in list(self.retained.items()):
            if not SnapshotCache._valid_last_known(sample["value"], sample["observed"], now):
                del self.retained[field]

    def _remember_locked(self, limits):
        self._prune_retained_locked()
        observed = limits.get("limitsUpdatedAt")
        parsed = SnapshotCache._parse_utc(observed)
        now = time.time()
        if parsed is None or parsed.timestamp() > now + 60:
            return
        for field in ("fiveHour", "weekly"):
            value = limits.get(field)
            previous = self.retained.get(field)
            if (SnapshotCache._valid_last_known(value, observed, now)
                    and (previous is None or parsed.timestamp() >= previous["at"])):
                self.retained[field] = {"value": copy.deepcopy(value), "observed": observed,
                                        "at": parsed.timestamp()}

    def _load(self):
        try:
            from .providers import ClaudeLimitsProvider
            provider = ClaudeLimitsProvider()
            limits = provider.snapshot()
            if not isinstance(limits, dict):
                return None
            from . import claude_identity
            identity = claude_identity.current_identity()
            if not (identity and limits.get("identityVerified") is True
                    and limits.get("identityGeneration") == identity.generation):
                return None
            with self.condition:
                if self.identity_generation != identity.generation:
                    return None
                self.value = copy.deepcopy(limits)
                self.at = time.monotonic()
                self._remember_locked(limits)
            return limits
        except Exception:
            return None
        finally:
            with self.condition:
                self.loading = False
                self.condition.notify_all()

    def overlay(self, snapshot: dict, *, owned: bool = False) -> dict:
        """Overlay quota data, copying unless the caller owns its snapshot."""
        from . import claude_identity
        _, identity = claude_identity.AUTHORITY.capture_nonblocking()
        epoch = identity.generation if identity else None
        with self.condition:
            if self.identity_generation != epoch:
                self.identity_generation = epoch
                self.value = None
                self.at = 0.0
                self.retained.clear()
        if identity is None:
            return snapshot
        restored = any(p.get("name") == "Claude" and p.get("stale") is True
                       for p in snapshot.get("providers", []) if isinstance(p, dict))
        from .provider_policy import POLICIES, policy_for
        if not policy_for("Claude", POLICIES).allows("show"):
            return snapshot
        if restored:
            start = False
            with self.condition:
                now = time.monotonic()
                self._prune_retained_locked()
                if isinstance(self.value, dict):
                    self._remember_locked(self.value)
                retained = copy.deepcopy(self.retained)
                if (self.value is None or now - self.at > self.TTL_SECONDS) and not self.loading:
                    self.loading = True
                    start = True
            if start:
                try:
                    threading.Thread(target=self._load, name="fast-claude-limits", daemon=True).start()
                except (RuntimeError, OSError):
                    with self.condition:
                        self.loading = False
                        self.condition.notify_all()
            if not retained:
                return snapshot
            result = snapshot if owned else copy.deepcopy(snapshot)
            for provider in result.get("providers", []):
                if isinstance(provider, dict) and provider.get("name") == "Claude":
                    self._overlay_retained(provider, retained)
                    break
            return result
        waiting = False
        with self.condition:
            now = time.monotonic()
            if self.value is not None and now - self.at <= self.TTL_SECONDS:
                limits = copy.deepcopy(self.value)
                self._remember_locked(limits)
            elif self.loading:
                limits = None
                waiting = True
            else:
                self.loading = True
                limits = None
        if limits is None:
            if waiting:
                return self._apply_retained(snapshot, owned=owned)
            try:
                threading.Thread(target=self._load, name="fast-claude-limits", daemon=True).start()
            except (RuntimeError, OSError):
                with self.condition:
                    self.loading = False
                    self.condition.notify_all()
            return self._apply_retained(snapshot, owned=owned)
        result = snapshot if owned else copy.deepcopy(snapshot)
        for provider in result.get("providers", []):
            if isinstance(provider, dict) and provider.get("name") == "Claude":
                if provider.get("identityGeneration") != epoch or provider.get("identityVerified") is not True:
                    provider["fiveHour"] = copy.deepcopy(limits.get("fiveHour"))
                    provider["weekly"] = copy.deepcopy(limits.get("weekly"))
                    provider["limitsUpdatedAt"] = limits.get("limitsUpdatedAt")
                    provider.pop("_quotaWindowObservedAt", None)
                    provider["identityGeneration"] = epoch
                    provider["identityVerified"] = True
                    provider["identityLabel"] = identity.label
                    break
                previous_at = provider.get("limitsUpdatedAt")
                with self.condition:
                    self._prune_retained_locked()
                    available = set(self.retained)
                for field in ("fiveHour", "weekly", "limitsUpdatedAt"):
                    if field in limits:
                        current = provider.get(field)
                        times = provider.get("_quotaWindowObservedAt")
                        observed = times.get(field, previous_at) if isinstance(times, dict) else previous_at
                        known = (isinstance(current, dict) and current.get("lastKnown") is True
                                 and SnapshotCache._valid_last_known(current, observed, time.time()))
                        if field in ("fiveHour", "weekly") and (known or field in available) and (limits.get(field) or {}).get("percent") is None:
                            provider.setdefault("_quotaWindowObservedAt", {}).setdefault(field, previous_at)
                            continue
                        if field in ("fiveHour", "weekly") and isinstance(provider.get("_quotaWindowObservedAt"), dict):
                            provider["_quotaWindowObservedAt"].pop(field, None)
                        provider[field] = copy.deepcopy(limits[field])
                        if field in ("fiveHour", "weekly") and isinstance(provider[field], dict):
                            reset = provider[field].get("resetAt")
                            if type(reset) in (int, float) and reset <= time.time():
                                provider[field]["percent"] = None
                break
        return self._apply_retained(result, owned=True)

    def _apply_retained(self, snapshot, *, owned):
        with self.condition:
            self._prune_retained_locked()
            retained = copy.deepcopy(self.retained)
        if not retained:
            return snapshot
        result = snapshot if owned else copy.deepcopy(snapshot)
        for provider in result.get("providers", []):
            if isinstance(provider, dict) and provider.get("name") == "Claude":
                self._overlay_retained(provider, retained)
                break
        return result

    @staticmethod
    def _overlay_retained(provider, retained):
        previous_at = provider.get("limitsUpdatedAt")
        times = provider.get("_quotaWindowObservedAt")
        times = dict(times) if isinstance(times, dict) else {}
        now = time.time()
        replaced = False
        for field in ("fiveHour", "weekly"):
            current, sample = provider.get(field), retained.get(field)
            times.setdefault(field, previous_at)
            if not sample:
                continue
            observed = SnapshotCache._parse_utc(times[field])
            if (not SnapshotCache._valid_last_known(sample["value"], sample["observed"], now)
                    or isinstance(current, dict) and current.get("percent") is not None and observed is not None and (observed.timestamp() > sample["at"]
                        or observed.timestamp() == sample["at"] and isinstance(current, dict)
                        and current.get("percent") is not None and not current.get("lastKnown"))):
                continue
            provider[field] = copy.deepcopy(sample["value"])
            if now - sample["at"] > 600:
                provider[field]["lastKnown"] = True
            else:
                provider[field].pop("lastKnown", None)
            times[field] = sample["observed"]
            replaced = True
        if replaced:
            provider["_quotaWindowObservedAt"] = times
            newest = max((SnapshotCache._parse_utc(t) for t in times.values() if SnapshotCache._parse_utc(t)), default=None)
            if newest is not None:
                provider["limitsUpdatedAt"] = newest.isoformat()


CACHE = SnapshotCache(persist_path=product.state_root() / "stats-snapshot.json")


def _insights_snapshot():
    snapshot = CACHE.get()
    if snapshot is not None:
        for provider in snapshot.get("providers", []):
            for field in ("fiveHour", "weekly"):
                quota = provider.get(field)
                if isinstance(quota, dict) and quota.get("lastKnown") is True:
                    quota["percent"] = None
    return snapshot
FAST_CLAUDE_LIMITS = FastClaudeLimits()


class Handler(SimpleHTTPRequestHandler):
    reset_service = None

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(STATIC), **kwargs)

    def do_GET(self):
        if not self._valid_local_host():
            self._send_json({"ok": False, "error": "local Host required"}, 403)
            return
        parsed = urlparse(self.path)
        if parsed.path in {"/api/features", "/api/statusline", "/api/claude-collection"}:
            self._get_features_or_statusline(parsed)
            return
        if parsed.path in {"/api/provider-policy", "/api/debug-report", "/api/debug-report.json", "/api/identity"}:
            self._get_provider_state(parsed)
            return
        if parsed.path in {"/api/stats", "/api/insights", "/api/ledger/status", "/api/ledger", "/api/ledger/export", "/api/live", "/api/today"}:
            self._get_usage(parsed)
            return
        if parsed.path == "/api/open":
            self._get_open(parsed)
            return
        if parsed.path in {"/api/avatars", "/api/limit-reset", "/api/quota"}:
            self._get_codex_state(parsed)
            return
        if parsed.path == "/api/reset-action":
            self._get_reset_action(parsed)
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
            self.path = "/runway.html"
        super().do_GET()

    def _get_features_or_statusline(self, parsed):
        if parsed.path in {"/api/features", "/api/statusline"}:
            if parsed.query or not self._open_request_allowed():
                self._send_json({"ok": False, "error": "same-origin request required"}, 403)
                return
            try:
                if parsed.path == "/api/features":
                    from . import features, ledger_service
                    from .provider_policy import policy_for
                    policy_for("Claude")  # Resolve the startup send setting before reporting pending changes.
                    data = {name: features.state(name) for name in features.ENV}
                    data["ledger"]["restart_pending"] = ledger_service._RESTART_PENDING
                else:
                    from . import statusline_connect
                    data = statusline_connect.status()
                self._send_json({"ok": True, **data})
            except Exception:
                self._send_json({"ok": False, "error": "Settings status is unavailable."}, 503)
            return
        if parsed.path == "/api/claude-collection":
            if parsed.query or not self._open_request_allowed():
                self._send_json({"ok": False, "error": "same-origin request required"}, 403)
                return
            try:
                from .claude_collection import status as claude_collection_status
                self._send_json({"ok": True, **claude_collection_status()})
            except Exception as exc:
                logging.getLogger(__name__).error("Claude Code collection status failed (%s)", type(exc).__name__)
                self._send_json({"ok": False, "error": "Claude Code collection status is unavailable."}, 503)
            return

    def _get_provider_state(self, parsed):
        if parsed.path == "/api/provider-policy":
            try:
                from .provider_policy import public_policies
                payload = public_policies()
            except Exception:
                self._send_json({"ok": False, "error": "Provider policy is unavailable."}, 503)
                return
            self._send_json(payload)
            return
        if parsed.path == "/api/debug-report":
            # Same local-origin rules as the other /api reads; no CORS headers, never cached.
            if parsed.query or not self._open_request_allowed():
                self._send_json({"ok": False, "error": "same-origin request required"}, 403)
                return
            try:
                from . import debug_report
                text = debug_report.serialize(debug_report.build_report(CACHE.get(background=True)))
            except Exception:
                self._send_json({"ok": False, "error": "Debug report is unavailable."}, 503)
                return
            payload = text.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/markdown; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        if parsed.path == "/api/debug-report.json":
            # Structured form for the CLI. Same guards as the markdown route; no CORS, never cached.
            if parsed.query or not self._open_request_allowed():
                self._send_json({"ok": False, "error": "same-origin request required"}, 403)
                return
            try:
                from . import debug_report
                payload = json.dumps(debug_report.report_to_dict(debug_report.build_report(CACHE.get(background=True)))).encode("utf-8")
            except Exception:
                self._send_json({"ok": False, "error": "Debug report is unavailable."}, 503)
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        if parsed.path == "/api/identity":
            if not self._open_request_allowed():
                self._send_json({"ok": False, "error": "same-origin request required"}, 403)
                return
            self._send_json(_identity_payload())
            return

    def _get_usage(self, parsed):
        if parsed.path == "/api/stats":
            query = parse_qs(parsed.query)
            force = query.get("refresh", [""])[0] == "1"
            if query.get("background", [""])[0] == "1":
                snapshot = CACHE.get(force=force, background=True)
            else:
                snapshot = CACHE.get(force=force)
            if snapshot is None:
                self._send_json({"ok": False, "error": "Snapshot is building."}, 503)
                return
            if query.get("background", [""])[0] == "1":
                # CACHE.get returns a private egress copy for each caller.
                snapshot = FAST_CLAUDE_LIMITS.overlay(snapshot, owned=True)
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
            # Insights aggregates stay server-side; strip them with a shallow copy
            # (a deep copy of the whole snapshot would cost every request).
            snapshot = {**snapshot, "providers": [
                {k: v for k, v in provider.items() if k != "_insightsData"} if isinstance(provider, dict) else provider
                for provider in snapshot.get("providers") or []]}
            from . import claude_identity
            _, identity = claude_identity.AUTHORITY.capture_nonblocking()
            for provider in snapshot["providers"]:
                if isinstance(provider, dict) and provider.get("name") == "Claude":
                    verified = bool(identity and provider.get("identityVerified") is True
                                    and provider.get("identityGeneration") == identity.generation)
                    provider["identityVerified"] = verified
                    provider["identityLabel"] = identity.label if verified else "Account unverified"
            self._send_json(snapshot)
            return
        if parsed.path == "/api/insights":
            from . import insights
            from .provider_policy import POLICIES, public_policies
            query = parse_qs(parsed.query)
            range_name = query.get("range", ["30d"])[0]
            if range_name not in insights.RANGES:
                self._send_json({"ok": False, "error": "Invalid insights range."}, 400)
                return
            allowed = _insights_allowed_names()
            generation = public_policies(POLICIES)["generation"]
            payload = insights.INSIGHTS_CACHE.get(
                range_name, generation, allowed_names=allowed,
                snapshot_loader=_insights_snapshot,
                peak_store=insights.LimitPeakStore())
            if payload is None:
                self._send_json({"ok": False, "error": "Snapshot is building."}, 503)
            else:
                self._send_json(payload)
            return
        if parsed.path == "/api/ledger/status":
            from . import ledger_service
            self._send_json(ledger_service.status_payload())
            return
        if parsed.path in {"/api/ledger", "/api/ledger/export"}:
            from . import ledger_service
            status, headers, body = ledger_service.http_response(parsed.path, parse_qs(parsed.query))
            self.send_response(status)
            for name, value in headers.items():
                self.send_header(name, value)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if parsed.path == "/api/live":
            selected = LIVE_CACHE.get(encoded=True)
            if selected is None:
                self._send_json({"ok": False, "error": "Live telemetry unavailable"}, 503)
            else:
                self._send_live_json(*selected)
            return
        if parsed.path == "/api/today":
            self._send_json(today_usage())
            return

    def _get_open(self, parsed):
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
                url = f"codex://threads/{thread}"
                startfile = getattr(os, "startfile", None)
                if startfile is None and sys.platform == "darwin":
                    # macOS has no os.startfile; LaunchServices opens the codex:// link.
                    subprocess.run(["/usr/bin/open", url], check=True, timeout=10,
                                   stdin=subprocess.DEVNULL, capture_output=True)
                else:
                    startfile(url)  # type: ignore[misc]
                self._send_json({"ok": True})
            except (AttributeError, TypeError, OSError, subprocess.SubprocessError) as exc:
                self._send_json({"ok": False, "error": str(exc)[:120]})
            return

    def _get_codex_state(self, parsed):
        if parsed.path == "/api/avatars":
            if not self._open_request_allowed():
                self._send_json({"ok": False, "error": "same-origin request required"}, 403)
                return
            if not _codex_allows("collect"):
                self._send_json({"avatars": []})
                return
            # Codex's own subagent avatars, read from the local app install (cached per version).
            from .codex_avatars import codex_avatars
            from .limit_reset import _runtime_dir
            self._send_json({"avatars": codex_avatars(_runtime_dir())})
            return
        if parsed.path == "/api/limit-reset":
            if not self._open_request_allowed():
                self._send_json({"ok": False, "error": "same-origin request required"}, 403)
                return
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

    def _get_reset_action(self, parsed):
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

    def do_POST(self):
        parsed = urlparse(self.path)
        settings_mutation = parsed.path in {"/api/claude-collection", "/api/ledger/purge", "/api/claude-greeting-rearm", "/api/features", "/api/statusline"}
        if not self._valid_local_host():
            if settings_mutation:
                self._reject_post(parsed.path, {"ok": False, "error": _SETTINGS_AUTH_MESSAGE}, 403)
                return
            self._reject_post(parsed.path, {"ok": False, "error": "local Host required"}, 403)
            return
        if parsed.path not in {"/api/queue", "/api/reset-action", "/api/claude-collection", "/api/ledger/purge", "/api/claude-greeting-rearm", "/api/features", "/api/statusline"}:
            self._reject_post(parsed.path, {"ok": False, "error": "not found"}, 404)
            return
        if not self._same_origin_request():
            if settings_mutation:
                self._reject_post(parsed.path, {"ok": False, "error": _SETTINGS_AUTH_MESSAGE}, 403)
                return
            self._reject_post(parsed.path, {"ok": False, "error": "same-origin request required"}, 403)
            return
        if settings_mutation and (parsed.query or not _settings_token_matches(
                self.headers.get("X-Branchglance-Settings-Token"))):
            self._reject_post(parsed.path, {"ok": False, "error": _SETTINGS_AUTH_MESSAGE}, 403)
            return
        if self.headers.get_content_type() != "application/json":
            if settings_mutation and parsed.path == "/api/claude-collection":
                self._reject_post(parsed.path, {"ok": False, "error": _SETTINGS_AUTH_MESSAGE}, 403)
                return
            self._reject_post(parsed.path, {"ok": False, "error": "application/json required"}, 415)
            return
        try:
            length = int(self.headers.get("Content-Length", "-1"))
        except ValueError:
            length = -1
        size_limit = QUEUE_MESSAGE_LIMIT + 512 if parsed.path == "/api/queue" else 4096
        if length < 0 or length > size_limit:
            self._reject_post(parsed.path, {"ok": False, "error": "invalid request size"}, 413)
            return
        try:
            body = json.loads(self.rfile.read(length))
        except (json.JSONDecodeError, UnicodeDecodeError):
            self._send_json({"ok": False, "error": "invalid JSON"}, 400)
            return
        if parsed.path in {"/api/features", "/api/statusline"}:
            with _SETTINGS_TOKEN_LOCK:
                if not _settings_token_matches_locked(self.headers.get("X-Branchglance-Settings-Token")):
                    self._send_json({"ok": False, "error": _SETTINGS_AUTH_MESSAGE}, 403)
                    return
                try:
                    if parsed.path == "/api/features":
                        from . import features, ledger_service, claude_usage_api
                        from .provider_policy import policy_for
                        if (not isinstance(body, dict) or set(body) != {"name", "value"}
                                or body["name"] not in features.ENV or type(body["value"]) is not bool):
                            raise ValueError("Invalid feature setting.")
                        policy_for("Claude")
                        result = features.save(body["name"], body["value"])
                        if body["name"] == "ledger":
                            result["restart_pending"] = ledger_service.apply_feature(result["effective"])
                        elif body["name"] == "live_usage":
                            if result["effective"]:
                                claude_usage_api.REFRESHER.start()
                            else:
                                claude_usage_api.REFRESHER.stop()
                    else:
                        from . import statusline_connect
                        if not isinstance(body, dict) or set(body) != {"action"} or body["action"] not in {"connect", "disconnect"}:
                            raise ValueError("Invalid status line action.")
                        result = (statusline_connect.connect() if body["action"] == "connect"
                                  else statusline_connect.disconnect())
                except (ValueError, OSError) as exc:
                    self._send_json({"ok": False, "error": str(exc) if isinstance(exc, ValueError) else "Settings could not be saved."}, 409)
                    return
                except Exception as exc:
                    from .claude_hook_install import HookInstallError
                    message = str(exc) if isinstance(exc, HookInstallError) else "Settings could not be changed."
                    self._send_json({"ok": False, "error": message}, 409)
                    return
            self._send_json({"ok": True, **result})
            return
        if parsed.path == "/api/ledger/purge":
            self._purge_ledger(body)
            return
        if parsed.path == "/api/claude-greeting-rearm":
            if body != {} or self.reset_service is None:
                self._send_json({"ok": False, "error": "Claude greeting is unavailable."}, 400)
                return
            with _SETTINGS_TOKEN_LOCK:
                if not _settings_token_matches_locked(self.headers.get("X-Branchglance-Settings-Token")):
                    self._send_json({"ok": False, "error": _SETTINGS_AUTH_MESSAGE}, 403)
                    return
                try:
                    self.reset_service.rearm_claude_greeting()
                except Exception:
                    self._send_json({"ok": False, "error": "Claude greeting could not be armed."}, 409)
                    return
            self._send_json({"ok": True})
            return
        if settings_mutation:
            action = body.get("action") if isinstance(body, dict) else None
            if not isinstance(action, str) or action not in {"enable", "disable"} or set(body) != {"action"}:
                self._send_json({"ok": False, "error": "Expected action enable or disable."}, 400)
                return
            from . import claude_collection
            operation = claude_collection.enable if action == "enable" else claude_collection.disable
            provided_token = self.headers.get("X-Branchglance-Settings-Token")
            with _SETTINGS_TOKEN_LOCK:
                if not _settings_token_matches_locked(provided_token):
                    self._send_json({"ok": False, "error": _SETTINGS_AUTH_MESSAGE}, 403)
                    return
                with _CLAUDE_COLLECTION_LOCK:
                    try:
                        result = operation()
                    except Exception as exc:
                        logging.getLogger(__name__).error("Claude Code collection change failed (%s)", type(exc).__name__)
                        self._send_json({"ok": False, "error": "Claude Code collection could not be changed."}, 503)
                        return
            self._send_json(result, 200 if result.get("ok") else 409)
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

    def _purge_ledger(self, body) -> None:
        """POST /api/ledger/purge: delete one provider's saved ledger history through the widget's single writer."""
        provider = body.get("provider") if isinstance(body, dict) else None
        if (not isinstance(body, dict) or set(body) != {"provider"} or not isinstance(provider, str)
                or provider not in {"Claude", "Codex"}):
            self._send_json({"ok": False, "error": "Expected provider Claude or Codex."}, 400)
            return
        from . import ledger_service
        service = ledger_service.current()
        if service is None or service.health()["state"] == "disabled":
            self._send_json({"ok": False, "error": "Ledger is off."}, 503)
            return
        # The writer's resolved collect flags are the authority; the window's copy is advisory.
        if service.collect.get(provider):
            self._send_json({"ok": False, "error": f"Turn off collection for {provider} first."}, 409)
            return
        # The settings capability must still hold when the purge is queued: the token may have been revoked
        # while the body was read. The lock is released before waiting on the ticket.
        provided_token = self.headers.get("X-Branchglance-Settings-Token")
        with _SETTINGS_TOKEN_LOCK:
            if not _settings_token_matches_locked(provided_token):
                self._send_json({"ok": False, "error": _SETTINGS_AUTH_MESSAGE}, 403)
                return
            ticket = service.request_purge(provider)
        if not ticket.wait(ledger_service.PURGE_WAIT_SECONDS):
            self._send_json({"ok": True, "pending": True, "ticket": ticket.id}, 202)
            return
        if ticket.status == "done":
            self._send_json({"ok": True, "ticket": ticket.id})
        elif ticket.status == "failed":
            self._send_json({"ok": False, "error": ticket.error or "failed", "ticket": ticket.id}, 500)
        else:
            self._send_json({"ok": False, "error": "Not deleted; Branchglance is closing.", "ticket": ticket.id}, 503)

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

    def _send_live_json(self, value, plain: bytes | None, compressed: bytes | None) -> None:
        gzip_ok, identity_ok = _live_encodings(self.headers.get("Accept-Encoding", ""))
        if gzip_ok and compressed is not None:
            payload, encoding = compressed, "gzip"
        elif identity_ok:
            payload, encoding = plain if plain is not None else json.dumps(value).encode("utf-8"), None
        else:
            self.send_response(406)
            self.send_header("Vary", "Accept-Encoding")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Vary", "Accept-Encoding")
        if encoding:
            self.send_header("Content-Encoding", encoding)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _reject_post(self, path: str, value, status: int) -> None:
        self.close_connection = True
        lengths = self.headers.get_all("Content-Length", [])
        limit = QUEUE_MESSAGE_LIMIT + 512 if path == "/api/queue" else 4096
        digits = lengths[0].lstrip("0") or "0" if len(lengths) == 1 else ""
        # Drain a small body before replying: closing with unread request data
        # makes the OS reset the connection, so the client may never see the status.
        if (path in {"/api/queue", "/api/reset-action", "/api/claude-collection", "/api/ledger/purge", "/api/quota",
                     "/api/claude-greeting-rearm", "/api/features", "/api/statusline"}
                and not self.headers.get_all("Transfer-Encoding", []) and len(lengths) == 1
                and re.fullmatch(r"[0-9]+", lengths[0]) and len(digits) <= len(str(limit))
                and int(digits) <= limit):
            remaining = int(digits)
            deadline = time.monotonic() + 0.25
            original_timeout = self.connection.gettimeout()
            try:
                while remaining:
                    timeout = deadline - time.monotonic()
                    if timeout <= 0:
                        break
                    self.connection.settimeout(timeout)
                    chunk = self.rfile.read1(remaining)
                    if not chunk:
                        break
                    remaining -= len(chunk)
            except OSError:
                pass
            finally:
                self.connection.settimeout(original_timeout)
        payload = json.dumps(value).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(payload)

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
    from .codex_client import resolve_codex, run_contained_codex
    executable = resolve_codex()
    if not executable:
        raise FileNotFoundError("codex")
    result = run_contained_codex(executable, ["queue", "--thread", thread, "--message", message],
                                 timeout=QUEUE_TIMEOUT_SECONDS)
    if result.returncode:
        raise subprocess.CalledProcessError(result.returncode, result.args)

def main() -> None:
    parser = argparse.ArgumentParser(description="Run the local Token Tracker 2a widget")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-open", action="store_true")
    args = parser.parse_args()
    from .claude_hook import start_event_retention
    start_event_retention()
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    from . import claude_usage_api
    claude_usage_api.REFRESHER.start()
    url = f"http://{args.host}:{args.port}"
    print(f"Token Tracker running at {url}")
    if not args.no_open:
        threading.Timer(0.4, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        claude_usage_api.REFRESHER.stop()
        server.server_close()


if __name__ == "__main__":
    main()
