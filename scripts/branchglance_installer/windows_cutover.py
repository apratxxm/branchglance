from __future__ import annotations

import ctypes
import json
import os
import socket
import subprocess
import time
from pathlib import Path

from .cutover import Cutover, STATE_FILES
from .core import SafetyError, atomic_json, read_json, safe_child, verify_manifest
from .windows import WindowsFileSystem

PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
SYNCHRONIZE = 0x00100000
WAIT_OBJECT_0 = 0
WAIT_TIMEOUT = 0x102


class WindowsCutoverAdapter:
    """Native adapter used only by an explicit user-attended cutover command."""
    def __init__(self, old_pid: int, old_shortcut: Path, renamed_shortcut: Path,
                 expected_target: Path, expected_workdir: Path, product_root: Path, control_port: int = 8766):
        self.old_pid, self.old_shortcut, self.renamed_shortcut = old_pid, old_shortcut, renamed_shortcut
        self.expected_target, self.expected_workdir, self.product_root = expected_target, expected_workdir, product_root
        self.launcher_pid = None
        self.pending_app_pid = None
        self.launch_pending = False
        self.instance_mutex_held = False
        self.branch_pid = None
        self.branch_handle = None
        self.control_port = control_port
        self.file_system = WindowsFileSystem()
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True) if os.name == "nt" else None
        if os.name == "nt":
            kernel = self.kernel
            kernel.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
            kernel.OpenProcess.restype = ctypes.c_void_p
            kernel.QueryFullProcessImageNameW.argtypes = [ctypes.c_void_p, ctypes.c_ulong,
                ctypes.c_wchar_p, ctypes.POINTER(ctypes.c_ulong)]
            kernel.QueryFullProcessImageNameW.restype = ctypes.c_int
            kernel.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
            kernel.WaitForSingleObject.restype = ctypes.c_ulong
            kernel.CloseHandle.argtypes = [ctypes.c_void_p]
            kernel.CloseHandle.restype = ctypes.c_int

    def old_pythonw_running(self):
        kernel = self.kernel
        handle = kernel.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION | SYNCHRONIZE, False, self.old_pid)
        if not handle:
            error = ctypes.get_last_error()
            if error == 87:
                return False
            raise OSError(error, "Cannot inspect the supplied old Python process")
        try:
            state = kernel.WaitForSingleObject(ctypes.c_void_p(handle), 0)
            if state == WAIT_OBJECT_0:
                return False
            if state != WAIT_TIMEOUT:
                raise OSError(ctypes.get_last_error(), "Cannot read old process state")
            buffer = ctypes.create_unicode_buffer(32768)
            length = ctypes.c_ulong(len(buffer))
            if not kernel.QueryFullProcessImageNameW(ctypes.c_void_p(handle), 0, buffer, ctypes.byref(length)):
                raise OSError(ctypes.get_last_error(), "Cannot verify old process image")
            if Path(buffer.value).resolve() != self.expected_target.resolve():
                raise SafetyError("Supplied PID is not the inventoried Python executable")
            return True
        finally:
            kernel.CloseHandle(ctypes.c_void_p(handle))

    def port_is_free(self, port):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            try:
                probe.bind(("127.0.0.1", port))
                return True
            except OSError:
                return False

    def _read_shortcut(self, path):
        from .windows import WindowsIntegrationAdapter
        result = WindowsIntegrationAdapter().read_shortcut(path)
        if result is None:
            raise SafetyError("Shortcut is missing")
        return result

    def snapshot_old_shortcut(self):
        path = self.old_shortcut if self.old_shortcut.exists() else self.renamed_shortcut
        if not path.exists():
            raise SafetyError("Inventoried Token Tracker shortcut is missing")
        value = self._read_shortcut(path)
        if (Path(value["target"]).resolve() != self.expected_target.resolve()
                or Path(value["workdir"]).resolve() != self.expected_workdir.resolve()
                or value["arguments"] != "-m token_tracker.widget"):
            raise SafetyError("Old shortcut changed from the inventoried target; refusing cutover")
        return {**value, "path": str(path)}

    def rename_old_shortcut(self, snapshot):
        if self.renamed_shortcut.exists():
            raise SafetyError("Old shortcut backup name is occupied")
        self.file_system.replace(self.old_shortcut, self.renamed_shortcut)

    def _write_link(self, path, target, arguments, workdir, description):
        from .windows import WindowsIntegrationAdapter
        if path.exists():
            raise SafetyError("Branchglance shortcut already exists; refusing overwrite")
        WindowsIntegrationAdapter().replace_shortcut(path, target=target, arguments=arguments,
            workdir=workdir, app_id="Branchglance.App", description=description)

    def create_branchglance_shortcut(self, record):
        desktop = Path(record["path"])
        self._write_link(desktop, record["target"], record["arguments"], record["workdir"], record["description"])
        self._branch_shortcut = desktop
        return {**record, "path": str(desktop)}

    def remove_branchglance_shortcut(self, record=None):
        path = Path(record["path"]) if record and record.get("path") else getattr(
            self, "_branch_shortcut", Path(os.environ.get("USERPROFILE", Path.home())) / "Desktop" / "Branchglance.lnk")
        if record and path.exists():
            info = self._read_shortcut(path)
            if all(info.get(key) == record.get(key) for key in ("target", "arguments", "workdir", "description")):
                from .windows import WindowsIntegrationAdapter
                WindowsIntegrationAdapter().remove_shortcut(path)

    def start_branchglance(self):
        launcher = self.product_root.parent / "Programs" / "Branchglance" / "Branchglance.exe"
        self.launch_pending = True
        try:
            process = subprocess.Popen([str(launcher)], cwd=launcher.parent, close_fds=True)
        except BaseException:
            self.launch_pending = False
            raise
        self.launcher_pid = process.pid
        self.pending_app_pid = None
        self.branch_pid = None
        self.branch_handle = None
        return process.pid

    def identity(self):
        from token_tracker import product
        deadline = time.monotonic() + 9.0
        last_error = None
        nonce_path = self.product_root / "state" / "control.nonce"
        while time.monotonic() < deadline:
            try:
                nonce = product.read_nonce_file(nonce_path)
                if nonce is None:
                    last_error = "control nonce is not available yet"
                    time.sleep(0.05)
                    continue
                who = product.control_health(nonce, port=self.control_port, timeout=min(0.4, max(0.05, deadline-time.monotonic())))
                self.pending_app_pid = who.get("pid")
                root = self.product_root.parent / "Programs" / "Branchglance"
                pointer = read_json(root / "current.json")
                release = manifest = None
                for ref in (pointer.get("current"), pointer.get("previous")):
                    if not isinstance(ref, dict):
                        continue
                    try:
                        candidate = safe_child(root, f"releases/{ref['version']}")
                        candidate_manifest = verify_manifest(candidate, expected_version=ref["version"])
                    except (KeyError, SafetyError):
                        continue
                    release, manifest = candidate.resolve(), candidate_manifest
                    break
                if release is None or manifest is None:
                    raise SafetyError("Neither selected Branchglance release validates")
                if (who.get("product") != "Branchglance" or who.get("build") != manifest.get("buildId")
                        or who.get("stateRoot") != str(self.product_root / "state")):
                    raise SafetyError("Authenticated health identity does not match the selected release")
                handle = self.kernel.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION | SYNCHRONIZE, False, who["pid"])
                if not handle:
                    raise OSError(ctypes.get_last_error(), "Cannot open authenticated Branchglance process")
                buffer = ctypes.create_unicode_buffer(32768)
                length = ctypes.c_ulong(len(buffer))
                if not self.kernel.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(length)):
                    self.kernel.CloseHandle(handle)
                    raise OSError(ctypes.get_last_error(), "Cannot verify Branchglance process image")
                image = Path(buffer.value).resolve()
                if os.path.commonpath((str(release), str(image))) != str(release):
                    self.kernel.CloseHandle(handle)
                    raise SafetyError("Authenticated Branchglance PID is outside the selected release")
                if self.branch_handle:
                    self.kernel.CloseHandle(self.branch_handle)
                self.branch_handle, self.branch_pid = handle, int(who["pid"])
                self.launch_pending = False
                return who
            except (OSError, KeyError, TypeError, ValueError, SafetyError) as exc:
                last_error = exc
                time.sleep(0.1)
        raise SafetyError(f"Branchglance did not pass authenticated health before deadline: {last_error}")

    def branchglance_running(self):
        if self.branch_handle is not None:
            state = self.kernel.WaitForSingleObject(self.branch_handle, 0)
            if state == WAIT_TIMEOUT:
                return True
            if state == WAIT_OBJECT_0:
                return False
            raise OSError(ctypes.get_last_error(), "Cannot determine verified Branchglance process state")
        if self.launch_pending or self.pending_app_pid is not None:
            raise SafetyError("Branchglance launch is pending but its process is not verified")
        if self.instance_mutex_held:
            return False
        if self.pending_app_pid is None:
            try:
                self.identity()
                return True
            except (OSError, SafetyError) as exc:
                raise SafetyError(f"Branchglance health is unavailable; process exit is unverified: {exc}") from exc
        raise SafetyError("Branchglance process handle is unavailable; process exit is unverified")

    def quit_branchglance(self):
        nonce_path = self.product_root / "state" / "control.nonce"
        try:
            # Challenge-response: the listener must prove it holds the installed
            # app's nonce before it receives a command; the nonce is never sent.
            from token_tracker import product
            nonce = product.read_nonce_file(nonce_path)
            reply = product.control_command("maintenance-exit", nonce, port=self.control_port, timeout=2)
            if reply not in ("ok", None):
                return False
            if self.branch_handle is None:
                return False  # unverified handle is never evidence of exit
            return self.kernel.WaitForSingleObject(self.branch_handle, 20000) == WAIT_OBJECT_0
        except (OSError, ValueError):
            return False

    def restore_old_shortcut(self, snapshot):
        if self.renamed_shortcut.exists():
            backup = self._read_shortcut(self.renamed_shortcut)
            fields = ("target", "arguments", "workdir", "description", "app_id")
            if any(backup.get(key) != snapshot.get(key) for key in fields):
                raise SafetyError("Old shortcut backup does not match the recorded snapshot; refusing restore")
            if self.old_shortcut.exists():
                raise SafetyError("Old shortcut destination is occupied; refusing to overwrite it")
            self.file_system.replace(self.renamed_shortcut, self.old_shortcut)

    def start_old_app(self, snapshot):
        subprocess.Popen([snapshot["target"], *snapshot["arguments"].split()], cwd=snapshot["workdir"], close_fds=True)


def migrate_state_copy(directory: Path) -> None:
    path = directory / "limit-reset-state.json"
    if not path.exists():
        return
    data = read_json(path)
    state = data.get("resetAction")
    if not isinstance(state, dict) or state.get("schemaVersion") == 2:
        return
    from token_tracker.reset_actions import _valid_ids, default_action
    ids = []
    saved_ids = state.get("threadIds")
    if isinstance(saved_ids, list):
        for value in saved_ids:
            try:
                valid = _valid_ids([value])
            except (TypeError, ValueError):
                continue
            if valid and valid[0] not in ids:
                ids.append(valid[0])
    migrated = default_action()
    migrated.update({"mode": "continue" if ids else "greet", "threadIds": ids, "armed": False,
                     "lastRun": state.get("lastRun"), "ledger": state.get("ledger", {"cycles": []})})
    data["resetAction"] = migrated
    atomic_json(path, data)
