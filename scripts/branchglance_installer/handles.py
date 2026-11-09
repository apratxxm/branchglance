"""Handle-bound deletion for the owned install tree (Windows).

Why this exists: checking names for reparse points and then deleting by name is a
time-of-check/time-of-use race (a checked directory can be swapped for a junction
before a descendant is opened by its full path).

Design
- The owned root is the trust anchor. It is opened once (final component not followed,
  FILE_OPEN_REPARSE_POINT) and must not be a reparse point. Because the root is opened by
  full path, a link in any ANCESTOR would silently anchor the walk elsewhere; so right after the
  open, GetFinalPathNameByHandleW is called on the held handle and the result must equal the
  expected absolute path (links not resolved, extended-length prefix stripped, case-insensitive, no
  trailing slash). A mismatch raises WalkError naming both paths and nothing is touched. The
  handle is then held without FILE_SHARE_DELETE, so an ancestor cannot be swapped afterwards.
  A legitimately redirected profile (ancestor on a junction or subst drive) therefore fails
  closed by design. Everything below it is opened
  as a single-component name RELATIVE to a held parent directory handle
  (NtCreateFile + OBJECT_ATTRIBUTES.RootDirectory), so no later path resolution can be
  redirected. Win32 CreateFileW has no parent-handle form, hence ntdll.
- Every open uses FILE_OPEN_REPARSE_POINT, so a junction/symlink is opened as the link
  itself. Attributes and the file ID are read from the opened handle; a reparse point
  aborts the whole operation (existing policy; links are never deleted or followed).
- The delete is issued on that same handle (FileDispositionInformationEx with POSIX
  semantics; FileDispositionInformation only if the Ex class is unsupported). If neither
  works the operation fails closed. There is no delete-by-name path.
- Sharing: held directories and the object being deleted are opened FILE_SHARE_READ |
  FILE_SHARE_WRITE (no FILE_SHARE_DELETE), so while we hold them nobody can rename or
  delete them to substitute a link. Read-only scans use full sharing and release at once.
  A sharing violation (for example a running executable) aborts; it is never retried by name.
- Entries that disappear mid-walk (name not found) are skipped. Entries that appear mid-walk
  are handled by re-listing a directory until it is empty (bounded passes); a directory that
  keeps refilling fails closed.
- A pre-scan walks the whole tree first and aborts on any reparse point before anything is
  deleted; the delete pass re-checks every object it opens and compares its file ID to the
  scan, so an object swapped between scan and delete aborts.
"""
from __future__ import annotations

import os
from typing import Callable

# Test seam: called as hook(stage, display_path) with stage "open" (before a child is opened)
# or "mutate" (after it is held, before the disposition is issued).
race_hook: Callable[[str, str], None] | None = None

DELETE, SYNCHRONIZE, FILE_READ_ATTRIBUTES, FILE_LIST_DIRECTORY = 0x10000, 0x100000, 0x80, 0x1
SHARE_READ, SHARE_WRITE, SHARE_DELETE = 1, 2, 4
OPTIONS = 0x20 | 0x200000 | 0x4000  # SYNCHRONOUS_IO_NONALERT | OPEN_REPARSE_POINT | BACKUP_INTENT
ATTR_DIRECTORY, ATTR_REPARSE = 0x10, 0x400
NOT_FOUND = {0xC0000034, 0xC000003A, 0xC0000039}
ACCESS_DENIED = 0xC0000022
NOT_EMPTY = 0xC0000101
NO_MORE = {0x80000006, 0xC000000F}
UNSUPPORTED_CLASS = {0xC0000002, 0xC0000003, 0xC00000BB, 0xC000000D}
_MAX_PASSES = 8


class WalkError(RuntimeError):
    pass


def _hook(stage: str, display: str) -> None:
    if race_hook is not None:
        race_hook(stage, display)


_api_cache: dict = {}


def _api():
    if _api_cache:
        return _api_cache
    if os.name != "nt":
        raise WalkError("Handle-bound deletion requires Windows")
    import ctypes
    from ctypes import wintypes as w

    class UNICODE_STRING(ctypes.Structure):
        _fields_ = [("Length", w.USHORT), ("MaximumLength", w.USHORT), ("Buffer", ctypes.c_void_p)]

    class OBJECT_ATTRIBUTES(ctypes.Structure):
        _fields_ = [("Length", w.ULONG), ("RootDirectory", ctypes.c_void_p),
                    ("ObjectName", ctypes.POINTER(UNICODE_STRING)), ("Attributes", w.ULONG),
                    ("SecurityDescriptor", ctypes.c_void_p), ("SecurityQualityOfService", ctypes.c_void_p)]

    class IO_STATUS_BLOCK(ctypes.Structure):
        _fields_ = [("Status", ctypes.c_void_p), ("Information", ctypes.c_void_p)]

    nt = ctypes.WinDLL("ntdll")
    nt.NtCreateFile.argtypes = [ctypes.POINTER(ctypes.c_void_p), w.ULONG, ctypes.POINTER(OBJECT_ATTRIBUTES),
                                ctypes.POINTER(IO_STATUS_BLOCK), ctypes.c_void_p, w.ULONG, w.ULONG, w.ULONG,
                                w.ULONG, ctypes.c_void_p, w.ULONG]
    nt.NtCreateFile.restype = ctypes.c_long
    nt.NtClose.argtypes = [ctypes.c_void_p]
    nt.NtQueryInformationFile.argtypes = [ctypes.c_void_p, ctypes.POINTER(IO_STATUS_BLOCK), ctypes.c_void_p,
                                          w.ULONG, w.ULONG]
    nt.NtQueryInformationFile.restype = ctypes.c_long
    nt.NtSetInformationFile.argtypes = [ctypes.c_void_p, ctypes.POINTER(IO_STATUS_BLOCK), ctypes.c_void_p,
                                        w.ULONG, w.ULONG]
    nt.NtSetInformationFile.restype = ctypes.c_long
    nt.NtQueryDirectoryFile.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
                                        ctypes.POINTER(IO_STATUS_BLOCK), ctypes.c_void_p, w.ULONG, w.ULONG,
                                        w.BOOLEAN, ctypes.c_void_p, w.BOOLEAN]
    nt.NtQueryDirectoryFile.restype = ctypes.c_long
    _api_cache.update(ctypes=ctypes, w=w, nt=nt, US=UNICODE_STRING, OA=OBJECT_ATTRIBUTES, IOSB=IO_STATUS_BLOCK)
    return _api_cache


BS = chr(92)


def _normalise(path: str) -> str:
    prefix, unc = BS * 2 + "?" + BS, BS * 2 + "?" + BS + "UNC" + BS
    if path.upper().startswith(unc.upper()):
        path = BS * 2 + path[len(unc):]
    elif path.startswith(prefix):
        path = path[len(prefix):]
    return path.rstrip(BS).casefold()


def verify_anchor(handle, expected: str) -> None:
    """Require the held handle's final path to equal the expected path (no ancestor link)."""
    import ctypes
    from ctypes import wintypes
    k = ctypes.WinDLL("kernel32", use_last_error=True)
    k.GetFinalPathNameByHandleW.argtypes = [wintypes.HANDLE, wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD]
    k.GetFinalPathNameByHandleW.restype = wintypes.DWORD
    want = os.path.abspath(expected)
    size = 1024
    while True:
        buffer = ctypes.create_unicode_buffer(size)
        # VOLUME_NAME_DOS | FILE_NAME_NORMALIZED are both 0
        length = k.GetFinalPathNameByHandleW(handle, buffer, size, 0)
        if length == 0:
            raise WalkError(f"Cannot verify the location of {want} (Windows {ctypes.get_last_error()}); retained")
        if length < size:
            break
        size = length + 1
    if _normalise(buffer.value) != _normalise(want):
        raise WalkError(f"Install path is not where it claims to be: expected {want}, "
                        f"the opened object is at {buffer.value}; refusing to touch it")


def _nt_path(path: str) -> str:
    full = os.path.abspath(path)
    if full.startswith("\\\\?\\"):
        return "\\??\\" + full[4:]
    if full.startswith("\\\\"):
        return "\\??\\UNC\\" + full[2:]
    return "\\??\\" + full


def _open(parent, name: str, access: int, share: int):
    """Open one object (single component when parent is a handle). Returns (status, handle)."""
    a = _api()
    ctypes = a["ctypes"]
    buf = ctypes.create_unicode_buffer(name)
    us = a["US"](len(name) * 2, len(name) * 2 + 2, ctypes.cast(buf, ctypes.c_void_p))
    oa = a["OA"](ctypes.sizeof(a["OA"]), parent, ctypes.pointer(us), 0x40, None, None)
    handle, iosb = ctypes.c_void_p(), a["IOSB"]()
    status = a["nt"].NtCreateFile(ctypes.byref(handle), access, ctypes.byref(oa), ctypes.byref(iosb), None, 0,
                                  share, 1, OPTIONS, None, 0)
    return status & 0xFFFFFFFF, handle.value


def _close(handle) -> None:
    if handle:
        _api()["nt"].NtClose(handle)


def _identity(handle, display: str) -> tuple[int, int]:
    """(attributes, file ID) read from the opened handle."""
    a = _api()
    ctypes = a["ctypes"]
    iosb = a["IOSB"]()
    basic = (ctypes.c_ubyte * 40)()
    if a["nt"].NtQueryInformationFile(handle, ctypes.byref(iosb), basic, 40, 4) & 0xFFFFFFFF:
        raise WalkError(f"Cannot read attributes: {display}")
    attrs = int.from_bytes(bytes(basic[32:36]), "little")
    internal = (ctypes.c_ubyte * 8)()
    if a["nt"].NtQueryInformationFile(handle, ctypes.byref(iosb), internal, 8, 6) & 0xFFFFFFFF:
        raise WalkError(f"Cannot read file ID: {display}")
    return attrs, int.from_bytes(bytes(internal), "little")


def _list(handle, display: str) -> list[str]:
    a = _api()
    ctypes = a["ctypes"]
    names: list[str] = []
    buffer = (ctypes.c_ubyte * 65536)()
    restart = True
    while True:
        iosb = a["IOSB"]()
        status = a["nt"].NtQueryDirectoryFile(handle, None, None, None, ctypes.byref(iosb), buffer, 65536, 12,
                                              False, None, restart) & 0xFFFFFFFF
        restart = False
        if status in NO_MORE:
            return names
        if status:
            raise WalkError(f"Cannot enumerate {display} (NTSTATUS {status:#x})")
        data = bytes(buffer)
        offset = 0
        while True:
            nxt = int.from_bytes(data[offset:offset + 4], "little")
            length = int.from_bytes(data[offset + 8:offset + 12], "little")
            name = data[offset + 12:offset + 12 + length].decode("utf-16-le")
            if name not in (".", ".."):
                names.append(name)
            if not nxt:
                break
            offset += nxt


def _dispose(handle, display: str) -> int:
    """Delete the object behind this very handle; returns the NTSTATUS only for 'directory not empty'."""
    a = _api()
    ctypes = a["ctypes"]
    iosb = a["IOSB"]()
    flags = ctypes.c_ulong(3)  # FILE_DISPOSITION_DELETE | FILE_DISPOSITION_POSIX_SEMANTICS
    status = a["nt"].NtSetInformationFile(handle, ctypes.byref(iosb), ctypes.byref(flags), 4, 64) & 0xFFFFFFFF
    if status in UNSUPPORTED_CLASS:
        legacy = ctypes.c_ubyte(1)
        iosb = a["IOSB"]()
        status = a["nt"].NtSetInformationFile(handle, ctypes.byref(iosb), ctypes.byref(legacy), 1, 13) & 0xFFFFFFFF
    if status == NOT_EMPTY:
        return status
    if status:
        raise WalkError(f"Cannot delete through the held handle: {display} (NTSTATUS {status:#x})")
    return 0


def _open_child(parent, name: str, display: str, *, access: int, share: int, listing: bool):
    """Open a child relative to a held directory; None when it no longer exists."""
    _hook("open", display)
    base = FILE_READ_ATTRIBUTES | SYNCHRONIZE | access
    status, handle = _open(parent, name, base | (FILE_LIST_DIRECTORY if listing else 0), share)
    if status == ACCESS_DENIED and listing:
        status, handle = _open(parent, name, base, share)  # a file we may delete but not read
    if status in NOT_FOUND:
        return None
    if status:
        raise WalkError(f"Cannot open {display} without following links (NTSTATUS {status:#x}); retained")
    return handle


def _scan(dirh, display: str, ids: dict) -> None:
    for name in _list(dirh, display):
        child = os.path.join(display, name)
        handle = _open_child(dirh, name, child, access=0, share=SHARE_READ | SHARE_WRITE | SHARE_DELETE, listing=True)
        if handle is None:
            continue
        try:
            attrs, fid = _identity(handle, child)
            if attrs & ATTR_REPARSE:
                raise WalkError(f"Reparse point refused: {child}")
            ids[child] = fid
            if attrs & ATTR_DIRECTORY:
                _scan(handle, child, ids)
        finally:
            _close(handle)


def _delete_dir(dirh, display: str, ids: dict) -> None:
    for _ in range(_MAX_PASSES):
        for name in _list(dirh, display):
            child = os.path.join(display, name)
            handle = _open_child(dirh, name, child, access=DELETE, share=SHARE_READ | SHARE_WRITE, listing=True)
            if handle is None:
                continue  # vanished mid-walk
            try:
                attrs, fid = _identity(handle, child)
                if attrs & ATTR_REPARSE:
                    raise WalkError(f"Reparse point refused: {child}")
                if ids.get(child, fid) != fid:
                    raise WalkError(f"Object changed between scan and delete: {child}")
                if attrs & ATTR_DIRECTORY:
                    _delete_dir(handle, child, ids)
                else:
                    _hook("mutate", child)
                    _dispose(handle, child)
            finally:
                _close(handle)
        _hook("mutate", display)
        if _dispose(dirh, display) == 0:
            return
        # Directory not empty: something appeared after the listing; list again.
    raise WalkError(f"Directory kept changing during deletion: {display}")


def remove_tree(path: str) -> bool:
    """Delete an owned directory tree through handles. False when it does not exist."""
    status, root = _open(None, _nt_path(path), DELETE | FILE_LIST_DIRECTORY | FILE_READ_ATTRIBUTES | SYNCHRONIZE,
                         SHARE_READ | SHARE_WRITE)
    if status in NOT_FOUND:
        return False
    if status:
        raise WalkError(f"Cannot open {path} without following links (NTSTATUS {status:#x}); retained")
    try:
        verify_anchor(root, path)
        attrs, _ = _identity(root, path)
        if attrs & ATTR_REPARSE or not attrs & ATTR_DIRECTORY:
            raise WalkError(f"Reparse point refused: {path}")
        ids: dict = {}
        _scan(root, str(path), ids)
        _delete_dir(root, str(path), ids)
        return True
    finally:
        _close(root)


def remove_file(path: str) -> bool:
    """Delete one ordinary file through a handle opened relative to its parent directory."""
    parent_path, name = os.path.split(os.path.abspath(path))
    status, parent = _open(None, _nt_path(parent_path), FILE_LIST_DIRECTORY | FILE_READ_ATTRIBUTES | SYNCHRONIZE,
                           SHARE_READ | SHARE_WRITE | SHARE_DELETE)
    if status in NOT_FOUND:
        return False
    if status:
        raise WalkError(f"Cannot open {parent_path} without following links (NTSTATUS {status:#x})")
    try:
        verify_anchor(parent, parent_path)
        attrs, _ = _identity(parent, parent_path)
        if attrs & ATTR_REPARSE or not attrs & ATTR_DIRECTORY:
            raise WalkError(f"Reparse point refused: {parent_path}")
        handle = _open_child(parent, name, str(path), access=DELETE, share=SHARE_READ | SHARE_WRITE, listing=False)
        if handle is None:
            return False
        try:
            attrs, _ = _identity(handle, str(path))
            if attrs & (ATTR_REPARSE | ATTR_DIRECTORY):
                raise WalkError(f"Unsafe owned install file: {path}")
            _hook("mutate", str(path))
            _dispose(handle, str(path))
            return True
        finally:
            _close(handle)
    finally:
        _close(parent)
