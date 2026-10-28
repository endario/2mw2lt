"""A zcode session's binding names the process to serve (#6257).

A zcode session cannot name itself: no variable it exports reaches the processes it runs
(measured 2026-10-13, spikes/zcode-glue — the session id reaches hooks and stops there), and no
MCP template carries one. What the session does have is a process: every hook, every tool child
and the plugin's MCP server hang off the same zcode process, so a binding keyed by that pid —
published by the hooks, which know the session id, resolved by walking the reader's own
ancestor chain — names the session without a guess and without trusting a cwd that two
sessions can share.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

import atomic_file  # noqa: E402  # steering/ is on the path of every importer

# The claim cadence (holder.FRESH_S): a binding older than this names a process whose session
# had no hook fire for that long — gone, or close enough that guessing by it is wrong.
BINDING_FRESH = 180
_SAFE_PID = re.compile(r"^[0-9]+$")


class Refused(Exception):
    """Why no binding was resolved. The caller reports it; nothing is guessed."""


def binding_path(ws: Path, pid) -> Path:
    """The binding file for a zcode process, beside the holder claims — one holder location."""
    if not isinstance(pid, int) or not _SAFE_PID.match(str(pid)):
        raise ValueError(f"unsafe pid: {pid!r}")
    return ws / ".claude" / "steering-holders" / f"{pid}.binding.json"


def _write(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    atomic_file.write_atomic(path, json.dumps(payload), 0o600)


def publish(ws: Path, psession: str, cwd: str, pid: int | None = None, at: float | None = None,
            reap_at: float | None = None, chain=None, proof: dict | None = None) -> Path:
    """Write this session's binding, reaping what has gone stale.

    `pid` is the zcode process; the default finds it by walking this process's ancestors — the
    publisher is a hook, a direct child of the session it serves. The reaper keeps the directory
    to live bindings: a stale file is a session that is gone, and leaving it would have the next
    pid the kernel reuses resolve to it. A restricted SessionStart passes `proof` to replace its
    model/source/proof_at; later events pass None and preserve those fields while refreshing `at`.
    """
    pid = pid if pid is not None else _zcode_pid(chain)
    path = binding_path(ws, pid)
    now = at if at is not None else time.time()
    record = {"psession": psession, "cwd": str(cwd), "pid": pid, "at": now}
    if proof is None:
        try:
            prior = json.loads(path.read_text())
        except (OSError, ValueError, AttributeError):
            prior = {}
        if not isinstance(prior, dict):
            prior = {}
        for key in ("model", "source", "proof_at"):
            if key in prior:
                record[key] = prior[key]
    else:
        for key in ("model", "source"):
            value = proof.get(key)
            if isinstance(value, str) and value.strip():
                record[key] = value.strip()
        proof_at = proof.get("proof_at")
        record["proof_at"] = proof_at if isinstance(proof_at, (int, float)) and not isinstance(proof_at, bool) else now
    _write(path, record)
    cutoff = (reap_at if reap_at is not None else now) - BINDING_FRESH
    for old in path.parent.glob("*.binding.json"):
        if old == path:
            continue
        try:
            at = json.loads(old.read_text()).get("at")
        except (OSError, ValueError, AttributeError):
            at = None
        # the payload's `at` is the truth, not the file's mtime: a binding is written when its
        # hook fires, and an unreadable one serves nobody.
        if not isinstance(at, (int, float)) or at < cutoff:
            try:
                old.unlink()
            except OSError:
                pass
    return path


def resolve(ws: Path, cwd: str | None = None, at: float | None = None, entry: str | None = None,
            chain=None) -> dict:
    """The binding of the zcode process this process hangs off, or `Refused` saying why.

    Fresh and naming this reader's own workspace: the pair bounds pid reuse to a window in
    which the reused pid must also name the same cwd, and it keeps two sessions in one
    workspace from answering for each other. `entry` is the zcode entry path to match in an
    ancestor's cmdline (the `ZCODE_APP_CLI_ENTRY` the runtime exports to hooks and the plugin's
    server); `chain` is the ancestor walk, injectable for tests.
    """
    pid = _zcode_pid(chain, entry)
    path = binding_path(ws, pid)
    try:
        binding = json.loads(path.read_text())
    except (OSError, ValueError):
        raise Refused(f"the binding for zcode pid {pid} is unreadable; run the session's hooks "
                      f"(a SessionStart publishes it)") from None
    if not isinstance(binding, dict) or not isinstance(binding.get("psession"), str):
        raise Refused(f"the binding for zcode pid {pid} is unreadable; run the session's hooks")
    named = binding.get("cwd")
    mine = os.path.realpath(str(cwd if cwd is not None else os.getcwd()))
    if not isinstance(named, str) or os.path.realpath(named) != mine:
        raise Refused(f"the binding for zcode pid {pid} names another workspace ({named!r}, "
                      f"this reader is in {mine!r}); a reused pid, or another session's file")
    then = binding.get("at")
    if not isinstance(then, (int, float)) or (at if at is not None else time.time()) - then > BINDING_FRESH:
        raise Refused(f"the binding for zcode pid {pid} is stale; its session had no hook fire "
                      f"for over {BINDING_FRESH} s")
    return binding


def _zcode_pid(chain=None, entry: str | None = None) -> int:
    """The zcode process at the root of this process's ancestor chain.

    The walk is what the host offers — `/proc` on Linux, `ps` on macOS — behind one seam so the
    tests drive a fake chain. An ancestor's cmdline names the zcode entry (`entry`, or the
    `ZCODE_APP_CLI_ENTRY` the runtime exports); the first such ancestor from this process
    upward is the session's own zcode process.
    """
    want = entry or os.environ.get("ZCODE_APP_CLI_ENTRY")
    for pid, cmdline in (chain() if chain is not None else _walk()):
        # The runtime retitles itself `zcode-cli`, and it is the parent of every hook, the
        # plugin's MCP server and every tool child; its launcher's cmdline names the entry by the
        # symlink it was started through, never the real path the env carries (measured live,
        # M4 2026-10-14), so the title is what is matched first.
        argv0 = cmdline.split()[0] if cmdline.split() else ""
        if os.path.basename(argv0) == "zcode-cli" or (want is not None and want in cmdline):
            return pid
    raise Refused("no zcode process in this process's ancestry; a zcode session's hook, server "
                  "or tool child resolves, nothing else does")


def _walk():
    """(pid, cmdline) from this process to init, on the host's own primitives."""
    if sys.platform == "darwin":
        pid = os.getpid()
        while pid > 1:
            asked = subprocess.run(["ps", "-o", "ppid=,command=", "-p", str(pid)],
                                   capture_output=True, text=True, timeout=2)
            line = asked.stdout.strip()
            if not line:
                return
            ppid_s, _, command = line.partition(" ")
            yield pid, command
            try:
                pid = int(ppid_s)
            except ValueError:
                return
        return
    pid = os.getpid()
    while pid > 1:
        try:
            with open(f"/proc/{pid}/cmdline", "rb") as f:
                cmdline = f.read().replace(b"\0", b" ").decode("utf-8", "replace").strip()
            with open(f"/proc/{pid}/stat", "rb") as f:
                stat = f.read().decode("utf-8", "replace")
        except OSError:
            return
        yield pid, cmdline
        # the ppid is the field after the parenthesised comm, itself the first close paren
        tail = stat.rpartition(")")[2].split()
        try:
            pid = int(tail[1])
        except (IndexError, ValueError):
            return
