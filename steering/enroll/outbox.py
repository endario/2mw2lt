"""The status-verb outbox (#1798): a verb is written down before its first send, and stays until
the door has settled it, so a sender that dies or loses its answer leaves something to finish.

One directory per workspace under one `flock`, which is the order: a verb is numbered as it is
admitted, and nothing later of its session is sent while an earlier one is pending. A file is
published whole by `os.link`, which refuses an existing name, so no reader sees half of one and
no id is ever overwritten.
"""
from __future__ import annotations

import fcntl
import json
import os
import uuid
from contextlib import contextmanager
from pathlib import Path

import spool

LIMIT = 4096  # the remote door's own cap on a line (`remote.py`); past it no resend can succeed


class Conflict(Exception):
    """A pending write already holds this id with a different line."""


def outbox_dir(workspace: Path) -> Path:
    return workspace / ".claude" / "steering-verbs"


def _fsync_dir(d: Path) -> None:
    fd = os.open(d, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _ensure(d: Path) -> None:
    """Create `d` and any missing parents, syncing each parent whose entry was new."""
    missing = []
    p = d
    while not p.exists():
        missing.append(p)
        p = p.parent
    for m in reversed(missing):
        try:
            m.mkdir(mode=0o700)
        except FileExistsError:
            continue
        _fsync_dir(m.parent)


@contextmanager
def locked(workspace: Path, block: bool = True):
    """The outbox, held. Yields False instead when `block` is off and someone else holds it."""
    d = outbox_dir(workspace)
    _ensure(d)
    fd = os.open(d / ".lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | (0 if block else fcntl.LOCK_NB))
        except BlockingIOError:
            yield False
            return
        yield True
    finally:
        os.close(fd)


def pending(workspace: Path, session: str | None = None) -> list[tuple[Path, dict]]:
    """Unsettled writes in admission order, of one session or all. Call under the lock."""
    d = outbox_dir(workspace)
    out = []
    for p in d.glob("*.json") if d.exists() else []:
        try:
            o = json.loads(p.read_text())
        except (OSError, ValueError):
            continue
        if session is None or o.get("session") == session:
            out.append((p, o))
    return sorted(out, key=lambda po: po[1].get("seq", 0))


def _next_seq(d: Path) -> int:
    counter = d / ".seq"
    try:
        n = int(counter.read_text() or 0) + 1
    except FileNotFoundError:
        n = 1
    spool.write_atomic(counter, str(n))
    return n


def admit(workspace: Path, line: str, oid: str, session: str, door: str) -> Path:
    """Write one intent durably, under the lock. A pending intent under `oid` is returned as it is
    when its line is this one, and refused when it is not."""
    d = outbox_dir(workspace)
    for p, o in pending(workspace):
        if o.get("id") == oid:
            if o.get("line") != line:
                raise Conflict(f"id {oid} is pending with another line: {o.get('line')!r}")
            return p
    seq = _next_seq(d)
    final = d / f"{seq:012d}-{oid}.json"
    tmp = d / f".{oid}.{uuid.uuid4().hex}.tmp"
    with open(tmp, "w") as f:
        json.dump({"seq": seq, "line": line, "id": oid, "session": session, "door": door},
                  f, sort_keys=True)
        f.flush(); os.fsync(f.fileno())
    try:
        os.link(tmp, final)
    finally:
        tmp.unlink()
    _fsync_dir(d)
    return final


def settle(path: Path) -> None:
    """The door answered this write: it is done, whatever the answer was."""
    try:
        path.unlink()
    except FileNotFoundError:
        return
    _fsync_dir(path.parent)


def reject(path: Path, why: str) -> Path:
    """No resend will deliver this write: set it aside in `rejected/` with its reason, so it stops
    holding its session's later writes back and is still there to be read."""
    q = path.parent / "rejected"
    _ensure(q)
    dest = q / path.name
    os.replace(path, dest)
    dest.with_suffix(".why").write_text(why + "\n")
    _fsync_dir(q); _fsync_dir(path.parent)
    return dest


def sweep(workspace: Path) -> int:
    """Remove temporary files a death during `admit` left behind. Call under the lock."""
    d = outbox_dir(workspace)
    n = 0
    for p in d.glob(".*.tmp") if d.exists() else []:
        p.unlink(missing_ok=True); n += 1
    return n
