from datetime import datetime, timedelta, timezone

from token_tracker import claude_provider, insights, providers


LOCAL_TZ = timezone(timedelta(hours=-5))


def _store(tmp_path):
    return insights.LimitPeakStore(tmp_path / "peaks.jsonl", owner_check=lambda: False)


def _usage(tokens):
    return {"input_tokens": tokens, "cached_tokens": 0, "output_tokens": 0,
            "cache_5m_tokens": 0, "cache_1h_tokens": 0, "total_tokens": tokens}


def _snapshot(hourly):
    return {"providers": [{"name": "Codex", "activity": [],
                           "_insightsData": {"dailyModels": {}, "hourlyModels": hourly}}]}


def _build(snapshot, tmp_path, monkeypatch):
    monkeypatch.setattr(providers, "_local_timezone", lambda: LOCAL_TZ)
    monkeypatch.setattr(providers, "_local_hour_at", lambda epoch: datetime.fromtimestamp(epoch, LOCAL_TZ))
    now = datetime(2026, 10, 27, 10, 35, tzinfo=LOCAL_TZ)
    return insights.build_insights(snapshot, "24h", now=now, peak_store=_store(tmp_path))["providers"][0]


def test_hour_key_truncates_to_local_clock_hour():
    epoch = datetime(2026, 10, 27, 10, 59, 59, tzinfo=LOCAL_TZ).timestamp()
    assert insights._hour_key(epoch, LOCAL_TZ) == "2026-10-27T10:00:00-05:00"


def test_provider_timestamps_bucket_on_local_hour_boundaries(tmp_path, monkeypatch):
    monkeypatch.setattr(providers, "_local_timezone", lambda: LOCAL_TZ)
    monkeypatch.setattr(providers, "_local_hour_at", lambda epoch: datetime.fromtimestamp(epoch, LOCAL_TZ))
    before = datetime(2026, 10, 27, 14, 59, 59, tzinfo=timezone.utc)
    boundary = datetime(2026, 10, 27, 15, 0, tzinfo=timezone.utc)
    codex_key = providers._insights_hour_from_timestamp(before.isoformat())
    claude_key = claude_provider._local_hour_key(int(boundary.timestamp()), LOCAL_TZ, {})
    provider = _build(_snapshot({codex_key: {"gpt-5.6-sol": _usage(30)},
                                claude_key: {"gpt-5.6-sol": _usage(70)}}),
                      tmp_path, monkeypatch)
    assert codex_key == "2026-10-27T09:00:00-05:00"
    assert claude_key == "2026-10-27T10:00:00-05:00"
    assert [row["tokens"] for row in provider["series"][-2:]] == [30, 70]


def test_24h_series_has_24_local_hour_points_ending_at_current_hour(tmp_path, monkeypatch):
    provider = _build(_snapshot({}), tmp_path, monkeypatch)
    series = provider["series"]
    assert len(series) == 24
    assert series[0]["date"] == "2026-10-26T11:00:00-05:00"
    assert series[-1]["date"] == "2026-10-27T10:00:00-05:00"
    assert all(row["tokens"] == 0 and row["apiEquivalentCost"] == 0.0 for row in series)
    assert all("costPartial" not in row for row in series)


def test_hour_buckets_land_on_boundaries_and_outside_hours_are_excluded(tmp_path, monkeypatch):
    hourly = {
        "2026-10-26T11:00:00-05:00": {"gpt-5.6-sol": _usage(50)},   # oldest bucket in window
        "2026-10-27T10:00:00-05:00": {"gpt-5.6-sol": _usage(100)},  # current hour
        "2026-10-26T10:00:00-05:00": {"gpt-5.6-sol": _usage(999)},  # one hour before window
        "2026-10-27T11:00:00-05:00": {"gpt-5.6-sol": _usage(999)},  # after current hour
    }
    provider = _build(_snapshot(hourly), tmp_path, monkeypatch)
    series = provider["series"]
    assert series[0]["tokens"] == 50
    assert series[-1]["tokens"] == 100
    assert sum(row["tokens"] for row in series) == 150
    assert provider["totals"]["tokens"] == 150
    assert all(row["tokens"] == 0 for row in series[1:-1])


def test_hour_series_uses_local_timezone_not_utc(tmp_path, monkeypatch):
    # 15:00 UTC is 10:00 at UTC-5: it must land in the 10:00 local bucket.
    hourly = {"2026-10-27T10:00:00-05:00": {"gpt-5.6-sol": _usage(70)}}
    provider = _build(_snapshot(hourly), tmp_path, monkeypatch)
    assert provider["series"][-1]["date"] == "2026-10-27T10:00:00-05:00"
    assert provider["series"][-1]["tokens"] == 70


def test_unpriced_hour_is_flagged_and_priced_hour_is_not(tmp_path, monkeypatch):
    hourly = {
        "2026-10-27T09:00:00-05:00": {"no-such-model-for-pricing": _usage(30)},
        "2026-10-27T08:00:00-05:00": {"gpt-5.6-sol": _usage(40)},
    }
    provider = _build(_snapshot(hourly), tmp_path, monkeypatch)
    by_date = {row["date"]: row for row in provider["series"]}
    unpriced = by_date["2026-10-27T09:00:00-05:00"]
    assert unpriced["tokens"] == 30
    assert unpriced["apiEquivalentCost"] is None
    assert unpriced["costPartial"] is True
    priced = by_date["2026-10-27T08:00:00-05:00"]
    assert priced["tokens"] == 40
    assert isinstance(priced["apiEquivalentCost"], float)
    assert "costPartial" not in priced
