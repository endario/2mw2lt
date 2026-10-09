"""The one authenticated POST shape every per-turn sender uses (doc 15, doc 113).

Shared between `enroll/readout.py` (a Stop-hook subprocess) and `wakeexec.py` (the long-lived
agent process, posting a self-backfilled targeting record): both reach the same daemon door the
same way, so the shape lives once rather than as two copies that could drift.
"""
from __future__ import annotations

import json
import sys
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "enroll"))
from ack import minted_for  # noqa: E402
from door import door_url, remote, brain_route, send  # noqa: E402


# What Go's targeting record holds of the one `targeting.build` makes: `source` is its `provider`,
# and the record's version and observation time are not Go's to keep.
GO_TARGETING = ("provider_session", "runtime_id", "entrypoint", "pid", "cwd",
                "tmux_socket", "tmux_pane", "user_data_dir", "app_pid")


def go_session(workspace: Path, provider_session: object) -> str | None:
    """The session Go enrolled here for this harness session, or None when its credential is not Go's."""
    import ack
    for name, held in ack.records(workspace).items():
        if provider_session and held.get("provider_session") == provider_session and held.get("authority") == "coordination":
            return name
    return None


def post_go_targeting(rec: dict, session: str, timeout: float) -> None:
    """The record to Go's `POST /sessions/{session}/targeting`, on the machine's own credential: a
    retire's checkout and a wake's pane are read from it."""
    import session_routes
    body = {k: rec[k] for k in GO_TARGETING if k in rec} | {"provider": rec.get("source")}
    try:
        url = f"{session_routes._base()}/sessions/{urllib.parse.quote(session, safe='')}/targeting"
        token = session_routes._machine_token()
    except SystemExit as e:  # the callers keep a failed capture to a line, never an exit
        raise RuntimeError(str(e)) from None
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST", headers={
        "Content-Type": "application/json", session_routes.MACHINE_CARRIER: token,
        "User-Agent": session_routes.door.USER_AGENT})
    with session_routes.door.open_direct(req, timeout):
        pass


def post(rec: dict, workspace: Path | None, timeout: float, route: str = "vitals") -> None:
    if route == "vitals":
        return  # the machine's agent sends each held session's reading itself (agent/reading)
    headers = {"Content-Type": "application/json"}
    seated = brain_route(route)
    go = route == "targeting" and not seated and workspace is not None and go_session(workspace, rec.get("provider_session"))
    if go:
        post_go_targeting(rec, go, timeout)
        return
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
