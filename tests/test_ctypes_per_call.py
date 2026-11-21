"""Windows regressions for ctypes pointer-cache growth in hot/native helpers."""

import ctypes
import os
import subprocess
import uuid
from datetime import date
from types import SimpleNamespace

import pytest


pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows ctypes APIs")


def _assert_pointer_cache_stable(call):
    call()
    baseline = len(ctypes._pointer_type_cache)
    for _ in range(3):
        call()
    assert len(ctypes._pointer_type_cache) == baseline


def test_agent_links_windows_receipt_types_do_not_accumulate(tmp_path):
    from token_tracker import agent_links

    file = tmp_path / "identity"
    fd = os.open(file, os.O_CREAT | os.O_RDWR, 0o600)
    inbox = tmp_path / "inbox"
    receipt = inbox / "receipts.jsonl"
    for key in ("known_folder", "file_identity", "private_inbox"):
        agent_links._windows_receipt_api(key)  # Build the real ctypes bindings before stubbing calls.
    import ctypes

    def convert(_sddl, _revision, out, _size):
        ctypes.cast(out, ctypes.POINTER(ctypes.c_void_p)).contents.value = 1234
        return 1

    kernel = SimpleNamespace(CreateDirectoryW=lambda *_: 1, CreateFileW=lambda *_: 1,
                              CloseHandle=lambda *_: 1, LocalFree=lambda *_: 0,
                              SetFileSecurityW=lambda *_: 1)
    advapi = SimpleNamespace(ConvertStringSecurityDescriptorToSecurityDescriptorW=convert)
    advapi.SetFileSecurityW = lambda *_: 1
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(agent_links, "_windows_receipt_api",
                        lambda key: (kernel, advapi) if key == "private_inbox" else
                        agent_links._WINDOWS_RECEIPT_APIS[key])
    monkeypatch.setattr(agent_links, "_plain_chain", lambda _path: True)
    monkeypatch.setattr(type(inbox), "is_dir", lambda _path: True)
    monkeypatch.setattr(type(receipt), "is_file", lambda _path: True)
    try:
        def call():
            assert agent_links._known_local_app_data()
            assert agent_links._file_identity(fd)
            agent_links._create_private_inbox(inbox, receipt)

        _assert_pointer_cache_stable(call)
    finally:
        monkeypatch.undo()
        os.close(fd)


def test_claude_hook_windows_types_and_bindings_do_not_accumulate(tmp_path):
    from token_tracker import claude_hook

    append_path = str(tmp_path / "events.jsonl")
    empty_retention_dir = tmp_path / "retention"
    empty_retention_dir.mkdir()

    def call():
        claude_hook._process_table()
        assert claude_hook.probe_process(os.getpid())[0] == "alive"
        assert claude_hook._append_windows(append_path, b"{}\n", 1024) == "ok"
        claude_hook._retention_windows(str(empty_retention_dir), date.today())

    _assert_pointer_cache_stable(call)


def test_codex_windows_job_types_do_not_accumulate(monkeypatch):
    from token_tracker import codex_client

    class Kernel:
        def CreateJobObjectW(self, _attributes, _name):
            return 1

        def SetInformationJobObject(self, _job, _kind, _limits, _size):
            return True

        def CloseHandle(self, _handle):
            return True

    monkeypatch.setattr(codex_client, "_windows_codex_api", lambda: Kernel())

    def fake_process_in_job(_kernel, _job, command, _cwd, _env, _timeout, _mark_started):
        import ctypes

        startup_info, startup_info_ex, process_info = codex_client._windows_codex_types()[3:]
        for structure in (startup_info, startup_info_ex, process_info):
            ctypes.POINTER(structure)
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(codex_client, "_windows_process_in_job", fake_process_in_job)
    resolved = codex_client.CodexExecutable("codex.exe")

    def call():
        result = codex_client._run_contained_codex(
            resolved, ["--version"], timeout=1, cwd=None, env=None, mark_started=lambda: None)
        assert result.returncode == 0

    _assert_pointer_cache_stable(call)


def test_product_windows_mutex_security_attributes_do_not_accumulate():
    from token_tracker import product

    name = f"Global\\Branchglance-{product.current_user_sid()}-Instance.Test.{uuid.uuid4().hex}"

    def call():
        handle = product.create_secured_mutex(name)
        assert handle
        ctypes.WinDLL("kernel32", use_last_error=True).CloseHandle(handle)

    _assert_pointer_cache_stable(call)


def test_widget_monitor_structures_do_not_accumulate():
    from token_tracker import widget

    def call():
        widget._work_area()
        widget._monitor_work_areas()
        widget._monitor_descriptors()

    _assert_pointer_cache_stable(call)

