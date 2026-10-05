"""The padded UTC-seconds wire timestamp and its validity predicate."""
from __future__ import annotations

import re
import time


def now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


_INSTANT = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")


def is_instant(s: object) -> bool:
    """Whether `s` is an instant of the one shape `now` mints, and so orderable against another.

    Every store that keeps a newest-wins record compares these as strings, which is only sound
    while they all have this shape: `"t"` sorts above every real stamp and would stay newest for
    good. The predicate lives beside the minting so the readers cannot each hold their own idea
    of what a stamp looks like.

    Neither check alone is enough. The shape admits `"2026-13-99T99:99:99Z"`, which is no date,
    so no reader can take an age from it; parsing admits `"2026-9-20T12:00:00Z"`, which is a
    date and sorts above December's because the month is not padded. Both are required, and
    between them they leave the trailing-junk case doubly covered.
    """
    if not isinstance(s, str) or not _INSTANT.match(s):
        return False
    try:
        time.strptime(s, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        return False
    return True
