from __future__ import annotations

from pathlib import Path

import pytest

from token_tracker import limit_reset
from token_tracker.limit_reset import LimitResetService, LimitSample
from token_tracker.provider_policy import load_policies


def make(tmp_path: Path, **kwargs) -> LimitResetService:
    policies = kwargs.pop("policies", None)
    return LimitResetService(
        sources={"Codex": lambda: pytest.fail("quota source must stay idle")},
        senders={"Codex": lambda _cwd: pytest.fail("greeting must stay disabled")},
        state_path=tmp_path / "state.json",
        log_path=tmp_path / "events.jsonl",
        cwd=tmp_path,
        policies=policies if policies is not None else load_policies(),
        **kwargs,
    )


def test_default_is_disabled_and_status_does_not_poll_or_send(tmp_path, monkeypatch):
    monkeypatch.delenv("TOKEN_TRACKER_LIMIT_RESET", raising=False)
    watcher = make(tmp_path)

    watcher.check_once()
    watcher.check_provider("Codex", LimitSample(100, 20, 1_800_000_000, 1_800_000_600))
    status = watcher.status()

    assert status["enabled"] is False
    assert status["running"] is False
    assert status["providers"]["Codex"]["phase"] == "disabled"


@pytest.mark.parametrize("value", ["1", "true", "on", " TRUE ", "On"])
def test_recognized_environment_opt_in_enables_greeter(tmp_path, monkeypatch, value):
    monkeypatch.setenv("TOKEN_TRACKER_LIMIT_RESET", value)

    assert make(tmp_path).status()["enabled"] is True


@pytest.mark.parametrize("value", [None, "0", "false", "off"])
def test_absent_and_explicit_false_environment_values_disable(tmp_path, monkeypatch, value):
    if value is None:
        monkeypatch.delenv("TOKEN_TRACKER_LIMIT_RESET", raising=False)
    else:
        monkeypatch.setenv("TOKEN_TRACKER_LIMIT_RESET", value)

    assert make(tmp_path).status()["enabled"] is False


@pytest.mark.parametrize("value", ["", "yes", "enabled", "2"])
def test_unknown_environment_values_are_rejected(tmp_path, monkeypatch, value):
    monkeypatch.setenv("TOKEN_TRACKER_LIMIT_RESET", value)
    with pytest.raises(ValueError):
        make(tmp_path)


def test_explicit_constructor_value_overrides_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("TOKEN_TRACKER_LIMIT_RESET", "true")

    assert make(tmp_path, enabled=False).status()["enabled"] is False
    assert make(tmp_path, enabled=True).status()["enabled"] is True


def test_enabled_override_does_not_widen_explicit_provider_denial(tmp_path, monkeypatch):
    monkeypatch.setenv("TOKEN_TRACKER_LIMIT_RESET", "true")
    monkeypatch.setenv("TOKEN_TRACKER_CODEX_SEND", "false")
    watcher = make(tmp_path, enabled=True)
    watcher.check_provider("Codex", LimitSample(100, 20, 1_800_000_000, 1_800_000_600))
    assert watcher.enabled is True
    assert watcher._sending == set()


def test_injected_sources_do_not_acquire_real_senders(tmp_path):
    policies = load_policies({
        "TOKEN_TRACKER_CODEX_COLLECT": "true",
        "TOKEN_TRACKER_CODEX_SEND": "true",
    })
    watcher = LimitResetService(
        sources={"Codex": lambda: LimitSample(0, 0, 1_800_000_000, 1_800_000_600)},
        state_path=tmp_path / "state.json", log_path=tmp_path / "events.jsonl",
        cwd=tmp_path, enabled=True, policies=policies,
    )
    assert watcher.senders == {}


def test_direct_greeting_sender_requires_policy_before_cli_lookup(tmp_path, monkeypatch):
    looked_up = []
    monkeypatch.setattr(limit_reset, "_find_executable", lambda name: looked_up.append(name))
    with pytest.raises(ValueError):
        limit_reset.send_codex_greeting(tmp_path, policies=load_policies({}))
    assert looked_up == []


def test_claude_legacy_origin_cannot_reach_sender_lookup(tmp_path, monkeypatch):
    looked_up = []
    monkeypatch.setattr(limit_reset, "_find_executable", lambda name: looked_up.append(name))
    malformed = load_policies({"TOKEN_TRACKER_CLAUDE_COLLECT": "true"})["Claude"]
    malformed = type(malformed)("Claude", True, True, True, "legacy_global", frozenset({"greet"}))
    with pytest.raises(ValueError):
        limit_reset.send_claude_greeting(tmp_path, policies={"Claude": malformed})
    assert looked_up == []


@pytest.mark.parametrize("value", [0, 1, "true", "false", [], object()])
def test_invalid_constructor_values_raise_before_service_setup(tmp_path, value):
    with pytest.raises(ValueError, match="enabled must be a bool or None"):
        make(tmp_path, enabled=value)


def test_legacy_scheduled_state_reports_disabled(tmp_path, monkeypatch):
    monkeypatch.delenv("TOKEN_TRACKER_LIMIT_RESET", raising=False)
    (tmp_path / "state.json").write_text(
        '{"providers":{"Codex":{"nextGreetingAt":1800000600}}}', encoding="utf-8"
    )

    status = make(tmp_path).status()

    assert status["providers"]["Codex"]["phase"] == "disabled"
    assert status["providers"]["Codex"]["nextGreetingAt"] == 1_800_000_600
