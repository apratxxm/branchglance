"""Bounded T3 v2 projection primitives; callers own connection and trust checks.

The projection is display evidence only and must never be used to admit actions.
"""
from __future__ import annotations

import json
import hashlib
import os
import re
import sqlite3
import stat
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, NamedTuple

from . import claude_collection, claude_hook_install

_UUID = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\Z")
_PROVIDERS = {"claudeAgent": "Claude", "codex": "Codex"}
_PREFIX = "orchestration_v2_projection_"
_LOCK = threading.Lock()
_OWNER = threading.Lock()
_CACHE: dict[frozenset[str], dict[str, Any]] = {}
POLL_SECONDS = 1.0
DEADLINE_SECONDS = 0.5
MAX_UNCHANGED_SECONDS = 5.0
MAX_STALE_SECONDS = 60.0


class Projection(NamedTuple):
    edges: tuple[tuple[str, str], ...]
    titles: tuple[tuple[str, str], ...]
    digest: str
    scope: tuple[str, ...]


def _publish(edges=(), titles=None, *, scope=()) -> Projection:
    from .claude_provider import public_title
    choices: dict[str, set[str]] = {}
    for child, parent in edges:
        choices.setdefault(child, set()).add(parent)
    normalized = {child: next(iter(parents)) for child, parents in choices.items()
                  if len(parents) == 1 and child not in parents}
    cyclic = set()
    for start in normalized:
        path, seen, node = [], {}, start
        while node in normalized and node not in seen:
            seen[node] = len(path)
            path.append(node)
            node = normalized[node]
        if node in seen:
            cyclic.update(path[seen[node]:])
    clean_edges = tuple(sorted((child, parent) for child, parent in normalized.items() if child not in cyclic))
    clean_titles = tuple(sorted((key, clean) for key, value in (titles or {}).items()
                                if (clean := public_title(value))))
    scope = tuple(sorted(scope))
    data = json.dumps([scope, clean_edges, clean_titles], ensure_ascii=True, separators=(",", ":"))
    return Projection(clean_edges, clean_titles, hashlib.sha256(data.encode()).hexdigest(), scope)


EMPTY = _publish()


def reset() -> None:
    with _LOCK:
        _CACHE.clear()


def database() -> Path:
    root = Path.home() / ".t3" / "userdata"
    v2 = root / "statev2.sqlite"
    try:
        v2.lstat()
    except FileNotFoundError:
        return root / "state.sqlite"
    except OSError:
        pass  # An inaccessible v2 is not proof that it is absent.
    return v2


def _checked_files(path: Path) -> list[tuple[Path, Any]]:
    """Match local-source redirection and identity checks; host links are display-only."""
    expected = Path.home() / ".t3" / "userdata"
    if path.parent != expected or path.name not in {"state.sqlite", "statev2.sqlite"}:
        raise OSError("unexpected T3 database path")
    claude_hook_install._validate_path_chain(path)
    directories = []
    for directory in (expected.parent, expected):
        info = directory.lstat()
        if claude_collection._info_is_reparse(info) or not stat.S_ISDIR(info.st_mode):
            raise OSError("redirected T3 directory")
        directories.append((directory, info))
    snapshots = directories
    for item in (path, Path(str(path) + "-wal"), Path(str(path) + "-shm")):
        try:
            before = item.lstat()
        except FileNotFoundError:
            if item == path:
                raise
            snapshots.append((item, None))
            continue
        if claude_collection._info_is_reparse(before) or not stat.S_ISREG(before.st_mode):
            raise OSError("redirected T3 file")
        snapshots.append((item, before))
    return snapshots


def _recheck(snapshots: list[tuple[Path, Any]], *, immutable: bool = False) -> None:
    main = next((path for path, before in snapshots if before is not None and stat.S_ISREG(before.st_mode)
                 and not str(path).endswith(("-wal", "-shm"))), None)
    if main is not None:
        claude_hook_install._validate_path_chain(main)
    for path, before in snapshots:
        is_directory = before is not None and stat.S_ISDIR(before.st_mode)
        is_sidecar = str(path).endswith(("-wal", "-shm"))
        try:
            after = path.lstat()
        except FileNotFoundError:
            if is_sidecar:
                continue
            raise
        if (claude_collection._info_is_reparse(after)
                or not (stat.S_ISDIR(after.st_mode) if is_directory else stat.S_ISREG(after.st_mode))):
            raise OSError("T3 file changed during read")
        if not is_sidecar and (before is None or (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino)):
            raise OSError("T3 file changed during read")
        if is_sidecar and immutable:
            raise OSError("T3 sidecar appeared during immutable read")


def _connect(path: Path, *, immutable: bool = False, live: bool = False, cancelled=lambda: False) -> sqlite3.Connection:
    # A dedicated connection has no application-registered functions, collations,
    # or virtual tables. T3 uses a built-in json_extract expression index.
    db = sqlite3.connect(path.absolute().as_uri() + "?mode=ro" + ("&immutable=1" if immutable else ""), uri=True, timeout=0.02 if live else 0.1)
    db.execute("PRAGMA trusted_schema=ON")
    deadline = time.monotonic() + (0.05 if live else DEADLINE_SECONDS)
    db.set_progress_handler(lambda: int(cancelled() or time.monotonic() >= deadline), 1000)
    return db


def _read_v1(db: Any, allowed: frozenset[str] | set[str]) -> tuple[list[tuple[str, str]], dict[str, str]] | None:
    if "Claude" not in allowed:
        return [], {}
    records = _seek(db, "SELECT t.title, r.resume_cursor_json FROM projection_threads t "
                    "JOIN provider_session_runtime r ON r.thread_id=t.thread_id "
                    "WHERE r.provider_name='claudeAgent' AND t.deleted_at IS NULL AND t.archived_at IS NULL",
                    (), 4096)
    if records is None:
        return None
    titles, seen, ambiguous = {}, set(), set()
    for title, raw in records:
        session = _payload(raw).get("resume")
        if not isinstance(session, str) or not session:
            continue
        if session in seen:
            ambiguous.add(session)
        elif isinstance(title, str):
            titles[session] = title
        seen.add(session)
    return [], {key: value for key, value in titles.items() if key not in ambiguous}


def poll(allowed: frozenset[str] | set[str], *, path: Path | None = None,
         live: bool = False, cancelled=lambda: False) -> Projection:
    """Publish one immutable, consent-scoped generation; never wait for its owner."""
    scope = frozenset(allowed) & frozenset(_PROVIDERS.values())
    if not scope or cancelled():
        return EMPTY
    now = time.monotonic()
    scoped_empty = _publish(scope=scope)
    with _LOCK:
        cache = _CACHE.setdefault(scope, {"path": None, "at": None, "result": scoped_empty,
                                          "identity": None, "bindings": {}, "stamps": None,
                                          "successful_at": None})
        success = cache["successful_at"]
        if success is not None and now - success >= MAX_STALE_SECONDS:
            cache.update(result=scoped_empty, bindings={}, stamps=None, successful_at=None)
        output = cache["result"]
        if cache["at"] is not None and now - cache["at"] < POLL_SECONDS:
            return output
    if not _OWNER.acquire(blocking=False):
        return output
    try:
        if cancelled():
            return output
        path = Path(path) if path is not None else database()
        with _LOCK:
            if cache["path"] != path:
                cache.update(path=path, result=scoped_empty, identity=None, bindings={}, stamps=None, successful_at=None)
            cache["at"] = now
            output = cache["result"]
        try:
            snapshots = _checked_files(path)
            info = next(info for item, info in snapshots if item == path)
            identity = (info.st_dev, info.st_ino)
            if cache["identity"] is not None and cache["identity"] != identity:
                cache.update(result=scoped_empty, bindings={}, stamps=None, successful_at=None)
                output = scoped_empty
            cache["identity"] = identity
            def stamp(item):
                return None if item is None else (item.st_dev, item.st_ino, item.st_size, item.st_mtime_ns)
            sidecars = [info for item, info in snapshots if str(item) in (str(path) + "-wal", str(path) + "-shm")]
            immutable = len(sidecars) != 2 or any(info is None for info in sidecars)
            stamps = tuple(stamp(next((info for item, info in snapshots if item == target), None))
                           for target in (path, Path(str(path) + "-wal")))
            if (cache["successful_at"] is not None and stamps == cache["stamps"]
                    and now - cache["successful_at"] < MAX_UNCHANGED_SECONDS):
                _recheck(snapshots, immutable=immutable)
                return output
            db = _connect(path, immutable=immutable, live=live, cancelled=cancelled)
            try:
                db.execute("BEGIN")
                since = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat(timespec="milliseconds").replace("+00:00", "Z")
                bindings: dict[str, tuple[str, str]] = {}
                result = (read_v2(db, since, allowed=scope, previous_bindings=cache["bindings"], bindings_out=bindings)
                          if path.name == "statev2.sqlite" else _read_v1(db, scope))
                db.rollback()
            finally:
                db.close()
            _recheck(snapshots, immutable=immutable)
            if result is not None and not cancelled():
                output = _publish(*result, scope=scope)
                with _LOCK:
                    cache.update(result=output, bindings=bindings, stamps=stamps, successful_at=time.monotonic())
        except FileNotFoundError:
            with _LOCK:
                cache.update(result=scoped_empty, identity=None, bindings={}, stamps=None, successful_at=None)
            output = scoped_empty
        except (sqlite3.Error, OSError, ValueError, claude_hook_install.HookInstallError):
            pass
        return output
    finally:
        _OWNER.release()


def _payload(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, str) or len(raw) > 65536:
        return {}
    try:
        value = json.loads(raw)
        return value if isinstance(value, dict) else {}
    except (ValueError, TypeError, RecursionError):
        return {}


def _native(raw: Any) -> str | None:
    ref = _payload(raw).get("nativeThreadRef")
    native = ref.get("nativeId") if isinstance(ref, dict) else None
    return native if isinstance(native, str) and _UUID.fullmatch(native) else None


def _seek(db: Any, sql: str, args: tuple[Any, ...], cap: int) -> list[Any] | None:
    rows = db.execute(sql + " LIMIT ?", (*args, cap + 1)).fetchall()
    return rows if len(rows) <= cap else None


def read_v2(db: Any, since: str, *, allowed: frozenset[str] | set[str], seed_cap: int = 2048, seek_cap: int = 32,
            previous_bindings: dict[str, tuple[str, str]] | None = None,
            bindings_out: dict[str, tuple[str, str]] | None = None
            ) -> tuple[list[tuple[str, str]], dict[str, str]] | None:
    """Read one bounded recent projection from an already-open transaction.

    None denotes incomplete coverage. Query errors are left to the caller so
    it can distinguish unavailability from a complete empty result.
    """
    if not allowed:
        return [], {}
    if seed_cap < 1 or seek_cap < 1:
        raise ValueError("caps must be positive")
    if len(previous_bindings or {}) > seed_cap:
        return None
    t, s, a, p = (_PREFIX + name for name in ("threads", "subagents", "run_attempts", "provider_threads"))
    permitted = tuple(provider for provider, display in _PROVIDERS.items() if display in allowed)
    if not permitted:
        return [], {}
    marks = ",".join("?" for _ in permitted)
    plans = (
        (f"SELECT thread_id FROM {t} WHERE deleted_at IS NULL AND archived_at IS NULL AND updated_at >= ? ORDER BY updated_at DESC LIMIT 1", (since,)),
        (f"SELECT thread_id FROM {s} WHERE child_thread_id = ? LIMIT 1", ("",)),
        (f"SELECT provider FROM {a} WHERE thread_id = ? AND run_id = ? LIMIT 1", ("", "")),
        (f"SELECT provider FROM {a} WHERE thread_id = ? LIMIT 1", ("",)),
        (f"SELECT provider FROM {p} WHERE provider_thread_id = ? LIMIT 1", ("",)),
        (f"SELECT thread_id FROM {t} WHERE thread_id = ? LIMIT 1", ("",)),
    )
    for sql, args in plans:
        plan = db.execute("EXPLAIN QUERY PLAN " + sql, args).fetchall()
        if not any("SEARCH " in str(row[3]).upper() for row in plan):
            raise ValueError("T3 projection requires indexed seeks")
    seeds = _seek(db, f"SELECT thread_id, active_provider_thread_id FROM {t} "
                  "WHERE deleted_at IS NULL AND archived_at IS NULL AND updated_at >= ? "
                  "ORDER BY updated_at DESC", (since,), seed_cap)
    if seeds is None:
        return None

    def binding(key: Any) -> Any:
        if not isinstance(key, str) or not key or key.startswith("pending:"):
            return None
        rows = _seek(db, f"SELECT thread_id, provider, provider_instance_id, payload_json "
                     f"FROM {p} WHERE provider_thread_id = ? AND provider IN ({marks})", (key, *permitted), 1)
        return rows[0] if rows and len(rows) == 1 else None

    titles: dict[str, str] = {}
    title_seen: set[str] = set()
    ambiguous: set[str] = set()
    bindings: dict[str, tuple[str, str]] = {}
    seed_ids = {thread for thread, _ in seeds}
    candidates: list[tuple[str, str]] = []
    for thread, current in seeds:
        active = binding(current) if "Claude" in allowed else None
        if active and active[0] == thread and active[1] == "claudeAgent":
            session = _native(active[3])
            if session:
                # Fetch title only after the allowed Claude binding is known.
                title_rows = _seek(db, f"SELECT title FROM {t} WHERE thread_id = ?", (thread,), 1)
                title = title_rows[0][0] if title_rows else None
                if session in title_seen:
                    ambiguous.add(session)
                elif isinstance(title, str):
                    titles[session] = title
                    bindings[session] = (thread, current)
                title_seen.add(session)
        subs = _seek(db, f"SELECT thread_id, run_id, parent_node_id, provider, provider_thread_id, "
                     f"child_thread_id, provider_instance_id FROM {s} "
                     f"WHERE child_thread_id = ? AND origin = 'app_owned' AND provider IN ({marks})", (thread, *permitted), seek_cap)
        if subs is None:
            return None
        for parent_thread, run, node, provider, child_key, child_thread, instance in subs:
            child = binding(child_key)
            if (not isinstance(instance, str) or not instance or not child
                    or child_thread != thread or child[0] != thread or child[1] != provider
                    or child[2] != instance or provider not in _PROVIDERS):
                continue
            child_native = _native(child[3])
            if not child_native:
                continue
            parent_rows = _seek(db, f"SELECT deleted_at, archived_at FROM {t} WHERE thread_id = ?",
                                (parent_thread,), 1)
            if not parent_rows or parent_rows[0] != (None, None):
                continue
            attempts = _seek(db, f"SELECT provider, provider_thread_id, provider_instance_id, CASE WHEN provider IN ({marks}) THEN payload_json END "
                             f"FROM {a} WHERE thread_id = ? AND run_id = ? AND root_node_id = ?",
                             (*permitted, parent_thread, run, node), seek_cap)
            if attempts is None:
                return None
            if len(attempts) != 1:
                continue
            parent_provider, parent_key, parent_instance, raw = attempts[0]
            parent = binding(parent_key)
            if (not isinstance(parent_instance, str) or not parent_instance or not parent
                    or parent[0] != parent_thread or parent[1] != parent_provider
                    or parent[2] != parent_instance or parent_provider not in _PROVIDERS):
                continue
            parent_native = _native(parent[3])
            evidence = _payload(raw).get("nativeThreadId")
            if not parent_native or (evidence is not None and evidence != parent_native):
                continue
            child_attempts = _seek(db, f"SELECT provider, provider_thread_id, provider_instance_id, CASE WHEN provider IN ({marks}) THEN payload_json END "
                                   f"FROM {a} WHERE thread_id = ?", (*permitted, thread), seek_cap)
            if child_attempts is None:
                return None
            if any(ap != provider or ak != child_key or ai != instance
                   or (_payload(ar).get("nativeThreadId") not in (None, child_native))
                   for ap, ak, ai, ar in child_attempts):
                continue
            candidates.append((f"{_PROVIDERS[provider]}:{child_native}",
                               f"{_PROVIDERS[parent_provider]}:{parent_native}"))
    # A binding leaving the recent range is not evidence of deletion. Recheck
    # previously observed threads by primary key before clearing their titles.
    # T3 has no native-ID expression index; uniqueness is limited to this
    # bounded recent scan plus previously observed bindings, never a full scan.
    for session, (thread, old_key) in (previous_bindings or {}).items() if "Claude" in allowed else ():
        if thread in seed_ids:
            continue
        records = _seek(db, f"SELECT active_provider_thread_id, deleted_at, archived_at FROM {t} WHERE thread_id = ?",
                        (thread,), 1)
        if not records:
            continue
        key, deleted, archived = records[0]
        if deleted is not None or archived is not None or key != old_key:
            continue
        row = binding(key)
        if not row or row[0] != thread or row[1] != "claudeAgent" or _native(row[3]) != session:
            continue
        title_rows = _seek(db, f"SELECT title FROM {t} WHERE thread_id = ?", (thread,), 1)
        title = title_rows[0][0] if title_rows else None
        if session in title_seen:
            ambiguous.add(session)
        elif isinstance(title, str):
            titles[session] = title
            bindings[session] = (thread, key)
        title_seen.add(session)
    if len(bindings) > seed_cap:
        return None
    if bindings_out is not None:
        bindings_out.update({key: value for key, value in bindings.items() if key not in ambiguous})
    return candidates, {key: value for key, value in titles.items() if key not in ambiguous}
