#!/usr/bin/env python3
"""The sender: a Claude Code command hook (doc 16 §11.2).

Configured for every event doc 15 §3 names, e.g.
  {"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "python3 <repo>/steering/enroll/observe.py", "timeout": 5}]}]}}
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parents[1]))
import python_floor  # noqa: E402

python_floor.require()

import json
import os
import subprocess
import time
import urllib.request
import uuid

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))
import hooks  # noqa: E402
import readout  # noqa: E402
import runtime_id  # noqa: E402
import spool  # noqa: E402
import supervision  # noqa: E402
import verb_help  # noqa: E402
from local_workspace import workspace_root  # noqa: E402
from door import door_url, failure, record, remote, brain_route, send  # noqa: E402


BUDGET = 4.0  # seconds, end to end


def claude_version(ws: Path, deadline: float) -> str:
    """`claude --version`, cached in the spool directory for a day; on a cache miss the lookup
    gets what is left of the budget minus what admission needs, never more than one second."""
    cache = spool.spool_dir(ws) / ".claude-version"
    try:
        if time.time() - cache.stat().st_mtime < 86400:
            cached = cache.read_text().strip()
            if supervision._version(cached) >= supervision.MIN_VERSION:
                return cached  # anything else — empty, torn, below the floor — is a miss
    except OSError:
        pass
    v = os.environ.get("STEERING_CLAUDE_VERSION")
    if not v:
        left = min(1.0, deadline - time.monotonic() - 1.5)
        try:
            v = subprocess.run(["claude", "--version"], capture_output=True, text=True, timeout=max(left, 0.1)).stdout.split()[0]
        except (OSError, subprocess.TimeoutExpired, IndexError):
            return "0.0.0"  # below the floor, so the event is not admitted — and never cached
    try:  # published atomically: a hook killed mid-write must not leave a truncated cache behind
        cache.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        spool.write_atomic(cache, v)
    except OSError:
        pass
    return v


def main() -> int:
    argv = sys.argv[1:]
    if verb_help.help_requested("observe", argv, bare=False):
        print(verb_help.script_help("observe")); return 0
    if argv:
        return verb_help.error("observe", f"unexpected arguments: {' '.join(argv)}")
    deadline = time.monotonic() + BUDGET
    try:
        hook = json.load(sys.stdin)
    except ValueError:
        return finish()
    anchor = Path(os.environ.get("CLAUDE_PROJECT_DIR") or hook.get("cwd") or os.getcwd())
    try:
        ws = workspace_root(anchor, timeout=1.0)
    except Exception:
        return finish()
    try:
        # Every hook event reaches here, which makes this the highest-frequency point to keep
        # the workspace launcher current — self-healing the #1875 round-3 gap where a launcher
        # wired before `exec` existed would otherwise sit stale until something else happened to
        # repin it. Best-effort: a malformed settings.local.json or unreadable launcher must not
        # cost the observation this hook exists to send.
        hooks.repin(ws)
    except Exception:
        pass
    o = supervision.observe(hook, str(uuid.uuid4()), time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                            claude_version(ws, deadline), None)
    if o is None:
        return finish()
    if o["event"] == "Stop":
        try:
            h = supervision.head(readout.last_assistant_text(hook))
            if h:
                o["reply_head"] = h
        except Exception:
            pass  # no reply head: the observation still stands
    try:
        rid = runtime_id.derive("claude", timeout=min(1.0, max(0.1, deadline - time.monotonic() - 1.5)))
        if rid:
            o["runtime_id"] = rid
    except Exception:
        pass  # no runtime: admitted, unattributed
    try:
        path = spool.admit(ws, o)
    except OSError:
        return finish()
    if path is None:
        return finish()
    left = deadline - time.monotonic()
    if left < 0.2:
        record("observe", f"admitted, no time left to send ({left:.2f}s of {BUDGET})", ws)
        return finish()  # admitted; the sweep delivers it
    seated = brain_route("observe")
    base = seated[0] if seated else f"{door_url()}/steering/observe"
    sent_at = time.monotonic(); notice = None
    try:
        headers = {"Content-Type": "application/json"}
        if seated:
            headers.update(seated[1])
        elif not remote():
            token = (ws / ".claude" / "steering-observe-token").read_text().strip()
            if not token:
                return finish()
            headers["X-Steering-Observe"] = token
        req = urllib.request.Request(base, data=json.dumps(o).encode(), headers=headers, method="POST")
        with send(req, timeout=min(2.0, left)) as r:
            if 200 <= r.status < 300:
                claimed = spool.claim(path)
                if claimed:
                    claimed.unlink()
                notice = notice_in(r)
    except Exception as e:  # the sweep owns whatever stays; the hook log keeps why
        record("observe", f"send to {base} failed after {time.monotonic() - sent_at:.2f}s: {failure(e)}", ws)
    # A turn that runs long reaches no Stop, so its tool calls carry the reading (#1159).
    left = deadline - time.monotonic()
    if o["event"] == "PostToolUse" and left > 0.3:
        try:
            if readout.due(ws, hook, time.time()):
                readout.midturn(hook, ws, o.get("runtime_id"), min(1.5, left))
        except Exception as e:  # a reading is worth one attempt; the next window brings another
            record("readout", f"mid-turn post failed: {failure(e)}", ws)
    return finish(o["event"], notice) if notice else finish()


def notice_in(r) -> str | None:
    """What the daemon told this session about itself in the answer (doc 71), or None."""
    try:
        n = json.loads(r.read() or b"{}").get("notice")
    except (ValueError, AttributeError, OSError):
        return None
    return n if isinstance(n, str) and n else None


def finish(event: str | None = None, notice: str | None = None) -> int:
    out = ({"hookSpecificOutput": {"hookEventName": event, "additionalContext": notice}}
           if event and notice else {})
    sys.stdout.write(json.dumps(out) + "\n"); sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
