"""Bounded, content-free Claude Code quota status-line projection."""
from __future__ import annotations

import json
import math
import os
import sys
import tempfile
import time
import stat
import threading
import queue
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from . import product

MAX_BYTES = 65536
FILENAME = "claude-statusline.json"
WINDOWS = ("five_hour", "seven_day")
DURATIONS = {"five_hour": 18000, "seven_day": 604800}
SKEW = 300
DEADLINE_SECONDS = 1.5



def _check_path(path, *, allow_missing=False, directory=None):
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        if allow_missing:
            return None
        raise
    if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
        raise OSError("reparse point refused")
    if directory is True and not stat.S_ISDIR(info.st_mode):
        raise OSError("not a directory")
    if directory is False and (not stat.S_ISREG(info.st_mode) or getattr(info, "st_nlink", 1) != 1):
        raise OSError("not a safe regular file")
    return info


def _windows_open(path, access, share, disposition, directory=False):
    import ctypes
    from ctypes import wintypes
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    create = kernel.CreateFileW
    create.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
                       wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    create.restype = wintypes.HANDLE
    flags = 0x00200000 | (0x02000000 if directory else 0)  # OPEN_REPARSE_POINT, BACKUP_SEMANTICS
    handle = create(str(path), access, share, None, disposition, flags, None)
    if handle == wintypes.HANDLE(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    class Info(ctypes.Structure):
        _fields_ = [("attributes", wintypes.DWORD), ("creation", wintypes.FILETIME),
                    ("access", wintypes.FILETIME), ("write", wintypes.FILETIME),
                    ("volume", wintypes.DWORD), ("size_high", wintypes.DWORD),
                    ("size_low", wintypes.DWORD), ("links", wintypes.DWORD),
                    ("index_high", wintypes.DWORD), ("index_low", wintypes.DWORD)]
    info = Info()
    kernel.GetFileInformationByHandle.argtypes = [wintypes.HANDLE, ctypes.POINTER(Info)]
    kernel.GetFileInformationByHandle.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    if not kernel.GetFileInformationByHandle(handle, ctypes.byref(info)):
        error = ctypes.get_last_error(); kernel.CloseHandle(handle); raise ctypes.WinError(error)
    if info.attributes & 0x400 or bool(info.attributes & 0x10) != directory or (not directory and info.links != 1):
        kernel.CloseHandle(handle)
        raise OSError("unsafe file handle")
    return handle


@contextmanager
def _guard_directories(path, create=False):
    path = Path(os.path.abspath(path))
    chain = list(reversed((path, *path.parents)))
    handles = []
    try:
        for part in chain:
            info = _check_path(part, allow_missing=create, directory=True)
            if info is None:
                part.mkdir()
                _check_path(part, directory=True)
            if os.name == "nt":
                # Denying delete sharing pins each ancestor until the operation ends.
                handles.append(_windows_open(part, 0x81, 3, 3, directory=True))
                _check_path(part, directory=True)
        yield
    finally:
        if os.name == "nt":
            import ctypes
            for handle in reversed(handles):
                ctypes.windll.kernel32.CloseHandle(ctypes.c_void_p(handle))


@contextmanager
def _open_checked(path, *, write=False, create=False):
    path = Path(path)
    _check_path(path, allow_missing=create, directory=False)
    if os.name == "nt":
        import msvcrt
        access = 0x80000000 | (0x40000000 if write else 0)
        handle = _windows_open(path, access, 3, 4 if create else 3)
        try:
            fd = msvcrt.open_osfhandle(handle, os.O_BINARY | (os.O_RDWR if write else os.O_RDONLY))
        except Exception:
            import ctypes
            ctypes.windll.kernel32.CloseHandle(ctypes.c_void_p(handle))
            raise
    else:
        flags = (os.O_RDWR if write else os.O_RDONLY) | getattr(os, "O_NOFOLLOW", 0)
        if create:
            flags |= os.O_CREAT
        fd = os.open(path, flags, 0o600)
    with os.fdopen(fd, "r+b" if write else "rb") as stream:
        path_info = _check_path(path, directory=False)
        handle_info = os.fstat(stream.fileno())
        if handle_info.st_nlink != 1 or not os.path.samestat(path_info, handle_info):
            raise OSError("file identity changed")
        yield stream

def _number(value):
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def _reset(value):
    if _number(value):
        return float(value) if value > 0 else None
    if not isinstance(value, str):
        return None
    try:
        date = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return date.timestamp() if date.tzinfo is not None else None
    except (ValueError, OverflowError, OSError):
        return None


def parse(payload, now=None):
    if not isinstance(payload, dict) or not isinstance(payload.get("rate_limits"), dict):
        return {}
    result = {}
    now = time.time() if now is None else now
    for name in WINDOWS:
        value = payload["rate_limits"].get(name)
        if not isinstance(value, dict):
            continue
        used, reset = value.get("used_percentage"), _reset(value.get("resets_at"))
        if _number(used) and 0 <= used <= 100 and reset is not None and now - SKEW <= reset <= now + DURATIONS[name] + SKEW:
            result[name] = {"used_percentage": used, "resets_at": reset, "receivedAt": now}
    return result


def read(path, now=None, max_age=None):
    path = Path(os.path.abspath(path))
    now = time.time() if now is None else now
    try:
        with _guard_directories(Path(path).parent), _open_checked(Path(path), write=False) as file:
            raw = file.read(MAX_BYTES + 1)
        if len(raw) > MAX_BYTES:
            return {}
        data = json.loads(raw)
        if not isinstance(data, dict) or data.get("source") != "claude-statusline":
            return {}
        result = {}
        for name in WINDOWS:
            window = data.get(name)
            if not isinstance(window, dict):
                continue
            used, reset, received = window.get("used_percentage"), window.get("resets_at"), window.get("receivedAt")
            if not (_number(used) and 0 <= used <= 100 and _number(reset) and reset > 0 and _number(received) and received - SKEW <= reset <= received + DURATIONS[name] + SKEW):
                continue
            if received > now + 60 or (max_age is not None and now - received > max_age):
                continue
            result[name] = {"used_percentage": used, "resets_at": reset, "receivedAt": received}
        return result
    except (OSError, ValueError, TypeError, OverflowError, RecursionError):
        return {}


def write(windows, root=None, now=None, deadline=None):
    """Serialize writers using a verified, non-following lock handle."""
    root = Path(os.path.abspath(root if root is not None else product.state_root()))
    now = time.time() if now is None else now
    checked = {}
    for name in WINDOWS:
        value = windows.get(name) if isinstance(windows, dict) else None
        if not isinstance(value, dict):
            continue
        used, reset, received = value.get("used_percentage"), value.get("resets_at"), value.get("receivedAt")
        if (_number(used) and 0 <= used <= 100 and _number(reset) and _number(received)
                and received <= now + 60 and received - SKEW <= reset <= received + DURATIONS[name] + SKEW):
            checked[name] = {"used_percentage": used, "resets_at": reset, "receivedAt": received}
    if not checked:
        return
    windows = checked
    deadline = time.monotonic() + DEADLINE_SECONDS if deadline is None else deadline
    with _guard_directories(root, create=True):
        with _open_checked(root / "claude-statusline.lock", write=True, create=True) as lock:
            while True:
                if time.monotonic() >= deadline:
                    raise TimeoutError("statusline deadline")
                try:
                    if os.name == "nt":
                        import msvcrt
                        lock.seek(0)
                        msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError:
                    time.sleep(min(0.02, max(0, deadline - time.monotonic())))
            try:
                if time.monotonic() >= deadline:
                    raise TimeoutError("statusline deadline")
                _write_locked(windows, root, now)
            finally:
                if os.name == "nt":
                    lock.seek(0)
                    msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(lock, fcntl.LOCK_UN)


def _write_locked(windows, root=None, now=None):
    now = time.time() if now is None else now
    root = Path(root) if root is not None else product.state_root()
    path = root / FILENAME
    previous = read(path, now)
    windows = {name: dict(value) for name, value in windows.items() if name in WINDOWS}
    for name, old in previous.items():
        windows.setdefault(name, old)
    for name, current in windows.items():
        old = previous.get(name)
        if old and abs(old["resets_at"] - current["resets_at"]) <= 60:
            if old["used_percentage"] >= current["used_percentage"]:
                windows[name] = old
        elif old and old["resets_at"] > current["resets_at"] and old["resets_at"] > now:
            windows[name] = old
    _check_path(path, allow_missing=True, directory=False)
    temp = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=root, prefix=".claude-statusline-", delete=False) as file:
            temp = file.name
            _check_path(Path(temp), directory=False)
            if not os.path.samestat(os.fstat(file.fileno()), os.lstat(temp)):
                raise OSError("temporary file changed")
            json.dump({"source": "claude-statusline", **windows}, file, separators=(",", ":"), allow_nan=False)
        _check_path(path, allow_missing=True, directory=False)
        _check_path(Path(temp), directory=False)
        os.replace(temp, path)
        temp = None
    finally:
        if temp:
            try:
                os.unlink(temp)
            except OSError:
                pass


def _read_stdin(deadline):
    result = queue.Queue(maxsize=1)
    def read_input():
        try:
            chunks, remaining = [], MAX_BYTES + 1
            while remaining:
                chunk = os.read(sys.stdin.fileno(), remaining)
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            result.put_nowait(b"".join(chunks))
        except Exception:
            result.put_nowait(None)
    threading.Thread(target=read_input, daemon=True).start()
    try:
        return result.get(timeout=max(0, deadline - time.monotonic()))
    except queue.Empty:
        return None


def main():
    deadline = time.monotonic() + DEADLINE_SECONDS
    line = ""
    try:
        raw = _read_stdin(deadline)
        if raw is None or time.monotonic() >= deadline:
            return 0
        windows = parse(json.loads(raw)) if len(raw) <= MAX_BYTES else {}
        if windows:
            write(windows, deadline=deadline)
            parts = [("5h" if name == "five_hour" else "wk") + " " + str(round(windows[name]["used_percentage"])) + "%" for name in WINDOWS if name in windows]
            line = "  ".join(parts)
    except TimeoutError:
        return 0
    except Exception:
        pass
    try:
        print(line)
    except Exception:
        pass
    return 0



def sample_paths(root=None, environ=None):
    """Real state path and the equivalent Claude MSIX redirected path."""
    env = os.environ if environ is None else environ
    root = Path(root) if root is not None else product.state_root()
    paths = [root / FILENAME]
    local = Path(env.get("LOCALAPPDATA") or product.local_app_data(env))
    try:
        relative = root.relative_to(local)
    except ValueError:
        return paths
    paths.append(local / "Packages" / "Claude_pzs8sxrjxfjjc" / "LocalCache" / "Local" / relative / FILENAME)
    return paths


def choose(a, b, now):
    """Select by window identity and usage, never observation order."""
    if not a:
        return b
    if not b:
        return a
    if abs(a["resets_at"] - b["resets_at"]) <= 60:
        av, bv = a["used_percentage"], b["used_percentage"]
        if av is None: return b if bv is not None else a
        if bv is None: return a
        return a if av >= bv else b
    return a if a["resets_at"] > b["resets_at"] else b


def merge_usage(desktop, paths=None, now=None, max_age=600):
    now = time.time() if now is None else now
    result = dict(desktop)
    samples = [read(path, now=now) for path in (sample_paths() if paths is None else paths)]
    selected_times = []
    for name, key in (("five_hour", "fiveHour"), ("seven_day", "weekly")):
        reset, used, observed = desktop.get(key), desktop.get(key + "Percent"), desktop.get("observedAt")
        best = desktop.get("_" + key + "Watermark")
        if best is None and _number(reset) and _number(observed) and observed <= now + 60 and (used is None or _number(used)):
            best = {"resets_at": reset, "used_percentage": used, "receivedAt": observed}
        for sample in samples:
            best = choose(best, sample.get(name), now)
        if best:
            result["_" + key + "Selected"] = True
            result[key] = best["resets_at"] if best["resets_at"] > now else None
            result[key + "Percent"] = (best["used_percentage"] if best["used_percentage"] is not None and best["resets_at"] > now
                                        and now - best["receivedAt"] <= max_age else None)
            if best["resets_at"] <= now:
                result["lastKnown" + key[0].upper() + key[1:]] = best["resets_at"]
            if result[key + "Percent"] is not None:
                selected_times.append(best["receivedAt"])
        elif result.get("lastKnown" + key[0].upper() + key[1:]):
            result[key + "Percent"] = None
    if selected_times:
        # A shared timestamp must not make an older selected window appear fresher.
        result["observedAt"] = min(selected_times)
    return result

if __name__ == "__main__":
    raise SystemExit(main())
