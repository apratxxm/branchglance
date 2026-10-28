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
import secrets
import queue
import threading
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from token_tracker import product

from .core import (JOURNAL, LAUNCHER, MANIFEST, OWNER_FILE, SCHEMA, UNINSTALLER, Journal, LocalFileSystem,
                   ReleaseOps, SafetyError, atomic_json, create_owner, is_reparse, read_json, require_owned_root, uninstall_preflight,
                   uninstall_owned_root, validate_version, verify_manifest)
from .integration import Integrations
from .lifecycle import Lifecycle
from .preflight import HostInfo, check_host, staged_self_test

FOLDERID_DESKTOP = "B4BFCC3A-DB2C-424C-B029-7FE99A87C641"
FOLDERID_PROGRAMS = "A77F5D77-2E2B-44C3-A6A2-ABA601054A51"
FOLDERID_STARTUP = "B97D20BB-F46A-4C97-BA10-5E3608430854"
log = logging.getLogger(__name__)

SHCNE_UPDATEITEM = 0x00002000
SHCNE_ASSOCCHANGED = 0x08000000
SHCNF_IDLIST = 0x0000
SHCNF_PATHW = 0x0005


def _shell_notify_icons(executable: Path, shortcuts: tuple[Path, ...]) -> None:
    """Ask Explorer to refresh the installed executable and owned shortcuts."""
    try:
        shell32 = ctypes.WinDLL("shell32", use_last_error=True)
        notify = shell32.SHChangeNotify
        notify.argtypes = [ctypes.c_long, ctypes.c_uint, ctypes.c_void_p, ctypes.c_void_p]
        notify.restype = None
        notify(SHCNE_ASSOCCHANGED, SHCNF_IDLIST, None, None)
        for item in (executable, *shortcuts):
            notify(SHCNE_UPDATEITEM, SHCNF_PATHW, ctypes.c_wchar_p(str(item)), None)
    except Exception as exc:
        log.warning("Could not notify the Windows shell about Branchglance icon updates (%s)",
                    exc.__class__.__name__)


def _notify_installed_icons(paths: "Paths", native: "Native") -> None:
    executable = paths.root / LAUNCHER
    owned_shortcuts = []
    integrations = _integrations(paths, native, "")
    for shortcut in integrations.shortcut_paths:
        try:
            found = native.integration_adapter.read_shortcut(shortcut)
            if (found is not None and found.get("description") == "Branchglance managed shortcut"
                    and Path(found.get("target", "")).resolve() == executable.resolve()):
                owned_shortcuts.append(shortcut)
        except Exception as exc:
            log.warning("Could not verify shortcut before icon refresh (%s)", exc.__class__.__name__)
    try:
        _shell_notify_icons(executable, tuple(owned_shortcuts))
    except Exception as exc:
        # The shell refresh is cosmetic and must never turn a committed install into failure.
        log.warning("Branchglance icon refresh failed (%s)", exc.__class__.__name__)


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
        return self._show(text, 0x24 | (0 if default else 0x100)) == 6  # MB_YESNO | MB_ICONQUESTION -> IDYES


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
    restore_popen: Callable | None = None

    @classmethod
    def real(cls, paths: Paths) -> "Native":
        from .lifecycle import Win32MutexAdapter
        from .preflight import current_host
        from .windows import (NoWait, WindowsControl, WindowsFileSystem, WindowsIntegrationAdapter,
                              is_elevated, spawn_restore_job)
        return cls(host=current_host, elevated=is_elevated, mutex=Win32MutexAdapter(),
                   control=WindowsControl(paths.product_root / "state" / "control.nonce"),
                   processes=NoWait(), integration_adapter=WindowsIntegrationAdapter(),
                   file_system=WindowsFileSystem(), restore_popen=spawn_restore_job)


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


def _check_appdata(paths: Paths, action: str = "Branchglance Setup") -> None:
    local = paths.product_root.parent
    environment = {"LOCALAPPDATA": str(local)}
    probe_status, reason = product.appdata_probe(environment)
    if probe_status != "not_redirected":
        raise SafetyError(f"Run {action} from File Explorer or the Start menu, not from inside another app's terminal. "
                          f"AppData probe: {probe_status}; {reason or 'destination could not be verified'}")


def _prepare_appdata(paths: Paths) -> list[str]:
    _check_appdata(paths)
    local = paths.product_root.parent
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


def _integration_snapshot_bounded(paths: Paths, native: Native, version_full: str,
                                 deadline: float | None) -> dict:
    return _bounded_shortcut_read(lambda: _integrations(paths, native, version_full).snapshot(), deadline)


def _startup_enabled_bounded(paths: Paths, native: Native, deadline: float | None) -> bool:
    return _bounded_shortcut_read(lambda: _integrations(paths, native, "0.0.0+unknown").startup_enabled(), deadline)


def _bounded_shortcut_read(operation, deadline: float | None):
    if deadline is None:
        return operation()
    timeout = min(5.0, deadline - time.monotonic())
    if timeout <= 0:
        raise SafetyError("Quiet setup deadline expired during shortcut ownership preflight")
    result: queue.Queue = queue.Queue(maxsize=1)

    def read_only_snapshot():
        try:
            result.put((True, operation()))
        except BaseException as exc:
            result.put((False, exc))

    # IShellLink COM reads can hang on damaged/remote shortcut targets. A
    # daemon reader keeps quiet setup bounded; it has no write authority.
    threading.Thread(target=read_only_snapshot, daemon=True, name="Branchglance-shortcut-preflight").start()
    try:
        ok, value = result.get(timeout=timeout)
    except queue.Empty:
        raise SafetyError("Shortcut ownership preflight timed out; setup stopped before changing the installation") from None
    if not ok:
        raise value
    return value


def _setup_preflight(paths: Paths, native: Native, manifest: dict, version_full: str,
                     deadline: float | None = None, *, check_handoff: bool = False) -> None:
    """Refuse collisions and invalid install roots before asking a running app to stop."""
    _raise_already_installed(paths, manifest)
    root = paths.root
    if os.path.lexists(root):
        if is_reparse(root) or not root.is_dir():
            raise SafetyError(f"The install folder is a link or a file: {root}")
        if (root / OWNER_FILE).exists():
            require_owned_root(root)
        elif any(root.iterdir()):
            raise SafetyError(f"The install folder is not owned by Branchglance: {root}")
    handoff = root / ".cutover-handoff.json"
    if check_handoff and os.path.lexists(handoff):
        raise SafetyError(f"A Branchglance startup handoff is already pending: {handoff}")
    # This checks shortcut ownership and the uninstall registry without writing.
    _integration_snapshot_bounded(paths, native, version_full, deadline)


def _write_owned_restore_handoff(paths: Paths, version: str, token: str) -> dict:
    journal = Journal(paths.root)
    if journal.load() is not None:
        raise SafetyError("Cannot record setup restore ownership while another installer journal exists")
    pointer = ReleaseOps(paths.root).current()
    if pointer is None or pointer.get("current", {}).get("version") != version:
        raise SafetyError("Restore-handoff target is not the selected committed release")
    now = time.time()
    handoff_record = {"file": ".cutover-handoff.json", "nonce": token, "expires": now + 30.0}
    record = {"schemaVersion": SCHEMA, "id": uuid.uuid4().hex, "operation": "install", "target": version,
              "phase": "committed", "before": {"pointer": pointer, "integration": {}}, "created": now,
              "restoreHandoff": handoff_record}
    journal.save(record)  # Durable ownership is recorded before the marker is created.
    marker = paths.root / handoff_record["file"]
    if os.path.lexists(marker):
        raise SafetyError("A startup handoff appeared before setup could write its owned marker")
    atomic_json(marker, {"schemaVersion": 1, "nonce": token, "expires": handoff_record["expires"]})
    return record


def _read_restore_marker(path: Path) -> dict:
    if is_reparse(path) or not path.is_file() or path.stat().st_size > 1024:
        raise SafetyError("Setup startup handoff became unsafe; it was preserved")
    try:
        record = read_json(path)
    except (OSError, SafetyError) as exc:
        raise SafetyError("Setup startup handoff is malformed; it was preserved") from exc
    if (not isinstance(record, dict) or record.get("schemaVersion") != 1
            or not isinstance(record.get("nonce"), str) or type(record.get("expires")) not in (int, float)):
        raise SafetyError("Setup startup handoff is foreign or malformed; it was preserved")
    return record


def _recover_expired_restore_handoff(paths: Paths) -> bool:
    """Under Install exclusion, clear only an expired marker owned by a committed install journal."""
    journal = Journal(paths.root)
    record = journal.load() if paths.root.exists() else None
    marker = paths.root / ".cutover-handoff.json"
    ownership = record.get("restoreHandoff") if isinstance(record, dict) else None
    if ownership is None:
        return False
    if not os.path.lexists(marker):
        ReleaseOps(paths.root).recover()  # launcher already consumed it before the crash
        return True
    found = _read_restore_marker(marker)
    if (found.get("nonce") != ownership.get("nonce")
            or found.get("expires") != ownership.get("expires")):
        raise SafetyError("Setup restore handoff does not match its journal; marker was preserved")
    if time.time() < ownership["expires"]:
        raise SafetyError("A recorded setup restore handoff is still active; retry after it expires")
    marker.unlink()
    ReleaseOps(paths.root).recover()
    return True


def _clear_owned_restore_handoff(paths: Paths, token: str) -> None:
    journal = Journal(paths.root)
    record = journal.load() if paths.root.exists() else None
    ownership = record.get("restoreHandoff") if isinstance(record, dict) else None
    marker = paths.root / ".cutover-handoff.json"
    if os.path.lexists(marker):
        found = _read_restore_marker(marker)
        if not secrets.compare_digest(found.get("nonce", ""), token):
            raise SafetyError("Setup startup handoff ownership changed; it was preserved")
        marker.unlink()
    if ownership is not None:
        if not secrets.compare_digest(ownership.get("nonce", ""), token):
            raise SafetyError("Setup restore journal ownership changed; it was preserved")
        ReleaseOps(paths.root).recover()


def _restart_after_install(paths: Paths, native: Native, lifecycle: Lifecycle) -> None:
    launcher = paths.root / LAUNCHER
    handoff = paths.root / ".cutover-handoff.json"
    token = secrets.token_hex(32)
    process = None
    try:
        if lifecycle.install_handle is None or lifecycle.instance_handle is None:
            raise SafetyError("Setup lost lifecycle exclusion before restoring the app")
        if is_reparse(launcher) or not launcher.is_file():
            raise SafetyError("Installed launcher is missing or redirected")
        pointer = read_json(paths.root / "current.json")
        version = validate_version(pointer["current"]["version"])
        manifest = verify_manifest(paths.root / "releases" / version, expected_version=version)
        state_root = str(paths.product_root / "state")
        if os.path.lexists(handoff):
            raise SafetyError("A startup handoff appeared while setup held Install; it was preserved")
        _write_owned_restore_handoff(paths, version, token)
        # Install remains held. Only this child can pass the startup gate using
        # the one-use owner token; a waiting second setup cannot overtake restore.
        lifecycle.release_instance()
        arguments = [str(launcher), "--install-handoff", token]
        restore_popen = getattr(native, "restore_popen", None)
        if restore_popen is not None:
            process = restore_popen(arguments, cwd=str(paths.root))
        else:  # injectable test adapter; the real Windows adapter always uses a suspended Job launch
            process = native.popen(arguments, cwd=str(paths.root), close_fds=True,
                                   stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL)
        start = time.monotonic()
        deadline = start + 9.0
        if lifecycle.deadline is not None:
            deadline = min(deadline, lifecycle.deadline - 5.0)  # reserve time to terminate and confirm the tree
        identity = None
        while time.monotonic() < deadline:
            try:
                candidate = native.control.health()
            except Exception:
                candidate = None
            if candidate is not None:
                if (candidate.get("product") != "Branchglance"
                        or type(candidate.get("pid")) is not int
                        or candidate.get("build") != manifest.get("buildId")
                        or candidate.get("stateRoot") != state_root):
                    raise SafetyError("A live Branchglance did not match the restored release")
                identity = candidate
                break
            time.sleep(0.1)
        if identity is None:
            raise SafetyError("The restored Branchglance did not return authenticated health before deadline")
        if hasattr(process, "release_job"):
            process.release_job()  # authenticated app survives after setup exits
    except Exception as exc:
        if process is not None:
            termination_timeout = 5.0
            if lifecycle.deadline is not None:
                termination_timeout = max(0.0, min(termination_timeout, lifecycle.deadline - time.monotonic()))
            terminated = False
            try:
                terminate_tree = getattr(process, "terminate_tree", None)
                if terminate_tree is not None:
                    terminated = bool(terminate_tree(termination_timeout))
                    if not terminated:
                        # Preserve Install until the Job confirms no member remains.
                        # This fallback is reachable only when the bounded kill did
                        # not complete; releasing sooner can race a delayed child.
                        terminated = bool(terminate_tree(None))
                elif hasattr(process, "terminate"):
                    process.terminate()
                    process.wait(timeout=termination_timeout)
                    terminated = True
            except Exception:
                terminated = False
            if terminated:
                lifecycle.restore_child_terminated = True
                try:
                    if hasattr(process, "release_job"):
                        process.release_job()
                except Exception:
                    pass
            else:
                # Keep Install owned until this process exits. Closing the Job
                # on teardown kills the descendant tree instead of exposing a
                # newly committed pointer to a late old-build child.
                lifecycle.hold_install_until_process_exit = True
                try:
                    close_job = getattr(process, "close_job", None)
                    if close_job is not None:
                        close_job()
                except Exception:
                    pass
        raise SafetyError(f"Setup could not restore the previously running Branchglance: {exc}") from exc
    finally:
        _clear_owned_restore_handoff(paths, token)


def install(paths: Paths, payload: Path, native: Native, *, startup: bool,
            deadline: float | None = None) -> dict:
    if native.elevated():
        raise SafetyError("Run Branchglance setup as a standard user, not as administrator.")
    check_host(native.host())
    release = payload / "release"
    manifest = verify_manifest(release)
    version_full = validate_version(manifest.get("version"))
    version, build_id = version_full.split("+", 1)
    tools = {LAUNCHER: payload / LAUNCHER, UNINSTALLER: payload / UNINSTALLER}
    holder: dict = {}

    # Fast, read-only refusal before Lifecycle can send maintenance-exit.
    try:
        _setup_preflight(paths, native, manifest, version_full, deadline)
    except _AlreadyInstalled as already:
        return already.result

    def pre_stop() -> None:
        _recover_expired_restore_handoff(paths)
        _setup_preflight(paths, native, manifest, version_full, deadline, check_handoff=True)
        test_timeout = 30.0
        if deadline is not None:
            test_timeout = deadline - time.monotonic() - 9.0  # reserve bounded restore health time
            if test_timeout <= 0:
                raise SafetyError("Quiet setup deadline leaves no time for staged-release preflight")
            test_timeout = min(test_timeout, 15.0)
        staged_self_test(release, runner=native.runner, timeout=test_timeout)

    def recover_abandoned() -> None:
        holder["abandoned"] = True  # install() always runs journal recovery first

    failure = None
    try:
        lifecycle = Lifecycle(native.mutex, native.control, native.processes, recover_abandoned,
                              before_stop=pre_stop,
                              deadline=deadline)
        with lifecycle:
            try:
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
                                 self_test=lambda _staged: None)  # source release was tested before shutdown
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
            except BaseException as exc:
                failure = exc
            if lifecycle.shutdown_confirmed:
                lifecycle.relaunch_attempted = True
                try:
                    _restart_after_install(paths, native, lifecycle)
                except Exception as restart_error:
                    if failure is not None:
                        failure = SafetyError(f"Setup failed ({failure}); restoring the running app also failed "
                                              f"({restart_error})")
                    else:
                        failure = restart_error
    except _AlreadyInstalled as already:
        return already.result
    except BaseException as exc:
        if failure is None:
            failure = exc
    if "lifecycle" in locals():
        lifecycle.release_locks()
    if failure is not None:
        raise failure
    _notify_installed_icons(paths, native)
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
    _check_appdata(paths, "the Branchglance uninstaller")
    if native.elevated():
        raise SafetyError("Run the Branchglance uninstaller as a standard user, not as administrator.")
    # Refuse before stopping the app or touching Claude settings wherever removal would refuse.
    uninstall_preflight(paths.root, verify_releases=True)
    with Lifecycle(native.mutex, native.control, native.processes, lambda: None):
        root = paths.root
        uninstall_preflight(root, verify_releases=True)
        try:
            from token_tracker import claude_hook_install
            claude_settings = claude_hook_install.claude_config_dir(paths.home) / "settings.json"
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
        completed = []
        def report_mutation(name: str) -> None:
            if name == "integrations-removed":
                completed.append("managed shortcuts and registry entries")
            elif name in {"uninstall-release-trashed", "uninstall-release-deleted"}:
                completed.append("program release files")
            elif name == "ledger-deleted":
                completed.append("install ledger")
            elif name in {"root-removed", "owner-marker-deleted"}:
                completed.append(name.replace("-", " "))
            native.after_mutation(name)
        try:
            if os.path.lexists(root):
                uninstall_owned_root(root, integration_remove=integrations.remove, after_mutation=report_mutation,
                                     integration_snapshot=integrations.snapshot,
                                     fs=native.file_system or LocalFileSystem())
            else:
                integrations.remove()  # ownership-checked; cleans up after a removed install folder
                completed.append("managed shortcuts and registry entries")
        except Exception as exc:
            removed = []
            if claude_integration.get("consentRemoved"):
                removed.append("Claude consent")
            if claude_integration.get("changed"):
                removed.append("Claude hooks")
            removed.extend(completed)
            detail = ", ".join(removed) if removed else "no confirmed items"
            raise SafetyError(f"Uninstall failed; already removed or changed: {detail}. Other steps may have partially completed: {exc}") from exc
    return {"uninstalled": str(paths.root), "keptData": str(paths.product_root),
            "claudeIntegration": claude_integration}


def rollback(paths: Paths, native: Native) -> dict:
    _check_appdata(paths, "Branchglance rollback")
    with Lifecycle(native.mutex, native.control, native.processes, lambda: None):
        root = require_owned_root(paths.root)
        pointer = read_json(root / "current.json")
        version = validate_version(pointer["current"]["version"])
        integrations = _integrations(paths, native, version)
        ops = ReleaseOps(root, fs=native.file_system or LocalFileSystem(),
                         integration_snapshot=integrations.snapshot, integration_restore=integrations.restore,
                         integration_apply=lambda release: integrations.apply(startup_enabled=integrations.startup_enabled()),
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
    wait_deadline = time.monotonic() + 30.0 if args.quiet else None
    try:
        stranded_appdata = None
        if role == "setup" and not (args.uninstall or args.rollback or args.uninstall_worker):
            stranded_appdata = _prepare_appdata(paths)
        else:
            _check_appdata(paths, "Branchglance rollback" if args.rollback else "the Branchglance uninstaller")
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
        startup = args.with_startup or ui.confirm(
            "Start Branchglance automatically when you sign in? Choose No to turn this off.",
            default=_startup_enabled_bounded(paths, native, wait_deadline))
        result = install(paths, payload, native, startup=startup, deadline=wait_deadline)
        suffix = ""
        if stranded_appdata:
            suffix = " Older package-cache data was found and left untouched."
        ui.info(f"Branchglance is ready. Open it from the Start menu.{suffix}")
        return 0
    except (SafetyError, OSError) as exc:
        ui.error(f"Branchglance {'uninstall' if role == 'uninstall' or args.uninstall_worker else 'setup'} "
                 f"did not complete:\n\n{exc}")
        return 2
