"""ISO-8601 timestamp parsing shared by the product modules.

``naive`` says what a value without a UTC offset means:

* ``"local"``  the system's local time (``datetime.timestamp()`` semantics)
* ``"utc"``    UTC
* ``"reject"`` not a timestamp; the caller gets None
"""

from __future__ import annotations

from datetime import datetime, timezone

NAIVE_MODES = ("local", "utc", "reject")


def parse_iso_datetime(value: object, *, naive: str) -> datetime | None:
    """An aware UTC datetime for an ISO-8601 string, or None.

    ``Z`` and numeric offsets are honoured. Non-strings, unparseable text and
    instants that do not fit the datetime range in UTC return None.
    """
    if naive not in NAIVE_MODES:
        raise ValueError(f"unknown naive mode: {naive!r}")
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            if naive == "reject":
                return None
            if naive == "utc":
                parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except (OverflowError, OSError, ValueError, TypeError):
        return None


def parse_iso_epoch(value: object, *, naive: str) -> float | None:
    """Epoch seconds for an ISO-8601 string, or None (see :func:`parse_iso_datetime`)."""
    parsed = parse_iso_datetime(value, naive=naive)
    return None if parsed is None else parsed.timestamp()
