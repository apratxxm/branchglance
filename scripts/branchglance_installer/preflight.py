from __future__ import annotations

import json
import os
import platform
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .core import SafetyError

WEBVIEW_KEYS = (
    ("HKLM", r"SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"),
    ("HKCU", r"SOFTWARE\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"),
)
MIN_WINDOWS_BUILD = 19041
MIN_DOTNET_RELEASE = 528040


@dataclass(frozen=True)
class HostInfo:
    windows: bool
    build: int
    x64: bool
    dotnet_release: int
    webview_pv: tuple[str, ...]
    elevated: bool = False


def check_host(host: HostInfo) -> None:
    failures = []
    if not host.windows or host.build < MIN_WINDOWS_BUILD:
        failures.append("Windows 10 build 19041 or newer is required")
    if not host.x64:
        failures.append("Branchglance v1 requires 64-bit x64 Windows")
    if host.dotnet_release < MIN_DOTNET_RELEASE:
        failures.append(".NET Framework 4.8 or newer is required")
    if not any(value and value != "0.0.0.0" for value in host.webview_pv):
        failures.append("Install the Microsoft Edge WebView2 Runtime, then retry")
    if host.elevated:
        failures.append("Run Branchglance setup as a standard user, not elevated")
    if failures:
        raise SafetyError("Preflight failed: " + "; ".join(failures))


def staged_self_test(release: Path, *, runner: Callable = subprocess.run, timeout: float = 30.0) -> dict:
    """Headless native check of a staged release before anything is promoted.

    The windowed app has no usable stdout, so it writes a JSON report file
    (pythonnet load + WebView2 version query; no ports, no windows).
    """
    exe = release / "BranchglanceApp.exe"
    if not exe.is_file():
        raise SafetyError("Staged release is missing BranchglanceApp.exe")
    import tempfile
    with tempfile.TemporaryDirectory(prefix="branchglance-selftest-") as folder:
        report = Path(folder) / "self-test.json"
        try:
            result = runner([str(exe), "--self-test", str(report)], cwd=release, capture_output=True,
                            timeout=max(0.1, float(timeout)), check=False)
        except subprocess.TimeoutExpired as exc:
            raise SafetyError("Staged self-test timed out") from exc
        try:
            payload = json.loads(report.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            payload = None
    if result.returncode != 0 or not isinstance(payload, dict) or payload.get("ok") is not True:
        raise SafetyError(f"Staged self-test failed (exit {result.returncode}): {payload}")
    return payload


def current_host() -> HostInfo:
    if sys.platform != "win32":
        return HostInfo(False, 0, False, 0, ())
    import ctypes
    import winreg
    build = int(platform.version().split(".")[-1])
    release = 0
    with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\NET Framework Setup\NDP\v4\Full") as key:
        release = int(winreg.QueryValueEx(key, "Release")[0])
    webview = []
    hive_names = {"HKLM": winreg.HKEY_LOCAL_MACHINE, "HKCU": winreg.HKEY_CURRENT_USER}
    for hive, path in WEBVIEW_KEYS:
        try:
            with winreg.OpenKey(hive_names[hive], path) as key:
                pv = str(winreg.QueryValueEx(key, "pv")[0]).strip()
                if pv:
                    webview.append(pv)
        except OSError:
            pass
    try:
        elevated = bool(ctypes.windll.shell32.IsUserAnAdmin())
    except (AttributeError, OSError):
        elevated = False
    return HostInfo(True, build, platform.machine().casefold() in ("amd64", "x86_64"), release,
                    tuple(webview), elevated)


def is_windows_reparse(path: Path) -> bool:
    return bool(path.lstat().st_file_attributes & 0x400)
