"""What resuming a Claude Code session whose process is gone reads from this machine (#5417): whether
any process still runs it, and where it stood, from its own transcript, as the brain skill's rescue
recipe read them by hand."""

from __future__ import annotations

import glob
import json
import os
import subprocess
import sys
from pathlib import Path

import runtime_id

# How much of a transcript's end is read for where it stood: a long session's file runs to tens of
# megabytes, and the lines wanted are its last.
TAIL_BYTES = 4 << 20


def _registry_start(pid: int) -> str | None:
    """`pid`'s start in the form Claude Code's registry records it in: clock ticks since boot on Linux,
    `ps`'s `lstart` in UTC elsewhere."""
    if sys.platform.startswith("linux"):
        return runtime_id.boot_ticks(pid)
    try:
        out = subprocess.run(["ps", "-o", "lstart=", "-p", str(pid)], capture_output=True, text=True,
                             timeout=5, env={**os.environ, "LC_ALL": "C", "TZ": "UTC0"}).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None
    return out or None


def running(psession: str, rows: str = "~/.claude*/sessions/*.json", start=_registry_start) -> bool:
    """Whether a process still runs `psession`, by this login's Claude Code registry: a row of it
    names a live pid, and where the row records the process's start, the pid's start is that one. A row a reboot
    left names a pid the OS may since have handed to anything; its start is what tells them apart."""
    return started(psession, rows, start) is not None


def started(psession: str, rows: str = "~/.claude*/sessions/*.json", start=_registry_start) -> int | None:
    """When the process that runs `psession` now started, in Unix milliseconds as its registry row
    records it, 0 for a row that records none; None when no process runs it, as `running` reads."""
    for f in glob.glob(os.path.expanduser(rows)):
        try:
            with open(f) as fh:
                row = json.load(fh)
        except (OSError, ValueError):
            continue
        if not isinstance(row, dict) or row.get("sessionId") != psession:
            continue
        pid = row.get("pid")
        if not isinstance(pid, int) or isinstance(pid, bool):
            continue
        now = start(pid)
        if now is not None and (not isinstance(row.get("procStart"), str) or row["procStart"] == now):
            at = row.get("startedAt")
            ok = isinstance(at, (int, float)) and not isinstance(at, bool) and at > 0 and at == int(at)
            return int(at) if ok else 0
    return None


def transcript(psession: str, homes: list[str]) -> tuple[str, Path] | None:
    """`(home, transcript)` for the Claude home among `homes` that holds `psession`'s transcript."""
    for home in homes:
        found = sorted(Path(home).glob(f"projects/*/{glob.escape(psession)}.jsonl"))
        if found:
            return home, found[0]
    return None


def last_seen(path: Path, tail: int = TAIL_BYTES) -> tuple[str | None, str | None]:
    """The transcript's last `cwd` and the model of its last assistant turn, read from its end."""
    with open(path, "rb") as fh:
        fh.seek(0, os.SEEK_END)
        fh.seek(max(0, fh.tell() - tail))
        lines = fh.read().splitlines()
    cwd = model = None
    for raw in reversed(lines):
        if cwd and model:
            break
        try:
            line = json.loads(raw)
        except ValueError:
            continue           # the cut first line, or one the harness was still writing
        if not isinstance(line, dict):
            continue
        if cwd is None and isinstance(line.get("cwd"), str) and line["cwd"]:
            cwd = line["cwd"]
        message = line.get("message")
        if model is None and line.get("type") == "assistant" and isinstance(message, dict) \
                and isinstance(message.get("model"), str) and not message["model"].startswith("<"):
            model = message["model"]
    return cwd, model
