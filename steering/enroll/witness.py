#!/usr/bin/env python3
"""An observation for a harness that installs no hook (doc 47 §12).

`bind:` is admitted only for an incarnation the observation store has seen, so a session of a
harness that posts none can enrol and never bind. Claude Code's hooks are that seeing; this is
the equivalent for a harness with none, and it refuses to speak for an incarnation it cannot
find serving its own transcript on this machine.
"""
from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))
import machine_harness as harness_mod  # noqa: E402
import runtime_id  # noqa: E402
from ack import provider_ids, records  # noqa: E402
from door import record  # noqa: E402

class Unwitnessed(RuntimeError):
    """The incarnation could not be established, so nothing was posted."""


def refuse(ws: Path, check: str, why: str) -> None:
    record(check, why, ws)
    raise Unwitnessed(why)


def witness(ws: Path, h: harness_mod.Harness, psession: str, runtime: str) -> None:
    """Post one observation for `psession`, having checked the harness still serves it here.

    The descriptor is the precondition, and it is what a session cannot fabricate: the thread's
    transcript has to be open for writing by a live process whose identity derives to the
    runtime being claimed. A launch's linked thread supplies that descriptor while the
    observation names the job its epoch was enrolled under."""
    try:
        enrolled, served = provider_ids(records(ws), psession)
    except ValueError as why:
        refuse(ws, "witness-alias-ambiguous", str(why))
    if h.loaded is None:
        refuse(ws, "witness-probe-unavailable", f"{h.provider} cannot check whether session {served} is served here")
    if not h.loaded(served):
        refuse(ws, "witness-session-unserved", f"nothing on this machine is serving {h.provider} session {served}")
    pid = h.harness_pid(served) if h.harness_pid else None
    if not pid:
        refuse(ws, "witness-process-missing", f"no process serving {served} could be identified")
    derived = runtime_id.derive(h.provider, pid=pid)
    if derived is None:
        refuse(ws, "witness-runtime-missing", f"no runtime id for the process serving {served}")
    if derived != runtime:
        # The claim and the descriptor disagree, so one of them is about another process. An
        # observation posted here would attribute this thread's work to whichever it was.
        refuse(ws, "witness-runtime-mismatch", f"the process serving {served} is not {runtime}")
    import session_routes  # noqa: E402
    try:
        session_routes.observe(h.provider, enrolled, runtime)
    except session_routes.Refused as e:
        refuse(ws, "witness-observation-refused", f"the observation was refused: {e}")
    except session_routes.Unsent as e:
        refuse(ws, "witness-observation-unsent", f"the observation could not be sent: {e}")
    except OSError as e:
        refuse(ws, "witness-observation-io", f"the observation could not be sent: {e}")
