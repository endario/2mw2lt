"""Bounded local process identity and writing-descriptor probes."""
from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Callable

import runtime_id


def live(pid, since) -> bool:
    """That `pid` names the same process the record was written for. A pid alone does not: the
    kernel reuses them, which is how a reach once stood for a session that had ended (#682)."""
    return (isinstance(pid, int) and not isinstance(pid, bool)
            and since is not None and runtime_id.started_at(pid) == since)


class Undetermined(RuntimeError):
    """The local process probe could not answer; absence has not been established."""


# lsof walks every process's descriptors: on one host a lookup overran five seconds and the same one
# later took under one (#2356), as `codex_proxy.LSOF_TIMEOUT` found for its own (#2336).
LSOF_TIMEOUT = 30


def writers(path: Path, timeout: float = LSOF_TIMEOUT) -> list[int]:
    """Pids holding `path` open for writing. Raises `Undetermined` when lsof cannot say, so a
    caller never reads a lookup that failed as a worker that is gone."""
    try:
        out = subprocess.run(["lsof", "-F", "pa", "--", str(path)],
                             capture_output=True, text=True, timeout=timeout).stdout
    except subprocess.TimeoutExpired as e:
        raise Undetermined(f"lsof did not answer about {path} within {timeout:g} s") from e
    except (OSError, subprocess.SubprocessError) as e:
        raise Undetermined(f"lsof could not be run: {e}") from e
    found: list[int] = []
    pid: int | None = None
    for line in out.splitlines():
        tag, val = line[:1], line[1:]
        if tag == "p":
            try:
                pid = int(val)
            except ValueError:
                pid = None
        elif tag == "a" and pid is not None and ("w" in val or "u" in val):
            if pid not in found:
                found.append(pid)
    return found


def pid_or_none(pid_of: Callable[[str], int | None], worker: str) -> int | None:
    """`pid_of` for a reply that only reports the pid, where no answer is not worth failing."""
    try:
        return pid_of(worker)
    except Undetermined:
        return None
