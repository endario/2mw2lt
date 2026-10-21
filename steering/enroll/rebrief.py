#!/usr/bin/env python3
"""The `SessionStart` hook for `compact` and `clear` (doc 154 §8): hand the session back its latest
checkpoint at its epoch, the text the platform keeps (#6324 design §5), asked of the door once and
within `FETCH_TIMEOUT`. A door that cannot answer falls back to the pointer `checkpoint.py` left in
the workspace. What a hook adds to the context is capped at 10,000 characters, which a checkpoint's
8192 bytes and the framing stay within. It always exits 0: a hook never fails the start it is
attached to.

`--keep <session>` is the plugin's hooks module asking, at a compaction, for the line it adds to
the summariser's instructions (#3872 D5). It reads the same files, and answers even when it can
read none of them."""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))

CAP = 10_000
FETCH_TIMEOUT = 3.0  # seconds; the session's start waits on it
REBUILD = "Rebuild what you need from your card, its pull request and its issue before acting."
CLEARED_FOR = 60  # seconds a clear's SessionEnd waits for the SessionStart that follows it


def _help_or_error() -> int | None:
    from verb_help import error, help_requested, script_help
    argv = sys.argv[1:]
    if help_requested("rebrief", argv, bare=False):
        print(script_help("rebrief")); return 0
    if len(argv) == 2 and argv[0] == "--keep" and not argv[1].startswith("-"):
        return None
    if argv:
        return error("rebrief", f"unexpected arguments: {' '.join(argv)}")
    return None


def _path(ws: Path, session: str) -> Path:
    return ws / ".claude" / "steering-checkpoints" / (hashlib.sha256(session.encode()).hexdigest()[:32] + ".json")


def remember(ws: Path, session: str, boundary: str, *, checkpoint: str | None = None, note: str | None = None) -> None:
    """Keep which checkpoint the platform just recorded, by its id, or the note URL of the pointer
    form, for this hook to point at."""
    import spool
    p = _path(ws, session)
    p.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    spool.write_atomic(p, json.dumps({**({"checkpoint": checkpoint} if checkpoint else {"note": note}),
                                      "boundary": boundary,
                                      "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}))


def _where(last: dict, session: str) -> str:
    """Where the last checkpoint is read: its note, or the command that prints its text."""
    if last.get("checkpoint"):
        return f"`python3 {HERE / 'checkpoint.py'} {session} read {last['checkpoint']}`"
    return last["note"]


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
    return last if isinstance(last, dict) and (last.get("note") or last.get("checkpoint")) else None


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
        return (f"This is a new provider session after /clear of {session}: run /2mw2lt:connect before "
                "anything else. " + _handed(ws, session, records))
    session = _session(payload, records) if source == "compact" else None
    if session is None:
        return None
    return "Your context was compacted. " + _handed(ws, session, records)


def _handed(ws: Path, session: str, records: dict[str, dict]) -> str:
    """The session's latest checkpoint at its epoch, as the door holds it: its text, or why there is
    none. The daemon, not this file, knows whether a directive acknowledged since made it stale."""
    try:
        import session_routes
        token = (records.get(session) or {}).get("token")
        if not token:
            raise LookupError("no stored token")
        got = session_routes.checkpoint_now(session, token, FETCH_TIMEOUT)
    except (Exception, SystemExit) as e:  # noqa: BLE001 — whatever stops the read, the pointer stands in
        last = _last(ws, session)
        if last is None:
            return f"This session has no checkpoint recorded here, and the platform could not be asked ({e}). {REBUILD}"
        return (f"Your last checkpoint ({last.get('boundary')}, {last.get('at')}) is at {_where(last, session)}; "
                f"its text could not be fetched ({e}). Read it, and any directive you acknowledged after it, then act.")
    if got is None:
        return f"This session has no checkpoint recorded at this epoch. {REBUILD}"
    said = f"Your last checkpoint, {got.get('boundary')} at {got.get('at')}"
    if got.get("text") is not None:
        return f"{said}, follows. Act on it and on any directive you acknowledged after it.\n\n{got['text']}"
    if got.get("expired"):
        return f"{said}, expired on {got.get('expires_at')}. {REBUILD}"
    return f"{said}, is at {got.get('note')}. Read it, and any directive you acknowledged after it, then act."


def keep(session: str, cwd: Path) -> str:
    """What a compaction's summary must keep verbatim: the session, its branch and its last
    checkpoint. The card, its pull request and the lease are the daemon's, and the rebrief after
    the compaction restates them."""
    try:
        r = subprocess.run(["git", "symbolic-ref", "--short", "HEAD"], cwd=cwd,
                           capture_output=True, text=True, timeout=2)
        branch = r.stdout.strip() if r.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        branch = ""
    try:
        from local_workspace import workspace_root
        ws = workspace_root(Path(os.environ.get("CLAUDE_PROJECT_DIR") or cwd), timeout=1.0)
        last = _last(ws, session) if ws else None
    except Exception:  # noqa: BLE001 — what cannot be read is said to be unknown
        last = None
    return (f"Keep verbatim: steering session {session}; branch {branch or 'unknown'}; "
            f"last checkpoint {(last.get('checkpoint') or last['note']) if last else 'none recorded'}.")


def main() -> int:
    result = _help_or_error()
    if result is not None:
        return result
    if sys.argv[1:2] == ["--keep"]:
        print(keep(sys.argv[2], Path.cwd()))
        return 0
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
