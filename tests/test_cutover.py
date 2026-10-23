from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from branchglance_installer.core import SafetyError  # noqa: E402
from branchglance_installer.cutover import Cutover, STATE_FILES  # noqa: E402


class PowerLoss(BaseException):
    pass


class FakeCutover:
    def __init__(self, state_root):
        self.state_root = state_root
        self.old_running = False
        self.free = {8765: True, 8766: True}
        self.shortcut = {"target": r"C:\old\pythonw.exe", "workdir": r"C:\old", "arguments": "-m token_tracker.widget"}
        self.renamed = False
        self.new_link = False
        self.running = False
        self.pid = 520
        self.calls = []

    def old_pythonw_running(self): self.calls.append("old-process-read"); return self.old_running
    def port_is_free(self, port): self.calls.append(("port-read", port)); return self.free[port]
    def snapshot_old_shortcut(self): self.calls.append("shortcut-read"); return dict(self.shortcut)
    def rename_old_shortcut(self, snapshot): self.calls.append("rename-old"); self.renamed = True
    def create_branchglance_shortcut(self, record): self.calls.append("create-new"); self.new_link = dict(record); return record
    def remove_branchglance_shortcut(self, record=None):
        self.calls.append("remove-new")
        if record is not None and self.new_link == record: self.new_link = False
    def start_branchglance(self, handoff_token):
        assert len(handoff_token) == 64
        marker = self.state_root.parent.parent / "Programs" / "Branchglance" / ".cutover-handoff.json"
        if marker.exists():
            record = json.loads(marker.read_text())
            assert record["nonce"] == handoff_token
            marker.unlink()
        self.calls.append("start-new"); self.running = True; return self.pid
    def identity(self): return {"product": "Branchglance", "build": "build-a", "pid": self.pid, "stateRoot": str(self.state_root)}
    def branchglance_running(self): return self.running
    def quit_branchglance(self): self.calls.append("quit-new"); self.running = False; return True
    def restore_old_shortcut(self, snapshot): self.calls.append("restore-old"); self.renamed = False
    def start_old_app(self, snapshot): self.calls.append("start-old")


def setup(tmp_path):
    legacy = tmp_path / "TokenTracker"
    legacy.mkdir()
    (legacy / "limit-reset-state.json").write_text(json.dumps({"providers": {"Codex": {"status": "legacy"}},
                                                               "resetAction": {"armed": False}}))
    (legacy / "limit-reset.jsonl").write_text('{"event":"old"}\n')
    (legacy / "window-state.json").write_text('{"x":1}')
    root = tmp_path / "Branchglance"
    (root.parent / "Programs" / "Branchglance").mkdir(parents=True)
    state = root / "state"
    adapter = FakeCutover(state)

    def migrate(staging):
        path = staging / "limit-reset-state.json"
        data = json.loads(path.read_text())
        data["schemaVersion"] = 2
        data["providers"] = {"Codex": {"lastTriggerAt": 123}}
        path.write_text(json.dumps(data))

    cutover = Cutover(adapter, legacy, root, Path(r"C:\old\pythonw.exe"), Path(r"C:\old"), migrate)
    return cutover, adapter, legacy, root


def test_cutover_plan_reads_only_and_returns_zero_writes(tmp_path):
    cutover, adapter, legacy, root = setup(tmp_path)
    before = {p.name: p.read_bytes() for p in legacy.iterdir()}
    plan = cutover.plan()
    assert plan["writes"] == 0 and set(plan["state_files"]) == set(STATE_FILES)
    assert not root.exists()
    assert {p.name: p.read_bytes() for p in legacy.iterdir()} == before
    assert adapter.calls and all(c in ("old-process-read", "shortcut-read") or c[0] == "port-read" for c in adapter.calls)


def test_cutover_copies_verifies_migrates_and_checks_launched_identity(tmp_path):
    cutover, adapter, legacy, root = setup(tmp_path)
    result = cutover.run()
    state = root / "state"
    assert result["pid"] == adapter.pid and (state / "window-state.json").read_text() == '{"x":1}'
    assert json.loads((state / "limit-reset-state.json").read_text())["schemaVersion"] == 2
    assert json.loads((root / "policy.json").read_text())["providers"]["Codex"]["send"] is True
    assert (legacy / "limit-reset-state.json").read_text().find("resetAction") >= 0
    assert adapter.renamed and adapter.new_link and not (root / "cutover-journal.json").exists()
    assert not (root.parent / "Programs" / "Branchglance" / ".cutover-handoff.json").exists()


def test_cutover_recovery_removes_expired_owned_handoff_and_allows_retry(tmp_path, monkeypatch):
    import branchglance_installer.cutover as cutover_module
    cutover, adapter, _legacy, root = setup(tmp_path)
    def crash_after_marker(name):
        if name == "handoff-written": raise PowerLoss("crash after marker write")
    cutover.after_mutation = crash_after_marker
    with pytest.raises(PowerLoss, match="after marker write"):
        cutover.run()
    marker = root.parent / "Programs" / "Branchglance" / ".cutover-handoff.json"
    journal = json.loads((root / "cutover-journal.json").read_text())
    assert marker.is_file() and journal["handoff"]["nonce"]
    now = journal["handoff"]["expires"] + 1
    monkeypatch.setattr(cutover_module.time, "time", lambda: now)
    assert cutover.recover() == "rolled-back"
    assert not marker.exists()
    cutover.after_mutation = lambda _name: None
    assert cutover.run()["pid"] == adapter.pid


def test_cutover_recovery_preserves_mismatched_handoff_marker(tmp_path, monkeypatch):
    import branchglance_installer.cutover as cutover_module
    cutover, _adapter, _legacy, root = setup(tmp_path)
    cutover.after_mutation = lambda name: (_ for _ in ()).throw(PowerLoss("crash")) if name == "handoff-written" else None
    with pytest.raises(PowerLoss):
        cutover.run()
    marker = root.parent / "Programs" / "Branchglance" / ".cutover-handoff.json"
    journal = json.loads((root / "cutover-journal.json").read_text())
    foreign = {"schemaVersion": 1, "nonce": "f" * 64, "expires": journal["handoff"]["expires"]}
    marker.write_text(json.dumps(foreign), encoding="utf-8")
    before = marker.read_bytes()
    monkeypatch.setattr(cutover_module.time, "time", lambda: journal["handoff"]["expires"] + 1)
    with pytest.raises(SafetyError, match="does not match its journal"):
        cutover.recover()
    assert marker.read_bytes() == before


def test_cutover_run_refuses_old_app_or_port_conflict_without_writes(tmp_path):
    cutover, adapter, _legacy, root = setup(tmp_path)
    adapter.old_running = True
    with pytest.raises(SafetyError, match="Quit Token Tracker"):
        cutover.run()
    assert not root.exists()
    adapter.old_running = False
    adapter.free[8766] = False
    with pytest.raises(SafetyError, match="ports"):
        cutover.run()
    assert not root.exists()


def test_cutover_health_failure_restores_shortcut_without_deleting_state(tmp_path):
    cutover, adapter, _legacy, root = setup(tmp_path)
    adapter.identity = lambda: {"product": "other"}
    with pytest.raises(SafetyError, match="identity"):
        cutover.run()
    assert not adapter.renamed and not adapter.new_link and not adapter.running
    assert (root / "state" / "limit-reset-state.json").exists()
    assert (root / "cutover-journal.json").exists() and (root / "policy.json").exists()
    assert cutover.recover() == "rolled-back"
    assert not (root / "state").exists()


def test_cutover_failed_shutdown_keeps_recovery_journal(tmp_path):
    cutover, adapter, _legacy, root = setup(tmp_path)
    adapter.identity = lambda: {"product": "foreign"}
    adapter.quit_branchglance = lambda: False
    with pytest.raises(SafetyError, match="journal retained"):
        cutover.run()
    assert (root / "cutover-journal.json").exists() and (root / "policy.json").exists()


def test_cutover_cleanup_preserves_a_changed_shortcut(tmp_path):
    cutover, adapter, _legacy, root = setup(tmp_path)
    original = adapter.identity
    def fail_after_foreign_replacement():
        adapter.new_link = {"target": "foreign", "arguments": "", "workdir": "foreign",
                            "description": "Branchglance managed shortcut other-owner"}
        return {"product": "foreign"}
    adapter.identity = fail_after_foreign_replacement
    with pytest.raises(SafetyError, match="identity"):
        cutover.run()
    assert adapter.new_link["target"] == "foreign"
    assert (root / "cutover-journal.json").exists()


@pytest.mark.parametrize("stop_after", ["state-copied", "state-migrated", "policy-written", "state-promoted",
    "old-shortcut-renamed", "new-shortcut-created", "branchglance-started"])
def test_cutover_journal_recovers_after_injected_interruption(tmp_path, stop_after):
    cutover, adapter, _legacy, root = setup(tmp_path)
    def interrupt(name):
        if name == stop_after: raise PowerLoss("power loss")
    cutover.after_mutation = interrupt
    with pytest.raises(PowerLoss, match="power loss"):
        cutover.run()
    assert cutover.recover() == "rolled-back"
    assert not (root / "cutover-journal.json").exists()
    if stop_after in ("old-shortcut-renamed", "new-shortcut-created", "branchglance-started"):
        assert not adapter.renamed and not adapter.new_link


def test_cutover_rollback_merges_only_greeting_fields_and_preserves_legacy_action(tmp_path):
    cutover, adapter, legacy, root = setup(tmp_path)
    cutover.run()
    branch = root / "state" / "limit-reset-state.json"
    data = json.loads(branch.read_text())
    data["providers"]["Codex"].update({"nextGreetingAt": 456, "unsafe": "not-merged"})
    branch.write_text(json.dumps(data))
    result = cutover.rollback()
    old = json.loads((legacy / "limit-reset-state.json").read_text())
    assert old["resetAction"] == {"armed": False}
    assert old["providers"]["Codex"]["lastTriggerAt"] == 123
    assert old["providers"]["Codex"]["nextGreetingAt"] == 456
    assert "unsafe" not in old["providers"]["Codex"]
    assert result["restored"] and not adapter.running and not adapter.renamed


def test_windows_cutover_identity_uses_authenticated_control_health(monkeypatch, tmp_path):
    import ctypes
    from token_tracker import product
    from branchglance_installer.windows_cutover import WindowsCutoverAdapter
    identity = {"product":"Branchglance", "build":"test-build", "pid":123,
                "stateRoot":str(tmp_path / "Branchglance" / "state"), "policyWarnings":[]}

    nonce_reads = iter([None, None, "a" * 64])
    health_calls = []
    monkeypatch.setattr(product, "read_nonce_file", lambda _path: next(nonce_reads))
    monkeypatch.setattr(product, "control_health", lambda *a, **k: health_calls.append((a, k)) or identity)
    state = tmp_path / "Branchglance" / "state"
    state.mkdir(parents=True)
    (state / "control.nonce").write_text("a" * 64)
    release = tmp_path / "Programs" / "Branchglance" / "releases" / "1.0.0+test-build"
    release.mkdir(parents=True)
    (release / "BranchglanceApp.exe").write_bytes(b"app")
    (tmp_path / "Programs" / "Branchglance" / "current.json").write_text(json.dumps({
        "current": {"version": "1.0.0+test-build"}}))
    from branchglance_installer.core import write_manifest
    write_manifest(release, "1.0.0", "test-build")
    adapter = WindowsCutoverAdapter(1, tmp_path / "old.lnk", tmp_path / "old-renamed.lnk",
        tmp_path / "pythonw.exe", tmp_path, tmp_path / "Branchglance")
    class Kernel:
        def OpenProcess(self, *_): return 123
        def QueryFullProcessImageNameW(self, _handle, _flags, buffer, _size):
            ctypes.cast(buffer, ctypes.POINTER(ctypes.c_wchar * 32768)).contents.value = str(release / "BranchglanceApp.exe")
            return 1
        def CloseHandle(self, *_): pass
    adapter.kernel = Kernel()
    assert adapter.identity() == identity
    assert len(health_calls) == 1 and adapter.launcher_pid is None and adapter.pending_app_pid == identity["pid"]
    adapter.launch_pending = False
    adapter.pending_app_pid = identity["pid"]
    adapter.branch_handle = None
    with pytest.raises(SafetyError, match="process is not verified"):
        adapter.branchglance_running()


def test_restore_old_shortcut_refuses_changed_backup(tmp_path):
    from branchglance_installer.windows_cutover import WindowsCutoverAdapter
    adapter = WindowsCutoverAdapter(0, tmp_path / "old.lnk", tmp_path / "old-backup.lnk",
                                    tmp_path / "pythonw.exe", tmp_path, tmp_path / "Branchglance")
    adapter.renamed_shortcut.write_bytes(b"test backup")
    adapter._read_shortcut = lambda _path: {"target": "foreign", "arguments": "x", "workdir": "foreign",
                                             "description": "changed", "app_id": "other"}
    adapter.file_system.replace = lambda *_: pytest.fail("mismatched shortcut backup must not move")
    snapshot = {"target": "old", "arguments": "-m token_tracker.widget", "workdir": "old-dir",
                "description": "old desc", "app_id": "old id"}
    with pytest.raises(SafetyError, match="does not match"):
        adapter.restore_old_shortcut(snapshot)
    assert adapter.renamed_shortcut.exists() and not adapter.old_shortcut.exists()


def test_cutover_cli_releases_instance_before_app_launch(tmp_path, monkeypatch, capsys):
    import types
    import cutover_branchglance as cli
    from branchglance_installer.app import Paths
    from branchglance_installer.cutover import OLD_ARGUMENTS
    appdata = tmp_path / "LocalAppData"
    legacy = appdata / "TokenTracker"
    legacy.mkdir(parents=True)
    root = appdata / "Branchglance"
    (appdata / "Programs" / "Branchglance").mkdir(parents=True)
    paths = Paths(appdata / "Programs" / "Branchglance", tmp_path / "Menu", tmp_path / "Startup",
                  root, tmp_path / "Temp")
    monkeypatch.setattr(cli, "local_app_data", lambda: appdata)
    monkeypatch.setattr(cli.product, "appdata_probe", lambda: ("not_redirected", None))
    monkeypatch.setattr(cli.Paths, "for_user", classmethod(lambda _cls: paths))
    state = {"instance": False, "install": False, "handoff": False}
    class Mutex:
        def acquire(self, name, timeout):
            if name.endswith("-Install"): state["install"] = True
            else: state["instance"] = True
            return name, "acquired"
        def release(self, handle):
            if handle.endswith("-Instance"): state["instance"] = False
            else: state["install"] = False
    class Control:
        def identity(self): return None
    class Processes:
        def wait_for_instance_exit(self, _timeout): return True
    native = types.SimpleNamespace(mutex=Mutex(), control=Control(), processes=Processes())
    class Adapter:
        def __init__(self, *_args): pass
        def old_pythonw_running(self): return False
        def port_is_free(self, _port): return True
        def snapshot_old_shortcut(self):
            return {"target": r"C:\old\pythonw.exe", "workdir": r"C:\old", "arguments": OLD_ARGUMENTS}
        def rename_old_shortcut(self, _snapshot): pass
        def create_branchglance_shortcut(self, _record): pass
        def start_branchglance(self, token):
            assert state["install"] and not state["instance"]
            assert len(token) == 64
            state["handoff"] = True
            return 500
        def identity(self):
            return {"product": "Branchglance", "build": "b", "pid": 501,
                    "stateRoot": str(root / "state")}
        def branchglance_running(self): return False
        def quit_branchglance(self): return True
        def restore_old_shortcut(self, _snapshot): pass
        def remove_branchglance_shortcut(self, _record=None): pass
    result = cli.main(["run", "--old-pid", "1", "--old-target", r"C:\old\pythonw.exe",
                       "--old-workdir", r"C:\old"], native_factory=lambda _paths: native,
                      adapter_factory=Adapter)
    assert result == 0 and state["handoff"] and not state["instance"] and not state["install"]
    assert json.loads(capsys.readouterr().out)["pid"] == 501


@pytest.mark.parametrize("command", ["recover", "rollback"])
def test_fresh_cutover_commands_use_instance_mutex_as_app_absence_proof(tmp_path, monkeypatch, capsys, command):
    import types
    import cutover_branchglance as cli
    from branchglance_installer.app import Paths
    from branchglance_installer.cutover import OLD_ARGUMENTS
    appdata = tmp_path / "LocalAppData"
    (appdata / "TokenTracker").mkdir(parents=True)
    product_root = appdata / "Branchglance"
    product_root.mkdir()
    journal_path = product_root / "cutover-journal.json"
    paths = Paths(appdata / "Programs" / "Branchglance", tmp_path / "Menu", tmp_path / "Startup",
                  product_root, tmp_path / "Temp")
    monkeypatch.setattr(cli, "local_app_data", lambda: appdata)
    monkeypatch.setattr(cli.product, "appdata_probe", lambda: ("not_redirected", None))
    monkeypatch.setattr(cli.Paths, "for_user", classmethod(lambda _cls: paths))
    if command == "recover":
        journal_path.write_text(json.dumps({
            "schemaVersion": 1, "id": "a" * 32, "phase": "planned", "staging": "state.cutover-" + "a" * 32 + ".partial",
            "shortcut": {"target": r"C:\\old\\pythonw.exe", "workdir": r"C:\\old",
                         "arguments": OLD_ARGUMENTS}, "beforeHashes": {}, "beforePolicy": None,
        }))
    state = {"instance": False}
    class Mutex:
        def acquire(self, name, _timeout):
            if name.endswith("-Instance"): state["instance"] = True
            return name, "acquired"
        def release(self, name):
            if name.endswith("-Instance"): state["instance"] = False
    class Control:
        def identity(self): return None
    class Processes: pass
    native = types.SimpleNamespace(mutex=Mutex(), control=Control(), processes=Processes())
    class NoAppAdapter(FakeCutover):
        def __init__(self, *_args): super().__init__(product_root / "state")
        def branchglance_running(self):
            assert state["instance"] and self.instance_mutex_held
            return False
    result = cli.main([command, "--old-pid", "1", "--old-target", r"C:\\old\\pythonw.exe",
                       "--old-workdir", r"C:\\old"], native_factory=lambda _paths: native,
                      adapter_factory=NoAppAdapter)
    assert result == 0 and not state["instance"]
    if command == "recover":
        assert not journal_path.exists()
        assert json.loads(capsys.readouterr().out) == "rolled-back"
    else:
        assert json.loads(capsys.readouterr().out)["restored"] is True


def test_cutover_rollback_refuses_interrupted_journal_after_reboot_without_abandoned_mutex(tmp_path, monkeypatch):
    cutover, adapter, legacy, root = setup(tmp_path)
    def interrupt(name):
        if name == "state-promoted": raise PowerLoss("power loss")
    cutover.after_mutation = interrupt
    with pytest.raises(PowerLoss):
        cutover.run()
    journal = root / "cutover-journal.json"
    before = journal.read_bytes()
    import types
    import cutover_branchglance as cli
    from branchglance_installer.app import Paths
    monkeypatch.setattr(cli, "local_app_data", lambda: tmp_path)
    monkeypatch.setattr(cli.product, "appdata_probe", lambda: ("not_redirected", None))
    paths = Paths(tmp_path / "Programs" / "Branchglance", tmp_path / "Menu", tmp_path / "Startup",
                  root, tmp_path / "Temp")
    monkeypatch.setattr(cli.Paths, "for_user", classmethod(lambda _cls: paths))
    calls = []
    class Mutex:
        def acquire(self, name, _timeout): calls.append(("acquire", name)); return name, "acquired"
        def release(self, name): calls.append(("release", name))
    class Control:
        def identity(self): return None
    class Processes: pass
    native = types.SimpleNamespace(mutex=Mutex(), control=Control(), processes=Processes())
    with pytest.raises(SafetyError, match="must be recovered"):
        cli.main(["rollback", "--old-pid", "1", "--old-target", r"C:\old\pythonw.exe",
                  "--old-workdir", r"C:\old"], native_factory=lambda _paths: native,
                 adapter_factory=lambda *_args: FakeCutover(root / "state"))
    assert journal.read_bytes() == before
    assert calls[0][0] == "acquire" and calls[0][1].endswith("-Install")


def test_cutover_refuses_redirected_context_before_adapter_or_state_access(monkeypatch):
    import cutover_branchglance as cli
    monkeypatch.setattr(cli.product, "appdata_probe", lambda: ("redirected", "package LocalCache"))
    touched = []
    with pytest.raises(SafetyError, match="File Explorer or the Start menu"):
        cli.main(["run", "--old-pid", "1", "--old-target", r"C:\old\pythonw.exe",
                  "--old-workdir", r"C:\old"], adapter_factory=lambda *_args: touched.append(True))
    assert not touched


@pytest.mark.skipif(sys.platform != "win32", reason="Windows process and COM adapter proof")
def test_windows_cutover_exited_pid_and_shortcut_read_work_in_fresh_process(tmp_path):
    import os
    import subprocess
    import textwrap
    from branchglance_installer.windows import WindowsIntegrationAdapter
    shortcut = tmp_path / "fixture.lnk"
    values = {"target": str(tmp_path / "fixture.exe"), "arguments": "--fixture",
              "workdir": str(tmp_path), "app_id": "Branchglance.Test", "description": "test-only"}
    shortcut.touch()
    WindowsIntegrationAdapter().replace_shortcut(shortcut, **values)
    code = textwrap.dedent("""
        import json, sys
        from pathlib import Path
        from branchglance_installer.windows_cutover import WindowsCutoverAdapter
        from branchglance_installer.windows import WindowsIntegrationAdapter
        base = Path(sys.argv[1]); adapter = WindowsCutoverAdapter(0, base/'old.lnk', base/'old-old.lnk', base/'pythonw.exe', base, base/'Branchglance')
        print(json.dumps({'exited': adapter.old_pythonw_running(), 'shortcut': WindowsIntegrationAdapter().read_shortcut(base/'fixture.lnk')}))
    """)
    env = os.environ.copy()
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "scripts") + os.pathsep + str(Path(__file__).resolve().parents[1])
    result = subprocess.run([sys.executable, "-c", code, str(tmp_path)], capture_output=True, text=True,
                            timeout=20, env=env)
    try:
        assert result.returncode == 0, result.stderr
        answer = json.loads(result.stdout)
        assert answer["exited"] is False
        for key in ("target", "arguments", "workdir", "description"):
            assert answer["shortcut"][key] == (values.get(key) if key != "target" else str(tmp_path / "fixture.exe"))
    finally:
        shortcut.unlink(missing_ok=True)


@pytest.mark.parametrize("nonce_matches", [True, False])
def test_cutover_quit_uses_challenge_response_and_never_sends_raw_nonce(tmp_path, monkeypatch, nonce_matches):
    import threading
    from token_tracker import widget
    from branchglance_installer.windows_cutover import WindowsCutoverAdapter
    root = tmp_path / "Branchglance"
    monkeypatch.setattr(widget, "CONTROL_PORT", 0)
    app = widget.WidgetApp()
    app.nonce_path = root / "state" / "control.nonce"
    quit_called = threading.Event()
    app.quit = lambda *_: quit_called.set()
    app.start_control()
    try:
        monkeypatch.setattr(widget, "CONTROL_PORT", app.control.getsockname()[1])
        if not nonce_matches:
            app.nonce_path.write_text("9f" * 32)  # the adapter now holds a nonce the app does not
        adapter = WindowsCutoverAdapter(0, tmp_path / "old.lnk", tmp_path / "old (old).lnk",
                                        tmp_path / "pythonw.exe", tmp_path, root,
                                        control_port=widget.CONTROL_PORT)
        assert adapter.quit_branchglance() is False  # no launched pid to wait on in this test
        assert quit_called.wait(2) is nonce_matches
        deadline = time.monotonic() + 2
        while app.maintenance_exit_requested is not nonce_matches and time.monotonic() < deadline:
            time.sleep(0.001)
        assert app.maintenance_exit_requested is nonce_matches
    finally:
        app.quitting = True
        app.control.close()
