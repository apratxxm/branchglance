"""Exact, fail-closed links between Claude sessions and Codex executions.

This module deliberately exposes injectable OS functions so process identity,
ancestry and receipt rules can be tested without inspecting real processes.
"""
from __future__ import annotations

import ctypes
import copy
from functools import wraps
import json
import logging
import os
import sys
import re
import shlex
import stat
import tempfile
import threading
import time
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

THREAD_ID = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\Z")
MAX_RECEIPT_BYTES = 4096
MAX_INBOX_BYTES = 1024 * 1024
REGISTRY_NAME = "agent-runs.json"
INBOX_NAME = "codex-link-receipts"
RECEIPT_FILE = "receipts.jsonl"
KF_FLAG_NO_PACKAGE_REDIRECTION = 0x00010000
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
_RUN_ROWS: dict[tuple[str, int, int], dict[str, Any]] = {}
_CLAUDE_PROCESS_LINKS: dict[str, dict[str, Any]] = {}
_CLAUDE_LINK_CONTRADICTIONS: set[tuple[str, int, int]] = set()
_RECEIPT_CONFLICTS: set[tuple[str, int, int]] = set()
_CODEX_LINK_CONTRADICTIONS: set[tuple[str, int, int]] = set()
# Execution identities seen per thread in this app process, for the resume
# badge. Never pruned by run retention; bounded by thread count.
_OBSERVED_IDENTITIES: dict[str, set[tuple[int, int]]] = {}
_OBSERVED_IDENTITY_LIMIT = 4096
STOPPED_RETENTION_SECONDS = 15 * 60
_LOG = logging.getLogger(__name__)

# Codex launcher attribution remains disabled until a rig probe verifies that
# root and native-descendant processes export their own thread ids.
_CODEX_ENV_PER_THREAD_VERIFIED = False


# The Codex executable is codex.exe on Windows and codex on macOS.
CODEX_EXE_NAMES = frozenset({"codex.exe", "codex"})


def _optional_thread_id(value: Any) -> str | None:
    """Validate an optional provider thread UUID without coercing other values."""
    return value if isinstance(value, str) and THREAD_ID.fullmatch(value) else None


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
    if sys.platform == "darwin":
        return f"uid:{os.getuid()}".encode()
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
    if sys.platform == "darwin":
        from .macos_process import process_uid
        uid = process_uid(pid)
        return f"uid:{uid}".encode() if uid is not None else None
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
    if sys.platform == "darwin":
        from .macos_process import command_line
        return command_line(pid)
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


def _command_argv(pid: int) -> list[str] | None:
    """Read argv as tokens; never infer token boundaries from joined text."""
    if sys.platform == "darwin":
        from .macos_process import command_line_argv
        return command_line_argv(pid)
    if os.name != "nt":
        return None
    line = _command_line(pid)
    if not line:
        return None
    try:
        shell = ctypes.WinDLL("shell32", use_last_error=True)
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        shell.CommandLineToArgvW.argtypes = [ctypes.c_wchar_p, ctypes.POINTER(ctypes.c_int)]
        shell.CommandLineToArgvW.restype = ctypes.POINTER(ctypes.c_wchar_p)
        kernel.LocalFree.argtypes = [ctypes.c_void_p]
        count = ctypes.c_int()
        values = shell.CommandLineToArgvW(line, ctypes.byref(count))
        if not values or count.value <= 0 or count.value > 32768:
            return None
        try:
            return [values[index] for index in range(count.value)]
        finally:
            kernel.LocalFree(values)
    except Exception:
        return None


def parse_exec_argv(argv: list[str] | tuple[str, ...] | None) -> tuple[str, str | None, tuple[str, ...]] | None:
    """Parse Codex exec identity from a structured argv vector."""
    if not isinstance(argv, (list, tuple)) or not argv or len(argv) > 32768:
        return None
    if any(not isinstance(arg, str) for arg in argv):
        return None
    args = list(argv)
    base = args[0].replace("\\", "/").rsplit("/", 1)[-1].casefold()
    if base not in CODEX_EXE_NAMES and not base.startswith("codex"):
        # Node-hosted @openai/codex commands carry the package entry in argv.
        entry = next((index for index, arg in enumerate(args)
                      if "@openai/codex" in arg.replace("\\", "/").casefold()), None)
        if entry is None:
            return None
        args = args[entry + 1:]
    else:
        args = args[1:]
    if not args or args[0].casefold() != "exec":
        return None
    if len(args) > 1 and args[1].casefold() == "resume":
        if len(args) < 3:
            return None
        candidate = args[2].casefold()
        # Some macOS process snapshots expose "resume <uuid>" as one argv
        # element; accept that exact shape without splitting arbitrary text.
        if candidate.startswith("resume "):
            candidate = candidate[7:].strip()
        return ("resume", candidate, (candidate,)) if THREAD_ID.fullmatch(candidate) else None
    if len(args) > 1 and args[1].casefold().startswith("resume "):
        candidate = args[1][7:].strip().casefold()
        return ("resume", candidate, (candidate,)) if THREAD_ID.fullmatch(candidate) else None
    return ("exec", None, ())


def classify_agent_process(name: str, argv: list[str] | tuple[str, ...] | None) -> str:
    """Return claude, codex, other, or unknown for a process-table entry."""
    if not isinstance(name, str):
        return "unknown"
    image = name.casefold()
    if image in {"claude", "claude.exe"}:
        return "claude"
    if image in CODEX_EXE_NAMES:
        return "codex"
    if not image.endswith(("node", "node.exe", "bun", "bun.exe")):
        return "other"
    if argv is None:
        return "unknown"
    normalized = [item.replace("\\", "/").casefold() for item in argv if isinstance(item, str)]
    if len(normalized) != len(argv):
        return "unknown"
    if any("@anthropic-ai/claude-code" in item or item.endswith("/claude") for item in normalized):
        return "claude"
    if any("@openai/codex" in item for item in normalized):
        return "codex"
    return "other"


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
                if association.get("ambiguous") is True:
                    continue
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
                         table_reader: Callable[[], dict[int, tuple[int, str]]] | None = None,
                         snapshot_time: int | None = None,
                         current_sid: bytes | None = None,
                         equal_sid: Callable[[bytes | None, bytes | None], bool] = _equal_sid,
                         process_sid: Callable[[int], bytes | None] = _process_sid,
                         created: Callable[[int], int | None] = _process_created,
                         command_line: Callable[[int], str | None] = _command_line,
                         command_argv: Callable[[int], list[str] | None] | None = None,
                         watched_threads: set[str] | None = None,
                         claude_states: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """Discover executions by same-user process ancestry; any uncertain edge drops the row."""
    try:
        table = (table_reader or _process_table)() if table is None else table
        snapshot_time = snapshot_time or int((time.time() + 11_644_473_600) * 10_000_000)
        current_sid = _current_user_sid() if current_sid is None else current_sid
    except Exception:
        return []
    if not current_sid:
        return []
    associations = _association_map(claude_states or {})
    argv_reader = command_argv or _command_argv
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
        if exe.casefold() not in CODEX_EXE_NAMES:
            continue
        try:
            child_created = created(child)
            if child_created is None:
                continue
            created_cache[child] = child_created
            if not equal_sid(owner(child), current_sid):
                continue
            if command_argv is not None:
                invocation = parse_exec_argv(command_argv(child))
            elif command_line is _command_line:
                invocation = parse_exec_argv(_command_argv(child))
            else:
                # Compatibility for older injected test probes. Runtime paths
                # always use the structured argv reader above.
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
            if current_created is None or current_created >= snapshot_time or not equal_sid(owner(current), current_sid):
                valid = False
                break
            chain.append((current, current_created))
            kind = classify_agent_process(name, argv_reader(current) if
                                          name.casefold().endswith(("node", "node.exe", "bun", "bun.exe")) else None)
            if kind == "unknown":
                valid = False
                break
            if current != child and kind in {"claude", "codex"}:
                if kind == "claude":
                    claude_identity = (current, current_created)
                # Never skip past a nearer Codex launcher.
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
            launcher_index = next((index for index, (pid, _tick) in enumerate(chain)
                                   if pid == claude_identity[0]), None)
            launch_child_created = (chain[launcher_index - 1][1]
                                    if launcher_index is not None and launcher_index > 0
                                    else child_created)
            direct_epoch = _filetime_epoch(launch_child_created)
            if created_epoch is not None and direct_epoch is not None:
                session_ids = {sid for sid, first, last in associations.get(claude_identity, ())
                               if first <= direct_epoch < last and first <= created_epoch < last}
        # Incarnation binding: the whole observed chain must remain identical
        # across two process snapshots and every creation tick must be stable.
        try:
            snapshot2 = (table_reader() if table_reader is not None else table)
            if any(pid not in snapshot2 or pid not in table or snapshot2[pid] != table[pid]
                   for pid, _created_at in chain):
                continue
            if identity(child) != child_created or created(child) != child_created:
                continue
            if any(created(pid) != created_at or created_at >= snapshot_time for pid, created_at in chain):
                continue
        except Exception:
            continue
        linked = len(session_ids) == 1
        if not linked and thread_id not in watched:
            continue
        output.append({"thread_id": thread_id, "codex_pid": child,
                       "codex_created": child_created,
                       "claude_pid": claude_identity[0] if linked else None,
                       "claude_created": claude_identity[1] if linked else None,
                       "claude_session_id": next(iter(session_ids)) if linked else None,
                       "method": "cmdline" if linked else "unlinked"})
    return output


def scan_claude_process_links(*, claude_states: dict[str, Any],
                              table: dict[int, tuple[int, str]],
                              codex_threads: list[dict[str, Any]] | None = None,
                              table_reader: Callable[[], dict[int, tuple[int, str]]] | None = None,
                              snapshot_time: int | None = None,
                              current_sid: bytes | None = None,
                              equal_sid: Callable[[bytes | None, bytes | None], bool] = _equal_sid,
                              process_sid: Callable[[int], bytes | None] = _process_sid,
                              created: Callable[[int], int | None] = _process_created,
                              command_argv: Callable[[int], list[str] | None] = _command_argv
                              ) -> list[dict[str, Any]]:
    """Find Claude child processes launched inside uniquely owned Claude intervals."""
    if not claude_states or not table or not current_sid:
        return []
    snapshot_time = snapshot_time or int((time.time() + 11_644_473_600) * 10_000_000)
    associations = _association_map(claude_states)
    codex_threads = codex_threads or []
    candidates: dict[tuple[int, int], set[str]] = {}
    for key, state in claude_states.items():
        if not isinstance(key, tuple) or len(key) != 2 or key[1] or not isinstance(state, dict):
            continue
        sid = str(key[0])
        for item in state.get("pid_associations") or ():
            try:
                ident = (int(item["pid"]), int(item["created"]))
            except (KeyError, TypeError, ValueError):
                continue
            candidates.setdefault(ident, set()).add(sid)
    output = []
    for (child, child_created), _candidate_sessions in candidates.items():
        if child not in table or created(child) != child_created:
            continue
        if child_created >= snapshot_time or not equal_sid(process_sid(child), current_sid):
            continue
        child_epoch = _filetime_epoch(child_created)
        if child_epoch is None:
            continue
        session_ids = {sid for sid, first, last in associations.get((child, child_created), ())
                       if first <= child_epoch < last}
        if len(session_ids) != 1:
            continue
        child_sid = next(iter(session_ids))
        child_state = claude_states.get((child_sid, ""), {})
        launcher_signatures = set()
        for capture in child_state.get("launcher_associations") or ():
            try:
                if (int(capture.get("pid", -1)), int(capture.get("created", -1))) == (child, child_created):
                    launcher_signatures.add((capture.get("launcher_claude_session_id"),
                                             capture.get("launcher_codex_thread_id")))
            except (TypeError, ValueError):
                continue
        valid_codex_ids = {value for _claude, value in launcher_signatures if _optional_thread_id(value)}
        valid_claude_ids = {value for value, _codex in launcher_signatures if _optional_thread_id(value)}
        if len(valid_codex_ids) > 1 or len(valid_claude_ids) > 1:
            continue
        if (child_sid, child, child_created) in _CLAUDE_LINK_CONTRADICTIONS:
            continue
        child_kind = classify_agent_process(table[child][1], command_argv(child))
        if child_kind != "claude":
            continue
        direct = table[child][0]
        if direct not in table:
            continue
        direct_created = created(direct)
        if direct_created is None or direct_created >= child_created:
            continue
        current, seen, chain = direct, {child}, []
        parent_identity = None
        parent_kind = None
        valid = True
        for _ in range(32):
            if current in seen or current not in table:
                valid = False
                break
            seen.add(current)
            parent, name = table[current]
            tick = created(current)
            if tick is None or tick >= snapshot_time or not equal_sid(process_sid(current), current_sid):
                valid = False
                break
            chain.append((current, (parent, name), tick))
            argv = command_argv(current) if name.casefold().endswith(("node", "node.exe", "bun", "bun.exe")) else None
            kind = classify_agent_process(name, argv)
            if kind == "unknown":
                valid = False
                break
            if kind in {"claude", "codex"}:
                parent_identity = (current, tick)
                parent_kind = kind
                break
            if parent not in table:
                valid = False
                break
            parent_tick = created(parent)
            if parent_tick is None or parent_tick >= tick:
                valid = False
                break
            current = parent
        if not valid or parent_identity is None:
            continue
        launcher_index = next((index for index, (pid, _edge, _tick) in enumerate(chain)
                               if pid == parent_identity[0]), None)
        launch_child_pid, d_created = ((chain[launcher_index - 1][0], chain[launcher_index - 1][2])
                                       if launcher_index is not None and launcher_index > 0
                                       else (child, child_created))
        c_epoch = _filetime_epoch(child_created)
        d_epoch = _filetime_epoch(d_created) if d_created is not None else None
        if c_epoch is None or d_epoch is None:
            continue
        parent_session_id = None
        parent_thread_id = None
        launcher_codex_thread_id = None
        if parent_kind == "claude":
            owners = {sid for sid, first, last in associations.get(parent_identity, ())
                      if first <= d_epoch < last and first <= c_epoch < last}
            if len(owners) != 1:
                continue
            parent_session_id = next(iter(owners))
        else:
            # v5: no Codex launcher attribution path is reachable until the
            # rig establishes per-thread environment semantics.
            if not _CODEX_ENV_PER_THREAD_VERIFIED:
                continue
            child_state_ids = []
            for state_key, state in claude_states.items():
                if not isinstance(state_key, tuple) or len(state_key) != 2 or state_key[1] or not isinstance(state, dict):
                    continue
                if str(state_key[0]) not in session_ids:
                    continue
                for capture in state.get("launcher_associations") or ():
                    try:
                        if (int(capture.get("pid", -1)), int(capture.get("created", -1))) == (child, child_created):
                            child_state_ids.append(capture.get("launcher_codex_thread_id"))
                    except (TypeError, ValueError):
                        continue
            launch_ids = {value for value in child_state_ids if _optional_thread_id(value)}
            if len(launch_ids) != 1:
                continue
            codex_row_by_id = {str(row.get("id") or row.get("threadId")): row for row in codex_threads}
            host_ids = {str(row.get("id") or row.get("threadId")) for row in codex_threads
                        if int(row.get("runPid", -1) or -1) == parent_identity[0]
                        and str(row.get("runCreated", "")) == str(parent_identity[1])}
            argv = command_argv(parent_identity[0])
            host_invocation = parse_exec_argv(argv)
            if host_invocation and host_invocation[0] == "resume" and host_invocation[1]:
                host_ids.add(host_invocation[1])
            launch_id = next(iter(launch_ids))
            if host_ids and launch_id not in host_ids:
                continue
            if not host_ids and launch_id not in codex_row_by_id:
                continue
            parent_thread_id = launch_id
            launcher_codex_thread_id = launch_id
            # The native-descendant gate is a refusal check only.
            if not _codex_descendants_quiet(launch_id, codex_threads, d_epoch, c_epoch):
                continue
        table2 = table_reader() if table_reader is not None else table
        if (table2.get(child) != table.get(child) or created(child) != child_created
                or any(table2.get(pid) != edge or created(pid) != tick
                       for pid, edge, tick in chain)):
            continue
        output.append({"child_session_id": child_sid,
                       "child_pid": child, "child_created": child_created,
                       "parent_session_id": parent_session_id,
                       "parent_thread_id": parent_thread_id,
                       "parent_provider": "Codex" if parent_kind == "codex" else "Claude",
                       "launcher_codex_thread_id": launcher_codex_thread_id,
                       "parent_pid": parent_identity[0],
                       "parent_created": parent_identity[1],
                       "direct_child_pid": launch_child_pid,
                       "direct_child_created": d_created,
                       "method": "process"})
    return output


def _codex_descendants_quiet(thread_id: str, codex_threads: list[dict[str, Any]],
                            direct_created: float, child_created: float) -> bool:
    """Fail closed when a native descendant has open, unknown, or overlapping activity."""
    rows = {str(row.get("id") or row.get("threadId")): row for row in codex_threads}
    descendants, frontier = set(), {thread_id}
    while frontier:
        ancestor = frontier.pop()
        for row in codex_threads:
            row_id = str(row.get("id") or row.get("threadId") or "")
            parent_id = str(row.get("parentId") or row.get("parentThreadId") or "")
            if parent_id == ancestor and row_id and row_id not in descendants:
                descendants.add(row_id)
                frontier.add(row_id)
    for descendant in descendants:
        row = rows.get(descendant)
        if row is None or row.get("rolloutReadable") is False or row.get("rolloutAvailable") is False:
            return False
        status = str(row.get("executionStatus") or row.get("status") or "").lower()
        if status in {"unknown", "unavailable"} or status in _ACTIVE_STATES:
            return False
        start = _epoch(row.get("activityStartedAt") or row.get("runStartedAt"))
        end = _epoch(row.get("activityEndedAt") or row.get("runEndedAt") or row.get("activityAt"))
        if start is None or end is None or (start <= child_created + 2 and end >= direct_created - 2):
            return False
    return True


def _codex_receipt_parent(receipt: dict[str, Any], *, table: dict[int, tuple[int, str]],
                         table_reader: Callable[[], dict[int, tuple[int, str]]],
                         current_sid: bytes | None,
                         process_sid: Callable[[int], bytes | None],
                         equal_sid: Callable[[bytes | None, bytes | None], bool],
                         created: Callable[[int], int | None],
                         command_argv: Callable[[int], list[str] | None],
                         codex_threads: list[dict[str, Any]], snapshot_time: int
                         ) -> dict[str, Any] | None:
    """Resolve a receipt's Codex launcher only behind the v5 verified-env gate."""
    if not _CODEX_ENV_PER_THREAD_VERIFIED:
        return None
    launcher = _optional_thread_id(receipt.get("launcher_codex_thread_id"))
    try:
        child, child_created = int(receipt["codex_pid"]), int(receipt["codex_created"])
    except (KeyError, TypeError, ValueError):
        return None
    if not launcher or child not in table or created(child) != child_created or child_created >= snapshot_time:
        return None
    current = table[child][0]
    chain = []
    seen = {child}
    descendant_created = child_created
    parent = None
    for _ in range(32):
        if current in seen or current not in table:
            return None
        seen.add(current)
        ppid, name = table[current]
        tick = created(current)
        if (tick is None or tick >= snapshot_time or tick >= descendant_created
                or not equal_sid(process_sid(current), current_sid)):
            return None
        chain.append((current, (ppid, name), tick))
        argv = command_argv(current) if name.casefold().endswith(("node", "node.exe", "bun", "bun.exe")) else None
        kind = classify_agent_process(name, argv)
        if kind == "unknown":
            return None
        if kind in {"claude", "codex"}:
            parent = (current, tick, kind)
            break
        if ppid not in table:
            return None
        descendant_created = tick
        current = ppid
    if parent is None or parent[2] != "codex":
        return None
    rows_by_id = {str(row.get("id") or row.get("threadId")): row for row in codex_threads}
    host_ids = {str(row.get("id") or row.get("threadId")) for row in codex_threads
                if int(row.get("runPid", -1) or -1) == parent[0]
                and str(row.get("runCreated", "")) == str(parent[1])}
    parsed = parse_exec_argv(command_argv(parent[0]))
    if parsed and parsed[0] == "resume" and parsed[1]:
        host_ids.add(parsed[1])
    if host_ids and launcher not in host_ids:
        return None
    if not host_ids and launcher not in rows_by_id:
        return None
    launcher_index = next((index for index, entry in enumerate(chain)
                           if entry[0] == parent[0]), None)
    if launcher_index == 0:
        direct, direct_created = child, child_created
    elif launcher_index is not None:
        direct, direct_created = chain[launcher_index - 1][0], chain[launcher_index - 1][2]
    else:
        return None
    if direct_created is None:
        return None
    child_epoch, direct_epoch = _filetime_epoch(child_created), _filetime_epoch(direct_created)
    if child_epoch is None or direct_epoch is None or not _codex_descendants_quiet(
            launcher, codex_threads, direct_epoch, child_epoch):
        return None
    table2 = table_reader()
    if (any(table2.get(pid) != edge or created(pid) != tick for pid, edge, tick in chain)
            or created(child) != child_created):
        return None
    return {"parent_thread_id": launcher, "parent_pid": parent[0],
            "parent_created": parent[1], "direct_child_pid": direct,
            "direct_child_created": direct_created}


def receipt_path(root: Path | None = None) -> Path:
    if root is None:
        from .product import hook_data_root, is_frozen
        root = _known_local_app_data() / "Branchglance" if is_frozen() else hook_data_root()
    return Path(root) / INBOX_NAME


def _known_local_app_data() -> Path:
    """Resolve LocalAppData independently of inherited environment variables."""
    if os.name != "nt":
        raise OSError("Windows known folder unavailable")
    from ctypes import wintypes
    class GUID(ctypes.Structure):
        _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD),
                    ("Data3", wintypes.WORD), ("Data4", ctypes.c_ubyte * 8)]
    folder = GUID(0xF1B32785, 0x6FBA, 0x4FCF,
                  (ctypes.c_ubyte * 8)(0x9D, 0x55, 0x7B, 0x8E, 0x7F, 0x15, 0x70, 0x91))
    shell = ctypes.WinDLL("shell32", use_last_error=True)
    ole = ctypes.WinDLL("ole32", use_last_error=True)
    shell.SHGetKnownFolderPath.argtypes = [ctypes.POINTER(GUID), wintypes.DWORD, wintypes.HANDLE,
                                           ctypes.POINTER(ctypes.c_void_p)]
    shell.SHGetKnownFolderPath.restype = ctypes.c_long
    ole.CoTaskMemFree.argtypes = [ctypes.c_void_p]
    value = ctypes.c_void_p()
    # Ask for the real folder even from inside a packaged (MSIX) process, so a
    # receipt can never land in a package-private copy the widget doesn't read.
    if shell.SHGetKnownFolderPath(ctypes.byref(folder), KF_FLAG_NO_PACKAGE_REDIRECTION, None,
                                  ctypes.byref(value)) != 0 or not value.value:
        raise OSError("LocalAppData known folder unavailable")
    try:
        return Path(ctypes.wstring_at(value))
    finally:
        ole.CoTaskMemFree(value)


def _plain_chain(path: Path) -> bool:
    """All existing path components must be inspectable and free of links."""
    for part in (path, *path.parents):
        try:
            info = os.lstat(part)
        except OSError:
            return False
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            return False
    return True


def _shadow_exists(local: Path) -> bool:
    packages = local / "Packages"
    try:
        os.lstat(packages)
    except FileNotFoundError:
        return False
    # A shadow in any package can intercept the destination. Errors are unknown, so refuse.
    with os.scandir(packages) as entries:
        for entry in entries:
            shadow = Path(entry.path) / "LocalCache" / "Local" / "Branchglance" / INBOX_NAME
            try:
                os.lstat(shadow)
            except FileNotFoundError:
                continue
            return True
    return False


def _file_identity(fd: int) -> tuple[int, int, int] | None:
    if os.name != "nt":
        info = os.fstat(fd)
        return (info.st_dev, info.st_ino, 0)
    import msvcrt
    from ctypes import wintypes
    class FILETIME(ctypes.Structure):
        _fields_ = [("low", wintypes.DWORD), ("high", wintypes.DWORD)]
    class INFO(ctypes.Structure):
        _fields_ = [("attributes", wintypes.DWORD), ("created", FILETIME),
                    ("accessed", FILETIME), ("written", FILETIME),
                    ("volume", wintypes.DWORD), ("size_high", wintypes.DWORD),
                    ("size_low", wintypes.DWORD), ("links", wintypes.DWORD),
                    ("index_high", wintypes.DWORD), ("index_low", wintypes.DWORD)]
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.GetFileInformationByHandle.argtypes = [wintypes.HANDLE, ctypes.POINTER(INFO)]
    kernel.GetFileInformationByHandle.restype = wintypes.BOOL
    info = INFO()
    if not kernel.GetFileInformationByHandle(msvcrt.get_osfhandle(fd), ctypes.byref(info)):
        return None
    if not (info.index_high or info.index_low):
        return None
    return (info.volume, info.index_high, info.index_low)


def _create_private_inbox(directory: Path, file: Path) -> None:
    if os.name != "nt":
        directory.mkdir(mode=0o700, exist_ok=True)
        fd = os.open(file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600) if not file.exists() else None
        if fd is not None:
            os.close(fd)
        return
    from ctypes import wintypes
    from .product import current_user_sid
    class SECURITY_ATTRIBUTES(ctypes.Structure):
        _fields_ = [("length", wintypes.DWORD), ("descriptor", ctypes.c_void_p),
                    ("inherit", wintypes.BOOL)]
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(wintypes.DWORD)]
    advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW.restype = wintypes.BOOL
    descriptor = ctypes.c_void_p()
    sddl = f"D:P(A;OICI;GA;;;{current_user_sid()})(A;OICI;GA;;;SY)"
    if not advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW(sddl, 1, ctypes.byref(descriptor), None):
        raise ctypes.WinError(ctypes.get_last_error())
    attrs = SECURITY_ATTRIBUTES(ctypes.sizeof(SECURITY_ATTRIBUTES), descriptor, False)
    kernel.CreateDirectoryW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(SECURITY_ATTRIBUTES)]
    kernel.CreateDirectoryW.restype = wintypes.BOOL
    kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                   ctypes.POINTER(SECURITY_ATTRIBUTES), wintypes.DWORD,
                                   wintypes.DWORD, wintypes.HANDLE]
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    advapi.SetFileSecurityW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, ctypes.c_void_p]
    advapi.SetFileSecurityW.restype = wintypes.BOOL
    try:
        if not kernel.CreateDirectoryW(str(directory), ctypes.byref(attrs)) and ctypes.get_last_error() != 183:
            raise ctypes.WinError(ctypes.get_last_error())
        if not _plain_chain(directory) or not directory.is_dir():
            raise OSError("receipt directory is redirected")
        handle = kernel.CreateFileW(str(file), 0x40000000, 7, ctypes.byref(attrs), 1, 0x80, None)
        if handle == ctypes.c_void_p(-1).value:
            if ctypes.get_last_error() != 80:
                raise ctypes.WinError(ctypes.get_last_error())
        else:
            kernel.CloseHandle(handle)
        if not _plain_chain(file) or not file.is_file():
            raise OSError("receipt file is redirected")
        for target in (directory, file):
            if not advapi.SetFileSecurityW(str(target), 0x80000004, descriptor):
                raise ctypes.WinError(ctypes.get_last_error())
    finally:
        kernel.LocalFree(descriptor)


def initialize_receipt_inbox(root: Path | None = None) -> bool:
    """Create the inbox only from the widget's instance-mutex owner."""
    try:
        directory = receipt_path(root)
        if not _plain_chain(directory.parent) or not directory.parent.is_dir():
            return False
        _create_private_inbox(directory, directory / RECEIPT_FILE)
        return _plain_chain(directory / RECEIPT_FILE) and (directory / RECEIPT_FILE).is_file()
    except Exception:
        _LOG.warning("Could not initialize receipt inbox", exc_info=True)
        return False


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
                       command_line: Callable[[int], str | None] = _command_line,
                       command_argv: Callable[[int], list[str] | None] | None = None) -> int | None:
    """Validate same-user codex.exe identity around owner and argv reads."""
    try:
        table = table_reader()
        if table.get(pid, (None, ""))[1].casefold() not in CODEX_EXE_NAMES:
            return None
        before = created(pid)
        sid = current_sid()
        if before is None or not equal_sid(process_sid(pid), sid):
            return None
        invocation = (parse_exec_argv(command_argv(pid)) if command_argv is not None
                      else parse_exec_argv(_command_argv(pid)) if command_line is _command_line
                      else parse_exec_invocation(command_line(pid)))
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
                             command_line: Callable[[int], str | None] = _command_line,
                             command_argv: Callable[[int], list[str] | None] | None = None,
                             claude_states: dict[str, Any] | None = None,
                             table_reader: Callable[[], dict[int, tuple[int, str]]] | None = None,
                             snapshot_time: int | None = None) -> bool:
    try:
        pid = int(receipt["codex_pid"])
        expected = int(receipt["codex_created"])
        thread = str(receipt["thread_id"]).lower()
        launcher_claude = receipt.get("launcher_claude_session_id") or receipt.get("claude_session_id")
        launcher_codex = receipt.get("launcher_codex_thread_id")
        if launcher_claude is not None and _optional_thread_id(launcher_claude) != launcher_claude:
            return False
        if launcher_codex is not None and _optional_thread_id(launcher_codex) != launcher_codex:
            return False
        if (receipt.get("claude_session_id") and receipt.get("launcher_claude_session_id")
                and receipt["claude_session_id"] != receipt["launcher_claude_session_id"]):
            return False
        if table.get(pid, (None, ""))[1].casefold() not in CODEX_EXE_NAMES:
            return False
        if created(pid) != expected or not equal_sid(process_sid(pid), current_sid):
            return False
        invocation = (parse_exec_argv(command_argv(pid)) if command_argv is not None
                      else parse_exec_argv(_command_argv(pid)) if command_line is _command_line
                      else parse_exec_invocation(command_line(pid)))
        if invocation is None:
            return False
        if invocation[0] == "resume" and invocation[1] != thread:
            return False
        if created(pid) != expected:
            return False
        if claude_states is not None and launcher_claude:
            current = table.get(pid, (0, ""))[0]
            seen = {pid}
            nearest = None
            chain = []
            snapshot_time = snapshot_time or int((time.time() + 11_644_473_600) * 10_000_000)
            if expected >= snapshot_time:
                return False
            descendant_created = expected
            while current in table and current not in seen:
                seen.add(current)
                parent, name = table[current]
                tick = created(current)
                if (tick is None or tick >= snapshot_time or tick >= descendant_created
                        or not equal_sid(process_sid(current), current_sid)):
                    return False
                chain.append((current, tick, (parent, name)))
                kind = classify_agent_process(name,
                    (command_argv or _command_argv)(current)
                    if name.casefold().endswith(("node", "node.exe", "bun", "bun.exe")) else None)
                if kind == "unknown":
                    return False
                if kind in {"claude", "codex"}:
                    nearest = (current, tick, kind)
                    break
                if parent not in table:
                    break
                descendant_created = tick
                current = parent
            if nearest is None or nearest[2] != "claude":
                return False
            table2 = table_reader() if table_reader is not None else table
            if (created(pid) != expected
                    or any(table2.get(pid0) != edge or created(pid0) != tick
                           for pid0, tick, edge in chain)):
                return False
            epoch = _filetime_epoch(expected)
            associations = _association_map(claude_states).get((nearest[0], nearest[1]), ())
            launcher_index = next((index for index, entry in enumerate(chain)
                                   if entry[0] == nearest[0]), None)
            launch_child_created = (expected if launcher_index == 0
                                    else chain[launcher_index - 1][2]
                                    if launcher_index is not None else None)
            direct_epoch = (_filetime_epoch(launch_child_created)
                            if launch_child_created is not None else None)
            matches = {sid for sid, first, last in associations
                       if sid == launcher_claude and epoch is not None and direct_epoch is not None
                       and first <= direct_epoch < last and first <= epoch < last}
            if matches != {launcher_claude}:
                return False
        # Codex launcher fields remain diagnostic only until the rig gate opens.
        return True
    except Exception:
        return False


def write_receipt(thread_id: str, codex_pid: int, *, environ: dict[str, str] | None = None,
                  created: Callable[[int], int | None] = _process_created,
                  expected_created: int | None = None,
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
    sid = _optional_thread_id(env.get("CLAUDE_CODE_SESSION_ID"))
    codex_launcher = _optional_thread_id(env.get("CODEX_THREAD_ID"))
    if not THREAD_ID.fullmatch(str(thread_id)):
        return False
    if not isinstance(codex_pid, int) or codex_pid <= 0:
        return False
    if expected_created is not None and (type(expected_created) is not int or expected_created <= 0):
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
    if expected_created is not None and created_at != expected_created:
        return False
    try:
        path = receipt_path(root) / RECEIPT_FILE
        if root is None:
            from .product import is_frozen
            if is_frozen() and _shadow_exists(_known_local_app_data()):
                _LOG.warning("Refusing receipt append: package-local shadow exists")
                return False
        if not _plain_chain(path):
            return False
        record = {"thread_id": thread_id.lower(), "codex_pid": codex_pid,
                  "codex_created": created_at,
                  "recorded_at": time.time(), "method": "receipt"}
        if sid:
            # Preserve the original field for old readers and receipt recovery.
            record["claude_session_id"] = sid
            record["launcher_claude_session_id"] = sid
        if codex_launcher:
            record["launcher_codex_thread_id"] = codex_launcher
        encoded = (json.dumps(record, separators=(",", ":")) + "\n").encode("utf-8")
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | getattr(os, "O_BINARY", 0))
        try:
            other = os.open(path, os.O_RDONLY | getattr(os, "O_BINARY", 0))
            try:
                identity = _file_identity(fd)
                if identity is None or identity != _file_identity(other) or not _plain_chain(path):
                    return False
                info = os.fstat(fd)
                if not stat.S_ISREG(info.st_mode) or len(encoded) > MAX_RECEIPT_BYTES or info.st_size + len(encoded) > MAX_INBOX_BYTES:
                    return False
                if expected_created is not None and created(codex_pid) != expected_created:
                    return False
                return os.write(fd, encoded) == len(encoded)
            finally:
                os.close(other)
        finally:
            os.close(fd)
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
                    and _optional_thread_id(item.get("claude_session_id")) == item.get("claude_session_id")
                    and _optional_thread_id(item.get("launcher_claude_session_id")) == item.get("launcher_claude_session_id")
                    and _optional_thread_id(item.get("launcher_codex_thread_id")) == item.get("launcher_codex_thread_id")
                    and not (item.get("claude_session_id") and item.get("launcher_claude_session_id")
                             and item["claude_session_id"] != item["launcher_claude_session_id"])
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
                    or not _optional_thread_id(run.get("claude_session_id")) == run.get("claude_session_id")
                    or not _optional_thread_id(run.get("launcher_claude_session_id")) == run.get("launcher_claude_session_id")
                    or not _optional_thread_id(run.get("launcher_codex_thread_id")) == run.get("launcher_codex_thread_id")
                    or (run.get("claude_session_id") and run.get("launcher_claude_session_id")
                        and run["claude_session_id"] != run["launcher_claude_session_id"])
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
                "launcher_claude_session_id", "launcher_codex_thread_id",
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
                        and _optional_thread_id(run.get("claude_session_id")) == run.get("claude_session_id")
                        and _optional_thread_id(run.get("launcher_claude_session_id")) == run.get("launcher_claude_session_id")
                        and _optional_thread_id(run.get("launcher_codex_thread_id")) == run.get("launcher_codex_thread_id")
                        and not (run.get("claude_session_id") and run.get("launcher_claude_session_id")
                                 and run["claude_session_id"] != run["launcher_claude_session_id"])
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
        if sys.platform == "darwin":
            from .product import macos_instance_held
            return macos_instance_held()
        from . import widget
        return getattr(widget, "_INSTANCE_HANDLE", None) is not None
    except Exception:
        return False


def _run_identity(run: dict[str, Any]) -> tuple[int, int]:
    return int(run["codex_pid"]), int(run["codex_created"])


_RUN_FIELDS = ("runPid", "runCreated", "runVerified")
_LINEAGE_FIELDS = ("parentId", "parentProvider", "parentLinkMethod", "linkSource",
                   "parentIncarnation", "childRunIdentity")
_STOP_FIELDS = ("retainedRun", "stoppedAt", "runEndedAt", "executionTerminalAt")
_ACTIVE_STATES = frozenset({"running", "waiting", "active", "working", "thinking", "compacting",
                            "needs input", "needs-input", "needs_input"})
# A gap in telemetry is not evidence that the thread stopped.
_UNSETTLED_STATES = frozenset({"unavailable", "unknown"})


def _epoch(value: Any) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value) / (1000 if value > 1e12 else 1)
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp() if value else None
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def _row_outlives_stop(row: dict[str, Any], stopped: dict[str, Any]) -> bool:
    """True when the current row is active, unsettled, or has activity newer
    than the stopped execution: that execution then describes nothing current."""
    states = {str(row.get(field) or "").lower() for field in ("status", "executionStatus")}
    if states & (_ACTIVE_STATES | _UNSETTLED_STATES) or bool(row.get("pendingQuestion")):
        return True
    # previousStatus describes the state before a telemetry gap; a settled row may
    # carry a leftover value, so it only counts while telemetry is unavailable.
    if row.get("telemetryUnavailable") and str(row.get("previousStatus") or "").lower() in _ACTIVE_STATES:
        return True
    stopped_at = _epoch(stopped.get("stoppedAt"))
    activity = [stamp for field in ("activityAt", "runStartedAt", "activeRunStartedAt")
                if (stamp := _epoch(row.get(field))) is not None]
    return stopped_at is not None and bool(activity) and max(activity) > stopped_at


def _execution_order(row: dict[str, Any]) -> tuple[int, int, bool]:
    identity = _row_identity(row)
    return (identity[1], identity[0], bool(row.get("retainedRun"))) if identity else (-1, -1, False)


def _row_identity(row: dict[str, Any]) -> tuple[int, int] | None:
    try:
        return int(row["runPid"]), int(row["runCreated"])
    except (KeyError, TypeError, ValueError):
        return None


def _conflicting_receipt_identities(receipts: Iterable[dict[str, Any]]) -> set[tuple[str, int, int]]:
    grouped: dict[tuple[str, int, int], list[dict[str, Any]]] = {}
    for receipt in receipts:
        if not isinstance(receipt, dict):
            continue
        try:
            key = (str(receipt["thread_id"]).lower(), int(receipt["codex_pid"]),
                   int(receipt["codex_created"]))
        except (KeyError, TypeError, ValueError, OverflowError):
            continue
        grouped.setdefault(key, []).append(receipt)
    conflicts = set()
    for key, entries in grouped.items():
        for index, left in enumerate(entries):
            left_claude = left.get("launcher_claude_session_id") or left.get("claude_session_id")
            left_codex = left.get("launcher_codex_thread_id")
            for right in entries[index + 1:]:
                right_claude = right.get("launcher_claude_session_id") or right.get("claude_session_id")
                right_codex = right.get("launcher_codex_thread_id")
                if ((left_claude and right_claude and left_claude != right_claude)
                        or (left_codex and right_codex and left_codex != right_codex)):
                    conflicts.add(key)
    return conflicts


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
        _RUN_ROWS.clear()
        return
    try:
        if not permission_check(policies):
            _RUN_ROWS.clear()
            return
    except Exception:
        _RUN_ROWS.clear()
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
    codex_ids = {str(row.get("id")) for row in codex.get("chats") or [] if row.get("id")}

    def cross_provider_candidate(row: dict[str, Any]) -> bool:
        # A cached Claude parent can arrive without its provider tag; it is
        # still a cross-provider link to revalidate. Native Codex lineage wins:
        # an untagged parent that names a Codex row stays native.
        parent = row.get("parentId")
        return (not parent or row.get("parentProvider") == "Claude"
                or (not row.get("parentProvider") and str(parent) in claude_ids
                    and str(parent) not in codex_ids))

    # Native/untagged Codex parents are never stripped or re-linked by this poll.
    native_rows = {id(row) for row in codex.get("chats") or []
                   if row.get("parentId") and not cross_provider_candidate(row)}
    for row in codex.get("chats") or []:
        if id(row) in native_rows:
            row.setdefault("linkSource", "native")
    for row in codex.get("chats") or []:
        if row.get("runBindingUnresolved") and row.get("runBindingPreviousExecutionStatus"):
            row["executionStatus"] = row.pop("runBindingPreviousExecutionStatus")
            row.pop("runBindingUnresolved", None)
    live_rows = [row for row in codex.get("chats") or []
                 if row.get("source") == "exec"
                 and row.get("executionStatus") == "running"]
    live_claude_rows = [row for row in claude.get("chats") or []
                        if row.get("id") and str(row.get("status") or "").lower()
                        in {"running", "working", "active", "thinking", "waiting", "needs input"}]
    global _LAST_FULL_SCAN_AT
    with _SCAN_LOCK:
        discovery_due = now - _LAST_FULL_SCAN_AT >= 10
    need_full_scan = bool(live_rows or live_claude_rows) and discovery_due
    verified_by_thread: dict[str, list[dict[str, Any]]] = {}
    unresolved_by_thread: dict[str, list[dict[str, Any]]] = {}
    verdicts: dict[tuple[str, int, int], bool | None] = {}
    watched_threads = {str(row.get("id") or "").lower() for row in live_rows
                       if row.get("id")}
    unlinked_live_threads: set[str] = set()
    unknown_unlinked_threads: set[str] = set()
    verified_unlinked: dict[str, list[dict[str, Any]]] = {}
    unlinked_identities: dict[str, set[tuple[int, int]]] = {}
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
            unlinked_identities.setdefault(identity[0], set()).add((identity[1], identity[2]))
            if verdict is True:
                verified_unlinked.setdefault(identity[0], []).append(run)
            else:
                unknown_unlinked_threads.add(identity[0])
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
            if need_full_scan:
                # A due full scan supersedes per-pid verification this poll.
                continue
            # A cached live result cannot establish liveness on the next poll.
            verdict = verify(pid, created_at)
            verdicts[(thread, pid, created_at)] = verdict
            if verdict is True:
                if now - verified_at >= 10:
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
    codex_receipt_parents: dict[tuple[str, int, int], dict[str, Any]] = {}
    codex_receipt_contradictions: set[tuple[str, int, int]] = set()
    claude_links: list[dict[str, Any]] = []
    conflicting_receipts: set[tuple[str, int, int]] = set()
    scan_table: dict[int, tuple[int, str]] = {}
    scan_snapshot_time = None
    if need_full_scan:
        try:
            scan_table = process_table_reader()
            scan_snapshot_time = int((time.time() + 11_644_473_600) * 10_000_000)
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
                              table_reader=process_table_reader,
                              snapshot_time=scan_snapshot_time,
                              watched_threads=watched_threads)
        except Exception:
            discovered = []
        try:
            claude_links = scan_claude_process_links(
                claude_states=claude_states, table=scan_table,
                codex_threads=codex.get("chats") or [],
                table_reader=process_table_reader, snapshot_time=scan_snapshot_time,
                current_sid=current_sid_reader(),
                equal_sid=equal_sid, process_sid=process_sid,
                created=process_created_reader)
        except Exception:
            claude_links = []
        with _SCAN_LOCK:
            _LAST_FULL_SCAN_AT = now
        if _instance_mutex_held():
            try:
                current_sid = current_sid_reader()
                raw_receipts = receipts_reader(root)
                conflicting_receipts = _conflicting_receipt_identities(raw_receipts)
                _RECEIPT_CONFLICTS.update(conflicting_receipts)
                receipt_rows = [item for item in raw_receipts
                                if validate_receipt_process(
                                    item, table=scan_table, current_sid=current_sid,
                                    process_sid=process_sid, equal_sid=equal_sid,
                                    created=process_created_reader, command_line=command_line,
                                    claude_states=claude_states,
                                    table_reader=process_table_reader,
                                    snapshot_time=scan_snapshot_time)]
                for item in receipt_rows:
                    if not _optional_thread_id(item.get("launcher_codex_thread_id")):
                        continue
                    key = (str(item["thread_id"]).lower(), int(item["codex_pid"]),
                           int(item["codex_created"]))
                    parent = _codex_receipt_parent(
                        item, table=scan_table, table_reader=process_table_reader,
                        current_sid=current_sid, process_sid=process_sid, equal_sid=equal_sid,
                        created=process_created_reader, command_argv=_command_argv,
                        codex_threads=codex.get("chats") or [],
                        snapshot_time=scan_snapshot_time or 0)
                    if parent is not None:
                        codex_receipt_parents[key] = parent
                    elif _CODEX_ENV_PER_THREAD_VERIFIED:
                        codex_receipt_contradictions.add(key)
                _CODEX_LINK_CONTRADICTIONS.update(codex_receipt_contradictions)
            except Exception:
                receipt_rows = []

    for item in claude_links:
        item["last_seen"] = now
        child_sid = str(item["child_session_id"])
        identity = (child_sid, int(item["child_pid"]), int(item["child_created"]))
        if identity in _CLAUDE_LINK_CONTRADICTIONS:
            continue
        prior_link = _CLAUDE_PROCESS_LINKS.get(child_sid)
        if prior_link and (int(prior_link["child_pid"]), int(prior_link["child_created"])) == identity[1:]:
            same_parent = (prior_link.get("parent_provider"), prior_link.get("parent_session_id"),
                           prior_link.get("parent_thread_id")) == (
                               item.get("parent_provider"), item.get("parent_session_id"),
                               item.get("parent_thread_id"))
            if not same_parent:
                _CLAUDE_LINK_CONTRADICTIONS.add(identity)
                _CLAUDE_PROCESS_LINKS.pop(child_sid, None)
                continue
            prior_link["last_seen"] = now
            continue
        if prior_link:
            _CLAUDE_LINK_CONTRADICTIONS.add((child_sid, int(prior_link["child_pid"]),
                                              int(prior_link["child_created"])))
        _CLAUDE_PROCESS_LINKS[child_sid] = item
    for key in conflicting_receipts | _conflicting_receipt_identities(receipt_rows):
        _RECEIPT_CONFLICTS.add(key)
        saved = _RUN_ROWS.get(key)
        if saved:
            saved["parent"] = None
            saved["parentProvider"] = None
            for field in _LINEAGE_FIELDS:
                    saved["row"].pop(field, None)
    for key in codex_receipt_contradictions:
        saved = _RUN_ROWS.get(key)
        if saved:
            saved["parent"] = None
            saved["parentProvider"] = None
            for field in _LINEAGE_FIELDS:
                saved["row"].pop(field, None)
    current_claude = {str(row.get("id")): row for row in claude.get("chats") or [] if row.get("id")}
    states_by_session = {str(key[0]): value for key, value in (claude_states or {}).items()
                         if isinstance(key, tuple) and len(key) == 2 and not key[1]}
    for child_sid, link in list(_CLAUDE_PROCESS_LINKS.items()):
        row = current_claude.get(child_sid)
        if row is None:
            if now - float(link.get("last_seen") or now) >= RETENTION_SECONDS:
                _CLAUDE_PROCESS_LINKS.pop(child_sid, None)
            continue
        link["last_seen"] = now
        if row.get("parentId"):
            # Native lineage always wins and invalidates process provenance.
            _CLAUDE_LINK_CONTRADICTIONS.add((child_sid, int(link["child_pid"]), int(link["child_created"])))
            _CLAUDE_PROCESS_LINKS.pop(child_sid, None)
            continue
        child_state = states_by_session.get(child_sid)
        if child_state:
            identities = {(int(entry.get("pid", -1)), int(entry.get("created", -1)))
                          for entry in child_state.get("pid_associations") or ()}
            current_identity = None
            try:
                current_identity = (int(child_state["pid"]), int(child_state["pid_created"]))
            except (KeyError, TypeError, ValueError):
                pass
            if (current_identity is not None
                    and current_identity != (int(link["child_pid"]), int(link["child_created"]))
                    or current_identity is None and identities
                    and (int(link["child_pid"]), int(link["child_created"])) not in identities):
                _CLAUDE_LINK_CONTRADICTIONS.add((child_sid, int(link["child_pid"]), int(link["child_created"])))
                _CLAUDE_PROCESS_LINKS.pop(child_sid, None)
                continue
            captures = [capture for capture in child_state.get("launcher_associations") or ()
                        if int(capture.get("pid", -1)) == int(link["child_pid"])
                        and int(capture.get("created", -1)) == int(link["child_created"])]
            codex_values = {capture.get("launcher_codex_thread_id") for capture in captures
                            if _optional_thread_id(capture.get("launcher_codex_thread_id"))}
            claude_values = {capture.get("launcher_claude_session_id") for capture in captures
                             if _optional_thread_id(capture.get("launcher_claude_session_id"))}
            expected_codex = link.get("launcher_codex_thread_id")
            if (len(codex_values) > 1 or len(claude_values) > 1
                    or (expected_codex and codex_values and codex_values != {expected_codex})):
                _CLAUDE_LINK_CONTRADICTIONS.add((child_sid, int(link["child_pid"]), int(link["child_created"])))
                _CLAUDE_PROCESS_LINKS.pop(child_sid, None)
                continue
        parent_state = states_by_session.get(str(link.get("parent_session_id")))
        if parent_state and link.get("parent_provider") == "Claude":
            raw = parent_state.get("pid_associations") or ()
            relevant = [entry for entry in raw
                        if int(entry.get("pid", -1)) == int(link["parent_pid"])
                        and int(entry.get("created", -1)) == int(link["parent_created"])]
            if relevant:
                parents = _association_map(states_by_session and claude_states or {}).get(
                    (int(link["parent_pid"]), int(link["parent_created"])), ())
                d_epoch = _filetime_epoch(int(link["direct_child_created"]))
                c_epoch = _filetime_epoch(int(link["child_created"]))
                matches = {sid for sid, first, last in parents
                           if d_epoch is not None and c_epoch is not None
                           and first <= d_epoch < last and first <= c_epoch < last}
                if matches != {str(link["parent_session_id"])}:
                    _CLAUDE_LINK_CONTRADICTIONS.add((child_sid, int(link["child_pid"]), int(link["child_created"])))
                    _CLAUDE_PROCESS_LINKS.pop(child_sid, None)
                    continue
        row["parentId"] = str(link.get("parent_session_id") if link.get("parent_provider") == "Claude"
                                else link.get("parent_thread_id"))
        row["parentProvider"] = str(link.get("parent_provider") or "Claude")
        row["parentLinkMethod"] = "process"
        row["linkSource"] = "process"
        row["parentIncarnation"] = {
            "provider": row["parentProvider"], "id": row["parentId"],
            "pid": int(link["parent_pid"]), "created": int(link["parent_created"]),
        }
        row["childRunIdentity"] = {
            "provider": "Claude", "id": child_sid,
            "pid": int(link["child_pid"]), "created": int(link["child_created"]),
        }
    by_identity = {(str(run["thread_id"]).lower(), int(run["codex_pid"]),
                    int(run["codex_created"]), str(run.get("claude_session_id") or "")): run
                   for run in runs if all(key in run for key in (
                       "thread_id", "codex_pid", "codex_created"))}
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
        unlinked_identities.setdefault(identity[0], set()).add((identity[1], identity[2]))
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
                with _UNLINKED_LOCK:
                    _UNLINKED_EXECUTIONS.pop(identity, None)
                    still_unlinked = any(key[0] == identity[0] for key in _UNLINKED_EXECUTIONS)
                if not still_unlinked:
                    unlinked_live_threads.discard(identity[0])
            sid = str(run.get("claude_session_id") or "")
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
                verdicts[(key[0], key[1], key[2])] = True
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
    seen_live_runs: set[tuple[str, int, int]] = set()
    for row in codex.get("chats") or []:
        thread = str(row.get("id") or "").lower()
        native_parent = not cross_provider_candidate(row)
        # Existing Claude links must be revalidated from the registry/discovery
        # before they are republished in this poll.
        if row.get("parentId") and not native_parent:
            row.pop("parentId", None)
            row.pop("parentProvider", None)
            row.pop("parentLinkMethod", None)
        row["runVerified"] = None
        historical = by_thread.get(thread, [])
        if not historical:
            continue
        status = str(row.get("executionStatus") or row.get("status") or "").lower()
        if status == "running":
            # Any live candidate that cannot be verified blocks binding, even when
            # another candidate is a receipt.
            if unresolved_by_thread.get(thread) or thread in unknown_unlinked_threads:
                continue
            live_runs = verified_by_thread.get(thread, [])
            row_identity = _row_identity(row)
            receipt_runs = [run for run in live_runs if run.get("method") == "receipt"
                            and (thread, int(run["codex_pid"]), int(run["codex_created"]))
                            not in _RECEIPT_CONFLICTS]
            exact_unlinked = unlinked_identities.get(thread, set())
            if receipt_runs:
                # A live unlinked execution blocks receipts unless the receipt is that exact process.
                receipts = [run for run in receipt_runs
                            if thread not in unlinked_live_threads or _run_identity(run) in exact_unlinked]
                if row_identity is not None:
                    receipts = [run for run in receipts if _run_identity(run) == row_identity]
                if not receipts:
                    continue
                selected = max(receipts, key=lambda run: (int(run["codex_created"]), float(run.get("last_seen") or 0)))
                identity = _run_identity(selected)
                sid = str(selected.get("claude_session_id") or "")
            else:
                scanned = [run for run in discovered if str(run.get("thread_id") or "").lower() == thread]
                candidates = scanned if scanned else [*live_runs, *verified_unlinked.get(thread, [])]
                identities = {_run_identity(run) for run in candidates}
                if len(identities) != 1:
                    continue
                selected = candidates[0]
                identity = _run_identity(selected)
                if row_identity is not None and identity != row_identity:
                    continue
                # Process scans establish liveness only, never lineage, unless the
                # run is an exact cmdline ancestry match.
                if selected.get("method") != "cmdline":
                    try:
                        started = datetime.fromisoformat(str(row.get("runStartedAt") or row.get("activeRunStartedAt")).replace("Z", "+00:00")).timestamp()
                        created_epoch = _filetime_epoch(int(selected["codex_created"]))
                        if created_epoch is None or created_epoch < started:
                            continue
                    except (TypeError, ValueError, OverflowError, OSError):
                        continue
                # Exact cmdline lineage: one session for this identity or no parent.
                sids = {str(run.get("claude_session_id") or "") for run in candidates
                        if _run_identity(run) == identity}
                sid = next(iter(sids)) if len(sids) == 1 else ""
            run_key = (thread, *identity)
            prior = _RUN_ROWS.get(run_key)
            if run_key in _RECEIPT_CONFLICTS:
                sid = ""
            if run_key in _CODEX_LINK_CONTRADICTIONS:
                sid = ""
            # A parentless prior binding (parent None) must not override a later receipt.
            if prior and prior.get("parent") is not None and run_key not in _RECEIPT_CONFLICTS:
                sid = prior["parent"]
            if prior and prior.get("parentProvider") == "Codex":
                row["parentId"], row["parentProvider"] = sid, "Codex"
                native_parent = True
            if native_parent or sid in claude_ids or not sid:
                if not native_parent and sid:
                    row["parentId"], row["parentProvider"] = sid, "Claude"
                row["parentLinkMethod"] = selected.get("method")
                if native_parent:
                    row.setdefault("linkSource", "native")
                elif sid:
                    row["linkSource"] = "process"
                    row["parentIncarnation"] = {
                        "provider": "Claude", "id": sid,
                        "pid": int(selected.get("claude_pid", -1) or -1),
                        "created": int(selected.get("claude_created", -1) or -1),
                    }
                    row["childRunIdentity"] = {
                        "provider": "Codex", "id": thread,
                        "pid": int(identity[0]), "created": int(identity[1]),
                    }
                codex_parent = codex_receipt_parents.get(run_key)
                if codex_parent and not native_parent and run_key not in _CODEX_LINK_CONTRADICTIONS:
                    row.update(parentId=codex_parent["parent_thread_id"], parentProvider="Codex",
                               parentLinkMethod="receipt", linkSource="process",
                               parentIncarnation={"provider": "Codex", "id": codex_parent["parent_thread_id"],
                                                  "pid": codex_parent["parent_pid"],
                                                  "created": codex_parent["parent_created"]},
                               childRunIdentity={"provider": "Codex", "id": thread,
                                                 "pid": identity[0], "created": identity[1]})
                row["runPid"], process_created = identity
                row["runCreated"] = str(process_created)
                row["runVerified"] = True
                row.pop("runBindingUnresolved", None)
                parent_row = next((item for item in claude.get("chats") or [] if str(item.get("id")) == sid), None)
                _RUN_ROWS[run_key] = {"row": copy.deepcopy(row), "parent": row.get("parentId"), "parentProvider": row.get("parentProvider"), "stoppedAt": None,
                                      "parentRow": copy.deepcopy(parent_row)}
                seen_live_runs.add(run_key)
                # Other live receipts are retained as separate rows, never merged into this one.
                for older in receipt_runs:
                    other_key = (thread, int(older["codex_pid"]), int(older["codex_created"]))
                    if other_key == run_key or other_key in _RUN_ROWS:
                        continue
                    older_sid = str(older.get("claude_session_id") or "")
                    old_row = copy.deepcopy(row)
                    for field in ("tokens", "toolCalls", "filesEdited", "fileChanges", "contextTokens", "contextPercent", "contextWindow", "runStartedAt", "activeRunStartedAt"):
                        old_row.pop(field, None)
                    old_row.update(runPid=other_key[1], runCreated=str(other_key[2]), metricsUnavailable=True)
                    if older_sid in claude_ids:
                        old_row.update(parentId=older_sid, parentProvider="Claude", parentLinkMethod="receipt")
                    else:
                        old_row.pop("parentId", None)
                        old_row.pop("parentProvider", None)
                    old_parent = next((item for item in claude.get("chats") or [] if str(item.get("id")) == older_sid), None)
                    _RUN_ROWS[other_key] = {"row": old_row, "parent": older_sid, "parentRow": copy.deepcopy(old_parent), "stoppedAt": None}
            else:
                # The parent session is absent from the Claude list: keep this row's own
                # status and bind only the run identity, never a parent.
                row["runPid"], process_created = identity
                row["runCreated"] = str(process_created)
                row["runVerified"] = True
                row.pop("runBindingUnresolved", None)
                seen_live_runs.add(run_key)
            continue
        if row.get("runBindingUnresolved") or id(row) in native_rows:
            continue
        # A terminal thread row has no provable connection to a historical
        # execution unless it carries some execution's process identity.
        if _row_identity(row) is None:
            continue
        # Newest historical execution wins (latest codex_created, then last_seen).
        selected = max(historical, key=lambda run: (int(run["codex_created"]), float(run.get("last_seen") or 0)))
        sid = str(selected.get("claude_session_id") or "")
        if sid not in claude_ids:
            continue
        turn_start = row.get("runStartedAt") or row.get("activeRunStartedAt")
        try:
            turn_epoch = datetime.fromisoformat(str(turn_start).replace("Z", "+00:00")).timestamp() if turn_start else None
        except (TypeError, ValueError, OverflowError, OSError):
            turn_epoch = None
        if turn_epoch is not None and turn_epoch > float(selected["last_seen"]):
            continue
        if turn_epoch is None and row.get("detailPending"):
            # A deferred row has no transcript detail; without a run boundary it cannot prove an old association.
            continue
        verdict = verify(*_run_identity(selected))
        if verdict is None:
            continue
        row["parentId"], row["parentProvider"] = sid, "Claude"
        row["parentLinkMethod"] = selected.get("method")
        row["runVerified"] = verdict

    for row in codex.get("chats") or []:
        # Only a running row whose thread had link candidates and whose binding failed is unresolved.
        thread = str(row.get("id") or "").lower()
        if (row.get("source") == "exec" and row.get("runVerified") is None and by_thread.get(thread)
                and str(row.get("executionStatus") or row.get("status") or "").lower() == "running"):
            if id(row) not in native_rows:
                row.pop("parentId", None)
                row.pop("parentProvider", None)
            row["previousStatus"] = row.get("status")
            row["runBindingPreviousExecutionStatus"] = row.get("executionStatus")
            row.update(status="unknown", statusSource="unavailable", executionStatus="unavailable",
                       executionSource="unavailable", telemetryUnavailable=True, runBindingUnresolved=True)

    # Retain only runs observed with an exact process identity.  An unknown
    # verification is not evidence of death, and never freezes a current row.
    retained = []
    needed_ancestors = {str(row.get("parentId")) for row in codex.get("chats") or []
                        if row.get("runVerified") is True and row.get("parentProvider") == "Codex" and row.get("parentId")}
    while True:
        more = {str(saved["row"].get("parentId")) for key, saved in _RUN_ROWS.items()
                if key[0] in needed_ancestors and saved["row"].get("parentProvider") == "Codex"
                and saved["row"].get("parentId")}
        if more <= needed_ancestors:
            break
        needed_ancestors.update(more)
    for identity, saved in list(_RUN_ROWS.items()):
        if identity in seen_live_runs:
            continue
        stopped_at = saved.get("stoppedAt")
        if stopped_at is None:
            verdict = verdicts.get(identity)
            if identity not in verdicts:
                if any(str(run.get("thread_id", "")).lower() == identity[0] and run.get("codex_pid") == identity[1] and run.get("codex_created") == identity[2] and run.get("retired_at") for run in runs):
                    verdict = False
                elif need_full_scan:
                    # The due scan supersedes per-pid verification; an unscanned identity stays unresolved.
                    verdict = None
                else:
                    verdict = verify(identity[1], identity[2])
            if verdict is None:
                continue
            if verdict is True:
                row = copy.deepcopy(saved["row"])
                row.update(retainedRun=True, runVerified=True)
                retained.append(row)
                continue
            stopped_at = saved["stoppedAt"] = now
        if now - stopped_at >= STOPPED_RETENTION_SECONDS and identity[0] not in needed_ancestors:
            del _RUN_ROWS[identity]
            continue
        row = copy.deepcopy(saved["row"])
        if saved["parent"] not in claude_ids and saved.get("parentRow"):
            claude.setdefault("chats", []).append(copy.deepcopy(saved["parentRow"]))
            claude_ids.add(saved["parent"])
        stamp = datetime.fromtimestamp(stopped_at, timezone.utc).isoformat()
        row.update(retainedRun=True, runVerified=False, stoppedAt=stamp, runEndedAt=stamp,
                   executionStatus="idle", executionTerminalAt=stamp, stateSince=stamp)
        if str(row.get("status") or "").lower() not in {"failed", "completed"}:
            row.update(status="idle", stateDetail="stopped")
        retained.append(row)
    codex.setdefault("chats", []).extend(retained)
    # One row per thread. The current provider row is the base: a retained copy
    # is frozen at its last live poll and never replaces fresher data. Only
    # execution and lineage fields come from the newest execution, an
    # unresolved current row stays authoritative (an older run's parent cannot
    # prove its lineage), and a stopped execution never overrides a current row
    # that is active again under another (or no) execution.
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in codex["chats"]:
        grouped.setdefault(str(row.get("id") or "").lower(), []).append(row)
    collapsed = []
    for thread, rows in grouped.items():
        if not thread:
            collapsed.extend(rows)
            continue
        executions = [row for row in rows if _row_identity(row) is not None]
        newest = max(executions, key=_execution_order) if executions else None
        current_rows = [row for row in rows if not row.get("retainedRun")]
        if current_rows:
            selected = max(current_rows, key=_execution_order)
            stopped = bool(newest and newest.get("stoppedAt"))
            if stopped and newest is not selected and _row_identity(selected) == _row_identity(newest):
                # The current row still names the execution that was verified
                # dead: its process claim is stale, so the frozen run stands.
                selected = newest
            elif (newest is not None and newest is not selected and not selected.get("runBindingUnresolved")
                    and not (stopped and _row_outlives_stop(selected, newest))):
                fields = _RUN_FIELDS if id(selected) in native_rows else _RUN_FIELDS + _LINEAGE_FIELDS
                for field in fields:
                    if field in newest:
                        selected[field] = newest[field]
                    else:
                        selected.pop(field, None)
                if stopped:
                    for field in _STOP_FIELDS:
                        if field in newest:
                            selected[field] = newest[field]
                    if str(selected.get("status") or "").lower() not in {"failed", "completed"}:
                        selected.update(status="idle", stateDetail="stopped")
        else:
            selected = newest or rows[-1]
        identities = {identity for row in rows if (identity := _row_identity(row)) is not None}
        identities.update((pid, created) for (tid, pid, created) in _RUN_ROWS if tid == thread)
        identities.update(_run_identity(run) for run in verified_by_thread.get(thread, []))
        with _UNLINKED_LOCK:
            identities.update((pid, created) for (tid, pid, created) in _UNLINKED_EXECUTIONS if tid == thread)
        # Every execution observed in this app process counts, even after its
        # retained copy expires; registry history from earlier sessions does not.
        observed = _OBSERVED_IDENTITIES.pop(thread, set()) | identities
        _OBSERVED_IDENTITIES[thread] = observed
        while len(_OBSERVED_IDENTITIES) > _OBSERVED_IDENTITY_LIMIT:
            _OBSERVED_IDENTITIES.pop(next(iter(_OBSERVED_IDENTITIES)))
        selected.pop("resumeCount", None)
        if len(observed) >= 2:
            selected["resumeCount"] = len(observed)
        selected.pop("firstSeenAt", None)
        selected_identity = _row_identity(selected)
        matches = [run for run in by_thread.get(thread, []) if _run_identity(run) == selected_identity]
        if matches:
            first_seen = min(float(run["first_seen"]) for run in matches)
            selected["firstSeenAt"] = datetime.fromtimestamp(first_seen, timezone.utc).isoformat()
        collapsed.append(selected)
    codex["chats"] = collapsed
