import json
import os
import socket
import sqlite3
import threading
import ctypes
import time
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from pathlib import Path
from types import MappingProxyType

import pytest

from token_tracker import provider_policy, providers, server, widget
from token_tracker.providers import ClaudeDesktopProvider, CodexProvider, CodexUnavailable, _claude_transcript_status, _codex_rollout_status, _human_tokens, _reset_label
from token_tracker.provider_policy import load_policies


@pytest.fixture(autouse=True)
def isolated_agent_homes(tmp_path_factory, monkeypatch):
    """Provider tests must never read the real Claude or Codex transcripts on this machine."""
    homes = tmp_path_factory.mktemp("agent-homes")
    monkeypatch.setenv("CLAUDE_HOME", str(homes / "claude"))
    monkeypatch.setenv("CODEX_HOME", str(homes / "codex"))
    monkeypatch.setattr(widget, "_warm_snapshot", lambda: None)  # no real snapshot builds from widget tests
    monkeypatch.setattr(widget, "_window_state_path", lambda: homes / "window-state.json")  # never the user's saved bounds
    monkeypatch.setattr(widget, "_monitor_work_areas", lambda physical=False: [(0, 0, 2560, 1392)])
    monkeypatch.setattr(widget, "_system_scale", lambda: 1.0)
    monkeypatch.setattr(providers, "_LIVE_INDEX", {})
    monkeypatch.setattr(providers, "ENABLED_PROVIDERS", ("Codex",))
    monkeypatch.setattr(providers, "LIMIT_ONLY_PROVIDERS", ("Claude",))
    monkeypatch.setattr(providers, "POLICIES", load_policies({
        "TOKEN_TRACKER_CODEX_COLLECT": "true", "TOKEN_TRACKER_CODEX_SHOW": "true",
        "TOKEN_TRACKER_CLAUDE_COLLECT": "false", "TOKEN_TRACKER_CLAUDE_SHOW": "false",
    }))


@pytest.fixture
def isolated_token_event_cache(monkeypatch):
    cache = providers.CodexProvider
    monkeypatch.setattr(cache, "_token_event_cache", providers.OrderedDict())
    monkeypatch.setattr(cache, "_token_event_cache_sizes", {})
    monkeypatch.setattr(cache, "_token_event_cache_bytes", 0)
    monkeypatch.setattr(cache, "_token_event_path_locks", providers.OrderedDict())


def test_human_tokens():
    assert _human_tokens(0) == "0"
    assert _human_tokens(1_230) == "1.2K"
    assert _human_tokens(12_300_000) == "12.3M"


def test_codex_agent_lineage_and_period_usage(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    recent = _recent_test_timestamp()
    for identity, parent in [("child", "root"), ("grandchild", "child"), ("unrelated", "missing")]:
        rows = [{"type": "session_meta", "payload": {"id": identity, "parent_thread_id": parent, "agent_path": "/root/" + identity}},
                {"type": "compacted", "timestamp": recent},
                {"type": "turn_context", "payload": {"model": "gpt-5.6-luna", "effort": "low"}}]
        for total in [100, 100, 130, 10]:
            rows.append({"type": "event_msg", "timestamp": recent, "payload": {"type": "token_count", "info": {"total_token_usage": {"total_tokens": total}}}})
        (sessions / (identity + ".jsonl")).write_text("\n".join(json.dumps(row) for row in rows), encoding="utf-8")
    _register_live_desktop_threads(tmp_path / "state_5.sqlite", [
        _live_desktop_row(identity, sessions / (identity + ".jsonl"), parent=parent)
        for identity, parent in [("root", None), ("child", "root"), ("grandchild", "child")]
    ])
    chats = CodexProvider()._with_agents([{"id": "root"}])
    assert {chat["id"] for chat in chats} == {"root", "child", "grandchild"}
    child = next(chat for chat in chats if chat["id"] == "child")
    assert child["parentId"] == "root"
    assert child["reasoningEffort"] == "low"
    assert child["compactions"] == 1
    assert sum(child["dailyTokens"].values()) == 140
    assert CodexProvider._thread({"id": "inactive", "status": "inactive"})["status"] == "idle"


def test_generated_app_observation_uses_unrounded_epoch_but_supplied_future_stays_rejected(monkeypatch):
    captured_epoch = 1_791_618_071.1234567
    rounded_stamp = datetime.fromtimestamp(captured_epoch, timezone.utc).isoformat()
    assert providers._timestamp_epoch(rounded_stamp) > captured_epoch
    monkeypatch.setattr(providers.time, "time", lambda: captured_epoch)
    generated = CodexProvider._thread({"id": "fresh-generated", "status": "inactive"})
    assert generated["status"] == "idle" and generated["statusSource"] == "app"
    supplied = CodexProvider._thread({"id": "future-supplied", "status": "inactive", "statusObservedAt": rounded_stamp})
    assert supplied["status"] == "unavailable" and supplied["statusSource"] == "unavailable"


def test_claude_agents_join_desktop_alias_with_daily_tokens(tmp_path):
    provider = ClaudeDesktopProvider(str(tmp_path))
    provider.root = tmp_path
    sessions = tmp_path / "claude-code-sessions"
    sessions.mkdir()
    (sessions / "parent.json").write_text(json.dumps({"sessionId": "desktop", "cliSessionId": "cli", "title": "Parent"}), encoding="utf-8")
    usage = {"cli": {"status": "idle", "dailyTokens": {"2026-09-03": 10}},
             "agent-child": {"parentId": "cli", "title": "Worker", "status": "needs input", "dailyTokens": {"2026-09-03": 20}, "tokens": 20}}
    chats = provider._chats(usage)
    child = next(chat for chat in chats if chat.get("parentId"))
    assert child["parentId"] == "desktop"
    assert child["title"] == "Worker"
    assert child["dailyTokens"] == {"2026-09-03": 20}


def test_claude_provider_filters_latest_org(tmp_path, monkeypatch):
    package = tmp_path / "Packages" / "Claude_test" / "LocalCache" / "Roaming" / "Claude"
    sessions = package / "claude-code-sessions" / "workspace" / "org"
    sessions.mkdir(parents=True)
    (package / "plan-usage-history.json").write_text(json.dumps({
        "version": 2,
        "samples": [
            {"t": 1000, "org": "old", "u": {"fh": 99, "sd": 99}},
            {"t": 2000, "org": "current", "u": {"fh": 20, "sd": 30}},
        ],
    }), encoding="utf-8")
    (package / "claude_desktop_config.json").write_text("{}", encoding="utf-8")
    (sessions / "one.json").write_text(json.dumps({
        "sessionId": "one", "title": "Real chat", "model": "claude-sonnet-5",
        "lastActivityAt": 2000, "isArchived": False, "completedTurns": 4,
    }), encoding="utf-8")
    provider = ClaudeDesktopProvider(str(tmp_path))
    snap = provider.snapshot()
    assert snap["fiveHour"]["percent"] == 20
    assert snap["weekly"]["percent"] == 30
    assert snap["limitsUpdatedAt"].startswith("1970-01-01T00:00:02")
    assert snap["chats"][0]["title"] == "Real chat"


def test_usage_limits_preserve_zero_and_expose_absent_values_as_unknown(tmp_path):
    package = tmp_path / "Packages" / "Claude_test" / "LocalCache" / "Roaming" / "Claude"
    package.mkdir(parents=True)
    (package / "plan-usage-history.json").write_text(json.dumps({"samples": [{"t": 1, "org": "o", "u": {"fh": 0}}]}), encoding="utf-8")
    provider = ClaudeDesktopProvider(str(tmp_path))
    snap = provider.snapshot()
    assert snap["fiveHour"]["percent"] == 0
    assert snap["weekly"]["percent"] is None

    assert CodexProvider._telemetry_limit({"primary": {"usedPercent": 0}}, "primary")["percent"] == 0
    assert CodexProvider._telemetry_limit({"primary": {}}, "primary")["percent"] is None


def test_claude_usage_keeps_last_numeric_sample_when_new_cache_row_is_empty(tmp_path):
    package = tmp_path / "Packages" / "Claude_test" / "LocalCache" / "Roaming" / "Claude"
    package.mkdir(parents=True)
    (package / "plan-usage-history.json").write_text(json.dumps({"samples": [
        {"t": 1000, "org": "current", "u": {"fh": 17, "sd": 31}},
        {"t": 2000, "org": "current", "u": {}},
    ]}), encoding="utf-8")
    provider = ClaudeDesktopProvider(str(tmp_path))
    _samples, latest = provider._usage()
    assert latest == {"t": 1000, "org": "current", "u": {"fh": 17, "sd": 31}}


def test_claude_transcripts_dedupe_join_price_and_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(providers, "_local_today", lambda: date(2026, 9, 4))
    package = tmp_path / "Packages" / "Claude_test" / "LocalCache" / "Roaming" / "Claude"
    sessions = package / "claude-code-sessions"; sessions.mkdir(parents=True)
    (package / "plan-usage-history.json").write_text(json.dumps({"samples": [{"t": 2_000, "org": "o", "u": {"fh": 1, "sd": 2}}]}), encoding="utf-8")
    (sessions / "chat.json").write_text(json.dumps({"sessionId": "chat", "cliSessionId": "cli", "title": "Joined", "lastActivityAt": 2_000}), encoding="utf-8")
    transcript = tmp_path / "projects" / "project"; transcript.mkdir(parents=True)
    event = {"timestamp": "2026-09-03T10:00:00Z", "message": {"id": "dedupe", "model": "claude-sonnet-5", "usage": {"input_tokens": 100, "output_tokens": 20, "cache_read_input_tokens": 50, "cache_creation": {"ephemeral_5m_input_tokens": 10, "ephemeral_1h_input_tokens": 5}}}}
    (transcript / "cli.jsonl").write_text(json.dumps(event) + "\n" + json.dumps(event), encoding="utf-8")
    provider = ClaudeDesktopProvider(str(tmp_path)); provider.transcript_root = tmp_path / "projects"
    snap = provider.snapshot()
    assert snap["lifetimeTokens"] == 185
    assert snap["chats"][0]["tokens"] == 185
    assert snap["cache"]["read"] == 50
    assert snap["cache"]["uncachedInput"] == 100
    assert snap["cache"]["cacheCreation"] == 15
    assert snap["cache"]["output"] == 20
    assert snap["cache"]["percent"] == 30
    assert snap["cache"]["pricingCoverage"] == 100
    assert snap["chats"][0]["contextTokens"] == 165
    assert snap["chats"][0]["contextWindow"] == 1_000_000


def test_duration_and_pricing_coverage():
    assert providers._duration_since(0) == "just now"
    assert providers._duration_since(42) == "42m"
    assert providers._duration_since(2_253) == "1d 13h"
    cost = providers._costs({"total_tokens": 2_000_000}, {"gpt-5.6-terra": {"input_tokens": 1_000_000, "cached_tokens": 500_000, "output_tokens": 500_000, "total_tokens": 2_000_000}}, providers.CODEX_PRICING)
    assert cost["apiEquivalentCost"] == 7.1
    assert cost["cacheSavings"] == .9
    assert cost["pricingCoverage"] == 100
    assert providers._price_for("claude-opus-4-8", providers.CLAUDE_PRICING) == providers.CLAUDE_PRICING["opus-4.8"]
    assert providers._price_for("claude-haiku-4-5-20251001", providers.CLAUDE_PRICING) == providers.CLAUDE_PRICING["haiku-4.5"]
    assert providers._claude_context_window("claude-opus-4-8") == 1_000_000
    assert providers._claude_context_window("claude-haiku-4-5-20251001") == 200_000


def test_activity_dates_use_the_local_desktop_calendar(monkeypatch):
    ist = timezone(timedelta(hours=5, minutes=30))
    monkeypatch.setattr(providers, "_local_timezone", lambda: ist)
    # This event is Sep 3 in UTC but Sep 4 on the user's desktop calendar.
    assert providers._local_date_from_timestamp("2026-09-03T20:00:00Z") == "2026-09-04"


def test_codex_usage_buckets_are_normalized_to_the_local_calendar(monkeypatch):
    ist = timezone(timedelta(hours=5, minutes=30))
    monkeypatch.setattr(providers, "_local_timezone", lambda: ist)
    class Client:
        def snapshot(self):
            return type("Snapshot", (), {
                "rate_limits": {"rateLimits": {}}, "usage": {"summary": {}, "dailyUsageBuckets": [
                    {"startDate": "2026-09-03T20:00:00Z", "tokens": 7},
                ]}, "threads": [],
            })()
    provider = CodexProvider(Client())
    monkeypatch.setattr(provider, "_local_usage", lambda **_kwargs: {"daily": {}, "totals": {}, "models": {}, "rate": None})
    snap = provider.snapshot()
    assert snap["activity"] == [{"date": "2026-09-04", "value": 7}]
    assert snap["tracked31Days"] == 7
    assert snap["activityNote"] == "Usage is grouped from Codex app-server daily buckets."


def test_activity_age_uses_days_then_a_short_local_date(monkeypatch):
    ist = timezone(timedelta(hours=5, minutes=30))
    monkeypatch.setattr(providers, "_local_timezone", lambda: ist)
    now = datetime(2026, 9, 4, 12, tzinfo=timezone.utc)
    assert providers._activity_age("2026-09-04T11:59:30Z", now) == "just now"
    assert providers._activity_age("2026-09-03T09:30:00Z", now) == "1d 2h since activity"
    assert providers._activity_age("2026-08-20T12:00:00Z", now) == "Aug 20"


def test_claude_transcript_filters_old_rows_and_counts_missing_desktop_turns(tmp_path):
    project = tmp_path / "projects" / "p"; project.mkdir(parents=True)
    old = {"timestamp": "2020-01-01T00:00:00Z", "message": {"id": "old", "usage": {"input_tokens": 999}}}
    recent = {"timestamp": datetime.now(timezone.utc).isoformat(), "message": {"id": "recent", "model": "claude-haiku-4-5-20251001", "usage": {"input_tokens": 7}}}
    path = project / "joined.jsonl"; path.write_text(json.dumps(old) + "\n" + json.dumps(recent), encoding="utf-8")
    provider = ClaudeDesktopProvider(str(tmp_path)); provider.transcript_root = tmp_path / "projects"
    usage = provider._transcript_usage()
    assert usage["totals"]["total_tokens"] == 7
    assert usage["sessions"]["joined"]["turns"] == 1


def test_claude_transcript_usage_uses_fixed_local_31_day_calendar_window(tmp_path, monkeypatch):
    project = tmp_path / "projects" / "p"; project.mkdir(parents=True)
    monkeypatch.setattr(providers, "_local_today", lambda: date(2026, 9, 4))
    ist = timezone(timedelta(hours=5, minutes=30))
    monkeypatch.setattr(providers, "_local_timezone", lambda: ist)
    def event(identifier, timestamp, tokens, model="claude-sonnet-5"):
        return {"timestamp": timestamp, "message": {"id": identifier, "model": model, "usage": {"input_tokens": tokens}}}
    (project / "window.jsonl").write_text("\n".join(json.dumps(row) for row in [
        event("oldest", "2026-08-05T00:30:00Z", 7),  # Aug 5 local: included
        event("prior", "2026-08-04T18:29:00Z", 11),  # Aug 4 local: excluded
        event("future", "2026-09-04T18:30:00Z", 13, "claude-haiku-4-5"),  # Sep 5 local: excluded
    ]), encoding="utf-8")
    provider = ClaudeDesktopProvider(str(tmp_path)); provider.transcript_root = tmp_path / "projects"
    usage = provider._transcript_usage()
    assert dict(usage["daily"]) == {"2026-08-05": 7}
    assert usage["totals"]["total_tokens"] == 7
    assert dict(usage["models"]) == {"claude-sonnet-5": {"input_tokens": 7, "output_tokens": 0, "cached_tokens": 0, "cache_5m_tokens": 0, "cache_1h_tokens": 0, "total_tokens": 7}}


def test_claude_synthetic_rows_do_not_replace_concrete_chat_presentation(tmp_path):
    project = tmp_path / "projects" / "p"; project.mkdir(parents=True)
    timestamp = datetime.now(timezone.utc)
    concrete = {
        "timestamp": timestamp.isoformat(),
        "message": {"id": "concrete", "model": "claude-opus-5", "usage": {"input_tokens": 100, "cache_read_input_tokens": 25}},
    }
    synthetic = {
        "timestamp": (timestamp + timedelta(seconds=1)).isoformat(),
        "message": {"id": "synthetic", "model": "<synthetic>", "usage": {"input_tokens": 20}},
    }
    path = project / "session.jsonl"
    path.write_text(json.dumps(concrete) + "\n" + json.dumps(synthetic), encoding="utf-8")
    provider = ClaudeDesktopProvider(str(tmp_path)); provider.transcript_root = tmp_path / "projects"
    usage = provider._transcript_usage()
    session = usage["sessions"]["session"]
    assert usage["totals"]["total_tokens"] == 145
    assert session["turns"] == 2
    assert session["model"] == "claude-opus-5"
    assert session["contextTokens"] == 125


def test_claude_cache_breakdown_does_not_double_count_or_overstate_savings():
    totals = {"input_tokens": 100, "cached_tokens": 0, "cache_5m_tokens": 0, "cache_1h_tokens": 14_097, "total_tokens": 14_197}
    models = {"claude-sonnet-5": {**totals}}
    cost = providers._costs(totals, models, providers.CLAUDE_PRICING, claude=True)
    # The aggregate creation field is not counted again when explicit 1h data is available.
    assert cost["apiEquivalentCost"] == round((100 * 2 + 14_097 * 4) / 1_000_000, 4)
    assert cost["cacheSavings"] < 0


def test_claude_transcript_file_cache_reuses_and_invalidates(tmp_path):
    path = tmp_path / "sample.jsonl"
    first = {"message": {"id": "one", "usage": {"input_tokens": 1}}}
    second = {"message": {"id": "two", "usage": {"input_tokens": 1}}}
    path.write_text(json.dumps(first) + "\n", encoding="utf-8")
    original = ClaudeDesktopProvider._file_usage(path)
    assert ClaudeDesktopProvider._file_usage(path) is original
    path.write_text(json.dumps(first) + "\n" + json.dumps(second) + "\n", encoding="utf-8")
    assert len(ClaudeDesktopProvider._file_usage(path)) == 2


def test_reset_label_without_value():
    assert _reset_label(None) == "reset unavailable"
    assert _reset_label(None, 0) == "available now"


def test_claude_reset_reads_recent_electron_local_storage(tmp_path):
    package = tmp_path / "Packages" / "Claude_test" / "LocalCache" / "Roaming" / "Claude"
    storage = package / "Local Storage" / "leveldb"
    storage.mkdir(parents=True)
    future = int(providers.time.time()) + 7200
    (storage / "000123.ldb").write_bytes(f"noise resetsAt\x00:{future} trailing".encode())
    provider = ClaudeDesktopProvider(str(tmp_path))
    assert provider._reset_at() == future


def test_codex_token_event_reads_only_telemetry(tmp_path):
    rollout = tmp_path / "rollout.jsonl"
    rollout.write_text(
        "\n".join([
            json.dumps({"type": "response_item", "payload": {"text": "private prompt"}}),
            json.dumps({
                "timestamp": "2026-09-02T12:00:00Z",
                "type": "event_msg",
                "payload": {"type": "token_count", "info": {
                    "total_token_usage": {"input_tokens": 200, "cached_input_tokens": 150, "total_tokens": 240},
                    "last_token_usage": {"total_tokens": 80},
                    "model_context_window": 400,
                }},
            }),
        ]),
        encoding="utf-8",
    )
    event = CodexProvider._last_token_event(str(rollout))
    assert event["total_token_usage"]["total_tokens"] == 240
    assert "private prompt" not in json.dumps(event)


def test_codex_model_falls_back_to_full_rollout_scan_when_tail_lacks_context(tmp_path):
    rollout = tmp_path / "large-rollout.jsonl"
    context = json.dumps({"type": "turn_context", "payload": {"model": "gpt-5.6-sol"}})
    filler = json.dumps({"type": "response_item", "payload": {"text": "x" * 600_000}})
    token = json.dumps({"type": "event_msg", "payload": {"type": "token_count", "info": {"total_token_usage": {"total_tokens": 9}}}})
    rollout.write_text("\n".join((context, filler, token)), encoding="utf-8")
    event = CodexProvider._last_token_event(str(rollout))
    assert event["model"] == "gpt-5.6-sol"
    assert event["total_token_usage"]["total_tokens"] == 9


def test_codex_local_usage_uses_counter_deltas_per_event_date_and_model(tmp_path, monkeypatch):
    monkeypatch.setattr(providers, "_local_today", lambda: date(2026, 9, 4))
    sessions = tmp_path / "sessions" / "2026" / "09" / "04"; sessions.mkdir(parents=True)
    rollout = sessions / "rollout.jsonl"
    def context(model): return {"type": "turn_context", "payload": {"model": model}}
    def count(timestamp, total, input_tokens, output_tokens):
        return {"timestamp": timestamp, "type": "event_msg", "payload": {"type": "token_count", "info": {
            "total_token_usage": {"total_tokens": total, "input_tokens": input_tokens, "output_tokens": output_tokens},
        }}}
    rollout.write_text("\n".join(json.dumps(row) for row in [
        context("gpt-5.6-luna"), count("2026-09-03T12:00:00Z", 100, 80, 20),
        count("2026-09-03T13:00:00Z", 100, 80, 20),  # duplicate cumulative counter
        context("gpt-5.6-terra"), count("2026-09-04T12:00:00Z", 175, 130, 45),
        count("2026-09-04T13:00:00Z", 15, 10, 5),  # counter reset, new epoch
    ]), encoding="utf-8")
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    usage = CodexProvider()._local_usage()
    assert dict(usage["daily"]) == {"2026-09-03": 100, "2026-09-04": 90}
    assert usage["totals"]["total_tokens"] == 190
    assert usage["models"]["gpt-5.6-luna"]["total_tokens"] == 100
    assert usage["models"]["gpt-5.6-terra"]["total_tokens"] == 90


def test_codex_local_usage_marks_observed_requests_and_subagent_share(tmp_path, monkeypatch):
    sessions = tmp_path / "sessions" / "2026" / "09" / "04"; sessions.mkdir(parents=True)
    def records(model, total, parent=None):
        meta = {"type": "session_meta", "payload": {"id": "agent" if parent else "root"}}
        if parent:
            meta["payload"]["parent_thread_id"] = parent
        return [meta, {"type": "turn_context", "payload": {"model": model}},
                {"timestamp": datetime.now(timezone.utc).isoformat(), "type": "event_msg",
                 "payload": {"type": "token_count", "info": {"total_token_usage": {"total_tokens": total}}}}]
    (sessions / "root.jsonl").write_text("\n".join(json.dumps(row) for row in records("gpt-5.6-sol", 100)), encoding="utf-8")
    (sessions / "agent.jsonl").write_text("\n".join(json.dumps(row) for row in records("gpt-5.6-sol", 40, "root")), encoding="utf-8")
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    model = CodexProvider()._local_usage()["models"]["gpt-5.6-sol"]
    assert model["total_tokens"] == 140
    assert model["observed_requests"] == 2
    assert model["subagent_tokens"] == 40
    assert model["subagent_requests"] == 1


def test_codex_token_event_cache_reuses_unchanged_file(tmp_path):
    rollout = tmp_path / "rollout.jsonl"
    rollout.write_text(json.dumps({"type": "event_msg", "payload": {"info": {"total_token_usage": {"total_tokens": 1}}}}), encoding="utf-8")
    first = CodexProvider._token_events(rollout)
    assert CodexProvider._token_events(rollout) is first


def test_codex_token_event_compact_cache_preserves_consumer_projection(tmp_path, isolated_token_event_cache):
    rollout = tmp_path / "compact-events.jsonl"
    shared_limits = {"primary": {"usedPercent": 12, "resetsAt": 1_800_000_000}}
    rows = [
        {"type": "turn_context", "payload": {"model": "gpt-6-luna"}},
        {"type": "event_msg", "timestamp": "2026-10-20T10:00:00Z", "payload": {
            "type": "token_count", "rate_limits": shared_limits,
            "info": {"total_token_usage": {"input_tokens": 4, "total_tokens": 5},
                     "last_token_usage": {"input_tokens": 4, "total_tokens": 5}},
        }},
        {"type": "event_msg", "timestamp": "2026-10-20T10:00:01Z", "payload": {
            "type": "token_count", "rate_limits": dict(shared_limits),
            "info": {"total_token_usage": {"input_tokens": 8, "total_tokens": 9, "vendor_counter": 2}},
        }},
    ]
    _rollout(rollout, rows)

    events = CodexProvider._token_events(rollout)

    assert isinstance(events, providers._CompactTokenEvents)
    assert list(events) == [
        {"timestamp": "2026-10-20T10:00:00Z", "rate_limits": shared_limits, "model": "gpt-6-luna",
         "usage": {"input_tokens": 4, "total_tokens": 5},
         "last": {"input_tokens": 4, "total_tokens": 5}, "inherited": False},
        {"timestamp": "2026-10-20T10:00:01Z", "rate_limits": shared_limits, "model": "gpt-6-luna",
         "usage": {"input_tokens": 8, "total_tokens": 9, "vendor_counter": 2},
         "last": None, "inherited": False},
    ]
    events[0]["usage"]["total_tokens"] = 100
    assert events[0]["usage"]["total_tokens"] == 5
    cache_key = next(iter(CodexProvider._token_event_cache))
    compact_bytes = CodexProvider._estimate_token_event_cache_entry_bytes(cache_key, events)
    expanded_bytes = CodexProvider._estimate_token_event_cache_entry_bytes(cache_key, list(events))
    assert compact_bytes < expanded_bytes


def test_codex_live_cache_keeps_oversized_projection_complete_but_uncached(tmp_path, monkeypatch, isolated_token_event_cache):
    rollout = tmp_path / "oversized-projection.jsonl"
    stamp = _recent_test_timestamp()
    records = [{"type": "event_msg", "timestamp": stamp, "payload": {"type": "token_count", "info": {
        "total_token_usage": {"input_tokens": 100, "total_tokens": total},
    }}} for total in (120, 180, 250)]
    _rollout(rollout, records)
    monkeypatch.setattr(CodexProvider, "_estimate_token_event_cache_entry_bytes",
                        classmethod(lambda cls, key, events: cls._TOKEN_EVENT_CACHE_MAX_BYTES + 1))

    events = CodexProvider._token_events(rollout)

    assert [event["usage"]["total_tokens"] for event in events] == [120, 180, 250]
    assert not any(key[0] == str(rollout.resolve()) for key in CodexProvider._token_event_cache)
    assert CodexProvider._token_event_cache_bytes == 0


def test_codex_token_event_cache_evicts_by_48mib_budget_and_accounts_bytes(
    tmp_path, monkeypatch, isolated_token_event_cache
):
    cache = CodexProvider
    assert cache._TOKEN_EVENT_CACHE_MAX_BYTES == 48 * 1024 * 1024
    rollouts = [tmp_path / f"cache-{name}.jsonl" for name in ("first", "second", "third")]
    stamp = _recent_test_timestamp()
    for index, rollout in enumerate(rollouts, 1):
        _rollout(rollout, [{"type": "event_msg", "timestamp": stamp, "payload": {"type": "token_count", "info": {
            "total_token_usage": {"total_tokens": index},
        }}}])
    retained_size = {"cache-first.jsonl": 10 * 1024 * 1024,
                     "cache-second.jsonl": 20 * 1024 * 1024,
                     "cache-third.jsonl": 25 * 1024 * 1024}
    monkeypatch.setattr(cache, "_estimate_token_event_cache_entry_bytes",
                        classmethod(lambda cls, key, events: retained_size[Path(key[0]).name]))

    cache._token_events(rollouts[0])
    cache._token_events(rollouts[1])
    cache._token_events(rollouts[0])  # Touch first so second is least recently used.
    cache._token_events(rollouts[2])  # 55 MiB forces only the second entry out.

    resolved = [str(path.resolve()) for path in rollouts]
    retained_keys = set(cache._token_event_cache)
    assert any(key[0] == resolved[0] for key in retained_keys)
    assert not any(key[0] == resolved[1] for key in retained_keys)
    assert any(key[0] == resolved[2] for key in retained_keys)
    assert cache._token_event_cache_bytes == 35 * 1024 * 1024
    assert cache._token_event_cache_bytes == sum(cache._token_event_cache_sizes.values())


def test_codex_token_event_cache_shares_same_path_parse_between_callers(
    tmp_path, monkeypatch, isolated_token_event_cache
):
    from concurrent.futures import ThreadPoolExecutor

    rollout = tmp_path / "singleflight.jsonl"
    stamp = _recent_test_timestamp()
    _rollout(rollout, [{"type": "event_msg", "timestamp": stamp, "payload": {"type": "token_count", "info": {
        "total_token_usage": {"total_tokens": 7},
    }}}])
    original_open = Path.open
    open_count = [0]
    open_lock = threading.Lock()

    def counted_open(path, *args, **kwargs):
        if path == rollout:
            with open_lock:
                open_count[0] += 1
        return original_open(path, *args, **kwargs)

    original_acquire = CodexProvider._acquire_token_event_path_lock.__func__
    second_waiting = threading.Event()
    arrivals = [0]
    arrival_lock = threading.Lock()

    def counted_acquire(cls, path):
        with arrival_lock:
            arrivals[0] += 1
            if arrivals[0] == 2:
                second_waiting.set()
        return original_acquire(cls, path)

    estimate_entered = threading.Event()
    release_estimate = threading.Event()
    original_estimate = CodexProvider._estimate_token_event_cache_entry_bytes.__func__

    def gated_estimate(cls, key, events):
        estimate_entered.set()
        assert release_estimate.wait(2)
        return original_estimate(cls, key, events)

    monkeypatch.setattr(Path, "open", counted_open)
    monkeypatch.setattr(CodexProvider, "_acquire_token_event_path_lock", classmethod(counted_acquire))
    monkeypatch.setattr(CodexProvider, "_estimate_token_event_cache_entry_bytes", classmethod(gated_estimate))
    pool = ThreadPoolExecutor(max_workers=2)
    try:
        first = pool.submit(CodexProvider._token_events, rollout)
        assert estimate_entered.wait(1)
        second = pool.submit(CodexProvider._token_events, rollout)
        assert second_waiting.wait(1)
    finally:
        release_estimate.set()
    try:
        first_events = first.result(timeout=2)
        second_events = second.result(timeout=2)
    finally:
        pool.shutdown(wait=True)

    assert first_events is second_events
    assert first_events[0]["usage"]["total_tokens"] == 7
    assert open_count == [1]


def test_codex_local_usage_retains_full_fingerprint_and_prunes_stale_cache_accounting(
    tmp_path, monkeypatch, isolated_token_event_cache
):
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    rollout = sessions / "kept.jsonl"
    stamp = _recent_test_timestamp()
    _rollout(rollout, [{"type": "event_msg", "timestamp": stamp, "payload": {"type": "token_count", "info": {
        "total_token_usage": {"total_tokens": 5},
    }}}])
    cache = CodexProvider
    existing = cache._token_events(rollout)
    full_key = next(iter(cache._token_event_cache))
    stale_same_file = full_key[:3]
    stale_missing_file = (str((sessions / "removed.jsonl").resolve()), 11, 12, 13, 14, 15)
    cache._token_event_cache[stale_same_file] = []
    cache._token_event_cache[stale_missing_file] = []
    cache._token_event_cache_sizes[stale_same_file] = 101
    cache._token_event_cache_sizes[stale_missing_file] = 202
    cache._token_event_cache_bytes += 303
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))

    CodexProvider()._local_usage()

    assert cache._token_event_cache.get(full_key) is existing
    assert stale_same_file not in cache._token_event_cache
    assert stale_missing_file not in cache._token_event_cache
    assert cache._token_event_cache_bytes == cache._token_event_cache_sizes[full_key]
    assert cache._token_event_cache_bytes == sum(cache._token_event_cache_sizes.values())


def test_codex_local_usage_uses_pre_window_counter_as_a_baseline(tmp_path, monkeypatch):
    sessions = tmp_path / "sessions"; sessions.mkdir()
    rollout = sessions / "rollout.jsonl"
    def count(timestamp, total):
        return {"timestamp": timestamp, "type": "event_msg", "payload": {"info": {
            "total_token_usage": {"total_tokens": total, "input_tokens": total},
        }}}
    rollout.write_text("\n".join(json.dumps(row) for row in [
        count("2026-08-04T12:00:00Z", 100),  # outside the last-31-date window
        count("2026-09-03T12:00:00Z", 150),
    ]), encoding="utf-8")
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    monkeypatch.setattr(providers, "_local_today", lambda: date(2026, 9, 4))
    usage = CodexProvider()._local_usage()
    assert dict(usage["daily"]) == {"2026-09-03": 50}
    assert usage["totals"]["total_tokens"] == 50


def test_iso_from_ms_and_reset_label(monkeypatch):
    assert providers._iso_from_ms(None) is None
    assert providers._iso_from_ms(1_000).startswith("1970-01-01T00:00:01")
    monkeypatch.setattr(providers.time, "time", lambda: 1_000)
    assert _reset_label(1_000) == "resets 0m"
    assert _reset_label(1_000 + 3_660) == "resets 1h 01m"
    assert _reset_label(1_000 + 90_000) == "resets 1d 1h"


def test_codex_thread_normalizes_status_and_context(tmp_path):
    rollout = tmp_path / "rollout.jsonl"
    rollout.write_text(json.dumps({
        "timestamp": "2026-09-02T12:00:00Z", "type": "event_msg", "payload": {"type": "token_count", "info": {
            "total_token_usage": {"total_tokens": 480}, "last_token_usage": {"total_tokens": 120},
            "model_context_window": 400,
        }},
    }) + "\n" + json.dumps({"type": "turn_context", "payload": {"model": "gpt-5.6-terra"}}), encoding="utf-8")
    thread = CodexProvider._thread({"id": "task", "status": "needs attention", "modelProvider": "openai", "path": str(rollout)})
    assert thread["status"] == "needs input"
    # The only turn_context follows this sample, so the model cannot be
    # paired with the earlier occupancy/capacity measurement.
    assert thread["contextPercent"] is None
    assert thread["contextTokens"] is None
    assert thread["tokens"] == 480
    assert thread["model"] == "gpt-5.6-terra"


def test_codex_chat_folder_is_not_labeled_as_project_workspace(tmp_path):
    workspace = tmp_path / "Documents" / "Codex" / "2026-10-06" / "x20-new-chat"
    workspace.mkdir(parents=True)
    assert providers._workspace_kind(str(workspace)) == "codex-chat"
    (workspace / ".git").mkdir()
    assert providers._workspace_kind(str(workspace)) is None


def test_codex_thread_derives_turns_or_leaves_them_unknown(tmp_path):
    rollout = tmp_path / "rollout.jsonl"
    rollout.write_text("\n".join((
        json.dumps({"type": "turn_context", "payload": {}}),
        json.dumps({"type": "turn_context", "payload": {}}),
    )), encoding="utf-8")
    assert CodexProvider._thread({"id": "task", "turnCount": 0, "path": str(rollout)})["turns"] == 2
    assert CodexProvider._thread({"id": "task", "turnCount": 0})["turns"] is None


def test_codex_thread_uses_freshest_timestamp_and_preview_title():
    thread = CodexProvider._thread({
        "id": "task",
        "name": None,
        "preview": "  Build   the token tracker with correct live ordering " + ("x" * 80),
        "recencyAt": 100,
        "updatedAt": 200,
    })
    assert thread["updatedAt"] == "1970-01-01T00:03:20+00:00"
    assert thread["title"].startswith("Build the token tracker")
    assert len(thread["title"]) == 72
    assert thread["title"].endswith("…")


def test_chat_status_uses_persisted_state_signals(tmp_path):
    claude = tmp_path / "claude.jsonl"
    claude.write_text(json.dumps({"type": "assistant", "message": {"content": [
        {"type": "tool_use", "name": "AskUserQuestion"}
    ]}}), encoding="utf-8")
    assert _claude_transcript_status(claude) == "needs input"
    claude.write_text(claude.read_text(encoding="utf-8") + "\n" + json.dumps({"type": "result"}), encoding="utf-8")
    assert _claude_transcript_status(claude) == "idle"

    codex = tmp_path / "codex.jsonl"
    codex.write_text(json.dumps({"type": "response_item", "payload": {
        "type": "function_call", "name": "request_user_input"
    }}), encoding="utf-8")
    assert _codex_rollout_status(str(codex)) == "needs input"
    codex.write_text(codex.read_text(encoding="utf-8") + "\n" + json.dumps({
        "type": "event_msg", "payload": {"type": "task_complete"}
    }), encoding="utf-8")
    assert _codex_rollout_status(str(codex)) == "idle"


def test_snapshot_cache_honors_ttl_and_forced_refresh(monkeypatch):
    calls = []
    clock = [10.0]
    monkeypatch.setattr(server.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(server, "build_snapshot", lambda: calls.append(len(calls)) or {"n": len(calls)})
    cache = server.SnapshotCache(ttl=1.0)
    assert cache.get()["n"] == 1
    assert cache.get()["n"] == 1
    clock[0] = 11.1
    assert cache.get()["n"] == 2
    assert cache.get(force=True)["n"] == 3
    assert len(calls) == 3


def test_snapshot_cache_serializes_concurrent_builds(monkeypatch):
    started = threading.Event()
    release = threading.Event()
    calls = []
    def build():
        calls.append(1)
        started.set()
        release.wait(1)
        return {"value": 1}
    monkeypatch.setattr(server, "build_snapshot", build)
    cache = server.SnapshotCache(ttl=30)
    first = threading.Thread(target=cache.get)
    second = threading.Thread(target=cache.get)
    first.start(); started.wait(1); second.start(); release.set()
    first.join(1); second.join(1)
    assert len(calls) == 1


def test_live_cache_bounds_cold_wait_and_shares_one_builder(monkeypatch):
    entered = threading.Event()
    release = threading.Event()
    calls = []

    def build():
        calls.append(1)
        entered.set()
        assert release.wait(2)
        return {"live": True}

    monkeypatch.setattr(server, "live_state", build)
    cache = server.LiveCache()

    def get_timed():
        started = time.perf_counter()
        value = cache.get()
        return value, time.perf_counter() - started

    with pytest.MonkeyPatch.context() as _:
        from concurrent.futures import ThreadPoolExecutor

        pool = ThreadPoolExecutor(max_workers=2)
        try:
            first = pool.submit(get_timed)
            assert entered.wait(1)
            second = pool.submit(get_timed)
            first_result = first.result(timeout=1)
            second_result = second.result(timeout=1)
            assert first_result[0] is None and second_result[0] is None
            assert first_result[1] <= server.LIVE_COLD_WAIT_SECONDS + 0.15
            assert second_result[1] <= server.LIVE_COLD_WAIT_SECONDS + 0.15
            assert calls == [1]
        finally:
            release.set()
            pool.shutdown(wait=True)
    with cache.condition:
        assert cache.condition.wait_for(lambda: not cache.building, timeout=1)
    assert cache.value == {"live": True}


def test_live_cache_stale_api_returns_503_without_advancing_timestamp(monkeypatch):
    cache = server.LiveCache()
    cache.value = {"old": True}
    cache.completed_at = time.monotonic() - server.LIVE_MAX_AGE_SECONDS - 0.01
    old_completed_at = cache.completed_at
    cache.retry_after = time.monotonic() + 60
    monkeypatch.setattr(server, "LIVE_CACHE", cache)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    connection = HTTPConnection("127.0.0.1", httpd.server_port, timeout=2)
    try:
        connection.request("GET", "/api/live")
        response = connection.getresponse()
        assert response.status == 503
        assert json.loads(response.read())["ok"] is False
        assert cache.completed_at == old_completed_at
    finally:
        connection.close()
        httpd.shutdown()
        httpd.server_close()
        thread.join(1)


def test_live_cache_error_preserves_prior_timestamp_and_cools_down_for_one_second(monkeypatch):
    now = [100.0]
    calls = []
    monkeypatch.setattr(server.time, "monotonic", lambda: now[0])

    def broken_live_state():
        calls.append(now[0])
        raise RuntimeError("live unavailable")

    monkeypatch.setattr(server, "live_state", broken_live_state)

    class InlineThread:
        def __init__(self, *, target, **_kwargs):
            self.target = target

        def start(self):
            self.target()

    monkeypatch.setattr(server.threading, "Thread", InlineThread)
    cache = server.LiveCache()
    cache.value = {"old": True}
    cache.completed_at = 95.0
    cache._refresh()
    assert cache.completed_at == 95.0
    assert cache.retry_after == 101.0

    now[0] = 100.999
    assert cache.get() is None
    assert calls == [100.0]
    assert cache.completed_at == 95.0

    now[0] = 101.0
    assert cache.get() is None
    assert calls == [100.0, 101.0]
    assert cache.completed_at == 95.0
    assert cache.retry_after == 102.0


def test_http_health_stats_and_force_refresh(monkeypatch):
    calls = []
    monkeypatch.setattr(server, "CACHE", server.SnapshotCache(ttl=60))
    monkeypatch.setattr(server, "build_snapshot", lambda: calls.append(1) or {"generatedAt": "now"})
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    connection = HTTPConnection("127.0.0.1", httpd.server_port, timeout=2)
    try:
        connection.request("GET", "/healthz")
        assert connection.getresponse().read() == b'{"ok":true}'
        connection.request("GET", "/api/stats")
        first = json.loads(connection.getresponse().read())
        assert first["generatedAt"] == "now"
        metadata = first["snapshotCache"]
        assert set(metadata) == {"generation", "builtAt", "ageSeconds", "building", "stale",
                                 "lastFailureAt", "lastFailureCategory"}
        assert metadata["generation"] == 1 and metadata["building"] is False and metadata["stale"] is False
        assert metadata["builtAt"] and metadata["ageSeconds"] >= 0
        assert metadata["lastFailureAt"] is None and metadata["lastFailureCategory"] is None
        connection.request("GET", "/api/stats")
        connection.getresponse().read()
        connection.request("GET", "/api/stats?refresh=1")
        connection.getresponse().read()
        assert len(calls) == 2
    finally:
        connection.close()
        httpd.shutdown(); httpd.server_close(); thread.join(1)


def test_http_limit_reset_status_endpoint(monkeypatch):
    class ResetService:
        def status(self):
            return {"enabled": True, "running": True, "events": [{"event": "service_started"}]}

    monkeypatch.setattr(server.Handler, "reset_service", ResetService())
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    connection = HTTPConnection("127.0.0.1", httpd.server_port, timeout=2)
    try:
        connection.request("GET", "/api/limit-reset")
        response = connection.getresponse()
        assert response.status == 200
        assert json.loads(response.read()) == {
            "enabled": True, "running": True, "events": [{"event": "service_started"}],
        }
    finally:
        connection.close()
        httpd.shutdown(); httpd.server_close(); thread.join(1)


def test_notch_bounds_center_and_fit():
    assert widget._notch_bounds((0, 0, 1920, 1080)) == (680, 148, 620, 0)
    assert widget._notch_bounds((-1280, 20, 0, 740), True) == (680, 560, -980, 20)
    assert widget._notch_bounds((0, 0, 320, 150)) == (320, 148, 0, 0)
    assert widget._notch_bounds((0, 0, 320, 150), True, 400) == (320, 150, 0, 0)
    # Expanded requests below the collapsed rail height clamp to that 96px minimum.
    assert widget._notch_bounds((0, 0, 1920, 1080), True, 90) == (680, 148, 620, 0)
    assert widget._notch_bounds((0, 0, 1920, 1080), True, 999) == (680, 999, 620, 0)


def test_notch_geometry_does_not_replace_legacy_bounds(tmp_path, monkeypatch):
    path = tmp_path / "window-state.json"
    monkeypatch.setattr(widget, "_window_state_path", lambda: path)
    app = widget.WidgetApp()
    try:
        app._remember(x=1, y=2, width=440, height=132)
        app.save_bounds()
        assert not path.exists()
        calls = []
        api = widget.WindowApi(None, None, None, on_notch=lambda expanded, height=None: calls.append((expanded, height)))
        api.set_notch(True)
        api.set_notch(True, 320)
        api.set_notch(False, 320)
        api.set_notch("false")
        api.set_notch(True, True)
        api.set_notch(True, float("nan"))
        api.set_notch(True, float("inf"))
        api.set_notch(True, "320")
        assert calls == [(True, None), (True, 320), (False, 320)]
    finally:
        app.control.close()


def test_notch_hover_never_restores_a_minimized_window(monkeypatch):
    user32 = _RectUser32((0, 0, 420, 880))
    user32.iconic = True
    _install_fake_win32(monkeypatch, user32, _FakeGdi32())
    monkeypatch.setattr(widget, "_work_area", lambda hwnd=None: (0, 0, 1920, 1080))
    app = widget.WidgetApp(); app.window = _WidgetFakeWindow(handle=1234)
    app.minimized, app.visible = True, False
    try:
        app.set_notch(True, 300)
        assert app.window.calls == []
        assert user32.placements == []
        assert app.minimized is True and app.visible is False
    finally:
        app.control.close()


def test_notch_size_requests_leave_the_latest_native_placement(monkeypatch):
    user32 = _RectUser32((0, 0, 420, 880))
    _install_fake_win32(monkeypatch, user32, _FakeGdi32())
    monkeypatch.setattr(widget, "_work_area", lambda hwnd=None: (0, 0, 1920, 1080))
    app = widget.WidgetApp(); app.window = _WidgetFakeWindow(handle=1234)
    try:
        app.set_notch(True, 180)
        app.set_notch(False)
        app.set_notch(True, 560)
        if app._notch_thread:
            app._notch_thread.join(timeout=2)
        assert user32.placements[-1][1:5] == (620, 0, 680, 560)
    finally:
        app.control.close()


def test_window_api_invokes_callbacks():
    calls = []
    api = widget.WindowApi(lambda: calls.append("min"), lambda: calls.append("max"),
                           lambda: calls.append("close"), on_fit=lambda: calls.append("fit"))
    api.minimize(); api.toggle_maximize(); api.fit(); api.close()
    assert calls == ["min", "max", "fit", "close"]


def test_widget_defaults_match_the_narrow_single_column_widget():
    assert widget.WINDOW_WIDTH == 420
    assert widget.WINDOW_HEIGHT == 880


def test_native_resize_enforces_minimum_and_ignores_maximized_window():
    calls = []
    class Window:
        width, height = 420, 880
        def resize(self, width, height): calls.append((width, height))
    app = widget.WidgetApp(); app.window = Window()
    try:
        api = widget.WindowApi(app.minimize, app.toggle_maximize, app.hide, app.size, app.resize)
        assert api.resize_start() == {"width": 420, "height": 880}
        api.resize(250, 500)
        api.resize(420, 880)
        assert calls == [(300, 600), (420, 880)]
        app.maximized = True
        assert api.resize_start() is None
        api.resize(500, 900)
        assert len(calls) == 2
    finally:
        app.control.close()


def test_widget_enables_native_transparency_for_rounded_corners():
    source = Path(widget.__file__).read_text(encoding="utf-8")
    assert "frameless=True" in source
    assert "transparent=True" in source


class _FakeNativeWindow:
    class Native:
        Handle = 1234

    native = Native()


class _FakeGdi32:
    def __init__(self):
        self.created = []
        self.deleted = []

    def CreateRoundRectRgn(self, *args):
        self.created.append(args)
        return 456

    def DeleteObject(self, region):
        self.deleted.append(region.value)


class _FakeUser32:
    def __init__(self, *, maximized=False, set_result=True, dpi=96):
        self.maximized = maximized
        self.set_result = set_result
        self.dpi = dpi
        self.regions = []

    def IsZoomed(self, _hwnd): return self.maximized
    def GetDpiForWindow(self, _hwnd): return self.dpi
    def GetClientRect(self, _hwnd, rect):
        value = ctypes.cast(rect, ctypes.POINTER(widget._Rect)).contents
        value.right, value.bottom = 720, 880
        return True
    def SetWindowRgn(self, hwnd, region, redraw):
        self.regions.append((hwnd.value, None if region is None else region.value, redraw))
        return self.set_result


def _install_fake_win32(monkeypatch, user32, gdi32):
    monkeypatch.setattr(widget.ctypes, "windll", type("WinDll", (), {
        "user32": user32, "gdi32": gdi32,
    })(), raising=False)


def test_native_region_rounds_host_and_windows_own_successful_region(monkeypatch):
    user32, gdi32 = _FakeUser32(), _FakeGdi32()
    _install_fake_win32(monkeypatch, user32, gdi32)
    app = widget.WidgetApp(); app.window = _FakeNativeWindow()
    try:
        app.apply_native_window_region()
        assert gdi32.created == [(0, 0, 721, 881, 40, 40)]
        assert user32.regions == [(1234, 456, True)]
        assert gdi32.deleted == []
    finally:
        app.control.close()


def test_native_region_scales_with_dpi_and_releases_failed_region(monkeypatch):
    user32, gdi32 = _FakeUser32(set_result=False, dpi=144), _FakeGdi32()
    _install_fake_win32(monkeypatch, user32, gdi32)
    app = widget.WidgetApp(); app.window = _FakeNativeWindow()
    try:
        app.apply_native_window_region()
        assert gdi32.created[0][-2:] == (60, 60)
        assert gdi32.deleted == [456]
    finally:
        app.control.close()


def test_native_region_is_removed_when_native_window_is_maximized(monkeypatch):
    user32, gdi32 = _FakeUser32(maximized=True), _FakeGdi32()
    _install_fake_win32(monkeypatch, user32, gdi32)
    app = widget.WidgetApp(); app.window = _FakeNativeWindow()
    try:
        app.apply_native_window_region()
        assert user32.regions == [(1234, None, True)]
        assert gdi32.created == []
    finally:
        app.control.close()


def test_native_region_ignores_unavailable_native_handle():
    app = widget.WidgetApp(); app.window = object()
    try:
        app.apply_native_window_region()
    finally:
        app.control.close()


def _fake_control_listener(respond, connections=1):
    """Bind a control-port stand-in.

    ``respond(line, exchange)`` returns the reply for each line received on a
    connection (or None to stay silent). Every line any client sends is
    recorded in ``received`` so tests can prove what was (not) transmitted.
    """
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind((widget.HTTP_HOST, 0)); listener.listen(4)
    received = []

    def serve():
        for _ in range(connections):
            try:
                conn, _addr = listener.accept()
            except OSError:
                return
            exchange = {}
            with conn:
                conn.settimeout(1)
                buffer = b""
                while True:
                    try:
                        chunk = conn.recv(4096)
                    except OSError:
                        break
                    if not chunk:
                        break
                    buffer += chunk
                    while b"\n" in buffer:
                        raw, buffer = buffer.split(b"\n", 1)
                        line = raw.decode().strip()
                        received.append(line)
                        reply = respond(line, exchange)
                        if reply is not None:
                            conn.sendall(reply.encode() + b"\n")

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    return listener, listener.getsockname()[1], received, thread


def _genuine_responder(nonce, pid=4242):
    """A stand-in that holds ``nonce`` and speaks the challenge-response protocol."""
    product = widget.product

    def respond(line, exchange):
        verb, _, arg = line.partition(" ")
        if verb == "hello":
            exchange["cc"], exchange["sc"] = arg, product.new_challenge()
            return json.dumps({"product": "Branchglance", "build": "b1", "pid": pid,
                               "challenge": exchange["sc"],
                               "proof": product.control_mac(nonce, "server", arg, exchange["sc"])})
        expected = product.control_mac(nonce, "client", exchange.get("cc", ""), exchange.get("sc", ""), verb)
        return "ok" if product.control_mac_matches(expected, arg) else "denied"
    return respond


def _write_nonce(value):
    path = widget.product.control_nonce_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value)
    return value


def test_notify_existing_authenticates_listener_then_sends_mac_never_the_nonce(monkeypatch):
    nonce = _write_nonce("a1" * 32)
    listener, port, received, thread = _fake_control_listener(_genuine_responder(nonce))
    monkeypatch.setattr(widget, "CONTROL_PORT", port)
    try:
        assert widget.WidgetApp.notify_existing() is True
        thread.join(2)
        assert [line.split()[0] for line in received] == ["hello", "show"]
        assert all(nonce not in line for line in received)
    finally:
        listener.close()


def _forged_hello(identity_fields, proof):
    def respond(line, exchange):
        if line.startswith("hello "):
            fields = dict(identity_fields)
            fields["challenge"] = "d4" * 32
            if proof == "wrong-key":
                fields["proof"] = widget.product.control_mac("e5" * 32, "server", line.split()[1], fields["challenge"])
            elif proof == "replayed":
                fields["proof"] = widget.product.control_mac("b2" * 32, "server", "f6" * 32, fields["challenge"])
            elif proof is not None:
                fields["proof"] = proof
            return json.dumps(fields)
        return "ok"
    return respond


PLAUSIBLE = {"schema": 1, "product": "Branchglance", "version": "1.0.0", "build": "b1", "pid": 4242,
             "stateRoot": "C:\\anything", "frozen": True}


@pytest.mark.parametrize("respond", [
    lambda line, exchange: None,  # silent / foreign listener
    lambda line, exchange: "not json",
    lambda line, exchange: json.dumps(PLAUSIBLE),  # old-style identity echo, no proof
    _forged_hello(PLAUSIBLE, None),
    _forged_hello(PLAUSIBLE, "0" * 64),
    _forged_hello(PLAUSIBLE, "wrong-key"),
    _forged_hello(PLAUSIBLE, "replayed"),  # right key, proof bound to another exchange
    _forged_hello(dict(PLAUSIBLE, product="TokenTracker"), None),
])
def test_notify_existing_never_sends_nonce_or_command_to_unverified_listener(monkeypatch, respond):
    nonce = _write_nonce("b2" * 32)
    listener, port, received, thread = _fake_control_listener(respond)
    monkeypatch.setattr(widget, "CONTROL_PORT", port)
    try:
        assert widget.WidgetApp.notify_existing() is False
        thread.join(3)
        assert len(received) == 1 and received[0].startswith("hello ")
        assert all(nonce not in line for line in received)
    finally:
        listener.close()


def test_codex_provider_uses_local_telemetry_when_app_server_is_unavailable(tmp_path, monkeypatch):
    class UnavailableClient:
        def snapshot(self):
            raise CodexUnavailable("not running")
    provider = CodexProvider(UnavailableClient())
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    monkeypatch.setattr(provider, "_local_usage", lambda **_kwargs: {
        "daily": {"2026-09-01": 250}, "totals": {"input_tokens": 100, "cached_input_tokens": 20, "total_tokens": 250},
        "rate": {"primary": {"usedPercent": 12}},
    })
    fallback_row = {
        "id": "local", "title": "Local", "path": "local-rollout.jsonl",
        "status": "running", "statusSource": "transcript", "statusObservedAt": None,
        "model": "gpt-6-luna", "contextTokens": 20,
    }
    monkeypatch.setattr(provider, "_local_threads", lambda **_kwargs: [fallback_row])
    joined = []

    def accept_fallback_rows(chats, *, appserver_rows=True, session_paths=None):
        joined.append((appserver_rows, chats))
        return [{"id": chat["id"]} for chat in chats]

    monkeypatch.setattr(provider, "_with_agents", accept_fallback_rows)
    snap = provider.snapshot()
    assert snap["source"] == "Codex local session telemetry"
    assert snap["fiveHour"]["percent"] == 12
    assert snap["limitsUpdatedAt"] is None
    assert snap["cache"]["percent"] == 20
    assert snap["cache"]["read"] == 20
    assert snap["cache"]["uncachedInput"] == 80
    assert snap["cache"]["cacheCreation"] is None
    assert snap["chats"] == [{"id": "local"}]
    assert joined == [(False, [fallback_row])]


def test_codex_prefers_fresher_tracked_total_over_stale_summary(monkeypatch):
    class Client:
        def snapshot(self):
            return type("Snapshot", (), {"rate_limits": {"rateLimits": {}}, "usage": {"summary": {"lifetimeTokens": 5}, "dailyUsageBuckets": []}, "threads": []})()
    provider = CodexProvider(Client())
    monkeypatch.setattr(provider, "_local_usage", lambda **_kwargs: {"daily": {}, "totals": {"total_tokens": 100}, "models": {}, "rate": None})
    assert provider.snapshot()["lifetimeTokens"] == 100


def test_build_snapshot_aggregates_sources_and_isolates_provider_failure(monkeypatch):
    today = providers._local_today().isoformat()
    monkeypatch.setattr(providers, "LIMIT_ONLY_PROVIDERS", ("Claude",))
    monkeypatch.setattr(providers, "POLICIES", load_policies({
        "TOKEN_TRACKER_CODEX_COLLECT": "true", "TOKEN_TRACKER_CODEX_SHOW": "true",
        "TOKEN_TRACKER_CLAUDE_COLLECT": "true", "TOKEN_TRACKER_CLAUDE_SHOW": "true",
    }))
    class BrokenClaudeLimits:
        def snapshot(self):
            raise RuntimeError("Claude unavailable")
    class WorkingCodex:
        def snapshot(self):
            return {
                "connected": True, "name": "Codex", "accent": "#4a95e0", "fiveHour": {}, "weekly": {}, "cache": None,
                "chats": [], "activity": [{"date": today, "value": 123}], "lifetimeTokens": 123,
            }
    monkeypatch.setattr(providers, "ClaudeLimitsProvider", BrokenClaudeLimits)
    monkeypatch.setattr(providers, "CodexProvider", WorkingCodex)
    snap = providers.build_snapshot()
    assert snap["hero"]["today"] == 123
    assert snap["hero"]["sevenDays"] == 123
    assert snap["providers"][1]["connected"] is False
    assert snap["activity"] == [{"date": today, "value": 123}]


@pytest.mark.parametrize("area", [(0, 0, 2560, 1392), (0, 0, 1366, 728), (-1920, 0, 0, 1040), (0, 0, 800, 560)])
def test_opening_size_fits_work_area(area):
    width, height, x, y = widget._opening_bounds(area)
    assert x >= area[0] and y >= area[1]
    assert x+width <= area[2] and y+height <= area[3]
    assert height <= widget.WINDOW_HEIGHT
    # V2 opens narrow everywhere; short screens shrink the list, not widen the window.
    assert width == min(420, area[2]-area[0]-32)


def test_codex_only_snapshot_never_reads_claude(monkeypatch):
    monkeypatch.setattr(providers, "ENABLED_PROVIDERS", ("Codex",))
    monkeypatch.setattr(providers, "LIMIT_ONLY_PROVIDERS", ())
    monkeypatch.setattr(providers, "POLICIES", load_policies({
        "TOKEN_TRACKER_CODEX_COLLECT": "true", "TOKEN_TRACKER_CODEX_SHOW": "true",
        "TOKEN_TRACKER_CLAUDE_COLLECT": "false", "TOKEN_TRACKER_CLAUDE_SHOW": "false",
    }))
    def forbidden():
        pytest.fail("Disabled Claude provider must not be constructed")
    class Codex:
        def snapshot(self):
            return {"name": "Codex", "connected": True, "chats": [],
                    "activity": [{"date": providers._local_today().isoformat(), "value": 123}]}
    monkeypatch.setattr(providers, "ClaudeDesktopProvider", forbidden)
    monkeypatch.setattr(providers, "CodexProvider", Codex)
    snap = providers.build_snapshot()
    assert snap["enabledProviders"] == ["Codex"]
    assert [p["name"] for p in snap["providers"]] == ["Codex"]
    assert snap["hero"]["today"] == snap["hero"]["sevenDays"] == 123
    assert [provider["name"] for provider in snap["providers"]] == ["Codex"]


def test_provider_policy_denial_skips_collection_and_hides_cached_live_state(monkeypatch):
    monkeypatch.setattr(providers, "POLICIES", load_policies({
        "TOKEN_TRACKER_CODEX_COLLECT": "false", "TOKEN_TRACKER_CODEX_SHOW": "false",
        "TOKEN_TRACKER_CLAUDE_COLLECT": "false", "TOKEN_TRACKER_CLAUDE_SHOW": "false",
    }))
    monkeypatch.setattr(providers, "ENABLED_PROVIDERS", ("Codex",))
    monkeypatch.setattr(providers, "LIMIT_ONLY_PROVIDERS", ("Claude",))
    monkeypatch.setattr(providers, "_persisted_codex_threads", lambda *args, **kwargs: pytest.fail("disabled Codex read SQLite"))
    monkeypatch.setattr(providers, "_structure_stamp", lambda names=None: "none" if not names else pytest.fail("hidden structure scan"))
    monkeypatch.setattr(providers, "CodexProvider", lambda: pytest.fail("disabled Codex provider constructed"))
    monkeypatch.setattr(providers, "ClaudeLimitsProvider", lambda: pytest.fail("disabled Claude provider constructed"))
    providers._LIVE_INDEX["Codex:cached"] = {"id": "cached", "path": "private-rollout", "status": "running"}

    live = providers.live_state()
    snapshot = providers.build_snapshot()
    today = providers.today_usage()

    assert live["chats"] == []
    assert snapshot["providers"] == [] and snapshot["activity"] == []
    assert snapshot["hero"]["today"] == snapshot["hero"]["sevenDays"] == 0
    assert today["providers"] == []
    assert live["providerPolicies"]["schemaVersion"] == 1
    assert live["providerPolicies"]["generation"] == snapshot["providerPolicies"]["generation"]
    assert today["providerPolicies"]["generation"] == snapshot["providerPolicies"]["generation"]
    assert providers._LIVE_INDEX == {}
    for payload in (live, snapshot, today):
        encoded = json.dumps(payload)
        assert "private-rollout" not in encoded and '"status": "running"' not in encoded


def test_collected_but_hidden_provider_does_not_leak_cached_or_global_metrics(monkeypatch):
    monkeypatch.setattr(providers, "POLICIES", load_policies({
        "TOKEN_TRACKER_CODEX_COLLECT": "true", "TOKEN_TRACKER_CODEX_SHOW": "false",
        "TOKEN_TRACKER_CLAUDE_COLLECT": "false", "TOKEN_TRACKER_CLAUDE_SHOW": "false",
    }))
    monkeypatch.setattr(providers, "ENABLED_PROVIDERS", ("Codex",))
    monkeypatch.setattr(providers, "LIMIT_ONLY_PROVIDERS", ())
    monkeypatch.setattr(providers, "_structure_stamp", lambda names=None: "none" if not names else pytest.fail("hidden structure scan"))

    class HiddenCodex:
        def snapshot(self):
            return {"name": "Codex", "connected": True,
                    "chats": [{"id": "hidden-chat", "status": "running"}],
                    "activity": [{"date": providers._local_today().isoformat(), "value": 9876}]}

    monkeypatch.setattr(providers, "CodexProvider", HiddenCodex)
    snapshot = providers.build_snapshot()

    assert snapshot["providers"] == []
    assert snapshot["enabledProviders"] == []
    assert snapshot["activity"] == []
    assert snapshot["hero"]["today"] == snapshot["hero"]["sevenDays"] == 0
    assert "hidden-chat" not in json.dumps(snapshot) and "9876" not in json.dumps(snapshot)


def test_claude_limits_only_adds_quota_meters_without_chats_or_traffic(tmp_path, monkeypatch):
    package = tmp_path / "Packages" / "Claude_test" / "LocalCache" / "Roaming" / "Claude"
    package.mkdir(parents=True)
    (package / "plan-usage-history.json").write_text(json.dumps({"samples": [
        {"t": 1000, "org": "current", "u": {"fh": 4, "sd": 32}}]}), encoding="utf-8")
    # A transcript that a full Claude provider would count; limits-only must ignore it.
    transcript = tmp_path / "projects" / "project"; transcript.mkdir(parents=True)
    event = {"timestamp": "2026-09-03T10:00:00Z", "message": {"id": "m", "model": "claude-sonnet-5", "usage": {"input_tokens": 100, "output_tokens": 20}}}
    (transcript / "cli.jsonl").write_text(json.dumps(event), encoding="utf-8")
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.setattr(providers, "ENABLED_PROVIDERS", ("Codex",))
    monkeypatch.setattr(providers, "LIMIT_ONLY_PROVIDERS", ("Claude",))
    monkeypatch.setattr(providers, "POLICIES", load_policies({
        "TOKEN_TRACKER_CODEX_COLLECT": "true", "TOKEN_TRACKER_CODEX_SHOW": "true",
        "TOKEN_TRACKER_CLAUDE_COLLECT": "true", "TOKEN_TRACKER_CLAUDE_SHOW": "true",
    }))
    class Codex:
        def snapshot(self):
            return {"name": "Codex", "connected": True, "chats": [],
                    "activity": [{"date": providers._local_today().isoformat(), "value": 123}]}
    monkeypatch.setattr(providers, "CodexProvider", Codex)
    snap = providers.build_snapshot()
    assert snap["enabledProviders"] == ["Codex"] and snap["limitProviders"] == ["Claude"]
    assert [p["name"] for p in snap["providers"]] == ["Codex", "Claude"]
    claude = snap["providers"][1]
    assert claude["limitsOnly"] is True and claude["chats"] == [] and claude["cache"] is None
    assert claude["fiveHour"]["percent"] == 4 and claude["weekly"]["percent"] == 32
    assert claude["limitsUpdatedAt"].startswith("1970-01-01T00:00:01")
    assert snap["hero"]["today"] == snap["hero"]["sevenDays"] == 123
    assert "Codex" in snap["hero"]["note"] and "Claude" not in snap["hero"]["note"]
    assert providers.trim_chats(snap)["providers"][1]["chatTotal"] == 0


def test_claude_limits_only_failure_leaves_codex_intact(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))  # no Claude Desktop package installed
    monkeypatch.setattr(providers, "ENABLED_PROVIDERS", ("Codex",))
    monkeypatch.setattr(providers, "LIMIT_ONLY_PROVIDERS", ("Claude",))
    monkeypatch.setattr(providers, "POLICIES", load_policies({
        "TOKEN_TRACKER_CODEX_COLLECT": "true", "TOKEN_TRACKER_CODEX_SHOW": "true",
        "TOKEN_TRACKER_CLAUDE_COLLECT": "true", "TOKEN_TRACKER_CLAUDE_SHOW": "true",
    }))
    monkeypatch.setattr(providers, "_LAST_GOOD", {})
    class Codex:
        def snapshot(self):
            return {"name": "Codex", "connected": True, "chats": [], "activity": []}
    monkeypatch.setattr(providers, "CodexProvider", Codex)
    snap = providers.build_snapshot()
    codex, claude = snap["providers"]
    assert codex["connected"] is True
    assert claude["name"] == "Claude" and claude["connected"] is False and claude["fiveHour"]["percent"] is None


def test_power_user_metadata_is_bounded_and_excludes_private_reasoning(tmp_path):
    path = tmp_path / "rollout.jsonl"
    rows = [
        {"type": "session_meta", "payload": {"id": "session", "cwd": "A:/work"}},
        {"type": "event_msg", "timestamp": "2026-09-14T11:00:00Z", "payload": {"type": "user_message", "message": "Fix keyboard navigation"}},
        {"type": "response_item", "timestamp": "2026-09-14T12:00:00Z", "payload": {"type": "message", "role": "assistant", "phase": "commentary", "content": [{"type": "output_text", "text": "Checking navigation. "*100}]}},
        {"type": "response_item", "payload": {"type": "message", "role": "assistant", "phase": "analysis", "content": [{"text": "Private reasoning must not be displayed"}]}},
    ]
    path.write_text("\n".join(json.dumps(row) for row in rows), encoding="utf-8")
    info = providers._session_insights(path)
    assert info["workspace"] == "A:/work"
    assert info["latestRequest"] == "Fix keyboard navigation"
    assert info["latestRequestAt"] == "2026-09-14T11:00:00Z"
    assert len(info["latestUpdate"]) == 240
    assert "Private reasoning" not in json.dumps(info)


def test_recent_tokens_use_deltas_and_exclude_inherited_and_old_usage(tmp_path, monkeypatch):
    path = tmp_path / "rollout.jsonl"
    path.touch()
    now = datetime.now(timezone.utc)
    def event(seconds, total, inherited=False, last=None):
        return {"timestamp": (now+timedelta(seconds=seconds)).isoformat(),
                "usage": {"total_tokens": total}, "inherited": inherited,
                "last": {"total_tokens": last} if last is not None else None}
    events = [event(-1800, 1000, True), event(-1200, 1100), event(-100, 1150),
              event(-90, 1150), event(-80, 20, last=20), event(100, 50)]
    monkeypatch.setattr(CodexProvider, "_token_events", lambda path: events)
    assert CodexProvider._recent_tokens(str(path)) == 70
    assert CodexProvider._recent_tokens(None) is None


def test_subagent_label_and_latest_user_request(tmp_path):
    path = tmp_path / "worker.jsonl"
    rows = [{"type": "session_meta", "payload": {"id": "worker", "parent_thread_id": "parent", "agent_path": "/root/keyboard_audit"}},
            {"type": "response_item", "timestamp": "2026-09-14T11:30:00Z", "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "Audit tab navigation and report failures."}]}}]
    path.write_text("\n".join(map(json.dumps, rows)), encoding="utf-8")
    chat = CodexProvider._thread({"id": "worker", "path": str(path)})
    assert chat["title"] == "Keyboard audit"
    assert chat["agentName"] == "/root/keyboard_audit"
    assert chat["latestRequest"] == "Audit tab navigation and report failures."
    assert chat["latestRequestAt"] == "2026-09-14T11:30:00Z"


def test_subagent_task_path_wins_over_generated_nickname(tmp_path):
    path = tmp_path / "worker.jsonl"
    path.write_text(json.dumps({"type": "session_meta", "payload": {
        "id": "worker", "parent_thread_id": "parent",
        "agent_path": "/root/check_display_names", "agent_nickname": "Erdos",
    }}), encoding="utf-8")

    chat = CodexProvider._thread({"id": "worker", "path": str(path), "agentNickname": "Erdos"})

    assert chat["agentName"] == "/root/check_display_names"
    assert chat["agentNickname"] == "Erdos"
    assert chat["title"] == "Check display names"


def test_subagent_nickname_is_fallback_when_task_path_is_missing(tmp_path):
    path = tmp_path / "worker.jsonl"
    path.write_text(json.dumps({"type": "session_meta", "payload": {
        "id": "worker", "parent_thread_id": "parent", "agent_nickname": "Erdos",
    }}), encoding="utf-8")

    chat = CodexProvider._thread({"id": "worker", "path": str(path)})

    assert chat["agentName"] == "Erdos"
    assert chat["agentNickname"] == "Erdos"


def test_row_task_path_wins_over_nickname_only_transcript(tmp_path):
    path = tmp_path / "worker.jsonl"
    path.write_text(json.dumps({"type": "session_meta", "payload": {
        "id": "worker", "parent_thread_id": "parent", "agent_nickname": "Erdos",
    }}), encoding="utf-8")

    chat = CodexProvider._thread({"id": "worker", "path": str(path), "agentPath": "/root/check_display_names"})

    assert chat["agentName"] == "/root/check_display_names"
    assert chat["agentNickname"] == "Erdos"
    assert chat["title"] == "Check display names"


def test_parent_title_is_preserved_with_agent_nickname_metadata():
    chat = CodexProvider._thread({"id": "parent", "title": "My exact parent title", "agentNickname": "Erdos"})

    assert chat["title"] == "My exact parent title"
    assert chat["agentNickname"] == "Erdos"


def test_opening_bounds_helper_still_fits_the_work_area(monkeypatch):
    calls = []
    app = widget.WidgetApp()
    app.notch_mode = False  # Legacy free-window fit remains supported.
    app.window = type("Window", (), {"resize": lambda self, w, h: calls.append((w, h)),
                                    "move": lambda self, x, y: calls.append((x, y))})()
    monkeypatch.setattr(app, "_native_window_handle", lambda: 123)
    monkeypatch.setattr(widget, "_work_area", lambda hwnd: (0, 0, 2560, 1392))
    try:
        app.fit_opening_size()
        assert calls == [(420, 880), (2124, 496)]
    finally:
        app.control.close()


def test_native_fit_opening_size_stays_inside_physical_work_area_at_125_percent(monkeypatch):
    user32 = _RectUser32((0, 0, 420, 880))
    user32.dpi = 120
    _install_fake_win32(monkeypatch, user32, _FakeGdi32())
    monkeypatch.setattr(widget, "_work_area", lambda hwnd: (0, 0, 2560, 1392))
    monkeypatch.setattr(widget, "_system_scale", lambda: 1.0)  # per-window DPI must take precedence
    app = widget.WidgetApp(); app.window = _WidgetFakeWindow(handle=1234)
    app.notch_mode = False
    try:
        app.fit_opening_size()
        _, x, y, width, height, _flags = user32.placements[-1]
        assert x >= 0 and y >= 0
        assert x + width <= 2560 and y + height <= 1392
        assert (width, height) == (525, 1100)
        assert (x, y) == (2015, 272)
        assert not [call for call in app.window.calls if isinstance(call, tuple)]  # native placement used physical coordinates
    finally:
        app.control.close()


@pytest.mark.parametrize("maximized,iconic", [(True, False), (False, True)])
def test_native_fit_restores_maximized_or_minimized_window_before_placing(monkeypatch, maximized, iconic):
    user32 = _RectUser32((0, 0, 420, 880))
    user32.maximized, user32.iconic = maximized, iconic
    _install_fake_win32(monkeypatch, user32, _FakeGdi32())
    monkeypatch.setattr(widget, "_monitor_descriptors", lambda: [{
        "id": "test", "primary": True, "scale": 1.0, "area": (0, 0, 1920, 1080),
        "index": 1, "label": "Monitor 1",
    }])
    app = widget.WidgetApp(); app.window = _WidgetFakeWindow(handle=1234)
    try:
        app.fit_opening_size()
        assert app.window.calls[0] == "restore"
        assert app.maximized is False and app.minimized is False
        assert user32.placements
    finally:
        app.control.close()


def test_build_snapshot_uses_exactly_seven_local_calendar_dates(monkeypatch):
    today = date(2026, 9, 4)
    monkeypatch.setattr(providers, "_local_today", lambda: today)
    class Provider:
        def __init__(self, name): self.name = name
        def snapshot(self):
            return {
                "connected": True, "name": self.name, "accent": "#000", "fiveHour": {}, "weekly": {}, "cache": None,
                "chats": [], "lifetimeTokens": 0,
                "activity": [
                    {"date": "2026-08-28", "value": 100},  # eight days ago: excluded
                    {"date": "2026-08-29", "value": 10},
                    {"date": "2026-09-04", "value": 1},
                ],
            }
    monkeypatch.setattr(providers, "CodexProvider", lambda: Provider("Codex"))
    snap = providers.build_snapshot()
    assert snap["hero"]["sevenDays"] == 11
    assert snap["activityMeta"] == {"activeDays": 3, "currentStreakDays": 1, "longestStreakDays": 2, "trackingWindowDays": 31}


def test_build_snapshot_uses_the_same_exact_31_day_window_for_chart_and_activity_meta(monkeypatch):
    today = date(2026, 9, 4)
    monkeypatch.setattr(providers, "_local_today", lambda: today)
    class Provider:
        def snapshot(self):
            return {"connected": True, "activity": [
                {"date": "2026-08-05", "value": 50},  # oldest of 31 inclusive dates: included
                {"date": "2026-08-04", "value": 50},  # outside: excluded
                {"date": "2026-09-05", "value": 50},  # future: excluded
                {"date": "2026-09-03", "value": 2},
                {"date": "2026-09-04", "value": 3},
            ]}
    monkeypatch.setattr(providers, "CodexProvider", Provider)
    snap = providers.build_snapshot()
    assert snap["activity"] == [
        {"date": "2026-08-05", "value": 50},
        {"date": "2026-09-03", "value": 2},
        {"date": "2026-09-04", "value": 3},
    ]
    assert snap["hero"]["sevenDays"] == 5
    assert snap["activityMeta"] == {"activeDays": 3, "currentStreakDays": 2, "longestStreakDays": 2, "trackingWindowDays": 31}


def test_widget_visibility_and_close_lifecycle():
    class FakeWindow:
        def __init__(self): self.calls = []
        def show(self): self.calls.append("show")
        def hide(self): self.calls.append("hide")
        def minimize(self): self.calls.append("minimize")
        def restore(self): self.calls.append("restore")
        def maximize(self): self.calls.append("maximize")
        def evaluate_js(self, script): self.calls.append(script)
    app = widget.WidgetApp()
    app.window = FakeWindow()
    try:
        app.minimize()
        assert app.visible is False
        assert app.window.calls == ["minimize"]
        # Tray/default toggle and a second desktop-shortcut launch share the show path.
        app.toggle()
        assert app.visible is True
        assert app.window.calls[1:3] == ["restore", "show"]
        assert "tokenTrackerRefresh" in app.window.calls[3]
        app.toggle_maximize()
        assert app.maximized is True
        assert app.window.calls[-1] == "maximize"
        app.minimize()
        app.show()
        assert app.maximized is True
        # Straight back to maximized: no restore() flash of the normal bounds.
        assert app.window.calls[-4:-1] == ["minimize", "maximize", "show"]
        app.toggle_maximize()
        assert app.maximized is False
        assert app.window.calls[-1] == "restore"
        assert app.on_closing() is False
        assert app.visible is False
        app.quitting = True
        assert app.on_closing() is True
    finally:
        app.control.close()


def test_forked_rollout_keeps_its_own_session_meta(tmp_path):
    rollout = tmp_path / "fork.jsonl"
    rollout.write_text("\n".join(json.dumps(row) for row in [
        {"type": "session_meta", "payload": {"id": "child", "parent_thread_id": "root", "agent_path": "/root/worker"}},
        {"type": "session_meta", "payload": {"id": "root"}},  # source thread's meta copied by the fork
    ]), encoding="utf-8")
    insights = providers._session_insights(rollout)
    assert insights["sessionId"] == "child"
    assert insights["parentId"] == "root"
    assert insights["agentName"] == "/root/worker"


def test_claude_repeated_response_lines_keep_the_complete_usage(tmp_path):
    path = tmp_path / "streamed.jsonl"
    def block(output): return {"timestamp": "2026-09-03T10:00:00Z", "message": {"id": "msg", "usage": {"input_tokens": 2, "cache_read_input_tokens": 100, "output_tokens": output}}}
    path.write_text("\n".join(json.dumps(row) for row in [block(3), block(109), block(109)]), encoding="utf-8")
    rows = ClaudeDesktopProvider._file_usage(path)
    assert len(rows) == 1
    assert rows[0]["usage"]["output_tokens"] == 109


def test_claude_copied_history_is_credited_to_the_earliest_transcript(tmp_path, monkeypatch):
    project = tmp_path / "projects" / "p"; project.mkdir(parents=True)
    shared = {"timestamp": providers.datetime.now(timezone.utc).isoformat(), "message": {"id": "shared", "model": "claude-sonnet-5", "usage": {"input_tokens": 50}}}
    new = {"timestamp": shared["timestamp"], "message": {"id": "new", "model": "claude-sonnet-5", "usage": {"input_tokens": 7}}}
    # The resumed copy sorts first by name, so directory order alone would credit it.
    (project / "z-original.jsonl").write_text(json.dumps(shared), encoding="utf-8")
    (project / "a-resumed.jsonl").write_text(json.dumps(shared) + "\n" + json.dumps(new), encoding="utf-8")
    created = {"z-original.jsonl": 1.0, "a-resumed.jsonl": 2.0}
    real_stat = Path.stat
    monkeypatch.setattr(providers, "_created_at", lambda stat: next(value for name, value in created.items() if stat.st_size == real_stat(project / name).st_size))
    provider = ClaudeDesktopProvider(str(tmp_path)); provider.transcript_root = tmp_path / "projects"
    usage = provider._transcript_usage()
    assert usage["sessions"]["z-original"]["tokens"] == 50
    assert usage["sessions"]["a-resumed"]["tokens"] == 7
    assert usage["totals"]["total_tokens"] == 57


def test_codex_counters_copied_before_session_start_are_only_a_baseline(tmp_path, monkeypatch):
    sessions = tmp_path / "sessions"; sessions.mkdir()
    rollout = sessions / "fork.jsonl"
    today = providers._local_today()
    def at(hour): return datetime(today.year, today.month, today.day, hour, tzinfo=timezone.utc).isoformat()
    def count(hour, total): return {"timestamp": at(hour), "type": "event_msg", "payload": {"type": "token_count", "info": {"total_token_usage": {"total_tokens": total, "input_tokens": total}}}}
    rollout.write_text("\n".join(json.dumps(row) for row in [
        {"type": "session_meta", "payload": {"id": "fork", "timestamp": at(2)}},
        count(1, 900),  # inherited from the source thread
        count(3, 940),
    ]), encoding="utf-8")
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    assert CodexProvider()._local_usage()["totals"]["total_tokens"] == 40
    assert sum(CodexProvider._thread_daily(str(rollout)).values()) == 40


def test_cached_running_status_still_expires_when_the_file_goes_quiet(tmp_path, monkeypatch):
    path = tmp_path / "busy.jsonl"
    path.write_text(json.dumps({"type": "assistant", "message": {"content": []}}), encoding="utf-8")
    assert _claude_transcript_status(path) == "running"
    # Minutes of silence are uncertain (a long tool call), not a confident idle.
    quiet = path.stat().st_mtime + 600
    monkeypatch.setattr(providers.time, "time", lambda: quiet)
    assert providers._claude_transcript_state(path) == {"status": "quiet", "question": None, "detail": "no recent writes"}
    inactive = path.stat().st_mtime + 3600
    monkeypatch.setattr(providers.time, "time", lambda: inactive)
    assert providers._claude_transcript_state(path) == {"status": "idle", "question": None, "detail": "inactive"}


def test_build_snapshot_queries_providers_concurrently(monkeypatch):
    barrier = threading.Barrier(2, timeout=5)
    monkeypatch.setattr(providers, "LIMIT_ONLY_PROVIDERS", ("Claude",))
    monkeypatch.setattr(providers, "POLICIES", load_policies({
        "TOKEN_TRACKER_CODEX_COLLECT": "true", "TOKEN_TRACKER_CODEX_SHOW": "true",
        "TOKEN_TRACKER_CLAUDE_COLLECT": "true", "TOKEN_TRACKER_CLAUDE_SHOW": "true",
    }))
    class Provider:
        def __init__(self, name): self.name = name
        def snapshot(self):
            barrier.wait()  # deadlocks (and times out) if providers run sequentially
            return {"connected": True, "name": self.name, "activity": [], "chats": []}
    monkeypatch.setattr(providers, "ClaudeLimitsProvider", lambda: Provider("Claude"))
    monkeypatch.setattr(providers, "CodexProvider", lambda: Provider("Codex"))
    snap = providers.build_snapshot()
    assert [p["name"] for p in snap["providers"]] == ["Codex", "Claude"]
    assert all(p["connected"] for p in snap["providers"])


# ---------------------------------------------------------------------------
# Widget native window behaviour (fake windows only; no real GUI)
# ---------------------------------------------------------------------------

import pytest  # noqa: E402  (kept local to this appended widget block)

class _WidgetFakeWindow:
    """pywebview Window stand-in that records calls; optional native HWND."""

    def __init__(self, handle=None):
        self.calls = []
        self.width, self.height = 420, 880
        if handle is not None:
            self.native = type("Native", (), {"Handle": handle})()

    def show(self): self.calls.append("show")
    def hide(self): self.calls.append("hide")
    def minimize(self): self.calls.append("minimize")
    def restore(self): self.calls.append("restore")
    def maximize(self): self.calls.append("maximize")
    def destroy(self): self.calls.append("destroy")
    def resize(self, width, height): self.calls.append(("resize", width, height))
    def evaluate_js(self, script): self.calls.append("refresh" if "tokenTrackerRefresh" in script else script)


class _WidgetStateUser32(_FakeUser32):
    """Fake user32 whose zoomed/iconic/visible state the test drives like the OS."""

    def __init__(self):
        super().__init__()
        self.iconic = False
        self.shown = True

    def IsIconic(self, _hwnd): return self.iconic
    def IsWindowVisible(self, _hwnd): return self.shown


def test_widget_flags_follow_os_maximize_minimize_and_taskbar_restore(monkeypatch):
    user32, gdi32 = _WidgetStateUser32(), _FakeGdi32()
    _install_fake_win32(monkeypatch, user32, gdi32)
    app = widget.WidgetApp(); app.window = _WidgetFakeWindow(handle=1234)
    try:
        # Win+Up: no page button involved; the maximized event syncs the flag.
        user32.maximized = True
        app.on_maximized()
        app.apply_native_window_region(1000, 1000)
        assert app.maximized is True and user32.regions[-1] == (1234, None, True)
        assert app.size() is None
        app.resize(500, 900)
        assert not [c for c in app.window.calls if isinstance(c, tuple)]
        # Taskbar minimize from maximized: IsZoomed reads False but it restores maximized.
        user32.maximized, user32.iconic = False, True
        app.on_minimized()
        assert (app.maximized, app.minimized, app.visible) == (True, True, False)
        # Tray toggle on a minimized window shows it straight back to maximized.
        app.toggle()
        assert app.window.calls[-3:] == ["maximize", "show", "refresh"]
        assert app.visible is True and app.minimized is False
        user32.maximized, user32.iconic = True, False
        # A single maximize-button click restores even though Win+Up maximized it.
        app.toggle_maximize()
        assert app.window.calls[-1] == "restore" and app.maximized is False
        user32.maximized = False
        app.on_restored()
        app.apply_native_window_region(420, 880)
        assert user32.regions[-1] == (1234, 456, True)  # rounded again after restore
        assert app.size() == {"width": 420, "height": 880}
        # Taskbar restore after a page-button minimize: tray toggle must hide, not show.
        app.minimize()
        assert app.visible is False
        app.on_restored()
        assert app.visible is True
        app.toggle()
        assert app.window.calls[-1] == "hide"
    finally:
        app.control.close()


def test_widget_state_events_without_native_handle_use_event_meaning():
    app = widget.WidgetApp(); app.window = _WidgetFakeWindow()
    try:
        app.on_minimized()
        assert (app.visible, app.minimized) == (False, True)
        assert app.size() is None
        app.resize(500, 900)
        app.on_restored()
        assert (app.visible, app.minimized, app.maximized) == (True, False, False)
        app.on_maximized()
        assert app.maximized is True
        app.resize(500, 900)
        assert not [c for c in app.window.calls if isinstance(c, tuple)]
    finally:
        app.control.close()


def test_widget_maximize_hide_to_tray_show_twice_then_restore():
    app = widget.WidgetApp(); app.window = _WidgetFakeWindow()
    try:
        app.toggle_maximize()
        assert app.on_closing() is False  # the × button hides to tray
        assert app.visible is False and app.window.calls[-1] == "hide"
        app.toggle()  # tray show
        app.show()    # second launch while already visible: idempotent
        assert app.window.calls[-6:] == ["maximize", "show", "refresh"] * 2
        assert (app.visible, app.maximized) == (True, True)
        app.toggle_maximize()
        assert app.window.calls[-1] == "restore" and app.maximized is False
        app.resize(250, 500)
        app.resize(float("nan"), 900)
        app.resize(float("inf"), 900)
        app.resize(None, 900)
        assert [c for c in app.window.calls if isinstance(c, tuple)] == [("resize", 300, 600)]
    finally:
        app.control.close()


def test_widget_control_listener_survives_silent_clients_and_stops_on_quit(monkeypatch):
    monkeypatch.setattr(widget, "CONTROL_PORT", 0)
    app = widget.WidgetApp()
    shown = threading.Event()
    app.show = lambda *_: shown.set()
    try:
        app.start_control()
        port = app.control.getsockname()[1]
        silent = socket.create_connection((widget.HTTP_HOST, port))
        monkeypatch.setattr(widget, "CONTROL_PORT", port)
        assert widget.WidgetApp.notify_existing() is True
        assert shown.wait(3)
        silent.close()
    finally:
        app.quitting = True
        app.control.close()
    assert widget.WidgetApp.notify_existing() is False


def test_widget_second_instance_loses_control_port_and_notifies_winner(monkeypatch):
    nonce = _write_nonce("c3" * 32)
    received = []
    winner =socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    widget._claim_address(winner)
    winner.bind((widget.HTTP_HOST, 0)); winner.listen(4)
    port = winner.getsockname()[1]
    respond = _genuine_responder(nonce)

    def serve():
        conn, _addr = winner.accept()
        exchange, buffer = {}, b""
        with conn:
            conn.settimeout(1)
            while True:
                try:
                    chunk = conn.recv(4096)
                except OSError:
                    break
                if not chunk:
                    break
                buffer += chunk
                while b"\n" in buffer:
                    raw, buffer = buffer.split(b"\n", 1)
                    line = raw.decode().strip()
                    received.append(line)
                    conn.sendall(respond(line, exchange).encode() + b"\n")

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    monkeypatch.setattr(widget, "CONTROL_PORT", port)
    monkeypatch.setattr(widget, "BIND_RETRY_ATTEMPTS", 2)
    monkeypatch.setattr(widget, "BIND_RETRY_DELAY", 0)
    messages = []
    monkeypatch.setattr(widget, "show_message", lambda *a: messages.append(a))
    app = widget.WidgetApp()
    created = []
    monkeypatch.setattr(widget.webview, "create_window", lambda *a, **k: created.append(a))
    try:
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            # Windows SO_REUSEADDR would otherwise let a racing launch share the port.
            intruder = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            intruder.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                with pytest.raises(OSError):
                    intruder.bind((widget.HTTP_HOST, port))
            finally:
                intruder.close()
        app.run()
        thread.join(2)
        assert [line.split()[0] for line in received] == ["hello", "show"]
        assert all(nonce not in line for line in received)
        assert created == [] and messages == []
    finally:
        app.control.close()
        winner.close()


def test_widget_http_server_port_is_exclusive():
    first = widget._ExclusiveHTTPServer((widget.HTTP_HOST, 0), server.Handler)
    try:
        port = first.server_address[1]
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            with pytest.raises(OSError):
                ThreadingHTTPServer((widget.HTTP_HOST, port), server.Handler).server_close()
    finally:
        first.server_close()


def test_widget_quit_tears_everything_down_even_if_a_step_fails(monkeypatch):
    monkeypatch.setattr(widget, "HTTP_PORT", 0)
    calls = []
    app = widget.WidgetApp(); app.window = _WidgetFakeWindow()
    class Reset:
        def stop(self): calls.append("reset"); raise RuntimeError("boom")
    class Tray:
        def stop(self): calls.append("tray")
    app.limit_reset, app.tray = Reset(), Tray()
    app.start_http()
    port = app.server.server_address[1]
    app.quit()
    assert calls == ["reset", "tray"]
    assert app.window.calls == ["destroy"]
    assert app.control.fileno() == -1
    assert app.on_closing() is True  # the destroy's closing event is not cancelled
    with pytest.raises(OSError):
        socket.create_connection((widget.HTTP_HOST, port), timeout=0.5).close()


def test_widget_run_wires_native_state_and_region_events(monkeypatch):
    monkeypatch.setattr(widget, "CONTROL_PORT", 0)
    monkeypatch.setattr(widget, "HTTP_PORT", 0)
    class Event(list):
        def __iadd__(self, handler): self.append(handler); return self
    class Window(_WidgetFakeWindow):
        def __init__(self):
            super().__init__()
            self.events = type("Events", (), {})()
            for name in ("closing", "loaded", "before_show", "resized", "moved", "maximized", "minimized", "restored"):
                setattr(self.events, name, Event())
    window, started = Window(), []
    user32 = type("User32", (), {"GetSystemMetrics": lambda self, index: [2560, 1440][index]})()
    monkeypatch.setattr(widget.ctypes, "windll", type("WinDll", (), {"user32": user32})(), raising=False)
    monkeypatch.setattr(widget.webview, "create_window", lambda *a, **k: window)
    monkeypatch.setattr(widget.webview, "start", lambda **k: started.append(k))
    monkeypatch.setattr(widget.pystray, "Icon", lambda *a, **k: type("Tray", (), {"run": lambda self, **kw: None, "stop": lambda self: None})())
    app = widget.WidgetApp()
    app.limit_reset = type("Reset", (), {"start": lambda self: None, "stop": lambda self: None})()
    try:
        app.run()
        assert started and app.server is not None
        assert window.events.maximized == [app.on_maximized]
        assert window.events.minimized == [app.on_minimized]
        assert window.events.restored == [app.on_restored]
        assert window.events.resized == [app.apply_native_window_region, app.on_bounds_resized]
        assert window.events.moved == [app.on_moved]
        assert app.restore_saved_placement in window.events.loaded
        assert window.events.closing == [app.on_closing]
    finally:
        app.quit()


# --- Chat coverage, live state and latest-15 trimming -------------------------------------------

def test_claude_chats_merge_prior_segments_hide_archived_and_skip_greetings(tmp_path):
    provider = ClaudeDesktopProvider(str(tmp_path))
    provider.root = tmp_path
    records = tmp_path / "claude-code-sessions"; records.mkdir()
    (records / "chat.json").write_text(json.dumps({"sessionId": "desktop", "cliSessionId": "cli-2", "priorCliSessionIds": ["cli-1"], "title": "Resumed", "effort": "high", "lastActivityAt": 2_000}), encoding="utf-8")
    (records / "old.json").write_text(json.dumps({"sessionId": "archived", "cliSessionId": "cli-a", "isArchived": True}), encoding="utf-8")
    recent = providers.datetime.now(timezone.utc).isoformat()
    usage = {
        "cli-1": {"tokens": 30, "turns": 2, "dailyTokens": {"2026-09-01": 30}},
        "cli-2": {"tokens": 12, "turns": 1, "dailyTokens": {"2026-09-02": 12}, "status": "needs input", "question": "Deploy now?"},
        "agent-old-segment": {"parentId": "cli-1", "title": "Earlier worker", "dailyTokens": {}},
        "cli-a": {"tokens": 5, "dailyTokens": {}},
        "agent-archived": {"parentId": "cli-a", "dailyTokens": {}},
        "greeting": {"tokens": 3, "turns": 1, "preview": "Reply with exactly: Hi", "updatedAt": recent, "dailyTokens": {}},
        "orphan": {"tokens": 9, "title": "Deleted chat", "updatedAt": recent, "dailyTokens": {}},
        "stale-empty": {"updatedAt": "2020-01-01T00:00:00Z", "dailyTokens": {}},
    }
    chats = {chat["id"]: chat for chat in provider._chats(usage)}
    assert set(chats) == {"desktop", "agent-old-segment", "orphan"}
    desktop = chats["desktop"]
    assert desktop["tokens"] == 42 and desktop["dailyTokens"] == {"2026-09-01": 30, "2026-09-02": 12}
    assert desktop["reasoningEffort"] == "high" and desktop["pendingQuestion"] == "Deploy now?" and desktop["segments"] == 2
    assert chats["agent-old-segment"]["parentId"] == "desktop"
    assert chats["orphan"]["recordSource"] == "transcript"


def test_pending_questions_are_extracted_and_cleared_when_work_resumes(tmp_path):
    claude = tmp_path / "claude.jsonl"
    ask = {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "AskUserQuestion", "input": {"questions": [{"header": "Scope", "question": "Include   archived chats?", "options": []}]}}]}}
    claude.write_text(json.dumps(ask), encoding="utf-8")
    assert providers._claude_transcript_state(claude) == {"status": "needs input", "question": "Include archived chats?", "detail": None}
    claude.write_text(json.dumps(ask) + "\n" + json.dumps({"type": "user", "message": {"content": [{"type": "tool_result"}]}}), encoding="utf-8")
    assert providers._claude_transcript_state(claude)["question"] is None

    codex = tmp_path / "codex.jsonl"
    call = {"type": "response_item", "payload": {"type": "function_call", "name": "request_user_input", "call_id": "sync-2", "arguments": json.dumps({"questions": [{"question": "Use the fast path?"}]})}}
    codex.write_text(json.dumps(call), encoding="utf-8")
    assert providers._codex_rollout_state(str(codex)) == {"status": "needs input", "question": "Use the fast path?", "questionAt": None, "questionIds": [], "questionResolutionAt": None, "questionCorrelationUnavailable": False, "detail": None, "stateSince": None, "compactionStartedAt": None, "terminalAt": None, "executionStatus": "unknown", "executionSource": "transcript", "executionObservedAt": None, "executionSince": None, "executionCompactionStartedAt": None, "executionTerminalAt": None, "executionActivity": None, "activity": None, "observedAt": None, "recordTimestampPresent": False, "telemetryUnavailable": False}
    codex.write_text(json.dumps(call) + "\n" + json.dumps({"type": "response_item", "payload": {"type": "function_call_output", "call_id": "sync-2", "output": json.dumps({"answer": "yes"})}}), encoding="utf-8")
    resumed = providers._codex_rollout_state(str(codex))
    assert resumed["status"] == "running"
    assert resumed["question"] is None and resumed["questionAt"] is None


def test_execution_lifecycle_is_independent_of_question_attention(tmp_path):
    from datetime import datetime, timedelta, timezone

    now = datetime.now(timezone.utc)
    stamp = lambda seconds: (now - timedelta(seconds=seconds)).isoformat()
    path = tmp_path / "solo-execution.jsonl"
    records = [
        {"type": "event_msg", "timestamp": stamp(4), "payload": {"type": "task_started"}},
        {"type": "response_item", "timestamp": stamp(3), "payload": {"type": "function_call", "name": "read_file", "call_id": "read-1"}},
        {"type": "response_item", "timestamp": stamp(2), "payload": {"type": "function_call", "name": "request_user_input_async", "call_id": "ask-1", "arguments": json.dumps({"questions": [{"title": "Which option?"}]})}},
    ]
    path.write_text("\n".join(json.dumps(record) for record in records), encoding="utf-8")
    waiting = providers._codex_rollout_state(str(path))
    assert waiting["status"] == "needs input"
    assert waiting["questionIds"] == [["request_user_input_async", "ask-1", 0]]
    assert waiting["executionStatus"] == "running"
    assert waiting["executionSince"] == stamp(4)
    assert waiting["executionActivity"]["kind"] == "reading"

    path.write_text(path.read_text(encoding="utf-8") + "\n" + json.dumps({
        "type": "event_msg", "timestamp": stamp(1), "payload": {"type": "turn_complete"}
    }), encoding="utf-8")
    completed = providers._codex_rollout_state(str(path))
    assert completed["status"] == "needs input"
    assert completed["questionIds"] == waiting["questionIds"]
    assert completed["executionStatus"] == "idle"
    assert completed["executionTerminalAt"] == stamp(1)


def test_execution_projection_rejects_stale_runs_and_cached_child_resurrection():
    from datetime import datetime, timedelta, timezone

    now = datetime.now(timezone.utc)
    iso = lambda seconds: (now - timedelta(seconds=seconds)).isoformat()
    terminal = iso(20)
    current = providers._codex_execution_projection(
        {"executionStatus": "running", "executionObservedAt": iso(2), "executionSince": iso(10)},
        "running", True, iso(1), now.timestamp(),
    )
    assert current["executionStatus"] == "running"
    stale = providers._codex_execution_projection(
        {"executionStatus": "running", "executionObservedAt": iso(180), "executionSince": iso(200)},
        None, False, None, now.timestamp(),
    )
    assert stale["executionStatus"] == "unavailable"
    cached_child = providers._codex_execution_projection(
        {"executionStatus": "idle", "executionTerminalAt": terminal},
        "running", True, iso(1), now.timestamp(),
    )
    assert cached_child["executionStatus"] == "idle"
    transcript_restart = providers._codex_execution_projection(
        {"executionStatus": "running", "executionObservedAt": iso(1), "executionTerminalAt": terminal,
         "executionSince": iso(1)}, None, False, None, now.timestamp(),
    )
    assert transcript_restart["executionStatus"] == "running", "a later recorded run can restart after the retained terminal"
    long_silent = providers._codex_execution_projection(
        {"executionStatus": "running", "executionObservedAt": iso(180), "executionSince": iso(240)},
        "running", True, iso(1), now.timestamp(),
    )
    assert long_silent["executionStatus"] == "running", "fresh runtime confirmation keeps a silent tool visible"
    unavailable = providers._codex_execution_projection(
        {"executionStatus": "unavailable"}, "running", True, iso(1), now.timestamp()
    )
    assert unavailable["executionStatus"] == "unavailable"
    assert unavailable["executionSince"] is None
    future = providers._codex_execution_projection(
        {"executionStatus": "running", "executionObservedAt": (now + timedelta(seconds=60)).isoformat()},
        None, False, None, now.timestamp(),
    )
    assert future["executionStatus"] == "unavailable"


def test_execution_restart_requires_qualifying_transcript_activity_not_general_run_start(tmp_path):
    from datetime import datetime, timedelta, timezone

    now = datetime.now(timezone.utc)
    iso = lambda seconds: (now - timedelta(seconds=seconds)).isoformat()
    path = tmp_path / "terminal-then-user-message.jsonl"
    path.write_text("\n".join(json.dumps(record) for record in [
        {"type": "event_msg", "timestamp": iso(30), "payload": {"type": "task_started"}},
        {"type": "event_msg", "timestamp": iso(20), "payload": {"type": "task_complete"}},
        {"type": "event_msg", "timestamp": iso(1), "payload": {"type": "user_message", "message": "ordinary message"}},
    ]), encoding="utf-8")
    recorded = providers._codex_rollout_state(str(path))
    assert recorded["status"] == "running", "the public activity ledger still sees the later user message"
    assert recorded["executionStatus"] == "idle"
    assert recorded["executionTerminalAt"] == iso(20)
    assert not providers._codex_execution_restarted_after_terminal(recorded)
    projected = providers._codex_execution_projection(recorded, "running", True, iso(0), now.timestamp())
    assert projected["executionStatus"] == "idle", "fresh cached app activity cannot revive a terminal child"
    assert projected["executionSince"] is None, "a terminal run start cannot leak into a later activity-ledger event"


def test_fresh_runtime_confirms_silent_qualifying_restart_but_not_preterminal_activity(tmp_path):
    from datetime import datetime, timedelta, timezone

    now = datetime.now(timezone.utc)
    iso = lambda seconds: (now - timedelta(seconds=seconds)).isoformat()
    path = tmp_path / "terminal-restart-silent-tool.jsonl"
    path.write_text("\n".join(json.dumps(record) for record in [
        {"type": "event_msg", "timestamp": iso(800), "payload": {"type": "task_started"}},
        {"type": "event_msg", "timestamp": iso(700), "payload": {"type": "task_complete"}},
        {"type": "event_msg", "timestamp": iso(500), "payload": {"type": "task_started"}},
        {"type": "response_item", "timestamp": iso(200), "payload": {"type": "function_call", "name": "read_file", "call_id": "silent-read"}},
    ]), encoding="utf-8")
    recorded = providers._codex_rollout_state(str(path))
    assert recorded["executionTerminalAt"] == iso(700)
    assert recorded["executionObservedAt"] == iso(200), "the old invocation is stale without runtime confirmation"
    assert recorded["executionSince"] == iso(500), "the new task clock survives its later tool invocation"
    assert providers._codex_execution_restarted_after_terminal(recorded)
    resumed = providers._codex_execution_projection(recorded, "running", True, iso(1), now.timestamp())
    assert resumed["executionStatus"] == "running"
    assert resumed["executionSource"] == "app"
    assert resumed["executionSince"] == iso(500)

    preterminal_only = {
        "executionStatus": "running", "executionObservedAt": iso(200),
        "executionSince": iso(800), "executionTerminalAt": iso(100),
    }
    assert not providers._codex_execution_restarted_after_terminal(preterminal_only)
    stale = providers._codex_execution_projection(preterminal_only, "running", True, iso(1), now.timestamp())
    assert stale["executionStatus"] == "idle", "fresh runtime state cannot revive activity older than its terminal"
    assert stale["executionSince"] is None


def test_response_item_reasoning_refreshes_only_a_proven_current_run(tmp_path):
    from datetime import datetime, timedelta, timezone

    now = datetime.now(timezone.utc)
    iso = lambda seconds: (now - timedelta(seconds=seconds)).isoformat()
    current_path = tmp_path / "current-run-old-async-reasoning.jsonl"
    current_path.write_text("\n".join(json.dumps(record) for record in [
        {"type": "response_item", "timestamp": iso(25 * 3600), "payload": {"type": "function_call", "name": "request_user_input_async", "call_id": "old-question", "arguments": json.dumps({"questions": [{"title": "Old question?"}]})}},
        {"type": "event_msg", "timestamp": iso(300), "payload": {"type": "task_started"}},
        {"type": "response_item", "timestamp": iso(200), "payload": {"type": "function_call", "name": "read_file", "call_id": "old-read"}},
    ]), encoding="utf-8")
    before_reasoning = providers._codex_rollout_state(str(current_path))
    assert before_reasoning["executionStatus"] == "running"
    assert before_reasoning["executionObservedAt"] == iso(200)
    assert before_reasoning["questionIds"] == [["request_user_input_async", "old-question", 0]]
    current_path.write_text(current_path.read_text(encoding="utf-8") + "\n" + json.dumps({
        "type": "response_item", "timestamp": iso(20), "payload": {"type": "reasoning"},
    }), encoding="utf-8")
    current = providers._codex_rollout_state(str(current_path))
    assert current["executionStatus"] == "running"
    assert current["executionObservedAt"] == iso(20)
    assert current["executionSince"] == iso(300), "reasoning refresh does not reset the run clock"
    assert current["executionActivity"]["kind"] == "thinking"
    assert current["questionIds"] == [["request_user_input_async", "old-question", 0]]
    visible = providers._codex_execution_projection(current, None, False, None, now.timestamp())
    assert visible["executionStatus"] == "running"

    compacting_path = tmp_path / "compacting-run-reasoning.jsonl"
    compacting_path.write_text("\n".join(json.dumps(record) for record in [
        {"type": "event_msg", "timestamp": iso(300), "payload": {"type": "task_started"}},
        {"type": "event_msg", "timestamp": iso(200), "payload": {"type": "context_compaction_start"}},
        {"type": "response_item", "timestamp": iso(20), "payload": {"type": "reasoning"}},
    ]), encoding="utf-8")
    compacting = providers._codex_rollout_state(str(compacting_path))
    assert compacting["executionStatus"] == "compacting"
    assert compacting["executionObservedAt"] == iso(200), "reasoning cannot refresh a compacting execution"
    assert compacting["executionCompactionStartedAt"] == iso(200)
    assert compacting["executionSince"] == iso(300)
    assert compacting["executionActivity"] is None, "compaction keeps its activity suppressed"

    terminal_path = tmp_path / "reasoning-after-terminal.jsonl"
    terminal_path.write_text("\n".join(json.dumps(record) for record in [
        {"type": "event_msg", "timestamp": iso(200), "payload": {"type": "task_started"}},
        {"type": "event_msg", "timestamp": iso(100), "payload": {"type": "task_complete"}},
        {"type": "response_item", "timestamp": iso(20), "payload": {"type": "reasoning"}},
    ]), encoding="utf-8")
    after_terminal = providers._codex_rollout_state(str(terminal_path))
    assert after_terminal["executionStatus"] == "idle"
    assert after_terminal["executionObservedAt"] == iso(100)

    future_path = tmp_path / "future-reasoning.jsonl"
    future_path.write_text("\n".join(json.dumps(record) for record in [
        {"type": "event_msg", "timestamp": iso(300), "payload": {"type": "task_started"}},
        {"type": "response_item", "timestamp": iso(200), "payload": {"type": "function_call", "name": "read_file", "call_id": "future-read"}},
        {"type": "response_item", "timestamp": (now + timedelta(seconds=60)).isoformat(), "payload": {"type": "reasoning"}},
    ]), encoding="utf-8")
    future = providers._codex_rollout_state(str(future_path))
    assert future["executionObservedAt"] == iso(200), "future reasoning cannot renew stale execution"
    assert providers._codex_execution_projection(future, None, False, None, now.timestamp())["executionStatus"] == "unavailable"

    unproven_path = tmp_path / "reasoning-without-run.jsonl"
    unproven_path.write_text(json.dumps({
        "type": "response_item", "timestamp": iso(1), "payload": {"type": "reasoning"},
    }), encoding="utf-8")
    unproven = providers._codex_rollout_state(str(unproven_path))
    assert unproven["executionStatus"] == "unknown"
    assert unproven["executionObservedAt"] is None


def test_execution_source_order_prefers_newer_runtime_terminal_and_newer_transcript_restart():
    from datetime import datetime, timedelta, timezone

    now = datetime.now(timezone.utc)
    iso = lambda seconds: (now - timedelta(seconds=seconds)).isoformat()
    runtime_terminal = providers._codex_execution_projection(
        {"executionStatus": "running", "executionObservedAt": iso(5), "executionSince": iso(15)},
        "failed", True, iso(1), now.timestamp(),
    )
    assert runtime_terminal["executionStatus"] == "failed"
    assert runtime_terminal["executionSource"] == "app"
    older_runtime = providers._codex_execution_projection(
        {"executionStatus": "running", "executionObservedAt": iso(1), "executionSince": iso(10),
         "executionTerminalAt": iso(25)},
        "failed", True, iso(2), now.timestamp(),
    )
    assert older_runtime["executionStatus"] == "running"
    assert older_runtime["executionSource"] == "transcript"


def test_sync_question_blocks_execution_without_resolving_old_async_question(tmp_path):
    from datetime import datetime, timedelta, timezone

    now = datetime.now(timezone.utc)
    iso = lambda seconds: (now - timedelta(seconds=seconds)).isoformat()
    path = tmp_path / "old-async-new-sync.jsonl"
    records = [
        {"type": "event_msg", "timestamp": iso(25 * 3600 + 10), "payload": {"type": "task_started"}},
        {"type": "response_item", "timestamp": iso(25 * 3600 + 5), "payload": {"type": "function_call", "name": "read_file", "call_id": "old-read"}},
        {"type": "response_item", "timestamp": iso(25 * 3600), "payload": {"type": "function_call", "name": "request_user_input_async", "call_id": "old-async", "arguments": json.dumps({"questions": [{"title": "Old async question?"}]})}},
    ]
    path.write_text("\n".join(json.dumps(record) for record in records), encoding="utf-8")
    before_blocker = providers._codex_rollout_state(str(path))
    assert before_blocker["executionStatus"] == "running", "async questions preserve current execution"
    assert before_blocker["executionActivity"]["kind"] == "reading"
    path.write_text(path.read_text(encoding="utf-8") + "\n" + json.dumps(
        {"type": "response_item", "timestamp": iso(1), "payload": {"type": "function_call", "name": "ask_user_question", "call_id": "new-sync", "arguments": json.dumps({"questions": [{"question": "Blocking question?"}]})}}
    ), encoding="utf-8")
    recorded = providers._codex_rollout_state(str(path))
    assert recorded["questionIds"] == [["request_user_input_async", "old-async", 0]]
    assert recorded["question"] == "Blocking question?"
    assert recorded["questionAt"] == iso(1), "the current blocking question takes display priority"
    assert recorded["executionStatus"] == "waiting"
    assert recorded["executionActivity"] is None
    projected = providers._codex_execution_projection(recorded, "running", True, iso(0), now.timestamp())
    assert projected["executionStatus"] == "waiting", "a blocking question cannot expose prior running work"
    assert projected["executionSince"] is None

    path.write_text(path.read_text(encoding="utf-8") + "\n" + json.dumps({
        "type": "response_item", "timestamp": iso(0.75), "payload": {
            "type": "function_call_output", "call_id": "new-sync",
            "output": json.dumps({"answer": "Continue"}),
        },
    }) + "\n" + json.dumps({
        "type": "response_item", "timestamp": iso(0.5),
        "payload": {"type": "function_call", "name": "read_file", "call_id": "resumed-read"},
    }), encoding="utf-8")
    resumed = providers._codex_rollout_state(str(path))
    assert resumed["status"] == "needs input", "the unrelated async question remains pending after sync resolution"
    assert resumed["question"] == "Old async question?"
    assert resumed["questionAt"] == iso(25 * 3600)
    assert resumed["questionIds"] == [["request_user_input_async", "old-async", 0]]
    assert resumed["executionStatus"] == "running"


def test_fresh_runtime_running_confirms_same_unfinished_compaction_without_resetting_clock():
    from datetime import datetime, timedelta, timezone

    now = datetime.now(timezone.utc)
    iso = lambda seconds: (now - timedelta(seconds=seconds)).isoformat()
    compaction_start = iso(220)
    stale_compaction = providers._codex_execution_projection(
        {"executionStatus": "compacting", "executionObservedAt": iso(200),
         "executionSince": iso(500), "executionCompactionStartedAt": compaction_start},
        "running", True, iso(1), now.timestamp(),
    )
    assert stale_compaction["executionStatus"] == "compacting"
    assert stale_compaction["executionObservedAt"] == iso(1)
    assert stale_compaction["executionCompactionStartedAt"] == compaction_start
    terminal = providers._codex_execution_projection(
        {"executionStatus": "idle", "executionTerminalAt": iso(100),
         "executionCompactionStartedAt": compaction_start},
        "running", True, iso(1), now.timestamp(),
    )
    assert terminal["executionStatus"] == "idle", "a later terminal record still wins over cached running"
    restarted = providers._codex_execution_projection(
        {"executionStatus": "running", "executionObservedAt": iso(1), "executionTerminalAt": iso(100),
         "executionSince": iso(1)},
        "running", True, iso(0), now.timestamp(),
    )
    assert restarted["executionStatus"] == "running", "a qualifying post-terminal invocation can restart"


def test_trim_chats_keeps_latest_roots_by_agent_activity_and_pins():
    def chat(identity, minute, parent=None):
        return {"id": identity, "parentId": parent, "updatedAt": f"2026-09-13T10:{minute:02d}:00+00:00"}
    snapshot = {"structureStamp": "s", "providers": [
        {"name": "Claude", "chats": [chat("a", 1), chat("b", 2), chat("c", 3), chat("agent", 59, "a")]},
        {"name": "Codex", "chats": [chat("x", 4), chat("y", 0)]},
    ]}
    trimmed = providers.trim_chats(snapshot, limit=2, pinned=["Codex:y"])
    kept = {(p["name"], c["id"]) for p in trimmed["providers"] for c in p["chats"]}
    # The pinned root counts toward the cap; "a" is recent through its agent.
    assert kept == {("Claude", "a"), ("Claude", "agent"), ("Codex", "y")}
    assert [p["chatTotal"] for p in trimmed["providers"]] == [3, 2]
    assert trimmed["structureStamp"] == "s" and len(snapshot["providers"][0]["chats"]) == 4


def test_server_filters_all_roots_before_limiting_and_compares_timezone_instants():
    snapshot = {"providers": [{"name": "Claude", "chats": [
        {"id": "old", "title": "Older root", "updatedAt": "2026-09-13T05:00:00Z"},
        {"id": "child", "parentId": "old", "title": "Unique worker", "status": "needs input", "updatedAt": "2026-09-13T06:00:00Z"},
        {"id": "new", "title": "New root", "status": "idle", "updatedAt": "2026-09-13T12:00:00+05:30"},
    ]}]}
    assert providers.trim_chats(snapshot, 1)["providers"][0]["chats"][0]["id"] == "new"
    result = providers.trim_chats(snapshot, 1, query="Unique", status_filter="attention")
    assert [c["id"] for c in result["providers"][0]["chats"]] == ["old", "child"]
    assert result["chatMatches"] == 1
    assert providers.trim_chats(snapshot, 1, provider_filter="Codex")["providers"][0]["chats"] == []


def test_claude_nested_agents_and_metadata_only_label_changes(tmp_path):
    provider = ClaudeDesktopProvider(str(tmp_path))
    provider.root = tmp_path
    transcripts = {"root": {"tokens": 2, "updatedAt": datetime.now(timezone.utc).isoformat()},
                   "grandchild": {"parentId": "child", "tokens": 4},
                   "child": {"parentId": "root", "tokens": 3}}
    chats = {c["id"]: c for c in provider._chats(transcripts)}
    assert chats["child"]["parentId"] == "root" and chats["grandchild"]["parentId"] == "child"
    path = tmp_path / "root" / "subagents" / "child.jsonl"
    path.parent.mkdir(parents=True)
    path.write_text('{}\n', encoding="utf-8")
    providers._session_insights(path)
    path.with_suffix(".meta.json").write_text('{"description":"Renamed worker"}', encoding="utf-8")
    assert providers._session_insights(path)["title"] == "Renamed worker"


def test_async_question_acknowledgement_does_not_clear_pending_question(tmp_path):
    path = tmp_path / "codex.jsonl"
    rows = [
        {"type": "response_item", "payload": {"type": "function_call", "name": "request_user_input_async", "call_id": "async-ack", "arguments": json.dumps({"questions": [{"title": "Which variant?"}]})}},
        {"type": "response_item", "payload": {"type": "function_call_output", "call_id": "async-ack", "output": "Question displayed"}},
        {"type": "response_item", "payload": {"type": "function_call", "name": "exec_command"}},
    ]
    path.write_text("\n".join(map(json.dumps, rows)), encoding="utf-8")
    assert providers._codex_rollout_state(str(path)) == {"status": "needs input", "question": "Which variant?", "questionAt": None, "questionIds": [["request_user_input_async", "async-ack", 0]], "questionResolutionAt": None, "questionCorrelationUnavailable": False, "detail": None, "stateSince": None, "compactionStartedAt": None, "terminalAt": None, "executionStatus": "unknown", "executionSource": "transcript", "executionObservedAt": None, "executionSince": None, "executionCompactionStartedAt": None, "executionTerminalAt": None, "executionActivity": None, "activity": None, "observedAt": None, "recordTimestampPresent": False, "telemetryUnavailable": False}
    rows.append({"type": "event_msg", "payload": {"type": "user_message", "message": "2A"}})
    path.write_text("\n".join(map(json.dumps, rows)), encoding="utf-8")
    still_waiting = providers._codex_rollout_state(str(path))
    assert still_waiting["question"] == "Which variant?" and still_waiting["questionIds"]
    rows.append({"type": "response_item", "payload": {"type": "function_call", "name": "send_user_message_question_reply",
                "arguments": json.dumps([{"questionItemId": ["request_user_input_async", "async-ack", 0], "answer": "2A"}])}})
    path.write_text("\n".join(map(json.dumps, rows)), encoding="utf-8")
    answered = providers._codex_rollout_state(str(path))
    assert answered["question"] is None and answered["questionAt"] is None and answered["questionIds"] == []


@pytest.mark.parametrize("app_status", ["active", "idle"])
def test_codex_pending_question_overrides_app_server_status(tmp_path, app_status):
    path = tmp_path / f"question-{app_status}.jsonl"
    path.write_text(json.dumps({
        "type": "response_item", "payload": {
            "type": "function_call", "name": "request_user_input_async",
            "call_id": f"question-{app_status}",
            "arguments": json.dumps({"questions": [{"title": "Which option?", "options": ["A", "B"]}]}),
        },
    }) + "\n", encoding="utf-8")
    chat = CodexProvider._thread({"id": "question", "status": {"type": app_status}, "path": str(path)})
    assert chat["status"] == "needs input"
    assert chat["statusSource"] == "transcript"
    assert chat["pendingQuestion"] == "Which option?"
    assert chat["pendingOptions"] == ["A", "B"]


def test_codex_agent_join_only_includes_active_children(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    now = datetime.now(timezone.utc).isoformat()
    identity_rows = []
    for identity, records in [
        ("active", [{"type": "event_msg", "timestamp": now, "payload": {"type": "task_started"}}]),
        ("finished", [{"type": "event_msg", "timestamp": now, "payload": {"type": "task_complete"}}]),
    ]:
        path = sessions / f"{identity}.jsonl"
        path.write_text("\n".join(map(json.dumps, [
            {"type": "session_meta", "timestamp": now, "payload": {"id": identity, "parent_thread_id": "root"}},
            *records,
        ])) + "\n", encoding="utf-8")
        source = json.dumps({"subagent": {"thread_spawn": {"parent_thread_id": "root"}}})
        identity_rows.append((identity, str(path), source, str(tmp_path), identity, identity, None, 0, 1))
    with sqlite3.connect(tmp_path / "state_5.sqlite") as connection:
        connection.execute("CREATE TABLE threads (id TEXT PRIMARY KEY, rollout_path TEXT, source TEXT, cwd TEXT, name TEXT, title TEXT, agent_path TEXT, archived INTEGER, updated_at INTEGER)")
        connection.executemany("INSERT INTO threads VALUES (?,?,?,?,?,?,?,?,?)", [
            ("root", None, "vscode", str(tmp_path), "root", "root", None, 0, 1),
            *identity_rows,
        ])
    chats = CodexProvider()._with_agents([{"id": "root"}])
    assert {chat["id"] for chat in chats} == {"root", "active"}
    assert {chat["id"] for chat in CodexProvider()._local_threads()} == {"active"}


def test_live_state_reports_fresh_status_without_exposing_paths(tmp_path, monkeypatch):
    # This test exercises path privacy for Claude telemetry, so enable it under
    # an explicit provider policy instead of bypassing live-state filtering.
    monkeypatch.setattr(providers, "ENABLED_PROVIDERS", ("Codex", "Claude"))
    monkeypatch.setattr(providers, "LIMIT_ONLY_PROVIDERS", ())
    monkeypatch.setattr(providers, "POLICIES", load_policies({
        "TOKEN_TRACKER_CODEX_COLLECT": "true", "TOKEN_TRACKER_CODEX_SHOW": "true",
        "TOKEN_TRACKER_CLAUDE_COLLECT": "true", "TOKEN_TRACKER_CLAUDE_SHOW": "true",
    }))
    transcript = tmp_path / "t.jsonl"
    transcript.write_text(json.dumps({"type": "assistant", "message": {"content": []}}), encoding="utf-8")
    snapshot_providers = [{"name": "Claude", "chats": [{"id": "chat", "path": str(transcript)}]}]
    providers._index_live_paths(snapshot_providers)
    assert "path" not in snapshot_providers[0]["chats"][0]
    live = providers.live_state()
    # Claude execution comes only from hook events: a bare transcript is never "running".
    chat = live["chats"][0]
    assert len(live["chats"]) == 1 and chat["key"] == "Claude:chat"
    assert chat["status"] == "unavailable" and chat["executionStatus"] == "unavailable"
    assert chat["executionActivity"] is None and "path" not in chat
    assert str(tmp_path) not in json.dumps(live)
    assert live["structureStamp"]


def test_live_state_includes_recorded_codex_request_and_update(tmp_path, monkeypatch):
    transcript = tmp_path / "rollout.jsonl"
    transcript.write_text("{}\n", encoding="utf-8")
    providers._index_live_paths([{"name": "Codex", "chats": [{"id": "thread", "path": str(transcript)}]}])
    monkeypatch.setattr(providers, "_codex_rollout_state", lambda path: {"status": "running", "question": None})
    monkeypatch.setattr(CodexProvider, "_last_token_event", classmethod(lambda cls, path: {"model": "gpt-6-luna"}))
    monkeypatch.setattr(providers, "_session_insights", lambda path: {
        "latestRequest": "Inspect the chat summary",
        "latestRequestAt": "2026-10-04T09:00:00+00:00",
        "latestUpdate": "Summary updated from the last recorded turn",
        "latestUpdateAt": "2026-10-04T10:00:00+00:00",
        "trackerUpdate": '[tracker]{"task":"Inspect chat","doing":"Checking update","next":"Finish review","blocked":null}[/tracker]',
        "trackerUpdateAt": "2026-10-04T10:00:02+00:00",
        "contextWindow": 200000,
    })
    monkeypatch.setattr(providers, "_codex_agent_stats", lambda path, status, is_subagent: {})
    monkeypatch.setattr(CodexProvider, "_recent_tokens", classmethod(
        lambda cls, path: (_ for _ in ()).throw(AssertionError("live telemetry must not parse recent token totals"))))

    live = providers.live_state()
    chat = next(row for row in live["chats"] if row["key"] == "Codex:thread")
    assert chat["latestRequest"] == "Inspect the chat summary"
    assert chat["latestRequestAt"] == "2026-10-04T09:00:00+00:00"
    assert chat["latestUpdate"] == "Summary updated from the last recorded turn"
    assert chat["latestUpdateAt"] == "2026-10-04T10:00:00+00:00"
    assert chat["trackerUpdate"] == '[tracker]{"task":"Inspect chat","doing":"Checking update","next":"Finish review","blocked":null}[/tracker]'
    assert chat["trackerUpdateAt"] == "2026-10-04T10:00:02+00:00"
    assert chat["model"] == "gpt-6-luna"
    assert "recentTokens" not in chat
    assert str(tmp_path) not in json.dumps(live)


def test_tracker_update_survives_ordinary_commentary_and_ignores_invalid_or_untrusted_text(tmp_path):
    rollout = tmp_path / "tracker-update.jsonl"
    base = datetime.now(timezone.utc).replace(microsecond=0) - timedelta(minutes=2)
    at = lambda seconds: (base + timedelta(seconds=seconds)).isoformat().replace("+00:00", "Z")
    valid_a = {"task": "Inspect provider", "doing": "Reading events", "next": "Check export", "blocked": None}
    valid_b = {"task": "Inspect provider", "doing": "Exporting live phase", "next": "Review", "blocked": None}
    invalid = {"task": "x" * 49, "doing": "Too long task", "next": "Review", "blocked": None}
    rows = [
        {"type": "event_msg", "timestamp": at(0), "payload": {"type": "task_started"}},
        {"type": "response_item", "timestamp": at(1), "payload": {
            "type": "message", "role": "assistant", "phase": "commentary",
            "content": [{"text": "[tracker]" + json.dumps(valid_a) + "[/tracker]"}]}},
        {"type": "response_item", "timestamp": at(2), "payload": {
            "type": "message", "role": "assistant", "phase": "commentary",
            "content": [{"text": "Reading the latest provider events."}]}},
        {"type": "response_item", "timestamp": at(3), "payload": {
            "type": "message", "role": "assistant", "phase": "commentary",
            "content": [{"text": "[tracker]" + json.dumps(invalid) + "[/tracker]"}]}},
        {"type": "response_item", "timestamp": at(4), "payload": {
            "type": "message", "role": "user", "content": [{"text": "[tracker]" + json.dumps(valid_b) + "[/tracker]"}]}},
        {"type": "response_item", "timestamp": at(5), "payload": {
            "type": "function_call_output", "call_id": "snippet-tool", "output": "[tracker]" + json.dumps(valid_b) + "[/tracker]"}},
        {"type": "response_item", "timestamp": at(6), "payload": {
            "type": "message", "role": "assistant", "phase": "analysis",
            "content": [{"text": "[tracker]" + json.dumps(valid_b) + "[/tracker]"}]}},
        {"type": "response_item", "timestamp": at(7), "payload": {
            "type": "message", "role": "assistant", "phase": "commentary",
            "content": [{"text": "[tracker]" + json.dumps(valid_b) + "[/tracker]"}]}},
    ]
    _rollout(rollout, rows[:4])
    before_later_phase = providers._session_insights(rollout)
    assert before_later_phase["latestUpdateAt"] == at(3)
    assert before_later_phase["trackerUpdate"] == "[tracker]" + json.dumps(valid_a, ensure_ascii=False) + "[/tracker]"
    assert before_later_phase["trackerUpdateAt"] == at(1)

    _rollout(rollout, rows)
    insight = providers._session_insights(rollout)
    assert insight["latestUpdate"] == "[tracker]" + json.dumps(valid_b, ensure_ascii=False) + "[/tracker]"
    assert insight["latestUpdateAt"] == at(7)
    assert insight["trackerUpdate"] == "[tracker]" + json.dumps(valid_b, ensure_ascii=False) + "[/tracker]"
    assert insight["trackerUpdateAt"] == at(7)


def test_tracker_update_keeps_original_source_time_across_later_run_commentary(tmp_path):
    rollout = tmp_path / "tracker-update-old-run.jsonl"
    base = datetime.now(timezone.utc).replace(microsecond=0) - timedelta(minutes=2)
    at = lambda seconds: (base + timedelta(seconds=seconds)).isoformat().replace("+00:00", "Z")
    snippet = {"task": "Old run", "doing": "Waiting", "next": "Continue", "blocked": None}
    rows = [
        {"type": "response_item", "timestamp": at(0), "payload": {
            "type": "message", "role": "assistant", "phase": "commentary",
            "content": [{"text": "[tracker]" + json.dumps(snippet) + "[/tracker]"}]}},
        {"type": "event_msg", "timestamp": at(10), "payload": {"type": "task_started"}},
        {"type": "response_item", "timestamp": at(11), "payload": {
            "type": "message", "role": "assistant", "phase": "commentary",
            "content": [{"text": "New run has no status snippet."}]}},
    ]
    _rollout(rollout, rows)
    insight = providers._session_insights(rollout)
    assert insight["trackerUpdateAt"] == at(0)
    assert insight["latestUpdateAt"] == at(11)
    assert insight["trackerUpdate"] == "[tracker]" + json.dumps(snippet, ensure_ascii=False) + "[/tracker]"


def test_structure_stamp_changes_when_a_session_file_is_created(tmp_path, monkeypatch):
    claude_home = tmp_path / "claude"; (claude_home / "projects" / "p").mkdir(parents=True)
    codex_home = tmp_path / "codex"; (codex_home / "sessions" / "2026" / "09" / "13").mkdir(parents=True)
    monkeypatch.setenv("CLAUDE_HOME", str(claude_home)); monkeypatch.setenv("CODEX_HOME", str(codex_home))
    before = providers._structure_stamp()
    import os as _os, time as _time
    target = codex_home / "sessions" / "2026" / "09" / "13"
    (target / "rollout-new.jsonl").write_text("{}", encoding="utf-8")
    future = _time.time() + 5
    _os.utime(target, (future, future))  # guard against coarse directory timestamps
    assert providers._structure_stamp() != before


def test_structure_stamp_changes_when_an_agent_is_added_to_existing_subagents_folder(tmp_path, monkeypatch):
    codex_home = tmp_path / "codex"
    day = codex_home / "sessions" / "2026" / "09" / "13"
    subagents = day / "subagents"
    subagents.mkdir(parents=True)
    (subagents / "existing.jsonl").write_text("{}\n", encoding="utf-8")
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path / "claude"))
    before = providers._structure_stamp()
    (subagents / "new-agent.jsonl").write_text("{}\n", encoding="utf-8")
    import os as _os, time as _time
    future = _time.time() + 5
    _os.utime(subagents, (future, future))
    assert providers._structure_stamp() != before


def test_http_stats_limit_and_live_endpoints(monkeypatch):
    snapshot = {"providers": [{"name": "Claude", "chats": [{"id": str(i), "updatedAt": f"2026-09-13T10:{i:02d}:00"} for i in range(20)]}]}
    monkeypatch.setattr(server, "build_snapshot", lambda: snapshot)
    monkeypatch.setattr(server, "live_state", lambda: {"chats": [], "structureStamp": "z"})
    monkeypatch.setattr(server, "today_usage", lambda: {"providers": [{"name": "Codex", "todayTokens": 42}]})
    server.CACHE.value = None
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True); thread.start()
    try:
        conn = HTTPConnection("127.0.0.1", httpd.server_address[1], timeout=5)
        conn.request("GET", "/api/stats?limit=15&pins=Claude%3A0")
        body = json.loads(conn.getresponse().read())
        ids = {chat["id"] for chat in body["providers"][0]["chats"]}
        assert len(ids) == 15 and "0" in ids and body["providers"][0]["chatTotal"] == 20
        conn.request("GET", "/api/stats")
        assert len(json.loads(conn.getresponse().read())["providers"][0]["chats"]) == 20
        conn.request("GET", "/api/live")
        assert json.loads(conn.getresponse().read())["structureStamp"] == "z"
        conn.request("GET", "/api/today")
        assert json.loads(conn.getresponse().read())["providers"][0]["todayTokens"] == 42
    finally:
        httpd.shutdown(); httpd.server_close(); server.CACHE.value = None


def test_codex_resumed_rollouts_and_rewritten_counters_count_only_new_requests(tmp_path, monkeypatch):
    sessions = tmp_path / "sessions"; sessions.mkdir()
    today = providers._local_today()
    def at(hour): return datetime(today.year, today.month, today.day, hour, tzinfo=timezone.utc).isoformat()
    def count(hour, total, last): return {"timestamp": at(hour), "type": "event_msg", "payload": {"type": "token_count", "info": {
        "total_token_usage": {"total_tokens": total, "input_tokens": total}, "last_token_usage": {"total_tokens": last, "input_tokens": last}}}}
    meta = {"type": "session_meta", "payload": {"id": "thread"}}
    # First file: 100 → 160, counter rewritten lower to 130 by a 20-token request, then 150.
    (sessions / "a.jsonl").write_text("\n".join(json.dumps(r) for r in [meta, count(1, 100, 100), count(2, 160, 60), count(3, 130, 20), count(4, 150, 20)]), encoding="utf-8")
    # Resumed file continues the counter at 175 after a 25-token request.
    (sessions / "b.jsonl").write_text("\n".join(json.dumps(r) for r in [meta, count(5, 175, 25), count(6, 180, 5)]), encoding="utf-8")
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    assert CodexProvider()._local_usage()["totals"]["total_tokens"] == 100 + 60 + 20 + 20 + 25 + 5
    chats = CodexProvider()._with_agents([{"id": "thread", "dailyTokens": {}}])
    assert sum(chats[0]["dailyTokens"].values()) == 230 and chats[0]["segments"] == 2


def test_widget_http_start_warms_the_snapshot_cache(monkeypatch):
    warmed = threading.Event()
    monkeypatch.setattr(widget, "HTTP_PORT", 0)
    monkeypatch.setattr(widget, "_warm_snapshot", warmed.set)
    app = widget.WidgetApp()
    try:
        app.start_http()
        assert warmed.wait(2)
    finally:
        if app.server:
            app.server.shutdown(); app.server.server_close()
        app.control.close()


# --- Resilience to real-world record shapes -----------------------------------------------------

def test_codex_identifier_shaped_context_window_is_not_a_capacity(tmp_path):
    rollout = tmp_path / "child.jsonl"
    rollout.write_text("\n".join(json.dumps(row) for row in [
        {"type": "session_meta", "payload": {"id": "child", "parent_thread_id": "root", "context_window": {"window_id": "w-123"}}},
        {"timestamp": "2026-09-16T10:00:00Z", "type": "event_msg", "payload": {"type": "token_count", "info": {
            "total_token_usage": {"total_tokens": 500}, "last_token_usage": {"total_tokens": 120}, "model_context_window": {"window_id": "w-123"}}}},
    ]), encoding="utf-8")
    assert providers._session_insights(rollout)["contextWindow"] is None
    assert providers._session_insights(rollout)["contextWindowId"] == "w-123"
    thread = CodexProvider._thread({"id": "child", "parentThreadId": "root", "path": str(rollout)})
    assert thread["contextWindow"] is None and thread["contextPercent"] is None
    assert thread["contextTokens"] == 120 and thread["tokens"] == 500
    assert providers._positive_int(True) is None and providers._positive_int("272000") == 272000 and providers._positive_int(float("nan")) is None


def test_codex_one_malformed_chat_does_not_blank_the_provider(monkeypatch):
    class Client:
        def snapshot(self):
            rate = {"rateLimits": {"primary": {"usedPercent": 40, "resetsAt": 2_000_000_000}, "secondary": {"usedPercent": 10}}}
            return type("Snapshot", (), {"rate_limits": rate, "usage": {}, "threads": [{"id": "good", "name": "Good"}, {"id": "bad", "name": "Bad"}]})()
    provider = CodexProvider(Client())
    monkeypatch.setattr(provider, "_local_usage", lambda **_kwargs: {"daily": {}, "totals": {}, "models": {}, "rate": None})
    real_thread = CodexProvider._thread.__func__
    def thread(cls, row):
        if row["id"] == "bad":
            raise TypeError("unsupported operand type(s) for /: 'int' and 'dict'")
        return real_thread(cls, row)
    monkeypatch.setattr(CodexProvider, "_thread", classmethod(thread))
    snap = provider.snapshot()
    assert snap["connected"] is True and snap["fiveHour"]["percent"] == 40
    chats = {chat["id"]: chat for chat in snap["chats"]}
    assert chats["good"]["title"] == "Good"
    assert chats["bad"]["status"] == "unknown" and "could not be read" in chats["bad"]["dataError"]
    assert len(snap["chatErrors"]) == 1

    monkeypatch.setattr(provider, "_with_agents", lambda chats, **_kwargs: (_ for _ in ()).throw(RuntimeError("lineage")))
    snap = provider.snapshot()
    assert snap["connected"] is True and {"good", "bad"} <= {chat["id"] for chat in snap["chats"]}
    assert any("Subagent records could not be joined" in error for error in snap["chatErrors"])


def test_failed_provider_refresh_keeps_last_good_data_marked_stale(monkeypatch):
    providers._LAST_GOOD.clear()
    outcomes = [{"connected": True, "name": "Codex", "chats": [{"id": "kept"}], "activity": []}]
    class Provider:
        def snapshot(self):
            if outcomes:
                return outcomes.pop()
            raise RuntimeError("parse failure")
    first = providers._provider_snapshot(Provider, "Codex", "#000")
    assert first["connected"] and not first.get("stale")
    second = providers._provider_snapshot(Provider, "Codex", "#000")
    assert second["stale"] is True and second["chats"] == [{"id": "kept"}]
    assert second["error"] == "parse failure" and second["staleSince"]
    providers._LAST_GOOD.clear()
    assert providers._provider_snapshot(Provider, "Codex", "#000")["connected"] is False


def test_codex_astra_pricing_applies_long_prompt_rule_per_request(tmp_path, monkeypatch):
    sessions = tmp_path / "sessions"; sessions.mkdir()
    today = providers._local_today()
    def at(hour): return datetime(today.year, today.month, today.day, hour, tzinfo=timezone.utc).isoformat()
    def count(hour, total, last):
        return {"timestamp": at(hour), "type": "event_msg", "payload": {"type": "token_count", "info": {"total_token_usage": total, "last_token_usage": last}}}
    short = {"input_tokens": 100_000, "cached_input_tokens": 80_000, "output_tokens": 10_000, "total_tokens": 110_000}
    long = {"input_tokens": 300_000, "cached_input_tokens": 200_000, "output_tokens": 10_000, "total_tokens": 310_000}
    cumulative = {key: short[key] + long[key] for key in short}
    (sessions / "astra.jsonl").write_text("\n".join(json.dumps(row) for row in [
        {"type": "turn_context", "payload": {"model": "gpt-6-astra"}}, count(1, short, short), count(2, cumulative, long),
    ]), encoding="utf-8")
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    usage = CodexProvider()._local_usage()
    cost = providers._costs(usage["totals"], usage["models"], providers.CODEX_PRICING)
    standard = (20_000 * 10 + 80_000 * 1 + 10_000 * 50) / 1e6
    over_threshold = (100_000 * 20 + 200_000 * 2 + 10_000 * 75) / 1e6
    assert cost["apiEquivalentCost"] == round(standard + over_threshold, 4)
    assert cost["cacheSavings"] == round((80_000 * 9 + 200_000 * 18) / 1e6, 4)
    assert cost["pricingCoverage"] == 100 and cost["unpricedModels"] == []
    assert "Standard-tier" in cost["priceBasis"]



def test_recorded_states_distinguish_completed_stopped_and_errored(tmp_path):
    def codex(*rows):
        path = tmp_path / f"codex-{len(list(tmp_path.iterdir()))}.jsonl"
        path.write_text("\n".join(json.dumps(row) for row in rows), encoding="utf-8")
        return providers._codex_rollout_state(str(path))
    user = {"type": "event_msg", "payload": {"type": "user_message", "message": "Build the thing"}}
    assert codex(user, {"type": "event_msg", "payload": {"type": "task_complete"}})["detail"] == "completed"
    aborted = codex(user, {"type": "event_msg", "payload": {"type": "turn_aborted"}})
    assert aborted == {
        "status": "failed", "question": None, "questionAt": None, "detail": "interrupted",
        "questionIds": [], "questionResolutionAt": None, "questionCorrelationUnavailable": False,
        "stateSince": None, "compactionStartedAt": None, "terminalAt": None,
        "executionStatus": "unknown", "executionSource": "transcript",
        "executionObservedAt": None, "executionSince": None,
        "executionCompactionStartedAt": None, "executionTerminalAt": None,
        "executionActivity": None,
        "activity": None, "observedAt": None, "recordTimestampPresent": False,
        "telemetryUnavailable": False,
    }
    assert aborted["questionAt"] is None
    assert codex(user, {"type": "event_msg", "payload": {"type": "task_failed"}})["detail"] == "errored"
    claude = tmp_path / "claude.jsonl"
    claude.write_text(json.dumps({"type": "assistant", "message": {"stop_reason": "end_turn", "content": []}}), encoding="utf-8")
    assert providers._claude_transcript_state(claude) == {"status": "idle", "question": None, "detail": "completed"}


def test_codex_objective_is_the_first_request_not_the_latest(tmp_path):
    rollout = tmp_path / "objective.jsonl"
    rollout.write_text("\n".join(json.dumps(row) for row in [
        {"type": "session_meta", "payload": {"id": "t"}},
        {"type": "event_msg", "payload": {"type": "user_message", "message": "Fix the Codex provider crash"}},
        {"type": "event_msg", "payload": {"type": "user_message", "message": "continue"}},
    ]), encoding="utf-8")
    thread = CodexProvider._thread({"id": "t", "path": str(rollout)})
    assert thread["objective"] == "Fix the Codex provider crash"
    assert thread["latestRequest"] == "continue"



class _FakeDwmApi:
    def __init__(self, result=0):
        self.result = result
        self.calls = []

    def DwmSetWindowAttribute(self, hwnd, attribute, value, size):
        self.calls.append((hwnd.value, attribute, ctypes.cast(value, ctypes.POINTER(ctypes.c_uint32)).contents.value, size))
        return self.result


def _install_fake_win32_with_dwm(monkeypatch, user32, gdi32, dwmapi):
    monkeypatch.setattr(widget.ctypes, "windll", type("WinDll", (), {"user32": user32, "gdi32": gdi32, "dwmapi": dwmapi})(), raising=False)


def test_native_frame_uses_compositor_rounding_with_no_border_on_windows_11(monkeypatch):
    user32, gdi32, dwmapi = _FakeUser32(), _FakeGdi32(), _FakeDwmApi()
    _install_fake_win32_with_dwm(monkeypatch, user32, gdi32, dwmapi)
    app = widget.WidgetApp(); app.window = _FakeNativeWindow()
    try:
        app.apply_native_window_region()
        assert dwmapi.calls == [(1234, widget.DWMWA_WINDOW_CORNER_PREFERENCE, widget.DWMWCP_ROUND, 4),
                                (1234, widget.DWMWA_BORDER_COLOR, widget.DWMWA_COLOR_NONE, 4)]
        # No GDI region: it would disable compositor rounding and never clipped WebView2 anyway.
        assert gdi32.created == [] and user32.regions == [(1234, None, True)]
        user32.maximized = True
        app.apply_native_window_region()
        assert dwmapi.calls[-1] == (1234, widget.DWMWA_WINDOW_CORNER_PREFERENCE, widget.DWMWCP_DONOTROUND, 4)
    finally:
        app.control.close()


def test_native_frame_falls_back_to_region_when_compositor_rounding_is_unsupported(monkeypatch):
    user32, gdi32, dwmapi = _FakeUser32(), _FakeGdi32(), _FakeDwmApi(result=-2147024809)  # E_INVALIDARG
    _install_fake_win32_with_dwm(monkeypatch, user32, gdi32, dwmapi)
    app = widget.WidgetApp(); app.window = _FakeNativeWindow()
    try:
        app.apply_native_window_region()
        assert gdi32.created == [(0, 0, 721, 881, 40, 40)] and user32.regions == [(1234, 456, True)]
    finally:
        app.control.close()



# --- Window size and position persistence ------------------------------------------------------

def test_reopening_from_tray_keeps_the_users_size_and_position(monkeypatch):
    app = widget.WidgetApp(); app.window = _WidgetFakeWindow()
    app.bounds = {"x": 900, "y": 120, "width": 520, "height": 1000, "maximized": False}
    app.visible = False
    moves = []
    app.window.move = lambda x, y: moves.append((x, y))
    try:
        app.show()
        assert not [call for call in app.window.calls if isinstance(call, tuple)] and moves == []
        assert app.window.calls[:2] == ["restore", "show"]
    finally:
        app.control.close()


def test_moves_and_resizes_are_saved_once_settled_and_restored_on_launch(tmp_path, monkeypatch):
    path = tmp_path / "window-state.json"
    monkeypatch.setattr(widget, "_window_state_path", lambda: path)
    monkeypatch.setattr(widget, "BOUNDS_SAVE_DELAY_SECONDS", 0.05)
    app = widget.WidgetApp(); app.window = _WidgetFakeWindow()
    app.notch_mode = False
    app.window.x, app.window.y = 2124, 496
    try:
        app.on_moved(1500, 200)
        app.on_bounds_resized(560, 1040)
        app.on_moved(-32000, -32000)  # minimized parking position is ignored
        deadline = __import__("time").time() + 2
        while not path.exists() and __import__("time").time() < deadline:
            __import__("time").sleep(0.02)
        assert json.loads(path.read_text(encoding="utf-8")) == {"x": 1500, "y": 200, "width": 560, "height": 1040, "maximized": False}
        app.maximized = True
        app.on_bounds_resized(2560, 1392)  # maximized geometry is not the user's normal size
        assert app.bounds["width"] == 560
    finally:
        app.control.close()
    relaunched = widget.WidgetApp()
    relaunched.notch_mode = False
    try:
        assert relaunched.initial_bounds() == (560, 1040, 1500, 200)
    finally:
        relaunched.control.close()


def test_saved_bounds_are_rescued_when_their_screen_is_gone_and_clamped_to_the_work_area():
    areas = [(0, 0, 1920, 1040)]
    assert widget._restorable_bounds({"x": 3000, "y": 100, "width": 420, "height": 880}, areas) is None
    assert widget._restorable_bounds({"x": 1800, "y": 300, "width": 420, "height": 1200}, areas) == (420, 1040, 1500, 0)
    assert widget._restorable_bounds({"x": 100, "y": 100, "width": 420, "height": 880}, areas) == (420, 880, 100, 100)
    assert widget._restorable_bounds(None, areas) is None


def test_hide_and_quit_flush_bounds_and_maximized_state_is_reapplied(tmp_path, monkeypatch):
    path = tmp_path / "window-state.json"
    monkeypatch.setattr(widget, "_window_state_path", lambda: path)
    app = widget.WidgetApp(); app.window = _WidgetFakeWindow()
    app.notch_mode = False
    app.window.x, app.window.y = 10, 20
    try:
        app.on_moved(40, 50)
        app.hide()
        assert json.loads(path.read_text(encoding="utf-8"))["x"] == 40
        app.on_maximized()
        app.quit()
        assert json.loads(path.read_text(encoding="utf-8"))["maximized"] is True
    finally:
        app.control.close()
    relaunched = widget.WidgetApp(); relaunched.window = _WidgetFakeWindow()
    relaunched.notch_mode = False
    try:
        relaunched.restore_maximized_state()
        assert relaunched.window.calls == ["maximize"] and relaunched.maximized is True
    finally:
        relaunched.control.close()


def test_corrupt_saved_bounds_fall_back_to_the_opening_position(tmp_path):
    path = tmp_path / "window-state.json"
    for content in ("not json", json.dumps({"x": "a", "y": 1, "width": 400, "height": 800}), json.dumps({"x": 1, "y": 1, "width": 20, "height": 800}), json.dumps({"x": float("nan"), "y": 1, "width": 400, "height": 800})):
        path.write_text(content, encoding="utf-8")
        assert widget._load_window_bounds(path) is None



class _RectUser32(_WidgetStateUser32):
    """Fake user32 that also reports and sets a window rectangle."""

    def __init__(self, rect):
        super().__init__()
        self.rect = rect
        self.placements = []

    def SetWindowPos(self, hwnd, _after, x, y, width, height, flags):
        self.placements.append((hwnd.value, x, y, width, height, flags))
        self.rect = (x, y, width, height)
        return True

    def GetWindowRect(self, _hwnd, pointer):
        value = ctypes.cast(pointer, ctypes.POINTER(widget._Rect)).contents
        left, top, width, height = self.rect
        value.left, value.top, value.right, value.bottom = left, top, left + width, top + height
        return True


def test_restore_from_minimized_parking_position_neither_saves_nor_stays_off_screen(monkeypatch):
    user32 = _RectUser32((-32000, -32000, 300, 600))
    _install_fake_win32(monkeypatch, user32, _FakeGdi32())
    app = widget.WidgetApp(); app.window = _WidgetFakeWindow(handle=1234)
    app.notch_mode = False
    moves = []
    app.window.move = lambda x, y: moves.append((x, y))
    app.bounds = {"x": 173, "y": 30, "width": 525, "height": 1342, "maximized": False}
    try:
        # The resize fired while still parked must not overwrite the user's size.
        app.on_bounds_resized(300, 600)
        assert app.bounds["width"] == 525 and app.bounds["height"] == 1342
        # Showing puts the window back at the saved bounds instead of leaving it parked.
        app.show()
        assert user32.placements == [(1234, 173, 30, 525, 1342, 0x14)] and moves == []
    finally:
        app.control.close()


def test_show_leaves_an_on_screen_window_where_the_user_put_it(monkeypatch):
    user32 = _RectUser32((600, 80, 480, 900))
    _install_fake_win32(monkeypatch, user32, _FakeGdi32())
    app = widget.WidgetApp(); app.window = _WidgetFakeWindow(handle=1234)
    app.notch_mode = False
    moves = []
    app.window.move = lambda x, y: moves.append((x, y))
    app.bounds = {"x": 173, "y": 30, "width": 525, "height": 1342, "maximized": False}
    try:
        app.show()
        assert moves == [] and not [call for call in app.window.calls if isinstance(call, tuple)]
    finally:
        app.control.close()



def test_native_rectangle_round_trips_exactly_across_relaunch(tmp_path, monkeypatch):
    path = tmp_path / "window-state.json"
    monkeypatch.setattr(widget, "_window_state_path", lambda: path)
    user32 = _RectUser32((173, 30, 525, 1342))
    _install_fake_win32(monkeypatch, user32, _FakeGdi32())
    app = widget.WidgetApp(); app.window = _WidgetFakeWindow(handle=1234)
    app.notch_mode = False  # Notch hover bounds intentionally do not persist.
    try:
        # pywebview's DPI-converted event values (509x1303) are not trusted over the real rectangle.
        app.on_bounds_resized(509, 1303)
        app.save_bounds()
        assert json.loads(path.read_text(encoding="utf-8")) == {"x": 173, "y": 30, "width": 525, "height": 1342, "maximized": False}
    finally:
        app.control.close()
    user32.rect = (2124, 496, 420, 880)  # the relaunched window starts at the default spot
    relaunched = widget.WidgetApp(); relaunched.window = _WidgetFakeWindow(handle=1234)
    relaunched.notch_mode = False
    try:
        relaunched.restore_saved_placement()
        assert user32.placements[-1] == (1234, 173, 30, 525, 1342, 0x14)
        assert user32.rect == (173, 30, 525, 1342)
    finally:
        relaunched.control.close()


def test_saved_physical_bounds_are_not_clamped_to_scaled_screen(tmp_path, monkeypatch):
    # 2560px-wide screen at 125%: logical work area 2048 wide, physical 2560.
    path = tmp_path / "window-state.json"
    path.write_text(json.dumps({"x": 1961, "y": 211, "width": 531, "height": 825, "maximized": False}), encoding="utf-8")
    monkeypatch.setattr(widget, "_window_state_path", lambda: path)
    monkeypatch.setattr(widget, "_system_scale", lambda: 1.25)
    monkeypatch.setattr(widget, "_monitor_work_areas",
                        lambda physical=False: [(0, 0, 2560, 1392)] if physical else [(0, 0, 2048, 1114)])
    user32 = _RectUser32((1517, 211, 531, 825))
    _install_fake_win32(monkeypatch, user32, _FakeGdi32())
    app = widget.WidgetApp(); app.window = _WidgetFakeWindow(handle=1234)
    app.notch_mode = False  # This case exercises persisted full-window placement.
    try:
        assert app.initial_bounds() == (425, 660, 1569, 169)  # logical creation bounds
        app.restore_saved_placement()
        assert user32.placements[-1] == (1234, 1961, 211, 531, 825, 0x14)
        user32.placements.clear()
        app.ensure_on_screen()  # already where it was left: no move
        assert user32.placements == []
    finally:
        app.control.close()


def _warmth_events(gap_minutes, hit):
    base = datetime(2026, 9, 1, tzinfo=timezone.utc)
    last = lambda cached: {"input_tokens": 50000, "cached_input_tokens": cached, "output_tokens": 10}
    return [
        {"timestamp": base.isoformat(), "usage": {}, "last": last(0), "inherited": False},
        {"timestamp": (base + timedelta(minutes=gap_minutes)).isoformat(), "usage": {},
         "last": last(45000 if hit else 1000), "inherited": False},
    ]


def test_cache_warmth_learns_warm_and_cold_edges(monkeypatch):
    files = {}
    for minutes, hits in [(1, 4), (3, 4), (8, 4), (12, 4), (18, 4), (25, 4), (40, 2), (50, 2), (75, 1), (180, 0)]:
        for i in range(4):
            files[f"{minutes}-{i}"] = _warmth_events(minutes, i < hits)
    monkeypatch.setattr(providers.CodexProvider, "_token_events", classmethod(lambda cls, path: files[path]))
    result = providers.CodexProvider._cache_warmth(list(files))
    assert result == {"warmSeconds": 1800, "coldSeconds": 3600, "samples": 36, "learned": True}


def test_cache_warmth_sparse_falls_back(monkeypatch):
    files = {str(i): _warmth_events(10, True) for i in range(5)}
    files["inherited"] = [dict(event, inherited=True) for event in _warmth_events(10, True)]
    monkeypatch.setattr(providers.CodexProvider, "_token_events", classmethod(lambda cls, path: files[path]))
    result = providers.CodexProvider._cache_warmth(list(files))
    assert result == {"warmSeconds": 300, "coldSeconds": 3600, "samples": 5, "learned": False}


def test_live_state_codex_rows_include_last_request(tmp_path, monkeypatch):
    rollout = tmp_path / "rollout.jsonl"
    rollout.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(providers, "_codex_rollout_state", lambda path: {"status": "idle", "question": None, "detail": None})
    monkeypatch.setattr(providers, "_session_insights", lambda path: {})
    monkeypatch.setattr(providers.CodexProvider, "_recent_tokens", staticmethod(lambda path: None))
    monkeypatch.setattr(providers.CodexProvider, "_last_token_event", classmethod(lambda cls, path: {
        "last_token_usage": {"input_tokens": 900, "cached_input_tokens": 800, "output_tokens": 7, "total_tokens": 907}}))
    providers._index_live_paths([{"name": "Codex", "chats": [{"id": "c1", "path": str(rollout)}]}])
    row = next(chat for chat in providers.live_state()["chats"] if chat["key"] == "Codex:c1")
    assert row["lastRequest"] == {"input": 900, "cached": 800, "output": 7}



def _rollout(path, records):
    path.write_text("".join(json.dumps(record) + "\n" for record in records), encoding="utf-8")
    return path


def _recent_test_timestamp(offset_seconds=0):
    at = datetime.now(timezone.utc).replace(microsecond=0) - timedelta(minutes=2) + timedelta(seconds=offset_seconds)
    return at.isoformat().replace("+00:00", "Z")


class _FakeAppServer:
    """Answers JSON-RPC lines written to stdin; rejects sourceKinds when asked to."""

    def __init__(self, reject_source_kinds=False):
        import queue as queue_module
        self.reject = reject_source_kinds
        self.lines = queue_module.Queue()
        self.thread_params = []
        self.methods = []
        self.rate_requests = 0
        outer = self

        class Stdin:
            def write(self, text):
                for line in text.splitlines():
                    outer._answer(json.loads(line))

            def flush(self):
                pass

            def close(self):
                outer.lines.put(None)

        self.stdin = Stdin()
        self.stderr = None

    @property
    def stdout(self):
        while (line := self.lines.get()) is not None:
            yield line

    def _answer(self, message):
        method, ident = message.get("method"), message.get("id")
        self.methods.append(method)
        if ident is None:
            return
        if method == "thread/list":
            self.thread_params.append(message["params"])
            if self.reject and "sourceKinds" in message["params"]:
                reply = {"id": ident, "error": {"code": -32600, "message": "Invalid request: unknown field `sourceKinds`"}}
            else:
                reply = {"id": ident, "result": {"data": [{"id": "exec-1", "source": "exec"}]}}
        elif method == "account/rateLimits/read":
            self.rate_requests += 1
            reply = {"id": ident, "result": {"rateLimits": {"primary": {"usedPercent": 42}}}}
        else:
            reply = {"id": ident, "result": {}}
        self.lines.put(json.dumps({"jsonrpc": "2.0", **reply}) + "\n")

    def wait(self, timeout=None):
        return 0

    def poll(self):
        return None

    def terminate(self):
        self.lines.put(None)

    def kill(self):
        self.lines.put(None)


@pytest.mark.parametrize("reject", [False, True])
def test_codex_thread_list_requests_exec_sources_and_falls_back(monkeypatch, reject):
    from token_tracker import codex_client
    fake = _FakeAppServer(reject_source_kinds=reject)
    monkeypatch.setattr(codex_client.subprocess, "Popen", lambda *args, **kwargs: fake)
    snap = codex_client.CodexAppServer(executable="codex").snapshot()
    assert "exec" in fake.thread_params[0]["sourceKinds"] and "vscode" in fake.thread_params[0]["sourceKinds"]
    assert not any(kind.startswith("subAgent") for kind in fake.thread_params[0]["sourceKinds"])
    assert [row["id"] for row in snap.threads] == ["exec-1"]
    assert len(fake.thread_params) == (2 if reject else 1)
    if reject:
        assert "sourceKinds" not in fake.thread_params[1]


def test_codex_persistent_rate_limit_reader_reuses_initialized_process(monkeypatch):
    from token_tracker import codex_client
    fake = _FakeAppServer()
    launches = []
    monkeypatch.setattr(codex_client.subprocess, "Popen", lambda *args, **kwargs: launches.append(args) or fake)
    client = codex_client.CodexAppServer(executable="codex")
    assert client.rate_limits()["rateLimits"]["primary"]["usedPercent"] == 42
    assert client.rate_limits()["rateLimits"]["primary"]["usedPercent"] == 42
    assert len(launches) == 1
    assert fake.rate_requests == 2
    assert fake.methods.count("initialize") == 1
    assert fake.methods.count("initialized") == 1
    client.close()


def test_keepalive_greetings_are_ephemeral():
    source = (Path(providers.__file__).parent / "limit_reset.py").read_text(encoding="utf-8")
    assert '"exec", "--ephemeral"' in source


def test_codex_chat_source_normalization():
    assert providers._chat_source("vscode") == "app"
    assert providers._chat_source("exec") == "exec"
    assert providers._chat_source("cli") == "cli"
    assert providers._chat_source({"subAgent": {"other": "guardian"}}) == "review"
    assert providers._chat_source({"subagent": {"thread_spawn": {"parent_thread_id": "p"}}}) == "subagent"
    assert providers._chat_source(None) is None


def test_codex_auto_review_child_gets_role_and_rollout_model(tmp_path):
    path = _rollout(tmp_path / "guardian.jsonl", [
        {"timestamp": "2026-09-22T18:56:03.968Z", "type": "session_meta", "payload": {
            "id": "g1", "parent_thread_id": "root", "timestamp": "2026-09-22T18:56:03.854Z",
            "source": {"subagent": {"other": "guardian"}}, "thread_source": "guardian_review"}},
        {"timestamp": "2026-09-22T18:56:04.000Z", "type": "event_msg", "payload": {
            "type": "thread_settings_applied", "thread_settings": {"model": "codex-auto-review"}}},
    ])
    chat = CodexProvider._thread({"id": "g1", "parentThreadId": "root", "name": "Subagent", "path": str(path)})
    assert chat["agentRole"] == "auto-review"
    assert chat["source"] == "review"
    assert chat["model"] == "codex-auto-review"
    assert chat["tokens"] is None and chat["turns"] is None
    assert chat["runStartedAt"] == "2026-09-22T18:56:03.854Z"


def test_codex_spawned_child_stats_count_tools_and_patched_files(tmp_path):
    js_patch = 'const patch = "*** Begin Patch\\n*** Add File: work/a.py\\n+x\\n*** Update File: work/b.py\\n@@\\n*** End Patch";'
    path = _rollout(tmp_path / "child.jsonl", [
        {"timestamp": "2026-09-22T20:39:54.000Z", "type": "session_meta", "payload": {
            "id": "c1", "timestamp": "2026-09-20T08:00:00.000Z",
            "source": {"subagent": {"thread_spawn": {"parent_thread_id": "root"}}}}},
        {"timestamp": "2026-09-22T20:39:55.000Z", "type": "event_msg", "payload": {"type": "task_started"}},
        {"timestamp": "2026-09-22T20:39:56.000Z", "type": "response_item", "payload": {
            "type": "custom_tool_call", "name": "apply_patch",
            "input": "*** Begin Patch\n*** Update File: work/a.py\n@@\n*** Delete File: old.txt\n*** End Patch"}},
        {"timestamp": "2026-09-22T20:39:57.000Z", "type": "response_item", "payload": {
            "type": "custom_tool_call", "name": "exec", "input": js_patch}},
        {"timestamp": "2026-09-22T20:39:58.000Z", "type": "response_item", "payload": {
            "type": "function_call", "name": "shell", "arguments": json.dumps({"command": ["ls"]})}},
        {"timestamp": "2026-09-22T20:39:59.000Z", "type": "response_item", "payload": {
            "type": "local_shell_call", "action": {"command": ["cat", "x"]}}},
        {"timestamp": "2026-09-22T20:39:59.500Z", "type": "response_item", "payload": {
            "type": "function_call_output", "output": "*** Begin Patch\n*** Add File: not-counted.py"}},
        {"timestamp": "2026-09-22T20:40:16.535Z", "type": "event_msg", "payload": {"type": "task_complete"}},
    ])
    chat = CodexProvider._thread({"id": "c1", "parentThreadId": "root", "status": {"type": "idle"}, "path": str(path)})
    assert chat["agentRole"] == "spawned"
    assert chat["source"] == "subagent"
    assert chat["toolCalls"] == 4
    assert chat["filesEdited"] == 3  # work/a.py, old.txt, work/b.py
    assert chat["runStartedAt"] == "2026-09-20T08:00:00.000Z"  # legacy field retains the whole subagent session
    assert chat["activeRunStartedAt"] is None  # completed children have no active run window
    assert chat["runEndedAt"] == "2026-09-22T20:40:16.535Z"
    assert chat["failureReason"] is None


def test_codex_root_run_window_and_failure_reason(tmp_path):
    records = [
        {"timestamp": "2026-09-22T10:00:00.000Z", "type": "session_meta", "payload": {"id": "r1", "timestamp": "2026-09-22T10:00:00.000Z", "source": "exec"}},
        {"timestamp": "2026-09-22T10:00:01.000Z", "type": "event_msg", "payload": {"type": "task_started"}},
        {"timestamp": "2026-09-22T10:05:00.000Z", "type": "event_msg", "payload": {"type": "task_complete"}},
        {"timestamp": "2026-09-22T11:00:00.000Z", "type": "event_msg", "payload": {"type": "task_started"}},
        {"timestamp": "2026-09-22T11:00:01.000Z", "type": "event_msg", "payload": {"type": "user_message", "message": "go"}},
        {"timestamp": "2026-09-22T11:02:00.000Z", "type": "event_msg", "payload": {"type": "turn_aborted", "reason": "interrupted"}},
    ]
    path = _rollout(tmp_path / "root.jsonl", records)
    chat = CodexProvider._thread({"id": "r1", "source": "exec", "path": str(path)})
    assert chat["source"] == "exec" and chat["agentRole"] is None
    assert chat["status"] == "failed"
    assert chat["runStartedAt"] == "2026-09-22T11:00:00.000Z"  # the last run, not the session
    assert chat["runEndedAt"] == "2026-09-22T11:02:00.000Z"
    assert chat["failureReason"] == "interrupted"

    long_error = {"timestamp": "2026-09-22T12:00:02.000Z", "type": "event_msg", "payload": {"type": "task_failed", "message": "x" * 200}}
    _rollout(path, records + [
        {"timestamp": "2026-09-22T12:00:00.000Z", "type": "event_msg", "payload": {"type": "task_started"}}, long_error])
    chat = CodexProvider._thread({"id": "r1", "path": str(path)})
    assert chat["status"] == "failed" and len(chat["failureReason"]) == 80
    assert chat["runStartedAt"] == "2026-09-22T12:00:00.000Z"


def test_codex_running_chat_has_no_run_end(tmp_path):
    path = _rollout(tmp_path / "live.jsonl", [
        {"timestamp": "2026-09-22T10:00:00.000Z", "type": "session_meta", "payload": {"id": "r2"}},
        {"timestamp": "2026-09-22T10:00:01.000Z", "type": "event_msg", "payload": {"type": "user_message", "message": "go"}},
        {"timestamp": "2026-09-22T10:00:02.000Z", "type": "response_item", "payload": {"type": "function_call", "name": "shell", "arguments": "{}"}},
    ])
    chat = CodexProvider._thread({"id": "r2", "status": {"type": "active"}, "path": str(path)})
    assert chat["status"] == "running"
    assert chat["runStartedAt"] == "2026-09-22T10:00:01.000Z"
    assert chat["runEndedAt"] is None and chat["failureReason"] is None


def test_codex_run_stats_reuses_record_checkpoint_without_rereading_records(tmp_path, monkeypatch):
    path = _rollout(tmp_path / "memo.jsonl", [{"type": "session_meta", "payload": {"id": "m"}}])
    calls = []
    real = providers._reduce_run_stats
    monkeypatch.setattr(providers, "_reduce_run_stats",
                        lambda state, item, now: calls.append(item) or real(state, item, now))
    providers._codex_run_stats(path)
    providers._codex_run_stats(path)
    assert len(calls) == 1
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"type": "response_item", "payload": {"type": "function_call"}}) + "\n")
    assert providers._codex_run_stats(path)["toolCalls"] == 1
    assert len(calls) == 2


def test_incremental_jsonl_checkpoint_append_matches_full_reduce_and_returns_copies(tmp_path):
    path = tmp_path / "incremental.jsonl"
    initial = {"values": []}

    def reduce(state, item):
        state["values"].append(item["value"])

    path.write_text('{"value":1}\n{"value":2}\n', encoding="utf-8")
    cached = providers._incremental_jsonl_reduce("test-incremental", path, initial, reduce)
    assert cached == {"values": [1, 2]}
    cached["values"].append(99)  # A caller cannot mutate the retained checkpoint.
    assert providers._incremental_jsonl_reduce("test-incremental", path, initial, reduce) == {"values": [1, 2]}

    with path.open("a", encoding="utf-8") as handle:
        handle.write('{"value":3}\n')
    appended = providers._incremental_jsonl_reduce("test-incremental", path, initial, reduce)
    full = {"values": []}
    for line in path.read_text(encoding="utf-8").splitlines():
        reduce(full, json.loads(line))
    assert appended == full == {"values": [1, 2, 3]}


def test_incremental_jsonl_eof_records_are_visible_without_double_count_and_malformed_tail_waits(tmp_path):
    path = tmp_path / "eof.jsonl"
    initial = {"values": []}
    reduce = lambda state, item: state["values"].append(item["value"])
    path.write_text('{"value":1}\n{"value":2}', encoding="utf-8")
    assert providers._incremental_jsonl_reduce("test-eof", path, initial, reduce) == {"values": [1, 2]}
    key = ("test-eof", str(path.resolve()))
    assert key not in providers._JSONL_CHECKPOINTS  # Valid unterminated EOF records remain visible but uncached.
    with path.open("a", encoding="utf-8") as handle:
        handle.write("\n{" + '"value":3}\n')
    assert providers._incremental_jsonl_reduce("test-eof", path, initial, reduce) == {"values": [1, 2, 3]}

    partial = tmp_path / "partial.jsonl"
    partial.write_text('{"value":1}\n{"value":', encoding="utf-8")
    assert providers._incremental_jsonl_reduce("test-partial", partial, initial, reduce) == {"values": [1]}
    with partial.open("a", encoding="utf-8") as handle:
        handle.write('2}\n')
    assert providers._incremental_jsonl_reduce("test-partial", partial, initial, reduce) == {"values": [1, 2]}


def test_incremental_jsonl_replays_future_record_after_clock_advances(tmp_path):
    path = tmp_path / "future-replay.jsonl"
    path.write_text('{"at":20,"value":"later"}\n', encoding="utf-8")
    now = [10]

    def reduce(state, item):
        if item["at"] > now[0]:
            state["_incremental_uncacheable"] = True
        else:
            state["values"].append(item["value"])

    initial = {"values": []}
    first = providers._incremental_jsonl_reduce("test-future-replay", path, initial, reduce)
    assert first["values"] == [] and first["_incremental_uncacheable"] is True
    key = ("test-future-replay", str(path.resolve()))
    assert key not in providers._JSONL_CHECKPOINTS
    now[0] = 21
    assert providers._incremental_jsonl_reduce("test-future-replay", path, initial, reduce) == {"values": ["later"]}


def test_incremental_jsonl_detects_same_size_rewrite_and_truncation(tmp_path):
    path = tmp_path / "replacement.jsonl"
    initial = {"values": []}
    reduce = lambda state, item: state["values"].append(item["value"])
    original = b'{"value":1}\n'
    replacement = b'{"value":2}\n'
    assert len(original) == len(replacement)
    path.write_bytes(original)
    assert providers._incremental_jsonl_reduce("test-rewrite", path, initial, reduce) == {"values": [1]}
    old = path.stat()
    path.write_bytes(replacement)
    os.utime(path, ns=(old.st_atime_ns, old.st_mtime_ns + 2_000_000_000))
    assert providers._incremental_jsonl_reduce("test-rewrite", path, initial, reduce) == {"values": [2]}

    path.write_bytes(b"{\"value\":")
    assert providers._incremental_jsonl_reduce("test-rewrite", path, initial, reduce) == {"values": []}
    path.write_bytes(b'{"value":3}\n')
    assert providers._incremental_jsonl_reduce("test-rewrite", path, initial, reduce) == {"values": [3]}


def test_incremental_jsonl_checkpoints_enforce_entry_budget_without_large_state(tmp_path):
    initial = {"n": 0}
    reduce = lambda state, item: state.__setitem__("n", state["n"] + item["n"])
    for group in range(3):
        for index in range(200):
            path = tmp_path / f"tiny-{group}-{index}.jsonl"
            path.write_text('{"n":1}\n', encoding="utf-8")
            assert providers._incremental_jsonl_reduce("test-budget", path, initial, reduce) == {"n": 1}
    assert len(providers._JSONL_CHECKPOINTS) >= 600
    assert len(providers._JSONL_CHECKPOINTS) <= providers._JSONL_CHECKPOINT_MAX_ENTRIES
    assert 0 < providers._JSONL_CHECKPOINT_BYTES <= providers._JSONL_CHECKPOINT_MAX_BYTES


def test_session_insights_reloads_changed_subagent_sidecar_and_thread_name_beats_preview(tmp_path):
    folder = tmp_path / "subagents"
    folder.mkdir()
    rollout = folder / "child.jsonl"
    rollout.write_text('{"type":"session_meta","payload":{"id":"child"}}\n', encoding="utf-8")
    sidecar = rollout.with_suffix(".meta.json")
    sidecar.write_text(json.dumps({"description": "First label", "agentType": "worker"}), encoding="utf-8")
    first = providers._session_insights(rollout)
    assert first["title"] == "First label"
    sidecar.write_text(json.dumps({"description": "Second label", "agentType": "worker"}), encoding="utf-8")
    os.utime(sidecar, None)
    second = providers._session_insights(rollout)
    assert second["title"] == "Second label"

    long_preview = "A" * 180
    root_rollout = tmp_path / "named-root.jsonl"
    root_rollout.write_text('{"type":"session_meta","payload":{"id":"named-root"}}\n', encoding="utf-8")
    named = CodexProvider._thread({"id": "named-root", "name": "Explicit root name", "preview": long_preview,
                                  "path": str(root_rollout)})
    assert named["title"] == "Explicit root name"
    fallback = CodexProvider._thread({"id": "preview-root", "preview": long_preview, "path": str(root_rollout)})
    assert len(fallback["title"]) == 72 and fallback["title"].endswith("…")


def test_live_state_codex_rows_include_agent_stats(tmp_path):
    path = _rollout(tmp_path / "live-stats.jsonl", [
        {"timestamp": "2026-09-22T10:00:00.000Z", "type": "session_meta", "payload": {
            "id": "k1", "parent_thread_id": "root", "timestamp": "2026-09-22T10:00:00.000Z"}},
        {"timestamp": "2026-09-22T10:00:01.000Z", "type": "event_msg", "payload": {"type": "task_started"}},
        {"timestamp": "2026-09-22T10:00:02.000Z", "type": "response_item", "payload": {
            "type": "custom_tool_call", "name": "apply_patch", "input": "*** Begin Patch\n*** Add File: a.txt\n+a\n*** End Patch"}},
        {"timestamp": "2026-09-22T10:00:09.000Z", "type": "event_msg", "payload": {"type": "task_complete"}},
    ])
    providers._index_live_paths([{"name": "Codex", "chats": [{"id": "k1", "path": str(path)}]}])
    row = next(chat for chat in providers.live_state()["chats"] if chat["key"] == "Codex:k1")
    assert row["toolCalls"] == 1 and row["filesEdited"] == 1
    assert row["runStartedAt"] == "2026-09-22T10:00:00.000Z"
    assert row["runEndedAt"] == "2026-09-22T10:00:09.000Z"


def test_question_options_are_extracted_for_codex_and_claude():
    codex = {"questions": [{"title": "Did it work?", "options": ["Yes, both worked", "Heard it, no duck", "No"]}]}
    assert providers._question_options(json.dumps(codex)) == ["Yes, both worked", "Heard it, no duck", "No"]
    claude = {"questions": [{"question": "Pick", "options": [{"label": "A", "description": "x"}, {"label": "B"}]}]}
    assert providers._question_options(claude) == ["A", "B"]
    assert providers._question_options({"questions": [{"title": "Free text"}]}) == []
    assert providers._question_options("not json") == []


def test_file_changes_report_net_created_modified_deleted():
    ops = [("Add", "new.py"), ("Update", "new.py"), ("Update", "a.py"), ("Delete", "old.py"),
           ("Add", "tmp.txt"), ("Delete", "tmp.txt"), ("Update", "b.py"), ("Delete", "b.py")]
    assert providers._file_changes(ops) == {"created": 1, "modified": 1, "deleted": 2}


def test_async_question_stays_pending_after_its_turn_completes(tmp_path):
    rollout = tmp_path / "rollout.jsonl"
    rows = [
        {"type": "response_item", "timestamp": "2026-10-02T19:53:21.675Z", "payload": {"type": "function_call", "name": "request_user_input_async",
                                              "call_id": "pending-turn", "arguments": json.dumps({"questions": [{"title": "What did you say?"}]})}},
        {"type": "response_item", "payload": {"type": "function_call_output", "output": "{\"accepted\":true}"}},
        {"type": "response_item", "payload": {"type": "message", "role": "assistant", "channel": "final", "content": []}},
        {"type": "event_msg", "payload": {"type": "task_complete"}},
    ]
    rollout.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    state = providers._codex_recorded_state(rollout)
    assert state["status"] == "needs input" and state["question"] == "What did you say?"
    assert state["questionAt"] == "2026-10-02T19:53:21.675Z"
    assert providers._codex_rollout_state(str(rollout))["questionAt"] == "2026-10-02T19:53:21.675Z"
    rows.append({"type": "event_msg", "payload": {"type": "task_started"}})
    rollout.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    assert providers._codex_recorded_state(rollout)["status"] == "needs input"
    rows.append({"type": "response_item", "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "circle"}]}})
    rollout.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    ordinary = providers._codex_recorded_state(rollout)
    assert ordinary["status"] == "needs input" and ordinary["questionIds"]
    rows.append({"type": "response_item", "payload": {"type": "function_call", "name": "send_user_message_question_reply",
                 "arguments": json.dumps([{"questionItemId": ["request_user_input_async", "pending-turn", 0], "answer": "circle"}])}})
    rollout.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    answered = providers._codex_recorded_state(rollout)
    assert answered["status"] == "running" and answered["questionIds"] == []


def test_async_question_identity_requires_nonempty_exact_answer_and_survives_large_rollout(tmp_path):
    path = tmp_path / "correlated-question.jsonl"
    call = {"type": "response_item", "payload": {"type": "function_call", "name": "request_user_input_async",
            "call_id": "call-a", "arguments": json.dumps({"questions": [{"id": "native-q", "title": "Choose?"}]})}}
    identity = ["request_user_input_async", "call-a", 0]
    rows = [call, {"type": "event_msg", "payload": {"type": "large_irrelevant_record", "padding": "x" * (525 * 1024)}}]
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
    state = providers._codex_recorded_state(path)
    assert state["status"] == "needs input" and state["questionIds"] == [identity]

    rows.extend([
        {"type": "event_msg", "payload": {"type": "user_message", "message": "ordinary text"}},
        {"type": "response_item", "payload": {"type": "function_call_output", "call_id": "call-a", "output": "Question displayed"}},
        {"type": "response_item", "payload": {"type": "function_call", "name": "send_user_message_question_reply",
         "arguments": json.dumps([{"questionItemId": ["request_user_input_async", "wrong-call", 0], "answer": "No"}])}},
        {"type": "response_item", "payload": {"type": "function_call", "name": "send_user_message_question_reply",
         "arguments": json.dumps([{"questionItemId": identity, "answer": "  "}])}},
        {"type": "event_msg", "payload": {"type": "task_complete"}},
    ])
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
    state = providers._codex_recorded_state(path)
    assert state["status"] == "needs input" and state["questionIds"] == [identity]
    rows.append({"type": "response_item", "timestamp": datetime.now(timezone.utc).isoformat(), "payload": {"type": "function_call",
        "name": "send_user_message_question_reply", "arguments": json.dumps([{"questionItemId": identity, "answer": ["Yes", "also yes"]}])}})
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
    resolved = providers._codex_recorded_state(path)
    assert resolved["status"] == "idle" and resolved["questionIds"] == []
    assert resolved["questionResolutionAt"] is not None


def test_async_question_without_call_id_has_no_synthetic_identity_and_exact_supersession_resolves(tmp_path):
    path = tmp_path / "unavailable-correlation.jsonl"
    missing = {"type": "response_item", "payload": {"type": "function_call", "name": "request_user_input_async",
               "arguments": json.dumps({"questions": [{"title": "Uncorrelated?"}]})}}
    path.write_text(json.dumps(missing) + "\n", encoding="utf-8")
    state = providers._codex_recorded_state(path)
    assert state["status"] == "unknown" and state["question"] is None and state["questionIds"] == []
    assert state["questionCorrelationUnavailable"] is True

    call = {"type": "response_item", "payload": {"type": "function_call", "name": "request_user_input_async",
            "call_id": "call-b", "arguments": json.dumps({"questions": [{"title": "Exact?"}]})}}
    identity = ["request_user_input_async", "call-b", 0]
    final = {"type": "response_item", "timestamp": datetime.now(timezone.utc).isoformat(),
             "payload": {"type": "message", "role": "assistant", "channel": "final",
             "content": "[tracker-questions]" + json.dumps({"superseded": [identity]}) + "[/tracker-questions]"}}
    path.write_text("\n".join(json.dumps(row) for row in [call, final]) + "\n", encoding="utf-8")
    state = providers._codex_recorded_state(path)
    assert state["status"] == "idle" and state["questionIds"] == []
    assert state["questionResolutionAt"] is not None


def test_sync_question_requires_actual_answer_or_complete_native_mapping(tmp_path):
    path = tmp_path / "sync-question.jsonl"
    call = {"type": "response_item", "payload": {"type": "function_call", "name": "request_user_input",
            "call_id": "sync-a", "arguments": json.dumps({"questions": [{"id": "native-1", "question": "One?"},
                                                                    {"id": "native-2", "question": "Two?"}]})}}
    def output(value):
        return {"type": "response_item", "payload": {"type": "function_call_output", "call_id": "sync-a", "output": json.dumps(value)}}
    rows = [call, output({"accepted": True}), output({"answers": {"native-1": {"answers": ["A"]}}}), output({"answers": {"native-1": {"answers": ["A"]}, "native-2": {"answers": [""]}}})]
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
    assert providers._codex_recorded_state(path)["status"] == "needs input"
    rows.append(output({"answers": {"native-1": {"answers": ["A"]}, "native-2": {"answers": ["B"]}}}))
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
    state = providers._codex_recorded_state(path)
    assert state["status"] == "running" and state["question"] is None


def test_exactly_resolved_question_does_not_return_from_fresh_stale_app_attention(tmp_path):
    path = tmp_path / "resolved-but-stale-app.jsonl"
    now = datetime.now(timezone.utc)
    call_at, answer_at, terminal_at, app_at = [
        (now - timedelta(seconds=delta)).isoformat() for delta in (4, 3, 1, 2)
    ]
    identity = ["request_user_input_async", "resolve-call", 0]
    rows = [
        {"type": "response_item", "timestamp": call_at, "payload": {"type": "function_call",
         "name": "request_user_input_async", "call_id": "resolve-call",
         "arguments": json.dumps({"questions": [{"title": "Already answered?"}]})}},
        {"type": "response_item", "timestamp": answer_at, "payload": {"type": "function_call",
         "name": "send_user_message_question_reply",
         "arguments": json.dumps([{"questionItemId": identity, "answer": "Yes"}])}},
        {"type": "event_msg", "timestamp": terminal_at, "payload": {"type": "task_complete"}},
    ]
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
    chat = CodexProvider._thread({"id": "resolved-app-lag", "status": {"type": "needs input"},
                                  "statusObservedAt": app_at, "updatedAt": app_at, "path": str(path)})
    assert chat["status"] == "idle" and chat["statusSource"] == "transcript"
    assert chat["pendingQuestion"] is None and chat["pendingQuestionIds"] == []
    rows.append({"type": "event_msg", "timestamp": (now - timedelta(milliseconds=500)).isoformat(),
                 "payload": {"type": "task_started"}})
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
    restarted = CodexProvider._thread({"id": "resolved-app-lag", "status": {"type": "needs input"},
                                       "statusObservedAt": app_at, "updatedAt": app_at, "path": str(path)})
    assert restarted["status"] == "running" and restarted["statusSource"] == "transcript"
    assert restarted["pendingQuestion"] is None and restarted["pendingQuestionIds"] == []


def test_duplicate_async_call_deduplicates_native_alias_but_distinct_calls_remain_ambiguous(tmp_path):
    def ask(call_id):
        return {"type": "response_item", "payload": {"type": "function_call", "name": "request_user_input_async",
                "call_id": call_id, "arguments": json.dumps({"questions": [{"id": "shared-native", "title": "Choose?"}]})}}
    alias_answer = {"type": "response_item", "payload": {"type": "function_call", "name": "send_user_message_question_reply",
                     "arguments": json.dumps([{"questionItemId": "shared-native", "answer": "Yes"}])}}
    duplicate_path = tmp_path / "duplicate-call.jsonl"
    duplicate_path.write_text("\n".join(map(json.dumps, [ask("same-call"), ask("same-call"), alias_answer])) + "\n", encoding="utf-8")
    assert providers._codex_recorded_state(duplicate_path)["questionIds"] == []

    ambiguous_path = tmp_path / "distinct-calls.jsonl"
    ambiguous_path.write_text("\n".join(map(json.dumps, [ask("call-a"), ask("call-b"), alias_answer])) + "\n", encoding="utf-8")
    state = providers._codex_recorded_state(ambiguous_path)
    assert state["questionIds"] == [["request_user_input_async", "call-a", 0],
                                    ["request_user_input_async", "call-b", 0]]


def test_repeated_task_started_resets_running_clock_but_ordinary_activity_does_not(tmp_path):
    rollout = tmp_path / "restarted-run.jsonl"
    base = datetime.now(timezone.utc).replace(microsecond=0) - timedelta(minutes=2)
    at = lambda seconds: (base + timedelta(seconds=seconds)).isoformat().replace("+00:00", "Z")
    rows = [
        {"type": "event_msg", "timestamp": at(0), "payload": {"type": "task_started"}},
        {"type": "response_item", "timestamp": at(1), "payload": {"type": "function_call", "name": "tool.read", "arguments": "{}"}},
        {"type": "response_item", "timestamp": at(2), "payload": {"type": "function_call_output", "output": "read complete"}},
        {"type": "event_msg", "timestamp": at(3), "payload": {"type": "user_message", "message": "continue"}},
        {"type": "event_msg", "timestamp": at(4), "payload": {"type": "task_started"}},
        {"type": "response_item", "timestamp": at(5), "payload": {"type": "custom_tool_call_output", "output": "later output"}},
        {"type": "event_msg", "timestamp": at(6), "payload": {"type": "user_message", "message": "more context"}},
    ]
    rollout.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")

    state = providers._codex_recorded_state(rollout)

    assert state["status"] == "running"
    assert state["stateSince"] == at(4)


def test_codex_activity_identifies_literal_read_and_only_matching_output_clears_it(tmp_path):
    rollout = tmp_path / "activity-read.jsonl"
    base = datetime.now(timezone.utc).replace(microsecond=0) - timedelta(minutes=2)
    at = lambda seconds: (base + timedelta(seconds=seconds)).isoformat().replace("+00:00", "Z")
    rows = [
        {"type": "event_msg", "timestamp": at(0), "payload": {"type": "task_started"}},
        {"type": "response_item", "timestamp": at(1), "payload": {
            "type": "function_call", "name": "read_file", "call_id": "read-1",
            "arguments": json.dumps({"path": "src/voice.py"})}},
        {"type": "response_item", "timestamp": at(2), "payload": {
            "type": "function_call_output", "call_id": "other-call", "output": "unrelated"}},
    ]
    rollout.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
    state = providers._codex_recorded_state(rollout)
    assert state["activity"] == {"kind": "reading", "label": "Reading file", "files": ["src/voice.py"], "observedAt": at(1)}

    rows.append({"type": "response_item", "timestamp": at(3), "payload": {
        "type": "function_call_output", "call_id": "read-1", "output": "read complete"}})
    rollout.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
    assert providers._codex_recorded_state(rollout)["activity"] is None


def test_codex_activity_identifies_patch_and_clears_on_terminal_event(tmp_path):
    rollout = tmp_path / "activity-edit.jsonl"
    base = datetime.now(timezone.utc).replace(microsecond=0) - timedelta(minutes=2)
    at = lambda seconds: (base + timedelta(seconds=seconds)).isoformat().replace("+00:00", "Z")
    rows = [
        {"type": "event_msg", "timestamp": at(0), "payload": {"type": "task_started"}},
        {"type": "response_item", "timestamp": at(1), "payload": {
            "type": "custom_tool_call", "name": "apply_patch", "call_id": "patch-1",
            "input": "*** Begin Patch\n*** Update File: token_tracker/providers.py\n@@\n-x\n+y\n*** End Patch"}},
    ]
    rollout.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
    state = providers._codex_recorded_state(rollout)
    assert state["activity"] == {"kind": "editing", "label": "Editing file", "files": ["token_tracker/providers.py"], "observedAt": at(1)}

    rows.append({"type": "event_msg", "timestamp": at(2), "payload": {"type": "task_complete"}})
    rollout.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
    assert providers._codex_recorded_state(rollout)["activity"] is None


def test_codex_activity_supports_literal_exec_wrapper_and_explicit_thinking(tmp_path):
    rollout = tmp_path / "activity-exec.jsonl"
    base = datetime.now(timezone.utc).replace(microsecond=0) - timedelta(minutes=2)
    at = lambda seconds: (base + timedelta(seconds=seconds)).isoformat().replace("+00:00", "Z")
    rows = [
        {"type": "event_msg", "timestamp": at(0), "payload": {"type": "task_started"}},
        {"type": "response_item", "timestamp": at(1), "payload": {
            "type": "custom_tool_call", "name": "exec", "call_id": "exec-1",
            "input": {"code": 'await tools.exec_command({cmd: "Get-Content src/voice.py"})'}}},
    ]
    rollout.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
    state = providers._codex_recorded_state(rollout)
    assert state["activity"]["kind"] == "reading"
    assert state["activity"]["files"] == ["src/voice.py"]

    rows.append({"type": "response_item", "timestamp": at(2), "payload": {
        "type": "function_call_output", "call_id": "exec-1", "output": "done"}})
    rows.append({"type": "event_msg", "timestamp": at(3), "payload": {"type": "reasoning_started"}})
    rollout.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
    state = providers._codex_recorded_state(rollout)
    assert state["activity"] == {"kind": "thinking", "label": "Thinking", "files": [], "observedAt": at(3)}


def test_codex_activity_unknown_tool_is_visible_and_reasoning_does_not_override_it(tmp_path):
    rollout = tmp_path / "activity-unknown.jsonl"
    base = datetime.now(timezone.utc).replace(microsecond=0) - timedelta(minutes=2)
    at = lambda seconds: (base + timedelta(seconds=seconds)).isoformat().replace("+00:00", "Z")
    rows = [
        {"type": "event_msg", "timestamp": at(0), "payload": {"type": "task_started"}},
        {"type": "response_item", "timestamp": at(1), "payload": {
            "type": "function_call", "name": "opaque_tool", "call_id": "opaque-1", "arguments": "{}"}},
        {"type": "event_msg", "timestamp": at(2), "payload": {"type": "thinking_started"}},
    ]
    rollout.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
    state = providers._codex_recorded_state(rollout)
    assert state["activity"] == {"kind": "using_tool", "label": "Using tool", "files": [], "observedAt": at(1)}


def test_codex_activity_without_matching_call_id_stays_blank(tmp_path):
    rollout = tmp_path / "activity-no-id.jsonl"
    stamp = _recent_test_timestamp()
    _rollout(rollout, [
        {"type": "event_msg", "timestamp": stamp, "payload": {"type": "task_started"}},
        {"type": "response_item", "timestamp": _recent_test_timestamp(1), "payload": {
            "type": "function_call", "name": "read_file", "arguments": json.dumps({"path": "src/unknown.py"})}},
    ])
    assert providers._codex_recorded_state(rollout)["activity"] is None


def test_codex_activity_classifies_checks_searches_and_mixed_exec_wrappers(tmp_path):
    at = _recent_test_timestamp()
    check = providers._codex_tool_activity({"name": "exec", "arguments":
        'await tools.exec_command({cmd: "python -m pytest tests/test_tracker.py"})'}, at)
    assert check == {"kind": "testing", "label": "Running checks", "files": [], "observedAt": at}

    search = providers._codex_tool_activity({"name": "exec", "arguments":
        'await tools.exec_command({cmd: "rg TODO src/app.py"})'}, at)
    assert search == {"kind": "searching", "label": "Searching files", "files": ["src/app.py"], "observedAt": at}

    mixed = providers._codex_tool_activity({"name": "exec", "arguments":
        'await Promise.all([tools.exec_command({cmd: "rg TODO src/app.py"}), '
        'tools.exec_command({cmd: "Get-Content src/app.py"})])'}, at)
    assert mixed == {"kind": "running_command", "label": "Running command", "files": [], "observedAt": at}


def test_codex_activity_accepts_anchored_js_and_shell_declarations():
    at = _recent_test_timestamp()
    javascript = providers._codex_tool_activity({"name": "exec", "arguments": {
        "code": '// tracker-activity: {"kind":"reading","files":["token_tracker/providers.py"]}\n'
                'await tools.exec_command({cmd: "Get-Content token_tracker/providers.py"})'}}, at)
    assert javascript == {"kind": "reading", "label": "Reading file", "files": ["token_tracker/providers.py"],
                          "observedAt": at, "source": "declared"}

    shell = providers._codex_tool_activity({"name": "exec_command", "arguments": {
        "cmd": '# tracker-activity: {"kind":"testing","files":["tests/test_token_tracker.py"]}\n'
               'python -m pytest tests/test_token_tracker.py'}}, at)
    assert shell == {"kind": "testing", "label": "Running checks", "files": ["tests/test_token_tracker.py"],
                     "observedAt": at, "source": "declared"}

    pragma = providers._codex_tool_activity({"name": "functions.exec", "input": {
        "code": '// @exec: {"yield_time_ms": 1000}\n'
                '// tracker-activity: {"kind":"checking_data","files":["state_5.sqlite"]}\n'
                'await tools.exec_command({cmd: "Get-Item state_5.sqlite"})'}}, at)
    assert pragma == {"kind": "checking_data", "label": "Checking data", "files": ["state_5.sqlite"],
                      "observedAt": at, "source": "declared"}


def test_codex_activity_declarations_fail_closed_for_forged_or_unsafe_headers():
    at = _recent_test_timestamp()
    declarations = [
        'const example = 1;\n// tracker-activity: {"kind":"editing","files":["src/fake.py"]}\n'
        'await tools.exec_command({cmd: "Get-Content src/real.py"})',
        '// tracker-activity: {not-json}\nawait tools.exec_command({cmd: "Get-Content src/real.py"})',
        '// tracker-activity: {"kind":"invented","files":[]}\nawait tools.exec_command({cmd: "Get-Content src/real.py"})',
        '// tracker-activity: {"kind":[],"files":[]}\nawait tools.exec_command({cmd: "Get-Content src/real.py"})',
        '// tracker-activity: {"kind":{},"files":[]}\nawait tools.exec_command({cmd: "Get-Content src/real.py"})',
        '// tracker-activity: {"kind":"reading","files":[],"label":"Editing"}\n'
        'await tools.exec_command({cmd: "Get-Content src/real.py"})',
        '// tracker-activity: ' + json.dumps({"kind": "reading", "files": ["x" * 261]}) +
        '\nawait tools.exec_command({cmd: "Get-Content src/real.py"})',
        '// tracker-activity: {"kind":"reading","files":["https://example.test/private"]}\n'
        'await tools.exec_command({cmd: "Get-Content src/real.py"})',
        '// tracker-activity: {"kind":"reading","files":["api_key=sk-12345678901234567890"]}\n'
        'await tools.exec_command({cmd: "Get-Content src/real.py"})',
        '// tracker-activity: ' + json.dumps({"kind": "reading", "files": [f"src/{i}.py" for i in range(17)]}) +
        '\nawait tools.exec_command({cmd: "Get-Content src/real.py"})',
    ]
    for source in declarations:
        activity = providers._codex_tool_activity({"name": "exec", "arguments": source}, at)
        assert activity["kind"] == "reading"
        assert activity["files"] == ["src/real.py"]
        assert "source" not in activity


def test_codex_activity_nested_declarations_require_a_complete_consistent_batch():
    at = _recent_test_timestamp()
    matching = providers._codex_tool_activity({"name": "exec", "arguments":
        'await Promise.all([tools.exec_command({cmd: "# tracker-activity: {\\"kind\\":\\"reading\\",\\"files\\":[\\"src/a.py\\"]}\\nGet-Content src/a.py"}), '
        'tools.exec_command({cmd: "# tracker-activity: {\\"kind\\":\\"reading\\",\\"files\\":[\\"src/b.py\\"]}\\nGet-Content src/b.py"})])'}, at)
    assert matching == {"kind": "reading", "label": "Reading file", "files": ["src/a.py", "src/b.py"],
                        "observedAt": at, "source": "declared"}

    single_quoted = providers._codex_tool_activity({"name": "exec", "arguments":
        "await tools.exec_command({cmd: '# tracker-activity: {\"kind\":\"reading\",\"files\":[\"src/c.py\"]}\\nGet-Content src/c.py'})"}, at)
    assert single_quoted["kind"] == "reading"
    assert single_quoted["files"] == ["src/c.py"]
    assert single_quoted["source"] == "declared"

    mixed = providers._codex_tool_activity({"name": "exec", "arguments":
        'await Promise.all([tools.exec_command({cmd: "# tracker-activity: {\\"kind\\":\\"reading\\",\\"files\\":[\\"src/a.py\\"]}\\nGet-Content src/a.py"}), '
        'tools.exec_command({cmd: "# tracker-activity: {\\"kind\\":\\"testing\\",\\"files\\":[\\"tests/test_a.py\\"]}\\npython -m pytest tests/test_a.py"})])'}, at)
    assert mixed["kind"] == "running_code"
    assert mixed["files"] == []
    assert "source" not in mixed

    partial = providers._codex_tool_activity({"name": "exec", "arguments":
        'await Promise.all([tools.exec_command({cmd: "# tracker-activity: {\\"kind\\":\\"reading\\",\\"files\\":[\\"src/a.py\\"]}\\nGet-Content src/a.py"}), '
        'tools.exec_command({cmd: "Get-Content src/b.py"})])'}, at)
    assert partial["kind"] == "running_code"
    assert partial["files"] == []
    assert "source" not in partial


def test_codex_activity_nested_scanner_ignores_quoted_and_commented_examples():
    at = _recent_test_timestamp()
    source = ('// tools.exec_command({cmd: "# tracker-activity: {\\"kind\\":\\"editing\\",\\"files\\":[\\"fake.py\\"]}"})\n'
              'const sample = "tools.exec_command({cmd: \'# tracker-activity: {\\\"kind\\\":\\\"editing\\\",\\\"files\\\":[\\\"fake.py\\\"]}\'})";\n'
              'await tools.exec_command({cmd: "Get-Content src/real.py"})')
    activity = providers._codex_tool_activity({"name": "exec", "arguments": source}, at)
    assert activity["kind"] == "reading"
    assert activity["files"] == ["src/real.py"]
    assert "source" not in activity


def test_codex_activity_dynamic_exec_command_values_invalidate_nested_declarations():
    at = _recent_test_timestamp()
    concatenated = providers._codex_tool_activity({"name": "exec", "arguments":
        'await tools.exec_command({cmd: "# tracker-activity: {\\"kind\\":\\"reading\\",\\"files\\":[\\"src/a.py\\"]}\\nGet-Content src/a.py" + suffix})'}, at)
    assert concatenated["kind"] == "running_code"
    assert "source" not in concatenated

    mixed_dynamic = providers._codex_tool_activity({"name": "exec", "arguments":
        'await Promise.all([tools.exec_command({cmd: "# tracker-activity: {\\"kind\\":\\"reading\\",\\"files\\":[\\"src/a.py\\"]}\\nGet-Content src/a.py"}), '
        'tools.exec_command({cmd: makeCommand()})])'}, at)
    assert mixed_dynamic["kind"] == "running_code"
    assert "source" not in mixed_dynamic


def test_codex_declared_activity_stays_start_evidence_and_late_output_does_not_resurrect(tmp_path):
    rollout = tmp_path / "activity-declared-lifecycle.jsonl"
    base = datetime.now(timezone.utc).replace(microsecond=0) - timedelta(minutes=2)
    at = lambda seconds: (base + timedelta(seconds=seconds)).isoformat().replace("+00:00", "Z")
    rows = [
        {"type": "event_msg", "timestamp": at(0), "payload": {"type": "task_started"}},
        {"type": "response_item", "timestamp": at(1), "payload": {
            "type": "function_call", "name": "exec_command", "call_id": "declared-1",
            "arguments": {"cmd": '# tracker-activity: {"kind":"reading","files":["src/a.py"]}\nGet-Content src/a.py'}}},
    ]
    _rollout(rollout, rows)
    state = providers._codex_recorded_state(rollout)
    assert state["status"] == "running"
    assert state["activity"] == {"kind": "reading", "label": "Reading file", "files": ["src/a.py"],
                                  "observedAt": at(1), "source": "declared"}

    rows.append({"type": "response_item", "timestamp": at(2), "payload": {
        "type": "function_call_output", "call_id": "declared-1", "output": "read complete"}})
    _rollout(rollout, rows)
    assert providers._codex_recorded_state(rollout)["activity"] is None

    rows.extend([
        {"type": "response_item", "timestamp": at(3), "payload": {
            "type": "function_call", "name": "exec_command", "call_id": "declared-late",
            "arguments": {"cmd": '# tracker-activity: {"kind":"reading","files":["src/b.py"]}\nGet-Content src/b.py'}}},
        {"type": "event_msg", "timestamp": at(4), "payload": {"type": "task_complete"}},
        {"type": "response_item", "timestamp": at(5), "payload": {
            "type": "function_call_output", "call_id": "declared-late", "output": "late result"}},
    ])
    _rollout(rollout, rows)
    state = providers._codex_recorded_state(rollout)
    assert state["status"] == "idle"
    assert state["activity"] is None


def test_codex_activity_classifies_known_browser_agent_and_data_tools(tmp_path):
    at = _recent_test_timestamp()
    assert providers._codex_tool_activity({"name": "mcp__codex_app__open_in_codex"}, at)["label"] == "Inspecting page"
    assert providers._codex_tool_activity({"name": "mcp__codex_app__spawn_agent"}, at)["label"] == "Coordinating agents"
    assert providers._codex_tool_activity({"name": "sql_query"}, at)["label"] == "Checking data"


def test_codex_activity_includes_only_bounded_plain_tool_titles():
    at = _recent_test_timestamp()
    call = {"name": "node_repl__js", "arguments": {"title": "Inspect native widget", "code": "await inspect()"}}
    activity = providers._codex_tool_activity(call, at)
    assert activity == {"kind": "using_tool", "label": "Using tool", "files": [],
                       "observedAt": at, "detail": "Inspect native widget"}
    for title in ("Import settings", "Return to widget", "Check function labels"):
        accepted = providers._codex_tool_activity({"name": "node_repl__js", "arguments": {"title": title}}, at)
        assert accepted["detail"] == title

    unsafe_titles = [
        "Inspect widget\nawait exec_command()",
        "await tools.exec_command({cmd: 'whoami'})",
        "const x = 1",
        "let result = inspect()",
        "var value = 2",
        "import requests from 'module'",
        "import('module')",
        "return inspect()",
        "node_repl({})",
        "x" * 81,
        {"text": "Inspect native widget"},
        "Use token sk-1234567890abcdefghijkl",
    ]
    for title in unsafe_titles:
        payload = {"name": "node_repl__js", "arguments": {"title": title, "code": "private code"}}
        assert "detail" not in providers._codex_tool_activity(payload, at)

    # Command and code arguments alone are not promoted into user-facing details.
    command = providers._codex_tool_activity({"name": "exec", "arguments": {
        "cmd": "Get-Content private.txt", "code": "private implementation",
    }}, at)
    assert "detail" not in command


def test_codex_activity_title_clears_with_matching_output_or_run_end(tmp_path):
    base = datetime.now(timezone.utc).replace(microsecond=0) - timedelta(minutes=2)
    at = lambda seconds: (base + timedelta(seconds=seconds)).isoformat().replace("+00:00", "Z")
    call = {"type": "response_item", "timestamp": at(1), "payload": {
        "type": "function_call", "name": "node_repl__js", "call_id": "inspect-1",
        "arguments": json.dumps({"title": "Inspect native widget", "code": "await inspect()"}),
    }}
    start = {"type": "event_msg", "timestamp": at(0), "payload": {"type": "task_started"}}

    matched = tmp_path / "activity-title-matched.jsonl"
    rows = [start, call]
    matched.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
    state = providers._codex_recorded_state(matched)
    assert state["activity"]["detail"] == "Inspect native widget"
    rows.append({"type": "response_item", "timestamp": at(2), "payload": {
        "type": "function_call_output", "call_id": "inspect-1", "output": "done",
    }})
    matched.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
    assert providers._codex_recorded_state(matched)["activity"] is None

    ended = tmp_path / "activity-title-ended.jsonl"
    rows = [start, call, {"type": "event_msg", "timestamp": at(2), "payload": {"type": "task_complete"}}]
    ended.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
    assert providers._codex_recorded_state(ended)["activity"] is None


def test_codex_activity_exec_patch_requires_an_actual_literal_call(tmp_path):
    at = _recent_test_timestamp()
    actual = providers._codex_tool_activity({"name": "exec", "arguments":
        'await tools.apply_patch("*** Begin Patch\\n*** Update File: src/live.py\\n@@\\n-old\\n+new\\n*** End Patch")'}, at)
    assert actual == {"kind": "editing", "label": "Editing file", "files": ["src/live.py"], "observedAt": at}

    example = providers._codex_tool_activity({"name": "exec", "arguments":
        'const example = "tools.apply_patch(\\\"*** Begin Patch\\n*** Update File: fake.py\\\")";'}, at)
    assert example == {"kind": "running_code", "label": "Running code", "files": [], "observedAt": at}


def test_future_run_records_do_not_anchor_a_truncated_rollout(tmp_path):
    rollout = tmp_path / "future-run.jsonl"
    future = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat().replace("+00:00", "Z")
    rollout.write_text("x" * 530_000 + "\n" + json.dumps({
        "type": "event_msg", "timestamp": future, "payload": {"type": "task_started"}
    }) + "\n", encoding="utf-8")
    assert providers._codex_run_stats(rollout).get("runStart") is None
    assert providers._codex_recorded_state(rollout)["status"] == "unknown"


def test_session_insights_ignore_future_model_request_message_and_compaction_records(tmp_path):
    rollout = tmp_path / "future-insights.jsonl"
    now = datetime.now(timezone.utc)
    at = lambda seconds: (now - timedelta(minutes=2) + timedelta(seconds=seconds)).isoformat().replace("+00:00", "Z")
    future = (now + timedelta(hours=1)).isoformat().replace("+00:00", "Z")
    _rollout(rollout, [
        {"type": "session_meta", "timestamp": at(0), "payload": {"id": "child", "parent_thread_id": "parent"}},
        {"type": "turn_context", "timestamp": at(1), "payload": {"model": "model-current"}},
        {"type": "event_msg", "timestamp": at(2), "payload": {"type": "user_message", "message": "current request"}},
        {"type": "response_item", "timestamp": at(3), "payload": {
            "type": "agent_message", "author": "parent", "recipient": "child", "content": "current"}},
        {"type": "compacted", "timestamp": at(4), "payload": {}},
        {"type": "turn_context", "timestamp": future, "payload": {"model": "model-future"}},
        {"type": "event_msg", "timestamp": future, "payload": {"type": "user_message", "message": "future request"}},
        {"type": "response_item", "timestamp": future, "payload": {
            "type": "message", "role": "assistant", "phase": "commentary",
            "content": [{"text": "future update"}]}},
        {"type": "response_item", "timestamp": future, "payload": {
            "type": "agent_message", "author": "parent", "recipient": "child", "content": "future"}},
        {"type": "compacted", "timestamp": future, "payload": {}},
    ])
    insights = providers._session_insights(rollout)
    assert insights["model"] == "model-current"
    assert insights["latestRequest"] == "current request"
    assert insights["latestMessageAt"] == at(3)
    assert insights["latestMessageDirection"] == "parent-to-agent"
    assert insights.get("latestUpdate") is None
    assert insights["compactions"] == 1


def test_path_caches_refresh_on_file_replacement_and_missing_recreation(tmp_path):
    path = tmp_path / "cache-identity.jsonl"
    path.write_text("old", encoding="utf-8")
    original_stat = path.stat()
    original_stamp = providers._stat_fingerprint(path)
    assert providers._file_memo("identity-test", path, path.read_text) == "old"

    replacement = tmp_path / "replacement.jsonl"
    replacement.write_text("new", encoding="utf-8")
    os.utime(replacement, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))
    os.replace(replacement, path)
    assert path.stat().st_size == original_stat.st_size
    assert path.stat().st_mtime_ns == original_stat.st_mtime_ns
    assert providers._stat_fingerprint(path) != original_stamp
    assert providers._file_memo("identity-test", path, path.read_text) == "new"

    path.unlink()
    assert providers._file_memo("identity-test", path, lambda: "missing") == "missing"
    assert ("identity-test", str(path)) not in providers._FILE_MEMO
    path.write_text("old", encoding="utf-8")
    assert providers._file_memo("identity-test", path, path.read_text) == "old"


def test_insight_and_token_event_caches_evict_missing_paths_and_detect_same_mtime_append(tmp_path):
    rollout = tmp_path / "cache-currentness.jsonl"
    stamp = _recent_test_timestamp()

    def row(request, total):
        return {"type": "event_msg", "timestamp": stamp, "payload": {
            "type": "user_message", "message": request,
            "info": {"total_token_usage": {"total_tokens": total}}}}

    _rollout(rollout, [row("old", 10)])
    original_stat = rollout.stat()
    assert providers._session_insights(rollout)["latestRequest"] == "old"
    assert providers.CodexProvider._token_events(rollout)[0]["usage"]["total_tokens"] == 10

    with rollout.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row("new", 20)) + "\n")
    os.utime(rollout, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))
    assert providers._session_insights(rollout)["latestRequest"] == "new"
    events = providers.CodexProvider._token_events(rollout)
    assert [event["usage"]["total_tokens"] for event in events] == [10, 20]

    resolved = str(rollout.resolve())
    appended_stat = rollout.stat()
    rollout.unlink()
    assert providers._session_insights(rollout) == {}
    assert providers.CodexProvider._token_events(rollout) == []
    assert ("codex-session-insights", resolved) not in providers._JSONL_CHECKPOINTS
    assert not any(key[0] == resolved for key in providers.CodexProvider._token_event_cache)

    _rollout(rollout, [row("old", 10), row("alt", 21)])
    os.utime(rollout, ns=(appended_stat.st_atime_ns, appended_stat.st_mtime_ns))
    assert rollout.stat().st_size == appended_stat.st_size
    assert rollout.stat().st_mtime_ns == appended_stat.st_mtime_ns
    assert providers._session_insights(rollout)["latestRequest"] == "alt"
    events = providers.CodexProvider._token_events(rollout)
    assert [event["usage"]["total_tokens"] for event in events] == [10, 21]


def test_codex_activity_old_output_cannot_resurrect_previous_run_call(tmp_path):
    rollout = tmp_path / "activity-old-output.jsonl"
    base = datetime.now(timezone.utc).replace(microsecond=0) - timedelta(minutes=2)
    at = lambda seconds: (base + timedelta(seconds=seconds)).isoformat().replace("+00:00", "Z")
    rows = [
        {"type": "event_msg", "timestamp": at(0), "payload": {"type": "task_started"}},
        {"type": "response_item", "timestamp": at(1), "payload": {
            "type": "function_call", "name": "read_file", "call_id": "old-read",
            "arguments": json.dumps({"path": "old.py"})}},
        {"type": "event_msg", "timestamp": at(2), "payload": {"type": "task_started"}},
        {"type": "response_item", "timestamp": at(3), "payload": {
            "type": "function_call_output", "call_id": "old-read", "output": "late output"}},
    ]
    rollout.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
    assert providers._codex_recorded_state(rollout)["activity"] is None


def test_late_unmatched_output_keeps_completed_run_idle_until_new_start(tmp_path):
    rollout = tmp_path / "late-output-after-complete.jsonl"
    at = lambda seconds: _recent_test_timestamp(seconds)
    rows = [
        {"type": "event_msg", "timestamp": at(0), "payload": {"type": "task_started"}},
        {"type": "event_msg", "timestamp": at(1), "payload": {"type": "task_complete"}},
        {"type": "response_item", "timestamp": at(2), "payload": {
            "type": "function_call_output", "call_id": "old-call", "output": "late result"}},
    ]
    _rollout(rollout, rows)
    assert providers._codex_recorded_state(rollout)["status"] == "idle"

    started = at(3)
    rows.append({"type": "event_msg", "timestamp": started, "payload": {"type": "task_started"}})
    _rollout(rollout, rows)
    state = providers._codex_recorded_state(rollout)
    assert state["status"] == "running"
    assert state["stateSince"] == started


def test_late_unmatched_output_keeps_failed_run_failed_until_user_message(tmp_path):
    rollout = tmp_path / "late-output-after-failure.jsonl"
    at = lambda seconds: _recent_test_timestamp(seconds)
    rows = [
        {"type": "event_msg", "timestamp": at(0), "payload": {"type": "task_started"}},
        {"type": "event_msg", "timestamp": at(1), "payload": {"type": "task_failed", "message": "failed"}},
        {"type": "response_item", "timestamp": at(2), "payload": {
            "type": "custom_tool_call_output", "call_id": "old-call", "output": "late result"}},
    ]
    _rollout(rollout, rows)
    assert providers._codex_recorded_state(rollout)["status"] == "failed"

    resumed = at(3)
    rows.append({"type": "event_msg", "timestamp": resumed,
                 "payload": {"type": "user_message", "message": "continue"}})
    _rollout(rollout, rows)
    state = providers._codex_recorded_state(rollout)
    assert state["status"] == "running"
    assert state["stateSince"] == resumed


def test_compaction_state_is_live_until_compaction_boundary(tmp_path):
    rollout = tmp_path / "rollout.jsonl"
    base = datetime.now(timezone.utc).replace(microsecond=0) - timedelta(minutes=2)
    at = lambda seconds: (base + timedelta(seconds=seconds)).isoformat().replace("+00:00", "Z")
    rows = [{"type": "event_msg", "timestamp": at(0), "payload": {"type": "context_compaction_started"}}]
    rollout.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
    state = providers._codex_recorded_state(rollout)
    assert state["status"] == "compacting"
    assert state["stateSince"] == at(0)
    assert state["compactionStartedAt"] == at(0)
    rows.append({"type": "system", "timestamp": at(5), "subtype": "compact_boundary"})
    rollout.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
    state = providers._codex_recorded_state(rollout)
    assert state["status"] == "running"
    assert state["stateSince"] == at(5)
    assert state["compactionStartedAt"] is None


def test_silent_codex_tool_remains_running_when_fresh_app_snapshot_confirms_it(tmp_path):
    rollout = tmp_path / "rollout.jsonl"
    task_at = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat().replace("+00:00", "Z")
    rollout.write_text(json.dumps({"type": "event_msg", "timestamp": task_at,
                                   "payload": {"type": "task_started"}}) + "\n", encoding="utf-8")
    old = time.time() - 600
    os.utime(rollout, (old, old))
    observed = datetime.now(timezone.utc).isoformat()
    thread = CodexProvider._thread({"id": "silent", "status": "running", "statusObservedAt": observed,
                                    "path": str(rollout)})
    assert thread["status"] == "running"
    assert thread["statusSource"] == "app"
    assert thread["stateSince"] == task_at


def test_completed_child_terminal_beats_fresh_app_status_until_a_new_run(tmp_path):
    rollout = tmp_path / "completed-child.jsonl"
    now = datetime.now(timezone.utc).replace(microsecond=0)
    started = (now - timedelta(seconds=20)).isoformat().replace("+00:00", "Z")
    completed = (now - timedelta(seconds=10)).isoformat().replace("+00:00", "Z")
    fresh = now.isoformat().replace("+00:00", "Z")
    rows = [
        {"type": "session_meta", "payload": {"id": "child", "parent_thread_id": "parent"}},
        {"type": "event_msg", "timestamp": started, "payload": {"type": "task_started"}},
        {"type": "event_msg", "timestamp": completed, "payload": {"type": "task_complete"}},
    ]
    _rollout(rollout, rows)
    full = CodexProvider._thread({"id": "child", "parentThreadId": "parent", "status": "running",
                                  "statusObservedAt": fresh, "path": str(rollout)})
    assert full["status"] == "idle"
    providers._index_live_paths([{"name": "Codex", "chats": [full]}])
    live = next(row for row in providers.live_state()["chats"] if row["key"] == "Codex:child")
    assert live["status"] == "idle"

    restarted = (now - timedelta(seconds=5)).isoformat().replace("+00:00", "Z")
    rows.append({"type": "event_msg", "timestamp": restarted, "payload": {"type": "task_started"}})
    _rollout(rollout, rows)
    full = CodexProvider._thread({"id": "child", "parentThreadId": "parent", "status": "running",
                                  "statusObservedAt": fresh, "path": str(rollout)})
    assert full["status"] == "running"
    providers._index_live_paths([{"name": "Codex", "chats": [full]}])
    live = next(row for row in providers.live_state()["chats"] if row["key"] == "Codex:child")
    assert live["status"] == "running"


def test_app_running_does_not_reuse_a_completed_state_clock(tmp_path):
    rollout = tmp_path / "old-complete.jsonl"
    old_completion = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
    rollout.write_text(json.dumps({"type": "event_msg", "timestamp": old_completion,
                                   "payload": {"type": "task_complete"}}) + "\n", encoding="utf-8")
    observed = datetime.now(timezone.utc).isoformat()
    thread = CodexProvider._thread({"id": "resumed", "status": "running",
                                    "statusObservedAt": observed, "path": str(rollout)})
    assert thread["status"] == "running"
    assert thread["stateSince"] is None


def test_future_terminal_timestamp_cannot_override_fresh_app_running(tmp_path):
    rollout = tmp_path / "future-complete.jsonl"
    future_completion = (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat()
    rollout.write_text(json.dumps({"type": "event_msg", "timestamp": future_completion,
                                   "payload": {"type": "task_complete"}}) + "\n", encoding="utf-8")
    observed = datetime.now(timezone.utc).isoformat()
    thread = CodexProvider._thread({"id": "future-complete", "status": "running",
                                    "statusObservedAt": observed, "path": str(rollout)})
    assert thread["status"] == "running"
    assert thread["stateSince"] is None
    providers._index_live_paths([{"name": "Codex", "chats": [thread]}])
    live = next(row for row in providers.live_state()["chats"] if row.get("key") == "Codex:future-complete")
    assert live["status"] == "running"


def test_future_app_observation_is_not_treated_as_fresh(tmp_path):
    rollout = tmp_path / "stale.jsonl"
    rollout.write_text(json.dumps({"type": "event_msg", "timestamp": "2026-10-07T08:00:00Z",
                                   "payload": {"type": "task_started"}}) + "\n", encoding="utf-8")
    old = time.time() - 600
    os.utime(rollout, (old, old))
    observed = (datetime.now(timezone.utc) + timedelta(seconds=5)).isoformat()
    thread = CodexProvider._thread({"id": "uncertain", "status": "running",
                                    "statusObservedAt": observed, "path": str(rollout)})
    assert thread["status"] == "unavailable"
    assert thread["statusSource"] == "unavailable"


def test_terminal_transcript_written_after_app_observation_wins(tmp_path):
    rollout = tmp_path / "rollout.jsonl"
    observed = datetime.now(timezone.utc) - timedelta(seconds=2)
    rows = [
        {"type": "event_msg", "timestamp": (observed + timedelta(milliseconds=100)).isoformat(),
         "payload": {"type": "task_started"}},
        {"type": "event_msg", "timestamp": (observed + timedelta(seconds=1)).isoformat(),
         "payload": {"type": "task_complete"}},
    ]
    rollout.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
    thread = CodexProvider._thread({"id": "finished", "status": "running",
                                    "statusObservedAt": observed.isoformat(), "path": str(rollout)})
    assert thread["status"] == "idle"
    assert thread["statusSource"] == "transcript"
    assert thread["stateSince"] == rows[-1]["timestamp"]


def test_truncated_rollout_uses_full_run_ledger_for_state_clock(tmp_path):
    rollout = tmp_path / "large-running.jsonl"
    started_at = _recent_test_timestamp()
    started = {"type": "event_msg", "timestamp": started_at,
               "payload": {"type": "task_started"}}
    filler = {"type": "response_item", "payload": {"type": "function_call_output", "output": "x" * 600_000}}
    rollout.write_text(json.dumps(started) + "\n" + json.dumps(filler) + "\n", encoding="utf-8")
    state = providers._codex_recorded_state(rollout)
    assert state["status"] == "running"
    assert providers._timestamp_epoch(state["stateSince"]) == providers._timestamp_epoch(started_at)
    assert state["terminalAt"] is None


def test_missing_codex_rollout_retains_prior_active_metadata_as_unavailable(tmp_path):
    rollout = tmp_path / "rollout.jsonl"
    rollout.write_text("{}", encoding="utf-8")
    providers._index_live_paths([{"name": "Codex", "chats": [{
        "id": "gone", "path": str(rollout), "status": "running", "statusSource": "app",
        "stateSince": "2026-10-07T09:00:00Z", "latestMessageAt": "2026-10-07T09:01:00Z",
    }]}])
    rollout.unlink()
    row = next(chat for chat in providers.live_state()["chats"] if chat["key"] == "Codex:gone")
    assert row["status"] == "running"
    assert row["telemetryUnavailable"] is False
    assert row["metricsUnavailable"] is True
    assert row["statusSource"] == "app"
    assert row["stateSince"] == "2026-10-07T09:00:00Z"
    assert row["latestMessageAt"] == "2026-10-07T09:01:00Z"


def test_codex_agent_messages_require_explicit_sender_and_recipient(tmp_path):
    rollout = tmp_path / "messages.jsonl"
    rows = [
        {"type": "session_meta", "payload": {"id": "child", "parent_thread_id": "root"}},
        {"type": "response_item", "timestamp": "2026-10-07T10:00:00Z", "payload": {
            "type": "agent_message", "author": "root", "recipient": "child", "content": "ignored"}},
    ]
    rollout.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
    info = providers._session_insights(rollout)
    assert info["latestMessageAt"] == "2026-10-07T10:00:00Z"
    assert info["latestMessageDirection"] == "parent-to-agent"
    assert info["latestMessageSourceId"] == "root"
    assert info["latestMessageTargetId"] == "child"
    rows.append({"type": "response_item", "timestamp": "2026-10-07T10:00:01Z",
                 "payload": {"type": "message", "role": "user", "content": [{"text": "answer"}]}})
    rollout.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
    info = providers._session_insights(rollout)
    assert info["latestMessageAt"] == "2026-10-07T10:00:01Z"
    assert info["latestMessageDirection"] is None
    assert info["latestMessageSourceId"] is None


def test_context_sample_keeps_zero_and_invalidates_after_compaction(tmp_path):
    rollout = tmp_path / "context.jsonl"
    first_stamp = _recent_test_timestamp(0)
    second_stamp = _recent_test_timestamp(1)
    rows = [
        {"type": "turn_context", "payload": {"model": "model-a"}},
        {"type": "event_msg", "timestamp": first_stamp, "payload": {"type": "token_count", "info": {
            "total_token_usage": {"total_tokens": 10}, "last_token_usage": {"total_tokens": 0},
            "model_context_window": 200}}},
        {"type": "turn_context", "payload": {"model": "model-b"}},
        {"type": "event_msg", "timestamp": second_stamp, "payload": {"type": "token_count", "info": {
            "total_token_usage": {"total_tokens": 10}, "last_token_usage": {"total_tokens": 0}}}},
    ]
    rollout.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
    initial = CodexProvider._thread({"id": "context", "path": str(rollout)})
    assert initial["contextModel"] == "model-b"
    assert initial["contextTokens"] == 0
    assert initial["contextWindow"] is None
    assert initial["contextPercent"] is None
    rows.append({"type": "turn_context", "payload": {"model": "model-c"}})
    rollout.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
    thread = CodexProvider._thread({"id": "context", "path": str(rollout)})
    assert thread["contextModel"] is None
    assert thread["contextTokens"] is None
    assert thread["contextWindow"] is None
    assert thread["contextPercent"] is None
    rows.append({"type": "event_msg", "timestamp": _recent_test_timestamp(2), "payload": {"type": "token_count", "info": {
        "total_token_usage": {"total_tokens": 20}, "last_token_usage": {"total_tokens": 1},
        "model_context_window": 100}}})
    rows.append({"type": "event_msg", "timestamp": _recent_test_timestamp(3),
                 "payload": {"type": "context_compaction_started"}})
    rollout.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
    thread = CodexProvider._thread({"id": "context", "path": str(rollout)})
    assert thread["contextTokens"] is None
    assert thread["contextWindow"] is None
    assert thread["contextModel"] is None
    assert thread["lastRequest"] == {"input": None, "cached": None, "output": None}


def test_same_model_turn_context_keeps_the_paired_sample_and_zero_cache(tmp_path):
    rollout = tmp_path / "same-model.jsonl"
    sampled = _recent_test_timestamp(0)
    rows = [
        {"type": "turn_context", "payload": {"model": "model-a"}},
        {"type": "event_msg", "timestamp": sampled, "payload": {"type": "token_count", "info": {
            "total_token_usage": {"total_tokens": 100},
            "last_token_usage": {"total_tokens": 50, "cached_input_tokens": 0},
            "model_context_window": 200}}},
        {"type": "turn_context", "payload": {"model": "model-a"}},
    ]
    rollout.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
    thread = CodexProvider._thread({"id": "same", "path": str(rollout)})
    assert thread["contextModel"] == "model-a"
    assert thread["contextTokens"] == 50
    assert thread["contextWindow"] == 200
    assert thread["contextPercent"] == 25
    assert thread["lastRequest"]["cached"] == 0


def test_display_model_mismatch_clears_context_and_last_request_pair(tmp_path):
    rollout = tmp_path / "mismatch.jsonl"
    rows = [
        {"type": "turn_context", "payload": {"model": "model-a"}},
        {"type": "event_msg", "timestamp": "2026-10-07T10:00:00Z", "payload": {"type": "token_count", "info": {
            "total_token_usage": {"total_tokens": 100},
            "last_token_usage": {"total_tokens": 50, "cached_input_tokens": 20},
            "model_context_window": 200}}},
    ]
    rollout.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
    thread = CodexProvider._thread({"id": "mismatch", "model": "model-b", "path": str(rollout)})
    assert thread["model"] == "model-b"
    assert thread["contextTokens"] is None
    assert thread["contextWindow"] is None
    assert thread["contextModel"] is None
    assert thread["lastRequest"] == {"input": None, "cached": None, "output": None}


def test_active_run_does_not_reuse_context_sample_from_before_task_started(tmp_path):
    rollout = tmp_path / "prior-run-context.jsonl"
    started = datetime.now(timezone.utc).replace(microsecond=0) - timedelta(minutes=2)
    old_sample = (started - timedelta(seconds=5)).isoformat().replace("+00:00", "Z")
    run_start = started.isoformat().replace("+00:00", "Z")
    rows = [
        {"type": "turn_context", "payload": {"model": "model-a"}},
        {"type": "event_msg", "timestamp": old_sample, "payload": {"type": "token_count", "info": {
            "total_token_usage": {"total_tokens": 100}, "last_token_usage": {"total_tokens": 75},
            "model_context_window": 200}}},
        {"type": "event_msg", "timestamp": run_start, "payload": {"type": "task_started"}},
    ]
    _rollout(rollout, rows)
    observed = datetime.now(timezone.utc).isoformat()
    thread = CodexProvider._thread({"id": "new-run", "status": "running", "statusObservedAt": observed,
                                    "path": str(rollout)})
    assert thread["contextTokens"] is None
    assert thread["contextWindow"] is None
    assert thread["contextPercent"] is None
    assert thread["contextModel"] is None
    assert thread["contextObservedAt"] is None
    assert thread["lastRequest"] == {"input": None, "cached": None, "output": None}

    providers._index_live_paths([{"name": "Codex", "chats": [thread]}])
    live = next(row for row in providers.live_state()["chats"] if row["key"] == "Codex:new-run")
    assert live["contextTokens"] is None
    assert live["contextWindow"] is None
    assert live["contextPercent"] is None
    assert live["contextModel"] is None
    assert live["contextObservedAt"] is None
    assert live["lastRequest"] == {"input": None, "cached": None, "output": None}


def test_active_run_keeps_zero_context_from_a_fresh_sample(tmp_path):
    rollout = tmp_path / "fresh-zero-context.jsonl"
    started = datetime.now(timezone.utc).replace(microsecond=0) - timedelta(minutes=2)
    run_start = started.isoformat().replace("+00:00", "Z")
    sampled = (started + timedelta(seconds=1)).isoformat().replace("+00:00", "Z")
    _rollout(rollout, [
        {"type": "turn_context", "payload": {"model": "model-a"}},
        {"type": "event_msg", "timestamp": run_start, "payload": {"type": "task_started"}},
        {"type": "event_msg", "timestamp": sampled, "payload": {"type": "token_count", "info": {
            "total_token_usage": {"total_tokens": 10}, "last_token_usage": {"total_tokens": 0},
            "model_context_window": 200}}},
    ])
    thread = CodexProvider._thread({"id": "fresh-zero", "status": "running",
                                    "statusObservedAt": datetime.now(timezone.utc).isoformat(), "path": str(rollout)})
    assert thread["contextTokens"] == 0
    assert thread["contextWindow"] == 200
    assert thread["contextPercent"] == 0
    assert thread["contextObservedAt"] == sampled


def test_active_run_keeps_context_sample_across_ordinary_tool_call(tmp_path):
    rollout = tmp_path / "same-run-context.jsonl"
    started = datetime.now(timezone.utc).replace(microsecond=0) - timedelta(minutes=2)
    run_start = started.isoformat().replace("+00:00", "Z")
    sampled = (started + timedelta(seconds=1)).isoformat().replace("+00:00", "Z")
    _rollout(rollout, [
        {"type": "turn_context", "payload": {"model": "model-a"}},
        {"type": "event_msg", "timestamp": run_start, "payload": {"type": "task_started"}},
        {"type": "event_msg", "timestamp": sampled, "payload": {"type": "token_count", "info": {
            "total_token_usage": {"total_tokens": 100}, "last_token_usage": {"total_tokens": 60},
            "model_context_window": 200}}},
        {"type": "response_item", "timestamp": (started + timedelta(seconds=2)).isoformat().replace("+00:00", "Z"), "payload": {
            "type": "function_call", "name": "read_file", "call_id": "read-1", "arguments": "{}"}},
        {"type": "response_item", "timestamp": (started + timedelta(seconds=3)).isoformat().replace("+00:00", "Z"), "payload": {
            "type": "function_call_output", "call_id": "read-1", "output": "read complete"}},
    ])
    thread = CodexProvider._thread({"id": "same-run", "status": "running",
                                    "statusObservedAt": datetime.now(timezone.utc).isoformat(), "path": str(rollout)})
    assert thread["contextTokens"] == 60
    assert thread["contextWindow"] == 200
    assert thread["contextPercent"] == 30
    assert thread["contextObservedAt"] == sampled


def test_active_run_accepts_a_new_context_sample_after_task_started(tmp_path):
    rollout = tmp_path / "post-start-context.jsonl"
    started = datetime.now(timezone.utc).replace(microsecond=0) - timedelta(minutes=2)
    old_sample = (started - timedelta(seconds=5)).isoformat().replace("+00:00", "Z")
    run_start = started.isoformat().replace("+00:00", "Z")
    sampled = (started + timedelta(seconds=5)).isoformat().replace("+00:00", "Z")
    _rollout(rollout, [
        {"type": "turn_context", "payload": {"model": "model-a"}},
        {"type": "event_msg", "timestamp": old_sample, "payload": {"type": "token_count", "info": {
            "total_token_usage": {"total_tokens": 50}, "last_token_usage": {"total_tokens": 40},
            "model_context_window": 200}}},
        {"type": "event_msg", "timestamp": run_start, "payload": {"type": "task_started"}},
        {"type": "event_msg", "timestamp": sampled, "payload": {"type": "token_count", "info": {
            "total_token_usage": {"total_tokens": 55}, "last_token_usage": {"total_tokens": 5},
            "model_context_window": 200}}},
    ])
    thread = CodexProvider._thread({"id": "new-sample", "status": "running",
                                    "statusObservedAt": datetime.now(timezone.utc).isoformat(), "path": str(rollout)})
    assert thread["contextTokens"] == 5
    assert thread["contextObservedAt"] == sampled


def test_context_delta_tracks_signed_samples_and_resets_at_model_and_compaction_epochs(tmp_path):
    rollout = tmp_path / "context-delta-epochs.jsonl"
    start = datetime.now(timezone.utc).replace(microsecond=0) - timedelta(seconds=40)
    at = lambda seconds: (start + timedelta(seconds=seconds)).isoformat().replace("+00:00", "Z")

    def sample(seconds, total, capacity=200):
        return {"type": "event_msg", "timestamp": at(seconds), "payload": {"type": "token_count", "info": {
            "total_token_usage": {"total_tokens": 1000 + seconds},
            "last_token_usage": {"total_tokens": total}, "model_context_window": capacity}}}

    rows = [
        {"type": "turn_context", "payload": {"model": "model-a"}},
        {"type": "event_msg", "timestamp": at(0), "payload": {"type": "task_started"}},
        sample(1, 40), sample(2, 55), sample(3, 50),
        {"type": "event_msg", "timestamp": at(4), "payload": {"type": "context_compaction_started"}},
        sample(5, 75), sample(6, 80),
        {"type": "turn_context", "timestamp": at(7), "payload": {"model": "model-b"}},
        sample(8, 10, 100), sample(9, 12, 100),
    ]
    _rollout(rollout, rows)
    stats = providers._read_codex_run_stats(rollout)
    assert stats["contextDelta"] == 2
    assert stats["contextDeltaObservedAt"] == at(9)
    assert stats["modelChangeAt"] == at(7)

    rows = rows[:5]
    _rollout(rollout, rows)
    stats = providers._read_codex_run_stats(rollout)
    assert stats["contextDelta"] == -5
    assert stats["contextDeltaObservedAt"] == at(3)
    assert stats["modelChangeAt"] is None


def test_context_delta_and_model_change_timestamp_match_full_and_live_rows(tmp_path):
    rollout = tmp_path / "context-delta-live.jsonl"
    started = datetime.now(timezone.utc).replace(microsecond=0) - timedelta(seconds=10)
    at = lambda seconds: (started + timedelta(seconds=seconds)).isoformat().replace("+00:00", "Z")
    _rollout(rollout, [
        {"type": "turn_context", "payload": {"model": "model-a"}},
        {"type": "event_msg", "timestamp": at(0), "payload": {"type": "task_started"}},
        {"type": "event_msg", "timestamp": at(1), "payload": {"type": "token_count", "info": {
            "total_token_usage": {"total_tokens": 100}, "last_token_usage": {"total_tokens": 20},
            "model_context_window": 200}}},
        {"type": "turn_context", "timestamp": at(2), "payload": {"model": "model-b"}},
        {"type": "event_msg", "timestamp": at(3), "payload": {"type": "token_count", "info": {
            "total_token_usage": {"total_tokens": 110}, "last_token_usage": {"total_tokens": 30},
            "model_context_window": 100}}},
        {"type": "event_msg", "timestamp": at(4), "payload": {"type": "token_count", "info": {
            "total_token_usage": {"total_tokens": 120}, "last_token_usage": {"total_tokens": 35},
            "model_context_window": 100}}},
    ])
    full = CodexProvider._thread({"id": "delta-live", "status": "running", "model": "model-b",
                                  "statusObservedAt": datetime.now(timezone.utc).isoformat(), "path": str(rollout)})
    assert full["contextDelta"] == 5
    assert full["contextDeltaObservedAt"] == at(4)
    assert full["modelChangeAt"] == at(2)

    providers._index_live_paths([{"name": "Codex", "chats": [full]}])
    live = next(row for row in providers.live_state()["chats"] if row["key"] == "Codex:delta-live")
    assert live["contextDelta"] == full["contextDelta"]
    assert live["contextDeltaObservedAt"] == full["contextDeltaObservedAt"]
    assert live["modelChangeAt"] == full["modelChangeAt"]


def test_last_token_event_ignores_future_context_and_token_records_full_and_live(tmp_path):
    rollout = tmp_path / "future-token-events.jsonl"
    now = datetime.now(timezone.utc)
    at = lambda seconds: (now - timedelta(minutes=2) + timedelta(seconds=seconds)).isoformat().replace("+00:00", "Z")
    future = (now + timedelta(hours=1)).isoformat().replace("+00:00", "Z")
    sample = {"type": "event_msg", "timestamp": at(1), "payload": {"type": "token_count", "info": {
        "total_token_usage": {"total_tokens": 10}, "last_token_usage": {"total_tokens": 7},
        "model_context_window": 200}}}
    future_sample = {"type": "event_msg", "timestamp": future, "payload": {"type": "token_count", "info": {
        "total_token_usage": {"total_tokens": 99}, "last_token_usage": {"total_tokens": 88},
        "model_context_window": 500}}}
    _rollout(rollout, [
        {"type": "turn_context", "timestamp": at(0), "payload": {"model": "model-current"}},
        {"type": "event_msg", "timestamp": at(0), "payload": {"type": "task_started"}},
        sample,
        {"type": "turn_context", "timestamp": future, "payload": {"model": "model-future"}},
        future_sample,
    ])

    event = CodexProvider._read_last_token_event(rollout)
    assert event["timestamp"] == at(1)
    assert event["model"] == "model-current"
    assert event["total_token_usage"]["total_tokens"] == 10

    full = CodexProvider._thread({"id": "future-token", "status": "running",
                                  "statusObservedAt": now.isoformat(), "path": str(rollout)})
    assert full["model"] == "model-current"
    assert full["contextModel"] == "model-current"
    assert full["contextTokens"] == 7
    providers._index_live_paths([{"name": "Codex", "chats": [full]}])
    live = next(row for row in providers.live_state()["chats"] if row["key"] == "Codex:future-token")
    assert live["model"] == "model-current"
    assert live["contextModel"] == "model-current"
    assert live["contextTokens"] == 7


def test_future_only_token_and_model_records_do_not_leak_into_full_or_live_fallback(tmp_path):
    rollout = tmp_path / "future-only-token-events.jsonl"
    future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat().replace("+00:00", "Z")
    _rollout(rollout, [
        {"type": "turn_context", "timestamp": future, "payload": {"model": "model-future"}},
        {"type": "event_msg", "timestamp": future, "payload": {"type": "token_count", "info": {
            "total_token_usage": {"total_tokens": 99}, "last_token_usage": {"total_tokens": 88},
            "model_context_window": 500}}},
    ])

    assert CodexProvider._read_last_token_event(rollout) is None
    full = CodexProvider._thread({"id": "future-only", "path": str(rollout)})
    assert full["model"] == "Codex"
    assert full["contextModel"] is None
    providers._index_live_paths([{"name": "Codex", "chats": [full]}])
    live = next(row for row in providers.live_state()["chats"] if row["key"] == "Codex:future-only")
    assert live.get("model") is None
    assert live.get("contextModel") is None


def test_last_token_event_rechecks_future_model_and_token_at_deadline_without_file_change(tmp_path, monkeypatch):
    rollout = tmp_path / "future-token-deadline.jsonl"
    now = [1_791_618_000.0]
    monkeypatch.setattr(providers.time, "time", lambda: now[0])
    older_at = datetime.fromtimestamp(now[0] - 10, timezone.utc).isoformat().replace("+00:00", "Z")
    future_at = datetime.fromtimestamp(now[0] + 15, timezone.utc).isoformat().replace("+00:00", "Z")
    _rollout(rollout, [
        {"type": "turn_context", "timestamp": older_at, "payload": {"model": "model-old"}},
        {"type": "event_msg", "timestamp": older_at, "payload": {"type": "token_count", "info": {
            "total_token_usage": {"total_tokens": 10}, "last_token_usage": {"total_tokens": 8}, "model_context_window": 100}}},
        {"type": "turn_context", "timestamp": future_at, "payload": {"model": "model-future"}},
        {"type": "event_msg", "timestamp": future_at, "payload": {"type": "token_count", "info": {
            "total_token_usage": {"total_tokens": 20}, "last_token_usage": {"total_tokens": 18}, "model_context_window": 200}}},
    ])
    before_stat = rollout.stat()
    direct = CodexProvider._read_last_token_event(rollout)
    assert direct["model"] == "model-old" and direct["total_token_usage"]["total_tokens"] == 10
    first = CodexProvider._last_token_event(str(rollout))
    assert first["model"] == direct["model"] and first["context_valid"] == direct["context_valid"]
    now[0] += 16
    second = CodexProvider._last_token_event(str(rollout))
    assert second["model"] == "model-future"
    assert second["total_token_usage"]["total_tokens"] == 20
    assert rollout.stat().st_size == before_stat.st_size and rollout.stat().st_mtime_ns == before_stat.st_mtime_ns


def test_future_only_last_token_event_becomes_available_without_file_change(tmp_path, monkeypatch):
    rollout = tmp_path / "future-only-deadline.jsonl"
    now = [1_791_618_000.0]
    monkeypatch.setattr(providers.time, "time", lambda: now[0])
    future_at = datetime.fromtimestamp(now[0] + 12, timezone.utc).isoformat().replace("+00:00", "Z")
    _rollout(rollout, [{"type": "event_msg", "timestamp": future_at, "payload": {"type": "token_count", "info": {
        "total_token_usage": {"total_tokens": 42}, "last_token_usage": {"total_tokens": 4}, "model_context_window": 256}}}])
    before_stat = rollout.stat()
    assert CodexProvider._read_last_token_event(rollout) is None
    assert CodexProvider._last_token_event(str(rollout)) is None
    now[0] += 13
    event = CodexProvider._last_token_event(str(rollout))
    assert event["total_token_usage"]["total_tokens"] == 42
    assert rollout.stat().st_size == before_stat.st_size and rollout.stat().st_mtime_ns == before_stat.st_mtime_ns


def test_last_token_event_ordinary_memo_does_not_add_unneeded_reread(tmp_path, monkeypatch):
    rollout = tmp_path / "ordinary-token-event.jsonl"
    rollout.write_text('{"type":"event_msg","payload":{"type":"token_count"}}\n', encoding="utf-8")
    calls = []
    original = CodexProvider._read_last_token_projection
    monkeypatch.setattr(CodexProvider, "_read_last_token_projection", staticmethod(lambda path: calls.append(path) or original(path)))
    first = CodexProvider._last_token_event(str(rollout))
    second = CodexProvider._last_token_event(str(rollout))
    assert first == second and len(calls) == 1


def test_irrelevant_future_event_does_not_expire_last_token_memo(tmp_path, monkeypatch):
    rollout = tmp_path / "irrelevant-future-event.jsonl"
    now = [1_791_618_000.0]
    monkeypatch.setattr(providers.time, "time", lambda: now[0])
    future_at = datetime.fromtimestamp(now[0] + 12, timezone.utc).isoformat().replace("+00:00", "Z")
    _rollout(rollout, [{"type": "event_msg", "timestamp": future_at,
                        "payload": {"type": "task_started"}}])
    calls = []
    original = CodexProvider._read_last_token_projection
    monkeypatch.setattr(CodexProvider, "_read_last_token_projection",
                        staticmethod(lambda path: calls.append(path) or original(path)))

    assert CodexProvider._last_token_event(str(rollout)) is None
    now[0] += 13
    assert CodexProvider._last_token_event(str(rollout)) is None
    assert len(calls) == 1


def test_future_context_after_last_token_does_not_expire_last_token_memo(tmp_path, monkeypatch):
    rollout = tmp_path / "irrelevant-future-context.jsonl"
    now = [1_791_618_000.0]
    monkeypatch.setattr(providers.time, "time", lambda: now[0])
    future_at = datetime.fromtimestamp(now[0] + 12, timezone.utc).isoformat().replace("+00:00", "Z")
    _rollout(rollout, [
        {"type": "event_msg", "payload": {"type": "token_count", "info": {
            "total_token_usage": {"total_tokens": 42}}}},
        {"type": "turn_context", "timestamp": future_at, "payload": {"model": "model-future"}},
    ])
    calls = []
    original = CodexProvider._read_last_token_projection
    monkeypatch.setattr(CodexProvider, "_read_last_token_projection",
                        staticmethod(lambda path: calls.append(path) or original(path)))

    first = CodexProvider._last_token_event(str(rollout))
    now[0] += 13
    second = CodexProvider._last_token_event(str(rollout))
    assert first == second and len(calls) == 1


def test_context_delta_resets_after_a_new_task_started(tmp_path):
    rollout = tmp_path / "context-delta-new-run.jsonl"
    start = datetime.now(timezone.utc).replace(microsecond=0) - timedelta(seconds=30)
    at = lambda seconds: (start + timedelta(seconds=seconds)).isoformat().replace("+00:00", "Z")

    def sample(seconds, total):
        return {"type": "event_msg", "timestamp": at(seconds), "payload": {"type": "token_count", "info": {
            "total_token_usage": {"total_tokens": 100 + seconds}, "last_token_usage": {"total_tokens": total},
            "model_context_window": 200}}}

    _rollout(rollout, [
        {"type": "turn_context", "payload": {"model": "model-a"}},
        {"type": "event_msg", "timestamp": at(0), "payload": {"type": "task_started"}},
        sample(1, 40), sample(2, 50),
        {"type": "event_msg", "timestamp": at(3), "payload": {"type": "task_started"}},
        sample(4, 10),
    ])
    stats = providers._read_codex_run_stats(rollout)
    assert stats["contextDelta"] is None
    assert stats["contextDeltaObservedAt"] is None

    with rollout.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(sample(5, 15)) + "\n")
    stats = providers._read_codex_run_stats(rollout)
    assert stats["contextDelta"] == 5
    assert stats["contextDeltaObservedAt"] == at(5)


def test_recent_rollout_records_keep_old_mtime_child_running_after_cold_join(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))
    now = datetime.now(timezone.utc).replace(microsecond=0)
    task_at = (now - timedelta(seconds=10)).isoformat().replace("+00:00", "Z")
    call_at = (now - timedelta(seconds=9)).isoformat().replace("+00:00", "Z")
    child_path = tmp_path / "codex" / "sessions" / "parent" / "subagents" / "child.jsonl"
    child_path.parent.mkdir(parents=True)
    _rollout(child_path, [
        {"type": "session_meta", "timestamp": task_at, "payload": {"id": "child", "parent_thread_id": "parent"}},
        {"type": "event_msg", "timestamp": task_at, "payload": {"type": "task_started"}},
        {"type": "response_item", "timestamp": call_at, "payload": {
            "type": "function_call", "name": "read_file", "call_id": "read-1", "arguments": json.dumps({"path": "src/current.py"})}},
    ])
    old_mtime = time.time() - 600
    os.utime(child_path, (old_mtime, old_mtime))
    _register_live_desktop_threads(tmp_path / "codex" / "state_5.sqlite", [
        _live_desktop_row("parent", child_path.parent / "parent.jsonl"),
        _live_desktop_row("child", child_path, parent="parent"),
    ])

    full = CodexProvider()._with_agents([{"id": "parent", "title": "Parent", "status": "running"}])
    child = next(row for row in full if row["id"] == "child")
    assert child["status"] == "running"
    assert child["activity"] == {"kind": "reading", "label": "Reading file", "files": ["src/current.py"], "observedAt": call_at}

    providers._index_live_paths([{"name": "Codex", "chats": full}])
    live = next(row for row in providers.live_state()["chats"] if row["key"] == "Codex:child")
    assert live["status"] == "running"
    assert providers._timestamp_epoch(live["activityAt"]) == providers._timestamp_epoch(call_at)
    assert live["activity"] == child["activity"]


def test_old_record_timestamp_does_not_become_fresh_from_recent_file_mtime(tmp_path):
    rollout = tmp_path / "old-event-fresh-file.jsonl"
    old_stamp = (datetime.now(timezone.utc) - timedelta(seconds=300)).isoformat().replace("+00:00", "Z")
    _rollout(rollout, [{"type": "event_msg", "timestamp": old_stamp, "payload": {"type": "task_started"}}])
    thread = CodexProvider._thread({"id": "old-event", "path": str(rollout)})
    assert thread["status"] == "unavailable"

    providers._index_live_paths([{"name": "Codex", "chats": [thread]}])
    live = next(row for row in providers.live_state()["chats"] if row["key"] == "Codex:old-event")
    assert live["status"] == "unavailable"
    assert providers._timestamp_epoch(live["activityAt"]) == providers._timestamp_epoch(old_stamp)


def test_future_rollout_records_do_not_refresh_status_or_activity(tmp_path):
    rollout = tmp_path / "future-event.jsonl"
    future_stamp = (datetime.now(timezone.utc) + timedelta(seconds=30)).isoformat().replace("+00:00", "Z")
    _rollout(rollout, [{"type": "event_msg", "timestamp": future_stamp, "payload": {"type": "task_started"}}])
    state = providers._codex_rollout_state(str(rollout))
    assert state["status"] == "unknown"
    assert state["observedAt"] is None
    assert state["recordTimestampPresent"] is True
    thread = CodexProvider._thread({"id": "future-event", "path": str(rollout)})
    assert thread["status"] == "unavailable"


def test_future_task_start_becomes_visible_after_clock_advances_without_file_change(tmp_path, monkeypatch):
    rollout = tmp_path / "future-task-start.jsonl"
    now = [1_791_618_000.0]
    future = datetime.fromtimestamp(now[0] + 60, timezone.utc).isoformat().replace("+00:00", "Z")
    _rollout(rollout, [{"type": "event_msg", "timestamp": future, "payload": {"type": "task_started"}}])
    original_stat = rollout.stat()
    monkeypatch.setattr(providers.time, "time", lambda: now[0])
    first = providers._codex_rollout_state(str(rollout))
    assert first["status"] == "unknown"
    assert first["recordTimestampPresent"] is True
    now[0] += 61
    second = providers._codex_rollout_state(str(rollout))
    assert second["status"] == "running"
    assert second["recordTimestampPresent"] is True
    assert rollout.stat().st_size == original_stat.st_size and rollout.stat().st_mtime_ns == original_stat.st_mtime_ns


def test_json_compacted_event_invalidates_context_sample(tmp_path):
    rollout = tmp_path / "compacted.jsonl"
    rows = [
        {"type": "turn_context", "payload": {"model": "model-a"}},
        {"type": "event_msg", "timestamp": "2026-10-07T10:00:00Z", "payload": {"type": "token_count", "info": {
            "total_token_usage": {"total_tokens": 100}, "last_token_usage": {"total_tokens": 50},
            "model_context_window": 200}}},
        {"type": "compacted", "timestamp": "2026-10-07T10:01:00Z"},
    ]
    rollout.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
    event = CodexProvider._last_token_event(str(rollout))
    assert event["context_valid"] is False


def test_stale_compaction_is_unavailable_without_fresh_runtime_evidence(tmp_path):
    rollout = tmp_path / "old-compaction.jsonl"
    event = {"type": "event_msg", "timestamp": "2026-10-07T08:00:00Z",
             "payload": {"type": "context_compaction_started"}}
    rollout.write_text(json.dumps(event) + "\n", encoding="utf-8")
    old = time.time() - 600
    os.utime(rollout, (old, old))

    thread = CodexProvider._thread({"id": "old-compaction", "path": str(rollout)})
    assert thread["status"] == "unavailable"
    assert thread["statusSource"] == "unavailable"

    providers._LIVE_INDEX["Codex:old-compaction"] = {
        "id": "old-compaction", "path": str(rollout), "status": "compacting",
        "statusSource": "transcript", "stateSince": event["timestamp"],
        "compactionStartedAt": event["timestamp"], "statusObservedAt": None,
        "appStatus": None, "lastValidAt": event["timestamp"],
    }
    live = next(row for row in providers.live_state()["chats"] if row["key"] == "Codex:old-compaction")
    assert live["status"] == "unavailable"
    assert live["statusSource"] == "unavailable"


def test_fresh_app_running_preserves_recorded_compaction_in_full_and_live_state(tmp_path):
    rollout = tmp_path / "active-compaction.jsonl"
    event = {"type": "event_msg", "timestamp": _recent_test_timestamp(),
             "payload": {"type": "context_compaction_started"}}
    rollout.write_text(json.dumps(event) + "\n", encoding="utf-8")
    old = time.time() - 600
    os.utime(rollout, (old, old))
    observed = datetime.now(timezone.utc).isoformat()

    thread = CodexProvider._thread({"id": "active-compaction", "status": "running",
                                    "statusObservedAt": observed, "path": str(rollout)})
    assert thread["status"] == "compacting"
    assert thread["stateSince"] == event["timestamp"]
    providers._index_live_paths([{"name": "Codex", "chats": [thread]}])
    live = next(row for row in providers.live_state()["chats"] if row["key"] == "Codex:active-compaction")
    assert live["status"] == "compacting"
    assert live["stateSince"] == event["timestamp"]


def test_newer_app_failure_overrides_old_compaction(tmp_path):
    rollout = tmp_path / "failed-after-compaction.jsonl"
    event = {"type": "event_msg", "timestamp": "2026-10-07T08:00:00Z",
             "payload": {"type": "context_compaction_started"}}
    rollout.write_text(json.dumps(event) + "\n", encoding="utf-8")
    old = time.time() - 600
    os.utime(rollout, (old, old))
    observed = datetime.now(timezone.utc).isoformat()

    thread = CodexProvider._thread({"id": "failed-compaction", "status": "failed",
                                    "statusObservedAt": observed, "path": str(rollout)})
    assert thread["status"] == "failed"
    providers._index_live_paths([{"name": "Codex", "chats": [thread]}])
    live = next(row for row in providers.live_state()["chats"] if row["key"] == "Codex:failed-compaction")
    assert live["status"] == "failed"


def test_stale_provider_retains_active_row_without_extending_validity(tmp_path):
    rollout = tmp_path / "stale-provider.jsonl"
    rollout.write_text("{}\n", encoding="utf-8")
    observed = datetime.now(timezone.utc).isoformat()
    active = {"id": "stale-provider", "path": str(rollout), "status": "running",
              "statusSource": "app", "statusObservedAt": observed, "appStatus": "running"}
    providers._index_live_paths([{"name": "Codex", "chats": [dict(active)]}])
    prior_valid_at = providers._LIVE_INDEX["Codex:stale-provider"]["lastValidAt"]

    for _ in range(2):
        providers._index_live_paths([{"name": "Codex", "stale": True, "chats": []}])
        row = next(chat for chat in providers.live_state()["chats"] if chat["key"] == "Codex:stale-provider")
        assert row["status"] == "unavailable"
        assert row["previousStatus"] == "running"
        assert row["telemetryUnavailable"] is True
        assert row["lastValidAt"] == prior_valid_at
        assert providers._LIVE_INDEX["Codex:stale-provider"]["lastValidAt"] == prior_valid_at


def test_stale_provider_expires_retained_active_row_after_150_seconds(tmp_path):
    rollout = tmp_path / "expired-provider.jsonl"
    rollout.write_text("{}\n", encoding="utf-8")
    observed = (datetime.now(timezone.utc) - timedelta(seconds=151)).isoformat()
    stale_provider = {"name": "Codex", "stale": True, "chats": [{
        "id": "expired-provider", "path": str(rollout), "status": "running",
        "statusSource": "app", "statusObservedAt": observed, "appStatus": "running",
    }]}
    providers._index_live_paths([stale_provider])
    assert stale_provider["chats"] == []
    assert all(row["key"] != "Codex:expired-provider" for row in providers.live_state()["chats"])


def test_successful_empty_snapshot_removes_previously_active_row(tmp_path):
    rollout = tmp_path / "removed-provider.jsonl"
    rollout.write_text("{}\n", encoding="utf-8")
    providers._index_live_paths([{"name": "Codex", "chats": [{
        "id": "removed-provider", "path": str(rollout), "status": "running",
        "statusSource": "app", "statusObservedAt": datetime.now(timezone.utc).isoformat(),
    }]}])
    providers._index_live_paths([{"name": "Codex", "stale": False, "chats": []}])
    assert "Codex:removed-provider" not in providers._LIVE_INDEX
    assert all(row["key"] != "Codex:removed-provider" for row in providers.live_state()["chats"])


def test_valid_null_message_and_context_fields_do_not_restore_stale_values(tmp_path):
    rollout = tmp_path / "cleared-fields.jsonl"
    rollout.write_text("{}\n", encoding="utf-8")
    providers._index_live_paths([{"name": "Codex", "chats": [{
        "id": "cleared-fields", "path": str(rollout), "status": "running",
        "statusSource": "app", "statusObservedAt": datetime.now(timezone.utc).isoformat(),
        "latestMessageAt": "2026-10-07T09:00:00Z", "latestMessageDirection": "parent-to-agent",
        "contextTokens": 50, "contextWindow": 100, "contextPercent": 50,
        "contextObservedAt": "2026-10-07T09:00:00Z", "contextModel": "model-a",
        "lastRequest": {"input": 50, "cached": 10, "output": 5}, "model": "model-a",
    }]}])
    providers._index_live_paths([{"name": "Codex", "chats": [{
        "id": "cleared-fields", "path": str(rollout), "status": "running",
        "statusSource": "app", "statusObservedAt": datetime.now(timezone.utc).isoformat(),
        "latestMessageAt": None, "latestMessageDirection": None, "latestMessageSourceId": None,
        "latestMessageTargetId": None, "contextTokens": None, "contextWindow": None,
        "contextPercent": None, "contextObservedAt": None, "contextModel": None,
        "lastRequest": {"input": None, "cached": None, "output": None}, "model": "model-b",
    }]}])
    row = providers._LIVE_INDEX["Codex:cleared-fields"]
    assert row["latestMessageAt"] is None
    assert row["latestMessageDirection"] is None
    assert row["contextTokens"] is None
    assert row["contextWindow"] is None
    assert row["contextModel"] is None
    assert row["lastRequest"] == {"input": None, "cached": None, "output": None}


def test_question_answer_endpoint_fails_closed_until_structured_transport_exists():
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    worker = threading.Thread(target=httpd.serve_forever, daemon=True)
    worker.start()
    connection = HTTPConnection(*httpd.server_address)
    try:
        connection.request("POST", "/api/answer", body=json.dumps({
            "thread": "01a0cad8-b212-72f2-88d3-2c887f7d4445", "answer": "Yes, both worked",
        }), headers={"Content-Type": "application/json"})
        response = connection.getresponse()
        assert response.status == 404
        assert b'"not found"' in response.read()
    finally:
        connection.close()
        httpd.shutdown()
        httpd.server_close()
        worker.join(timeout=2)


def test_question_option_queue_is_same_origin_validated_and_labeled_queued(monkeypatch):
    policies = dict(provider_policy.POLICIES)
    policies["Codex"] = replace(policies["Codex"], send=True, send_origin="provider_env")
    monkeypatch.setattr(provider_policy, "POLICIES", MappingProxyType(policies))
    queued = []
    monkeypatch.setattr(server, "_is_collected_codex_thread", lambda thread: True)
    monkeypatch.setattr(server, "queue_codex_message", lambda thread, message: queued.append((thread, message)))
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    worker = threading.Thread(target=httpd.serve_forever, daemon=True)
    worker.start()
    connection = HTTPConnection(*httpd.server_address)
    origin = f"http://{httpd.server_address[0]}:{httpd.server_address[1]}"
    body = json.dumps({"thread": "01a0cad8-b212-72f2-88d3-2c887f7d4445", "message": "Yes, both worked"})
    try:
        connection.request("POST", "/api/queue", body=body, headers={
            "Content-Type": "application/json", "Origin": origin,
        })
        response = connection.getresponse()
        payload = json.loads(response.read())
        assert response.status == 200
        assert payload["status"] == "queued"
        assert "does not answer" in payload["detail"]
        assert queued == [("01a0cad8-b212-72f2-88d3-2c887f7d4445", "Yes, both worked")]

        connection.request("POST", "/api/queue", body=body, headers={
            "Content-Type": "application/json", "Origin": "https://evil.example",
        })
        response = connection.getresponse()
        assert response.status == 403
        response.read()
        assert len(queued) == 1

        invalid = json.dumps({"thread": "codex://threads/evil", "message": "hello"})
        connection.request("POST", "/api/queue", body=invalid, headers={
            "Content-Type": "application/json", "Origin": origin,
        })
        response = connection.getresponse()
        assert response.status == 400
        response.read()
        assert len(queued) == 1
    finally:
        connection.close()
        httpd.shutdown()
        httpd.server_close()
        worker.join(timeout=2)


def test_queue_codex_message_uses_noninteractive_bounded_cli(monkeypatch):
    calls = []
    policies = dict(provider_policy.POLICIES)
    policies["Codex"] = replace(policies["Codex"], send=True, send_origin="provider_env")
    monkeypatch.setattr(provider_policy, "POLICIES", MappingProxyType(policies))
    monkeypatch.setattr(server.shutil, "which", lambda command: r"C:\\Codex\\codex.exe")
    monkeypatch.setattr(server.subprocess, "run", lambda *args, **kwargs: calls.append((args, kwargs)) or type("Result", (), {"returncode": 0})())
    thread = "01a0cad8-b212-72f2-88d3-2c887f7d4445"
    server.queue_codex_message(thread, "hello")
    args, kwargs = calls[0]
    assert args[0] == [r"C:\\Codex\\codex.exe", "queue", "--thread", thread, "--message", "hello"]
    assert kwargs["timeout"] == server.QUEUE_TIMEOUT_SECONDS
    assert kwargs["stdin"] is server.subprocess.DEVNULL


def test_open_endpoint_accepts_only_thread_ids():
    assert server.THREAD_ID.fullmatch("01a0cad8-b212-72f2-88d3-2c887f7d4445")
    for bad in ("", "codex://x", "01a0cad8-b212-72f2-88d3-2c887f7d4445&x", "../etc", "calc.exe"):
        assert not server.THREAD_ID.fullmatch(bad)


def test_open_endpoint_checks_host_and_supplied_browser_origin_without_launching_app(monkeypatch):
    launched = []
    monkeypatch.setattr(server, "_is_collected_codex_thread", lambda thread: True)
    monkeypatch.setattr(server.os, "startfile", lambda target: launched.append(target), raising=False)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    worker = threading.Thread(target=httpd.serve_forever, daemon=True)
    worker.start()
    connection = HTTPConnection(*httpd.server_address)
    host = f"127.0.0.1:{httpd.server_address[1]}"
    origin = f"http://{host}"
    route = "/api/open?thread=01a0cad8-b212-72f2-88d3-2c887f7d4445"
    try:
        connection.request("GET", route, headers={"Host": "evil.example"})
        response = connection.getresponse()
        assert response.status == 403
        response.read()
        assert launched == []

        for headers in (
            {"Origin": "https://evil.example"},
            {"Referer": "https://evil.example/page"},
            {"Sec-Fetch-Site": "cross-site"},
        ):
            connection.request("GET", route, headers=headers)
            response = connection.getresponse()
            assert response.status == 403
            response.read()
        assert launched == []

        connection.request("GET", route, headers={"Origin": origin, "Sec-Fetch-Site": "same-origin"})
        response = connection.getresponse()
        assert response.status == 200
        response.read()
        assert launched == ["codex://threads/01a0cad8-b212-72f2-88d3-2c887f7d4445"]

        connection.request("GET", route, headers={"Referer": origin + "/index.html", "Sec-Fetch-Site": "same-site"})
        response = connection.getresponse()
        assert response.status == 200
        response.read()
        assert launched[-1] == "codex://threads/01a0cad8-b212-72f2-88d3-2c887f7d4445"
    finally:
        connection.close()
        httpd.shutdown()
        httpd.server_close()
        worker.join(timeout=2)


def test_codex_desktop_index_recovers_missing_parent_and_active_worker(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    now = datetime.now(timezone.utc).isoformat()
    rows = []
    for identity, parent, terminal in [("desktop", None, False), ("worker", "desktop", False),
                                       ("finished", "desktop", True), ("archived", None, False)]:
        source = {"subagent": {"thread_spawn": {"parent_thread_id": parent}}} if parent else "vscode"
        path = sessions / (identity + ".jsonl")
        path.write_text("\n".join(json.dumps(record) for record in [
            {"type": "session_meta", "payload": {"id": identity, "parent_thread_id": parent}},
            *([{"type": "turn_context", "payload": {"model": "gpt-6-luna"}}] if identity == "worker" else []),
            {"type": "event_msg", "timestamp": now, "payload": {"type": "task_started"}},
            *([{"type": "event_msg", "timestamp": now, "payload": {"type": "task_complete"}}] if terminal else []),
        ]) + "\n", encoding="utf8")
        rows.append((identity, str(path), json.dumps(source), str(tmp_path), identity,
                     identity, "gpt-6-luna" if parent else "gpt-6.1-sol", None,
                     int(identity == "archived"), 1))
    with sqlite3.connect(tmp_path / "state_5.sqlite") as connection:
        connection.execute("CREATE TABLE threads (id, rollout_path, source, cwd, name, title, model, agent_path, archived, updated_at)")
        connection.executemany("INSERT INTO threads VALUES (?,?,?,?,?,?,?,?,?,?)", rows)
    chats = CodexProvider()._with_agents([])
    assert {chat["id"] for chat in chats} == {"desktop", "worker"}
    worker = next(chat for chat in chats if chat["id"] == "worker")
    assert worker["parentId"] == "desktop" and worker["status"] == "running"
    assert worker["model"] == "gpt-6-luna"
    assert worker["contextTokens"] is None and worker["contextWindow"] is None
    assert next(chat for chat in chats if chat["id"] == "desktop")["statusSource"] == "transcript"


def test_codex_desktop_index_never_invents_activity_or_duplicates_api_parent(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    path = tmp_path / "empty.jsonl"
    path.write_text(json.dumps({"type": "session_meta", "payload": {"id": "unknown"}}) + "\n")
    with sqlite3.connect(tmp_path / "state_5.sqlite") as connection:
        connection.execute("CREATE TABLE threads (id, rollout_path, source, cwd, name, title, model, agent_path, archived, updated_at)")
        connection.execute("INSERT INTO threads VALUES (?,?,?,?,?,?,?,?,?,?)",
                           ("unknown", str(path), "vscode", str(tmp_path), "Unknown", "Unknown", None, None, 0, time.time()))
    provider = CodexProvider()
    chats = provider._with_agents([])
    assert len(chats) == 1 and chats[0]["status"] == "unavailable"
    original = {"id": "unknown", "status": "idle", "title": "API title"}
    assert provider._with_agents([original]) == [original]
    assert provider._persisted_threads(limit=0) == []
    (tmp_path / "state_5.sqlite").write_bytes(b"invalid database")
    assert provider._persisted_threads() == []


def _register_live_desktop_threads(database, rows):
    with sqlite3.connect(database) as connection:
        connection.execute(
            "CREATE TABLE IF NOT EXISTS threads (id, rollout_path, source, cwd, name, title, model, agent_path, archived, updated_at)"
        )
        connection.executemany("INSERT OR REPLACE INTO threads VALUES (?,?,?,?,?,?,?,?,?,?)", rows)


def _live_desktop_row(identity, path, *, parent=None, title=None, updated_at=1):
    source = {"subagent": {"thread_spawn": {"parent_thread_id": parent}}} if parent else "vscode"
    return (identity, str(path), json.dumps(source), str(path.parent), title or identity,
            title or identity, "db-model-must-not-be-used", None, 0, updated_at)


def test_live_state_discovers_new_desktop_root_from_db_without_snapshot(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    sessions = tmp_path / "sessions" / "2026" / "10" / "09"
    sessions.mkdir(parents=True)
    rollout = sessions / "root.jsonl"
    at = _recent_test_timestamp()
    _rollout(rollout, [
        {"type": "session_meta", "payload": {"id": "root", "cwd": "A:/workspace"}},
        {"type": "turn_context", "payload": {"model": "gpt-6.1-sol", "cwd": "A:/workspace"}},
        {"type": "event_msg", "timestamp": at, "payload": {"type": "task_started"}},
    ])
    before = providers._structure_stamp()
    _register_live_desktop_threads(tmp_path / "state_5.sqlite", [
        _live_desktop_row("root", rollout, title="Daily work", updated_at=time.time()),
    ])
    assert providers._structure_stamp() == before
    monkeypatch.setattr(CodexProvider, "snapshot", lambda self: (_ for _ in ()).throw(AssertionError("snapshot called")))
    monkeypatch.setattr(providers, "build_snapshot", lambda: (_ for _ in ()).throw(AssertionError("build_snapshot called")))
    monkeypatch.setattr(providers, "CodexAppServer", lambda: (_ for _ in ()).throw(AssertionError("app server constructed")))

    live = providers.live_state()
    row = next(chat for chat in live["chats"] if chat["key"] == "Codex:root")
    assert row["id"] == "root" and row["title"] == "Daily work"
    assert row["status"] == "running" and row["model"] == "gpt-6.1-sol"
    assert row["workspace"] == "A:/workspace"
    assert "path" not in row and "indexedAt" not in row
    assert str(tmp_path) not in json.dumps(live)


def test_live_state_adds_child_lineage_and_idle_parent_anchor(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    at = _recent_test_timestamp()
    parent = _rollout(sessions / "parent.jsonl", [
        {"type": "session_meta", "payload": {"id": "parent"}},
        {"type": "event_msg", "timestamp": at, "payload": {"type": "task_started"}},
        {"type": "event_msg", "timestamp": at, "payload": {"type": "task_complete"}},
    ])
    child = _rollout(sessions / "child.jsonl", [
        {"type": "session_meta", "payload": {"id": "child", "parent_thread_id": "parent"}},
        {"type": "event_msg", "timestamp": at, "payload": {"type": "task_started"}},
    ])
    _register_live_desktop_threads(tmp_path / "state_5.sqlite", [
        _live_desktop_row("parent", parent, title="Parent"),
        _live_desktop_row("child", child, parent="parent", title="Worker"),
    ])

    rows = {row["id"]: row for row in providers.live_state()["chats"]}
    assert rows["parent"]["status"] == "idle"
    assert rows["child"]["status"] == "running"
    assert rows["child"]["parentId"] == "parent"


def test_live_state_preserves_database_child_name_without_full_snapshot(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    (tmp_path / "sessions").mkdir()
    rollout = _rollout(tmp_path / "sessions" / "worker.jsonl", [
        {"type": "session_meta", "payload": {"id": "worker"}},
        {"type": "event_msg", "timestamp": _recent_test_timestamp(), "payload": {"type": "task_started"}},
    ])
    row = list(_live_desktop_row("worker", rollout, parent="root", title="Review worker"))
    row[3] = "A:/workspace"
    row[4] = "Assigned reviewer"
    _register_live_desktop_threads(tmp_path / "state_5.sqlite", [tuple(row)])
    monkeypatch.setattr(CodexProvider, "snapshot", lambda self: (_ for _ in ()).throw(AssertionError("snapshot called")))
    monkeypatch.setattr(providers, "build_snapshot", lambda: (_ for _ in ()).throw(AssertionError("build_snapshot called")))
    monkeypatch.setattr(providers, "CodexAppServer", lambda: (_ for _ in ()).throw(AssertionError("app server constructed")))

    live = providers.live_state()
    worker = next(chat for chat in live["chats"] if chat["id"] == "worker")
    assert worker["status"] == "running"
    assert worker["parentId"] == "root"
    assert worker["agentName"] == "Assigned reviewer"
    assert not any(key.startswith("identity") for key in worker)
    assert "path" not in worker and "indexedAt" not in worker


def test_live_state_refreshes_resumed_existing_rollout_after_terminal(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    at = lambda offset: _recent_test_timestamp(offset)
    rollout = _rollout(sessions / "reused.jsonl", [
        {"type": "session_meta", "payload": {"id": "reused"}},
        {"type": "turn_context", "payload": {"model": "gpt-old"}},
        {"type": "event_msg", "timestamp": at(0), "payload": {"type": "task_started"}},
        {"type": "event_msg", "timestamp": at(1), "payload": {"type": "task_complete"}},
    ])
    _register_live_desktop_threads(tmp_path / "state_5.sqlite", [
        _live_desktop_row("reused", rollout, title="Reused chat"),
    ])
    first = next(row for row in providers.live_state()["chats"] if row["id"] == "reused")
    assert first["status"] == "idle" and first["model"] == "gpt-old"

    with rollout.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"type": "turn_context", "payload": {"model": "gpt-new"}}) + "\n")
        handle.write(json.dumps({"type": "event_msg", "timestamp": at(2), "payload": {"type": "task_started"}}) + "\n")
    second = next(row for row in providers.live_state()["chats"] if row["id"] == "reused")
    assert second["status"] == "running" and second["model"] == "gpt-new"


def test_live_state_changed_rollout_path_drops_old_run_observations(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    at = _recent_test_timestamp()
    old = _rollout(sessions / "old.jsonl", [
        {"type": "session_meta", "payload": {"id": "same"}},
        {"type": "turn_context", "payload": {"model": "gpt-old"}},
        {"type": "event_msg", "timestamp": at, "payload": {"type": "task_started"}},
    ])
    database = tmp_path / "state_5.sqlite"
    _register_live_desktop_threads(database, [_live_desktop_row("same", old)])
    providers.live_state()
    providers._LIVE_INDEX["Codex:same"].update(
        status="running", statusSource="app", statusObservedAt=at, appStatus="running",
        contextTokens=90000, contextWindow=100000, contextPercent=90,
        contextModel="gpt-old", lastValidAt=at,
    )
    new = _rollout(sessions / "new.jsonl", [
        {"type": "session_meta", "payload": {"id": "same"}},
        {"type": "turn_context", "payload": {"model": "gpt-new"}},
        {"type": "event_msg", "timestamp": at, "payload": {"type": "task_started"}},
    ])
    with sqlite3.connect(database) as connection:
        connection.execute("UPDATE threads SET rollout_path=? WHERE id='same'", (str(new),))

    row = next(row for row in providers.live_state()["chats"] if row["id"] == "same")
    assert row["model"] == "gpt-new"
    assert row["contextTokens"] is None and row["contextPercent"] is None


def test_live_state_sqlite_identity_timestamp_cannot_activate_chat(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    rollout = tmp_path / "sessions" / "unknown.jsonl"
    rollout.parent.mkdir()
    _rollout(rollout, [{"type": "session_meta", "payload": {"id": "unknown"}}])
    _register_live_desktop_threads(tmp_path / "state_5.sqlite", [
        _live_desktop_row("unknown", rollout, updated_at=time.time()),
    ])

    row = next(row for row in providers.live_state()["chats"] if row["id"] == "unknown")
    assert row["status"] not in {"running", "compacting", "needs input", "quiet"}


def test_live_state_missing_or_locked_sqlite_keeps_existing_index(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    rollout = _rollout(tmp_path / "existing.jsonl", [
        {"type": "session_meta", "payload": {"id": "existing"}},
        {"type": "event_msg", "timestamp": _recent_test_timestamp(), "payload": {"type": "task_started"}},
    ])
    providers._index_live_paths([{"name": "Codex", "chats": [{"id": "existing", "path": str(rollout)}]}])
    missing = providers.live_state()
    assert any(row["key"] == "Codex:existing" for row in missing["chats"])

    database = tmp_path / "state_5.sqlite"
    _register_live_desktop_threads(database, [])
    connection = sqlite3.connect(database)
    try:
        connection.execute("BEGIN EXCLUSIVE")
        locked = providers.live_state()
        assert any(row["key"] == "Codex:existing" for row in locked["chats"])
    finally:
        connection.rollback()
        connection.close()
