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


def connect_path(ws: Path, psession: str) -> Path:
    """Where connect records that it bound this session, for the module to start its hold from."""
    return claim_path(ws, psession).with_suffix(".connect")


def mark_connected(ws: Path, psession: str, now: float | None = None) -> None:
    try:
        connect_path(ws, psession).write_text(repr(time.time() if now is None else now))
    except (OSError, ValueError):
        pass  # the module still starts on the command it recognises, or within its next refresh


def connected_at(ws: Path, psession: str) -> float | None:
    try:
        return float(connect_path(ws, psession).read_text())
    except (OSError, ValueError):
        return None


def fresh(ws: Path, psession: str, now: float | None = None) -> bool:
    try:
        claim = json.loads(claim_path(ws, psession).read_text())
        at = float(claim["at"])
    except (OSError, ValueError, KeyError, TypeError):
        return False
    return claim.get("provider_session") == psession and \
        0 <= (time.time() if now is None else now) - at <= FRESH_S


def state(ws: Path, psession: str, now: float | None = None) -> dict | None:
    """The plugin's own account of this session's hold, as its module last wrote it, while its
    claim is fresh; None when the plugin holds nothing for this session (#5641)."""
    if not fresh(ws, psession, now):
        return None
    try:
        hold = json.loads(claim_path(ws, psession).read_text()).get("hold")
    except (OSError, ValueError, AttributeError):
        return None
    return hold if isinstance(hold, dict) else None


def _at(t: object) -> str:
    try:
        return time.strftime("%H:%M:%SZ", time.gmtime(float(t)))   # type: ignore[arg-type]
    except (TypeError, ValueError, OverflowError, OSError):
        return "?"


def said(hold: dict) -> str:
    """One line for the session: holding (which hold, since when), retrying (attempt, the last
    refusal, the next try), stopped (why, and the remedy), or starting."""
    s = hold.get("state")
    if s == "holding":
        return f"The 2mw2lt plugin holds this session's stream: hold {hold.get('hold')}, admitted at {_at(hold.get('since'))}."
    if s == "retrying":
        return (f"The 2mw2lt plugin is retrying this session's hold: attempt {hold.get('attempt')}, "
                f"last refused: {hold.get('refusal')}; next try at {_at(hold.get('next_at'))}. Arm no hold yourself.")
    if s == "stopped":
        return (f"The 2mw2lt plugin stopped holding this session's stream: {hold.get('refusal')}. "
                f"To repair it, {hold.get('remedy')}.")
    return "The 2mw2lt plugin is starting this session's hold."


# How long after connect the module may take to start its hold before that is said as a fault: it
# reacts to the connect command at once, and to the connect record within its 60 s refresh.
START_S = 75


def unstarted(ws: Path, psession: str, now: float | None = None) -> str | None:
    """With a live module that has started no hold since connect asked it to: why, and the remedy."""
    if not fresh(ws, psession, now) or state(ws, psession, now) is not None:
        return None
    at, now = connected_at(ws, psession), time.time() if now is None else now
    if at is None:   # never connected here, or a module from before connect's record: nothing to judge
        return None
    if now - at < START_S:
        return f"The 2mw2lt plugin is starting this session's hold, asked by connect at {_at(at)}. Arm no hold yourself."
    return (f"The 2mw2lt plugin's module started no hold after connect at {_at(at)}. Run connect.py --current "
            "again; if it still starts none, restart or --resume this process so the module reloads. "
            "Arm no hold yourself.")


def describe(ws: Path, psession: str) -> str | None:
    """`hold.py --plugin-state <provider session>`: the line above, why a live module holds nothing,
    or nothing when the plugin's module is not live for this session."""
    hold = state(ws, psession)
    return said(hold) if hold is not None else unstarted(ws, psession)
