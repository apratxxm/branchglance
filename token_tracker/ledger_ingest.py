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
# A prefix whose verification restarted this many times (the file kept growing) is verified in one pass.
_VERIFY_RESTART_LIMIT = 8
_GAP_COUNTERS = {"truncation": "truncations", "replacement": "replacements", "oversize": "oversizeRecords", "ambiguous-reset": "ambiguousResets",
                 "source-missing-unverified": "missingUnverified"}
FaultHook = Callable[[str], None]


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
        self.claude_root = Path(claude_root)
        self.codex_root = Path(codex_root)
        self.clock = clock
        self.kind_registry_path = kind_registry_path
        self.registry = dict(classify_mod.DEFAULT_KIND_REGISTRY)
        self.stats = {"sourceReads": 0}
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

    def forget_provider(self, provider: str) -> None:
        """Drop in-memory partial verification for one provider (its stored rows were purged)."""
        for key in [key for key in self._partial_verification if key[0] == provider]:
            del self._partial_verification[key]

    def run(self, collect: Mapping[str, bool], budget: int | None = None,
            fault: FaultHook | None = None, should_stop: Callable[[], bool] | None = None) -> dict[str, Any]:
        self.registry = classify_mod.load_kind_registry(self.kind_registry_path) if self.kind_registry_path else dict(classify_mod.DEFAULT_KIND_REGISTRY)
        discovered = discover(self.claude_root, self.codex_root, collect)
        read_total = 0
        visited = 0
        for provider, path in discovered:
            if not collect.get(provider):
                continue
            if should_stop and should_stop():
                # Cooperative stop between files: nothing from this pass is left half-applied.
                return {"filesVisited": visited, "bytesRead": read_total, "pendingBytes": None, "stopped": True}
            if budget is not None and read_total >= budget:
                break
            remaining = None if budget is None else budget - read_total
            read_total += self.ingest_file(provider, path, remaining, fault, should_stop)
            visited += 1
        pending_by_provider = {provider: self._pending(discovered, {provider: bool(collect.get(provider))})
                               for provider in (model_mod.CLAUDE, model_mod.CODEX)}
        verifying = {provider: self._verifying(discovered, provider) if collect.get(provider) else 0
                     for provider in (model_mod.CLAUDE, model_mod.CODEX)}
        pending = sum(pending_by_provider.values())
        now = int(self.clock())
        found = {provider: {path_key(path) for name, path in discovered if name == provider}
                 for provider in (model_mod.CLAUDE, model_mod.CODEX)}
        with store.transaction(self.conn) as conn:
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
        return {"filesVisited": visited, "bytesRead": read_total, "pendingBytes": pending}

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
        partial = self._partial_verification.get((provider, path_key(path)))
        if partial and cursor["state"].get("verify"):
            remaining = max(1, remaining - partial["position"])
        return remaining

    def _pending(self, discovered: list[tuple[str, Path]], collect: Mapping[str, bool]) -> int:
        total = 0
        for provider, path in discovered:
            if not collect.get(provider):
                continue
            try:
                info = path.stat()
                size = info.st_size
            except OSError:
                continue
            cursor = store.load_source(self.conn, provider, path_key(path))
            total += max(0, size - (cursor["byte_offset"] if cursor else 0))
            if cursor:
                verify = cursor["state"].get("verify")
                if verify:
                    total += self._remaining(provider, path, cursor)
                elif cursor["byte_offset"] and cursor["state"].get("file_stamp") != _file_stamp(info):
                    total += cursor["byte_offset"]
        return total

    def _verifying(self, discovered: list[tuple[str, Path]], provider: str) -> int:
        total = 0
        for name, path in discovered:
            if name != provider:
                continue
            cursor = store.load_source(self.conn, provider, path_key(path))
            if cursor and cursor["state"].get("verify"):
                total += self._remaining(provider, path, cursor)
            elif cursor and cursor["byte_offset"]:
                try:
                    if cursor["state"].get("file_stamp") != _file_stamp(path.stat()):
                        total += cursor["byte_offset"]
                except OSError:
                    continue
        return total

    def ingest_file(self, provider: str, path: Path, budget: int | None, fault: FaultHook | None,
                    should_stop: Callable[[], bool] | None = None) -> int:
        key = path_key(path)
        try:
            info = path.stat()
            cursor = store.load_source(self.conn, provider, key)
            plan = self._plan(provider, path, info, cursor, budget, should_stop)
        except OSError:
            self._partial_verification.pop((provider, key), None)
            with store.transaction(self.conn) as conn:
                store.bump_counter(conn, provider, "readFailures")
            return 0
        try:
            stable = _file_stamp(path.stat()) == _file_stamp(info)
        except OSError:
            stable = False
        if not stable:
            self._partial_verification.pop((provider, key), None)
            return plan.get("work", plan["consumed"])
        if cursor is not None and not plan["consumed"] and not plan["gaps"] and not plan.get("state_changed"):
            return plan.get("work", 0)
        with store.transaction(self.conn) as conn:
            self._apply(conn, provider, key, plan, fault)
        return plan.get("work", plan["consumed"])

    def _plan(self, provider: str, path: Path, info: Any, cursor: dict[str, Any] | None,
              budget: int | None, should_stop: Callable[[], bool] | None = None) -> dict[str, Any]:
        generation, offset, head_key = 1, 0, None
        state = _fresh_state()
        gaps: list[tuple[str, int]] = []
        work = 0
        state_changed = False
        provisional = False  # tail is read while the prefix is still unverified
        tail_read = False  # a capped pass that completes verification reads the tail in the same pass
        tail_budget = 0
        promote = False
        if cursor is not None:
            generation, offset, head_key = cursor["generation"], cursor["byte_offset"], cursor["head_key"]
            state = {**_fresh_state(), **cursor["state"]}
            reset = None
            if info.st_size < offset:
                reset = "truncation"
            elif offset and state.get("file_stamp") != _file_stamp(info):
                spans = _spans(state, offset)
                if not spans:
                    reset = "replacement"  # Legacy cursor has no trustworthy consumed-prefix hashes.
                else:
                    verify = state.get("verify")
                    had_verify = bool(verify)
                    restarts = int(verify.get("restarts") or 0) if verify else 0
                    if verify and verify.get("stamp") != _file_stamp(info):
                        # Only growth counts toward the cap; a metadata-only change restarts without counting.
                        if info.st_size > int(verify.get("size") or 0):
                            restarts += 1
                    if not verify or verify.get("stamp") != _file_stamp(info):
                        verify = {"stamp": _file_stamp(info), "next": 0, "boundary": False, "count": len(spans),
                                  "restarts": restarts, "size": info.st_size}
                    verify.setdefault("count", len(spans))
                    capped = int(verify.get("restarts") or 0) >= _VERIFY_RESTART_LIMIT
                    count = min(int(verify["count"]), len(spans))
                    starts = [sum(length for length, _ in spans[:index]) for index in range(len(spans))]
                    # A pass that finds a changed file verifies first. Later passes reserve a quarter of the
                    # budget for the appended tail, so a growing file cannot starve its own ingestion.
                    reserve = budget // 4 if budget is not None and had_verify and info.st_size > offset else 0
                    # A capped verification ignores the budget for its loop only: it verifies the whole prefix in this pass.
                    limit = offset + _HASH_CHUNK if budget is None or capped else budget - reserve
                    # Finish the boundary span first. A partial digest is process-local;
                    # only completed spans advance the durable cursor.
                    cache_key = (provider, path_key(path))
                    stopped = False
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
                            chunk = handle.read(want)
                            work += len(chunk)
                            if len(chunk) != want:
                                raise OSError("source changed while verifying")
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
                    if stopped:
                        # The unfinished digest is dropped; the durable cursor has not moved.
                        self._partial_verification.pop(cache_key, None)
                        return {"generation": generation, "offset": offset, "head_key": head_key,
                                "state": state, "gaps": [], "facts": [], "threads": {}, "omissions": [],
                                "consumed": 0, "work": work, "state_changed": False, "counters": {}}
                    if not reset:
                        state_changed = True
                        if not verify["boundary"] or verify["next"] < count - 1:
                            state["verify"] = verify
                            # Codex counters are shared across files, so its tail waits for verification.
                            if reserve <= 0 or provider != model_mod.CLAUDE:
                                return {"generation": generation, "offset": offset, "head_key": head_key,
                                        "state": state, "gaps": [], "facts": [], "threads": {}, "omissions": [],
                                        "consumed": 0, "work": work, "state_changed": True, "counters": {}}
                            provisional = True
                            tail_budget = reserve
                        else:
                            state.pop("verify", None)
                            state["file_stamp"] = _file_stamp(info)
                            promote = bool(state.get("provisional"))
                            if budget is not None and work and not (capped and info.st_size > offset):
                                return {"generation": generation, "offset": offset, "head_key": head_key,
                                        "state": state, "gaps": [], "facts": [], "threads": {}, "omissions": [],
                                        "consumed": 0, "work": work, "state_changed": True, "counters": {},
                                        **self._promotion(state, promote)}
                            if budget is not None and work:
                                # A capped verification already overspent its budget; its confirmed tail is read now.
                                budget = max(budget - work, budget // 4, 1)
                                tail_read = True
            if reset:
                self._partial_verification.pop((provider, path_key(path)), None)
                gaps.append((reset, offset))
                generation, offset, head_key = generation + 1, 0, None
                state = _replay_state(info.st_size)
                # Do not spend ingestion's record completion allowance in a verification pass.
                if work and budget is not None:
                    return {"generation": generation, "offset": 0, "head_key": None, "state": state,
                            "gaps": gaps, "facts": [], "threads": {}, "omissions": [],
                            "consumed": 0, "work": work, "state_changed": True, "counters": {}}
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
            event = model_mod.fact_key(provider, path_key(path), json.dumps(item, sort_keys=True))
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
                "price_tier": model_mod.STANDARD_TIER, "hour": _hour(epoch), "fields": fields, "merge": True,
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
        source = path_key(path)  # immutable path key; session_meta's chat source is kept separately
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
        if state.get("audit") and not state.get("replay_ambiguous") and not state.get("verify"):
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
