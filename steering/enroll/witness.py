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

class Unwitnessed(RuntimeError):
    """The incarnation could not be established, so nothing was posted."""


def witness(ws: Path, h: harness_mod.Harness, psession: str, runtime: str) -> None:
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
    import session_routes  # noqa: E402
    try:
        session_routes.observe(h.provider, psession, runtime)
    except session_routes.Refused as e:
        raise Unwitnessed(f"the observation was refused: {e}") from e
