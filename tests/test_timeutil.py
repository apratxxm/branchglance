"""Table tests for the shared ISO-8601 parser: Z, offset, naive, garbage, out-of-range."""
from datetime import datetime, timezone

import pytest

from token_tracker import timeutil

UTC_EPOCH = 1767225600.0  # 2026-01-01T00:00:00Z


@pytest.mark.parametrize("value, mode, expected", [
    # Z suffix and numeric offsets are the same instant, whatever the naive mode.
    ("2026-01-01T00:00:00Z", "local", UTC_EPOCH),
    ("2026-01-01T00:00:00Z", "utc", UTC_EPOCH),
    ("2026-01-01T00:00:00Z", "reject", UTC_EPOCH),
    ("2026-01-01T01:00:00+01:00", "local", UTC_EPOCH),
    ("2026-01-01T01:00:00+01:00", "utc", UTC_EPOCH),
    ("2026-01-01T01:00:00+01:00", "reject", UTC_EPOCH),
    # Naive text: UTC or local by mode; refused by "reject".
    ("2026-01-01T00:00:00", "utc", UTC_EPOCH),
    ("2026-01-01T00:00:00", "local", datetime(2026, 1, 1).timestamp()),
    ("2026-01-01T00:00:00", "reject", None),
    # Garbage and non-strings.
    ("not a timestamp", "utc", None),
    ("", "local", None),
    ("2026-13-40T00:00:00Z", "utc", None),
    (None, "utc", None),
    (1767225600, "reject", None),
    # Out of range once converted to UTC (year 10000 for this offset).
    ("9999-12-31T23:59:59-01:00", "utc", None),
    ("9999-12-31T23:59:59-01:00", "reject", None),
])
def test_parse_iso_epoch_table(value, mode, expected):
    assert timeutil.parse_iso_epoch(value, naive=mode) == expected


def test_parse_iso_datetime_is_aware_utc():
    parsed = timeutil.parse_iso_datetime("2026-01-01T01:00:00+01:00", naive="reject")
    assert parsed == datetime(2026, 1, 1, tzinfo=timezone.utc)
    assert parsed.utcoffset().total_seconds() == 0


def test_unknown_naive_mode_is_a_programming_error():
    with pytest.raises(ValueError):
        timeutil.parse_iso_epoch("2026-01-01T00:00:00Z", naive="assume")
