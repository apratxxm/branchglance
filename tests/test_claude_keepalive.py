import io
import json
from pathlib import Path

import pytest

from token_tracker import limit_reset as lr
from token_tracker.provider_policy import load_policies

NOW = 1800000000.0
POLICIES = load_policies({"TOKEN_TRACKER_CLAUDE_COLLECT": "1", "TOKEN_TRACKER_CLAUDE_SHOW": "1", "TOKEN_TRACKER_CLAUDE_SEND": "1"})


@pytest.fixture(autouse=True)
def reset_widget_auth_cache():
    from token_tracker.providers import ClaudeLimitsProvider
    with ClaudeLimitsProvider._auth_cache_lock:
        ClaudeLimitsProvider._auth_cache = None
    yield
    with ClaudeLimitsProvider._auth_cache_lock:
        ClaudeLimitsProvider._auth_cache = None


class Clock:
    def __init__(self):
        self.now = NOW
        self.mono = 0
    def __call__(self):
        return self.now
    def monotonic(self):
        return self.mono


@pytest.fixture
def setup(tmp_path, monkeypatch):
    clock = Clock()
    sent = []
    evidence = {"deadline": NOW + 100, "weekly": 10}
    monkeypatch.setattr(lr, "_claude_auth_status", lambda cwd: {"ok": True, "orgId": "org-a"})
    monkeypatch.setattr(lr.LimitResetService, "_claude_account_sample", lambda self, org, now: lr.LimitSample(None, evidence["weekly"], now, evidence["deadline"]))
    def sender(cwd):
        sent.append(cwd)
        return {"ok": True, "delivered": True, "orgId": "org-a", "limits": {}}
    def service():
        return lr.LimitResetService(sources={}, senders={"Claude": sender}, state_path=tmp_path / "state.json", log_path=tmp_path / "events.jsonl", cwd=tmp_path, enabled=True, clock=clock, monotonic=clock.monotonic, policies=POLICIES)
    return clock, sent, evidence, service


def poll(service):
    service.check_provider("Claude", lr.LimitSample(None, None, 0))


def test_deadline_and_restart(setup):
    clock, sent, evidence, service = setup
    watcher = service()
    poll(watcher)
    assert not sent
    clock.now += 129
    clock.mono += 129
    poll(watcher)
    assert not sent
    clock.now += 1
    clock.mono += 1
    poll(watcher)
    assert len(sent) == 1
    poll(service())
    assert len(sent) == 1


def test_recovery_consumes_intent(setup, tmp_path):
    clock, sent, evidence, service = setup
    clock.now += 200
    (tmp_path / "state.json").write_text(json.dumps({"providers": {"Claude": {"claudeOrgId": "org-a", "claudeDeadline": NOW + 100, "dispatchIntent": {"cycleKey": f"claude:{NOW + 100}", "at": NOW + 130}}}}))
    poll(service())
    assert not sent
    assert "dispatch_recovered" in (tmp_path / "events.jsonl").read_text()


def test_save_failure_never_sends(setup, monkeypatch):
    clock, sent, evidence, service = setup
    watcher = service()
    clock.now += 200
    monkeypatch.setattr(watcher, "_save_state", lambda: False)
    poll(watcher)
    assert not sent


def test_bootstrap_and_weekly_gate(setup):
    clock, sent, evidence, service = setup
    evidence.update(deadline=None, weekly=100)
    watcher = service()
    poll(watcher)
    assert not sent
    evidence["weekly"] = None
    poll(watcher)
    assert len(sent) == 1
    clock.now += 17999
    clock.mono += 17999
    poll(watcher)
    assert len(sent) == 1
    clock.now += 1
    clock.mono += 1
    poll(watcher)
    assert len(sent) == 2


def test_auth_failure_blocks(setup, monkeypatch):
    clock, sent, evidence, service = setup
    monkeypatch.setattr(lr, "_claude_auth_status", lambda cwd: {"ok": False, "error": "subscription sign-in required"})
    clock.now += 200
    watcher = service()
    poll(watcher)
    assert not sent
    assert watcher._provider_state("Claude")["needsLogin"]


def test_not_subscription_consumes_and_pauses(setup):
    clock, sent, evidence, service = setup
    watcher = service()
    watcher.senders["Claude"] = lambda cwd: {"ok": False, "delivered": True, "failureKind": "not_subscription", "limits": {"five_hour": {"used_percentage": 1, "resets_at": NOW + 1000, "receivedAt": NOW}}}
    clock.now += 200
    poll(watcher)
    state = watcher._provider_state("Claude")
    assert state["needsLogin"] and state["lastFailureKind"] == "not_subscription"
    assert f"claude:{NOW + 100}" in state["consumedCycles"]
    assert not (watcher.state_path.parent / "claude-cli-limits.json").exists()
    clock.now += 20000
    clock.mono += 20000
    watcher.senders["Claude"] = lambda cwd: sent.append(cwd)
    poll(watcher)
    assert len(sent) == 1
    assert not state["needsLogin"]


def test_greeting_limits_verify_and_persist(setup):
    clock, sent, evidence, service = setup
    clock.now += 200
    watcher = service()
    watcher.senders["Claude"] = lambda cwd: {"ok": True, "delivered": True, "orgId": "org-a", "limits": {"five_hour": {"used_percentage": 2, "resets_at": NOW + 1000, "receivedAt": clock.now}}}
    poll(watcher)
    assert watcher._provider_state("Claude")["nextGreetingAt"] == NOW + 1000
    assert json.loads((watcher.state_path.parent / "claude-cli-limits.json").read_text())["orgId"] == "org-a"


def test_account_evidence_excludes_other_org(tmp_path, monkeypatch):
    monkeypatch.setattr(lr.ClaudeDesktopProvider, "_usage", lambda self: ([{"org": "other"}], {}))
    monkeypatch.setattr(lr, "usage_info", lambda *args, **kwargs: pytest.fail("mismatched Desktop org"))
    watcher = lr.LimitResetService(sources={}, senders={}, state_path=tmp_path / "state.json", log_path=tmp_path / "events.jsonl", enabled=True, policies=POLICIES)
    result = watcher._claude_account_sample("org-a", NOW)
    assert result.reset_at is None and result.weekly_percent is None


def test_no_consent_runs_no_cli(tmp_path, monkeypatch):
    monkeypatch.setattr(lr, "_claude_auth_status", lambda cwd: pytest.fail("CLI must not run"))
    policy = load_policies({"TOKEN_TRACKER_CLAUDE_COLLECT": "1", "TOKEN_TRACKER_CLAUDE_SHOW": "1"})
    watcher = lr.LimitResetService(sources={}, senders={}, state_path=tmp_path / "state.json", log_path=tmp_path / "events.jsonl", enabled=True, policies=policy)
    poll(watcher)


def test_cli_parser_and_environment(tmp_path, monkeypatch):
    observed = []
    events = [
        {"type": "system", "subtype": "init", "apiKeySource": "none"},
        {"type": "rate_limit_event", "rate_limit_info": {"unifiedWindows": {"five_hour": {"utilization": .06, "resetsAt": NOW + 100}, "seven_day": {"utilization": 2, "resetsAt": NOW + 300}}}},
        {"type": "result", "is_error": False},
    ]
    class Process:
        def __init__(self, command, **kwargs):
            observed.append((command, kwargs["env"]))
            data = {"loggedIn": True, "authMethod": "claude.ai", "apiProvider": "firstParty", "orgId": "org-a", "email": "private"} if command[1:3] == ["auth", "status"] else None
            self.stdout = io.BytesIO(json.dumps(data).encode() if data else b"\n".join(json.dumps(e).encode() for e in events))
        def wait(self, timeout=None): return 0
    monkeypatch.setattr(lr, "_find_executable", lambda name: "claude")
    monkeypatch.setattr(lr.subprocess, "Popen", Process)
    monkeypatch.setattr(lr.time, "time", lambda: NOW)
    for key in lr._CLAUDE_ENV_BLOCK:
        monkeypatch.setenv(key, "private")
    result = lr.send_claude_greeting(tmp_path, policies=POLICIES)
    assert result["ok"] and result["delivered"]
    assert result["limits"]["five_hour"]["used_percentage"] == 6
    assert "seven_day" not in result["limits"]
    assert all(not set(lr._CLAUDE_ENV_BLOCK).intersection(env) for _, env in observed)
    assert "private" not in str(result)
    events[0]["apiKeySource"] = "api_key"
    result = lr.send_claude_greeting(tmp_path, policies=POLICIES)
    assert not result["ok"] and result["failureKind"] == "not_subscription" and not result["limits"]


def test_missing_desktop_keeps_cli_evidence(tmp_path, monkeypatch):
    from token_tracker import claude_statusline as sl, product
    import time
    now = time.time()
    # An empty LOCALAPPDATA keeps provider discovery away from real Claude profiles.
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    monkeypatch.setattr(lr.ClaudeDesktopProvider, "_usage", lambda self: (_ for _ in ()).throw(FileNotFoundError("missing")))
    monkeypatch.setattr(product, "state_root", lambda: tmp_path)
    monkeypatch.setattr(sl, "sample_paths", lambda: [])
    sl.write({"five_hour": {"used_percentage": 10, "resets_at": now + 100, "receivedAt": now}}, tmp_path, now=now, filename=sl.CLI_FILENAME, source="claude-cli", org_id="org-a")
    watcher = lr.LimitResetService(sources={}, senders={}, state_path=tmp_path / "state.json", policies=POLICIES)
    assert watcher._claude_account_sample("org-a", now).reset_at == now + 100
    assert lr.claude_limit_sample().reset_at == now + 100


def test_weekly_selects_newer_unexpired_window(tmp_path, monkeypatch):
    desktop = {"weekly": NOW - 1, "weeklyPercent": 100, "observedAt": NOW - 100}
    cli = {"orgId": "org-a", "seven_day": {"used_percentage": 10, "resets_at": NOW + 500, "receivedAt": NOW}}
    monkeypatch.setattr(lr, "_claude_desktop_data", lambda **kwargs: ("org-a", desktop))
    monkeypatch.setattr(lr.claude_statusline, "read", lambda *args, **kwargs: cli)
    watcher = lr.LimitResetService(sources={}, senders={}, state_path=tmp_path / "state.json", policies=POLICIES)
    assert watcher._claude_account_sample("org-a", NOW).weekly_percent == 10
    desktop["weekly"] = NOW + 200
    assert watcher._claude_account_sample("org-a", NOW).weekly_percent == 10
    desktop["weekly"] = NOW + 800
    assert watcher._claude_account_sample("org-a", NOW).weekly_percent == 100


def test_inner_auth_mismatch_never_starts_print(tmp_path, monkeypatch):
    monkeypatch.setattr(lr, "_claude_auth_status", lambda cwd: {"ok": True, "orgId": "org-b"})
    monkeypatch.setattr(lr, "_claude_process", lambda *args: pytest.fail("print must not start"))
    result = lr.send_claude_greeting(tmp_path, policies=POLICIES, expected_org_id="org-a")
    assert result["preSendFailure"] and not result["ok"]


def test_monotonic_bootstrap_floor_and_restart(setup):
    clock, sent, evidence, service = setup
    evidence["deadline"] = None
    watcher = service()
    poll(watcher)
    clock.now += 20000
    poll(watcher)
    assert len(sent) == 1
    restarted = service()
    clock.mono += 17999
    poll(restarted)
    assert len(sent) == 1
    clock.mono += 1
    poll(restarted)
    assert len(sent) == 2


def test_wall_clock_jump_does_not_end_active_cycle(setup):
    clock, sent, evidence, service = setup
    watcher = service()
    poll(watcher)
    clock.now += 20000
    poll(watcher)
    assert not sent
    clock.mono += 130
    poll(watcher)
    assert len(sent) == 1


def test_wall_clock_backwards_preserves_cooldown(setup):
    clock, sent, evidence, service = setup
    evidence["deadline"] = None
    watcher = service()
    poll(watcher)
    clock.now -= 10
    clock.mono += 100
    poll(watcher)
    clock.now += 20010
    poll(watcher)
    assert len(sent) == 1


@pytest.mark.parametrize("outcome,event", [("failed", "greeting_failed"), ("uncertain", "greeting_uncertain")])
def test_failed_and_uncertain_are_not_success(setup, outcome, event):
    clock, sent, evidence, service = setup
    watcher = service()
    watcher._provider_state("Claude")["lastError"] = "previous failure"
    watcher.senders["Claude"] = lambda cwd: {"ok": False, "delivered": outcome == "failed", "outcome": outcome, "orgId": "org-a"}
    clock.now += 200
    poll(watcher)
    state = watcher._provider_state("Claude")
    assert state["lastError"] == "previous failure"
    events = [row["event"] for row in watcher.status()["events"]]
    assert event in events and "greeting_sent" not in events
    assert state["consumedCycles"]


def test_missing_init_is_uncertain(tmp_path, monkeypatch):
    monkeypatch.setattr(lr, "_claude_auth_status", lambda cwd: {"ok": True, "orgId": "org-a"})
    monkeypatch.setattr(lr, "_find_executable", lambda name: "claude")
    monkeypatch.setattr(lr, "_claude_process", lambda *args: {"started": True, "data": b""})
    result = lr.send_claude_greeting(tmp_path, policies=POLICIES, expected_org_id="org-a")
    assert result["outcome"] == "uncertain" and result["failureKind"] is None


def test_widget_shows_cli_limits_without_desktop_history(tmp_path, monkeypatch):
    from token_tracker import claude_statusline as sl, product, providers
    import time
    now = time.time()
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    monkeypatch.setattr(product, "state_root", lambda: tmp_path)
    monkeypatch.setattr(sl, "sample_paths", lambda: [])
    monkeypatch.setattr(providers.ClaudeLimitsProvider, "_last_live", {})
    provider = providers.ClaudeLimitsProvider(str(tmp_path / "local"), auth_status=lambda cwd: {"ok": True, "orgId": "org-a"})
    with pytest.raises(FileNotFoundError):
        provider.snapshot()
    sl.write({"five_hour": {"used_percentage": 12, "resets_at": now + 600, "receivedAt": now}}, tmp_path, now=now, filename=sl.CLI_FILENAME, source="claude-cli", org_id="org-a")
    snapshot = provider.snapshot()
    assert snapshot["fiveHour"]["percent"] == 12
    assert snapshot["fiveHour"]["resetAt"] == pytest.approx(now + 600, abs=1)


@pytest.fixture
def widget_cli(tmp_path, monkeypatch):
    from token_tracker import claude_statusline as sl, product, providers
    import time
    now = time.time()
    local = tmp_path / "local"
    monkeypatch.setenv("LOCALAPPDATA", str(local))
    monkeypatch.setattr(product, "state_root", lambda: tmp_path)
    monkeypatch.setattr(sl, "sample_paths", lambda: [])
    monkeypatch.setattr(providers.ClaudeLimitsProvider, "_last_live", {})
    sl.write({"five_hour": {"used_percentage": 12, "resets_at": now + 600, "receivedAt": now}}, tmp_path, now=now, filename=sl.CLI_FILENAME, source="claude-cli", org_id="org-a")
    return local, now


def test_widget_excludes_other_account_without_desktop(widget_cli):
    from token_tracker import providers
    local, _ = widget_cli
    provider = providers.ClaudeLimitsProvider(str(local), auth_status=lambda cwd: {"ok": True, "orgId": "org-b"})
    with pytest.raises(FileNotFoundError):
        provider.snapshot()


def test_widget_caches_matching_auth_for_300_seconds(widget_cli):
    from token_tracker import providers
    local, _ = widget_cli
    calls, clock = [], [0]
    def auth(cwd):
        calls.append(cwd)
        return {"ok": True, "orgId": "org-a"}
    instances = [providers.ClaudeLimitsProvider(str(local), auth_status=auth, monotonic=lambda: clock[0]) for _ in range(3)]
    for instance in instances:
        assert instance.snapshot()["fiveHour"]["percent"] == 12
        clock[0] += 100
    assert len(calls) == 1
    instances[0].snapshot()
    assert len(calls) == 2


def test_widget_caches_auth_failure_and_excludes_cli(widget_cli):
    from token_tracker import providers
    local, _ = widget_cli
    calls = []
    def auth(cwd):
        calls.append(cwd)
        return {"ok": False}
    providers_to_check = [providers.ClaudeLimitsProvider(str(local), auth_status=auth) for _ in range(2)]
    for provider in providers_to_check:
        with pytest.raises(FileNotFoundError):
            provider.snapshot()
    assert len(calls) == 1


def test_widget_desktop_org_needs_no_auth(widget_cli, monkeypatch):
    from token_tracker import providers
    local, _ = widget_cli
    monkeypatch.setattr(providers.ClaudeLimitsProvider, "_usage", lambda self: ([{"org": "org-a", "u": {}}], {"u": {}}))
    provider = providers.ClaudeLimitsProvider(str(local), auth_status=lambda cwd: pytest.fail("auth must not run"))
    assert provider.snapshot()["fiveHour"]["percent"] == 12


def test_widget_malformed_desktop_uses_matching_cli(widget_cli):
    from token_tracker import providers
    local, _ = widget_cli
    root = local / "Claude-Data" / "claude_test"
    root.mkdir(parents=True)
    (root / "plan-usage-history.json").write_text('{"samples":[null]}', encoding="utf-8")
    provider = providers.ClaudeLimitsProvider(str(local), auth_status=lambda cwd: {"ok": True, "orgId": "org-a"})
    assert provider.snapshot()["fiveHour"]["percent"] == 12


def test_widget_no_fresh_cli_does_not_call_auth(widget_cli, tmp_path, monkeypatch):
    from token_tracker import claude_statusline as sl, providers
    local, now = widget_cli
    monkeypatch.setattr(sl.time, "time", lambda: now + 601)
    provider = providers.ClaudeLimitsProvider(str(local), auth_status=lambda cwd: pytest.fail("auth must not run"))
    with pytest.raises(FileNotFoundError):
        provider.snapshot()
