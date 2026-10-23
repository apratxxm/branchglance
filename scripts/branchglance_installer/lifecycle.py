from __future__ import annotations

import ctypes
import time
from dataclasses import dataclass
from typing import Callable, Protocol

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
    before_stop: Callable[[], None] | None = None

    def __post_init__(self):
        self.running_identity = None
        self.shutdown_requested = False
        self.shutdown_confirmed = False
        self.stopped_running_app = False
        self.relaunch_attempted = False

    def __enter__(self):
        self.install_handle, _ = self.mutex.acquire(INSTALL_MUTEX, self.timeout)
        try:
            if self.before_stop is not None:
                self.before_stop()
            self.running_identity = self.control.identity()
            if self.running_identity is not None:
                reply = self.control.maintenance_exit()
                if reply not in (True, "ok", None):
                    raise SafetyError("Running Branchglance did not confirm maintenance exit")
                self.shutdown_requested = True
                if reply in (True, "ok") and not self.processes.wait_for_instance_exit(self.timeout):
                    raise SafetyError("Branchglance is still running; quit it from the tray and retry")
            self.instance_handle, state = self.mutex.acquire(INSTANCE_MUTEX, self.timeout)
            self.shutdown_confirmed = self.running_identity is not None
            self.stopped_running_app = self.shutdown_confirmed
            if state == "abandoned":
                self.recover()
            # Instance ownership is proof the old process exited. Only this
            # confirmed state authorizes setup to restore the running app.
            return self
        except BaseException:
            # After a shutdown request, keep Install while setup makes one
            # bounded late-exit confirmation attempt and restores the app.
            if not self.shutdown_requested:
                self.release_locks()
            raise

    def __exit__(self, *_):
        self.release_locks()

    def release_locks(self) -> None:
        if getattr(self, "instance_handle", None) is not None:
            self.mutex.release(self.instance_handle)
            self.instance_handle = None
        if getattr(self, "install_handle", None) is not None:
            self.mutex.release(self.install_handle)
            self.install_handle = None

    def confirm_late_exit(self, timeout: float) -> None:
        """Take Instance once after an earlier shutdown wait timed out."""
        if not self.shutdown_requested or self.shutdown_confirmed:
            return
        if getattr(self, "instance_handle", None) is not None:
            self.shutdown_confirmed = True
            self.stopped_running_app = True
            return
        self.instance_handle, state = self.mutex.acquire(INSTANCE_MUTEX, timeout)
        if state == "abandoned":
            self.recover()
        self.shutdown_confirmed = True
        self.stopped_running_app = True

    def release_instance(self) -> None:
        """Release only the app-startup mutex while keeping installer exclusion held."""
        if getattr(self, "instance_handle", None) is not None:
            self.mutex.release(self.instance_handle)
            self.instance_handle = None
