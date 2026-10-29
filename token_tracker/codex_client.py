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


def resolve_codex() -> CodexExecutable | None:
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
        if found:
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
            if found:
                return CodexExecutable(str(found), str(canonical_root), _package_manager(canonical_root, root))

    local = os.environ.get("LOCALAPPDATA", "")
    candidates = []
    if local and Path(local).is_absolute():
        candidates.extend((Path(local) / "Programs" / "OpenAI" / "Codex" / "bin" / "codex.exe",
                           Path(local) / "Programs" / "codex" / "bin" / "codex.exe"))
    candidates.append(Path.home() / "AppData" / "Local" / "Programs" / "OpenAI" / "Codex" / "bin" / "codex.exe")
    for candidate in candidates:
        found = safe(candidate)
        if found:
            return CodexExecutable(str(found))
    return None


class CodexUnavailable(RuntimeError):
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
        resolved = CodexExecutable(executable) if executable else resolve_codex()
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

    def close(self) -> None:
        with self._persistent_lock:
            self._discard_persistent()

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
