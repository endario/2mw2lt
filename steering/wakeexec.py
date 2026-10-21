"""The tmux pane a session runs in, found and asked (doc 118 §3.1, doc 120 §5).

The wake and control routes this module once executed retired with the Python agent (#7075); the
Go machine executes them now (machine/internal/wake, machine/internal/control). What the enroll
clients still read lives here: which pane a process runs in, and whether a client is attached to
it — a session's connect hands its pane over at enrolment (enroll/connect.py), and the Stop hook
states both on its reading (enroll/readout.py).
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import runtime_id


class Refused(Exception):
    """A precondition that did not hold. Every one is a disposition, not an error."""


TMUX_BIN = ("/opt/homebrew/bin/tmux", "/usr/local/bin/tmux", "/usr/bin/tmux")
SOCKET = "2mw2lt-launch"          # tmux -L: the platform's own server, apart from the login's


def tmux_bin() -> str | None:
    import shutil
    return shutil.which("tmux") or next((b for b in TMUX_BIN if Path(b).exists()), None)


def pane_of(pid: int, *, run=subprocess.run, socket: str | None = None) -> tuple[str, str] | None:
    """(socket, pane) of the pane whose process is `pid` or an ancestor of it, or None. For a
    harness that does not hand `TMUX` down to what it runs. Without `socket`, the login's default
    server and then the platform's own (`SOCKET`), where every session the agent
    launches runs (#2267); a server on any other socket is not searched."""
    b = tmux_bin()
    if not b:
        return None
    chain = set(runtime_id._ancestors(pid))
    for server in ([["-S", socket]] if socket else [[], ["-L", SOCKET]]):
        try:
            r = run([b, *server, "list-panes", "-a", "-F", "#{pane_pid} #{pane_id} #{socket_path}"],
                    capture_output=True, text=True, timeout=5)
        except Exception:
            continue
        if r.returncode != 0:
            continue  # no server on this socket
        for line in r.stdout.splitlines():
            parts = line.split(" ", 2)
            if len(parts) == 3 and parts[0].isdigit() and int(parts[0]) in chain:
                return parts[2], parts[1]
    return None


class Tmux:
    """One pane, addressed by its socket and id. Every call names the socket: the agent runs
    under launchd with none of the terminal's `TMUX`, and a pane id is only unique within its
    server."""

    def __init__(self, socket: str, pane: str, run=subprocess.run):
        self.socket, self.pane_id, self._run = socket, pane, run
        self.bin = tmux_bin()

    def _tmux(self, *args: str, input: str | None = None) -> str:
        if not self.bin:
            raise Refused("no tmux on this machine")
        try:
            r = self._run([self.bin, "-S", self.socket, *args], input=input,
                          capture_output=True, text=True, timeout=10)
        except subprocess.TimeoutExpired:
            raise Refused(f"`tmux {args[0]}` did not return") from None
        if r.returncode != 0:
            raise Refused(f"`tmux {args[0]}` exited {r.returncode}: {r.stderr.strip() or 'no detail'}")
        return r.stdout


def pane_state(pid: int, *, run=subprocess.run, socket: str | None = None) -> tuple[tuple[str, str] | None, str | None]:
    """The (socket, pane) `pid` runs in and whether a client is attached to its tmux session —
    `attached`, `detached`, or None when it is in no pane or tmux does not say. A session a person
    watches is the one with a window on it, which is all "headed" is."""
    found = pane_of(pid, run=run, socket=socket)
    if found is None:
        return None, None
    try:
        out = Tmux(*found, run=run)._tmux("display-message", "-p", "-t", found[1], "#{session_attached}")
    except Refused:
        return found, None
    n = out.strip()
    return found, None if not n.isdigit() else "attached" if int(n) > 0 else "detached"
