#!/usr/bin/env python3
"""The `SessionStart` hook for `compact` and `clear` (doc 154 §8): point the session back at its
last checkpoint note. A pointer and never the note itself, because what a hook adds to the
context is capped at 10,000 characters. It reads nothing but files in the workspace, and it
always exits 0: a hook never fails the start it is attached to."""
from __future__ import annotations

import hashlib
import json
import os
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))

CAP = 10_000
CLEARED_FOR = 60  # seconds a clear's SessionEnd waits for the SessionStart that follows it


def _help_or_error() -> int | None:
    from verb_help import error, help_requested, script_help
    argv = sys.argv[1:]
    if help_requested("rebrief", argv, bare=False):
        print(script_help("rebrief")); return 0
    if argv:
        return error("rebrief", f"unexpected arguments: {' '.join(argv)}")
    return None


def _path(ws: Path, session: str) -> Path:
    return ws / ".claude" / "steering-checkpoints" / (hashlib.sha256(session.encode()).hexdigest()[:32] + ".json")


def remember(ws: Path, session: str, note: str, boundary: str) -> None:
    """Keep the note of the checkpoint the door just recorded, for this hook to point at."""
    import spool
    p = _path(ws, session)
    p.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    spool.write_atomic(p, json.dumps({"note": note, "boundary": boundary,
                                      "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}))


def _session(payload: dict, records: dict[str, dict]) -> str | None:
    """The enrolment this provider session holds here: the one minted for it, or a lone record
    minted with none, which `ack.minted_for` accepts the same way."""
    mine = [n for n, r in records.items() if r.get("provider_session") == payload.get("session_id")]
    if mine:
        return mine[0]
    if len(records) == 1 and not next(iter(records.values())).get("provider_session"):
        return next(iter(records))
    return None


def _last(ws: Path, session: str) -> dict | None:
    try:
        last = json.loads(_path(ws, session).read_text())
    except (OSError, ValueError):
        return None
    return last if isinstance(last, dict) and last.get("note") else None


def _cleared(ws: Path) -> Path:
    return ws / ".claude" / "steering-checkpoints" / "cleared"


def _took_clear(ws: Path) -> str | None:
    """The enrolment whose session was just cleared, when exactly one was: the new provider
    session holds no enrolment, so the `SessionEnd` before it said which one it was. Each
    marker is spent here, so a later clear is not told an earlier one's note."""
    now, fresh = time.time(), []
    for m in sorted(_cleared(ws).glob("*.json")) if _cleared(ws).is_dir() else []:
        try:
            rec = json.loads(m.read_text())
            m.unlink()
        except (OSError, ValueError):
            continue
        if now - rec.get("at", 0) <= CLEARED_FOR and isinstance(rec.get("session"), str):
            fresh.append(rec["session"])
    return fresh[0] if len(set(fresh)) == 1 else None


def context(payload: dict, ws: Path, records: dict[str, dict]) -> str | None:
    """What to add to the context after a compaction or a clear, or None. Nothing for a session
    steering does not enrol here, whose person has no card to be pointed at."""
    if payload.get("hook_event_name") == "SessionEnd":
        session = _session(payload, records) if payload.get("reason") == "clear" else None
        if session is not None:
            import spool
            d = _cleared(ws)
            d.mkdir(parents=True, exist_ok=True, mode=0o700)
            spool.write_atomic(d / f"{time.time_ns()}-{os.getpid()}.json",
                               json.dumps({"session": session, "at": time.time()}))
        return None  # a session ending is told nothing
    source = payload.get("source")
    if source == "clear":
        session = _took_clear(ws) if records else None
        if session is None:
            return None
        last = _last(ws, session)
        told = (f" Your last checkpoint ({last.get('boundary')}, {last.get('at')}) is {last['note']}. "
                "Read it, then act." if last else " It has no checkpoint recorded.")
        return (f"This is a new provider session after /clear of {session}: run /2mw2lt:connect before "
                "anything else." + told)[:CAP]
    session = _session(payload, records) if source == "compact" else None
    if session is None:
        return None
    last = _last(ws, session)
    if last is None:
        return ("Your context was compacted, and this session has no checkpoint recorded. Rebuild what "
                "you need from your card, its pull request and its issue before acting.")[:CAP]
    # The daemon, not this file, knows whether a directive acknowledged since made it stale.
    return (f"Your context was compacted. Your last checkpoint ({last.get('boundary')}, {last.get('at')}) "
            f"is {last['note']}. Read it, and any directive you acknowledged after it, then act.")[:CAP]


def main() -> int:
    result = _help_or_error()
    if result is not None:
        return result
    try:
        payload = json.loads(sys.stdin.read() or "{}")
        from bind import records
        from local_workspace import workspace_root
        ws = workspace_root(Path(os.environ.get("CLAUDE_PROJECT_DIR") or payload.get("cwd") or os.getcwd()),
                            timeout=1.0)
        text = context(payload, ws, records(ws)) if ws else None
    except Exception:  # noqa: BLE001 — a hook never fails the start it is attached to
        return 0
    if text and payload.get("hook_event_name") == "SessionStart":
        print(json.dumps({"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": text}}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
