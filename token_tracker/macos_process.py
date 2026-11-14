"""macOS process facts from libproc and sysctl, shaped like the Windows helpers.

Creation times are returned as Windows FILETIME ticks (100 ns since 1601) so
records, receipts and comparisons stay identical across platforms.
"""
from __future__ import annotations

import ctypes
import ctypes.util
import os
import struct
import sys

_EPOCH_DELTA = 11644473600  # seconds from 1601-01-01 to 1970-01-01
_PROC_PIDTBSDINFO = 3
_CTL_KERN, _KERN_ARGMAX, _KERN_PROCARGS2 = 1, 8, 49
_ESRCH = 3


class _BsdInfo(ctypes.Structure):
    _fields_ = [("flags", ctypes.c_uint32), ("status", ctypes.c_uint32), ("xstatus", ctypes.c_uint32),
                ("pid", ctypes.c_uint32), ("ppid", ctypes.c_uint32),
                ("uid", ctypes.c_uint32), ("gid", ctypes.c_uint32), ("ruid", ctypes.c_uint32),
                ("rgid", ctypes.c_uint32), ("svuid", ctypes.c_uint32), ("svgid", ctypes.c_uint32),
                ("rfu_1", ctypes.c_uint32), ("comm", ctypes.c_char * 16), ("name", ctypes.c_char * 32),
                ("nfiles", ctypes.c_uint32), ("pgid", ctypes.c_uint32), ("pjobc", ctypes.c_uint32),
                ("e_tdev", ctypes.c_uint32), ("e_tpgid", ctypes.c_uint32), ("nice", ctypes.c_int32),
                ("start_tvsec", ctypes.c_uint64), ("start_tvusec", ctypes.c_uint64)]


_libc = None


def _lib():
    global _libc
    if _libc is None:
        if sys.platform != "darwin":
            raise OSError("libproc is macOS-only")
        _libc = ctypes.CDLL(ctypes.util.find_library("c") or "libc.dylib", use_errno=True)
        _libc.proc_listallpids.argtypes = [ctypes.c_void_p, ctypes.c_int]
        _libc.proc_pidinfo.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_uint64, ctypes.c_void_p, ctypes.c_int]
        _libc.proc_pidpath.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32]
        _libc.sysctl.argtypes = [ctypes.POINTER(ctypes.c_int), ctypes.c_uint, ctypes.c_void_p,
                                 ctypes.POINTER(ctypes.c_size_t), ctypes.c_void_p, ctypes.c_size_t]
    return _libc


def _info(pid: int) -> _BsdInfo | None:
    info = _BsdInfo()
    size = ctypes.sizeof(info)
    if _lib().proc_pidinfo(pid, _PROC_PIDTBSDINFO, 0, ctypes.byref(info), size) != size:
        return None
    return info


def _ticks(info: _BsdInfo) -> int:
    return (int(info.start_tvsec) + _EPOCH_DELTA) * 10_000_000 + int(info.start_tvusec) * 10


def _exe_name(pid: int, info: _BsdInfo) -> str:
    buffer = ctypes.create_string_buffer(4096)
    if _lib().proc_pidpath(pid, buffer, len(buffer)) > 0:
        return os.path.basename(buffer.value.decode("utf-8", "ignore")).lower()
    return info.comm.decode("utf-8", "ignore").lower()


def process_table() -> dict[int, tuple[int, str]]:
    """pid -> (parent pid, lowercase executable name), like the Toolhelp32 snapshot."""
    lib = _lib()
    count = lib.proc_listallpids(None, 0)
    if count <= 0:
        return {}
    pids = (ctypes.c_int * (count + 64))()
    count = lib.proc_listallpids(pids, ctypes.sizeof(pids))
    table: dict[int, tuple[int, str]] = {}
    for pid in pids[:max(count, 0)]:
        if pid <= 0:
            continue
        info = _info(pid)
        if info is not None:
            table[int(pid)] = (int(info.ppid), _exe_name(pid, info))
    return table


def probe_process(pid: int) -> tuple[str, int | None]:
    """("alive", creation ticks) | ("gone", None) | ("unknown", None)."""
    if not isinstance(pid, int) or pid <= 0:
        return "unknown", None
    try:
        info = _info(pid)
        if info is None:
            return ("gone", None) if ctypes.get_errno() == _ESRCH else ("unknown", None)
        return "alive", _ticks(info)
    except Exception:
        return "unknown", None


def process_uid(pid: int) -> int | None:
    try:
        info = _info(pid) if isinstance(pid, int) and pid > 0 else None
        return int(info.uid) if info is not None else None
    except Exception:
        return None


def command_line_argv(pid: int) -> list[str] | None:
    """Return the structured argv from KERN_PROCARGS2 without joining tokens."""
    if not isinstance(pid, int) or pid <= 0:
        return None
    try:
        lib = _lib()
        argmax = ctypes.c_int()
        size = ctypes.c_size_t(ctypes.sizeof(argmax))
        mib = (ctypes.c_int * 2)(_CTL_KERN, _KERN_ARGMAX)
        if lib.sysctl(mib, 2, ctypes.byref(argmax), ctypes.byref(size), None, 0) != 0:
            return None
        buffer = ctypes.create_string_buffer(argmax.value)
        size = ctypes.c_size_t(argmax.value)
        mib = (ctypes.c_int * 3)(_CTL_KERN, _KERN_PROCARGS2, pid)
        if lib.sysctl(mib, 3, buffer, ctypes.byref(size), None, 0) != 0:
            return None
        raw = buffer.raw[:size.value]
        argc = struct.unpack_from("i", raw)[0]
        rest = raw[4:]
        exe_end = rest.find(b"\0")
        if exe_end < 0:
            return None
        rest = rest[exe_end:].lstrip(b"\0")
        args = rest.split(b"\0")[:argc]
        if len(args) != argc:
            return None
        return [arg.decode("utf-8", "replace") for arg in args]
    except Exception:
        return None


def command_line(pid: int) -> str | None:
    """The argv joined with spaces (Windows-style quoting not applied), from KERN_PROCARGS2."""
    args = command_line_argv(pid)
    return " ".join(args) if args is not None else None
