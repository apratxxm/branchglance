"""Resumed long-lived Codex thread whose final record is a terminal event.

Built from record shapes only (type and payload type, in order); ids, text and
timings are synthetic. The thread must reduce to idle, never to running.
"""
import json
from datetime import datetime, timedelta, timezone
from unittest import mock

from token_tracker import providers

SHAPES = [
    ("event_msg", "thread_settings_applied"), ("event_msg", "thread_settings_applied"), ("event_msg", "task_started"),
    ("response_item", "message"), ("world_state", None), ("turn_context", None), ("response_item", "message"),
    ("event_msg", "item_completed"), ("event_msg", "item_completed"), ("response_item", "message"),
    ("response_item", "custom_tool_call"), ("token_usage_record", None), ("event_msg", "item_completed"),
    ("event_msg", "item_completed"), ("response_item", "custom_tool_call_output"), ("event_msg", "token_count"),
    ("event_msg", "item_completed"), ("response_item", "reasoning"), ("response_item", "custom_tool_call"),
    ("token_usage_record", None), ("event_msg", "item_completed"), ("event_msg", "item_completed"),
    ("response_item", "custom_tool_call_output"), ("event_msg", "token_count"), ("event_msg", "item_completed"),
    ("response_item", "message"), ("token_usage_record", None), ("event_msg", "token_count"),
    ("event_msg", "task_complete"),
]
BASE = datetime(2026, 11, 1, 19, 33, 51, tzinfo=timezone.utc)


def _stamp(offset):
    return (BASE + timedelta(seconds=offset)).isoformat().replace("+00:00", "Z")


def _rows(shapes, start=0):
    rows = []
    for index, (kind, ptype) in enumerate(shapes, start=start):
        row = {"type": kind, "timestamp": _stamp(index)}
        if kind == "event_msg":
            row["payload"] = {"type": ptype}
            if ptype == "token_count":
                row["payload"]["info"] = {"total_token_usage": {"total_tokens": 1000 + index},
                                          "last_token_usage": {"total_tokens": 500 + index},
                                          "model_context_window": 400000}
        elif kind == "response_item":
            row["payload"] = {"type": ptype}
            if ptype == "custom_tool_call":
                row["payload"].update(name="exec_command", call_id=f"c{index}", input="{}")
            if ptype == "custom_tool_call_output":
                row["payload"].update(call_id=f"c{index - 1}", output="")
        elif kind == "turn_context":
            row["payload"] = {"model": "gpt-6.1-terra"}
        else:
            row["payload"] = {}
        rows.append(row)
    return rows


def _write(path, rows):
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")


def test_resumed_thread_ending_in_terminal_reduces_idle_not_running(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    path = tmp_path / "rollout-shape.jsonl"
    _write(path, _rows(SHAPES))
    now = (BASE + timedelta(hours=3)).timestamp()
    with mock.patch.object(providers.time, "time", lambda: now):
        state = providers._codex_rollout_state(str(path))
        assert state["executionStatus"] == "idle"
        assert state["executionTerminalAt"] == _stamp(len(SHAPES) - 1)
        projected = providers._codex_execution_projection(state, None, False, None, now)
        assert projected["executionStatus"] == "idle"


def test_tail_reads_after_a_later_start_are_running_only_until_their_terminal(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    path = tmp_path / "rollout-shape-2.jsonl"
    _write(path, _rows(SHAPES))
    later = _rows([("event_msg", "task_started"), ("response_item", "custom_tool_call")], start=len(SHAPES))
    _write(path, _rows(SHAPES) + later)
    now = (BASE + timedelta(hours=3)).timestamp()
    with mock.patch.object(providers.time, "time", lambda: now):
        state = providers._codex_rollout_state(str(path))
        assert state["executionStatus"] == "running"
        assert state["executionSince"] == _stamp(len(SHAPES))
