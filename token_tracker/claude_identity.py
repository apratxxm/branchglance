"""One process-local authority for Claude credential and account identity."""
from __future__ import annotations

import json
import hashlib
import math
import os
import stat
import tempfile
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator


@dataclass(frozen=True)
class Identity:
    account_uuid: str
    org_id: str
    generation: str
    label: str = field(default="", repr=False, compare=False)
    fingerprint: str = ""
    changed_at: float = 0.0


class IdentityAuthority:
    """Publish verified account pairs and fence work across credential epochs.

    Epochs are random and never persisted, so data restored after a restart is
    historical until verified again. The credential key is internal only.
    """

    def __init__(self, state_path=None, *, clock=time.time) -> None:
        self._lock = threading.RLock()
        self._refresh_lock = threading.Lock()
        self._generation = uuid.uuid4().hex
        self._credential_key: object = None
        self._passive_key: object = None
        self._identity: Identity | None = None
        # Last consistent (generation, identity) pair, replaced atomically under
        # the lock so a reader that finds the lock busy can serve it unlocked.
        self._published: tuple[str, Identity | None] = (self._generation, None)
        self._state_path = state_path
        self._clock = clock
        self._history: tuple[str, float] | None = None

    def _advance(self) -> None:
        self._generation = uuid.uuid4().hex
        self._identity = None
        self._published = (self._generation, None)

    def credential_changed(self, key: object) -> str:
        with self._lock:
            if key != self._credential_key:
                self._credential_key = key
                self._advance()
            return self._generation

    def invalidate(self, *, expected_generation: str | None = None) -> str:
        with self._lock:
            if expected_generation is not None and expected_generation != self._generation:
                return self._generation
            self._credential_key = None
            self._advance()
            return self._generation

    def bind(self, generation: str, account_uuid: str, org_id: str, label: str = "", *, fingerprint: str = "", changed_at: float = 0.0) -> Identity | None:
        if not all(isinstance(value, str) and value.strip() for value in (account_uuid, org_id)):
            return None
        with self._lock:
            if generation != self._generation or (self._credential_key is None and self._passive_key is None):
                return None
            if self._identity and (self._identity.account_uuid, self._identity.org_id) != (account_uuid, org_id):
                self._advance()
            self._identity = Identity(account_uuid, org_id, self._generation, label, fingerprint, changed_at)
            self._published = (self._generation, self._identity)
            return self._identity

    def refresh_passive(self, path: Path | None = None, *, other: tuple[str, str] | None = None) -> Identity | None:
        """Read the selected account without reading Claude's credentials file."""
        path = Path(path) if path is not None else account_path()
        with self._refresh_lock:
            account = read_passive_account(path)
            verified_other = (other if isinstance(other, tuple) and len(other) == 2
                              and all(isinstance(v, str) and v.strip() for v in other) else None)
            if account is not None and verified_other is not None and verified_other != account[:2]:
                self._set_passive_key(("unavailable", os.path.abspath(path)))
                return None
            if account is None and verified_other is not None:
                account = (*verified_other, verified_other[0][:8])
            if account is None:
                self._set_passive_key(("unavailable", os.path.abspath(path)))
                return None
            account_id, org_id, label = account
            fingerprint = account_fingerprint(account_id, org_id)
            # Fence old work as soon as the account change is observed, before
            # durable I/O can delay publication of the new identity.
            epoch = self._set_passive_key(("passive", os.path.abspath(path), account_id, org_id))
            try:
                changed_at = self._account_changed_at(fingerprint)
            except (OSError, ValueError, TypeError):
                self._set_passive_key(("unavailable", os.path.abspath(path)))
                return None
            return self.bind(epoch, account_id, org_id, label, fingerprint=fingerprint, changed_at=changed_at)

    def _account_changed_at(self, fingerprint: str) -> float:
        from . import claude_statusline
        state_path = self._state_path() if callable(self._state_path) else self._state_path
        previous = self._history
        if previous is None and state_path is not None:
            try:
                with claude_statusline._guard_directories(Path(state_path).parent), claude_statusline._open_checked(Path(state_path)) as stream:
                    raw = stream.read(4097)
                document = json.loads(raw) if len(raw) <= 4096 else None
                stamp = document.get("changedAt") if isinstance(document, dict) else None
                saved = document.get("fingerprint") if isinstance(document, dict) else None
                if (isinstance(saved, str) and len(saved) == 64 and all(c in "0123456789abcdef" for c in saved)
                        and type(stamp) in (int, float) and math.isfinite(stamp) and 0 <= stamp <= self._clock()):
                    previous = (saved, float(stamp))
            except (OSError, ValueError, TypeError, OverflowError, RecursionError):
                previous = None
        if previous and previous[0] == fingerprint:
            self._history = previous
            return previous[1]
        now = self._clock()
        if state_path is not None:
            path = Path(state_path)
            with claude_statusline._guard_directories(path.parent, create=True):
                claude_statusline._check_path(path, allow_missing=True, directory=False)
                temp = None
                try:
                    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, prefix=".identity-", delete=False) as stream:
                        temp = stream.name
                        json.dump({"fingerprint": fingerprint, "changedAt": now}, stream, separators=(",", ":"))
                        stream.flush()
                        os.fsync(stream.fileno())
                    os.replace(temp, path)
                    temp = None
                finally:
                    if temp:
                        try:
                            os.unlink(temp)
                        except OSError:
                            pass
        self._history = (fingerprint, now)
        return now

    def _set_passive_key(self, key: object) -> str:
        with self._lock:
            if key != self._passive_key:
                self._passive_key = key
                self._advance()
            return self._generation

    def capture(self) -> tuple[str, Identity | None]:
        with self._lock:
            return self._generation, self._identity

    def capture_nonblocking(self) -> tuple[str | None, Identity | None]:
        if not self._lock.acquire(blocking=False):
            # Busy (e.g. snapshot publication holds guard()): a busy lock is not
            # an account change, so serve the last consistent pair.
            return self._published
        try:
            return self._generation, self._identity
        finally:
            self._lock.release()

    @contextmanager
    def guard(self, generation: str) -> Iterator[Identity | None]:
        with self._lock:
            yield self._identity if generation == self._generation else None


def account_fingerprint(account_uuid: str, org_id: str) -> str:
    payload = json.dumps([account_uuid, org_id], ensure_ascii=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _identity_state_path() -> Path:
    from . import product
    return product.state_root() / "claude-identity.json"


AUTHORITY = IdentityAuthority(state_path=_identity_state_path)


def current_identity() -> Identity | None:
    """Refresh the passive account before accepting data or authorizing work."""
    return AUTHORITY.refresh_passive()


def matches(identity: Identity | None, generation: object, org_id: object) -> bool:
    return (identity is not None and isinstance(generation, str) and bool(generation)
            and generation == identity.generation and isinstance(org_id, str)
            and bool(org_id.strip()) and org_id == identity.org_id)


def matches_fingerprint(identity: Identity | None, fingerprint: object) -> bool:
    return bool(identity and identity.fingerprint and isinstance(fingerprint, str)
                and fingerprint == identity.fingerprint)


def account_path() -> Path:
    config = os.environ.get("CLAUDE_CONFIG_DIR")
    return Path(config).expanduser() / ".claude.json" if config else Path.home() / ".claude.json"


def read_passive_account(path: Path) -> tuple[str, str, str] | None:
    """Bounded, no-follow read; the account and bytes come from the same handle."""
    data = _read_passive_document(path)
    account = data.get("oauthAccount") if isinstance(data, dict) else None
    if not isinstance(account, dict):
        return None
    account_id, org_id = account.get("accountUuid"), account.get("organizationUuid")
    if not all(isinstance(v, str) and v.strip() and len(v) <= 256 for v in (account_id, org_id)):
        return None
    email = account.get("emailAddress")
    label = email.strip()[:200] if isinstance(email, str) and email.strip() else account_id[:8]
    return account_id, org_id, label


def read_claude_code_usage(identity: Identity | None, path: Path | None = None, *, now: float | None = None) -> dict:
    """Limits Claude Code caches in its config after its own usage fetch (display only).

    Every Claude Code surface (CLI, IDE, Agent SDK) refreshes this cache while it
    runs, so it works without a status line. The cache names its account, and the
    selected account in the same document must be the verified identity. The cache
    names no organization, so a fetch older than the current selection is ignored.
    """
    from . import claude_statusline
    if identity is None:
        return {}
    data = _read_passive_document(Path(path) if path is not None else account_path())
    if not isinstance(data, dict):
        return {}
    account, cached = data.get("oauthAccount"), data.get("cachedUsageUtilization")
    if not isinstance(account, dict) or not isinstance(cached, dict):
        return {}
    if (account.get("accountUuid"), account.get("organizationUuid")) != (identity.account_uuid, identity.org_id):
        return {}
    if cached.get("accountUuid") != identity.account_uuid:
        return {}
    fetched = cached.get("fetchedAtMs")
    if type(fetched) not in (int, float) or not math.isfinite(fetched) or fetched <= 0:
        return {}
    observed = fetched / 1000
    now = time.time() if now is None else now
    if observed > now + 60:
        return {}
    if observed < identity.changed_at:
        # Fetched before this account/org became selected (the cache names no org),
        # so it is not current evidence; wait for Claude Code's next fetch.
        return {}
    windows = claude_statusline.parse_live_usage(cached.get("utilization"), observed_at=observed)
    return {name: {"used_percentage": window["used_percentage"], "resets_at": window["resets_at"], "receivedAt": observed}
            for name, window in windows.items() if window.get("crossCheckPassed")}


def _read_passive_document(path: Path) -> dict | None:
    from . import claude_statusline
    path = Path(os.path.abspath(path))
    try:
        if os.name == "nt":
            import msvcrt
            with claude_statusline._guard_directories(path.parent):
                handle = claude_statusline._windows_open(path, 0x80000000, 7, 3)
                try:
                    fd = msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)
                except BaseException:
                    import ctypes
                    ctypes.windll.kernel32.CloseHandle(ctypes.c_void_p(handle))
                    raise
                with os.fdopen(fd, "rb") as stream:
                    raw = stream.read(1024 * 1024 + 1)
        else:
            if not hasattr(os, "O_NOFOLLOW") or not hasattr(os, "O_DIRECTORY"):
                return None
            parts = path.parts
            fds = []
            try:
                parent = os.open(parts[0], os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
                fds.append(parent)
                for component in parts[1:-1]:
                    parent = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
                    fds.append(parent)
                fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
                with os.fdopen(fd, "rb") as stream:
                    info = os.fstat(stream.fileno())
                    if not stat.S_ISREG(info.st_mode):
                        return None
                    raw = stream.read(1024 * 1024 + 1)
            finally:
                for fd in reversed(fds):
                    os.close(fd)
        if len(raw) > 1024 * 1024:
            return None
        data = json.loads(raw)
        return data if isinstance(data, dict) else None
    except (OSError, ValueError, TypeError, UnicodeError, RecursionError):
        return None
