"""The holder claim: the plugin's hooks module, not the model, holds this session's stream.

The module writes it and refreshes it every 60 s; connect and hold.py read it. A claim that is
missing, unreadable, someone else's or older than FRESH_S means the module is not live, and the
session keeps today's recipe (#3872 D2). Nothing in the daemon reads it.
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path

FRESH_S = 180

SAFE = re.compile(r"[A-Za-z0-9_-]{1,128}")


def claim_path(ws: Path, psession: str) -> Path:
    if not SAFE.fullmatch(psession):
        raise ValueError("a provider session is a filename-safe token")
    return Path(ws) / ".claude" / "steering-holders" / f"{psession}.json"


def fresh(ws: Path, psession: str, now: float | None = None) -> bool:
    try:
        claim = json.loads(claim_path(ws, psession).read_text())
        at = float(claim["at"])
    except (OSError, ValueError, KeyError, TypeError):
        return False
    return claim.get("provider_session") == psession and \
        0 <= (time.time() if now is None else now) - at <= FRESH_S
