"""Timestamps read from harness-owned files."""
from __future__ import annotations

from datetime import datetime, timezone


def moment(value: object) -> datetime | None:
    """One boundary for every timestamp read out of a harness-owned file.

    These files are not ours and their shapes are not guaranteed, so this answers `None` for
    anything that cannot become a moment comparable to `now` — including the cases that raise
    rather than return: an offset-less string, and a number that is not finite or is outside
    the range a datetime can hold.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(value / 1000, timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    if not isinstance(value, str) or not value:
        return None
    try:
        when = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return when if when.tzinfo is not None else None
