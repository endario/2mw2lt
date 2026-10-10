"""Local worker records and provider-specific process/descriptor proof."""
from __future__ import annotations

import json
from pathlib import Path
import os

import runtime_id
from process_probe import live, writers


def record(directory: Path, worker: str) -> Path:
    return directory / f"{worker}.pid"


def read(directory: Path, worker: str) -> dict | None:
    try:
        rec = json.loads(record(directory, worker).read_text())
    except (OSError, ValueError):
        return None
    return rec if isinstance(rec, dict) else None


def shape(rel: Path) -> tuple[bool, str | None]:
    """A worker's transcript is one file directly under the root, named for the worker.

    `.jsonl` because `enroll.identify_transcript` admits no other suffix, which is what doc 55
    meant by the file being a real one. Named `.ndjson`, an enrolment naming it was refused and
    a worker could enrol only unobserved — no transcript, and so no reading and no board row
    (#949).
    """
    if len(rel.parts) != 1 or rel.suffix != ".jsonl":
        return False, None
    return True, rel.stem


def workspace_of(fd: int, rel: Path, workspace: str, provider: str) -> str | None:
    """Why this transcript does not belong to `workspace`, read from the descriptor the door
    already holds — so the answer is about the file that was opened, not one looked up again."""
    # `pread`, not a dup: a dup shares the file offset, so reading through one moves the
    # door's own descriptor and the next read of it returns nothing.
    head = os.pread(fd, 65536, 0)
    try:
        rec = json.loads(head.split(b"\n", 1)[0])
    except ValueError:
        return f"a {provider} worker's transcript does not open with a record naming its workspace"
    named = rec.get("workspace") if isinstance(rec, dict) else None
    if not isinstance(named, str) or not named:
        return f"a {provider} worker's transcript names no workspace"
    try:
        same = Path(named).resolve() == Path(workspace).resolve()
    except ValueError:
        return f"a {provider} worker's transcript names no workspace"
    if not same:
        return f"that worker belongs to {named}, not {workspace}"
    return None


GOOSE_CONFIG = ".local/share/goose"
OPENCODE_CONFIG = ".local/share/opencode"
ROOT = "steering-workers"


def goose_workers_dir() -> Path:
    return Path.home() / GOOSE_CONFIG / ROOT


def goose_transcript(worker: str) -> Path:
    return goose_workers_dir() / f"{worker}.jsonl"


def goose_path_of(worker: str) -> Path | None:
    p = goose_transcript(worker)
    return p if p.exists() else None


def goose_launch_record(worker: str) -> Path:
    return record(goose_workers_dir(), worker)


def goose_declared_pid(worker: str) -> int | None:
    """The process this worker was launched as, or None when nothing in the record still holds.

    Three things have to: the named process is the one named, the manager that wrote the record
    is still the manager, and the named process is that manager's own child. The last is what a
    forged record cannot satisfy — `goose acp`'s descendants have the worker or init for a
    parent, and a process cannot choose who forked it.
    """
    rec = read(goose_workers_dir(), worker)
    if rec is None:
        return None
    pid, by = rec.get("pid"), rec.get("by")
    if not _live(pid, rec.get("since")) or not _live(by, rec.get("by_since")):
        return None
    return pid if runtime_id.parent_of(pid) == by else None


def goose_pid_of(worker: str) -> int | None:
    """The Goose process serving this worker, or None when nothing is.

    `pass_fds` clears close-on-exec, so every descendant of `goose acp` inherits the transcript's
    descriptor: a detached `git fsmonitor--daemon` kept it and outlived the turn, and
    sole-writership held only while the worker was idle (#870).

    No fallback to that rule when the record names nobody. It would name whichever process holds
    the transcript, and once the worker exits that is the surviving descendant.
    """
    named = goose_declared_pid(worker)
    return named if named is not None and named in writers(goose_transcript(worker)) else None


def goose_loaded(worker: str) -> bool:
    return goose_pid_of(worker) is not None


def goose_workspace_of(fd: int, rel: Path, workspace: str) -> str | None:
    return workspace_of(fd, rel, workspace, "goose")


def opencode_workers_dir() -> Path:
    return Path.home() / OPENCODE_CONFIG / ROOT


def opencode_transcript(worker: str) -> Path:
    return opencode_workers_dir() / f"{worker}.jsonl"


def opencode_declared_pid(worker: str) -> int | None:
    """The server this worker was started as, or None unless the whole chain still holds: the
    server is the recorded leash's child and the leash the recorded agent's (doc 82 §4)."""
    rec = read(opencode_workers_dir(), worker)
    if rec is None:
        return None
    pid, lsh, by = rec.get("pid"), rec.get("leash"), rec.get("by")
    if not (live(pid, rec.get("since")) and live(lsh, rec.get("leash_since"))
            and live(by, rec.get("by_since"))):
        return None
    return pid if runtime_id.parent_of(pid) == lsh and runtime_id.parent_of(lsh) == by else None


def opencode_pid_of(worker: str) -> int | None:
    """The server serving this worker: the chain holds and it holds the transcript. A shell
    command inherits the descriptor too, which is why the file alone names nobody (#870)."""
    named = opencode_declared_pid(worker)
    if named is None:
        return None
    return named if any(_descends(w, named) for w in writers(opencode_transcript(worker))) else None


def _descends(pid: int, ancestor: int, hops: int = 4) -> bool:
    """`pid` is `ancestor` or a few forks below it: on Linux the server named is the cage's outer
    bubblewrap, which closes the transcript and leaves it to the server it forked (2026-10-10)."""
    for _ in range(hops + 1):
        if pid == ancestor:
            return True
        parent = runtime_id.parent_of(pid)
        if parent is None or parent <= 1:
            return False
        pid = parent
    return False


def opencode_loaded(worker: str) -> bool:
    return opencode_pid_of(worker) is not None


def opencode_path_of(worker: str) -> Path | None:
    p = opencode_transcript(worker)
    return p if p.exists() else None


def opencode_workspace_of(fd: int, rel: Path, workspace: str) -> str | None:
    return workspace_of(fd, rel, workspace, "opencode")


_live = live


# The manager's actual Goose workers; execution and discovery share this mapping.
RUNNING: dict[str, object] = {}


def serving(worker: str) -> object | None:
    """By worker id, or — retire's own key, once a launch has enrolled it (#2151) — by the
    session name the worker-launched fact reports instead."""
    w = RUNNING.get(worker)
    if w is not None:
        return w
    return next((w for w in RUNNING.values() if w.session == worker), None)


def goose_reading(worker: str) -> dict | None:
    """What a worker this process is running is running, for `Harness.reading` (doc 58 §4).

    Only a worker in `RUNNING` can answer: the handshake is held on the live session and there
    is nothing on disk to read it from, so a worker of another process answers nothing rather
    than a reading built out of this machine's environment.
    """
    import vitals
    w = serving(worker)
    return vitals.stated("goose", w.reading()) if w is not None else None
