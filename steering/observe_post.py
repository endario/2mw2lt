"""The one authenticated POST shape every per-turn sender uses (doc 15, doc 113).

Shared between `enroll/readout.py` (a Stop-hook subprocess) and `wakeexec.py` (the long-lived
agent process, posting a self-backfilled targeting record): both reach the same daemon door the
same way, so the shape lives once rather than as two copies that could drift.
"""
from __future__ import annotations

import json
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "enroll"))
from ack import minted_for  # noqa: E402
from door import door_url, remote, seat_route, send  # noqa: E402


def post(rec: dict, workspace: Path | None, timeout: float, route: str = "vitals") -> None:
    headers = {"Content-Type": "application/json"}
    seated = seat_route(route)
    if seated:
        url, extra = seated
        headers.update(extra)
    else:
        url = f"{door_url()}/steering/{route}"
        if not remote():
            if workspace is None:
                return
            token = (workspace / ".claude" / "steering-observe-token").read_text().strip()
            if not token:
                return
            headers["X-Steering-Observe"] = token
            if route == "targeting":
                # The workspace token names no session; the loopback door takes a targeting
                # record only with its own session's enrolment token beside it (#2052).
                own = minted_for(workspace, rec.get("provider_session"))
                if own:
                    headers["X-Steering-Session"] = own
    req = urllib.request.Request(url, data=json.dumps(rec).encode(), headers=headers, method="POST")
    with send(req, timeout=timeout):
        pass
