from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from token_tracker import claude_provider, insights, providers


@pytest.fixture
def new_york(monkeypatch):
    zone = ZoneInfo("America/New_York")
    monkeypatch.setattr(providers, "_local_timezone", lambda: zone)
    monkeypatch.setattr(providers, "_local_hour_at", lambda epoch: datetime.fromtimestamp(epoch, zone))
    return zone


@pytest.mark.parametrize("now, expected, absent", [
    (datetime(2026, 11, 1, 8, 30, tzinfo=timezone.utc),
     ["2026-11-01T01:00:00-04:00", "2026-11-01T01:00:00-05:00"], None),
    (datetime(2026, 3, 8, 9, 30, tzinfo=timezone.utc),
     ["2026-03-08T01:00:00-05:00", "2026-03-08T03:00:00-04:00"], "2026-03-08T02:"),
])
def test_transition_hours_are_consecutive_instants(new_york, now, expected, absent):
    window = insights._range_window("24h", now.astimezone(new_york))
    for keys in (window["previousKeys"], window["keys"]):
        instants = [datetime.fromisoformat(key).astimezone(timezone.utc) for key in keys]
        assert len(instants) == 24
        assert all(b - a == timedelta(hours=1) for a, b in zip(instants, instants[1:]))
    assert all(key in window["keys"] for key in expected)
    if absent:
        assert not any(key.startswith(absent) for key in window["keys"])
    assert datetime.fromisoformat(window["keys"][0]).timestamp() == window["start"]
    assert datetime.fromisoformat(window["previousKeys"][-1]).timestamp() == window["start"] - 3600


def test_repeated_hour_records_match_series(new_york, tmp_path):
    times = [datetime(2026, 11, 1, hour, 30, tzinfo=timezone.utc) for hour in (5, 6)]
    keys = [providers._insights_hour_from_timestamp(t.isoformat()) for t in times]
    assert keys == [claude_provider._local_hour_key(int(t.timestamp()), new_york, {}) for t in times]
    usage = lambda n: {"input_tokens": n, "total_tokens": n}
    snapshot = {"providers": [{"name": "Codex", "activity": [], "_insightsData": {
        "dailyModels": {}, "hourlyModels": {key: {"gpt-5.6-sol": usage(n)}
                                          for key, n in zip(keys, (30, 70))}}}]}
    store = insights.LimitPeakStore(tmp_path / "peaks.jsonl", owner_check=lambda: False)
    result = insights.build_insights(snapshot, "24h", now=datetime(2026, 11, 1, 8, 30, tzinfo=timezone.utc), peak_store=store)
    series = {row["date"]: row["tokens"] for row in result["providers"][0]["series"]}
    assert [series[key] for key in keys] == [30, 70]


def test_normal_day_matches_local_hour_sequence(new_york):
    now = datetime(2026, 2, 2, 15, 30, tzinfo=timezone.utc).astimezone(new_york)
    expected = [(now.replace(minute=0, second=0, microsecond=0) - timedelta(hours=i)).isoformat()
                for i in reversed(range(48))]
    window = insights._range_window("24h", now)
    assert window["previousKeys"] + window["keys"] == expected


def test_half_hour_timezone_keeps_local_hour_boundaries(monkeypatch):
    zone = ZoneInfo("Asia/Kolkata")
    monkeypatch.setattr(providers, "_local_timezone", lambda: zone)
    monkeypatch.setattr(providers, "_local_hour_at", lambda epoch: datetime.fromtimestamp(epoch, zone))
    now = datetime(2026, 2, 2, 15, 50, tzinfo=timezone.utc)
    window = insights._range_window("24h", now)
    expected_end = now.astimezone(zone).replace(minute=0, second=0, microsecond=0)
    assert window["keys"][-1] == expected_end.isoformat()
    assert all(datetime.fromisoformat(key).minute == 0 for key in window["previousKeys"] + window["keys"])
    assert providers._insights_hour_from_timestamp(now.isoformat()) == expected_end.isoformat()
    assert claude_provider._local_hour_key(int(now.timestamp()), zone, {}) == expected_end.isoformat()


def test_system_hourly_seam_uses_offset_at_each_instant():
    now = datetime.now(timezone.utc)
    window = insights._range_window("24h", now)
    instants = [datetime.fromisoformat(key) for key in window["keys"]]
    assert all(b.timestamp() - a.timestamp() == 3600 for a, b in zip(instants, instants[1:]))
    assert all(hour.utcoffset() == datetime.fromtimestamp(hour.timestamp()).astimezone().utcoffset()
               for hour in instants)


def test_lord_howe_transition_record_matches_hourly_series(monkeypatch, tmp_path):
    zone = ZoneInfo("Australia/Lord_Howe")
    def local_at(epoch):
        local = datetime.fromtimestamp(epoch, zone)
        return local.astimezone(timezone(local.utcoffset()))
    monkeypatch.setattr(providers, "_local_timezone", lambda: zone)
    monkeypatch.setattr(providers, "_local_hour_at", local_at)
    recorded = datetime(2026, 10, 3, 15, 15, tzinfo=timezone.utc)
    now = datetime(2026, 10, 3, 18, 15, tzinfo=timezone.utc)
    key = providers._insights_hour_from_timestamp(recorded.isoformat())
    assert key == claude_provider._local_hour_key(int(recorded.timestamp()), zone, {})
    snapshot = {"providers": [{"name": "Codex", "activity": [], "_insightsData": {
        "dailyModels": {}, "hourlyModels": {key: {"gpt-5.6-sol": {
            "input_tokens": 70, "total_tokens": 70}}}}}]}
    store = insights.LimitPeakStore(tmp_path / "peaks.jsonl", owner_check=lambda: False)
    result = insights.build_insights(snapshot, "24h", now=now, peak_store=store)
    series = result["providers"][0]["series"]
    assert all(datetime.fromisoformat(row["date"]).minute == 0 for row in series)
    assert sum(row["tokens"] for row in series if row["date"] == key) == 70
