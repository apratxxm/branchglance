import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "darwin", reason="macOS bg-codex launcher")

from token_tracker import bg_codex

THREAD = "0f8fad5b-d9cb-469f-a165-70867728950e"
FAKE_CODEX = r'''
#include <stdio.h>
#include <unistd.h>
int main(int argc, char **argv) {
    printf("{\"type\":\"thread.started\",\"thread_id\":\"%s\"}\n", "0f8fad5b-d9cb-469f-a165-70867728950e");
    printf("{\"type\":\"turn.completed\"}\n");
    fflush(stdout);
    usleep(300000);
    return argc > 4 ? 7 : 0;
}
'''


@pytest.fixture
def fake_codex(tmp_path):
    if shutil.which("cc") is None:
        pytest.skip("no C compiler")
    source = tmp_path / "codex.c"
    source.write_text(FAKE_CODEX)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    subprocess.run(["cc", "-o", str(bin_dir / "codex"), str(source)], check=True)
    return bin_dir


def test_resolve_skips_working_directory_and_the_launcher_itself(tmp_path, fake_codex):
    work = tmp_path / "work"
    planted = work / "bin"
    planted.mkdir(parents=True)
    (planted / "codex").write_text("#!/bin/sh\n")
    (planted / "codex").chmod(0o755)
    launcher = tmp_path / "launcher" / "codex"
    launcher.parent.mkdir()
    launcher.write_text("#!/bin/sh\n")
    launcher.chmod(0o755)
    path = os.pathsep.join(["relative/bin", str(planted), str(launcher.parent), str(fake_codex)])
    assert bg_codex.resolve(path, str(work), str(launcher)) == str(fake_codex / "codex")
    assert bg_codex.resolve(str(planted), str(work)) is None


def test_argument_parsing():
    assert bg_codex.resume_thread(["exec", "resume", THREAD.upper(), "--json"]) == THREAD
    assert bg_codex.resume_thread(["exec", "resume", "not-a-uuid"]) is None
    assert bg_codex.json_exec(["exec", "--json", "task"])
    assert not bg_codex.json_exec(["--json", "exec"])


def test_json_exec_forwards_output_and_links_the_started_thread(monkeypatch, capfdbinary, fake_codex):
    linked = []
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")
    monkeypatch.setenv("CODEX_THREAD_ID", "bbbbbbbb-cccc-dddd-eeee-ffffffffffff")
    monkeypatch.setenv("UNRELATED_SECRET", "must-not-be-captured")
    monkeypatch.setattr(bg_codex, "_link", lambda child, thread, environment:
                        linked.append((child.pid, thread, environment)))
    code = bg_codex.run(["exec", "--json", "task"], str(fake_codex / "codex"))
    out = capfdbinary.readouterr().out
    assert code == 0
    assert out == (b'{"type":"thread.started","thread_id":"' + THREAD.encode() + b'"}\n'
                   b'{"type":"turn.completed"}\n')
    assert [thread for _pid, thread, _environment in linked] == [THREAD]
    captured = linked[0][2]
    assert captured == {
        "CLAUDE_CODE_SESSION_ID": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
        "CODEX_THREAD_ID": "bbbbbbbb-cccc-dddd-eeee-ffffffffffff",
    }
    assert "UNRELATED_SECRET" not in captured


def test_exit_code_passes_through_and_resume_links_once(monkeypatch, fake_codex):
    linked = []
    monkeypatch.setattr(bg_codex, "_link", lambda child, thread, environment: linked.append(thread))
    assert bg_codex.run(["exec", "resume", THREAD, "--json"], str(fake_codex / "codex")) == 7
    assert linked == [THREAD]


def test_missing_codex_fails_clearly(capsys):
    assert bg_codex.run(["exec"], None) == 1
    assert "Codex executable not found" in capsys.readouterr().err


def test_codex_pid_finds_the_binary_under_a_wrapper(tmp_path, fake_codex):
    wrapper = tmp_path / "wrapper.sh"
    wrapper.write_text(f'#!/bin/sh\n"{fake_codex / "codex"}" a b c >/dev/null\n')
    wrapper.chmod(wrapper.stat().st_mode | stat.S_IXUSR)
    child = subprocess.Popen([str(wrapper)])
    try:
        pid = bg_codex._codex_pid(child.pid, __import__("time").monotonic() + 2)
        assert pid is not None and pid != child.pid
    finally:
        child.wait()


def test_link_writes_a_receipt_for_the_real_codex_process(monkeypatch, fake_codex):
    from token_tracker import agent_links
    calls = []
    monkeypatch.setattr(agent_links, "write_receipt",
                        lambda thread, pid, expected_created=None, environ=None:
                        calls.append((thread, pid, expected_created, environ)))
    child = subprocess.Popen([str(fake_codex / "codex")], stdout=subprocess.DEVNULL)
    try:
        bg_codex._link(child, THREAD)
    finally:
        child.wait()
    assert calls and calls[0][0] == THREAD and calls[0][1] == child.pid and calls[0][2] > 0
    assert calls[0][3] is not None
