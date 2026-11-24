"""Incremental ingestion of Claude and Codex transcripts into the work ledger.

Each source file has a cursor: generation, byte offset of the last complete record,
and a fingerprint of its first bytes. A pass reads only complete lines (a partial
last line waits for its newline). Its facts, thread counters, gaps and cursor commit
in one transaction, so a crash between the facts and the cursor counts nothing twice
and loses nothing.

A truncated or replaced file starts a new generation and records a gap. Nothing is
read unless the caller's collection policy allows that provider.
"""

from __future__ import annotations

import hashlib
import json
import stat as stat_module
import time
import bisect
from contextlib import contextmanager
from copy import deepcopy
from pathlib import Path
from typing import Any, Callable, Mapping

from . import claude_provider, providers
from . import ledger_classify as classify_mod
from . import ledger_model as model_mod
from . import ledger_store as store

HEAD_BYTES = 512
MAX_RECORD_BYTES = 8 * 1024 * 1024
_READ_CHUNK = 64 * 1024
_HASH_CHUNK = 64 * 1024
# Keep the recurring cursor and missing-source scans in this writer's page cache.
# SQLite's default cache is smaller than the sources table on typical installations.
_WRITER_CACHE_KIB = 16 * 1024
# A prefix whose verification restarted this many times (the file kept growing) is verified in one pass.
_VERIFY_RESTART_LIMIT = 8
IDLE_VERIFY_SECONDS = 120
DEADLINE_VERIFY_SECONDS = 1800
DEBT_BACKLOG_BYTES = 64 * 1024 * 1024
VERIFY_RETRY_START_SECONDS = 60
VERIFY_RETRY_MAX_SECONDS = 600
_GAP_COUNTERS = {"truncation": "truncations", "replacement": "replacements", "oversize": "oversizeRecords", "ambiguous-reset": "ambiguousResets",
                 "source-missing-unverified": "missingUnverified"}
FaultHook = Callable[[str], None]
# Thread hint for a first-seen rollout whose leading metadata record is still being written.
_PENDING_THREAD = object()


class _VerificationReadError(OSError):
    def __init__(self, message: str, work: int, state: Mapping[str, Any] | None = None) -> None:
        super().__init__(message)
        self.work = work
        self.state = deepcopy(dict(state)) if state is not None else None


class _PassChanged(Exception):
    """The pass index no longer describes the database or a source identity."""


class _PassStopped(Exception):
    """A cooperative stop was requested during snapshot acquisition."""


def _digest(data: bytes) -> str:
    return hashlib.blake2b(data, digest_size=16).hexdigest()


def path_key(path: Path) -> str:
    return _digest(str(path.resolve()).encode("utf-8", "replace"))


def _omission_key(path: Path, generation: int, position: int) -> str:
    return f"{path_key(path)}:{generation}:{position}"


def _hour(epoch: float) -> int:
    return int(epoch // 3600) * 3600


def _fresh_state() -> dict[str, Any]:
    return {"thread": None, "started": None, "model": None, "slug": None, "agent_type": None,
            "sub": False, "interactive": False, "exec_review": False, "read_only": False,
            "evidence_done": False, "segment": None, "tag_count": 0,
            "thread_loaded": False, "review_title": False}


def _tag_turn(state: dict[str, Any], thread: str, kind: str) -> None:
    """A `kind:` user message starts a new segment. Only the kind and a turn id are kept."""
    state["tag_count"] = int(state.get("tag_count") or 0) + 1
    state["segment"] = {"kind": kind, "turn": f"{thread}#{state['tag_count']}"}


def discover(claude_root: Path, codex_root: Path, collect: Mapping[str, bool]) -> list[tuple[str, Path]]:
    """Every retained transcript, earliest-created first so resumed chains replay in order."""
    found: list[tuple[float, str, Path]] = []
    for provider, base in ((model_mod.CLAUDE, Path(claude_root) / "projects"),
                           (model_mod.CODEX, Path(codex_root) / "sessions")):
        if not collect.get(provider):
            continue
        if not base.is_dir():
            continue
        for path in base.rglob("*.jsonl"):
            try:
                info = path.stat()
            except OSError:
                continue
            if stat_module.S_ISREG(info.st_mode):
                found.append((providers._created_at(info), provider, path))
    found.sort(key=lambda item: (item[0], str(item[2])))
    return [(provider, path) for _created, provider, path in found]


def _parse(complete: bytes) -> tuple[list[tuple[int, dict[str, Any]]], list[int]]:
    items: list[tuple[int, dict[str, Any]]] = []
    malformed: list[int] = []
    offset = 0
    for line in complete.split(b"\n"):
        position = offset
        offset += len(line) + 1
        if not line.strip():
            continue
        try:
            item = json.loads(line.decode("utf-8", "replace"))
        except ValueError:
            malformed.append(position)
            continue
        if isinstance(item, dict):
            items.append((position, item))
        else:
            malformed.append(position)
    return items, malformed


def _head_matches(path: Path, head_key: str) -> bool:
    length_text, _, digest = head_key.partition(":")
    length = int(length_text)
    with path.open("rb") as handle:
        head = handle.read(length)
    return len(head) == length and _digest(head) == digest


def _file_stamp(info: Any) -> list[int]:
    return [info.st_size, info.st_mtime_ns, info.st_ctime_ns, info.st_dev, info.st_ino]


def _file_identity_changed(previous: Any, current: list[int]) -> bool:
    return isinstance(previous, list) and len(previous) >= 5 and previous[3:5] != current[3:5]


def _spans(state: Mapping[str, Any], offset: int) -> list[tuple[int, str]]:
    """(length, digest) of each hashed span of the consumed prefix, in file order."""
    hashes = list(state.get("chunk_hashes") or [])
    lengths = state.get("chunk_lens")
    if not hashes:
        return []
    if not isinstance(lengths, list) or len(lengths) != len(hashes):
        lengths = [_HASH_CHUNK] * (len(hashes) - 1) + [offset - (len(hashes) - 1) * _HASH_CHUNK]
    return list(zip((int(length) for length in lengths), hashes))


def _store_spans(state: dict[str, Any], spans: list[tuple[int, str]]) -> None:
    state["chunk_hashes"] = [digest for _length, digest in spans]
    state["chunk_lens"] = [length for length, _digest_text in spans]


def _verify_remaining(state: Mapping[str, Any], offset: int) -> int:
    """Bytes of the consumed prefix not yet re-verified against the file on disk."""
    verify = state.get("verify")
    if not verify:
        return offset
    spans = _spans(state, offset)
    count = min(int(verify.get("count") or len(spans)), len(spans))
    done = sum(length for length, _ in spans[:int(verify.get("next", 0))])
    if verify.get("boundary") and count:
        done += spans[count - 1][0]
    return max(1, offset - done)


def _claude_user_text(item: Mapping[str, Any]) -> str | None:
    message = item.get("message") if isinstance(item.get("message"), dict) else {}
    content = message.get("content")
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        text = " ".join(str(block.get("text") or "") for block in content
                        if isinstance(block, dict) and block.get("type") == "text")
    else:
        return None
    # Injected context (<...>) is not the user's prompt, matching the existing reducers.
    return None if text.lstrip().startswith("<") else text


def _codex_user_text(item: Mapping[str, Any], payload: Mapping[str, Any]) -> str | None:
    if item.get("type") == "response_item" and payload.get("type") == "message" and payload.get("role") == "user":
        blocks = payload.get("content") if isinstance(payload.get("content"), list) else []
        text = " ".join(str(block.get("text") or "") for block in blocks if isinstance(block, dict))
    elif item.get("type") == "event_msg" and payload.get("type") == "user_message":
        text = str(payload.get("message") or "")
    else:
        return None
    return None if text.lstrip().startswith("<") else text


def _read_only(payload: Mapping[str, Any]) -> bool:
    policy = payload.get("sandbox_policy")
    if isinstance(policy, dict):
        return policy.get("type") == "read-only"
    return payload.get("sandbox") == "read-only"


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _replay_state(replay_end: int) -> dict[str, Any]:
    """Fresh state for a source read again from its start: its counters are ambiguous up to replay_end."""
    state = _fresh_state()
    state["replay_ambiguous"] = True
    state["replay_end"] = replay_end
    state["audit"] = True
    return state


def _merge_promoted_thread(conn: Any, provider: str, thread: str, staged: Mapping[str, Any]) -> dict[str, Any]:
    """Promotion merges into the stored row: a staged baseline (counters, epoch, source) never rewinds newer ones."""
    stored = store.load_thread(conn, provider, thread)
    if stored is None:
        return dict(staged)
    merged = {**staged, **stored}
    for name in ("segment", "tag_count"):
        if name in staged:
            merged[name] = staged[name]
    return merged


def _record_valid(conn: Any, provider: str, message_key: str, epoch: float) -> None:
    """A valid copy of a response: it resolves earlier incomplete copies and clears their omissions."""
    conn.execute("INSERT INTO ledger_message_evidence(provider, message_key, valid_epoch) VALUES(?, ?, ?) "
                 "ON CONFLICT(provider, message_key) DO UPDATE SET valid_epoch=MAX(valid_epoch, excluded.valid_epoch)",
                 (provider, message_key, epoch))
    conn.execute("DELETE FROM ledger_omissions WHERE provider=? AND message_key=? AND event_epoch IS NOT NULL AND event_epoch<=?",
                 (provider, message_key, epoch))


class Ingestor:
    """Runs ingestion passes against one writer connection. Not thread-safe by design."""

    def __init__(self, conn: Any, *, claude_root: Path, codex_root: Path,
                 clock: Callable[[], float] = time.time, kind_registry_path: Path | None = None) -> None:
        self.conn = conn
        self.conn.execute(f"PRAGMA cache_size=-{_WRITER_CACHE_KIB}")
        self.claude_root = Path(claude_root)
        self.codex_root = Path(codex_root)
        self.clock = clock
        self.kind_registry_path = kind_registry_path
        self.registry = dict(classify_mod.DEFAULT_KIND_REGISTRY)
        self.stats = {"sourceReads": 0}
        self._pass_paths: dict[str, str] | None = None
        self._pass_cursors: dict[tuple[str, str], dict[str, Any] | None] | None = None
        self._pass_identities: set[tuple[str, str]] = set()
        self._pass_providers: list[str] = []
        self._pass_version: int | None = None
        self._last_fast_idle = False
        self._last_identity: tuple[str, str] | None = None
        self._last_cursor: dict[str, Any] | None = None
        self._active_source: tuple[str, Path] | None = None
        self._pass_visited = 0
        self._pass_read_total = 0
        self._pass_invalidated = False
        self._conflict_streak = 0
        self._verification_turn: tuple[str, str] | None = None
        self._last_outcome: dict[str, bool] = {}
        self._thread_hint_cache: dict[tuple[str, str], tuple[tuple[int, ...], str | None]] = {}
        # Only digest state is retained; a restart safely rechecks an unfinished chunk.
        self._partial_verification: dict[tuple[str, str], dict[str, Any]] = {}
        self.conn.execute("CREATE TABLE IF NOT EXISTS ledger_omissions("
                          "provider TEXT NOT NULL, identity TEXT NOT NULL, "
                          "PRIMARY KEY(provider, identity))")
        columns = {row[1] for row in self.conn.execute("PRAGMA table_info(ledger_omissions)")}
        for column in ("source_key", "message_key", "event_epoch"):
            if column not in columns:
                self.conn.execute(f"ALTER TABLE ledger_omissions ADD COLUMN {column} "
                                  + ("REAL" if column == "event_epoch" else "TEXT"))
        self.conn.execute("CREATE TABLE IF NOT EXISTS ledger_message_evidence("
                          "provider TEXT NOT NULL, message_key TEXT NOT NULL, valid_epoch REAL NOT NULL, "
                          "PRIMARY KEY(provider, message_key))")
        self.conn.execute("CREATE TABLE IF NOT EXISTS ledger_unknown_kinds("
                          "provider TEXT NOT NULL, kind TEXT NOT NULL, event_key TEXT NOT NULL, "
                          "PRIMARY KEY(provider, event_key))")
        self.conn.execute("CREATE TABLE IF NOT EXISTS ledger_gap_details("
                          "gap_id INTEGER PRIMARY KEY, thread_id TEXT, interval_start INTEGER, interval_end INTEGER)")

    def _path_key(self, path: Path) -> str:
        if self._pass_paths is None:
            return path_key(path)
        raw = str(path)
        if raw not in self._pass_paths:
            self._pass_paths[raw] = path_key(path)
        return self._pass_paths[raw]

    def _current_identity(self, provider: str, path: Path) -> tuple[str, str]:
        """Resolve again at cursor and reconciliation boundaries."""
        key = path_key(path)
        if self._pass_paths is not None:
            if self._pass_paths.get(str(path)) != key:
                raise _PassChanged
        identity = (provider, key)
        return identity

    def _refresh_pass_cursors(self, *, force: bool = False,
                              should_stop: Callable[[], bool] | None = None) -> None:
        if self._pass_cursors is None:
            return
        if self._pass_invalidated:
            raise _PassChanged
        version = self.conn.execute("PRAGMA data_version").fetchone()[0]
        if self._pass_version is not None and version != self._pass_version:
            raise _PassChanged
        if not force:
            if version != self._pass_version:
                raise _PassChanged
            return
        loaded = store.load_sources(self.conn, self._pass_providers, self._pass_identities)
        after = self.conn.execute("PRAGMA data_version").fetchone()[0]
        if after != version:
            if should_stop and should_stop():
                raise _PassStopped
            raise _PassChanged
        self._pass_cursors.clear()
        self._pass_cursors.update(loaded)
        self._pass_version = after

    @contextmanager
    def _transaction(self):
        with store.transaction(self.conn) as conn:
            if self._pass_version is not None:
                if (self._pass_invalidated
                        or conn.execute("PRAGMA data_version").fetchone()[0] != self._pass_version):
                    raise _PassChanged
                if self._active_source is not None:
                    self._current_identity(*self._active_source)
            yield conn

    def forget_provider(self, provider: str) -> None:
        """Drop in-memory partial verification for one provider (its stored rows were purged)."""
        if self._pass_cursors is not None:
            self._pass_invalidated = True
            for key in [key for key in self._pass_cursors if key[0] == provider]:
                del self._pass_cursors[key]
        for key in [key for key in self._partial_verification if key[0] == provider]:
            del self._partial_verification[key]
        for key in [key for key in self._thread_hint_cache if key[0] == provider]:
            del self._thread_hint_cache[key]
    def _codex_thread_hint(self, path: Path) -> Any:
        """Read only the bounded leading metadata needed to order a first-seen rollout.

        Returns the thread id, None when the leading records name no thread, or
        _PENDING_THREAD when a leading record is incomplete or unreadable, so the
        caller orders the rollout conservatively until its metadata can be read."""
        key = (model_mod.CODEX, self._path_key(path))
        try:
            stamp = tuple(_file_stamp(path.stat()))
        except OSError:
            return None
        cached = self._thread_hint_cache.get(key)
        if cached and cached[0] == stamp:
            return cached[1]
        thread: Any = None
        try:
            with path.open("rb") as handle:
                scanned = 0
                # The first record may be a large session_meta (long base instructions); later records
                # are scanned only within the leading chunk, matching where session_meta appears.
                while scanned < _READ_CHUNK:
                    line = handle.readline(MAX_RECORD_BYTES + 1)
                    if not line:
                        break
                    scanned += len(line)
                    if not line.endswith(b"\n"):
                        # Still being written (or oversize, which ingestion skips): do not guess.
                        thread = _PENDING_THREAD if len(line) <= MAX_RECORD_BYTES else None
                        break
                    try:
                        item = json.loads(line.decode("utf-8", "replace"))
                    except ValueError:
                        continue
                    payload = item.get("payload") if isinstance(item, dict) else None
                    if isinstance(item, dict) and item.get("type") == "session_meta":
                        if isinstance(payload, dict) and isinstance(payload.get("id"), str) and payload["id"]:
                            thread = payload["id"][:128]
                        break  # only the first session_meta names the thread, as in _codex_pass
        except OSError:
            return _PENDING_THREAD  # not cached: the next pass retries the read
        self._thread_hint_cache[key] = (stamp, thread)
        return thread

    def run(self, collect: Mapping[str, bool], budget: int | None = None,
            fault: FaultHook | None = None, should_stop: Callable[[], bool] | None = None) -> dict[str, Any]:
        compat = self._conflict_streak >= 3
        self._pass_paths = None if compat else {}
        self._pass_visited = 0
        self._pass_read_total = 0
        self._pass_invalidated = False
        try:
            if not compat:
                self._pass_version = self.conn.execute("PRAGMA data_version").fetchone()[0]
            result = self._run(collect, budget, fault, should_stop, compat=compat)
            if not result.get("stopped"):
                self._conflict_streak = 0
            return result
        except _PassStopped:
            return {"filesVisited": self._pass_visited, "bytesRead": self._pass_read_total,
                    "pendingBytes": None, "stopped": True}
        except _PassChanged:
            self._conflict_streak += 1
            self._partial_verification.clear()
            self._thread_hint_cache.clear()
            return {"filesVisited": self._pass_visited, "bytesRead": self._pass_read_total,
                    "pendingBytes": None, "stopped": True, "retryAfterChange": True}
        finally:
            self._pass_paths = None
            self._pass_cursors = None
            self._pass_identities = set()
            self._pass_providers = []
            self._pass_version = None
            self._active_source = None

    def _run(self, collect: Mapping[str, bool], budget: int | None,
             fault: FaultHook | None, should_stop: Callable[[], bool] | None,
             *, compat: bool = False) -> dict[str, Any]:
        self.registry = classify_mod.load_kind_registry(self.kind_registry_path) if self.kind_registry_path else dict(classify_mod.DEFAULT_KIND_REGISTRY)
        discovered = discover(self.claude_root, self.codex_root, collect)
        read_total = 0
        visited = 0
        now = self.clock()
        self._pass_identities = (set() if compat else
                                 {(provider, self._path_key(path)) for provider, path in discovered})
        self._pass_providers = [name for name in (model_mod.CLAUDE, model_mod.CODEX) if collect.get(name)]
        source_cursors: dict[tuple[str, str], dict[str, Any] | None] = {}
        if not compat:
            self._pass_cursors = source_cursors
            self._refresh_pass_cursors(force=True, should_stop=should_stop)
        source_threads: dict[tuple[str, str], str | None] = {}
        source_positions: dict[tuple[str, str], int] = {}
        unresolved_positions: dict[str, list[int]] = {}
        pending_hints: set[tuple[str, str]] = set()

        def rebuild_ordering() -> None:
            source_threads.clear()
            source_positions.clear()
            unresolved_positions.clear()
            pending_hints.clear()
            for position, (provider, path) in enumerate(discovered):
                identity = (provider, self._path_key(path))
                source_positions[identity] = position
                if compat:
                    cursor = store.load_source(self.conn, *identity) if collect.get(provider) else None
                    source_cursors[identity] = cursor
                else:
                    cursor = source_cursors.get(identity)
                state = (cursor or {}).get("state", {})
                thread = state.get("thread")
                if provider == model_mod.CODEX and not thread:
                    thread = self._codex_thread_hint(path)
                    if thread is _PENDING_THREAD:
                        pending_hints.add(identity)
                        thread = None
                source_threads[identity] = thread
                unresolved = bool(state.get("verification_debt") or state.get("verify")
                                  or state.get("provisional") or state.get("ordered_tail_pending"))
                if provider == model_mod.CODEX and thread and unresolved:
                    unresolved_positions.setdefault(thread, []).append(position)
        rebuild_ordering()

        def debt_due(entry: tuple[str, Path]) -> bool:
            provider, path = entry
            cursor = source_cursors.get((provider, self._path_key(path)))
            debt = (cursor or {}).get("state", {}).get("verification_debt")
            if not debt:
                return False
            try:
                info = path.stat()
            except OSError:
                return False
            stamp = _file_stamp(info)
            if debt.get("verified_stamp") == stamp:
                return True
            retry_due = bool(debt.get("retry_pending")
                             and now >= float(debt.get("next_attempt_epoch", 0)))
            if now < float(debt.get("next_attempt_epoch", 0)):
                return False
            if retry_due:
                return True
            return (now - float(debt.get("last_change_epoch", now)) >= IDLE_VERIFY_SECONDS
                    or now - float(debt.get("first_deferred_epoch", now)) >= DEADLINE_VERIFY_SECONDS
                    or max(0, info.st_size - int(cursor["byte_offset"])) > DEBT_BACKLOG_BYTES)
        debt_entries = []
        for entry in discovered:
            if not collect.get(entry[0]):
                continue
            cursor = source_cursors.get((entry[0], self._path_key(entry[1])))
            if (cursor or {}).get("state", {}).get("verification_debt"):
                debt_entries.append(entry)
        debt_ids = [(provider, self._path_key(path)) for provider, path in debt_entries]
        if self._verification_turn not in debt_ids:
            self._verification_turn = debt_ids[0] if debt_ids else None
        if debt_ids:
            start = debt_ids.index(self._verification_turn)
            rotated_debt_ids = debt_ids[start:] + debt_ids[:start]
            due_by_id = {(provider, self._path_key(path)): (provider, path)
                         for provider, path in discovered if collect.get(provider) and debt_due((provider, path))}
            due_entries = [due_by_id[identity] for identity in rotated_debt_ids if identity in due_by_id]
        else:
            rotated_debt_ids = []
            due_entries = []
        due_keys = {(name, path) for name, path in due_entries}
        due_identities = {(provider, self._path_key(path)) for provider, path in due_entries}
        ordered = due_entries + [entry for entry in discovered if entry not in due_keys]
        self._capped_scan_used = False
        cadence = {"cursorAdvanced": False, "actionableBytes": 0,
                   "verificationAdvanced": False, "verificationRemaining": 0}
        blocked_order: set[tuple[str, Path]] = set()
        for provider, path in ordered:
            if not collect.get(provider):
                continue
            if should_stop and should_stop():
                # Cooperative stop between files: nothing from this pass is left half-applied.
                return {"filesVisited": visited, "bytesRead": read_total, "pendingBytes": None, "stopped": True}
            self._refresh_pass_cursors()
            old_identity = (provider, self._path_key(path))
            identity = old_identity if compat else self._current_identity(provider, path)
            source_position = source_positions[old_identity]
            thread = source_threads.get(old_identity)
            prior = unresolved_positions.get(thread, []) if provider == model_mod.CODEX and thread else []
            blocked = bool(prior and bisect.bisect_left(prior, source_position) > 0)
            if identity in pending_hints and source_cursors.get(identity) is None:
                # Unknown thread: wait behind any earlier unresolved Codex source.
                blocked = any(positions and positions[0] < source_position
                              for positions in unresolved_positions.values())
            if blocked:
                blocked_order.add((provider, path))
            if budget is not None and read_total >= budget:
                break
            remaining = None if budget is None else budget - read_total
            before = source_cursors.get(identity)
            self._last_fast_idle = False
            self._last_identity = identity
            self._last_cursor = before
            self._active_source = (provider, path)
            result = self.ingest_file(provider, path, remaining, fault, should_stop, block_events=blocked)
            self._active_source = None
            read_total += result
            visited += 1
            self._pass_read_total = read_total
            self._pass_visited = visited
            if not compat:
                identity = self._last_identity or identity
            if not compat:
                before = self._last_cursor
            after = source_cursors.get(identity) if self._last_fast_idle else store.load_source(self.conn, provider, identity[1])
            source_cursors[identity] = after
            after_thread = (after or {}).get("state", {}).get("thread")
            if after_thread:
                source_threads[identity] = after_thread
                thread = after_thread
            old_unresolved = bool((before or {}).get("state", {}).get("verification_debt")
                                 or (before or {}).get("state", {}).get("verify")
                                 or (before or {}).get("state", {}).get("provisional")
                                 or (before or {}).get("state", {}).get("ordered_tail_pending"))
            new_unresolved = bool((after or {}).get("state", {}).get("verification_debt")
                                 or (after or {}).get("state", {}).get("verify")
                                 or (after or {}).get("state", {}).get("provisional")
                                 or (after or {}).get("state", {}).get("ordered_tail_pending"))
            if provider == model_mod.CODEX and thread and old_unresolved != new_unresolved:
                positions = unresolved_positions.setdefault(thread, [])
                if new_unresolved and source_position not in positions:
                    bisect.insort(positions, source_position)
                elif not new_unresolved and source_position in positions:
                    positions.remove(source_position)
            if identity in due_identities and not blocked and debt_ids:
                served_index = debt_ids.index(identity)
                self._verification_turn = debt_ids[(served_index + 1) % len(debt_ids)]
            outcome = self._last_outcome
            cadence["cursorAdvanced"] = cadence["cursorAdvanced"] or outcome.get("cursorAdvanced", False)
            cadence["verificationAdvanced"] = cadence["verificationAdvanced"] or outcome.get("verificationAdvanced", False)
        now = self.clock()
        actionable = 0
        verification_left = 0
        for provider, path in discovered:
            if not collect.get(provider):
                continue
            identity = self._current_identity(provider, path)
            cursor = (store.load_source(self.conn, provider, identity[1]) if compat
                      else source_cursors.get(identity))
            try:
                info = path.stat()
            except OSError:
                continue
            offset = int(cursor["byte_offset"]) if cursor else 0
            debt = cursor["state"].get("verification_debt") if cursor else None
            stamp = _file_stamp(info)
            retry_due = not debt or now >= float(debt.get("next_attempt_epoch", 0))
            scan_due = bool(debt and retry_due and
                            (bool(debt.get("retry_pending"))
                             or now - float(debt.get("last_change_epoch", now)) >= IDLE_VERIFY_SECONDS
                             or now - float(debt.get("first_deferred_epoch", now)) >= DEADLINE_VERIFY_SECONDS
                             or max(0, info.st_size - offset) > DEBT_BACKLOG_BYTES))
            tail_ready = bool(debt and debt.get("verified_stamp") == stamp)
            blocked = (provider, path) in blocked_order
            if not blocked and (not debt or scan_due or tail_ready):
                actionable += max(0, info.st_size - offset)
            if scan_due and not blocked:
                verification_left += self._remaining(provider, path, cursor)
        cadence["actionableBytes"] = actionable
        cadence["verificationRemaining"] = verification_left
        pending_by_provider = {provider: self._pending(discovered, {provider: bool(collect.get(provider))}, None if compat else source_cursors)
                               for provider in (model_mod.CLAUDE, model_mod.CODEX)}
        verifying = {provider: self._verifying(discovered, provider, None if compat else source_cursors) if collect.get(provider) else 0
                     for provider in (model_mod.CLAUDE, model_mod.CODEX)}
        pending = sum(pending_by_provider.values())
        now = int(self.clock())
        found = {provider: {self._current_identity(name, path)[1] for name, path in discovered if name == provider}
                 for provider in (model_mod.CLAUDE, model_mod.CODEX)}
        with self._transaction() as conn:
            for provider in (model_mod.CLAUDE, model_mod.CODEX):
                if collect.get(provider) and self._resolve_missing(conn, provider, found[provider], now):
                    store.meta_set(conn, "revision", int(store.meta_value(conn, "revision") or 0) + 1)
            store.meta_set(conn, "pendingBytes", pending)
            store.meta_set(conn, "verifyingBytes", sum(verifying.values()))
            for provider, amount in verifying.items():
                store.meta_set(conn, f"verifyingBytes:{provider}", amount)
            for provider in (model_mod.CLAUDE, model_mod.CODEX):
                store.meta_set(conn, f"pendingBytes:{provider}", pending_by_provider[provider])
            store.meta_set(conn, "collected", json.dumps(sorted(name for name, on in collect.items() if on)))
        return {"filesVisited": visited, "bytesRead": read_total, "pendingBytes": pending, **cadence}

    def _resolve_missing(self, conn: Any, provider: str, found: set[str], now: int) -> bool:
        """A source that was deleted or went undiscovered while unverified keeps no unconfirmed usage."""
        changed = False
        for source in store.unverified_sources(conn, provider):
            if source["path_key"] in found:
                continue
            key = source["path_key"]
            store.discard_provisional(conn, provider, key)
            conn.execute("DELETE FROM ledger_omissions WHERE provider=? AND source_key=?", (provider, key))
            # The discarded tail is read again if the file reappears, as after a replacement.
            store.save_source(conn, provider, key, generation=source["generation"] + 1, byte_offset=0,
                              head_key=None, state=_replay_state(source["byte_offset"]))
            store.add_gap(conn, provider, "source-missing-unverified", now, source["byte_offset"])
            store.bump_counter(conn, provider, _GAP_COUNTERS["source-missing-unverified"])
            changed = True
        return changed

    def _remaining(self, provider: str, path: Path, cursor: dict[str, Any]) -> int:
        """Unverified bytes, less the partially hashed span this process is holding."""
        remaining = _verify_remaining(cursor["state"], cursor["byte_offset"])
        partial = self._partial_verification.get((provider, self._path_key(path)))
        if partial and cursor["state"].get("verify"):
            remaining = max(1, remaining - partial["position"])
        return remaining

    @staticmethod
    def _next_complete_line(path: Path, start: int, end: int) -> tuple[int | None, int]:
        """Find the next complete supported record boundary with one bounded line read."""
        if start >= end:
            return None, 0
        want = min(MAX_RECORD_BYTES + 1, end - start)
        with path.open("rb") as handle:
            handle.seek(start)
            line = handle.readline(want)
        newline = line.find(b"\n")
        return (start + newline + 1 if newline >= 0 else None), len(line)

    def _pending(self, discovered: list[tuple[str, Path]], collect: Mapping[str, bool],
                 source_cursors: Mapping[tuple[str, str], dict[str, Any] | None] | None) -> int:
        total = 0
        for provider, path in discovered:
            if not collect.get(provider):
                continue
            try:
                info = path.stat()
                size = info.st_size
            except OSError:
                continue
            identity = self._current_identity(provider, path)
            cursor = (source_cursors.get(identity) if source_cursors is not None
                      else store.load_source(self.conn, *identity))
            total += max(0, size - (cursor["byte_offset"] if cursor else 0))
            if cursor:
                verify = cursor["state"].get("verify")
                if verify:
                    total += self._remaining(provider, path, cursor)
                elif cursor["state"].get("verification_debt"):
                    total += max(1, cursor["byte_offset"])
                elif cursor["byte_offset"] and cursor["state"].get("file_stamp") != _file_stamp(info):
                    total += cursor["byte_offset"]
        return total

    def _verifying(self, discovered: list[tuple[str, Path]], provider: str,
                   source_cursors: Mapping[tuple[str, str], dict[str, Any] | None] | None) -> int:
        total = 0
        for name, path in discovered:
            if name != provider:
                continue
            identity = self._current_identity(provider, path)
            cursor = (source_cursors.get(identity) if source_cursors is not None
                      else store.load_source(self.conn, *identity))
            if cursor and cursor["state"].get("verify"):
                total += self._remaining(provider, path, cursor)
            elif cursor and cursor["state"].get("verification_debt"):
                total += max(1, cursor["byte_offset"])
            elif cursor and cursor["byte_offset"]:
                try:
                    if cursor["state"].get("file_stamp") != _file_stamp(path.stat()):
                        total += cursor["byte_offset"]
                except OSError:
                    continue
        return total

    def ingest_file(self, provider: str, path: Path, budget: int | None, fault: FaultHook | None,
                    should_stop: Callable[[], bool] | None = None, *, block_events: bool = False) -> int:
        self._last_outcome = {"cursorAdvanced": False, "verificationAdvanced": False}
        self._last_fast_idle = False
        identity = self._current_identity(provider, path)
        self._last_identity = identity
        key = identity[1]
        cursor = None
        try:
            info = path.stat()
            self._refresh_pass_cursors()
            if self._pass_cursors is not None:
                identity = self._current_identity(provider, path)
                self._last_identity = identity
                key = identity[1]
            cursor = (self._pass_cursors.get((provider, key)) if self._pass_cursors is not None
                      else store.load_source(self.conn, provider, key))
            self._last_cursor = cursor
            if block_events:
                return self._defer_ordered_source(provider, path, info, cursor, fault)
            state = (cursor or {}).get("state", {})
            idle = bool(self._pass_cursors is not None and cursor and info.st_size == cursor["byte_offset"]
                        and state.get("file_stamp") == _file_stamp(info)
                        and not any(state.get(k) for k in
                                    ("verification_debt", "verify", "provisional", "ordered_tail_pending")))
            plan = None if idle else self._plan(provider, path, info, cursor, budget, should_stop)
        except _VerificationReadError as error:
            self._partial_verification.pop((provider, key), None)
            with self._transaction() as conn:
                store.bump_counter(conn, provider, "readFailures")
                if cursor and cursor["byte_offset"]:
                    state = error.state or {**_fresh_state(), **deepcopy(cursor["state"])}
                    debt = state.get("verification_debt")
                    if debt:
                        now = self.clock()
                        if error.work:
                            delay = min(VERIFY_RETRY_MAX_SECONDS,
                                        int(debt.get("retry_delay_seconds") or VERIFY_RETRY_START_SECONDS))
                            debt.update(retry_pending=True, next_attempt_epoch=now + delay,
                                        retry_delay_seconds=min(VERIFY_RETRY_MAX_SECONDS, delay * 2))
                        store.save_source(conn, provider, key, generation=cursor["generation"],
                                          byte_offset=cursor["byte_offset"], head_key=cursor["head_key"], state=state)
            return error.work
        except OSError:
            self._partial_verification.pop((provider, key), None)
            with store.transaction(self.conn) as conn:
                store.bump_counter(conn, provider, "readFailures")
            return 0
        final_stat_failed = False
        try:
            stable = _file_stamp(path.stat()) == _file_stamp(info)
        except OSError:
            stable = False
            final_stat_failed = True
            # A read failure is a fact about this attempt: it is counted on its own, even when the pass
            # index is stale or the cursor write that follows fails.
            with store.transaction(self.conn) as conn:
                store.bump_counter(conn, provider, "readFailures")
        if plan is None:
            if not stable:
                self._partial_verification.pop((provider, key), None)
            self._last_fast_idle = True
            return 0
        if not stable:
            self._partial_verification.pop((provider, key), None)
            scan_work = int(plan.get("verification_work", 0))
            if cursor is not None and (plan.get("state_changed") or scan_work):
                # The plan is discarded as a unit when the final stamp check fails. Keep the
                # persisted cursor/debt, even if the discarded plan had tentatively cleared it.
                state = {**_fresh_state(), **cursor["state"]}
                debt = dict(state.get("verification_debt") or
                            plan["state"].get("verification_debt") or {})
                if not debt and cursor["byte_offset"]:
                    debt = {"open": True, "generation": cursor["generation"],
                            "first_deferred_epoch": self.clock(), "last_change_epoch": self.clock(),
                            "last_stamp": state.get("file_stamp"), "last_size": cursor["byte_offset"]}
                changed_stamp = None
                if debt:
                    try:
                        changed_stamp = _file_stamp(path.stat())
                    except OSError:
                        changed_stamp = None
                    now = self.clock()
                    if changed_stamp is not None:
                        if (not debt.get("replacement_observed")
                                and _file_identity_changed(debt.get("last_stamp") or state.get("file_stamp"),
                                                           changed_stamp)):
                            debt.update(replacement_observed=True, replacement_boundary=changed_stamp[0])
                        debt.update(last_stamp=changed_stamp, last_size=changed_stamp[0],
                                    last_change_epoch=now)
                    debt.update(open=True, generation=cursor["generation"])
                    if scan_work:
                        delay = min(VERIFY_RETRY_MAX_SECONDS,
                                    int(debt.get("retry_delay_seconds") or VERIFY_RETRY_START_SECONDS))
                        debt.update(retry_pending=True, next_attempt_epoch=now + delay,
                                    retry_delay_seconds=min(VERIFY_RETRY_MAX_SECONDS, delay * 2))
                    state["verification_debt"] = debt
                    state.pop("verify", None)
                with self._transaction() as conn:
                    if debt and changed_stamp is None and not final_stat_failed:
                        store.bump_counter(conn, provider, "readFailures")
                    store.save_source(conn, provider, key, generation=cursor["generation"],
                                      byte_offset=cursor["byte_offset"], head_key=cursor["head_key"], state=state)
            return plan.get("work", plan["consumed"])
        if cursor is not None and not plan["consumed"] and not plan["gaps"] and not plan.get("state_changed"):
            return plan.get("work", 0)
        with self._transaction() as conn:
            self._apply(conn, provider, key, plan, fault)
        old_generation = cursor["generation"] if cursor else plan["generation"]
        generation_changed = plan["generation"] != old_generation
        self._last_outcome = {
            "cursorAdvanced": bool(plan["offset"] > 0 if generation_changed
                                    else plan["offset"] > (cursor["byte_offset"] if cursor else 0)),
            "verificationAdvanced": bool(plan.get("verification_advanced")),
        }
        return plan.get("work", plan["consumed"])

    def _defer_ordered_source(self, provider: str, path: Path, info: Any,
                              cursor: dict[str, Any] | None, fault: FaultHook | None) -> int:
        """Observe queued Codex files without committing their counter or tag events."""
        key = self._path_key(path)
        if cursor is not None and info.st_size < cursor["byte_offset"]:
            state = _replay_state(info.st_size)
            plan = {"generation": cursor["generation"] + 1, "offset": 0, "head_key": None,
                    "state": state, "gaps": [("truncation", cursor["byte_offset"])],
                    "facts": [], "threads": {}, "omissions": [], "unknown_tags": [],
                    "consumed": 0, "work": 0, "state_changed": True, "counters": {}}
            try:
                stable = _file_stamp(path.stat()) == _file_stamp(info)
            except OSError:
                stable = False
                with store.transaction(self.conn) as conn:
                    store.bump_counter(conn, provider, "readFailures")
            if stable:
                with self._transaction() as conn:
                    self._apply(conn, provider, key, plan, fault)
                return 0
            return 0
        try:
            # Opening the source records access failures immediately, without parsing queued events.
            with path.open("rb"):
                pass
        except OSError:
            with store.transaction(self.conn) as conn:
                store.bump_counter(conn, provider, "readFailures")
            return 0
        if cursor is None or not cursor["byte_offset"]:
            return 0
        stamp = _file_stamp(info)
        state = {**_fresh_state(), **deepcopy(cursor["state"])}
        debt = state.get("verification_debt")
        changed = False
        if debt:
            previous_stamp = debt.get("last_stamp")
            if previous_stamp != stamp:
                debt.update(last_stamp=stamp, last_size=info.st_size, last_change_epoch=self.clock(),
                            tail_boundary=info.st_size)
                debt.pop("verified_stamp", None)
                if (not debt.get("replacement_observed") and
                        (_file_identity_changed(previous_stamp, stamp)
                         or (cursor["head_key"] and not _head_matches(path, cursor["head_key"])) )):
                    debt.update(replacement_observed=True, replacement_boundary=info.st_size)
                changed = True
        elif state.get("file_stamp") != stamp:
            now = self.clock()
            replacement_observed = bool(
                _file_identity_changed(state.get("file_stamp"), stamp)
                or (cursor["head_key"] and not _head_matches(path, cursor["head_key"]))
            )
            debt = {"open": True, "generation": cursor["generation"],
                    "first_deferred_epoch": now, "last_change_epoch": now,
                    "last_stamp": stamp, "last_size": info.st_size,
                    "replacement_observed": replacement_observed,
                    "replacement_boundary": info.st_size if replacement_observed else None,
                    "tail_boundary": info.st_size}
            state["verification_debt"] = debt
            changed = True
        if changed:
            with self._transaction() as conn:
                store.save_source(conn, provider, key, generation=cursor["generation"],
                                  byte_offset=cursor["byte_offset"], head_key=cursor["head_key"], state=state)
        return 0

    def _plan(self, provider: str, path: Path, info: Any, cursor: dict[str, Any] | None,
              budget: int | None, should_stop: Callable[[], bool] | None = None) -> dict[str, Any]:
        generation, offset, head_key = 1, 0, None
        state = _fresh_state()
        gaps: list[tuple[str, int]] = []
        work = 0
        state_changed = False
        provisional = False  # tail is read while the prefix is still unverified
        tail_read = False  # verification work is already accounted for in the remaining tail allowance
        tail_budget = 0
        promote = False
        verification_work = 0
        verification_advanced = False
        verified_tail_boundary: int | None = None
        checkpoint_before: tuple[bool, int] | None = None
        if cursor is not None:
            generation, offset, head_key = cursor["generation"], cursor["byte_offset"], cursor["head_key"]
            state = {**_fresh_state(), **deepcopy(cursor["state"])}
            reset = None
            if info.st_size < offset:
                reset = "truncation"
            elif offset and (state.get("verification_debt") or state.get("file_stamp") != _file_stamp(info)):
                spans = _spans(state, offset)
                if not spans:
                    reset = "replacement"  # Legacy cursor has no trustworthy consumed-prefix hashes.
                else:
                    now = self.clock()
                    stamp = _file_stamp(info)
                    debt = state.get("verification_debt")
                    if debt is None:
                        replacement_observed = bool(
                            _file_identity_changed(state.get("file_stamp"), stamp)
                            or (head_key and not _head_matches(path, head_key))
                        )
                        debt = {"open": True, "generation": generation,
                                "first_deferred_epoch": now, "last_change_epoch": now,
                                "last_stamp": stamp, "last_size": info.st_size,
                                "replacement_observed": replacement_observed,
                                "replacement_boundary": info.st_size if replacement_observed else None,
                                "tail_boundary": info.st_size}
                        state["verification_debt"] = debt
                        state_changed = True
                    elif debt.get("last_stamp") != stamp:
                        previous_stamp = debt.get("last_stamp")
                        debt["last_change_epoch"] = now
                        debt["last_stamp"] = stamp
                        debt["last_size"] = info.st_size
                        debt["tail_boundary"] = info.st_size
                        debt.pop("verified_stamp", None)
                        if (not debt.get("replacement_observed")
                                and (_file_identity_changed(previous_stamp, stamp)
                                     or (head_key and not _head_matches(path, head_key)))):
                            debt["replacement_observed"] = True
                            debt["replacement_boundary"] = info.st_size
                        state_changed = True
                    if debt.get("verified_stamp") == stamp:
                        # Compatibility with persisted round-one state: exact verification succeeded.
                        verified_tail_boundary = int(debt.get("tail_boundary", info.st_size))
                        state.pop("verification_debt", None)
                        state["file_stamp"] = stamp
                        state_changed = True
                    else:
                        verify = state.get("verify")
                        restarts = int(verify.get("restarts") or 0) if verify else 0
                        if verify and verify.get("stamp") != stamp:
                            cache_key = (provider, self._path_key(path))
                            partial = self._partial_verification.get(cache_key)
                            scan_progress_lost = bool(verify.get("boundary") or int(verify.get("next", 0))
                                                     or (partial and partial.get("position")))
                            self._partial_verification.pop(cache_key, None)
                            state.pop("verify", None)
                            restarts += 1
                            if info.st_size > int(verify.get("size") or 0):
                                restarts += 0  # preserve the historical growth count in the checkpoint
                            if scan_progress_lost:
                                delay = min(VERIFY_RETRY_MAX_SECONDS,
                                            int(debt.get("retry_delay_seconds") or VERIFY_RETRY_START_SECONDS))
                                debt.update(retry_pending=True, next_attempt_epoch=now + delay,
                                            retry_delay_seconds=min(VERIFY_RETRY_MAX_SECONDS, delay * 2))
                                state["verification_debt"] = debt
                                return {"generation": generation, "offset": offset, "head_key": head_key,
                                        "state": state, "gaps": [], "facts": [], "threads": {}, "omissions": [],
                                        "consumed": 0, "work": 0, "state_changed": True, "counters": {},
                                        "deferred": True}
                        due = (now - float(debt.get("last_change_epoch", now)) >= IDLE_VERIFY_SECONDS
                               or now - float(debt.get("first_deferred_epoch", now)) >= DEADLINE_VERIFY_SECONDS
                               or max(0, info.st_size - offset) > DEBT_BACKLOG_BYTES
                               or (debt.get("retry_pending")
                                   and now >= float(debt.get("next_attempt_epoch", 0))))
                        if not due or now < float(debt.get("next_attempt_epoch", 0)):
                            return {"generation": generation, "offset": offset, "head_key": head_key,
                                    "state": state, "gaps": [], "facts": [], "threads": {}, "omissions": [],
                                    "consumed": 0, "work": 0, "state_changed": state_changed, "counters": {},
                                    "deferred": True}
                        debt.pop("retry_pending", None)
                    if verified_tail_boundary is None:
                        verify = state.get("verify")
                        if not verify or verify.get("stamp") != stamp:
                            verify = {"stamp": stamp, "next": 0, "boundary": False, "count": len(spans),
                                      "restarts": restarts, "size": info.st_size}
                        verify.setdefault("count", len(spans))
                        checkpoint_before = (bool(verify.get("boundary")), int(verify.get("next", 0)))
                    else:
                        verify = None
                    capped = bool(verified_tail_boundary is None
                                  and now - float(debt.get("first_deferred_epoch", now)) >= DEADLINE_VERIFY_SECONDS
                                  and not self._capped_scan_used)
                    if capped:
                        self._capped_scan_used = True
                    count = min(int(verify["count"]), len(spans)) if verify else 0
                    starts = []
                    span_start = 0
                    for length, _digest_text in spans:
                        starts.append(span_start)
                        span_start += length
                    # Leave a bounded allowance for complete tail records when exact verification finishes.
                    tail_reserve = (min(_READ_CHUNK, max(1, budget // 4))
                                    if budget is not None and info.st_size > offset else 0)
                    # A capped scan ignores the pass budget, but only one is allowed per pass.
                    limit = offset + _HASH_CHUNK if budget is None or capped else max(1, budget - tail_reserve)
                    # Finish the boundary span first. A partial digest is process-local;
                    # only completed spans advance the durable cursor.
                    cache_key = (provider, self._path_key(path))
                    stopped = False
                    if verify:
                        try:
                            with path.open("rb") as handle:
                                while work < limit and (not verify["boundary"] or verify["next"] < count - 1):
                                    if should_stop and should_stop():
                                        stopped = True  # checked once per hashed chunk; commits nothing from this pass
                                        break
                                    index = count - 1 if not verify["boundary"] else verify["next"]
                                    length, expected = spans[index]
                                    identity = (generation, tuple(_file_stamp(info)), index, expected, length)
                                    partial = self._partial_verification.get(cache_key)
                                    if not partial or partial["identity"] != identity:
                                        partial = {"identity": identity, "position": 0,
                                                   "digest": hashlib.blake2b(digest_size=16)}
                                        self._partial_verification[cache_key] = partial
                                    handle.seek(starts[index] + partial["position"])
                                    want = min(length - partial["position"], limit - work)
                                    try:
                                        chunk = handle.read(want)
                                    except OSError as error:
                                        state["verify"] = verify
                                        state["verification_debt"] = debt
                                        raise _VerificationReadError(str(error), work, state) from error
                                    work += len(chunk)
                                    if len(chunk) != want:
                                        state["verify"] = verify
                                        state["verification_debt"] = debt
                                        raise _VerificationReadError("short read while verifying source", work, state)
                                    partial["digest"].update(chunk)
                                    partial["position"] += len(chunk)
                                    if partial["position"] != length:
                                        break
                                    self._partial_verification.pop(cache_key, None)
                                    if partial["digest"].hexdigest() != expected:
                                        reset = "replacement"
                                        break
                                    if not verify["boundary"]:
                                        verify["boundary"] = True
                                    else:
                                        verify["next"] = index + 1
                        except _VerificationReadError:
                            raise
                        except OSError as error:
                            state["verify"] = verify
                            state["verification_debt"] = debt
                            raise _VerificationReadError(str(error), work, state) from error
                    if stopped:
                        # The unfinished digest is dropped; the durable cursor has not moved.
                        self._partial_verification.pop(cache_key, None)
                        return {"generation": generation, "offset": offset, "head_key": head_key,
                                "state": state, "gaps": [], "facts": [], "threads": {}, "omissions": [],
                                "consumed": 0, "work": work, "verification_work": work,
                                "state_changed": False, "counters": {}}
                    verification_work = work
                    if not reset:
                        state_changed = True
                        if verify and (not verify["boundary"] or verify["next"] < count - 1):
                            state["verify"] = verify
                            verification_advanced = bool(work and checkpoint_before !=
                                                         (bool(verify.get("boundary")), int(verify.get("next", 0))))
                            # Debt blocks every provider's tail until exact verification succeeds.
                            return {"generation": generation, "offset": offset, "head_key": head_key,
                                    "state": state, "gaps": [], "facts": [], "threads": {}, "omissions": [],
                                    "consumed": 0, "work": work, "verification_work": work,
                                    "state_changed": True, "counters": {},
                                    "verification_advanced": verification_advanced}
                        else:
                            state.pop("verify", None)
                            verified_tail_boundary = int(debt.get("tail_boundary", info.st_size))
                            state.pop("verification_debt", None)
                            state["file_stamp"] = stamp
                            verification_advanced = bool(work)
                            promote = bool(state.get("provisional"))
                            if budget is not None and work:
                                # The tail reader receives only the allowance left by this verified plan.
                                # A capped scan ignored the budget for the prefix; its tail still gets the
                                # normal pass budget, so constant appends cannot hold confirmation to one
                                # small allowance per deadline.
                                budget = max(1, budget - work) if not capped else max(1, budget)
                                tail_read = True
            if reset:
                self._partial_verification.pop((provider, self._path_key(path)), None)
                gaps.append((reset, offset))
                generation, offset, head_key = generation + 1, 0, None
                debt = state.get("verification_debt") or {}
                replay_end = (int(debt.get("replacement_boundary") or info.st_size)
                              if (reset == "replacement" and debt.get("replacement_observed")
                                  and info.st_size > int(debt.get("replacement_boundary") or 0))
                              else info.st_size)
                state = _replay_state(replay_end)
                # Do not spend ingestion's record completion allowance in a verification pass.
                if work and budget is not None:
                    return {"generation": generation, "offset": 0, "head_key": None, "state": state,
                            "gaps": gaps, "facts": [], "threads": {}, "omissions": [],
                            "consumed": 0, "work": work, "verification_work": verification_work,
                            "state_changed": True, "counters": {}}
        if provisional:
            budget = max(1, tail_budget)
        elif budget is not None and not tail_read:
            budget = max(1, budget - work)
        remaining = info.st_size - offset
        data = b""
        if remaining > 0:
            want = min(remaining, _READ_CHUNK if budget is None else max(1, budget), MAX_RECORD_BYTES + 1)
            self.stats["sourceReads"] += 1
            with path.open("rb") as handle:
                handle.seek(offset)
                data = handle.read(want)
                # Finish at most one record beyond the budget, without reading the
                # remainder of the file or allowing an unbounded record buffer.
                if not state.get("skip_oversize"):
                    while b"\n" not in data and len(data) <= MAX_RECORD_BYTES and len(data) < remaining:
                        data += handle.read(min(_READ_CHUNK, MAX_RECORD_BYTES + 1 - len(data),
                                                remaining - len(data)))
        if state.get("skip_oversize"):
            newline = data.find(b"\n")
            cut = newline + 1 if newline >= 0 else len(data)
            state["skip_oversize"] = newline < 0
            complete = b""
        elif len(data) > MAX_RECORD_BYTES and (data.find(b"\n") < 0 or data.find(b"\n") > MAX_RECORD_BYTES):
            gaps.append(("oversize", offset))
            state["skip_oversize"] = True
            cut = len(data)
            complete = b""
        else:
            cut = data.rfind(b"\n") + 1
            complete = data[:cut]
        if offset == 0 and complete:
            length = min(HEAD_BYTES, len(complete))
            head_key = f"{length}:{_digest(complete[:length])}"
        if cut:
            spans = _spans(state, offset)
            if provisional:
                # The prefix is unverified, so its last span is not re-read: hash exactly what was parsed.
                for index in range(0, cut, _HASH_CHUNK):
                    piece = complete[index:index + _HASH_CHUNK]
                    spans.append((len(piece), _digest(piece)))
            else:
                start = offset
                if spans and spans[-1][0] < _HASH_CHUNK:
                    start = offset - spans.pop()[0]
                with path.open("rb") as handle:
                    handle.seek(start)
                    left = offset + cut - start
                    while left:
                        chunk = handle.read(min(_HASH_CHUNK, left))
                        if not chunk:
                            raise OSError("source changed while hashing")
                        spans.append((len(chunk), _digest(chunk)))
                        left -= len(chunk)
                state["file_stamp"] = _file_stamp(info)
            _store_spans(state, spans)
        ordered = state.get("ordered_tail_pending") if provider == model_mod.CODEX else None
        if verified_tail_boundary is not None and provider == model_mod.CODEX:
            order_boundary, probe_work = self._next_complete_line(path, offset + cut, verified_tail_boundary)
            work += probe_work
            if order_boundary is not None:
                state["ordered_tail_pending"] = {"boundary": order_boundary,
                                                  "tail_boundary": verified_tail_boundary}
            elif offset + cut + probe_work < verified_tail_boundary:
                # An oversize record hides the next boundary: hold the gate until it is skipped.
                state["ordered_tail_pending"] = {"boundary": offset + cut + probe_work,
                                                  "tail_boundary": verified_tail_boundary}
        elif ordered is not None and offset + cut >= int(ordered.get("boundary", 0)):
            order_boundary, probe_work = self._next_complete_line(
                path, offset + cut, int(ordered.get("tail_boundary", info.st_size)))
            work += probe_work
            tail_boundary = int(ordered.get("tail_boundary", info.st_size))
            if order_boundary is None and offset + cut + probe_work >= tail_boundary:
                state.pop("ordered_tail_pending", None)
            elif order_boundary is None:
                # No complete record before the verified boundary yet (an oversize record is being
                # skipped): keep the gate until the boundary is passed.
                state["ordered_tail_pending"] = {"boundary": offset + cut + probe_work,
                                                  "tail_boundary": tail_boundary}
            else:
                state["ordered_tail_pending"] = {"boundary": order_boundary,
                                                  "tail_boundary": int(ordered.get("tail_boundary", info.st_size))}
        items, malformed = _parse(complete)
        eof = len(data) == remaining  # the whole remaining file was scanned in this pass
        if provider == model_mod.CLAUDE:
            facts, unknown, threads, omissions = self._claude_pass(path, items, state, generation, offset)
        else:
            facts, unknown, threads, omissions = self._codex_pass(path, items, state, generation, offset)
        if state.get("replay_ambiguous") and offset + cut >= state.get("replay_end", info.st_size):
            state.pop("replay_ambiguous", None)
            state.pop("replay_end", None)
        gaps.extend(state.pop("ambiguous_gaps", []))
        omissions.extend((_omission_key(path, generation, offset + pos), True, None, None)
                         for pos in malformed)
        unknown_tags = state.pop("unknown_tags", [])
        if not state["evidence_done"] and (state["slug"] or eof):
            state["evidence_done"] = True
        classification = classify_mod.classify(
            slug=state["slug"], agent_type=state["agent_type"], exec_review=state["exec_review"],
            read_only=state["read_only"], interactive=state["interactive"], review_title=state["review_title"])
        for fact in facts:
            segment = fact.pop("segment", None)
            if segment:
                # A tagged segment outranks inferred evidence for every record after its tag.
                tagged = classify_mod.classify(tag=segment["kind"], registry=self.registry)
                fact.update(kind=tagged["kind"], rule=tagged["rule"], segment_turn=segment["turn"])
            else:
                fact.update(kind=classification["kind"], rule=classification["rule"], segment_turn=None)
            fact.update(slug=state["slug"], classification_version=classification["classificationVersion"])
        observed = state.pop("observed_keys", [])
        if provisional:
            # Tail facts and thread changes wait, under this source, until the prefix verifies.
            if cut:
                state["provisional"] = True
            state.setdefault("prov_threads", {}).update(threads)
            threads = {}
            extra = {"provisional": True, "epoch": int(state.get("epoch") or 0)}
        else:
            extra = self._promotion(state, promote)
        return {"generation": generation, "offset": offset + cut, "head_key": head_key, "state": state,
                "gaps": gaps, "facts": facts, "threads": threads, "omissions": omissions,
                "unknown_tags": unknown_tags, "consumed": cut, "observed": observed,
                "work": work + cut, "state_changed": state_changed,
                "verification_work": verification_work,
                "verification_advanced": verification_advanced,
                "counters": {"malformedLines": len(malformed), "unknownUsageRecords": unknown}, **extra}

    @staticmethod
    def _promotion(state: dict[str, Any], promote: bool) -> dict[str, Any]:
        """Hand a verified generation's staged facts and thread changes to the transaction."""
        if not promote:
            return {}
        epoch = int(state.get("epoch") or 0)
        threads = state.pop("prov_threads", {})
        state.pop("provisional", None)
        state["epoch"] = epoch + 1
        return {"promote": True, "epoch": epoch, "promote_threads": threads}

    def _thread_row(self, state: Mapping[str, Any], provider: str, thread: str) -> dict[str, Any] | None:
        row = (state.get("prov_threads") or {}).get(thread)
        return row if row is not None else store.load_thread(self.conn, provider, thread)

    def _load_segment(self, state: dict[str, Any], provider: str, thread: str) -> None:
        """The active kind segment is per thread, so it carries into resumed files."""
        row = self._thread_row(state, provider, thread)
        state["thread_loaded"] = True
        if row and isinstance(row.get("segment"), dict):
            state["segment"] = row["segment"]
            state["tag_count"] = int(row.get("tag_count") or 0)

    def _record_tag(self, state: dict[str, Any], provider: str, path: Path,
                    item: dict[str, Any], thread: str, tag: str) -> None:
        if tag not in self.registry and tag != "other":
            event = model_mod.fact_key(provider, self._path_key(path), json.dumps(item, sort_keys=True))
            state.setdefault("unknown_tags", []).append((tag, event))
            tag = "other"
        _tag_turn(state, thread, tag)

    def _claude_pass(self, path: Path, items: list[tuple[int, dict[str, Any]]], state: dict[str, Any], generation: int, offset: int):
        state["thread"] = path.stem
        if not state["thread_loaded"]:
            self._load_segment(state, model_mod.CLAUDE, state["thread"])
        state["sub"] = "subagents" in path.parts or state["sub"]
        state["interactive"] = not state["sub"]
        if not state["evidence_done"]:
            if state["sub"] and state["agent_type"] is None:
                meta = _read_json(path.with_suffix(".meta.json"))
                if isinstance(meta, dict):
                    state["agent_type"] = claude_provider.public_agent_name(meta.get("agentType"))
            for _, item in items:
                if state["slug"]:
                    break
                if item.get("type") == "user":
                    text = _claude_user_text(item)
                    if classify_mod.title_has_review(text):
                        state["review_title"] = True
                    state["slug"] = classify_mod.recognised_slug(text)
        facts: list[dict[str, Any]] = []
        unknown = 0
        omissions: list[tuple[str, bool]] = []
        for position, item in items:
            if item.get("type") == "user":
                tag = classify_mod.kind_tag(_claude_user_text(item))
                if tag:
                    self._record_tag(state, model_mod.CLAUDE, path, item, state["thread"], tag)
            message = item.get("message") if isinstance(item.get("message"), dict) else {}
            usage = message.get("usage")
            message_id = message.get("id")
            message_key = model_mod.fact_key(model_mod.CLAUDE, "message", message_id) if isinstance(message_id, str) and message_id else None
            omission_key = _omission_key(path, generation, offset + position)
            epoch = providers._timestamp_epoch(item.get("timestamp"))
            if not isinstance(usage, dict):
                if item.get("type") == "assistant":
                    unknown += 1
                    omissions.append((omission_key, True, message_key, epoch))
                continue
            fields = model_mod.claude_usage(usage)
            if fields is None:
                omissions.append((omission_key, True, message_key, epoch))
                continue
            prompt_evidence = model_mod.claude_prompt_evidence(usage)
            if epoch is None:
                unknown += 1
                omissions.append((omission_key, True, message_key, epoch))
                continue
            if message_key:
                omissions.append((omission_key, False, message_key, epoch))
            identity = message.get("id") or item.get("requestId") or f"{item.get('timestamp')}|{sorted(fields.items())}"
            facts.append({
                "provider": model_mod.CLAUDE, "fact_key": model_mod.fact_key(model_mod.CLAUDE, identity),
                "thread_id": state["thread"], "model": claude_provider.public_model(message.get("model")) or "unknown",
                "price_tier": model_mod.claude_price_tier(claude_provider.public_model(message.get("model")) or "unknown", prompt_evidence),
                "prompt_evidence": prompt_evidence, "hour": _hour(epoch), "fields": fields, "merge": True,
                "segment": dict(state["segment"]) if state["segment"] else None,
            })
        threads: dict[str, Any] = {}
        if state["tag_count"]:
            base = self._thread_row(state, model_mod.CLAUDE, state["thread"]) or {}
            threads[state["thread"]] = {**base, "segment": state["segment"], "tag_count": state["tag_count"]}
        return facts, unknown, threads, omissions

    def _codex_pass(self, path: Path, items: list[tuple[int, dict[str, Any]]], state: dict[str, Any], generation: int, offset: int):
        facts: list[dict[str, Any]] = []
        unknown = 0
        counters: dict[str, Any] = {}
        touched: set[str] = set()
        seen: set[str] = set()
        ambiguous: list[tuple[Any, ...]] = []
        counter_epochs: dict[str, int | None] = {}
        counter_sources: dict[str, str | None] = {}
        source = self._path_key(path)  # immutable path key; session_meta's chat source is kept separately
        flagged: set[str] = set()
        omissions: list[tuple[str, bool]] = []
        if not state["thread_loaded"]:
            for _, item in items:
                head = item.get("payload") if isinstance(item.get("payload"), dict) else {}
                if item.get("type") == "session_meta":
                    # Only the first session_meta names the thread; a forked rollout's copied parent meta follows it.
                    if isinstance(head.get("id"), str) and head["id"]:
                        state["thread"] = head["id"][:128]
                    break
            state["thread"] = state["thread"] or path.stem
            self._load_segment(state, model_mod.CODEX, state["thread"])
        for position, item in items:
            payload = item.get("payload") if isinstance(item.get("payload"), dict) else {}
            user_text = _codex_user_text(item, payload)
            if user_text is not None:
                tag = classify_mod.kind_tag(user_text)
                if tag:
                    self._record_tag(state, model_mod.CODEX, path, item, state["thread"] or path.stem, tag)
                if not state["evidence_done"] and state["slug"] is None:
                    if classify_mod.title_has_review(user_text):
                        state["review_title"] = True
                    state["slug"] = classify_mod.recognised_slug(user_text)
            kind = item.get("type")
            if kind == "session_meta":
                if not state.get("meta_seen"):  # later metas are copied context, not this source's identity
                    state["meta_seen"] = True
                    state["started"] = state["started"] or providers._timestamp_epoch(payload.get("timestamp"))
                    if isinstance(payload.get("id"), str) and payload["id"]:
                        state["thread"] = state["meta_id"] = payload["id"][:128]
                    chat_source = providers._chat_source(payload.get("source"))
                    sub = bool(payload.get("parent_thread_id")) or chat_source in {"subagent", "review"}
                    state.update(sub=sub, exec_review=chat_source == "review",
                                 interactive=chat_source in {"app", "cli"} and not sub, read_only=_read_only(payload))
                elif (isinstance(payload.get("id"), str) and payload["id"] and state.get("meta_id")
                        and payload["id"][:128] != state["meta_id"]):
                    # A later meta naming another thread is copied parent context; its history is not this thread's.
                    state["foreign_context"] = True
            elif kind == "turn_context":
                if isinstance(payload.get("model"), str):
                    state["model"] = model_mod.raw_model(payload["model"])
            elif kind == "event_msg" and payload.get("type") == "token_count":
                info = payload.get("info") if isinstance(payload.get("info"), dict) else {}
                usage = info.get("total_token_usage")
                epoch = providers._timestamp_epoch(item.get("timestamp"))
                if not isinstance(usage, dict) or epoch is None:
                    unknown += 1
                    omissions.append((_omission_key(path, generation, offset + position), True, None, None))
                    continue
                thread = state["thread"] or path.stem
                current = {key: model_mod.count(usage.get(key)) or 0 for key in model_mod.COUNTER_KEYS}
                present = frozenset(key for key in model_mod.COUNTER_KEYS if model_mod.count(usage.get(key)) is not None)
                last_raw = info.get("last_token_usage")
                last = last_raw if isinstance(last_raw, dict) else None
                identity = _omission_key(path, generation, offset + position)
                if ((last_raw is not None and last is None) or
                        (last is not None and any(model_mod.count(value) is None for key, value in last.items()
                                                  if key in model_mod.COUNTER_KEYS))):
                    omissions.append((identity, True, None, None))
                    unknown += 1
                    continue
                if thread not in counters:
                    row = self._thread_row(state, model_mod.CODEX, thread)
                    counters[thread] = row.get("counters") if row else None
                    counter_epochs[thread] = row.get("counter_epoch") if row else None
                    counter_sources[thread] = row.get("counter_source") if row else None
                fact_identity = model_mod.fact_key(model_mod.CODEX, thread, item.get("timestamp"),
                                                   *(current[key] for key in model_mod.COUNTER_KEYS))
                if self.conn.execute("SELECT 1 FROM facts WHERE provider=? AND fact_key=?",
                                     (model_mod.CODEX, fact_identity)).fetchone() or fact_identity in seen:
                    state.setdefault("observed_keys", []).append(fact_identity)
                    continue
                seen.add(fact_identity)
                started = state["started"]
                seeded = state.setdefault("seeded", [])
                if started is not None and epoch < started:
                    # History before this session began: it never counts and never gaps. In a forked rollout it
                    # belongs to another counter domain, so it is skipped. Otherwise it may establish a baseline, and
                    # advance one that this source seeded from history, until the thread's own usage begins.
                    if not state.get("foreign_context"):
                        if counters[thread] is None:
                            counters[thread], counter_epochs[thread], counter_sources[thread] = current, epoch, source
                            if thread not in seeded:
                                seeded.append(thread)
                            touched.add(thread)
                        elif (thread in seeded and counter_sources.get(thread) == source
                              and counter_epochs.get(thread) is not None and epoch > counter_epochs[thread]):
                            # A seed follows history in time order, resets included: the newest report is the baseline.
                            counters[thread], counter_epochs[thread] = current, epoch
                            touched.add(thread)
                    continue
                if thread in seeded:
                    seeded.remove(thread)  # own usage begins: the seeded baseline becomes an ordinary baseline
                if (state.get("replay_ambiguous") and offset + position < state.get("replay_end", float("inf")) and counters[thread] is not None and
                        current["total_tokens"] < counters[thread]["total_tokens"]):
                    baseline_epoch = counter_epochs.get(thread)
                    endpoints = [value for value in (epoch, baseline_epoch) if value is not None]
                    ambiguous.append(("ambiguous-reset", 0, thread, min(endpoints), max(endpoints)))
                    continue
                baseline_epoch, baseline_source = counter_epochs.get(thread), counter_sources.get(thread)
                if (counters[thread] is not None and baseline_epoch is not None and baseline_source not in (None, source)
                        and epoch < baseline_epoch and current["total_tokens"] < counters[thread]["total_tokens"]):
                    # Across files, an earlier lower counter may be stale or a reset: withhold it, leave the baseline,
                    # and record one ambiguity gap per thread per pass rather than declaring it stale.
                    state.setdefault("observed_keys", []).append(fact_identity)
                    if thread not in flagged:
                        flagged.add(thread)
                        ambiguous.append(("ambiguous-reset", 0, thread, min(epoch, baseline_epoch), max(epoch, baseline_epoch)))
                    continue
                delta = providers._counter_delta(counters[thread], current, last)
                counters[thread] = current
                counter_epochs[thread] = epoch
                counter_sources[thread] = source
                touched.add(thread)
                if not any(delta.values()):
                    continue  # Equal counters are a duplicate report.
                model = state["model"] or "unknown"
                last_input = model_mod.count(last.get("input_tokens")) if last else None
                facts.append({
                    "provider": model_mod.CODEX,
                    "fact_key": model_mod.fact_key(model_mod.CODEX, thread, item.get("timestamp"),
                                                   *(current[key] for key in model_mod.COUNTER_KEYS)),
                    "thread_id": thread, "model": model,
                    "price_tier": model_mod.codex_price_tier(model, last_input), "hour": _hour(epoch),
                    "fields": model_mod.codex_usage(delta, present), "merge": False,
                    "segment": dict(state["segment"]) if state["segment"] else None,
                })
        main = state["thread"] or path.stem
        threads: dict[str, Any] = {}
        for name in set(touched) | ({main} if state["tag_count"] else set()):
            base = self._thread_row(state, model_mod.CODEX, name) or {}
            row = {**base, "counters": counters.get(name, base.get("counters")),
                   "counter_epoch": counter_epochs.get(name, base.get("counter_epoch")),
                   "counter_source": counter_sources.get(name, base.get("counter_source"))}
            if name == main:
                row.update(segment=state["segment"], tag_count=state["tag_count"])
            threads[name] = row
        state.setdefault("ambiguous_gaps", []).extend(ambiguous)
        return facts, unknown, threads, omissions

    def _apply(self, conn: Any, provider: str, key: str, plan: dict[str, Any], fault: FaultHook | None) -> None:
        now = int(self.clock())
        overrides = store.load_overrides(conn)
        changed = False
        generation = plan["generation"]
        if any(gap[0] in {"replacement", "truncation"} for gap in plan["gaps"]):
            conn.execute("DELETE FROM ledger_omissions WHERE provider=? AND source_key=?", (provider, key))
            # A rewrite was detected: the unverified tail is rolled back and re-derived by the replay.
            if store.has_provisional(conn, provider, key):
                store.discard_provisional(conn, provider, key)
                changed = True
        if plan.get("promote"):
            changed = store.promote_epoch(conn, provider, key, generation, plan["epoch"]) or changed
            for thread, row in plan.get("promote_threads", {}).items():
                store.save_thread(conn, provider, thread, _merge_promoted_thread(conn, provider, thread, row))
            for message_key, epoch in plan["state"].pop("prov_evidence", []):
                _record_valid(conn, provider, message_key, epoch)
            changed = True
        for fact in plan["facts"]:
            if plan.get("provisional"):
                store.stage_observation(conn, key, generation, plan["epoch"], fact)
                changed = True
                continue
            if store.upsert_fact(conn, fact, merge=fact["merge"], overrides=overrides):
                changed = True
            store.note_source_fact(conn, provider, key, fact["fact_key"], generation, int(fact["fields"]["total"]))
        for fact_key in plan.get("observed", []):
            store.note_source_fact(conn, provider, key, fact_key, generation, None)
        for thread, row in plan["threads"].items():
            store.save_thread(conn, provider, thread, row)
        for identity, unresolved, message_key, epoch in plan["omissions"]:
            if unresolved:
                evidence = (conn.execute("SELECT valid_epoch FROM ledger_message_evidence "
                                         "WHERE provider=? AND message_key=?", (provider, message_key)).fetchone()
                            if message_key and epoch is not None else None)
                if evidence is None or evidence[0] < epoch:
                    conn.execute("INSERT OR IGNORE INTO ledger_omissions(provider, identity, source_key, message_key, event_epoch) VALUES(?, ?, ?, ?, ?)",
                                 (provider, identity, key, message_key, epoch))
            elif message_key and epoch is not None:
                if plan.get("provisional"):
                    # Provisional evidence is reversible: it applies only when its epoch verifies.
                    plan["state"].setdefault("prov_evidence", []).append([message_key, epoch])
                else:
                    _record_valid(conn, provider, message_key, epoch)
        for kind, event in plan.get("unknown_tags", []):
            count = conn.execute("SELECT COUNT(*) FROM ledger_unknown_kinds WHERE provider=?", (provider,)).fetchone()[0]
            kinds = conn.execute("SELECT COUNT(DISTINCT kind) FROM ledger_unknown_kinds WHERE provider=?", (provider,)).fetchone()[0]
            known = conn.execute("SELECT 1 FROM ledger_unknown_kinds WHERE provider=? AND kind=? LIMIT 1", (provider, kind)).fetchone()
            if count < 4096 and (known or kinds < 64):
                conn.execute("INSERT OR IGNORE INTO ledger_unknown_kinds(provider, kind, event_key) VALUES(?, ?, ?)",
                             (provider, kind, event))
        for gap in plan["gaps"]:
            gap_kind, bytes_before = gap[:2]
            store.add_gap(conn, provider, gap_kind, now, bytes_before)
            if len(gap) > 2:
                gap_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
                conn.execute("INSERT INTO ledger_gap_details VALUES(?, ?, ?, ?)", (gap_id, *gap[2:]))
            store.bump_counter(conn, provider, _GAP_COUNTERS[gap_kind])
            changed = True
        for name, amount in plan["counters"].items():
            if amount:
                store.bump_counter(conn, provider, name, amount)
        state = plan["state"]
        if (state.get("audit") and not state.get("replay_ambiguous")
                and not state.get("verify") and not state.get("verification_debt")
                and not state.get("ordered_tail_pending")):
            # The replay of a rewritten file is complete: confirmed usage is never lowered, so report it.
            state.pop("audit")
            if store.audit_rewrite(conn, provider, key, generation):
                store.add_gap(conn, provider, "rewrite-lowered-confirmed", now, 0)
                changed = True
        if fault:
            fault("after_facts")
        store.save_source(conn, provider, key, generation=plan["generation"], byte_offset=plan["offset"],
                          head_key=plan["head_key"], state=plan["state"])
        if fault:
            fault("after_cursor")
        if store.meta_value(conn, "observedFrom") is None:
            store.meta_set(conn, "observedFrom", now)
        if store.meta_value(conn, f"observedFrom:{provider}") is None:
            store.meta_set(conn, f"observedFrom:{provider}", now)
        store.meta_set(conn, "lastIngestedAt", now)
        store.meta_set(conn, f"lastIngestedAt:{provider}", now)
        if changed:
            store.meta_set(conn, "revision", int(store.meta_value(conn, "revision") or 0) + 1)
