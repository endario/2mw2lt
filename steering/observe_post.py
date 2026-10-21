"""The one authenticated POST shape every per-turn sender uses (doc 15, doc 113).

Shared between `enroll/readout.py` (a Stop-hook subprocess) and `wakeexec.py` (the long-lived
agent process, posting a self-backfilled targeting record): both reach the same daemon door the
same way, so the shape lives once rather than as two copies that could drift.
"""
from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "enroll"))
import local_workspace  # noqa: E402
from ack import minted_for  # noqa: E402
from door import door_url, failure, remote, brain_route, send  # noqa: E402


# What Go's targeting record holds of the one `targeting.build` makes: `source` is its `harness`,
# and the record's version and observation time are not Go's to keep.
GO_TARGETING = ("provider_session", "runtime_id", "entrypoint", "pid", "cwd",
                "tmux_socket", "tmux_pane", "tmux_attached", "user_data_dir", "app_pid")


def go_session(workspace: Path, provider_session: object) -> str | None:
    """The session Go enrolled here for this harness session, or None when its credential is not Go's."""
    import ack
    for name, held in ack.records(workspace).items():
        if provider_session and held.get("provider_session") == provider_session and held.get("authority") == "coordination":
            return name
    return None


def relayed(workspace: Path, session: str, rec: dict, timeout: float) -> bool:
    """Whether this machine's agent took the record to send on its warm connection (#8005), as the
    observe hook's ride does (#7491): a hook's own send pays a fresh TLS handshake inside its cap,
    which a slow route to the door fails in every bad window. An agent that answered is final: its
    relayed refusal is raised, and one still forwarding when the wait ends holds the record, which
    a second, cold send would only race. False when no agent can take it, so the hook sends it."""
    req = local_workspace.agent_request(workspace, "/steering/targeting", {"session": session, "record": rec},
                                        timeout=min(1.0, timeout))
    if req is None:
        return False
    try:
        with send(req, timeout=timeout):
            return True
    except urllib.error.HTTPError as e:
        # An agent older than the route answers its mux's plain-text 404; a refusal it relays is JSON.
        if "json" not in (e.headers.get("Content-Type") or ""):
            return False
        raise RuntimeError(f"the agent relayed the door's refusal: {failure(e)}") from None
    except TimeoutError:  # urllib wraps a connect's timeout in URLError; a bare one is the answer's
        return True
    except OSError:  # no agent answering
        return False


def post_go_targeting(rec: dict, session: str, timeout: float, workspace: Path | None = None) -> None:
    """The record to Go's `POST /sessions/{session}/targeting`, on the machine's own credential: a
    retire's checkout and a wake's pane are read from it. Through the workspace's agent where one
    takes it."""
    deadline = time.monotonic() + timeout
    if workspace is not None and relayed(workspace, session, rec, timeout):
        return
    timeout = max(0.1, deadline - time.monotonic())
    import session_routes
    body = {k: rec[k] for k in GO_TARGETING if k in rec} | {"harness": rec.get("source")}
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
        post_go_targeting(rec, go, timeout, workspace)
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
