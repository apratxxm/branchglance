import json
import subprocess
import sys

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "darwin", reason="macOS hook command")

from token_tracker import claude_hook_install as hooks


def test_source_command_runs_this_python_on_the_hook_script():
    command = hooks.default_command()
    assert command.startswith(hooks._bash_quote(sys.executable))
    assert "claude_hook.py" in command and command.endswith(hooks.MARKER)


def test_round_trip_keeps_user_hooks_and_settings(tmp_path):
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({"theme": "dark", "hooks": {"Stop": [
        {"hooks": [{"type": "command", "command": "echo mine"}]}]}}))
    result = hooks.install(settings, data_directory=tmp_path / "data")
    data = json.loads(settings.read_text())
    assert result["ok"] and set(data["hooks"]) == set(hooks.EVENTS)
    command = data["hooks"]["Stop"][-1]["hooks"][0]["command"]
    assert subprocess.run(["bash", "-c", command], input=b"{}", timeout=10).returncode == 0
    assert hooks.uninstall(settings, data_directory=tmp_path / "data")["ok"]
    data = json.loads(settings.read_text())
    assert data["theme"] == "dark"
    assert data["hooks"] == {"Stop": [{"hooks": [{"type": "command", "command": "echo mine"}]}]}


def test_hook_records_its_claude_ancestor(monkeypatch):
    from token_tracker import claude_hook
    table = {10: (1, "claude"), 20: (10, "zsh"), 30: (20, "python")}
    monkeypatch.setattr(claude_hook, "_process_table", lambda: table)
    monkeypatch.setattr(claude_hook.os, "getpid", lambda: 30)
    monkeypatch.setattr(claude_hook, "process_created", lambda pid: 1234 if pid == 10 else None)
    assert claude_hook._claude_process() == (10, 1234)
