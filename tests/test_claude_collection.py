from __future__ import annotations

import json
import os
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path

import pytest

from token_tracker import claude_collection as collection
from token_tracker import claude_hook_install, product, provider_policy


@contextmanager
def no_install_mutex():
    yield


@pytest.fixture
def setup_paths(tmp_path, monkeypatch):
    policy = tmp_path / "Branchglance" / "policy.json"
    consent = tmp_path / "Branchglance" / "claude-consent.json"
    settings = tmp_path / "claude" / "settings.json"
    monkeypatch.setattr(product, "policy_file_path", lambda: policy)
    monkeypatch.setattr(product, "claude_consent_marker_path", lambda: consent)
    monkeypatch.setattr(claude_hook_install, "default_settings_path", lambda: settings)
    monkeypatch.setattr(claude_hook_install.claude_hook, "data_dir", lambda: str(consent.parent))
    monkeypatch.setattr(claude_hook_install, "status", lambda: {"state": "missing", "installed": [], "reason": None})
    monkeypatch.setattr(claude_hook_install, "install", lambda: {"ok": True})
    monkeypatch.setattr(claude_hook_install, "uninstall", lambda: {"ok": True, "hooksLeft": []})
    monkeypatch.setattr(collection, "_installer_update_lock", no_install_mutex)
    yield policy, consent, settings


def _installed(monkeypatch, consent: Path):
    def install():
        consent.parent.mkdir(parents=True, exist_ok=True)
        consent.write_text("{}", encoding="utf-8")
        monkeypatch.setattr(claude_hook_install, "status", lambda: {
            "state": "installed", "installed": list(range(14)), "reason": None})
        return {"ok": True}

    def uninstall():
        consent.unlink(missing_ok=True)
        monkeypatch.setattr(claude_hook_install, "status", lambda: {"state": "missing", "installed": [], "reason": None})
        return {"ok": True, "hooksLeft": []}

    monkeypatch.setattr(claude_hook_install, "install", install)
    monkeypatch.setattr(claude_hook_install, "uninstall", uninstall)


def test_enable_disable_preserves_other_providers_and_metadata(setup_paths, monkeypatch):
    policy, consent, _settings = setup_paths
    policy.parent.mkdir(parents=True)
    original = {"schemaVersion": 1, "origin": "installer", "writtenAt": "2026-10-23T00:00:00Z",
                "providers": {"Codex": {"collect": True, "show": True, "send": False}}}
    policy.write_text(json.dumps(original), encoding="utf-8")
    _installed(monkeypatch, consent)

    enabled = collection.enable()
    assert enabled["ok"] and enabled["state"] == "on" and enabled["grant"] is True
    saved = json.loads(policy.read_text(encoding="utf-8"))
    assert saved["providers"]["Claude"] == {"collect": True, "show": True}
    assert saved["providers"]["Codex"] == original["providers"]["Codex"]
    assert (saved["origin"], saved["writtenAt"]) == (original["origin"], original["writtenAt"])
    assert provider_policy.read_policy_file(policy, claude_consent=True).status == "ok"
    assert enabled["restartRequired"] is True

    disabled = collection.disable()
    assert disabled["ok"] and disabled["state"] == "off" and disabled["grant"] is False
    saved = json.loads(policy.read_text(encoding="utf-8"))
    assert "Claude" not in saved["providers"]
    assert saved["providers"]["Codex"] == original["providers"]["Codex"]
    assert not consent.exists()


@pytest.mark.parametrize("original", [
    {"schemaVersion": 1, "providers": {}, "futureKey": True},
    {"schemaVersion": 1, "providers": {"FutureProvider": {"collect": True}}},
    {"schemaVersion": 1, "providers": {"Codex": {"collect": True, "custom": [1]}}},
])
def test_policy_the_reader_would_ignore_is_refused_before_hooks_change(setup_paths, monkeypatch, original):
    policy, _consent, _settings = setup_paths
    policy.parent.mkdir(parents=True)
    policy.write_text(json.dumps(original), encoding="utf-8")
    before = policy.read_bytes()
    calls = []
    monkeypatch.setattr(claude_hook_install, "install", lambda: calls.append("install") or {"ok": True})
    result = collection.enable()
    assert result["ok"] is False and "left unchanged" in result["reason"]
    assert calls == [] and policy.read_bytes() == before


def test_enable_policy_write_failure_rolls_back_hooks(setup_paths, monkeypatch):
    _policy, _consent, _settings = setup_paths
    calls = []
    monkeypatch.setattr(claude_hook_install, "install", lambda: calls.append("install") or {"ok": True})
    monkeypatch.setattr(claude_hook_install, "uninstall", lambda: calls.append("uninstall") or {"ok": True, "hooksLeft": []})
    monkeypatch.setattr(collection, "_write_policy", lambda *args: (_ for _ in ()).throw(collection.ClaudeCollectionError("no write")))
    result = collection.enable()
    assert result["ok"] is False
    assert "rolled back" in result["reason"]
    assert calls == ["install", "uninstall"]


def test_disable_removes_unsupported_claude_send_flag(setup_paths, monkeypatch):
    policy, consent, _settings = setup_paths
    policy.parent.mkdir(parents=True)
    policy.write_text(json.dumps({"schemaVersion": 1, "providers": {
        "Claude": {"collect": True, "show": True, "send": True}}}), encoding="utf-8")
    consent.parent.mkdir(parents=True, exist_ok=True)
    consent.write_text("{}", encoding="utf-8")
    _installed(monkeypatch, consent)
    result = collection.disable()
    assert result["ok"] and result["state"] == "off"
    saved = json.loads(policy.read_text(encoding="utf-8"))
    assert "Claude" not in saved["providers"]
    assert provider_policy.read_policy_file(policy, claude_consent=False).status == "ok"


def test_expanded_output_over_reader_limit_is_rejected_before_publish(setup_paths):
    policy, _consent, _settings = setup_paths
    policy.parent.mkdir(parents=True)
    limit = provider_policy._FILE_MAX_BYTES
    chosen = None
    for n in range(65000, limit):
        document = {"schemaVersion": 1, "providers": {"Codex": {"collect": True}}, "origin": "x" * n}
        raw = json.dumps(document, separators=(",", ":")).encode()
        output = (json.dumps({**document, "providers": {**document["providers"],
                          "Claude": {"collect": True, "show": True}}}, ensure_ascii=False, indent=2) + "\n").encode()
        if len(raw) <= limit < len(output):
            chosen = raw
            break
    assert chosen is not None
    policy.write_bytes(chosen)
    before = policy.read_bytes()
    with pytest.raises(collection.ClaudeCollectionError, match="size limit"):
        collection._write_policy(policy, True)
    assert policy.read_bytes() == before


def test_policy_cas_preserves_concurrent_codex_change(setup_paths, monkeypatch):
    policy, _consent, _settings = setup_paths
    policy.parent.mkdir(parents=True)
    original = {"schemaVersion": 1, "providers": {"Codex": {"collect": True, "show": True}}}
    policy.write_text(json.dumps(original), encoding="utf-8")
    real_temp = collection._exclusive_temp

    def racing_temp(path, payload):
        result = real_temp(path, payload)
        changed = {"schemaVersion": 1, "providers": {"Codex": {"collect": False, "show": False}}}
        path.write_text(json.dumps(changed), encoding="utf-8")
        return result

    monkeypatch.setattr(collection, "_exclusive_temp", racing_temp)
    with pytest.raises(collection.ClaudeCollectionError, match="changed while editing"):
        collection._write_policy(policy, True)
    saved = json.loads(policy.read_text(encoding="utf-8"))
    assert saved["providers"]["Codex"] == {"collect": False, "show": False}
    assert "Claude" not in saved["providers"]


def test_busy_install_mutex_refuses_before_hook_install(setup_paths, monkeypatch):
    calls = []

    @contextmanager
    def busy():
        raise collection.ClaudeCollectionError("Branchglance is being updated; try again.")
        yield

    monkeypatch.setattr(collection, "_installer_update_lock", busy)
    monkeypatch.setattr(claude_hook_install, "install", lambda: calls.append("install") or {"ok": True})
    result = collection.enable()
    assert result["ok"] is False
    assert result["reason"] == "Branchglance is being updated; try again."
    assert calls == []


def test_policy_writer_refuses_busy_install_mutex_without_changes(setup_paths, monkeypatch):
    policy, _consent, _settings = setup_paths
    policy.parent.mkdir(parents=True)
    original = b'{"schemaVersion":1,"providers":{"Codex":{"collect":true}}}'
    policy.write_bytes(original)

    @contextmanager
    def busy():
        raise collection.ClaudeCollectionError("Branchglance is being updated; try again.")
        yield

    monkeypatch.setattr(collection, "_installer_update_lock", busy)
    with pytest.raises(collection.ClaudeCollectionError, match="Branchglance is being updated"):
        collection._write_policy(policy, True)
    assert policy.read_bytes() == original


@pytest.mark.parametrize("document", ["{bad", "[]", '{"schemaVersion": 1, "providers": []}'])
def test_malformed_policy_is_blocked_and_untouched(setup_paths, document):
    policy, _consent, _settings = setup_paths
    policy.parent.mkdir(parents=True)
    policy.write_text(document, encoding="utf-8")
    before = policy.read_bytes()
    result = collection.status()
    assert result["state"] == "blocked"
    assert policy.read_bytes() == before
    with pytest.raises(collection.ClaudeCollectionError):
        collection._write_policy(policy, True)


def test_reparse_policy_is_refused(setup_paths, tmp_path):
    policy, _consent, _settings = setup_paths
    policy.parent.mkdir(parents=True)
    target = tmp_path / "actual-policy.json"
    target.write_text('{"schemaVersion": 1, "providers": {}}', encoding="utf-8")
    try:
        os.symlink(target, policy)
    except (OSError, NotImplementedError):
        pytest.skip("policy symlinks unavailable")
    result = collection.status()
    assert result["state"] == "blocked"
    with pytest.raises(collection.ClaudeCollectionError, match="redirected"):
        collection._write_policy(policy, True)
    assert target.read_text(encoding="utf-8") == '{"schemaVersion": 1, "providers": {}}'


def test_reparse_policy_directory_is_refused(setup_paths, tmp_path, monkeypatch):
    policy, _consent, _settings = setup_paths
    actual = tmp_path / "actual-policy-dir"
    actual.mkdir()
    (actual / "policy.json").write_text('{"schemaVersion": 1, "providers": {}}', encoding="utf-8")
    redirected = tmp_path / "redirected-policy-dir"
    try:
        os.symlink(actual, redirected, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("directory symlinks unavailable")
    redirected_policy = redirected / "policy.json"
    monkeypatch.setattr(product, "policy_file_path", lambda: redirected_policy)
    assert collection.status()["state"] == "blocked"
    with pytest.raises(collection.ClaudeCollectionError, match="redirected"):
        collection._write_policy(redirected_policy, True)


@pytest.mark.skipif(sys.platform != "win32", reason="junction test requires Windows")
def test_policy_junction_chain_is_refused(setup_paths, tmp_path, monkeypatch):
    target = tmp_path / "junction-target"
    target.mkdir()
    policy = target / "policy.json"
    policy.write_text('{"schemaVersion": 1, "providers": {}}', encoding="utf-8")
    link = tmp_path / "junction-policy"
    made = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)], capture_output=True)
    if made.returncode:
        pytest.skip("junction creation unavailable")
    try:
        redirected_policy = link / "policy.json"
        monkeypatch.setattr(product, "policy_file_path", lambda: redirected_policy)
        assert collection.status()["state"] == "blocked"
        with pytest.raises(collection.ClaudeCollectionError, match="redirected"):
            collection._write_policy(redirected_policy, True)
        assert policy.read_text(encoding="utf-8") == '{"schemaVersion": 1, "providers": {}}'
    finally:
        subprocess.run(["cmd", "/c", "rmdir", str(link)], capture_output=True)


def test_environment_collect_veto_is_not_reported_as_on_or_restart(setup_paths, monkeypatch):
    policy, consent, _settings = setup_paths
    policy.parent.mkdir(parents=True)
    policy.write_text(json.dumps({"schemaVersion": 1, "providers": {
        "Claude": {"collect": True, "show": True}}}), encoding="utf-8")
    consent.parent.mkdir(parents=True, exist_ok=True)
    consent.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(claude_hook_install, "status", lambda: {
        "state": "installed", "installed": list(range(14)), "reason": "private path C:/secret/settings"})
    monkeypatch.setenv("TOKEN_TRACKER_CLAUDE_COLLECT", "0")
    result = collection.status()
    assert result["state"] == "vetoed"
    assert result["reason"] == "turned off by an environment setting"
    assert result["restartRequired"] is False
    assert "private path" not in json.dumps(result) and "C:/secret" not in json.dumps(result)


def test_installer_diagnostic_is_mapped_to_fixed_public_reason(setup_paths, monkeypatch):
    policy, _consent, _settings = setup_paths
    policy.parent.mkdir(parents=True)
    policy.write_text('{"schemaVersion": 1, "providers": {}}', encoding="utf-8")
    monkeypatch.setattr(claude_hook_install, "status", lambda: {
        "state": "blocked", "installed": ["Stop"], "reason": "private settings body at C:/outside/private.json"})
    result = collection.status()
    assert result["state"] == "blocked"
    assert result["reason"] == "Claude Code hooks are blocked by settings or build requirements."
    assert "private" not in json.dumps(result) and "C:/outside" not in json.dumps(result)


def test_development_without_policy_file_is_unavailable(setup_paths, monkeypatch):
    monkeypatch.setattr(product, "policy_file_path", lambda: None)
    monkeypatch.setattr(claude_hook_install, "install", lambda: pytest.fail("must not install without policy file"))
    status = collection.status()
    assert status["state"] == "unavailable"
    assert status["reason"] == "development build has no policy file"
    assert collection.enable()["ok"] is False
