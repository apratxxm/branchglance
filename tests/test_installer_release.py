from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from branchglance_installer.core import (  # noqa: E402
    OWNER_FILE, ReleaseOps, SafetyError, create_owner, read_json, uninstall_owned_root, verify_manifest, write_manifest,
)
from branchglance_installer.lifecycle import Lifecycle  # noqa: E402
from branchglance_installer.preflight import HostInfo, check_host, staged_self_test  # noqa: E402
from branchglance_installer.integration import Integrations, MARKER  # noqa: E402


def _release(path: Path, name: str, body="bundle", version="1.0.0", build_id="a"):
    folder = path / name
    folder.mkdir(parents=True)
    (folder / "BranchglanceApp.exe").write_text(body, encoding="utf-8")
    write_manifest(folder, version, build_id)
    return folder


def _root(tmp_path):
    root = tmp_path / "programs" / "Branchglance"
    create_owner(root)
    return root


def _fake_installer_native(paths, *, running, events, interrupt=None, wait_exit=True):
    from types import SimpleNamespace
    from branchglance_installer.app import Native
    class Mutex:
        def acquire(self, name, timeout): events.append(("mutex-acquire", name)); return name, "acquired"
        def release(self, handle): events.append(("mutex-release", handle))
    class Control:
        def identity(self):
            if not running:
                return None
            pointer = read_json(paths.root / "current.json")
            version = pointer["current"]["version"]
            manifest = verify_manifest(paths.root / "releases" / version, expected_version=version)
            return {"product": "Branchglance", "pid": 404, "build": manifest["buildId"],
                    "stateRoot": str(paths.product_root / "state")}
        def health(self):
            events.append(("authenticated-health",))
            return self.identity()
        def maintenance_exit(self): events.append(("maintenance-exit",)); return "ok"
    class Processes:
        def wait_for_instance_exit(self, _timeout): events.append(("wait-exit",)); return wait_exit
    class Adapter:
        def __init__(self): self.links = {}; self.registry = None
        def read_shortcut(self, path): return self.links.get(str(path))
        def replace_shortcut(self, path, **values): self.links[str(path)] = dict(values)
        def remove_shortcut(self, path): self.links.pop(str(path), None)
        def read_uninstall(self): return self.registry
        def read_uninstall_typed(self): return {"values": self.registry or {}, "types": {}}
        def write_uninstall(self, values, **_kwargs): self.registry = {**(self.registry or {}), **values}
        def remove_uninstall(self, *, expected=None):
            if expected is None: self.registry = None
            else: self.registry = {k:v for k,v in (self.registry or {}).items()
                                   if not (k in expected and expected[k] == v)}
    def runner(args, **_kwargs):
        Path(args[-1]).write_text(json.dumps({"ok": True, "webview2": "test"}), encoding="utf-8")
        return SimpleNamespace(returncode=0)
    def after(name):
        events.append(("mutation", name))
        if interrupt and name == interrupt: raise RuntimeError("injected setup failure")
    return Native(lambda: HostInfo(True, 19041, True, 528040, ("155",)), lambda: False,
                  Mutex(), Control(), Processes(), Adapter(), runner=runner,
                  popen=lambda args, **_kwargs: events.append(("popen", list(args))), after_mutation=after)


def _setup_payload(tmp_path, *, version, build_id, body):
    payload = tmp_path / "payload"
    (payload / "release").mkdir(parents=True)
    (payload / "release" / "BranchglanceApp.exe").write_text(body, encoding="utf-8")
    write_manifest(payload / "release", version, build_id)
    (payload / "Branchglance.exe").write_bytes(b"launcher")
    (payload / "BranchglanceUninstall.exe").write_bytes(b"uninstaller")
    return payload


def test_manifest_detects_missing_extra_and_modified_files(tmp_path):
    release = _release(tmp_path, "stage")
    write_manifest(release, "1.0.0", "abc")
    assert verify_manifest(release)["version"] == "1.0.0+abc"
    (release / "BranchglanceApp.exe").write_text("changed")
    with pytest.raises(SafetyError, match="SHA-256"):
        verify_manifest(release)


def test_install_is_immutable_and_second_build_becomes_rollback_target(tmp_path):
    root = _root(tmp_path)
    ops = ReleaseOps(root)
    first = _release(tmp_path, "first", version="1.0.0", build_id="a")
    second = _release(tmp_path, "second", version="1.0.1", build_id="b")
    ops.install(first, "1.0.0", "a")
    result = ops.install(second, "1.0.1", "b")
    assert result["current"]["version"] == "1.0.1+b"
    assert result["previous"]["version"] == "1.0.0+a"
    with pytest.raises(SafetyError, match="Immutable"):
        ops.install(second, "1.0.1", "b")
    assert ops.rollback()["current"]["version"] == "1.0.0+a"
    assert verify_manifest(root / "releases/1.0.1+b")["version"] == "1.0.1+b"


def test_setup_same_version_identical_release_is_noop_before_stopping_running_app(tmp_path):
    from branchglance_installer.app import Paths, install
    root = _root(tmp_path)
    body = "same-release"
    ReleaseOps(root).install(_release(tmp_path, "initial", body, "1.0.0", "a"), "1.0.0", "a")
    payload = _setup_payload(tmp_path, version="1.0.0", build_id="a", body=body)
    paths = Paths(root, tmp_path / "Menu", tmp_path / "Startup", tmp_path / "LocalAppData" / "Branchglance",
                  tmp_path / "Temp")
    events = []
    native = _fake_installer_native(paths, running=True, events=events)
    result = install(paths, payload, native, startup=False)
    assert result["alreadyInstalled"] is True
    assert not any(event[0] == "maintenance-exit" for event in events)


def test_setup_same_version_different_release_refuses_before_stopping_running_app(tmp_path):
    from branchglance_installer.app import Paths, install
    root = _root(tmp_path)
    ReleaseOps(root).install(_release(tmp_path, "initial", "old-bytes", "1.0.0", "a"), "1.0.0", "a")
    payload = _setup_payload(tmp_path, version="1.0.0", build_id="a", body="different-bytes")
    paths = Paths(root, tmp_path / "Menu", tmp_path / "Startup", tmp_path / "LocalAppData" / "Branchglance",
                  tmp_path / "Temp")
    events = []
    native = _fake_installer_native(paths, running=True, events=events)
    with pytest.raises(SafetyError, match="Immutable|different bytes"):
        install(paths, payload, native, startup=False)
    assert not any(event[0] == "maintenance-exit" for event in events)


@pytest.mark.parametrize("running,interrupt,should_relaunch", [(True, None, True),
                                                                (True, "release-promoted", True),
                                                                (True, "tool-backed-up", True),
                                                                (False, None, False)])
def test_setup_relaunches_only_previously_running_app_after_success_or_failure(
        tmp_path, running, interrupt, should_relaunch):
    from branchglance_installer.app import Paths, install
    from branchglance_installer.core import read_owned_ledger, write_owned_ledger
    root = _root(tmp_path)
    ReleaseOps(root).install(_release(tmp_path, "initial", "old", "1.0.0", "a"), "1.0.0", "a")
    (root / "Branchglance.exe").write_bytes(b"old launcher")
    ledger = read_owned_ledger(root)
    ledger["tools"].append("Branchglance.exe")
    write_owned_ledger(root, ledger)
    payload = _setup_payload(tmp_path, version="1.0.1", build_id="b", body="new")
    paths = Paths(root, tmp_path / "Menu", tmp_path / "Startup", tmp_path / "LocalAppData" / "Branchglance",
                  tmp_path / "Temp")
    events = []
    native = _fake_installer_native(paths, running=running, events=events, interrupt=interrupt)
    if interrupt:
        with pytest.raises(RuntimeError, match="injected setup failure"):
            install(paths, payload, native, startup=False)
    else:
        install(paths, payload, native, startup=False)
    launches = [event for event in events if event[0] == "popen"]
    assert bool(launches) is should_relaunch
    if interrupt == "tool-backed-up":
        assert (root / "Branchglance.exe").read_bytes() == b"old launcher"
        assert read_json(root / "current.json")["current"]["version"] == "1.0.0+a"
    if launches:
        assert launches[0][1][0] == str(root / "Branchglance.exe")
        assert events.index(launches[0]) > max(i for i, event in enumerate(events) if event[0] == "mutex-release")


def test_setup_does_not_relaunch_until_old_instance_exit_is_confirmed(tmp_path):
    from branchglance_installer.app import Paths, install
    root = _root(tmp_path)
    ReleaseOps(root).install(_release(tmp_path, "initial", "old", "1.0.0", "a"), "1.0.0", "a")
    payload = _setup_payload(tmp_path, version="1.0.1", build_id="b", body="new")
    paths = Paths(root, tmp_path / "Menu", tmp_path / "Startup", tmp_path / "LocalAppData" / "Branchglance",
                  tmp_path / "Temp")
    events = []
    native = _fake_installer_native(paths, running=True, events=events, wait_exit=False)
    with pytest.raises(SafetyError, match="still running"):
        install(paths, payload, native, startup=False)
    assert not any(event[0] == "popen" for event in events)


def test_setup_relaunches_once_when_old_instance_exits_during_bounded_late_wait(tmp_path):
    from branchglance_installer.app import Paths, install
    root = _root(tmp_path)
    ReleaseOps(root).install(_release(tmp_path, "initial", "old", "1.0.0", "a"), "1.0.0", "a")
    (root / "Branchglance.exe").write_bytes(b"launcher")
    payload = _setup_payload(tmp_path, version="1.0.1", build_id="b", body="new")
    paths = Paths(root, tmp_path / "Menu", tmp_path / "Startup", tmp_path / "Local" / "Branchglance",
                  tmp_path / "Temp")
    events = []
    native = _fake_installer_native(paths, running=True, events=events, wait_exit=False)
    with pytest.raises(SafetyError, match="still running"):
        install(paths, payload, native, startup=False)
    launches = [event for event in events if event[0] == "popen"]
    assert len(launches) == 1
    assert any(event[0] == "authenticated-health" for event in events)
    assert events.index(launches[0]) > max(i for i, event in enumerate(events) if event[0] == "mutex-release")


def test_restart_rejects_matching_public_identity_without_authenticated_health(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from branchglance_installer import app
    from branchglance_installer.app import Paths, _restart_after_install
    root = _root(tmp_path)
    ReleaseOps(root).install(_release(tmp_path, "initial", "old", "1.0.0", "a"), "1.0.0", "a")
    (root / "Branchglance.exe").write_bytes(b"launcher")
    paths = Paths(root, tmp_path / "Menu", tmp_path / "Startup", tmp_path / "Local" / "Branchglance",
                  tmp_path / "Temp")
    events = []
    clock = [0.0]
    monkeypatch.setattr(app.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(app.time, "sleep", lambda delta: clock.__setitem__(0, clock[0] + delta))
    public = {"product": "Branchglance", "pid": 404, "build": "a", "stateRoot": str(paths.product_root / "state")}
    class Control:
        def identity(self): return public
        def health(self): return None
    native = SimpleNamespace(control=Control(), popen=lambda *args, **_kwargs: events.append(args))
    with pytest.raises(SafetyError, match="authenticated health"):
        _restart_after_install(paths, native)
    assert len(events) == 1


def test_setup_refuses_redirected_appdata_before_creating_product_folders(monkeypatch, tmp_path):
    from branchglance_installer import app
    from branchglance_installer.app import Paths
    paths = Paths(tmp_path / "Programs" / "Branchglance", tmp_path / "Menu", tmp_path / "Startup",
                  tmp_path / "Local" / "Branchglance", tmp_path / "Temp")
    monkeypatch.setattr(app.product, "appdata_probe", lambda _environment: ("redirected", "package LocalCache"))
    with pytest.raises(SafetyError, match="Run Branchglance Setup from File Explorer or the Start menu"):
        app._prepare_appdata(paths)
    assert not paths.product_root.exists()


def test_setup_fails_closed_on_unknown_appdata_probe_with_reason(monkeypatch, tmp_path):
    from branchglance_installer import app
    from branchglance_installer.app import Paths
    paths = Paths(tmp_path / "Programs" / "Branchglance", tmp_path / "Menu", tmp_path / "Startup",
                  tmp_path / "Local" / "Branchglance", tmp_path / "Temp")
    monkeypatch.setattr(app.product, "appdata_probe", lambda _environment: ("unknown", "PermissionError: denied"))
    with pytest.raises(SafetyError, match="AppData probe: unknown; PermissionError: denied"):
        app._prepare_appdata(paths)
    assert not paths.product_root.exists()


def test_setup_bootstrap_creates_real_product_and_claude_event_folders_and_reports_strays(monkeypatch, tmp_path):
    from branchglance_installer import app
    from branchglance_installer.app import Paths
    paths = Paths(tmp_path / "Programs" / "Branchglance", tmp_path / "Menu", tmp_path / "Startup",
                  tmp_path / "Local" / "Branchglance", tmp_path / "Temp")
    stray = tmp_path / "Local" / "Packages" / "Claude" / "LocalCache" / "Local" / "Branchglance"
    monkeypatch.setattr(app.product, "appdata_probe", lambda _environment: ("not_redirected", None))
    monkeypatch.setattr(app.product, "stranded_appdata_paths", lambda _local: [stray])
    assert app._prepare_appdata(paths) == [str(stray)]
    assert paths.product_root.is_dir() and (paths.product_root / "claude-events").is_dir()
    assert not stray.exists()


def test_setup_reports_recovery_failure_and_returns_failure_exit_code(monkeypatch, tmp_path, capsys):
    from branchglance_installer import app
    from branchglance_installer.app import Paths
    from branchglance_installer.core import ReleaseOps
    from branchglance_installer import windows
    root = _root(tmp_path)
    ReleaseOps(root).install(_release(tmp_path, "initial", "old", "1.0.0", "a"), "1.0.0", "a")
    (root / "Branchglance.exe").write_bytes(b"old launcher")
    payload = _setup_payload(tmp_path, version="1.0.1", build_id="b", body="new")
    paths = Paths(root, tmp_path / "Menu", tmp_path / "Startup", tmp_path / "Local" / "Branchglance",
                  tmp_path / "Temp")
    events = []
    native = _fake_installer_native(paths, running=True, events=events, interrupt="tool-backed-up")
    monkeypatch.setattr(app.product, "appdata_probe", lambda _environment: ("not_redirected", None))
    monkeypatch.setattr(app.product, "stranded_appdata_paths", lambda _local: [])
    monkeypatch.setattr(windows, "cleanup_stale_workers", lambda *_args: None)
    original_recover = ReleaseOps.recover
    recoveries = []
    def fail_recovery_after_install(self):
        recoveries.append(True)
        if len(recoveries) == 3:
            raise SafetyError("injected recovery refusal")
        return original_recover(self)
    monkeypatch.setattr(ReleaseOps, "recover", fail_recovery_after_install)
    code = app.main(["--quiet", "--payload", str(payload)], paths=paths, native=native)
    assert code == 2
    stderr = capsys.readouterr().err
    assert "transaction restoration also failed" in stderr and "injected recovery refusal" in stderr


def test_rollback_uses_healthy_previous_when_current_release_is_damaged(tmp_path):
    root = _root(tmp_path)
    ops = ReleaseOps(root)
    ops.install(_release(tmp_path, "first", version="1.0.0", build_id="a"), "1.0.0", "a")
    ops.install(_release(tmp_path, "second", version="1.0.1", build_id="b"), "1.0.1", "b")
    (root / "releases" / "1.0.1+b" / "BranchglanceApp.exe").write_text("damaged")
    pointer = ops.rollback()
    assert pointer["current"]["version"] == "1.0.0+a" and pointer["previous"] is None
    damaged = [item for item in (root / "releases").iterdir() if item.name.startswith(".quarantine-")]
    assert len(damaged) == 1 and (damaged[0] / "BranchglanceApp.exe").read_text() == "damaged"


@pytest.mark.parametrize("stop_after", ["journal-created", "release-staged", "release-verified",
    "release-promoted", "pointer-switched", "integration-applied", "commit-journaled"])
def test_interrupted_install_recovers_idempotently(tmp_path, stop_after):
    root = _root(tmp_path)
    before = ReleaseOps(root)
    first = _release(tmp_path, "first", version="1.0.0", build_id="a")
    before.install(first, "1.0.0", "a")
    old_pointer = before.current()
    stage = _release(tmp_path, "next", version="1.0.1", build_id="b")
    def interrupt(name):
        if name == stop_after:
            raise RuntimeError("power loss")
    ops = ReleaseOps(root, after_mutation=interrupt)
    with pytest.raises(RuntimeError, match="power loss"):
        ops.install(stage, "1.0.1", "b")
    recovery = ReleaseOps(root)
    recovery.recover()
    recovery.recover()
    expected = old_pointer if stop_after != "commit-journaled" else {"schemaVersion": 1,
        "current": {"version": "1.0.1+b", "path": "releases/1.0.1+b"},
        "previous": old_pointer["current"]}
    assert recovery.current() == expected
    assert not (root / "install-journal.json").exists()


def test_trash_reservation_recovers_between_ledger_and_journal_writes(tmp_path):
    from branchglance_installer.core import Journal, read_owned_ledger, trash_hash_snapshot
    import shutil
    root = _root(tmp_path)
    source = _release(tmp_path, "trash-source", version="2.0.0", build_id="t")
    releases = root / "releases"
    releases.mkdir()
    shutil.copytree(source, releases / "2.0.0+t")
    record = Journal(root).begin("install", "3.0.0+x", {"pointer": None, "integration": {}})
    entry = {"name": f".trash-{record['id']}-0", "version": "2.0.0+t",
             "files": trash_hash_snapshot(releases / "2.0.0+t", "2.0.0+t")}
    def interrupt_ledger(name):
        if name == "trash-intent-ledgered": raise RuntimeError("ledger committed before journal finalize")
    with pytest.raises(RuntimeError, match="ledger committed"):
        ReleaseOps(root, after_mutation=interrupt_ledger)._reserve_trash(entry, read_owned_ledger(root), record)
    pending = Journal(root).load()
    assert pending["trashIntent"]["name"] == entry["name"]
    def interrupt_recovery(name):
        if name == "trash-reserved": raise RuntimeError("recovery interrupted after paired write")
    with pytest.raises(RuntimeError, match="recovery interrupted"):
        ReleaseOps(root, after_mutation=interrupt_recovery)._reconcile_trash_intent(pending)
    recovered = Journal(root).load()
    assert [item["name"] for item in recovered["trashEntries"]] == [entry["name"]]
    assert "trashIntent" not in recovered
    ReleaseOps(root)._reconcile_trash_intent(recovered)
    assert [item["name"] for item in read_owned_ledger(root)["trash"]] == [entry["name"]]


def test_reserved_trash_resume_renames_source_and_recovers_end_to_end(tmp_path):
    import shutil
    from branchglance_installer.core import Journal, trash_hash_snapshot
    root = _root(tmp_path)
    releases = root / "releases"
    releases.mkdir()
    source = _release(tmp_path, "reserved-source", version="1.0.0", build_id="old")
    shutil.copytree(source, releases / "1.0.0+old")
    record = Journal(root).begin("install", "2.0.0+new", {"pointer": None, "integration": {}})
    entry = {"name": f".trash-{record['id']}-0", "version": "1.0.0+old",
             "files": trash_hash_snapshot(releases / "1.0.0+old", "1.0.0+old")}
    ops = ReleaseOps(root)
    ops._reserve_trash(entry, ops._ledger(), record)
    assert ops._trash_valid_release(releases / "1.0.0+old", "1.0.0+old", record=record) == entry["name"]
    assert not (releases / "1.0.0+old").exists() and (releases / entry["name"]).exists()
    assert ReleaseOps(root).recover() == "rolled-back"
    assert not (releases / entry["name"]).exists() and not (root / "install-journal.json").exists()


def test_recorded_trash_partial_deletion_retries_but_added_changed_and_redirected_files_refuse(tmp_path):
    import shutil
    from branchglance_installer.core import LocalFileSystem, read_owned_ledger, remove_plain_tree
    root = _root(tmp_path)
    source = _release(tmp_path, "trash-source", body="bundle", version="2.0.0", build_id="t")
    (source / "nested").mkdir()
    (source / "nested" / "asset.bin").write_bytes(b"asset")
    write_manifest(source, "2.0.0", "t")
    (root / "releases").mkdir()
    shutil.copytree(source, root / "releases" / "2.0.0+t")
    class PartialDelete(LocalFileSystem):
        def __init__(self): self.fail = True
        def remove_tree(self, path):
            if self.fail:
                self.fail = False
                next(item for item in path.rglob("*") if item.is_file()).unlink()
                raise PermissionError("antivirus held one trash file")
            remove_plain_tree(path)
    fs = PartialDelete()
    ops = ReleaseOps(root, fs=fs)
    name = ops._trash_valid_release(root / "releases" / "2.0.0+t", "2.0.0+t")
    entry = next(item for item in read_owned_ledger(root)["trash"] if item["name"] == name)
    with pytest.raises(PermissionError, match="antivirus"):
        ops._delete_recorded_trash(entry)
    ledger_entry = next(item for item in read_owned_ledger(root)["trash"] if item["name"] == name)
    assert ledger_entry["deleting"] is True
    assert ops._delete_recorded_trash(entry) and not (root / "releases" / name).exists()

    for mutation in ("added", "changed", "redirected"):
        release = root / "releases" / "2.0.1+t"
        shutil.copytree(source, release)
        name = ReleaseOps(root)._trash_valid_release(release, "2.0.0+t")
        trash = root / "releases" / name
        if mutation == "added":
            (trash / "extra.txt").write_text("foreign")
        elif mutation == "changed":
            (trash / "nested" / "asset.bin").write_bytes(b"changed")
        else:
            outside = tmp_path / "outside"
            outside.mkdir(exist_ok=True)
            try:
                (trash / "redirect").symlink_to(outside, target_is_directory=True)
            except (OSError, NotImplementedError):
                pytest.skip("directory symlinks are unavailable")
        entry = next(item for item in read_owned_ledger(root)["trash"] if item["name"] == name)
        with pytest.raises(SafetyError):
            ReleaseOps(root)._delete_recorded_trash(entry)
        assert trash.exists()


@pytest.mark.parametrize("stop_after", ["rollback-journaled", "pointer-rolled-back",
                                        "integration-rolled-back", "rollback-committed"])
def test_interrupted_rollback_recovers_idempotently(tmp_path, stop_after):
    root = _root(tmp_path)
    base = ReleaseOps(root)
    base.install(_release(tmp_path, "first", version="1.0.0", build_id="a"), "1.0.0", "a")
    base.install(_release(tmp_path, "second", version="1.0.1", build_id="b"), "1.0.1", "b")
    before = base.current()
    def interrupt(name):
        if name == stop_after:
            raise RuntimeError("power loss")
    with pytest.raises(RuntimeError, match="power loss"):
        ReleaseOps(root, after_mutation=interrupt).rollback()
    recovery = ReleaseOps(root)
    recovery.recover()
    recovery.recover()
    expected = before if stop_after != "rollback-committed" else {
        "schemaVersion":1, "current":{"version":"1.0.0+a","path":"releases/1.0.0+a"},
        "previous":{"version":"1.0.1+b","path":"releases/1.0.1+b"}}
    assert recovery.current() == expected
    assert not (root / "install-journal.json").exists()


def test_corrupt_journal_fails_closed_and_foreign_root_is_refused(tmp_path):
    root = _root(tmp_path)
    (root / "install-journal.json").write_text("{bad", encoding="utf-8")
    with pytest.raises(SafetyError, match="valid JSON"):
        ReleaseOps(root).recover()
    foreign = tmp_path / "foreign"
    foreign.mkdir()
    with pytest.raises(SafetyError, match="ownership"):
        ReleaseOps(foreign).install(_release(tmp_path, "stage"), "1.0.0", "x")


def test_install_journal_cannot_supply_an_arbitrary_release_path(tmp_path):
    from branchglance_installer.core import JOURNAL
    root = _root(tmp_path)
    record = {"schemaVersion": 1, "id": "a" * 32, "operation": "install", "target": "1.0.0+x",
              "phase": "switching", "before": {"pointer": None, "integration": {}}, "created": 1.0,
              "releasePath": "releases"}
    (root / JOURNAL).write_text(json.dumps(record))
    with pytest.raises(SafetyError, match="unsupported fields"):
        ReleaseOps(root).recover()


def test_prune_keeps_only_current_and_previous_and_quarantines_bad_release(tmp_path):
    root = _root(tmp_path)
    ops = ReleaseOps(root)
    for n in range(3):
        ops.install(_release(tmp_path, f"stage{n}", version=f"1.0.{n}", build_id=f"b{n}"),
                    f"1.0.{n}", f"b{n}")
    removed = ops.prune()
    assert removed == ["releases/1.0.0+b0"]
    assert (root / "releases/1.0.1+b1").exists() and (root / "releases/1.0.2+b2").exists()


def test_preflight_host_matrix_and_staged_self_test(tmp_path):
    check_host(HostInfo(True, 19041, True, 528040, ("155.0.1.0",)))
    with pytest.raises(SafetyError, match="WebView2"):
        check_host(HostInfo(True, 19041, True, 528040, ("0.0.0.0",)))
    with pytest.raises(SafetyError, match="standard user"):
        check_host(HostInfo(True, 22631, True, 528040, ("1",), elevated=True))
    release = _release(tmp_path, "release", version="1.0.0", build_id="a")
    write_manifest(release, "1.0.0", "a")
    calls = []
    def runner(args, **kw):
        calls.append((args, kw))
        Path(args[-1]).write_text(json.dumps({"ok": True, "webview2": "test"}), encoding="utf-8")
        return type("R", (), {"returncode": 0})()
    staged_self_test(release, runner=runner)
    assert calls[0][0][-2] == "--self-test"
    assert calls[0][1]["timeout"] == 30


def test_current_host_maps_registry_hive_labels_to_winreg_constants(monkeypatch):
    import ctypes
    import platform
    import types
    from branchglance_installer import preflight

    hklm, hkcu = object(), object()
    opened = []
    class Key:
        def __init__(self, values): self.values = values
        def __enter__(self): return self
        def __exit__(self, *_): return False
    registry = types.ModuleType("winreg")
    registry.HKEY_LOCAL_MACHINE, registry.HKEY_CURRENT_USER = hklm, hkcu
    def open_key(hive, path):
        opened.append((hive, path))
        if "NET Framework" in path: return Key({"Release": 528040})
        return Key({"pv": "155.0.1.0"})
    registry.OpenKey = open_key
    registry.QueryValueEx = lambda key, name: (key.values[name], 1)
    monkeypatch.setitem(sys.modules, "winreg", registry)
    monkeypatch.setattr(platform, "version", lambda: "10.0.19045")
    monkeypatch.setattr(platform, "machine", lambda: "AMD64")
    monkeypatch.setattr(ctypes, "windll", types.SimpleNamespace(shell32=types.SimpleNamespace(IsUserAnAdmin=lambda: 0)), raising=False)
    host = preflight.current_host()
    assert host.windows and host.build == 19045 and host.x64 and host.dotnet_release == 528040
    assert host.webview_pv == ("155.0.1.0", "155.0.1.0") and not host.elevated
    assert opened[1][0] is hklm and opened[2][0] is hkcu


def test_lifecycle_orders_verified_exit_before_instance_mutex_and_recovers_abandoned():
    calls = []
    class Mutex:
        def acquire(self, name, timeout):
            calls.append(("acquire", name)); return name, "abandoned" if name.endswith("Instance") else "acquired"
        def release(self, handle): calls.append(("release", handle))
    class Control:
        def identity(self): calls.append(("identity",)); return {"pid": 10}
        def maintenance_exit(self): calls.append(("exit",)); return True
    class Processes:
        def wait_for_instance_exit(self, timeout): calls.append(("wait",)); return True
    with Lifecycle(Mutex(), Control(), Processes(), lambda: calls.append(("recover",))):
        calls.append(("commit",))
    assert [x[0] for x in calls] == ["acquire", "identity", "exit", "wait", "acquire", "recover", "commit", "release", "release"]
    from branchglance_installer.lifecycle import INSTALL_MUTEX, INSTANCE_MUTEX
    from token_tracker.product import mutex_name
    assert INSTALL_MUTEX == mutex_name("Install") and INSTANCE_MUTEX == mutex_name("Instance")
    assert INSTALL_MUTEX.startswith("Global\\Branchglance-S-1-") and INSTANCE_MUTEX.endswith("-Instance")


def test_lifecycle_confirms_lost_maintenance_reply_by_acquiring_instance_mutex():
    calls = []
    class Mutex:
        def acquire(self, name, timeout):
            calls.append(("acquire", name))
            return name, "acquired"
        def release(self, handle): calls.append(("release", handle))
    class Control:
        def identity(self): return {"product": "Branchglance", "pid": 42}
        def maintenance_exit(self): calls.append(("maintenance", "response-lost")); return None
    class Processes:
        def wait_for_instance_exit(self, _timeout): pytest.fail("lost reply must be resolved by mutex ownership")
    with Lifecycle(Mutex(), Control(), Processes(), lambda: None):
        calls.append(("entered",))
    assert calls[0][0] == "acquire" and calls[1] == ("maintenance", "response-lost")
    assert any(item[0] == "acquire" and item[1].endswith("-Instance") for item in calls)


def test_app_main_resumes_markerless_uninstall_journal(tmp_path, monkeypatch):
    from branchglance_installer import app
    from branchglance_installer.app import Native, Paths, Ui
    from branchglance_installer.core import JOURNAL
    root = tmp_path / "Programs" / "Branchglance"
    root.mkdir(parents=True)
    record = {"schemaVersion": 1, "id": "b" * 32, "operation": "uninstall", "target": "uninstall",
              "phase": "switching", "before": {"pointer": None, "integration": {}}, "created": 1.0}
    (root / JOURNAL).write_text(json.dumps(record))
    temp = tmp_path / "Temp"
    temp.mkdir()
    worker = temp / ("Branchglance-uninstall-" + "c" * 32 + ".exe")
    worker.write_bytes(b"worker")
    paths = Paths(root, tmp_path / "Menu", tmp_path / "Startup", tmp_path / "AppData", temp)
    class Mutex:
        def acquire(self, name, timeout): return object(), "acquired"
        def release(self, handle): pass
    class Control:
        def identity(self): return None
    class Adapter:
        def read_shortcut(self, path): return None
        def remove_shortcut(self, path): pass
        def read_uninstall(self): return None
        def remove_uninstall(self, **kwargs): pass
    native = Native(lambda: HostInfo(True, 19041, True, 528040, ("155",)), lambda: False,
                    Mutex(), Control(), object(), Adapter())
    monkeypatch.setattr("branchglance_installer.windows.schedule_self_delete",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("cleanup denied")))
    assert app.main(["--uninstall-worker"], role="uninstall", paths=paths, native=native,
                    ui=Ui(quiet=True), executable=worker) == 0
    assert not root.exists()


def test_app_main_dispatches_release_rollback(tmp_path, monkeypatch):
    from branchglance_installer import app
    from branchglance_installer.app import Native, Paths, Ui
    paths = Paths(tmp_path / "root", tmp_path / "menu", tmp_path / "startup", tmp_path / "data", tmp_path / "temp")
    native = Native(lambda: HostInfo(True, 19041, True, 528040, ("155",)), lambda: False,
                    object(), object(), object(), object())
    calls = []
    monkeypatch.setattr(app, "rollback", lambda got_paths, got_native: calls.append((got_paths, got_native)) or
                        {"current": {"version": "1.0.0+a"}})
    assert app.main(["--rollback"], paths=paths, native=native, ui=Ui(quiet=True)) == 0
    assert calls == [(paths, native)]


def test_owned_root_marker_is_present_and_strict(tmp_path):
    root = _root(tmp_path)
    assert json.loads((root / OWNER_FILE).read_text())["product"] == "Branchglance"


def test_uninstall_removes_only_owned_programs_and_leaves_app_data(tmp_path):
    root = _root(tmp_path)
    ops = ReleaseOps(root)
    ops.install(_release(tmp_path, "stage", version="1.0.0", build_id="a"), "1.0.0", "a")
    app_data = tmp_path / "local" / "Branchglance" / "state"
    app_data.mkdir(parents=True)
    (app_data / "user.json").write_text("keep")
    removed = []
    uninstall_owned_root(root, integration_remove=lambda: removed.append("known-integrations"))
    assert removed == ["known-integrations"]
    assert not root.exists() and (app_data / "user.json").read_text() == "keep"


def test_uninstall_refuses_foreign_root_and_unfinished_journal_before_mutation(tmp_path):
    foreign = tmp_path / "foreign"
    foreign.mkdir()
    calls = []
    with pytest.raises(SafetyError, match="ownership"):
        uninstall_owned_root(foreign, integration_remove=lambda: calls.append("removed"))
    root = _root(tmp_path)
    (root / "install-journal.json").write_text(json.dumps({"schemaVersion": 1, "id": "d" * 32,
        "operation": "install", "target": "1.0.0+a", "phase": "switching",
        "before": {"pointer": None, "integration": {}}, "created": 1.0, "promotionIntent": False,
        "partialIntent": True}))
    with pytest.raises(SafetyError, match="Unfinished"):
        uninstall_owned_root(root, integration_remove=lambda: calls.append("removed"))
    assert calls == []


@pytest.mark.parametrize("stop_after", ["integrations-removed", "uninstall-journaled", "uninstall-trash-journaled",
    "uninstall-release-trashed", "uninstall-release-deleted", "uninstall-ledger-updated",
    "pointer-removed", "known-file-removed", "ledger-deleted", "owner-marker-deleted", "root-removed"])
def test_interrupted_uninstall_resumes_idempotently_and_keeps_app_data(tmp_path, stop_after):
    from branchglance_installer.core import read_owned_ledger, write_owned_ledger
    root = _root(tmp_path)
    ops = ReleaseOps(root)
    ops.install(_release(tmp_path, "stage", version="1.0.0", build_id="a"), "1.0.0", "a")
    (root / "Branchglance.exe").write_bytes(b"owned launcher")
    ledger = read_owned_ledger(root)
    ledger["tools"].append("Branchglance.exe")
    write_owned_ledger(root, ledger)
    data = tmp_path / "LocalAppData" / "Branchglance" / "state" / "history.json"
    data.parent.mkdir(parents=True)
    data.write_text("keep")
    fired = []

    def interrupt(name):
        if name == stop_after and not fired:
            fired.append(name)
            raise RuntimeError("power loss")

    with pytest.raises(RuntimeError, match="power loss"):
        uninstall_owned_root(root, after_mutation=interrupt)
    uninstall_owned_root(root)
    assert not root.exists()
    assert data.read_text() == "keep"


def test_owned_tree_deletion_refuses_reparse_points(tmp_path):
    import os
    from branchglance_installer.core import remove_plain_tree
    root = tmp_path / "owned"
    outside = tmp_path / "outside"
    root.mkdir(); outside.mkdir()
    sentinel = outside / "keep.txt"
    sentinel.write_text("keep")
    try:
        os.symlink(outside, root / "link", target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("directory symlink creation is unavailable")
    with pytest.raises(SafetyError, match="Reparse point"):
        remove_plain_tree(root)
    assert sentinel.read_text() == "keep"


def test_integration_manager_uses_temp_replace_owned_marker_and_preserves_foreign(tmp_path):
    class Adapter:
        links = {}
        registry = None
        def read_shortcut(self, path): return self.links.get(str(path))
        def replace_shortcut(self, path, **values): self.links[str(path)] = dict(values)
        def remove_shortcut(self, path): self.links.pop(str(path), None)
        def read_uninstall(self): return self.registry
        def write_uninstall(self, values): self.registry = dict(values)
        def remove_uninstall(self, *, expected=None):
            if self.registry is None: return
            if expected is None: self.registry = None
            else: self.registry = {k:v for k,v in self.registry.items() if not (k in expected and expected[k] == v)}
    adapter = Adapter()
    root, menu, startup = tmp_path / "Programs" / "Branchglance", tmp_path / "Menu", tmp_path / "Startup"
    launcher = root / "Branchglance.exe"
    integration = Integrations(adapter, root, launcher, "1.0.0", menu, startup)
    integration.apply(startup_enabled=True)
    assert adapter.links[str(menu / "Branchglance.lnk")]["description"] == MARKER
    assert adapter.links[str(startup / "Branchglance.lnk")]["arguments"] == "--background"
    assert adapter.registry["InstallLocation"] == str(root)
    snapshot = integration.snapshot()
    integration.apply(startup_enabled=False)
    integration.restore(snapshot)
    foreign = dict(adapter.links[str(menu / "Branchglance.lnk")], target=str(tmp_path / "foreign.exe"))
    adapter.links[str(menu / "Branchglance.lnk")] = foreign
    adapter.registry["OtherAppValue"] = "keep"
    integration.remove()
    assert str(menu / "Branchglance.lnk") in adapter.links
    assert str(startup / "Branchglance.lnk") not in adapter.links
    assert adapter.registry == {"OtherAppValue": "keep"}


def test_integration_shortcuts_use_launcher_icon_on_start_menu_desktop_and_startup(tmp_path):
    from token_tracker import product
    class Adapter:
        def __init__(self): self.links = {}; self.registry = None
        def read_shortcut(self, path): return self.links.get(str(path))
        def replace_shortcut(self, path, **values): self.links[str(path)] = dict(values)
        def remove_shortcut(self, path): self.links.pop(str(path), None)
        def read_uninstall(self): return self.registry
        def write_uninstall(self, values): self.registry = dict(values)
        def remove_uninstall(self, *, expected=None): self.registry = None
    adapter = Adapter()
    root = tmp_path / "Programs" / "Branchglance"
    launcher = root / "Branchglance.exe"
    menu, desktop, startup = tmp_path / "Menu", tmp_path / "Desktop", tmp_path / "Startup"
    integration = Integrations(adapter, root, launcher, "1.0.0", menu, startup,
                                root / "BranchglanceUninstall.exe", desktop)
    integration.apply(startup_enabled=True)
    expected = {menu / "Branchglance.lnk", desktop / "Branchglance.lnk", startup / "Branchglance.lnk"}
    assert {Path(path) for path in adapter.links} == expected
    assert all(link["target"] == str(launcher) for link in adapter.links.values())
    assert all(link["app_id"] == product.APP_USER_MODEL_ID for link in adapter.links.values())
    assert all(link["description"] == MARKER for link in adapter.links.values())
    assert adapter.registry["DisplayIcon"] == str(launcher)


def test_integration_manager_refuses_foreign_collision_before_writes(tmp_path):
    class Adapter:
        def __init__(self): self.links = {}; self.registry = None; self.writes = 0
        def read_shortcut(self, path): return self.links.get(str(path))
        def replace_shortcut(self, path, **values): self.writes += 1
        def remove_shortcut(self, path): self.writes += 1
        def read_uninstall(self): return self.registry
        def write_uninstall(self, values): self.writes += 1
        def remove_uninstall(self, *, expected=None): self.writes += 1
    adapter = Adapter()
    path = tmp_path / "Menu" / "Branchglance.lnk"
    adapter.links[str(path)] = {"description":"Other app", "target":"elsewhere.exe"}
    integration = Integrations(adapter, tmp_path / "Programs" / "Branchglance",
                               tmp_path / "Programs" / "Branchglance" / "Branchglance.exe",
                               "1.0.0", tmp_path / "Menu")
    with pytest.raises(SafetyError, match="Foreign shortcut"):
        integration.apply()
    assert adapter.writes == 0


def test_registry_recovery_removes_only_values_written_by_interrupted_install(tmp_path):
    class Adapter:
        registry = None
        def read_shortcut(self, path): return None
        def replace_shortcut(self, path, **values): pass
        def remove_shortcut(self, path): pass
        def read_uninstall(self): return self.registry
        def write_uninstall(self, values):
            self.registry = {**(self.registry or {}), **values}
        def remove_uninstall(self, *, expected=None):
            self.registry = {k:v for k,v in (self.registry or {}).items()
                             if not (expected and k in expected and expected[k] == v)}
    adapter = Adapter()
    root = tmp_path / "Programs" / "Branchglance"
    integration = Integrations(adapter, root, root / "Branchglance.exe", "1.0.0+a", tmp_path / "Menu")
    before = integration.snapshot()
    adapter.registry = {"DisplayName": "Branchglance", "ForeignValue": "keep"}
    integration.restore(before)
    assert adapter.registry == {"ForeignValue": "keep"}


@pytest.mark.skipif(sys.platform != "win32", reason="Windows COM/registry adapter contract")
def test_real_shell_link_adapter_uses_only_temp_shortcuts_and_removes_them(tmp_path):
    from branchglance_installer.windows import WindowsIntegrationAdapter
    adapter = WindowsIntegrationAdapter(uninstall_key=r"Software\BranchglanceTest\unused")
    target = tmp_path / "fake-Branchglance.exe"
    target.write_bytes(b"not an executable")
    shortcuts = [tmp_path / "Desktop" / "Branchglance.lnk", tmp_path / "Startup" / "Branchglance.lnk"]
    for path, args in zip(shortcuts, ("", "--background")):
        adapter.replace_shortcut(path, target=str(target), arguments=args, workdir=str(tmp_path),
                                 app_id="Branchglance.Test", description="Branchglance managed shortcut")
        found = adapter.read_shortcut(path)
        assert found == {"target": str(target), "arguments": args, "workdir": str(tmp_path),
                         "description": "Branchglance managed shortcut", "app_id": "Branchglance.Test"}
    for path in shortcuts:
        adapter.remove_shortcut(path)
        assert adapter.read_shortcut(path) is None


@pytest.mark.skipif(sys.platform != "win32" or os.environ.get("BRANCHGLANCE_SKIP_REAL_REGISTRY_WRITE") == "1",
                    reason="HKCU adapter contract unavailable in this sandbox")
def test_real_registry_adapter_uses_and_deletes_only_a_throwaway_test_key():
    import uuid
    import winreg
    from branchglance_installer.windows import WindowsIntegrationAdapter
    parent = r"Software\BranchglanceTest"
    leaf = "AdapterTest-" + uuid.uuid4().hex
    test_key = parent + "\\" + leaf
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, parent):
            parent_preexisted = True
    except FileNotFoundError:
        parent_preexisted = False
    adapter = WindowsIntegrationAdapter(uninstall_key=test_key)
    try:
        assert adapter.read_uninstall() is None
        expected = {"DisplayName":"Branchglance test", "InstallLocation":r"C:\fake\Branchglance", "NoRepair":1}
        try:
            adapter.write_uninstall(expected)
        except PermissionError as exc:
            pytest.skip(f"sandbox denied write to disposable HKCU test key: {exc}")
        assert adapter.read_uninstall() == expected
        adapter.remove_uninstall()
        assert adapter.read_uninstall() is None
    finally:
        try:
            winreg.DeleteKey(winreg.HKEY_CURRENT_USER, test_key)
        except OSError:
            pass
        if not parent_preexisted:
            try:
                winreg.DeleteKey(winreg.HKEY_CURRENT_USER, parent)
            except OSError:
                pass


@pytest.mark.skipif(sys.platform != "win32", reason="MoveFileExW adapter contract")
def test_real_movefile_adapter_replaces_only_temp_files(tmp_path):
    from branchglance_installer.windows import WindowsFileSystem
    source, target = tmp_path / "new.json", tmp_path / "current.json"
    source.write_text("new")
    target.write_text("old")
    WindowsFileSystem().replace(source, target)
    assert target.read_text() == "new" and not source.exists()


def test_uninstall_worker_cleanup_is_best_effort_and_ledger_confined(tmp_path):
    from branchglance_installer.windows import cleanup_stale_workers, schedule_self_delete
    worker = tmp_path / ("Branchglance-uninstall-" + "a" * 32 + ".exe")
    worker.write_bytes(b"temp")
    ledger = tmp_path / "temp-workers.json"
    assert not schedule_self_delete(worker, tmp_path, ledger, delete_file=lambda _path: False, pid=4321)
    assert json.loads(ledger.read_text()) == {"schemaVersion": 1, "workers": [{"name": worker.name, "pid": 4321}]}
    calls = []
    def delete(path):
        calls.append(path)
        path.unlink()
        return True
    assert cleanup_stale_workers(tmp_path, ledger, is_running=lambda *_: False, delete_file=delete) == [worker.name]
    assert calls == [worker] and not worker.exists() and not ledger.exists()
    calls.clear()
    evil = tmp_path / ("Branchglance-uninstall-%" + "a" * 31 + ".exe")
    evil.write_bytes(b"temp")
    assert not schedule_self_delete(evil, tmp_path, ledger, delete_file=lambda _path: calls.append(_path) or True)
    assert not schedule_self_delete(tmp_path / "Branchglance.exe", tmp_path, ledger,
                                    delete_file=lambda _path: calls.append(_path) or True)
    assert calls == []


def test_mismatched_release_is_quarantined_and_foreign_content_is_retained(tmp_path):
    root = _root(tmp_path)
    ops = ReleaseOps(root)
    ops.install(_release(tmp_path, "a", version="1.0.0", build_id="a"), "1.0.0", "a")
    tampered = root / "releases" / "1.0.1+b"
    tampered.mkdir()
    (tampered / "BranchglanceApp.exe").write_text("foreign")  # no manifest: not ours
    ops.install(_release(tmp_path, "b", version="1.0.1", build_id="b"), "1.0.1", "b")
    quarantined = [p for p in (root / "releases").iterdir() if p.name.startswith(".quarantine-")]
    assert len(quarantined) == 1 and (quarantined[0] / "BranchglanceApp.exe").read_text() == "foreign"
    ops.install(_release(tmp_path, "c", version="1.0.2", build_id="c"), "1.0.2", "c")
    removed = ops.prune()
    assert "releases/1.0.0+a" in removed
    assert quarantined[0].exists() and (quarantined[0] / "BranchglanceApp.exe").read_text() == "foreign"


def test_version_names_cannot_escape_or_look_like_quarantine_dirs():
    from branchglance_installer.core import validate_version
    for good in ("1.0.0+abc", "1.2.3-rc.1+build.7", "1.0.0-beta-2+x_y-z"):
        assert validate_version(good) == good
    for bad in ("1.0.0+..", "1.0.0+a..b", "1.0.0+.x", "1.0.0+x.", "1.0.0-..+x", "1.0.0+a\b", ".quarantine-1"):
        with pytest.raises(SafetyError):
            validate_version(bad)


def test_missing_owner_marker_is_reported_as_ownership(tmp_path):
    from branchglance_installer.core import require_owned_root
    bare = tmp_path / "bare"
    bare.mkdir()
    with pytest.raises(SafetyError, match="ownership marker is missing"):
        require_owned_root(bare)
    (bare / OWNER_FILE).write_text("{bad")
    with pytest.raises(SafetyError, match="ownership marker is unreadable"):
        require_owned_root(bare)
