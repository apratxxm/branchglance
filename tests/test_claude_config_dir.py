"""One resolver for Claude Code's config folder: CLAUDE_CONFIG_DIR, then ~/.claude."""

import json

import pytest

from token_tracker import claude_identity, claude_provider, claude_usage_api, product, providers
from token_tracker import claude_hook_install as installer, statusline_connect


def _env(monkeypatch, config=None):
    if config is None:
        monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    else:
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config))


def _folders(tmp_path, monkeypatch):
    """Every Branchglance entry point that resolves the config folder, as the folder it uses."""
    local = tmp_path / "local"
    return {
        "product.claude_config_dir": product.claude_config_dir(),
        "claude_hook_install.claude_config_dir": installer.claude_config_dir(),
        "claude_hook_install.default_settings_path.parent": installer.default_settings_path().parent,
        "claude_provider.claude_home": claude_provider.claude_home(),
        "claude_provider.projects_root.parent": claude_provider.projects_root().parent,
        "providers.ClaudeDesktopProvider.transcript_root.parent":
            providers.ClaudeDesktopProvider(str(local)).transcript_root.parent,
        "statusline_connect.settings_path.parent": statusline_connect.settings_path().parent,
    }


def _credentials_folder(monkeypatch):
    seen = []
    monkeypatch.setattr(claude_identity, "_read_passive_document", lambda path: seen.append(path) or {})
    claude_usage_api._read_credentials()
    assert len(seen) == 1
    return seen[0].parent


def test_config_dir_set_every_caller_uses_it(tmp_path, monkeypatch):
    config = tmp_path / "config"
    _env(monkeypatch, config=config)
    folders = _folders(tmp_path, monkeypatch)
    assert set(folders.values()) == {config}, folders
    assert _credentials_folder(monkeypatch) == config
    assert claude_identity.account_path() == config / ".claude.json"
    assert installer.default_settings_path() == config / "settings.json"


def test_claude_home_is_ignored(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    # Path.home() reads USERPROFILE on Windows and HOME elsewhere.
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path / "legacy"))
    _env(monkeypatch)
    folders = _folders(tmp_path, monkeypatch)
    assert set(folders.values()) == {home / ".claude"}, folders
    assert _credentials_folder(monkeypatch) == home / ".claude"
    assert claude_identity.account_path() == home / ".claude.json"
    # With CLAUDE_CONFIG_DIR set, the legacy value is still never read.
    config = tmp_path / "config"
    _env(monkeypatch, config=config)
    assert set(_folders(tmp_path, monkeypatch).values()) == {config}


def test_neither_set_every_caller_uses_home_claude(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    # Path.home() reads USERPROFILE on Windows and HOME elsewhere.
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    _env(monkeypatch)
    folders = _folders(tmp_path, monkeypatch)
    assert set(folders.values()) == {home / ".claude"}, folders
    assert _credentials_folder(monkeypatch) == home / ".claude"
    # Without an override Claude Code keeps .claude.json beside ~/.claude, not inside it.
    assert claude_identity.account_path() == home / ".claude.json"


def test_empty_values_are_ignored(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    config = tmp_path / "config"
    _env(monkeypatch, config="")
    assert product.claude_config_dir() == home / ".claude"
    assert installer.claude_config_dir() == home / ".claude"
    assert _credentials_folder(monkeypatch) == home / ".claude"
    _env(monkeypatch, config=config)
    assert product.claude_config_dir() == config
    assert installer.claude_config_dir() == config
    assert _credentials_folder(monkeypatch) == config


def test_status_line_recorded_under_old_folder_is_still_disconnected(tmp_path, monkeypatch):
    first, second = tmp_path / "first", tmp_path / "second"
    first.mkdir()
    second.mkdir()
    root = tmp_path / "state"
    _env(monkeypatch, config=first)
    (first / "settings.json").write_text("{}", encoding="utf-8")
    assert statusline_connect.connect(root=root, command="wrapper")["connected"]
    # The folder changed after connecting: the file that was edited stays the target.
    _env(monkeypatch, config=second)
    assert statusline_connect.status(root=root)["connected"]
    assert statusline_connect.disconnect(root=root)["changed"]
    assert "statusLine" not in json.loads((first / "settings.json").read_text(encoding="utf-8"))
    assert not (second / "settings.json").exists()


WRAPPER = {"type": "command", "command": "wrapper"}


def _call(action, root):
    return {"root": root, "command": "wrapper"} if action == "connect" else {"root": root}


def _record_file(root, settings_path):
    root.mkdir(parents=True, exist_ok=True)
    record = {"schemaVersion": 1, "settingsPath": settings_path, "originalPresent": False,
              "originalValue": None, "wrapper": WRAPPER, "createdAt": 1.0}
    (root / "statusline-chain.json").write_text(json.dumps(record), encoding="utf-8")


@pytest.mark.parametrize("action", ["status", "connect", "disconnect"])
def test_recorded_unrelated_absolute_file_is_refused(tmp_path, monkeypatch, action):
    config = tmp_path / "config"
    config.mkdir()
    _env(monkeypatch, config=config)
    root = tmp_path / "state"
    unrelated = tmp_path / "unrelated.json"
    unrelated.write_text(json.dumps({"statusLine": WRAPPER}), encoding="utf-8")
    before = unrelated.read_bytes()
    _record_file(root, str(unrelated))
    with pytest.raises(installer.HookInstallError, match="does not name settings.json"):
        getattr(statusline_connect, action)(**_call(action, root))
    assert unrelated.read_bytes() == before
    assert set(config.iterdir()) == set()


@pytest.mark.parametrize("action", ["status", "connect", "disconnect"])
def test_recorded_relative_path_is_refused(tmp_path, monkeypatch, action):
    config = tmp_path / "config"
    config.mkdir()
    _env(monkeypatch, config=config)
    root = tmp_path / "state"
    _record_file(root, "relative/settings.json")
    with pytest.raises(installer.HookInstallError, match="absolute settings path"):
        getattr(statusline_connect, action)(**_call(action, root))
    assert set(config.iterdir()) == set()


def test_recorded_settings_path_with_dotdot_is_normalised_and_used(tmp_path, monkeypatch):
    config = tmp_path / "config"
    config.mkdir()
    _env(monkeypatch, config=config)
    root = tmp_path / "state"
    (config / "settings.json").write_text(json.dumps({"statusLine": WRAPPER}), encoding="utf-8")
    _record_file(root, str(tmp_path / "x" / ".." / "config" / "settings.json"))
    assert statusline_connect.status(root=root)["connected"]
    assert statusline_connect.disconnect(root=root)["changed"]
    assert "statusLine" not in json.loads((config / "settings.json").read_text(encoding="utf-8"))
