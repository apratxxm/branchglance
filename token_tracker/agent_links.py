"""Exact, fail-closed links between Claude sessions and Codex executions.

This module deliberately exposes injectable OS functions so process identity,
ancestry and receipt rules can be tested without inspecting real processes.
"""
from __future__ import annotations

import ctypes
from functools import wraps
import json
import os
import re
import shlex
import tempfile
import threading
import time
import math
from pathlib import Path
from typing import Any, Callable, Iterable

THREAD_ID = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\Z")
MAX_RECEIPT_BYTES = 4096
MAX_INBOX_BYTES = 1024 * 1024
REGISTRY_NAME = "agent-runs.json"
INBOX_NAME = "codex-link-receipts"
RECEIPT_FILE = "receipts.jsonl"
RETENTION_SECONDS = 7 * 86400
_GRAPH_LOCK = threading.Lock()
_GRAPH_EDGES: dict[str, str | None] = {}
_GRAPH_REVISION = 0
_SID_LOCK = threading.Lock()
_CURRENT_SID: bytes | None = None
_LINK_LOCK = threading.RLock()
_SCAN_LOCK = threading.Lock()
_LAST_FULL_SCAN_AT = float("-inf")
_UNLINKED_LOCK = threading.Lock()
_UNLINKED_EXECUTIONS: dict[tuple[str, int, int], dict[str, Any]] = {}


def _serialized_link_poll(function):
    @wraps(function)
    def run(*args, **kwargs):
        with _LINK_LOCK:
            return function(*args, **kwargs)
    return run


def snapshot_graph(rows: Iterable[dict[str, Any]], *, delta: bool = False) -> dict[str, Any]:
    """Attach canonical row keys and return a revisioned graph (or live delta)."""
    global _GRAPH_EDGES, _GRAPH_REVISION
    indexed: dict[str, dict[str, Any]] = {}
    for row in rows:
        provider = row.get("provider") or row.get("providerName")
        identity = row.get("id") or row.get("sessionId")
        if not isinstance(provider, str) or not isinstance(identity, str):
            continue
        key = f"{provider}:{identity}"
        row["key"] = key
        indexed[key] = row
    current: dict[str, str | None] = {}
    for key, row in indexed.items():
        parent_id = row.get("parentId")
        parent_provider = row.get("parentProvider") or row.get("provider") or row.get("providerName")
        parent_key = f"{parent_provider}:{parent_id}" if isinstance(parent_id, str) and parent_id else None
        if parent_key not in indexed:
            parent_key = None
        row["parentKey"] = parent_key
        current[key] = parent_key
    # Any cycle is rendered as a set of roots; malformed/native stale parents
    # cannot create a loop in the public tree contract.
    for origin in tuple(current):
        path: list[str] = []
        positions: dict[str, int] = {}
        node: str | None = origin
        while node is not None and node in current:
            if node in positions:
                for cyclic in path[positions[node]:]:
                    current[cyclic] = None
                    indexed[cyclic]["parentKey"] = None
                break
            positions[node] = len(path)
            path.append(node)
            node = current[node]
    with _GRAPH_LOCK:
        if current != _GRAPH_EDGES:
            _GRAPH_REVISION += 1
            changes = {key: parent for key, parent in current.items() if _GRAPH_EDGES.get(key) != parent}
            changes.update({key: None for key in _GRAPH_EDGES.keys() - current.keys()})
            _GRAPH_EDGES = current
        else:
            changes = {}
        edges = changes if delta else dict(_GRAPH_EDGES)
        return {"revision": _GRAPH_REVISION, "edges": edges}


def _process_table() -> dict[int, tuple[int, str]]:
    from .claude_hook import _process_table as snapshot
    return snapshot()


def _read_current_user_sid() -> bytes | None:
    """Return the current token SID bytes, failing closed on any API error."""
    if os.name != "nt":
        return None
    try:
        advapi = ctypes.WinDLL("advapi32", use_last_error=True)
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.GetCurrentProcess.restype = ctypes.c_void_p
        advapi.OpenProcessToken.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.POINTER(ctypes.c_void_p)]
        advapi.OpenProcessToken.restype = ctypes.c_int
        advapi.GetTokenInformation.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p,
                                               ctypes.c_ulong, ctypes.POINTER(ctypes.c_ulong)]
        advapi.GetTokenInformation.restype = ctypes.c_int
        advapi.GetLengthSid.argtypes = [ctypes.c_void_p]
        advapi.GetLengthSid.restype = ctypes.c_ulong
        token = ctypes.c_void_p()
        if not advapi.OpenProcessToken(kernel.GetCurrentProcess(), 0x0008, ctypes.byref(token)):
            return None
        try:
            needed = ctypes.c_ulong()
            advapi.GetTokenInformation(token, 1, None, 0, ctypes.byref(needed))
            if not needed.value or needed.value > 65536:
                return None
            buffer = ctypes.create_string_buffer(needed.value)
            if not advapi.GetTokenInformation(token, 1, buffer, needed, ctypes.byref(needed)):
                return None
            sid_ptr = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_void_p))[0]
            length = advapi.GetLengthSid(sid_ptr)
            if not length or length > 256:
                return None
            return ctypes.string_at(sid_ptr, length)
        finally:
            kernel.CloseHandle(token)
    except Exception:
        return None


def _current_user_sid() -> bytes | None:
    global _CURRENT_SID
    if _CURRENT_SID is not None:
        return _CURRENT_SID
    with _SID_LOCK:
        if _CURRENT_SID is None:
            observed = _read_current_user_sid()
            if observed:
                _CURRENT_SID = observed
    return _CURRENT_SID


def _process_sid(pid: int) -> bytes | None:
    if os.name != "nt" or not isinstance(pid, int) or pid <= 0:
        return None
    try:
        advapi = ctypes.WinDLL("advapi32", use_last_error=True)
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
        kernel.OpenProcess.restype = ctypes.c_void_p
        advapi.OpenProcessToken.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.POINTER(ctypes.c_void_p)]
        advapi.OpenProcessToken.restype = ctypes.c_int
        advapi.GetTokenInformation.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p,
                                               ctypes.c_ulong, ctypes.POINTER(ctypes.c_ulong)]
        advapi.GetTokenInformation.restype = ctypes.c_int
        advapi.GetLengthSid.argtypes = [ctypes.c_void_p]
        advapi.GetLengthSid.restype = ctypes.c_ulong
        process = kernel.OpenProcess(0x1000, False, pid)  # QUERY_LIMITED_INFORMATION
        if not process:
            return None
        token = ctypes.c_void_p()
        try:
            if not advapi.OpenProcessToken(process, 0x0008, ctypes.byref(token)):
                return None
            try:
                needed = ctypes.c_ulong()
                advapi.GetTokenInformation(token, 1, None, 0, ctypes.byref(needed))
                if not needed.value or needed.value > 65536:
                    return None
                buffer = ctypes.create_string_buffer(needed.value)
                if not advapi.GetTokenInformation(token, 1, buffer, needed, ctypes.byref(needed)):
                    return None
                sid_ptr = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_void_p))[0]
                length = advapi.GetLengthSid(sid_ptr)
                return ctypes.string_at(sid_ptr, length) if 0 < length <= 256 else None
            finally:
                kernel.CloseHandle(token)
        finally:
            kernel.CloseHandle(process)
    except Exception:
        return None


def _process_created(pid: int) -> int | None:
    from .claude_hook import process_created
    return process_created(pid)


def _probe_process(pid: int) -> tuple[str, int | None]:
    from .claude_hook import probe_process
    return probe_process(pid)


def verify_process(pid: int, expected_created: int) -> bool | None:
    """Cheap bounded recheck for a remembered pid; None means unverifiable."""
    try:
        state, observed = _probe_process(pid)
    except Exception:
        return None
    # A finished earlier run of a resumed thread is "gone", not unverifiable;
    # treating it as unknown would block the thread's current link forever.
    if state == "gone":
        return False
    return observed == expected_created if state == "alive" and observed is not None else None


def _equal_sid(left: bytes | None, right: bytes | None) -> bool:
    if not left or not right:
        return False
    if os.name != "nt":
        return left == right
    try:
        advapi = ctypes.WinDLL("advapi32")
        a, b = ctypes.create_string_buffer(left), ctypes.create_string_buffer(right)
        return bool(advapi.EqualSid(ctypes.byref(a), ctypes.byref(b)))
    except Exception:
        return False


def _read_process_command_line(query: Callable[[Any, int], tuple[int, int]], *,
                               pointer_size: int | None = None) -> str | None:
    """Size-query and read one bounded PROCESS_COMMAND_LINE_INFORMATION result."""
    try:
        pointer_size = pointer_size or ctypes.sizeof(ctypes.c_void_p)
        pointer_offset = 8 if pointer_size == 8 else 4
        header = pointer_offset + pointer_size
        status, required = query(None, 0)
        maximum = header + 32767 * 2
        if status == 0 or required < header or required > maximum:
            return None
        buffer = ctypes.create_string_buffer(required)
        status, returned = query(buffer, required)
        if status != 0 or returned < header or returned > required:
            return None
        length = int.from_bytes(buffer.raw[:2], "little")
        maximum_length = int.from_bytes(buffer.raw[2:4], "little")
        pointer = int.from_bytes(buffer.raw[pointer_offset:pointer_offset + pointer_size], "little")
        base = ctypes.addressof(buffer)
        offset = pointer - base
        if (not length or length % 2 or length > 32767 * 2
                or maximum_length < length or maximum_length > 32767 * 2
                or offset < header or offset > returned
                or offset + maximum_length > returned):
            return None
        return ctypes.wstring_at(pointer, length // 2)
    except Exception:
        return None


def _command_line(pid: int) -> str | None:
    """Query ProcessCommandLineInformation (60); never retain the raw value."""
    if os.name != "nt":
        return None
    try:
        ntdll = ctypes.WinDLL("ntdll")
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
        kernel.OpenProcess.restype = ctypes.c_void_p
        kernel.CloseHandle.argtypes = [ctypes.c_void_p]
        ntdll.NtQueryInformationProcess.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p,
                                                    ctypes.c_ulong, ctypes.POINTER(ctypes.c_ulong)]
        ntdll.NtQueryInformationProcess.restype = ctypes.c_long
        process = kernel.OpenProcess(0x1000, False, pid)
        if not process:
            return None
        try:
            def query(buffer: Any, size: int) -> tuple[int, int]:
                returned = ctypes.c_ulong()
                status = ntdll.NtQueryInformationProcess(
                    process, 60, buffer, size, ctypes.byref(returned))
                return int(status), int(returned.value)
            return _read_process_command_line(query)
        finally:
            kernel.CloseHandle(process)
    except Exception:
        return None


def parse_exec_resume_thread(command_line: str | None) -> str | None:
    """Extract the UUID directly following exec resume."""
    invocation = parse_exec_invocation(command_line)
    return invocation[1] if invocation and invocation[0] == "resume" else None


def parse_exec_invocation(command_line: str | None) -> tuple[str, str | None, tuple[str, ...]] | None:
    """Parse supported codex exec and codex exec resume UUID argv."""
    if not isinstance(command_line, str) or len(command_line) > 32760:
        return None
    try:
        args = shlex.split(command_line, posix=False)
    except ValueError:
        return None
    if len(args) < 2 or args[1].strip('"').lower() != "exec":
        return None
    next_token = args[2].strip('"').lower() if len(args) > 2 else ""
    if next_token == "resume":
        if len(args) < 4:
            return None
        candidate = args[3].strip('"').lower()
        return ("resume", candidate, (candidate,)) if THREAD_ID.fullmatch(candidate) else None
    # Fresh exec prompt text is not an identity source, even if it contains a UUID.
    return ("exec", None, ())


def _filetime_epoch(created: int) -> float | None:
    try:
        value = int(created) / 10_000_000 - 11_644_473_600
        return value if math.isfinite(value) else None
    except (TypeError, ValueError, OverflowError):
        return None


def _association_map(claude_states: dict[str, Any]) -> dict[tuple[int, int], list[tuple[str, float, float]]]:
    grouped: dict[tuple[int, int], list[tuple[str, float, float]]] = {}
    for identity, entry in (claude_states or {}).items():
        if not isinstance(identity, tuple) or len(identity) != 2 or identity[1]:
            continue
        raw = entry.get("pid_associations") or {}
        associations = raw.values() if isinstance(raw, dict) else raw
        for association in associations:
            try:
                key = (int(association["pid"]), int(association["created"]))
                first, last = float(association["first"]), float(association["last"])
                if not math.isfinite(first) or not math.isfinite(last) or first > last:
                    continue
            except (KeyError, TypeError, ValueError, OverflowError):
                continue
            grouped.setdefault(key, []).append(
                (str(association.get("sid") or identity[0]), first, last))
    found: dict[tuple[int, int], list[tuple[str, float, float]]] = {}
    for identity, transitions in grouped.items():
        ordered = sorted(transitions, key=lambda item: (item[1], item[0], item[2]))
        entries = []
        for index, (sid, first, _observed_last) in enumerate(ordered):
            # A process identity remains associated with its latest session
            # while alive; a later session on the same process closes the
            # earlier interval at that new session's first hook event.
            next_first = next((entry[1] for entry in ordered[index + 1:]
                               if entry[1] > first), math.inf)
            entries.append((sid, first, next_first if math.isfinite(next_first) else math.inf))
        # Hooks can arrive out of order. When two sessions' observed ranges on
        # one process overlap, the owner after the overlap can't be proven:
        # make both candidates from that point on so selection fails closed.
        for index, (sid, first, observed_last) in enumerate(ordered):
            for other_sid, other_first, other_last in ordered[index + 1:]:
                if other_sid != sid and first < other_last and other_first < observed_last:
                    start = max(first, other_first)
                    entries.append((sid, start, math.inf))
                    entries.append((other_sid, start, math.inf))
        found[identity] = entries
    return found


def scan_codex_processes(*, table: dict[int, tuple[int, str]] | None = None,
                         current_sid: bytes | None = None,
                         equal_sid: Callable[[bytes | None, bytes | None], bool] = _equal_sid,
                         process_sid: Callable[[int], bytes | None] = _process_sid,
                         created: Callable[[int], int | None] = _process_created,
                         command_line: Callable[[int], str | None] = _command_line,
                         watched_threads: set[str] | None = None,
                         claude_states: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """Discover executions by same-user process ancestry; any uncertain edge drops the row."""
    try:
        table = _process_table() if table is None else table
        current_sid = _current_user_sid() if current_sid is None else current_sid
    except Exception:
        return []
    if not current_sid:
        return []
    associations = _association_map(claude_states or {})
    created_cache: dict[int, int | None] = {}
    sid_cache: dict[int, bytes | None] = {}

    def identity(pid: int) -> int | None:
        if pid not in created_cache:
            created_cache[pid] = created(pid)
        return created_cache[pid]

    def owner(pid: int) -> bytes | None:
        if pid not in sid_cache:
            sid_cache[pid] = process_sid(pid)
        return sid_cache[pid]

    output = []
    for child, (_parent, exe) in table.items():
        if exe.casefold() != "codex.exe":
            continue
        try:
            child_created = created(child)
            if child_created is None:
                continue
            created_cache[child] = child_created
            if not equal_sid(owner(child), current_sid):
                continue
            invocation = parse_exec_invocation(command_line(child))
            if invocation is None:
                continue
            kind, resume_id, argv_thread_ids = invocation
            if kind == "resume":
                thread_id = resume_id
            else:
                matches = set(argv_thread_ids) & {item.lower() for item in watched_threads or ()}
                thread_id = next(iter(matches)) if len(matches) == 1 else None
            if created(child) != child_created:
                continue
        except Exception:
            continue
        if not thread_id or child_created is None:
            continue
        watched = {item.lower() for item in watched_threads or ()}
        if watched and thread_id not in watched:
            continue
        current, visited, chain = child, set(), []
        claude_identity = None
        valid = True
        for _ in range(16):
            if current in visited or current not in table:
                valid = False
                break
            visited.add(current)
            parent, name = table[current]
            current_created = identity(current)
            if current_created is None:
                valid = False
                break
            chain.append((current, current_created))
            if current != child and name.casefold() in {"claude.exe", "claude"}:
                claude_identity = (current, current_created)
                break
            if parent not in table:
                valid = False
                break
            parent_created = identity(parent)
            if parent_created is None or parent_created >= current_created:
                valid = False
                break
            current = parent
        session_ids: set[str] = set()
        if valid and claude_identity is not None:
            created_epoch = _filetime_epoch(child_created)
            if created_epoch is not None:
                session_ids = {sid for sid, first, last in associations.get(claude_identity, ())
                               if first <= created_epoch < last}
        # Recheck the child across owner and command-line observations, then
        # revalidate every ancestor identity immediately before publication.
        try:
            if identity(child) != child_created or created(child) != child_created:
                continue
            if any(created(pid) != created_at for pid, created_at in chain):
                continue
        except Exception:
            continue
        linked = len(session_ids) == 1
        if not linked and thread_id not in watched:
            continue
        output.append({"thread_id": thread_id, "codex_pid": child,
                       "codex_created": child_created,
                       "claude_session_id": next(iter(session_ids)) if linked else None,
                       "method": "cmdline" if linked else "unlinked"})
    return output


def receipt_path(root: Path | None = None) -> Path:
    if root is None:
        from .product import hook_data_root
        root = hook_data_root()
    return Path(root) / INBOX_NAME


def _cross_provider_collection_allowed(policies: Any = None) -> bool:
    try:
        from .provider_policy import policy_for
        return all(policy_for(name, policies).allows("collect") for name in ("Claude", "Codex"))
    except Exception:
        return False


def validate_codex_pid(pid: int, thread_id: str, *,
                       table_reader: Callable[[], dict[int, tuple[int, str]]] = _process_table,
                       current_sid: Callable[[], bytes | None] = _current_user_sid,
                       process_sid: Callable[[int], bytes | None] = _process_sid,
                       equal_sid: Callable[[bytes | None, bytes | None], bool] = _equal_sid,
                       created: Callable[[int], int | None] = _process_created,
                       command_line: Callable[[int], str | None] = _command_line) -> int | None:
    """Validate same-user codex.exe identity around owner and argv reads."""
    try:
        table = table_reader()
        if table.get(pid, (None, ""))[1].casefold() != "codex.exe":
            return None
        before = created(pid)
        sid = current_sid()
        if before is None or not equal_sid(process_sid(pid), sid):
            return None
        invocation = parse_exec_invocation(command_line(pid))
        if invocation is None:
            return None
        if invocation[0] == "resume" and invocation[1] != thread_id.lower():
            return None
        return before if created(pid) == before else None
    except Exception:
        return None


def validate_receipt_process(receipt: dict[str, Any], *, table: dict[int, tuple[int, str]],
                             current_sid: bytes | None,
                             process_sid: Callable[[int], bytes | None] = _process_sid,
                             equal_sid: Callable[[bytes | None, bytes | None], bool] = _equal_sid,
                             created: Callable[[int], int | None] = _process_created,
                             command_line: Callable[[int], str | None] = _command_line) -> bool:
    try:
        pid = int(receipt["codex_pid"])
        expected = int(receipt["codex_created"])
        thread = str(receipt["thread_id"]).lower()
        if table.get(pid, (None, ""))[1].casefold() != "codex.exe":
            return False
        if created(pid) != expected or not equal_sid(process_sid(pid), current_sid):
            return False
        invocation = parse_exec_invocation(command_line(pid))
        if invocation is None:
            return False
        if invocation[0] == "resume" and invocation[1] != thread:
            return False
        return created(pid) == expected
    except Exception:
        return False


def write_receipt(thread_id: str, codex_pid: int, *, environ: dict[str, str] | None = None,
                  created: Callable[[int], int | None] = _process_created,
                  root: Path | None = None, policies: Any = None,
                  permission_check: Callable[[Any], bool] = _cross_provider_collection_allowed,
                  pid_check: Callable[[int, str], int | bool | None] = validate_codex_pid) -> bool:
    """Append a bounded display-only association receipt for this execution."""
    try:
        if not permission_check(policies):
            return False
    except Exception:
        return False
    env = os.environ if environ is None else environ
    sid = env.get("CLAUDE_CODE_SESSION_ID")
    if not THREAD_ID.fullmatch(str(thread_id)) or not isinstance(sid, str) or not THREAD_ID.fullmatch(sid):
        return False
    if not isinstance(codex_pid, int) or codex_pid <= 0:
        return False
    try:
        validated = pid_check(codex_pid, thread_id.lower())
    except Exception:
        return False
    if not validated:
        return False
    created_at = (validated if isinstance(validated, int) and not isinstance(validated, bool)
                  else created(codex_pid))
    if created_at is None:
        return False
    try:
        path = receipt_path(root)
        path.mkdir(parents=True, exist_ok=True)
        path = path / RECEIPT_FILE
        record = {"thread_id": thread_id.lower(), "codex_pid": codex_pid,
                  "codex_created": created_at, "claude_session_id": sid,
                  "recorded_at": time.time(), "method": "receipt"}
        encoded = (json.dumps(record, separators=(",", ":")) + "\n").encode("utf-8")
        try:
            existing_bytes = path.stat().st_size
        except FileNotFoundError:
            existing_bytes = 0
        if len(encoded) > MAX_RECEIPT_BYTES or existing_bytes + len(encoded) > MAX_INBOX_BYTES:
            return False
        with path.open("ab") as handle:
            handle.write(encoded)
        return True
    except Exception:
        return False


def read_receipts(root: Path | None = None, *, policies: Any = None,
                  permission_check: Callable[[Any], bool] = _cross_provider_collection_allowed,
                  owner_check: Callable[[], bool] | None = None) -> list[dict[str, Any]]:
    try:
        if not permission_check(policies):
            return []
        if owner_check is None:
            owner_check = _instance_mutex_held
        if not owner_check():
            return []
    except Exception:
        return []
    try:
        path = receipt_path(root)
        path = path / RECEIPT_FILE
        if path.stat().st_size > MAX_INBOX_BYTES:
            return []
        out = []
        for line in path.read_bytes().splitlines():
            if len(line) > MAX_RECEIPT_BYTES:
                continue
            try:
                item = json.loads(line)
            except (ValueError, UnicodeDecodeError):
                continue
            if (isinstance(item, dict) and THREAD_ID.fullmatch(str(item.get("thread_id") or ""))
                    and THREAD_ID.fullmatch(str(item.get("claude_session_id") or ""))
                    and item.get("method") == "receipt"):
                try:
                    if int(item["codex_pid"]) > 0 and int(item["codex_created"]) > 0 \
                            and 0 < float(item["recorded_at"]) <= time.time() + 60:
                        out.append(item)
                except (KeyError, TypeError, ValueError, OverflowError):
                    continue
        return out
    except Exception:
        return []


def write_registry(runs: Iterable[dict[str, Any]], path: Path, *, now: float | None = None) -> bool:
    """Validate, retain and atomically replace the app-owned execution registry."""
    now = time.time() if now is None else now
    clean = []
    for run in runs:
        try:
            if (not THREAD_ID.fullmatch(str(run["thread_id"]))
                    or not THREAD_ID.fullmatch(str(run["claude_session_id"]))
                    or int(run["codex_pid"]) <= 0 or int(run["codex_created"]) <= 0
                    or run.get("method") not in {"cmdline", "receipt"}):
                continue
            first, last = float(run["first_seen"]), float(run["last_seen"])
            verified = float(run.get("verified_at", 0))
            retired = float(run.get("retired_at", 0))
            if (not all(math.isfinite(value) for value in (first, last, verified, retired))
                    or first > last or last > now or verified > now or retired > now
                    or now - last > RETENTION_SECONDS):
                continue
            clean.append({key: run[key] for key in (
                "thread_id", "codex_pid", "codex_created", "claude_session_id",
                "first_seen", "last_seen", "verified_at", "retired_at", "method") if key in run})
        except (KeyError, TypeError, ValueError, OverflowError):
            continue
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        data = json.dumps({"version": 1, "runs": clean}, separators=(",", ":")).encode("utf-8")
        fd, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(name, path)
        finally:
            try:
                os.unlink(name)
            except OSError:
                pass
        return True
    except Exception:
        return False


def load_registry(path: Path, *, now: float | None = None) -> list[dict[str, Any]]:
    now = time.time() if now is None else now
    try:
        if path.stat().st_size > 4 * 1024 * 1024:
            return []
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict) or value.get("version") != 1 or not isinstance(value.get("runs"), list):
            return []
        valid = []
        for run in value["runs"]:
            if not isinstance(run, dict):
                continue
            # Reuse the writer's strict per-record validation without replacing
            # the on-disk file during load.
            try:
                if (THREAD_ID.fullmatch(str(run["thread_id"]))
                        and THREAD_ID.fullmatch(str(run["claude_session_id"]))
                        and int(run["codex_pid"]) > 0 and int(run["codex_created"]) > 0
                        and run.get("method") in {"cmdline", "receipt"}):
                    first, last = float(run["first_seen"]), float(run["last_seen"])
                    verified = float(run.get("verified_at", 0))
                    retired = float(run.get("retired_at", 0))
                    if (not all(math.isfinite(value) for value in (first, last, verified, retired))
                            or first > last or last > now or first > now or verified > now
                            or retired > now
                            or now - last > RETENTION_SECONDS):
                        continue
                    valid.append(run)
            except (KeyError, TypeError, ValueError, OverflowError):
                continue
        return valid
    except Exception:
        return []


def _instance_mutex_held() -> bool:
    try:
        from . import widget
        return getattr(widget, "_INSTANCE_HANDLE", None) is not None
    except Exception:
        return False


@_serialized_link_poll
def apply_exact_links(
    providers: list[dict[str, Any]], *, root: Path | None = None,
    claude_states: dict[str, Any] | None = None, now: float | None = None,
    registry_path: Path | None = None,
    policies: Any = None,
    permission_check: Callable[[Any], bool] = _cross_provider_collection_allowed,
    process_table_reader: Callable[[], dict[int, tuple[int, str]]] = _process_table,
    current_sid_reader: Callable[[], bytes | None] = _current_user_sid,
    process_sid: Callable[[int], bytes | None] = _process_sid,
    equal_sid: Callable[[bytes | None, bytes | None], bool] = _equal_sid,
    process_created_reader: Callable[[int], int | None] = _process_created,
    command_line: Callable[[int], str | None] = _command_line,
    scan: Callable[..., list[dict[str, Any]]] = scan_codex_processes,
    verify: Callable[[int, int], bool | None] = verify_process,
    hooks: Callable[[], dict[str, Any]] | None = None,
    receipts_reader: Callable[[Path | None], list[dict[str, Any]]] = read_receipts,
    registry_reader: Callable[[Path], list[dict[str, Any]]] = load_registry,
    registry_writer: Callable[..., bool] = write_registry,
) -> None:
    """Link exact executions, scanning only for new or unverifiable live runs."""
    claude = next((item for item in providers if item.get("name") == "Claude"), None)
    codex = next((item for item in providers if item.get("name") == "Codex"), None)
    if not claude or not codex:
        return
    try:
        if not permission_check(policies):
            return
    except Exception:
        return
    now = time.time() if now is None else now
    registry_changed = False
    if registry_path is None:
        try:
            from .product import state_root
            registry_path = state_root() / REGISTRY_NAME
        except Exception:
            return
    runs = registry_reader(registry_path)
    claude_ids = {str(row.get("id")) for row in claude.get("chats") or [] if row.get("id")}

    def cross_provider_candidate(row: dict[str, Any]) -> bool:
        # A cached Claude parent can arrive without its provider tag; it is
        # still a cross-provider link to revalidate, not native Codex lineage.
        parent = row.get("parentId")
        return (not parent or row.get("parentProvider") == "Claude"
                or (not row.get("parentProvider") and str(parent) in claude_ids))

    live_rows = [row for row in codex.get("chats") or []
                 if cross_provider_candidate(row)
                 and row.get("source") == "exec"
                 and row.get("executionStatus") == "running"]
    global _LAST_FULL_SCAN_AT
    with _SCAN_LOCK:
        discovery_due = now - _LAST_FULL_SCAN_AT >= 10
    need_full_scan = bool(live_rows) and discovery_due
    verified_by_thread: dict[str, list[dict[str, Any]]] = {}
    unresolved_by_thread: dict[str, list[dict[str, Any]]] = {}
    watched_threads = {str(row.get("id") or "").lower() for row in live_rows
                       if row.get("id")}
    unlinked_live_threads: set[str] = set()
    with _UNLINKED_LOCK:
        remembered_unlinked = [
            (identity, dict(run)) for identity, run in _UNLINKED_EXECUTIONS.items()
            if identity[0] in watched_threads]
    for identity, run in remembered_unlinked:
        verdict = verify(identity[1], identity[2])
        if verdict is False:
            with _UNLINKED_LOCK:
                _UNLINKED_EXECUTIONS.pop(identity, None)
        else:
            unlinked_live_threads.add(identity[0])
    for row in live_rows:
        thread = str(row.get("id") or "").lower()
        candidates = [run for run in runs if str(run.get("thread_id") or "").lower() == thread]
        candidates.sort(key=lambda run: float(run.get("last_seen") or 0), reverse=True)
        for known in candidates:
            try:
                if float(known.get("retired_at") or 0) > 0:
                    continue
                pid, created_at = int(known["codex_pid"]), int(known["codex_created"])
                verified_at = float(known.get("verified_at") or 0)
            except (KeyError, TypeError, ValueError, OverflowError):
                continue
            fresh = now - verified_at < 10
            verdict = True if fresh else verify(pid, created_at)
            if verdict is True:
                if not fresh:
                    known["verified_at"] = now
                    known["last_seen"] = now
                    registry_changed = True
                verified_by_thread.setdefault(thread, []).append(known)
            elif verdict is None:
                # An unresolved identity can only block a parent; it is never
                # positive evidence for publishing one.
                unresolved_by_thread.setdefault(thread, []).append(known)
            else:
                known["retired_at"] = now
                registry_changed = True

    discovered: list[dict[str, Any]] = []
    receipt_rows: list[dict[str, Any]] = []
    scan_table: dict[int, tuple[int, str]] = {}
    if need_full_scan:
        try:
            scan_table = process_table_reader()
        except Exception:
            scan_table = {}
        if claude_states is None:
            if hooks is None:
                from . import claude_events
                hooks = claude_events.load_states
            try:
                claude_states = hooks()
            except Exception:
                claude_states = {}
        try:
            discovered = scan(claude_states=claude_states, table=scan_table,
                              watched_threads=watched_threads)
        except Exception:
            discovered = []
        with _SCAN_LOCK:
            _LAST_FULL_SCAN_AT = now
        if _instance_mutex_held():
            try:
                current_sid = current_sid_reader()
                receipt_rows = [item for item in receipts_reader(root)
                                if validate_receipt_process(
                                    item, table=scan_table, current_sid=current_sid,
                                    process_sid=process_sid, equal_sid=equal_sid,
                                    created=process_created_reader, command_line=command_line)]
            except Exception:
                receipt_rows = []
    by_identity = {(str(run["thread_id"]).lower(), int(run["codex_pid"]),
                    int(run["codex_created"]), str(run["claude_session_id"])): run
                   for run in runs if all(key in run for key in (
                       "thread_id", "codex_pid", "codex_created", "claude_session_id"))}
    discovered_threads = {(str(run.get("thread_id") or "").lower(),
                           int(run.get("codex_pid", -1)), int(run.get("codex_created", -1)))
                          for run in discovered if run.get("claude_session_id")}
    for run in discovered:
        if run.get("method") != "unlinked" or run.get("claude_session_id") is not None:
            continue
        try:
            identity = (str(run["thread_id"]).lower(), int(run["codex_pid"]), int(run["codex_created"]))
        except (KeyError, TypeError, ValueError, OverflowError):
            continue
        with _UNLINKED_LOCK:
            _UNLINKED_EXECUTIONS[identity] = {**run, "verified_at": now}
        unlinked_live_threads.add(identity[0])
    for run in discovered:
        if run.get("claude_session_id") is not None:
            try:
                identity = (str(run["thread_id"]).lower(), int(run["codex_pid"]),
                            int(run["codex_created"]))
                with _UNLINKED_LOCK:
                    _UNLINKED_EXECUTIONS.pop(identity, None)
            except (KeyError, TypeError, ValueError, OverflowError):
                pass
    receipt_processes: dict[tuple[int, int], set[str]] = {}
    for run in discovered:
        receipt_processes.setdefault((int(run.get("codex_pid", -1)),
                                      int(run.get("codex_created", -1))), set()).add(
                                          str(run.get("thread_id") or "").lower())
    for run in [*discovered, *receipt_rows]:
        try:
            if run.get("method") == "receipt":
                identity = (str(run["thread_id"]).lower(), int(run["codex_pid"]), int(run["codex_created"]))
                competing = receipt_processes.get((identity[1], identity[2]), set())
                if identity not in discovered_threads and any(thread != identity[0] for thread in competing):
                    continue
                if verify(identity[1], identity[2]) is not True:
                    continue
                with _UNLINKED_LOCK:
                    _UNLINKED_EXECUTIONS.pop(identity, None)
                    still_unlinked = any(key[0] == identity[0] for key in _UNLINKED_EXECUTIONS)
                if not still_unlinked:
                    unlinked_live_threads.discard(identity[0])
            sid = str(run["claude_session_id"])
            key = (str(run["thread_id"]).lower(), int(run["codex_pid"]),
                   int(run["codex_created"]), sid)
            observed = float(run.get("recorded_at", now)) if run.get("method") == "receipt" else now
            prior = by_identity.get(key)
            record = {**run, "first_seen": min(float(prior["first_seen"]), observed) if prior else observed,
                      "last_seen": max(float(prior["last_seen"]), observed) if prior else observed,
                      "verified_at": now if run.get("method") == "cmdline" else
                      float(prior.get("verified_at") or 0) if prior else 0}
            by_identity[key] = record
            if run.get("method") == "cmdline":
                verified_by_thread.setdefault(key[0], []).append(record)
            elif run.get("method") == "receipt":
                record["verified_at"] = now
                verified_by_thread.setdefault(key[0], []).append(record)
        except (KeyError, TypeError, ValueError, OverflowError):
            continue
    runs = list(by_identity.values())
    if (need_full_scan or registry_changed) and _instance_mutex_held():
        registry_writer(runs, registry_path, now=now)

    by_thread: dict[str, list[dict[str, Any]]] = {}
    for run in runs:
        try:
            thread = str(run["thread_id"]).lower()
            by_thread.setdefault(thread, []).append(run)
        except (KeyError, TypeError, ValueError, OverflowError):
            continue
    for row in codex.get("chats") or []:
        thread = str(row.get("id") or "").lower()
        if not cross_provider_candidate(row):
            continue
        # Existing Claude links must be revalidated from the registry/discovery
        # before they are republished in this poll.
        if row.get("parentId"):
            row.pop("parentId", None)
            row.pop("parentProvider", None)
            row.pop("parentLinkMethod", None)
        historical = by_thread.get(thread, [])
        if not historical:
            continue
        status = str(row.get("executionStatus") or row.get("status") or "").lower()
        if status == "running":
            if thread in unlinked_live_threads or unresolved_by_thread.get(thread):
                continue
            live_runs = verified_by_thread.get(thread, [])
            live_sessions = {str(run.get("claude_session_id")) for run in live_runs}
            if len(live_sessions) != 1:
                continue
            sid = next(iter(live_sessions))
            selected = max((run for run in live_runs if str(run.get("claude_session_id")) == sid),
                           key=lambda run: float(run.get("last_seen") or 0))
            if sid in claude_ids:
                row["parentId"], row["parentProvider"] = sid, "Claude"
                row["parentLinkMethod"] = selected.get("method")
            continue
        selected = max(historical, key=lambda run: float(run.get("last_seen") or 0))
        sid = str(selected.get("claude_session_id") or "")
        if sid not in claude_ids:
            continue
        turn_start = row.get("runStartedAt") or row.get("activeRunStartedAt")
        try:
            from datetime import datetime
            turn_epoch = datetime.fromisoformat(str(turn_start).replace("Z", "+00:00")).timestamp() if turn_start else None
        except (TypeError, ValueError, OverflowError, OSError):
            turn_epoch = None
        if turn_epoch is not None and turn_epoch > float(selected["last_seen"]) or sid not in claude_ids:
            continue
        row["parentId"], row["parentProvider"] = sid, "Claude"
        row["parentLinkMethod"] = selected.get("method")
