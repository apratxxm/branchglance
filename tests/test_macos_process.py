"""Real process facts exposed by the macOS libproc helpers."""
import os
import subprocess
import sys

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "darwin", reason="requires macOS libproc")

from token_tracker import macos_process


def test_process_table_and_facts_describe_the_current_process():
    pid = os.getpid()
    table = macos_process.process_table()
    assert pid in table
    parent_pid, executable = table[pid]
    assert parent_pid == os.getppid()
    assert executable

    state, started = macos_process.probe_process(pid)
    assert state == "alive"
    assert isinstance(started, int) and started > 0
    assert macos_process.process_uid(pid) == os.getuid()


def test_command_line_reads_this_pytest_process():
    command = macos_process.command_line(os.getpid())

    # CI runs `python -m pytest -q`, so the argv names pytest but not this file.
    assert command is not None
    assert "pytest" in command


def test_dead_pid_returns_empty_process_facts():
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    dead_pid = child.pid
    child.wait(timeout=10)

    table = macos_process.process_table()
    assert dead_pid not in table
    assert macos_process.probe_process(dead_pid) == ("gone", None)
    assert macos_process.process_uid(dead_pid) is None
    assert macos_process.command_line(dead_pid) is None
