"""Regression tests for opt-in features and guarded status-line connection."""
import json
import time
from pathlib import Path

import pytest

from token_tracker import (claude_hook_install, claude_identity, claude_usage_api,
                           claude_statusline, features, ledger_service, limit_reset,
                           product, providers, statusline_connect)


def test_feature_precedence_and_save_failure(tmp_path, monkeypatch):
    env = {}
    assert features.state("ledger", root=tmp_path, environ=env) == {
        "persisted": None, "effective": False, "env_locked": False, "restart_pending": False}
    features.save("ledger", True, root=tmp_path, environ=env)
    assert features.enabled("ledger", root=tmp_path, environ=env)
    state = features.state("ledger", root=tmp_path, environ={"TOKEN_TRACKER_LEDGER": "0"})
    assert state["persisted"] is True and not state["effective"] and state["env_locked"]
    with pytest.raises(ValueError):
        features.save("ledger", False, root=tmp_path, environ={"TOKEN_TRACKER_LEDGER": "0"})
    def fail(*args):
        raise OSError("replace failed")
    monkeypatch.setattr(features.os, "replace", fail)
    with pytest.raises(OSError):
        features.save("ledger", False, root=tmp_path, environ=env)
    assert features.enabled("ledger", root=tmp_path, environ=env)


def test_claude_send_feature_requires_consent_and_env_zero_veto():
    from token_tracker.provider_policy import PolicyFile, load_policies
    flags = {"Claude": {"collect": True, "show": True}}
    consented = PolicyFile(flags, claude_consent=True)
    denied = PolicyFile(flags, claude_consent=False)
    assert load_policies({}, policy_file=consented, claude_send_feature=True)["Claude"].allows("greet")
    assert not load_policies({}, policy_file=denied, claude_send_feature=True)["Claude"].allows("greet")
    assert not load_policies({"TOKEN_TRACKER_CLAUDE_SEND": "0"}, policy_file=consented,
                             claude_send_feature=True)["Claude"].allows("greet")
    assert not load_policies({})["Claude"].allows("greet")
    assert not load_policies({})["Codex"].allows("greet")


def test_claude_send_reports_restart_pending(tmp_path, monkeypatch):
    monkeypatch.setattr(features, "_send_at_start", None)
    features.mark_send_start(False)
    features.save("claude_send", True, root=tmp_path, environ={})
    state = features.state("claude_send", root=tmp_path, environ={})
    assert state["persisted"] is True and state["effective"] is True
    assert state["restart_pending"] is True and state["env_locked"] is False


def test_live_usage_off_does_not_read_credentials_or_construct_opener(tmp_path, monkeypatch):
    monkeypatch.setattr(product, "state_root", lambda: tmp_path)
    monkeypatch.setenv("TOKEN_TRACKER_CLAUDE_LIVE_USAGE", "0")
    monkeypatch.setattr(claude_usage_api, "policy_for", lambda _: type("P", (), {"allows": lambda *_: True})())
    monkeypatch.setattr(claude_usage_api, "_credentials", lambda: pytest.fail("credentials read"))
    monkeypatch.setattr(claude_usage_api, "_opener", lambda *a: pytest.fail("opener constructed"))
    refresher = claude_usage_api.ClaudeUsageRefresher(tmp_path)
    assert refresher.read(None) == {}
    refresher.poll()
    refresher.start()
    assert refresher._thread is None
    with pytest.raises(RuntimeError):
        claude_usage_api._request("/api/oauth/usage", "test")


def test_direct_credentials_reader_fails_closed(tmp_path, monkeypatch):
    monkeypatch.setattr(product, "state_root", lambda: tmp_path)
    monkeypatch.setenv("TOKEN_TRACKER_CLAUDE_LIVE_USAGE", "0")
    monkeypatch.setattr(claude_usage_api, "policy_for", lambda _: type("P", (), {"allows": lambda *_: True})())
    monkeypatch.setattr(claude_usage_api, "_read_credentials", lambda: pytest.fail("credentials opened"))
    assert claude_usage_api._credentials() is None


def test_statusline_connect_conflict_and_disconnect(tmp_path):
    root, path = tmp_path / "state", tmp_path / "claude" / "settings.json"
    path.parent.mkdir()
    path.write_text('{"other": 1}', encoding="utf-8")
    assert statusline_connect.connect(root=root, path=path, command="wrapper")['connected']
    assert statusline_connect.status(root=root, path=path)['connected']
    assert statusline_connect.disconnect(root=root, path=path)['changed']
    assert json.loads(path.read_text()) == {"other": 1}
    path.write_text('{"statusLine":{"type":"command","command":"existing"}}', encoding="utf-8")
    before = path.read_bytes()
    with pytest.raises(claude_hook_install.HookInstallError, match="Cannot safely chain"):
        statusline_connect.connect(root=root, path=path, command="wrapper")
    assert path.read_bytes() == before


def test_statusline_external_replacement_and_profile_isolation(tmp_path):
    root, path = tmp_path / "state", tmp_path / "one" / "settings.json"
    path.parent.mkdir()
    statusline_connect.connect(root=root, path=path, command="wrapper")
    other = tmp_path / "two" / "settings.json"
    other.parent.mkdir()
    with pytest.raises(claude_hook_install.HookInstallError, match="another Claude configuration"):
        statusline_connect.disconnect(root=root, path=other)
    assert not other.exists()
    path.write_text('{"statusLine":{"type":"command","command":"replacement"}}')
    result = statusline_connect.disconnect(root=root, path=path)
    assert not result["changed"] and "changed since" in result["reason"]
    assert json.loads(path.read_text())["statusLine"]["command"] == "replacement"


def test_ledger_join_timeout_blocks_restart(monkeypatch):
    class Thread:
        def is_alive(self): return True
    class Service:
        _thread = Thread()
        def stop(self): pass
    monkeypatch.setattr(ledger_service, "_CURRENT", Service())
    monkeypatch.setattr(ledger_service, "_RESTART_PENDING", False)
    assert ledger_service.apply_feature(False)
    assert ledger_service.apply_feature(True)


def test_ledger_reenable_creates_new_worker(monkeypatch):
    instances = []
    class Thread:
        def is_alive(self): return False
    class Service:
        def __init__(self): self._thread = Thread(); instances.append(self)
        def stop(self): pass
        def start(self): self.started = True
    old = Service()
    monkeypatch.setattr(ledger_service, "_CURRENT", old)
    monkeypatch.setattr(ledger_service, "_RESTART_PENDING", False)
    monkeypatch.setattr(ledger_service, "LedgerService", Service)
    assert not ledger_service.apply_feature(False)
    assert not ledger_service.apply_feature(True)
    assert ledger_service.current() is instances[-1] and instances[-1] is not old and instances[-1].started


def test_disabled_save_off_then_enable_opens_writer(tmp_path, monkeypatch):
    import threading
    opened = threading.Event()
    enabled = [False]
    class Writer:
        def __init__(self, *args, **kwargs): opened.set()
        def ingest(self, *args, **kwargs): return {"pendingBytes": 0, "bytesRead": 0}
        def pending_bytes(self, show): return 0
        def close(self): pass
    monkeypatch.setattr(ledger_service.features, "enabled", lambda *a, **k: enabled[0])
    monkeypatch.setattr(ledger_service, "STARTUP_DELAY_SECONDS", 0)
    monkeypatch.setattr(ledger_service, "_RESTART_PENDING", False)
    service = ledger_service.LedgerService(data_dir=tmp_path / "ledger", ledger_factory=Writer)
    monkeypatch.setattr(ledger_service, "_CURRENT", service)
    service.start()
    assert service._thread is None and not service._stop.is_set()
    assert not ledger_service.apply_feature(False)
    assert not service._stop.is_set()
    enabled[0] = True
    try:
        assert not ledger_service.apply_feature(True)
        assert opened.wait(3)
        assert ledger_service.current()._thread.is_alive()
    finally:
        ledger_service.current().stop()


def test_enable_replaces_stopped_never_started_service(monkeypatch):
    import threading
    class Service:
        def __init__(self): self._thread = None; self._stop = threading.Event(); self.started = False
        def start(self): self.started = True
    old = Service()
    old._stop.set()
    monkeypatch.setattr(ledger_service, "_CURRENT", old)
    monkeypatch.setattr(ledger_service, "_RESTART_PENDING", False)
    monkeypatch.setattr(ledger_service, "LedgerService", Service)
    assert not ledger_service.apply_feature(True)
    assert ledger_service.current() is not old and ledger_service.current().started


def test_observed_statusline_edit_refuses_without_retry(tmp_path, monkeypatch):
    root, path = tmp_path / "state", tmp_path / "settings.json"
    path.write_text('{"other":1}')
    original = claude_hook_install._unique_adjacent
    def concurrent(path_arg, tag, payload):
        target = original(path_arg, tag, payload)
        if tag == "tmp":
            path.write_text('{"other":2}')
        return target
    monkeypatch.setattr(claude_hook_install, "_unique_adjacent", concurrent)
    with pytest.raises(claude_hook_install.HookInstallError, match="changed while editing"):
        statusline_connect.connect(root=root, path=path, command="wrapper")
    assert json.loads(path.read_text()) == {"other": 2}
    assert not statusline_connect.status(root=root, path=path)["connected"]


def test_stale_record_is_discarded_on_disconnect(tmp_path):
    root, path = tmp_path / "state", tmp_path / "settings.json"
    statusline_connect.connect(root=root, path=path, command="wrapper")
    path.write_text('{"other":1}')
    result = statusline_connect.disconnect(root=root, path=path)
    assert not result["changed"] and not (root / "statusline-chain.json").exists()
    assert json.loads(path.read_text()) == {"other": 1}


def test_remaining_source_selection_preserves_high_water_and_partial_windows(tmp_path, monkeypatch):
    now = time.time()
    fingerprint = claude_identity.account_fingerprint("a", "org")
    identity = claude_identity.Identity("a", "org", "generation", "", fingerprint, now - 1000)
    monkeypatch.setattr(claude_identity, "current_identity", lambda: identity)
    code = {"five_hour": {"used_percentage": 20, "resets_at": now + 1000, "receivedAt": now}}
    cli = {"identityFingerprint": fingerprint,
           "five_hour": {"used_percentage": 90, "resets_at": now + 1000, "receivedAt": now - 10},
           "seven_day": {"used_percentage": 33, "resets_at": now + 2000, "receivedAt": now}}
    monkeypatch.setattr(product, "state_root", lambda: tmp_path)
    monkeypatch.setattr(claude_identity, "read_claude_code_usage", lambda *a, **k: code)
    monkeypatch.setattr(claude_statusline, "read", lambda *a, **k: cli if k.get("source") == "claude-cli" else {})
    monkeypatch.setattr(claude_usage_api, "read", lambda *_: {})
    result = providers.ClaudeLimitsProvider(str(tmp_path)).snapshot()
    assert result["fiveHour"]["percent"] == 90 and result["weekly"]["percent"] == 33
    assert result["identityVerified"] is True


@pytest.mark.parametrize("changed_at", [0, 100, 10000])
def test_desktop_only_never_yields_limits_or_greeting_evidence(tmp_path, monkeypatch, changed_at):
    fingerprint = claude_identity.account_fingerprint("a", "org")
    identity = claude_identity.Identity("a", "org", "generation", "", fingerprint, changed_at)
    monkeypatch.setattr(product, "state_root", lambda: tmp_path)
    monkeypatch.setattr(claude_identity, "current_identity", lambda: identity)
    monkeypatch.setattr(claude_identity, "read_claude_code_usage", lambda *a, **k: {})
    monkeypatch.setattr(claude_usage_api, "read", lambda *_: {})
    monkeypatch.setattr(claude_statusline, "read", lambda *a, **k: {})
    monkeypatch.setattr(providers.ClaudeDesktopProvider, "_usage", lambda self: ([{"org": "org", "u": {"fh": 99, "sd": 99}}], {"u": {"fh": 99, "sd": 99}}))
    result = providers.ClaudeLimitsProvider(str(tmp_path)).snapshot()
    assert result["fiveHour"]["percent"] is None and result["weekly"]["percent"] is None
    watcher = limit_reset.LimitResetService(sources={}, senders={}, state_path=tmp_path / "state.json")
    sample = watcher._claude_account_sample("org", time.time())
    assert sample.reset_at is None and sample.weekly_percent is None and sample.identity_generation is None


def test_account_switch_leaves_unbound_statusline_non_authoritative(tmp_path, monkeypatch):
    now = time.time()
    fingerprint = claude_identity.account_fingerprint("new-account", "org")
    identity = claude_identity.Identity("new-account", "org", "new-generation", "", fingerprint, now)
    monkeypatch.setattr(product, "state_root", lambda: tmp_path)
    monkeypatch.setattr(claude_identity, "current_identity", lambda: identity)
    monkeypatch.setattr(claude_identity, "read_claude_code_usage", lambda *a, **k: {})
    monkeypatch.setattr(claude_usage_api, "read", lambda *_: {})
    monkeypatch.setattr(claude_statusline, "sample_paths", lambda: [tmp_path / claude_statusline.FILENAME])
    claude_statusline.write({"five_hour": {"used_percentage": 80, "resets_at": now + 1000, "receivedAt": now}}, tmp_path, now=now)
    result = providers.ClaudeLimitsProvider(str(tmp_path)).snapshot()
    assert result["identityVerified"] is False and result["identityGeneration"] is None
    sample = limit_reset.claude_limit_sample()
    assert sample.five_hour_percent is None and sample.identity_generation is None
    watcher = limit_reset.LimitResetService(sources={}, senders={}, state_path=tmp_path / "state.json")
    assert watcher._claude_account_sample("org", now).reset_at is None
