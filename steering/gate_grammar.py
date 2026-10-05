"""The gate lines a session sends and the vendors it may name (doc 81 §2)."""
from __future__ import annotations

import re


# What each vendor is asked to run (doc 81 §2). Which vendor is tried first is unlimited's rank,
# over the gate's own history (#2444): "no more built-in rules or heuristics on our end" (owner,
# 2026-09-26). `STEERING_JUDGE_ORDER` is a machine's own list of the vendors it may offer.
# The model each vendor runs is not here: it is unlimited's model catalog's, read when a gate is
# commissioned (#2337), and the effort is this project's own policy. `stealth` runs unnamed
# preview models whoever makes them: independent of every other vendor, never of itself (owner).
# At `heavy` only the vendors the catalog lists a heavy model for run, at the runner's heavy
# settings (doc 127 §2).
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
