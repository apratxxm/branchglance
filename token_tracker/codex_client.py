from __future__ import annotations

import json
import os
import queue
import re
import platform
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import product


@dataclass(frozen=True)
class CodexExecutable:
    path: str
    package_root: str | None = None
    package_manager: str = "NPM"

    def environment(self) -> dict[str, str] | None:
        if self.package_root is None:
            return None
        env = os.environ.copy()
        for name in ("NPM", "BUN", "PNPM", "VITE_PLUS"):
            env.pop("CODEX_MANAGED_BY_" + name, None)
        env["CODEX_MANAGED_PACKAGE_ROOT"] = self.package_root
        env["CODEX_MANAGED_BY_" + self.package_manager] = "1"
        return env


def _package_manager(root: Path, lexical_root: Path) -> str:
    """Mirror the package ownership and environment hints in bin/codex.js."""
    def matches(path: Path) -> bool:
        try:
            return path.resolve(strict=True) == root
        except (OSError, RuntimeError):
            return False

    for start in (root, lexical_root / "bin"):
        for current in (start, *start.parents):
            if current.name == "packages":
                try:
                    metadata = json.loads((current / "@openai" / "codex.json").read_text(encoding="utf-8"))
                    if metadata.get("name") == "@openai/codex":
                        ident = metadata.get("installId") or ""
                        install = current / "@openai" / ("codex" + ident) if ident.startswith("#") else current / "@openai" / "codex" / ident
                        if any(matches(install / suffix / "@openai" / "codex") for suffix in ("lib/node_modules", "node_modules")):
                            return "VITE_PLUS"
                except (OSError, ValueError, TypeError, AttributeError):
                    pass
            modules = current / "node_modules"
            if (modules / ".modules.yaml").is_file() and matches(modules / "@openai" / "codex"):
                return "PNPM"
    agent = os.environ.get("npm_config_user_agent", "")
    if (re.search(r"\bbun/", agent) or "bun" in os.environ.get("npm_execpath", "")
            or ".bun/install/global" in str(lexical_root).replace("\\", "/")):
        return "BUN"
    return "NPM"


def resolve_codex(executable: str | None = None) -> CodexExecutable | None:
    """Resolve an absolute native Codex executable without searching cwd."""
    cwd = Path.cwd().resolve()

    def safe(path: Path) -> Path | None:
        try:
            if not path.is_absolute():
                return None
            resolved = path.resolve(strict=True)
            if resolved == cwd or cwd in resolved.parents or not resolved.is_file():
                return None
            return resolved
        except (OSError, RuntimeError):
            return None

    if executable is not None:
        path = Path(executable)
        # An override is a native executable, never a shell script or shim.
        if path.suffix.lower() in {".cmd", ".bat", ".ps1"} or (os.name == "nt" and path.suffix.lower() != ".exe"):
            return None
        found = safe(path)
        if found is None or found.suffix.lower() in {".cmd", ".bat", ".ps1"}:
            return None
        if os.name == "nt" and found.suffix.lower() != ".exe":
            return None
        for ancestor in found.parents:
            root = (ancestor.parent / "codex" if ancestor.name.startswith("codex-win32-")
                    else ancestor)
            if safe(root / "bin" / "codex.js") is not None:
                try:
                    canonical_root = root.resolve(strict=True)
                    return CodexExecutable(str(found), str(canonical_root), _package_manager(canonical_root, root))
                except (OSError, RuntimeError):
                    return None
        return CodexExecutable(str(found))

    machine = platform.machine().lower()
    targets = {"arm64": ("arm64", "aarch64-pc-windows-msvc"), "aarch64": ("arm64", "aarch64-pc-windows-msvc"),
               "amd64": ("x64", "x86_64-pc-windows-msvc"), "x86_64": ("x64", "x86_64-pc-windows-msvc")}
    target = targets.get(machine)
    for entry in os.environ.get("PATH", "").split(os.pathsep):
        if not entry or not Path(entry).is_absolute():
            continue
        directory = Path(entry)
        try:
            resolved_directory = directory.resolve(strict=True)
            if resolved_directory == cwd or cwd in resolved_directory.parents:
                continue
        except (OSError, RuntimeError):
            continue
        found = safe(directory / "codex.exe")
        if found and found.suffix.lower() == ".exe":
            return CodexExecutable(str(found))
        # Only recognize the npm package layout beside an npm shim; never run it.
        if not target:
            continue
        arch, triple = target
        if not any(safe(directory / ("codex" + ext)) for ext in (".cmd", ".ps1")):
            continue
        modules = directory.parent if directory.name.lower() == ".bin" and directory.parent.name.lower() == "node_modules" else directory / "node_modules"
        root = modules / "@openai" / "codex"
        if not safe(root / "bin" / "codex.js"):
            continue
        try:
            canonical_root = root.resolve(strict=True)
            if canonical_root == cwd or cwd in canonical_root.parents:
                continue
        except (OSError, RuntimeError):
            continue
        # require.resolve starts at the real JS entrypoint and walks parent
        # node_modules directories, selecting the first package.json it finds.
        vendor = None
        entry_dir = safe(root / "bin" / "codex.js").parent
        for ancestor in (entry_dir, *entry_dir.parents):
            candidate = ancestor / "node_modules" / "@openai" / ("codex-win32-" + arch)
            metadata = candidate / "package.json"
            if metadata.is_file():
                if safe(metadata) is None:
                    break
                vendor = candidate / "vendor"
                break
        else:
            vendor = canonical_root / "vendor"
        if vendor is not None:
            found = safe(vendor / triple / "bin" / "codex.exe")
            if found and found.suffix.lower() == ".exe":
                return CodexExecutable(str(found), str(canonical_root), _package_manager(canonical_root, root))

    local = os.environ.get("LOCALAPPDATA", "")
    candidates = []
    if local and Path(local).is_absolute():
        candidates.extend((Path(local) / "Programs" / "OpenAI" / "Codex" / "bin" / "codex.exe",
                           Path(local) / "Programs" / "codex" / "bin" / "codex.exe"))
    candidates.append(Path.home() / "AppData" / "Local" / "Programs" / "OpenAI" / "Codex" / "bin" / "codex.exe")
    for candidate in candidates:
        found = safe(candidate)
        if found and found.suffix.lower() == ".exe":
            return CodexExecutable(str(found))
    return None


class CodexUnavailable(RuntimeError):
    pass


class CodexNotStarted(OSError):
    """The child did not execute and therefore could not deliver a message."""


def run_contained_codex(resolved: CodexExecutable, args: list[str], *, timeout: float,
                        cwd: Path | None = None, env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    """Run a short delivery inside a kill-on-close job before it can execute."""
    command = [resolved.path, *args]
    started = False

    def mark_started() -> None:
        nonlocal started
        started = True

    try:
        return _run_contained_codex(resolved, args, timeout=timeout, cwd=cwd, env=env,
                                    mark_started=mark_started)
    except subprocess.TimeoutExpired as exc:
        if not started:
            raise CodexNotStarted(str(exc)) from exc
        raise
    except Exception as exc:
        if started:
            raise subprocess.TimeoutExpired(command, timeout) from exc
        raise CodexNotStarted(str(exc)) from exc


def _run_contained_codex(resolved: CodexExecutable, args: list[str], *, timeout: float,
                         cwd: Path | None, env: dict[str, str] | None,
                         mark_started: Any) -> subprocess.CompletedProcess:
    command = [resolved.path, *args]
    child_env = resolved.environment() or os.environ.copy()
    if env:
        child_env.update(env)
    if os.name != "nt":
        proc = subprocess.Popen(command, cwd=cwd, env=child_env, stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, shell=False)
        mark_started()
        try:
            stdout, stderr = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            try:
                proc.communicate(timeout=2)
            except subprocess.TimeoutExpired:
                pass
            raise
        return subprocess.CompletedProcess(command, proc.returncode, stdout, stderr)

    import ctypes
    from ctypes import wintypes as wt

    class IO_COUNTERS(ctypes.Structure):
        _fields_ = [(name, ctypes.c_ulonglong) for name in
                    ("ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                     "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

    class BASIC_LIMIT(ctypes.Structure):
        _fields_ = [("PerProcessUserTimeLimit", ctypes.c_longlong), ("PerJobUserTimeLimit", ctypes.c_longlong),
                    ("LimitFlags", wt.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                    ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wt.DWORD),
                    ("Affinity", ctypes.c_size_t), ("PriorityClass", wt.DWORD), ("SchedulingClass", wt.DWORD)]

    class EXTENDED_LIMIT(ctypes.Structure):
        _fields_ = [("BasicLimitInformation", BASIC_LIMIT), ("IoInfo", IO_COUNTERS),
                    ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                    ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wt.LPCWSTR]
    kernel.CreateJobObjectW.restype = wt.HANDLE
    kernel.SetInformationJobObject.argtypes = [wt.HANDLE, ctypes.c_int, ctypes.c_void_p, wt.DWORD]
    kernel.SetInformationJobObject.restype = wt.BOOL
    kernel.CloseHandle.argtypes = [wt.HANDLE]
    kernel.CloseHandle.restype = wt.BOOL
    job = kernel.CreateJobObjectW(None, None)  # default security attributes: non-inheritable
    if not job:
        raise OSError(ctypes.get_last_error(), "could not create containment job")
    try:
        limits = EXTENDED_LIMIT()
        limits.BasicLimitInformation.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not kernel.SetInformationJobObject(job, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
            raise OSError(ctypes.get_last_error(), "could not configure containment job")
        return _windows_process_in_job(kernel, job, command, cwd, child_env, timeout, mark_started)
    finally:
        kernel.CloseHandle(job)


def _windows_process_in_job(kernel: Any, job: Any, command: list[str], cwd: Path | None,
                            env: dict[str, str], timeout: float, mark_started: Any) -> subprocess.CompletedProcess:
    """Create a process atomically assigned to the job using STARTUPINFOEX."""
    import ctypes
    import msvcrt
    from ctypes import wintypes as wt

    class STARTUPINFO(ctypes.Structure):
        _fields_ = [("cb", wt.DWORD), ("lpReserved", wt.LPWSTR), ("lpDesktop", wt.LPWSTR),
                    ("lpTitle", wt.LPWSTR), ("dwX", wt.DWORD), ("dwY", wt.DWORD),
                    ("dwXSize", wt.DWORD), ("dwYSize", wt.DWORD), ("dwXCountChars", wt.DWORD),
                    ("dwYCountChars", wt.DWORD), ("dwFillAttribute", wt.DWORD), ("dwFlags", wt.DWORD),
                    ("wShowWindow", wt.WORD), ("cbReserved2", wt.WORD),
                    ("lpReserved2", ctypes.c_void_p), ("hStdInput", wt.HANDLE),
                    ("hStdOutput", wt.HANDLE), ("hStdError", wt.HANDLE)]

    class STARTUPINFOEX(ctypes.Structure):
        _fields_ = [("StartupInfo", STARTUPINFO), ("lpAttributeList", ctypes.c_void_p)]

    class PROCESS_INFORMATION(ctypes.Structure):
        _fields_ = [("hProcess", wt.HANDLE), ("hThread", wt.HANDLE),
                    ("dwProcessId", wt.DWORD), ("dwThreadId", wt.DWORD)]

    kernel.InitializeProcThreadAttributeList.argtypes = [ctypes.c_void_p, wt.DWORD, wt.DWORD, ctypes.POINTER(ctypes.c_size_t)]
    kernel.InitializeProcThreadAttributeList.restype = wt.BOOL
    kernel.UpdateProcThreadAttribute.argtypes = [ctypes.c_void_p, wt.DWORD, ctypes.c_size_t, ctypes.c_void_p,
                                                 ctypes.c_size_t, ctypes.c_void_p, ctypes.c_void_p]
    kernel.UpdateProcThreadAttribute.restype = wt.BOOL
    kernel.DeleteProcThreadAttributeList.argtypes = [ctypes.c_void_p]
    kernel.CreateProcessW.argtypes = [wt.LPCWSTR, wt.LPWSTR, ctypes.c_void_p, ctypes.c_void_p, wt.BOOL,
                                      wt.DWORD, ctypes.c_void_p, wt.LPCWSTR,
                                      ctypes.POINTER(STARTUPINFO), ctypes.POINTER(PROCESS_INFORMATION)]
    kernel.CreateProcessW.restype = wt.BOOL
    kernel.WaitForSingleObject.argtypes = [wt.HANDLE, wt.DWORD]
    kernel.WaitForSingleObject.restype = wt.DWORD
    kernel.GetExitCodeProcess.argtypes = [wt.HANDLE, ctypes.POINTER(wt.DWORD)]
    kernel.GetExitCodeProcess.restype = wt.BOOL
    kernel.TerminateJobObject.argtypes = [wt.HANDLE, wt.UINT]
    kernel.TerminateJobObject.restype = wt.BOOL
    fds: set[int] = set()
    info = PROCESS_INFORMATION()
    attr_initialized = False
    threads: list[threading.Thread] = []
    outputs = ["", ""]
    errors: list[Exception] = []
    try:
        out_read, out_write = os.pipe()
        fds.update((out_read, out_write))
        err_read, err_write = os.pipe()
        fds.update((err_read, err_write))
        devnull = os.open(os.devnull, os.O_RDONLY)
        fds.add(devnull)
        for fd in (out_write, err_write, devnull):
            os.set_inheritable(fd, True)
        handles = (wt.HANDLE * 3)(*(msvcrt.get_osfhandle(fd) for fd in (devnull, out_write, err_write)))
        jobs = (wt.HANDLE * 1)(job)
        size = ctypes.c_size_t()
        kernel.InitializeProcThreadAttributeList(None, 2, 0, ctypes.byref(size))
        if not size.value:
            raise OSError(ctypes.get_last_error(), "could not size process attributes")
        storage = ctypes.create_string_buffer(size.value)
        attributes = ctypes.cast(storage, ctypes.c_void_p)
        if not kernel.InitializeProcThreadAttributeList(attributes, 2, 0, ctypes.byref(size)):
            raise OSError(ctypes.get_last_error(), "could not initialize process attributes")
        attr_initialized = True
        for key, values in ((0x00020002, handles), (0x0002000D, jobs)):
            if not kernel.UpdateProcThreadAttribute(attributes, 0, key, ctypes.byref(values), ctypes.sizeof(values), None, None):
                raise OSError(ctypes.get_last_error(), "could not set process attributes")
        startup = STARTUPINFOEX()
        startup.StartupInfo.cb = ctypes.sizeof(startup)
        startup.StartupInfo.dwFlags = 0x100  # STARTF_USESTDHANDLES
        startup.StartupInfo.hStdInput, startup.StartupInfo.hStdOutput, startup.StartupInfo.hStdError = handles
        startup.lpAttributeList = attributes
        command_line = ctypes.create_unicode_buffer(subprocess.list2cmdline(command))
        environment = ctypes.create_unicode_buffer("\0".join(f"{key}={value}" for key, value in sorted(env.items(), key=lambda item: item[0].upper())) + "\0\0")
        if not kernel.CreateProcessW(command[0], command_line, None, None, True,
                                     0x00080000 | 0x00000400 | subprocess.CREATE_NO_WINDOW,
                                     environment, str(cwd) if cwd is not None else None,
                                     ctypes.byref(startup.StartupInfo), ctypes.byref(info)):
            raise OSError(ctypes.get_last_error(), "could not create contained process")
        mark_started()  # Successful CreateProcess is the delivery boundary.
        for fd in (out_write, err_write, devnull):
            os.close(fd)
            fds.remove(fd)
        def drain(index: int, fd: int) -> None:
            try:
                with os.fdopen(fd, "r", errors="replace") as stream:
                    outputs[index] = stream.read()
            except Exception as exc:
                errors.append(exc)
        for index, fd in enumerate((out_read, err_read)):
            thread = threading.Thread(target=drain, args=(index, fd), daemon=True)
            thread.start()
            fds.remove(fd)
            threads.append(thread)
        wait_ms = min(0xFFFFFFFE, max(0, int(timeout * 1000)))
        wait_result = kernel.WaitForSingleObject(info.hProcess, wait_ms)
        if wait_result != 0:
            kernel.TerminateJobObject(job, 1)
            for thread in threads:
                thread.join(1)
            raise subprocess.TimeoutExpired(command, timeout)
        code = wt.DWORD()
        if not kernel.GetExitCodeProcess(info.hProcess, ctypes.byref(code)):
            raise OSError(ctypes.get_last_error(), "could not read process exit status")
        # A finished parent may leave descendants holding the pipes.
        kernel.TerminateJobObject(job, 1)
        for thread in threads:
            thread.join(1)
        if errors or any(thread.is_alive() for thread in threads):
            raise subprocess.TimeoutExpired(command, timeout)
        return subprocess.CompletedProcess(command, code.value, outputs[0], outputs[1])
    finally:
        if info.hThread:
            kernel.CloseHandle(info.hThread)
        if info.hProcess:
            kernel.CloseHandle(info.hProcess)
        if attr_initialized:
            kernel.DeleteProcThreadAttributeList(attributes)
        for fd in fds:
            try:
                os.close(fd)
            except OSError:
                pass



# thread/list defaults to interactive sources only, which hides `codex exec`
# sessions. Subagent kinds stay excluded: children are joined from rollouts.
# The token tracker's own keepalive greetings run with --ephemeral and are
# never persisted, so they cannot appear here.
THREAD_SOURCE_KINDS = ["cli", "vscode", "exec", "appServer"]


@dataclass
class CodexSnapshot:
    rate_limits: dict[str, Any]
    usage: dict[str, Any]
    threads: list[dict[str, Any]]


class CodexAppServer:
    """Small JSON-RPC client for the signed-in local Codex app server."""

    def __init__(self, executable: str | None = None, timeout: int = 5) -> None:
        resolved = resolve_codex(executable)
        self.executable = resolved.path if resolved else None
        self._executable_env = resolved.environment() if resolved else None
        self.timeout = timeout
        self._persistent_lock = threading.Lock()
        self._persistent_proc: subprocess.Popen[str] | None = None
        self._persistent_messages: queue.Queue[dict[str, Any] | None] | None = None
        self._persistent_reader: threading.Thread | None = None
        self._persistent_id = 0

    def rate_limits(self) -> dict[str, Any]:
        """Read current limits over a reusable app-server stdio connection.

        The reset watcher may call this every two seconds while a quota window
        is active. Reusing one initialized process avoids paying process startup
        and authentication setup on every sample.
        """
        if not self.executable:
            raise CodexUnavailable("Codex is not installed or is not on PATH")
        with self._persistent_lock:
            try:
                self._ensure_persistent()
                self._initialize_persistent()
                response = self._persistent_request("account/rateLimits/read", {})
                if "error" in response:
                    raise CodexUnavailable(str(response["error"]))
                return response.get("result") or {}
            except Exception:
                self._discard_persistent()
                raise

    def read_thread(self, thread_id: str, *, include_turns: bool = False) -> dict[str, Any]:
        """Read one thread's current runtime status, without hydrating history by default."""
        if not self.executable:
            raise CodexUnavailable("Codex is not installed or is not on PATH")
        with self._persistent_lock:
            try:
                self._ensure_persistent()
                self._initialize_persistent()
                response = self._persistent_request("thread/read", {
                    "threadId": thread_id,
                    "includeTurns": bool(include_turns),
                })
                if "error" in response:
                    raise CodexUnavailable(str(response["error"]))
                thread = (response.get("result") or {}).get("thread")
                if not isinstance(thread, dict):
                    raise CodexUnavailable("Codex app server returned no thread")
                return thread
            except Exception:
                self._discard_persistent()
                raise

    def latest_turn(self, thread_id: str) -> dict[str, Any] | None:
        """Read only the latest persisted turn, avoiding full rollout hydration."""
        if not self.executable:
            raise CodexUnavailable("Codex is not installed or is not on PATH")
        with self._persistent_lock:
            try:
                self._ensure_persistent()
                self._initialize_persistent()
                response = self._persistent_request("thread/turns/list", {
                    "threadId": thread_id,
                    "limit": 1,
                    "sortDirection": "desc",
                    "itemsView": "notLoaded",
                })
                if "error" in response:
                    raise CodexUnavailable(str(response["error"]))
                result = response.get("result") or {}
                turns = result.get("data")
                if not isinstance(turns, list) or any(not isinstance(turn, dict) for turn in turns):
                    raise CodexUnavailable("Codex app server returned an invalid turn list")
                return turns[0] if turns else None
            except Exception:
                self._discard_persistent()
                raise

    def list_threads(self, limit: int = 100) -> list[dict[str, Any]]:
        """List a bounded page of non-archived interactive Codex threads."""
        if not 1 <= limit <= 100:
            raise ValueError("thread list limit must be between 1 and 100")
        if not self.executable:
            raise CodexUnavailable("Codex is not installed or is not on PATH")
        with self._persistent_lock:
            try:
                self._ensure_persistent()
                self._initialize_persistent()
                response = self._persistent_request("thread/list", {
                    "limit": limit,
                    "sortKey": "recency_at",
                    "sortDirection": "desc",
                    "useStateDbOnly": False,
                    "sourceKinds": THREAD_SOURCE_KINDS,
                })
                if "error" in response:
                    raise CodexUnavailable(str(response["error"]))
                result = response.get("result") or {}
                threads = result.get("data") or result.get("threads") or []
                if not isinstance(threads, list) or any(not isinstance(row, dict) for row in threads):
                    raise CodexUnavailable("Codex app server returned an invalid thread list")
                return threads
            except Exception:
                self._discard_persistent()
                raise

    def _initialize_persistent(self) -> None:
        if self._persistent_id != 0:
            return
        initialized = self._persistent_request("initialize", {
            "clientInfo": {"name": "branchglance", "version": product.VERSION},
            "capabilities": {"experimentalApi": True},
        })
        if "error" in initialized:
            raise CodexUnavailable(str(initialized["error"]))
        self._persistent_send({"jsonrpc": "2.0", "method": "initialized", "params": {}})

    def close(self, timeout: float | None = None) -> None:
        acquired = (self._persistent_lock.acquire() if timeout is None
                    else self._persistent_lock.acquire(timeout=timeout))
        if not acquired:
            return
        try:
            self._discard_persistent()
        finally:
            self._persistent_lock.release()

    def _ensure_persistent(self) -> None:
        if self._persistent_proc is not None and self._persistent_proc.poll() is None:
            return
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        try:
            proc = subprocess.Popen(
                [self.executable, "app-server", "--stdio"], text=True,
                encoding="utf-8", errors="replace", stdin=subprocess.PIPE,
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, creationflags=flags,
                env=self._executable_env,
            )
        except OSError as exc:
            raise CodexUnavailable(f"Could not query Codex: {exc}") from exc
        messages: queue.Queue[dict[str, Any] | None] = queue.Queue()

        def read_stdout() -> None:
            assert proc.stdout is not None
            for line in proc.stdout:
                try:
                    messages.put(json.loads(line))
                except json.JSONDecodeError:
                    continue
            messages.put(None)

        self._persistent_proc = proc
        self._persistent_messages = messages
        self._persistent_id = 0
        self._persistent_reader = threading.Thread(target=read_stdout, daemon=True)
        self._persistent_reader.start()

    def _persistent_send(self, item: dict[str, Any]) -> None:
        proc = self._persistent_proc
        if proc is None or proc.stdin is None:
            raise CodexUnavailable("Codex app server connection is closed")
        try:
            proc.stdin.write(json.dumps(item, separators=(",", ":")) + "\n")
            proc.stdin.flush()
        except (OSError, BrokenPipeError) as exc:
            raise CodexUnavailable(f"Codex app server write failed: {exc}") from exc

    def _persistent_request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        self._persistent_id += 1
        ident = self._persistent_id
        self._persistent_send({"jsonrpc": "2.0", "id": ident, "method": method, "params": params})
        assert self._persistent_messages is not None
        deadline = time.monotonic() + self.timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise CodexUnavailable(f"Codex app server timed out waiting for {ident}")
            try:
                message = self._persistent_messages.get(timeout=remaining)
            except queue.Empty as exc:
                raise CodexUnavailable(f"Codex app server timed out waiting for {ident}") from exc
            if message is None:
                raise CodexUnavailable("Codex app server closed its output")
            if message.get("id") == ident:
                return message

    def _discard_persistent(self) -> None:
        proc, self._persistent_proc = self._persistent_proc, None
        self._persistent_messages = None
        self._persistent_reader = None
        self._persistent_id = 0
        if proc is None:
            return
        if proc.stdin:
            try:
                proc.stdin.close()
            except OSError:
                pass
        try:
            proc.wait(timeout=1)
        except subprocess.TimeoutExpired:
            proc.terminate()
            try:
                proc.wait(timeout=1)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=1)

    def snapshot(
        self,
        thread_limit: int = 100,
        *,
        include_usage: bool = True,
        include_threads: bool = True,
    ) -> CodexSnapshot:
        if not self.executable:
            raise CodexUnavailable("Codex is not installed or is not on PATH")

        initialize = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "clientInfo": {"name": "branchglance", "version": product.VERSION},
                "capabilities": {"experimentalApi": True},
            },
        }
        requests = [
            {"jsonrpc": "2.0", "id": 2, "method": "account/rateLimits/read", "params": {}},
        ]
        if include_usage:
            requests.append({"jsonrpc": "2.0", "id": 3, "method": "account/usage/read", "params": {}})
        if include_threads:
            requests.append({
                "jsonrpc": "2.0",
                "id": 4,
                "method": "thread/list",
                "params": {
                    "limit": thread_limit,
                    "sortKey": "recency_at",
                    "sortDirection": "desc",
                    "useStateDbOnly": False,
                    "sourceKinds": THREAD_SOURCE_KINDS,
                },
            })
        expected_ids = {request["id"] for request in requests}
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        try:
            proc = subprocess.Popen(
                [self.executable, "app-server", "--stdio"],
                text=True,
                encoding="utf-8",
                errors="replace",
                stdin=subprocess.PIPE,
                env=self._executable_env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                creationflags=flags,
            )
        except OSError as exc:
            raise CodexUnavailable(f"Could not query Codex: {exc}") from exc

        messages: queue.Queue[dict[str, Any] | None] = queue.Queue()

        def read_stdout() -> None:
            assert proc.stdout is not None
            for line in proc.stdout:
                try:
                    messages.put(json.loads(line))
                except json.JSONDecodeError:
                    continue
            messages.put(None)

        threading.Thread(target=read_stdout, daemon=True).start()

        def send(item: dict[str, Any]) -> None:
            assert proc.stdin is not None
            proc.stdin.write(json.dumps(item, separators=(",", ":")) + "\n")
            proc.stdin.flush()

        def receive(expected: set[int]) -> dict[int, dict[str, Any]]:
            found: dict[int, dict[str, Any]] = {}
            deadline = time.monotonic() + self.timeout
            while expected - found.keys():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise CodexUnavailable(
                        f"Codex app server timed out waiting for {sorted(expected - found.keys())}"
                    )
                try:
                    message = messages.get(timeout=remaining)
                except queue.Empty as exc:
                    raise CodexUnavailable(
                        f"Codex app server timed out waiting for {sorted(expected - found.keys())}"
                    ) from exc
                if message is None:
                    break
                if isinstance(message.get("id"), int) and message["id"] in expected:
                    found[message["id"]] = message
            return found

        responses: dict[int, dict[str, Any]] = {}
        try:
            send(initialize)
            init_response = receive({1})
            if 1 not in init_response or "error" in init_response[1]:
                raise CodexUnavailable(str(init_response.get(1, {}).get("error", "initialize failed")))
            send({"jsonrpc": "2.0", "method": "initialized", "params": {}})
            for request in requests:
                send(request)
            responses = receive(expected_ids)
            thread_error = str((responses.get(4) or {}).get("error") or "")
            if any(marker in thread_error for marker in ("sourceKinds", "unknown variant", "unknown field")):
                # Older app servers reject the filter; fall back to their defaults.
                retry = next(request for request in requests if request["id"] == 4)
                retry_params = {key: value for key, value in retry["params"].items() if key != "sourceKinds"}
                send({**retry, "id": 5, "params": retry_params})
                retried = receive({5})
                if 5 in retried:
                    responses[4] = retried[5]
        finally:
            if proc.stdin:
                proc.stdin.close()
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                proc.terminate()
                try:
                    proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=2)

        missing = [request_id for request_id in expected_ids if request_id not in responses]
        if missing:
            stderr = proc.stderr.read().strip() if proc.stderr else ""
            detail = stderr.splitlines()[-1] if stderr else "no response"
            raise CodexUnavailable(f"Codex app server did not answer {missing}: {detail}")

        for request_id in expected_ids:
            if "error" in responses[request_id]:
                raise CodexUnavailable(str(responses[request_id]["error"]))

        thread_result = responses.get(4, {}).get("result") or {}
        threads = thread_result.get("data") or thread_result.get("threads") or []
        return CodexSnapshot(
            rate_limits=responses[2].get("result") or {},
            usage=responses.get(3, {}).get("result") or {},
            threads=threads,
        )
