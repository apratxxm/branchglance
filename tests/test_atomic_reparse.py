"""product.atomic_write_bytes and the fail-closed reparse helpers."""
import os
import stat
import threading
from datetime import datetime, timedelta, timezone
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
    written = []

    def writer(payload):
        for _ in range(25):
            try:
                product.atomic_write_bytes(target, payload)
                written.append(payload)
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
    assert written and len(written) + len(errors) == len(threads) * 25
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


def test_failed_fdopen_closes_the_descriptor_and_removes_the_temp(tmp_path, monkeypatch):
    target = tmp_path / "state.json"
    target.write_bytes(b"previous")
    opened = []
    real_mkstemp = product.tempfile.mkstemp

    def track(*args, **kwargs):
        result = real_mkstemp(*args, **kwargs)
        opened.append(result[0])
        return result

    def fail(*_args, **_kwargs):
        raise OSError("no stream")

    monkeypatch.setattr(product.tempfile, "mkstemp", track)
    monkeypatch.setattr(product.os, "fdopen", fail)
    with pytest.raises(OSError, match="no stream"):
        product.atomic_write_bytes(target, b"next")
    monkeypatch.undo()
    with pytest.raises(OSError):
        os.fstat(opened[0])  # already closed
    assert target.read_bytes() == b"previous"
    assert _leftovers(tmp_path) == []


def test_directory_cleanup_failure_does_not_report_a_committed_write_as_failed(tmp_path, monkeypatch):
    target = tmp_path / "state.json"
    target.write_bytes(b"previous")
    spare = os.open(tmp_path / "spare", os.O_CREAT | os.O_RDWR)
    real_open, real_close, real_fsync = os.open, os.close, os.fsync
    closes = []

    def open_directory(path, *args, **kwargs):
        return spare if Path(path) == tmp_path else real_open(path, *args, **kwargs)

    def close(descriptor):
        closes.append(descriptor)
        real_close(descriptor)
        if descriptor == spare:
            raise OSError("close failed")

    monkeypatch.setattr(product.os, "open", open_directory)
    monkeypatch.setattr(product.os, "close", close)
    monkeypatch.setattr(product.os, "fsync", lambda fd: None if fd == spare else real_fsync(fd))
    product.atomic_write_bytes(target, b"next")
    monkeypatch.undo()
    assert spare in closes
    assert target.read_bytes() == b"next"


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


def test_hook_installer_refuses_a_path_it_cannot_inspect(tmp_path, monkeypatch):
    from token_tracker import claude_hook_install

    settings = tmp_path / "settings.json"
    settings.write_text("{}", encoding="utf-8")
    claude_hook_install._snapshot_file(settings)
    real = Path.lstat

    def deny(path, *args, **kwargs):
        if path == settings:
            raise PermissionError("denied")
        return real(path, *args, **kwargs)

    monkeypatch.setattr(Path, "lstat", deny)
    with pytest.raises(claude_hook_install.HookInstallError):
        claude_hook_install._validate_path_chain(settings)
    with pytest.raises(claude_hook_install.HookInstallError):
        claude_hook_install._snapshot_file(settings)


@pytest.mark.parametrize("info", [
    SimpleNamespace(st_mode=stat.S_IFREG | 0o644, st_file_attributes=0x400),
    SimpleNamespace(st_mode=stat.S_IFLNK | 0o777, st_file_attributes=0),
])
def test_hook_installer_refuses_redirected_settings(tmp_path, monkeypatch, info):
    from token_tracker import claude_hook_install

    settings = tmp_path / "settings.json"
    settings.write_text("{}", encoding="utf-8")
    real = Path.lstat
    monkeypatch.setattr(Path, "lstat",
                        lambda path, *a, **k: info if path == settings else real(path, *a, **k))
    with pytest.raises(claude_hook_install.HookInstallError):
        claude_hook_install._validate_path_chain(settings)


def test_one_unreadable_timestamp_does_not_drop_other_providers():
    from token_tracker import insights

    now = datetime(2026, 11, 24, 12, 0, tzinfo=timezone.utc)
    rows = [
        {"connected": True, "limitsUpdatedAt": 10 ** 4500},
        {"connected": True, "limitsUpdatedAt": (now - timedelta(minutes=1)).isoformat()},
    ]
    result = insights._fresh_limit_observations(rows, now=now)
    assert [provider for provider, _ in result] == [rows[1]]
