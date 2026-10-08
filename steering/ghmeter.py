"""Local gh invocation evidence for sessions the agent launches; no arguments or credentials."""
from __future__ import annotations

import contextlib
import json
import os
import shlex
import sys
from pathlib import Path

import atomic_file


def root() -> Path:
    """The meter's home under the agent's state root (`agentjob.root`), read here directly so a
    meter stays a leaf both the agent's launchers and the enroll client may import; the path is
    frozen by the data already recorded under it."""
    return Path.home() / ".local" / "share" / "2mw2lt-agent" / "gh-calls"


def directory(identity: str) -> Path:
    return root() / identity


def prepare(identity: str, workspace: Path) -> Path | None:
    try:
        leaf = directory(identity).resolve()
        if all((leaf / name).is_file() for name in ("gh", "calls", "metadata.json")):
            return leaf
        leaf.mkdir(parents=True, exist_ok=True, mode=0o700)
        calls = leaf / "calls"
        fd = os.open(calls, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        os.close(fd)
        with contextlib.suppress(FileExistsError):
            atomic_file.write_atomic(leaf / "metadata.json",
                               json.dumps({"launch": identity, "workspace": str(workspace), "session": None}),
                               mode=0o600, overwrite=False)
        # An auth wrapper execs gh again in the same pid; a nested command has a new pid.
        script = ("#!/bin/sh\n"
                  f"here={shlex.quote(str(leaf))}\n"
                  'if [ "${STEERING_GH_METER_PID:-}" != "$$" ]; then\n'
                  f"  printf '%s\\n' \"$(/bin/date -u +%Y-%m-%dT%H:%M:%SZ)\" >> {shlex.quote(str(calls))} "
                  "|| printf '%s\\n' 'gh meter: invocation was not recorded' >&2\nfi\n"
                  "STEERING_GH_METER_PID=$$\nexport STEERING_GH_METER_PID\nremaining=$PATH\n"
                  "while :; do\n"
                  "  entry=${remaining%%:*}\n  next=${entry:-.}/gh\n"
                  '  if [ -f "$next" ] && [ -x "$next" ] && ! [ "$next" -ef "$here/gh" ]; then\n'
                  '    exec "$next" "$@"\n  fi\n'
                  "  case $remaining in *:*) remaining=${remaining#*:} ;; *) break ;; esac\n"
                  "done\nprintf '%s\\n' 'gh: command not found' >&2\nexit 127\n")
        atomic_file.write_atomic(leaf / "gh", script, mode=0o700)
        return leaf
    except OSError as e:
        print(f"gh meter: prepare {identity} failed: {e}", file=sys.stderr, flush=True)
        return None


def environment(env: dict[str, str], identity: str, workspace: Path) -> dict[str, str]:
    return on_path(env, prepare(identity, workspace))


def on_path(env: dict[str, str], leaf: Path | None) -> dict[str, str]:
    if leaf is None:
        return dict(env)
    meter_root = root().resolve()
    path = [part for part in env.get("PATH", os.defpath).split(os.pathsep)
            if Path(part).resolve().parent != meter_root]
    return {**env, "PATH": os.pathsep.join([str(leaf), *path])}


def shadowed(path: str) -> str | None:
    """Why this session's own PATH does not count its gh calls, or None when it does or the session
    is not metered. The launch puts the metering gh first, but a shell profile that runs afterwards
    can put another before it, which then execs the real gh without passing the meter (#4515)."""
    meter_root = root().resolve()
    entries = [Path(part) for part in path.split(os.pathsep) if part]
    leaf = next((e for e in entries if e.resolve().parent == meter_root), None)
    if leaf is None:
        return None
    first = next((e / "gh" for e in entries if (e / "gh").is_file() and os.access(e / "gh", os.X_OK)), None)
    if first is None or first.resolve() == (leaf / "gh").resolve():
        return None
    return (f"gh meter: {first} comes before this session's metering gh ({leaf / 'gh'}), so its GitHub "
            f"calls are not counted; put {leaf} first on PATH in the shell's startup")


def bind(identity: str, session: str) -> None:
    try:
        path = directory(identity) / "metadata.json"
        if not path.is_file():
            return
        metadata = json.loads(path.read_text())
        metadata["session"] = session
        atomic_file.write_atomic(path, json.dumps(metadata), mode=0o600)
    except (OSError, ValueError, TypeError) as e:
        print(f"gh meter: bind {identity} failed: {e}", file=sys.stderr, flush=True)
