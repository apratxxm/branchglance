from __future__ import annotations

import functools
import json
import hashlib
import math
import os
import re
import sqlite3
import sys
import threading
import time
from copy import deepcopy
from collections import OrderedDict, defaultdict
from contextlib import closing, contextmanager
from contextvars import ContextVar
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

from .codex_client import CodexAppServer, CodexUnavailable
from .claude_cache import reset_info, usage_info

CLAUDE_DISPLAY_USAGE_AGE_SECONDS = 600  # matches the widget's quota freshness (app.js resetView)
from .provider_policy import POLICIES, policy_for, public_policies

# Full transcript collection is intentionally limited to providers listed here.
# Claude is full-collected (hook-sourced sessions) only when its policy allows
# collection; otherwise it stays quota-only via LIMIT_ONLY_PROVIDERS.
ENABLED_PROVIDERS = (
    "Codex",
    "Claude",
)
LIMIT_ONLY_PROVIDERS = ("Claude",)


def _iso_from_ms(value: int | float | None) -> str | None:
    if not value:
        return None
    return datetime.fromtimestamp(float(value) / 1000, timezone.utc).isoformat()


def _codex_sqlite_identity(record: Any) -> dict[str, Any]:
    """Project a desktop-index row to identity and archive membership only."""
    raw_source = record["source"]
    try:
        raw_source = json.loads(raw_source)
    except (TypeError, ValueError):
        pass
    sub = raw_source.get("subagent", raw_source.get("subAgent")) if isinstance(raw_source, dict) else None
    spawn = sub.get("thread_spawn") or {} if isinstance(sub, dict) else {}
    parent = spawn.get("parent_thread_id")
    agent_path = record["agent_path"] or spawn.get("agent_path")
    if isinstance(raw_source, str):
        source = raw_source if raw_source.lower() in {"cli", "exec", "vscode", "appserver", "app_server", "mcp"} else None
    elif isinstance(sub, dict):
        safe_sub = {}
        if isinstance(parent, str) and parent:
            safe_sub["thread_spawn"] = {"parent_thread_id": parent}
            if isinstance(spawn.get("agent_path"), str) and spawn["agent_path"]:
                safe_sub["thread_spawn"]["agent_path"] = spawn["agent_path"]
        if sub.get("other") == "guardian":
            safe_sub["other"] = "guardian"
        source = {"subagent": safe_sub}
    else:
        source = None
    return {
        "id": record["id"], "path": record["rollout_path"], "source": source,
        "cwd": record["cwd"], "name": record["name"] or agent_path or record["title"],
        "threadName": record["name"], "title": record["title"], "agentPath": agent_path,
        "parentThreadId": parent, "archived": bool(record["archived"]),
    }


def _sqlite_variable_limit(connection: Any) -> int:
    """Use the active SQLite bind-variable limit for bounded ID batches."""
    getter = getattr(connection, "getlimit", None)
    category = getattr(sqlite3, "SQLITE_LIMIT_VARIABLE_NUMBER", None)
    if getter is not None and category is not None:
        try:
            value = int(getter(category))
            if value > 0:
                return value
        except (AttributeError, sqlite3.Error, TypeError, ValueError):
            pass
    try:
        for row in connection.execute("PRAGMA compile_options"):
            option = row[0]
            if isinstance(option, str) and option.startswith("MAX_VARIABLE_NUMBER="):
                value = int(option.partition("=")[2])
                if value > 0:
                    return value
    except (sqlite3.Error, TypeError, ValueError):
        pass
    # SQLite's historical default when the build does not report an override.
    return 999


def _codex_identity_batch(connection: Any, ids: list[str]) -> list[Any]:
    marks = ",".join("?" for _ in ids)
    return connection.execute(
        "SELECT id, rollout_path, source, cwd, name, title, agent_path, archived "
        f"FROM threads WHERE id IN ({marks})", ids,
    ).fetchall()


def _codex_identity_snapshot(ids: Iterable[Any] = (), limit: int = 200) -> dict[str, Any]:
    """Read the bounded recent catalog and requested identity memberships consistently.

    This reads no live status or model signal. ``no_match_ids`` means a completed
    lookup found no row; ``failed_ids`` means a batch could not be read.
    """
    requested = {str(value) for value in ids
                 if (isinstance(value, str) and value.strip()) or type(value) is int}
    root = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))
    database = root / "state_5.sqlite"
    try:
        database.stat()
    except FileNotFoundError:
        # Only a genuinely catalog-less (CLI-only) install counts as missing. Another
        # schema version or a sqlite/ store means "present but unsupported": fail closed.
        try:
            other_catalog = (any(root.glob("state_*.sqlite"))
                             or (root / "sqlite").exists())
        except OSError:
            other_catalog = True
        return {"status": "unavailable", "catalog": [], "by_id": {},
                "no_match_ids": set(), "failed_ids": requested,
                "catalog_missing": not other_catalog}
    except OSError:
        return {"status": "unavailable", "catalog": [], "by_id": {},
                "no_match_ids": set(), "failed_ids": requested, "catalog_missing": False}
    if not database.is_file():
        return {"status": "unavailable", "catalog": [], "by_id": {},
                "no_match_ids": set(), "failed_ids": requested, "catalog_missing": False}
    try:
        with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True, timeout=0.1)) as connection:
            connection.row_factory = sqlite3.Row
            connection.execute("BEGIN")
            records = connection.execute(
                "SELECT id, rollout_path, source, cwd, name, title, agent_path, archived "
                "FROM threads WHERE archived = 0 ORDER BY updated_at DESC LIMIT ?", (limit,)
            ).fetchall()
            catalog = [_codex_sqlite_identity(record) for record in records]
            by_id: dict[str, dict[str, Any]] = {}
            failed_ids: set[str] = set()
            pending = set(requested)
            variable_limit = _sqlite_variable_limit(connection) if pending else 0
            queried: set[str] = set()
            while pending:
                batch = sorted(pending)[:variable_limit]
                pending.difference_update(batch)
                queried.update(batch)
                try:
                    matches = _codex_identity_batch(connection, batch)
                except sqlite3.Error:
                    failed_ids.update(batch)
                    continue
                for record in matches:
                    identity = str(record["id"])
                    row = _codex_sqlite_identity(record)
                    by_id[identity] = row
                    parent = row.get("parentThreadId")
                    if isinstance(parent, str) and parent.strip() and parent not in queried:
                        pending.add(parent)
            connection.commit()
    except (sqlite3.Error, OSError, ValueError):
        return {"status": "unavailable", "catalog": [], "by_id": {},
                "no_match_ids": set(), "failed_ids": requested, "catalog_missing": False}
    return {"status": "partial" if failed_ids else "complete", "catalog": catalog,
            "by_id": by_id, "no_match_ids": queried - by_id.keys() - failed_ids,
            "failed_ids": failed_ids, "catalog_missing": False}


def _persisted_codex_threads(limit: int = 200) -> list[dict[str, Any]]:
    return _codex_identity_snapshot(limit=limit)["catalog"]


def _human_tokens(value: int | float | None) -> str:
    n = float(value or 0)
    for suffix, divisor in (("B", 1_000_000_000), ("M", 1_000_000), ("K", 1_000)):
        if abs(n) >= divisor:
            digits = 1 if abs(n) < divisor * 100 else 0
            return f"{n / divisor:.{digits}f}{suffix}"
    return str(int(n))


def _reset_label(epoch_seconds: int | float | None, percent: int | float | None = None) -> str:
    if not epoch_seconds:
        if _usage_percent(percent) == 0:
            return "available now"
        return "reset unavailable"
    seconds = max(0, int(float(epoch_seconds) - time.time()))
    days, seconds = divmod(seconds, 86400)
    hours, seconds = divmod(seconds, 3600)
    minutes = seconds // 60
    if days:
        return f"resets {days}d {hours}h"
    if hours:
        return f"resets {hours}h {minutes:02d}m"
    return f"resets {minutes}m"


def _positive_int(value: Any) -> int | None:
    """A token count or capacity: a positive finite number, never an identifier object or flag."""
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    try:
        number = float(value)
    except ValueError:
        return None
    return int(number) if math.isfinite(number) and number > 0 else None


def _nonnegative_int(value: Any) -> int | None:
    """A measured count where an explicit zero is meaningful."""
    parsed = _positive_int(value)
    if parsed is not None:
        return parsed
    if value == 0 and not isinstance(value, bool):
        return 0
    if isinstance(value, str) and value.strip() == "0":
        return 0
    return None


def _usage_percent(value: Any) -> int | None:
    """Preserve an explicitly reported zero while leaving absent limits unknown."""
    if value is None or isinstance(value, bool):
        return None
    try:
        return max(0, min(100, int(value)))
    except (TypeError, ValueError, OverflowError):
        return None


def _walk(value: Any) -> Iterable[tuple[str, Any]]:
    if isinstance(value, dict):
        for key, child in value.items():
            yield key, child
            yield from _walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk(child)


CLAUDE_PRICING = {
    "fable-5": (10, 1, 12.5, 20, 50),
    "opus-5": (5, .5, 6.25, 10, 25), "opus-4.8": (5, .5, 6.25, 10, 25),
    "sonnet-5": (2, .2, 2.5, 4, 10), "haiku-4.5": (1, .1, 1.25, 2, 5),
    # Published 2026-10 rates; cache writes use the standard 1.25x/2x input multipliers.
    # Haiku 5.5 doubles beyond 100K-token prompts, which per-response rows can't see.
    "fable-5.1": (10, .25, 12.5, 20, 50), "opus-5.5": (4, .2, 5, 8, 20),
    "sonnet-5.5": (2, .2, 2.5, 4, 10), "haiku-5.5": (.1, .01, .125, .2, .5),
}
CODEX_PRICING = {
    "gpt-6-astra": (10, 1, 50), "gpt-5.6-sol": (4, .4, 20),
    "gpt-5.6-terra": (2, .2, 12), "gpt-5.6-luna": (.2, .02, 1.2),
    # Standard API rates, read 2026-10-29: https://developers.openai.com/api/docs/pricing
    "gpt-6.1-sol": (2, .1, 10), "gpt-6.1-terra": (1, .05, 5), "gpt-6-luna": (.1, .01, .5),
    # https://developers.openai.com/api/docs/models/gpt-6-sol (read 2026-10-29)
    "gpt-6-sol": (2, .2, 10),
}
# Standard-tier long-prompt rule (OpenAI model page): a request whose input exceeds the
# threshold is priced at the input/cache multiplier and output multiplier for the whole request.
# Read 2026-10-29: https://developers.openai.com/api/docs/models/gpt-6.1-sol
# https://developers.openai.com/api/docs/models/gpt-6.1-terra
# https://developers.openai.com/api/docs/models/gpt-6-luna
# https://developers.openai.com/api/docs/pricing?r44b=no (GPT-5.6 Sol)
CODEX_LONG_CONTEXT = {name: (272_000, 2.0, 1.5) for name in
                      ("gpt-6-astra", "gpt-6.1-sol", "gpt-6.1-terra", "gpt-6-luna", "gpt-6-sol", "gpt-5.6-sol")}
LONG_CONTEXT_SUFFIX = "|long-context"
PRICE_BASIS = "Standard-tier list prices; Batch, Flex and Fast billing are not recorded locally"

# Exact provider model IDs that intentionally share the listed price rows. Keep this
# explicit: substring matching makes unknown derived names look priced.
PRICE_MODEL_ALIASES = {
    "claude-fable-5": "fable-5",
    "claude-mythos-5": "fable-5",
    "claude-opus-5": "opus-5",
    "claude-opus-4-8": "opus-4.8",
    "claude-sonnet-5": "sonnet-5",
    "claude-haiku-4-5": "haiku-4.5",
    "claude-haiku-4-5-20251001": "haiku-4.5",
    "claude-fable-5-1": "fable-5.1",
    "claude-mythos-5-1": "fable-5.1",
    "claude-opus-5-5": "opus-5.5",
    "claude-sonnet-5-5": "sonnet-5.5",
    "claude-haiku-5-5": "haiku-5.5",
}


def _duration_since(minutes: float) -> str:
    minutes = max(0, int(minutes))
    if not minutes:
        return "just now"
    hours, minutes = divmod(minutes, 60)
    days, hours = divmod(hours, 24)
    return f"{days}d {hours}h" if days else f"{hours}h {minutes:02d}m" if hours else f"{minutes}m"


def _tail_json(path: Path, byte_limit: int = 524_288) -> list[dict[str, Any]]:
    try:
        with path.open("rb") as handle:
            size = handle.seek(0, 2)
            handle.seek(max(0, size - byte_limit))
            lines = handle.read().decode("utf-8", errors="ignore").splitlines()
    except OSError:
        return []
    rows = []
    for line in lines:
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(item, dict):
            rows.append(item)
    return rows


_TOKEN_USAGE_KEYS = ("input_tokens", "cached_input_tokens", "cache_write_input_tokens",
                     "output_tokens", "reasoning_output_tokens", "total_tokens")
_TOKEN_ABSENT = object()


def _pack_token_counters(value: Any) -> Any:
    """Tuple of the consumed counter values; the original dict when it holds anything else."""
    if not isinstance(value, dict) or any(key not in _TOKEN_USAGE_KEYS for key in value):
        return value
    return tuple(value.get(key, _TOKEN_ABSENT) for key in _TOKEN_USAGE_KEYS)


def _unpack_token_counters(value: Any) -> Any:
    if not isinstance(value, tuple):
        return value
    return {key: item for key, item in zip(_TOKEN_USAGE_KEYS, value) if item is not _TOKEN_ABSENT}


class _CompactTokenEvents:
    """Read-only sequence of token events stored as tuples; items materialize as dicts.

    Consumers see the same event dicts as before (timestamp, rate_limits, model,
    usage, last, inherited). Equal consecutive rate-limit objects and model names
    share one object, so retained size is far below one dict tree per event.
    """
    __slots__ = ("_rows",)

    def __init__(self) -> None:
        self._rows: list[tuple[Any, ...]] = []

    def append(self, timestamp: Any, rate_limits: Any, model: Any, usage: Any, last: Any,
               inherited: bool) -> None:
        if self._rows:
            previous = self._rows[-1]
            # Share an identical (type- and order-exact) rate-limit object.
            if isinstance(rate_limits, dict) and isinstance(previous[1], dict):
                if (rate_limits == previous[1]
                        and json.dumps(rate_limits) == json.dumps(previous[1])):
                    rate_limits = previous[1]
            if model == previous[2]:
                model = previous[2]
        self._rows.append((timestamp, rate_limits, model, _pack_token_counters(usage),
                           _pack_token_counters(last), inherited))

    @staticmethod
    def _event(row: tuple[Any, ...]) -> dict[str, Any]:
        return {"timestamp": row[0], "rate_limits": row[1], "model": row[2],
                "usage": _unpack_token_counters(row[3]), "last": _unpack_token_counters(row[4]),
                "inherited": row[5]}

    def __len__(self) -> int:
        return len(self._rows)

    def __bool__(self) -> bool:
        return bool(self._rows)

    def __iter__(self) -> Any:
        return (self._event(row) for row in self._rows)

    def __getitem__(self, index: Any) -> Any:
        if isinstance(index, slice):
            return [self._event(row) for row in self._rows[index]]
        return self._event(self._rows[index])

    def __eq__(self, other: Any) -> bool:
        if isinstance(other, _CompactTokenEvents):
            return list(self) == list(other)
        return isinstance(other, list) and list(self) == other

    __hash__ = None  # type: ignore[assignment]


_FILE_MEMO: dict[tuple[str, str], tuple[tuple[int, ...], Any]] = {}
_FILE_MEMO_LOCK = threading.Lock()


class _TimedProjection:
    __slots__ = ("value", "valid_until")

    def __init__(self, value: Any, valid_until: float) -> None:
        self.value = value
        self.valid_until = valid_until


class _BuildMemo:
    """Per-build stat and path-resolution memo; never shared across builds."""
    __slots__ = ("stats", "resolved")

    def __init__(self) -> None:
        self.stats: dict[str, Any] = {}
        self.resolved: dict[str, str] = {}


_BUILD_MEMO: ContextVar[_BuildMemo | None] = ContextVar("token_tracker_build_memo", default=None)


@contextmanager
def _build_scope() -> Any:
    """Scope one live/snapshot build. The outermost scope owns the memo and drops it on exit."""
    if _BUILD_MEMO.get() is not None:
        yield
        return
    token = _BUILD_MEMO.set(_BuildMemo())
    try:
        yield
    finally:
        _BUILD_MEMO.reset(token)


def _in_build_scope(function: Any) -> Any:
    @functools.wraps(function)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        with _build_scope():
            return function(*args, **kwargs)
    return wrapper


def _fingerprint_of(stat: os.stat_result) -> tuple[int, int, int, int, int]:
    return (stat.st_size, stat.st_mtime_ns, stat.st_dev, stat.st_ino,
            getattr(stat, "st_ctime_ns", int(stat.st_ctime * 1_000_000_000)))


def _stat_fresh(path: Path) -> os.stat_result:
    """Stat the path now and, inside a build, record it for later pre-read lookups."""
    memo = _BUILD_MEMO.get()
    try:
        stat = path.stat()
    except OSError as exc:
        if memo is not None:
            memo.stats[os.fspath(path)] = exc
        raise
    if memo is not None:
        memo.stats[os.fspath(path)] = stat
    return stat


def _stat_memoized(path: Path) -> os.stat_result:
    """Pre-read stat: at most one filesystem stat per path per build."""
    memo = _BUILD_MEMO.get()
    if memo is None:
        return path.stat()
    cached = memo.stats.get(os.fspath(path))
    if cached is None:
        return _stat_fresh(path)
    if isinstance(cached, OSError):
        raise cached
    return cached


def _resolve_memoized(path: Path) -> str:
    memo = _BUILD_MEMO.get()
    if memo is None:
        return str(path.resolve(strict=False))
    name = os.fspath(path)
    resolved = memo.resolved.get(name)
    if resolved is None:
        resolved = memo.resolved[name] = str(path.resolve(strict=False))
    return resolved


def _stat_fingerprint(path: Path) -> tuple[int, int, int, int, int]:
    return _fingerprint_of(_stat_fresh(path))


def _stat_fingerprint_memoized(path: Path) -> tuple[int, int, int, int, int]:
    return _fingerprint_of(_stat_memoized(path))


def _codex_session_inventory() -> tuple[Path, ...]:
    """Capture session paths once for one full provider collection."""
    sessions = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")) / "sessions"
    if not sessions.exists():
        return ()
    return tuple(sessions.glob("**/*.jsonl"))


def _provider_collection_sets(policies: Any) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Filter retained full and quota-only provider capabilities by consent."""
    full = tuple(name for name in ENABLED_PROVIDERS
                 if policy_for(name, policies).allows("collect"))
    limits = tuple(name for name in LIMIT_ONLY_PROVIDERS
                   if name not in full and policy_for(name, policies).allows("collect"))
    return full, limits


_JSONL_CHECKPOINTS: OrderedDict[tuple[str, str], dict[str, Any]] = OrderedDict()
_JSONL_CHECKPOINT_LOCK = threading.Lock()
_JSONL_CHECKPOINT_BYTES = 0
_JSONL_CHECKPOINT_MAX_BYTES = 8 * 1024 * 1024
_JSONL_CHECKPOINT_MAX_ENTRIES = 1024
_JSONL_GUARD_BYTES = 128
# Fixed lock stripes serialize calls for a path without retaining one lock per
# path or holding a global lock during file IO. Collisions only serialize extra work.
_JSONL_PATH_LOCKS = tuple(threading.Lock() for _ in range(64))


def _jsonl_stat(handle: Any) -> tuple[int, int, int, int, int]:
    stat = os.fstat(handle.fileno())
    return (stat.st_size, stat.st_mtime_ns, stat.st_dev, stat.st_ino,
            getattr(stat, "st_ctime_ns", int(stat.st_ctime * 1_000_000_000)))


def _jsonl_guards(handle: Any, offset: int, size: int) -> tuple[bytes, bytes]:
    handle.seek(0)
    prefix = handle.read(min(_JSONL_GUARD_BYTES, size))
    cursor_start = max(0, offset - _JSONL_GUARD_BYTES)
    handle.seek(cursor_start)
    cursor = handle.read(max(0, offset - cursor_start))
    return (hashlib.blake2s(prefix).digest(), hashlib.blake2s(cursor).digest())


def _jsonl_checkpoint_remove(key: tuple[str, str]) -> None:
    global _JSONL_CHECKPOINT_BYTES
    with _JSONL_CHECKPOINT_LOCK:
        removed = _JSONL_CHECKPOINTS.pop(key, None)
        if removed:
            _JSONL_CHECKPOINT_BYTES -= removed["cost"]


def _jsonl_object_size(value: Any, seen: set[int]) -> int:
    identity = id(value)
    if identity in seen:
        return 0
    seen.add(identity)
    size = sys.getsizeof(value)
    if isinstance(value, dict):
        size += sum(_jsonl_object_size(key, seen) + _jsonl_object_size(item, seen)
                    for key, item in value.items())
    elif isinstance(value, (list, tuple, set, frozenset, OrderedDict)):
        size += sum(_jsonl_object_size(item, seen) for item in value)
    return size


def _jsonl_state_cost(state: Any, key: tuple[str, str], extra: Any = None) -> int:
    # Account for the retained Python graph plus the checkpoint/key containers,
    # fingerprint metadata, and a bounded estimate of OrderedDict/index overhead.
    seen: set[int] = set()
    metadata = {"offset": 0, "stat": (0, 0, 0, 0, 0),
                "prefix": bytes(32), "cursor": bytes(32), "extra": extra}
    return (_jsonl_object_size(state, seen) + _jsonl_object_size(key, seen)
            + _jsonl_object_size(metadata, seen) + 512)


def _jsonl_state_uncacheable(state: Any) -> bool:
    return isinstance(state, dict) and bool(state.get("_incremental_uncacheable"))


def _incremental_jsonl_reduce(kind: str, path: Path, initial_state: Any,
                              reduce_record: Any, project: Any = None,
                              fingerprint_extra: Any = None, unavailable: Any = None,
                              needles: tuple[bytes, ...] | None = None) -> Any:
    """Reduce complete JSONL records incrementally.

    ``initial_state`` is a value or zero-argument factory. ``reduce_record``
    mutates that state for one decoded record. ``project`` optionally converts
    the private reducer state to its public result. Checkpoints contain reducer
    state only, never raw JSONL lines. Calls for a path are serialized, and the
    retained checkpoint LRU is bounded by estimated state bytes and entry count.
    """
    resolved = _resolve_memoized(path)
    key = (kind, resolved)
    lock = _JSONL_PATH_LOCKS[hash(resolved) % len(_JSONL_PATH_LOCKS)]
    with lock:
        fresh = False
        while True:
            try:
                before = _stat_fresh(path) if fresh else _stat_memoized(path)
                before = _fingerprint_of(before)
            except OSError:
                _jsonl_checkpoint_remove(key)
                return deepcopy(unavailable)

            with _JSONL_CHECKPOINT_LOCK:
                # Checkpoints are replaced, never mutated in place, so a reference
                # is safe to read after the lock is released; copies are made only
                # where the data is handed out or mutated.
                cached = _JSONL_CHECKPOINTS.get(key)
                if cached:
                    _JSONL_CHECKPOINTS.move_to_end(key)
            if (cached and cached["stat"] == before
                    and cached.get("extra") == fingerprint_extra):
                # Full file identity (size, mtime, dev, ino, ctime) and extra
                # match: nothing was appended or rewritten, so skip open + guards.
                # Accepted gap: a same-size in-place rewrite with a restored mtime is
                # not detected. Codex/Claude transcripts are append-only.
                state = deepcopy(cached["state"])
                return project(state) if project else state
            result = _incremental_jsonl_reduce_locked(
                key, path, initial_state, reduce_record, project, fingerprint_extra,
                unavailable, before, cached, retry_on_stale=not fresh, needles=needles)
            if result is not _STALE_PRE_READ_STAT:
                return result
            fresh = True


_STALE_PRE_READ_STAT = object()


def _incremental_jsonl_reduce_locked(key: tuple[str, str], path: Path, initial_state: Any,
                                     reduce_record: Any, project: Any, fingerprint_extra: Any,
                                     unavailable: Any, before: tuple[int, int, int, int, int],
                                     cached: dict[str, Any] | None, retry_on_stale: bool,
                                     needles: tuple[bytes, ...] | None = None) -> Any:
    try:
        handle_context = path.open("rb")
    except OSError:
        _jsonl_checkpoint_remove(key)
        return deepcopy(unavailable)

    with handle_context as handle:
        opened = _jsonl_stat(handle)
        if opened != before:
            if retry_on_stale:
                # The memoized pre-read stat is older than the file; restat.
                return _STALE_PRE_READ_STAT
            _jsonl_checkpoint_remove(key)
            state = initial_state() if callable(initial_state) else deepcopy(initial_state)
            return project(state) if project else state

        append = bool(cached and cached["stat"][2:4] == before[2:4]
                      and cached["stat"][0] < before[0])
        unchanged = bool(cached and cached["stat"] == before
                         and cached.get("extra") == fingerprint_extra)
        reusable_same_file = bool(cached and cached["stat"] == before)
        if cached and (append or reusable_same_file):
            # Validate exactly the bytes covered by the old checkpoint.
            # For a short file, hashing to the new EOF would include the
            # appended bytes and make every append look like a rewrite.
            guards = _jsonl_guards(handle, cached["offset"], cached["stat"][0])
            if guards != (cached["prefix"], cached["cursor"]):
                append = unchanged = reusable_same_file = False
        if reusable_same_file:
            after_handle = _jsonl_stat(handle)
            try:
                after_path = _stat_fingerprint(path)
            except OSError:
                after_path = None
            if after_handle == before and after_path == before:
                value = cached["state"]
                if cached.get("extra") != fingerprint_extra:
                    _jsonl_checkpoint_remove(key)
                    _jsonl_cache_checkpoint(key, cached["state"], cached["offset"], before,
                                            (cached["prefix"], cached["cursor"]), fingerprint_extra)
                state = deepcopy(value)
                return project(state) if project else state
            _jsonl_checkpoint_remove(key)
            state = deepcopy(cached["state"])
            return project(state) if project else state

        if cached and not append:
            cached = None
        state = deepcopy(cached["state"]) if cached else (initial_state() if callable(initial_state) else deepcopy(initial_state))
        start_offset = cached["offset"] if cached else 0
        handle.seek(start_offset)
        committed_offset = start_offset
        valid_unterminated_record = False
        while True:
            line = handle.readline()
            if not line:
                break
            if not line.endswith(b"\n"):
                # Preserve the old parser's valid-EOF behavior in this
                # projection, but never checkpoint it at the prior offset.
                try:
                    item = json.loads(line.decode("utf-8", errors="ignore"))
                except (ValueError, UnicodeError, RecursionError):
                    break
                reduce_record(state, item)
                valid_unterminated_record = True
                break
            committed_offset = handle.tell()
            # A reducer that only reads records carrying a key can skip lines
            # without that key's bytes. A \u escape or invalid UTF-8 (dropped on
            # decode) could hide the key, so those lines are always parsed.
            if needles and b"\\u" not in line and not any(needle in line for needle in needles):
                try:
                    line.decode("utf-8")
                except UnicodeDecodeError:
                    pass
                else:
                    continue
            try:
                item = json.loads(line.decode("utf-8", errors="ignore"))
            except (ValueError, UnicodeError, RecursionError):
                continue
            reduce_record(state, item)

        guards = _jsonl_guards(handle, committed_offset, before[0])
        after_handle = _jsonl_stat(handle)
        try:
            after_path = _stat_fingerprint(path)
        except OSError:
            after_path = None
        stable = after_handle == before and after_path == before
        if not stable:
            _jsonl_checkpoint_remove(key)
            return project(state) if project else state

        cost = _jsonl_state_cost(state, key, fingerprint_extra)
        _jsonl_checkpoint_remove(key)
        if (cost <= _JSONL_CHECKPOINT_MAX_BYTES and not _jsonl_state_uncacheable(state)
                and not valid_unterminated_record):
            # The checkpoint keeps its own deep copy, so ``state`` is private to
            # this call and may be projected (and handed out) without another copy.
            _jsonl_cache_checkpoint(key, state, committed_offset, before, guards, fingerprint_extra)
        return project(state) if project else state


def _jsonl_cache_checkpoint(key: tuple[str, str], state: Any, offset: int,
                            stamp: tuple[int, int, int, int, int], guards: tuple[bytes, bytes],
                            fingerprint_extra: Any = None) -> None:
    global _JSONL_CHECKPOINT_BYTES
    cost = _jsonl_state_cost(state, key, fingerprint_extra)
    if cost > _JSONL_CHECKPOINT_MAX_BYTES or _jsonl_state_uncacheable(state):
        return
    checkpoint = {"state": deepcopy(state), "offset": offset, "stat": stamp,
                  "prefix": guards[0], "cursor": guards[1], "cost": cost,
                  "extra": deepcopy(fingerprint_extra)}
    with _JSONL_CHECKPOINT_LOCK:
        old = _JSONL_CHECKPOINTS.pop(key, None)
        if old:
            _JSONL_CHECKPOINT_BYTES -= old["cost"]
        _JSONL_CHECKPOINTS[key] = checkpoint
        _JSONL_CHECKPOINT_BYTES += cost
        while (_JSONL_CHECKPOINT_BYTES > _JSONL_CHECKPOINT_MAX_BYTES
               or len(_JSONL_CHECKPOINTS) > _JSONL_CHECKPOINT_MAX_ENTRIES):
            _old_key, old = _JSONL_CHECKPOINTS.popitem(last=False)
            _JSONL_CHECKPOINT_BYTES -= old["cost"]


def _file_memo(kind: str, path: Path, compute: Any) -> Any:
    """Reuse a path-local projection only while its file identity and stat stamp match."""
    def unwrap(value: Any) -> Any:
        return value.value if isinstance(value, _TimedProjection) else value

    try:
        stamp = _stat_fingerprint_memoized(path)
    except OSError:
        path_key = str(path)
        with _FILE_MEMO_LOCK:
            for key in [key for key in _FILE_MEMO if key[1] == path_key]:
                _FILE_MEMO.pop(key, None)
        return unwrap(compute())
    key = (kind, str(path))
    with _FILE_MEMO_LOCK:
        cached = _FILE_MEMO.get(key)
        if cached and cached[0] == stamp:
            value = cached[1]
            if not isinstance(value, _TimedProjection) or time.time() < value.valid_until:
                return unwrap(value)
    value = compute()
    with _FILE_MEMO_LOCK:
        if len(_FILE_MEMO) >= 8192:
            _FILE_MEMO.clear()
        _FILE_MEMO[key] = (stamp, value)
    return unwrap(value)


QUIET_AFTER_SECONDS = 180
INACTIVE_AFTER_SECONDS = 30 * 60


def _expire_running(path: Path, status: str) -> str:
    """A recorded running state is trusted while the file is being written.

    Long tool calls can go minutes without a write, so a silent "running" session
    is reported as uncertain ("quiet") before it is considered inactive.
    """
    if status not in {"running", "compacting"}:
        return status
    try:
        silent = time.time() - path.stat().st_mtime
    except OSError:
        return "idle"
    return status if silent <= QUIET_AFTER_SECONDS else "quiet" if silent <= INACTIVE_AFTER_SECONDS else "idle"


def _state_detail(recorded: dict[str, Any], status: str) -> str | None:
    """Why a chat has its state: completed vs. inactive idle, stopped vs. errored."""
    if status == "quiet":
        return "no recent writes"
    if status == "idle" and recorded.get("status") == "running":
        return "inactive"
    return recorded.get("detail")


def _question_text(value: Any) -> str | None:
    """First human-facing question in a tool input; never other arguments."""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return None
    questions = value.get("questions") if isinstance(value, dict) else None
    candidates = questions if isinstance(questions, list) else [value]
    for candidate in candidates:
        if isinstance(candidate, dict):
            text = candidate.get("question") or candidate.get("title") or candidate.get("prompt") or candidate.get("header")
            if isinstance(text, str) and text.strip():
                return " ".join(text.split())[:200]
    return None


def _question_options(value: Any) -> list[str]:
    """Answer choices of the first question (Codex: strings; Claude: {label})."""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return []
    questions = value.get("questions") if isinstance(value, dict) else None
    candidates = questions if isinstance(questions, list) else [value]
    for candidate in candidates:
        if not isinstance(candidate, dict) or not isinstance(candidate.get("options"), list):
            continue
        labels = []
        for option in candidate["options"][:6]:
            label = option.get("label") or option.get("title") if isinstance(option, dict) else option
            if isinstance(label, str) and label.strip():
                labels.append(" ".join(label.split())[:80])
        return labels
    return []


def _claude_transcript_status(path: Path) -> str:
    return _claude_transcript_state(path)["status"]


def _claude_transcript_state(path: Path) -> dict[str, Any]:
    state = _file_memo("claude-status", path, lambda: _claude_recorded_state(path))
    status = _expire_running(path, state["status"])
    return {"status": status, "question": state.get("question") if status == "needs input" else None, "detail": _state_detail(state, status), **({"options": state["options"]} if status == "needs input" and state.get("options") else {})}


def _claude_recorded_state(path: Path) -> dict[str, Any]:
    status, question, detail = "idle", None, None
    options: list[str] = []
    for item in _tail_json(path):
        item_type = str(item.get("type") or "").lower()
        if item.get("error") or item_type == "error":
            status, question, detail = "failed", None, "errored"
            continue
        if item_type == "result":
            status, question = ("failed" if item.get("is_error") else "idle"), None
            detail = "errored" if item.get("is_error") else "completed"
            continue
        message = item.get("message") or {}
        content = message.get("content") or []
        asks = [
            block for block in content
            if isinstance(block, dict) and block.get("type") == "tool_use"
            and str(block.get("name") or "") in {"AskUserQuestion", "ask_user_question"}
        ] if isinstance(content, list) else []
        if asks:
            status, question, detail = "needs input", _question_text(asks[-1].get("input")), None
            options = _question_options(asks[-1].get("input"))
        elif message.get("stop_reason") in {"end_turn", "stop_sequence"}:
            status, question, detail = "idle", None, "completed"
        elif item_type in {"user", "assistant"}:
            status, question, detail = "running", None, None
    return {"status": status, "question": question, "detail": detail, **({"options": options} if status == "needs input" and options else {})}


def _codex_rollout_status(path_value: str | None) -> str:
    return _codex_rollout_state(path_value)["status"]


def _codex_rollout_state(path_value: str | None) -> dict[str, Any]:
    unavailable_execution = {
        "executionStatus": "unavailable", "executionSource": "unavailable",
        "executionObservedAt": None, "executionSince": None,
        "executionCompactionStartedAt": None, "executionTerminalAt": None,
        "executionActivity": None,
    }
    if not path_value:
        return {"status": "unavailable", "question": None, "questionIds": [], "detail": None,
                "telemetryUnavailable": True, **unavailable_execution}
    path = Path(path_value)
    try:
        with path.open("rb") as handle:
            handle.read(1)
        state = _codex_recorded_state(path)
    except (OSError, IsADirectoryError):
        return {"status": "unavailable", "question": None, "questionIds": [], "detail": None,
                "telemetryUnavailable": True, **unavailable_execution}
    if not path.is_file():
        return {"status": "unavailable", "question": None, "questionIds": [], "detail": None,
                "telemetryUnavailable": True, **unavailable_execution}
    status = state["status"]
    waiting = status == "needs input"
    return {"status": status, "question": state.get("question") if waiting else None,
            "questionIds": state.get("questionIds", []),
            "questionResolutionAt": state.get("questionResolutionAt"),
            "questionCorrelationUnavailable": state.get("questionCorrelationUnavailable", False),
            "questionAt": state.get("questionAt") if waiting else None,
            "detail": state.get("detail"),
            "stateSince": state.get("stateSince"),
            "compactionStartedAt": state.get("compactionStartedAt"),
            "terminalAt": state.get("terminalAt"),
            "executionStatus": state.get("executionStatus", "unknown"),
            "executionSource": state.get("executionSource", "transcript"),
            "executionObservedAt": state.get("executionObservedAt"),
            "executionSince": state.get("executionSince"),
            "executionCompactionStartedAt": state.get("executionCompactionStartedAt"),
            "executionTerminalAt": state.get("executionTerminalAt"),
            "executionActivity": state.get("executionActivity"),
            "activity": state.get("activity"),
            "observedAt": state.get("observedAt"),
            "recordTimestampPresent": state.get("recordTimestampPresent", False),
            "telemetryUnavailable": status == "unknown",
            **({"options": state["options"]} if waiting and state.get("options") else {})}


def _codex_execution_restarted_after_terminal(recorded: dict[str, Any]) -> bool:
    """Only a recorded post-terminal run can re-establish transcript execution."""
    terminal = _timestamp_epoch(recorded.get("executionTerminalAt") or recorded.get("terminalAt"))
    observed = _timestamp_epoch(recorded.get("executionObservedAt"))
    return (terminal is not None and observed is not None and observed > terminal
            and recorded.get("executionStatus") in {"running", "compacting"})


def _codex_execution_projection(recorded: dict[str, Any], app_status: str | None,
                                app_fresh: bool, app_observed: Any,
                                now: float | None = None) -> dict[str, Any]:
    """Project transcript execution independently of user-attention status."""
    now = time.time() if now is None else now
    status = recorded.get("executionStatus") or "unknown"
    if status == "unavailable":
        return {"executionStatus": "unavailable", "executionSource": "unavailable",
                "executionObservedAt": None, "executionSince": None,
                "executionCompactionStartedAt": None, "executionTerminalAt": None,
                "executionActivity": None}
    observed = recorded.get("executionObservedAt")
    observed_epoch = _timestamp_epoch(observed)
    terminal = recorded.get("executionTerminalAt") or recorded.get("terminalAt")
    terminal_epoch = _timestamp_epoch(terminal)
    app_epoch = _timestamp_epoch(app_observed)
    app_terminal_newer = (app_fresh and app_status in {"idle", "failed"}
                          and app_epoch is not None
                          and (observed_epoch is None or app_epoch > observed_epoch))
    if app_terminal_newer:
        status, source, observed = app_status, "app", app_observed
        terminal = app_observed
    elif status == "waiting":
        if _fresh_epoch(observed_epoch, now=now) and (terminal_epoch is None or observed_epoch > terminal_epoch):
            source = "transcript"
        else:
            status, source, observed = "unavailable", "unavailable", None
    elif status in {"idle", "failed"}:
        source = "transcript"
    elif status in {"running", "compacting"}:
        if terminal_epoch is not None and (observed_epoch is None or observed_epoch <= terminal_epoch):
            status, source = "idle", "transcript"
        elif _fresh_epoch(observed_epoch, now=now):
            source = "transcript"
        elif (status == "compacting" and app_fresh and app_status == "running"
              and app_epoch is not None
              and (terminal_epoch is None or terminal_epoch < (_timestamp_epoch(recorded.get("executionCompactionStartedAt")) or 0))
              and app_epoch >= (_timestamp_epoch(recorded.get("executionCompactionStartedAt")) or float("inf"))):
            source, observed = "transcript", app_observed
        elif (app_fresh and app_status == "running"
              and (terminal_epoch is None or _codex_execution_restarted_after_terminal(recorded))):
            status, source, observed = "running", "app", app_observed
        else:
            status, source, observed = "unavailable", "unavailable", None
    else:
        status, source, observed = "unavailable", "unavailable", None
    active = status in {"running", "compacting"}
    return {
        "executionStatus": status,
        "executionSource": source,
        "executionObservedAt": observed if active or status == "waiting" else None,
        "executionSince": recorded.get("executionSince") if active else None,
        "executionCompactionStartedAt": recorded.get("executionCompactionStartedAt") if active and status == "compacting" else None,
        "executionTerminalAt": terminal if status in {"idle", "failed"} else None,
        "executionActivity": recorded.get("executionActivity") if active and source == "transcript" else None,
    }


def _question_item_id(value: Any) -> tuple[str, str, int] | None:
    """Parse one exact [tool, call, index] identity, including its JSON encoding."""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (ValueError, TypeError):
            return None
    if (not isinstance(value, list) or len(value) != 3
            or not all(isinstance(part, str) and part for part in value[:2])
            or isinstance(value[2], bool) or not isinstance(value[2], int) or value[2] < 0):
        return None
    return value[0].strip().lower().split(".")[-1], value[1], value[2]


def _question_reply_answers(text: Any) -> list[dict[str, Any]]:
    """Accept only a complete structured reply wrapper, never quoted examples."""
    if not isinstance(text, str):
        return []
    trimmed = text.strip()
    prefix, suffix = "<send_user_message_question_reply>", "</send_user_message_question_reply>"
    if not trimmed.startswith(prefix) or not trimmed.endswith(suffix):
        return []
    try:
        value = json.loads(trimmed[len(prefix):-len(suffix)].strip())
    except (ValueError, TypeError):
        return []
    return value if isinstance(value, list) else []


def _valid_question_answer(value: Any) -> bool:
    return (isinstance(value, str) and bool(value.strip())) or (
        isinstance(value, list) and bool(value)
        and all(isinstance(answer, str) and bool(answer.strip()) for answer in value)
    )


def _observed_question_id(value: Any, aliases: dict[str, list[tuple[str, str, int]]]) -> tuple[str, str, int] | None:
    identity = _question_item_id(value)
    if identity is not None:
        return identity
    if isinstance(value, (str, int)) and not isinstance(value, bool):
        matches = aliases.get(str(value), [])
        return matches[0] if len(matches) == 1 else None
    return None


def _drop_pending_question(identity: tuple[str, str, int], pending: dict[Any, Any],
                           aliases: dict[str, list[tuple[str, str, int]]]) -> bool:
    if identity not in pending:
        return False
    pending.pop(identity, None)
    for alias, matches in list(aliases.items()):
        remaining = [candidate for candidate in matches if candidate != identity]
        if remaining:
            aliases[alias] = remaining
        else:
            aliases.pop(alias, None)
    return True


def _tracker_question_supersessions(text: Any) -> list[tuple[str, str, int]]:
    if not isinstance(text, str) or len(text) > 8192:
        return []
    prefix, suffix = "[tracker-questions]", "[/tracker-questions]"
    start = text.find(prefix)
    if start < 0:
        return []
    end = text.find(suffix, start + len(prefix))
    if end < 0 or text.find(prefix, start + len(prefix)) >= 0 or end - start > 4096:
        return []
    try:
        value = json.loads(text[start + len(prefix):end].strip())
    except (ValueError, TypeError):
        return []
    if not isinstance(value, dict) or not isinstance(value.get("superseded"), list):
        return []
    return [identity for raw in value["superseded"] if (identity := _question_item_id(raw))]


def _codex_recorded_state(path: Path) -> dict[str, Any]:
    def initial_state() -> dict[str, Any]:
        return {"status": "unknown", "detail": None, "stateSince": None,
                "compactionStartedAt": None, "activity": None,
                "executionStatus": "unknown", "executionSource": "transcript",
                "executionObservedAt": None, "executionSince": None,
                "executionCompactionStartedAt": None, "executionTerminalAt": None,
                "executionPendingCalls": {}, "executionResumeAt": None,
                "executionActivity": None,
                "pendingActivityCalls": {}, "pendingQuestions": {},
                "questionAliases": {}, "syncQuestion": None,
                "questionResolutionAt": None, "questionCorrelationUnavailable": False,
                "sessionSource": None, "sessionMetaSeen": False, "turnEndedAt": None,
                "observedAt": None,
                "recordTimestampPresent": False}

    def start_after_exec_turn_end(pending: dict[Any, Any], aliases: dict[str, list[tuple[str, str, int]]],
                                  ended_at: Any, started_at: Any) -> tuple[bool, bool]:
        """Supersede only questions from an ended exec turn at a later start."""
        ended_epoch = _timestamp_epoch(ended_at)
        started_epoch = _timestamp_epoch(started_at)
        if ended_epoch is None or started_epoch is None or started_epoch <= ended_epoch:
            return False, False
        stale = []
        for identity, question in pending.items():
            question_epoch = _timestamp_epoch(question.get("at"))
            if question_epoch is None or question_epoch <= ended_epoch:
                stale.append(identity)
        for identity in stale:
            _drop_pending_question(identity, pending, aliases)
        return True, bool(stale)

    def reduce_record(state: dict[str, Any], item: dict[str, Any]) -> None:
        status = state["status"]
        detail = state["detail"]
        state_since = state["stateSince"]
        compaction_started_at = state["compactionStartedAt"]
        activity = state["activity"]
        pending_activity_calls = state["pendingActivityCalls"]
        pending_questions = state["pendingQuestions"]
        question_aliases = state["questionAliases"]
        sync_question = state["syncQuestion"]
        question_resolution_at = state["questionResolutionAt"]
        question_correlation_unavailable = state["questionCorrelationUnavailable"]
        observed_at = state["observedAt"]
        record_timestamp_present = state["recordTimestampPresent"]
        session_source = state.get("sessionSource")
        session_meta_seen = state.get("sessionMetaSeen", False)
        turn_ended_at = state.get("turnEndedAt")
        stamp = item.get("timestamp")
        if stamp:
            record_timestamp_present = True
            state["recordTimestampPresent"] = True
            stamp_epoch = _timestamp_epoch(stamp)
            if stamp_epoch is not None and stamp_epoch > time.time():
                state["_incremental_uncacheable"] = True
                return
            if stamp_epoch is not None:
                observed_at = stamp
        previous_status = status
        clock_transition = False
        matched_activity_call = False
        matched_question_reply = False
        resolved_blocking_question = False
        payload = item.get("payload") or {}
        payload_type = str(payload.get("type") or "").lower()
        item_kind = item.get("type")
        if item_kind == "session_meta" and not session_meta_seen:
            session_meta_seen = True
            session_source = "exec" if isinstance(payload, dict) and payload.get("source") == "exec" else None
        event_type = payload_type if item.get("type") == "event_msg" else str(item.get("subtype") or "").lower()
        timestamp = item.get("timestamp") or payload.get("timestamp")
        execution_status_before = state.get("executionStatus", "unknown")
        execution_stamp = item.get("timestamp") or payload.get("timestamp")
        execution_epoch = _timestamp_epoch(execution_stamp)
        execution_floor_epoch = _timestamp_epoch(state.get("executionSince"))
        execution_terminal_epoch = _timestamp_epoch(state.get("executionTerminalAt"))
        execution_invocation_admitted = (
            execution_status_before == "running" and execution_floor_epoch is not None
            and execution_epoch is not None and execution_epoch <= time.time()
            and execution_epoch >= execution_floor_epoch
            and (execution_terminal_epoch is None or execution_epoch > execution_terminal_epoch)
        )
        # Execution is reduced independently from the attention/question ledger.
        # Only actual run lifecycle records and correlated assistant work can move
        # these fields; messages, question control traffic and file mtimes cannot.
        exec_status = state.get("executionStatus", "unknown")
        exec_observed_at = state.get("executionObservedAt")
        exec_since = state.get("executionSince")
        exec_compaction_at = state.get("executionCompactionStartedAt")
        exec_terminal_at = state.get("executionTerminalAt")
        exec_pending_calls = state.setdefault("executionPendingCalls", {})
        exec_resume_at = state.get("executionResumeAt")
        exec_activity = state.get("executionActivity")
        exec_stamp = item.get("timestamp") or payload.get("timestamp")
        exec_stamp_epoch = _timestamp_epoch(exec_stamp)
        exec_observed_epoch = _timestamp_epoch(exec_observed_at)
        exec_floor_epoch = _timestamp_epoch(exec_since)
        exec_terminal_epoch = _timestamp_epoch(exec_terminal_at)
        exec_resume_epoch = _timestamp_epoch(exec_resume_at)
        exec_stamp_valid = exec_stamp_epoch is not None and exec_stamp_epoch <= time.time()
        exec_after_terminal = (exec_stamp_epoch is not None
                               and (exec_terminal_epoch is None or exec_stamp_epoch > exec_terminal_epoch))
        exec_transition_admitted = exec_stamp_valid and exec_after_terminal
        terminal_floors = [epoch for epoch in (exec_floor_epoch, exec_resume_epoch) if epoch is not None]
        exec_terminal_admitted = (
            exec_transition_admitted
            and (not terminal_floors or exec_stamp_epoch >= max(terminal_floors))
        )
        exec_current_running = (
            exec_status == "running" and exec_floor_epoch is not None and exec_stamp_valid
            and exec_stamp_epoch >= exec_floor_epoch and exec_after_terminal
            and (exec_observed_epoch is None or exec_stamp_epoch >= exec_observed_epoch)
        )
        exec_resume_admitted = (
            exec_status == "unknown" and exec_resume_epoch is not None and exec_stamp_valid
            and exec_stamp_epoch > exec_resume_epoch and exec_after_terminal
        )
        exec_qualifies = False
        exec_activity_changed = False
        exec_turn_ended = False
        # Starts and compaction obey the same floor as terminals: a delayed record older
        # than the current run start or resume boundary cannot reopen or restart it.
        if item_kind == "event_msg" and payload_type == "task_started" and exec_terminal_admitted:
            exec_status, exec_since, exec_compaction_at = "running", exec_stamp, None
            exec_resume_at = None
            exec_activity = None
            exec_pending_calls.clear()
            exec_qualifies = True
            if session_source == "exec":
                later_turn, superseded = start_after_exec_turn_end(
                    pending_questions, question_aliases, turn_ended_at, exec_stamp,
                )
                if later_turn:
                    turn_ended_at = None
                    if superseded:
                        question_resolution_at = exec_stamp
        elif (item_kind == "event_msg" and payload_type in {"task_complete", "turn_complete", "turn_aborted", "task_failed"}
              and exec_terminal_admitted):
            exec_status = "failed" if payload_type in {"turn_aborted", "task_failed"} else "idle"
            exec_terminal_at = exec_stamp
            exec_compaction_at, exec_activity = None, None
            exec_resume_at = None
            exec_pending_calls.clear()
            exec_qualifies = True
            exec_turn_ended = True
        elif ("compact" in event_type and any(word in event_type for word in ("start", "begin", "progress", "ing"))
              and exec_terminal_admitted):
            exec_status, exec_compaction_at = "compacting", exec_stamp
            exec_activity = None
            exec_pending_calls.clear()
            exec_qualifies = True
        elif ("compact" in event_type and any(word in event_type for word in ("complete", "finish", "end", "done", "boundary"))
              and exec_transition_admitted):
            exec_status, exec_compaction_at = "running", None
            exec_activity = None
            exec_qualifies = True
        elif ((item_kind == "compacted" or (item_kind == "system" and item.get("subtype") == "compact_boundary"))
              and exec_transition_admitted):
            exec_status, exec_compaction_at = "running", None
            exec_activity = None
            exec_qualifies = True
        elif (item_kind == "event_msg" and payload_type in {"reasoning_started", "thinking_started"}
              and exec_current_running):
            # This explicit start event is the only rollout signal that proves
            # reasoning is in progress. A completed response_item is not a start.
            exec_activity = {"kind": "thinking", "label": "Reasoning", "files": [], "observedAt": exec_stamp}
            exec_activity_changed = True
        elif item_kind == "response_item":
            name = str(payload.get("name") or "").lower().split(".")[-1]
            control_call = name in {"request_user_input", "request_user_input_async", "ask_user_question", "send_user_message_question_reply"}
            ptype = str(payload.get("type") or "").lower()
            call_id = payload.get("call_id") or payload.get("id")
            if (ptype in {"function_call", "custom_tool_call", "local_shell_call"}
                    and name in {"request_user_input", "ask_user_question"} and exec_transition_admitted):
                # Blocking questions pause this execution. Async asks remain in
                # the attention ledger without changing execution state.
                exec_status, exec_observed_at, exec_activity = "waiting", exec_stamp, None
                exec_compaction_at = None
                exec_pending_calls.clear()
                exec_resume_at = None
                exec_qualifies = True
            elif ptype in {"function_call", "custom_tool_call", "local_shell_call"} and not control_call:
                if exec_current_running or exec_resume_admitted:
                    if exec_resume_admitted:
                        exec_status, exec_since = "running", exec_resume_at
                        exec_resume_at = None
                    if call_id is not None:
                        exec_pending_calls[str(call_id)] = _codex_tool_activity(payload, exec_stamp)
                    exec_activity = _codex_tool_activity(payload, exec_stamp)
                    exec_observed_at = exec_stamp
                    exec_qualifies = True
            elif (ptype in {"function_call_output", "custom_tool_call_output"}
                  and call_id is not None and str(call_id) in exec_pending_calls):
                if exec_current_running:
                    exec_pending_calls.pop(str(call_id), None)
                    exec_activity = next(reversed(exec_pending_calls.values()), None) if exec_pending_calls else None
                    exec_observed_at = exec_stamp
                    exec_qualifies = True
                elif exec_stamp is None:
                    # A matching untimed result can retire a pending call, but
                    # cannot advance the execution observation or status.
                    exec_pending_calls.pop(str(call_id), None)
            elif ptype == "message" and payload.get("role") == "assistant":
                channel = str(payload.get("channel") or "").lower()
                if channel == "final" and exec_terminal_admitted:
                    exec_status, exec_terminal_at = "idle", exec_stamp
                    exec_compaction_at, exec_activity = None, None
                    exec_pending_calls.clear()
                    exec_qualifies = True
                    exec_turn_ended = True
                elif channel in {"analysis", "reasoning"}:
                    reasoning_epoch = _timestamp_epoch(exec_stamp)
                    observed_epoch = _timestamp_epoch(exec_observed_at)
                    floor_epoch = _timestamp_epoch(exec_since)
                    terminal_epoch = _timestamp_epoch(exec_terminal_at)
                    if (exec_status == "running" and reasoning_epoch is not None
                        and reasoning_epoch <= time.time() and floor_epoch is not None
                        and reasoning_epoch >= floor_epoch
                        and (observed_epoch is None or reasoning_epoch >= observed_epoch)
                        and (terminal_epoch is None or reasoning_epoch > terminal_epoch)):
                        exec_activity = {"kind": "thinking", "label": "Reasoning", "files": [], "observedAt": exec_stamp}
                        exec_observed_at = exec_stamp
                        exec_qualifies = True
            elif ptype == "reasoning":
                reasoning_epoch = _timestamp_epoch(exec_stamp)
                observed_epoch = _timestamp_epoch(exec_observed_at)
                floor_epoch = _timestamp_epoch(exec_since)
                terminal_epoch = _timestamp_epoch(exec_terminal_at)
                if (exec_status == "running" and reasoning_epoch is not None
                        and reasoning_epoch <= time.time() and floor_epoch is not None
                        and reasoning_epoch >= floor_epoch
                        and (observed_epoch is None or reasoning_epoch >= observed_epoch)
                        and (terminal_epoch is None or reasoning_epoch > terminal_epoch)):
                    exec_activity = {"kind": "thinking", "label": "Reasoning", "files": [], "observedAt": exec_stamp}
                    exec_observed_at = exec_stamp
                    exec_qualifies = True
            elif ptype == "agent_message":
                # A recorded inter-agent message is direct evidence of coordination.
                # Keep the label generic; message content and participant names are private.
                if exec_current_running:
                    exec_activity = {
                        "kind": "coordinating", "label": _ACTIVITY_LABELS["coordinating"],
                        "files": [], "observedAt": exec_stamp,
                    }
                    exec_activity_changed = True
        if exec_qualifies:
            exec_observed_at = exec_stamp or exec_observed_at
            state.update(executionStatus=exec_status, executionObservedAt=exec_observed_at,
                         executionSince=exec_since, executionCompactionStartedAt=exec_compaction_at,
                         executionTerminalAt=exec_terminal_at, executionPendingCalls=exec_pending_calls,
                         executionResumeAt=exec_resume_at,
                         executionActivity=exec_activity)
        elif exec_activity_changed:
            # Fill the activity field without extending the execution-status
            # freshness window; only existing lifecycle evidence does that.
            state["executionActivity"] = exec_activity
        if session_source == "exec" and exec_turn_ended and exec_stamp_valid:
            previous_end_epoch = _timestamp_epoch(turn_ended_at)
            if previous_end_epoch is None or exec_stamp_epoch > previous_end_epoch:
                turn_ended_at = exec_stamp
        if "compact" in event_type:
            if any(word in event_type for word in ("start", "begin", "progress", "ing")):
                status, sync_question, detail = "compacting", None, None
            elif any(word in event_type for word in ("complete", "finish", "end", "done", "boundary")):
                status, detail = "running", None
            if status != previous_status:
                state_since = item.get("timestamp") or payload.get("timestamp") or state_since
            if status == "compacting" and previous_status != "compacting":
                compaction_started_at = item.get("timestamp") or payload.get("timestamp")
            if status in {"compacting", "running"}:
                activity = None
                pending_activity_calls.clear()
        elif item.get("type") == "compacted" or (item.get("type") == "system" and item.get("subtype") == "compact_boundary"):
            status, detail = "running", None
            if status != previous_status:
                state_since = item.get("timestamp") or payload.get("timestamp") or state_since
            activity = None
            pending_activity_calls.clear()
        elif item.get("type") == "event_msg" and payload_type in {"task_complete", "turn_complete"}:
            status, detail = "idle", "completed"
            sync_question = None
            clock_transition = True
            activity = None
            pending_activity_calls.clear()
        elif item.get("type") == "event_msg" and payload_type == "task_started":
            status, detail = "running", None
            question_correlation_unavailable = False
            clock_transition = True
            activity = None
            pending_activity_calls.clear()
        elif item.get("type") == "event_msg" and payload_type in {"turn_aborted", "task_failed"}:
            status, detail = "failed", "interrupted" if payload_type == "turn_aborted" else "errored"
            sync_question = None
            clock_transition = True
            activity = None
            pending_activity_calls.clear()
        elif item.get("type") == "event_msg" and payload_type == "user_message":
            status, detail = "running", None
            clock_transition = True
            resolved_blocking_question = (
                sync_question is not None and execution_status_before == "waiting"
            )
            # Ordinary user input is not evidence that async question items were answered.
            message = payload.get("message")
            if isinstance(message, dict):
                message = message.get("content")
            if isinstance(message, list):
                message = "\n".join(block.get("text", "") for block in message if isinstance(block, dict) and block.get("type") in {"input_text", "text"})
            answers = _question_reply_answers(message)
            matched = False
            for answer in answers:
                if not isinstance(answer, dict):
                    continue
                raw_id = answer.get("questionItemId")
                if not _valid_question_answer(answer.get("answer")):
                    continue
                identity = _observed_question_id(raw_id, question_aliases)
                if identity in pending_questions:
                    matched = _drop_pending_question(identity, pending_questions, question_aliases) or matched
            matched_question_reply = matched
            if matched:
                question_resolution_at = item.get("timestamp") or payload.get("timestamp")
            else:
                activity = None
                pending_activity_calls.clear()
                if session_source == "exec":
                    later_turn, superseded = start_after_exec_turn_end(
                        pending_questions, question_aliases, turn_ended_at, exec_stamp,
                    )
                    if later_turn:
                        turn_ended_at = None
                        if superseded:
                            question_resolution_at = exec_stamp
            sync_question = None
        elif item.get("type") == "event_msg" and payload_type in {"reasoning_started", "thinking_started"}:
            if not pending_activity_calls and activity is None and status not in {"idle", "failed", "compacting"}:
                activity = {"kind": "thinking", "label": "Thinking", "files": [], "observedAt": timestamp}
        elif item.get("type") == "response_item":
            name = str(payload.get("name") or "").lower()
            short_name = name.split(".")[-1]
            arguments = payload.get("arguments") or payload.get("input")
            try:
                parsed_arguments = json.loads(arguments) if isinstance(arguments, str) else arguments
            except (ValueError, TypeError):
                parsed_arguments = None
            questions = parsed_arguments.get("questions") if isinstance(parsed_arguments, dict) else None
            is_question_tool = short_name in {"request_user_input", "request_user_input_async", "ask_user_question"}
            call_id = payload.get("call_id")
            call_id = str(call_id) if isinstance(call_id, (str, int)) and not isinstance(call_id, bool) else None
            if is_question_tool:
                question_at = item.get("timestamp") or payload.get("timestamp")
                question_resolution_at = None
                if short_name == "request_user_input_async":
                    if call_id and isinstance(questions, list):
                        for index, question in enumerate(questions):
                            if not isinstance(question, dict):
                                continue
                            text = question.get("question") or question.get("title") or question.get("prompt") or question.get("header")
                            if not isinstance(text, str) or not text.strip():
                                continue
                            identity = (short_name, call_id, index)
                            pending_questions[identity] = {
                                "text": " ".join(text.split())[:200],
                                "options": _question_options({"questions": [question]}),
                                "at": question_at,
                            }
                            native_id = question.get("questionItemId") or question.get("id")
                            if isinstance(native_id, (str, int)) and not isinstance(native_id, bool):
                                matches = question_aliases.setdefault(str(native_id), [])
                                if identity not in matches:
                                    matches.append(identity)
                    elif not call_id and isinstance(questions, list):
                        question_correlation_unavailable = any(
                            isinstance(question, dict)
                            and isinstance(question.get("question") or question.get("title") or question.get("prompt") or question.get("header"), str)
                            and bool((question.get("question") or question.get("title") or question.get("prompt") or question.get("header")).strip())
                            for question in questions
                        ) or question_correlation_unavailable
                else:
                    sync_question = {"text": _question_text(arguments), "options": _question_options(arguments),
                                     "at": question_at, "call_id": call_id, "name": short_name,
                                     "question_count": len(questions) if isinstance(questions, list) else 1,
                                     "native_ids": [str(question.get("questionItemId") or question.get("id"))
                                                    for question in questions if isinstance(question, dict)
                                                    and (question.get("questionItemId") or question.get("id"))]
                                     if isinstance(questions, list) else []}
                    status, detail = "needs input", None
                    clock_transition = True
                    activity = None
                    pending_activity_calls.clear()
            elif payload_type in {"function_call", "custom_tool_call"} and short_name == "send_user_message_question_reply":
                answers = _question_reply_answers(parsed_arguments if isinstance(parsed_arguments, str) else payload.get("arguments") or payload.get("input"))
                # Some wrappers carry the complete protocol in the tool arguments string.
                if not answers and isinstance(parsed_arguments, list):
                    answers = parsed_arguments
                matched = False
                for answer in answers:
                    if not isinstance(answer, dict):
                        continue
                    if not _valid_question_answer(answer.get("answer")):
                        continue
                    identity = _observed_question_id(answer.get("questionItemId"), question_aliases)
                    if identity in pending_questions:
                        matched = _drop_pending_question(identity, pending_questions, question_aliases) or matched
                if matched:
                    question_resolution_at = item.get("timestamp") or payload.get("timestamp")
            elif payload_type == "message" and payload.get("role") == "assistant":
                content = payload.get("content")
                text = "\n".join(block.get("text", "") for block in content if isinstance(block, dict) and block.get("type") in {"output_text", "text"}) if isinstance(content, list) else content
                superseded = _tracker_question_supersessions(text)
                matched = False
                for identity in superseded:
                    if identity in pending_questions:
                        matched = _drop_pending_question(identity, pending_questions, question_aliases) or matched
                if matched:
                    question_resolution_at = item.get("timestamp") or payload.get("timestamp")
                if payload.get("channel") == "final" and not sync_question:
                    status, detail = "idle", "completed"
                    clock_transition = True
                    activity = None
                    pending_activity_calls.clear()
                elif payload.get("channel") in {"analysis", "reasoning"}:
                    if not pending_activity_calls and activity is None and status not in {"idle", "failed", "compacting"}:
                        activity = {"kind": "thinking", "label": "Thinking", "files": [], "observedAt": timestamp}
            elif payload_type in {"function_call", "custom_tool_call", "local_shell_call"}:
                status, detail = "running", None
                clock_transition = True
                call_activity = _codex_tool_activity(payload, timestamp)
                activity_call_id = payload.get("call_id") or payload.get("id")
                if activity_call_id:
                    activity_call_id = str(activity_call_id)
                    # Only current-run calls enter the shared correlation ledger;
                    # late calls must not match a later orphaned output.
                    if execution_invocation_admitted:
                        pending_activity_calls[activity_call_id] = call_activity
                    activity = call_activity
                else:
                    activity = next(reversed(pending_activity_calls.values()), None) if pending_activity_calls else None
            elif payload_type in {"function_call_output", "custom_tool_call_output"}:
                output_call_id = payload.get("call_id") or payload.get("id")
                output_call_id = str(output_call_id) if output_call_id is not None else None
                matched_call = bool(output_call_id and output_call_id in pending_activity_calls)
                matched_activity_call = matched_call
                if matched_call:
                    pending_activity_calls.pop(output_call_id, None)
                    activity = next(reversed(pending_activity_calls.values()), None) if pending_activity_calls else None
                output = payload.get("output")
                if sync_question and sync_question.get("call_id") == output_call_id:
                    try:
                        parsed_output = json.loads(output) if isinstance(output, str) else output
                    except (ValueError, TypeError):
                        parsed_output = None
                    actual_sync_answer = False
                    if isinstance(parsed_output, dict) and parsed_output.get("accepted") is not True:
                        actual_sync_answer = (sync_question.get("question_count") == 1
                                              and _valid_question_answer(parsed_output.get("answer")))
                        mapped = parsed_output.get("answers")
                        native_ids = sync_question.get("native_ids") or []
                        if isinstance(mapped, dict) and native_ids:
                            actual_sync_answer = all(
                                isinstance(mapped.get(native_id), dict)
                                and _valid_question_answer(mapped[native_id].get("answers"))
                                for native_id in native_ids
                            )
                    if actual_sync_answer:
                        resolved_blocking_question = execution_status_before == "waiting"
                        sync_question = None
                        question_resolution_at = item.get("timestamp") or payload.get("timestamp")
                        status, detail = "running", None
                        clock_transition = True
                elif not pending_questions and not sync_question and (status not in {"idle", "failed"} or matched_call):
                    status, detail = "running", None
            elif payload_type == "reasoning":
                if not pending_activity_calls and activity is None and status not in {"idle", "failed", "compacting"}:
                    activity = {"kind": "thinking", "label": "Thinking", "files": [], "observedAt": timestamp}
            elif payload.get("role") == "user":
                content = payload.get("content")
                text = content if isinstance(content, str) else "\n".join(block.get("text", "") for block in content or [] if isinstance(block, dict) and block.get("type") in {"input_text", "text"})
                answers = _question_reply_answers(text)
                matched = False
                for answer in answers:
                    if not isinstance(answer, dict):
                        continue
                    if not _valid_question_answer(answer.get("answer")):
                        continue
                    identity = _observed_question_id(answer.get("questionItemId"), question_aliases)
                    if identity in pending_questions:
                        matched = _drop_pending_question(identity, pending_questions, question_aliases) or matched
                matched_question_reply = matched
                if matched:
                    question_resolution_at = item.get("timestamp") or payload.get("timestamp")
                if not matched:
                    pending_activity_calls.clear()
                    activity = None
                    status, detail = "running", None
                    clock_transition = True
                    if session_source == "exec":
                        later_turn, superseded = start_after_exec_turn_end(
                            pending_questions, question_aliases, turn_ended_at, exec_stamp,
                        )
                        if later_turn:
                            turn_ended_at = None
                            if superseded:
                                question_resolution_at = exec_stamp
        if clock_transition and (status != previous_status or (payload_type == "task_started" and status == "running")):
            state_since = item.get("timestamp") or payload.get("timestamp")
        if status == "compacting" and previous_status != "compacting":
            compaction_started_at = item.get("timestamp") or payload.get("timestamp")
        if status != "compacting":
            compaction_started_at = None

        state.update(status=status, detail=detail, stateSince=state_since,
                     compactionStartedAt=compaction_started_at, activity=activity,
                     pendingActivityCalls=pending_activity_calls, pendingQuestions=pending_questions,
                     questionAliases=question_aliases, syncQuestion=sync_question,
                     questionResolutionAt=question_resolution_at,
                     questionCorrelationUnavailable=question_correlation_unavailable,
                     sessionSource=session_source, sessionMetaSeen=session_meta_seen,
                     turnEndedAt=turn_ended_at,
                     observedAt=observed_at,
                     recordTimestampPresent=record_timestamp_present)
        if resolved_blocking_question:
            state.update(executionStatus="unknown", executionSource="transcript",
                         executionObservedAt=None, executionSince=None,
                         executionCompactionStartedAt=None, executionActivity=None,
                         executionPendingCalls={},
                         executionResumeAt=exec_stamp if exec_transition_admitted else None)

    state = _incremental_jsonl_reduce("codex-question-state", path, initial_state, reduce_record)
    pending = state.get("pendingQuestions") or {}
    sync = state.get("syncQuestion")
    first = sync or next(iter(pending.values()), None)
    status = "needs input" if pending or sync else state.get("status", "unknown")
    options = first.get("options") if first else None
    return {"status": status, "question": first.get("text") if first else None,
            "questionAt": first.get("at") if first else None,
            "questionIds": [list(identity) for identity in pending],
            "questionResolutionAt": state.get("questionResolutionAt"),
            "questionCorrelationUnavailable": state.get("questionCorrelationUnavailable", False),
            "detail": state.get("detail"), "stateSince": state.get("stateSince"),
            "compactionStartedAt": state.get("compactionStartedAt"),
            "terminalAt": state.get("stateSince") if state.get("status") in {"idle", "failed"} else None,
            "executionStatus": state.get("executionStatus", "unknown"),
            "executionSource": state.get("executionSource", "transcript"),
            "executionObservedAt": state.get("executionObservedAt"),
            "executionSince": state.get("executionSince"),
            "executionCompactionStartedAt": state.get("executionCompactionStartedAt"),
            "executionTerminalAt": state.get("executionTerminalAt"),
            "executionActivity": state.get("executionActivity"),
            "activity": state.get("activity") if status not in {"idle", "failed", "needs input", "compacting"} else None,
            "observedAt": state.get("observedAt"),
            "recordTimestampPresent": state.get("recordTimestampPresent", False),
            "telemetryUnavailable": state.get("status") == "unknown",
            **({"options": options} if options else {})}
_PATCH_HEADER = re.compile(r"^\*\*\* (Update|Add|Delete) File: (.+?)\s*$", re.M)
_TOOL_ITEMS = {"function_call", "custom_tool_call", "local_shell_call"}
_RUN_END_EVENTS = {"task_complete", "turn_complete", "turn_aborted", "task_failed"}
_ACTIVE_STATES = {"running", "quiet", "needs input", "compacting"}


def _strings(value: Any) -> Iterable[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _strings(item)


def _patched_files(payload: dict[str, Any]) -> list[tuple[str, str]]:
    """(operation, path) from apply_patch headers in a tool call's input, arguments or shell command, in order."""
    found: list[tuple[str, str]] = []
    for key in ("input", "arguments", "action"):
        value = payload.get(key)
        if isinstance(value, str) and value.lstrip().startswith("{"):
            try:
                value = json.loads(value)
            except ValueError:
                pass
        for text in _strings(value):
            if "*** Begin Patch" in text:
                # Code-mode `exec` calls embed the patch as a JS string literal.
                if "\\n*** " in text:
                    text = text.replace("\\r\\n", "\n").replace("\\n", "\n")
                found.extend((op, path.strip()) for op, path in _PATCH_HEADER.findall(text))
    return found


_READ_TOOL_NAMES = {"read", "read_file", "read_text_file", "get_file", "cat", "head", "tail", "rg", "grep", "search", "file_search", "list_files", "find_files"}
_WRITE_TOOL_NAMES = {"apply_patch", "edit_file", "write_file", "create_file", "set_content", "add_content", "out_file", "write_text"}
_READ_COMMANDS = {"get-content", "gc", "type", "cat", "head", "tail", "rg", "grep", "select-string", "findstr"}
_WRITE_COMMANDS = {"set-content", "sc", "add-content", "ac", "out-file", "new-item", "copy-item", "move-item", "remove-item", "del", "erase", "rm", "touch", "tee"}


def _activity_paths(value: Any) -> list[str]:
    """Extract explicit file-path values from recognized tool arguments only."""
    paths: list[str] = []
    if isinstance(value, dict):
        for key, child in value.items():
            if str(key).casefold() in {"path", "file", "file_path", "filepath", "target", "target_file", "filename", "files", "paths"}:
                candidates = child if isinstance(child, list) else [child]
                for candidate in candidates:
                    if isinstance(candidate, str) and candidate.strip():
                        paths.append(candidate.strip())
            elif isinstance(child, (dict, list)):
                paths.extend(_activity_paths(child))
    return list(dict.fromkeys(paths))


def _parse_tool_arguments(value: Any) -> Any:
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            if isinstance(parsed, (dict, list)):
                return parsed
        except ValueError:
            pass
    return value


def _literal_exec_command(value: Any) -> str | None:
    """Read only literal command strings from ordinary exec arguments or JS wrappers."""
    parsed = _parse_tool_arguments(value)
    if isinstance(parsed, dict):
        command = parsed.get("cmd") or parsed.get("command")
        if isinstance(command, str):
            return command
        if isinstance(command, list) and command and all(isinstance(part, str) for part in command):
            return " ".join(command)
    if isinstance(value, str):
        commands = _scan_literal_exec_commands(value)
        return commands[0] if commands else None
    if isinstance(parsed, dict):
        for key in ("code", "source", "script"):
            nested = parsed.get(key)
            if isinstance(nested, str):
                command = _literal_exec_command(nested)
                if command is not None:
                    return command
    return None


def _literal_exec_commands(value: Any) -> list[str]:
    """Extract every literal exec_command cmd from a recorded JS wrapper."""
    parsed = _parse_tool_arguments(value)
    if isinstance(parsed, dict):
        command = parsed.get("cmd") or parsed.get("command")
        if isinstance(command, str):
            return [command]
        if isinstance(command, list) and command and all(isinstance(part, str) for part in command):
            return [" ".join(command)]
        for key in ("code", "source", "script"):
            nested = parsed.get(key)
            if isinstance(nested, str):
                found = _literal_exec_commands(nested)
                if found:
                    return found
    if isinstance(value, str):
        commands = _scan_literal_exec_commands(value)
        if commands:
            return commands
    command = _literal_exec_command(value)
    return [command] if command is not None else []


_EXEC_COMMAND_HEADER = re.compile(r"\bexec_command\s*\(\s*\{\s*cmd\s*:\s*")


def _decode_js_string(raw: str, quote: str) -> str | None:
    """Decode a quoted JS command literal without evaluating surrounding code."""
    if quote == '"':
        try:
            return json.loads('"' + raw + '"')
        except (ValueError, TypeError):
            return None
    decoded: list[str] = []
    i = 0
    escapes = {"n": "\n", "r": "\r", "t": "\t", "b": "\b", "f": "\f", "v": "\v", "0": "\0",
               "\\": "\\", "'": "'", '"': '"'}
    while i < len(raw):
        char = raw[i]
        if char != "\\":
            decoded.append(char)
            i += 1
            continue
        i += 1
        if i >= len(raw):
            return None
        escaped = raw[i]
        if escaped in {"\n", "\r"}:
            if escaped == "\r" and i + 1 < len(raw) and raw[i + 1] == "\n":
                i += 1
            i += 1
            continue
        if escaped == "x" and i + 2 < len(raw):
            try:
                decoded.append(chr(int(raw[i + 1:i + 3], 16)))
                i += 3
                continue
            except ValueError:
                return None
        if escaped == "u" and i + 4 < len(raw):
            try:
                decoded.append(chr(int(raw[i + 1:i + 5], 16)))
                i += 5
                continue
            except ValueError:
                return None
        decoded.append(escapes.get(escaped, escaped))
        i += 1
    return "".join(decoded)


def _scan_literal_exec_commands(source: str) -> list[str]:
    """Find literal exec_command cmd strings while skipping JS strings/comments."""
    if "exec_command" not in source:
        return []
    commands: list[str] = []
    i = 0
    length = len(source)
    while i < length:
        if source.startswith("//", i):
            end = source.find("\n", i + 2)
            i = length if end < 0 else end + 1
            continue
        if source.startswith("/*", i):
            end = source.find("*/", i + 2)
            i = length if end < 0 else end + 2
            continue
        char = source[i]
        if char in {"'", '"', "`"}:
            quote = char
            start = i + 1
            cursor = start
            while cursor < length:
                if source[cursor] == "\\":
                    cursor += 2
                    continue
                if source[cursor] == quote:
                    break
                cursor += 1
            i = min(length, cursor + 1)
            continue
        header = _EXEC_COMMAND_HEADER.match(source, i)
        if header:
            quote_at = header.end()
            if quote_at < length and source[quote_at] in {"'", '"'}:
                quote = source[quote_at]
                cursor = quote_at + 1
                while cursor < length:
                    if source[cursor] == "\\":
                        cursor += 2
                        continue
                    if source[cursor] == quote:
                        break
                    if source[cursor] in "\r\n":
                        break
                    cursor += 1
                if cursor < length and source[cursor] == quote:
                    command = _decode_js_string(source[quote_at + 1:cursor], quote)
                    if command is None:
                        return []
                    delimiter = cursor + 1
                    while delimiter < length and source[delimiter].isspace():
                        delimiter += 1
                    if delimiter >= length or source[delimiter] not in {",", "}"}:
                        # Concatenations and other expressions are dynamic, so
                        # none of this wrapper's calls are safe to declare.
                        return []
                    commands.append(command)
                    i = delimiter + 1
                    continue
            # A recognized call with a non-literal cmd makes the whole wrapper
            # ineligible for declaration extraction, including earlier literals.
            return []
        i += 1
    return commands


_ACTIVITY_LABELS = {
    "searching": "Searching files", "reading": "Reading file", "editing": "Editing file",
    "testing": "Running checks", "running_command": "Running command",
    "inspecting": "Inspecting page", "coordinating": "Coordinating agents",
    "checking_data": "Checking data", "using_tool": "Using tool",
    "waiting": "Waiting for result", "reading_output": "Reading command output",
    "searching_web": "Searching web", "running_code": "Running code",
}
_ACTIVITY_SECRET = re.compile(
    r"(?i)(?:\b(?:api[_-]?key|secret|(?:access[_-]?)?token|password|passwd|credential|authorization)\b\s*[:=]"
    r"|\bbearer\s+[A-Za-z0-9._~+/=-]+|(?:sk|rk|ghp|github_pat|xox[baprs])-[-A-Za-z0-9_]{12,}"
    r"|eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,})"
)


def _activity_declaration(text: Any, marker: str, *, allow_exec_pragma: bool = False) -> tuple[bool, dict[str, Any] | None]:
    """Read a strict tracker declaration from the first line of literal source."""
    if not isinstance(text, str):
        return False, None
    lines = text.splitlines()
    first_line = lines[0] if lines else ""
    prefix = f"{marker} tracker-activity:"
    if (allow_exec_pragma and marker == "//" and re.fullmatch(r"[ \t]*// @exec:[^\r\n]*", first_line)
            and len(lines) > 1):
        first_line = lines[1]
    if not first_line.startswith(prefix):
        return False, None
    match = re.fullmatch(re.escape(prefix) + r"[ \t]+(.+?)[ \t]*", first_line)
    if not match:
        return True, None
    try:
        value = json.loads(match.group(1))
    except (ValueError, TypeError):
        return True, None
    if not isinstance(value, dict) or set(value) != {"kind", "files"}:
        return True, None
    kind, files = value.get("kind"), value.get("files")
    if not isinstance(kind, str) or kind not in _ACTIVITY_LABELS or not isinstance(files, list) or len(files) > 16:
        return True, None
    clean_files = []
    for path in files:
        if (not isinstance(path, str) or not path.strip() or len(path) > 260
                or re.search(r"[\x00-\x1f\x7f-\x9f]", path)
                or re.search(r"(?i)(?:[a-z][a-z0-9+.-]*://|\bwww\.)", path)
                or _ACTIVITY_SECRET.search(path)):
            return True, None
        clean_files.append(path.strip())
    return True, {"kind": kind, "files": list(dict.fromkeys(clean_files))}


def _declared_exec_activity(value: Any, *, nested_commands: bool = False,
                             allow_exec_pragma: bool = False) -> dict[str, Any] | None:
    """Accept explicit labels only from anchored raw JS or shell command headers."""
    parsed = _parse_tool_arguments(value)
    source = parsed if isinstance(parsed, str) else None
    if isinstance(parsed, dict):
        for key in ("code", "source", "script"):
            if isinstance(parsed.get(key), str):
                source = parsed[key]
                break
    if source is not None:
        present, declaration = _activity_declaration(source, "//", allow_exec_pragma=allow_exec_pragma)
        if present:
            return declaration
    if not nested_commands:
        return None
    commands = _literal_exec_commands(value)
    if not commands:
        return None
    declarations = []
    for command in commands:
        present, declaration = _activity_declaration(command, "#")
        if not present or declaration is None:
            return None
        declarations.append(declaration)
    kinds = {item["kind"] for item in declarations}
    files = list(dict.fromkeys(path for item in declarations for path in item["files"]))
    if len(kinds) != 1 or len(files) > 16:
        return None
    return {"kind": declarations[0]["kind"], "files": files}


def _literal_apply_patch_argument(value: Any) -> str | None:
    """Extract a literal argument only from an executable JS apply_patch call."""
    parsed = _parse_tool_arguments(value)
    if isinstance(parsed, dict):
        for key in ("code", "source", "script"):
            nested = parsed.get(key)
            if isinstance(nested, str):
                literal = _literal_apply_patch_argument(nested)
                if literal is not None:
                    return literal
    if not isinstance(value, str):
        return None
    i = 0
    n = len(value)
    while i < n:
        if value.startswith("//", i):
            end = value.find("\n", i + 2)
            i = n if end < 0 else end + 1
            continue
        if value.startswith("/*", i):
            end = value.find("*/", i + 2)
            i = n if end < 0 else end + 2
            continue
        if value[i] in "'\"`":
            quote = value[i]
            i += 1
            while i < n:
                if value[i] == "\\":
                    i += 2
                elif value[i] == quote:
                    i += 1
                    break
                else:
                    i += 1
            continue
        call = re.match(r"(?<![\w$])(?:tools\s*\.\s*)?apply_patch\s*\(\s*", value[i:])
        if call:
            j = i + call.end()
            if j < n and value[j] in "'\"`":
                quote = value[j]
                start = j + 1
                j = start
                while j < n:
                    if value[j] == "\\":
                        j += 2
                    elif value[j] == quote:
                        literal = value[start:j]
                        return literal if "*** Begin Patch" in literal else None
                    else:
                        j += 1
            i += call.end()
            continue
        i += 1
    return None


def _command_activity(command: str) -> tuple[str, list[str]] | None:
    """Classify a recorded command using only its literal executable and arguments."""
    command = re.sub(r"^\s*&\s*", "", command)
    match = re.match(r"\s*(?:&\s*)?(?:[\w.-]+[/\\])*([\w.-]+)(?:\.exe)?\b(.*)$", command, re.I | re.S)
    if not match:
        return None
    tool, remainder = match.group(1).casefold(), match.group(2)
    if re.search(r"[;&|]", command):
        return "running_command", []
    if tool in {"pytest", "py.test", "ruff", "mypy", "pylint", "flake8", "coverage"}:
        return "testing", []
    if tool in {"python", "python3", "py", "uv", "poetry", "tox", "nox"} and re.search(
            r"(?:\b-m\s+(?:pytest|unittest|tox|nox)\b|\bpytest\b|\bruff\b|\bmypy\b|\bpylint\b|\bflake8\b)",
            remainder, re.I):
        return "testing", []
    if tool in {"rg", "grep", "select-string", "findstr"}:
        kind = "searching"
    elif tool in _READ_COMMANDS:
        kind = "reading"
    elif tool in _WRITE_COMMANDS:
        kind = "editing"
    else:
        return "running_command", []
    tokens = re.findall(r"(?:\"([^\"]+)\"|'([^']+)'|([^\s;&|]+))", remainder)
    candidates = [next(part for part in groups if part) for groups in tokens]
    if tool in {"rg", "grep", "select-string", "findstr"}:
        takes_value = {"-e", "--regexp", "-f", "--file", "-g", "--glob", "--iglob", "--type", "--type-add",
                       "-m", "--max-count", "-a", "--after-context", "-b", "--before-context", "-c", "--context"}
        positional = []
        skip_value = False
        for part in candidates:
            if skip_value:
                skip_value = False
                continue
            if part in takes_value:
                skip_value = True
            elif part.startswith("-"):
                continue
            else:
                positional.append(part)
        candidates = positional[1:]  # the first positional argument is the search expression
    paths = [part for part in candidates if not part.startswith("-") and
             ("/" in part or "\\" in part or bool(re.search(r"\.[A-Za-z0-9]{1,8}$", part)))]
    return kind, list(dict.fromkeys(paths))


def _codex_tool_activity(payload: dict[str, Any], timestamp: Any) -> dict[str, Any]:
    """Turn one recorded call into a privacy-safe, evidence-backed activity label."""
    raw_name = str(payload.get("name") or "").casefold()
    name = raw_name.split(".")[-1].split("__")[-1]
    args = _parse_tool_arguments(payload.get("arguments", payload.get("input")))

    def result(kind: str, files: list[str] | None = None, *, declared: bool = False) -> dict[str, Any]:
        activity = {"kind": kind, "label": _ACTIVITY_LABELS[kind], "files": files or [], "observedAt": timestamp}
        if declared:
            activity["source"] = "declared"
        detail = _codex_activity_detail(args)
        if detail:
            activity["detail"] = detail
        return activity

    if name in _WRITE_TOOL_NAMES:
        files = [path for _, path in _patched_files(payload) if path] or _activity_paths(args)
        return result("editing", files)
    if name in _READ_TOOL_NAMES:
        return result("searching" if name in {"rg", "grep", "search", "file_search", "find_files"} else "reading",
                      _activity_paths(args))
    if name == "exec":
        patch_input = _literal_apply_patch_argument(args)
        if patch_input is not None:
            patched = [path for _, path in _patched_files({"arguments": patch_input}) if path]
            if patched:
                return result("editing", list(dict.fromkeys(patched)))
        declaration = _declared_exec_activity(payload.get("arguments", payload.get("input")), nested_commands=True,
                                               allow_exec_pragma=True)
        if declaration is not None:
            return result(declaration["kind"], declaration["files"], declared=True)
        commands = _literal_exec_commands(args)
        if not commands:
            commands = _literal_exec_commands(payload.get("input") or payload.get("arguments"))
        if commands:
            classified = [_command_activity(command) for command in commands]
            if all(item is not None for item in classified):
                kinds = {item[0] for item in classified if item is not None}
                if len(kinds) == 1:
                    return result(next(iter(kinds)), list(dict.fromkeys(
                        path for item in classified if item for path in item[1])))
                return result("running_command")
        return result("running_code")
    if name in {"exec_command", "shell", "run_command", "terminal"}:
        declaration = _declared_exec_activity(payload.get("arguments", payload.get("input")), nested_commands=True)
        if declaration is not None:
            return result(declaration["kind"], declaration["files"], declared=True)
        commands = _literal_exec_commands(args) or _literal_exec_commands(payload)
        if commands:
            classified = [_command_activity(command) for command in commands]
            if all(item is not None for item in classified):
                kinds = {item[0] for item in classified if item is not None}
                if len(kinds) == 1:
                    return result(next(iter(kinds)), list(dict.fromkeys(
                        path for item in classified if item for path in item[1])))
                return result("running_command")
        return result("running_command")
    if name == "local_shell_call":
        command = _literal_exec_command(args) or _literal_exec_command(payload)
        if command is not None:
            classified = _command_activity(command)
            if classified:
                return result(classified[0], classified[1])
        return result("using_tool")
    if name in {"write_stdin", "wait_for_result", "wait_tool"}:
        return result("waiting")
    if name in {"read_command_output", "read_stdin_output", "get_command_output"}:
        return result("reading_output")
    if name in {"web_search", "search_query", "search_web"} or (
            "web" in raw_name and name == "run" and isinstance(args, dict) and "search_query" in args):
        return result("searching_web")
    if name in {"read_mcp_resource", "list_mcp_resources", "read_resource", "read_file_resource"}:
        return result("reading", _activity_paths(args))
    if name == "js" and "cua_repl" in str(payload.get("name") or "").casefold():
        code = args.get("code") if isinstance(args, dict) else args
        if isinstance(code, str) and re.search(r"\b(?:getTab|createBrowserTab|click|screenshot|navigate)\s*\(", code):
            return result("inspecting")
    if name in {"pytest", "run_tests", "test", "test_file", "check", "lint", "typecheck", "compile"}:
        return result("testing", _activity_paths(args))
    if name in {"browser", "browser_open", "open_page", "open_in_codex", "navigate", "screenshot", "inspect_page", "webpage"}:
        return result("inspecting")
    if name in {"spawn_agent", "send_message", "send_message_to_thread", "handoff_thread", "delegate", "wait_threads"}:
        return result("coordinating")
    if name in {"query", "sql_query", "database_query", "lookup", "search_records", "read_table", "spreadsheet_query"}:
        return result("checking_data")
    return result("using_tool")


def _codex_activity_detail(args: Any) -> str | None:
    """Return only a short, plain-language title explicitly supplied with the tool call."""
    if not isinstance(args, dict):
        return None
    value = args.get("title")
    if not isinstance(value, str):
        return None
    detail = value.strip()
    if not detail or len(detail) > 80 or any(ord(char) < 32 or ord(char) == 127 for char in detail):
        return None
    if any(marker in detail for marker in ("`", "{", "}", "=>", ";")):
        return None
    code_patterns = (
        r"\bawait\s+[^\s]+\s*\(",
        r"\b(?:const|let|var)\s+[A-Za-z_$][\w$]*\s*=",
        r"\bimport\s*(?:[^\n]*\s+from\s+|\()",
        r"\breturn\s+[^\n]*\(",
        r"\b(?:exec_command|node_repl)\s*\(",
    )
    if any(re.search(pattern, detail, re.I) for pattern in code_patterns):
        return None
    if re.search(r"\b(?:sk-[A-Za-z0-9_-]{12,}|Bearer\s+\S+|AKIA[0-9A-Z]{16})\b|https?://", detail, re.I):
        return None
    return detail


def _file_changes(operations: list[tuple[str, str]]) -> dict[str, int]:
    """Net effect per path over a run: created, modified or deleted (created-then-deleted is no change)."""
    first: dict[str, str] = {}
    last: dict[str, str] = {}
    for op, path in operations:
        first.setdefault(path, op)
        last[path] = op
    counts = {"created": 0, "modified": 0, "deleted": 0}
    for path, op in last.items():
        if first[path] == "Add":
            counts["created"] += op != "Delete"
        elif op == "Delete":
            counts["deleted"] += 1
        else:
            counts["modified"] += 1
    return counts


def _chat_source(value: Any) -> str | None:
    """Normalize app-server/rollout sources to app | cli | exec | subagent | review."""
    if isinstance(value, str):
        lowered = value.lower()
        if lowered in {"vscode", "appserver", "app_server", "mcp"}:
            return "app"
        if lowered in {"cli", "exec"}:
            return lowered
        if lowered.startswith("subagent"):
            return "review" if "review" in lowered else "subagent"
        return None
    if isinstance(value, dict):
        sub = value.get("subagent", value.get("subAgent"))
        if sub is None:
            return None
        text = json.dumps(sub).lower()
        return "review" if "guardian" in text or "review" in text else "subagent"
    return None


_BRIEF_TITLE_PREFIX = re.compile(r"^(?:#+\s+)?(?:\[[^\]]{1,40}\]\s*)?")


# Codex stored "·" as "?" or U+FFFD in lossy-encoded titles; a run of 1-3 "?" counts as a separator too.
_BRIEF_HEAD_SEP = re.compile(r" (?:·|\?{1,3}|�+) ")
_BRIEF_GOAL_SEP = re.compile(r" (?:—|–|\?{1,3}|�+) ")
_REPLACEMENT_CUT = re.compile(r" �+ ")
_STANDALONE_REPLACEMENT = re.compile(r"(?<!\S)�+(?!\S)\s*")


_MOJIBAKE_MARKERS = ("Â", "â€")  # "Â" and "â€": UTF-8 read back as cp1252
_CODEX_MODEL_FAMILIES = frozenset({"astra", "sol", "terra", "luna"})


def _repair_mojibake(title: str) -> str:
    """Repair UTF-8 text that was read back as cp1252, one non-ASCII run at a time."""
    if not any(marker in title for marker in _MOJIBAKE_MARKERS):
        return title

    def fix(match: re.Match[str]) -> str:
        run = match.group(0)
        try:
            repaired = run.encode("cp1252").decode("utf-8")
        except (UnicodeEncodeError, UnicodeDecodeError):
            return run
        return run if any(marker in repaired for marker in _MOJIBAKE_MARKERS) else repaired

    return re.sub(r"[^\x00-\x7f]+", fix, title)


def _tidy_codex_title(title: str, model: Any) -> str:
    """Drop brief-style prefixes ("# [Project] Terra · task"); the row already shows model and workspace."""
    title = _repair_mojibake(title)
    first = next((line for line in title.splitlines() if line.strip()), title)
    if first.startswith("/root/"):
        # Spawned agents are named by task path; show a readable task label.
        first = first.rsplit("/", 1)[-1].replace("_", " ").replace("-", " ").capitalize() or first
    tidy = _BRIEF_TITLE_PREFIX.sub("", first, count=1)
    head_sep = _BRIEF_HEAD_SEP.search(tidy)
    if head_sep:
        head, rest = tidy[:head_sep.start()], tidy[head_sep.end():]
        model_id = str(model or "").strip().lower()
        # Threads keep their brief's worker name after being resumed on another model,
        # so the Codex family names always count alongside the model id's own words.
        model_words = set(re.split(r"[^a-z]+", model_id)) | _CODEX_MODEL_FAMILIES
        if rest.strip() and head.isalpha() and head.lower() in model_words:
            # Brief-style title: the goal follows the first dash or separator; keep only the task part.
            goal = _BRIEF_GOAL_SEP.search(rest)
            task = rest[:goal.start()] if goal else rest
            tidy = task.strip() or rest.strip()
    # "task — a long goal sentence": keep the task. Short tails ("Fix bug — urgent") stay.
    dash = re.search(r" [—–] ", tidy)
    if dash and len(tidy[dash.end():].strip()) > 25 and tidy[:dash.start()].strip():
        tidy = tidy[:dash.start()].strip()
    # U+FFFD is never typed on purpose: anything after a replacement run is the goal, not the title.
    kept = _REPLACEMENT_CUT.split(tidy, maxsplit=1)[0].strip()
    if kept:
        tidy = kept
    tidy = _STANDALONE_REPLACEMENT.sub("", tidy).strip()
    return tidy or first


def _workspace_kind(workspace: str | None) -> str | None:
    """Identify Codex's dated per-chat folders so the widget won't call them projects."""
    if not workspace:
        return None
    value = str(workspace).rstrip("/\\")
    if not re.search(r"[/\\]Documents[/\\]Codex[/\\]\d{4}-\d{2}-\d{2}[/\\][^/\\]+$", value, re.I):
        return None
    try:
        if not (Path(value) / ".git").exists():
            return "codex-chat"
    except OSError:
        pass
    return None


def _is_guardian(value: Any) -> bool:
    """Codex Desktop's auto-review children: source {"subagent": {"other": "guardian"}}."""
    if not isinstance(value, dict):
        return False
    sub = value.get("subagent", value.get("subAgent"))
    return isinstance(sub, dict) and sub.get("other") == "guardian"


def _codex_run_stats(path_value: str | Path | None) -> dict[str, Any]:
    if not path_value:
        return {}
    path = Path(path_value)
    return _read_codex_run_stats(path)


def _current_context_sample_valid(event: dict[str, Any] | None, status: str,
                                  run_stats: dict[str, Any] | None = None) -> bool:
    """Keep an active row's context only when its sample belongs to this run."""
    if not event or not event.get("context_valid", True):
        return False
    if status not in _ACTIVE_STATES:
        return True
    stats = run_stats or {}
    sample_at = _timestamp_epoch(event.get("timestamp"))
    run_start = _timestamp_epoch(stats.get("runStart"))
    now = time.time()
    return sample_at is not None and run_start is not None and run_start <= sample_at <= now


def _new_run_stats_state() -> dict[str, Any]:
    return {"stats": {"toolCalls": 0, "filesEdited": 0, "sessionStart": None, "source": None,
                       "threadSource": None, "settingsModel": None, "runStart": None,
                       "runEnd": None, "lastEventAt": None, "failureReason": None,
                       "lastTokenModel": None, "lastTokenTimestamp": None,
                       "lastTokenUsage": None, "lastTokenLastUsage": None,
                       "lastTokenContextValid": False, "contextDelta": None,
                       "contextDeltaObservedAt": None, "modelChangeAt": None},
            "currentModel": None, "priorContextSample": None, "seenMeta": False,
            # insertion-ordered path -> [first operation, latest operation]
            "changes": {}}


def _reduce_run_stats(state: dict[str, Any], item: Any, now_epoch: float) -> None:
    if not isinstance(item, dict):
        return
    stats = state["stats"]
    current_model = state["currentModel"]
    prior_context_sample = state["priorContextSample"]
    kind = item.get("type")
    payload = item.get("payload") or {}
    if not isinstance(payload, dict):
        return
    stamp = item.get("timestamp")
    stamp_epoch = _timestamp_epoch(stamp)
    if stamp_epoch is not None and stamp_epoch > now_epoch:
        state["_incremental_uncacheable"] = True
        return
    if kind == "session_meta":
        # Forked rollouts append their source's meta; only the first describes this file.
        if not state["seenMeta"]:
            state["seenMeta"] = True
            stats.update(sessionStart=payload.get("timestamp") or stamp, source=payload.get("source"),
                         threadSource=payload.get("thread_source"))
        return
    if stamp:
        stats["lastEventAt"] = stamp
    payload_type = str(payload.get("type") or "").lower()
    if kind == "turn_context":
        next_model = payload.get("model")
        if isinstance(next_model, str) and next_model:
            if current_model and next_model != current_model:
                stats["lastTokenContextValid"] = False
                prior_context_sample = None
                stats.update(contextDelta=None, contextDeltaObservedAt=None)
                change_epoch = _timestamp_epoch(stamp)
                if change_epoch is not None and change_epoch <= time.time():
                    stats["modelChangeAt"] = stamp
            if (stats.get("lastTokenUsage") is not None
                    and next_model != stats.get("lastTokenModel")):
                stats["lastTokenContextValid"] = False
        current_model = next_model or current_model
        state["currentModel"] = current_model
        state["priorContextSample"] = prior_context_sample
        return
    if kind == "event_msg" and "compaction" in payload_type:
        stats["lastTokenContextValid"] = False
        prior_context_sample = None
        stats.update(contextDelta=None, contextDeltaObservedAt=None)
    if kind == "compacted" or (kind == "system" and item.get("subtype") == "compact_boundary"):
        stats["lastTokenContextValid"] = False
        prior_context_sample = None
        stats.update(contextDelta=None, contextDeltaObservedAt=None)
    if kind == "event_msg" and payload_type == "token_count":
        stats.update(contextDelta=None, contextDeltaObservedAt=None)
        info = payload.get("info") or {}
        usage = info.get("total_token_usage") if isinstance(info, dict) else None
        if isinstance(usage, dict):
            sample_at = _timestamp_epoch(stamp)
            run_start = _timestamp_epoch(stats.get("runStart"))
            last_usage = info.get("last_token_usage")
            context_total = _nonnegative_int(last_usage.get("total_tokens")) if isinstance(last_usage, dict) else None
            capacity = _positive_int(info.get("model_context_window"))
            if (sample_at is not None and sample_at <= now_epoch and run_start is not None
                    and sample_at >= run_start and current_model and context_total is not None and capacity):
                if (prior_context_sample and prior_context_sample[0] == current_model
                        and prior_context_sample[3] == capacity):
                    stats["contextDelta"] = context_total - prior_context_sample[2]
                    stats["contextDeltaObservedAt"] = stamp
                else:
                    stats.update(contextDelta=None, contextDeltaObservedAt=None)
                prior_context_sample = (current_model, sample_at, context_total, capacity)
            stats.update(lastTokenModel=current_model, lastTokenTimestamp=stamp,
                         lastTokenUsage=usage,
                         lastTokenLastUsage=last_usage if isinstance(last_usage, dict) else None,
                         lastTokenContextValid=True)
    if kind == "response_item" and payload_type in _TOOL_ITEMS:
        stats["toolCalls"] += 1
        changes = state["changes"]
        for operation, changed_path in _patched_files(payload):
            previous = changes.get(changed_path)
            if previous is None:
                changes[changed_path] = [operation, operation]
            else:
                previous[1] = operation
    elif kind == "event_msg":
        if payload_type == "task_started":
            stats.update(runStart=stamp, runEnd=None, failureReason=None)
            prior_context_sample = None
            stats.update(contextDelta=None, contextDeltaObservedAt=None)
            # A sample from the prior run remains useful for history, but cannot describe this run.
            stats["lastTokenContextValid"] = False
        elif payload_type == "user_message" and (stats["runStart"] is None or stats["runEnd"]):
            stats.update(runStart=stamp, runEnd=None, failureReason=None)
            prior_context_sample = None
            stats.update(contextDelta=None, contextDeltaObservedAt=None)
            stats["lastTokenContextValid"] = False
        elif payload_type in _RUN_END_EVENTS:
            stats["runEnd"] = stamp
            if payload_type == "turn_aborted":
                stats["failureReason"] = payload.get("reason") or "interrupted"
            elif payload_type == "task_failed":
                stats["failureReason"] = payload.get("message") or payload.get("error") or stats["failureReason"] or "errored"
            else:
                stats["failureReason"] = None
        elif payload_type == "error":
            stats["failureReason"] = payload.get("message") or payload.get("codex_error_info") or "error"
        elif payload_type == "thread_settings_applied":
            settings = payload.get("thread_settings") or {}
            stats["settingsModel"] = settings.get("model") or stats["settingsModel"] if isinstance(settings, dict) else stats["settingsModel"]
    state["currentModel"] = current_model
    state["priorContextSample"] = prior_context_sample


def _project_run_stats(state: dict[str, Any]) -> dict[str, Any]:
    stats = state["stats"]
    changes = state["changes"]
    counts = {"created": 0, "modified": 0, "deleted": 0}
    for first, last in changes.values():
        if first == "Add":
            counts["created"] += last != "Delete"
        elif last == "Delete":
            counts["deleted"] += 1
        else:
            counts["modified"] += 1
    stats["filesEdited"] = len(changes)
    stats["fileChanges"] = counts
    if stats["failureReason"] is not None:
        stats["failureReason"] = " ".join(str(stats["failureReason"]).split())[:80]
    return stats


def _read_codex_run_stats(path: Path) -> dict[str, Any]:
    """Incrementally reduce complete rollout records to per-agent statistics."""
    now_epoch = time.time()
    return _incremental_jsonl_reduce("codex-run-stats", path, _new_run_stats_state,
                                     lambda state, item: _reduce_run_stats(state, item, now_epoch),
                                     _project_run_stats, unavailable={})


def _codex_agent_stats(path_value: str | Path | None, status: str, is_subagent: bool,
                       *, run_stats: dict[str, Any] | None = None) -> dict[str, Any]:
    """toolCalls, filesEdited, runStartedAt, runEndedAt and failureReason for one chat row."""
    stats = _codex_run_stats(path_value) if run_stats is None else run_stats
    start = stats.get("sessionStart") if is_subagent else stats.get("runStart") or stats.get("sessionStart")
    ended = None if status in _ACTIVE_STATES else stats.get("runEnd") or stats.get("lastEventAt")
    return {
        "toolCalls": stats.get("toolCalls"),
        "filesEdited": stats.get("filesEdited"),
        "fileChanges": stats.get("fileChanges"),
        "runStartedAt": start,
        "activeRunStartedAt": stats.get("runStart") if status in {"running", "compacting"} else None,
        "runEndedAt": ended,
        "failureReason": stats.get("failureReason") if status == "failed" else None,
    }


def _valid_tracker_snippet(value: Any) -> bool:
    """Match the existing Runway tracker snippet field validation bounds."""
    if not isinstance(value, dict):
        return False
    for key, maximum in (("task", 48), ("doing", 80), ("next", 80), ("blocked", 100)):
        field = value.get(key)
        if key in value and field is None and key in {"next", "blocked"}:
            continue
        if (not isinstance(field, str) or not field.strip()
                or len(field.encode("utf-16-le", "surrogatepass")) // 2 > maximum):
            return False
    return True


def _new_session_insights_state() -> dict[str, Any]:
    return {"compactions": 0}


def _reduce_session_insights(result: dict[str, Any], item: Any, now_epoch: float) -> None:
    if not isinstance(item, dict):
        return
    event_epoch = _timestamp_epoch(item.get("timestamp"))
    if event_epoch is not None and event_epoch > now_epoch:
        result["_incremental_uncacheable"] = True
        return
    kind = item.get("type")
    payload = item.get("payload") or {}
    if not isinstance(payload, dict):
        payload = {}
    if kind == "session_meta" and "sessionId" not in result:
        source = payload.get("source") or {}
        spawn = ((source.get("subagent") or {}).get("thread_spawn") or {}) if isinstance(source, dict) and isinstance(source.get("subagent"), dict) else {}
        result.update({"sessionId": payload.get("id") or payload.get("session_id"),
                       "source": source,
                       "parentId": payload.get("parent_thread_id") or spawn.get("parent_thread_id"),
                       "agentName": payload.get("agent_path") or spawn.get("agent_path") or payload.get("agent_nickname"),
                       "agentPath": payload.get("agent_path") or spawn.get("agent_path"),
                       "agentNickname": payload.get("agent_nickname") or spawn.get("agent_nickname"),
                       "workspace": payload.get("cwd"),
                       "contextWindow": _positive_int(payload.get("context_window")),
                       "contextWindowId": (payload.get("context_window") or {}).get("window_id") if isinstance(payload.get("context_window"), dict) else None})
    if kind == "turn_context":
        result["model"] = payload.get("model") or result.get("model")
        reasoning = payload.get("reasoning") or {}
        result["reasoningEffort"] = payload.get("effort") or payload.get("reasoning_effort") or (reasoning.get("effort") if isinstance(reasoning, dict) else None)
        result["workspace"] = payload.get("cwd") or result.get("workspace")
    if kind == "event_msg" and payload.get("type") == "user_message":
        text = payload.get("message")
        if isinstance(text, str) and text.strip() and not text.lstrip().startswith("<"):
            result["latestRequest"] = " ".join(text.split())[:240]
            result["latestRequestAt"] = item.get("timestamp")
            result.update(latestMessageAt=item.get("timestamp"), latestMessageDirection=None,
                          latestMessageSourceId=None, latestMessageTargetId=None)
            result.setdefault("objective", result["latestRequest"])
    if kind == "response_item" and payload.get("type") == "message" and payload.get("role") == "user":
        text = " ".join(str(b.get("text") or "") for b in (payload.get("content") or []) if isinstance(b, dict))
        if text.strip() and not text.lstrip().startswith("<"):
            result["latestRequest"] = " ".join(text.split())[:240]
            result["latestRequestAt"] = item.get("timestamp")
            result.update(latestMessageAt=item.get("timestamp"), latestMessageDirection=None,
                          latestMessageSourceId=None, latestMessageTargetId=None)
    if kind == "response_item" and payload.get("type") == "agent_message":
        author, recipient = payload.get("author"), payload.get("recipient")
        source_id = author if isinstance(author, str) and author.strip() else None
        target_id = recipient if isinstance(recipient, str) and recipient.strip() else None
        direction = None
        parent = result.get("parentId")
        identity = result.get("sessionId")
        if source_id and target_id and source_id != target_id:
            if source_id == parent and target_id == identity:
                direction = "parent-to-agent"
            elif source_id == identity and target_id == parent:
                direction = "agent-to-parent"
            else:
                direction = "agent-to-agent"
        result.update(latestMessageAt=item.get("timestamp"), latestMessageDirection=direction,
                      latestMessageSourceId=source_id, latestMessageTargetId=target_id)
    if kind == "response_item" and payload.get("type") == "message" and payload.get("role") == "assistant":
        result.update(latestMessageAt=item.get("timestamp"), latestMessageDirection=None,
                      latestMessageSourceId=None, latestMessageTargetId=None)
    if (kind == "response_item" and payload.get("type") == "message" and payload.get("role") == "assistant"
            and payload.get("phase") in {"commentary", "final_answer"}):
        text = " ".join(str(b.get("text") or "") for b in (payload.get("content") or []) if isinstance(b, dict))
        if text.strip():
            result["latestUpdate"] = " ".join(text.split())[:240]
            snippets = re.findall(r"\[tracker\]([\s\S]*?)\[/tracker\]", text)
            if snippets and len(snippets[-1]) <= 1024:
                try:
                    snippet = json.loads(snippets[-1])
                    if isinstance(snippet, dict):
                        result["latestUpdate"] = "[tracker]" + json.dumps(snippet, ensure_ascii=False) + "[/tracker]"
                except (ValueError, TypeError):
                    pass
            result["latestUpdateAt"] = item.get("timestamp")
            for raw_snippet in snippets:
                if len(raw_snippet) > 1024:
                    continue
                try:
                    snippet = json.loads(raw_snippet)
                except (ValueError, TypeError):
                    continue
                if _valid_tracker_snippet(snippet):
                    result["trackerUpdate"] = "[tracker]" + json.dumps(snippet, ensure_ascii=False) + "[/tracker]"
                    result["trackerUpdateAt"] = item.get("timestamp")
    if kind == "compacted" or (kind == "system" and item.get("subtype") == "compact_boundary"):
        result["compactions"] += 1
        result["lastCompactionAt"] = item.get("timestamp")
    if item.get("customTitle"):
        result["title"] = str(item["customTitle"]).strip()
        result["customTitle"] = True
    elif item.get("aiTitle") and not result.get("customTitle"):
        result["title"] = str(item["aiTitle"]).strip()
    if kind == "user" and item.get("entrypoint"):
        result["entrypoint"] = item["entrypoint"]
    if kind in {"user", "assistant", "response_item", "event_msg", "token_usage_record"} and item.get("timestamp"):
        result["updatedAt"] = item["timestamp"]
    if kind == "response_item" and payload.get("type") in {"function_call", "custom_tool_call"}:
        result["lastTool"] = str(payload.get("name") or "")[:100]
    message = item.get("message") or {}
    content = message.get("content") or []
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") == "tool_use":
                result["lastTool"] = str(block.get("name") or "")[:100]
    if kind == "user":
        text = content if isinstance(content, str) else " ".join(str(b.get("text") or "") for b in content if isinstance(b, dict))
        if text and not text.lstrip().startswith("<"):
            if item.get("timestamp"):
                result["latestRequestAt"] = item.get("timestamp")
            if not result.get("preview"):
                result["preview"] = " ".join(text.split())[:160]


def _session_insights(path_value: str | Path | None) -> dict[str, Any]:
    """Keep only display metadata, never tool output or full messages, in the cache."""
    if not path_value:
        return {}
    path = Path(path_value)
    try:
        _stat_fingerprint_memoized(path)
    except OSError:
        _jsonl_checkpoint_remove(("codex-session-insights", _resolve_memoized(path)))
        return {}
    meta_path = path.with_suffix(".meta.json") if path.parent.name == "subagents" else None
    meta_stamp: tuple[int, ...] = ()
    if meta_path is not None:
        try:
            meta_stamp = _stat_fingerprint_memoized(meta_path)
        except OSError:
            meta_stamp = (-1, -1, -1, -1, -1)
    now_epoch = time.time()
    result = _incremental_jsonl_reduce(
        "codex-session-insights", path, _new_session_insights_state,
        lambda state, item: _reduce_session_insights(state, item, now_epoch),
        fingerprint_extra=meta_stamp, unavailable={})
    if result is None:
        return {}
    result.pop("_incremental_uncacheable", None)
    if meta_path is not None:
        result["parentId"] = path.parent.parent.name
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            meta = {}
        if not isinstance(meta, dict):
            meta = {}
        result["title"] = meta.get("description") or meta.get("agentType") or result.get("preview") or "Subagent"
        result["agentName"] = meta.get("agentType")
    return result


def _mtime(path_value: str | Path) -> float:
    try:
        return Path(path_value).stat().st_mtime
    except OSError:
        return 0.0


def _counter_delta(previous: dict[str, int] | None, current: dict[str, int], last: dict[str, Any] | None) -> dict[str, int]:
    """New usage represented by one cumulative Codex token_count event.

    Equal totals are duplicate reports. A file's first event, or a total lower
    than the previous one, cannot be diffed: resumed threads continue their
    counter in a new rollout file and Codex sometimes rewrites the counter
    lower (not to zero). The event's own per-request usage is the new usage
    there; only without it does the whole counter start a new epoch.
    """
    if previous is not None and current["total_tokens"] == previous["total_tokens"]:
        return {key: 0 for key in current}
    if previous is None or current["total_tokens"] < previous["total_tokens"]:
        if last and int(last.get("total_tokens") or 0) > 0:
            return {key: min(value, max(0, int(last.get(key) or 0))) for key, value in current.items()}
        return dict(current)
    return {key: max(0, value - previous[key]) for key, value in current.items()}


def _usage_size(usage: dict[str, Any]) -> int:
    total = 0
    for key in ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"):
        try: total += int(usage.get(key) or 0)
        except (TypeError, ValueError): continue
    return total


def _created_at(stat: os.stat_result) -> float:
    """File birth time where the platform records it; Windows reports it as st_ctime."""
    birth = getattr(stat, "st_birthtime", None)
    if birth is not None:
        return float(birth)
    return float(stat.st_ctime if os.name == "nt" else stat.st_mtime)


def _is_placeholder_model(model: Any) -> bool:
    """Telemetry-only rows must not erase the concrete model shown for a chat."""
    return str(model or "").strip().lower() in {"", "unknown", "synthetic", "<synthetic>"}


def _local_timezone():
    """The calendar used by the desktop widget, rather than UTC reporting dates."""
    return datetime.now().astimezone().tzinfo or timezone.utc


def _local_today() -> date:
    return datetime.now(_local_timezone()).date()


def _local_date_from_timestamp(value: Any, fallback_epoch: float | None = None) -> str | None:
    epoch = _timestamp_epoch(value) if value else fallback_epoch
    if epoch is None:
        return None
    return datetime.fromtimestamp(epoch, _local_timezone()).date().isoformat()


def _insights_hour_from_timestamp(value: Any, fallback_epoch: float | None = None) -> str | None:
    epoch = _timestamp_epoch(value) if value else fallback_epoch
    if epoch is None:
        return None
    return datetime.fromtimestamp(epoch, _local_timezone()).replace(
        minute=0, second=0, microsecond=0).isoformat()


def _insights_add_model(bucket: dict[str, Any], model: str, usage: dict[str, int]) -> None:
    target = bucket.setdefault(model, defaultdict(int))
    for key, value in usage.items():
        target[key] += int(value or 0)


def _last_calendar_dates(days: int, today: date | None = None) -> set[str]:
    """Return calendar dates inclusive of today (seven means today plus six days)."""
    anchor = today or _local_today()
    return {(anchor - timedelta(days=offset)).isoformat() for offset in range(days)}


def _activity_age(updated: Any, now: datetime | None = None) -> str:
    """Compact local age: date labels avoid unhelpful multi-hundred-hour ages."""
    if isinstance(updated, (int, float)):
        timestamp = float(updated) / 1000
    else:
        timestamp = _timestamp_epoch(updated)
    if timestamp is None:
        return "activity unknown"
    current = now or datetime.now(timezone.utc)
    elapsed_minutes = max(0, int((current.timestamp() - timestamp) / 60))
    if elapsed_minutes < 7 * 24 * 60:
        age = _duration_since(elapsed_minutes)
        return age if age == "just now" else f"{age} since activity"
    local_date = datetime.fromtimestamp(timestamp, _local_timezone()).date()
    return f"{local_date.strftime('%b')} {local_date.day}"


def _price_for(model: str | None, table: dict[str, tuple[float, ...]]) -> tuple[float, ...] | None:
    value = (model or "").strip().lower()
    if value.endswith(LONG_CONTEXT_SUFFIX):
        value = value[:-len(LONG_CONTEXT_SUFFIX)]
    price_key = PRICE_MODEL_ALIASES.get(value, value)
    return next((rates for key, rates in table.items() if key.lower() == price_key), None)


def _claude_context_window(model: str | None) -> int | None:
    value = "".join(char for char in (model or "").lower() if char.isalnum())
    if any(key in value for key in ("fable5", "opus5", "opus48", "sonnet5")): return 1_000_000
    if "haiku45" in value: return 200_000
    return None


def _timestamp_epoch(value: Any) -> float | None:
    if not value: return None
    try: return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError, OverflowError, OSError): return None


def _fresh_epoch(epoch: float | None, *, now: float | None = None, max_age: int = 150) -> bool:
    current = time.time() if now is None else now
    return epoch is not None and 0 <= current - epoch <= max_age


def _costs(totals: dict[str, int], model_totals: dict[str, dict[str, int]], table: dict[str, tuple[float, ...]], claude: bool = False) -> dict[str, Any]:
    cost = savings = 0.0; covered = 0; total = int(totals.get("total_tokens") or 0)
    unpriced: dict[str, int] = defaultdict(int)
    for model_key, usage in model_totals.items():
        model = (model_key[:-len(LONG_CONTEXT_SUFFIX)]
                 if model_key.endswith(LONG_CONTEXT_SUFFIX) else model_key)
        rates = _price_for(model, table)
        amount = int(usage.get("total_tokens") or 0)
        if not rates:
            unpriced[model] += amount
            continue
        if model_key.endswith(LONG_CONTEXT_SUFFIX):
            _threshold, input_multiplier, output_multiplier = next(
                (rule for name, rule in CODEX_LONG_CONTEXT.items() if _price_for(model, {name: rates})), (0, 1.0, 1.0))
            rates = (rates[0] * input_multiplier, rates[1] * input_multiplier, rates[2] * output_multiplier)
        covered += amount
        input_tokens = int(usage.get("input_tokens") or 0); cached = int(usage.get("cached_tokens") or 0)
        output = int(usage.get("output_tokens") or 0)
        if claude:
            write_5m = int(usage.get("cache_5m_tokens") or 0); write_1h = int(usage.get("cache_1h_tokens") or 0)
            cost += (input_tokens * rates[0] + cached * rates[1] + write_5m * rates[2] + write_1h * rates[3] + output * rates[4]) / 1_000_000
            savings += (cached * (rates[0] - rates[1]) - write_5m * (rates[2] - rates[0]) - write_1h * (rates[3] - rates[0])) / 1_000_000
        else:
            cost += ((input_tokens - cached) * rates[0] + cached * rates[1] + output * rates[2]) / 1_000_000
            savings += cached * (rates[0] - rates[1]) / 1_000_000
    return {"apiEquivalentCost": round(cost, 4) if covered or not total else None,
            "cacheSavings": round(savings, 4), "pricingCoverage": round(covered * 100 / total, 1) if total else 100,
            "estimated": covered != total, "priceBasis": PRICE_BASIS,
            "unpricedModels": [{"model": name, "tokens": value} for name, value in sorted(unpriced.items(), key=lambda item: -item[1]) if value]}


class ClaudeDesktopProvider:
    _file_cache: dict[tuple[str, int, int], list[dict[str, Any]]] = {}
    _cache_lock = threading.Lock()
    def __init__(self, local_app_data: str | None = None) -> None:
        local = Path(local_app_data or os.environ.get("LOCALAPPDATA", ""))
        # Claude Desktop moved its user data out of the MSIX package into
        # %LOCALAPPDATA%\Claude-Data\claude_<package>; the package path remains
        # behind with only a marker file. Prefer whichever root holds usage history.
        candidates = sorted(local.glob("Claude-Data/claude_*")) + [
            match / "LocalCache" / "Roaming" / "Claude" for match in sorted((local / "Packages").glob("Claude_*"))
        ]
        self.root = next((path for path in candidates if (path / "plan-usage-history.json").is_file()),
                         candidates[0] if candidates else Path("__missing__"))
        from .claude_hook_install import claude_config_dir
        self.transcript_root = claude_config_dir() / "projects"

    def _usage(self) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        path = self.root / "plan-usage-history.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        samples = data.get("samples") or []
        if not samples:
            raise RuntimeError("Claude usage history is empty")
        latest_org = samples[-1].get("org")
        own = [sample for sample in samples if sample.get("org") == latest_org]
        latest = next(
            (
                sample for sample in reversed(own)
                if isinstance(sample.get("u"), dict)
                and (sample["u"].get("fh") is not None or sample["u"].get("sd") is not None)
            ),
            own[-1],
        )
        return own, latest

    def _reset_at(self) -> int | None:
        path = self.root / "claude_desktop_config.json"
        candidates: list[int] = []
        now = time.time()
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            data = {}
        for key, value in _walk(data):
            if key == "resetsAt" and isinstance(value, (int, float)):
                seconds = float(value) / 1000 if value > 10_000_000_000 else float(value)
                if now - 300 <= seconds <= now + 5 * 3600 + 300:
                    candidates.append(int(seconds))

        # Claude Desktop's web layer persists the authoritative reset timestamp
        # in Electron's LevelDB store rather than plan-usage-history.json. Read
        # only recent, bounded local files and extract the timestamp without
        # loading or exposing any account/session values around it.
        storage = self.root / "Local Storage" / "leveldb"
        try:
            files = sorted(
                (item for item in storage.iterdir() if item.suffix.lower() in {".ldb", ".log"}),
                key=lambda item: item.stat().st_mtime,
                reverse=True,
            )[:12]
        except OSError:
            files = []
        pattern = re.compile(rb"resetsAt[^0-9]{0,32}([0-9]{10,13})", re.IGNORECASE)
        for item in files:
            try:
                with item.open("rb") as handle:
                    size = handle.seek(0, 2)
                    handle.seek(max(0, size - 2_000_000))
                    payload = handle.read()
            except OSError:
                continue
            for match in pattern.finditer(payload):
                raw = int(match.group(1))
                seconds = raw / 1000 if raw > 10_000_000_000 else raw
                if now - 300 <= seconds <= now + 5 * 3600 + 300:
                    candidates.append(int(seconds))
        return min(candidates) if candidates else None

    @classmethod
    def _file_usage(cls, path: Path) -> list[dict[str, Any]]:
        try:
            stat = path.stat(); key = (str(path.resolve()), stat.st_size, stat.st_mtime_ns)
        except OSError: return []
        with cls._cache_lock:
            if key in cls._file_cache: return cls._file_cache[key]
        by_id: dict[str, dict[str, Any]] = {}
        try:
            with path.open("r", encoding="utf-8", errors="ignore") as handle:
                for line_no, line in enumerate(handle):
                    try: item = json.loads(line)
                    except json.JSONDecodeError: continue
                    message = item.get("message") or ((item.get("payload") or {}).get("message")) or {}
                    usage = message.get("usage") or item.get("usage") or {}
                    if not usage: continue
                    model = message.get("model") or item.get("model") or "unknown"
                    identity = str(message.get("id") or item.get("requestId") or item.get("request_id") or f"{key[0]}:{line_no}")
                    timestamp = item.get("timestamp") or message.get("timestamp")
                    # One response is written once per content block; early
                    # blocks can carry partial output counts, so keep the most
                    # complete report instead of the first line.
                    previous = by_id.get(identity)
                    if previous and _usage_size(previous["usage"]) > _usage_size(usage): continue
                    by_id[identity] = {"id": identity, "model": model, "timestamp": timestamp, "order": line_no, "usage": usage, "session": path.stem}
        except OSError: return []
        rows = list(by_id.values())
        with cls._cache_lock:
            cls._file_cache[key] = rows
            # Old versions of a changed file cannot be reused.
            for old in list(cls._file_cache):
                if old[0] == key[0] and old != key: cls._file_cache.pop(old, None)
        return rows

    def _transcript_usage(self, days: int = 31) -> dict[str, Any]:
        window_dates = _last_calendar_dates(days)
        totals: dict[str, int] = defaultdict(int); daily: dict[str, int] = defaultdict(int)
        models: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int)); sessions: dict[str, dict[str, Any]] = defaultdict(lambda: {"tokens": 0, "turns": 0, "model": None, "contextTokens": 0, "last": ("", -1), "dailyTokens": defaultdict(int)})
        seen = set(); live_keys = set()
        try: paths = list(self.transcript_root.glob("**/*.jsonl"))
        except OSError: paths = []
        entries = []
        for path in paths:
            try: entries.append((path, path.stat()))
            except OSError: continue
        # Resumed and forked sessions copy earlier responses into a new file
        # under the new session id. Credit each response to the earliest
        # created transcript so chat totals do not depend on directory order.
        entries.sort(key=lambda entry: (_created_at(entry[1]), str(entry[0])))
        for path, stat in entries:
            live_keys.add((str(path.resolve()), stat.st_size, stat.st_mtime_ns))
            sessions[path.stem].update(_session_insights(path))
            state = _claude_transcript_state(path)
            sessions[path.stem].update(status=state["status"], question=state["question"], stateDetail=state["detail"], path=str(path))
            for row in self._file_usage(path):
                timestamp = row.get("timestamp") or datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat()
                if row["id"] in seen: continue
                seen.add(row["id"])
                local_date = _local_date_from_timestamp(timestamp, stat.st_mtime)
                if not local_date or local_date not in window_dates:
                    continue
                usage = row["usage"]; model = row["model"]
                input_tokens = int(usage.get("input_tokens") or 0); output = int(usage.get("output_tokens") or 0)
                read = int(usage.get("cache_read_input_tokens") or 0)
                creation = usage.get("cache_creation") or {}
                if creation:
                    write_5m = int(creation.get("ephemeral_5m_input_tokens") or 0)
                    write_1h = int(creation.get("ephemeral_1h_input_tokens") or 0)
                else:
                    write_5m = int(usage.get("cache_creation_input_tokens") or 0); write_1h = 0
                amount = input_tokens + output + read + write_5m + write_1h
                values = {"input_tokens": input_tokens, "output_tokens": output, "cached_tokens": read, "cache_5m_tokens": write_5m, "cache_1h_tokens": write_1h, "total_tokens": amount}
                for key, value in values.items(): totals[key] += value; models[model][key] += value
                daily[local_date] += amount
                session = sessions[row["session"]]; session["tokens"] += amount; session["turns"] += 1
                session["dailyTokens"][local_date] += amount
                marker = (str(timestamp), int(row.get("order") or 0))
                if marker >= session["last"] and not _is_placeholder_model(model):
                    session["last"] = marker; session["model"] = model; session["contextTokens"] = input_tokens + read + write_5m + write_1h
        with self._cache_lock:
            for key in list(self._file_cache):
                if key not in live_keys: self._file_cache.pop(key, None)
        return {"totals": totals, "daily": daily, "models": models, "sessions": sessions}

    @staticmethod
    def _merge_segments(transcripts: list[dict[str, Any]]) -> dict[str, Any]:
        """Combine a Desktop chat's current and prior CLI transcript segments."""
        if not transcripts:
            return {"tokens": None, "turns": None, "dailyTokens": None, "compactions": None}
        daily: dict[str, int] = defaultdict(int)
        for transcript in transcripts:
            for day, value in (transcript.get("dailyTokens") or {}).items():
                daily[day] += int(value or 0)
        compactions = [t.get("compactions") for t in transcripts if t.get("compactions") is not None]
        return {
            "tokens": sum(int(t.get("tokens") or 0) for t in transcripts) or None,
            "turns": sum(int(t.get("turns") or 0) for t in transcripts) or None,
            "dailyTokens": dict(daily),
            "compactions": sum(compactions) if compactions else None,
            "lastCompactionAt": max(filter(None, (t.get("lastCompactionAt") for t in transcripts)), default=None),
        }

    @staticmethod
    def _chat_row(session_id: str, transcript: dict[str, Any], **overrides: Any) -> dict[str, Any]:
        context = transcript.get("contextTokens") or None
        model = transcript.get("model") or overrides.pop("fallbackModel", None) or "Claude"
        window = _claude_context_window(model)
        row = {
            "id": session_id, "title": transcript.get("title") or "Untitled session", "model": model,
            "status": transcript.get("status") or "idle", "pendingQuestion": transcript.get("question"),
            "statusSource": "transcript" if transcript else "unavailable",
            "updatedAt": transcript.get("updatedAt"), "tokens": transcript.get("tokens") or None,
            "turns": transcript.get("turns"), "contextTokens": context, "contextWindow": window,
            "contextPercent": min(100, round(context * 100 / window)) if context and window else None,
            "contextSource": "Last recorded input; model capacity inferred", "tokenScope": "31-day recorded usage",
            "compactions": transcript.get("compactions"), "lastCompactionAt": transcript.get("lastCompactionAt"),
            "lastTool": transcript.get("lastTool"),
            "stateDetail": transcript.get("stateDetail"),
            # The first real request is the durable objective; later "continue"-style turns don't replace it.
            "objective": transcript.get("objective") or transcript.get("preview"),
            "dailyTokens": dict(transcript["dailyTokens"]) if "dailyTokens" in transcript else None,
            "path": transcript.get("path"),
        }
        row.update(overrides)
        return row

    def _chats(self, usage_by_session: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
        root = self.root / "claude-code-sessions"
        rows = []
        aliases: dict[str, str] = {}
        archived: set[str] = set()
        for path in root.glob("**/*.json"):
            try:
                item = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            session_id = item.get("sessionId")
            if not session_id:
                continue
            prior = item.get("priorCliSessionIds") if isinstance(item.get("priorCliSessionIds"), list) else []
            segments = [value for value in [item.get("cliSessionId") or session_id, *prior] if isinstance(value, str) and value]
            if item.get("isArchived"):
                # Archived chats stay hidden with their segments and subagents;
                # their usage still counts in the provider totals.
                archived.update(segments)
                continue
            for segment in segments:
                aliases[segment] = session_id
            current = usage_by_session.get(segments[0], {})
            merged = self._merge_segments([usage_by_session[s] for s in segments if s in usage_by_session])
            updated = item.get("lastActivityAt") or item.get("createdAt") or 0
            error = item.get("error")
            title = (item.get("title") or Path(item.get("cwd") or "Untitled").name or "Untitled").strip()
            rows.append(self._chat_row(
                session_id, current, fallbackModel=item.get("model"),
                title=title if item.get("title") else current.get("title") or title,
                status="failed" if error else current.get("status") or "idle",
                detail=str(error)[:120] if error else _activity_age(updated) if updated else "activity unknown",
                turns=item.get("completedTurns") if item.get("completedTurns") is not None else merged["turns"],
                tokens=merged["tokens"], dailyTokens=merged["dailyTokens"],
                compactions=merged["compactions"], lastCompactionAt=merged.get("lastCompactionAt") or current.get("lastCompactionAt"),
                updatedAt=max(filter(None, [_iso_from_ms(updated), *(usage_by_session[s].get("updatedAt") for s in segments if s in usage_by_session)]), default=None),
                reasoningEffort=item.get("effort") if isinstance(item.get("effort"), str) else None,
                segments=len([s for s in segments if s in usage_by_session]) or None,
                recordSource="desktop",
            ))
        cutoff = (datetime.now(timezone.utc) - timedelta(days=31)).isoformat()
        for session_id, transcript in usage_by_session.items():
            if transcript.get("parentId") or session_id in aliases or session_id in archived:
                continue
            preview = str(transcript.get("preview") or "")
            # The reset watcher's own greeting runs are not user chats.
            if preview.startswith("Reply with exactly:") and int(transcript.get("turns") or 0) <= 1:
                continue
            # Without a Desktop record, only sessions that recorded model usage in
            # the window are chats; failed one-shot CLI runs record none.
            if not transcript.get("tokens") or str(transcript.get("updatedAt") or "") < cutoff:
                continue
            title = transcript.get("title") or ("Continued session" if preview.startswith("This session is being continued") else preview[:72] or "Untitled session")
            aliases[session_id] = session_id
            rows.append(self._chat_row(session_id, transcript, title=title, recordSource="transcript",
                                       detail="No Claude Desktop record (older segment, deleted chat or CLI run)"))
        pending = {sid: t for sid, t in usage_by_session.items() if t.get("parentId") and sid not in aliases}
        while pending:
            joined = []
            for session_id, transcript in pending.items():
                parent = aliases.get(transcript.get("parentId"))
                if not parent:
                    continue
                rows.append(self._chat_row(session_id, transcript, parentId=parent,
                                           title=transcript.get("title") or "Subagent", agentName=transcript.get("agentName")))
                aliases[session_id] = session_id
                joined.append(session_id)
            if not joined:
                break
            for session_id in joined:
                pending.pop(session_id)
        rows.sort(key=lambda row: row.get("updatedAt") or "", reverse=True)
        return rows

    @_in_build_scope
    def snapshot(self) -> dict[str, Any]:
        samples, latest = self._usage()
        usage = latest.get("u") or {}
        transcript = self._transcript_usage()
        totals = transcript["totals"]
        cache_denominator = sum(int(totals[key]) for key in ("input_tokens", "cached_tokens", "cache_5m_tokens", "cache_1h_tokens"))
        cache = {
            "percent": min(100, round(int(totals["cached_tokens"]) * 100 / cache_denominator)) if cache_denominator else 0,
            "read": int(totals["cached_tokens"]), "uncachedInput": int(totals["input_tokens"]),
            "cacheCreation": int(totals["cache_5m_tokens"] + totals["cache_1h_tokens"]),
            "output": int(totals["output_tokens"]),
            **_costs(totals, transcript["models"], CLAUDE_PRICING, claude=True),
        }
        deadlines = reset_info(self.root, samples)
        reset_at = deadlines["fiveHour"] if deadlines["observedAt"] is not None else self._reset_at()
        weekly_reset = deadlines["weekly"]
        return {
            "connected": True,
            "name": "Claude",
            "accent": "#e8813f",
            "fiveHour": {"percent": _usage_percent(usage.get("fh")), "reset": _reset_label(reset_at, usage.get("fh")), "resetAt": reset_at, "lastKnownResetAt": deadlines.get("lastKnownFiveHour")},
            "weekly": {"percent": _usage_percent(usage.get("sd")), "reset": _reset_label(weekly_reset, usage.get("sd")), "resetAt": weekly_reset, "lastKnownResetAt": deadlines.get("lastKnownWeekly")},
            "cache": cache,
            "chats": self._chats(transcript["sessions"]),
            "activity": [{"date": date, "value": value} for date, value in sorted(transcript["daily"].items())],
            "activityNote": "Usage is grouped from recorded Claude transcript events in this 31-day window.",
            # This is transcript telemetry retained for the current 31-day scan,
            # not an account lifetime. Keep lifetimeTokens for existing clients.
            "tracked31Days": int(totals["total_tokens"]),
            "tracked31DaysLabel": _human_tokens(totals["total_tokens"]),
            "trackingWindowDays": 31,
            "lifetimeTokens": int(totals["total_tokens"]),
            "lastSync": _iso_from_ms(latest.get("t")),
            # Usage history is sampled separately from this snapshot build.
            "limitsUpdatedAt": _iso_from_ms(latest.get("t")),
            "source": "Claude Desktop + Claude Code transcript telemetry",
        }


class ClaudeLimitsProvider(ClaudeDesktopProvider):
    """Claude's account quota meters alone, read from Claude Desktop's local files.

    No transcript scan: this stays cheap while Claude chats are not tracked.
    """

    # Chromium rewrites its cache index, so a scan can briefly miss the newest
    # usage response and find only an older one. Never step back to it.
    _last_live: dict[str, dict[str, Any]] = {}
    _last_live_lock = threading.Lock()

    def _newest_live(self, info: dict[str, Any], now: float | None = None) -> dict[str, Any]:
        now = time.time() if now is None else now
        key = str(self.root)
        with self._last_live_lock:
            last = self._last_live.get(key)
            observed = info.get("observedAt")
            has_percent = info.get("fiveHourPercent") is not None or info.get("weeklyPercent") is not None
            if observed is not None and has_percent and (last is None or observed >= last["observedAt"]):
                self._last_live[key] = dict(info)
                return info
            if last is not None and now - last["observedAt"] <= CLAUDE_DISPLAY_USAGE_AGE_SECONDS and (
                    observed is None or observed < last["observedAt"] or not has_percent):
                return dict(last)
            return info

    @_in_build_scope
    def snapshot(self) -> dict[str, Any]:
        samples, latest = self._usage()
        usage = latest.get("u") or {}
        # Claude Desktop refetches usage only every few minutes, so a 90-second window
        # leaves the meters blank most of the time. Display accepts a response up to the
        # widget's own 10-minute quota freshness; the greeter keeps the strict default.
        info = usage_info(self.root, samples, max_usage_age=CLAUDE_DISPLAY_USAGE_AGE_SECONDS)
        info = self._newest_live(info)
        live = info["fiveHourPercent"] is not None or info["weeklyPercent"] is not None
        five = info["fiveHourPercent"] if info["fiveHourPercent"] is not None else usage.get("fh")
        weekly = info["weeklyPercent"] if info["weeklyPercent"] is not None else usage.get("sd")
        reset_at = info["fiveHour"] if info["observedAt"] is not None else self._reset_at()
        updated_at = datetime.fromtimestamp(info["observedAt"], timezone.utc).isoformat() if live else _iso_from_ms(latest.get("t"))
        return {
            "connected": True,
            "name": "Claude",
            "accent": "#e8813f",
            "limitsOnly": True,
            "fiveHour": {"percent": _usage_percent(five), "reset": _reset_label(reset_at, five), "resetAt": reset_at,
                         "lastKnownResetAt": info.get("lastKnownFiveHour")},
            "weekly": {"percent": _usage_percent(weekly), "reset": _reset_label(info["weekly"], weekly), "resetAt": info["weekly"],
                       "lastKnownResetAt": info.get("lastKnownWeekly")},
            "cache": None,
            "chats": [],
            "activity": [],
            "limitsUpdatedAt": updated_at,
            "source": "Claude Desktop local quota cache (limits only)",
        }


class CodexProvider:
    _TOKEN_EVENT_CACHE_MAX_BYTES = 48 * 1024 * 1024
    _TOKEN_EVENT_CACHE_ENTRY_OVERHEAD = 256
    _TOKEN_EVENT_PATH_LOCK_MAX = 512
    _token_event_cache: OrderedDict[tuple[str, int, int, int, int, int], list[dict[str, Any]]] = OrderedDict()
    _token_event_cache_sizes: dict[tuple[str, int, int, int, int, int], int] = {}
    _token_event_cache_bytes = 0
    _token_event_cache_lock = threading.Lock()
    _token_event_path_locks: OrderedDict[str, tuple[threading.Lock, int]] = OrderedDict()
    _token_event_path_locks_lock = threading.Lock()
    _meta_cache: dict[str, tuple[tuple[int, int, int, int, int], dict[str, Any]]] = {}

    def __init__(self, client: CodexAppServer | None = None) -> None:
        self.client = client or CodexAppServer()

    @classmethod
    def _last_token_event(cls, path_value: str | None) -> dict[str, Any] | None:
        if not path_value:
            return None
        path = Path(path_value)
        return _file_memo("codex-last-token", path, lambda: cls._read_last_token_projection(path))

    @staticmethod
    def _read_last_token_event(path: Path) -> dict[str, Any] | None:
        projection = CodexProvider._read_last_token_projection(path)
        return projection.value if isinstance(projection, _TimedProjection) else projection

    @staticmethod
    def _read_last_token_projection(path: Path) -> dict[str, Any] | None | _TimedProjection:
        try:
            with path.open("rb") as handle:
                size = handle.seek(0, 2)
                handle.seek(max(0, size - 524_288))
                lines = handle.read().decode("utf-8", errors="ignore").splitlines()
        except OSError:
            return None
        model = None
        latest = None
        now_epoch = time.time()
        earliest_future = None
        future_context_epoch = None
        future_context_models: list[tuple[float, str]] = []
        pending_future_model = None
        pending_future_compaction = None
        for line in lines:
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(event, dict):
                continue
            event_epoch = _timestamp_epoch(event.get("timestamp"))
            if event_epoch is not None and math.isfinite(event_epoch) and event_epoch > now_epoch:
                event_type = event.get("type")
                payload = event.get("payload") or {}
                if not isinstance(payload, dict):
                    continue
                payload_type = str(payload.get("type") or "").lower()
                is_token_count = event_type == "event_msg" and payload_type == "token_count"
                compaction = ((event_type == "event_msg" and "compaction" in payload_type)
                              or event_type == "compacted"
                              or (event_type == "system" and event.get("subtype") == "compact_boundary"))
                if event_type == "turn_context" and isinstance(payload, dict) and payload.get("model"):
                    future_context_epoch = (event_epoch if future_context_epoch is None
                                            else min(future_context_epoch, event_epoch))
                    next_model = payload.get("model")
                    if latest is not None and isinstance(next_model, str) and next_model:
                        if isinstance(model, str) and model:
                            if next_model != model:
                                pending_future_model = (event_epoch if pending_future_model is None
                                                        else min(pending_future_model, event_epoch))
                        else:
                            future_context_models.append((event_epoch, next_model))
                elif compaction and latest is not None:
                    pending_future_compaction = (event_epoch if pending_future_compaction is None
                                                 else min(pending_future_compaction, event_epoch))
                elif is_token_count:
                    info = payload.get("info") or {}
                    if isinstance(info, dict) and isinstance(info.get("total_token_usage"), dict):
                        earliest_future = event_epoch if earliest_future is None else min(earliest_future, event_epoch)
                continue
            payload = event.get("payload") or {}
            if not isinstance(payload, dict):
                continue
            if event.get("type") == "turn_context":
                context_model = payload.get("model") if isinstance(payload, dict) else None
                if context_model:
                    model = context_model
                    future_context_epoch = None
                continue
            payload_type = str(payload.get("type") or "").lower()
            if event.get("type") != "event_msg" or payload_type != "token_count":
                continue
            info = payload.get("info") or {}
            if not isinstance(info, dict):
                continue
            usage = info.get("total_token_usage")
            if isinstance(usage, dict):
                if future_context_epoch is not None:
                    earliest_future = (future_context_epoch if earliest_future is None
                                       else min(earliest_future, future_context_epoch))
                # A later current sample supersedes future compaction earlier in file order.
                pending_future_compaction = None
                latest = {"timestamp": event.get("timestamp"), "rate_limits": payload.get("rate_limits"),
                          "model": model, **info}
        if latest is not None:
            stats = _codex_run_stats(path)
            latest["modelChangeAt"] = stats.get("modelChangeAt")
            same_sample = (
                stats.get("lastTokenTimestamp") == latest.get("timestamp")
                and stats.get("lastTokenUsage") == latest.get("total_token_usage")
                and stats.get("lastTokenLastUsage") == latest.get("last_token_usage")
            )
            if same_sample:
                latest["model"] = stats.get("lastTokenModel")
                latest["context_valid"] = bool(stats.get("lastTokenContextValid"))
                latest["contextDelta"] = stats.get("contextDelta")
                latest["contextDeltaObservedAt"] = stats.get("contextDeltaObservedAt")
            else:
                # A bounded-tail sample without a matching full-pass anchor
                # cannot safely be paired to a model/context epoch.
                latest["context_valid"] = False
            anchor_model = stats.get("lastTokenModel") if stats.get("lastTokenContextValid") else None
            if anchor_model:
                model_deadlines = [epoch for epoch, next_model in future_context_models
                                   if next_model != anchor_model]
                if model_deadlines:
                    deadline = min(model_deadlines)
                    pending_future_model = (deadline if pending_future_model is None
                                            else min(pending_future_model, deadline))
        if pending_future_compaction is not None:
            earliest_future = (pending_future_compaction if earliest_future is None
                               else min(earliest_future, pending_future_compaction))
        if pending_future_model is not None:
            earliest_future = (pending_future_model if earliest_future is None
                               else min(earliest_future, pending_future_model))
        if earliest_future is not None and math.isfinite(earliest_future):
            return _TimedProjection(latest, earliest_future)
        return latest

    @classmethod
    def _token_events(cls, path: Path) -> list[dict[str, Any]]:
        """Read only counter telemetry, cached by immutable file state.

        Returned events are read-only: equal consecutive ``rate_limits`` dicts are
        shared between events and with the cache, so callers must never mutate them.

        Each token_count is cumulative for its session. Callers must use deltas,
        not assign a session total to its final event date.
        """
        resolved = str(path.resolve())
        try:
            key = (resolved, *_stat_fingerprint(path))
        except OSError:
            with cls._token_event_cache_lock:
                for cached_key in [item for item in cls._token_event_cache if item[0] == resolved]:
                    cls._drop_token_event_cache_entry(cached_key)
            return []
        with cls._token_event_cache_lock:
            cached = cls._token_event_cache.get(key)
            if cached is not None:
                cls._token_event_cache.move_to_end(key)
                return cached

        path_lock = cls._acquire_token_event_path_lock(resolved)
        try:
            try:
                # The file may have grown while this caller waited for its path lock.
                key = (resolved, *_stat_fingerprint(path))
            except OSError:
                with cls._token_event_cache_lock:
                    for cached_key in [item for item in cls._token_event_cache if item[0] == resolved]:
                        cls._drop_token_event_cache_entry(cached_key)
                return []
            with cls._token_event_cache_lock:
                cached = cls._token_event_cache.get(key)
                if cached is not None:
                    cls._token_event_cache.move_to_end(key)
                    return cached

            events = _CompactTokenEvents()
            model = None
            started: float | None = None
            try:
                with path.open("r", encoding="utf-8", errors="ignore") as handle:
                    for line in handle:
                        try:
                            item = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        payload = item.get("payload") or {}
                        if item.get("type") == "session_meta":
                            if started is None:
                                started = _timestamp_epoch(payload.get("timestamp"))
                            continue
                        if item.get("type") == "turn_context":
                            model = payload.get("model") or model
                            continue
                        info = payload.get("info") or {}
                        if info.get("total_token_usage"):
                            recorded = _timestamp_epoch(item.get("timestamp"))
                            events.append(
                                item.get("timestamp"), payload.get("rate_limits"), model,
                                info["total_token_usage"],
                                info.get("last_token_usage") if isinstance(info.get("last_token_usage"), dict) else None,
                                # Counters copied from a source thread before this
                                # session began are a baseline, never new usage.
                                started is not None and recorded is not None and recorded < started,
                            )
            except OSError:
                return []

            retained_bytes = cls._estimate_token_event_cache_entry_bytes(key, events)
            try:
                unchanged = key == (resolved, *_stat_fingerprint(path))
            except OSError:
                unchanged = False
            with cls._token_event_cache_lock:
                cached = cls._token_event_cache.get(key)
                if cached is not None:
                    cls._token_event_cache.move_to_end(key)
                    return cached
                for old in [item for item in cls._token_event_cache if item[0] == resolved and item != key]:
                    cls._drop_token_event_cache_entry(old)
                if unchanged and retained_bytes <= cls._TOKEN_EVENT_CACHE_MAX_BYTES:
                    while (cls._token_event_cache
                           and cls._token_event_cache_bytes + retained_bytes > cls._TOKEN_EVENT_CACHE_MAX_BYTES):
                        oldest, _ = cls._token_event_cache.popitem(last=False)
                        cls._token_event_cache_bytes -= cls._token_event_cache_sizes.pop(oldest, 0)
                    cls._token_event_cache[key] = events
                    cls._token_event_cache_sizes[key] = retained_bytes
                    cls._token_event_cache_bytes += retained_bytes
            return events
        finally:
            cls._release_token_event_path_lock(resolved, path_lock)

    @classmethod
    def _acquire_token_event_path_lock(cls, path: str) -> threading.Lock:
        with cls._token_event_path_locks_lock:
            entry = cls._token_event_path_locks.get(path)
            lock, users = entry if entry is not None else (threading.Lock(), 0)
            cls._token_event_path_locks[path] = (lock, users + 1)
            cls._token_event_path_locks.move_to_end(path)
            cls._trim_token_event_path_locks()
        lock.acquire()
        return lock

    @classmethod
    def _release_token_event_path_lock(cls, path: str, lock: threading.Lock) -> None:
        lock.release()
        with cls._token_event_path_locks_lock:
            entry = cls._token_event_path_locks.get(path)
            if entry is not None and entry[0] is lock:
                cls._token_event_path_locks[path] = (lock, max(0, entry[1] - 1))
            cls._trim_token_event_path_locks()

    @classmethod
    def _trim_token_event_path_locks(cls) -> None:
        # Active locks stay registered; idle historical paths are bounded.
        while len(cls._token_event_path_locks) > cls._TOKEN_EVENT_PATH_LOCK_MAX:
            idle = next((path for path, (_lock, users) in cls._token_event_path_locks.items() if users == 0), None)
            if idle is None:
                break
            cls._token_event_path_locks.pop(idle, None)

    @classmethod
    def _drop_token_event_cache_entry(cls, key: tuple[str, int, int, int, int, int]) -> None:
        cls._token_event_cache.pop(key, None)
        cls._token_event_cache_bytes -= cls._token_event_cache_sizes.pop(key, 0)

    @classmethod
    def _estimate_token_event_cache_entry_bytes(
        cls, key: tuple[str, int, int, int, int, int], events: list[dict[str, Any]]
    ) -> int:
        """Estimate retained projection size without double-counting shared objects."""
        seen: set[int] = set()
        pending: list[Any] = [key, events._rows if isinstance(events, _CompactTokenEvents) else events]
        total = cls._TOKEN_EVENT_CACHE_ENTRY_OVERHEAD + (
            sys.getsizeof(events) if isinstance(events, _CompactTokenEvents) else 0)
        while pending:
            value = pending.pop()
            identity = id(value)
            if identity in seen:
                continue
            seen.add(identity)
            total += sys.getsizeof(value)
            if isinstance(value, dict):
                pending.extend(value.keys())
                pending.extend(value.values())
            elif isinstance(value, (list, tuple, set, frozenset)):
                pending.extend(value)
        return total

    @classmethod
    def _thread(cls, row: dict[str, Any]) -> dict[str, Any]:
        insights = _session_insights(row.get("path"))
        recency = row.get("recencyAt") or row.get("recency_at")
        changed = row.get("updatedAt") or row.get("updated_at")
        if isinstance(recency, (int, float)) and isinstance(changed, (int, float)):
            updated = max(recency, changed)
        else:
            updated = changed or recency
        if isinstance(updated, (int, float)):
            updated_iso = datetime.fromtimestamp(updated, timezone.utc).isoformat()
        else:
            updated_iso = updated
        updated_iso = max(filter(None, [updated_iso, insights.get("updatedAt")]), default=None)
        status_value = row.get("status") or ""
        status_raw = json.dumps(status_value).lower()
        status_type = (status_value.get("type") if isinstance(status_value, dict) else str(status_value)).lower()
        recorded = _codex_rollout_state(row.get("path"))
        run_stats = _codex_run_stats(row.get("path"))
        parent_id = row.get("parentThreadId") or row.get("parentId") or insights.get("parentId")
        parent_provider = "Codex" if parent_id else None
        if not parent_id and (row.get("id") or row.get("threadId")):
            # A cached parent keeps its provider with it; a Claude link must not
            # come back looking like native Codex lineage.
            with _LIVE_LOCK:
                cached = _LIVE_INDEX.get(f"Codex:{row.get('id') or row.get('threadId')}", {})
                parent_id, parent_provider = cached.get("parentId"), cached.get("parentProvider")
        now_epoch = time.time()
        app_observed = row.get("statusObservedAt")
        if app_observed is not None:
            observed_epoch = _timestamp_epoch(app_observed)
        elif row.get("status"):
            # Keep the generated timestamp and its freshness epoch on one exact
            # clock sample; ISO formatting can round into the future.
            observed_epoch = now_epoch
            app_observed = datetime.fromtimestamp(now_epoch, timezone.utc).isoformat()
        else:
            observed_epoch = None
        app_fresh = _fresh_epoch(observed_epoch, now=now_epoch)
        terminal_epoch = _timestamp_epoch(recorded.get("terminalAt"))
        recorded_run_start = _timestamp_epoch(run_stats.get("runStart"))
        completed_agent = bool(parent_id) and recorded["status"] in {"idle", "failed"} and terminal_epoch is not None
        agent_restarted = (recorded_run_start is not None and terminal_epoch is not None
                           and recorded_run_start > terminal_epoch)
        terminal_newer_than_app = (terminal_epoch is not None and terminal_epoch <= now_epoch
                                   and observed_epoch is not None and terminal_epoch > observed_epoch)
        try:
            transcript_stat = Path(row["path"]).stat() if row.get("path") else None
            if recorded.get("recordTimestampPresent"):
                transcript_epoch = _timestamp_epoch(recorded.get("observedAt"))
                transcript_fresh = _fresh_epoch(transcript_epoch, now=now_epoch)
            else:
                transcript_epoch = transcript_stat.st_mtime if transcript_stat else None
                transcript_fresh = _fresh_epoch(transcript_epoch, now=now_epoch)
            transcript_after_app = observed_epoch is not None and transcript_epoch is not None and transcript_epoch > observed_epoch
        except OSError:
            transcript_fresh = False
            transcript_after_app = False
        app_needs_input = any(word in status_raw for word in ("input", "attention", "approval"))
        # A matched exact resolution invalidates an app-server attention label
        # until a new question or run starts; it cannot invent lifecycle state.
        if recorded.get("questionResolutionAt") and not recorded.get("question"):
            app_needs_input = False
        app_active = status_type in {"running", "active"}
        app_failed = "failed" in status_raw or "error" in status_raw
        app_idle = status_type in {"idle", "completed", "inactive"}
        # The persisted request is the authoritative signal for an unanswered
        # question. App-server snapshots can still say "active" while an
        # async request is waiting, or lag behind the transcript and say idle.
        if recorded["status"] == "needs input":
            status = "needs input"
            status_source = "transcript"
        elif terminal_newer_than_app and recorded["status"] in {"idle", "failed"}:
            status = recorded["status"]
            status_source = "transcript"
        elif app_needs_input and app_fresh:
            status = "needs input"
            status_source = "app"
        elif (recorded["status"] == "compacting" and
              ((app_fresh and app_active) or transcript_after_app or (not app_fresh and transcript_fresh))):
            status = "compacting"
            status_source = "transcript"
        elif completed_agent and not agent_restarted and app_fresh and app_active:
            # Fetch time proves the roster was refreshed, not that a child
            # whose transcript completed has started another run.
            status = recorded["status"]
            status_source = "transcript"
        elif app_fresh and app_active:
            status = "running"
            status_source = "app"
        elif app_fresh and app_failed:
            status = "failed"
            status_source = "app"
        elif app_fresh and app_idle and transcript_fresh and recorded["status"] in {"running", "compacting"}:
            status = recorded["status"]
            status_source = "transcript"
        elif app_fresh and app_idle:
            status = "idle"
            status_source = "app"
        elif recorded["status"] in {"idle", "failed"} and not recorded.get("telemetryUnavailable"):
            status = recorded["status"]
            status_source = "transcript"
        elif (transcript_fresh and recorded["status"] != "unknown") or recorded["status"] == "needs input":
            status = recorded["status"]
            status_source = "transcript"
        else:
            status = "unavailable"
            status_source = "unavailable"
        agent_path = insights.get("agentPath") or row.get("agentPath") or row.get("agent_path")
        named = row.get("name") or row.get("title") or row.get("displayName") or insights.get("title") or agent_path or insights.get("agentName")
        title = named or row.get("preview") or "Untitled task"
        title = " ".join(str(title).split())
        if title.startswith("/root/") and (row.get("parentThreadId") or insights.get("parentId")):
            # Keep the exact lineage separately; use a readable task label in the list.
            title = title.rsplit("/", 1)[-1].replace("_", " ").replace("-", " ").capitalize()
        if not named and len(title) > 72:
            title = title[:71].rstrip() + "…"
        token_event = cls._last_token_event(row.get("path")) or {}
        row_model = row.get("model")
        provider_model = row.get("modelProvider")
        model = (row_model or insights.get("model") or token_event.get("model") or run_stats.get("settingsModel")
                 or (provider_model if str(provider_model or "").lower() not in {"", "openai"} else None) or "Codex")
        title = _tidy_codex_title(title, model)
        guardian = _is_guardian(row.get("source")) or _is_guardian(run_stats.get("source")) or run_stats.get("threadSource") == "guardian_review"
        agent_role = "auto-review" if guardian else ("spawned" if parent_id else None)
        agent_stats = _codex_agent_stats(row.get("path"), status, bool(parent_id))
        if status == "failed" and not agent_stats["failureReason"]:
            agent_stats["failureReason"] = recorded["detail"] if recorded["status"] == "failed" else None
        total_usage = token_event.get("total_token_usage") or {}
        last_usage = token_event.get("last_token_usage") or {}
        context_valid = _current_context_sample_valid(token_event, status, run_stats)
        context_window = _positive_int(token_event.get("model_context_window")) if context_valid else None
        last_tokens = _nonnegative_int(last_usage.get("total_tokens") if isinstance(last_usage, dict) else None) if context_valid else None
        context_model = token_event.get("model") if context_valid else None
        if context_valid and model and model != "Codex" and model != context_model:
            context_valid = False
            context_window = None
            last_tokens = None
            context_model = None
        context_percent = (
            min(100, round(last_tokens * 100 / context_window)) if context_window and last_tokens is not None else None
        )
        declared_turns = row.get("turnCount")
        if not isinstance(declared_turns, int) or declared_turns <= 0:
            turns_list = row.get("turns")
            declared_turns = len(turns_list) if isinstance(turns_list, list) and turns_list else cls._turn_count(row.get("path"))
        workspace = row.get("cwd") or insights.get("workspace")
        workspace_kind = _workspace_kind(workspace)
        execution_app_status = ("failed" if app_failed else "idle" if app_idle
                                else "running" if app_active else None)
        execution = _codex_execution_projection(
            recorded, execution_app_status, app_fresh, app_observed,
        )
        return {
            "id": row.get("id") or row.get("threadId"),
            "title": title,
            "model": model,
            "status": status,
            "statusSource": status_source,
            "statusObservedAt": app_observed if app_fresh else None,
            "appStatus": (
                "needs input" if app_needs_input else
                "running" if app_active else
                "failed" if app_failed else
                "idle" if app_idle else None
            ) if app_fresh else None,
            "telemetryUnavailable": status_source == "unavailable",
            "metricsUnavailable": bool(recorded.get("telemetryUnavailable")),
            "stateSince": recorded.get("stateSince") if status == recorded["status"] else None,
            "compactionStartedAt": recorded.get("compactionStartedAt") if status == "compacting" else None,
            # An asynchronous question can remain unanswered while the app is running.
            "pendingQuestion": recorded["question"],
            "pendingQuestionIds": recorded.get("questionIds", []),
            "questionResolutionAt": recorded.get("questionResolutionAt"),
            "questionCorrelationUnavailable": recorded.get("questionCorrelationUnavailable", False),
            "activity": recorded.get("activity") if status not in {"idle", "failed", "needs input", "compacting", "unavailable"} else None,
            "activityUnavailable": False,
            **execution,
            **({"pendingQuestionAt": recorded["questionAt"]} if recorded.get("questionAt") else {}),
            **({"pendingOptions": recorded["options"]} if recorded.get("options") else {}),
            "stateDetail": recorded["detail"] if status == recorded["status"] else "completed" if status_type == "completed" else None,
            "objective": insights.get("objective"),
            "path": row.get("path"),
            "detail": _activity_age(updated_iso) if updated_iso else "activity unknown",
            "turns": declared_turns,
            "tokens": _positive_int(total_usage.get("total_tokens") if isinstance(total_usage, dict) else None),
            "contextTokens": last_tokens,
            "contextWindow": context_window,
            "contextPercent": context_percent,
            "contextObservedAt": token_event.get("timestamp") if context_valid else None,
            "contextModel": context_model,
            "contextDelta": (token_event.get("contextDelta") if context_valid else None),
            "contextDeltaObservedAt": (token_event.get("contextDeltaObservedAt") if context_valid else None),
            "modelChangeAt": run_stats.get("modelChangeAt"),
            "updatedAt": updated_iso,
            "parentId": parent_id,
            "parentProvider": parent_provider if parent_id else None,
            # Task paths are stable and descriptive; generated nicknames can vary
            # between ingestion sources, so keep them as secondary metadata.
            "agentName": (agent_path or insights.get("agentName") or row.get("agentNickname")
                          or insights.get("agentNickname")),
            "agentNickname": row.get("agentNickname") or insights.get("agentNickname"),
            "agentRole": agent_role,
            "source": _chat_source(row.get("source")) or _chat_source(run_stats.get("source")),
            **agent_stats,
            "reasoningEffort": insights.get("reasoningEffort"),
            "compactions": insights.get("compactions"),
            "lastCompactionAt": insights.get("lastCompactionAt"),
            "lastTool": insights.get("lastTool"),
            "workspace": workspace,
            **({"workspaceKind": workspace_kind} if workspace_kind else {}),
            "latestRequest": insights.get("latestRequest"),
            "latestRequestAt": insights.get("latestRequestAt"),
            "latestMessageAt": insights.get("latestMessageAt"),
            "latestMessageDirection": insights.get("latestMessageDirection"),
            "latestMessageSourceId": insights.get("latestMessageSourceId"),
            "latestMessageTargetId": insights.get("latestMessageTargetId"),
            "latestUpdate": insights.get("latestUpdate"),
            "latestUpdateAt": insights.get("latestUpdateAt"),
            "trackerUpdate": insights.get("trackerUpdate"),
            "trackerUpdateAt": insights.get("trackerUpdateAt"),
            "usageUpdatedAt": token_event.get("timestamp"),
            "recentTokens": cls._recent_tokens(row.get("path")),
            "lastRequest": {"input": last_usage.get("input_tokens") if context_valid else None,
                            "cached": last_usage.get("cached_input_tokens") if context_valid else None,
                            "output": last_usage.get("output_tokens") if context_valid else None},
            "statusSource": status_source,
            "contextSource": "Last recorded request; not a live token count",
            "tokenScope": "Session cumulative usage",
            "dailyTokens": cls._thread_daily(row.get("path")),
        }

    @classmethod
    def _thread_daily(cls, path_value: str | None) -> dict[str, int] | None:
        if not path_value or not Path(path_value).is_file():
            return None
        return dict(_file_memo("codex-daily", Path(path_value), lambda: cls._read_thread_daily(path_value)))

    @classmethod
    def _recent_tokens(cls, path_value: str | None) -> int | None:
        if not path_value or not Path(path_value).is_file():
            return None
        now = datetime.now(timezone.utc).timestamp()
        previous, total = None, 0
        events = cls._token_events(Path(path_value))
        if not events:
            return None
        for event in events:
            current = {"total_tokens": max(0, int((event.get("usage") or {}).get("total_tokens") or 0))}
            delta = _counter_delta(previous, current, event.get("last"))["total_tokens"]
            previous = current
            at = _timestamp_epoch(event.get("timestamp"))
            if at is not None and now-900 <= at <= now and not event.get("inherited"):
                total += delta
        return total

    @classmethod
    def _read_thread_daily(cls, path_value: str) -> dict[str, int]:
        daily: dict[str, int] = defaultdict(int)
        previous = None
        for event in cls._token_events(Path(path_value)):
            current = {"total_tokens": max(0, int((event.get("usage") or {}).get("total_tokens") or 0))}
            delta = _counter_delta(previous, current, event.get("last"))["total_tokens"]
            previous = current
            day = _local_date_from_timestamp(event.get("timestamp"))
            if day and not event.get("inherited"):
                daily[day] += delta
        return dict(daily)

    @classmethod
    def _turn_count(cls, path_value: str | None) -> int | None:
        """Count persisted turn boundaries only when the app did not provide a count."""
        if not path_value:
            return None
        path = Path(path_value)
        return _file_memo("codex-turns", path, lambda: cls._read_turn_count(path))

    @staticmethod
    def _read_turn_count(path: Path) -> int | None:
        try:
            with path.open("r", encoding="utf-8", errors="ignore") as handle:
                count = 0
                for line in handle:
                    if "turn_context" not in line:
                        continue
                    try:
                        if json.loads(line).get("type") == "turn_context":
                            count += 1
                    except json.JSONDecodeError:
                        continue
                return count or None
        except OSError:
            return None

    def _local_usage(self, days: int = 31, *, session_paths: tuple[Path, ...] | None = None) -> dict[str, Any]:
        window_dates = _last_calendar_dates(days)
        daily: dict[str, int] = defaultdict(int)
        totals: dict[str, int] = defaultdict(int)
        models: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
        daily_models: dict[str, dict[str, Any]] = defaultdict(dict)
        hourly_models: dict[str, dict[str, Any]] = defaultdict(dict)
        active_hours: set[str] = set()
        session_activity: dict[str, dict[str, Any]] = {}
        latest_rate: dict[str, Any] | None = None
        latest_rate_at = ""
        live_keys: set[tuple[str, int, int, int, int, int]] = set()
        paths: list[Path] = []
        if session_paths is None:
            session_paths = _codex_session_inventory()
        if not session_paths:
            return {"daily": daily, "totals": totals, "models": models, "rate": latest_rate,
                    "rateUpdatedAt": None, "paths": paths,
                    "insightsData": {"trackingWindowDays": days, "dailyModels": {},
                                     "hourlyModels": {}, "activeHours": [], "sessions": []}}
        for path in session_paths:
            try:
                live_keys.add((str(path.resolve()), *_stat_fingerprint(path)))
                stat = path.stat()
            except OSError:
                continue
            paths.append(path)
            # Session metadata is the authoritative local signal for child
            # sessions. Keep this read to the first record; transcript bodies
            # are already scanned once below for token counters.
            is_subagent = False
            try:
                with path.open("r", encoding="utf-8", errors="ignore") as handle:
                    first = json.loads(handle.readline()).get("payload") or {}
                source = first.get("source") or {}
                is_subagent = bool(first.get("parent_thread_id")) or _chat_source(source) in {"subagent", "review"}
            except (OSError, ValueError, AttributeError):
                pass
            events = self._token_events(path)
            if not events:
                continue
            previous: dict[str, int] | None = None
            for event in events:
                usage = event["usage"]
                current = {key: max(0, int(usage.get(key) or 0)) for key in (
                    "input_tokens", "cached_input_tokens", "cache_write_input_tokens",
                    "output_tokens", "reasoning_output_tokens", "total_tokens",
                )}
                delta = _counter_delta(previous, current, event.get("last"))
                previous = current
                timestamp = event.get("timestamp")
                if event.get("inherited"):
                    continue
                if event.get("rate_limits") and str(timestamp or "") > latest_rate_at:
                    latest_rate = event["rate_limits"]
                    latest_rate_at = str(timestamp)
                local_date = _local_date_from_timestamp(timestamp, stat.st_mtime)
                if not local_date or local_date not in window_dates:
                    continue
                for key, value in delta.items():
                    totals[key] += value
                model = event.get("model") or "unknown"
                long_rule = next((rule for name, rule in CODEX_LONG_CONTEXT.items() if _price_for(model, {name: ()}) is not None), None)
                last_input = _positive_int((event.get("last") or {}).get("input_tokens")) or 0
                if long_rule and last_input > long_rule[0]:
                    model = model + LONG_CONTEXT_SUFFIX
                models[model]["input_tokens"] += delta["input_tokens"]
                models[model]["cached_tokens"] += delta["cached_input_tokens"]
                models[model]["output_tokens"] += delta["output_tokens"]
                models[model]["total_tokens"] += delta["total_tokens"]
                insight_usage = {
                    "input_tokens": delta["input_tokens"],
                    "cached_tokens": delta["cached_input_tokens"],
                    "output_tokens": delta["output_tokens"],
                    "total_tokens": delta["total_tokens"],
                }
                _insights_add_model(daily_models[local_date], model, insight_usage)
                hour = _insights_hour_from_timestamp(timestamp, stat.st_mtime)
                if hour and delta["total_tokens"] > 0:
                    _insights_add_model(hourly_models[hour], model, insight_usage)
                    active_hours.add(hour)
                    session = session_activity.setdefault(
                        path.stem, {"id": path.stem, "subagent": is_subagent, "lastActivityAt": None})
                    if (session["lastActivityAt"] is None
                            or str(timestamp or "") > str(session["lastActivityAt"])):
                        session["lastActivityAt"] = timestamp or datetime.fromtimestamp(
                            stat.st_mtime, timezone.utc).isoformat()
                # Codex records cumulative token_count snapshots, not explicit
                # request objects. Count only snapshots that add usage and
                # expose them as observed requests, never exact turns.
                if delta["total_tokens"] > 0:
                    models[model]["observed_requests"] += 1
                if is_subagent:
                    models[model]["subagent_tokens"] += delta["total_tokens"]
                    if delta["total_tokens"] > 0:
                        models[model]["subagent_requests"] += 1
                daily[local_date] += delta["total_tokens"]
        with self._token_event_cache_lock:
            for key in list(self._token_event_cache):
                if key not in live_keys:
                    self._drop_token_event_cache_entry(key)
        return {"daily": daily, "totals": totals, "models": models, "rate": latest_rate,
                "rateUpdatedAt": latest_rate_at or None, "paths": paths,
                "insightsData": {"trackingWindowDays": days,
                                 "dailyModels": {day: dict(value) for day, value in daily_models.items()},
                                 "hourlyModels": {hour: dict(value) for hour, value in hourly_models.items()},
                                 "activeHours": sorted(active_hours),
                                 "sessions": list(session_activity.values())}}

    _WARMTH_EDGES_MIN = (2, 5, 10, 15, 20, 30, 45, 60, 90, 120, 240, 1e9)

    @classmethod
    def _cache_warmth(cls, paths: Iterable[Path]) -> dict[str, Any]:
        """Learn how long idle gaps keep this user's prompt cache warm."""
        samples: list[tuple[float, bool]] = []
        for path in paths:
            prev: dict[str, Any] | None = None
            for event in cls._token_events(path):
                last = event.get("last")
                if event.get("inherited") or not last:
                    continue
                cur_input = _positive_int(last.get("input_tokens")) or 0
                prev_input = _positive_int((prev or {}).get("input_tokens")) or 0
                cur_ts = _timestamp_epoch(event.get("timestamp"))
                if prev is not None and prev_input >= 20000 and cur_input >= 20000 and cur_ts is not None and prev["ts"] is not None:
                    gap = cur_ts - prev["ts"]
                    if gap > 30:
                        cached = _positive_int(last.get("cached_input_tokens")) or 0
                        samples.append((gap, cached / cur_input >= 0.5))
                prev = {"input_tokens": last.get("input_tokens"), "ts": cur_ts}
        informative = sum(1 for gap, _ in samples if gap > 120)
        if informative < 8:
            return {"warmSeconds": 300, "coldSeconds": 3600, "samples": informative, "learned": False}
        edges = [edge * 60 for edge in cls._WARMTH_EDGES_MIN]
        buckets = [[0, 0] for _ in edges]  # [n, hits]
        for gap, hit in samples:
            index = next(i for i, edge in enumerate(edges) if gap <= edge)
            buckets[index][0] += 1
            buckets[index][1] += int(hit)
        warm, warm_index = 0.0, -1
        for i, (n, hits) in enumerate(buckets):
            if n >= 3:
                if hits / n < 0.8:
                    break
                warm, warm_index = edges[i], i
        warm = max(120, int(warm))
        cold = None
        for i in range(warm_index + 1, len(buckets)):
            n, hits = buckets[i]
            if n >= 3 and hits / n < 0.5:
                cold = int(edges[i - 1]) if i > 0 else 0
                break
        if cold is None:
            cold = max(2 * warm, 3600)
        cold = max(cold, warm + 1)
        return {"warmSeconds": warm, "coldSeconds": int(cold), "samples": informative, "learned": True}

    @staticmethod
    def _telemetry_limit(rate: dict[str, Any] | None, key: str) -> dict[str, Any]:
        raw = (rate or {}).get(key) or {}
        return {
            "percent": _usage_percent(raw.get("usedPercent", raw.get("used_percent"))),
            "reset": _reset_label(raw.get("resetsAt", raw.get("resets_at"))),
            "resetAt": raw.get("resetsAt", raw.get("resets_at")),
        }

    def _persisted_threads(self, limit: int = 200) -> list[dict[str, Any]]:
        """Read bounded metadata omitted by older app-server thread catalogs.

        The desktop's index identifies a thread, never its live lifecycle or
        context occupancy. Those still come from its recorded events.
        """
        return _persisted_codex_threads(limit)

    def _with_agents(self, chats: list[dict[str, Any]], *, appserver_rows: bool = True,
                     session_paths: tuple[Path, ...] | None = None) -> list[dict[str, Any]]:
        """Join full-snapshot file identities to archive ownership and transcript state."""
        if session_paths is None:
            session_paths = _codex_session_inventory()
        live = set()
        paths_by_id: dict[str, list[Path]] = defaultdict(list)
        inventory: dict[str, dict[str, Any]] = {}
        metadata_parents: dict[str, set[str]] = defaultdict(set)
        for path in session_paths:
            live.add(str(path))
            try:
                fingerprint = _stat_fingerprint(path)
            except OSError:
                continue
            cached = self._meta_cache.get(str(path))
            meta = cached[1] if cached is not None and cached[0] == fingerprint else None
            if meta is None:
                try:
                    with path.open(encoding="utf-8", errors="ignore") as handle:
                        line = handle.readline()
                    meta = json.loads(line).get("payload") or {}
                except (OSError, ValueError, AttributeError):
                    continue
                try:
                    if _stat_fingerprint(path) != fingerprint:
                        continue
                except OSError:
                    continue
                if line.endswith("\n"):
                    projected = {key: meta.get(key) for key in ("id", "session_id", "parent_thread_id", "source", "agent_path", "agent_nickname")}
                    self._meta_cache[str(path)] = (fingerprint, projected)
            if not isinstance(meta, dict):
                continue
            source = meta.get("source") or {}
            subagent = source.get("subagent") if isinstance(source, dict) else None
            spawn = subagent.get("thread_spawn") or {} if isinstance(subagent, dict) else {}
            parent = meta.get("parent_thread_id") or spawn.get("parent_thread_id")
            identity = meta.get("id") or meta.get("session_id")
            if not isinstance(identity, str) or not identity.strip():
                continue
            if isinstance(parent, str) and parent.strip():
                parent = parent.strip()
                metadata_parents[identity].add(parent)
            else:
                parent = None
            paths_by_id[identity].append(path)
            candidate = {
                "id": identity, "parentThreadId": parent,
                "name": meta.get("agent_path") or spawn.get("agent_path") or meta.get("agent_nickname"),
                "path": str(path),
            }
            previous = inventory.get(identity)
            if (previous is None or _mtime(candidate["path"]) > _mtime(previous["path"])
                    or (_mtime(candidate["path"]) == _mtime(previous["path"])
                        and candidate["path"] > previous["path"])):
                inventory[identity] = candidate
        for stale in set(self._meta_cache) - live:
            self._meta_cache.pop(stale, None)
        api_by_id: dict[str, dict[str, Any]] = {}
        local_by_id: dict[str, dict[str, Any]] = {}
        api_without_id = []
        for chat in chats:
            identity = chat.get("id") or chat.get("threadId")
            if isinstance(identity, str) and identity.strip():
                (api_by_id if appserver_rows else local_by_id).setdefault(identity, chat)
            else:
                api_without_id.append(chat)
        with _LIVE_LOCK:
            prior_index = {key: dict(value) for key, value in _LIVE_INDEX.items()}
        requested_ids = set(inventory) | set(api_by_id) | set(local_by_id)
        requested_ids.update(parent for parents in metadata_parents.values() for parent in parents)
        for key, prior in prior_index.items():
            identity = prior.get("id") or key.partition(":")[2]
            if isinstance(identity, str) and identity.strip():
                requested_ids.add(identity)
            if isinstance(prior.get("parentId"), str) and prior["parentId"].strip():
                requested_ids.add(prior["parentId"].strip())
        identity_snapshot = _codex_identity_snapshot(requested_ids)
        catalog_by_id = {str(row["id"]): row for row in identity_snapshot["catalog"]
                         if isinstance(row.get("id"), str) and row["id"]}
        database_by_id = identity_snapshot["by_id"]
        failed_ids = identity_snapshot["failed_ids"]
        catalog_missing = identity_snapshot.get("catalog_missing") is True
        archived_ids = {identity for identity, row in database_by_id.items() if row.get("archived")}

        parent_values: dict[str, set[str]] = defaultdict(set)
        for identity, parents in metadata_parents.items():
            parent_values[identity].update(parents)
        for identity, row in database_by_id.items():
            parent = row.get("parentThreadId")
            if isinstance(parent, str) and parent.strip():
                parent_values[identity].add(parent.strip())
        for identity, chat in api_by_id.items():
            parent = chat.get("parentId") or chat.get("parentThreadId")
            if isinstance(parent, str) and parent.strip():
                parent_values[identity].add(parent.strip())
        for identity, chat in local_by_id.items():
            parent = chat.get("parentId") or chat.get("parentThreadId")
            if isinstance(parent, str) and parent.strip():
                parent_values[identity].add(parent.strip())
        conflicting_ids = {identity for identity, parents in parent_values.items() if len(parents) > 1}
        parent_by_id = {identity: next(iter(parents)) for identity, parents in parent_values.items()
                        if len(parents) == 1}

        rows_by_id: dict[str, dict[str, Any]] = {}
        for identity, chat in api_by_id.items():
            if identity in archived_ids or identity in conflicting_ids:
                continue
            if parent_by_id.get(identity) and not chat.get("parentId"):
                rows_by_id[identity] = {**chat, "parentId": parent_by_id[identity]}
            else:
                rows_by_id[identity] = chat

        # Catalog roots retain the existing full-snapshot history behavior.
        for identity, row in catalog_by_id.items():
            if (identity in rows_by_id or identity in conflicting_ids or identity in archived_ids
                    or parent_by_id.get(identity)):
                continue
            rows_by_id[identity] = self._safe_thread(row)

        confirmed_ids = set(rows_by_id) | {
            identity for identity, row in database_by_id.items()
            if not row.get("archived") and identity not in conflicting_ids
        } | {
            identity for identity in catalog_by_id if identity not in conflicting_ids
        }
        confirmed_ids.update(rows_by_id)
        candidate_ids = set(inventory) | set(parent_by_id.values())
        candidate_ids.update(local_by_id)
        remaining_candidates = set(candidate_ids)
        while remaining_candidates:
            before = len(rows_by_id)
            for identity in list(remaining_candidates):
                if (identity in rows_by_id or identity in archived_ids or identity in conflicting_ids
                        or (identity in failed_ids and not catalog_missing)):
                    continue
                meta_row = inventory.get(identity)
                db_row = database_by_id.get(identity) or catalog_by_id.get(identity)
                parent = parent_by_id.get(identity)
                if db_row is None:
                    # File metadata can recover a child only beneath a parent whose
                    # desktop/API identity is already confirmed. A root with no row
                    # is not proof that it is non-archived.
                    if meta_row is None or parent is None or parent not in confirmed_ids:
                        continue
                elif db_row.get("archived"):
                    continue
                row = dict(db_row or {})
                local_row = local_by_id.get(identity)
                if local_row is not None:
                    # Offline fallback rows are already normalized runtime views.
                    # Reuse only identity metadata; status/model/context fields must
                    # be re-established from the transcript, not promoted to app data.
                    local_identity = {field: local_row[field] for field in (
                        "id", "path", "title", "name", "cwd", "source", "parentId",
                        "parentThreadId", "agentName", "agentNickname", "agentRole", "agentPath",
                    ) if field in local_row}
                    row.update(local_identity)
                row["id"] = identity
                database_path = row.get("path")
                if database_path and Path(str(database_path)).is_file():
                    row["path"] = str(database_path)
                elif meta_row:
                    row["path"] = meta_row["path"]
                if meta_row and not row.get("name") and meta_row.get("name"):
                    row["name"] = meta_row["name"]
                if parent:
                    row["parentThreadId"] = parent
                chat = self._safe_thread(row)
                if chat.get("status") == "unavailable":
                    prior = prior_index.get(f"Codex:{identity}", {})
                    previous_status = prior.get("status") if prior.get("status") not in {"unavailable"} else prior.get("previousStatus")
                    same_epoch = not prior.get("path") or not chat.get("path") or str(prior.get("path")) == str(chat.get("path"))
                    last_valid = _timestamp_epoch(prior.get("lastValidAt"))
                    if same_epoch and previous_status in _ACTIVE_STATES and _fresh_epoch(last_valid):
                        for field in (
                            "stateSince", "compactionStartedAt", "pendingQuestion", "pendingQuestionAt", "pendingOptions", "pendingQuestionIds", "questionResolutionAt", "questionCorrelationUnavailable",
                            "latestRequest", "latestRequestAt", "latestMessageAt", "latestMessageDirection",
                            "latestMessageSourceId", "latestMessageTargetId", "contextTokens", "contextWindow",
                            "contextPercent", "contextObservedAt", "contextModel", "contextDelta",
                            "contextDeltaObservedAt", "modelChangeAt", "lastRequest", "model",
                            "title", "agentName", "agentNickname", "agentRole", "source",
                        ):
                            if chat.get(field) is None and prior.get(field) is not None:
                                chat[field] = prior[field]
                        chat.update(status="unavailable", previousStatus=previous_status,
                                    telemetryUnavailable=True, statusSource="unavailable", lastValidAt=prior.get("lastValidAt"))
                rows_by_id[identity] = chat
                if identity in rows_by_id:
                    confirmed_ids.add(identity)
                    remaining_candidates.discard(identity)
            if len(rows_by_id) == before:
                break

        # Keep only eligible prior truth for identities whose lookup actually
        # failed. Confirmed misses and archived rows are not stale fallbacks.
        retained_prior_ids: set[str] = set()
        if failed_ids:
            for key, prior in prior_index.items():
                identity = str(prior.get("id") or key.partition(":")[2])
                if (identity not in failed_ids or identity in rows_by_id or identity in archived_ids
                        or identity in conflicting_ids):
                    continue
                # Prior runtime truth cannot confirm a descendant's current
                # archive eligibility while its catalog lookup is failing.
                if parent_by_id.get(identity) or prior.get("parentId"):
                    continue
                current_row = inventory.get(identity) or api_by_id.get(identity) or local_by_id.get(identity)
                current_path = current_row.get("path") if current_row else None
                if prior.get("path") and current_path and str(prior["path"]) != str(current_path):
                    continue
                last_valid = _timestamp_epoch(prior.get("lastValidAt"))
                previous_status = prior.get("status") if prior.get("status") not in {"unavailable"} else prior.get("previousStatus")
                if previous_status not in _ACTIVE_STATES or not _fresh_epoch(last_valid):
                    continue
                row = {field: value for field, value in prior.items()
                       if field not in {"indexedAt"} and not field.startswith("identity")}
                row.update(id=identity, status="unavailable", previousStatus=previous_status,
                           statusSource="unavailable", telemetryUnavailable=True,
                           metricsUnavailable=True, executionStatus="unavailable", executionSource="unavailable",
                           executionObservedAt=None, executionSince=None,
                           executionCompactionStartedAt=None, executionTerminalAt=None,
                           executionActivity=None, lastValidAt=prior.get("lastValidAt"))
                rows_by_id[identity] = row
                retained_prior_ids.add(identity)
            # Keep fresh prior parent rows as idle/unavailable anchors for a
            # retained active descendant without changing their own activity.
            pending = [identity for identity, row in rows_by_id.items()
                       if row.get("status") in _ACTIVE_STATES
                       or (row.get("telemetryUnavailable")
                           and row.get("previousStatus") in _ACTIVE_STATES
                           and _fresh_epoch(_timestamp_epoch(row.get("lastValidAt"))))]
            while pending:
                child_id = pending.pop()
                child = rows_by_id[child_id]
                parent = child.get("parentId") or parent_by_id.get(child_id)
                if not isinstance(parent, str) or parent not in failed_ids or parent in rows_by_id:
                    continue
                prior = prior_index.get(f"Codex:{parent}", {})
                last_valid = _timestamp_epoch(prior.get("lastValidAt"))
                if (not prior or not _fresh_epoch(last_valid) or parent in archived_ids
                        or parent in conflicting_ids or parent_by_id.get(parent)
                        or prior.get("parentId")):
                    continue
                row = {field: value for field, value in prior.items()
                       if field not in {"indexedAt"} and not field.startswith("identity")}
                old_status = row.get("status") or row.get("previousStatus") or "unknown"
                row.update(id=parent, status="unavailable", previousStatus=old_status,
                           statusSource="unavailable", telemetryUnavailable=True,
                           metricsUnavailable=True, executionStatus="unavailable", executionSource="unavailable",
                           executionObservedAt=None, executionSince=None,
                           executionCompactionStartedAt=None, executionTerminalAt=None,
                           executionActivity=None, lastValidAt=prior.get("lastValidAt"))
                rows_by_id[parent] = row
                parent_by_id.setdefault(child_id, parent)
                pending.append(parent)

        def active_row(row: dict[str, Any]) -> bool:
            return (row.get("status") in _ACTIVE_STATES
                    or (row.get("telemetryUnavailable") and row.get("previousStatus") in _ACTIVE_STATES
                        and _fresh_epoch(_timestamp_epoch(row.get("lastValidAt")))))

        def lineage(identity: str) -> list[str] | None:
            chain = []
            seen = set()
            node = identity
            while node:
                if node in seen or node in archived_ids or node in conflicting_ids or node not in rows_by_id:
                    return None
                seen.add(node)
                chain.append(node)
                parent = parent_by_id.get(node) or rows_by_id[node].get("parentId")
                if not parent:
                    return chain
                if not isinstance(parent, str):
                    return None
                node = parent
            return chain

        def blocked_lineage(identity: str) -> bool:
            node = identity
            seen = set()
            while node:
                if node in seen or node in archived_ids or node in conflicting_ids:
                    return True
                seen.add(node)
                row = rows_by_id.get(node)
                parent = parent_by_id.get(node) or (row or {}).get("parentId")
                if not parent:
                    return False
                if parent not in rows_by_id:
                    return parent in archived_ids or parent in conflicting_ids
                node = parent
            return False

        selected = set()
        # Existing app-server roots and the existing bounded database root
        # catalog retain their full-snapshot behavior.
        for identity, row in rows_by_id.items():
            parent = parent_by_id.get(identity) or row.get("parentId")
            if (identity in api_by_id or identity in catalog_by_id) and not parent:
                selected.add(identity)
        for identity, row in rows_by_id.items():
            if not active_row(row):
                continue
            chain = lineage(identity)
            if chain:
                selected.update(chain)
            elif identity in api_by_id and not blocked_lineage(identity):
                # Preserve a genuine app-server row if its parent is absent;
                # do not synthesize a missing parent or runtime state.
                selected.add(identity)
            elif identity in retained_prior_ids and not blocked_lineage(identity):
                selected.add(identity)

        output = []
        emitted = set()
        for chat in chats:
            identity = chat.get("id") or chat.get("threadId")
            if not identity:
                if not chat.get("parentId"):
                    output.append(chat)
                continue
            identity = str(identity)
            if identity in selected and identity not in emitted:
                output.append(rows_by_id.get(identity, chat))
                emitted.add(identity)
        for identity, chat in rows_by_id.items():
            if identity in selected and identity not in emitted:
                output.append(chat)
                emitted.add(identity)

        for chat in output:
            paths = paths_by_id.get(chat.get("id")) or []
            if len(paths) > 1:
                daily: dict[str, int] = defaultdict(int)
                for path in paths:
                    for day, value in (self._thread_daily(str(path)) or {}).items():
                        daily[day] += value
                chat["dailyTokens"] = dict(daily)
                recent = [self._recent_tokens(str(path)) for path in paths]
                chat["recentTokens"] = sum(value for value in recent if value is not None) if any(value is not None for value in recent) else None
                chat["segments"] = len(paths)
        return output

    def _local_threads(self, limit: int = 10, *, session_paths: tuple[Path, ...] | None = None) -> list[dict[str, Any]]:
        if session_paths is None:
            session_paths = _codex_session_inventory()
        try:
            files = sorted(session_paths, key=lambda item: item.stat().st_mtime, reverse=True)
        except OSError:
            return []
        rows = []
        for path in files[:limit]:
            insights = _session_insights(path)
            rows.append(self._safe_thread({"id": insights.get("sessionId") or path.stem,
                                      "name": insights.get("title") or insights.get("agentName"),
                                      "preview": insights.get("preview") or "Local session · title unavailable",
                                      "path": str(path)}))
        return [row for row in rows if not row.get("parentId") or row.get("status") in _ACTIVE_STATES
                or (row.get("telemetryUnavailable") and row.get("previousStatus") in _ACTIVE_STATES
                    and _fresh_epoch(_timestamp_epoch(row.get("lastValidAt"))))]

    @classmethod
    def _safe_thread(cls, row: dict[str, Any]) -> dict[str, Any]:
        """One malformed local record must degrade only its own chat row."""
        try:
            return cls._thread(row)
        except Exception as exc:
            title = " ".join(str(row.get("name") or row.get("title") or row.get("preview") or "Unreadable chat").split())[:72]
            return {
                "id": row.get("id") or row.get("threadId"), "parentId": row.get("parentThreadId"),
                "title": title, "model": row.get("model") or "Codex", "status": "unknown", "statusSource": "unavailable",
                "updatedAt": None, "tokens": None, "dailyTokens": None, "contextTokens": None,
                "contextWindow": None, "contextPercent": None, "path": row.get("path"),
                "trackerUpdate": None, "trackerUpdateAt": None,
                "dataError": f"This chat's local record could not be read ({type(exc).__name__}: {str(exc)[:160]})",
            }

    @_in_build_scope
    def snapshot(self) -> dict[str, Any]:
        session_paths = _codex_session_inventory()
        local = self._local_usage(session_paths=session_paths)
        chat_errors: list[str] = []
        appserver_rows = True
        try:
            snap = self.client.snapshot()
            rate = snap.rate_limits.get("rateLimits") or {}
            usage_summary = snap.usage.get("summary") or {}
            buckets = snap.usage.get("dailyUsageBuckets") or []
            observed_at = datetime.now(timezone.utc).isoformat()
            chats = [self._safe_thread({**row, "statusObservedAt": observed_at}) for row in snap.threads]
            chats.sort(key=lambda row: row.get("updatedAt") or "", reverse=True)
            five_hour = {
                "percent": _usage_percent((rate.get("primary") or {}).get("usedPercent")),
                "reset": _reset_label((rate.get("primary") or {}).get("resetsAt")),
                "resetAt": (rate.get("primary") or {}).get("resetsAt"),
            }
            weekly = {
                "percent": _usage_percent((rate.get("secondary") or {}).get("usedPercent")),
                "reset": _reset_label((rate.get("secondary") or {}).get("resetsAt")),
                "resetAt": (rate.get("secondary") or {}).get("resetsAt"),
            }
            limits_updated_at = datetime.now(timezone.utc).isoformat()
            source = "Codex app server + local session telemetry"
        except CodexUnavailable:
            appserver_rows = False
            usage_summary = {}
            buckets = []
            chats = self._local_threads(session_paths=session_paths)
            five_hour = self._telemetry_limit(local.get("rate"), "primary")
            weekly = self._telemetry_limit(local.get("rate"), "secondary")
            limits_updated_at = local.get("rateUpdatedAt")
            source = "Codex local session telemetry"
        try:
            chats = self._with_agents(chats, appserver_rows=appserver_rows,
                                      session_paths=session_paths)
        except Exception as exc:  # quotas remain usable without lineage
            chat_errors.append(f"Subagent records could not be joined ({type(exc).__name__}: {str(exc)[:160]})")
        chats.sort(key=lambda row: row.get("updatedAt") or "", reverse=True)
        local_daily = local["daily"]
        local_totals = local["totals"]
        input_tokens = int(local_totals.get("input_tokens") or 0)
        cached_tokens = int(local_totals.get("cached_input_tokens") or 0)
        cache = {
            "percent": round(cached_tokens * 100 / input_tokens) if input_tokens else 0,
            "read": cached_tokens,
            "uncachedInput": max(0, input_tokens - cached_tokens),
            "cacheCreation": None,
            "output": int(local_totals["output_tokens"]) if local_totals.get("output_tokens") is not None else None,
            **_costs(local_totals, local.get("models") or {}, CODEX_PRICING),
        } if input_tokens else None
        model_usage: dict[str, dict[str, int]] = defaultdict(lambda: {
            "tokens": 0, "observedRequests": 0, "subagentTokens": 0, "subagentRequests": 0,
        })
        for model_key, usage in (local.get("models") or {}).items():
            model = model_key.removesuffix(LONG_CONTEXT_SUFFIX)
            row = model_usage[model]
            row["tokens"] += int(usage.get("total_tokens") or 0)
            row["observedRequests"] += int(usage.get("observed_requests") or 0)
            row["subagentTokens"] += int(usage.get("subagent_tokens") or 0)
            row["subagentRequests"] += int(usage.get("subagent_requests") or 0)
        if local_daily:
            buckets = [{"startDate": date, "tokens": value} for date, value in sorted(local_daily.items())]
            activity_note = (
                "Local session counters are grouped by recorded token events; a session first observed once "
                "cannot be distributed across earlier dates or models."
            )
        else:
            activity_note = "Usage is grouped from Codex app-server daily buckets."
        try:
            cache_warmth = self._cache_warmth(local.get("paths") or [])
        except Exception:  # learning is advisory; never break the snapshot
            cache_warmth = {"warmSeconds": 300, "coldSeconds": 3600, "samples": 0, "learned": False}
        tracked_tokens = int(local_totals.get("total_tokens") or 0) or sum(int(row.get("tokens") or 0) for row in buckets)
        return {
            "connected": True,
            "name": "Codex",
            "accent": "#4a95e0",
            "fiveHour": five_hour,
            "weekly": weekly,
            "cache": cache,
            "modelUsage": [
                {"model": model, **usage}
                for model, usage in sorted(model_usage.items(), key=lambda item: -item[1]["tokens"])
            ],
            "modelUsageNote": (
                "Local token_count deltas over the last 31 days. Observed requests count nonzero counter updates, "
                "not guaranteed one-per-turn; subagent totals use session parent metadata."
            ),
            "_insightsData": local.get("insightsData", {}),
            "cacheWarmth": cache_warmth,
            "chats": chats,
            "activity": [
                {"date": _local_date_from_timestamp(row.get("startDate")) or row.get("startDate"), "value": int(row.get("tokens") or 0)}
                for row in buckets if row.get("startDate")
            ],
            "activityNote": activity_note,
            "tracked31Days": tracked_tokens,
            "tracked31DaysLabel": _human_tokens(tracked_tokens),
            "trackingWindowDays": 31,
            "lifetimeTokens": max(tracked_tokens, int(usage_summary.get("lifetimeTokens") or 0)),
            "currentStreakDays": usage_summary.get("currentStreakDays"),
            "longestStreakDays": usage_summary.get("longestStreakDays"),
            "lastSync": datetime.now(timezone.utc).isoformat(),
            "limitsUpdatedAt": limits_updated_at,
            "source": source,
            "chatErrors": chat_errors + [chat["dataError"] for chat in chats if chat.get("dataError")],
        }


def _error_provider(name: str, accent: str, exc: Exception) -> dict[str, Any]:
    return {
        "connected": False,
        "name": name,
        "accent": accent,
        "error": str(exc),
        "fiveHour": {"percent": None, "reset": "not connected"},
        "weekly": {"percent": None, "reset": "not connected"},
        "cache": None,
        "chats": [],
        "activity": [],
        "activityNote": None,
        "limitsUpdatedAt": None,
    }


_LIVE_LOCK = threading.Lock()
_LIVE_INDEX: dict[str, dict[str, Any]] = {}
_LIVE_INDEX_POLICY_GENERATION: str | None = None


def _structure_stamp(provider_names: Iterable[str] | None = None) -> str:
    """Cheap fingerprint of directories that change when a chat, agent or record is created.

    Creating a file updates its parent directory's modification time, so a
    stat of these directories detects new sessions without listing every
    transcript.
    """
    names = tuple(ENABLED_PROVIDERS if provider_names is None else provider_names)
    codex_sessions = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")) / "sessions"
    roots = [(codex_sessions, 3)] if "Codex" in names else []
    if "Claude" in names:
        claude = ClaudeDesktopProvider()
        roots.extend(((claude.transcript_root, 1), (claude.root / "claude-code-sessions", 2)))
    directories: list[Path] = [root for root, _ in roots]
    for pattern_root, depth in roots:
        level = [pattern_root]
        for _ in range(depth):
            level = [child for parent in level for child in _subdirectories(parent)]
            directories.extend(level)
        if pattern_root == codex_sessions:
            # Codex agent rollouts are written under each day directory's
            # existing subagents folder. Include that folder's mtime so a new
            # child appears on the same two-second structural refresh as a chat.
            directories.extend(day / "subagents" for day in level if (day / "subagents").is_dir())
    if "Claude" in names:
        try:
            directories.extend(claude.transcript_root.glob("*/*/subagents"))
        except OSError:
            pass
    latest = 0
    for directory in directories:
        try:
            latest = max(latest, directory.stat().st_mtime_ns)
        except OSError:
            continue
    return f"{len(directories)}:{latest}"


def _subdirectories(path: Path) -> list[Path]:
    try:
        with os.scandir(path) as entries:
            return [Path(entry.path) for entry in entries if entry.is_dir(follow_symlinks=False)]
    except OSError:
        return []


def _index_live_paths(providers_list: list[dict[str, Any]], policy_generation: str | None = None) -> None:
    """Keep transcript paths server-side only; the API never exposes local paths."""
    global _LIVE_INDEX_POLICY_GENERATION
    if policy_generation is None:
        policy_generation = public_policies(POLICIES)["generation"]
    index = {}
    observed_at = datetime.now(timezone.utc).isoformat()
    with _LIVE_LOCK:
        previous_index = ({key: dict(value) for key, value in _LIVE_INDEX.items()}
                          if _LIVE_INDEX_POLICY_GENERATION == policy_generation else {})
    for provider in providers_list:
        _dedupe_provider_chats(provider)
        provider_stale = bool(provider.get("stale"))
        seen_keys = set()
        for chat in provider.get("chats") or []:
            identity = chat.get("id")
            valid_identity = ((isinstance(identity, str) and bool(identity.strip()))
                              or type(identity) is int)
            key = f"{provider.get('name')}:{identity}" if valid_identity else None
            if key is not None:
                seen_keys.add(key)
            path = chat.pop("path", None)
            if path and key is not None:
                prior = previous_index.get(key, {})
                if prior.get("path") and str(prior.get("path")) != str(path):
                    # A reused thread ID with a new rollout path starts a new
                    # runtime epoch. Do not carry the prior rollout's app or
                    # transcript observations into this index entry.
                    prior = {}
                entry = {field: chat.get(field) for field in (
                    "id", "status", "statusSource", "stateSince", "compactionStartedAt", "statusObservedAt", "appStatus",
                    "metricsUnavailable",
                    "pendingQuestion", "pendingQuestionAt", "pendingOptions", "pendingQuestionIds", "questionResolutionAt", "questionCorrelationUnavailable", "stateDetail",
                    "latestRequest", "latestRequestAt", "latestUpdate", "latestUpdateAt", "trackerUpdate", "trackerUpdateAt",
                    "latestMessageAt", "latestMessageDirection",
                    "latestMessageSourceId", "latestMessageTargetId", "contextTokens", "contextWindow",
                    "contextPercent", "contextObservedAt", "contextModel", "contextDelta",
                    "contextDeltaObservedAt", "modelChangeAt", "lastRequest", "model", "updatedAt",
                    "title", "parentId", "parentProvider", "agentName", "agentRole", "source", "previousStatus", "lastValidAt",
                    "activity", "activityUnavailable", "executionStatus", "executionSource",
                    "executionObservedAt", "executionSince", "executionCompactionStartedAt",
                    "executionTerminalAt", "executionActivity",
                )}
                if entry.get("appStatus") is None and chat.get("statusSource") == "app":
                    entry["appStatus"] = chat.get("status")
                # Carry metadata only when the current transcript is unavailable
                # or the provider snapshot itself is stale. A valid null is an
                # intentional invalidation (for example, after compaction).
                if provider_stale or chat.get("telemetryUnavailable"):
                    for field, value in prior.items():
                        if field in {"executionStatus", "executionSource", "executionObservedAt", "executionSince",
                                     "executionCompactionStartedAt", "executionTerminalAt", "executionActivity"}:
                            continue
                        if field in {"parentId", "parentProvider"}:
                            continue
                        current = entry.get(field)
                        empty_request = field == "lastRequest" and isinstance(current, dict) and not any(
                            value is not None for value in current.values()
                        )
                        if field not in entry or current is None or empty_request:
                            entry[field] = value
                    if entry.get("parentId") is None and entry.get("parentProvider") is None:
                        old_parent = prior.get("parentId")
                        if old_parent:
                            entry["parentId"] = old_parent
                            entry["parentProvider"] = prior.get("parentProvider")
                if provider_stale:
                    entry["lastValidAt"] = (prior.get("lastValidAt") or chat.get("lastValidAt")
                                             or chat.get("statusObservedAt") or prior.get("indexedAt"))
                    entry["statusObservedAt"] = prior.get("statusObservedAt")
                    if entry.get("activity"):
                        entry["activityUnavailable"] = True
                    entry.update(executionStatus="unavailable", executionSource="unavailable",
                                 executionObservedAt=None, executionSince=None,
                                 executionCompactionStartedAt=None, executionTerminalAt=None,
                                 executionActivity=None)
                    if entry.get("status") in {"running", "compacting", "needs input", "quiet"}:
                        entry["previousStatus"] = entry["status"]
                        entry["status"] = "unavailable"
                        entry["statusSource"] = "unavailable"
                        entry["telemetryUnavailable"] = True
                        entry["metricsUnavailable"] = True
                else:
                    if chat.get("statusObservedAt"):
                        entry["lastValidAt"] = chat["statusObservedAt"]
                    elif chat.get("telemetryUnavailable"):
                        entry["lastValidAt"] = prior.get("lastValidAt")
                    else:
                        entry["lastValidAt"] = observed_at
                    entry["statusObservedAt"] = chat.get("statusObservedAt") or (
                        observed_at if chat.get("statusSource") == "app" else None
                    )
                    entry["activityUnavailable"] = bool(chat.get("telemetryUnavailable") and entry.get("activity"))
                entry.update(path=str(path), indexedAt=observed_at)
                if provider_stale:
                    chat.update(status=entry.get("status"), previousStatus=entry.get("previousStatus"),
                                statusSource=entry.get("statusSource"), telemetryUnavailable=entry.get("telemetryUnavailable"),
                                metricsUnavailable=entry.get("metricsUnavailable"),
                                lastValidAt=entry.get("lastValidAt"))
                if provider_stale and not _fresh_epoch(_timestamp_epoch(entry.get("lastValidAt"))):
                    continue
                if provider_stale or chat.get("telemetryUnavailable"):
                    resolved_without_pending = bool(chat.get("questionResolutionAt")
                                                   and not chat.get("pendingQuestionIds")
                                                   and not chat.get("pendingQuestion"))
                    for field in (
                        "stateSince", "compactionStartedAt", "pendingQuestion", "pendingQuestionAt", "pendingOptions", "pendingQuestionIds", "questionResolutionAt", "questionCorrelationUnavailable",
                        "latestRequest", "latestRequestAt", "latestUpdate", "latestUpdateAt", "trackerUpdate", "trackerUpdateAt",
                        "latestMessageAt", "latestMessageDirection",
                        "latestMessageSourceId", "latestMessageTargetId", "contextTokens", "contextWindow",
                        "contextPercent", "contextObservedAt", "contextModel", "contextDelta",
                        "contextDeltaObservedAt", "modelChangeAt", "lastRequest", "model", "updatedAt",
                        "activity", "activityUnavailable", "executionStatus", "executionSource",
                        "executionObservedAt", "executionSince", "executionCompactionStartedAt",
                        "executionTerminalAt", "executionActivity",
                    ):
                        if resolved_without_pending and field in {"pendingQuestion", "pendingQuestionAt", "pendingOptions", "pendingQuestionIds"}:
                            continue
                        current = chat.get(field)
                        empty_request = field == "lastRequest" and isinstance(current, dict) and not any(
                            value is not None for value in current.values()
                        )
                        if (current is None or empty_request) and entry.get(field) is not None:
                            chat[field] = entry[field]
                index[key] = entry
                chat["lastValidAt"] = entry["lastValidAt"]
        if provider.get("name") == "Codex" and provider_stale:
            for key, prior in previous_index.items():
                previous_status = prior.get("status") if prior.get("status") not in {"unavailable"} else prior.get("previousStatus")
                last_valid = _timestamp_epoch(prior.get("lastValidAt"))
                if key in seen_keys or previous_status not in {"running", "compacting", "needs input", "quiet"}:
                    continue
                if not _fresh_epoch(last_valid):
                    continue
                entry = {**prior, "status": "unavailable", "previousStatus": previous_status,
                         "statusSource": "unavailable", "telemetryUnavailable": True, "metricsUnavailable": True,
                         "lastValidAt": prior.get("lastValidAt")}
                index[key] = entry
                provider.setdefault("chats", []).append({
                    **{field: prior.get(field) for field in (
                        "id", "title", "parentId", "agentName", "agentRole", "source", "stateSince",
                        "compactionStartedAt", "pendingQuestion", "pendingQuestionAt", "pendingOptions", "pendingQuestionIds", "questionResolutionAt", "questionCorrelationUnavailable",
                        "latestRequest", "latestRequestAt", "latestUpdate", "latestUpdateAt", "trackerUpdate", "trackerUpdateAt",
                        "latestMessageAt", "latestMessageDirection",
                        "latestMessageSourceId", "latestMessageTargetId", "contextTokens", "contextWindow",
                        "contextPercent", "contextObservedAt", "contextModel", "contextDelta",
                        "contextDeltaObservedAt", "modelChangeAt", "lastRequest", "model",
                        "activity", "activityUnavailable", "executionStatus", "executionSource",
                        "executionObservedAt", "executionSince", "executionCompactionStartedAt",
                        "executionTerminalAt", "executionActivity",
                    )},
                    "status": "unavailable", "previousStatus": previous_status,
                    "statusSource": "unavailable", "telemetryUnavailable": True, "metricsUnavailable": True,
                    "executionStatus": "unavailable", "executionSource": "unavailable",
                    "executionObservedAt": None, "executionSince": None,
                    "executionCompactionStartedAt": None, "executionTerminalAt": None,
                    "executionActivity": None,
                    "lastValidAt": prior.get("lastValidAt"),
                })
            provider["chats"] = [
                chat for chat in provider.get("chats") or []
                if not (
                    chat.get("telemetryUnavailable")
                    and not _fresh_epoch(_timestamp_epoch(chat.get("lastValidAt")))
                )
            ]
    with _LIVE_LOCK:
        _LIVE_INDEX.clear()
        _LIVE_INDEX.update(index)
        _LIVE_INDEX_POLICY_GENERATION = policy_generation


@_in_build_scope
def live_state() -> dict[str, Any]:
    """Seconds-fresh activity and state for known chats, without a full snapshot.

    File stats are cheap and state parsing is memoized per file stamp, so only
    transcripts that changed since the last poll are re-read.
    """
    policies = POLICIES
    full_collect, _limit_collect = _provider_collection_sets(policies)
    visible_full = tuple(name for name in full_collect if policy_for(name, policies).allows("show"))
    policy_payload = public_policies(policies)
    policy_generation = policy_payload["generation"]
    # Refresh identity cheaply from the bounded SQLite index before taking the
    # live roster copy. This does no app-server work and never promotes SQLite
    # timestamps or model columns into runtime observations.
    persisted = _persisted_codex_threads() if "Codex" in visible_full else []
    global _LIVE_INDEX_POLICY_GENERATION
    with _LIVE_LOCK:
        if _LIVE_INDEX_POLICY_GENERATION != policy_generation:
            _LIVE_INDEX.clear()
            _LIVE_INDEX_POLICY_GENERATION = policy_generation
        for record in persisted:
            identity, path_value = record.get("id"), record.get("path")
            if not identity or not path_value:
                continue
            key = f"Codex:{identity}"
            prior = _LIVE_INDEX.get(key)
            path = str(path_value)
            identity_fields = {
                "identityTitle": _tidy_codex_title(record.get("threadName") or record.get("title") or record.get("name") or "",
                                                   record.get("model")) or None,
                "identityWorkspace": record.get("cwd"),
                "identityParentId": record.get("parentThreadId"),
                "identityParentProvider": "Codex" if record.get("parentThreadId") else None,
                "identitySource": record.get("source"),
                # A database name is an agent label only when lineage confirms
                # this is a child. Root thread names remain titles.
                "identityAgentName": ((record.get("threadName") or record.get("agentPath"))
                                      if record.get("parentThreadId") else None),
            }
            if prior and str(prior.get("path")) == path:
                # Preserve app/transcript observations for the same rollout;
                # update only the identity fallback metadata.
                prior.update(identity_fields)
            else:
                # A path change is a new recorded run for this identity. Keep
                # only the new path and DB identity fallbacks so old status,
                # model, context, or app observations cannot bleed across runs.
                _LIVE_INDEX[key] = {"id": identity, "path": path, **identity_fields}
        index = {key: dict(value) for key, value in _LIVE_INDEX.items()
                 if key.partition(":")[0] in visible_full}
    chats = []
    for key, indexed in index.items():
        path_value = indexed["path"]
        path = Path(path_value)
        try:
            modified = _stat_memoized(path).st_mtime
        except OSError:
            if key.startswith("Codex:"):
                last_valid = _timestamp_epoch(indexed.get("lastValidAt"))
                if not _fresh_epoch(last_valid):
                    continue
                retained = {field: value for field, value in indexed.items()
                            if field not in {"path", "indexedAt"} and not field.startswith("identity")}
                retained["key"] = key
                if indexed.get("id"):
                    retained["id"] = indexed["id"]
                if indexed.get("identityParentId"):
                    retained["parentId"] = indexed["identityParentId"]
                    retained["parentProvider"] = indexed.get("identityParentProvider") or "Codex"
                if indexed.get("identityTitle"):
                    retained["title"] = indexed["identityTitle"]
                if indexed.get("identityWorkspace"):
                    retained["workspace"] = indexed["identityWorkspace"]
                if indexed.get("identitySource"):
                    retained["source"] = indexed["identitySource"]
                if indexed.get("identityAgentName"):
                    retained["agentName"] = indexed["identityAgentName"]
                retained["metricsUnavailable"] = True
                previous_status = indexed.get("status") if indexed.get("status") != "unavailable" else indexed.get("previousStatus")
                observed_epoch = _timestamp_epoch(indexed.get("statusObservedAt"))
                app_fresh = _fresh_epoch(observed_epoch) and indexed.get("statusSource") != "unavailable"
                if app_fresh:
                    stored_terminal_child = bool(indexed.get("parentId")) and indexed.get("status") in {"idle", "failed"}
                    if stored_terminal_child and indexed.get("appStatus") == "running":
                        retained.update(status=indexed["status"], statusSource="transcript", telemetryUnavailable=False)
                    elif ((indexed.get("pendingQuestion") or indexed.get("status") == "needs input")
                          and not indexed.get("questionResolutionAt")):
                        retained.update(status="needs input", statusSource="transcript", telemetryUnavailable=False)
                    elif indexed.get("appStatus") == "running" and indexed.get("status") == "compacting":
                        retained.update(status="compacting", statusSource="transcript", telemetryUnavailable=False)
                    elif indexed.get("appStatus") in {"running", "idle", "failed", "needs input"}:
                        retained.update(status=indexed["appStatus"], statusSource="app", telemetryUnavailable=False)
                    else:
                        retained.update(status="unavailable", previousStatus=previous_status,
                                        statusSource="unavailable", telemetryUnavailable=True)
                    retained["metricsUnavailable"] = True
                else:
                    retained.update(key=key, status="unavailable", previousStatus=previous_status,
                                    telemetryUnavailable=True, statusSource="unavailable")
                retained.update(executionStatus="unavailable", executionSource="unavailable",
                                executionObservedAt=None, executionSince=None,
                                executionCompactionStartedAt=None, executionTerminalAt=None,
                                executionActivity=None)
                chats.append(retained)
            continue
        provider = key.split(":", 1)[0]
        try:
            codex_revision_before = _stat_fingerprint_memoized(path) if provider == "Codex" else None
        except OSError:
            codex_revision_before = None
        state = _claude_transcript_state(path) if provider == "Claude" else _codex_rollout_state(path_value)
        if provider == "Codex" and state.get("recordTimestampPresent"):
            transcript_epoch = _timestamp_epoch(state.get("observedAt"))
        else:
            transcript_epoch = modified
        transcript_fresh = _fresh_epoch(transcript_epoch)
        app_status = indexed.get("appStatus") or (indexed.get("status") if indexed.get("statusSource") == "app" else None)
        observed_at = indexed.get("statusObservedAt")
        observed_epoch = _timestamp_epoch(observed_at)
        app_fresh = _fresh_epoch(observed_epoch) and indexed.get("statusSource") != "unavailable"
        recorded_terminal = _timestamp_epoch(state.get("terminalAt"))
        run_stats = _codex_run_stats(path_value) if provider == "Codex" else {}
        recorded_run_start = _timestamp_epoch(run_stats.get("runStart"))
        completed_agent = (provider == "Codex" and bool(indexed.get("parentId"))
                           and state.get("status") in {"idle", "failed"} and recorded_terminal is not None)
        agent_restarted = (recorded_run_start is not None and recorded_terminal is not None
                           and recorded_run_start > recorded_terminal)
        terminal_newer_than_app = (recorded_terminal is not None and recorded_terminal <= time.time()
                                   and observed_epoch is not None and recorded_terminal > observed_epoch)
        transcript_after_app = observed_epoch is not None and transcript_epoch is not None and transcript_epoch > observed_epoch
        if provider == "Codex":
            resolved_attention = bool(state.get("questionResolutionAt") and not state.get("question"))
            if resolved_attention and app_status == "needs input":
                app_status = None
            if state.get("status") == "needs input":
                effective_status, effective_source = "needs input", "transcript"
            elif terminal_newer_than_app and state.get("status") in {"idle", "failed"}:
                effective_status, effective_source = state["status"], "transcript"
            elif (state.get("status") == "compacting" and
                  ((app_fresh and app_status == "running") or transcript_after_app
                   or (not app_fresh and transcript_fresh))):
                effective_status, effective_source = "compacting", "transcript"
            elif completed_agent and not agent_restarted and app_fresh and app_status == "running":
                effective_status, effective_source = state["status"], "transcript"
            elif app_fresh and indexed.get("statusSource") == "app" and app_status:
                effective_status, effective_source = app_status, "app"
            elif transcript_after_app and state.get("status") != "unknown":
                effective_status, effective_source = state["status"], "transcript"
            elif app_fresh and app_status:
                effective_status, effective_source = app_status, indexed.get("statusSource") or "app"
            elif state.get("status") in {"idle", "failed"} and not state.get("telemetryUnavailable"):
                effective_status, effective_source = state["status"], "transcript"
            elif transcript_fresh and state.get("status") != "unknown":
                effective_status, effective_source = state["status"], "transcript"
            else:
                effective_status, effective_source = "unavailable", "unavailable"
        else:
            effective_status, effective_source = state["status"], None
        insights = _session_insights(path_value) if provider == "Codex" else {}
        execution = (_codex_execution_projection(
            state, app_status if app_fresh else None, app_fresh, observed_at,
        ) if provider == "Codex" else {})
        row = {
            "key": key,
            "activityAt": datetime.fromtimestamp(transcript_epoch, timezone.utc).isoformat() if transcript_epoch is not None else None,
            "status": effective_status,
            "statusSource": effective_source,
            "statusObservedAt": observed_at if app_fresh else None,
            **({"lastValidAt": indexed.get("lastValidAt")} if provider == "Codex" else {}),
            "telemetryUnavailable": effective_source == "unavailable",
            "metricsUnavailable": bool(state.get("telemetryUnavailable")),
            "stateSince": indexed.get("stateSince") if effective_source == "app" else state.get("stateSince") if effective_status == state.get("status") else None,
            "compactionStartedAt": indexed.get("compactionStartedAt") if effective_source == "app" else state.get("compactionStartedAt") if effective_status == "compacting" else None,
            "pendingQuestion": state["question"],
            "pendingQuestionIds": state.get("questionIds", []),
            "questionResolutionAt": state.get("questionResolutionAt"),
            "questionCorrelationUnavailable": state.get("questionCorrelationUnavailable", False),
            "activity": state.get("activity"),
            "activityUnavailable": bool(indexed.get("activityUnavailable") or state.get("telemetryUnavailable")),
            **execution,
            **({"pendingQuestionAt": state["questionAt"]} if state.get("questionAt") else {}),
            **({"pendingOptions": state["options"]} if state.get("options") else {}),
            "stateDetail": state.get("detail") or indexed.get("stateDetail"),
        }
        if provider == "Claude":
            # Hook-sourced rows keep their execution fields; the transcript state
            # above (which infers running from trailing records) is discarded.
            row = _claude_live_row(row, indexed)
        elif provider != "Codex":
            for field in ("statusSource", "statusObservedAt", "telemetryUnavailable", "metricsUnavailable", "stateSince", "compactionStartedAt", "activity", "activityUnavailable",
                          "executionStatus", "executionSource", "executionObservedAt", "executionSince",
                          "executionCompactionStartedAt", "executionTerminalAt", "executionActivity"):
                row.pop(field, None)
        elif effective_status == "unavailable":
            previous_status = indexed.get("status") if indexed.get("status") != "unavailable" else indexed.get("previousStatus")
            if previous_status:
                row["previousStatus"] = previous_status
        if provider == "Codex":
            parent_id, parent_provider = _codex_parent_pair(insights.get("parentId"), indexed)
            row.update({
                "id": indexed.get("id") or (key.split(":", 1)[1] if ":" in key else None),
                "title": _tidy_codex_title(indexed.get("identityTitle") or insights.get("title") or indexed.get("title") or "",
                                           row.get("model") or indexed.get("model") or insights.get("model")) or None,
                "parentId": parent_id,
                "parentProvider": parent_provider,
                "agentName": insights.get("agentName") or indexed.get("agentName") or indexed.get("identityAgentName"),
                "agentRole": indexed.get("agentRole"),
                "workspace": insights.get("workspace") or indexed.get("workspace") or indexed.get("identityWorkspace"),
                "source": insights.get("source") or indexed.get("source") or indexed.get("identitySource"),
            })
            # Context telemetry uses a bounded tail read; cumulative token
            # aggregation remains in the slower full provider snapshot.
            event = CodexProvider._last_token_event(path_value) or {}
            last = event.get("last_token_usage") or {}
            model = insights.get("model") or event.get("model") or run_stats.get("settingsModel")
            if model:
                row["model"] = model
            context_valid = _current_context_sample_valid(event, effective_status, run_stats)
            window = _positive_int(event.get("model_context_window")) if context_valid else None
            context_tokens = _nonnegative_int(last.get("total_tokens")) if context_valid else None
            context_model = event.get("model") if context_valid else None
            if context_valid and model and model != "Codex" and model != context_model:
                context_valid = False
                window = None
                context_tokens = None
                context_model = None
            row.update(_codex_agent_stats(path_value, state["status"], bool(insights.get("parentId")),
                                          run_stats=run_stats))
            unavailable = bool(state.get("telemetryUnavailable"))
            def retained_value(field: str, observed: Any) -> Any:
                return indexed.get(field) if unavailable and observed is None else observed
            def retained_context(field: str, observed: Any) -> Any:
                if unavailable and effective_status not in _ACTIVE_STATES and observed is None:
                    return indexed.get(field)
                return observed
            row.update({
                "latestRequest": retained_value("latestRequest", insights.get("latestRequest")),
                "latestRequestAt": retained_value("latestRequestAt", insights.get("latestRequestAt")),
                "latestMessageAt": retained_value("latestMessageAt", insights.get("latestMessageAt")),
                "latestMessageDirection": retained_value("latestMessageDirection", insights.get("latestMessageDirection")),
                "latestMessageSourceId": retained_value("latestMessageSourceId", insights.get("latestMessageSourceId")),
                "latestMessageTargetId": retained_value("latestMessageTargetId", insights.get("latestMessageTargetId")),
                "latestUpdate": insights.get("latestUpdate"),
                "latestUpdateAt": insights.get("latestUpdateAt"),
                "trackerUpdate": insights.get("trackerUpdate"),
                "trackerUpdateAt": insights.get("trackerUpdateAt"),
                "contextTokens": retained_context("contextTokens", context_tokens),
                "contextWindow": retained_context("contextWindow", window),
                "contextPercent": retained_context("contextPercent", min(100, round(context_tokens * 100 / window)) if context_tokens is not None and window else None),
                "contextObservedAt": retained_context("contextObservedAt", event.get("timestamp") if context_valid else None),
                "contextModel": retained_context("contextModel", context_model),
                "contextDelta": retained_context("contextDelta", event.get("contextDelta") if context_valid else None),
                "contextDeltaObservedAt": retained_context("contextDeltaObservedAt", event.get("contextDeltaObservedAt") if context_valid else None),
                "modelChangeAt": run_stats.get("modelChangeAt"),
                "usageUpdatedAt": event.get("timestamp"),
                "lastRequest": retained_context("lastRequest", {"input": last.get("input_tokens") if context_valid else None,
                                "cached": last.get("cached_input_tokens") if context_valid else None,
                                "output": last.get("output_tokens") if context_valid else None}),
            })
        if provider == "Codex":
            try:
                codex_revision_after = _stat_fingerprint(path)
            except OSError:
                codex_revision_after = None
            if codex_revision_before is None or codex_revision_after != codex_revision_before:
                row.update(status="unavailable", statusSource="unavailable", telemetryUnavailable=True,
                           metricsUnavailable=True, activityUnavailable=True, activityAt=None,
                           executionStatus="unavailable", executionSource="unavailable",
                           executionObservedAt=None, executionSince=None,
                           executionCompactionStartedAt=None, executionTerminalAt=None,
                           executionActivity=None,
                           statusObservedAt=None, stateSince=None, compactionStartedAt=None,
                           pendingQuestion=None, pendingQuestionAt=None, pendingOptions=None,
                           pendingQuestionIds=[], questionResolutionAt=None,
                           stateDetail=None, activity=None,
                           toolCalls=None, filesEdited=None, fileChanges=None,
                           runStartedAt=None, activeRunStartedAt=None, runEndedAt=None,
                           failureReason=None, contextTokens=None, contextWindow=None,
                           contextPercent=None, contextObservedAt=None, contextModel=None,
                           contextDelta=None, contextDeltaObservedAt=None, modelChangeAt=None,
                           usageUpdatedAt=None, lastRequest=None,
                           latestRequest=None, latestRequestAt=None, latestMessageAt=None,
                           latestMessageDirection=None, latestMessageSourceId=None,
                           latestMessageTargetId=None, latestUpdate=None, latestUpdateAt=None,
                           trackerUpdate=None, trackerUpdateAt=None)
                row.pop("model", None)
                previous_status = indexed.get("status") if indexed.get("status") != "unavailable" else indexed.get("previousStatus")
                if previous_status:
                    row["previousStatus"] = previous_status
                row.update(title=_tidy_codex_title(indexed.get("identityTitle") or indexed.get("title") or "",
                                                   indexed.get("model")) or None,
                           parentId=indexed.get("identityParentId") or indexed.get("parentId"),
                           parentProvider=indexed.get("parentProvider") or indexed.get("identityParentProvider"),
                           agentName=indexed.get("identityAgentName"),
                           workspace=indexed.get("identityWorkspace"),
                           source=indexed.get("identitySource"))
        chats.append(row)
    if "Claude" in visible_full and "Codex" in visible_full:
        try:
            from .agent_links import apply_exact_links
            grouped = [{"name": name, "chats": [chat for chat in chats
                                                 if str(chat.get("key") or "").startswith(name + ":")]}
                       for name in ("Claude", "Codex")]
            apply_exact_links(grouped)
            linked = {str(row.get("key")): (row.get("parentId"), row.get("parentProvider"))
                      for provider in grouped for row in provider["chats"]}
            for chat in chats:
                if chat.get("key") in linked:
                    chat["parentId"], parent_provider = linked[chat["key"]]
                    if parent_provider:
                        chat["parentProvider"] = parent_provider
        except Exception:
            pass
    for chat in chats:
        chat["provider"] = "Codex" if str(chat.get("key") or "").startswith("Codex:") else "Claude"
    from .agent_links import snapshot_graph
    # Full edges, not a delta: the revision is process-wide, so a delta is
    # "changes since anyone's last request" and other pollers would eat it.
    graph = snapshot_graph(chats)
    return {"generatedAt": datetime.now(timezone.utc).isoformat(),
            "structureStamp": _structure_stamp(visible_full), "chats": chats,
            "providerPolicies": policy_payload, "graph": graph}


def today_usage() -> dict[str, Any]:
    """Return only today's local token total without an app-server/thread scan."""
    today = _local_today().isoformat()
    policies = POLICIES
    full_collect, _limit_collect = _provider_collection_sets(policies)
    visible_full = tuple(name for name in full_collect if policy_for(name, policies).allows("show"))
    providers: list[dict[str, Any]] = []
    for name in visible_full:
        if name == "Codex":
            usage = CodexProvider()._local_usage(days=1)
            providers.append({"name": name, "connected": True, "todayTokens": int(usage["daily"].get(today, 0))})
        else:
            providers.append({"name": name, "connected": False, "todayTokens": None})
    return {"generatedAt": datetime.now(timezone.utc).isoformat(), "providers": providers,
            "providerPolicies": public_policies(policies)}


def trim_chats(snapshot: dict[str, Any], limit: int = 15, pinned: Iterable[str] = (), *,
               query: str = "", provider_filter: str = "all", status_filter: str = "all",
               sort: str = "latest", days: int = 7) -> dict[str, Any]:
    """Return the latest `limit` root chats across providers plus pinned roots, with their agents."""
    pinned = set(pinned)
    latest: dict[str, float] = {}
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    roots_by_provider: dict[str, dict[Any, Any]] = {}
    for provider in snapshot.get("providers") or []:
        name = str(provider.get("name"))
        chats = provider.get("chats") or []
        parents = {chat.get("id"): chat.get("parentId") for chat in chats}
        roots: dict[Any, Any] = {}
        for chat in chats:
            # Walk to the top-most known ancestor; unknown or cyclic lineage stays a root.
            node, seen = chat.get("id"), set()
            while parents.get(node) in parents and node not in seen:
                seen.add(node)
                node = parents[node]
            if node in seen:  # Match the frontend's cycle handling.
                node = chat.get("id")
            roots[chat.get("id")] = node
            key = f"{name}:{node}"
            try:
                at = datetime.fromisoformat(str(chat.get("updatedAt") or "").replace("Z", "+00:00")).timestamp()
            except (ValueError, OSError, OverflowError):
                at = 0
            latest[key] = max(latest.get(key, 0), at)
            groups[key].append(chat)
        roots_by_provider[name] = roots
    # A root's recency includes its agents, matching the widget's ordering.
    query = query.strip().casefold()
    def status(chat):
        value = str(chat.get("status") or "unknown").lower().replace(" ", "-").replace("_", "-")
        return {"needs-attention": "needs-input", "needs-input": "needs-input", "active": "running",
                "error": "failed", "inactive": "idle"}.get(value, value)
    def matches(chat):
        text = " ".join(str(chat.get(k) or "") for k in ("title", "model", "agentName", "reasoningEffort", "workspace", "latestRequest")).casefold()
        state = status(chat)
        return (not query or query in text) and (
            status_filter == "all" or status_filter == "running" and state == "running"
            or status_filter == "attention" and (state in {"needs-input", "failed"} or chat.get("pendingQuestion"))
            or status_filter == "context" and (chat.get("contextPercent") or 0) >= 85)
    dates = _last_calendar_dates(days if days in (1, 7, 30) else 7)
    def ranking(key):
        rows = groups[key]
        value = 0
        if sort == "attention":
            value = min(0 if c.get("pendingQuestion") else {"needs-input": 0, "failed": 1, "running": 2, "quiet": 2, "idle": 3}.get(status(c), 4) for c in rows)
        elif sort == "context":
            value = -max(c.get("contextPercent") or 0 for c in rows)
        elif sort == "tokens":
            value = -sum(v for c in rows for d, v in (c.get("dailyTokens") or {}).items() if d in dates)
        return (0 if sort == "latest" else key not in pinned, value, -latest[key], key)
    ordered = sorted((key for key in latest if (provider_filter == "all" or key.startswith(provider_filter + ":"))
                      and any(matches(c) for c in groups[key])), key=ranking)
    matching_pins = pinned & set(ordered)
    keep = matching_pins | set([key for key in ordered if key not in matching_pins][:max(0, limit - len(matching_pins))])
    providers_out = []
    for provider in snapshot.get("providers") or []:
        name = str(provider.get("name"))
        roots = roots_by_provider.get(name, {})
        chats = provider.get("chats") or []
        kept = [chat for chat in chats if f"{name}:{roots.get(chat.get('id'))}" in keep]
        providers_out.append({**provider, "chats": kept, "chatTotal": len(set(roots.values()))})
    return {**snapshot, "providers": providers_out, "chatLimit": limit, "chatMatches": len(ordered)}


_LAST_GOOD: dict[str, dict[str, Any]] = {}
_LAST_GOOD_LOCK = threading.Lock()


def _dedupe_provider_chats(provider: dict[str, Any]) -> None:
    """Keep the first row for each provider/thread identity in this projection."""
    chats = provider.get("chats")
    if not isinstance(chats, list):
        return
    unique = []
    seen = set()
    for chat in chats:
        identity = chat.get("id") if isinstance(chat, dict) else None
        if ((isinstance(identity, str) and bool(identity.strip())) or type(identity) is int):
            key = (provider.get("name"), str(identity))
            if key in seen:
                continue
            seen.add(key)
        unique.append(chat)
    provider["chats"] = unique


# --- Claude Code hook rows (token_tracker/claude_provider.py) ---------------
def _claude_code_provider() -> Any:
    from .claude_provider import ClaudeCodeProvider
    return ClaudeCodeProvider()


def _claude_live_row(row: dict[str, Any], indexed: dict[str, Any]) -> dict[str, Any]:
    from .claude_provider import live_row
    return live_row(row, indexed)


def _codex_parent_pair(native_parent_id: Any, indexed: dict[str, Any]) -> tuple[Any, str | None]:
    """Choose native Codex lineage before one inherited cross-provider pair."""
    native = native_parent_id or indexed.get("identityParentId")
    if native:
        return native, "Codex"
    cached_parent = indexed.get("parentId")
    if cached_parent:
        return cached_parent, indexed.get("parentProvider")
    return None, None
# --- end Claude Code hook rows -----------------------------------------------


def _provider_snapshot(factory: Any, name: str, accent: str,
                      policy_generation: str | None = None) -> dict[str, Any]:
    """A failed build keeps the last successful provider data, clearly marked stale."""
    if policy_generation is None:
        policy_generation = public_policies(POLICIES)["generation"]
    try:
        result = factory().snapshot()
    except Exception as exc:
        result = _error_provider(name, accent, exc)
    result.setdefault("name", name)
    if result.get("connected"):
        _dedupe_provider_chats(result)
        private = deepcopy(result)
        private["builtAt"] = datetime.now(timezone.utc).isoformat()
        private["_policyGeneration"] = policy_generation
        with _LAST_GOOD_LOCK:
            _LAST_GOOD[name] = private
        return result
    with _LAST_GOOD_LOCK:
        previous = _LAST_GOOD.get(name)
    if not previous or previous.get("_policyGeneration") != policy_generation:
        return result
    stale = deepcopy(previous)
    stale.pop("_policyGeneration", None)
    stale.update(stale=True, staleSince=previous.get("builtAt"), error=result.get("error"))
    _dedupe_provider_chats(stale)
    return stale


def build_snapshot() -> dict[str, Any]:
    # Policy is immutable for the process; capture the single mapping used by
    # this build before any provider collection or stale-data selection.
    policies = POLICIES
    full_collect, limit_only = _provider_collection_sets(policies)
    visible_full = tuple(name for name in full_collect if policy_for(name, policies).allows("show"))
    visible_limits = tuple(name for name in limit_only if policy_for(name, policies).allows("show"))
    policy_payload = public_policies(policies)
    # Providers read independent sources; the Codex app server round trip
    # should not delay the Claude transcript scan (or the reverse).
    # Taken before scanning so a session created during the build still
    # changes the stamp that live polling compares against.
    try:
        structure_stamp = _structure_stamp(visible_full)
    except Exception:
        structure_stamp = None
    factories = {"Claude": (_claude_code_provider, "#e8813f"), "Codex": (CodexProvider, "#4a95e0")}
    limit_factories = {"Claude": (ClaudeLimitsProvider, "#e8813f")}
    jobs = [(name, *factories[name]) for name in full_collect] + [(name, *limit_factories[name]) for name in limit_only]
    with ThreadPoolExecutor(max_workers=max(1, len(jobs)), thread_name_prefix="token-provider") as pool:
        futures = [pool.submit(_provider_snapshot, factory, name, accent, policy_payload["generation"])
                   for name, factory, accent in jobs]
        all_providers = [future.result() for future in futures]
    active_providers = all_providers[:len(full_collect)]
    visible_active = [provider for provider in active_providers
                      if policy_for(provider.get("name"), policies).allows("show")]
    visible_provider_data = [provider for provider in all_providers
                             if policy_for(provider.get("name"), policies).allows("show")]
    if "Claude" in full_collect and "Codex" in full_collect:
        try:
            from .agent_links import apply_exact_links
            apply_exact_links(visible_provider_data)
        except Exception:
            pass
    graph_rows = []
    for provider in visible_provider_data:
        for row in provider.get("chats") or []:
            row["provider"] = provider.get("name")
            graph_rows.append(row)
    from .agent_links import snapshot_graph
    graph = snapshot_graph(graph_rows)
    _index_live_paths(visible_active, policy_payload["generation"])

    today_date = _local_today()
    activity_dates = _last_calendar_dates(31, today_date)
    activity: dict[str, int] = defaultdict(int)
    for provider in visible_active:
        for row in provider.get("activity", []):
            activity_date = row.get("date")
            if activity_date not in activity_dates:
                continue
            # Dates are accepted only from the fixed local-calendar window.
            activity[activity_date] += int(row.get("value") or 0)
    today = today_date.isoformat()
    traffic_daily = activity
    today_tokens = traffic_daily.get(today, 0)
    seven_dates = _last_calendar_dates(7, today_date)
    seven_tokens = sum(value for activity_date, value in traffic_daily.items() if activity_date in seven_dates)

    active_dates = set()
    for activity_date, value in activity.items():
        if value <= 0:
            continue
        try:
            active_dates.add(date.fromisoformat(activity_date))
        except (TypeError, ValueError):
            continue
    current_streak = 0
    cursor = today_date
    while cursor in active_dates:
        current_streak += 1
        cursor -= timedelta(days=1)
    longest_streak = running_streak = 0
    previous: date | None = None
    for activity_date in sorted(active_dates):
        running_streak = running_streak + 1 if previous and activity_date == previous + timedelta(days=1) else 1
        longest_streak = max(longest_streak, running_streak)
        previous = activity_date

    return {
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "structureStamp": structure_stamp,
        "providerPolicies": policy_payload,
        "enabledProviders": [provider["name"] for provider in visible_active],
        "limitProviders": list(visible_limits),
        "hero": {
            "today": today_tokens,
            "todayLabel": _human_tokens(today_tokens),
            "sevenDays": seven_tokens,
            "sevenDaysLabel": _human_tokens(seven_tokens),
            "note": " + ".join(provider["name"] for provider in visible_active) + " transcript traffic · API-equivalent estimates",
        },
        "providers": visible_provider_data,
        "graph": graph,
        "activity": [{"date": date, "value": value} for date, value in sorted(activity.items())],
        "activityMeta": {
            "activeDays": len(active_dates),
            "currentStreakDays": current_streak,
            "longestStreakDays": longest_streak,
            "trackingWindowDays": 31,
        },
    }
