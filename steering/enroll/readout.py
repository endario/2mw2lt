#!/usr/bin/env python3
"""The readout: the vitals sender, a Claude Code `Stop` hook (doc 30 §3).

Installed beside `observe.py` in the same pack:
  {"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "python3 <repo>/steering/enroll/readout.py", "timeout": 5}]}]}}

A separate handler rather than a branch inside `observe.py`: that adapter enforces doc 15's
identifiers-and-enums allow-list, and vitals are the one record that is neither. Named for the
act rather than the subject because a script cannot import a module its own basename shadows —
Python puts a script's own directory first on the path.

Nothing here is spooled: a reading states what is true now, so one that could only be
delivered later is worth nothing.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parents[1]))
import python_floor  # noqa: E402

python_floor.require()

import hashlib
import json
import os
import time

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
import runtime_id  # noqa: E402
import targeting  # noqa: E402
import verb_help  # noqa: E402
import vitals as adapter  # noqa: E402
from local_workspace import workspace_root  # noqa: E402
from door import failure, record  # noqa: E402
import observe_post  # noqa: E402

BUDGET = 4.0  # seconds, end to end — the pack's per-handler timeout is 5
# Seconds between readings sent from inside a turn (#1159). The store is in memory (doc 30 §6), so
# a restart blanks a reading until the session's next Stop, and one long turn reaches none.
EVERY = 300


def transcript_of(hook: dict) -> Path | None:
    p = hook.get("transcript_path")
    return Path(p) if isinstance(p, str) and p else None


def last_assistant_entry(hook: dict) -> dict | None:
    """The transcript's last assistant entry for this hook, or None — the one transcript read
    the vitals and the observation's reply head both share."""
    path = transcript_of(hook)
    if path is None:
        return None
    try:
        return adapter.last_assistant(adapter.tail(path))
    except OSError:
        return None


def last_assistant_text(hook: dict) -> str | None:
    """The last assistant entry's own text, or None."""
    entry = last_assistant_entry(hook)
    if entry is None:
        return None
    content = (entry.get("message") or {}).get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [b["text"] for b in content if isinstance(b, dict) and b.get("type") == "text" and isinstance(b.get("text"), str)]
        return "".join(parts) or None
    return None


def build(hook: dict, restated: bool = False) -> dict | None:
    """The record this turn end can state, or None when the transcript yields no answer.

    `restated` is a reading sent again with no turn behind it, so it carries the instant its
    entry was written rather than now: the daemon takes a reading's instant as the session's
    evidence of life, and a hold reopening by itself is not the session doing anything (#2722)."""
    return adapter.of_entry(last_assistant_entry(hook), hook, restated)


def due(ws: Path, hook: dict, now: float) -> bool:
    """Whether a reading is owed from inside this session's turn, claiming it if so: none has been
    sent for the session in `EVERY` seconds. Claimed before the send, so a slow one is not
    repeated by the tool calls that land while it is in flight."""
    sid = hook.get("session_id")
    if not isinstance(sid, str) or not sid:
        return False
    stamp = ws / ".claude" / "steering-readout" / hashlib.sha256(sid.encode()).hexdigest()[:32]
    try:
        if now - stamp.stat().st_mtime < EVERY:
            return False
    except FileNotFoundError:
        pass
    stamp.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    stamp.touch()
    os.utime(stamp, (now, now))
    return True


def with_tmux(rec: dict, left: float) -> tuple[str, str] | None:
    """Whether a window is on the tmux session this process runs in, stated on the reading. Said
    only inside a pane, and never at the cost of the reading: the lookup makes up to three tmux calls
    in a row, each held to a sixth of what is left, half a second at most. A reading without it
    would read as a window gone, so a mid-turn one carries it too. Answers the (socket, pane)."""
    if left < 0.5:
        return None
    each = min(0.5, left / 6)
    import subprocess
    import wakeexec
    tmux = os.environ.get("TMUX")
    try:
        where, state = wakeexec.pane_state(
            os.getppid(), socket=tmux.split(",", 1)[0] if tmux else None,
            run=lambda *a, **k: subprocess.run(*a, **{**k, "timeout": each}))
    except Exception:
        return None
    if state:
        rec["tmux"] = state
    return where


def midturn(hook: dict, ws: Path, rid: str | None, timeout: float) -> None:
    """The reading a Stop would send, sent from a tool call instead. Raises what `post` raises."""
    rec = build(hook)
    if rec is None:
        return
    if rid:
        rec["runtime_id"] = rid
    deadline = time.monotonic() + timeout
    with_tmux(rec, timeout)
    observe_post.post(rec, ws, max(0.1, deadline - time.monotonic()))


def main() -> int:
    argv = sys.argv[1:]
    if verb_help.help_requested("readout", argv, bare=False):
        print(verb_help.script_help("readout")); return 0
    if argv:
        return verb_help.error("readout", f"unexpected arguments: {' '.join(argv)}")
    deadline = time.monotonic() + BUDGET
    try:
        hook = json.load(sys.stdin)
    except ValueError:
        return finish()
    if not isinstance(hook, dict):
        return finish()
    rec = build(hook)
    if rec is None:
        return finish()
    try:
        rid = runtime_id.derive("claude", timeout=min(1.0, max(0.1, deadline - time.monotonic() - 1.5)))
        if rid:
            rec["runtime_id"] = rid
    except Exception:
        pass  # no runtime: the record still states the session, and the projection drops it
    left = deadline - time.monotonic()
    if left < 0.2:
        return finish()
    pane = with_tmux(rec, left)
    left = deadline - time.monotonic()
    sent_at = time.monotonic(); anchor = Path(os.environ.get("CLAUDE_PROJECT_DIR") or hook.get("cwd") or os.getcwd()); where = anchor
    ws = anchor  # a fallback the targeting block below can still post through if resolution fails
    try:
        ws = workspace_root(anchor, timeout=1.0); where = ws
        observe_post.post(rec, ws, min(2.0, left))
    except Exception as e:  # a reading is worth exactly one attempt; the next turn end brings another
        record("readout", f"post failed after {time.monotonic() - sent_at:.2f}s: {failure(e)}", where)
    # Targeting facts: only for a claude-vscode session, only with budget to spare — a hard
    # precondition (doc 113 §4), not a caveat. Best-effort past this point, independent of
    # whether the vitals post above succeeded: a slow turn loses this turn's refresh and keeps
    # whatever the store already holds, never the hook's deadline. Gated on `rec.get("entrypoint")`
    # rather than a second transcript read: `build(hook)` above already copied it in from the same
    # entry (`vitals.read()`'s own `entry.get("entrypoint")`), so nothing here needs re-reading
    # the transcript just to decide whether to proceed.
    left = deadline - time.monotonic()
    # A pane is the session's route wherever it runs, as connect handed it over (doc 118 §3.1), with
    # whether a window is on it, which the desk marks (#5026).
    if pane is not None and rec.get("runtime_id") and left > 1.0:
        try:
            trec = targeting.build(provider_session=hook.get("session_id"),
                                   runtime_id=rec["runtime_id"], observed_at=adapter.utc(),
                                   entrypoint=targeting.TMUX_ENTRYPOINT, pid=os.getppid(), cwd=str(anchor),
                                   tmux=pane, tmux_attached=rec.get("tmux") == "attached" if rec.get("tmux") else None)
            why = targeting.validate(trec)
            if why:
                raise RuntimeError(why)
            observe_post.post(trec, ws, min(1.0, deadline - time.monotonic()), route="targeting")
        except Exception as e:
            record("readout", f"tmux targeting capture failed: {failure(e)}", where)
    elif rec.get("entrypoint") == targeting.TAB_ENTRYPOINT and left > 1.0:
        try:
            entry = last_assistant_entry(hook)
            if entry is None:
                raise RuntimeError("transcript no longer yields an assistant entry")
            walk_pid = os.getppid()
            instance = runtime_id.instance_of(walk_pid)
            trec = targeting.read(hook, entry, rec.get("runtime_id"), adapter.utc(),
                                  pid=walk_pid, instance=instance)
            if trec is not None:
                observe_post.post(trec, ws, min(1.0, deadline - time.monotonic()), route="targeting")
        except Exception as e:
            record("readout", f"targeting capture failed: {failure(e)}", where)
    return finish()


def finish() -> int:
    sys.stdout.write("{}\n"); sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
