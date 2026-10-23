"""Branchglance setup and uninstall program (frozen as BranchglanceSetup.exe / BranchglanceUninstall.exe).

All transactional work is the fault-tested core: ReleaseOps (journal, recovery,
tools, pointer, prune), Integrations (shortcuts + HKCU uninstall entry) and
uninstall_owned_root. This module only wires the real adapters, the lifecycle
lock and the user-facing messages. Every native boundary is injectable.
"""
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import time
import uuid
import ctypes
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from token_tracker import product

from .core import (JOURNAL, LAUNCHER, MANIFEST, OWNER_FILE, UNINSTALLER, Journal, LocalFileSystem, ReleaseOps, SafetyError,
                   create_owner, is_reparse, read_json, require_owned_root, uninstall_owned_root, validate_version,
                   verify_manifest)
from .integration import Integrations
from .lifecycle import Lifecycle
from .preflight import HostInfo, check_host, staged_self_test

FOLDERID_DESKTOP = "B4BFCC3A-DB2C-424C-B029-7FE99A87C641"
FOLDERID_PROGRAMS = "A77F5D77-2E2B-44C3-A6A2-ABA601054A51"
FOLDERID_STARTUP = "B97D20BB-F46A-4C97-BA10-5E3608430854"


class _GUID(ctypes.Structure):
    _fields_ = [("Data1", ctypes.c_uint32), ("Data2", ctypes.c_uint16), ("Data3", ctypes.c_uint16),
                ("Data4", ctypes.c_ubyte * 8)]

    @classmethod
    def from_text(cls, text: str):
        import uuid as uuid_module
        value = uuid_module.UUID(text)
        result = cls()
        ctypes.memmove(ctypes.byref(result), value.bytes_le, 16)
        return result


def windows_known_folder(folder_id: str) -> Path | None:
    """Resolve a Windows known folder; return None on unsupported systems or API failure."""
    if os.name != "nt":
        return None
    shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    ole32 = ctypes.WinDLL("ole32", use_last_error=True)
    shell32.SHGetKnownFolderPath.argtypes = [ctypes.POINTER(_GUID), ctypes.c_uint32,
                                             ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]
    shell32.SHGetKnownFolderPath.restype = ctypes.c_long
    ole32.CoTaskMemFree.argtypes = [ctypes.c_void_p]
    output = ctypes.c_void_p()
    guid = _GUID.from_text(folder_id)
    hr = shell32.SHGetKnownFolderPath(ctypes.byref(guid), 0, None, ctypes.byref(output))
    if hr < 0 or not output.value:
        return None
    try:
        value = ctypes.wstring_at(output.value)
        return Path(value) if value else None
    finally:
        ole32.CoTaskMemFree(output)


@dataclass
class Paths:
    root: Path
    start_menu: Path
    startup: Path
    product_root: Path
    temp: Path
    desktop: Path | None = None
    home: Path | None = None

    @classmethod
    def for_user(cls, environ=None, *, known_folder_resolver=None) -> "Paths":
        env = os.environ if environ is None else environ
        local = Path(env["LOCALAPPDATA"])
        roaming = Path(env["APPDATA"])
        home = Path(env.get("USERPROFILE") or Path.home())
        resolve = known_folder_resolver or windows_known_folder
        def known(folder_id):
            try:
                return resolve(folder_id)
            except Exception:
                return None
        desktop = known(FOLDERID_DESKTOP) or home / "Desktop"
        menu = known(FOLDERID_PROGRAMS) or roaming / "Microsoft" / "Windows" / "Start Menu" / "Programs"
        startup = known(FOLDERID_STARTUP) or menu / "Startup"
        return cls(root=local / "Programs" / "Branchglance", start_menu=menu, startup=startup,
                   product_root=local / "Branchglance", temp=Path(env.get("TEMP") or env["LOCALAPPDATA"]),
                   desktop=desktop, home=home)


@dataclass
class Ui:
    quiet: bool = False
    box: Callable[..., int] | None = None

    def _show(self, text: str, flags: int) -> int:
        if self.quiet:
            print(text, file=sys.stderr)
            return 0
        if self.box is None:
            from .windows import message_box
            self.box = message_box
        return self.box(text, flags=flags)

    def info(self, text: str) -> None:
        self._show(text, 0x40)

    def error(self, text: str) -> None:
        self._show(text, 0x10)

    def confirm(self, text: str, *, default: bool) -> bool:
        if self.quiet:
            return default
        return self._show(text, 0x24) == 6  # MB_YESNO | MB_ICONQUESTION -> IDYES


@dataclass
class Native:
    """The host boundary. Tests replace any of these with fakes."""
    host: Callable[[], HostInfo]
    elevated: Callable[[], bool]
    mutex: object
    control: object
    processes: object
    integration_adapter: object
    runner: Callable = subprocess.run
    popen: Callable = subprocess.Popen
    after_mutation: Callable[[str], None] = field(default=lambda _name: None)
    file_system: object | None = None

    @classmethod
    def real(cls, paths: Paths) -> "Native":
        from .lifecycle import Win32MutexAdapter
        from .preflight import current_host
        from .windows import NoWait, WindowsControl, WindowsFileSystem, WindowsIntegrationAdapter, is_elevated
        return cls(host=current_host, elevated=is_elevated, mutex=Win32MutexAdapter(),
                   control=WindowsControl(paths.product_root / "state" / "control.nonce"),
                   processes=NoWait(), integration_adapter=WindowsIntegrationAdapter(),
                   file_system=WindowsFileSystem())


def _integrations(paths: Paths, native: Native, version: str) -> Integrations:
    return Integrations(native.integration_adapter, paths.root, paths.root / LAUNCHER, version,
                        paths.start_menu, paths.startup, paths.root / UNINSTALLER, paths.desktop)


def _claim_root(root: Path) -> None:
    """Create or adopt the install root; refuse anything that is not ours."""
    if os.path.lexists(root):
        if is_reparse(root) or not root.is_dir():
            raise SafetyError(f"The install folder is a link or a file: {root}")
        if not (root / OWNER_FILE).exists():
            if any(root.iterdir()):
                raise SafetyError(f"The install folder is not owned by Branchglance: {root}")
            create_owner(root)
        require_owned_root(root)
    else:
        create_owner(root)


def _prepare_appdata(paths: Paths) -> list[str]:
    local = paths.product_root.parent
    environment = {"LOCALAPPDATA": str(local)}
    probe_status, reason = product.appdata_probe(environment)
    if probe_status != "not_redirected":
        raise SafetyError("Run Branchglance Setup from File Explorer or the Start menu, not from inside another app's terminal. "
                          f"AppData probe: {probe_status}; {reason or 'destination could not be verified'}")
    stranded = [str(path) for path in product.stranded_appdata_paths(local)]
    paths.product_root.mkdir(parents=True, exist_ok=True)
    (paths.product_root / "claude-events").mkdir(parents=True, exist_ok=True)
    return stranded


class _AlreadyInstalled(Exception):
    def __init__(self, result: dict):
        self.result = result


def _installed_release_preflight(paths: Paths, staged_manifest: dict) -> dict | None:
    """Check immutable same-version identity while Install is held but before stopping the app."""
    root = paths.root
    if not os.path.lexists(root) or not (root / OWNER_FILE).exists():
        return None
    require_owned_root(root)
    journal = Journal(root).load()
    if journal is not None and journal.get("phase") not in {"committed", "rolled-back"}:
        return None  # normal recovery owns interrupted transactions
    version_full = staged_manifest["version"]
    final = root / "releases" / version_full
    if not final.exists():
        return None
    try:
        existing = verify_manifest(final, expected_version=version_full)
    except SafetyError as exc:
        raise SafetyError("An immutable release with this version exists but is unverifiable") from exc
    if existing.get("files") != staged_manifest.get("files"):
        raise SafetyError("An immutable release with this version exists with different bytes")
    pointer = ReleaseOps(root).current()
    if pointer is None or pointer.get("current", {}).get("version") != version_full:
        raise SafetyError("Identical release is already installed but is not current")
    return {"installed": version_full, "pointer": pointer, "recovered": "clean",
            "abandonedLock": False, "pruned": [], "alreadyInstalled": True}


def _raise_already_installed(paths: Paths, staged_manifest: dict) -> None:
    result = _installed_release_preflight(paths, staged_manifest)
    if result is not None:
        raise _AlreadyInstalled(result)


def _restart_after_install(paths: Paths, native: Native) -> None:
    launcher = paths.root / LAUNCHER
    try:
        if is_reparse(launcher) or not launcher.is_file():
            raise SafetyError("Installed launcher is missing or redirected")
        native.popen([str(launcher)], cwd=str(paths.root), close_fds=True)
        pointer = read_json(paths.root / "current.json")
        version = validate_version(pointer["current"]["version"])
        manifest = verify_manifest(paths.root / "releases" / version, expected_version=version)
        deadline = time.monotonic() + 9.0
        identity = None
        while time.monotonic() < deadline:
            try:
                candidate = native.control.health()
                if (candidate and candidate.get("product") == "Branchglance"
                        and type(candidate.get("pid")) is int
                        and candidate.get("build") == manifest.get("buildId")
                        and candidate.get("stateRoot") == str(paths.product_root / "state")):
                    identity = candidate
                    break
            except Exception:
                pass
            time.sleep(0.1)
        if identity is None:
            raise SafetyError("The previously running Branchglance did not return authenticated health")
    except Exception as exc:
        raise SafetyError(f"Setup could not restore the previously running Branchglance: {exc}") from exc


def install(paths: Paths, payload: Path, native: Native, *, startup: bool) -> dict:
    if native.elevated():
        raise SafetyError("Run Branchglance setup as a standard user, not as administrator.")
    check_host(native.host())
    release = payload / "release"
    manifest = verify_manifest(release)
    version_full = validate_version(manifest.get("version"))
    version, build_id = version_full.split("+", 1)
    tools = {LAUNCHER: payload / LAUNCHER, UNINSTALLER: payload / UNINSTALLER}
    holder: dict = {}

    def recover_abandoned() -> None:
        holder["abandoned"] = True  # install() always runs journal recovery first

    failure = None
    try:
        lifecycle = Lifecycle(native.mutex, native.control, native.processes, recover_abandoned,
                              before_stop=lambda: _raise_already_installed(paths, manifest))
        with lifecycle:
            journal_record = Journal(paths.root).load() if paths.root.exists() else None
            if journal_record and journal_record["operation"] == "uninstall":
                before_integration = journal_record["before"].get("integration", {})
                managed = before_integration.get("uninstallManaged", {}) if isinstance(before_integration, dict) else {}
                uninstall_version = managed.get("DisplayVersion", "0.0.0+unknown")
                try:
                    uninstall_version = validate_version(uninstall_version)
                except SafetyError:
                    uninstall_version = "0.0.0+unknown"
                integrations = _integrations(paths, native, uninstall_version)
                uninstall_owned_root(paths.root, integration_remove=integrations.remove,
                                     after_mutation=native.after_mutation, integration_snapshot=integrations.snapshot,
                                     fs=native.file_system or LocalFileSystem())
            _claim_root(paths.root)
            integrations = _integrations(paths, native, version_full)
            ops = ReleaseOps(paths.root, fs=native.file_system or LocalFileSystem(),
                             integration_snapshot=integrations.snapshot,
                             integration_restore=integrations.restore,
                             integration_apply=lambda _release: integrations.apply(startup_enabled=startup),
                             after_mutation=native.after_mutation, tools=tools,
                             self_test=lambda staged: staged_self_test(staged, runner=native.runner))
            recovered = ops.recover()
            try:
                result = ops.install(release, version, build_id)
            except Exception as install_error:
                try:
                    ops.recover()
                except Exception as recovery_error:
                    raise SafetyError(f"Setup failed ({install_error}); transaction restoration also failed "
                                      f"({recovery_error})") from recovery_error
                raise
            try:
                pruned = ops.prune()
            except (OSError, SafetyError) as exc:  # committed already; tidying must not fail setup
                pruned = [f"prune skipped: {exc}"]
    except _AlreadyInstalled as already:
        return already.result
    except BaseException as exc:
        failure = exc
    if "lifecycle" in locals() and lifecycle.shutdown_requested:
        if not lifecycle.shutdown_confirmed:
            try:
                lifecycle.confirm_late_exit(timeout=10.0)
            except Exception as late_exit_error:
                lifecycle.release_locks()
                if failure is not None:
                    failure = SafetyError(f"Setup failed ({failure}); the old app did not exit within the "
                                          f"restoration deadline ({late_exit_error})")
                else:
                    failure = SafetyError(f"The old app did not exit within the restoration deadline: {late_exit_error}")
        if lifecycle.shutdown_confirmed:
            lifecycle.relaunch_attempted = True
            lifecycle.release_locks()
            try:
                _restart_after_install(paths, native)
            except Exception as restart_error:
                if failure is not None:
                    raise SafetyError(f"Setup failed ({failure}); restoring the running app also failed ({restart_error})") from failure
                raise
        else:
            lifecycle.release_locks()
    if failure is not None:
        raise failure
    return {"installed": version_full, "pointer": result, "recovered": recovered,
            "abandonedLock": bool(holder.get("abandoned")), "pruned": pruned}


def start_uninstall(paths: Paths, native: Native, executable: Path, *, quiet: bool) -> Path:
    """Copy this uninstaller to the temp folder and run the copy (it can then remove the original)."""
    paths.temp.mkdir(parents=True, exist_ok=True)
    worker = paths.temp / f"Branchglance-uninstall-{uuid.uuid4().hex}.exe"
    shutil.copyfile(executable, worker)
    arguments = [str(worker), "--uninstall-worker"] + (["--quiet"] if quiet else [])
    native.popen(arguments, close_fds=True)
    return worker


def uninstall(paths: Paths, native: Native) -> dict:
    if native.elevated():
        raise SafetyError("Run the Branchglance uninstaller as a standard user, not as administrator.")
    with Lifecycle(native.mutex, native.control, native.processes, lambda: None):
        root = paths.root
        claude_home = Path(os.environ.get("CLAUDE_HOME") or (paths.home or Path.home()) / ".claude")
        claude_settings = claude_home / "settings.json"
        try:
            from token_tracker import claude_hook_install
            claude_integration = claude_hook_install.uninstall(
                settings_path=claude_settings, data_directory=paths.product_root,
                command=claude_hook_install.default_command(executable=paths.root / LAUNCHER))
        except Exception as exc:
            claude_integration = {"ok": False, "changed": False, "backup": None,
                                  "consentRemoved": False, "hooksLeft": "unknown",
                                  "error": f"{type(exc).__name__}: {exc}"}
        pointer = root / "current.json"
        version = "0.0.0+unknown"
        try:
            record = Journal(root).load()
            managed = record["before"].get("integration", {}).get("uninstallManaged", {}) if record else {}
            version = validate_version(managed.get("DisplayVersion") or read_json(pointer)["current"]["version"])
        except (SafetyError, KeyError, TypeError):
            pass
        integrations = _integrations(paths, native, version)
        if os.path.lexists(root):
            uninstall_owned_root(root, integration_remove=integrations.remove, after_mutation=native.after_mutation,
                                 integration_snapshot=integrations.snapshot,
                                 fs=native.file_system or LocalFileSystem())
        else:
            integrations.remove()
    return {"uninstalled": str(paths.root), "keptData": str(paths.product_root),
            "claudeIntegration": claude_integration}


def rollback(paths: Paths, native: Native) -> dict:
    with Lifecycle(native.mutex, native.control, native.processes, lambda: None):
        root = require_owned_root(paths.root)
        pointer = read_json(root / "current.json")
        version = validate_version(pointer["current"]["version"])
        integrations = _integrations(paths, native, version)
        ops = ReleaseOps(root, fs=native.file_system or LocalFileSystem(),
                         integration_snapshot=integrations.snapshot, integration_restore=integrations.restore,
                         integration_apply=lambda release: integrations.apply(startup_enabled=False),
                         after_mutation=native.after_mutation)
        ops.recover()
        return ops.rollback()


def _payload_dir() -> Path | None:
    base = getattr(sys, "_MEIPASS", None)
    if base and (Path(base) / "payload" / "release" / MANIFEST).is_file():
        return Path(base) / "payload"
    return None


def main(argv=None, *, role: str = "setup", paths: Paths | None = None, native: Native | None = None,
         ui: Ui | None = None, executable: Path | None = None) -> int:
    parser = argparse.ArgumentParser(prog=f"Branchglance{role.capitalize()}")
    parser.add_argument("--quiet", action="store_true", help="no message boxes; answer prompts with defaults")
    parser.add_argument("--with-startup", action="store_true", help="also start Branchglance at sign-in")
    parser.add_argument("--payload", type=Path, help="development only: unpacked payload folder")
    parser.add_argument("--uninstall", action="store_true")
    parser.add_argument("--uninstall-worker", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--rollback", action="store_true", help="restore the previous verified release")
    args = parser.parse_args(argv)
    ui = ui or Ui(quiet=args.quiet)
    paths = paths or Paths.for_user()
    native = native or Native.real(paths)
    executable = Path(executable or sys.executable)
    try:
        stranded_appdata = None
        if role == "setup" and not (args.uninstall or args.rollback or args.uninstall_worker):
            stranded_appdata = _prepare_appdata(paths)
        from .windows import cleanup_stale_workers
        cleanup_stale_workers(paths.temp, paths.product_root / "temp-workers.json")
        if args.uninstall_worker:
            import re
            if (executable.parent.resolve() != paths.temp.resolve()
                    or not re.fullmatch(r"Branchglance-uninstall-[0-9a-f]{32}\.exe", executable.name)
                    or is_reparse(executable) or not executable.is_file()):
                raise SafetyError("Uninstall worker is not the issued UUID-named temp executable")
            try:
                result = uninstall(paths, native)
                claude = result["claudeIntegration"]
                if claude.get("ok") and not claude.get("hooksLeft"):
                    ui.info("Branchglance and its Claude Code hooks were removed. Your settings and history were kept.")
                elif claude.get("ok"):
                    ui.info("Branchglance was removed. Mixed Claude Code hook groups were preserved. "
                            "Your settings and history were kept.")
                else:
                    ui.info("Branchglance was removed. Some Claude Code hooks could not be removed: "
                            f"{claude.get('error') or 'settings could not be updated'}. Your settings and history were kept.")
            finally:
                if getattr(sys, "frozen", False) or executable.name.startswith("Branchglance-uninstall-"):
                    from .windows import schedule_self_delete
                    try:
                        schedule_self_delete(executable, paths.temp, paths.product_root / "temp-workers.json")
                    except Exception:
                        pass  # worker cleanup must not turn a successful uninstall into failure
            return 0
        if args.rollback:
            result = rollback(paths, native)
            ui.info(f"Branchglance rolled back to {result['current']['version']}.")
            return 0
        if args.uninstall or role == "uninstall":
            if not ui.confirm("Remove Branchglance? Your settings and history are kept.", default=True):
                return 1
            start_uninstall(paths, native, executable, quiet=args.quiet)
            return 0
        payload = args.payload or _payload_dir()
        if payload is None:
            raise SafetyError("This setup program has no Branchglance release inside it.")
        startup = args.with_startup or ui.confirm("Start Branchglance automatically when you sign in?",
                                                  default=False)
        result = install(paths, payload, native, startup=startup)
        suffix = ""
        if stranded_appdata:
            suffix = " Older package-cache data was found and left untouched."
        ui.info(f"Branchglance is ready. Open it from the Start menu.{suffix}")
        return 0
    except (SafetyError, OSError) as exc:
        ui.error(f"Branchglance {'uninstall' if role == 'uninstall' or args.uninstall_worker else 'setup'} "
                 f"did not complete:\n\n{exc}")
        return 2
