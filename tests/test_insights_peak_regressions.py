import json
from datetime import datetime, timezone

import pytest

from token_tracker import insights


def test_non_owner_peak_read_does_not_compact_or_unlink(tmp_path):
    path = tmp_path / "peaks.jsonl"
    path.write_bytes("".join(json.dumps({"provider": "Claude", "date": f"2026-10-{day:02}",
                                          "fiveHour": day, "weekly": day}) + "\n"
                           for day in range(1, 25)).encode())
    before = path.read_bytes()
    store = insights.LimitPeakStore(path, owner_check=lambda: False, max_bytes=128, max_days=2)

    result = store.peaks(["Claude"], {"2026-10-24"})

    assert result["Claude"]["fiveHour"] == 24
    assert path.read_bytes() == before


def test_non_owner_peak_read_preserves_oversized_unusable_journal(tmp_path):
    path = tmp_path / "empty-peaks.jsonl"
    path.write_bytes(b"{malformed}\n" * 100)
    before = path.read_bytes()
    store = insights.LimitPeakStore(path, owner_check=lambda: (_ for _ in ()).throw(RuntimeError("no owner")),
                                    max_bytes=128, max_days=2)

    assert store.peaks(["Claude"], {"2026-10-27"})["Claude"] == {"fiveHour": None, "weekly": None}
    assert path.exists() and path.read_bytes() == before


@pytest.mark.parametrize("owner", [True, False])
def test_peak_readers_skip_invalid_calendar_dates_and_numeric_dates(tmp_path, owner):
    path = tmp_path / "peaks.jsonl"
    rows = [{"provider": "Claude", "date": f"2026-09-{day:02}", "fiveHour": 10,
             "padding": "x" * 90} for day in range(1, 21)]
    rows.extend([
        {"provider": "Claude", "date": 123, "fiveHour": 99},
        {"provider": "Claude", "date": "2026-02-30", "weekly": 99},
        {"provider": "Claude", "date": "2026-10-27", "fiveHour": 10 ** 400},
        {"provider": "Claude", "date": "2026-10-27", "fiveHour": 30},
        ["malformed JSON object shape"],
    ])
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
    store = insights.LimitPeakStore(path, owner_check=lambda: owner, max_bytes=1024)

    assert store.peaks(["Claude"], {"2026-10-27"})["Claude"]["fiveHour"] == 30


def test_record_limit_peaks_requires_fresh_connected_sample_and_uses_observation_day(tmp_path, monkeypatch):
    path = tmp_path / "peaks.jsonl"
    store = insights.LimitPeakStore(path, owner_check=lambda: True)
    monkeypatch.setattr(insights, "_default_store", lambda: store)
    now = datetime(2026, 10, 26, 18, 31, tzinfo=timezone.utc)
    snapshot = {"providers": [{"name": "Claude", "connected": True, "stale": False,
                               "limitsUpdatedAt": "2026-10-26T18:25:00+00:00",
                               "fiveHour": {"percent": 95}}]}

    insights.record_limit_peaks(snapshot, allowed_names={"Claude"}, now=now)

    assert store.peaks(["Claude"], {"2026-10-26"})["Claude"]["fiveHour"] == 95
    assert store.peaks(["Claude"], {"2026-10-27"})["Claude"]["fiveHour"] is None


def test_record_limit_peaks_ignores_disconnected_stale_or_unstamped_samples(tmp_path, monkeypatch):
    path = tmp_path / "peaks.jsonl"
    store = insights.LimitPeakStore(path, owner_check=lambda: True)
    monkeypatch.setattr(insights, "_default_store", lambda: store)
    now = datetime(2026, 10, 27, 12, tzinfo=timezone.utc)
    providers = [
        {"name": "Claude", "connected": False, "stale": False, "limitsUpdatedAt": now.isoformat(), "fiveHour": {"percent": 95}},
        {"name": "Codex", "connected": True, "stale": True, "limitsUpdatedAt": now.isoformat(), "fiveHour": {"percent": 94}},
        {"name": "Claude", "connected": True, "stale": False, "limitsUpdatedAt": None, "fiveHour": {"percent": 93}},
    ]

    insights.record_limit_peaks({"providers": providers}, allowed_names={"Claude", "Codex"}, now=now)

    assert not path.exists()


def test_record_limit_peaks_ignores_invalid_or_future_observation_times(tmp_path, monkeypatch):
    path = tmp_path / "peaks.jsonl"
    store = insights.LimitPeakStore(path, owner_check=lambda: True)
    monkeypatch.setattr(insights, "_default_store", lambda: store)
    now = datetime(2026, 10, 27, 12, tzinfo=timezone.utc)
    providers = [
        {"name": "Claude", "connected": True, "stale": False, "limitsUpdatedAt": "bad", "fiveHour": {"percent": 95}},
        {"name": "Codex", "connected": True, "stale": False, "limitsUpdatedAt": "2026-10-27T12:01:00Z", "fiveHour": {"percent": 94}},
    ]

    insights.record_limit_peaks({"providers": providers}, allowed_names={"Claude", "Codex"}, now=now)

    assert not path.exists()
