"""Runtime feature switches, stored separately from the strict provider policy."""
from __future__ import annotations

import json
import os
import tempfile
import threading
from pathlib import Path

from . import product, claude_statusline

ENV = {"live_usage": "TOKEN_TRACKER_CLAUDE_LIVE_USAGE", "ledger": "TOKEN_TRACKER_LEDGER",
       "claude_send": "TOKEN_TRACKER_CLAUDE_SEND"}
_lock = threading.RLock()
_cache = {}
_send_at_start = None


def mark_send_start(value):
    global _send_at_start
    with _lock:
        if _send_at_start is None:
            _send_at_start = bool(value)


def _read(root):
    path = root / "features.json"
    try:
        # Avoid opening directory handles when the store does not exist yet.
        path.lstat()
        with claude_statusline._guard_directories(root):
            info = claude_statusline._check_path(path, directory=False)
            if info.st_size > 64 * 1024:
                return {}
            signature = (info.st_mtime_ns, info.st_size)
            cached = _cache.get(str(path))
            if cached and cached[0] == signature:
                return dict(cached[1])
            with claude_statusline._open_checked(path) as stream:
                doc = json.load(stream)
        if (not isinstance(doc, dict) or doc.get("schemaVersion") != 1 or
                set(doc) - ({"schemaVersion"} | set(ENV)) or
                any(type(doc[k]) is not bool for k in ENV if k in doc)):
            return {}
        values = {k: doc[k] for k in ENV if k in doc}
        _cache[str(path)] = (signature, values)
        return dict(values)
    except (OSError, ValueError, TypeError, RecursionError):
        _cache.pop(str(path), None)
        return {}


def state(name, *, root=None, environ=None):
    if name not in ENV:
        raise ValueError("unknown feature")
    root = Path(root) if root is not None else product.state_root()
    env = os.environ if environ is None else environ
    with _lock:
        persisted = _read(root).get(name)
        raw = env.get(ENV[name])
        locked = raw is not None
        # Invalid explicit values fail closed, just like an explicit denial.
        effective = ((raw == "1" if name == "ledger" else str(raw).strip().lower() in {"1", "true", "on"})
                     if locked else bool(persisted))
        return {"persisted": persisted, "effective": effective, "env_locked": locked,
                "restart_pending": bool(name == "claude_send" and _send_at_start is not None
                                        and effective != _send_at_start)}


def enabled(name, **kwargs):
    return state(name, **kwargs)["effective"]


def save(name, value, *, root=None, environ=None):
    if name not in ENV or type(value) is not bool:
        raise ValueError("invalid feature")
    root = Path(root) if root is not None else product.state_root()
    with _lock:
        if state(name, root=root, environ=environ)["env_locked"]:
            raise ValueError("feature is set by environment")
        values = _read(root)
        values[name] = value
        temp = None
        with claude_statusline._guard_directories(root, create=True):
            path = root / "features.json"
            claude_statusline._check_path(path, allow_missing=True, directory=False)
            try:
                with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=root, delete=False) as stream:
                    temp = stream.name
                    json.dump({"schemaVersion": 1, **values}, stream)
                os.replace(temp, path)
                temp = None
                _cache.pop(str(path), None)
            finally:
                if temp:
                    os.unlink(temp)
        return state(name, root=root, environ=environ)
