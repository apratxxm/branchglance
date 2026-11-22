"""product.atomic_write_bytes and the fail-closed reparse helpers."""
import os
import stat
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from token_tracker import product


def _leftovers(directory: Path):
    return [p.name for p in directory.iterdir() if p.name.endswith(".tmp")]


def test_atomic_write_replaces_and_leaves_no_temp(tmp_path):
    target = tmp_path / "state.json"
    target.write_bytes(b"old")
    product.atomic_write_bytes(target, b"new")
    assert target.read_bytes() == b"new"
    assert _leftovers(tmp_path) == []


def test_concurrent_writers_leave_one_complete_payload(tmp_path):
    target = tmp_path / "state.json"
    payloads = [bytes([65 + i]) * 4096 for i in range(8)]
    errors = []

    def writer(payload):
        try:
            for _ in range(25):
                product.atomic_write_bytes(target, payload)
        except Exception as exc:  # collected and asserted below
            errors.append(exc)

    threads = [threading.Thread(target=writer, args=(p,)) for p in payloads]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    # Windows can refuse a rename onto a target another thread is replacing (sharing
    # violation). A refused write must surface as PermissionError and never tear the file.
    assert all(isinstance(exc, PermissionError) for exc in errors)
    assert len(errors) < len(threads) * 25
    assert target.read_bytes() in payloads
    assert _leftovers(tmp_path) == []


def test_interrupted_write_keeps_previous_file(tmp_path, monkeypatch):
    target = tmp_path / "state.json"
    target.write_bytes(b"previous")

    def fail(_fd):
        raise OSError("disk went away")

    monkeypatch.setattr(product.os, "fsync", fail)
    with pytest.raises(OSError):
        product.atomic_write_bytes(target, b"partial replacement")
    monkeypatch.undo()
    assert target.read_bytes() == b"previous"
    assert _leftovers(tmp_path) == []


def test_failed_rename_keeps_previous_file(tmp_path, monkeypatch):
    target = tmp_path / "state.json"
    target.write_bytes(b"previous")

    def fail(_src, _dst):
        raise PermissionError("locked")

    monkeypatch.setattr(product.os, "replace", fail)
    with pytest.raises(PermissionError):
        product.atomic_write_bytes(target, b"next")
    monkeypatch.undo()
    assert target.read_bytes() == b"previous"
    assert _leftovers(tmp_path) == []


def test_before_replace_false_abandons_the_write(tmp_path):
    target = tmp_path / "log.jsonl"
    target.write_bytes(b"keep")
    product.atomic_write_bytes(target, b"discard", before_replace=lambda: False)
    assert target.read_bytes() == b"keep"
    assert _leftovers(tmp_path) == []


def test_reparse_attribute_and_symlink_are_detected():
    junction = SimpleNamespace(st_mode=stat.S_IFDIR | 0o755, st_file_attributes=0x400)
    link = SimpleNamespace(st_mode=stat.S_IFLNK | 0o777, st_file_attributes=0)
    plain = SimpleNamespace(st_mode=stat.S_IFREG | 0o644, st_file_attributes=0x20)
    assert product.info_is_reparse(junction) is True
    assert product.info_is_reparse(link) is True
    assert product.info_is_reparse(plain) is False


def test_is_reparse_fails_closed_on_lstat_error(tmp_path, monkeypatch):
    real = tmp_path / "file.txt"
    real.write_text("x", encoding="utf-8")
    assert product.is_reparse(real) is False
    assert product.is_reparse(tmp_path / "missing") is True

    def deny(path, *args, **kwargs):
        raise PermissionError("denied")

    monkeypatch.setattr(product.os, "lstat", deny)
    assert product.is_reparse(real) is True


def test_hook_installer_refuses_unknown_reparse_state(tmp_path, monkeypatch):
    from token_tracker import claude_hook_install

    settings = tmp_path / "settings.json"
    settings.write_text("{}", encoding="utf-8")
    assert claude_hook_install._is_reparse(settings, os.lstat(settings)) is False
    assert claude_hook_install._is_reparse(settings, None) is False

    def deny(path, *args, **kwargs):
        raise PermissionError("denied")

    monkeypatch.setattr(product.os, "lstat", deny)
    assert claude_hook_install._is_reparse(settings, None) is True
