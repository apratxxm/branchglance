from __future__ import annotations

import copy
import threading
import uuid

import pytest

from token_tracker import limit_reset, reset_actions
from token_tracker.limit_reset import LimitResetService, LimitSample
from token_tracker.provider_policy import ProviderPolicyError, load_policies
from token_tracker.reset_actions import ResetActionDispatcher, default_action


T0 = 1_800_000_000.0
ONE = "00000000-0000-4000-8000-000000000001"
TWO = "00000000-0000-4000-8000-000000000002"
THREE = "00000000-0000-4000-8000-000000000003"
TEST_POLICIES = load_policies({
    "TOKEN_TRACKER_CODEX_COLLECT": "true",
    "TOKEN_TRACKER_CODEX_SHOW": "true",
    "TOKEN_TRACKER_CODEX_SEND": "true",
})


@pytest.fixture(autouse=True)
def explicit_fake_provider_policy(monkeypatch):
    monkeypatch.setattr(limit_reset, "POLICIES", TEST_POLICIES)
    monkeypatch.setattr(reset_actions, "POLICIES", TEST_POLICIES)


class Harness:
    def __init__(self, *, inspection=None, queued=None, save_ok=True):
        self.now = T0
        self.state = default_action()
        self.inspection = inspection or {}
        self.queue_results = queued or {}
        self.queue_calls = []
        self.saved = []
        self.save_ok = save_ok
        self.dispatcher = self.make_dispatcher()

    def make_dispatcher(self):
        return ResetActionDispatcher(
            list_candidates=lambda limit=100: [
                {"id": ONE, "title": "one", "status": "idle"},
                {"id": TWO, "title": "two", "status": "idle"},
                {"id": THREE, "title": "three", "status": "idle"},
            ][:limit],
            inspect_target=lambda thread_id: self.inspection.get(thread_id, {
                "exists": True, "archived": False, "running": False, "waiting": False,
                "knownIdle": True, "observedAt": self.now,
            }),
            queue_continue=self.queue,
            clock=lambda: self.now,
            save=self.save,
            action_state=lambda: self.state,
        policies=TEST_POLICIES,
        )

    def save(self):
        self.saved.append(copy.deepcopy(self.state))
        return self.save_ok

    def queue(self, thread_id):
        self.queue_calls.append(thread_id)
        return self.queue_results.get(thread_id, {"status": "queued"})


def test_greeting_default_and_getter_are_copy_isolated():
    h = Harness()
    original = h.dispatcher.get()
    assert original == {"schemaVersion": 2, "mode": "greet", "threadIds": [], "armed": False,
                        "oneShot": True, "lastRun": None}
    original["threadIds"].append(ONE)
    assert h.dispatcher.get()["threadIds"] == []


def test_configure_continue_normalizes_deduplicates_and_requires_existing_uuid():
    h = Harness()
    configured = h.dispatcher.configure("continue", [ONE.upper(), ONE, TWO])
    assert configured["threadIds"] == [ONE, TWO]
    assert configured["armed"] is True
    assert h.dispatcher.get()["mode"] == "continue"
    with pytest.raises(ValueError):
        h.dispatcher.configure("continue", [str(uuid.uuid4())])
    with pytest.raises(ValueError):
        h.dispatcher.configure("greet", [ONE])
    with pytest.raises(ValueError):
        h.dispatcher.configure("continue", ["bad-id"])
    with pytest.raises(ValueError):
        h.dispatcher.configure("continue", [ONE] * 21)


def test_busy_stale_unknown_and_archived_targets_are_skipped():
    h = Harness(inspection={
        ONE: {"exists": True, "archived": False, "running": True, "waiting": False, "knownIdle": False, "observedAt": T0},
        TWO: {"exists": True, "archived": False, "running": False, "waiting": False, "knownIdle": True, "observedAt": T0 - 6},
    })
    h.dispatcher.configure("continue", [ONE, TWO])
    result = h.dispatcher.dispatch("codex:cycle-1")
    assert [row["status"] for row in result["targets"]] == ["skipped", "skipped"]
    assert not h.queue_calls


def test_nonfinite_inspection_time_and_unknown_archive_state_are_skipped():
    h = Harness(inspection={
        ONE: {"exists": True, "archived": False, "running": False, "waiting": False, "knownIdle": True, "observedAt": float("nan")},
        TWO: {"exists": True, "running": False, "waiting": False, "knownIdle": True, "observedAt": T0},
    })
    h.dispatcher.configure("continue", [ONE, TWO])
    result = h.dispatcher.dispatch("codex:cycle-inspection")
    assert [row["status"] for row in result["targets"]] == ["skipped", "skipped"]
    assert result["targets"][1]["reason"] == "chat archival status is unknown"
    assert not h.queue_calls


def test_partial_queue_is_recorded_and_one_shot_stays_unarmed():
    h = Harness(queued={TWO: {"status": "rejected", "error": "credential=secret"}})
    h.dispatcher.configure("continue", [ONE, TWO])
    result = h.dispatcher.dispatch("codex:cycle-2")
    assert [row["status"] for row in result["targets"]] == ["queued", "rejected"]
    assert result["targets"][1]["reason"] == "queue rejected the continuation"
    assert h.dispatcher.get()["mode"] == "continue"
    assert h.dispatcher.get()["armed"] is False
    assert "secret" not in repr(h.state)


def test_unknown_queue_outcome_is_terminal_across_restart_and_reconfiguration():
    h = Harness(queued={ONE: {"status": "unknown", "error": "timeout"}})
    h.dispatcher.configure("continue", [ONE])
    first = h.dispatcher.dispatch("codex:cycle-3")
    assert first["targets"][0]["status"] == "unknown"
    h.dispatcher.configure("continue", [ONE])
    second = h.dispatcher.dispatch("codex:cycle-3")
    assert second["targets"][0]["status"] == "unknown"
    assert h.queue_calls == [ONE]


def test_failed_claim_persistence_prevents_external_queue():
    h = Harness(save_ok=False)
    h.state.update({"mode": "continue", "threadIds": [ONE], "armed": True})
    result = h.dispatcher.dispatch("codex:cycle-4")
    assert result["targets"][0]["status"] == "skipped"
    assert result["targets"][0]["reason"] == "could not persist queue claim"
    assert not h.queue_calls


def test_crash_between_targets_preserves_consumed_batch_without_replay():
    h = Harness()
    h.dispatcher.configure("continue", [ONE, TWO])
    def interrupted(thread_id):
        h.queue_calls.append(thread_id)
        if thread_id == TWO:
            raise KeyboardInterrupt()
        return {"status": "queued"}
    h.dispatcher.queue_continue = interrupted
    with pytest.raises(KeyboardInterrupt):
        h.dispatcher.dispatch("codex:crash")
    durable = copy.deepcopy(h.saved[-1])
    assert durable["armed"] is False
    assert durable["finalizePending"]["targets"] == [ONE, TWO]
    assert durable["ledger"]["cycles"][-1]["targets"][1]["status"] == "unknown"
    h.state.clear()
    h.state.update(durable)
    h.dispatcher = h.make_dispatcher()
    assert h.dispatcher.dispatch("codex:crash") is None
    assert h.queue_calls == [ONE, TWO]


def test_final_save_failure_does_not_rearm_consumed_generation():
    h = Harness()
    h.dispatcher.configure("continue", [ONE])
    durable = []
    calls = [0]
    def save():
        calls[0] += 1
        if calls[0] == 3:
            return False
        durable.append(copy.deepcopy(h.state))
        return True
    h.dispatcher.save = save
    result = h.dispatcher.dispatch("codex:final-save")
    assert result["targets"][0]["status"] == "queued"
    assert durable[-1]["armed"] is False
    h.state.clear()
    h.state.update(durable[-1])
    h.dispatcher = h.make_dispatcher()
    assert h.dispatcher.dispatch("codex:final-save") is None
    assert h.queue_calls == [ONE]


@pytest.mark.parametrize("selection", [[TWO], [ONE, TWO]])
@pytest.mark.parametrize("first_status", ["queued", "unknown"])
def test_rearm_same_cycle_only_delivers_new_targets(selection, first_status):
    h = Harness(queued={ONE: {"status": first_status}})
    h.dispatcher.configure("continue", [ONE])
    h.dispatcher.dispatch("same-cycle")
    h.dispatcher.configure("continue", selection)
    result = h.dispatcher.dispatch("same-cycle")
    assert [item["threadId"] for item in result["targets"]] == selection
    assert h.queue_calls == [ONE, TWO]
    if ONE in selection:
        assert result["targets"][0]["status"] == first_status


def test_mixed_delivery_retries_only_proven_unstarted_target():
    h = Harness(queued={ONE: {"status": "queued"}, TWO: {"status": "not_started"}, THREE: {"status": "unknown"}})
    h.dispatcher.configure("continue", [ONE, TWO, THREE])
    generation = h.state["armGeneration"]
    first = h.dispatcher.dispatch("same-cycle")
    assert [item["status"] for item in first["targets"]] == ["queued", "not_started", "unknown"]
    assert h.state["armed"] is True and h.state["armGeneration"] == generation
    assert h.dispatcher.dispatch("same-cycle") is None
    h.now += reset_actions.NOT_STARTED_RETRY_SECONDS
    h.queue_results[TWO] = {"status": "queued"}
    second = h.dispatcher.dispatch("same-cycle")
    assert [item["status"] for item in second["targets"]] == ["queued", "queued", "unknown"]
    assert h.queue_calls == [ONE, TWO, THREE, TWO]
    assert h.state["armed"] is False


def test_not_started_retries_are_bounded():
    h = Harness(queued={ONE: {"status": "not_started"}})
    h.dispatcher.configure("continue", [ONE])
    for attempt in range(reset_actions.MAX_NOT_STARTED_ATTEMPTS):
        assert h.dispatcher.dispatch("same-cycle")["targets"][0]["status"] == "not_started"
        assert h.state["armed"] is (attempt + 1 < reset_actions.MAX_NOT_STARTED_ATTEMPTS)
        h.now += reset_actions.NOT_STARTED_RETRY_SECONDS
    assert h.dispatcher.dispatch("same-cycle") is None
    assert len(h.queue_calls) == reset_actions.MAX_NOT_STARTED_ATTEMPTS


def test_failed_result_save_does_not_release_unpersisted_claim():
    h = Harness(queued={ONE: {"status": "not_started"}, TWO: {"status": "not_started"}})
    h.dispatcher.configure("continue", [ONE, TWO])
    saves = []

    def save():
        saves.append(copy.deepcopy(h.state))
        return len(saves) != 3

    h.dispatcher.save = save
    result = h.dispatcher.dispatch("same-cycle")
    assert [row["status"] for row in result["targets"]] == ["not_started", "unknown"]
    h.now += reset_actions.NOT_STARTED_RETRY_SECONDS
    h.queue_results[ONE] = {"status": "queued"}
    result = h.dispatcher.dispatch("same-cycle")
    assert [row["status"] for row in result["targets"]] == ["queued", "unknown"]
    assert h.queue_calls == [ONE, TWO, ONE]


def test_typed_transport_prestart_exception_is_retryable():
    from token_tracker.codex_client import CodexNotStarted

    h = Harness()
    h.dispatcher.configure("continue", [ONE])
    def fail_before_start(_thread_id):
        raise CodexNotStarted("could not create process")
    h.dispatcher.queue_continue = fail_before_start
    assert h.dispatcher.dispatch("same-cycle")["targets"][0]["status"] == "not_started"
    assert h.state["armed"] is True


def test_result_commit_does_not_disarm_new_arm():
    h = Harness()
    h.dispatcher.configure("continue", [ONE])
    def rearm(_thread_id):
        h.dispatcher.configure("continue", [TWO])
        return {"status": "queued"}
    h.dispatcher.queue_continue = rearm
    h.dispatcher.dispatch("codex:old")
    assert h.state["armed"] is True
    assert h.state["threadIds"] == [TWO]


def trigger_service(tmp_path, now=T0):
    return LimitResetService(
        sources={}, senders={}, state_path=tmp_path / "trigger-state.json",
        log_path=tmp_path / "trigger-events.jsonl", cwd=tmp_path, enabled=True, clock=lambda: now,
    )


def armed_sample(*, five=50, weekly=20, observed=T0, reset=T0 + 100):
    return LimitSample(five, weekly, observed, reset)


def arm_state(*, five=50, weekly=20, observed=T0, reset=T0 + 100, deadline=T0 + 100, blocked=False):
    return {
        "armedAt": T0,
        "previousSample": {"fiveHourPercent": five, "weeklyPercent": weekly, "observedAt": observed, "resetAt": reset},
        "frozenDeadline": deadline,
        "sawBlocked": blocked or five >= 99 or weekly is not None and weekly >= 100,
    }


def test_blocked_exhaustion_then_small_allowance_and_weekly_reset_trigger(tmp_path):
    watcher = trigger_service(tmp_path, T0 + 2)
    exhausted = arm_state(five=99, weekly=20, observed=T0)
    assert watcher._continue_trigger_ready(exhausted, armed_sample(five=2, weekly=20, observed=T0 + 2, reset=T0 + 100))

    weekly = arm_state(five=50, weekly=100, observed=T0)
    assert watcher._continue_trigger_ready(weekly, armed_sample(five=50, weekly=10, observed=T0 + 2, reset=T0 + 100))


def test_bonus_reset_drop_can_trigger_before_frozen_deadline(tmp_path):
    watcher = trigger_service(tmp_path, T0 + 2)
    action = arm_state(five=20, observed=T0, reset=T0 + 100, deadline=T0 + 500)
    assert watcher._continue_trigger_ready(action, armed_sample(five=0, observed=T0 + 2, reset=T0 + 101))


def test_deadline_trigger_requires_new_fresh_sample_and_known_available_quotas(tmp_path):
    watcher = trigger_service(tmp_path, T0 + 101)
    action = arm_state(five=50, observed=T0, reset=T0 + 100, deadline=T0 + 100)
    assert watcher._continue_trigger_ready(action, armed_sample(five=50, observed=T0 + 101, reset=T0 + 100))

    stale = arm_state(five=50, observed=T0, reset=T0 + 100, deadline=T0)
    assert not watcher._continue_trigger_ready(stale, armed_sample(five=50, weekly=20, observed=T0 + 90, reset=T0 + 100))
    unknown_weekly = arm_state(five=50, observed=T0, reset=T0 + 100, deadline=T0)
    assert not watcher._continue_trigger_ready(unknown_weekly, armed_sample(five=50, weekly=None, observed=T0 + 101, reset=T0 + 100))


def test_stale_or_future_blocked_samples_do_not_poison_trigger_state(tmp_path):
    watcher = trigger_service(tmp_path, T0 + 1)
    for observed in (T0 - 10, T0 + 10):
        action = arm_state(five=20, observed=T0, reset=T0 + 100, deadline=T0 + 500)
        before = copy.deepcopy(action)
        assert not watcher._continue_trigger_ready(action, armed_sample(five=100, observed=observed, reset=T0 + 100))
        assert action == before
        assert not watcher._continue_trigger_ready(action, armed_sample(five=80, observed=T0 + 1, reset=T0 + 100))
        assert action["sawBlocked"] is False


def test_weekly_bonus_drop_triggers_without_five_hour_change(tmp_path):
    watcher = trigger_service(tmp_path, T0 + 2)
    action = arm_state(five=40, weekly=80, observed=T0, reset=T0 + 100, deadline=T0 + 500)
    assert watcher._continue_trigger_ready(action, armed_sample(five=40, weekly=10, observed=T0 + 2, reset=T0 + 100))


def test_zero_usage_jitter_and_ordinary_usage_increase_do_not_trigger(tmp_path):
    watcher = trigger_service(tmp_path, T0 + 2)
    jitter = arm_state(five=0, observed=T0, reset=T0 + 100, deadline=T0 + 500)
    assert not watcher._continue_trigger_ready(jitter, armed_sample(five=0, observed=T0 + 2, reset=T0 + 102))
    increase = arm_state(five=20, observed=T0, reset=T0 + 100, deadline=T0 + 500)
    assert not watcher._continue_trigger_ready(increase, armed_sample(five=25, observed=T0 + 2, reset=T0 + 100))


def test_first_active_sample_pins_deadline_but_zero_usage_does_not(tmp_path):
    watcher = trigger_service(tmp_path, T0 + 2)
    active = arm_state(five=20, observed=T0, reset=T0 + 100, deadline=None)
    active["armedAt"] = T0 + 1
    active["cycleId"] = "observation:token"
    assert not watcher._continue_trigger_ready(active, armed_sample(five=20, observed=T0 + 2, reset=T0 + 100))
    assert active["frozenDeadline"] == T0 + 100
    assert active["cycleId"] == f"deadline:{T0 + 100:.3f}"

    zero = arm_state(five=0, observed=T0, reset=T0 + 100, deadline=None)
    zero["armedAt"] = T0 + 1
    assert not watcher._continue_trigger_ready(zero, armed_sample(five=0, observed=T0 + 2, reset=T0 + 102))
    assert zero["frozenDeadline"] is None


def test_stale_initial_config_sample_cannot_mark_action_blocked(tmp_path):
    h = Harness()
    watcher = LimitResetService(
        sources={}, senders={}, state_path=tmp_path / "stale-state.json", log_path=tmp_path / "stale-events.jsonl",
        cwd=tmp_path, enabled=True, clock=lambda: h.now,
        list_candidates=h.dispatcher.list_candidates, inspect_target=h.dispatcher.inspect_target,
        queue_continue=lambda _thread_id: pytest.fail("stale baseline must not queue"),
    )
    watcher._provider_state("Codex")["lastStatus"] = {
        "five_hour_percent": 99, "weekly_percent": 20, "observed_at": T0 - 10, "reset_at": T0 + 100,
    }
    watcher.configure_reset_action("continue", [ONE])
    action = watcher._state["resetAction"]
    assert action["sawBlocked"] is False
    assert action["previousSample"]["fiveHourPercent"] is None
    h.now = T0 + 1
    watcher.check_provider("Codex", LimitSample(80, 20, h.now, T0 + 100))
    assert watcher.get_reset_action()["armed"] is True
    assert watcher.get_reset_action()["lastRun"] is None


def test_ready_dispatch_persistence_failure_withholds_and_keeps_armed(tmp_path):
    h = Harness()
    queued = []
    watcher = LimitResetService(
        sources={}, senders={}, state_path=tmp_path / "fail-state.json", log_path=tmp_path / "fail-events.jsonl",
        cwd=tmp_path, enabled=True, clock=lambda: h.now,
        list_candidates=h.dispatcher.list_candidates, inspect_target=lambda _id: {
            "exists": True, "archived": False, "running": False, "waiting": False,
            "knownIdle": True, "observedAt": h.now,
        }, queue_continue=lambda thread_id: (queued.append(thread_id) or {"status": "queued"}),
    )
    watcher.configure_reset_action("continue", [ONE])
    h.now = T0 + 0.5
    watcher.check_provider("Codex", LimitSample(99, 20, h.now, T0 + 100))
    h.now = T0 + 1
    watcher._save_state = lambda: False
    watcher.check_provider("Codex", LimitSample(80, 20, h.now, T0 + 100))
    assert queued == []
    assert watcher.get_reset_action()["armed"] is True
    assert watcher.get_reset_action()["lastRun"] is None


def test_concurrent_poll_cannot_dispatch_or_reconfigure_while_queue_is_pending(tmp_path):
    h = Harness()
    entered, release = threading.Event(), threading.Event()
    queue_calls = []

    def blocking_queue(thread_id):
        queue_calls.append(thread_id)
        entered.set()
        assert release.wait(5)
        return {"status": "queued"}

    watcher = LimitResetService(
        sources={}, senders={}, state_path=tmp_path / "concurrent-state.json", log_path=tmp_path / "concurrent-events.jsonl",
        cwd=tmp_path, enabled=True, clock=lambda: h.now,
        list_candidates=h.dispatcher.list_candidates,
        inspect_target=lambda _id: {"exists": True, "archived": False, "running": False, "waiting": False,
                                    "knownIdle": True, "observedAt": h.now},
        queue_continue=blocking_queue,
    )
    watcher.configure_reset_action("continue", [ONE])
    h.now = T0 + 0.5
    watcher.check_provider("Codex", LimitSample(99, 20, h.now, T0 + 100))
    h.now = T0 + 1
    first = threading.Thread(target=watcher.check_provider, args=("Codex", LimitSample(80, 20, h.now, T0 + 100)))
    first.start()
    assert entered.wait(5)
    with pytest.raises(RuntimeError):
        watcher.configure_reset_action("continue", [TWO])
    h.now += 0.1
    watcher.check_provider("Codex", LimitSample(70, 20, h.now, T0 + 100))
    release.set()
    first.join(5)
    assert not first.is_alive()
    assert queue_calls == [ONE]
    assert watcher.get_reset_action()["mode"] == "continue"


def test_limit_service_persists_selection_and_respects_quota_gate(tmp_path):
    h = Harness()
    queued = []
    watcher = LimitResetService(
        sources={}, senders={"Codex": lambda _cwd: pytest.fail("greeting fallback must not run")},
        state_path=tmp_path / "state.json", log_path=tmp_path / "events.jsonl", cwd=tmp_path,
        enabled=True, clock=lambda: h.now,
        list_candidates=h.dispatcher.list_candidates, inspect_target=h.dispatcher.inspect_target,
        queue_continue=lambda thread_id: (queued.append(thread_id) or {"status": "queued"}),
    )
    assert watcher.get_reset_action()["mode"] == "greet"
    assert watcher.configure_reset_action("continue", [ONE])["armed"] is True

    watcher.check_provider("Codex", LimitSample(20, 20, T0, T0 + 10))
    h.now = T0 + 10
    watcher.check_provider("Codex", LimitSample(0, 100, h.now, h.now + 5 * 60 * 60))
    assert watcher.get_reset_action()["armed"] is True
    assert queued == []

    h.now += 1
    watcher.check_provider("Codex", LimitSample(0, 20, h.now, h.now + 5 * 60 * 60))
    assert queued == [ONE]
    action = watcher.get_reset_action()
    assert action["mode"] == "continue" and action["armed"] is False
    assert action["lastRun"]["targets"][0]["status"] == "queued"
    persisted = __import__("json").loads((tmp_path / "state.json").read_text(encoding="utf-8"))
    assert persisted["resetAction"]["mode"] == "continue"
    h.now += 1
    watcher.check_provider("Codex", LimitSample(0, 20, h.now, h.now + 5 * 60 * 60))
    assert queued == [ONE]
    assert watcher.get_reset_action()["armed"] is False


def test_legacy_armed_selection_migrates_unarmed_and_preserves_delivery_ledger():
    h = Harness()
    ledger = {"cycles": [{"cycleId": "codex:cycle-old", "targets": [
        {"threadId": ONE, "status": "unknown", "reason": "old result"},
    ]}]}
    h.state.clear()
    h.state.update({"mode": "continue", "threadIds": [ONE], "armed": True,
                    "oneShot": True, "lastRun": {"cycleId": "old"}, "ledger": ledger})

    dispatcher = h.make_dispatcher()

    assert h.state["schemaVersion"] == 2
    assert dispatcher.get()["mode"] == "continue"
    assert dispatcher.get()["threadIds"] == [ONE]
    assert dispatcher.get()["armed"] is False
    assert h.state["ledger"] == ledger
    assert dispatcher.dispatch("codex:cycle-new") is None
    assert h.queue_calls == []


def test_legacy_global_consent_cannot_configure_or_dispatch_continue():
    legacy = load_policies({"TOKEN_TRACKER_LIMIT_RESET": "true"})
    state = default_action()
    listed = []
    queued = []
    dispatcher = ResetActionDispatcher(
        list_candidates=lambda **_kwargs: listed.append(True) or [{"id": ONE}],
        inspect_target=lambda _thread_id: {"exists": True, "archived": False,
                                           "knownIdle": True, "observedAt": T0},
        queue_continue=lambda thread_id: queued.append(thread_id) or {"status": "queued"},
        clock=lambda: T0, save=lambda: True, action_state=lambda: state, policies=legacy,
    )
    with pytest.raises(ProviderPolicyError):
        dispatcher.configure("continue", [ONE])
    state.update({"mode": "continue", "threadIds": [ONE], "armed": True})
    with pytest.raises(ProviderPolicyError):
        dispatcher.dispatch("codex:legacy-cycle")
    assert listed == [] and queued == []


def test_withdrawn_continue_policy_disarms_but_keeps_selection():
    state = default_action()
    state.update({"mode": "continue", "threadIds": [ONE], "armed": True,
                  "ledger": {"cycles": [{"cycleId": "old"}]}})
    queued = []
    dispatcher = ResetActionDispatcher(
        list_candidates=None, inspect_target=lambda _thread_id: {},
        queue_continue=lambda thread_id: queued.append(thread_id) or {"status": "queued"},
        clock=lambda: T0, save=lambda: True, action_state=lambda: state,
        policies=load_policies({}),
    )
    assert dispatcher.get()["threadIds"] == [ONE]
    assert dispatcher.get()["mode"] == "continue"
    assert dispatcher.get()["armed"] is False
    assert state["ledger"] == {"cycles": [{"cycleId": "old"}]}
    assert queued == []


def test_retained_selection_and_baseline_cannot_override_action():
    h = Harness()
    h.dispatcher.configure("continue", [ONE])
    h.dispatcher.list_candidates = lambda *, limit: []
    h.dispatcher.configure("continue", [ONE], baseline={"mode": "greet", "armed": False, "threadIds": [], "ledger": {}, "armedAt": 42})
    assert h.state["mode"] == "continue" and h.state["armed"] and h.state["threadIds"] == [ONE]
    assert h.state["armedAt"] == 42 and h.state["ledger"] == {"cycles": []}
    with pytest.raises(ValueError):
        h.dispatcher.configure("continue", [TWO])
    h.inspection[ONE] = {"exists": False, "archived": False}
    with pytest.raises(ValueError):
        h.dispatcher.configure("continue", [ONE])


def test_candidate_typeerror_is_not_retried():
    h = Harness()
    calls = []
    def broken(*, limit):
        calls.append(limit)
        raise TypeError("internal error")
    h.dispatcher.list_candidates = broken
    with pytest.raises(RuntimeError):
        h.dispatcher.configure("continue", [ONE])
    assert calls == [100]
