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


def verify_process(pid: int, expected_created: int) -> bool | None:
    """Cheap bounded recheck for a remembered pid; None means unverifiable."""
    try:
        observed = _process_created(pid)
        return observed == expected_created if observed is not None else None
    except Exception:
        return None


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
            size = 32768
            buffer = ctypes.create_string_buffer(size)
            returned = ctypes.c_ulong()
            status = ntdll.NtQueryInformationProcess(process, 60, buffer, size, ctypes.byref(returned))
            if status != 0 or returned.value < 8 or returned.value > size:
                return None
            # UNICODE_STRING starts with USHORT Length, USHORT MaximumLength,
            # followed by a pointer aligned to the native pointer width.
            pointer_offset = 8 if ctypes.sizeof(ctypes.c_void_p) == 8 else 4
            length = int.from_bytes(buffer.raw[:2], "little")
            pointer = int.from_bytes(buffer.raw[pointer_offset:pointer_offset + ctypes.sizeof(ctypes.c_void_p)], "little")
            if not length or length % 2 or length > 32760 or not pointer:
                return None
            return ctypes.wstring_at(pointer, length // 2)
        finally:
            kernel.CloseHandle(process)
    except Exception:
        return None


def parse_exec_resume_thread(command_line: str | None) -> str | None:
    """Extract only the UUID directly following ``exec resume``."""
    if not isinstance(command_line, str) or len(command_line) > 32760:
        return None
    try:
        args = shlex.split(command_line, posix=False)
    except ValueError:
        return None
    for index in range(len(args) - 2):
        if args[index].strip('"').lower() == "exec" and args[index + 1].strip('"').lower() == "resume":
            candidate = args[index + 2].strip('"')
            return candidate.lower() if THREAD_ID.fullmatch(candidate) else None
    return None


def _association_map(claude_states: dict[str, Any]) -> dict[tuple[int, int], set[str]]:
    found: dict[tuple[int, int], set[str]] = {}
    for identity, entry in (claude_states or {}).items():
        if not isinstance(identity, tuple) or len(identity) != 2 or identity[1]:
            continue
        sid = identity[0]
        for association in (entry.get("pid_associations") or {}).values():
            try:
                key = (int(association["pid"]), int(association["created"]))
                float(association["first"]), float(association["last"])
            except (KeyError, TypeError, ValueError, OverflowError):
                continue
            found.setdefault(key, set()).add(str(sid))
    return found


def scan_codex_processes(*, table: dict[int, tuple[int, str]] | None = None,
                         current_sid: bytes | None = None,
                         equal_sid: Callable[[bytes | None, bytes | None], bool] = _equal_sid,
                         process_sid: Callable[[int], bytes | None] = _process_sid,
                         created: Callable[[int], int | None] = _process_created,
                         command_line: Callable[[int], str | None] = _command_line,
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
            if not equal_sid(owner(child), current_sid):
                continue
            thread_id = parse_exec_resume_thread(command_line(child))
            child_created = identity(child)
        except Exception:
            continue
        if not thread_id or child_created is None:
            continue
        current, visited, chain = child, set(), []
        claude_identity = None
        valid = True
        for _ in range(12):
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
        if not valid or claude_identity is None:
            continue
        session_ids = associations.get(claude_identity, set())
        if len(session_ids) != 1:
            continue
        output.append({"thread_id": thread_id, "codex_pid": child,
                       "codex_created": child_created, "claude_session_id": next(iter(session_ids)),
                       "method": "cmdline"})
    return output


def receipt_path(root: Path | None = None) -> Path:
    if root is None:
        from .product import hook_data_root
        root = hook_data_root()
    return Path(root) / INBOX_NAME


def write_receipt(thread_id: str, codex_pid: int, *, environ: dict[str, str] | None = None,
                  created: Callable[[int], int | None] = _process_created,
                  root: Path | None = None) -> bool:
    """Append a bounded display-only association receipt for this execution."""
    env = os.environ if environ is None else environ
    sid, created_at = env.get("CLAUDE_CODE_SESSION_ID"), created(codex_pid)
    if not THREAD_ID.fullmatch(str(thread_id)) or not isinstance(sid, str) or not THREAD_ID.fullmatch(sid):
        return False
    if not isinstance(codex_pid, int) or codex_pid <= 0 or created_at is None:
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


def read_receipts(root: Path | None = None) -> list[dict[str, Any]]:
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
            last = float(run["last_seen"])
            if now - last > RETENTION_SECONDS:
                continue
            clean.append({key: run[key] for key in (
                "thread_id", "codex_pid", "codex_created", "claude_session_id",
                "first_seen", "last_seen", "verified_at", "method") if key in run})
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


def load_registry(path: Path) -> list[dict[str, Any]]:
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
                        and run.get("method") in {"cmdline", "receipt"}
                        and float(run["last_seen"]) >= float(run["first_seen"])):
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
    now = time.time() if now is None else now
    registry_changed = False
    if registry_path is None:
        try:
            from .product import state_root
            registry_path = state_root() / REGISTRY_NAME
        except Exception:
            return
    runs = registry_reader(registry_path)
    live_rows = [row for row in codex.get("chats") or []
                 if not row.get("parentId") and row.get("source") == "exec"
                 and row.get("executionStatus") == "running"]
    need_full_scan = False
    live_by_thread: dict[str, dict[str, Any]] = {}
    for row in live_rows:
        thread = str(row.get("id") or "").lower()
        candidates = [run for run in runs if str(run.get("thread_id") or "").lower() == thread]
        candidates.sort(key=lambda run: float(run.get("last_seen") or 0), reverse=True)
        known = candidates[0] if candidates else None
        if known is None:
            need_full_scan = True
            continue
        verified_at = float(known.get("verified_at") or 0)
        if now - verified_at < 10:
            live_by_thread[thread] = known
            continue
        verdict = verify(int(known["codex_pid"]), int(known["codex_created"]))
        if verdict is True:
            known["verified_at"] = now
            known["last_seen"] = now
            live_by_thread[thread] = known
            registry_changed = True
        else:
            need_full_scan = True

    discovered: list[dict[str, Any]] = []
    receipt_rows: list[dict[str, Any]] = []
    if need_full_scan:
        if claude_states is None:
            if hooks is None:
                from . import claude_events
                hooks = claude_events.load_states
            try:
                claude_states = hooks()
            except Exception:
                claude_states = {}
        try:
            discovered = scan(claude_states=claude_states)
        except Exception:
            discovered = []
        try:
            receipt_rows = receipts_reader(root)
        except Exception:
            receipt_rows = []
    by_identity = {(str(run["thread_id"]).lower(), int(run["codex_pid"]),
                    int(run["codex_created"]), str(run["claude_session_id"])): run
                   for run in runs if all(key in run for key in (
                       "thread_id", "codex_pid", "codex_created", "claude_session_id"))}
    for run in [*discovered, *receipt_rows]:
        try:
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
                live_by_thread[key[0]] = record
            elif verify(int(run["codex_pid"]), int(run["codex_created"])) is True:
                record["verified_at"] = now
                live_by_thread[key[0]] = record
        except (KeyError, TypeError, ValueError, OverflowError):
            continue
    runs = list(by_identity.values())
    if (need_full_scan or registry_changed) and _instance_mutex_held():
        registry_writer(runs, registry_path, now=now)

    by_thread: dict[str, set[str]] = {}
    latest_run: dict[str, dict[str, Any]] = {}
    for run in runs:
        try:
            thread = str(run["thread_id"]).lower()
            by_thread.setdefault(thread, set()).add(str(run["claude_session_id"]))
            if thread not in latest_run or float(run["last_seen"]) > float(latest_run[thread]["last_seen"]):
                latest_run[thread] = run
        except (KeyError, TypeError, ValueError, OverflowError):
            continue
    claude_ids = {str(row.get("id")) for row in claude.get("chats") or [] if row.get("id")}
    for row in codex.get("chats") or []:
        thread = str(row.get("id") or "").lower()
        sessions = by_thread.get(thread, set())
        if row.get("parentId") or len(sessions) != 1:
            continue
        selected = latest_run.get(thread)
        if selected is None:
            continue
        sid = next(iter(sessions))
        status = str(row.get("executionStatus") or row.get("status") or "").lower()
        if status == "running":
            live = live_by_thread.get(thread)
            if live and (live.get("codex_pid"), live.get("codex_created")) == (
                    selected.get("codex_pid"), selected.get("codex_created")) and sid in claude_ids:
                row["parentId"], row["parentProvider"] = sid, "Claude"
                row["parentLinkMethod"] = selected.get("method")
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
