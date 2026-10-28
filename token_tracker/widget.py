from __future__ import annotations

import ctypes
import argparse
import json
import logging
import math
import os
import secrets
import socket
import threading
import time
import unicodedata
from contextlib import suppress, contextmanager
from collections import deque
from pathlib import Path

import pystray
import webview
from PIL import Image

from .limit_reset import LimitResetService, _runtime_dir
from .server import CACHE, Handler, ThreadingHTTPServer
from . import product, reset_delivery, server as server_module


_WAIT_OBJECT_0 = 0x0
_WAIT_ABANDONED = 0x80
_WAIT_TIMEOUT = 0x102


def claim_install_mutex(timeout: float = 20.0) -> tuple[object | None, str]:
    """Wait briefly for setup to finish before importing the frozen policy snapshot."""
    if not product.is_frozen():
        return None, "acquired"
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
        kernel32.WaitForSingleObject.restype = ctypes.c_ulong
        kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        handle = product.create_secured_mutex(product.INSTALL_MUTEX_NAME)
    except (AttributeError, OSError):
        return None, "failed"
    result = kernel32.WaitForSingleObject(handle, max(0, min(int(timeout * 1000), 0xFFFFFFFE)))
    if result == _WAIT_OBJECT_0:
        return handle, "acquired"
    if result == _WAIT_ABANDONED:
        product.release_secured_mutex(handle)
        return None, "abandoned"
    kernel32.CloseHandle(handle)
    return None, "busy" if result == _WAIT_TIMEOUT else "failed"


def claim_instance_mutex(name: str | None = None) -> tuple[object | None, str]:
    """Try to own the per-session single-instance mutex, without waiting.

    Opening an existing mutex says nothing about ownership (another process
    may only hold a handle), so ownership is decided by a zero-time wait.
    Returns ``(handle, status)`` where status is ``acquired``, ``abandoned``
    (the previous owner died while holding it: we now own it and must recover),
    ``busy`` (a live owner: another Branchglance or the installer) or ``failed``.
    The handle is returned only when the mutex is owned.
    """
    name = name or product.INSTANCE_MUTEX_NAME
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    except (AttributeError, OSError):
        return None, "acquired"  # not Windows: no named-mutex facility
    kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p]
    kernel32.CreateMutexW.restype = ctypes.c_void_p
    kernel32.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
    kernel32.WaitForSingleObject.restype = ctypes.c_ulong
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    try:
        handle = product.create_secured_mutex(name)
    except (AttributeError, OSError):
        return None, "failed"
    if not handle:
        return None, "failed"
    result = kernel32.WaitForSingleObject(handle, 0)
    if result == _WAIT_OBJECT_0:
        return handle, "acquired"
    if result == _WAIT_ABANDONED:
        return handle, "abandoned"
    kernel32.CloseHandle(handle)
    return None, "busy" if result == _WAIT_TIMEOUT else "failed"


def acquire_instance_mutex(name: str | None = None) -> tuple[object | None, bool]:
    """``(handle, owned)`` view of :func:`claim_instance_mutex`; abandoned counts as owned."""
    handle, status = claim_instance_mutex(name)
    return (handle, True) if status in ("acquired", "abandoned") else (None, False)


def recover_abandoned_instance() -> None:
    """The previous instance died holding the mutex: discard what only it could have cleaned up."""
    logging.getLogger(__name__).warning("Previous Branchglance instance ended without releasing its lock; recovering")
    with suppress(OSError):
        product.control_nonce_path().unlink()


def show_message(title: str, text: str) -> None:
    """Visible, blocking notice for failures that happen before any window exists."""
    logging.getLogger(__name__).error("%s: %s", title, text)
    try:
        ctypes.windll.user32.MessageBoxW(None, text, title, 0x40)  # MB_OK | MB_ICONINFORMATION
    except (AttributeError, OSError):
        pass


_LineReader = product.LineReader
CONTROL_REPLY_LIMIT = product.CONTROL_REPLY_LIMIT


def _control_request(line: str, timeout: float = 0.6) -> str | None:
    """One line out, one line back, against the control port. None on any failure."""
    return product.control_request(line, port=CONTROL_PORT, timeout=timeout)


def control_command(command: str, nonce: str | None = None, timeout: float = 0.6) -> str | None:
    """Authenticated command to this session's app (see :func:`product.control_command`)."""
    nonce = read_control_nonce() if nonce is None else nonce
    return product.control_command(command, nonce, port=CONTROL_PORT, timeout=timeout)


def read_control_nonce() -> str | None:
    return product.read_nonce_file(product.control_nonce_path())


def _warm_snapshot() -> None:
    try:
        CACHE.get()
    except Exception:
        logging.getLogger(__name__).exception("Branchglance snapshot warmup failed")


class _StartupCancelled(Exception):
    """A shutdown (maintenance-exit) was accepted before startup finished."""


HTTP_HOST = "127.0.0.1"
_INSTANCE_HANDLE = None
HTTP_PORT = 8765
CONTROL_PORT = 8766
# A just-closed instance can leave the listening ports unavailable for a moment
# (TIME_WAIT); retry a bounded number of times, then fail visibly.
BIND_RETRY_ATTEMPTS = 20
BIND_RETRY_DELAY = 0.25
CONTROL_LINE_LIMIT = 160
WM_HOTKEY = 0x0312
WM_QUIT = 0x0012
PM_NOREMOVE = 0x0000
MOD_ALT, MOD_CONTROL, MOD_SHIFT, MOD_NOREPEAT = 0x0001, 0x0002, 0x0004, 0x4000
VK_T = 0x54
INSPECTION_HOTKEY_ID = 0x544B
INSPECTION_HOTKEY_STOP_SECONDS = 1.0
WINDOW_WIDTH = 420
# Main cards fit this opening size; the chat list takes the remaining space.
WINDOW_HEIGHT = 880
WINDOW_CORNER_RADIUS = 20
# Windows 11 DWM attributes: compositor-drawn rounded corners clip WebView2 content
# (a GDI window region does not) and the border colour "none" removes the 1px outline.
DWMWA_WINDOW_CORNER_PREFERENCE = 33
DWMWA_BORDER_COLOR = 34
DWMWCP_DONOTROUND = 1
DWMWCP_ROUND = 2
DWMWA_COLOR_NONE = 0xFFFFFFFE


def _set_dwm_attribute(dwmapi, hwnd_ptr, attribute: int, value: int) -> bool:
    """DwmSetWindowAttribute with a DWORD value; False when unsupported (pre-Windows 11)."""
    if dwmapi is None:
        return False
    data = ctypes.c_uint32(value)
    try:
        return dwmapi.DwmSetWindowAttribute(hwnd_ptr, attribute, ctypes.byref(data), ctypes.sizeof(data)) == 0
    except (AttributeError, OSError, ctypes.ArgumentError):
        return False


class _Rect(ctypes.Structure):
    """Win32 RECT, kept local so this module remains importable off Windows."""

    _fields_ = [
        ("left", ctypes.c_long),
        ("top", ctypes.c_long),
        ("right", ctypes.c_long),
        ("bottom", ctypes.c_long),
    ]


@contextmanager
def _physical_coordinates():
    """Use one coordinate space for native rectangles on mixed-DPI displays."""
    previous = None
    try:
        user32 = ctypes.windll.user32
        user32.SetThreadDpiAwarenessContext.argtypes = [ctypes.c_void_p]
        user32.SetThreadDpiAwarenessContext.restype = ctypes.c_void_p
        previous = user32.SetThreadDpiAwarenessContext(ctypes.c_void_p(-4))
    except (AttributeError, OSError, ctypes.ArgumentError):
        pass
    try:
        yield
    finally:
        if previous:
            with suppress(AttributeError, OSError, ctypes.ArgumentError):
                user32.SetThreadDpiAwarenessContext(previous)


@_physical_coordinates()
def _work_area(hwnd: int | None = None) -> tuple[int, int, int, int]:
    """Usable monitor bounds, excluding the taskbar (including secondary screens)."""
    user32 = ctypes.windll.user32
    try:
        if hwnd:
            class MonitorInfo(ctypes.Structure):
                _fields_ = [("size", ctypes.c_ulong), ("monitor", _Rect),
                            ("work", _Rect), ("flags", ctypes.c_ulong)]
            user32.MonitorFromWindow.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
            user32.MonitorFromWindow.restype = ctypes.c_void_p
            user32.GetMonitorInfoW.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
            info = MonitorInfo()
            info.size = ctypes.sizeof(info)
            monitor = user32.MonitorFromWindow(hwnd, 2)  # nearest monitor
            if monitor and user32.GetMonitorInfoW(monitor, ctypes.byref(info)):
                r = info.work
                return r.left, r.top, r.right, r.bottom
        rect = _Rect()
        if user32.SystemParametersInfoW(0x0030, 0, ctypes.byref(rect), 0):
            return rect.left, rect.top, rect.right, rect.bottom
    except (AttributeError, OSError):
        pass
    return 0, 0, user32.GetSystemMetrics(0), user32.GetSystemMetrics(1)


MIN_WINDOW_WIDTH, MIN_WINDOW_HEIGHT = 300, 600
BOUNDS_SAVE_DELAY_SECONDS = 0.6
NOTCH_WIDTH = 680
NOTCH_COLLAPSED_HEIGHT = 148
NOTCH_MIN_HEIGHT, NOTCH_DEFAULT_HEIGHT = NOTCH_COLLAPSED_HEIGHT, 560
COMPACT_NOTCH_MODES = {"idle", "rest", "controls", "inspect", "dashboard"}
COMPACT_NOTCH_SIZES = {"idle": (328, 100), "rest": (328, 100),
                       "controls": (328, 110), "inspect": (560, 114),
                       "dashboard": (720, 608)}
COMPACT_INSPECT_MAX_HEIGHT = 420
COMPACT_DRAG_LEASE_SECONDS = 60
COMPACT_DRAG_WATCHDOG_SECONDS = 0.10
# Keep the open movement proportional to the chat-list fade delay (midpoint).
COMPACT_NOTCH_TRANSITION_SECONDS = 0.286
COMPACT_NOTCH_CLOSE_TRANSITION_SECONDS = 0.22
NOTCH_TRANSITION_SECONDS = 0.125
NOTCH_OPEN_TRANSITION_SECONDS = 0.182
NOTCH_FRAME_INTERVAL = 1 / 120
NOTCH_EVENT_GUARD_SECONDS = 1.0


def _window_state_path() -> Path:
    return _runtime_dir() / "window-state.json"


def _load_window_bounds(path: Path) -> dict | None:
    """Last normal (un-maximized) bounds in pywebview logical pixels, if valid."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    bounds = {}
    for key in ("x", "y", "width", "height"):
        value = data.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            return None
        bounds[key] = int(value)
    if bounds["width"] < 100 or bounds["height"] < 100:
        return None
    bounds["maximized"] = data.get("maximized") is True
    return bounds


# Placement, lock, hotkey and monitor-name writes all merge into one file from
# different threads; the whole read-merge-write must not interleave.
_WINDOW_STATE_LOCK = threading.Lock()


def _save_window_bounds(path: Path, bounds: dict) -> None:
    with _WINDOW_STATE_LOCK:
        state = {}
        try:
            saved = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(saved, dict):
                state.update(saved)
        except (OSError, ValueError):
            pass
        state.update(bounds)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
        try:
            temporary.write_text(json.dumps(state, sort_keys=True), encoding="utf-8")
            os.replace(temporary, path)
        finally:
            with suppress(OSError):
                temporary.unlink()


def _system_scale() -> float:
    try:
        user32 = ctypes.windll.user32
        return (user32.GetDpiForSystem() or 96) / 96 if hasattr(user32, "GetDpiForSystem") else 1.0
    except (AttributeError, OSError):
        return 1.0


def _monitor_work_areas(physical: bool = False) -> list[tuple[int, int, int, int]]:
    """Work areas of every connected monitor.

    Logical pixels (what pywebview uses) by default; physical pixels, the space of
    GetWindowRect/SetWindowPos and of the saved bounds, with ``physical=True``.
    """
    areas: list[tuple[int, int, int, int]] = []
    try:
        user32 = ctypes.windll.user32

        class MonitorInfo(ctypes.Structure):
            _fields_ = [("size", ctypes.c_ulong), ("monitor", _Rect), ("work", _Rect), ("flags", ctypes.c_ulong)]

        callback_type = ctypes.WINFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(_Rect), ctypes.c_long)
        scale = 1.0 if physical else _system_scale()

        def collect(monitor, _dc, _rect, _data):
            info = MonitorInfo()
            info.size = ctypes.sizeof(info)
            if user32.GetMonitorInfoW(ctypes.c_void_p(monitor), ctypes.byref(info)):
                r = info.work
                areas.append((round(r.left / scale), round(r.top / scale), round(r.right / scale), round(r.bottom / scale)))
            return 1

        user32.EnumDisplayMonitors(None, None, callback_type(collect), 0)
    except (AttributeError, OSError, ValueError):
        return []
    return areas


class _DisplayConfigLuid(ctypes.Structure):
    _fields_ = [("low_part", ctypes.c_uint32), ("high_part", ctypes.c_int32)]


class _DisplayConfigPathSourceInfo(ctypes.Structure):
    _fields_ = [("adapter_id", _DisplayConfigLuid), ("id", ctypes.c_uint32),
                ("mode_info_idx", ctypes.c_uint32), ("status_flags", ctypes.c_uint32)]


class _DisplayConfigPathTargetInfo(ctypes.Structure):
    _fields_ = [("adapter_id", _DisplayConfigLuid), ("id", ctypes.c_uint32),
                ("mode_info_idx", ctypes.c_uint32), ("output_technology", ctypes.c_uint32),
                ("rotation", ctypes.c_uint32), ("scaling", ctypes.c_uint32),
                ("refresh_rate_numerator", ctypes.c_uint32),
                ("refresh_rate_denominator", ctypes.c_uint32),
                ("scan_line_ordering", ctypes.c_uint32), ("target_available", ctypes.c_int32),
                ("status_flags", ctypes.c_uint32)]


class _DisplayConfigPathInfo(ctypes.Structure):
    _fields_ = [("source_info", _DisplayConfigPathSourceInfo),
                ("target_info", _DisplayConfigPathTargetInfo), ("flags", ctypes.c_uint32)]


class _DisplayConfigModeUnion(ctypes.Union):
    _fields_ = [("alignment", ctypes.c_uint64), ("data", ctypes.c_byte * 48)]


class _DisplayConfigModeInfo(ctypes.Structure):
    _fields_ = [("info_type", ctypes.c_uint32), ("id", ctypes.c_uint32),
                ("adapter_id", _DisplayConfigLuid), ("data", _DisplayConfigModeUnion)]


class _DisplayConfigDeviceInfoHeader(ctypes.Structure):
    _fields_ = [("info_type", ctypes.c_uint32), ("size", ctypes.c_uint32),
                ("adapter_id", _DisplayConfigLuid), ("id", ctypes.c_uint32)]


class _DisplayConfigSourceNameInfo(ctypes.Structure):
    _fields_ = [("header", _DisplayConfigDeviceInfoHeader),
                ("view_gdi_device_name", ctypes.c_wchar * 32)]


class _DisplayConfigTargetNameInfo(ctypes.Structure):
    _fields_ = [("header", _DisplayConfigDeviceInfoHeader), ("flags", ctypes.c_uint32),
                ("output_technology", ctypes.c_uint32), ("edid_manufacturer_id", ctypes.c_uint16),
                ("edid_product_code_id", ctypes.c_uint16), ("connector_instance", ctypes.c_uint32),
                ("monitor_friendly_device_name", ctypes.c_wchar * 64),
                ("monitor_device_path", ctypes.c_wchar * 128)]


class _WinPoint(ctypes.Structure):
    _fields_ = [("x", ctypes.c_int32), ("y", ctypes.c_int32)]


class _WinMessage(ctypes.Structure):
    _fields_ = [("hwnd", ctypes.c_void_p), ("message", ctypes.c_uint32),
                ("wparam", ctypes.c_size_t), ("lparam", ctypes.c_ssize_t),
                ("time", ctypes.c_uint32), ("point", _WinPoint), ("private", ctypes.c_uint32)]


def _display_source_targets() -> dict[str, list[dict]]:
    """Return active GDI sources and target connector/path records from Windows."""
    try:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        get_sizes = user32.GetDisplayConfigBufferSizes
        get_sizes.argtypes = [ctypes.c_uint32, ctypes.POINTER(ctypes.c_uint32), ctypes.POINTER(ctypes.c_uint32)]
        get_sizes.restype = ctypes.c_int32
        query = user32.QueryDisplayConfig
        query.argtypes = [ctypes.c_uint32, ctypes.POINTER(ctypes.c_uint32),
                          ctypes.POINTER(_DisplayConfigPathInfo), ctypes.POINTER(ctypes.c_uint32),
                          ctypes.POINTER(_DisplayConfigModeInfo), ctypes.c_void_p]
        query.restype = ctypes.c_int32
        get_device_info = user32.DisplayConfigGetDeviceInfo
        get_device_info.argtypes = [ctypes.POINTER(_DisplayConfigDeviceInfoHeader)]
        get_device_info.restype = ctypes.c_int32

        flags = 0x00000002  # QDC_ONLY_ACTIVE_PATHS
        for _attempt in range(2):  # a single bounded retry handles a hot-plug between the two calls
            path_count, mode_count = ctypes.c_uint32(), ctypes.c_uint32()
            if get_sizes(flags, ctypes.byref(path_count), ctypes.byref(mode_count)) != 0:
                return {}
            paths = (_DisplayConfigPathInfo * path_count.value)()
            modes = (_DisplayConfigModeInfo * mode_count.value)()
            actual_paths, actual_modes = ctypes.c_uint32(path_count.value), ctypes.c_uint32(mode_count.value)
            result = query(flags, ctypes.byref(actual_paths), paths, ctypes.byref(actual_modes), modes, None)
            if result == 122:  # ERROR_INSUFFICIENT_BUFFER after a display change
                continue
            if result != 0:
                return {}

            sources: dict[str, list[dict]] = {}
            for path in paths[:actual_paths.value]:
                name = _DisplayConfigSourceNameInfo()
                name.header = _DisplayConfigDeviceInfoHeader(1, ctypes.sizeof(name),
                                                             path.source_info.adapter_id,
                                                             path.source_info.id)
                if get_device_info(ctypes.byref(name.header)) != 0 or not name.view_gdi_device_name:
                    continue
                target = _DisplayConfigTargetNameInfo()
                target.header = _DisplayConfigDeviceInfoHeader(2, ctypes.sizeof(target),
                                                               path.target_info.adapter_id,
                                                               path.target_info.id)
                device_path = None
                if get_device_info(ctypes.byref(target.header)) == 0 and target.monitor_device_path:
                    device_path = target.monitor_device_path
                sources.setdefault(name.view_gdi_device_name, []).append({
                    "output_technology": int(path.target_info.output_technology),
                    "device_path": device_path,
                })
            return sources
        return {}
    except (AttributeError, OSError, ValueError, ctypes.ArgumentError):
        return {}


def _display_source_output_technologies() -> dict[str, set[int]]:
    """Compatibility view used by diagnostics and focused classification tests."""
    return {source: {target["output_technology"] for target in targets}
            for source, targets in _display_source_targets().items()}


def _display_stable_ids_by_source(source_targets: dict[str, list[dict]]) -> dict[str, str]:
    """Use a physical path only when it maps one-to-one to one active GDI source."""
    paths_by_source = {}
    for source, targets in source_targets.items():
        paths = [target.get("device_path") for target in targets]
        if not paths or any(not isinstance(path, str) or not path for path in paths):
            continue
        paths_by_source[source] = {path.casefold(): path for path in paths}
    sources_by_path: dict[str, set[str]] = {}
    for source, paths in paths_by_source.items():
        for normalized_path in paths:
            sources_by_path.setdefault(normalized_path, set()).add(source)
    return {source: next(iter(paths.values())) for source, paths in paths_by_source.items()
            if len(paths) == 1 and len(sources_by_path[next(iter(paths))]) == 1}


def _monitor_key(value) -> str:
    return value.casefold() if isinstance(value, str) else ""


def _is_stable_monitor_id(value) -> bool:
    """DisplayConfig monitor device paths have this prefix; GDI aliases do not."""
    return _monitor_key(value).startswith("\\\\?\\display#")


def _display_kinds_by_device(source_technologies: dict[str, set[int]]) -> dict[str, str]:
    """Classify only unambiguous source-to-target identities; unknowns stay neutral."""
    internal = {0x80000000, 6, 11, 13}
    external = {0, 1, 2, 3, 4, 5, 8, 9, 10, 12, 14, 15, 16, 17, 18}
    kinds = {}
    for source, technologies in source_technologies.items():
        target_kinds = {"internal" if value in internal else "external" if value in external else None
                        for value in technologies}
        if len(target_kinds) == 1 and None not in target_kinds:
            kinds[source] = target_kinds.pop()
    return kinds


@_physical_coordinates()
def _monitor_descriptors() -> list[dict]:
    """Stable display device IDs, physical work areas and each display's own scale."""
    monitors = []
    try:
        user32 = ctypes.windll.user32
        user32.GetMonitorInfoW.argtypes = [ctypes.c_void_p, ctypes.c_void_p]

        class MonitorInfo(ctypes.Structure):
            _fields_ = [("size", ctypes.c_ulong), ("monitor", _Rect),
                        ("work", _Rect), ("flags", ctypes.c_ulong),
                        ("device", ctypes.c_wchar * 32)]

        callback_type = ctypes.WINFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p,
                                           ctypes.POINTER(_Rect), ctypes.c_ssize_t)

        def collect(handle, _dc, _rect, _data):
            info = MonitorInfo()
            info.size = ctypes.sizeof(info)
            if user32.GetMonitorInfoW(ctypes.c_void_p(handle), ctypes.byref(info)):
                scale = _system_scale()
                with suppress(AttributeError, OSError, ctypes.ArgumentError):
                    factor = ctypes.c_int()
                    if ctypes.windll.shcore.GetScaleFactorForMonitor(ctypes.c_void_p(handle), ctypes.byref(factor)) == 0 and factor.value:
                        scale = factor.value / 100
                r = info.work
                monitors.append({"id": info.device, "primary": bool(info.flags & 1),
                                 "scale": scale, "area": (r.left, r.top, r.right, r.bottom)})
            return 1

        user32.EnumDisplayMonitors(None, None, callback_type(collect), 0)
    except (AttributeError, OSError, ValueError):
        pass
    if not monitors:
        monitors = [{"id": "primary", "primary": True, "scale": _system_scale(), "area": _work_area()}]
    # Match GDI sources to active targets; only a one-to-one target path is a persisted identity.
    source_targets = _display_source_targets()
    technologies = {source: {target["output_technology"] for target in targets}
                    for source, targets in source_targets.items()}
    kinds = _display_kinds_by_device(technologies)
    stable_ids = _display_stable_ids_by_source(source_targets)
    monitors.sort(key=lambda monitor: monitor["id"])
    for index, monitor in enumerate(monitors, 1):
        gdi_id = monitor["id"]
        kind = kinds.get(gdi_id)
        stable_id = stable_ids.get(gdi_id)
        monitor.update(id=stable_id or gdi_id, stableId=stable_id, gdiId=gdi_id, index=index,
                       label="Laptop" if kind == "internal" else "Monitor" if kind == "external"
                       else f"Monitor {index}")
        if kind is not None:
            monitor["kind"] = kind
    return monitors


def _load_notch_placement(path: Path) -> dict:
    try:
        placement = json.loads(path.read_text(encoding="utf-8")).get("notch", {})
        if not isinstance(placement, dict):
            return {}
        valid = {"monitor_id": placement.get("monitor_id")} if isinstance(placement.get("monitor_id"), str) else {}
        if placement.get("manual") is True and all(
                isinstance(placement.get(key), (int, float)) and not isinstance(placement[key], bool)
                and math.isfinite(placement[key]) for key in ("x", "y")):
            valid.update(manual=True, x=round(placement["x"]), y=round(placement["y"]))
            reference_width = placement.get("reference_width")
            if type(reference_width) is int and reference_width > 0:
                valid["reference_width"] = reference_width
        return valid
    except (OSError, ValueError, AttributeError):
        return {}


def _monitor_names(path: Path) -> dict[str, str]:
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
        values = state.get("monitor_names", {}) if isinstance(state, dict) else {}
        if not isinstance(values, dict):
            return {}
        return {key.casefold(): value.strip() for key, value in values.items()
                if _is_stable_monitor_id(key) and isinstance(value, str)
                and _valid_monitor_name(value) is not None}
    except (OSError, ValueError, TypeError):
        return {}


def _valid_monitor_name(value) -> str | None:
    if not isinstance(value, str):
        return None
    name = value.strip()
    # Control, format (bidi overrides, zero-width) and line/paragraph separators
    # could reorder or hide text in the Settings list.
    if not name or len(name) > 32 or any(unicodedata.category(char) in {"Cc", "Cf", "Zl", "Zp"} for char in name):
        return None
    return name


def _load_widget_lock(path: Path) -> dict[str, bool]:
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(state, dict):
            return {"locked": False, "expanded": False, "keep_open": False}
        return {"locked": state.get("widget_locked") is True,
                "expanded": state.get("widget_lock_expanded") is True,
                "keep_open": state.get("keep_open") is True}
    except (OSError, ValueError, AttributeError):
        return {"locked": False, "expanded": False, "keep_open": False}


INSPECTION_HOTKEYS = {"ctrl_shift_t", "ctrl_alt_t", "off"}


def _load_inspection_hotkey(path: Path) -> str:
    try:
        value = json.loads(path.read_text(encoding="utf-8")).get("inspection_hotkey")
    except (OSError, ValueError, AttributeError):
        value = None
    return value if value in INSPECTION_HOTKEYS else "ctrl_shift_t"


def _restorable_bounds(bounds: dict | None, areas: list[tuple[int, int, int, int]]) -> tuple[int, int, int, int] | None:
    """Saved bounds adjusted to a connected screen, or None when that screen is gone.

    The window must keep a grabbable part of its top edge on some work area.
    Size is capped to that area and the position nudged so the whole widget fits.
    """
    if not bounds or not areas:
        return None
    x, y, width, height = bounds["x"], bounds["y"], bounds["width"], bounds["height"]
    for left, top, right, bottom in areas:
        visible_width = min(x + width, right) - max(x, left)
        if visible_width < 80 or not (top <= y < bottom - 24):
            continue
        width = max(min(width, right - left), min(MIN_WINDOW_WIDTH, right - left))
        height = max(min(height, bottom - top), min(MIN_WINDOW_HEIGHT, bottom - top))
        x = min(max(x, left), right - width)
        y = min(max(y, top), bottom - height)
        return width, height, x, y
    return None


def _opening_bounds(area: tuple[int, int, int, int]) -> tuple[int, int, int, int]:
    left, top, right, bottom = area
    available_width, available_height = max(1, right-left-32), max(1, bottom-top-32)
    # Always open as the single-column widget; on short screens the chat list
    # (the only scroll region) absorbs the lost height.
    width = min(WINDOW_WIDTH, available_width)
    height = min(WINDOW_HEIGHT, available_height)
    return width, height, right-width-16, bottom-height-16


def _notch_height(height=None):
    if height is None:
        return NOTCH_DEFAULT_HEIGHT
    if isinstance(height, bool) or not isinstance(height, (int, float)) or (isinstance(height, float) and not math.isfinite(height)):
        raise ValueError("Notch height must be a finite number")
    return round(max(NOTCH_MIN_HEIGHT, height))


def _compact_notch_dimensions(mode, height=None):
    if not isinstance(mode, str) or mode not in COMPACT_NOTCH_MODES:
        raise ValueError("Unknown compact notch mode")
    width, default_height = COMPACT_NOTCH_SIZES[mode]
    if height is not None and (isinstance(height, bool) or not isinstance(height, (int, float))
                               or (isinstance(height, float) and not math.isfinite(height))):
        raise ValueError("Notch height must be a finite number")
    if mode == "inspect":
        return width, round(min(COMPACT_INSPECT_MAX_HEIGHT,
                                max(default_height, height if height is not None else default_height)))
    return width, default_height


def _notch_animation_enabled():
    """Respect the Windows accessibility preference for client-area animation."""
    enabled = ctypes.c_int(1)
    try:
        if ctypes.windll.user32.SystemParametersInfoW(0x1042, 0, ctypes.byref(enabled), 0):
            return bool(enabled.value)
    except (AttributeError, OSError, ctypes.ArgumentError):
        pass
    return True


def _notch_bounds(area, expanded=False, height=None):
    left, top, right, bottom = area
    width = min(NOTCH_WIDTH, max(1, right - left))
    height = min(_notch_height(height) if expanded else NOTCH_COLLAPSED_HEIGHT, max(1, bottom - top))
    return width, height, left + (right - left - width) // 2, top


def _notch_smootherstep(progress):
    """Seventh-order easing with a soft start and zero endpoint velocity."""
    progress = min(1.0, max(0.0, float(progress)))
    squared = progress * progress
    return squared * squared * (35 + progress * (-84 + progress * (70 - 20 * progress)))


def _notch_transition_frame(start, target, progress):
    eased = _notch_smootherstep(progress)
    return tuple(round(a + (b - a) * eased) for a, b in zip(start, target))


def _advance_notch_deadline(deadline, now, interval=NOTCH_FRAME_INTERVAL):
    """Advance one or more sample slots so late frames never trigger catch-up bursts."""
    if now < deadline:
        return deadline
    missed_slots = math.floor((now - deadline) / interval) + 1
    return deadline + missed_slots * interval


class WindowApi:
    def __init__(self, on_minimize, on_toggle_maximize, on_close, on_size=None, on_resize=None, on_fit=None, on_notch=None,
                 on_monitors=None, on_monitor=None, on_recenter=None, on_drag_begin=None, on_drag_end=None,
                 on_lock=None, on_get_lock=None, on_reset_actions=None,
                 on_keep_open=None, on_get_keep_open=None, on_compact_profile=None,
                 on_set_monitor_name=None) -> None:
        self._minimize = on_minimize
        self._toggle_maximize = on_toggle_maximize
        self._close = on_close
        self._size = on_size
        self._resize = on_resize
        self._fit = on_fit
        self._notch = on_notch
        self._monitors = on_monitors
        self._monitor = on_monitor
        self._recenter = on_recenter
        self._drag_begin = on_drag_begin
        self._drag_end = on_drag_end
        self._set_widget_lock = on_lock
        self._get_widget_lock = on_get_lock
        self._open_reset_actions = on_reset_actions
        self._set_keep_open = on_keep_open
        self._get_keep_open = on_get_keep_open
        self._is_compact_profile = on_compact_profile
        self._set_monitor_name = on_set_monitor_name

    def open_reset_actions(self):
        return self._open_reset_actions() if self._open_reset_actions else False

    def begin_drag(self, intent=None):
        if intent is None:
            return self._drag_begin() if self._drag_begin else False
        if intent != "compact-v1":
            return {"accepted": False, "generation": None}
        return self._drag_begin(intent) if self._drag_begin else {"accepted": False, "generation": None}

    def end_drag(self, generation=None, commit=None):
        if self._drag_end and generation is None and commit is None:
            return self._drag_end(True)
        if self._drag_end and type(generation) is bool and commit is None:
            return self._drag_end(generation)
        if (self._drag_end and type(generation) is int and generation > 0
                and type(commit) is bool):
            return self._drag_end(generation, commit)
        return False

    def get_monitors(self):
        return self._monitors() if self._monitors else []

    def select_monitor(self, monitor_id):
        return self._monitor(monitor_id) if self._monitor else []

    def set_monitor_name(self, monitor_id, name):
        return self._set_monitor_name(monitor_id, name) if self._set_monitor_name else []

    def recenter(self):
        return self._recenter() if self._recenter else None

    def get_widget_lock(self):
        return self._get_widget_lock() if self._get_widget_lock else {
            "locked": False, "expanded": False, "revision": 0,
            "keepOpen": False, "keepOpenRevision": 0}

    def set_widget_lock(self, locked, expanded, height=None, expected_revision=None):
        if (not isinstance(locked, bool) or not isinstance(expanded, bool)
                or (expected_revision is not None
                    and (type(expected_revision) is not int or expected_revision < 0))
                or (height is not None and self._set_widget_lock is None)):
            return self.get_widget_lock()
        if self._set_widget_lock:
            if height is None:
                if expected_revision is None:
                    return self._set_widget_lock(locked, expanded)
                return self._set_widget_lock(locked, expanded, None, expected_revision)
            try:
                if (isinstance(height, bool) or not isinstance(height, (int, float))
                        or (isinstance(height, float) and not math.isfinite(height))):
                    raise ValueError("Invalid lock height")
                if not (self._is_compact_profile and self._is_compact_profile()):
                    height = _notch_height(height)
            except (TypeError, ValueError):
                return self.get_widget_lock()
            if expected_revision is None:
                return self._set_widget_lock(locked, expanded, height)
            return self._set_widget_lock(locked, expanded, height, expected_revision)
        return self.get_widget_lock()

    def get_keep_open(self):
        return self._get_keep_open() if self._get_keep_open else {"keepOpen": False, "keepOpenRevision": 0}

    def set_keep_open(self, keep_open, expected_revision=None):
        if (not isinstance(keep_open, bool)
                or (expected_revision is not None
                    and (type(expected_revision) is not int or expected_revision < 0))):
            return self.get_keep_open()
        if self._set_keep_open:
            return self._set_keep_open(keep_open, expected_revision)
        return self.get_keep_open()

    def set_notch(self, expanded=False, height=None, reduced_motion=False, mode=None) -> None:
        if self._notch and isinstance(expanded, bool) and isinstance(reduced_motion, bool):
            if mode is not None:
                try:
                    _compact_notch_dimensions(mode, height)
                except ValueError:
                    return
                self._notch(expanded, height, reduced_motion, mode)
                return
            if height is None:
                if reduced_motion:
                    self._notch(expanded, None, True)
                else:
                    self._notch(expanded)  # Preserve callbacks accepting the original single argument.
            else:
                try:
                    height = _notch_height(height)
                except ValueError:
                    return
                if reduced_motion:
                    self._notch(expanded, height, True)
                else:
                    self._notch(expanded, height)

    def minimize(self) -> None:
        self._minimize()

    def toggle_maximize(self) -> None:
        self._toggle_maximize()

    def close(self) -> None:
        self._close()

    def resize_start(self):
        return self._size() if self._size else None

    def resize(self, width, height) -> None:
        if self._resize:
            self._resize(width, height)

    def fit(self) -> None:
        if self._fit:
            self._fit()



def _claim_address(sock: socket.socket) -> None:
    """Make a listening socket single-owner.

    On Windows SO_REUSEADDR lets a second process bind a port that is already
    listening, so two widgets launched together could both "own" 8765/8766.
    SO_EXCLUSIVEADDRUSE makes the loser's bind fail instead.
    """
    exclusive = getattr(socket, "SO_EXCLUSIVEADDRUSE", None)
    if exclusive is not None:
        sock.setsockopt(socket.SOL_SOCKET, exclusive, 1)
    else:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)


class _ExclusiveHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = False

    def server_bind(self) -> None:
        _claim_address(self.socket)
        super().server_bind()


RESET_ACTIONS_URL = f"http://{HTTP_HOST}:{HTTP_PORT}/reset_actions.html"


class WidgetApp:
    def __init__(self, *, start_hidden: bool = False, compact_profile: bool = True,
                 recenter_on_start: bool = False, appdata_redirected: bool = False) -> None:
        self.window = None
        self.reset_actions_window = None
        self._reset_actions_token = None
        self._reset_actions_lock = threading.Lock()
        self.start_hidden = bool(start_hidden)
        self.visible = not self.start_hidden
        self.server: ThreadingHTTPServer | None = None
        self.tray: pystray.Icon | None = None
        self.quitting = False
        self.notch_mode = True
        self.compact_profile = bool(compact_profile)
        self._compact_notch_mode = "controls" if self.compact_profile else None
        self._recenter_on_start = bool(recenter_on_start)
        self.notch_expanded = False
        self.notch_height = NOTCH_DEFAULT_HEIGHT
        self._notch_lock = threading.RLock()
        self._notch_wake = threading.Event()
        self._notch_transition = None
        self._notch_thread = None
        self.maximized = False
        self.minimized = False
        self.control = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        _claim_address(self.control)
        self.nonce_path: Path | None = product.control_nonce_path()
        self._nonce: str | None = None
        self.maintenance_exit_requested = False
        # Startup and shutdown are serialized: each startup step checks for a
        # requested shutdown and acquires its resource under this lock, and
        # quit() marks the app quitting under the same lock before releasing
        # whatever exists. A shutdown accepted mid-startup cancels the rest.
        self._lifecycle = threading.RLock()
        self._shutdown_requested = threading.Event()
        self._maintenance_thread: threading.Thread | None = None
        self._startup_install_mutex = None
        self._policy_notice_shown = False
        self._appdata_redirected = appdata_redirected
        self._webview_storage: Path | None = None
        # Reopen where the user left it: last normal bounds plus the maximized flag.
        self.window_state_path = _window_state_path()
        self.bounds: dict | None = _load_window_bounds(self.window_state_path)
        widget_lock = _load_widget_lock(self.window_state_path)
        self.widget_locked = widget_lock["locked"]
        self.keep_open = widget_lock.get("keep_open", False)
        self._keep_open_revision = 0
        self.notch_expanded = widget_lock["expanded"] if self.widget_locked else False
        self._widget_lock_revision = 0
        self.inspection_hotkey = _load_inspection_hotkey(self.window_state_path)
        self._hotkey_lock = threading.RLock()
        self._hotkey_config_lock = threading.Lock()
        self._inspection_hotkey_thread = None
        self._inspection_hotkey_thread_id = None
        self._inspection_hotkey_stop_event = None
        self._inspection_hotkey_ready = threading.Event()
        self._inspection_hotkey_registered = False
        self._inspection_hotkey_status = "off" if self.inspection_hotkey == "off" else "pending"
        self._window_loaded = False
        self._inspection_pending = False
        self._notch_placement = _load_notch_placement(self.window_state_path)
        self.selected_monitor_id = self._notch_placement.get("monitor_id")
        self._notch_manual_position = bool(self._notch_placement.get("manual"))
        self._notch_position = ((self._notch_placement["x"], self._notch_placement["y"])
                                if self._notch_manual_position else None)
        self._notch_reference_width = self._notch_placement.get("reference_width")
        if self._recenter_on_start:
            self._notch_manual_position = False
            self._notch_position = None
            self._notch_reference_width = None
        self._widget_lock_render_position = None
        self._notch_native_frames = deque(maxlen=128)
        self._notch_placing = False
        self._notch_placement_ready = False
        self._notch_user_dragging = False
        self._notch_drag_events_registered = False
        self._notch_command_guard_until = 0.0
        self._notch_drag_before = None
        self._notch_drag_generation = 0
        self._active_compact_drag_generation = None
        self._compact_drag_deadline = None
        self._compact_drag_timer = None
        self._compact_drag_watchdog_timer = None
        self._compact_drag_start_rect = None
        self._bounds_lock = threading.Lock()
        self._bounds_timer: threading.Timer | None = None
        if self._recenter_on_start:
            self._remember_notch_placement()
            self._persist_widget_lock()
        self.limit_reset = LimitResetService(
            list_candidates=reset_delivery.list_candidates,
            inspect_target=reset_delivery.inspect_target,
            queue_continue=reset_delivery.queue_continue,
        )
        Handler.reset_service = self.limit_reset

    def _native_window_handle(self) -> int | None:
        """Return the Edge/WinForms host HWND once pywebview has created it."""
        native = getattr(self.window, "native", None)
        handle = getattr(native, "Handle", None)
        if handle is None:
            return None
        try:
            # WinForms exposes IntPtr; tests and some bridges expose an int.
            value = handle.ToInt64() if hasattr(handle, "ToInt64") else int(handle)
            return int(value) or None
        except (TypeError, ValueError, OverflowError):
            return None

    def _native_state(self) -> dict[str, bool] | None:
        """Read the real HWND state; None when no native window is available."""
        hwnd = self._native_window_handle()
        if hwnd is None:
            return None
        try:
            user32 = ctypes.windll.user32
            hwnd_ptr = ctypes.c_void_p(hwnd)
            return {
                "visible": bool(user32.IsWindowVisible(hwnd_ptr)),
                "minimized": bool(user32.IsIconic(hwnd_ptr)),
                "maximized": bool(user32.IsZoomed(hwnd_ptr)),
            }
        except (AttributeError, OSError, ctypes.ArgumentError):
            return None

    def sync_window_state(self, *, maximized: bool | None = None, minimized: bool | None = None) -> None:
        """Keep flags aligned with changes made outside the page buttons.

        Win+Up/Down, taskbar clicks and Aero Snap change the window without
        going through toggle_maximize/minimize. Native state wins; the hints
        are the pywebview event meaning, used when no HWND is available.
        """
        native = self._native_state()
        visible = None
        if native is not None:
            minimized = native["minimized"]
            # An iconic window reports IsZoomed False but still restores to
            # maximized, so keep the remembered flag while minimized.
            maximized = None if minimized else native["maximized"]
            visible = native["visible"] and not minimized
        elif minimized is not None:
            visible = not minimized
        if maximized is not None:
            self.maximized = maximized
        if minimized is not None:
            self.minimized = minimized
        if visible is not None:
            self.visible = visible

    def on_maximized(self, *_args) -> None:
        self._cancel_active_compact_drag()
        self._cancel_notch_transition()
        self.sync_window_state(maximized=True, minimized=False)
        self._remember(maximized=True)

    def on_minimized(self, *_args) -> None:
        self._cancel_active_compact_drag()
        self._cancel_notch_transition()
        self.sync_window_state(minimized=True)

    def on_restored(self, *_args) -> None:
        self.sync_window_state(maximized=False, minimized=False)
        self._remember(maximized=False)

    @_physical_coordinates()
    def _native_rect(self) -> tuple[int, int, int, int] | None:
        """Real window rectangle (left, top, width, height), or None without a native HWND."""
        hwnd = self._native_window_handle()
        if hwnd is None:
            return None
        try:
            rect = _Rect()
            if not ctypes.windll.user32.GetWindowRect(ctypes.c_void_p(hwnd), ctypes.byref(rect)):
                return None
            return rect.left, rect.top, rect.right - rect.left, rect.bottom - rect.top
        except (AttributeError, OSError, ctypes.ArgumentError):
            return None

    def _normal_bounds_event(self) -> bool:
        """Only a normal, on-screen window's geometry is remembered.

        Restoring from minimized can briefly report the parking position
        (-32000, -32000) with the minimum size; saving that would overwrite
        the user's real size.
        """
        self.sync_window_state()
        if self.maximized or self.minimized:
            return False
        rect = self._native_rect()
        return rect is None or (rect[0] > -30000 and rect[1] > -30000)

    def on_moved(self, x, y, *_args) -> None:
        # Minimized windows report a parking position around (-32000, -32000).
        if x <= -30000 or y <= -30000 or not self._normal_bounds_event():
            return
        if self.notch_mode:
            if not self._notch_placement_ready:
                return
            rect = self._native_rect()
            position = (rect[0], rect[1]) if rect else (int(x), int(y))
            with self._notch_lock:
                now = time.monotonic()
                active_generation = self._active_compact_drag_generation
                if (active_generation is not None and self._compact_drag_deadline is not None
                        and now >= self._compact_drag_deadline):
                    self._finish_compact_drag(active_generation, False)
                    rect = self._native_rect()
                    position = (rect[0], rect[1]) if rect else (int(x), int(y))
                    now = time.monotonic()
                while self._notch_native_frames and now-self._notch_native_frames[0][4] > NOTCH_EVENT_GUARD_SECONDS:
                    self._notch_native_frames.popleft()
                programmatic = (not self._notch_user_dragging and (
                    self._notch_placing or self._notch_transition is not None
                    or any(frame[2:4] == position for frame in self._notch_native_frames)))
                if (self.compact_profile and self._active_compact_drag_generation is not None
                        and self._notch_user_dragging and self._compact_drag_deadline is not None
                        and now < self._compact_drag_deadline and rect and rect[2] > 0):
                    if self.widget_locked:
                        self._widget_lock_render_position = position
                    return
                if self.widget_locked:
                    if programmatic:
                        return
                    if (self._widget_lock_render_position is not None
                            and position != self._widget_lock_render_position):
                        self._apply_notch_bounds(animate=False)
                    return
                # A moved callback is geometry telemetry, not evidence of a
                # drag. WinForms can round/coalesce a programmatic resize and
                # deliver its callback after a newer frame was already read.
                if self._notch_drag_events_registered and not self._notch_user_dragging:
                    return
                if programmatic or (not self._notch_user_dragging
                                    and now <= self._notch_command_guard_until):
                    return
                self._cancel_notch_transition()
                self._notch_manual_position = True
                self._notch_position = position
                self._notch_reference_width = rect[2] if rect and rect[2] > 0 else None
                # Dragging onto another monitor follows that monitor on restart.
                for monitor in _monitor_descriptors():
                    left, top, right, bottom = monitor["area"]
                    if left <= position[0] < right and top <= position[1] < bottom:
                        self.selected_monitor_id = monitor["id"]
                        break
                self._remember_notch_placement()
            return
        self._remember_geometry(x=int(x), y=int(y))

    def on_bounds_resized(self, width, height, *_args) -> None:
        if self.notch_mode:
            return
        if width < 100 or height < 100 or not self._normal_bounds_event():
            return
        self._remember_geometry(width=int(width), height=int(height))

    def _remember_geometry(self, **event_values) -> None:
        """Prefer the real window rectangle over pywebview's DPI-converted event values.

        pywebview converts with the DPI it reads for the window at that moment; on
        mixed-DPI setups those logical values do not round-trip, so a relaunch drifted.
        """
        rect = self._native_rect()
        if rect is not None and rect[2] >= 100 and rect[3] >= 100:
            self._remember(x=rect[0], y=rect[1], width=rect[2], height=rect[3])
        else:
            self._remember(**event_values)

    def _remember(self, **changes) -> None:
        if getattr(self, "notch_mode", False):
            return  # Hover geometry must never replace the legacy saved rectangle.
        with self._bounds_lock:
            current = dict(self.bounds or {})
            if not current and self.window is not None:
                with suppress(Exception):
                    current = {"x": int(self.window.x), "y": int(self.window.y),
                               "width": int(self.window.width), "height": int(self.window.height), "maximized": False}
            if not current and not {"x", "y", "width", "height"} <= set(changes):
                current = {}
            current.update(changes)
            if not {"x", "y", "width", "height"} <= set(current):
                self.bounds = current or None
                return
            current.setdefault("maximized", False)
            if current == self.bounds:
                return
            self.bounds = current
            if self._bounds_timer:
                self._bounds_timer.cancel()
            # Moves and resizes fire continuously while dragging; write once they settle.
            self._bounds_timer = threading.Timer(BOUNDS_SAVE_DELAY_SECONDS, self.save_bounds)
            self._bounds_timer.daemon = True
            self._bounds_timer.start()

    def save_bounds(self) -> None:
        with self._bounds_lock:
            if self._bounds_timer:
                self._bounds_timer.cancel()
                self._bounds_timer = None
            bounds = dict(self.bounds or {})
            if self._notch_placement:
                bounds["notch"] = dict(self._notch_placement)
        if {"x", "y", "width", "height"} <= set(bounds) or "notch" in bounds:
            with suppress(OSError):
                _save_window_bounds(self.window_state_path, bounds)

    def initial_bounds(self) -> tuple[int, int, int, int]:
        """Logical creation bounds; the exact physical rectangle is applied once the window loads."""
        if getattr(self, "notch_mode", False):
            scale = _system_scale()
            # Start with the collapsed rail so persisted expansion can wait for
            # the first painted page and its measured content height.
            width, height, x, y = self._notch_target(expanded=False)
            return tuple(round(value / scale) for value in (width, height, x, y))
        scale = _system_scale()
        logical = self.bounds and {key: round(self.bounds[key] / scale) for key in ("x", "y", "width", "height")}
        restored = _restorable_bounds(logical, _monitor_work_areas())
        return restored or _opening_bounds(_work_area())

    @_physical_coordinates()
    def _place(self, width: int, height: int, x: int, y: int, *, asynchronous=False) -> bool:
        """Set the exact native rectangle; False when no native window is available."""
        hwnd = self._native_window_handle()
        if hwnd is None:
            return False
        try:
            # SWP_NOZORDER | SWP_NOACTIVATE: keep the topmost widget where it is in z-order.
            # Background animation posts to the GUI thread rather than waiting
            # synchronously for that thread to dispatch native resize messages.
            flags = 0x0004 | 0x0010 | (0x4000 if asynchronous else 0)
            if self.notch_mode:
                self._record_notch_frame((int(width), int(height), int(x), int(y)))
            return bool(ctypes.windll.user32.SetWindowPos(ctypes.c_void_p(hwnd), None, int(x), int(y), int(width), int(height), flags))
        except (AttributeError, OSError, ctypes.ArgumentError):
            return False

    def restore_saved_placement(self, *_args) -> None:
        """Once the native window exists, put it exactly where it was left."""
        if getattr(self, "notch_mode", False):
            self._register_notch_drag_events()
            self._apply_notch_bounds(animate=False, expanded=False)
            if self.widget_locked and not self._notch_manual_position:
                self._capture_widget_lock_anchor()
                self._persist_widget_lock()
                self._apply_notch_bounds(animate=False, expanded=False)
            self._notch_placement_ready = True
            self._notify_widget_lock()
            return
        if not self.bounds or not self.window:
            return
        # Saved bounds are physical pixels, so they are fitted to physical work areas.
        target = _restorable_bounds(self.bounds, _monitor_work_areas(physical=True))
        if target is not None:
            self._place(*target)
        if self.bounds.get("maximized"):
            with suppress(Exception):
                self.window.maximize()
                self.maximized = True

    restore_maximized_state = restore_saved_placement

    def _register_notch_drag_events(self):
        """WinForms raises ResizeBegin/End for the native move/size modal loop."""
        native = getattr(self.window, "native", None)
        if native is None or self._notch_drag_events_registered:
            return
        try:
            native.ResizeBegin += self._on_notch_drag_begin
            native.ResizeEnd += self._on_notch_drag_end
            self._notch_drag_events_registered = True
        except (AttributeError, TypeError):
            pass

    def _primary_button_down(self):
        try:
            return bool(ctypes.windll.user32.GetAsyncKeyState(0x01) & 0x8000)
        except (AttributeError, OSError, ctypes.ArgumentError):
            return None

    def _schedule_compact_drag_watchdog(self, generation):
        if (generation != self._active_compact_drag_generation
                or self._compact_drag_deadline is None):
            return
        timer = threading.Timer(COMPACT_DRAG_WATCHDOG_SECONDS,
                                self._watch_compact_drag_release, args=(generation,))
        timer.daemon = True
        self._compact_drag_watchdog_timer = timer
        timer.start()

    def _watch_compact_drag_release(self, generation):
        with self._notch_lock:
            if (generation != self._active_compact_drag_generation
                    or not self._notch_user_dragging):
                return
            if (self._compact_drag_deadline is None
                    or time.monotonic() >= self._compact_drag_deadline):
                self._finish_compact_drag(generation, False)
                return
            normal = self._normal_bounds_event()
            rect = self._native_rect()
            if (not self.window or not self.visible or self.minimized or self.maximized
                    or self.quitting or not normal or rect is None
                    or rect[0] <= -30000 or rect[1] <= -30000 or rect[2] <= 0):
                self._finish_compact_drag(generation, False)
                return
            button_down = self._primary_button_down()
            if button_down is False:
                self._finish_compact_drag(generation, True, button_up_confirmed=True)
                return
            if generation == self._active_compact_drag_generation:
                self._schedule_compact_drag_watchdog(generation)

    def _arm_notch_drag(self, generation=None, start_rect=None):
        if self._notch_user_dragging:
            return False
        self._cancel_notch_transition()
        self._notch_drag_before = (self._notch_manual_position, self._notch_position,
                                   self._notch_reference_width, self.selected_monitor_id)
        self._notch_user_dragging = True
        self._notch_native_frames.clear()
        if generation is not None:
            self._compact_drag_start_rect = start_rect or self._native_rect()
            self._active_compact_drag_generation = generation
            self._compact_drag_deadline = time.monotonic() + COMPACT_DRAG_LEASE_SECONDS
            timer = threading.Timer(COMPACT_DRAG_LEASE_SECONDS,
                                    self._expire_compact_drag, args=(generation,))
            timer.daemon = True
            self._compact_drag_timer = timer
            timer.start()
            self._schedule_compact_drag_watchdog(generation)
        return True

    def _restore_drag_before(self):
        if self._notch_drag_before is not None:
            (self._notch_manual_position, self._notch_position,
             self._notch_reference_width, self.selected_monitor_id) = self._notch_drag_before
            self._remember_notch_placement()

    def _finish_compact_drag(self, generation, moved, *, button_up_confirmed=False):
        with self._notch_lock:
            if (type(generation) is not int or generation != self._active_compact_drag_generation
                    or not self._notch_user_dragging):
                return False
            if (self._compact_drag_deadline is None
                    or time.monotonic() >= self._compact_drag_deadline):
                moved = False
            if moved:
                normal = self._normal_bounds_event()
                rect = self._native_rect()
                if (not self.window or not self.visible or self.minimized or self.maximized
                        or self.quitting or not normal or rect is None
                        or rect[0] <= -30000 or rect[1] <= -30000 or rect[2] <= 0):
                    moved = False
            else:
                rect = None
            if moved and not button_up_confirmed and self._primary_button_down() is not False:
                return True
            if moved and rect == self._compact_drag_start_rect:
                moved = False
            if moved:
                self._notch_manual_position = True
                self._notch_position = (rect[0], rect[1])
                self._notch_reference_width = rect[2]
                for monitor in _monitor_descriptors():
                    left, top, right, bottom = monitor["area"]
                    if left <= rect[0] < right and top <= rect[1] < bottom:
                        self.selected_monitor_id = monitor["id"]
                        break
                self._remember_notch_placement()
                self.save_bounds()
            else:
                self._restore_drag_before()
                self.save_bounds()
            timer = self._compact_drag_timer
            watchdog = self._compact_drag_watchdog_timer
            self._compact_drag_timer = None
            self._compact_drag_watchdog_timer = None
            self._active_compact_drag_generation = None
            self._compact_drag_deadline = None
            self._compact_drag_start_rect = None
            self._notch_user_dragging = False
            self._notch_drag_before = None
            if timer is not None:
                timer.cancel()
            if watchdog is not None:
                watchdog.cancel()
            if (self.window and self.visible and not self.minimized and not self.quitting):
                self._apply_notch_bounds(animate=False)
            return True

    def _expire_compact_drag(self, generation):
        self._finish_compact_drag(generation, False)

    def _cancel_active_compact_drag(self):
        generation = self._active_compact_drag_generation
        if generation is not None:
            return self._finish_compact_drag(generation, False)
        return False

    def _on_notch_drag_begin(self, *_args):
        with self._notch_lock:
            if self._notch_user_dragging:
                # The explicit bridge token owns compact drags. ResizeBegin
                # only confirms an already armed gesture; it cannot authorize one.
                return True
            if self.compact_profile:
                return False  # Compact gestures must be armed by the explicit bridge call.
            if self.widget_locked:
                self._apply_notch_bounds(animate=False)
                return False
            self.sync_window_state()
            if not self.window or self.quitting or not self.visible or self.minimized:
                return False
            return self._arm_notch_drag()

    def _on_notch_drag_end(self, *_args):
        with self._notch_lock:
            if self.compact_profile:
                # Compact native ResizeEnd is untagged; the tokenized bridge end
                # or its generation-owned watchdog is authoritative.
                return
            self._finish_notch_drag(True)

    def _finish_notch_drag(self, moved):
        with self._notch_lock:
            if self.widget_locked or self._active_compact_drag_generation is not None or not self._notch_user_dragging:
                return False
            rect = self._native_rect()
            if moved and rect is not None:
                self.on_moved(rect[0], rect[1])
            elif not moved:
                self._restore_drag_before()
            self._notch_user_dragging = False
            self._notch_drag_before = None
            return True

    def begin_drag(self, intent=None):
        rejected = {"accepted": False, "generation": None}
        if intent is not None and intent != "compact-v1":
            return rejected
        with self._notch_lock:
            self.sync_window_state()
            if (not self.window or not self.notch_mode or self.quitting
                    or not self.visible or self.minimized):
                return rejected if intent is not None else False
            if intent is None:
                if self.widget_locked or self._notch_user_dragging:
                    return False
                if self.compact_profile:
                    if not self._notch_placement_ready or not self._normal_bounds_event():
                        return False
                    return self._arm_notch_drag()
                return bool(self._on_notch_drag_begin())
            start_rect = self._native_rect()
            if (not self.compact_profile or not self._notch_placement_ready or self.maximized
                    or not self._primary_button_down() or start_rect is None
                    or start_rect[0] <= -30000 or start_rect[1] <= -30000 or start_rect[2] <= 0
                    or not self._normal_bounds_event()):
                return rejected
            if self._active_compact_drag_generation is not None:
                if (self._notch_user_dragging and self._compact_drag_deadline is not None
                        and time.monotonic() < self._compact_drag_deadline):
                    return rejected
                self._cancel_active_compact_drag()
            if self._notch_user_dragging:
                return rejected
            self._notch_drag_generation += 1
            generation = self._notch_drag_generation
            if not self._arm_notch_drag(generation, start_rect):
                return rejected
            return {"accepted": True, "generation": generation}

    def end_drag(self, generation=None, commit=None):
        if generation is None and commit is None:
            generation, commit = True, None
        if type(generation) is bool and commit is None:
            legacy_moved = generation
        elif type(generation) is int and generation > 0 and type(commit) is bool:
            return self._finish_compact_drag(generation, commit)
        else:
            return False
        with self._notch_lock:
            if self._active_compact_drag_generation is not None:
                return False
            ended = self._finish_notch_drag(legacy_moved)
            if ended and not legacy_moved and self.window and self.visible and not self.minimized:
                self._apply_notch_bounds(animate=False)
            return ended

    def _record_notch_frame(self, frame):
        now = time.monotonic()
        self._notch_native_frames.append((*frame, now))
        self._notch_command_guard_until = now + NOTCH_EVENT_GUARD_SECONDS

    def _set_native_background_fallback(self, *_args) -> None:
        """Color the opaque WinForms host behind transparent WebView2 content."""
        window = self.window
        native = getattr(window, "native", None) if window else None
        if native is None or not all(
            hasattr(native, name) for name in ("BackColor", "Opacity", "TransparencyKey")
        ):
            return
        try:
            # Only touch hosts exposing the complete WinForms transparency surface.
            host_color = native.BackColor
            opacity = float(native.Opacity)
            transparency_key = native.TransparencyKey
            configured_color = window.background_color
            if opacity < 1.0 or int(host_color.A) < 255 or not transparency_key.IsEmpty:
                return
            from System.Drawing import ColorTranslator

            native.BackColor = ColorTranslator.FromHtml(configured_color)
        except Exception:
            logging.getLogger(__name__).warning(
                "Could not set the native background fallback; leaving the host unchanged",
                exc_info=True,
            )

    def apply_native_window_region(self, *_args) -> None:
        """Round the native window itself with no outline around it.

        pywebview's WinForms "transparent" host still paints its full rectangle,
        and a GDI window region does not clip WebView2's composited content, so
        a gray box showed around the rounded page. On Windows 11 the compositor
        rounds (and clips) the window and draws no border; older Windows falls
        back to a window region. SetWindowRgn owns a region only on success.
        """
        hwnd = self._native_window_handle()
        if hwnd is None:
            return
        try:
            user32 = ctypes.windll.user32
            gdi32 = ctypes.windll.gdi32
            dwmapi = getattr(ctypes.windll, "dwmapi", None)
            hwnd_ptr = ctypes.c_void_p(hwnd)
            # IsZoomed reflects native title-bar/double-click maximization too.
            is_maximized = bool(user32.IsZoomed(hwnd_ptr))
            if is_maximized:
                # A clipped maximized HWND can leave visible desktop slivers.
                user32.SetWindowRgn(hwnd_ptr, None, True)
                _set_dwm_attribute(dwmapi, hwnd_ptr, DWMWA_WINDOW_CORNER_PREFERENCE, DWMWCP_DONOTROUND)
                return
            if _set_dwm_attribute(dwmapi, hwnd_ptr, DWMWA_WINDOW_CORNER_PREFERENCE, DWMWCP_ROUND):
                # A window region disables compositor rounding; remove any earlier one.
                user32.SetWindowRgn(hwnd_ptr, None, True)
                _set_dwm_attribute(dwmapi, hwnd_ptr, DWMWA_BORDER_COLOR, DWMWA_COLOR_NONE)
                return
            rect = _Rect()
            if not user32.GetClientRect(hwnd_ptr, ctypes.byref(rect)):
                return
            width = rect.right - rect.left
            height = rect.bottom - rect.top
            if width <= 0 or height <= 0:
                return
            # Match the CSS radius on high-DPI displays.
            dpi = user32.GetDpiForWindow(hwnd_ptr) or 96
            diameter = max(1, round(WINDOW_CORNER_RADIUS * 2 * dpi / 96))
            region = gdi32.CreateRoundRectRgn(0, 0, width + 1, height + 1, diameter, diameter)
            if not region:
                return
            region_value = region.value if isinstance(region, ctypes.c_void_p) else region
            if not user32.SetWindowRgn(hwnd_ptr, ctypes.c_void_p(region_value), True):
                # Windows owns the region after a successful SetWindowRgn only.
                gdi32.DeleteObject(ctypes.c_void_p(region_value))
        except (AttributeError, OSError, ctypes.ArgumentError):
            # Keep the widget functional on non-Windows or unusual webview hosts.
            return

    @staticmethod
    def notify_existing() -> bool:
        """Focus a running Branchglance, but only after it proves who it is.

        The listener must prove it holds this session's control nonce (see
        :func:`control_command`); self-reported identity fields are not enough,
        and an unverified listener is never sent the nonce or a command.
        """
        return control_command("show") == "ok"

    def start_http(self) -> bool:
        """Bind the UI server, retrying briefly. Failure is reported, never swallowed."""
        self.server = None
        for attempt in range(max(1, BIND_RETRY_ATTEMPTS)):
            try:
                self.server = _ExclusiveHTTPServer((HTTP_HOST, HTTP_PORT), Handler)
                break
            except OSError:
                if attempt + 1 < BIND_RETRY_ATTEMPTS:
                    time.sleep(BIND_RETRY_DELAY)
        if self.server is None:
            return False
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        # The first transcript scan is the slow cold path; build it before the page asks.
        threading.Thread(target=_warm_snapshot, name="token-snapshot-warmup", daemon=True).start()
        return True

    def _write_control_nonce(self) -> None:
        nonce = secrets.token_hex(32)
        path = self.nonce_path
        if path is None:
            self._nonce = nonce
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(path.name + f".{os.getpid()}.tmp")
        temporary.write_text(nonce, encoding="ascii")
        os.replace(temporary, path)
        self._nonce = nonce

    def _remove_control_nonce(self) -> None:
        # Only the instance that wrote the nonce may remove it; a loser of the
        # control-port race must never delete the winner's file.
        written, self._nonce = self._nonce, None
        if written is not None and self.nonce_path is not None:
            with suppress(OSError):
                self.nonce_path.unlink()

    def handle_control_command(self, line: str) -> str:
        """Answer an unauthenticated one-line request. Only read-only identity is served this way."""
        if line.split() == ["identity"]:
            return json.dumps(product.identity(), separators=(",", ":"))
        return "error"

    def _request_maintenance_exit(self) -> None:
        # Cancel any remaining startup work synchronously, then quit on a thread
        # (quit waits for an in-progress startup step and releases what it made).
        thread = threading.Thread(target=self.quit, name="maintenance-exit", daemon=True)
        self._maintenance_thread = thread
        thread.start()
        self.maintenance_exit_requested = True
        self._shutdown_requested.set()

    def _execute_control(self, command: str) -> None:
        if command == "show":
            self.show()
        elif command == "maintenance-exit":
            self._request_maintenance_exit()

    def serve_control_connection(self, connection: socket.socket) -> None:
        """One exchange per connection: identity, or hello + authenticated command."""
        reader = _LineReader(connection, CONTROL_LINE_LIMIT)
        first = reader.line()
        if not first:
            return
        parts = first.split()
        if len(parts) != 2 or parts[0] != "hello":
            connection.sendall(self.handle_control_command(first).encode("utf-8") + b"\n")
            return
        client_challenge, nonce = parts[1], self._nonce
        if not product.valid_challenge(client_challenge) or not nonce:
            connection.sendall(b"denied\n")
            return
        server_challenge = product.new_challenge()
        hello = dict(product.identity(), protocol=product.CONTROL_PROTOCOL, challenge=server_challenge,
                     proof=product.control_mac(nonce, "server", client_challenge, server_challenge))
        connection.sendall(json.dumps(hello, separators=(",", ":")).encode("utf-8") + b"\n")
        second = reader.line()
        parts = second.split() if second else []
        if len(parts) != 2 or parts[0] not in product.CONTROL_COMMANDS:
            connection.sendall(b"error\n")
            return
        who = product.identity()
        health_parts = (str(who["pid"]), who["build"], who["stateRoot"])
        signed_parts = (parts[0], *health_parts) if parts[0] == "health" else (parts[0],)
        expected = product.control_mac(nonce, "client", client_challenge, server_challenge, *signed_parts)
        if self._nonce != nonce or not product.control_mac_matches(expected, parts[1]):
            connection.sendall(b"denied\n")
            return
        if parts[0] == "health":
            proof = product.control_mac(nonce, "server", client_challenge, server_challenge, "health", *health_parts)
            response = {"identity": who, "proof": proof}
            connection.sendall(json.dumps(response, separators=(",", ":")).encode("utf-8") + b"\n")
            return
        if parts[0] == "maintenance-exit":
            connection.sendall(b"ok\n")
            self._request_maintenance_exit()
            return
        self._execute_control(parts[0])
        connection.sendall(b"ok\n")

    def start_control(self) -> None:
        for attempt in range(max(1, BIND_RETRY_ATTEMPTS)):
            try:
                self.control.bind((HTTP_HOST, CONTROL_PORT))
                break
            except OSError:
                if attempt + 1 >= BIND_RETRY_ATTEMPTS:
                    raise
                with suppress(OSError):
                    self.control.close()
                self.control = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                _claim_address(self.control)
                time.sleep(BIND_RETRY_DELAY)
        self.control.listen(2)
        self._write_control_nonce()

        slots = threading.BoundedSemaphore(8)

        def serve(connection: socket.socket) -> None:
            try:
                with connection:
                    # A silent client only ever stalls its own handler thread.
                    connection.settimeout(1.0)
                    self.serve_control_connection(connection)
            except OSError:
                pass
            finally:
                slots.release()

        def listen() -> None:
            while True:
                try:
                    connection, _ = self.control.accept()
                except OSError:
                    # A client resetting before accept must not kill the
                    # listener; only a closed socket (quit) ends it.
                    if self.quitting or self.control.fileno() == -1:
                        return
                    continue
                if not slots.acquire(blocking=False):
                    connection.close()
                    continue
                threading.Thread(target=serve, args=(connection,), name="control-client", daemon=True).start()

        threading.Thread(target=listen, daemon=True).start()

    def _inspection_hotkey_loop(self, choice: str, stop_event: threading.Event) -> None:
        registered = False
        user32 = None
        try:
            user32 = ctypes.WinDLL("user32", use_last_error=True)
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            user32.PeekMessageW.argtypes = [ctypes.POINTER(_WinMessage), ctypes.c_void_p,
                                            ctypes.c_uint32, ctypes.c_uint32, ctypes.c_uint32]
            user32.PeekMessageW.restype = ctypes.c_int
            user32.RegisterHotKey.argtypes = [ctypes.c_void_p, ctypes.c_int,
                                              ctypes.c_uint32, ctypes.c_uint32]
            user32.RegisterHotKey.restype = ctypes.c_int
            user32.UnregisterHotKey.argtypes = [ctypes.c_void_p, ctypes.c_int]
            user32.UnregisterHotKey.restype = ctypes.c_int
            user32.GetMessageW.argtypes = [ctypes.POINTER(_WinMessage), ctypes.c_void_p,
                                           ctypes.c_uint32, ctypes.c_uint32]
            user32.GetMessageW.restype = ctypes.c_int
            kernel32.GetCurrentThreadId.restype = ctypes.c_uint32
            thread_id = int(kernel32.GetCurrentThreadId())
            message = _WinMessage()
            user32.PeekMessageW(ctypes.byref(message), None, 0, 0, PM_NOREMOVE)
            with self._hotkey_lock:
                self._inspection_hotkey_thread_id = thread_id
            if stop_event.is_set() or choice == "off":
                return
            modifiers = MOD_CONTROL | (MOD_SHIFT if choice == "ctrl_shift_t" else MOD_ALT) | MOD_NOREPEAT
            registered = bool(user32.RegisterHotKey(None, INSPECTION_HOTKEY_ID, modifiers, VK_T))
            with self._hotkey_lock:
                self._inspection_hotkey_registered = registered
                self._inspection_hotkey_status = "registered" if registered else "unavailable"
            self._inspection_hotkey_ready.set()
            if not registered:
                logging.getLogger(__name__).warning(
                    "Global inspection shortcut registration failed (Windows error %s)", ctypes.get_last_error())
                return
            while not stop_event.is_set():
                result = user32.GetMessageW(ctypes.byref(message), None, 0, 0)
                if result <= 0:
                    break
                if message.message == WM_HOTKEY and message.wparam == INSPECTION_HOTKEY_ID:
                    self._request_current_chat_inspection()
        except (AttributeError, OSError, ctypes.ArgumentError) as exc:
            with self._hotkey_lock:
                self._inspection_hotkey_registered = False
                self._inspection_hotkey_status = "unavailable"
            logging.getLogger(__name__).warning("Global inspection shortcut unavailable (%s)", type(exc).__name__)
        finally:
            if registered and user32 is not None:
                with suppress(AttributeError, OSError, ctypes.ArgumentError):
                    user32.UnregisterHotKey(None, INSPECTION_HOTKEY_ID)
            with self._hotkey_lock:
                self._inspection_hotkey_registered = False
                self._inspection_hotkey_thread_id = None
                self._inspection_hotkey_ready.set()
                if stop_event.is_set() or self.quitting:
                    if self._inspection_hotkey_status == "registered":
                        self._inspection_hotkey_status = "pending"
                elif self._inspection_hotkey_status != "unavailable":
                    self._inspection_hotkey_status = "unavailable"

    def _start_inspection_hotkey(self, wait_seconds: float = 1.0) -> str:
        with self._hotkey_lock:
            if self.inspection_hotkey == "off":
                self._inspection_hotkey_status = "off"
                return "off"
            if not self._window_loaded:
                self._inspection_hotkey_status = "pending"
                return "pending"
            if self._inspection_hotkey_thread and self._inspection_hotkey_thread.is_alive():
                return self._inspection_hotkey_status
            stop_event = threading.Event()
            self._inspection_hotkey_stop_event = stop_event
            self._inspection_hotkey_ready.clear()
            self._inspection_hotkey_status = "pending"
            thread = threading.Thread(target=self._inspection_hotkey_loop,
                                      args=(self.inspection_hotkey, stop_event),
                                      name="token-inspection-hotkey", daemon=True)
            self._inspection_hotkey_thread = thread
            thread.start()
        self._inspection_hotkey_ready.wait(timeout=max(0.0, wait_seconds))
        return self._inspection_hotkey_status

    def _stop_inspection_hotkey(self) -> bool:
        with self._hotkey_lock:
            thread = self._inspection_hotkey_thread
            stop_event = self._inspection_hotkey_stop_event
            thread_id = self._inspection_hotkey_thread_id
            ready = self._inspection_hotkey_ready
        if not thread:
            return True
        if stop_event:
            stop_event.set()
        if thread.is_alive():
            if thread_id is None:
                ready.wait(timeout=0.5)
                with self._hotkey_lock:
                    thread_id = self._inspection_hotkey_thread_id
            if thread_id is not None:
                try:
                    user32 = ctypes.WinDLL("user32", use_last_error=True)
                    post = user32.PostThreadMessageW
                    post.argtypes = [ctypes.c_uint32, ctypes.c_uint32, ctypes.c_size_t, ctypes.c_ssize_t]
                    post.restype = ctypes.c_int
                    post(thread_id, WM_QUIT, 0, 0)
                except (AttributeError, OSError, ctypes.ArgumentError):
                    pass
            thread.join(timeout=INSPECTION_HOTKEY_STOP_SECONDS)
        stopped = not thread.is_alive()
        if stopped:
            with self._hotkey_lock:
                if self._inspection_hotkey_thread is thread:
                    self._inspection_hotkey_thread = None
                    self._inspection_hotkey_thread_id = None
                    self._inspection_hotkey_registered = False
        return stopped

    def set_inspection_hotkey(self, choice: str) -> dict:
        if choice not in INSPECTION_HOTKEYS:
            return {"ok": False, "status": "invalid"}
        with self._hotkey_config_lock:
            if choice == self.inspection_hotkey and (
                    (choice == "off" and self._inspection_hotkey_status == "off")
                    or (choice != "off" and self._inspection_hotkey_status == "registered")):
                return {"ok": True, "choice": choice, "status": self._inspection_hotkey_status}
            if not self._stop_inspection_hotkey():
                return {"ok": False, "choice": self.inspection_hotkey, "status": "stopping"}
            try:
                _save_window_bounds(self.window_state_path, {"inspection_hotkey": choice})
            except OSError:
                return {"ok": False, "choice": self.inspection_hotkey, "status": "save_failed"}
            self.inspection_hotkey = choice
            if choice == "off":
                self._inspection_hotkey_status = "off"
                return {"ok": True, "choice": choice, "status": "off"}
            status = self._start_inspection_hotkey()
            return {"ok": status == "registered", "choice": choice, "status": status}

    def _inspection_hotkey_menu_item(self, choice: str, label: str):
        def text(_item):
            if choice == self.inspection_hotkey and choice != "off" and self._inspection_hotkey_status == "unavailable":
                return label + " (not registered)"
            return label

        def checked(_item):
            return choice == self.inspection_hotkey and (
                choice == "off" or self._inspection_hotkey_status == "registered")

        return pystray.MenuItem(text, lambda _icon, _item: self.set_inspection_hotkey(choice),
                                checked=checked, radio=True)

    def _request_current_chat_inspection(self) -> None:
        window = self.window
        if window is None:
            self._inspection_pending = True
            return
        if self.minimized:
            with suppress(Exception):
                window.restore()
            self.minimized = False
        self.visible = True
        with suppress(Exception):
            window.show()
        if not self._window_loaded:
            self._inspection_pending = True
            return
        self._inspection_pending = False
        with suppress(Exception):
            window.evaluate_js("window.tokenTrackerInspect && window.tokenTrackerInspect()")

    def _mark_window_loaded(self, *_args) -> None:
        self._window_loaded = True
        self._start_inspection_hotkey(wait_seconds=0)
        if self._inspection_pending:
            self._request_current_chat_inspection()

    def claim_instance(self) -> bool:
        """Own the control port, or hand off to the instance that already does."""
        try:
            self.start_control()
            return True
        except OSError as exc:
            # Another process holds the control port. The single-instance mutex
            # makes a second Branchglance unlikely here, so surface the owner
            # instead of crashing with a bind traceback.
            with suppress(OSError):
                self.control.close()
            self._remove_control_nonce()
            if not self.notify_existing():
                show_message(product.PRODUCT, "Branchglance could not start because its control port "
                             f"({CONTROL_PORT}) is in use by another program. If another Branchglance instance "
                             f"is running, quit it from its tray icon and start Branchglance again.\n\n{exc}")
            return False

    @staticmethod
    def icon_image() -> Image.Image:
        with Image.open(product.brand_icon_path()) as source:
            return source.convert("RGBA").resize((64, 64), Image.Resampling.LANCZOS)

    def show(self, *_args) -> None:
        if not self.window:
            return
        self.sync_window_state()
        self._cancel_notch_transition()
        # Going straight back to maximized avoids restore() flashing the
        # normal bounds (and firing restored/maximized events) first.
        with suppress(Exception):
            if self.maximized:
                self.window.maximize()
            else:
                # Keep the size and position the user left; only rescue an off-screen window.
                self.window.restore()
                if not self.notch_mode:
                    self.ensure_on_screen()
                else:
                    self.maximized = False
        self.visible = True
        self.minimized = False
        with suppress(Exception):
            self.window.show()
        if self.notch_mode and not self.maximized and self._native_window_handle():
            with suppress(Exception):
                self._apply_notch_bounds(animate=False)
        with suppress(Exception):
            self.window.evaluate_js("window.tokenTrackerRefresh && window.tokenTrackerRefresh(true)")

    def minimize(self, *_args) -> None:
        if self.window:
            with self._notch_lock:
                self.visible = False
                self.minimized = True
                self._cancel_notch_transition()
            self.window.minimize()

    def hide(self, *_args) -> None:
        if self.window:
            with self._notch_lock:
                self._cancel_active_compact_drag()
                self.visible = False
                self._cancel_notch_transition()
            self.save_bounds()
            self.window.hide()

    def toggle(self, *_args) -> None:
        # The taskbar can restore/minimize behind our back; decide on real state.
        self.sync_window_state()
        self.hide() if self.visible else self.show()

    def toggle_maximize(self, *_args) -> None:
        if not self.window:
            return
        # Win+Up / taskbar maximize would otherwise need two clicks to restore.
        self.sync_window_state()
        if self.maximized:
            self.window.restore()
            self.maximized = False
        else:
            self.window.maximize()
            self.maximized = True
        self.minimized = False

    def size(self):
        if not self.window:
            return None
        self.sync_window_state()
        if self.maximized or self.minimized:
            return None
        return {"width": self.window.width, "height": self.window.height}

    def _selected_monitor(self, monitors=None):
        monitors = _monitor_descriptors() if monitors is None else monitors
        saved_id = _monitor_key(self.selected_monitor_id)
        selected = next((monitor for monitor in monitors if _monitor_key(monitor["id"]) == saved_id), None)
        missing_stable_id = selected is None and _is_stable_monitor_id(saved_id)
        legacy_id = selected is None and bool(saved_id) and not missing_stable_id
        if legacy_id:
            legacy = [monitor for monitor in monitors if _monitor_key(monitor.get("gdiId")) == saved_id]
            selected = legacy[0] if len(legacy) == 1 else None
        if self._notch_manual_position and self._notch_position and (legacy_id or selected is None) and not missing_stable_id:
            x, y = self._notch_position
            containing = [monitor for monitor in monitors
                          if monitor["area"][0] <= x < monitor["area"][2]
                          and monitor["area"][1] <= y < monitor["area"][3]]
            if len(containing) == 1:
                selected = containing[0]
            else:
                selected = None
        if selected is None:
            fallback = next((monitor for monitor in monitors if monitor.get("primary")), monitors[0])
            if missing_stable_id:
                # Render on a connected screen while retaining the unavailable
                # physical target and its anchor for when that target returns.
                selected = {**fallback, "_temporary_physical_fallback": True}
            else:
                selected = fallback
                # Unknown/disconnected legacy IDs must not leave placement offscreen.
                self._notch_manual_position = False
                self._notch_position = None
                self._notch_reference_width = None
        if not missing_stable_id and self.selected_monitor_id != selected["id"]:
            self.selected_monitor_id = selected["id"]
            self._remember_notch_placement()
        return selected

    def _remember_notch_placement(self):
        placement = {"monitor_id": self.selected_monitor_id, "manual": self._notch_manual_position}
        if self._notch_manual_position and self._notch_position:
            placement.update(x=self._notch_position[0], y=self._notch_position[1])
            if type(self._notch_reference_width) is int and self._notch_reference_width > 0:
                placement["reference_width"] = self._notch_reference_width
        with self._bounds_lock:
            if self._notch_placement == placement:
                return
            self._notch_placement = placement
            if self._bounds_timer:
                self._bounds_timer.cancel()
            self._bounds_timer = threading.Timer(BOUNDS_SAVE_DELAY_SECONDS, self.save_bounds)
            self._bounds_timer.daemon = True
            self._bounds_timer.start()

    def get_widget_lock(self):
        with self._notch_lock:
            return {"locked": self.widget_locked, "expanded": self.notch_expanded,
                    "revision": self._widget_lock_revision, "keepOpen": self.keep_open,
                    "keepOpenRevision": self._keep_open_revision}

    def get_keep_open(self):
        with self._notch_lock:
            return {"keepOpen": self.keep_open, "keepOpenRevision": self._keep_open_revision}

    def set_keep_open(self, keep_open, expected_revision=None):
        if (not isinstance(keep_open, bool)
                or (expected_revision is not None
                    and (type(expected_revision) is not int or expected_revision < 0))):
            return self.get_keep_open()
        with self._notch_lock:
            if expected_revision is not None and expected_revision != self._keep_open_revision:
                return self.get_keep_open()
            if self.keep_open != keep_open:
                self.keep_open = keep_open
                self._keep_open_revision += 1
                self._persist_widget_lock()
                self._notify_widget_lock()
            return self.get_keep_open()

    def _persist_widget_lock(self):
        with self._bounds_lock:
            if self._bounds_timer:
                self._bounds_timer.cancel()
                self._bounds_timer = None
            state = {
                "widget_locked": self.widget_locked,
                "widget_lock_expanded": self.notch_expanded,
                "keep_open": self.keep_open,
            }
            if self._notch_placement:
                state["notch"] = dict(self._notch_placement)
            _save_window_bounds(self.window_state_path, state)

    def _notify_widget_lock(self):
        if not self.window:
            return
        state = json.dumps(self.get_widget_lock(), separators=(",", ":"))
        with suppress(Exception):
            self.window.evaluate_js(
                f"window.tokenTrackerSetWidgetLock&&window.tokenTrackerSetWidgetLock({state})")

    def _capture_widget_lock_anchor(self):
        previously_manual = self._notch_manual_position
        previous_reference_width = self._notch_reference_width
        rect = self._native_rect()
        if rect is None or rect[0] <= -30000 or rect[1] <= -30000 or rect[2] <= 0:
            target = self._notch_target()
            x, y = target[2], target[3]
            reference_width = previous_reference_width if previously_manual else None
        else:
            x, y = rect[0], rect[1]
            reference_width = rect[2] if rect[2] > 0 else None
        if previously_manual and previous_reference_width is None:
            reference_width = None
        self._notch_manual_position = True
        self._notch_position = (x, y)
        self._notch_reference_width = reference_width
        for monitor in _monitor_descriptors():
            left, top, right, bottom = monitor["area"]
            if left <= x < right and top <= y < bottom:
                self.selected_monitor_id = monitor["id"]
                break
        self._remember_notch_placement()

    def set_widget_lock(self, locked, expanded, height=None, expected_revision=None):
        if (not isinstance(locked, bool) or not isinstance(expanded, bool)
                or (expected_revision is not None
                    and (type(expected_revision) is not int or expected_revision < 0))
                or (height is not None and (isinstance(height, bool)
                    or not isinstance(height, (int, float))
                    or (isinstance(height, float) and not math.isfinite(height))))):
            return self.get_widget_lock()
        with self._notch_lock:
            compact_profile = self.compact_profile
            normalized_height = (_notch_height(height)
                                 if height is not None and not compact_profile else None)
            if expected_revision is not None and expected_revision != self._widget_lock_revision:
                return self.get_widget_lock()
            self._cancel_active_compact_drag()
            changed = self.widget_locked != locked or (
                not compact_profile and self.notch_expanded != expanded)
            if locked and not self.widget_locked and (not self.compact_profile or not self._notch_manual_position):
                self._capture_widget_lock_anchor()
            self.widget_locked = locked
            if not compact_profile:
                self.notch_expanded = expanded
            if locked:
                self._notch_user_dragging = False
                self._notch_drag_before = None
                self._notch_native_frames.clear()
            if not locked:
                self._widget_lock_render_position = None
            if normalized_height is not None:
                self.notch_height = normalized_height
            if changed:
                self._widget_lock_revision += 1
            self._persist_widget_lock()
            self._cancel_notch_transition()
            if self.window and self.visible and not self.minimized and not self.maximized:
                self._apply_notch_bounds(animate=False)
            self._notify_widget_lock()
            return self.get_widget_lock()

    def get_monitors(self):
        with self._notch_lock:
            monitors = _monitor_descriptors()
            selected = self._selected_monitor(monitors)
            saved_names = _monitor_names(self.window_state_path)
            result = []
            for monitor in monitors:
                left, top, right, bottom = monitor["area"]
                stable_id = monitor.get("stableId")
                result.append({"id": monitor["id"], "stableId": monitor.get("stableId"),
                               "gdiId": monitor.get("gdiId"), "kind": monitor.get("kind"),
                               "label": saved_names.get(_monitor_key(stable_id), monitor["label"]) if stable_id else monitor["label"],
                               "index": monitor["index"],
                               "primary": monitor["primary"], "selected": monitor["id"] == selected["id"],
                               "scale": monitor["scale"],
                               "workArea": {"x": left, "y": top, "width": right-left, "height": bottom-top}})
            return result

    def set_monitor_name(self, monitor_id, name):
        if not isinstance(monitor_id, str) or not _is_stable_monitor_id(monitor_id):
            return self.get_monitors()
        if not isinstance(name, str):
            return self.get_monitors()
        normalized = _valid_monitor_name(name)
        if name.strip() and normalized is None:
            return self.get_monitors()
        with self._notch_lock:
            matches = [monitor for monitor in _monitor_descriptors()
                       if _monitor_key(monitor.get("stableId")) == _monitor_key(monitor_id)]
            if len(matches) != 1:
                return self.get_monitors()
            with self._bounds_lock:
                names = _monitor_names(self.window_state_path)
                if normalized is None:
                    names.pop(_monitor_key(monitor_id), None)
                else:
                    names[_monitor_key(matches[0]["stableId"])] = normalized
                _save_window_bounds(self.window_state_path, {"monitor_names": names})
            return self.get_monitors()

    def select_monitor(self, monitor_id):
        with self._notch_lock:
            if self.widget_locked:
                return self.get_monitors()
            monitors = _monitor_descriptors()
            requested_key = _monitor_key(monitor_id)
            selected = next((monitor for monitor in monitors if _monitor_key(monitor["id"]) == requested_key), None)
            if selected is None:
                aliases = [monitor for monitor in monitors if _monitor_key(monitor.get("gdiId")) == requested_key]
                selected = aliases[0] if len(aliases) == 1 else None
            if selected is not None:
                self.selected_monitor_id = selected["id"]
                self.recenter()
            return self.get_monitors()

    def recenter(self):
        if not self.notch_mode:
            self.fit_opening_size()
            return self.get_monitors()
        with self._notch_lock:
            self._cancel_active_compact_drag()
            selected = self._selected_monitor()
            if selected.get("_temporary_physical_fallback"):
                # Recenter is an explicit placement choice, so it can adopt the
                # connected fallback instead of preserving a missing target.
                self.selected_monitor_id = selected["id"]
            self.widget_locked = False
            self._widget_lock_revision += 1
            self._notch_manual_position = False
            self._notch_position = None
            self._notch_reference_width = None
            self._widget_lock_render_position = None
            self._remember_notch_placement()
            self._persist_widget_lock()
            self._notify_widget_lock()
            if self.window and self.visible and not self.minimized:
                self._apply_notch_bounds(animate=False)
            return self.get_monitors()

    def _notch_target(self, expanded=None):
        monitor = self._selected_monitor()
        scale = monitor["scale"]
        left, top, right, bottom = monitor["area"]
        if self.compact_profile:
            mode = self._compact_notch_mode or "idle"
            width_logical, height_logical = _compact_notch_dimensions(
                mode, self.notch_height if mode == "inspect" else None)
            width = min(round(width_logical * scale), right-left)
            height = min(round(height_logical * scale), bottom-top)
            x, y = left + (right-left-width) // 2, top
            if self._notch_manual_position and self._notch_position and not monitor.get("_temporary_physical_fallback"):
                anchor_x, anchor_y = self._notch_position
                anchor_left = anchor_x
                if type(self._notch_reference_width) is int and self._notch_reference_width > 0:
                    anchor_left = round(anchor_x + (self._notch_reference_width-width)/2)
                if monitor.get("stableId"):
                    x = min(max(anchor_left, left), right-width)
                    y = min(max(anchor_y, top), max(top, bottom-height))
                elif (anchor_x >= right-24 or anchor_x+80 <= left
                        or anchor_y >= bottom-24 or anchor_y < top):
                    self._notch_manual_position = False
                    self._notch_position = None
                    self._notch_reference_width = None
                    self._remember_notch_placement()
                else:
                    x = min(max(anchor_left, left), right-width)
                    y = min(max(anchor_y, top), max(top, bottom-height))
            return max(1, width), max(1, height), x, y
        width = min(round(NOTCH_WIDTH * scale), right-left)
        is_expanded = self.notch_expanded if expanded is None else expanded
        requested_height = self.notch_height if is_expanded else NOTCH_COLLAPSED_HEIGHT
        height = round(min(requested_height, (bottom-top) / scale) * scale)
        x, y = left + (right-left-width) // 2, top
        if (self._notch_manual_position and self._notch_position
                and not monitor.get("_temporary_physical_fallback")):
            anchor_x, anchor_y = self._notch_position
            if monitor.get("stableId"):
                # A stable physical target lets us keep the manual anchor when
                # resolution changes; clamp it to the same monitor's new area.
                x = min(max(anchor_x, left), right-width)
                collapsed_height = min(round(NOTCH_COLLAPSED_HEIGHT * scale), bottom-top)
                # Expansion grows down from the saved top edge. If the requested
                # content is taller than the remaining work area, clip its height
                # instead of moving the user's anchor upward.
                y = min(max(anchor_y, top), max(top, bottom-collapsed_height))
                height = min(height, bottom-y)
                self._notch_position = (x, y)
            elif anchor_x >= right-24 or anchor_x+80 <= left or anchor_y >= bottom-24 or anchor_y < top:
                self._notch_manual_position = False
                self._notch_position = None
                self._notch_reference_width = None
                self._remember_notch_placement()
            else:
                x = min(max(anchor_x, left), right-width)
                collapsed_height = min(round(NOTCH_COLLAPSED_HEIGHT * scale), bottom-top)
                y = min(max(anchor_y, top), max(top, bottom-collapsed_height))
                height = min(height, bottom-y)
        return max(1, width), max(1, height), x, y

    def ensure_on_screen(self) -> None:
        """Put a window that restored off-screen (or at the parking position) back
        at the user's saved bounds, or the opening position if that screen is gone."""
        rect = self._native_rect()
        areas = _monitor_work_areas(physical=True)
        if rect is None or not areas:
            return
        current = {"x": rect[0], "y": rect[1], "width": rect[2], "height": rect[3]}
        if rect[0] > -30000 and rect[1] > -30000 and _restorable_bounds(current, areas) == (rect[2], rect[3], rect[0], rect[1]):
            return
        target = _restorable_bounds(self.bounds, areas)
        if target is None:
            self.fit_opening_size()
            return
        if not self._place(*target):
            width, height, x, y = target
            self.window.resize(width, height)
            self.window.move(x, y)

    def fit_opening_size(self) -> None:
        if getattr(self, "notch_mode", False):
            if not self.window:
                return
            self.sync_window_state()
            if self.maximized or self.minimized:
                self.window.restore()
                self.maximized = self.minimized = False
                # Fit is an explicit restoration request. Native visibility
                # can lag restore(), but the recenter must still be applied.
                self.visible = True
            self.recenter()
            return
        if not self.window:
            return
        self.sync_window_state()
        if self.maximized or self.minimized:
            self.window.restore()
            self.maximized = False
            self.minimized = False
        hwnd = self._native_window_handle()
        scale = _system_scale()
        if hwnd:
            with suppress(AttributeError, OSError, ctypes.ArgumentError):
                dpi = ctypes.windll.user32.GetDpiForWindow(ctypes.c_void_p(hwnd))
                if dpi:
                    scale = dpi / 96
        # Work areas are physical pixels; pywebview move/resize take logical
        # pixels. Compute one logical target, then place the exact native rect.
        area = tuple(round(value / scale) for value in _work_area(hwnd))
        width, height, x, y = _opening_bounds(area)
        target = tuple(round(value * scale) for value in (width, height, x, y))
        if not self._place(*target):
            self.window.resize(width, height)
            self.window.move(x, y)

    def resize(self, width, height) -> None:
        if not self.window or self.maximized or self.minimized:
            return
        try:
            width, height = max(300, round(float(width))), max(600, round(float(height)))
        except (TypeError, ValueError, OverflowError):
            return
        self.window.resize(width, height)

    def set_notch(self, expanded=False, height=None, reduced_motion=False, mode=None) -> None:
        if (not isinstance(expanded, bool) or not isinstance(reduced_motion, bool)
                or (mode is not None and (not isinstance(mode, str) or mode not in COMPACT_NOTCH_MODES))):
            return
        try:
            if mode is None:
                height = _notch_height(height)
            else:
                _width, height = _compact_notch_dimensions(mode, height)
        except ValueError:
            return
        if not self.window or not self.notch_mode or self.quitting:
            return
        with self._notch_lock:
            self.sync_window_state()
            self.compact_profile = mode is not None
            self._compact_notch_mode = mode
            if self.widget_locked and not self.compact_profile:
                # Lock state owns expansion intent. Content measurement may
                # still resize a locked-open widget while its native anchor stays fixed.
                if self.notch_expanded and height is not None:
                    self.notch_height = height
                if not self.visible or self.minimized or self._notch_user_dragging:
                    self._cancel_notch_transition()
                    return
                if self.maximized:
                    self.window.restore()
                    self.maximized = False
                self._apply_notch_bounds(animate=not reduced_motion,
                                         duration=NOTCH_TRANSITION_SECONDS,
                                         expanded=self.notch_expanded)
                return
            was_expanded = self.notch_expanded
            self.notch_expanded = expanded
            self.notch_height = height
            # A stale hover delivery must never surface a tray-hidden or
            # minimized window. Show/Fit explicitly handle restoration.
            if not self.visible or self.minimized or self._notch_user_dragging:
                self._cancel_notch_transition()
                return
            if self.maximized:
                self.window.restore()
                self.maximized = False
            duration = (COMPACT_NOTCH_TRANSITION_SECONDS if self.compact_profile and expanded else
                        COMPACT_NOTCH_CLOSE_TRANSITION_SECONDS if self.compact_profile else
                        NOTCH_OPEN_TRANSITION_SECONDS if expanded and not was_expanded else NOTCH_TRANSITION_SECONDS)
            self._apply_notch_bounds(animate=not reduced_motion, duration=duration)

    def _cancel_notch_transition(self) -> None:
        with self._notch_lock:
            self._notch_transition = None
            self._notch_wake.set()

    def _apply_notch_bounds(self, *, animate=True, duration=NOTCH_TRANSITION_SECONDS, expanded=None) -> None:
        if not self.window or self.quitting:
            return
        hwnd = self._native_window_handle()
        with self._notch_lock:
            if self.quitting:
                return
            native_target = self._notch_target(expanded=expanded)
            if self.widget_locked:
                self._widget_lock_render_position = (native_target[2], native_target[3])
            rect = self._native_rect() if animate and hwnd and _notch_animation_enabled() else None
            if rect and self.visible and not self.minimized and rect[0] > -30000 and rect[1] > -30000:
                start = (rect[2], rect[3], rect[0], rect[1])
                # Identical content measurements must not restart the animation.
                if self._notch_transition and self._notch_transition[1] == native_target:
                    return
                if start == native_target:
                    self._cancel_notch_transition()
                    return
                self._notch_transition = (start, native_target, time.monotonic(), duration)
                self._notch_wake.set()
                if self._notch_thread is None:
                    self._notch_thread = threading.Thread(target=self._animate_notch, daemon=True)
                    self._notch_thread.start()
                return
            self._cancel_notch_transition()
            self._record_notch_frame(native_target)
            if not self._place(*native_target):
                scale = _system_scale()
                width, height, x, y = (round(value / scale) for value in native_target)
                self._notch_placing = True
                try:
                    self.window.resize(width, height)
                    self.window.move(x, y)
                finally:
                    self._notch_placing = False

    def _animate_notch(self) -> None:
        transition = None
        sampled_transition = None
        last_frame = None
        next_sample_deadline = None
        try:
            while True:
                with self._notch_lock:
                    self._notch_wake.clear()
                    transition = self._notch_transition
                    if not transition or self.quitting or not self.notch_mode or not self.visible or self.minimized:
                        self._notch_transition = None
                        return
                    native = self._native_state()
                    if native and (not native["visible"] or native["minimized"] or native["maximized"]):
                        self._notch_transition = None
                        return
                    start, target, started, duration = transition
                    if transition is not sampled_transition:
                        sampled_transition = transition
                        last_frame = start
                        next_sample_deadline = started + NOTCH_FRAME_INTERVAL
                    progress = min(1.0, max(0.0, (time.monotonic() - started) / duration))
                    frame = target if progress >= 1 else _notch_transition_frame(start, target, progress)
                    if frame != last_frame or progress >= 1:
                        self._record_notch_frame(frame)
                        placed = self._place(*frame, asynchronous=True)
                        last_frame = frame
                    else:
                        placed = True
                    if not placed or progress >= 1:
                        self._notch_transition = None
                        if not placed:
                            return
                        return
                    after_placement = time.monotonic()
                    next_sample_deadline = _advance_notch_deadline(
                        next_sample_deadline, after_placement)
                    final_deadline = started + duration
                    wait_seconds = max(0.0, min(next_sample_deadline, final_deadline) - after_placement)
                self._notch_wake.wait(wait_seconds)
        except Exception:
            # Native teardown or an unavailable backend must not restart a
            # failed transition in an unbounded series of daemon workers.
            with self._notch_lock:
                if self._notch_transition is transition:
                    self._notch_transition = None
        finally:
            with self._notch_lock:
                self._notch_thread = None
                # A request can arrive after the last frame but before cleanup
                # acquires the lock. Preserve that new transition.
                if self._notch_transition and not self.quitting:
                    self._notch_thread = threading.Thread(target=self._animate_notch, daemon=True)
                    self._notch_thread.start()

    def quit(self, *_args) -> None:
        self._shutdown_requested.set()
        # Wait out any in-progress startup step; once quitting is set under the
        # lock no further startup resource is acquired, so the cleanup below
        # sees everything that exists.
        with self._lifecycle:
            self.quitting = True
        self._cancel_active_compact_drag()
        with self._notch_lock:
            self.quitting = True
            self._cancel_notch_transition()
        with suppress(Exception):
            self.save_bounds()
        self._stop_inspection_hotkey()
        # Each step is independent: one failure must not leave the process
        # alive with a destroyed tray or an undestroyed window.
        with suppress(Exception):
            self.limit_reset.stop()
        if self.tray:
            with suppress(Exception):
                self.tray.stop()
        # Revoke the settings capability, then destroy its window before the port is released.
        with self._reset_actions_lock:
            server_module.set_settings_token(None)
            self._reset_actions_token = None
        child, self.reset_actions_window = self.reset_actions_window, None
        if child is not None:
            with suppress(Exception):
                child.destroy()
        if self.server:
            with suppress(Exception):
                self.server.shutdown()
                self.server.server_close()
        with suppress(OSError):
            self.control.close()
        self._remove_control_nonce()
        if self.window:
            with suppress(Exception):
                self.window.destroy()

    def on_closing(self, *_args) -> bool:
        if self.quitting:
            return True
        self.hide()
        return False

    def _recenter_after_start(self, *_args) -> None:
        if self._recenter_on_start:
            self._recenter_on_start = False
            with self._notch_lock:
                selected = self._selected_monitor()
                if selected.get("_temporary_physical_fallback"):
                    self.selected_monitor_id = selected["id"]
                self._notch_manual_position = False
                self._notch_position = None
                self._notch_reference_width = None
                self._widget_lock_render_position = None
                self._remember_notch_placement()
                self._persist_widget_lock()
                if self.window and self.visible and not self.minimized:
                    self._apply_notch_bounds(animate=False)

    def open_reset_actions(self) -> bool:
        """Open or focus the user-requested reset action settings window."""
        with self._lifecycle:
            if self.quitting:
                return False
            return self._open_reset_actions_locked()

    def _open_reset_actions_locked(self) -> bool:
        child = self.reset_actions_window
        if child is not None:
            try:
                child.show()
                child.restore()
                child.focus()
                return True
            except Exception:
                self.reset_actions_window = None
        try:
            token = secrets.token_urlsafe(32)
            with self._reset_actions_lock:
                server_module.set_settings_token(token)
                self._reset_actions_token = token
            child = webview.create_window(
                "Reset action settings", f"{RESET_ACTIONS_URL}#t={token}",
                width=440, height=540, min_size=(360, 360), resizable=True,
                background_color="#111316",
            )
            self.reset_actions_window = child

            def on_closed(*_args):
                with self._reset_actions_lock:
                    if self.reset_actions_window is child:
                        self.reset_actions_window = None
                    if self._reset_actions_token == token:
                        self._reset_actions_token = None
                        server_module.set_settings_token(None)

            child.events.closed += on_closed
            return True
        except Exception:
            with self._reset_actions_lock:
                if self._reset_actions_token == locals().get("token"):
                    self._reset_actions_token = None
                    server_module.set_settings_token(None)
            logging.getLogger(__name__).warning("Could not open reset action settings")
            self.reset_actions_window = None
            return False

    def _startup_step(self, action):
        """Run one startup acquisition unless a shutdown was requested; serialized with quit()."""
        with self._lifecycle:
            if self.quitting or self._shutdown_requested.is_set():
                raise _StartupCancelled
            return action()

    def _finish_cancelled_startup(self) -> None:
        thread = self._maintenance_thread
        if thread is not None:
            thread.join(10)  # quit() releases everything acquired before the cancellation
        else:
            self.quit()

    def _tray_ready(self, icon) -> None:
        """pystray setup hook: show the icon, then report startup policy problems once."""
        icon.visible = True
        warnings = product.startup_policy_warnings()
        if self._appdata_redirected in (True, "redirected", "unknown") and not self._policy_notice_shown:
            self._policy_notice_shown = True
            with suppress(Exception):
                icon.notify("Branchglance may not see settings created by another app's terminal.", product.PRODUCT)
        if warnings and not self._policy_notice_shown:
            self._policy_notice_shown = True
            text = "Provider settings problem: " + "; ".join(warnings)
            with suppress(Exception):
                icon.notify(text[:250], product.PRODUCT)

    def _start_tray(self) -> None:
        inspection_menu = pystray.Menu(
            self._inspection_hotkey_menu_item("ctrl_shift_t", "Ctrl+Shift+T"),
            self._inspection_hotkey_menu_item("ctrl_alt_t", "Ctrl+Alt+T"),
            self._inspection_hotkey_menu_item("off", "Off"),
        )
        menu = pystray.Menu(
            pystray.MenuItem("Show / hide", self.toggle, default=True),
            pystray.MenuItem("Inspection shortcut", inspection_menu),
            pystray.MenuItem("Refresh", self.show),
            pystray.MenuItem("Quit", self.quit),
        )
        self.tray = pystray.Icon("branchglance", self.icon_image(), "Branchglance", menu)
        tray = self.tray
        threading.Thread(target=lambda: tray.run(setup=self._tray_ready), daemon=True).start()

    def run(self) -> None:
        # Installed storage must not be redirected (junctions into TokenTracker or
        # the legacy WebView2 profile); refuse before anything is written.
        try:
            try:
                product.validate_storage()
                self._webview_storage = product.prepare_webview_storage(product.webview_storage_path())
            except product.StorageRedirectedError as exc:
                show_message(product.PRODUCT, "Branchglance did not start because its data folder is "
                             f"redirected to another location, which it will not modify:\n\n{exc}")
                return
            if not self.claim_instance():
                return
            # The policy is already loaded by LimitResetService.__init__. The
            # control listener is now bound, so a waiting installer can safely
            # discover and stop this instance.
            self._release_startup_install_mutex()
            try:
                self._run_claimed()
            except _StartupCancelled:
                self._finish_cancelled_startup()
        finally:
            self._release_startup_install_mutex()

    def _release_startup_install_mutex(self) -> None:
        handle, self._startup_install_mutex = self._startup_install_mutex, None
        if handle is not None:
            product.release_secured_mutex(handle)

    def _run_claimed(self) -> None:
        if not self._startup_step(self.start_http):
            with suppress(OSError):
                self.control.close()
            self._remove_control_nonce()
            show_message(product.PRODUCT, "Branchglance could not start because its page server port "
                         f"({HTTP_PORT}) is in use by another program. If another Branchglance instance "
                         "is running, quit it from its tray icon and start Branchglance again.")
            return
        self._startup_step(self.limit_reset.start)
        self._startup_step(self._create_main_window)
        self._startup_step(self._start_tray)
        storage = self._startup_step(lambda: self._webview_storage)
        webview.start(gui="edgechromium", debug=False, private_mode=False, storage_path=str(storage),
                      icon=str(product.brand_icon_path()))

    def _create_main_window(self) -> None:
        window_width, window_height, x, y = self.initial_bounds()
        self.window = webview.create_window(
            "Branchglance",
            f"http://{HTTP_HOST}:{HTTP_PORT}/runway.html",
            width=window_width,
            height=window_height,
            x=x,
            y=y,
            hidden=self.start_hidden,
            background_color="#0b0c0e",
            frameless=True,
            transparent=True,
            easy_drag=False,
            on_top=True,
            resizable=True,
            min_size=(min(300, window_width),
                      min(100 if self.compact_profile else NOTCH_COLLAPSED_HEIGHT, window_height)
                      if self.notch_mode else 100),
            js_api=WindowApi(self.minimize, self.toggle_maximize, self.hide, self.size, self.resize,
                             self.fit_opening_size, self.set_notch, self.get_monitors, self.select_monitor, self.recenter,
                             self.begin_drag, self.end_drag, on_lock=self.set_widget_lock,
                             on_get_lock=self.get_widget_lock, on_reset_actions=self.open_reset_actions,
                             on_keep_open=self.set_keep_open, on_get_keep_open=self.get_keep_open,
                              on_compact_profile=lambda: self.compact_profile,
                              on_set_monitor_name=self.set_monitor_name),
        )
        self.window.events.before_show += self._set_native_background_fallback
        self.window.events.closing += self.on_closing
        # The native Handle does not exist until the GUI backend has loaded.
        self.window.events.loaded += self.apply_native_window_region
        # Resizing recreates the visible bounds, so keep the HWND region aligned.
        self.window.events.resized += self.apply_native_window_region
        # OS-driven state changes (Win+Up, taskbar, snap) keep the flags honest.
        self.window.events.maximized += self.on_maximized
        self.window.events.minimized += self.on_minimized
        self.window.events.restored += self.on_restored
        self.window.events.moved += self.on_moved
        self.window.events.resized += self.on_bounds_resized
        self.window.events.loaded += self.restore_saved_placement
        if self._recenter_on_start:
            self.window.events.loaded += self._recenter_after_start
        self.window.events.loaded += self._mark_window_loaded


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(description="Run the Branchglance widget")
    parser.add_argument("--background", action="store_true", help="start hidden in the tray")
    parser.add_argument("--recenter", action="store_true",
                        help="recenter the widget once on startup")
    parser.add_argument("--install-handoff", help=argparse.SUPPRESS)
    parser.add_argument("--redirect-relaunch", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--launcher-appdata-ok", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--launcher-appdata-unknown", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    # Wait for any setup transaction before taking the Instance lock or loading
    # provider policy. A timeout exits quietly so repeated Start-menu clicks do
    # not leave message-box processes behind.
    install_handle = None
    install_status = "busy"
    handoff_valid = False
    if args.install_handoff:
        handoff_valid = product.consume_install_handoff(args.install_handoff)
        if not handoff_valid:
            logging.getLogger(__name__).warning("Rejected invalid or expired install handoff; startup refused")
            raise SystemExit(2)
    if handoff_valid:
        # Bypass is accepted only while the owner that wrote the one-use marker
        # still holds Install; an invalid or stale token exited above.
        probe_handle, probe_status = claim_install_mutex(timeout=0)
        if probe_status == "busy":
            install_status = "acquired"
        elif probe_status == "acquired":
            install_handle, install_status = probe_handle, probe_status
    if install_status != "acquired":
        install_handle, install_status = claim_install_mutex()
    if install_status != "acquired":
        logging.getLogger(__name__).warning("Install mutex unavailable (%s); exiting without UI", install_status)
        return
    instance_handle = None
    product.set_app_user_model_id()
    try:
        instance_handle, status = claim_instance_mutex()
        if status == "busy":
            product.release_secured_mutex(install_handle)
            install_handle = None
            if not WidgetApp.notify_existing():
                show_message(product.PRODUCT, "Branchglance is already running; its control endpoint did not respond.")
            return
        if status == "failed":
            product.release_secured_mutex(install_handle)
            install_handle = None
            show_message(product.PRODUCT, "Branchglance could not check whether it is already running, "
                         "so it did not start. Try again in a moment.")
            return
        if status == "abandoned":
            recover_abandoned_instance()
        global _INSTANCE_HANDLE
        _INSTANCE_HANDLE = instance_handle  # held until the process exits
        redirected = ("unknown" if args.launcher_appdata_unknown else "not_redirected"
                      if args.launcher_appdata_ok or not product.is_frozen() else product.appdata_probe()[0])
        if redirected == "unknown":
            logging.getLogger(__name__).warning("AppData redirection probe is unknown; continuing with notice")
        app = WidgetApp(start_hidden=args.background, recenter_on_start=args.recenter)
        app._appdata_redirected = redirected
        app._startup_install_mutex = install_handle
        install_handle = None  # ownership transferred; WidgetApp releases after control bind
        app.run()
    finally:
        if install_handle is not None:
            product.release_secured_mutex(install_handle)


if __name__ == "__main__":
    main()
