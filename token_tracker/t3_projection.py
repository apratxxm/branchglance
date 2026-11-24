"""Bounded T3 v2 projection primitives; callers own connection and trust checks.

The projection is display evidence only and must never be used to admit actions.
"""
from __future__ import annotations

import json
import hashlib
import re
import secrets
import sqlite3
import stat
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, NamedTuple

from . import claude_hook_install, product

_UUID = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\Z")
_PROVIDERS = {"claudeAgent": "Claude", "codex": "Codex"}
_PREFIX = "orchestration_v2_projection_"
_LOCK = threading.Lock()
_OWNER = threading.Lock()
_CACHE: dict[tuple[frozenset[str], str], dict[str, Any]] = {}
_ACTIVE_KEY = None
POLL_SECONDS = 1.0
DEADLINE_SECONDS = 0.5
MAX_UNCHANGED_SECONDS = 5.0
MAX_STALE_SECONDS = 60.0


class Projection(NamedTuple):
    edges: tuple[tuple[str, str], ...]
    titles: tuple[tuple[str, str], ...]
    digest: str
    scope: tuple[str, ...]
    epoch: str = ""
    seq: int = 0

    def overlay(self) -> dict[str, Any]:
        return {"epoch": self.epoch, "seq": self.seq, "edges": dict(self.edges),
                "titles": {f"Claude:{key}": value for key, value in self.titles}}


def _publish(edges=(), titles=None, *, scope=(), check=lambda: None) -> Projection:
    from .claude_provider import public_title
    choices: dict[str, set[str]] = {}
    for child, parent in edges:
        check()
        choices.setdefault(child, set()).add(parent)
    normalized = {}
    for child, parents in choices.items():
        check()
        if len(parents) == 1 and child not in parents:
            normalized[child] = next(iter(parents))
    cyclic = set()
    for start in normalized:
        check()
        path, seen, node = [], {}, start
        while node in normalized and node not in seen:
            check()
            seen[node] = len(path)
            path.append(node)
            node = normalized[node]
        if node in seen:
            cyclic.update(path[seen[node]:])
    clean_edges_list = []
    for child, parent in normalized.items():
        check()
        if child not in cyclic:
            clean_edges_list.append((child, parent))
    clean_edges = tuple(sorted(clean_edges_list))
    clean_titles_list = []
    for key, value in (titles or {}).items():
        check()
        clean = public_title(value)
        if clean:
            clean_titles_list.append((key, clean))
    clean_titles = tuple(sorted(clean_titles_list))
    scope = tuple(sorted(scope))
    data = json.dumps([scope, clean_edges, clean_titles], ensure_ascii=True, separators=(",", ":"))
    return Projection(clean_edges, clean_titles, hashlib.sha256(data.encode()).hexdigest(), scope)


def reset() -> None:
    global _ACTIVE_KEY
    with _LOCK:
        _CACHE.clear()
        _ACTIVE_KEY = None


def _entry(scope: frozenset[str], generation: str) -> dict[str, Any]:
    global _ACTIVE_KEY
    key = (scope, generation)
    with _LOCK:
        if key != _ACTIVE_KEY:
            _ACTIVE_KEY = key
            _CACHE.clear()
        if key not in _CACHE:
            initial = _publish(scope=scope)._replace(epoch=secrets.token_hex(16))
            _CACHE[key] = {"path": None, "at": None, "result": initial, "identity": None,
                           "bindings": {}, "stamps": None, "successful_at": None,
                           "checkpoint": None}
        return _CACHE[key]


def _set_result(cache: dict[str, Any], candidate: Projection) -> Projection:
    with _LOCK:
        previous = cache["result"]
        result = candidate._replace(epoch=previous.epoch,
                                    seq=previous.seq + (candidate.digest != previous.digest))
        cache["result"] = result
        return result


def empty(allowed: frozenset[str] | set[str], generation: str = "") -> Projection:
    return _entry(frozenset(allowed) & frozenset(_PROVIDERS.values()), generation)["result"]


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


def _checked_files(path: Path, *, check=lambda: None) -> list[tuple[Path, Any]]:
    """Match local-source redirection and identity checks; host links are display-only."""
    expected = Path.home() / ".t3" / "userdata"
    if path.parent != expected or path.name not in {"state.sqlite", "statev2.sqlite"}:
        raise OSError("unexpected T3 database path")
    claude_hook_install._validate_path_chain(path)
    directories = []
    for directory in (expected.parent, expected):
        check()
        info = directory.lstat()
        if product.info_is_reparse(info) or not stat.S_ISDIR(info.st_mode):
            raise OSError("redirected T3 directory")
        directories.append((directory, info))
    snapshots = directories
    for item in (path, Path(str(path) + "-wal"), Path(str(path) + "-shm")):
        check()
        try:
            before = item.lstat()
        except FileNotFoundError:
            if item == path:
                raise
            snapshots.append((item, None))
            continue
        if product.info_is_reparse(before) or not stat.S_ISREG(before.st_mode):
            raise OSError("redirected T3 file")
        snapshots.append((item, before))
    return snapshots


def _recheck(snapshots: list[tuple[Path, Any]], *, check=lambda: None) -> None:
    main = next((path for path, before in snapshots if before is not None and stat.S_ISREG(before.st_mode)
                 and not str(path).endswith(("-wal", "-shm"))), None)
    if main is not None:
        claude_hook_install._validate_path_chain(main)
    for path, before in snapshots:
        check()
        is_directory = before is not None and stat.S_ISDIR(before.st_mode)
        is_sidecar = str(path).endswith(("-wal", "-shm"))
        try:
            after = path.lstat()
        except FileNotFoundError:
            if is_sidecar:
                continue
            raise
        if (product.info_is_reparse(after)
                or not (stat.S_ISDIR(after.st_mode) if is_directory else stat.S_ISREG(after.st_mode))):
            raise OSError("T3 file changed during read")
        if not is_sidecar and (before is None or (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino)):
            raise OSError("T3 file changed during read")


def _connect(path: Path, *, live: bool = False, cancelled=lambda: False, deadline: float | None = None) -> sqlite3.Connection:
    # A dedicated connection has no application-registered functions, collations,
    # or virtual tables. T3 uses a built-in json_extract expression index.
    db = sqlite3.connect(path.absolute().as_uri() + "?mode=ro", uri=True, timeout=0.02 if live else 0.1)
    try:
        db.execute("PRAGMA trusted_schema=ON")
        deadline = deadline if deadline is not None else time.monotonic() + (0.05 if live else DEADLINE_SECONDS)
        db.set_progress_handler(lambda: int(cancelled() or time.monotonic() >= deadline), 1000)
    except BaseException:
        db.close()
        raise
    return db


def _read_v1(db: Any, allowed: frozenset[str] | set[str], *, check=lambda: None) -> tuple[list[tuple[str, str]], dict[str, str]] | None:
    if "Claude" not in allowed:
        return [], {}
    check()
    records = _seek(db, "SELECT CASE WHEN length(t.title)<=4096 THEN t.title END, "
                    "CASE WHEN length(r.resume_cursor_json)<=65536 THEN r.resume_cursor_json END FROM projection_threads t "
                    "JOIN provider_session_runtime r ON r.thread_id=t.thread_id "
                    "WHERE r.provider_name='claudeAgent' AND t.deleted_at IS NULL AND t.archived_at IS NULL",
                    (), 4096)
    if records is None:
        return None
    titles, seen, ambiguous = {}, set(), set()
    for title, raw in records:
        check()
        session = _payload(raw).get("resume")
        if not isinstance(session, str) or not session:
            continue
        if session in seen:
            ambiguous.add(session)
        elif isinstance(title, str):
            titles[session] = title
        seen.add(session)
    unique = {}
    for key, value in titles.items():
        check()
        if key not in ambiguous:
            unique[key] = value
    return [], unique


class _Deadline(Exception):
    pass


def poll(allowed: frozenset[str] | set[str], *, generation: str = "", path: Path | None = None,
         live: bool = False, cancelled=lambda: False) -> Projection:
    """Publish one immutable, consent-scoped generation; never wait for its owner."""
    scope = frozenset(allowed) & frozenset(_PROVIDERS.values())
    cache = _entry(scope, generation)
    if not scope:
        return cache["result"]
    started = time.monotonic()
    deadline = started + (0.05 if live else DEADLINE_SECONDS)
    def check():
        if cancelled() or time.monotonic() >= deadline:
            raise _Deadline()
    output = cache["result"]
    if cancelled():
        if cache["successful_at"] is not None and started - cache["successful_at"] >= MAX_STALE_SECONDS:
            cache.update(bindings={}, stamps=None, successful_at=None, checkpoint=None)
            output = _set_result(cache, _publish(scope=scope))
        return output
    if cache["at"] is not None and started - cache["at"] < POLL_SECONDS:
        return output
    if not _OWNER.acquire(blocking=False):
        if cache["successful_at"] is not None and started - cache["successful_at"] >= MAX_STALE_SECONDS:
            cache.update(bindings={}, stamps=None, successful_at=None, checkpoint=None)
            output = _set_result(cache, _publish(scope=scope))
        return output
    try:
        check()
        path = Path(path) if path is not None else database()
        if cache["path"] != path:
            cache.update(path=path, identity=None, bindings={}, stamps=None, successful_at=None, checkpoint=None)
            output = _set_result(cache, _publish(scope=scope))
        cache["at"] = started
        snapshots = _checked_files(path, check=check)
        check()
        info = next(info for item, info in snapshots if item == path)
        def stamp(item):
            return None if item is None else (item.st_dev, item.st_ino, item.st_size, item.st_mtime_ns)
        main = stamp(info)
        sidecars = [info for item, info in snapshots if str(item) in (str(path) + "-wal", str(path) + "-shm")]
        if len(sidecars) != 2 or any(item is None for item in sidecars):
            _recheck(snapshots, check=check)
            after = _checked_files(path, check=check)
            after_main = next(item_info for item, item_info in after if item == path)
            after_sidecars = [item_info for item, item_info in after if str(item) in (str(path) + "-wal", str(path) + "-shm")]
            check()
            if (cache["checkpoint"] is None or cache["checkpoint"] != main or stamp(after_main) != main
                    or len(sidecars) != 2 or any(item is not None for item in sidecars)
                    or len(after_sidecars) != 2 or any(item is not None for item in after_sidecars)):
                cache.update(bindings={}, stamps=None, successful_at=None, checkpoint=None)
                output = _set_result(cache, _publish(scope=scope))
            return output
        # SQLite may recreate a sidecar if T3 removes it between this check and open.
        cache["checkpoint"] = None
        identity = (info.st_dev, info.st_ino)
        if cache["identity"] is not None and cache["identity"] != identity:
            cache.update(bindings={}, stamps=None, successful_at=None)
            output = _set_result(cache, _publish(scope=scope))
        cache["identity"] = identity
        stamps = (main, stamp(sidecars[0]))
        success = cache["successful_at"]
        if success is not None and started - success >= MAX_STALE_SECONDS:
            cache.update(bindings={}, stamps=None, successful_at=None)
            output = _set_result(cache, _publish(scope=scope))
        if cache["successful_at"] is not None and stamps == cache["stamps"] and started - cache["successful_at"] < MAX_UNCHANGED_SECONDS:
            _recheck(snapshots, check=check)
            check()
            return output
        db = _connect(path, live=live, cancelled=cancelled, deadline=deadline)
        try:
            db.execute("BEGIN")
            since = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat(timespec="milliseconds").replace("+00:00", "Z")
            bindings: dict[str, tuple[str, str]] = {}
            result = (read_v2(db, since, allowed=scope, previous_bindings=cache["bindings"], bindings_out=bindings, check=check)
                      if path.name == "statev2.sqlite" else _read_v1(db, scope, check=check))
            db.rollback()
        finally:
            db.close()
        _recheck(snapshots, check=check)
        check()
        if result is not None:
            candidate = _publish(*result, scope=scope, check=check)
            check()
            after = _checked_files(path, check=check)
            after_main = next(item for item, item_info in after if item == path)
            after_info = next(item_info for item, item_info in after if item == after_main)
            after_wal = next(item_info for item, item_info in after if str(item) == str(path) + "-wal")
            checkpoint = main if (stamp(after_info) == main and stamp(after_wal) == stamp(sidecars[0])
                                  and sidecars[0].st_size == 0) else None
            check()
            output = _set_result(cache, candidate)
            cache.update(bindings=bindings, stamps=stamps, successful_at=time.monotonic(), checkpoint=checkpoint)
    except FileNotFoundError:
        cache.update(identity=None, bindings={}, stamps=None, successful_at=None, checkpoint=None)
        output = _set_result(cache, _publish(scope=scope))
    except (_Deadline, sqlite3.Error, OSError, ValueError, claude_hook_install.HookInstallError):
        if cache["successful_at"] is not None and time.monotonic() - cache["successful_at"] >= MAX_STALE_SECONDS:
            cache.update(bindings={}, stamps=None, successful_at=None, checkpoint=None)
            output = _set_result(cache, _publish(scope=scope))
    finally:
        _OWNER.release()
    return output


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
            bindings_out: dict[str, tuple[str, str]] | None = None, check=lambda: None
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
        check()
        plan = db.execute("EXPLAIN QUERY PLAN " + sql, args).fetchall()
        if not any("SEARCH " in str(row[3]).upper() for row in plan):
            raise ValueError("T3 projection requires indexed seeks")
    def seek(sql, args, cap):
        check()
        rows = _seek(db, sql, args, cap)
        check()
        return rows

    # Cap the entire indexed recent range before filtering: denied threads may
    # affect completeness, but only their count is returned to Python.
    check()
    recent_count = db.execute(f"SELECT count(*) FROM (SELECT 1 FROM {t} "
                              "WHERE deleted_at IS NULL AND archived_at IS NULL AND updated_at >= ? "
                              "ORDER BY updated_at DESC LIMIT ?)", (since, seed_cap + 1)).fetchone()[0]
    if recent_count > seed_cap:
        return None
    seed_sql = (f"SELECT thread_id FROM {t} AS seed "
                "WHERE deleted_at IS NULL AND archived_at IS NULL AND updated_at >= ? "
                f"AND (EXISTS (SELECT 1 FROM {p} b WHERE b.provider_thread_id=seed.active_provider_thread_id AND b.provider IN ({marks})) "
                f"OR EXISTS (SELECT 1 FROM {s} sub WHERE sub.child_thread_id=seed.thread_id AND sub.origin='app_owned' AND sub.provider IN ({marks}))) "
                "ORDER BY updated_at DESC")
    seed_args = (since, *permitted, *permitted)
    check()
    seed_plan = db.execute("EXPLAIN QUERY PLAN " + seed_sql + " LIMIT 1", seed_args).fetchall()
    if not any("SEARCH SEED " in str(row[3]).upper() for row in seed_plan):
        raise ValueError("T3 projection requires an indexed recent range")
    seeds = seek(seed_sql, seed_args, seed_cap)
    if seeds is None:
        return None

    def binding(key: Any) -> Any:
        if not isinstance(key, str) or not key or key.startswith("pending:"):
            return None
        rows = seek(f"SELECT thread_id, provider, provider_instance_id, "
                     f"CASE WHEN length(payload_json)<=65536 THEN payload_json END "
                     f"FROM {p} WHERE provider_thread_id = ? AND provider IN ({marks})", (key, *permitted), 1)
        return rows[0] if rows and len(rows) == 1 else None

    titles: dict[str, str] = {}
    title_seen: set[str] = set()
    ambiguous: set[str] = set()
    bindings: dict[str, tuple[str, str]] = {}
    seed_ids = set()
    for (thread,) in seeds:
        check()
        seed_ids.add(thread)
    candidates: list[tuple[str, str]] = []
    for (thread,) in seeds:
        check()
        current_rows = (seek(f"SELECT b.provider_thread_id FROM {t} th JOIN {p} b "
                             "ON b.provider_thread_id=th.active_provider_thread_id "
                             "WHERE th.thread_id=? AND b.provider='claudeAgent'", (thread,), 1)
                        if "Claude" in allowed else None)
        current = current_rows[0][0] if current_rows else None
        active = binding(current) if current else None
        if active and active[0] == thread and active[1] == "claudeAgent":
            session = _native(active[3])
            if session:
                # Fetch title only after the allowed Claude binding is known.
                title_rows = seek(f"SELECT CASE WHEN length(title)<=4096 THEN title END FROM {t} WHERE thread_id = ?", (thread,), 1)
                title = title_rows[0][0] if title_rows else None
                if session in title_seen:
                    ambiguous.add(session)
                elif isinstance(title, str):
                    titles[session] = title
                    bindings[session] = (thread, current)
                title_seen.add(session)
        subs = seek(f"SELECT sub.thread_id, sub.run_id, sub.parent_node_id, sub.provider, sub.provider_thread_id, "
                     f"sub.child_thread_id, sub.provider_instance_id FROM {s} sub "
                     f"WHERE sub.child_thread_id = ? AND sub.origin = 'app_owned' AND sub.provider IN ({marks}) "
                     f"AND EXISTS (SELECT 1 FROM {a} parent WHERE parent.thread_id=sub.thread_id AND parent.run_id=sub.run_id "
                     f"AND parent.root_node_id=sub.parent_node_id AND parent.provider IN ({marks}))", (thread, *permitted, *permitted), seek_cap)
        if subs is None:
            return None
        for parent_thread, run, node, provider, child_key, child_thread, instance in subs:
            check()
            child = binding(child_key)
            if (not isinstance(instance, str) or not instance or not child
                    or child_thread != thread or child[0] != thread or child[1] != provider
                    or child[2] != instance or provider not in _PROVIDERS):
                continue
            child_native = _native(child[3])
            if not child_native:
                continue
            parent_rows = seek(f"SELECT deleted_at, archived_at FROM {t} WHERE thread_id = ?",
                                (parent_thread,), 1)
            if not parent_rows or parent_rows[0] != (None, None):
                continue
            check()
            count = db.execute(f"SELECT count(*) FROM (SELECT 1 FROM {a} WHERE thread_id=? AND run_id=? AND root_node_id=? LIMIT ?)",
                               (parent_thread, run, node, seek_cap + 1)).fetchone()[0]
            if count > seek_cap:
                return None
            if count != 1:
                continue
            attempts = seek(f"SELECT provider, provider_thread_id, provider_instance_id, "
                            f"CASE WHEN length(payload_json)<=65536 THEN payload_json END, "
                            f"coalesce(length(payload_json)>65536, 0) "
                            f"FROM {a} WHERE thread_id = ? AND run_id = ? AND root_node_id = ? AND provider IN ({marks})",
                            (parent_thread, run, node, *permitted), seek_cap)
            if attempts is None:
                return None
            if len(attempts) != 1:
                continue
            parent_provider, parent_key, parent_instance, raw, oversized = attempts[0]
            if oversized:
                continue
            parent = binding(parent_key)
            if (not isinstance(parent_instance, str) or not parent_instance or not parent
                    or parent[0] != parent_thread or parent[1] != parent_provider
                    or parent[2] != parent_instance or parent_provider not in _PROVIDERS):
                continue
            parent_native = _native(parent[3])
            evidence = _payload(raw).get("nativeThreadId")
            if not parent_native or (evidence is not None and evidence != parent_native):
                continue
            check()
            denied = db.execute(f"SELECT EXISTS(SELECT 1 FROM {a} WHERE thread_id=? AND (provider IS NULL OR provider NOT IN ({marks})) LIMIT 1)",
                                (thread, *permitted)).fetchone()[0]
            if denied:
                continue
            child_attempts = seek(f"SELECT provider, provider_thread_id, provider_instance_id, "
                                  f"CASE WHEN length(payload_json)<=65536 THEN payload_json END, "
                                  f"coalesce(length(payload_json)>65536, 0) "
                                  f"FROM {a} WHERE thread_id = ? AND provider IN ({marks})", (thread, *permitted), seek_cap)
            if child_attempts is None:
                return None
            contradictory = False
            for ap, ak, ai, ar, oversized in child_attempts:
                check()
                if (oversized or ap != provider or ak != child_key or ai != instance
                        or (_payload(ar).get("nativeThreadId") not in (None, child_native))):
                    contradictory = True
                    break
            if contradictory:
                continue
            candidates.append((f"{_PROVIDERS[provider]}:{child_native}",
                               f"{_PROVIDERS[parent_provider]}:{parent_native}"))
    # A binding leaving the recent range is not evidence of deletion. Recheck
    # previously observed threads by primary key before clearing their titles.
    # T3 has no native-ID expression index; uniqueness is limited to this
    # bounded recent scan plus previously observed bindings, never a full scan.
    for session, (thread, old_key) in (previous_bindings or {}).items() if "Claude" in allowed else ():
        check()
        if thread in seed_ids:
            continue
        records = seek(f"SELECT b.provider_thread_id, th.deleted_at, th.archived_at FROM {t} th JOIN {p} b "
                       "ON b.provider_thread_id=th.active_provider_thread_id WHERE th.thread_id=? AND b.provider='claudeAgent'",
                        (thread,), 1)
        if not records:
            continue
        key, deleted, archived = records[0]
        if deleted is not None or archived is not None or key != old_key:
            continue
        row = binding(key)
        if not row or row[0] != thread or row[1] != "claudeAgent" or _native(row[3]) != session:
            continue
        title_rows = seek(f"SELECT CASE WHEN length(title)<=4096 THEN title END FROM {t} WHERE thread_id = ?", (thread,), 1)
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
        for key, value in bindings.items():
            check()
            if key not in ambiguous:
                bindings_out[key] = value
    clean_titles = {}
    for key, value in titles.items():
        check()
        if key not in ambiguous:
            clean_titles[key] = value
    return candidates, clean_titles
