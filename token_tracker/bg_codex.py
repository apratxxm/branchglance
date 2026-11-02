"""bg-codex for macOS: run codex and link its exec job to the Claude chat that started it.

The Windows build ships scripts/launcher/BgCodex.cs; this is the same contract on macOS.
stdout is forwarded byte for byte; receipts are display-only and never change the exit code.
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

MAX_LINE = 65536
LINK_WAIT = 2.0


def _inside(path: Path, folder: Path) -> bool:
    return path == folder or folder in path.parents


def resolve(path_env: str | None, cwd: str, self_path: str | None = None) -> str | None:
    """The first codex on PATH outside the working directory, never this launcher."""
    current = Path(cwd).resolve()
    own = Path(self_path).resolve() if self_path else None
    for entry in (path_env or "").split(os.pathsep):
        if not entry or not os.path.isabs(entry):
            continue
        candidate = Path(entry) / "codex"
        try:
            real = candidate.resolve(strict=True)
        except OSError:
            continue
        if not os.access(real, os.X_OK) or real.is_dir() or real == own:
            continue
        if _inside(Path(entry).resolve(), current) or _inside(real, current):
            continue
        return str(candidate)
    return None


def _uuid(value: object) -> str | None:
    try:
        return str(uuid.UUID(str(value))) if isinstance(value, str) and len(value) == 36 else None
    except ValueError:
        return None


def resume_thread(args: list[str]) -> str | None:
    return _uuid(args[2]) if len(args) >= 3 and args[0] == "exec" and args[1] == "resume" else None


def json_exec(args: list[str]) -> bool:
    return bool(args) and args[0] == "exec" and "--json" in args


def _codex_pid(root: int, deadline: float) -> int | None:
    """The codex binary itself: the child, or (for the npm launcher) its codex descendant."""
    from .agent_links import CODEX_EXE_NAMES
    from .macos_process import process_table
    while True:
        table = process_table()
        if table.get(root, (0, ""))[1] in CODEX_EXE_NAMES:
            return root
        frontier, seen = [root], {root}
        while frontier:
            parent = frontier.pop()
            for pid, (ppid, name) in table.items():
                if ppid == parent and pid not in seen:
                    if name in CODEX_EXE_NAMES:
                        return pid
                    seen.add(pid)
                    frontier.append(pid)
        if time.monotonic() >= deadline or root not in table:
            return None
        time.sleep(0.05)


def _link(child: subprocess.Popen, thread: str) -> None:
    try:
        from . import product
        from .agent_links import write_receipt
        from .macos_process import probe_process
        # Read the consent the app uses (the child already has its own environment copy).
        os.environ.setdefault(product.POLICY_FILE_ENV, str(product.hook_data_root() / "policy.json"))
        pid = _codex_pid(child.pid, time.monotonic() + LINK_WAIT)
        if pid is None:
            return
        state, created = probe_process(pid)
        if state == "alive" and created is not None:
            write_receipt(thread, pid, expected_created=created)
    except Exception:
        pass


def run(args: list[str], executable: str | None) -> int:
    if executable is None:
        print("bg-codex: Codex executable not found outside the current directory", file=sys.stderr)
        return 1
    observe = json_exec(args)
    links: list[threading.Thread] = []
    lock = threading.Lock()

    def enqueue(child: subprocess.Popen, thread: str | None) -> None:
        with lock:
            if thread is None or links:
                return
            worker = threading.Thread(target=_link, args=(child, thread), daemon=True)
            links.append(worker)
            worker.start()

    # Ctrl-C reaches codex through the terminal's process group; the launcher just waits.
    previous = signal.signal(signal.SIGINT, signal.SIG_IGN)
    try:
        child = subprocess.Popen([executable, *args], stdout=subprocess.PIPE if observe else None)
    except OSError as exc:
        signal.signal(signal.SIGINT, previous)
        print(f"bg-codex: {exc}", file=sys.stderr)
        return 1
    try:
        enqueue(child, resume_thread(args))
        if observe and child.stdout is not None:
            out = sys.stdout.buffer
            line, discard = bytearray(), False
            while chunk := child.stdout.read1(8192):
                try:
                    out.write(chunk)
                    out.flush()
                except (BrokenPipeError, OSError):
                    child.stdout.close()
                    break
                for byte in chunk:
                    if byte == 10:
                        if not discard and line:
                            try:
                                event = json.loads(line.decode("utf-8"))
                                if isinstance(event, dict) and event.get("type") == "thread.started":
                                    enqueue(child, _uuid(event.get("thread_id")))
                            except (ValueError, UnicodeDecodeError):
                                pass
                        line.clear()
                        discard = False
                    elif not discard:
                        if len(line) >= MAX_LINE:
                            discard, line = True, bytearray()
                        else:
                            line.append(byte)
        code = child.wait()
        for worker in links:
            worker.join(LINK_WAIT)
        return code
    finally:
        signal.signal(signal.SIGINT, previous)


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    launcher = os.environ.get("BG_CODEX_LAUNCHER")
    return run(args, resolve(os.environ.get("PATH"), os.getcwd(), launcher))


if __name__ == "__main__":
    raise SystemExit(main())
