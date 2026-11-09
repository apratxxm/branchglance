from __future__ import annotations

import ctypes
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from datetime import date
from pathlib import Path

import pytest

from token_tracker import claude_hook, codex_client, limit_reset, reset_delivery, server
from token_tracker.provider_policy import load_policies


@pytest.fixture
def fake_codex(tmp_path, monkeypatch):
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.chdir(work)
    native = tmp_path / "codex.exe"
    shutil.copy2(sys._base_executable, native)
    for dll in Path(sys.base_prefix).glob("python*.dll"):
        shutil.copy2(dll, tmp_path / dll.name)
    monkeypatch.setenv("PYTHONHOME", sys.base_prefix)
    resolved = codex_client.resolve_codex(str(native))
    assert resolved is not None
    return resolved


def _process_gone(pid):
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
    kernel.OpenProcess.restype = ctypes.c_void_p
    kernel.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
    kernel.CloseHandle.argtypes = [ctypes.c_void_p]
    handle = kernel.OpenProcess(0x100000, False, pid)
    if not handle:
        return True
    try:
        return kernel.WaitForSingleObject(handle, 0) == 0
    finally:
        kernel.CloseHandle(handle)


def test_p1_metacharacters_are_literal_in_native_process(fake_codex, tmp_path):
    output = tmp_path / "argv.json"
    message = '& | % ^ " < >'
    result = codex_client.run_contained_codex(fake_codex, [
        "-c", "import json,sys;open(sys.argv[1],'w').write(json.dumps(sys.argv[2]))", str(output), message,
    ], timeout=10)
    assert result.returncode == 0
    assert json.loads(output.read_text()) == message


def test_p1_queue_caller_passes_metacharacters_literally(fake_codex, tmp_path, monkeypatch):
    marker = tmp_path / "queue-argv.json"
    Path("queue").write_text("import json,os,sys\nopen(os.environ['P1_CAPTURE'],'w').write(json.dumps(sys.argv[4]))\n")
    monkeypatch.setenv("P1_CAPTURE", str(marker))
    monkeypatch.setattr(codex_client, "resolve_codex", lambda: fake_codex)
    monkeypatch.setattr(server, "_codex_queue_allowed", lambda: True)
    message = '& | % ^ " < >'
    server.queue_codex_message("00000000-0000-4000-8000-000000000001", message)
    assert json.loads(marker.read_text()) == message


def test_p1_overrides_reject_shell_shims_and_relative_paths(tmp_path, monkeypatch):
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.chdir(work)
    for suffix in (".cmd", ".bat", ".ps1"):
        path = tmp_path / ("codex" + suffix)
        path.touch()
        assert codex_client.resolve_codex(str(path)) is None
    assert codex_client.resolve_codex("codex.exe") is None


def test_p1_all_callers_refuse_shim_only_path(tmp_path, monkeypatch):
    work = tmp_path / "work"
    work.mkdir()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for suffix in (".cmd", ".bat", ".ps1"):
        (bin_dir / ("codex" + suffix)).write_text("not native")
    monkeypatch.chdir(work)
    monkeypatch.setenv("PATH", str(bin_dir))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "missing"))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "missing"))
    monkeypatch.setattr(server, "_codex_queue_allowed", lambda: True)
    policies = load_policies({"TOKEN_TRACKER_CODEX_COLLECT": "true", "TOKEN_TRACKER_CODEX_SHOW": "true",
                              "TOKEN_TRACKER_CODEX_SEND": "true"})
    thread = "00000000-0000-4000-8000-000000000001"
    with pytest.raises(FileNotFoundError):
        server.queue_codex_message(thread, "hello")
    assert limit_reset.send_codex_greeting(work, policies=policies)["ok"] is False
    assert reset_delivery.queue_continue(thread, policies=policies)["status"] == "rejected"
    assert codex_client.CodexAppServer().executable is None


@pytest.mark.parametrize("timeout_parent", [True, False])
def test_p1_job_kills_descendant_and_closes_inherited_pipes(fake_codex, tmp_path, timeout_parent):
    marker = tmp_path / "child.pid"
    child = "import os,sys,time;open(sys.argv[1],'w').write(str(os.getpid()));time.sleep(60)"
    parent = ("import os,subprocess,sys,time;"
              "subprocess.Popen([sys.executable,'-c',sys.argv[2],sys.argv[1]]);"
              "[(time.sleep(.01)) for _ in range(500) if not os.path.exists(sys.argv[1])];" +
              ("time.sleep(60)" if timeout_parent else ""))
    if timeout_parent:
        with pytest.raises(subprocess.TimeoutExpired):
            codex_client.run_contained_codex(fake_codex, ["-c", parent, str(marker), child], timeout=5)
    else:
        result = codex_client.run_contained_codex(fake_codex, ["-c", parent, str(marker), child], timeout=10)
        assert result.returncode == 0
    assert marker.exists()
    pid = int(marker.read_text())
    deadline = time.monotonic() + 5
    while not _process_gone(pid) and time.monotonic() < deadline:
        time.sleep(.05)
    assert _process_gone(pid)


def test_p1_assignment_failure_never_resumes_child(fake_codex, tmp_path, monkeypatch):
    original = ctypes.WinDLL
    real_kernel = original("kernel32", use_last_error=True)
    class FailedAssignment:
        argtypes = None
        restype = None
        def __call__(self, *_args):
            return False
    class KernelProxy:
        AssignProcessToJobObject = FailedAssignment()
        def __getattr__(self, name):
            return getattr(real_kernel, name)
    monkeypatch.setattr(ctypes, "WinDLL", lambda name, **kwargs: KernelProxy() if name == "kernel32" else original(name, **kwargs))
    marker = tmp_path / "executed"
    with pytest.raises(OSError):
        codex_client.run_contained_codex(fake_codex, ["-c", "open(__import__('sys').argv[1],'w').write('bad')", str(marker)], timeout=5)
    assert not marker.exists()


def test_p7_retention_dates_reparse_and_concurrent_writer(tmp_path):
    directory = tmp_path / "claude-events"
    directory.mkdir()
    for name in ("20261102.jsonl", "20261103.jsonl", "20261109.jsonl", "20261110.jsonl",
                 "20261111.jsonl", "20260230.jsonl", "２０２６１１０１.jsonl", "dropped.jsonl"):
        (directory / name).write_text("old\n", encoding="utf-8")
    (directory / "20261101.jsonl").mkdir()
    outside = tmp_path / "outside.jsonl"
    outside.write_text("untouched", encoding="utf-8")
    os.symlink(outside, directory / "20261031.jsonl")
    stop = threading.Event()
    def writer():
        while not stop.is_set():
            with (directory / "20261110.jsonl").open("a", encoding="utf-8") as stream:
                stream.write("new\n")
    thread = threading.Thread(target=writer)
    thread.start()
    try:
        claude_hook.prune_event_logs(str(directory), today=date(2026, 11, 10))
    finally:
        stop.set()
        thread.join(5)
    assert not thread.is_alive()
    assert not (directory / "20261102.jsonl").exists()
    for name in ("20261103.jsonl", "20261109.jsonl", "20261110.jsonl", "20261111.jsonl",
                 "20260230.jsonl", "２０２６１１０１.jsonl", "dropped.jsonl", "20261101.jsonl", "20261031.jsonl"):
        assert (directory / name).exists()
    assert outside.read_text() == "untouched"
