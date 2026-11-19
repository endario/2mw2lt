#!/usr/bin/env python3
"""`load.py run [--cpus N] [--seconds S] -- <command>` / `load.py reap <pidfile>`: synthetic load a
session cannot leave behind.

A worker reproducing a load-failing test needs CPU hogs beside it. Twice they outlived the turn: 16
`yes` on a Mac mini, and an orphaned `load.sh` group that ignored TERM, each overloading a host that
other sessions share (doc 08, "Synthetic load is bounded"). This is the one way to make load, and
it holds the rule's four parts so a session need not:

- **bounded**: the hogs and the command both stop at `--seconds` (at most 600; `timeout`'s exit 124),
  and the hogs end themselves there even if this process is killed outright. The bound is built in
  because stock macOS has no `timeout` to wrap them in;
- **recorded**: every hog's pid is written to the pidfile before it burns and printed;
- **killed by pid**, never by pattern: on any exit, and on TERM, INT or HUP, each recorded pid gets TERM,
  then KILL where it ignored it; the command is a process group of this helper's making, signalled
  as a group so what it spawned goes with it;
- **verified gone**: the exit says so, and is 70 naming the survivors when one is not.

`reap <pidfile>` does the same for a run that died hard (SIGKILL, a crashed host): it signals a
recorded pid only while its command line still names the worker, so a pid the system has reused is
left alone.
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from verb_help import error, help_requested, script_help  # noqa: E402

MARKER = "2mw2lt-load-worker"
MAX_SECONDS = 600
DEFAULT_SECONDS = 120
GRACE = 3.0  # how long a TERM is given before KILL
TIMED_OUT = 124  # `timeout`'s own exit
SURVIVORS = 70
UNCHECKABLE = 69  # `reap` could not tell whether a recorded pid is still a worker

# A worker burns one core until its deadline, then ends itself; TERM's default action stops it sooner.
BUSY = ("import sys,time\n"
        "end=time.monotonic()+float(sys.argv[1])\n"
        "while time.monotonic()<end:\n"
        "    for _ in range(200000): pass\n")


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def is_worker(pid: int):
    """Whether pid is still a load worker of ours: an identity check on a recorded pid, not a search.
    None is a `ps` that cannot be run here (a cage that denies exec), which says nothing either way."""
    try:
        out = subprocess.run(["ps", "-o", "command=", "-p", str(pid)], capture_output=True, text=True).stdout
    except OSError:
        return None
    return MARKER in out


def gone(pid: int, group: bool, kids: dict) -> bool:
    """Whether pid, or its whole process group, no longer runs. A child of ours is collected first,
    since an unreaped exit reads as alive (and, for a group, as EPERM on macOS: doc 08)."""
    kid = kids.get(pid)
    exited = kid.poll() is not None if kid is not None else None
    if not group:
        return exited if kid is not None else not alive(pid)
    try:
        os.killpg(pid, 0)
    except ProcessLookupError:
        return True
    except PermissionError:
        return False
    return False


def wait_gone(pids, within: float, group: bool, kids: dict) -> list:
    """The pids still running after up to `within` seconds."""
    end = time.monotonic() + within
    while True:
        left = [pid for pid in pids if not gone(pid, group, kids)]
        if not left or time.monotonic() >= end:
            return left
        time.sleep(0.05)


def stop(pids, group: bool = False, children=()) -> list:
    """TERM each pid (its whole group when `group`), KILL what ignored it; the pids still running.
    `children` are this process's own, whose exit it collects."""
    kids = {p.pid: p for p in children}

    def send(sig, to):
        for pid in to:
            try:
                (os.killpg if group else os.kill)(pid, sig)
            except (ProcessLookupError, PermissionError):
                pass
    send(signal.SIGTERM, pids)
    left = wait_gone(pids, GRACE, group, kids)
    if left:
        send(signal.SIGKILL, left)
        left = wait_gone(left, GRACE, group, kids)
    return left


def pidfile_default() -> Path:
    return Path(tempfile.gettempdir()) / "2mw2lt-load" / f"{os.getpid()}.json"


def run(args) -> int:
    cap = os.cpu_count() or 1
    cpus = args.cpus if args.cpus is not None else max(1, cap // 2)
    if not 1 <= cpus <= cap:
        print(f"load: --cpus {cpus} is outside 1..{cap}, this machine's cores", file=sys.stderr)
        return 2
    if not 1 <= args.seconds <= MAX_SECONDS:
        print(f"load: --seconds {args.seconds} is outside 1..{MAX_SECONDS}", file=sys.stderr)
        return 2
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command:
        print("load: no command; usage: load.py run [--cpus N] [--seconds S] -- <command>", file=sys.stderr)
        return 2

    pidfile = Path(args.pidfile) if args.pidfile else pidfile_default()
    try:  # a pidfile that cannot be written is refused before anything burns
        pidfile.parent.mkdir(parents=True, exist_ok=True)
        pidfile.write_text(json.dumps({"owner": os.getpid(), "workers": []}))
    except OSError as e:
        print(f"load: the pidfile {pidfile} cannot be written: {e}", file=sys.stderr)
        return 2
    workers: list[subprocess.Popen] = []
    job = None
    code = SURVIVORS

    def interrupted(signum, _frame):
        raise SystemExit(128 + signum)

    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(sig, interrupted)
    try:
        for _ in range(cpus):
            workers.append(subprocess.Popen([sys.executable, "-I", "-c", BUSY, str(args.seconds), MARKER],
                                            stdin=subprocess.DEVNULL, start_new_session=True))
            pidfile.write_text(json.dumps({"owner": os.getpid(), "workers": [w.pid for w in workers]}))
        print(f"load: {cpus} workers for at most {args.seconds}s, pids {','.join(str(w.pid) for w in workers)}; "
              f"pidfile {pidfile}", file=sys.stderr, flush=True)
        job = subprocess.Popen(command, start_new_session=True)
        pidfile.write_text(json.dumps({"owner": os.getpid(), "workers": [w.pid for w in workers], "job": job.pid}))
        try:
            code = job.wait(timeout=args.seconds)
        except subprocess.TimeoutExpired:
            print(f"load: the command is still running at {args.seconds}s and is stopped", file=sys.stderr)
            code = TIMED_OUT
        except OSError:
            pass
    except SystemExit as e:
        code = e.code if isinstance(e.code, int) else 1
    except OSError as e:
        print(f"load: {e}", file=sys.stderr)
        code = 127
    finally:
        for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
            signal.signal(sig, signal.SIG_IGN)  # teardown is not interrupted half way
        left = []
        if job is not None:
            left += stop([job.pid], group=True, children=[job])
        left += stop([w.pid for w in workers], children=workers)
        if left:
            print(f"load: NOT STOPPED, still running: pids {','.join(str(p) for p in left)}; "
                  f"`load.py reap {pidfile}` or kill them by pid", file=sys.stderr)
            code = SURVIVORS
        else:
            pidfile.unlink(missing_ok=True)
            print(f"load: {len(workers)} workers and the command's group verified gone", file=sys.stderr)
    return code


def reap(args) -> int:
    path = Path(args.pidfile)
    try:
        record = json.loads(path.read_text())
        pids = [int(p) for p in record["workers"]]
    except (OSError, ValueError, KeyError, TypeError) as e:
        print(f"load: {path} is not a load pidfile: {e}", file=sys.stderr)
        return 2
    running = [p for p in pids if alive(p)]
    verdicts = {p: is_worker(p) for p in running}
    if any(v is None for v in verdicts.values()):
        print("load: `ps` cannot be run here, so a recorded pid cannot be checked to still be a load worker; "
              "nothing was signalled", file=sys.stderr)
        return UNCHECKABLE
    mine = [p for p in running if verdicts[p]]
    for p in running:
        if p not in mine:
            print(f"load: pid {p} is no longer a load worker and is left alone", file=sys.stderr)
    left = stop(mine)
    if left:
        print(f"load: NOT STOPPED, still running: pids {','.join(str(p) for p in left)}", file=sys.stderr)
        return SURVIVORS
    path.unlink(missing_ok=True)
    print(f"load: {len(mine)} workers stopped, none left", file=sys.stderr)
    job = record.get("job")
    if isinstance(job, int) and alive(job):
        # Not signalled: the command carries no marker, so a pid that is running now may be another
        # process the system has since given the number.
        print(f"load: the run's command had process group {job}; if it is still yours and running, "
              f"stop it by pid", file=sys.stderr)
    return 0


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if help_requested("load", argv):
        print(script_help("load", topic=argv[0] if len(argv) == 2 else None))
        return 0
    if argv[0] == "reap" and len(argv) == 2:
        return reap(argparse.Namespace(pidfile=argv[1]))
    if argv[0] != "run":
        return error("load", "load takes `run` or `reap`")
    ap = argparse.ArgumentParser(prog="load.py run", add_help=False)
    ap.add_argument("--cpus", type=int)
    ap.add_argument("--seconds", type=int, default=DEFAULT_SECONDS)
    ap.add_argument("--pidfile")
    ap.add_argument("command", nargs=argparse.REMAINDER)
    try:
        args = ap.parse_args(argv[1:])
    except SystemExit:
        return error("load", "run takes --cpus, --seconds and --pidfile before `-- <command>`")
    return run(args)


if __name__ == "__main__":
    sys.exit(main())
