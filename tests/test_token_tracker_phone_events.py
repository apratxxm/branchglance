from datetime import datetime, timezone

from token_tracker.phone_events import NotificationEvents


T0 = datetime(2026, 10, 7, 12, 0, tzinfo=timezone.utc)


def at(seconds=0):
    return T0.timestamp() + seconds


def chat(status="running", **fields):
    return {"provider": "Codex", "id": "thread-1", "title": "A private prompt", "status": status, **fields}


def test_first_healthy_observation_only_seeds_baseline():
    events = NotificationEvents()
    assert events.observe([chat()], at()) == []
    assert events.observe([chat("idle")], at(5)) == []


def test_empty_first_healthy_observation_still_initializes_baseline():
    events = NotificationEvents()
    assert events.observe([], at()) == []
    assert events.observe([chat()], at(1)) == []
    result = events.observe([chat()], at(4))
    assert len(result) == 1 and result[0]["kind"] == "launch"


def test_launches_group_until_fixed_three_second_deadline():
    events = NotificationEvents()
    events.observe([chat("idle")], at())
    assert events.observe([chat()], at(1)) == []
    assert events.observe([chat("idle"), {"provider": "Codex", "id": "thread-2", "status": "running"}], at(3.9)) == []
    emitted = events.observe([chat("idle")], at(4))
    assert len(emitted) == 1
    assert emitted[0]["kind"] == "launch"
    assert emitted[0]["body"] == "2 chats started."


def test_repeated_samples_and_unhealthy_poll_do_not_repeat_or_flush():
    events = NotificationEvents()
    events.observe([chat("idle")], at())
    events.observe([chat()], at(1))
    assert events.observe([chat()], at(2), healthy=False) == []
    assert events.observe([chat()], at(2)) == []
    assert len(events.observe([chat()], at(4))) == 1


def test_disappearance_is_not_completion_but_measured_idle_is():
    events = NotificationEvents()
    events.observe([chat()], at())
    assert events.observe([], at(5)) == []
    assert events.observe([chat("idle", stateSince=at(6))], at(6)) == []
    done = events.observe([chat("idle", stateSince=at(6))], at(9))
    assert len(done) == 1 and done[0]["kind"] == "completion"


def test_failed_actual_state_is_immediate_and_failure_is_separate():
    events = NotificationEvents()
    events.observe([chat()], at())
    result = events.observe([chat("failed", stateSince=at(1))], at(1))
    assert len(result) == 1
    assert result[0]["kind"] == "failure"
    assert result[0]["tag"].startswith("tt-failure-")


def test_new_quiet_chat_does_not_announce_a_launch():
    events = NotificationEvents()
    quiet_child = {"provider": "Codex", "id": "quiet-1", "parentId": "thread-1", "status": "quiet"}
    events.observe([chat("idle")], at())
    assert events.observe([chat("idle"), quiet_child], at(1)) == []
    assert events.observe([chat("idle"), quiet_child], at(4)) == []
    running_child = {**quiet_child, "status": "running", "stateSince": at(5)}
    assert events.observe([chat("idle"), running_child], at(5)) == []
    result = events.observe([chat("idle"), running_child], at(8))
    assert len(result) == 1 and result[0]["kind"] == "launch"


def test_new_run_token_during_active_state_creates_distinct_launch():
    events = NotificationEvents()
    events.observe([chat("running", latestRequestId="request-1")], at())
    assert events.observe([chat("running", latestRequestId="request-2")], at(1)) == []
    result = events.observe([chat("running", latestRequestId="request-2")], at(4))
    assert len(result) == 1 and result[0]["kind"] == "launch"


def test_new_active_child_is_measured_and_grouped():
    events = NotificationEvents()
    events.observe([chat("running")], at())
    child = {"provider": "Codex", "id": "agent-1", "parentId": "thread-1", "status": "running"}
    assert events.observe([chat("running"), child], at(1)) == []
    result = events.observe([chat("running"), child], at(4))
    assert len(result) == 1 and result[0]["kind"] == "launch"
    assert result[0]["body"] == "1 chat started."


def test_question_is_immediate_and_does_not_copy_private_content():
    events = NotificationEvents()
    events.observe([chat()], at())
    row = chat("needs input", stateSince=at(1), pendingQuestion="Reveal my prompt", contextTokens=9000, path="A:\\secret")
    result = events.observe([row], at(1))
    assert len(result) == 1 and result[0]["kind"] == "question"
    assert result[0]["title"] == "A chat needs your input"
    assert "Reveal" not in result[0]["body"] and "9000" not in str(result[0])


def test_distinct_chat_questions_receive_distinct_tags():
    events = NotificationEvents()
    other = {"provider": "Codex", "id": "thread-2", "status": "running"}
    events.observe([chat(), other], at())
    first = {**chat("needs-input", stateSince=at(1)), "questionId": "q1"}
    second = {**other, "status": "needs input", "stateSince": at(1), "questionId": "q2"}
    result = events.observe([first, second], at(1))
    assert len(result) == 2
    assert all(item["tag"].startswith("tt-question-") for item in result)
    assert len({item["tag"] for item in result}) == 2


def test_new_question_while_still_waiting_uses_advanced_state_time():
    events = NotificationEvents()
    events.observe([chat()], at())
    first = chat("needs input", stateSince=at(1), questionId="q1")
    second = chat("needs input", stateSince=at(2), questionId="q2")
    assert len(events.observe([first], at(1))) == 1
    result = events.observe([second], at(2))
    assert len(result) == 1 and result[0]["kind"] == "question"
    assert events.observe([second], at(3)) == []


def test_nonfinite_timestamps_are_ignored_without_exceptions():
    events = NotificationEvents()
    assert events.observe([chat()], "nan") == []
    assert events.observe([chat()], "inf") == []
    events.observe([chat()], at())
    # A malformed optional state/run timestamp is ignored safely; observed status remains usable.
    assert events.observe([chat("idle", stateSince="nan", runStartedAt="inf")], at(1)) == []
    result = events.observe([chat("idle", stateSince="nan", runStartedAt="inf")], at(4))
    assert len(result) == 1 and result[0]["kind"] == "completion"


def test_future_or_out_of_order_state_sample_cannot_rollback_baseline():
    events = NotificationEvents()
    events.observe([chat()], at())
    assert events.observe([chat("idle", stateSince=at(50))], at(2)) == []
    assert events.observe([chat("idle", stateSince=at(-5))], at(1)) == []
    assert events.observe([chat("idle", stateSince=at(3))], at(3)) == []
    assert len(events.observe([chat("idle", stateSince=at(3))], at(6))) == 1


def test_default_payload_is_generic_and_bounded():
    events = NotificationEvents()
    events.observe([chat("idle")], at())
    events.observe([chat()], at(1))
    result = events.observe([chat()], at(4))[0]
    assert result["body"] == "1 chat started."
    assert "private prompt" not in result["body"]
    assert len(result["title"]) <= 80 and len(result["body"]) <= 180
    assert result["url"] == "/phone.html" and len(result["tag"]) <= 32


def test_included_names_preserve_words_and_bound_display_copy():
    events = NotificationEvents(include_names=True)
    events.observe([chat("idle", title="Read files")], at())
    events.observe([chat("running", title="Read files")], at(1))
    result = events.observe([chat("running", title="Read files")], at(4))[0]
    assert result["body"] == "Read files started."
    assert len(result["body"]) <= 180
