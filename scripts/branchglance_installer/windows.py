"""Native Windows adapters for the installer core (no pywin32, no pythonnet).

Shortcuts use the shell's IShellLinkW / IPersistFile / IPropertyStore COM
interfaces called through their vtables with ctypes, so the AppUserModelID is
written into the .lnk. The uninstall registration is plain HKCU via winreg; the
key path is a parameter so tests use a throwaway test key.
"""
from __future__ import annotations

import ctypes
import json
import os
import subprocess
import sys
import uuid
from ctypes import wintypes
from pathlib import Path

from .core import SafetyError, is_reparse

UNINSTALL_KEY = r"Software\Microsoft\Windows\CurrentVersion\Uninstall\Branchglance"


def move_file_ex(source: Path, target: Path, *, flags: int = 0x1 | 0x8) -> None:
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.MoveFileExW.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p, wintypes.DWORD]
    kernel.MoveFileExW.restype = wintypes.BOOL
    if not kernel.MoveFileExW(str(source), str(target), flags):
        code = ctypes.get_last_error()
        raise OSError(code, ctypes.FormatError(code), str(target))


class WindowsFileSystem:
    """Owned-root filesystem adapter; same-volume renames use MoveFileExW."""
    MOVEFILE_REPLACE_EXISTING = 0x1
    MOVEFILE_WRITE_THROUGH = 0x8

    def exists(self, path: Path) -> bool:
        return os.path.lexists(path)

    def replace(self, source: Path, target: Path, *, mutation: str = "rename", after=None) -> None:
        move_file_ex(source, target, flags=self.MOVEFILE_REPLACE_EXISTING | self.MOVEFILE_WRITE_THROUGH)
        if after is not None:
            after(mutation)

    def remove_tree(self, path: Path) -> None:
        from .core import remove_plain_tree
        remove_plain_tree(Path(path))


class _GUID(ctypes.Structure):
    _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD), ("Data3", wintypes.WORD),
                ("Data4", ctypes.c_ubyte * 8)]

    @classmethod
    def parse(cls, text: str) -> "_GUID":
        value = uuid.UUID(text)
        guid = cls()
        ctypes.memmove(ctypes.byref(guid), value.bytes_le, 16)
        return guid


class _PROPERTYKEY(ctypes.Structure):
    _fields_ = [("fmtid", _GUID), ("pid", wintypes.DWORD)]


class _PROPVARIANT(ctypes.Structure):
    # vt + 3 reserved words, then a 16-byte union (pointer-sized members first).
    _fields_ = [("vt", ctypes.c_ushort), ("r1", ctypes.c_ushort), ("r2", ctypes.c_ushort), ("r3", ctypes.c_ushort),
                ("pwszVal", ctypes.c_void_p), ("pad", ctypes.c_void_p)]


CLSID_ShellLink = "00021401-0000-0000-C000-000000000046"
IID_IShellLinkW = "000214F9-0000-0000-C000-000000000046"
IID_IPersistFile = "0000010b-0000-0000-C000-000000000046"
IID_IPropertyStore = "886D8EEB-8CF2-4446-8D02-CDBA1DBDCF99"
PKEY_AppUserModel_ID = ("9F4C2855-9F79-4B39-A8D0-E1D42DE1D5F3", 5)
VT_LPWSTR = 31
_HRESULT = ctypes.c_long


class _Com:
    """Minimal vtable caller for one COM interface pointer."""

    def __init__(self, pointer: ctypes.c_void_p) -> None:
        self.pointer = pointer

    def call(self, index: int, *args_and_types):
        types = [ctypes.c_void_p] + [t for _, t in args_and_types]
        vtable = ctypes.cast(self.pointer, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
        function = ctypes.WINFUNCTYPE(_HRESULT, *types)(vtable[index])
        result = function(self.pointer, *[value for value, _ in args_and_types])
        if result < 0:
            raise OSError(result & 0xFFFFFFFF, f"COM call {index} failed (0x{result & 0xFFFFFFFF:08X})")
        return result

    def query(self, iid: str) -> "_Com":
        out = ctypes.c_void_p()
        self.call(0, (ctypes.byref(_GUID.parse(iid)), ctypes.c_void_p), (ctypes.byref(out), ctypes.c_void_p))
        return _Com(out)

    def release(self) -> None:
        if self.pointer:
            vtable = ctypes.cast(self.pointer, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
            ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)(vtable[2])(self.pointer)
            self.pointer = None


def _co_initialize() -> None:
    ole32 = ctypes.windll.ole32
    ole32.CoInitializeEx.argtypes = [ctypes.c_void_p, wintypes.DWORD]
    ole32.CoInitializeEx.restype = _HRESULT
    ole32.CoInitializeEx(None, 0x2)  # apartment-threaded; S_FALSE/RPC_E_CHANGED_MODE are fine


def _shell_link() -> _Com:
    _co_initialize()
    ole32 = ctypes.windll.ole32
    ole32.CoCreateInstance.argtypes = [ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p,
                                       ctypes.c_void_p]
    ole32.CoCreateInstance.restype = _HRESULT
    out = ctypes.c_void_p()
    result = ole32.CoCreateInstance(ctypes.byref(_GUID.parse(CLSID_ShellLink)), None, 1,
                                    ctypes.byref(_GUID.parse(IID_IShellLinkW)), ctypes.byref(out))
    if result < 0:
        raise OSError(result & 0xFFFFFFFF, "Cannot create the shell link object")
    return _Com(out)


def _get_string(link: _Com, index: int, *extra) -> str:
    buffer = ctypes.create_unicode_buffer(32768)
    link.call(index, (buffer, ctypes.c_wchar_p), (len(buffer), ctypes.c_int), *extra)
    return buffer.value


def _app_id_key() -> _PROPERTYKEY:
    return _PROPERTYKEY(_GUID.parse(PKEY_AppUserModel_ID[0]), PKEY_AppUserModel_ID[1])


class WindowsIntegrationAdapter:
    """Shortcuts (with AppUserModelID) and the HKCU uninstall registration."""

    def __init__(self, uninstall_key: str = UNINSTALL_KEY) -> None:
        self.uninstall_key = uninstall_key

    # --- shortcuts ------------------------------------------------------------------------------
    def read_shortcut(self, path: Path) -> dict | None:
        path = Path(path)
        if not os.path.lexists(path):
            return None
        if is_reparse(path) or not path.is_file():
            raise SafetyError(f"Shortcut path is not a plain file: {path}")
        link = _shell_link()
        persist = store = None
        try:
            persist = link.query(IID_IPersistFile)
            persist.call(5, (str(path), ctypes.c_wchar_p), (0, wintypes.DWORD))  # Load(STGM_READ)
            target = _get_string(link, 3, (None, ctypes.c_void_p), (0x4, wintypes.DWORD))  # SLGP_RAWPATH
            description = _get_string(link, 6)
            workdir = _get_string(link, 8)
            arguments = _get_string(link, 10)
            store = link.query(IID_IPropertyStore)
            value = _PROPVARIANT()
            store.call(5, (ctypes.byref(_app_id_key()), ctypes.c_void_p), (ctypes.byref(value), ctypes.c_void_p))
            app_id = ctypes.wstring_at(value.pwszVal) if value.vt == VT_LPWSTR and value.pwszVal else ""
            clear = ctypes.windll.ole32.PropVariantClear
            clear.argtypes = [ctypes.POINTER(_PROPVARIANT)]
            clear.restype = _HRESULT
            clear(ctypes.byref(value))
            return {"target": target, "arguments": arguments, "workdir": workdir,
                    "description": description, "app_id": app_id}
        finally:
            for item in (store, persist, link):
                if item is not None:
                    item.release()

    def replace_shortcut(self, path: Path, *, target: str, arguments: str, workdir: str,
                         app_id: str, description: str) -> None:
        """Write a new .lnk beside the target, then atomically rename it into place."""
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        if os.path.lexists(path) and (is_reparse(path) or not path.is_file()):
            raise SafetyError(f"Shortcut path is not a plain file: {path}")
        temp = path.with_name(f".{path.stem}.{uuid.uuid4().hex}.tmp.lnk")
        link = _shell_link()
        persist = store = None
        try:
            link.call(20, (str(target), ctypes.c_wchar_p))           # SetPath
            link.call(11, (arguments, ctypes.c_wchar_p))             # SetArguments
            link.call(9, (str(workdir), ctypes.c_wchar_p))           # SetWorkingDirectory
            link.call(7, (description, ctypes.c_wchar_p))            # SetDescription
            link.call(17, (str(target), ctypes.c_wchar_p), (0, ctypes.c_int))  # SetIconLocation
            store = link.query(IID_IPropertyStore)
            text = ctypes.create_unicode_buffer(app_id)
            value = _PROPVARIANT(vt=VT_LPWSTR, pwszVal=ctypes.cast(text, ctypes.c_void_p))
            store.call(6, (ctypes.byref(_app_id_key()), ctypes.c_void_p), (ctypes.byref(value), ctypes.c_void_p))
            store.call(7)                                            # Commit
            persist = link.query(IID_IPersistFile)
            persist.call(6, (str(temp), ctypes.c_wchar_p), (1, wintypes.BOOL))  # Save
        except BaseException:
            try:
                temp.unlink()
            except OSError:
                pass
            raise
        finally:
            for item in (store, persist, link):
                if item is not None:
                    item.release()
        move_file_ex(temp, path)

    def remove_shortcut(self, path: Path) -> None:
        path = Path(path)
        if os.path.lexists(path):
            if is_reparse(path) or not path.is_file():
                raise SafetyError(f"Shortcut path is not a plain file: {path}")
            path.unlink()

    # --- uninstall registration -----------------------------------------------------------------
    def read_uninstall(self) -> dict | None:
        snapshot = self.read_uninstall_typed()
        return snapshot["values"] if snapshot is not None else None

    def read_uninstall_typed(self) -> dict | None:
        import winreg
        try:
            key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, self.uninstall_key)
        except FileNotFoundError:
            return None
        values, types = {}, {}
        with key:
            index = 0
            while True:
                try:
                    name, value, kind = winreg.EnumValue(key, index)
                except OSError:
                    break
                values[name] = value
                types[name] = kind
                index += 1
        return {"values": values, "types": types}

    def write_uninstall(self, values: dict, *, types: dict | None = None) -> None:
        import winreg
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, self.uninstall_key, 0, winreg.KEY_SET_VALUE) as key:
            for name, value in values.items():
                if isinstance(value, bool) or not isinstance(value, (int, str)):
                    raise SafetyError(f"Unsupported uninstall value: {name}")
                kind = (types or {}).get(name, winreg.REG_DWORD if isinstance(value, int) else winreg.REG_SZ)
                winreg.SetValueEx(key, name, 0, kind, value)

    def remove_uninstall(self, *, expected: dict | None = None) -> None:
        import winreg
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, self.uninstall_key, 0, winreg.KEY_SET_VALUE | winreg.KEY_QUERY_VALUE) as key:
                index = 0
                names = []
                while True:
                    try:
                        name, value, _kind = winreg.EnumValue(key, index)
                    except OSError:
                        break
                    if expected is None or (name in expected and expected[name] == value):
                        names.append(name)
                    index += 1
                for name in names:
                    winreg.DeleteValue(key, name)
            try:
                with winreg.OpenKey(winreg.HKEY_CURRENT_USER, self.uninstall_key) as key:
                    try:
                        winreg.EnumValue(key, 0)
                        empty = False
                    except OSError:
                        try:
                            winreg.EnumKey(key, 0)
                            empty = False
                        except OSError:
                            empty = True
            except OSError:
                empty = True
            if empty:
                winreg.DeleteKey(winreg.HKEY_CURRENT_USER, self.uninstall_key)
        except FileNotFoundError:
            pass


class WindowsControl:
    """Talks to a running Branchglance over the authenticated control port."""

    def __init__(self, nonce_path: Path, port: int | None = None) -> None:
        from token_tracker import product
        self.product = product
        self.nonce_path = Path(nonce_path)
        self.port = product.CONTROL_PORT if port is None else port

    def identity(self) -> dict | None:
        reply = self.product.control_request("identity", port=self.port)
        try:
            who = json.loads(reply) if reply else None
        except ValueError:
            return None
        return who if isinstance(who, dict) and who.get("product") == self.product.PRODUCT else None

    def health(self) -> dict:
        """Return the app identity only after proving the current nonce and challenge."""
        nonce = self.product.read_nonce_file(self.nonce_path)
        return self.product.control_health(nonce, port=self.port, timeout=0.6)

    def maintenance_exit(self, timeout: float = 2.0) -> str | None:
        nonce = self.product.read_nonce_file(self.nonce_path)
        return self.product.control_command("maintenance-exit", nonce, port=self.port, timeout=timeout)


class NoWait:
    """The instance mutex acquisition (with its timeout) is the real wait for exit."""

    def wait_for_instance_exit(self, timeout: float) -> bool:
        return True


def is_elevated() -> bool:
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except (AttributeError, OSError):
        return False


def _delete_file_literal(path: Path) -> bool:
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.DeleteFileW.argtypes = [ctypes.c_wchar_p]
    kernel.DeleteFileW.restype = wintypes.BOOL
    return bool(kernel.DeleteFileW(str(path)))


def _worker_is_running(path: Path, pid: int) -> bool:
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel.WaitForSingleObject.restype = wintypes.DWORD
    kernel.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
                                                   ctypes.POINTER(wintypes.DWORD)]
    kernel.QueryFullProcessImageNameW.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.OpenProcess(0x1000 | 0x00100000, False, pid)  # query limited + synchronize
    if not handle:
        return ctypes.get_last_error() != 87  # unknown/access denied: retain
    try:
        state = kernel.WaitForSingleObject(handle, 0)
        if state == 0:
            return False
        if state != 0x102:
            return True
        buffer = ctypes.create_unicode_buffer(32768)
        length = wintypes.DWORD(len(buffer))
        if not kernel.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(length)):
            return True
        return Path(buffer.value).resolve() == path.resolve()
    finally:
        kernel.CloseHandle(handle)


def _load_worker_ledger(path: Path) -> list[dict] | None:
    from .core import SafetyError, read_json
    if not path.exists():
        return []
    try:
        value = read_json(path)
        workers = value.get("workers")
        if value.get("schemaVersion") != 1 or not isinstance(workers, list):
            raise SafetyError("Temp worker ledger is invalid")
        import re
        if any(not isinstance(item, dict) or not re.fullmatch(r"Branchglance-uninstall-[0-9a-f]{32}\.exe",
                str(item.get("name", ""))) or type(item.get("pid")) is not int or item["pid"] <= 0 for item in workers):
            raise SafetyError("Temp worker ledger contains an invalid entry")
        return workers
    except (OSError, SafetyError):
        return None


def cleanup_stale_workers(temp_dir: Path, ledger_path: Path, *, is_running=_worker_is_running,
                          delete_file=_delete_file_literal) -> list[str]:
    import json
    import re
    temp_dir, ledger_path = Path(temp_dir).resolve(), Path(ledger_path)
    workers = _load_worker_ledger(ledger_path)
    if workers is None:
        return []
    kept, removed = [], []
    for item in workers:
        name = item["name"]
        path = temp_dir / name
        if (path.parent.resolve() != temp_dir or not re.fullmatch(r"Branchglance-uninstall-[0-9a-f]{32}\.exe", name)
                or is_reparse(path) or not path.exists() or is_running(path, item["pid"])):
            if path.exists():
                kept.append(item)
            else:
                removed.append(name)
            continue
        try:
            if delete_file(path):
                removed.append(name)
            else:
                kept.append(item)
        except OSError:
            kept.append(item)
    try:
        if kept:
            from .core import atomic_json
            atomic_json(ledger_path, {"schemaVersion": 1, "workers": kept})
        else:
            ledger_path.unlink(missing_ok=True)
    except OSError:
        pass
    return removed


def schedule_self_delete(executable: Path, temp_dir: Path, ledger_path: Path, *,
                         delete_file=_delete_file_literal, pid: int | None = None) -> bool:
    """Try literal standard-user deletion; ledger an issued worker when Windows still has it open."""
    executable, temp_dir = Path(executable), Path(temp_dir)
    import re
    if (executable.parent.resolve() != temp_dir.resolve()
            or not re.fullmatch(r"Branchglance-uninstall-[0-9a-f]{32}\.exe", executable.name)
            or executable.suffix.lower() != ".exe" or not executable.is_file() or is_reparse(executable)):
        return False
    try:
        if delete_file(executable):
            return True
    except OSError:
        pass
    workers = _load_worker_ledger(Path(ledger_path))
    if workers is None:
        return False
    item = {"name": executable.name, "pid": int(pid or os.getpid())}
    if item not in workers:
        workers.append(item)
    try:
        from .core import atomic_json
        atomic_json(Path(ledger_path), {"schemaVersion": 1, "workers": workers})
    except OSError:
        pass
    return False


def message_box(text: str, *, title: str = "Branchglance", flags: int = 0x40) -> int:
    try:
        return int(ctypes.windll.user32.MessageBoxW(None, text, title, flags))
    except (AttributeError, OSError):
        print(f"{title}: {text}", file=sys.stderr)
        return 0
