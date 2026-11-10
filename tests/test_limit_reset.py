from __future__ import annotations

import json
from pathlib import Path

import pytest

from token_tracker import limit_reset
from token_tracker.limit_reset import (
    AUTH_RETRY_SECONDS,
    RETRY_SECONDS,
    LimitResetService,
    LimitSample,
    classify_send_failure,
    is_auth_failure,
    redact_secrets,
)
from token_tracker.provider_policy import load_policies

T0 = 1_800_000_000.0
TEST_POLICIES = load_policies({
    "TOKEN_TRACKER_CODEX_COLLECT": "true",
    "TOKEN_TRACKER_CODEX_SHOW": "true",
    "TOKEN_TRACKER_CODEX_SEND": "true",
    "TOKEN_TRACKER_CLAUDE_COLLECT": "true",
    "TOKEN_TRACKER_CLAUDE_SHOW": "true",
    "TOKEN_TRACKER_CLAUDE_SEND": "true",
})


@pytest.fixture(autouse=True)
def explicit_fake_provider_policy(monkeypatch):
    monkeypatch.setattr(limit_reset, "POLICIES", TEST_POLICIES)


class Clock:
    def __init__(self, now: float = T0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class Sender:
    """Scripted greeting sender; never touches a real CLI."""

    def __init__(self, *results: dict) -> None:
        self.results = list(results) or [{"ok": True}]
        self.calls = 0

    def __call__(self, _cwd: Path) -> dict:
        self.calls += 1
        index = min(self.calls - 1, len(self.results) - 1)
        result = dict(self.results[index])
        # The default scripted failure represents typed pre-start proof.
        if result.get("ok") is False and result.get("outcome") in (None, "not_started"):
            raise limit_reset.CodexNotStarted(result.get("error") or result.get("output") or "spawn failed")
        return result


@pytest.fixture(autouse=True)
def no_real_cli(monkeypatch):
    def forbidden(*_args, **_kwargs):
        raise AssertionError("tests must never run a real CLI")

    monkeypatch.setattr(limit_reset, "_claude_auth_status", lambda cwd: {"ok": True, "orgId": "test-org"})
    monkeypatch.setattr(limit_reset.LimitResetService, "_claude_account_sample", lambda self, org, now: LimitSample(None, None, now))
    monkeypatch.setattr(limit_reset, "_run_command", forbidden)
    monkeypatch.setattr(limit_reset.subprocess, "run", forbidden)


def test_keepalive_fixture_never_reads_desktop(tmp_path, monkeypatch):
    monkeypatch.setattr(limit_reset.ClaudeDesktopProvider, "_usage", lambda self: pytest.fail("real Desktop profile read"))
    watcher = make(tmp_path, Claude=Sender())
    watcher.check_provider("Claude", LimitSample(None, None, 0))


def make(tmp_path: Path, clock: Clock | None = None, **senders) -> LimitResetService:
    return LimitResetService(
        sources={},
        senders=senders or {"Claude": Sender(), "Codex": Sender()},
        state_path=tmp_path / "state.json",
        log_path=tmp_path / "events.jsonl",
        cwd=tmp_path,
        enabled=True,
        clock=clock or Clock(),
    )


def logged(tmp_path: Path) -> list[dict]:
    path = tmp_path / "events.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def names(tmp_path: Path) -> list[str]:
    return [row["event"] for row in logged(tmp_path)]


def provider(watcher: LimitResetService, name: str) -> dict:
    return watcher.status()["providers"][name]


def test_interrupted_codex_greeting_is_consumed_across_restart(tmp_path):
    clock = Clock()
    calls = []
    def interrupted(_cwd):
        calls.append("started")
        raise KeyboardInterrupt()
    watcher = make(tmp_path, clock, Codex=interrupted)
    deadline = T0 + 20
    watcher.check_provider("Codex", LimitSample(20, 20, T0, deadline))
    clock.advance(20)
    with pytest.raises(KeyboardInterrupt):
        watcher.check_provider("Codex", LimitSample(0, 20, clock(), deadline))
    persisted = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))
    assert persisted["providers"]["Codex"]["greetingIntent"]["cycle"] == str(deadline)
    sender = Sender()
    restarted = make(tmp_path, clock, Codex=sender)
    restarted.check_provider("Codex", LimitSample(0, 20, clock(), deadline))
    assert calls == ["started"]
    assert sender.calls == 0
    assert restarted._state["providers"]["Codex"]["lastGreetingOutcome"] == "unknown"


def test_unknown_codex_greeting_is_not_retried(tmp_path):
    clock = Clock()
    sender = Sender({"ok": False, "outcome": "unknown", "error": "timed out"})
    watcher = make(tmp_path, clock, Codex=sender)
    deadline = T0 + 20
    watcher.check_provider("Codex", LimitSample(20, 20, T0, deadline))
    clock.advance(20)
    watcher.check_provider("Codex", LimitSample(0, 20, clock(), deadline))
    clock.advance(RETRY_SECONDS + 1)
    watcher.check_provider("Codex", LimitSample(0, 20, clock(), deadline))
    assert sender.calls == 1


def test_started_sender_timeout_is_unknown_and_never_retried(tmp_path):
    import subprocess
    clock = Clock()
    calls = []
    def started_then_timeout(_cwd):
        calls.append("started")
        raise subprocess.TimeoutExpired("codex", 180)
    watcher = make(tmp_path, clock, Codex=started_then_timeout)
    watcher.check_provider("Codex", LimitSample(100, 10, T0))
    generation = watcher._state["providers"]["Codex"]["greetingGeneration"]
    watcher.check_provider("Codex", LimitSample(0, 10, T0 + 60))
    assert watcher._state["providers"]["Codex"]["lastGreetingOutcome"] == "unknown"
    assert watcher._state["providers"]["Codex"]["armed"] is False
    assert watcher._state["providers"]["Codex"]["greetingGeneration"] == generation
    clock.advance(RETRY_SECONDS + 1)
    watcher.check_provider("Codex", LimitSample(0, 10, T0 + 120))
    assert calls == ["started"]


def test_prestart_sender_failure_preserves_arm_generation(tmp_path):
    def not_started(_cwd):
        raise limit_reset.CodexNotStarted("spawn failed")
    watcher = make(tmp_path, Codex=not_started)
    watcher.check_provider("Codex", LimitSample(100, 10, T0))
    generation = watcher._state["providers"]["Codex"]["greetingGeneration"]
    watcher.check_provider("Codex", LimitSample(0, 10, T0 + 60))
    state = watcher._state["providers"]["Codex"]
    assert state["lastGreetingOutcome"] == "not_started"
    assert state["armed"] is True
    assert state["greetingGeneration"] == generation


@pytest.mark.parametrize("scheduled", [False, True])
def test_sender_exception_after_delivery_is_unknown_and_not_retried(tmp_path, scheduled):
    clock = Clock()
    deliveries = []
    def sender(_cwd):
        deliveries.append("delivered")
        raise ValueError("response decoding failed")
    watcher = make(tmp_path, clock, Codex=sender)
    deadline = T0 + 20 if scheduled else None
    watcher.check_provider("Codex", LimitSample(20 if scheduled else 100, 10, T0, deadline))
    clock.advance(20 if scheduled else 60)
    watcher.check_provider("Codex", LimitSample(0, 10, clock(), deadline))
    state = watcher._state["providers"]["Codex"]
    assert state["lastGreetingOutcome"] == "unknown"
    assert state["armed"] is False
    clock.advance(RETRY_SECONDS + 1)
    watcher.check_provider("Codex", LimitSample(0, 10, clock(), deadline))
    assert deliveries == ["delivered"]


def test_untyped_rejected_greeting_is_not_prestart_proof(tmp_path):
    watcher = make(tmp_path, Codex=lambda _cwd: {"ok": False, "outcome": "rejected"})
    watcher.check_provider("Codex", LimitSample(100, 10, T0))
    watcher.check_provider("Codex", LimitSample(0, 10, T0 + 60))
    assert watcher._state["providers"]["Codex"]["lastGreetingOutcome"] == "unknown"
    assert watcher._state["providers"]["Codex"]["armed"] is False


def test_scheduled_not_started_retry_keeps_same_generation(tmp_path):
    clock = Clock()
    sender = Sender({"ok": False, "outcome": "not_started", "error": "spawn failed"}, {"ok": True})
    watcher = make(tmp_path, clock, Codex=sender)
    deadline = T0 + 20
    watcher.check_provider("Codex", LimitSample(20, 20, T0, deadline))
    clock.advance(20)
    watcher.check_provider("Codex", LimitSample(0, 20, clock(), deadline))
    state = watcher._state["providers"]["Codex"]
    generation = state["greetingGeneration"]
    assert state["lastGreetingOutcome"] == "not_started"
    clock.advance(RETRY_SECONDS + 1)
    watcher.check_provider("Codex", LimitSample(0, 20, clock(), deadline))
    assert sender.calls == 2
    assert state["greetingGeneration"] == generation


def test_codex_greeting_exposes_start_phase_without_parsing_error_text(tmp_path, monkeypatch):
    from token_tracker.codex_client import CodexExecutable, CodexNotStarted
    import subprocess
    monkeypatch.setattr(limit_reset, "resolve_codex", lambda: CodexExecutable("codex.exe"))
    def fail_before(*_args, **_kwargs):
        raise CodexNotStarted("timed out")
    monkeypatch.setattr(limit_reset, "run_contained_codex", fail_before)
    assert limit_reset.send_codex_greeting(tmp_path, policies=TEST_POLICIES)["outcome"] == "not_started"
    def fail_after(*_args, **_kwargs):
        raise subprocess.TimeoutExpired("codex", 1)
    monkeypatch.setattr(limit_reset, "run_contained_codex", fail_after)
    assert limit_reset.send_codex_greeting(tmp_path, policies=TEST_POLICIES)["outcome"] == "unknown"


def test_contained_runner_separates_prestart_and_started_failures(monkeypatch):
    from token_tracker import codex_client
    import subprocess
    executable = codex_client.CodexExecutable("codex.exe")
    def fail_before(*_args, **_kwargs):
        raise OSError("same error")
    monkeypatch.setattr(codex_client, "_run_contained_codex", fail_before)
    with pytest.raises(codex_client.CodexNotStarted):
        codex_client.run_contained_codex(executable, [], timeout=1)
    def fail_after(*_args, mark_started, **_kwargs):
        mark_started()
        raise OSError("same error")
    monkeypatch.setattr(codex_client, "_run_contained_codex", fail_after)
    with pytest.raises(subprocess.TimeoutExpired):
        codex_client.run_contained_codex(executable, [], timeout=1)


# --- arming and reset observation -------------------------------------------------


def test_codex_schedules_from_deadline_sends_once_and_verifies_next_window(tmp_path):
    clock = Clock()
    sender = Sender()
    watcher = make(tmp_path, clock, Codex=sender)
    deadline = T0 + 600
    watcher.check_provider("Codex", LimitSample(20, 20, T0, deadline))
    state = provider(watcher, "Codex")
    assert state["phase"] == "keepalive_scheduled"
    assert state["nextGreetingAt"] == deadline
    assert names(tmp_path) == ["active_window_observed"]
    clock.advance(599)
    watcher.check_provider("Codex", LimitSample(20, 20, T0 + 599, deadline))
    assert sender.calls == 0
    clock.advance(2)
    watcher.check_provider("Codex", LimitSample(0, 20, T0 + 601, deadline))
    assert sender.calls == 1
    state = provider(watcher, "Codex")
    assert state["awaitingVerification"] is True
    assert state["phase"] == "greeting_verifying"
    next_deadline = T0 + 601 + 5 * 60 * 60
    watcher.check_provider("Codex", LimitSample(1, 20, T0 + 602, next_deadline))
    state = provider(watcher, "Codex")
    assert state["awaitingVerification"] is False
    assert state["lastVerifiedAt"] is not None
    assert state["verificationEvidence"] == "usage_and_deadline"
    assert state["quotaEffectVerified"] is None
    assert state["nextGreetingAt"] == next_deadline
    assert names(tmp_path) == ["active_window_observed", "send_started", "greeting_sent", "greeting_verified"]


def test_no_send_before_a_new_sample_even_when_percent_is_low(tmp_path):
    sender = Sender()
    watcher = make(tmp_path, Codex=sender)
    watcher.check_provider("Codex", LimitSample(100, 10, 500))
    # Same sample timestamp (stale Claude Desktop history) at a low percent.
    watcher.check_provider("Codex", LimitSample(11, 10, 500))
    watcher.check_provider("Codex", LimitSample(11, 10, 400))
    assert sender.calls == 0
    assert provider(watcher, "Codex")["resetObservedAt"] is None
    assert names(tmp_path) == ["armed"]


def test_codex_retries_when_successful_command_does_not_create_new_window(tmp_path):
    clock = Clock()
    sender = Sender()
    watcher = make(tmp_path, clock, Codex=sender)
    deadline = T0 + 600
    watcher.check_provider("Codex", LimitSample(20, 20, T0, deadline))
    clock.advance(601)
    watcher.check_provider("Codex", LimitSample(0, 20, T0 + 601, deadline))
    assert sender.calls == 1
    clock.advance(limit_reset.GREETING_VERIFY_SECONDS - 1)
    watcher.check_provider("Codex", LimitSample(0, 20, T0 + 659, deadline))
    assert sender.calls == 1
    clock.advance(2)
    watcher.check_provider("Codex", LimitSample(0, 20, T0 + 661, deadline))
    state = provider(watcher, "Codex")
    # An unverified greeting backs off instead of resending immediately.
    assert sender.calls == 1
    assert state["retryAfter"] == clock.now + RETRY_SECONDS
    assert state["unverifiedAttempts"] == 1
    assert "greeting_unverified" in names(tmp_path)
    clock.advance(RETRY_SECONDS - 1)
    watcher.check_provider("Codex", LimitSample(0, 20, clock.now, deadline))
    assert sender.calls == 1
    clock.advance(1)
    watcher.check_provider("Codex", LimitSample(0, 20, clock.now, deadline))
    assert sender.calls == 2
    assert provider(watcher, "Codex")["awaitingVerification"] is True


FIVE_HOURS = 5 * 60 * 60


def test_codex_stable_deadline_at_zero_percent_confirms_schedule_without_verifying_greeting(tmp_path):
    clock = Clock()
    sender = Sender()
    watcher = make(tmp_path, clock, Codex=sender)
    watcher.check_provider("Codex", LimitSample(0, 20, T0, T0 + 10))
    clock.advance(11)
    window = clock.now + FIVE_HOURS
    watcher.check_provider("Codex", LimitSample(0, 20, clock.now, window))
    assert sender.calls == 1
    # Simulate an older persisted state whose stable-deadline branch incorrectly
    # copied the current attempt into lastVerifiedAt.
    watcher._state["providers"]["Codex"]["lastVerifiedAt"] = watcher._state["providers"]["Codex"]["lastTriggerAt"]
    # The first post-send sample only pairs with the pre-send sample: no proof yet.
    clock.advance(5)
    watcher.check_provider("Codex", LimitSample(0, 20, clock.now, window))
    assert provider(watcher, "Codex")["awaitingVerification"] is True
    clock.advance(5)
    watcher.check_provider("Codex", LimitSample(0, 20, clock.now, window))
    state = provider(watcher, "Codex")
    assert state["awaitingVerification"] is False
    assert state["nextGreetingAt"] == window
    assert state["unverifiedAttempts"] == 0
    assert state["lastVerifiedAt"] is None
    assert state["lastWindowConfirmedAt"] is not None
    assert state["verificationEvidence"] == "stable_deadline"
    assert state["quotaEffectVerified"] is None
    events = logged(tmp_path)
    assert any(row["event"] == "quota_deadline_confirmed" and row["reason"] == "stable_deadline" for row in events)
    assert not any(row["event"] == "greeting_verified" for row in events)
    while clock.now < window - 30:
        clock.advance(30)
        watcher.check_provider("Codex", LimitSample(0, 20, clock.now, window))
    assert sender.calls == 1
    assert "greeting_unverified" not in names(tmp_path)


def test_codex_synthetic_deadline_backs_off_and_pauses_after_three_unverified(tmp_path):
    clock = Clock()
    sender = Sender()
    watcher = make(tmp_path, clock, Codex=sender)
    watcher.check_provider("Codex", LimitSample(0, 20, T0, T0 + 10))
    sends = []
    while clock.now < T0 + 4 * 60 * 60:
        clock.advance(10)
        before = sender.calls
        watcher.check_provider("Codex", LimitSample(0, 20, clock.now, clock.now + FIVE_HOURS))
        if sender.calls != before:
            sends.append(clock.now)
    assert len(sends) == 3
    assert all(later - earlier >= RETRY_SECONDS for earlier, later in zip(sends, sends[1:]))
    events = names(tmp_path)
    assert events.count("greeting_unverified") == 3
    assert events.count("keepalive_paused") == 1
    assert "greeting_verified" not in events
    state = provider(watcher, "Codex")
    assert state["unverifiedAttempts"] == 3
    assert state["nextGreetingAt"] > clock.now


def test_codex_stable_zero_percent_window_is_followed_not_greeted(tmp_path):
    clock = Clock()
    sender = Sender()
    watcher = make(tmp_path, clock, Codex=sender)
    window = T0 + 3 * 60 * 60
    for _ in range(50):
        watcher.check_provider("Codex", LimitSample(0, 20, clock.now, window))
        clock.advance(30)
    assert sender.calls == 0
    assert provider(watcher, "Codex")["nextGreetingAt"] == window


def test_quota_samples_keep_two_second_cadence_while_idle_and_armed(tmp_path):
    clock = Clock()
    watcher = make(tmp_path, clock, Codex=Sender())
    assert watcher._poll_interval() == 2
    watcher.check_provider("Codex", LimitSample(20, 20, T0, T0 + 3600))
    assert watcher._poll_interval() == 2
    assert watcher.status()["pollSeconds"] == 2
    clock.advance(3600 - 100)
    assert watcher._poll_interval() == 2
    assert watcher.status()["pollSeconds"] == 2


def test_codex_keepalive_schedule_survives_service_restart(tmp_path):
    clock = Clock()
    deadline = T0 + 600
    first = make(tmp_path, clock, Codex=Sender())
    first.check_provider("Codex", LimitSample(10, 20, T0, deadline))
    restored_sender = Sender()
    restored = LimitResetService(
        sources={}, senders={"Codex": restored_sender},
        state_path=tmp_path / "state.json", log_path=tmp_path / "events.jsonl",
        cwd=tmp_path, enabled=True, clock=clock,
    )
    assert provider(restored, "Codex")["nextGreetingAt"] == deadline
    clock.advance(601)
    restored.check_provider("Codex", LimitSample(0, 20, T0 + 601, deadline))
    assert restored_sender.calls == 1
    assert provider(restored, "Codex")["phase"] == "greeting_verifying"


def test_codex_synthetic_rolling_deadline_never_postpones_fixed_target(tmp_path):
    clock = Clock()
    sender = Sender()
    watcher = make(tmp_path, clock, Codex=sender)
    first_deadline = T0 + 5 * 60 * 60
    watcher.check_provider("Codex", LimitSample(0, 20, T0, first_deadline))
    for offset in (3, 6, 9):
        clock.advance(3)
        watcher.check_provider("Codex", LimitSample(0, 20, T0 + offset, first_deadline + offset))
    assert provider(watcher, "Codex")["nextGreetingAt"] == first_deadline
    assert names(tmp_path) == ["keepalive_scheduled"]
    assert sender.calls == 0


def test_codex_deadline_motion_without_usage_does_not_verify_greeting(tmp_path):
    clock = Clock()
    sender = Sender()
    watcher = make(tmp_path, clock, Codex=sender)
    deadline = T0 + 10
    watcher.check_provider("Codex", LimitSample(0, 20, T0, deadline))
    clock.advance(11)
    watcher.check_provider("Codex", LimitSample(0, 20, T0 + 11, deadline + 11))
    assert sender.calls == 1
    watcher.check_provider("Codex", LimitSample(0, 20, T0 + 14, deadline + 14))
    state = provider(watcher, "Codex")
    assert state["awaitingVerification"] is True
    assert state["lastVerifiedAt"] is None
    assert "greeting_verified" not in names(tmp_path)


def test_codex_active_window_overrides_stale_overdue_anchor_without_sending(tmp_path):
    clock = Clock()
    state_path = tmp_path / "state.json"
    state_path.write_text(json.dumps({"providers": {"Codex": {
        "lastTriggerAt": limit_reset._iso_from_epoch(T0 - 6 * 60 * 60),
        "nextGreetingAt": T0 - 60,
    }}}), encoding="utf-8")
    sender = Sender()
    watcher = LimitResetService(
        sources={}, senders={"Codex": sender}, state_path=state_path,
        log_path=tmp_path / "events.jsonl", cwd=tmp_path, enabled=True, clock=clock,
    )
    real_deadline = T0 + 5 * 60 * 60
    watcher.check_provider("Codex", LimitSample(3, 20, T0, real_deadline))
    state = provider(watcher, "Codex")
    assert sender.calls == 0
    assert state["nextGreetingAt"] == real_deadline
    assert names(tmp_path) == ["active_window_observed"]


def test_codex_keepalive_attempts_at_99_percent_weekly(tmp_path):
    clock = Clock()
    sender = Sender()
    watcher = make(tmp_path, clock, Codex=sender)
    deadline = T0 + 10
    watcher.check_provider("Codex", LimitSample(0, 99, T0, deadline))
    clock.advance(11)
    watcher.check_provider("Codex", LimitSample(0, 99, T0 + 11, deadline))
    assert sender.calls == 1
    assert provider(watcher, "Codex")["phase"] == "greeting_verifying"


def test_reset_observed_recorded_once_with_sample_details(tmp_path):
    clock = Clock()
    sender = Sender({"ok": False, "error": "network hiccup", "returnCode": 1})
    watcher = make(tmp_path, clock, Codex=sender)
    watcher.check_provider("Codex", LimitSample(100, 10, T0))
    watcher.check_provider("Codex", LimitSample(11, 12, T0 + 60))
    clock.advance(RETRY_SECONDS + 1)
    watcher.check_provider("Codex", LimitSample(5, 12, T0 + 120))
    rows = [row for row in logged(tmp_path) if row["event"] == "reset_observed"]
    assert len(rows) == 1
    assert rows[0]["fiveHourPercent"] == 11
    assert rows[0]["weeklyPercent"] == 12
    assert rows[0]["sampleAt"] == T0 + 60
    assert rows[0]["observedAt"].startswith("2027-01-15")
    assert rows[0]["scheduledResetAt"] is None
    state = provider(watcher, "Codex")
    assert state["resetObservedAt"] == rows[0]["observedAt"]
    assert state["resetObservedSampleAt"] == T0 + 60
    assert state["resetObservedPercent"] == 11


def test_rearms_when_window_exhausts_again_before_greeting_succeeds(tmp_path):
    clock = Clock()
    sender = Sender({"ok": False, "error": "boom"})
    watcher = make(tmp_path, clock, Codex=sender)
    watcher.check_provider("Codex", LimitSample(100, 10, T0))
    watcher.check_provider("Codex", LimitSample(10, 10, T0 + 60))
    watcher.check_provider("Codex", LimitSample(100, 10, T0 + 120))
    state = provider(watcher, "Codex")
    assert state["armed"] is True and state["resetObservedAt"] is None
    assert state["exhaustedSampleAt"] == T0 + 120
    assert names(tmp_path) == ["armed", "reset_observed", "send_failed", "armed"]
    assert logged(tmp_path)[-1]["rearmed"] is True


# --- sending ----------------------------------------------------------------------


def test_weekly_exhaustion_blocks_greeting_until_weekly_reset(tmp_path):
    sender = Sender()
    watcher = make(tmp_path, Codex=sender)
    watcher.check_provider("Codex", LimitSample(100, 100, 100))
    watcher.check_provider("Codex", LimitSample(0, 100, 200))
    watcher.check_provider("Codex", LimitSample(0, 100, 201))
    assert sender.calls == 0
    state = provider(watcher, "Codex")
    assert state["lastError"] == "weekly limit exhausted"
    assert state["phase"] == "blocked_weekly"
    assert names(tmp_path) == ["armed", "reset_observed", "blocked"]
    watcher.check_provider("Codex", LimitSample(0, 10, 202))
    assert sender.calls == 1
    assert provider(watcher, "Codex")["armed"] is False


def test_transient_failure_stays_armed_and_retries_after_15_minutes(tmp_path):
    clock = Clock()
    sender = Sender({"ok": False, "error": "connection reset", "returnCode": 1}, {"ok": True})
    watcher = make(tmp_path, clock, Codex=sender)
    watcher.check_provider("Codex", LimitSample(100, 20, T0))
    watcher.check_provider("Codex", LimitSample(0, 20, T0 + 60))
    state = provider(watcher, "Codex")
    assert sender.calls == 1
    assert state["armed"] is True
    assert state["needsLogin"] is False
    assert state["lastFailureKind"] == "transient"
    assert state["retryAfter"] == T0 + RETRY_SECONDS
    assert state["nextRetryAt"] == T0 + RETRY_SECONDS
    assert state["phase"] == "reset_observed_sending"
    clock.advance(RETRY_SECONDS - 1)
    watcher.check_provider("Codex", LimitSample(0, 20, T0 + 120))
    assert sender.calls == 1
    assert provider(watcher, "Codex")["armed"] is True
    clock.advance(2)
    watcher.check_provider("Codex", LimitSample(0, 20, T0 + 180))
    assert sender.calls == 2
    assert provider(watcher, "Codex")["armed"] is False


def test_auth_failure_needs_login_backs_off_and_records_one_event(tmp_path):
    clock = Clock()
    auth = {
        "ok": False, "returnCode": 1,
        "output": "Failed to authenticate: OAuth session expired and could not be refreshed",
    }
    sender = Sender(auth)
    watcher = make(tmp_path, clock, Codex=sender)
    watcher.check_provider("Codex", LimitSample(100, 10, T0))
    watcher.check_provider("Codex", LimitSample(11, 10, T0 + 60))
    state = provider(watcher, "Codex")
    assert sender.calls == 1
    assert state["armed"] is True
    assert state["needsLogin"] is True
    assert state["failedAttempts"] == 1
    assert state["lastError"] == "Codex CLI sign-in expired — sign in again with the Codex CLI"
    assert "OAuth session expired" in state["lastErrorDetail"]
    assert state["retryAfter"] == T0 + AUTH_RETRY_SECONDS
    assert state["phase"] == "needs_login"

    # A transient-style 15-minute wait is not enough.
    clock.advance(RETRY_SECONDS + 1)
    watcher.check_provider("Codex", LimitSample(11, 10, T0 + 120))
    assert sender.calls == 1

    for attempt in range(2, 5):
        clock.advance(AUTH_RETRY_SECONDS)
        watcher.check_provider("Codex", LimitSample(11, 10, T0 + 120 + attempt))
        assert sender.calls == attempt
        assert provider(watcher, "Codex")["failedAttempts"] == attempt

    failures = [row for row in logged(tmp_path) if row["event"] == "send_failed"]
    assert len(failures) == 1
    assert failures[0]["needsLogin"] is True
    assert failures[0]["failureKind"] == "auth"
    assert failures[0]["failedAttempts"] == 1
    assert provider(watcher, "Codex")["armed"] is True


def test_success_clears_errors_and_disarms(tmp_path):
    clock = Clock()
    sender = Sender({"ok": False, "error": "Not logged in · Please run /login"}, {"ok": True})
    watcher = make(tmp_path, clock, Codex=sender)
    watcher.check_provider("Codex", LimitSample(100, 10, T0))
    watcher.check_provider("Codex", LimitSample(0, 10, T0 + 60))
    assert provider(watcher, "Codex")["needsLogin"] is True
    clock.advance(AUTH_RETRY_SECONDS)
    watcher.check_provider("Codex", LimitSample(0, 10, T0 + 120))
    state = provider(watcher, "Codex")
    assert sender.calls == 2
    assert state["armed"] is False
    assert state["needsLogin"] is False
    assert state["failedAttempts"] == 0
    assert state["lastError"] is None
    assert state["lastFailureKind"] is None
    assert state["retryAfter"] == 0
    assert state["nextRetryAt"] is None
    assert state["phase"] == "greeting_sent"
    assert logged(tmp_path)[-1]["event"] == "greeting_sent"
    assert logged(tmp_path)[-1]["attempts"] == 2
    clock.advance(6 * 3600)
    assert provider(watcher, "Codex")["phase"] == "waiting_for_exhaustion"


def test_sender_exception_is_a_failure_not_a_crash(tmp_path):
    def explode(_cwd):
        raise limit_reset.CodexNotStarted("spawn failed")

    watcher = make(tmp_path, Codex=explode)
    watcher.check_provider("Codex", LimitSample(100, 10, T0))
    watcher.check_provider("Codex", LimitSample(0, 10, T0 + 60))
    state = provider(watcher, "Codex")
    assert state["armed"] is True
    assert state["lastError"] == "spawn failed"
    assert state["lastFailureKind"] == "transient"


def test_failed_send_stays_armed_and_state_is_persistent(tmp_path):
    clock = Clock()
    watcher = make(tmp_path, clock, Codex=Sender({"ok": False, "error": "not authenticated"}))
    watcher.check_provider("Codex", LimitSample(100, 10, T0))
    watcher.check_provider("Codex", LimitSample(0, 10, T0 + 60))
    assert provider(watcher, "Codex")["armed"] is True
    restored = LimitResetService(
        sources={}, senders={}, state_path=tmp_path / "state.json", log_path=tmp_path / "events.jsonl",
        enabled=True, clock=clock,
    )
    state = provider(restored, "Codex")
    assert state["armed"] is True
    assert state["needsLogin"] is True
    assert state["failedAttempts"] == 1
    assert state["resetObservedSampleAt"] == T0 + 60
    assert state["phase"] == "needs_login"
    assert [row["event"] for row in restored.status()["events"]] == ["armed", "reset_observed", "send_failed"]
    assert names(tmp_path) == ["armed", "reset_observed", "send_failed"]


def test_legacy_state_without_new_keys_still_loads(tmp_path):
    legacy = {"providers": {"Codex": {
        "armed": True, "exhaustedAt": "2026-09-13T09:44:00+00:00", "exhaustedSampleAt": T0,
        "lastTriggerAt": None, "lastStatus": {"five_hour_percent": 11, "weekly_percent": 20,
                                              "observed_at": T0 + 60, "reset_at": None, "checkedAt": "x"},
        "lastError": "Failed to authenticate", "lastPollError": None, "retryAfter": 0,
    }}}
    (tmp_path / "state.json").write_text(json.dumps(legacy), encoding="utf-8")
    sender = Sender({"ok": False, "output": "Failed to authenticate: OAuth session expired", "returnCode": 1})
    watcher = make(tmp_path, Codex=sender)
    assert provider(watcher, "Codex")["phase"] == "armed_waiting_for_reset"
    watcher.check_provider("Codex", LimitSample(11, 20, T0 + 120))
    assert provider(watcher, "Codex")["phase"] == "needs_login"
    assert names(tmp_path) == ["reset_observed", "send_failed"]


# --- polling and status -----------------------------------------------------------


def test_poll_exception_recorded_once_per_distinct_error(tmp_path):
    errors = iter(["timeout", "timeout", "timeout", "file missing", "file missing"])

    def failing():
        raise RuntimeError(next(errors))

    watcher = LimitResetService(
        sources={"Claude": failing, "Codex": lambda: LimitSample(10, 10, T0)},
        senders={}, state_path=tmp_path / "state.json", log_path=tmp_path / "events.jsonl",
        enabled=True, clock=Clock(),
    )
    for _ in range(5):
        watcher.check_once()
    polls = [row for row in logged(tmp_path) if row["event"] == "poll_failed"]
    assert [row["error"] for row in polls] == ["timeout", "file missing"]
    assert provider(watcher, "Claude")["lastPollError"] == "file missing"


def test_successful_poll_clears_only_transient_poll_error(tmp_path):
    watcher = LimitResetService(
        sources={
            "Claude": lambda: LimitSample(20, 30, 100),
            "Codex": lambda: LimitSample(40, 50, 100),
        },
        senders={}, state_path=tmp_path / "state.json", log_path=tmp_path / "events.jsonl",
        enabled=True,
    )
    watcher._provider_state("Claude")["lastPollError"] = "temporary"
    watcher._provider_state("Claude")["lastError"] = "not authenticated"
    watcher.check_once()
    status = watcher.status()["providers"]["Claude"]
    assert status["lastPollError"] is None
    assert status["lastError"] == "not authenticated"


def test_state_write_failure_keeps_watcher_polling_and_surfaces_reset(tmp_path, monkeypatch):
    clock = Clock()
    sender = Sender()
    watcher = make(tmp_path, clock, Claude=sender)
    def fail_state_write(*args, **kwargs):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(limit_reset.tempfile, "mkstemp", fail_state_write)
    watcher.check_provider("Claude", LimitSample(100, 10, T0))
    watcher.check_provider("Claude", LimitSample(0, 10, T0 + 60))

    status = watcher.status()
    assert sender.calls == 0
    assert status["providers"]["Claude"]["resetObservedAt"] is None
    assert status["persistenceError"].startswith("state persistence failed:")
    assert "No space left on device" in status["persistenceError"]
    assert watcher._poll_interval() == 2

    # The failed disk stays on a bounded retry cadence despite 2-second polling.
    attempts = 0
    def count_failed_state_write(*args, **kwargs):
        nonlocal attempts
        attempts += 1
        raise OSError(28, "No space left on device")
    monkeypatch.setattr(limit_reset.tempfile, "mkstemp", count_failed_state_write)
    watcher.check_provider("Claude", LimitSample(0, 10, T0 + 61))
    assert attempts == 0


def test_state_replace_failure_cleans_unique_temporary_file(tmp_path, monkeypatch):
    watcher = make(tmp_path)
    def fail_replace(_source, _target):
        raise OSError("replacement failed")
    monkeypatch.setattr(limit_reset.os, "replace", fail_replace)
    assert watcher._save_state() is False
    assert not list(tmp_path.glob("state.json.*.tmp"))
    assert "replacement failed" in watcher.status()["persistenceError"]


@pytest.mark.parametrize("step", ["mkstemp", "fdopen", "write", "fsync", "replace"])
def test_state_save_reports_each_io_failure(tmp_path, monkeypatch, step):
    watcher = make(tmp_path)
    def enospc(*_args, **_kwargs):
        raise OSError(28, "No space left on device")
    if step == "mkstemp":
        monkeypatch.setattr(limit_reset.tempfile, "mkstemp", enospc)
    elif step == "fdopen":
        monkeypatch.setattr(limit_reset.os, "fdopen", enospc)
    elif step == "write":
        monkeypatch.setattr(limit_reset.json, "dump", enospc)
    elif step == "fsync":
        monkeypatch.setattr(limit_reset.os, "fsync", enospc)
    else:
        monkeypatch.setattr(limit_reset.os, "replace", enospc)
    assert watcher._save_state() is False
    assert "No space left on device" in watcher.status()["persistenceError"]
    assert not list(tmp_path.glob("state.json.*.tmp"))


def test_event_log_write_failure_is_reported_and_does_not_raise(tmp_path, monkeypatch):
    watcher = make(tmp_path)
    real_open = Path.open
    log_path = watcher.log_path

    def fail_log_open(path, *args, **kwargs):
        if path == log_path:
            raise OSError(28, "No space left on device")
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", fail_log_open)
    watcher._record("Claude", "reset_observed", observedAt="2027-01-15T00:00:00+00:00")
    assert watcher.status()["events"][-1]["event"] == "reset_observed"
    assert "event log persistence failed" in watcher.status()["persistenceError"]


def test_reset_watcher_thread_survives_enospc(tmp_path, monkeypatch):
    import threading

    sampled = threading.Event()
    write_failed = threading.Event()
    watcher = LimitResetService(
        sources={"Claude": lambda: (sampled.set() or LimitSample(20, 10, T0))},
        senders={}, state_path=tmp_path / "state.json", log_path=tmp_path / "events.jsonl",
        enabled=True, clock=Clock(),
    )
    real_replace = limit_reset.os.replace
    def fail_replace(_source, _target):
        if Path(_target) != watcher.state_path:
            return real_replace(_source, _target)
        write_failed.set()
        raise OSError(28, "No space left on device")
    monkeypatch.setattr(limit_reset.os, "replace", fail_replace)
    try:
        watcher.start()
        assert sampled.wait(2)
        assert write_failed.wait(2)
        assert watcher.status()["running"] is True
        assert watcher.status()["persistenceError"] is not None
    finally:
        watcher.stop()


def test_status_contains_old_and_new_keys(tmp_path):
    watcher = make(tmp_path)
    watcher.check_provider("Codex", LimitSample(100, 20, T0, (T0 + 900) * 1000))
    status = watcher.status()
    for key in ("enabled", "running", "pollSeconds", "statePath", "logPath", "providers", "events", "startedAt"):
        assert key in status
    codex = status["providers"]["Codex"]
    for key in (
        "armed", "exhaustedAt", "exhaustedSampleAt", "lastTriggerAt", "lastStatus", "lastError",
        "lastPollError", "retryAfter", "resetObservedAt", "resetObservedSampleAt", "resetObservedPercent",
        "needsLogin", "failedAttempts", "lastFailureKind", "lastFailureAt", "lastAttemptAt", "lastErrorDetail",
        "phase", "scheduledResetAt", "scheduledResetPassed", "nextRetryAt", "nextGreetingAt",
        "awaitingVerification", "verificationDeadline", "preSendResetAt", "preSendPercent", "lastVerifiedAt",
        "verificationEvidence", "lastWindowConfirmedAt", "quotaEffectVerified",
    ):
        assert key in codex
    assert set(codex["lastStatus"]) >= {"reset_at", "checkedAt", "five_hour_percent", "weekly_percent", "observed_at"}
    assert codex["scheduledResetAt"] == T0 + 900  # milliseconds normalized to seconds
    assert codex["scheduledResetPassed"] is False
    assert all({"at", "provider", "event"} <= set(row) for row in status["events"])
    # Derived keys are never written back into the persisted state.
    persisted = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))["providers"]["Codex"]
    assert "phase" not in persisted and "scheduledResetAt" not in persisted


def test_back_to_back_service_starts_are_not_duplicated(tmp_path):
    watcher = LimitResetService(
        sources={"Claude": lambda: LimitSample(None, None, 0), "Codex": lambda: LimitSample(None, None, 0)},
        senders={}, state_path=tmp_path / "state.json", log_path=tmp_path / "events.jsonl",
        enabled=True, poll_seconds=15,
    )
    watcher._stop.set()
    watcher._run()
    again = LimitResetService(
        sources=watcher.sources, senders={}, state_path=tmp_path / "state.json",
        log_path=tmp_path / "events.jsonl", enabled=True, poll_seconds=15,
    )
    again._stop.set()
    again._run()
    assert names(tmp_path) == ["service_started"]
    assert again.status()["startedAt"] is not None


# --- classifier and command construction -----------------------------------------


@pytest.mark.parametrize("text", [
    "Failed to authenticate: OAuth session expired and could not be refreshed",
    "Not logged in · Please run /login",
    "Invalid API key · Please run /login",
    "API Error: 401 {\"type\":\"error\"}",
    "Unauthorized",
    "No credentials found",
    "Please sign in to continue",
    "Error: You need to log in first",
])
def test_auth_classifier_matches_login_problems(text):
    assert is_auth_failure(text)
    assert classify_send_failure({"ok": False, "output": text}) == "auth"


@pytest.mark.parametrize("text", [
    "Codex app server timed out",
    "connection reset by peer",
    "Claude Code CLI is not installed",
    "Rate limit reached, retry in 4010 seconds",
    "wrote the error log index",
    "",
    None,
])
def test_auth_classifier_ignores_transient_problems(text):
    assert not is_auth_failure(text)
    assert classify_send_failure({"ok": False, "error": text}) == "transient"


def test_failure_output_is_redacted_and_truncated(tmp_path):
    secret = "sk-ant-oat01-" + "A" * 40
    output = "x" * 400 + f" connection dropped token={secret} Bearer abcdefghijklmnop"
    watcher = make(tmp_path, Codex=Sender({"ok": False, "output": output}))
    watcher.check_provider("Codex", LimitSample(100, 10, T0))
    watcher.check_provider("Codex", LimitSample(0, 10, T0 + 60))
    state = provider(watcher, "Codex")
    assert len(state["lastError"]) <= 300
    assert secret not in state["lastError"] and "abcdefghijklmnop" not in state["lastError"]
    assert secret not in (tmp_path / "events.jsonl").read_text(encoding="utf-8")
    assert secret not in (tmp_path / "state.json").read_text(encoding="utf-8")
    assert "sk-" not in redact_secrets("key sk-abcdefghijk")


def test_greeting_commands_use_low_cost_models_without_tools(monkeypatch, tmp_path):
    from token_tracker.codex_client import CodexExecutable
    seen = []
    monkeypatch.setattr(limit_reset, "_find_executable", lambda name: f"{name}.exe")
    monkeypatch.setattr(limit_reset, "resolve_codex", lambda: CodexExecutable("codex.exe"))
    monkeypatch.setattr(limit_reset, "run_contained_codex", lambda executable, command, **kwargs:
                        seen.append(([executable.path, *command], kwargs["cwd"])) or
                        type("Result", (), {"returncode": 0, "stdout": "", "stderr": ""})())
    monkeypatch.setattr(limit_reset, "_claude_process", lambda command, cwd, timeout: seen.append((command, cwd)) or {"started": True, "returnCode": 0, "data": b'{"type":"system","subtype":"init","apiKeySource":"none"}\n{"type":"result","is_error":false}'})
    monkeypatch.setattr(
        limit_reset, "_run_command",
        lambda command, cwd, timeout=180: seen.append((command, cwd)) or {"ok": True},
    )
    assert limit_reset.send_claude_greeting(tmp_path, policies=TEST_POLICIES)["ok"]
    assert limit_reset.send_codex_greeting(tmp_path, policies=TEST_POLICIES)["ok"]
    claude, codex = seen[0][0], seen[1][0]
    assert ["--model", limit_reset.CLAUDE_GREETING_MODEL] == claude[claude.index("--model"):claude.index("--model") + 2]
    assert ["--effort", "low"] == claude[claude.index("--effort"):claude.index("--effort") + 2]
    assert "--tools" in claude and "--max-turns" in claude
    assert ["--model", "gpt-5.6-luna"] == codex[codex.index("--model"):codex.index("--model") + 2]
    assert "--ephemeral" in codex and "--ignore-user-config" in codex and "--ignore-rules" in codex
    assert codex[-1] == "Reply with exactly: hey"


def test_default_watcher_keeps_claude_greeter_independent_from_dashboard_history(tmp_path, monkeypatch):
    monkeypatch.setattr(limit_reset, "claude_limit_sample", lambda: LimitSample(None, None, T0))
    monkeypatch.setattr(limit_reset, "codex_limit_sample", lambda: LimitSample(10, 10, T0))
    state_path, log_path = tmp_path / "state.json", tmp_path / "events.jsonl"
    state_path.write_text(json.dumps({"providers": {"Claude": {"armed": True}}}), encoding="utf-8")
    log_path.write_text(json.dumps({"provider": "Claude", "event": "old", "at": "2026-09-01T00:00:00Z"})+"\n", encoding="utf-8")
    watcher = LimitResetService(state_path=state_path, log_path=log_path, clock=Clock(), enabled=True)
    watcher.check_once()
    status = watcher.status()
    assert status["enabledProviders"] == ["Claude", "Codex"]
    assert set(status["providers"]) == {"Claude", "Codex"}
    assert json.loads(state_path.read_text(encoding="utf-8"))["providers"]["Claude"]["armed"] is True
    assert "Claude" in log_path.read_text(encoding="utf-8")


def test_slow_greeting_keeps_status_responsive_and_prevents_duplicate_send(tmp_path):
    import threading
    entered, finish, received = threading.Event(), threading.Event(), threading.Event()
    calls = []
    def sender(_cwd):
        calls.append(1)
        entered.set()
        finish.wait(5)
        return {"ok": True}
    watcher = make(tmp_path, Codex=sender)
    watcher.check_provider("Codex", LimitSample(100, 10, T0))
    worker = threading.Thread(target=lambda: watcher.check_provider("Codex", LimitSample(0, 10, T0+60)))
    worker.start()
    assert entered.wait(2)
    status_reader = threading.Thread(target=lambda: (watcher.status(), received.set()))
    status_reader.start()
    try:
        assert received.wait(1)
        watcher.check_provider("Codex", LimitSample(0, 10, T0+61))
        assert len(calls) == 1
    finally:
        finish.set()
        worker.join(2)
        status_reader.join(2)
    assert provider(watcher, "Codex")["lastResetAt"] is not None
    previous = provider(watcher, "Codex")["lastResetAt"]
    watcher.check_provider("Codex", LimitSample(100, 10, T0+18000))
    assert provider(watcher, "Codex")["resetObservedAt"] is None
    assert provider(watcher, "Codex")["lastResetAt"] == previous


def test_claude_poll_without_fresh_percent_is_undated_so_it_cannot_blank_display_meters(tmp_path):
    watcher = make(tmp_path, Claude=Sender())
    watcher.check_provider("Claude", LimitSample(40, 20, T0))
    assert provider(watcher, "Claude")["lastStatus"]["checkedAt"] is not None
    # Between Claude Desktop refetches the strict greeter gate yields no percent.
    watcher.check_provider("Claude", LimitSample(None, None, 0))
    status = provider(watcher, "Claude")["lastStatus"]
    assert status["five_hour_percent"] is None and status["checkedAt"] is None


def test_forged_and_replayed_statusline_files_cannot_arm_or_send(tmp_path, monkeypatch):
    from token_tracker import claude_statusline
    clock = Clock()
    sender = Sender()
    watcher = make(tmp_path, clock, Claude=sender)
    root = tmp_path / 'sample'
    monkeypatch.setattr(claude_statusline, 'sample_paths', lambda: [root / claude_statusline.FILENAME])
    monkeypatch.setattr(claude_statusline.time, 'time', clock)
    monkeypatch.setattr(limit_reset.ClaudeDesktopProvider, '_usage', lambda self: ([], {}))
    calls = []
    def desktop_usage(*args, **kwargs):
        calls.append(kwargs)
        return {'fiveHourPercent': None, 'weeklyPercent': None, 'observedAt': None, 'fiveHour': None, 'weekly': None}
    monkeypatch.setattr(limit_reset, 'usage_info', desktop_usage)
    for used, reset in ((99, T0 + 100), (0, T0 + 18100)):
        payload = {'rate_limits': {'five_hour': {'used_percentage': used, 'resets_at': reset}}}
        claude_statusline.write(claude_statusline.parse(payload, now=clock()), root, now=clock())
        watcher.check_provider('Claude', limit_reset.claude_limit_sample())
        clock.advance(120)
    assert sender.calls == 1
    assert provider(watcher, 'Claude')['phase'] != 'armed'
    watcher.check_provider('Claude', LimitSample(99, None, clock(), clock() + 100))
    clock.advance(120)
    watcher.check_provider('Claude', limit_reset.claude_limit_sample())
    assert sender.calls == 1
    assert calls == [{}, {}, {}]  # retain usage_info's original 90-second default
