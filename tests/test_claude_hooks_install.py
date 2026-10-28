"""Hook installer: temp directories only, never the real Claude settings."""

import json
import os
import shlex
import subprocess
import sys
import time
from pathlib import Path

import pytest

from token_tracker import claude_hook, claude_hook_install as installer

CMD = '"C:/Program Files/Branchglance/branchglance.exe" --claude-hook --branchglance-hook'
REAL_FIND_GIT_BASH = installer.find_git_bash  # captured before the autouse stub
USER_GROUP = {"matcher": "Bash", "hooks": [{"type": "command", "command": "echo mine", "timeout": 5}]}


def test_config_dir_precedence_and_default(monkeypatch, tmp_path):
    legacy = tmp_path / "legacy"
    configured = tmp_path / "configured"
    monkeypatch.setenv("CLAUDE_HOME", str(legacy))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(configured))
    assert installer.default_settings_path() == legacy / "settings.json"
    monkeypatch.delenv("CLAUDE_HOME")
    assert installer.default_settings_path() == configured / "settings.json"
    monkeypatch.setenv("CLAUDE_HOME", "")
    assert installer.claude_config_dir(tmp_path) == configured
    monkeypatch.delenv("CLAUDE_CONFIG_DIR")
    assert installer.claude_config_dir(tmp_path) == tmp_path / ".claude"


@pytest.fixture()
def env(tmp_path):
    settings = tmp_path / "claude" / "settings.json"
    settings.parent.mkdir()
    return settings, tmp_path / "data"


def load(path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


# Executables the tests install with (G8: ownership requires a configured executable).
TEST_EXES = frozenset({"c:/program files/branchglance/branchglance.exe", "c:/apps/branchglance/branchglance.exe",
                       "c:/python 3/python.exe"})


def ours(data, event):
    return [g for g in data["hooks"][event] if installer.is_ours(g, installer.default_executables() | TEST_EXES)]


@pytest.fixture(autouse=True)
def git_bash_present(monkeypatch):
    """Deterministic default; the G2 tests override it."""
    monkeypatch.setattr(installer, "find_git_bash", lambda: "C:/Program Files/Git/bin/bash.exe")


def test_install_into_missing_file(env):
    settings, data_dir = env
    result = installer.install(settings, CMD, data_dir, now=1_800_000_000)
    assert result["changed"] and result["backup"] is None
    data = load(settings)
    assert set(data["hooks"]) == set(installer.EVENTS)
    group = ours(data, "PreToolUse")[0]
    assert group == {"hooks": [{"type": "command", "command": CMD, "shell": "bash", "async": True,
                                "timeout": installer.HOOK_TIMEOUT_SECONDS}]}
    assert 0 < installer.HOOK_TIMEOUT_SECONDS <= 30
    assert (data_dir / claude_hook.CONSENT_NAME).is_file()
    assert installer.status(settings, data_dir)["state"] == "installed"
    # Idempotent: a second install changes nothing and makes no backup.
    again = installer.install(settings, CMD, data_dir)
    assert again["changed"] is False and again["backup"] is None
    assert len(data["hooks"]["Stop"]) == 1 and len(load(settings)["hooks"]["Stop"]) == 1


def test_user_hooks_preserved_semantically_and_backup_written(env):
    settings, data_dir = env
    original = {"model": "opus", "permissions": {"allow": ["Bash(ls)"]}, "disableAllHooks": False,
                "hooks": {"PreToolUse": [USER_GROUP], "Stop": [{"hooks": [{"type": "command", "command": "notify"}]}],
                          "Custom": [USER_GROUP]}, "env": {"A": "ü"}}
    settings.write_text(json.dumps(original, indent=4), encoding="utf-8")
    raw = settings.read_bytes()
    result = installer.install(settings, CMD, data_dir, now=1_800_000_000)
    data = load(settings)
    assert result["backup"] and Path(result["backup"]).read_bytes() == raw
    for key in ("model", "permissions", "env", "disableAllHooks"):
        assert data[key] == original[key]
    assert data["hooks"]["Custom"] == [USER_GROUP]
    assert data["hooks"]["PreToolUse"][0] == USER_GROUP
    assert data["hooks"]["Stop"][0] == original["hooks"]["Stop"][0]
    assert len(data["hooks"]["PreToolUse"]) == 2


def test_utf8_bom_is_handled_and_preserved(env):
    settings, data_dir = env
    settings.write_bytes(b"\xef\xbb\xbf" + json.dumps({"hooks": {"Stop": [USER_GROUP]}}).encode())
    installer.install(settings, CMD, data_dir)
    assert settings.read_bytes().startswith(b"\xef\xbb\xbf")
    assert load(settings)["hooks"]["Stop"][0] == USER_GROUP
    assert installer.status(settings, data_dir)["state"] == "installed"
    installer.uninstall(settings, data_dir)
    assert load(settings) == {"hooks": {"Stop": [USER_GROUP]}}


def test_empty_file_and_status_variants(env):
    settings, data_dir = env
    assert installer.status(settings, data_dir)["state"] == "missing"
    settings.write_text("  \n", encoding="utf-8")
    installer.install(settings, CMD, data_dir)
    assert installer.status(settings, data_dir)["state"] == "installed"
    (data_dir / claude_hook.CONSENT_NAME).unlink()
    assert installer.status(settings, data_dir, command=CMD)["state"] == "partial"
    data = load(settings)
    del data["hooks"]["Notification"]
    data["disableAllHooks"] = True
    settings.write_text(json.dumps(data), encoding="utf-8")
    blocked = installer.status(settings, data_dir)
    assert blocked["state"] == "blocked" and "disableAllHooks" in blocked["reason"]
    assert "Notification" in blocked["missing"]
    assert blocked["managedPolicy"] == "not detectable"


@pytest.mark.parametrize("content", ["{not json", "[1, 2]", '{"hooks": []}', '{"hooks": {"Stop": {}}}',
                                      '// comment\n{"hooks": {}}'])
def test_unparseable_or_malformed_refused_untouched(env, content):
    settings, data_dir = env
    settings.write_text(content, encoding="utf-8")
    before = settings.read_bytes()
    with pytest.raises(installer.HookInstallError):
        installer.install(settings, CMD, data_dir)
    assert settings.read_bytes() == before
    assert not (data_dir / claude_hook.CONSENT_NAME).exists()
    assert installer.status(settings, data_dir)["state"] == "blocked"
    assert list(settings.parent.iterdir()) == [settings]


def test_symlink_refused(env):
    settings, data_dir = env
    target = settings.parent / "real.json"
    target.write_text("{}", encoding="utf-8")
    try:
        os.symlink(target, settings)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable")
    with pytest.raises(installer.HookInstallError, match="symlink"):
        installer.install(settings, CMD, data_dir)
    assert target.read_text() == "{}"


def test_parent_symlink_chain_refused(env):
    settings, data_dir = env
    target_dir = settings.parent / "real-dir"
    target_dir.mkdir()
    target = target_dir / "settings.json"
    target.write_text("{}", encoding="utf-8")
    redirect = settings.parent / "redirect"
    try:
        os.symlink(target_dir, redirect, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("directory symlinks unavailable")
    with pytest.raises(installer.HookInstallError, match="path contains"):
        installer.install(redirect / "settings.json", CMD, data_dir)
    assert target.read_text() == "{}"


@pytest.mark.skipif(sys.platform != "win32", reason="junction test requires Windows")
def test_junction_chain_refused(env):
    settings, data_dir = env
    target_dir = settings.parent / "junction-target"
    target_dir.mkdir()
    target = target_dir / "settings.json"
    target.write_text("{}", encoding="utf-8")
    junction = settings.parent / "junction-link"
    made = subprocess.run(["cmd.exe", "/c", "mklink", "/J", str(junction), str(target_dir)],
                          capture_output=True, text=True)
    if made.returncode:
        pytest.skip("junction creation unavailable")
    try:
        with pytest.raises(installer.HookInstallError, match="path contains"):
            installer.install(junction / "settings.json", CMD, data_dir)
        assert target.read_text() == "{}"
    finally:
        subprocess.run(["cmd.exe", "/c", "rmdir", str(junction)], capture_output=True, text=True)


def test_default_command_quotes_shell_metacharacters_as_literal_arguments():
    executable = "C:/Program Files/Branchglance/$(touch pwned) `x` 'runner.exe"
    script = "A:/source tree/$(touch nope)/it' s hook.py"
    command = installer.default_command(executable, script)
    assert shlex.split(command) == [executable, script, installer.MARKER]
    assert command.count("'") > 4


def test_concurrent_modification_retries_and_preserves_newer_settings(env, monkeypatch):
    settings, data_dir = env
    settings.write_text(json.dumps({"hooks": {}}), encoding="utf-8")
    real_create = installer._unique_adjacent
    injected = False

    def racing_create(path, tag, payload):
        nonlocal injected
        created = real_create(path, tag, payload)
        if tag.startswith("backup-") and not injected:
            injected = True
            # Model a concurrent Claude/user update after backup creation.
            path.write_text(json.dumps({"hooks": {}, "model": "changed-meanwhile"}), encoding="utf-8")
        return created

    monkeypatch.setattr(installer, "_unique_adjacent", racing_create)
    result = installer.install(settings, CMD, data_dir)
    assert result["ok"] and result["changed"]
    assert load(settings)["model"] == "changed-meanwhile"
    assert set(load(settings)["hooks"]) == set(installer.EVENTS)
    assert (data_dir / claude_hook.CONSENT_NAME).exists()
    remaining = list(settings.parent.iterdir())
    assert len(remaining) == 3 and settings in remaining
    backups = [p for p in remaining if p != settings]
    assert any(backup.read_text() == '{"hooks": {}}' for backup in backups)
    assert any('"model": "changed-meanwhile"' in backup.read_text() for backup in backups)


def test_uninstall_removes_only_marked_groups(env):
    settings, data_dir = env
    original = {"hooks": {"PreToolUse": [USER_GROUP], "Stop": [{"hooks": [{"type": "command", "command": "notify"}]}]},
                "model": "x"}
    settings.write_text(json.dumps(original), encoding="utf-8")
    installer.install(settings, CMD, data_dir)
    result = installer.uninstall(settings, data_dir)
    assert result["changed"] and result["backup"]
    assert load(settings) == original
    assert not (data_dir / claude_hook.CONSENT_NAME).exists()
    assert installer.status(settings, data_dir)["state"] == "missing"
    assert installer.uninstall(settings, data_dir)["changed"] is False
    assert "not atomic CAS" in result["writeSafety"]


def test_uninstall_leaves_user_edited_and_mixed_groups(env):
    settings, data_dir = env
    installer.install(settings, CMD, data_dir)
    data = load(settings)
    # The user added their own hook into our group: it is now theirs.
    mixed = data["hooks"]["Stop"][0]
    mixed["hooks"].append({"type": "command", "command": "echo theirs"})
    # And an unrelated group that merely mentions the marker text in a non-command field.
    data["hooks"]["Notification"].append({"hooks": [{"type": "command", "command": "echo hi", "note": installer.MARKER}]})
    settings.write_text(json.dumps(data), encoding="utf-8")
    result = installer.uninstall(settings, data_dir)
    after = load(settings)
    assert after["hooks"]["Stop"] == [mixed]
    assert result["mixedGroupsLeft"] == ["Stop"]
    assert after["hooks"]["Notification"][0]["hooks"][0]["command"] == "echo hi"
    assert "PreToolUse" not in after["hooks"]


def test_marker_substring_is_not_owned(env):
    settings, data_dir = env
    unrelated = {"hooks": [{"type": "command", "command": "echo --branchglance-hook-disabled"}]}
    settings.write_text(json.dumps({"hooks": {"Stop": [unrelated]}}), encoding="utf-8")
    result = installer.uninstall(settings, data_dir)
    assert result["changed"] is False
    assert load(settings)["hooks"]["Stop"] == [unrelated]


def test_changed_command_replaces_only_our_group(env):
    settings, data_dir = env
    settings.write_text(json.dumps({"hooks": {"Stop": [USER_GROUP]}}), encoding="utf-8")
    installer.install(settings, CMD, data_dir)
    new = CMD.replace("Program Files", "Apps")
    installer.install(settings, new, data_dir)
    groups = load(settings)["hooks"]["Stop"]
    assert groups[0] == USER_GROUP and len(groups) == 2 and new in groups[1]["hooks"][0]["command"]


def test_command_without_marker_refused(env):
    settings, data_dir = env
    with pytest.raises(installer.HookInstallError):
        installer.install(settings, '"x.exe" --hook', data_dir)
    assert not settings.exists()


def test_default_command_is_stable_and_marked():
    command = installer.default_command()
    assert installer.MARKER in command and "\\" not in command
    assert "claude_hook.py" in command


# ------------------------------------------------- review fixes F2-F6

def test_hostile_paths_survive_real_bash_as_literal_arguments(tmp_path):
    """F4: the generated command, run by bash, passes each path through verbatim."""
    bash = REAL_FIND_GIT_BASH()
    if sys.platform == "win32":
        assert bash is not None, "Git Bash is required (C:/Program Files/Git/bin)"
    elif not bash:
        pytest.skip("bash unavailable")
    hostile = ["C:/Program Files/x/$(touch pwned-a)/run.exe", "A:/src `touch pwned-b`/hook.py",
               "it's \"quoted\" $HOME ${PATH} ; touch pwned-c | & > pwned-d", "new\nline $((1+1))"]
    script = tmp_path / "args.py"
    script.write_text("import sys\nfor a in sys.argv[1:]:\n    print(repr(a))\n", encoding="utf-8")
    python = Path(sys.executable).as_posix()
    for value in hostile:
        command = installer.default_command(python, script.as_posix()) + " " + installer._bash_quote(value)
        done = subprocess.run([bash, "-c", command], cwd=tmp_path, capture_output=True, text=True, timeout=60)
        assert done.returncode == 0, done.stderr
        assert done.stdout.splitlines() == [repr(installer.MARKER), repr(value)]
    assert not [p for p in tmp_path.iterdir() if p.name.startswith("pwned")]


def test_standalone_marker_alone_is_not_ownership(env):
    """F5: the marker must be a standalone argument of a Branchglance invocation."""
    settings, data_dir = env
    lookalikes = [
        {"hooks": [{"type": "command", "command": "echo --branchglance-hook"}]},
        {"hooks": [{"type": "command", "command": "rm -rf ~/x --branchglance-hook"}]},
        {"hooks": [{"type": "command", "command": "'notify.exe' '--branchglance-hook=1'"}]},
        {"hooks": [{"type": "command", "command": "python -m other.module --branchglance-hook"}]},
        {"hooks": [{"type": "command", "command": "python other_hook.py --branchglance-hook"}]},
    ]
    for group in lookalikes:
        assert installer.is_ours(group) is False, group
    settings.write_text(json.dumps({"hooks": {"Stop": lookalikes}}), encoding="utf-8")
    assert installer.uninstall(settings, data_dir)["changed"] is False
    assert load(settings)["hooks"]["Stop"] == lookalikes
    with pytest.raises(installer.HookInstallError):
        installer.install(settings, "echo --branchglance-hook", data_dir)


def test_default_dev_command_installs_and_uninstalls(env):
    settings, data_dir = env
    command = installer.default_command("C:/Python 3/python.exe", "A:/it's here/token_tracker/claude_hook.py")
    installer.install(settings, command, data_dir)
    assert all(ours(load(settings), event) for event in installer.EVENTS)
    assert installer.status(settings, data_dir)["state"] == "installed"
    assert installer.uninstall(settings, data_dir)["changed"] is True
    assert "hooks" not in load(settings)


def test_temp_and_backup_names_are_unique_and_never_reuse_existing_paths(env, monkeypatch):
    """F3: exclusive creation skips a planted file or link at a predictable name."""
    settings, data_dir = env
    settings.write_text("{}", encoding="utf-8")
    victim = settings.parent / "victim.txt"
    victim.write_text("untouched", encoding="utf-8")
    tokens = iter(["planted", "planted", "fresh1", "planted", "fresh2"])
    monkeypatch.setattr(installer.secrets, "token_hex", lambda n: next(tokens))
    stamp = "20261022-120000"
    monkeypatch.setattr(installer.time, "strftime", lambda fmt, t=None: stamp)
    planted_backup = settings.with_name(f"settings.json.branchglance-backup-{stamp}-planted")
    planted_backup.write_text("planted backup", encoding="utf-8")
    planted_tmp = settings.with_name("settings.json.branchglance-tmp-planted")
    try:
        os.symlink(victim, planted_tmp)
    except (OSError, NotImplementedError):
        planted_tmp.write_text("planted tmp", encoding="utf-8")
    result = installer.install(settings, CMD, data_dir, now=1_800_000_000)
    assert result["backup"].endswith("-fresh1")
    assert planted_backup.read_text() == "planted backup"
    assert victim.read_text() == "untouched"
    assert os.path.lexists(planted_tmp)
    assert ours(load(settings), "Stop")
    assert not list(settings.parent.glob("*-fresh2"))  # temp file consumed by the replace


def test_exclusive_create_refuses_existing_path(tmp_path):
    existing = tmp_path / "taken"
    existing.write_text("keep", encoding="utf-8")
    with pytest.raises(FileExistsError):
        installer._exclusive_file(existing, b"new")
    assert existing.read_text() == "keep"


def test_path_chain_is_checked_from_user_profile(env, monkeypatch, tmp_path):
    """F3: the chain root is %USERPROFILE% for settings under it; redirects below it are refused."""
    home = tmp_path / "profile"
    (home / ".claude").mkdir(parents=True)
    monkeypatch.setattr(installer.Path, "home", classmethod(lambda cls: home))
    assert installer._chain_root(home / ".claude" / "settings.json") == Path(os.path.abspath(home))
    elsewhere = Path(os.path.abspath(tmp_path)).anchor
    assert installer._chain_root(Path(elsewhere) / "other" / "settings.json") == Path(elsewhere)
    real = tmp_path / "real-claude"
    real.mkdir()
    (real / "settings.json").write_text("{}", encoding="utf-8")
    link = home / ".claude-link"
    try:
        os.symlink(real, link, target_is_directory=True)
    except (OSError, NotImplementedError):
        made = subprocess.run(["cmd.exe", "/c", "mklink", "/J", str(link), str(real)], capture_output=True, text=True) \
            if sys.platform == "win32" else None
        if made is None or made.returncode:
            pytest.skip("no symlink or junction available")
    try:
        with pytest.raises(installer.HookInstallError, match="path contains"):
            installer.install(link / "settings.json", CMD, tmp_path / "data")
        assert (real / "settings.json").read_text() == "{}"
    finally:
        if sys.platform == "win32" and link.exists() and not link.is_symlink():
            subprocess.run(["cmd.exe", "/c", "rmdir", str(link)], capture_output=True, text=True)


def test_settings_replaced_by_link_during_edit_aborts(env, monkeypatch):
    """F2/F3: the recheck right before replace refuses a file swapped for a link or deleted."""
    settings, data_dir = env
    settings.write_text("{}", encoding="utf-8")
    real_create = installer._unique_adjacent

    def deleting_create(path, tag, payload):
        created = real_create(path, tag, payload)
        if tag == "tmp":
            path.unlink()
        return created

    monkeypatch.setattr(installer, "_unique_adjacent", deleting_create)
    with pytest.raises(installer.HookInstallError, match="changed while editing"):
        installer.install(settings, CMD, data_dir)
    assert not settings.exists()  # nothing recreated; the backup holds the original
    backups = list(settings.parent.glob("settings.json.branchglance-backup-*"))
    assert len(backups) == 1 and backups[0].read_text() == "{}"
    assert not list(settings.parent.glob("*branchglance-tmp*"))


def test_status_reports_best_effort_write_safety(env):
    settings, data_dir = env
    report = installer.status(settings, data_dir)
    assert "best-effort" in report["writeSafety"].lower() and "not atomic" in report["writeSafety"]
    assert "atomic" not in installer.__doc__.replace("not an atomic", "")


# ------------------------------------------------- round 2: G2, G4, G5, G7, G8

def test_real_git_bash_is_found_and_runs_the_quoted_command(tmp_path):
    """G2/F4: detection finds the real Git Bash, which runs the command with literal arguments."""
    bash = REAL_FIND_GIT_BASH()
    if sys.platform == "win32":
        assert bash is not None, "Git Bash must be installed for this test (C:/Program Files/Git/bin)"
        assert "system32" not in bash.lower() and "windowsapps" not in bash.lower()
    elif bash is None:
        pytest.skip("bash unavailable")
    script = tmp_path / "args.py"
    script.write_text("import sys\nfor a in sys.argv[1:]:\n    print(repr(a))\n", encoding="utf-8")
    value = "C:/x/$(touch pwned) `touch pwned2` it's"
    command = installer.default_command(Path(sys.executable).as_posix(), script.as_posix()) + " " + installer._bash_quote(value)
    done = subprocess.run([bash, "-c", command], cwd=tmp_path, capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr
    assert done.stdout.splitlines() == [repr(installer.MARKER), repr(value)]
    assert not [p for p in tmp_path.iterdir() if p.name.startswith("pwned")]


def test_every_branchglance_entry_pins_bash(env):
    settings, data_dir = env
    installer.install(settings, CMD, data_dir)
    data = load(settings)
    for event in installer.EVENTS:
        for hook in ours(data, event)[0]["hooks"]:
            assert hook["shell"] == "bash"


def test_missing_git_bash_blocks_install_and_status(env, monkeypatch):
    """G2: without Git Bash the PowerShell fallback would run a bash-quoted command; refuse."""
    settings, data_dir = env
    settings.write_text('{"model": "x"}', encoding="utf-8")
    monkeypatch.setattr(installer, "find_git_bash", lambda: None)
    with pytest.raises(installer.HookInstallError, match="Git Bash not found"):
        installer.install(settings, CMD, data_dir)
    assert settings.read_text(encoding="utf-8") == '{"model": "x"}'
    assert not (data_dir / claude_hook.CONSENT_NAME).exists()
    report = installer.status(settings, data_dir)
    assert report["state"] == "blocked" and report["reason"] == "blocked: Git Bash not found"
    assert report["gitBash"] is False


def test_wsl_bash_is_not_git_bash(monkeypatch, tmp_path):
    if sys.platform != "win32":
        pytest.skip("Windows detection rules")
    wsl = tmp_path / "Windows" / "System32"
    wsl.mkdir(parents=True)
    (wsl / "bash.exe").write_text("", encoding="utf-8")
    monkeypatch.setattr(installer.shutil, "which", lambda name: str(wsl / "bash.exe") if name == "bash" else None)
    for variable in ("CLAUDE_CODE_GIT_BASH_PATH", "ProgramFiles", "ProgramW6432", "ProgramFiles(x86)", "LOCALAPPDATA"):
        monkeypatch.delenv(variable, raising=False)
    assert REAL_FIND_GIT_BASH() is None
    git_bash = tmp_path / "Git" / "bin" / "bash.exe"
    git_bash.parent.mkdir(parents=True)
    git_bash.write_text("", encoding="utf-8")
    monkeypatch.setenv("CLAUDE_CODE_GIT_BASH_PATH", str(git_bash))
    assert REAL_FIND_GIT_BASH() == str(git_bash)


def test_uninstall_removes_consent_first_even_when_settings_edit_fails(env):
    """G4: consent goes first and unconditionally; hooks left behind are reported."""
    settings, data_dir = env
    installer.install(settings, CMD, data_dir)
    assert (data_dir / claude_hook.CONSENT_NAME).exists()
    settings.write_text(settings.read_text(encoding="utf-8") + "{garbage", encoding="utf-8")
    result = installer.uninstall(settings, data_dir)
    assert result["ok"] is False and result["consentRemoved"] is True
    assert not (data_dir / claude_hook.CONSENT_NAME).exists()
    assert result["hooksLeft"] == "unknown" and "parseable" in result["error"]


def test_uninstall_reports_hooks_left_when_write_fails(env, monkeypatch):
    settings, data_dir = env
    installer.install(settings, CMD, data_dir)
    before = settings.read_bytes()

    def refuse(*_a, **_k):
        raise installer.HookInstallError("could not write the settings update (PermissionError)")

    monkeypatch.setattr(installer, "_write_transform_checked", refuse)
    result = installer.uninstall(settings, data_dir)
    assert result["ok"] is False and result["consentRemoved"] is True
    assert result["hooksLeft"] == sorted(installer.EVENTS)
    assert settings.read_bytes() == before
    assert not (data_dir / claude_hook.CONSENT_NAME).exists()


def test_uninstall_retries_when_settings_change_before_replace_and_preserves_newer_content(env, monkeypatch):
    settings, data_dir = env
    installer.install(settings, CMD, data_dir)
    original_unique_adjacent = installer._unique_adjacent
    injected = False

    def race_on_temp(path, label, payload):
        nonlocal injected
        result = original_unique_adjacent(path, label, payload)
        if label == "tmp" and not injected:
            injected = True
            latest = load(settings)
            latest["concurrentClaudeChange"] = {"value": "keep"}
            settings.write_text(json.dumps(latest, indent=2), encoding="utf-8")
        return result

    monkeypatch.setattr(installer, "_unique_adjacent", race_on_temp)
    result = installer.uninstall(settings, data_dir, now=1_800_000_123)
    assert result["ok"] is True and result["changed"] is True
    final = load(settings)
    assert final["concurrentClaudeChange"] == {"value": "keep"}
    assert "hooks" not in final
    backups = list(settings.parent.glob("settings.json.branchglance-backup-*"))
    assert backups and any(b"concurrentClaudeChange" in backup.read_bytes() for backup in backups)


def test_install_compare_and_swap_retries_and_preserves_formatting(env, monkeypatch):
    settings, data_dir = env
    settings.write_bytes(b'\xef\xbb\xbf{\r\n    "custom": {"value": "keep"}\r\n}\r\n')
    original_unique_adjacent = installer._unique_adjacent
    injected = False

    def race_on_temp(path, label, payload):
        nonlocal injected
        result = original_unique_adjacent(path, label, payload)
        if label == "tmp" and not injected:
            injected = True
            settings.write_bytes(b'\xef\xbb\xbf{\r\n    "custom": {"value": "newer"}\r\n}\r\n')
        return result

    monkeypatch.setattr(installer, "_unique_adjacent", race_on_temp)
    result = installer.install(settings, CMD, data_dir)
    raw = settings.read_bytes()
    assert result["ok"] is True and result["changed"] is True
    assert raw.startswith(b"\xef\xbb\xbf") and b"\r\n" in raw and b"\r\n    \"hooks\":" in raw
    decoded = json.loads(raw.decode("utf-8-sig"))
    assert decoded["custom"] == {"value": "newer"}
    assert all(event in decoded["hooks"] for event in installer.EVENTS)


def test_settings_compare_and_swap_exhaustion_leaves_latest_concurrent_bytes_untouched(env, monkeypatch):
    settings, data_dir = env
    settings.write_text('{"initial": true}', encoding="utf-8")
    original_unique_adjacent = installer._unique_adjacent
    writes = []

    def race_every_attempt(path, label, payload):
        result = original_unique_adjacent(path, label, payload)
        if label == "tmp":
            latest = json.dumps({"foreignEdit": len(writes) + 1}, separators=(",", ":")).encode("utf-8")
            writes.append(latest)
            settings.write_bytes(latest)
        return result

    monkeypatch.setattr(installer, "_unique_adjacent", race_every_attempt)
    with pytest.raises(installer.HookInstallError, match="kept changing.*left untouched"):
        installer.install(settings, CMD, data_dir)
    assert len(writes) == 3
    assert settings.read_bytes() == writes[-1]
    assert "hooks" not in json.loads(settings.read_text(encoding="utf-8"))
    assert not (data_dir / claude_hook.CONSENT_NAME).exists()


def test_frozen_build_refuses_install_until_launcher_supports_hook(env, monkeypatch):
    """G5: no hook commands that nothing can run."""
    settings, data_dir = env
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    # The real launcher now relays --claude-hook; force the pre-capability state.
    monkeypatch.setattr(installer, "_launcher_supports_hook", lambda: False)
    with pytest.raises(installer.HookInstallError, match="launcher cannot run the Claude hook"):
        installer.install(settings, CMD, data_dir)
    assert not settings.exists() and not (data_dir / claude_hook.CONSENT_NAME).exists()
    assert installer.status(settings, data_dir, command=CMD)["state"] == "blocked"
    # Once the packaging lane sets the capability flag, install proceeds.
    monkeypatch.setattr(installer, "_launcher_supports_hook", lambda: True)
    assert installer.install(settings, CMD, data_dir)["ok"] is True


def test_launcher_capability_defaults_to_false_without_product_flag(monkeypatch):
    import types
    import token_tracker
    from token_tracker import product
    assert product.CLAUDE_HOOK_LAUNCHER is True  # the shipped launcher relays --claude-hook

    def stub(module):
        # `from . import product` resolves the package attribute before sys.modules.
        monkeypatch.setitem(sys.modules, "token_tracker.product", module)
        monkeypatch.setattr(token_tracker, "product", module, raising=False)

    stub(types.SimpleNamespace())
    assert installer._launcher_supports_hook() is False
    stub(types.SimpleNamespace(CLAUDE_HOOK_LAUNCHER="yes"))
    assert installer._launcher_supports_hook() is False
    stub(types.SimpleNamespace(CLAUDE_HOOK_LAUNCHER=True))
    assert installer._launcher_supports_hook() is True


def test_os_errors_become_clean_refusals(env, monkeypatch):
    """G7: replace/backup OS errors are HookInstallError, original untouched, no temp left."""
    settings, data_dir = env
    settings.write_text('{"keep": 1}', encoding="utf-8")

    def denied(*_a, **_k):
        raise PermissionError(13, "denied")

    monkeypatch.setattr(installer.os, "replace", denied)
    with pytest.raises(installer.HookInstallError, match="left unchanged") as caught:
        installer.install(settings, CMD, data_dir)
    assert caught.value.__cause__ is None and caught.value.__suppress_context__
    assert settings.read_text(encoding="utf-8") == '{"keep": 1}'
    assert not list(settings.parent.glob("*branchglance-tmp*"))
    monkeypatch.undo()
    monkeypatch.setattr(installer, "find_git_bash", lambda: "bash")
    monkeypatch.setattr(installer, "_exclusive_file", denied)
    with pytest.raises(installer.HookInstallError, match="backup"):
        installer.install(settings, CMD, data_dir)
    assert settings.read_text(encoding="utf-8") == '{"keep": 1}'
    assert installer.main(["bogus"]) == 2


def test_only_last_five_backups_are_kept_and_others_untouched(env):
    settings, data_dir = env
    settings.write_text("{}", encoding="utf-8")
    foreign = [settings.with_name("settings.json.branchglance-backup-notes"),
               settings.with_name("settings.json.bak"),
               settings.with_name("settings.json.branchglance-backup-20200101-000000-ABCDEF0123456789"),
               settings.with_name("other.json.branchglance-backup-20200101-000000-0123456789abcdef")]
    for path in foreign:
        path.write_text("foreign", encoding="utf-8")
    old_dir = settings.with_name("settings.json.branchglance-backup-20000101-000000-0123456789abcdef")
    old_dir.mkdir()  # a directory with our name is never deleted
    for i in range(8):
        installer.install(settings, CMD.replace("Program Files", f"P{i}"), data_dir, now=1_800_000_000 + i * 60)
    backups = sorted(p.name for p in settings.parent.iterdir()
                     if installer._backup_pattern(settings).fullmatch(p.name) and p.is_file())
    assert len(backups) == installer.KEEP_BACKUPS == 5
    stamps = [time.strftime("%Y%m%d-%H%M", time.localtime(1_800_000_000 + i * 60)) for i in range(3, 8)]
    assert [b.split("-backup-")[1][:13] for b in backups] == stamps
    assert all(p.read_text(encoding="utf-8") == "foreign" for p in foreign) and old_dir.is_dir()


def test_ownership_requires_configured_executable(env):
    """G8: marker + hook shape with some other executable is not ours."""
    settings, data_dir = env
    impostors = [
        {"hooks": [{"type": "command", "command": "'C:/evil/python.exe' 'C:/x/claude_hook.py' --branchglance-hook"}]},
        {"hooks": [{"type": "command", "command": "'C:/evil/run.exe' --claude-hook --branchglance-hook"}]},
        {"hooks": [{"type": "command", "command": "python -m token_tracker.claude_hook --branchglance-hook"}]},
    ]
    for group in impostors:
        assert installer.is_ours(group) is False
        assert installer.is_ours(group, TEST_EXES) is False
    settings.write_text(json.dumps({"hooks": {"Stop": impostors}}), encoding="utf-8")
    installer.install(settings, CMD, data_dir)
    assert load(settings)["hooks"]["Stop"][:3] == impostors  # not replaced
    installer.uninstall(settings, data_dir)
    assert load(settings)["hooks"]["Stop"] == impostors  # not removed
    # The real dev interpreter and the configured launcher are ours.
    dev = installer._group(installer.default_command())
    assert installer.is_ours(dev) is True
    assert installer.is_ours(installer._group(CMD), installer._known_executables(data_dir, CMD)) is True


def test_frozen_default_command_targets_the_stable_install_launcher(monkeypatch, tmp_path):
    """A frozen app runs from releases/<version>/; hooks must name the root launcher."""
    root = tmp_path / "Programs" / "Branchglance"
    app = root / "releases" / "1.0.0+b1" / "BranchglanceApp.exe"
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(app))
    command = installer.default_command()
    launcher = (root / "Branchglance.exe").resolve().as_posix()
    assert command.startswith(f'"{launcher}" --claude-hook') or command.startswith(f"'{launcher}' --claude-hook") \
        or command.startswith(f"{launcher} --claude-hook")
    assert "BranchglanceApp" not in command and "releases" not in command
    assert installer._executable_of(command) in installer.default_executables()
