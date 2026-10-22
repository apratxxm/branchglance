"""Branchglance setup and uninstall program (frozen as BranchglanceSetup.exe / BranchglanceUninstall.exe).

All transactional work is the fault-tested core: ReleaseOps (journal, recovery,
tools, pointer, prune), Integrations (shortcuts + HKCU uninstall entry) and
uninstall_owned_root. This module only wires the real adapters, the lifecycle
lock and the user-facing messages. Every native boundary is injectable.
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .core import (JOURNAL, LAUNCHER, MANIFEST, OWNER_FILE, UNINSTALLER, Journal, LocalFileSystem, ReleaseOps, SafetyError,
                   create_owner, is_reparse, read_json, require_owned_root, uninstall_owned_root, validate_version)
from .integration import Integrations
from .lifecycle import Lifecycle
from .preflight import HostInfo, check_host, staged_self_test


@dataclass
class Paths:
    root: Path
    start_menu: Path
    startup: Path
    product_root: Path
    temp: Path

    @classmethod
    def for_user(cls, environ=None) -> "Paths":
        env = os.environ if environ is None else environ
        local = Path(env["LOCALAPPDATA"])
        roaming = Path(env["APPDATA"])
        menu = roaming / "Microsoft" / "Windows" / "Start Menu" / "Programs"
        return cls(root=local / "Programs" / "Branchglance", start_menu=menu, startup=menu / "Startup",
                   product_root=local / "Branchglance", temp=Path(env.get("TEMP") or env["LOCALAPPDATA"]))


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
                        paths.start_menu, paths.startup, paths.root / UNINSTALLER)


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


def install(paths: Paths, payload: Path, native: Native, *, startup: bool) -> dict:
    if native.elevated():
        raise SafetyError("Run Branchglance setup as a standard user, not as administrator.")
    check_host(native.host())
    release = payload / "release"
    manifest = read_json(release / MANIFEST)
    version_full = validate_version(manifest.get("version"))
    version, build_id = version_full.split("+", 1)
    tools = {LAUNCHER: payload / LAUNCHER, UNINSTALLER: payload / UNINSTALLER}
    holder: dict = {}

    def recover_abandoned() -> None:
        holder["abandoned"] = True  # install() always runs journal recovery first

    with Lifecycle(native.mutex, native.control, native.processes, recover_abandoned):
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
        result = ops.install(release, version, build_id)
        try:
            pruned = ops.prune()
        except (OSError, SafetyError) as exc:  # committed already; tidying must not fail setup
            pruned = [f"prune skipped: {exc}"]
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
        pointer = root / "current.json"
        version = "0.0.0+unknown"
        try:
            record = Journal(root).load()
            managed = record["before"].get("integration", {}).get("uninstallManaged", {}) if record else {}
            version = validate_version(managed.get("DisplayVersion") or read_json(pointer)["current"]["version"])
        except (SafetyError, KeyError, TypeError):
            pass
        integrations = _integrations(paths, native, version)
        uninstall_owned_root(root, integration_remove=integrations.remove, after_mutation=native.after_mutation,
                             integration_snapshot=integrations.snapshot,
                             fs=native.file_system or LocalFileSystem())
    return {"uninstalled": str(paths.root), "keptData": str(paths.product_root)}


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
        from .windows import cleanup_stale_workers
        cleanup_stale_workers(paths.temp, paths.product_root / "temp-workers.json")
        if args.uninstall_worker:
            import re
            if (executable.parent.resolve() != paths.temp.resolve()
                    or not re.fullmatch(r"Branchglance-uninstall-[0-9a-f]{32}\.exe", executable.name)
                    or is_reparse(executable) or not executable.is_file()):
                raise SafetyError("Uninstall worker is not the issued UUID-named temp executable")
            try:
                uninstall(paths, native)
                ui.info("Branchglance was removed. Your settings and history were kept.")
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
        ui.info(f"Branchglance {result['installed']} is installed. Open it from the Start menu.")
        return 0
    except (SafetyError, OSError) as exc:
        ui.error(f"Branchglance {'uninstall' if role == 'uninstall' or args.uninstall_worker else 'setup'} "
                 f"did not complete:\n\n{exc}")
        return 2
