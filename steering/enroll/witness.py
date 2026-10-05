#!/usr/bin/env python3
"""An observation for a harness that installs no hook (doc 47 §12).

`bind:` is admitted only for an incarnation the observation store has seen, so a session of a
harness that posts none can enrol and never bind. Claude Code's hooks are that seeing; this is
the equivalent for a harness with none, and it refuses to speak for an incarnation it cannot
find serving its own transcript on this machine.
"""
from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))
import machine_harness as harness_mod  # noqa: E402
import runtime_id  # noqa: E402
import spool  # noqa: E402
from door import door_url, remote, send  # noqa: E402

EVENT = "Present"   # not one of Claude's hook events: the reducer has no boundary for it, so it
                    # makes a candidate and adds nothing to what the board says a session is doing


class Unwitnessed(RuntimeError):
    """The incarnation could not be established, so nothing was posted."""


def observation(h: harness_mod.Harness, psession: str, runtime: str) -> dict:
    return {"v": 4, "source": h.provider, "source_version": (h.version() if h.version else None) or "unknown",
            "session": psession, "epoch": None, "event": EVENT,
            "occurrence": str(uuid.uuid4()),
            "observed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "runtime_id": runtime}


def witness(ws: Path, h: harness_mod.Harness, psession: str, runtime: str,
            timeout: float = 5.0) -> dict:
    """Post one observation for `psession`, having checked the harness still serves it here.

    The descriptor is the precondition, and it is what a session cannot fabricate: the thread's
    transcript has to be open for writing by a live process whose identity derives to the
    runtime being claimed."""
    if h.loaded is None or not h.loaded(psession):
        raise Unwitnessed(f"nothing on this machine is serving {h.provider} session {psession}")
    pid = h.harness_pid(psession) if h.harness_pid else None
    derived = runtime_id.derive(h.provider, pid=pid) if pid else None
    if derived != runtime:
        # The claim and the descriptor disagree, so one of them is about another process. An
        # observation posted here would attribute this thread's work to whichever it was.
        raise Unwitnessed(f"the process serving {psession} is not {runtime}")
    o = observation(h, psession, runtime)
    spool.admit(ws, o)          # durable first: the sweep delivers whatever the send cannot
    base = door_url()
    headers = {"Content-Type": "application/json"}
    if not remote():
        token = (ws / ".claude" / "steering-observe-token").read_text().strip()
        if not token:
            raise Unwitnessed("the workspace holds no observe token")
        headers["X-Steering-Observe"] = token
    req = urllib.request.Request(f"{base}/steering/observe", data=json.dumps(o).encode(),
                                 headers=headers, method="POST")
    try:
        with send(req, timeout=timeout) as r:
            if not 200 <= r.status < 300:
                raise Unwitnessed(f"{base} answered {r.status}")
    except urllib.error.HTTPError as e:
        # A refusal is an answer, and the opener raises it rather than returning it. Reported as
        # a send failure it reads as a network fault, which is the wrong thing to go and check.
        raise Unwitnessed(f"{base} answered {e.code}") from e
    return o
