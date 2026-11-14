"""POSIX behavior of the macOS instance-lock implementation."""
import os
import select
import stat
import subprocess
import sys

import pytest

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="flock is POSIX-only")

from token_tracker import product


@pytest.fixture
def reset_instance_lock(monkeypatch):
    """Keep the module-global descriptor scoped to each in-process test."""
    monkeypatch.setattr(product, "_MACOS_INSTANCE_LOCK", None)
    yield
    current = product._MACOS_INSTANCE_LOCK
    if current is not None:
        os.close(current)
    monkeypatch.setattr(product, "_MACOS_INSTANCE_LOCK", None)


# Timeouts are failure ceilings only: interpreter start on a cold CI runner can take seconds.
def _run_lock_attempt(path):
    code = (
        "import sys; from pathlib import Path; from token_tracker.product import "
        "acquire_macos_instance_lock; "
        "print('ACQUIRED' if acquire_macos_instance_lock(Path(sys.argv[1])) else 'DENIED', flush=True)"
    )
    return subprocess.run(
        [sys.executable, "-c", code, str(path)], capture_output=True, text=True, check=True, timeout=10
    ).stdout.strip()


def test_lock_is_exclusive_across_processes_and_released_on_exit(tmp_path):
    lock_path = tmp_path / "instance.lock"
    holder_code = (
        "import sys; from pathlib import Path; from token_tracker.product import "
        "acquire_macos_instance_lock; "
        "assert acquire_macos_instance_lock(Path(sys.argv[1])); "
        "print('READY', flush=True); sys.stdin.readline()"
    )
    holder = subprocess.Popen(
        [sys.executable, "-c", holder_code, str(lock_path)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        ready, _, _ = select.select([holder.stdout], [], [], 10)
        assert ready, "lock holder did not become ready"
        assert holder.stdout.readline().strip() == "READY"
        assert _run_lock_attempt(lock_path) == "DENIED"
    finally:
        if holder.poll() is None:
            holder.stdin.write("exit\n")
            holder.stdin.flush()
        holder.communicate(timeout=10)

    assert _run_lock_attempt(lock_path) == "ACQUIRED"


def test_lock_file_has_private_permissions(tmp_path, reset_instance_lock):
    path = tmp_path / "instance.lock"

    assert product.acquire_macos_instance_lock(path)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_symlink_lock_path_is_refused(tmp_path, reset_instance_lock):
    target = tmp_path / "target.lock"
    target.write_text("untouched")
    link = tmp_path / "instance.lock"
    link.symlink_to(target)

    try:
        acquired = product.acquire_macos_instance_lock(link)
    except OSError:
        acquired = False

    assert not acquired
    assert product._MACOS_INSTANCE_LOCK is None
    assert link.is_symlink()
    assert target.read_text() == "untouched"
