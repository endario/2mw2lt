"""Codex rollout, local process presence and incremental machine readings."""
from __future__ import annotations

import json
import os
import re
import subprocess
import time
from pathlib import Path

import runtime_id
import process_probe


CODEX_HOME = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex")))

# A thread's transcript, named for the day it opened and the thread it belongs to. One file per
# thread, appended for the thread's whole life across restarts of the app.
_ROLLOUT = re.compile(r"^rollout-.*-(?P<thread>[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-"
                      r"[0-9a-f]{4}-[0-9a-f]{12})\.jsonl$")
_TURN_OPEN, _TURN_CLOSE = "task_started", "task_complete"


class Refused(Exception):
    """Why nothing was delivered. The caller reports it; the envelope stays where it was."""


def rollout(thread: str, home: Path | None = None) -> Path | None:
    """The thread's transcript, or None. Searched by the id in the filename rather than by
    date, because the day in the name is the day the thread opened and says nothing about when
    it was last used."""
    root = (home or CODEX_HOME) / "sessions"
    for p in sorted(root.glob("*/*/*/rollout-*.jsonl")) if root.is_dir() else []:
        m = _ROLLOUT.match(p.name)
        if m and m["thread"] == thread:
            return p
    return None


def app_server(timeout: float = 5.0) -> bool:
    """Whether a Codex app-server runs on this machine as this user. An idle thread is reached
    through the queue, and the app-server is what drains it: a rollout on disk with none running
    is a thread nothing will read a directive into (#1967)."""
    try:
        p = subprocess.run(["pgrep", "-u", str(os.getuid()), "-f", r"(^|/)codex( .*)? app-server( |$)"],
                           capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        raise Refused("cannot look for a Codex app-server")
    # 1 is "none matched"; anything else is a lookup that failed, which is not an absence.
    if p.returncode not in (0, 1):
        raise Refused(f"pgrep answered {p.returncode} looking for a Codex app-server")
    return p.returncode == 0


def serving_pid(path: Path, timeout: float = process_probe.LSOF_TIMEOUT) -> int | None:
    """The pid of the process Codex is serving this thread with, or None when nothing is.
    Raises `process_probe.Undetermined` when lsof cannot say, which is not nothing serving it (#2349).

    The discriminator is the **access mode**, not the presence of a descriptor: the server holds
    the transcript for writing and every reader — this module included — holds it for reading.
    A rule phrased over open descriptors is one the adapter satisfies on its own behalf.
    """
    found = process_probe.writers(path, timeout)
    return found[0] if found else None


# When each thread was last seen being served, and by which process: (pid, when that process
# started, when it was seen). Per thread, because one Codex process serves several, so its being
# alive says nothing about any particular thread.
_SERVED_BY: dict[str, tuple[int, str, float]] = {}
# How long a thread stays served after its descriptor was last seen. Bounded, and bounded here
# rather than in polls, so it does not change meaning when the census changes cadence.
GRACE = 300.0


# The rule is `runtime_id`'s, so this and goose agree on what a process is.
_ident = runtime_id.started_at


def still_served(path: Path, thread: str, clock=time.monotonic) -> bool:
    """Whether a thread is still being served, asked across polls rather than at one moment.

    `serving_pid` reads the descriptor as it is now, and Codex reopens the rollout on each
    write: a poll landing between a close and the next open sees nothing while the session is
    working. Measured on the brain machine, that withdrew a reach at 15:26 for a session that
    merged a pull request at 15:55 (#677).

    So a sighting stands for `GRACE` afterwards, and only while the process that made it is the
    one still running. A pid alone cannot say that: the operating system allocates pids
    sequentially to a wrap, so a busy machine can reuse one inside the grace and answer for an
    unrelated process — which keeps the reach standing for a session that ended (#682).
    """
    now = clock()
    try:
        pid = serving_pid(path)
    except process_probe.Undetermined:
        # No reading, so no sighting either way: one that still stands answers, and otherwise
        # the question goes to the caller as unanswered, never as a thread nothing serves.
        if _standing(thread, now):
            return True
        _SERVED_BY.pop(thread, None)
        raise
    if pid is not None:
        ident = _ident(pid)
        if ident is None:
            # Nothing to re-validate a sighting against, so none is kept: the thread is served
            # while its descriptor says so and not a second past it. Neither libproc nor `ps`
            # answering is the platform having no notion of a process, not this being unsure.
            _SERVED_BY.pop(thread, None)
        else:
            _SERVED_BY[thread] = (pid, ident, now)
        return True
    if _standing(thread, now):
        return True
    _SERVED_BY.pop(thread, None)
    return False


def _standing(thread: str, now: float) -> bool:
    """Whether a sighting of `thread` still answers: inside `GRACE`, by the process that made it."""
    was = _SERVED_BY.get(thread)
    return was is not None and now - was[2] <= GRACE and _ident(was[0]) == was[1]


# Where each thread's rollout has been read to, and what it had said by then. The file is
# appended to for the thread's whole life and grows large — measured 2026-09-07, 19MB for one
# thread, with the last turn's opening record 654KB from the end — so a tail cannot be relied
# on to contain the turn in progress, and each poll reads only what is new. The third element
# is the turn that has opened and not yet accounted for its tokens; see `_fold`.
_READ: dict[str, tuple[int, dict, dict]] = {}
# The three records a reading is built from. Every other line is skipped on a substring test
# rather than parsed: they are 99% of the file and none of them says anything read here.
_WANTED = (b'"session_meta"', b'"turn_context"', b'"token_count"', b'"task_started"', b'"task_complete"',
           b'"turn_aborted"')
# How a turn's record reads as the reading's `turn` (#1795): the rollout is the one place a Codex
# turn's boundaries are written, and nothing else in a Codex session reports them.
_TURN_STATES = {_TURN_OPEN: "open", _TURN_CLOSE: "ended", "turn_aborted": "aborted"}
# What Codex calls the effort it is running at, narrowed to what a reading may state.
_EFFORTS = frozenset({"low", "medium", "high", "xhigh", "max"})


def _short(v: object) -> str | None:
    return v[:200] if isinstance(v, str) and v else None


def _fold(out: dict, opened: dict, rec: dict) -> None:
    """One rollout record folded into the reading so far, in place.

    A turn opens with `turn_context` and accounts for itself in the `token_count` records that
    follow, so a poll landing between the two would otherwise publish the new turn's model
    beside the last turn's tokens under the new turn's timestamp. What a turn opens with is
    therefore held in `opened` and becomes the reading when that turn's first accounting lands.
    Later turns win, so a thread that changed model or effort mid-life reads as what it is now.
    """
    p = rec.get("payload")
    if not isinstance(p, dict):
        return
    kind, inner = rec.get("type"), p.get("type")
    if kind == "session_meta":
        # The thread's own opening: the CLI that wrote it, the surface it was opened from, and
        # the identity every later record is attributed under. None of it belongs to a turn.
        for key, val in (("source_version", p.get("cli_version")),
                         ("entrypoint", p.get("originator")),
                         ("provider_session", p.get("session_id"))):
            if _short(val):
                out[key] = _short(val)
        ts = rec.get("timestamp")
        if isinstance(ts, str) and len(ts) >= 19:
            out.setdefault("observed_at", ts[:19] + "Z")
        return
    if kind == "turn_context":
        for key, val in (("model", p.get("model")), ("cwd", p.get("cwd"))):
            if _short(val):
                opened[key] = _short(val)
        if p.get("effort") in _EFFORTS:
            opened["effort"] = p["effort"]
        return
    if inner in _TURN_STATES:
        out["turn"] = _TURN_STATES[inner]
        ts = rec.get("timestamp")
        if isinstance(ts, str) and len(ts) >= 19:
            out["observed_at"] = ts[:19] + "Z"
        return
    if inner != "token_count":
        return
    info = p.get("info")
    if not isinstance(info, dict):
        return
    used = info.get("last_token_usage")
    if isinstance(used, dict):
        # `input_tokens` is the whole prompt, cached part included, which is what occupies the
        # window — the same quantity `vitals._tokens` sums for Claude Code.
        counts = {"input": used.get("input_tokens"), "output": used.get("output_tokens"),
                  "cache_read": used.get("cached_input_tokens"),
                  "cache_creation": used.get("cache_write_input_tokens"),
                  "thinking": used.get("reasoning_output_tokens")}
        out["tokens"] = {k: v for k, v in counts.items()
                         if isinstance(v, int) and not isinstance(v, bool) and v >= 0}
    window = info.get("model_context_window")
    if isinstance(window, int) and not isinstance(window, bool) and window > 0:
        out["context_window"] = window
    out.update(opened)
    opened.clear()
    ts = rec.get("timestamp")
    # Seconds, and the Z the rest of the system compares and parses on. The rollout writes
    # milliseconds; nothing here is finer-grained than a turn.
    if isinstance(ts, str) and len(ts) >= 19:
        out["observed_at"] = ts[:19] + "Z"


def reading(path: Path, thread: str) -> dict | None:
    """What the rollout says the thread is: the model it runs, what it is holding, and the
    window that is a fraction of. None until it has yielded one.

    A Codex session publishes nothing from inside itself, so this is read from outside it — for
    reading only, so it is never the writing descriptor `serving_pid` looks for.
    """
    since, held, opened = _READ.get(thread, (0, {}, {}))
    try:
        with open(path, "rb") as h:
            size = h.seek(0, os.SEEK_END)
            if size < since:   # truncated or replaced under us: what was read is not this file
                since, held, opened = 0, {}, {}
            h.seek(since)
            data = h.read()
    except OSError:
        # The reading already taken still stands: a file that could not be opened this time
        # says nothing about the thread, and dropping it would blank the pill on one stat.
        return dict(held) or None
    end = data.rfind(b"\n") + 1   # never a half-written last line
    for line in data[:end].splitlines():
        if not any(w in line for w in _WANTED):
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        _fold(held, opened, rec)
    _READ[thread] = (since + end, held, opened)
    return dict(held) or None
