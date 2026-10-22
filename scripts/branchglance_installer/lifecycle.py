from __future__ import annotations

import ctypes
import time
from dataclasses import dataclass
from typing import Protocol

from .core import SafetyError
from token_tracker.product import mutex_name

INSTALL_MUTEX = mutex_name("Install")
INSTANCE_MUTEX = mutex_name("Instance")
WAIT_OBJECT_0 = 0
WAIT_ABANDONED = 0x80
WAIT_TIMEOUT = 0x102


class MutexAdapter(Protocol):
    def acquire(self, name: str, timeout: float) -> tuple[object, str]: ...
    def release(self, handle: object) -> None: ...


class Win32MutexAdapter:
    """Named kernel mutex; caller must acquire and release on the same thread."""
    def acquire(self, name: str, timeout: float) -> tuple[object, str]:
        from ctypes import wintypes
        from token_tracker import product
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        handle = product.create_secured_mutex(name)
        milliseconds = max(0, min(int(timeout * 1000), 0xFFFFFFFE))
        result = kernel.WaitForSingleObject(ctypes.c_void_p(handle), milliseconds)
        if result == WAIT_OBJECT_0:
            return handle, "acquired"
        if result == WAIT_ABANDONED:
            return handle, "abandoned"
        kernel.CloseHandle(ctypes.c_void_p(handle))
        if result == WAIT_TIMEOUT:
            raise SafetyError(f"Timed out waiting for lifecycle mutex {name}")
        raise OSError(ctypes.get_last_error(), f"WaitForSingleObject failed for {name}: {result}")

    def release(self, handle: object) -> None:
        kernel = ctypes.windll.kernel32
        kernel.ReleaseMutex(ctypes.c_void_p(handle))
        kernel.CloseHandle(ctypes.c_void_p(handle))


@dataclass
class Lifecycle:
    mutex: MutexAdapter
    control: object
    processes: object
    recover: object
    timeout: float = 20.0

    def __enter__(self):
        self.install_handle, _ = self.mutex.acquire(INSTALL_MUTEX, self.timeout)
        try:
            if self.control.identity() is not None:
                reply = self.control.maintenance_exit()
                if reply not in (True, "ok", None):
                    raise SafetyError("Running Branchglance did not confirm maintenance exit")
                if reply in (True, "ok") and not self.processes.wait_for_instance_exit(self.timeout):
                    raise SafetyError("Branchglance is still running; quit it from the tray and retry")
            self.instance_handle, state = self.mutex.acquire(INSTANCE_MUTEX, self.timeout)
            if state == "abandoned":
                self.recover()
            return self
        except BaseException:
            self.mutex.release(self.install_handle)
            self.install_handle = None
            raise

    def __exit__(self, *_):
        if getattr(self, "instance_handle", None) is not None:
            self.mutex.release(self.instance_handle)
        if getattr(self, "install_handle", None) is not None:
            self.mutex.release(self.install_handle)

    def release_instance(self) -> None:
        """Release only the app-startup mutex while keeping installer exclusion held."""
        if getattr(self, "instance_handle", None) is not None:
            self.mutex.release(self.instance_handle)
            self.instance_handle = None
