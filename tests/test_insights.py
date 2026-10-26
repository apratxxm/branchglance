from __future__ import annotations

import json
import copy
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

from token_tracker import insights, provider_policy, providers, server


def _fixture(now=None):
    now = now or datetime.now(providers._local_timezone()).replace(minute=35, second=0, microsecond=0)
    today = now.date().isoformat()
    previous_day = (now.date() - timedelta(days=8)).isoformat()
    hour = insights._hour_key(now.timestamp(), now.tzinfo)
    prior_hour = insights._hour_key((now - timedelta(hours=1)).timestamp(), now.tzinfo)
    old_stamp = (now - timedelta(days=8)).isoformat()
    root_stamp = (now - timedelta(hours=2)).isoformat()
    child_stamp = (now - timedelta(hours=3)).isoformat()
    return {
        "generatedAt": now.astimezone(timezone.utc).isoformat(),
        "providers": [
            {
                "name": "Claude",
                "chats": [{"id": "PRIVATE_ROOT_ID", "title": "secret title",
                           "path": "secret path", "parentId": None}],
                "activity": [{"date": today, "value": 180}, {"date": previous_day, "value": 50}],
                "fiveHour": {"percent": 42}, "weekly": {"percent": 68},
                "_insightsData": {
                    "trackingWindowDays": 31,
                    "dailyModels": {
                        today: {"claude-opus-5": {
                            "input_tokens": 100, "cached_tokens": 50, "cache_5m_tokens": 10,
                            "cache_1h_tokens": 0, "output_tokens": 20, "total_tokens": 180}},
                        previous_day: {"claude-opus-5": {
                            "input_tokens": 30, "cached_tokens": 10, "cache_5m_tokens": 0,
                            "cache_1h_tokens": 0, "output_tokens": 10, "total_tokens": 50}},
                    },
                    "hourlyModels": {
                        hour: {"claude-opus-5": {
                            "input_tokens": 60, "cached_tokens": 20, "cache_5m_tokens": 5,
                            "cache_1h_tokens": 0, "output_tokens": 10, "total_tokens": 95}},
                        prior_hour: {"claude-opus-5": {
                            "input_tokens": 20, "cached_tokens": 10, "cache_5m_tokens": 0,
                            "cache_1h_tokens": 0, "output_tokens": 5, "total_tokens": 35}},
                    },
                    "sessions": [
                        {"id": "PRIVATE_ROOT_ID", "lastActivityAt": root_stamp},
                        {"id": "PRIVATE_CHILD_ID", "subagent": True, "lastActivityAt": child_stamp},
                        {"id": "OLD_ROOT", "lastActivityAt": old_stamp},
                    ],
                    "activeHours": [hour, prior_hour, insights._hour_key(
                        (now - timedelta(days=8)).timestamp(), now.tzinfo)],
                },
            },
            {
                "name": "Codex",
                "chats": [{"id": "PRIVATE_CODEX_ROOT", "title": "another secret title",
                           "path": "another secret path", "parentId": None}],
                "activity": [{"date": today, "value": 130}],
                "fiveHour": {"percent": 30}, "weekly": {"percent": 55},
                "_insightsData": {
                    "trackingWindowDays": 31,
                    "dailyModels": {today: {"gpt-5.6-sol": {
                        "input_tokens": 100, "cached_tokens": 20, "output_tokens": 10,
                        "total_tokens": 130}}},
                    "hourlyModels": {hour: {"gpt-5.6-sol": {
                        "input_tokens": 100, "cached_tokens": 20, "output_tokens": 10,
                        "total_tokens": 130}}},
                    "sessions": [
                        {"id": "PRIVATE_CODEX_ROOT", "lastActivityAt": root_stamp},
                        {"id": "PRIVATE_CODEX_CHILD", "subagent": True, "lastActivityAt": child_stamp},
                    ],
                    "activeHours": [hour, prior_hour],
                },
            },
        ],
    }, now


def test_insights_period_totals_cost_models_sessions_cache_and_limits(tmp_path):
    snapshot, now = _fixture()
    store = insights.LimitPeakStore(tmp_path / "peaks.jsonl", owner_check=lambda: True)

    result = insights.build_insights(snapshot, "7d", now=now, peak_store=store)

    claude = result["providers"][0]
    assert claude["totals"]["tokens"] == 180
    assert claude["totals"]["sessions"] == 1
    assert claude["totals"]["subagentsActive"] == 1
    assert "subagents" not in claude["totals"]
    assert claude["totals"]["activeHours"] == 2.0
    assert claude["totals"]["cacheHitRate"] == 31.2
    assert claude["totals"]["apiEquivalentCost"] == 0.0011
    assert claude["previous"] == {"tokens": 50, "apiEquivalentCost": 0.0004}
    assert claude["models"][0]["name"] == "claude-opus-5"
    assert claude["limitPeaks"] == {"fiveHour": 42.0, "weekly": 68.0}
    codex = result["providers"][1]
    assert codex["totals"]["tokens"] == 130
    assert codex["totals"]["cacheHitRate"] == 20.0
    assert codex["totals"]["sessions"] == 1 and codex["totals"]["subagentsActive"] == 1
    assert codex["totals"]["apiEquivalentCost"] == 0.0005


def test_insights_24h_uses_hour_series_and_all_is_bounded_to_31_days(tmp_path):
    snapshot, now = _fixture()
    store = insights.LimitPeakStore(tmp_path / "peaks.jsonl", owner_check=lambda: False)

    short = insights.build_insights(snapshot, "24h", now=now, peak_store=store)
    assert len(short["providers"][0]["series"]) == 24
    assert short["providers"][0]["totals"]["tokens"] == 130
    assert short["providers"][0]["totals"]["activeHours"] == 2.0
    all_time = insights.build_insights(snapshot, "all", now=now, peak_store=store)
    assert len(all_time["providers"][0]["series"]) == 31
    assert all_time["providers"][0]["previous"] is None
    thirty = insights.build_insights(snapshot, "30d", now=now, peak_store=store)
    assert thirty["providers"][0]["previous"] is None


def test_unknown_models_make_cost_unreliable_and_identifiers_never_escape(tmp_path):
    snapshot, now = _fixture()
    provider = snapshot["providers"][0]
    provider["_insightsData"]["dailyModels"][now.date().isoformat()] = {
        "unlisted-model-secret": {
            "input_tokens": 1, "cached_tokens": 0, "cache_5m_tokens": 0,
            "cache_1h_tokens": 0, "output_tokens": 0, "total_tokens": 1,
        },
    }
    result = insights.build_insights(
        snapshot, "30d", now=now,
        peak_store=insights.LimitPeakStore(tmp_path / "peaks.jsonl", owner_check=lambda: False))
    payload = json.dumps(result)
    assert result["providers"][0]["totals"]["apiEquivalentCost"] is None
    for private in ("PRIVATE_ROOT_ID", "PRIVATE_CHILD_ID", "PRIVATE_CODEX_ROOT",
                    "secret title", "secret path", "another secret title", "another secret path"):
        assert private not in payload


def test_unpriced_codex_model_makes_cost_null(tmp_path):
    snapshot, now = _fixture()
    codex = snapshot["providers"][1]
    codex["_insightsData"]["dailyModels"][now.date().isoformat()] = {"gpt-6.1-sol": {
        "input_tokens": 10, "cached_tokens": 0, "output_tokens": 0, "total_tokens": 10}}
    result = insights.build_insights(
        snapshot, "7d", now=now,
        peak_store=insights.LimitPeakStore(tmp_path / "peaks.jsonl", owner_check=lambda: False))
    assert result["providers"][1]["totals"]["apiEquivalentCost"] is None


def test_snapshot_refresh_records_limit_peak_without_insights_request(monkeypatch, tmp_path):
    snapshot, now = _fixture()
    store = insights.LimitPeakStore(tmp_path / "peaks.jsonl", owner_check=lambda: True)
    monkeypatch.setattr(insights, "_default_store", lambda: store)
    monkeypatch.setattr(server, "_insights_allowed_names", lambda: {"Claude", "Codex"})
    monkeypatch.setattr(server, "build_snapshot", lambda: copy.deepcopy(snapshot))

    server.SnapshotCache()._build()

    assert store.peaks(["Claude", "Codex"], {now.date().isoformat()}) == {
        "Claude": {"fiveHour": 42.0, "weekly": 68.0},
        "Codex": {"fiveHour": 30.0, "weekly": 55.0},
    }


def test_peak_store_is_thread_safe_bounded_and_owner_gated(tmp_path):
    path = tmp_path / "limit-peaks.jsonl"
    store = insights.LimitPeakStore(path, owner_check=lambda: True, max_bytes=2048, max_days=5)
    now = datetime.now(providers._local_timezone())

    def observe(percent):
        store.observe([{"name": "Claude", "fiveHour": {"percent": percent},
                        "weekly": {"percent": percent / 2}}], now=now)

    with ThreadPoolExecutor(max_workers=12) as pool:
        list(pool.map(observe, range(1, 101)))
    peaks = store.peaks(["Claude"], {now.date().isoformat()})
    assert peaks["Claude"] == {"fiveHour": 100.0, "weekly": 50.0}
    assert path.stat().st_size <= store.max_bytes

    denied = insights.LimitPeakStore(tmp_path / "denied.jsonl", owner_check=lambda: False)
    denied.observe([{"name": "Claude", "fiveHour": {"percent": 99}}], now=now)
    assert not denied.path.exists()


def test_insights_cache_is_per_range_and_expires_after_sixty_seconds(monkeypatch, tmp_path):
    snapshot, _now = _fixture()
    cache = insights.InsightsCache()
    clock = [10.0]
    monkeypatch.setattr(insights.time, "monotonic", lambda: clock[0])
    loads = []
    loader = lambda: loads.append(1) or snapshot
    store = insights.LimitPeakStore(tmp_path / "peaks.jsonl", owner_check=lambda: False)

    first = cache.get("7d", "policy-a", allowed_names={"Claude"}, snapshot_loader=loader, peak_store=store)
    again = cache.get("7d", "policy-a", allowed_names={"Claude"}, snapshot_loader=loader, peak_store=store)
    other_range = cache.get("24h", "policy-a", allowed_names={"Claude"}, snapshot_loader=loader, peak_store=store)
    assert first == again and len(loads) == 2
    assert other_range["range"] == "24h"
    clock[0] += 61
    cache.get("7d", "policy-a", allowed_names={"Claude"}, snapshot_loader=loader, peak_store=store)
    assert len(loads) == 3


def test_insights_route_with_both_collections_disabled_does_not_read_snapshot(monkeypatch):
    disabled = provider_policy.load_policies({
        "TOKEN_TRACKER_CLAUDE_COLLECT": "0", "TOKEN_TRACKER_CLAUDE_SHOW": "0",
        "TOKEN_TRACKER_CODEX_COLLECT": "0", "TOKEN_TRACKER_CODEX_SHOW": "0",
    })
    monkeypatch.setattr(provider_policy, "POLICIES", disabled)
    monkeypatch.setattr(server.CACHE, "get", lambda **_kwargs: (_ for _ in ()).throw(
        AssertionError("disabled provider data must not be read")))
    sent = {}
    handler = object.__new__(server.Handler)
    handler.path = "/api/insights?range=7d"
    handler._valid_local_host = lambda: True
    handler._send_json = lambda value, status=200: sent.update(value=value, status=status)

    server.Handler.do_GET(handler)

    assert sent["status"] == 200
    assert sent["value"]["range"] == "7d" and sent["value"]["providers"] == []
