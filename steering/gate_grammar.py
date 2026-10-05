"""The gate lines a session sends and the vendors it may name (doc 81 §2)."""
from __future__ import annotations

import re


# The effort each vendor is asked to run at, per tier. `STEERING_JUDGE_ORDER` is a machine's own
# list of the vendors it may offer.
EFFORT = {"standard": {"glm": "high", "codex": "xhigh", "claude": "high", "grok": "high",
                       "deepseek": "high", "meta": "high", "stealth": "high"},
          "heavy": {"glm": "high", "codex": "low", "claude": "high"}}
REVIEWERS = tuple(EFFORT["standard"])
TIERS = tuple(EFFORT)
_STATUS = re.compile(r"^gate:\s*status\s+(?P<id>[0-9A-Za-z]{8,40})\s+token\s+\S+\s*$")
_CANCEL = re.compile(r"^gate:\s*cancel\s+(?P<id>[0-9A-Za-z]{8,40})\s+token\s+\S+\s*$")


def parse_status(text: str) -> str | None:
    """The commission a `gate: status <id>` line asks after, or None."""
    m = _STATUS.match(text)
    return m["id"] if m else None


def parse_cancel(text: str) -> str | None:
    """The commission a `gate: cancel <id>` line withdraws, or None."""
    m = _CANCEL.match(text)
    return m["id"] if m else None
