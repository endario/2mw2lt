"""The session commands on a workspace whose authority is Go (C5,
documentation/architecture/go-c5-session-client-design.md).

The workspace's `.env` says so: `STEERING_AUTHORITY=coordination`. Without that line nothing here
is used and the door's lines are spoken as before. The machine observes and enrols on its own
carrier; the session binds and detaches on its own, and no request carries both.
"""
from __future__ import annotations

import email.utils
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))
import door  # noqa: E402
import refusal  # noqa: E402

COORDINATION = "coordination"
MACHINE_CARRIER = "X-Steering-Agent-Credential"
SESSION_CARRIER = "X-Steering-Session-Credential"
# A refusal of an invalid token: unknown, expired, or one a detachment revoked. Go answers all of
# them alike, so the client cannot tell which, only that the token can no longer act.
INVALID_TOKEN = "session-token-locator-read"
# How long, and how many times, a throttled or unavailable answer is sent again: past either the
# refusal is reported rather than held, whatever wait Go asked for.
RETRY_LIMIT = 30.0
RETRY_ATTEMPTS = 5


class Refused(Exception):
    """Go refused the request. `code` and `field` are its problem's."""

    def __init__(self, status: int, code: str, field: str = ""):
        super().__init__(f"refused ({status}): {code}" + (f" [{field}]" if field else ""))
        self.status, self.code, self.field = status, code, field


class Unsent(OSError):
    """No answer came: the request may or may not have been committed."""


def authority(ws: Path | None = None) -> str:
    """The workspace's authority: "" for the incumbent's door, or `coordination`. Read as the door
    is (`door.configured_door`): the environment, else the workspace's `.env` (`ws`, or the one
    found from here); under `door.ANCHOR`, that workspace's `.env` alone. So every stage of one
    command reads the same. Any other value is refused, so a typo cannot quietly keep the
    incumbent."""
    anchored = door.ANCHOR.get()
    value = "" if anchored is not None else os.environ.get("STEERING_AUTHORITY", "").strip()
    if not value:
        value = _env_value(anchored or ws, "STEERING_AUTHORITY")
    if value not in ("", COORDINATION):
        raise SystemExit(f"STEERING_AUTHORITY={value}: the authorities are the incumbent's door "
                         f"(no line) and {COORDINATION}")
    return value


def _env_value(ws: Path | None, key: str) -> str:
    if ws is None:
        from local_workspace import workspace_root  # noqa: E402
        anchor = Path(os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd())
        try:
            ws = workspace_root(anchor, timeout=1.0)
        except (subprocess.SubprocessError, OSError) as e:
            # Not knowing the workspace is not knowing its authority: never read as the incumbent's.
            raise SystemExit(f"door: the workspace lookup from {anchor} failed ({e}); its authority is unknown")
    try:
        text = (ws / ".env").read_text()
    except FileNotFoundError:
        return ""
    for line in text.splitlines():
        k, _, v = line.strip().partition("=")
        if k == key:
            return v.strip().strip('"').strip("'")
    return ""


def on_coordination(ws: Path | None = None) -> bool:
    return authority(ws) == COORDINATION


def _base() -> str:
    """The workspace's routes: the door, which on Go names its workspace (`/w/<id>`)."""
    base, _ = door.door()
    _, workspace = door.split(base)
    if not workspace:
        raise SystemExit(f"STEERING_DOOR {base}: a Go workspace's door names it (…/w/<id>)")
    return f"{base}/api/v1"


def _machine_token() -> str:
    import credential  # noqa: E402
    token = credential.for_url(door.door()[0])
    if not token:
        raise SystemExit("this OS user holds no machine credential for this door: run /2mw2lt:install")
    return token


def post(path: str, body: dict, key: str, carrier: str, token: str, timeout: float = 10.0) -> dict:
    """One write under `key`, sent again under the same key while Go asks it to wait."""
    data = json.dumps(body).encode()
    started, attempts = time.monotonic(), 0
    while True:
        req = urllib.request.Request(_base() + path, data=data, method="POST", headers={
            "Content-Type": "application/json", "Idempotency-Key": key, carrier: token,
            "User-Agent": door.USER_AGENT})
        try:
            with door.open_direct(req, timeout) as r:
                return json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            problem = _problem(e)
            pause = _retry_after(e) if e.code in (429, 503) else None
            attempts += 1
            if pause is None or attempts >= RETRY_ATTEMPTS or time.monotonic() - started + pause > RETRY_LIMIT:
                raise Refused(e.code, problem.get("code") or "unexplained", problem.get("field") or "") from None
            time.sleep(pause)
        except (urllib.error.URLError, OSError, TimeoutError) as e:
            raise Unsent(f"{_base()}{path}: no answer ({e})") from None


def _problem(e: urllib.error.HTTPError) -> dict:
    try:
        got = json.loads(e.read() or b"{}")
    except ValueError:
        return {}
    return got if isinstance(got, dict) else {}


def _retry_after(e: urllib.error.HTTPError) -> float | None:
    """The wait Go asked for, or None when it named none this client can read: then the answer
    is reported as it came."""
    value = (e.headers.get("Retry-After") or "").strip()
    if value.isdecimal():
        return float(value)
    try:
        when = email.utils.parsedate_to_datetime(value) if value else None
    except (TypeError, ValueError):
        return None
    return max(0.0, when.timestamp() - time.time()) if when else None


def settled(e: Refused) -> bool:
    """Whether Go's refusal means nothing was issued: it answered, and not with a request to
    wait. A 5xx answer leaves the request's outcome unknown."""
    return 400 <= e.status < 500 and e.status not in (408, 429)


def _incarnation(provider: str, psession: str, runtime: str) -> dict:
    return {"provider": provider, "provider_session": psession, "runtime_id": runtime}


def observe(provider: str, psession: str, runtime: str) -> None:
    """The machine records an incarnation it saw: a harness with no hook of its own."""
    post("/incarnations", _incarnation(provider, psession, runtime), uuid.uuid4().hex,
         MACHINE_CARRIER, _machine_token())


def pending(ws: Path, session: str) -> bool:
    """Whether an enrolment of `session` was sent and not settled."""
    return _pending_path(ws, session).exists()


def _pending_path(ws: Path, session: str) -> Path:
    import ack  # noqa: E402
    p = ack.token_path(ws, session)
    return p.with_name(p.name + ".enrolling")


def enrol(ws: Path, session: str, provider: str, psession: str, runtime: str) -> dict:
    """Enrol `session` and store its credential. The request is written before it is sent, and an
    unsettled one is sent again as written, whatever process asks: Go answers a key it has seen
    with the credential it issued, and refuses the key with any other body."""
    pending = _pending_path(ws, session)
    try:
        request = json.loads(pending.read_text())
    except (OSError, ValueError):
        request = {"key": uuid.uuid4().hex,
                   "body": {"name": session, **_incarnation(provider, psession, runtime)}}
        pending.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(pending, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(json.dumps(request))
        _durable(pending)
    try:
        answer = post("/sessions", request["body"], request["key"], MACHINE_CARRIER, _machine_token())
    except Refused as e:
        if settled(e):
            # Nothing was issued, so the request is not one to recover: the next enrolment is a
            # new one, from the process that asks then.
            pending.unlink(missing_ok=True)
        raise
    import ack  # noqa: E402
    ack.store(ws, session, answer["credential"], request["body"]["provider_session"],
              authority=COORDINATION, epoch=answer.get("epoch"))
    # The credential is on disk before the request that recovers it is gone.
    _durable(ack.token_path(ws, session))
    pending.unlink(missing_ok=True)
    return answer


def _durable(path: Path) -> None:
    """`path` and its directory entry survive a machine crash."""
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
    fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def bind(session: str, token: str, provider: str, psession: str, runtime: str) -> str:
    """The session binds the process it now runs in; the answer reads as the door's did."""
    try:
        post(f"/sessions/{session}/bindings", _incarnation(provider, psession, runtime),
             uuid.uuid4().hex, SESSION_CARRIER, token)
    except Unsent as e:
        return refusal.retry(str(e))
    except Refused as e:
        if e.code == INVALID_TOKEN:
            return refusal.reconnect(f"unknown, detached or stale token for {session}")
        return refusal.reconnect(str(e))
    return f"bound: {psession} to {session}"


def detach(session: str, token: str, key: str | None = None) -> str:
    """The session ends its own epoch. A retry after a lost answer finds its token refused, which
    says only that the session can no longer act, never that it detached."""
    try:
        post(f"/sessions/{session}/detachment", {}, key or uuid.uuid4().hex, SESSION_CARRIER, token)
    except Unsent as e:
        return refusal.retry(str(e))
    except Refused as e:
        if e.code == INVALID_TOKEN:
            return refusal.reconnect(f"{session} can no longer act: its token is not valid")
        return refusal.retry(str(e))
    return f"detached: {session}"
