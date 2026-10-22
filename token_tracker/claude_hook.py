"""Claude Code hook collector: one allowlisted JSON line per hook event.

Runs as ``python -m token_tracker.claude_hook`` (and later as the frozen
launcher's ``--claude-hook`` mode). Claude Code passes the hook payload on
stdin. This process never prints, never raises and always exits 0: a hook
must not be able to slow down or break a Claude session.

Privacy: only the fields in ``_build_record`` are ever written. Prompt text,
tool inputs and outputs, messages, file names and full paths are never kept.
Nothing is written unless the user's consent marker exists.
"""

from __future__ import annotations

import json
import os
import queue
import re
import sys
import threading
import time
from typing import Any

CONSENT_NAME = "claude-consent.json"
EVENTS_DIR_NAME = "claude-events"
MAX_LINE_BYTES = 4096
MAX_DAY_BYTES = 32 * 1024 * 1024
MAX_STDIN_BYTES = 8 * 1024 * 1024
STDIN_TIMEOUT_SECONDS = 2.0
# Hard bound on the whole hook process, enforced by a watchdog thread that
# ends the process with exit code 0 whatever the main thread is blocked on.
PROCESS_DEADLINE_SECONDS = 5.0
DROP_LOG_NAME = "dropped.jsonl"
MAX_DROP_LOG_BYTES = 64 * 1024

# Field-specific identifier formats. Observed Claude Code values: session and
# prompt ids are UUIDs, agent ids are alphanumeric, tool-use ids are
# ``toolu_<base62>``. No dots, slashes or colons, so file names and paths fail.
_UUID = re.compile(r"[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}")
_AGENT_ID = re.compile(r"[A-Za-z0-9_\-]{1,64}")
_TOOL_USE_ID = re.compile(r"[A-Za-z0-9_\-]{1,96}")
_EVENTS = frozenset({
    "SessionStart", "SessionEnd", "UserPromptSubmit", "PreToolUse", "PostToolUse", "PostToolUseFailure",
    "PermissionRequest", "Notification", "Stop", "StopFailure", "SubagentStart", "SubagentStop",
    "PreCompact", "PostCompact",
})
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f  ]")
_MODEL = re.compile(r"claude-[a-z0-9.\-]{1,48}")
_AGENT_TYPE = re.compile(r"[A-Za-z0-9_\-]{1,40}")
_LEAF = re.compile(r"[A-Za-z0-9 _.+\-]{1,48}")  # folder name: no shell or control characters
_ENUMS = {
    "source": {"startup", "resume", "clear", "compact", "fork", "other"},
    "reason": {"clear", "resume", "logout", "prompt_input_exit", "bypass_permissions_disabled", "other"},
    "trigger": {"auto", "manual", "other"},
    "notification_type": {"idle_prompt", "permission_prompt", "elicitation_dialog", "auth_success", "other"},
}
_TOOL_CATEGORIES = {
    "Read": "read", "Edit": "edit", "Write": "edit", "MultiEdit": "edit", "NotebookEdit": "edit",
    "Grep": "search", "Glob": "search", "Bash": "command", "PowerShell": "command",
    "WebSearch": "web_search", "WebFetch": "web_fetch", "Task": "agent", "Agent": "agent",
    "AskUserQuestion": "ask", "TodoWrite": "other",
}


def _hook_data_root() -> str:
    """The shared hook root from product.py (the same path in dev and frozen processes)."""
    from . import product
    return str(product.hook_data_root())


def data_dir() -> str:
    return _hook_data_root()


def consent_path() -> str:
    return os.path.join(data_dir(), CONSENT_NAME)


def events_dir() -> str:
    return os.path.join(data_dir(), EVENTS_DIR_NAME)


# ---------------------------------------------------------------- processes

def _process_table() -> dict[int, tuple[int, str]]:
    """pid -> (parent pid, lowercase exe name) from a Toolhelp32 snapshot."""
    import ctypes
    import ctypes.wintypes as wt

    class ProcessEntry(ctypes.Structure):
        _fields_ = [("dwSize", wt.DWORD), ("cntUsage", wt.DWORD), ("th32ProcessID", wt.DWORD),
                    ("th32DefaultHeapID", ctypes.c_void_p), ("th32ModuleID", wt.DWORD),
                    ("cntThreads", wt.DWORD), ("th32ParentProcessID", wt.DWORD),
                    ("pcPriClassBase", ctypes.c_long), ("dwFlags", wt.DWORD),
                    ("szExeFile", ctypes.c_char * 260)]

    kernel = ctypes.windll.kernel32
    kernel.CreateToolhelp32Snapshot.restype = ctypes.c_void_p
    snapshot = kernel.CreateToolhelp32Snapshot(2, 0)
    table: dict[int, tuple[int, str]] = {}
    if not snapshot or snapshot == ctypes.c_void_p(-1).value:
        return table
    try:
        entry = ProcessEntry()
        entry.dwSize = ctypes.sizeof(ProcessEntry)
        ok = kernel.Process32First(ctypes.c_void_p(snapshot), ctypes.byref(entry))
        while ok:
            table[int(entry.th32ProcessID)] = (int(entry.th32ParentProcessID),
                                               entry.szExeFile.decode("mbcs", "ignore").lower())
            ok = kernel.Process32Next(ctypes.c_void_p(snapshot), ctypes.byref(entry))
    finally:
        kernel.CloseHandle(ctypes.c_void_p(snapshot))
    return table


def probe_process(pid: int) -> tuple[str, int | None]:
    """("alive", creation ticks) | ("gone", None) | ("unknown", None)."""
    if os.name != "nt" or not isinstance(pid, int) or pid <= 0:
        return "unknown", None
    try:
        import ctypes
        import ctypes.wintypes as wt
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.restype = ctypes.c_void_p
        kernel.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
        kernel.CloseHandle.argtypes = [ctypes.c_void_p]
        kernel.GetProcessTimes.argtypes = [ctypes.c_void_p] + [ctypes.POINTER(wt.FILETIME)] * 4
        kernel.GetExitCodeProcess.argtypes = [ctypes.c_void_p, ctypes.POINTER(wt.DWORD)]
        handle = kernel.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            # ERROR_INVALID_PARAMETER means no such process; anything else
            # (for example access denied) proves nothing about liveness.
            return ("gone", None) if ctypes.get_last_error() == 87 else ("unknown", None)
        try:
            code = wt.DWORD()
            if kernel.GetExitCodeProcess(handle, ctypes.byref(code)) and code.value != 259:
                return "gone", None
            created, exited, kernel_time, user_time = (wt.FILETIME() for _ in range(4))
            if not kernel.GetProcessTimes(handle, ctypes.byref(created), ctypes.byref(exited),
                                          ctypes.byref(kernel_time), ctypes.byref(user_time)):
                return "unknown", None
            return "alive", (created.dwHighDateTime << 32) | created.dwLowDateTime
        finally:
            kernel.CloseHandle(handle)
    except Exception:
        return "unknown", None


def process_created(pid: int) -> int | None:
    """Creation time (FILETIME ticks) of a live process, else None."""
    state, created = probe_process(pid)
    return created if state == "alive" else None


def nearest_claude_ancestor(table: dict[int, tuple[int, str]], start: int, limit: int = 12) -> int | None:
    pid, seen = start, set()
    for _ in range(limit):
        if pid in seen or pid not in table:
            return None
        seen.add(pid)
        parent, name = table[pid]
        if pid != start and name in {"claude.exe", "claude"}:
            return pid
        pid = parent
    return None


def _claude_process() -> tuple[int, int] | tuple[None, None]:
    if os.name != "nt":
        return None, None
    try:
        pid = nearest_claude_ancestor(_process_table(), os.getpid())
        if pid is None:
            return None, None
        created = process_created(pid)
        return (pid, created) if created is not None else (None, None)
    except Exception:
        return None, None


# ------------------------------------------------------------------- record

def clean_text(value: Any, limit: int) -> str | None:
    if not isinstance(value, str):
        return None
    text = _CONTROL.sub(" ", value).strip()
    text = re.sub(r"\s+", " ", text)
    return text[:limit] or None


def workspace_label(cwd: Any) -> str | None:
    """Drive plus last folder only, never the full path."""
    if not isinstance(cwd, str) or not cwd.strip():
        return None
    text = cwd.strip().replace("\\", "/").rstrip("/")
    drive = text[:2] if len(text) >= 2 and text[1] == ":" and text[0].isalpha() else ""
    # Only an absolute working directory names a folder; anything else is not a cwd.
    if not (drive and (len(text) == 2 or text[2] == "/")) and not cwd.strip().startswith(("/", "\\")):
        return None
    leaf = text.rsplit("/", 1)[-1]
    if not _LEAF.fullmatch(leaf) or leaf in {".", ".."}:
        leaf = ""
    if leaf.endswith(":") and not drive:
        leaf = ""
    label = f"{drive}/{leaf}" if drive and leaf and leaf != drive else (drive or leaf)
    return label or None


def build_record(payload: Any, now: float, claude_pid: int | None = None,
                 claude_created: int | None = None) -> dict[str, Any] | None:
    if not isinstance(payload, dict):
        return None
    event = payload.get("hook_event_name")
    session_id = payload.get("session_id")
    if not isinstance(event, str) or event not in _EVENTS:
        return None
    if not isinstance(session_id, str) or not _UUID.fullmatch(session_id):
        return None
    record: dict[str, Any] = {"ts": round(now, 6), "event": event, "session_id": session_id}

    def put(name: str, value: Any, pattern: re.Pattern[str]) -> None:
        if isinstance(value, str) and pattern.fullmatch(value):
            record[name] = value

    put("agent_id", payload.get("agent_id"), _AGENT_ID)
    put("prompt_id", payload.get("prompt_id"), _UUID)
    put("agent_type", payload.get("agent_type"), _AGENT_TYPE)
    raw_tool = payload.get("tool_name")
    if isinstance(raw_tool, str):
        record["tool_name"] = "mcp" if raw_tool.startswith("mcp__") else _TOOL_CATEGORIES.get(raw_tool, "other")
    put("tool_use_id", payload.get("tool_use_id"), _TOOL_USE_ID)
    for field, values in _ENUMS.items():
        value = payload.get(field)
        if isinstance(value, str):
            record[field] = value if value in values else "other"
    model = payload.get("model")
    if isinstance(model, str) and _MODEL.fullmatch(model):
        record["model"] = model
    if isinstance(payload.get("is_interrupt"), bool):
        record["is_interrupt"] = payload["is_interrupt"]
    workspace = workspace_label(payload.get("cwd"))
    if workspace:
        record["workspace"] = workspace
    if claude_pid and claude_created:
        record["claude_pid"] = int(claude_pid)
        record["claude_pid_created"] = int(claude_created)
    if record.get("tool_name") == "ask":
        record["ask"] = True
    return record


# ------------------------------------------------------------------- output

def _day_name(now: float) -> str:
    return time.strftime("%Y%m%d", time.localtime(now)) + ".jsonl"


def _append_windows(path: str, line: bytes, cap: int) -> str:
    """One WriteFile on a handle opened with append-only data access.

    The CRT's O_APPEND emulation seeks to the end and then writes, which is
    not atomic across processes. A handle with FILE_APPEND_DATA but without
    FILE_WRITE_DATA makes the kernel place every write at end-of-file
    atomically. FILE_READ_ATTRIBUTES is added for GetFileSizeEx only.
    """
    import ctypes
    import ctypes.wintypes as wt

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateFileW.restype = wt.HANDLE
    kernel.CreateFileW.argtypes = [wt.LPCWSTR, wt.DWORD, wt.DWORD, ctypes.c_void_p, wt.DWORD, wt.DWORD, wt.HANDLE]
    kernel.GetFileSizeEx.argtypes = [wt.HANDLE, ctypes.POINTER(ctypes.c_longlong)]
    kernel.GetFileSizeEx.restype = wt.BOOL
    kernel.WriteFile.argtypes = [wt.HANDLE, ctypes.c_char_p, wt.DWORD, ctypes.POINTER(wt.DWORD), ctypes.c_void_p]
    kernel.WriteFile.restype = wt.BOOL
    kernel.CloseHandle.argtypes = [wt.HANDLE]
    file_append_data, file_read_attributes = 0x0004, 0x0080
    share_all = 0x1 | 0x2 | 0x4  # read, write, delete
    open_always, normal = 4, 0x80
    handle = kernel.CreateFileW(path, file_append_data | file_read_attributes, share_all, None,
                                open_always, normal, None)
    if not handle or handle == wt.HANDLE(-1).value:
        raise OSError(ctypes.get_last_error(), "CreateFileW failed")
    try:
        size = ctypes.c_longlong(0)
        if not kernel.GetFileSizeEx(handle, ctypes.byref(size)):
            raise OSError(ctypes.get_last_error(), "GetFileSizeEx failed")
        if size.value + len(line) > cap:
            return "cap"
        written = wt.DWORD(0)
        if not kernel.WriteFile(handle, line, len(line), ctypes.byref(written), None):
            raise OSError(ctypes.get_last_error(), "WriteFile failed")
        return "ok" if written.value == len(line) else "short"
    finally:
        kernel.CloseHandle(handle)


def _append_posix(path: str, line: bytes, cap: int) -> str:
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    try:
        if os.fstat(fd).st_size + len(line) > cap:
            return "cap"
        return "ok" if os.write(fd, line) == len(line) else "short"
    finally:
        os.close(fd)


def append_line(path: str, line: bytes, cap: int) -> str:
    """Append one complete line with a single write: "ok", "cap" or "short"."""
    return (_append_windows if os.name == "nt" else _append_posix)(path, line, cap)


def _note_drop(directory: str, reason: str, now: float) -> None:
    """Best-effort bounded counter; a failure here is ignored."""
    try:
        line = (json.dumps({"ts": round(now, 3), "reason": reason}) + "\n").encode("ascii")
        append_line(os.path.join(directory, DROP_LOG_NAME), line, MAX_DROP_LOG_BYTES)
    except Exception:
        pass


def append_record(record: dict[str, Any], now: float) -> bool:
    directory = events_dir()
    os.makedirs(directory, exist_ok=True)
    line = (json.dumps(record, separators=(",", ":"), ensure_ascii=True) + "\n").encode("ascii")
    if len(line) > MAX_LINE_BYTES:
        _note_drop(directory, "oversize", now)
        return False
    outcome = append_line(os.path.join(directory, _day_name(now)), line, MAX_DAY_BYTES)
    if outcome == "cap":
        _note_drop(directory, "day-cap", now)
    return outcome == "ok"


def handle(raw: bytes, now: float | None = None) -> bool:
    """Process one hook payload. Returns True when a line was written."""
    if not os.path.isfile(consent_path()):
        return False
    now = time.time() if now is None else now
    try:
        # utf-8-sig: a .NET Framework parent writing through a UTF-8 console encoding prefixes a BOM.
        payload = json.loads(raw.decode("utf-8-sig", "replace"))
    except ValueError:
        return False
    pid, created = _claude_process()
    record = build_record(payload, now, pid, created)
    if record is None:
        return False
    return append_record(record, now)


def main(argv: list[str] | None = None) -> int:
    try:
        # Consent first: without the marker nothing is read, parsed or written.
        if not os.path.isfile(consent_path()):
            return 0
        result: queue.Queue[bytes | BaseException] = queue.Queue(maxsize=1)

        def read_input() -> None:
            try:
                result.put(sys.stdin.buffer.read(MAX_STDIN_BYTES + 1))
            except BaseException as exc:
                result.put(exc)

        reader = threading.Thread(target=read_input, name="claude-hook-stdin", daemon=True)
        reader.start()
        reader.join(STDIN_TIMEOUT_SECONDS)
        if reader.is_alive():
            return 0
        raw = result.get_nowait()
        if isinstance(raw, BaseException) or len(raw) > MAX_STDIN_BYTES:
            return 0
        handle(raw)
    except BaseException:
        pass
    return 0


def _watchdog(deadline: float) -> None:
    def expire() -> None:
        time.sleep(deadline)
        os._exit(0)

    threading.Thread(target=expire, name="claude-hook-deadline", daemon=True).start()


def run(argv: list[str] | None = None) -> None:
    """Process entry point: bounded lifetime, exit code always 0.

    ``os._exit`` skips interpreter finalisation, which could otherwise block
    or abort on the stdin reader thread that still holds the buffer lock.
    """
    try:
        _watchdog(PROCESS_DEADLINE_SECONDS)
        main(argv)
    except BaseException:
        pass
    os._exit(0)


if __name__ == "__main__":
    run(sys.argv[1:])
