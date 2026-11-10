"""One process-local authority for Claude credential and account identity."""
from __future__ import annotations

import json
import os
import stat
import threading
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


class IdentityAuthority:
    """Publish verified account pairs and fence work across credential epochs.

    Epochs are random and never persisted, so data restored after a restart is
    historical until verified again. The credential key is internal only.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._refresh_lock = threading.Lock()
        self._generation = uuid.uuid4().hex
        self._credential_key: object = None
        self._passive_key: object = None
        self._identity: Identity | None = None

    def _advance(self) -> None:
        self._generation = uuid.uuid4().hex
        self._identity = None

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

    def bind(self, generation: str, account_uuid: str, org_id: str, label: str = "") -> Identity | None:
        if not all(isinstance(value, str) and value.strip() for value in (account_uuid, org_id)):
            return None
        with self._lock:
            if generation != self._generation or (self._credential_key is None and self._passive_key is None):
                return None
            if self._identity and (self._identity.account_uuid, self._identity.org_id) != (account_uuid, org_id):
                self._advance()
            self._identity = Identity(account_uuid, org_id, self._generation, label)
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
            epoch = self._set_passive_key(("passive", os.path.abspath(path), account_id, org_id))
            return self.bind(epoch, account_id, org_id, label)

    def _set_passive_key(self, key: object) -> str:
        with self._lock:
            if key != self._passive_key:
                self._passive_key = key
                self._advance()
            return self._generation

    def capture(self) -> tuple[str, Identity | None]:
        with self._lock:
            return self._generation, self._identity

    @contextmanager
    def guard(self, generation: str) -> Iterator[Identity | None]:
        with self._lock:
            yield self._identity if generation == self._generation else None


AUTHORITY = IdentityAuthority()


def account_path() -> Path:
    config = os.environ.get("CLAUDE_CONFIG_DIR")
    return Path(config).expanduser() / ".claude.json" if config else Path.home() / ".claude.json"


def read_passive_account(path: Path) -> tuple[str, str, str] | None:
    """Bounded, no-follow read; the account and bytes come from the same handle."""
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
                fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent)
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
        account = data.get("oauthAccount") if isinstance(data, dict) else None
        if not isinstance(account, dict):
            return None
        account_id, org_id = account.get("accountUuid"), account.get("organizationUuid")
        if not all(isinstance(v, str) and v.strip() and len(v) <= 256 for v in (account_id, org_id)):
            return None
        email = account.get("emailAddress")
        label = email.strip()[:200] if isinstance(email, str) and email.strip() else account_id[:8]
        return account_id, org_id, label
    except (OSError, ValueError, TypeError, UnicodeError, RecursionError):
        return None
