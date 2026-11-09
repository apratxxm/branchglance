"""P4: handle-bound deletion under junction races, and conditional journal recovery."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from branchglance_installer import handles  # noqa: E402
from branchglance_installer.core import (  # noqa: E402
    RecoveryConflict, ReleaseOps, SafetyError, create_owner, read_json, remove_plain_tree, write_manifest,
)

pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows handle APIs")


def _junction(link: Path, target: Path) -> None:
    result = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


@pytest.fixture
def hook():
    yield lambda fn: setattr(handles, "race_hook", fn)
    handles.race_hook = None


def _tree(tmp_path: Path):
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "sentinel.txt"
    sentinel.write_text("keep", encoding="utf-8")
    root = tmp_path / "owned"
    (root / "sub" / "deep").mkdir(parents=True)
    (root / "a.txt").write_text("a", encoding="utf-8")
    (root / "sub" / "b.txt").write_text("b", encoding="utf-8")
    (root / "sub" / "deep" / "c.txt").write_text("c", encoding="utf-8")
    return root, sentinel


def test_remove_tree_deletes_nested_tree_and_reports_absent(tmp_path):
    root, sentinel = _tree(tmp_path)
    remove_plain_tree(root)
    assert not root.exists() and sentinel.exists()
    remove_plain_tree(root)  # absent: no error


def test_existing_reparse_point_aborts_before_any_delete(tmp_path):
    root, sentinel = _tree(tmp_path)
    _junction(root / "sub" / "link", sentinel.parent)
    with pytest.raises(SafetyError, match="Reparse point refused"):
        remove_plain_tree(root)
    assert (root / "a.txt").exists() and (root / "sub" / "b.txt").exists() and sentinel.exists()


def test_child_swapped_for_junction_before_open_is_not_followed(tmp_path, hook):
    root, sentinel = _tree(tmp_path)
    seen = {}

    def swap(stage, display):
        if stage == "open" and display.endswith("sub") and seen.setdefault("n", 0) == 1:
            os.rename(root / "sub", root / "sub-real")
            _junction(root / "sub", sentinel.parent)
        if stage == "open" and display.endswith("sub"):
            seen["n"] += 1

    hook(swap)
    with pytest.raises(SafetyError, match="Reparse point refused"):
        remove_plain_tree(root)
    assert sentinel.read_text(encoding="utf-8") == "keep"
    assert (root / "sub-real" / "b.txt").exists()  # nothing beneath the swapped name was touched


def test_held_ancestor_cannot_be_swapped_between_open_and_delete(tmp_path, hook):
    root, sentinel = _tree(tmp_path)
    outcomes = []

    def swap(stage, display):
        if stage == "mutate" and display.endswith("b.txt"):
            try:
                os.rename(root / "sub", root / "sub-real")
                _junction(root / "sub", sentinel.parent)
                outcomes.append("swapped")
            except OSError:
                outcomes.append("blocked")

    hook(swap)
    try:
        remove_plain_tree(root)
    except SafetyError:
        pass
    assert outcomes and "swapped" not in outcomes  # the held directory handle denies rename/delete
    assert sentinel.read_text(encoding="utf-8") == "keep"


def test_entry_appearing_mid_walk_is_deleted_or_fails_closed(tmp_path, hook):
    root, sentinel = _tree(tmp_path)
    added = []

    def add(stage, display):
        if stage == "mutate" and display == str(root / "sub") and not added:
            (root / "sub" / "late.txt").write_text("late", encoding="utf-8")
            added.append(1)

    hook(add)
    remove_plain_tree(root)
    assert not root.exists() and sentinel.exists()


def test_entry_vanishing_mid_walk_is_skipped(tmp_path, hook):
    root, sentinel = _tree(tmp_path)
    gone = []

    def vanish(stage, display):
        if stage == "open" and display.endswith("a.txt") and len(gone) == 1:
            (root / "a.txt").unlink()
        if stage == "open" and display.endswith("a.txt"):
            gone.append(1)

    hook(vanish)
    remove_plain_tree(root)
    assert not root.exists() and sentinel.exists()


def test_file_swapped_for_junction_before_delete_is_refused(tmp_path, hook):
    from branchglance_installer.core import _remove_known_file
    root, sentinel = _tree(tmp_path)
    target = root / "a.txt"

    def swap(stage, display):
        if stage == "open" and display.endswith("a.txt"):
            target.unlink()
            _junction(target, sentinel.parent)

    hook(swap)
    with pytest.raises(SafetyError):
        _remove_known_file(target)
    assert sentinel.read_text(encoding="utf-8") == "keep"


def test_remove_known_file_is_plain_file_only(tmp_path):
    from branchglance_installer.core import _remove_known_file
    root, sentinel = _tree(tmp_path)
    assert _remove_known_file(root / "a.txt") is True
    assert _remove_known_file(root / "a.txt") is False
    with pytest.raises(SafetyError):
        _remove_known_file(root / "sub")  # a directory is not an owned file
    assert (root / "sub").exists()


# ---------------------------------------------------------------- recovery


def _release(path: Path, name: str, version="1.0.0", build_id="a"):
    folder = path / name
    folder.mkdir(parents=True)
    (folder / "BranchglanceApp.exe").write_text("bundle", encoding="utf-8")
    write_manifest(folder, version, build_id)
    return folder


class Crash(RuntimeError):
    pass


def _crash_at(point):
    def hook(name):
        if name == point:
            raise Crash(point)
    return hook


def _setup(tmp_path, *, upgrade_crash=None, first_crash=None):
    root = tmp_path / "programs" / "Branchglance"
    create_owner(root)
    old, new = tmp_path / "old.exe", tmp_path / "new.exe"
    old.write_bytes(b"old tool bytes")
    new.write_bytes(b"new tool bytes")
    if first_crash:
        with pytest.raises(Crash):
            ReleaseOps(root, tools={"Branchglance.exe": new},
                       after_mutation=_crash_at(first_crash)).install(_release(tmp_path, "r1"), "1.0.0", "a")
        return root
    ReleaseOps(root, tools={"Branchglance.exe": old}).install(_release(tmp_path, "r1"), "1.0.0", "a")
    with pytest.raises(Crash):
        ReleaseOps(root, tools={"Branchglance.exe": new},
                   after_mutation=_crash_at(upgrade_crash)).install(_release(tmp_path, "r2", "1.0.1", "b"), "1.0.1", "b")
    return root


def _leftovers(root):
    return sorted(p.name for p in root.iterdir() if ".old-" in p.name or ".new-" in p.name)


@pytest.mark.parametrize("point", ["tool-staged", "tool-backed-up", "tool-replaced"])
def test_crash_at_each_tool_phase_restores_old_and_is_idempotent(tmp_path, point):
    root = _setup(tmp_path, upgrade_crash=point)
    assert ReleaseOps(root).recover() == "rolled-back"
    assert (root / "Branchglance.exe").read_bytes() == b"old tool bytes"
    assert _leftovers(root) == []
    assert not (root / "install-journal.json").exists()
    assert ReleaseOps(root).recover() == "clean"
    assert (root / "Branchglance.exe").read_bytes() == b"old tool bytes"


@pytest.mark.parametrize("point", ["tool-restored", "tool-new-removed"])
def test_recovery_interrupted_midway_is_idempotent(tmp_path, point):
    root = _setup(tmp_path, upgrade_crash="tool-replaced")
    with pytest.raises(Crash):
        ReleaseOps(root, after_mutation=_crash_at(point)).recover()
    assert ReleaseOps(root).recover() == "rolled-back"
    assert (root / "Branchglance.exe").read_bytes() == b"old tool bytes"
    assert _leftovers(root) == []


def test_created_tool_is_removed_only_when_it_is_the_installers(tmp_path):
    root = _setup(tmp_path, first_crash="tool-replaced")
    assert (root / "Branchglance.exe").read_bytes() == b"new tool bytes"
    assert ReleaseOps(root).recover() == "rolled-back"
    assert not (root / "Branchglance.exe").exists() and _leftovers(root) == []


def test_foreign_file_in_vacated_live_name_is_kept_with_backup_and_journal(tmp_path):
    root = _setup(tmp_path, upgrade_crash="tool-backed-up")  # live absent, backup holds the old tool
    (root / "Branchglance.exe").write_bytes(b"user's own file")
    with pytest.raises(RecoveryConflict):
        ReleaseOps(root).recover()
    assert (root / "Branchglance.exe").read_bytes() == b"user's own file"
    assert len([n for n in _leftovers(root) if ".old-" in n]) == 1
    journal = read_json(root / "install-journal.json")
    assert journal["phase"] != "rolled-back"
    with pytest.raises(RecoveryConflict):
        ReleaseOps(root).recover()  # still reported, still nothing forced
    assert (root / "Branchglance.exe").read_bytes() == b"user's own file"


def test_foreign_replacement_of_installed_new_tool_is_kept(tmp_path):
    root = _setup(tmp_path, upgrade_crash="tool-replaced")
    (root / "Branchglance.exe").unlink()
    (root / "Branchglance.exe").write_bytes(b"replaced by someone else")
    with pytest.raises(RecoveryConflict):
        ReleaseOps(root).recover()
    assert (root / "Branchglance.exe").read_bytes() == b"replaced by someone else"
    assert any(".old-" in n for n in _leftovers(root))
    assert (root / "install-journal.json").exists()


def test_foreign_live_file_with_backup_missing_is_kept(tmp_path):
    root = _setup(tmp_path, upgrade_crash="tool-staged")  # live is still the old tool, nothing moved
    (root / "Branchglance.exe").write_bytes(b"user edited the tool")
    with pytest.raises(RecoveryConflict):
        ReleaseOps(root).recover()
    assert (root / "Branchglance.exe").read_bytes() == b"user edited the tool"
    assert (root / "install-journal.json").exists()


def test_foreign_file_where_created_tool_was_is_kept(tmp_path):
    root = _setup(tmp_path, first_crash="tool-replaced")
    (root / "Branchglance.exe").unlink()
    (root / "Branchglance.exe").write_bytes(b"not ours")
    with pytest.raises(RecoveryConflict):
        ReleaseOps(root).recover()
    assert (root / "Branchglance.exe").read_bytes() == b"not ours"
    assert (root / "install-journal.json").exists()


def test_tampered_backup_is_not_restored_over_anything(tmp_path):
    root = _setup(tmp_path, upgrade_crash="tool-backed-up")
    backup = next(root / n for n in _leftovers(root) if ".old-" in n)
    backup.write_bytes(b"tampered backup")
    with pytest.raises(RecoveryConflict):
        ReleaseOps(root).recover()
    assert backup.read_bytes() == b"tampered backup" and not (root / "Branchglance.exe").exists()


def test_staged_cleanup_requires_ownership(tmp_path):
    root = _setup(tmp_path, upgrade_crash="tool-staged")
    staged = next(root / n for n in _leftovers(root) if ".new-" in n)
    staged.unlink()
    staged.write_bytes(b"someone else made this name")
    with pytest.raises(RecoveryConflict):
        ReleaseOps(root).recover()
    assert staged.read_bytes() == b"someone else made this name"
    assert (root / "Branchglance.exe").read_bytes() == b"old tool bytes"


def test_partially_copied_stage_is_removed_by_recorded_identity(tmp_path):
    root = _setup(tmp_path, upgrade_crash="tool-staged")
    journal = read_json(root / "install-journal.json")
    journal["tools"]["Branchglance.exe"]["stagedProvenance"] = None  # crash before the copy finished
    from branchglance_installer.core import atomic_json
    atomic_json(root / "install-journal.json", journal)
    staged = next(root / n for n in _leftovers(root) if ".new-" in n)
    with staged.open("ab") as stream:
        stream.write(b" partial")
    assert ReleaseOps(root).recover() == "rolled-back"
    assert _leftovers(root) == []


# ---------------------------------------------------------------- ancestor anchoring (Sol F5)


def _ancestor_junction(tmp_path: Path):
    """tmp/real/owned/... reached through tmp/link -> tmp/real; the sentinel lives under real."""
    real = tmp_path / "real"
    (real / "owned" / "sub").mkdir(parents=True)
    (real / "owned" / "a.txt").write_text("a", encoding="utf-8")
    sentinel = real / "owned" / "sub" / "sentinel.txt"
    sentinel.write_text("keep", encoding="utf-8")
    link = tmp_path / "link"
    _junction(link, real)
    return link / "owned", real / "owned", sentinel


def test_root_reached_through_junctioned_ancestor_is_refused(tmp_path):
    via_link, real_owned, sentinel = _ancestor_junction(tmp_path)
    with pytest.raises(SafetyError, match="expected .*link.*opened object is at"):
        remove_plain_tree(via_link)
    assert sentinel.read_text(encoding="utf-8") == "keep" and (real_owned / "a.txt").exists()


def test_file_reached_through_junctioned_ancestor_is_refused(tmp_path):
    from branchglance_installer.core import _remove_known_file
    via_link, real_owned, sentinel = _ancestor_junction(tmp_path)
    with pytest.raises(SafetyError, match="opened object is at"):
        _remove_known_file(via_link / "a.txt")
    assert (real_owned / "a.txt").exists()


def test_normal_path_forms_still_anchor(tmp_path):
    root, sentinel = _tree(tmp_path)
    remove_plain_tree(Path(str(root).upper()) / ".." / "owned")  # case-insensitive, dot segments normalised
    assert not root.exists() and sentinel.exists()


def test_recovery_refuses_wrapper_file_behind_junctioned_ancestor(tmp_path):
    from branchglance_installer.core import _LockedWrapper
    via_link, real_owned, _ = _ancestor_junction(tmp_path)
    with pytest.raises(SafetyError, match="opened object is at"):
        _LockedWrapper(via_link / "a.txt")
    with pytest.raises(SafetyError):
        from branchglance_installer.core import _conditional_delete
        _conditional_delete(via_link / "a.txt", lambda _p: True, "tool")
    assert (real_owned / "a.txt").read_text(encoding="utf-8") == "a"


def test_recovery_with_junctioned_install_root_keeps_everything(tmp_path):
    root = _setup(tmp_path, upgrade_crash="tool-backed-up")
    link = tmp_path / "programs-link"
    _junction(link, root.parent)
    before = sorted(p.name for p in root.iterdir())
    with pytest.raises((RecoveryConflict, SafetyError)):
        ReleaseOps(link / "Branchglance").recover()
    assert sorted(p.name for p in root.iterdir()) == before
    assert (root / "install-journal.json").exists()
    assert ReleaseOps(root).recover() == "rolled-back"  # the genuine path still recovers
