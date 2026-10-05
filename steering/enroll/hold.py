#!/usr/bin/env python3
"""`hold.py [--until-event|--service] [--provider <harness>] [--provider-session <id>] [--pid <pid>] [<session>]`:
hold this session's stream at its agent.

`--until-event` exits 0 once it has printed the first frame the session must act on, so a
harness that wakes an idle session when a background command ends is woken by that frame — no
watch deadline to re-arm (#231).

Every value the hold needs is read, not carried: the token from the enrollment's own store,
the runtime id derived from the running process the way `bind:` pinned it, the session name
from the enrollment minted for it, and the port from the workspace's record of its agent. A
shell that opens the stream therefore needs nothing exported from the one that ran `connect`
— a value printed for one shell and re-typed into another was how a stream opened with a
runtime id the epoch had superseded, refused exactly as a genuinely stale one is (#1073).
"""
from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import sys
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))
import action_notice  # noqa: E402
from local_workspace import agent_port, required_workspace_root, workspace_header  # noqa: E402
from moments import moment  # noqa: E402
import door  # noqa: E402
from bind import Refused, incarnation, pid_arg  # noqa: E402
from connect import agent_says, harness_of, own_enrolment, project_dir  # noqa: E402
import machine_harness as harness_mod  # noqa: E402
from process_probe import Undetermined  # noqa: E402
import observe_post  # noqa: E402
import readout  # noqa: E402
import spool  # noqa: E402
from verb_help import current_args, error, help_requested, script_help  # noqa: E402

REOPEN_AFTER = 2.0    # seconds between opens; the loop the skills carried slept the same
HOLD_READ = 90.0      # the agent keeps the stream alive every 20s; several missed means dead
MAX_FRAME_RECORD_BYTES = 64 * 1024
# The frames `--until-event` ends on. Presence, usage and fleet are state a later frame restates,
# and `closed` for an uplink is reopened here; a revoked hold is the session's to answer. A say that
# is an action's routine ending is recorded and read like any other, and ends nothing (#2455).
# A room frame says a room has a message the seat has not read (doc 156 §7).
ACTS = frozenset({"envelope", "say", "seat", "kick", "room"})
# Except a usage frame moving this session's account to this verdict: the pack-up trigger
# (doc 117 §5).
PACK_UP = "excluded"


def _frame(line: str) -> dict | None:
    if not line.startswith("data: "):
        return None
    try:
        frame = json.loads(line[6:])
    except ValueError:
        return None
    return frame if isinstance(frame, dict) else None


def is_usage(line: str) -> bool:
    frame = _frame(line)
    return bool(frame) and frame.get("kind") == "usage"


# How far two namings of one window's reset may sit apart and still be one exclusion: the
# reset is re-derived at every reading and its sub-second part moves with it — one exhausted
# account's frames named 22:59:59.611396, 23:00:00.490070 and everything between, all one
# reset (2026-09-30, #3222). Taken as a string, each reading was a new exclusion and woke a
# session that had already been told — five times in an hour. The windows the daemon reads are
# hours to days wide, so seconds of tolerance cannot merge two of them.
RESET_JITTER = 2.0


def exclusion_of(verdict: dict) -> datetime | str:
    """The moment `verdict`'s exclusion lifts, or the string it named when that cannot be
    computed (`""` when it names none). Compared through `same_exclusion`."""
    until = verdict.get("until")
    named = moment(until)
    return named if named is not None else until if isinstance(until, str) else ""


def same_exclusion(found: datetime | str, seen: datetime | str | None) -> bool:
    """That `found` names the exclusion `seen` was woken for: the same moment within
    `RESET_JITTER`, or — when either naming cannot be a moment — the same string."""
    if isinstance(found, datetime) and isinstance(seen, datetime):
        return abs((found - seen).total_seconds()) < RESET_JITTER
    return found == seen


def verdict_of(line: str) -> str | None:
    """The verdict state a usage frame carries, or None for any other line — and for an
    exclusion that has already lifted, a stale reading that would otherwise wake the session on
    nothing (#1459)."""
    if not is_usage(line):
        return None
    verdict = _frame(line).get("verdict")
    if not isinstance(verdict, dict):
        return None
    until = exclusion_of(verdict)
    return None if isinstance(until, datetime) and until <= datetime.now(timezone.utc) \
        else verdict.get("state")


def exclusion(line: str) -> datetime | str | None:
    """Which exclusion a usage frame carries (`""` when it names none), or None when the frame
    excludes nothing or its exclusion has already lifted — `exclusion_of` names it."""
    if verdict_of(line) != PACK_UP:
        return None
    return exclusion_of(_frame(line)["verdict"])


def lifted(line: str) -> bool:
    """Whether a usage frame is an exclusion that has already lifted, which clears the one
    remembered (#1459). Being ranked again does not: that is the forecast crossing back."""
    frame = _frame(line)
    verdict = frame.get("verdict") if frame and frame.get("kind") == "usage" else None
    return (isinstance(verdict, dict) and verdict.get("state") == PACK_UP
            and verdict_of(line) is None)


def acts(line: str, seen: datetime | str | None = None) -> bool:
    """Whether a stream line is a frame the session must act on. `seen` is the exclusion the
    session was last woken for, by the moment it lifts. An account whose forecast crosses the
    threshold back and forth is excluded again with the same reset, and that wakes nothing
    more (#2552) — however its reason or its sub-second string moves (#3222). A different
    reset is another exclusion, and wakes."""
    frame = _frame(line)
    if frame is None:
        return False
    if frame.get("kind") == "usage":
        found = exclusion(line)
        return found is not None and not same_exclusion(found, seen)
    if frame.get("kind") == "say" and action_notice.routine(frame.get("from"), frame.get("text")):
        return False
    return frame.get("kind") in ACTS or (frame.get("kind") == "closed" and frame.get("why") == "revoked")


def last_exclusion(frames: Path) -> datetime | str | None:
    """The exclusion an earlier run already woke the session for, since each `--until-event` run
    is a new process: the newest one recorded, or None when a lifted one came after it."""
    try:
        lines = frames.read_bytes().decode(errors="replace").splitlines()
    except OSError:
        return None
    for line in reversed(lines):
        if lifted(line):
            return None
        if (found := exclusion(line)) is not None:
            return found
    return None


def options(argv: list[str]) -> tuple[str | None, str | None, int | None, str | None]:
    """(provider, provider session, pid, session), the flags `bind.py` takes — this holds for
    the incarnation that command binds, so it asks for the same three."""
    provider = psession = session = None
    pid = None
    args = [a for a in argv if a not in ("--until-event", "--service")]
    while args and args[0].startswith("-"):
        flag = args.pop(0)
        if flag not in ("--provider", "--provider-session", "--pid"):
            raise Refused(f"no such flag: {flag}")
        if not args or args[0].startswith("-"):
            raise Refused(f"{flag} takes a value")
        value = args.pop(0)
        if flag == "--provider":
            provider = value
        elif flag == "--provider-session":
            psession = value
        elif flag == "--pid":
            pid = pid_arg(value)
    if args:
        session = args.pop(0)
    if args:
        raise Refused("one session name at most")
    return provider, psession, pid, session


def who(ws: Path, h: harness_mod.Harness, psession: str | None, pid: int | None,
        session: str | None) -> tuple[str, str, str, str]:
    """(session, token, runtime id, provider session) — the enrollment of the process this runs
    inside."""
    psession, rid = incarnation(psession, pid, h.provider)
    name, token = own_enrolment(ws, psession, session)
    return name, token, rid, psession


def restate(ws: Path, rid: str, psession: str, config: Path) -> None:
    """Send the reading this session's last turn end sent, read again from its transcript.

    The daemon keeps readings in memory (doc 30 §6), so a restart blanks each one until the
    session's next `Stop`, and a session idling in its hold has none coming: it showed no model,
    effort or machine to the seat that would place work on it (#2665). A restart ends every hold
    through the agent's uplink, so the reopened hold is where the reading is restated.

    Found by the session id alone: the hold may run from a directory other than the one the
    session's transcripts are kept under."""
    found = harness_mod.claude_transcript_path(config, psession)
    rec = found and readout.build({"session_id": psession, "transcript_path": str(found)},
                                   restated=True)
    if rec is None:
        return
    rec["runtime_id"] = rid
    try:
        observe_post.post(rec, ws, 2.0)
    except (Exception, SystemExit) as e:  # a door it cannot name exits; the next turn end restates it
        door.record("hold", f"restating the reading failed: {door.failure(e)}", ws)


def frame_path(ws: Path, session: str) -> Path:
    """The private, per-session record of complete stream data frames."""
    name = hashlib.sha256(session.encode()).hexdigest()[:32]
    return ws / ".claude" / "steering-frames" / f"{name}.ndjson"


def holding_dir(ws: Path, session: str) -> Path:
    """Where live holds for `session` announce themselves, one file each.

    Nothing else records it: a session is reachable only while it holds, and the fact was
    readable only by watching whether a frame ever arrived (#1506). One file per process
    rather than one naming the newest: two holds may run at once, and a single file would have
    the second's exit report the first as dark.
    """
    name = hashlib.sha256(session.encode()).hexdigest()[:32]
    return ws / ".claude" / "steering-frames" / f"{name}.holding"


def holding(ws: Path, session: str) -> bool:
    """That a hold for `session` is running on this machine now.

    Each holder keeps an exclusive lock on its own file for as long as it holds, so the
    question is asked of the kernel rather than of the process table: a lock is released when
    the process ends however it ends, and it cannot be inherited by whatever is given that pid
    next. Identifying a holder by its pid — with or without a start time beside it, which `ps`
    reports only to the second — left a reused pid answering for the session that died.

    A file nothing holds is that session's own leavings, and goes. Every uncertainty answers
    False: the remedy is to arm a hold, and arming a second is harmless where believing a dark
    session is held is not.
    """
    live = False
    try:
        marks = list(holding_dir(ws, session).iterdir())
    except OSError:
        return False
    for mark in marks:
        try:
            with open(mark, "a+") as file:
                try:
                    fcntl.flock(file, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except OSError:
                    live = True       # somebody is holding it, which is the whole question
                    continue
                mark.unlink()
        except OSError:
            continue
    return live


def still_named(path: Path, file) -> bool:
    """That `path` still leads to the open file, rather than to nothing or to a newer one.

    `holding` clears what nothing holds, so between a holder's open and its lock it may take
    that lock and unlink — leaving the holder with a locked inode no name reaches, and the
    session reading as dark for the rest of its life.
    """
    try:
        return os.stat(path).st_ino == os.fstat(file.fileno()).st_ino
    except OSError:
        return False


@contextlib.contextmanager
def held(ws: Path, session: str):
    """Announce this process as a holder, for exactly as long as it holds.

    The lock is what says so, and the kernel drops it when this process ends — including when
    it is killed outright, which is the case a file left behind cannot speak for.
    """
    path = holding_dir(ws, session) / str(os.getpid())
    file = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        # Open, lock, then check the name still leads to the file we locked. `holding` clears
        # what nothing holds, and between this open and this lock it may have taken the lock
        # and unlinked — leaving us holding an inode with no name, and the session reading as
        # dark for as long as it holds. Locking again under the new name settles it.
        for _ in range(5):
            file = open(path, "a+")
            fcntl.flock(file, fcntl.LOCK_EX | fcntl.LOCK_NB)
            if still_named(path, file):
                break
            file.close()
            file = None
        else:
            yield        # never settled: rather than claim a hold nothing can read, say nothing
            return
    except OSError:
        if file is not None:
            file.close()
        yield            # a hold that cannot say so still holds; it is only unreadable
        return
    try:
        yield
    finally:
        try:
            path.unlink()
        except OSError:
            pass
        file.close()


def recording(frames: Path):
    try:
        frames.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(frames, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.chmod(frames, 0o600)
        except OSError:
            os.close(fd)
            raise
        return os.fdopen(fd, "ab", buffering=0)
    except OSError as e:
        print(f"cannot record stream frames at {frames}: {e}", file=sys.stderr)
        return None


def say_read(port: int, session: str, raw: bytes) -> None:
    """Tell the agent this say is read, so the orchestrator's spool lets it go (#1550).

    Only the reader can attest a read. The agent used to post the receipt when it yielded the
    frame, which is when the ASGI server took it, not when anything read it — so a say handed to
    a stream closing in that same instant was deleted unread. Best-effort and never fatal: a
    receipt that does not arrive leaves the say spooled, and the next hold is handed it again,
    which is the safe direction.

    The response body is read, not just the absence of an exception: the agent's own local
    check can pass while its forwarded report to the orchestrator fails (#1723), and a `200`
    with `"received": false` is that failure — silent to a caller that only watches for a raised
    exception, exactly as invisible as the timeout this same line already prints for."""
    if not raw.startswith(b"data: "):
        return
    try:
        e = json.loads(raw[6:].decode(errors="replace"))
    except ValueError:
        return
    fid = spool.owed_id(e)
    if fid is None:
        return
    body = json.dumps({"session": session, "id": fid}).encode()
    req = urllib.request.Request(f"http://127.0.0.1:{port}/steering/read", data=body,
                                 headers={"Content-Type": "application/json"})
    try:
        with door.send(req, timeout=5) as resp:
            reply = json.loads(resp.read())
    except Exception as ex:
        print(f"the read of {fid} was not reported: {type(ex).__name__}", file=sys.stderr)
        return
    if not isinstance(reply, dict) or not reply.get("received"):
        print(f"the read of {fid} was not reported: {reply}", file=sys.stderr)


def record(recorded, frames: Path, raw: bytes):
    """Append one frame; the handle to keep writing to, or None when recording has to stop.

    At the cap the record is trimmed to the newest complete frames that fit, written beside it and
    renamed over it, so a reader opening the path reads the old record or the trimmed one, and a
    reader already in the old file keeps all of it (#1135)."""
    try:
        if frames.stat().st_size + len(raw) > MAX_FRAME_RECORD_BYTES:
            keep, size = [], len(raw)
            for line in reversed(frames.read_bytes().splitlines(keepends=True)):
                if size + len(line) > MAX_FRAME_RECORD_BYTES // 2:
                    break
                keep.append(line)
                size += len(line)
            spool.write_atomic(frames, b"".join(reversed(keep)), mode=0o600)
            close_recording(recorded)
            recorded = recording(frames)
            if recorded is None:
                return None
        recorded.write(raw)
    except OSError as e:
        print(f"cannot record stream frames at {frames}: {e}", file=sys.stderr)
        return None
    return recorded


def close_recording(recorded) -> None:
    try:
        recorded.close()
    except OSError:
        pass


def hold(port: str, session: str, token: str, rid: str, frames: Path, until: bool = False,
         service: bool = False, workspace: Path | None = None,
         connected: Callable[[], None] | None = None) -> int:
    """Open the stream and yield its frames, until a 403 says no reopen would help.

    A 403 is the one answer this loop cannot retry: the token, the node or the incarnation is
    not the one the ledger admits, and every reopen would carry the same values again. A 503
    (no uplink) and a dead connection clear on their own, so they reopen — said once, when
    the failure begins, and not on every attempt while it lasts.

    `connected` is called on each open the agent admits.
    """
    url = f"http://127.0.0.1:{port}/steering/session/{session}/stream"
    quiet = False    # the standing failure has been named; naming it again every 2s is noise
    seen = last_exclusion(frames)
    recorded = recording(frames)
    try:
        while True:
            req = urllib.request.Request(url, headers={"X-Steering-Session": token,
                                                       "X-Steering-Runtime": rid,
                                                       "X-Steering-Wake": ("service" if service else
                                                                           "event" if until else "monitor"),
                                                       **(workspace_header(workspace) if workspace else {})})
            try:
                with door.send(req, timeout=HOLD_READ) as r:
                    quiet = False   # a fresh open: the next failure is worth naming again
                    for raw in r:
                        line = raw.decode(errors="replace")   # the response iterates as bytes
                        if raw.startswith(b"data: ") and recorded:
                            recorded = record(recorded, frames, raw)
                            # After the record, so a receipt is never sent for a say this
                            # machine did not keep.
                            say_read(port, session, raw)
                        if connected and line.startswith(": connected "):
                            connected()
                        if line.startswith(": keepalive") or not line.strip():
                            continue
                        wakes, found = acts(line, seen), exclusion(line)
                        seen = None if lifted(line) else seen if found is None else found
                        if until and not wakes:
                            print(line, end="", file=sys.stderr, flush=True)
                            continue
                        sys.stdout.write(line)
                        sys.stdout.flush()
                        if until:
                            return 0
            except urllib.error.HTTPError as e:
                try:
                    body = json.loads((e.read() or b"{}").decode(errors="replace"))
                except ValueError:
                    body = {}
                if not isinstance(body, dict):
                    body = {"refused": body}
                why = body.get("refused")
                if e.code == 403:
                    serves = f" The agent serves {body['serves']}." if body.get("serves") else ""
                    print(f"refused 403{f': {why}' if why else ''}.{serves} "
                          f"nothing this loop retries will change that; run /2mw2lt:connect, "
                          f"then hold this stream again")
                    return 1
                if not quiet:
                    print(f"the stream at 127.0.0.1:{port} was refused {e.code}"
                          f"{f': {why}' if why else ''}; reopening", file=sys.stderr)
                    quiet = True
            except Exception as e:
                if not quiet:
                    print(f"the stream at 127.0.0.1:{port} answered {type(e).__name__}: {e}; "
                          f"reopening", file=sys.stderr)
                    quiet = True
            time.sleep(REOPEN_AFTER)
    finally:
        if recorded:
            close_recording(recorded)


def main(argv: list[str]) -> int:
    try:
        args = current_args("hold", argv)
    except ValueError as why:
        return error("hold", str(why))
    if help_requested("hold", argv):
        print(script_help("hold", topic=argv[0] if len(argv) == 2 else None))
        return 0
    if args[:1] == ["--frame-path"]:
        if len(args) != 2:
            return error("hold", "--frame-path takes one session name")
        if args[1].startswith("-"):
            return error("hold", f"no such flag: {args[1]}")
        try:
            ws = required_workspace_root(project_dir(), timeout=2.0)
        except Refused as why:
            print(str(why), file=sys.stderr)
            return 1
        print(frame_path(ws, args[1]))
        return 0
    if "--until-event" in args and "--service" in args:
        return error("hold", "--until-event and --service are alternatives")
    try:
        provider, psession, pid, session = options(args)
        h = harness_of(provider)   # the provider named, or the one that says this is its session
    except Refused as why:
        return error("hold", str(why))
    project = project_dir()
    with contextlib.ExitStack() as arming:
        try:
            if not h.holds:
                raise Refused(f"a {h.provider} session is reached by injection; do not hold a stream")
            ws = required_workspace_root(project, timeout=2.0)
            session, token, rid, psession = who(ws, h, psession, pid, session)
            # Announced as soon as there is a session to announce, and before the agent is
            # asked anything: a `Stop` firing in that window would otherwise refuse a turn
            # that had already armed this hold, and be answered with a second one.
            arming.enter_context(held(ws, session))
            port, why = agent_says(agent_port(project), ws, session)
            if why:
                raise Refused(why)
        except (Refused, Undetermined) as why:
            print(str(why), file=sys.stderr)
            return 1
        until = "--until-event" in args
        service = "--service" in args
        said = sys.stderr if until else sys.stdout
        print(f"holding {session} at 127.0.0.1:{port} as {rid}", file=said)
        frames = frame_path(ws, session)
        print(f"frames: {frames}", file=said)
        return hold(port, session, token, rid, frames, until, service, ws,
                    (lambda: restate(ws, rid, psession, h.config_dir()))
                    if h.provider == "claude" else None)


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except KeyboardInterrupt:
        sys.exit(0)
