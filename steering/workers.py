"""What a worker's launch record is, for every harness that launches workers (doc 63, doc 82 §4).

A record names processes by pid and start time, written by the agent that launched them. What
each harness proves from it differs — Goose's vendor is the agent's child, opencode's server is
its leash's — so the proof stays with the harness; the record, the liveness test and the sweep
are shared.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Callable

from process_probe import LSOF_TIMEOUT, Undetermined, live, pid_or_none, writers
from worker_probe import record, read, shape, workspace_of
import spool
import ghmeter

# `enrol` and `detach` run the enrolment scripts' own functions, and opencode reaches them
# without Goose having put the directory on the path first.
sys.path.insert(0, str(Path(__file__).resolve().parent / "enroll"))



def write(directory: Path, worker: str, fields: dict) -> None:
    """Replace the record whole, so a reader never sees half of one."""
    path = record(directory, worker)
    path.parent.mkdir(parents=True, exist_ok=True)
    spool.write_atomic(path, json.dumps({"v": 1, **fields}) + "\n")


def sweep(directory: Path, judged: tuple[str, str] = ("pid", "since"),
          companions: Callable[[str], list[Path]] = lambda w: []) -> list[str]:
    """Drop the records whose `judged` process — a (pid, start time) pair of keys — is gone, with
    the files that go with them, and say whose. It signals nothing: a record is writable by
    anything running as this user, so it can say what ended but never authorize ending
    something (doc 59 §2)."""
    gone: list[str] = []
    if not directory.is_dir():
        return gone
    for path in sorted(directory.glob("*.pid")):
        try:
            rec = json.loads(path.read_text())
        except (OSError, ValueError):
            rec = {}                       # unreadable is not a worker either
        if not isinstance(rec, dict):
            rec = {}
        if live(rec.get(judged[0]), rec.get(judged[1])):
            continue
        worker = path.stem
        for f in companions(worker):
            f.unlink(missing_ok=True)
        path.unlink(missing_ok=True)
        gone.append(worker)
    return gone


class Unenrolled(RuntimeError):
    """Why a launched worker could not be made one the fleet addresses."""


ENROL_ATTEMPTS = 3


# The workspace each worker this process enrolled keeps its token in: a worker may work in a clone
# of its own (doc 88), so it is not always this process's.
WORKSPACES: dict[str, Path] = {}


def enrol(provider: str, worker: str, project: Path, doing: str) -> tuple[str, str]:
    """Enrol a worker this process launched: the name the fleet knows it by, and the door's
    answer to `bind:`.

    The name comes back even when the bind did not, and that is the point: an `enroll:` that
    landed leaves a stored token and a row whether or not the bind after it did, so a caller
    that only learned the refusal would have nothing left to detach.

    The same `connect.connect` a session runs for itself, which is the point: a worker is
    enrolled, bound and named by the one implementation, so a change to the ritual reaches it
    without this file being told. `project` is passed rather than left to the cwd because the
    agent holds its workspace explicitly and launchd chose where it started.

    A launched harness installs no hook, so `connect` witnesses the incarnation itself. Its
    precondition — that a live process deriving to the claimed runtime holds the transcript
    open — is what the manager arranged one line ago, and it is checked rather than assumed.
    """
    import connect as connect_mod
    import machine_harness as harness_mod
    from workspace import required_workspace_root
    h = harness_mod.of(provider)
    try:
        ws = required_workspace_root(project, timeout=2.0)
    except SystemExit as why:
        # That helper is written for an operator at a terminal and exits rather than raising.
        # In here it would leave the route's `except Exception` untouched and the request
        # unanswered, so it becomes the failure this function declares.
        raise Unenrolled(str(why)) from None
    import door
    account = connect_mod.account(connect_mod.Options(), h)
    # At the workspace's own door, whichever workspace the process that launched it serves.
    # A lookup that could not answer says nothing about the worker just started, and the launch
    # retires whatever this fails to enrol, so it is asked again before it is given up (#2356).
    with door.anchored(ws):
        for attempt in range(ENROL_ATTEMPTS):
            try:
                session, answer, _rid, _psession = connect_mod.connect(
                    ws, None, account, doing, h, psession=worker, project=project)
                WORKSPACES[session] = ws
                ghmeter.bind(worker, session)
                return session, answer
            except Undetermined as e:
                unread = e
    raise Unenrolled(f"whether {worker} is served here could not be read "
                     f"in {ENROL_ATTEMPTS} attempts: {unread}")


def detach(session: str | None, project: Path, worker: str | None = None) -> str | None:
    """End an enrolment, as `disconnect.py` does: Go's answer, then the token.

    The token is dropped only on `detached:`. One dropped on a refusal leaves an enrolment
    nothing holds the token to end, and the row outlives every process that knew about it.

    Without a session it is the one stored for `worker`: an enrolment whose binding raised
    stored a token its caller never learned the name of. None when nothing was stored.
    """
    from ack import token_path
    from bind import records
    from connect import minted_name
    from door import anchored
    import session_routes
    from workspace import required_workspace_root
    try:
        ws = required_workspace_root(project, timeout=2.0)
    except (SystemExit, OSError, subprocess.SubprocessError) as why:
        # Answered rather than raised: the worker's process is already stopped by the time this
        # runs, and `retire_all` walks every worker, so one that cannot reach its workspace must
        # not take the rest of the shutdown with it. With no name, nothing was enrolled: an
        # enrolment is refused outside a workspace before it is sent.
        return f"{session} could not be detached: {why}" if session else None
    known = records(ws)
    session = session or (minted_name(known, worker) if worker else None)
    if session is None:
        return None
    rec = known.get(session)
    if rec is None:
        return f"no stored token for {session}"
    try:
        # Retired, never abandoned: a worker that finished and pushed is retired too (doc 123 §4).
        with anchored(ws):
            answer = session_routes.detach(session, rec["token"])
    except (Exception, SystemExit) as why:
        return f"{session} could not be detached: {why}"
    if answer.startswith("detached:"):
        token_path(ws, session).unlink(missing_ok=True)
    return answer


def board(session: str, line: str) -> str:
    """One `done:` or `blocked:` for a worker, on Go's status route, and what Go answered.

    Nothing awaits these, so a refusal that raised would otherwise lose the line in silence. It is
    sent on the worker's stored token (doc 131 §5), and printed as it was given.
    """
    from ack import token_path
    from door import anchored, occurrence
    import session_routes
    from verb import status_body
    from workspace import workspace_root
    ws = WORKSPACES.get(session)
    try:
        if ws is None:
            ws = workspace_root(Path(os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()), timeout=2.0)
        verb, _, rest = line.partition(": ")
        _, _, text = rest.partition(" ")
        body = status_body(verb, text) if verb in ("done", "blocked") else None
        if body is None:
            raise ValueError(f"not a done or blocked line: {line[:80]}")
        token = json.loads(token_path(ws, session).read_text())["token"]
        with anchored(ws):
            session_routes.status(session, token, occurrence(), body)
        answer = f"registered: {verb}"
    except (Exception, SystemExit) as why:
        answer = f"not said: {why}"
    print(f"agent: {line[:200]} -> {answer}", flush=True)
    return answer
