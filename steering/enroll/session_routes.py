"""The session commands on Go (C5, documentation/architecture/go-c5-session-client-design.md).

The machine observes and enrols on its own carrier. Session requests present their bearer with this
machine's credential.
"""
from __future__ import annotations

import email.utils
import hashlib
import http.client
import json
import os
import re
import shlex
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))
import door  # noqa: E402
import refusal  # noqa: E402
import requestlog  # noqa: E402

MACHINE_CARRIER = "X-Steering-Agent-Credential"
SESSION_CARRIER = "X-Steering-Session-Credential"
# A refusal of an invalid token: unknown or expired, which Go answers alike; one whose epoch was
# ended (retired, left, swept, or enrolled again elsewhere); or a stored token not shaped as one,
# which can never act. Each is repaired by connecting again.
INVALID_TOKEN = frozenset({"session-token-locator-read", "session-credential-ended", "session-credential-carrier-shape"})
# How long, and how many times, a throttled or unavailable answer is sent again: past either the
# refusal is reported rather than held, whatever wait Go asked for.
RETRY_LIMIT = 30.0
RETRY_ATTEMPTS = 5
# What the gateway answers when the silo behind it does not (`door.py` answers the same shape):
# the request itself is not refused, the wire is down, so it is resent like a lost answer. A 503
# that carries a `Retry-After` — one this client can read or not — is Go pacing the request, not
# the wire: an unreadable wait is reported at once, exactly as a readable one is waited.
GATEWAY_DOWN = frozenset({502, 503, 504})


class Refused(Exception):
    """Go refused the request. `code`, `field` and `remedy` are its problem's. The reason Go named,
    `detail`, rides the message after the request id, so every reader that prints the exception
    says why, not only the code and ids (#9022)."""

    def __init__(self, status: int, code: str, field: str = "", wait: int | None = None, request: str = "",
                 remedy: str = "", detail: str = ""):
        super().__init__(f"refused ({status}): {code}" + (f" [{field}]" if field else "")
                         + (f" request {request}" if request else "")
                         + (f": {detail}" if detail else ""))
        self.status, self.code, self.field, self.wait, self.request = status, code, field, wait, request
        self.remedy = remedy
        # What Go's refusal names beyond its code, where it names one: a commission's id.
        self.detail = detail


def reconnect(e: Refused, reason: str | None = None) -> str:
    """A refusal connecting again repairs, unless Go named this client older than its contract."""
    if e.remedy == "update-plugin":
        return refusal.outdated(str(e))
    return refusal.reconnect(reason or str(e))


def outdated(e: Refused) -> str | None:
    """The refusal to give when Go named this client older than its contract, which no resend or
    reconnect repairs; None otherwise."""
    return refusal.outdated(str(e)) if e.remedy == "update-plugin" else None


def _remedy(problem: dict) -> str:
    got = problem.get("remedy")
    return got if isinstance(got, str) else ""


class Unsent(OSError):
    """No answer came: the request may or may not have been committed."""


def _base() -> str:
    """The workspace's routes: the door, which on Go names its workspace (`/<team>/<workspace>`, or
    `/w/<alias>`)."""
    base, _ = door.door()
    _, workspace = door.split(base)
    if not workspace:
        raise SystemExit(f"STEERING_DOOR {base}: a Go workspace's door names it (…/<team>/<workspace>)")
    return f"{base}/api/v1"


def _machine_token() -> str:
    import credential  # noqa: E402
    token = credential.for_url(door.door()[0])
    if not token:
        raise SystemExit("this OS user holds no machine credential for this door: run /2mw2lt:install")
    return token


def session_headers(token: str) -> dict[str, str]:
    """The paired credentials for one direct session request."""
    return {SESSION_CARRIER: token, MACHINE_CARRIER: _machine_token(), "User-Agent": door.USER_AGENT}


def _renewed(e: urllib.error.HTTPError, problem: dict, presented: str | None) -> bool:
    """Whether the machine credential a request presented was superseded by its renewal between
    the read and the send. Go refuses it before any effect, so the request goes once more on the
    renewed one (measured 2026-10-13: a gate carry 100 ms after a renewal)."""
    if e.code != 403 or problem.get("code") != "binding-lifecycle-mismatch" or not presented:
        return False
    import credential  # noqa: E402
    try:
        return _machine_token() != presented
    except (SystemExit, credential.NoCredential):
        return False


def post(path: str, body: dict, key: str, carrier: str, token: str, timeout: float = 10.0,
         resend: bool = True, method: str = "POST") -> dict:
    """One write under `key`, sent again under the same key while Go asks it to wait, and again
    when the wire loses the answer — no answer, a reset, a gateway speaking for a silo that is
    down, an answer that is not JSON, as a captive portal's is — within one budget the call owns,
    each resend one line of progress on stderr (#5974). `resend=False` names a write Go records
    nothing to replay a resend under the key (a mint), which keeps one attempt however its
    answer is lost. `method` names the verb where a route's is not POST, as the bulletin's post
    is a PUT."""
    data = json.dumps(body).encode()
    started, attempts = time.monotonic(), 0
    again = door.resender(started, f"session: {path}", resend=resend)
    renewed = False
    while True:
        headers = (session_headers(token) if carrier == SESSION_CARRIER
                   else {carrier: token, "User-Agent": door.USER_AGENT})
        headers.update({"Content-Type": "application/json", "Idempotency-Key": key})
        req = urllib.request.Request(_base() + path, data=data, method=method, headers=headers)
        try:
            with door.open_direct(req, timeout) as r:
                return json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            problem = _problem(e)
            if not renewed and carrier == SESSION_CARRIER and _renewed(e, problem, req.get_header(MACHINE_CARRIER.capitalize())):
                renewed = True
                continue
            paced = (e.headers.get("Retry-After") or "").strip() if e.headers else ""
            pause = _retry_after(e) if e.code in (429, 503) and paced else None
            if pause is None:
                if e.code in GATEWAY_DOWN and not (e.code == 503 and paced) \
                        and (wait := again(f"the gateway answered {e.code}")) is not None:
                    time.sleep(wait)
                    continue
            else:
                # A wait Go named is waited out under the attempts this call owns: the pacing and
                # the wire share one budget from the call's start, and a gateway resend spends
                # none of the pacing's attempts.
                attempts += 1
                if attempts < RETRY_ATTEMPTS and time.monotonic() - started + pause <= RETRY_LIMIT:
                    time.sleep(pause)
                    continue
            wait_seconds = problem.get("wait_seconds")
            detail = problem.get("detail")
            raise Refused(e.code, problem.get("code") or "unexplained", problem.get("field") or "",
                          wait_seconds if isinstance(wait_seconds, int) else None, _request(e, problem), _remedy(problem),
                          detail if isinstance(detail, str) and len(detail) <= 200 else "") from None
        except (urllib.error.URLError, OSError, TimeoutError, http.client.HTTPException, ValueError) as e:
            if (wait := again(f"no answer ({e})")) is not None:
                time.sleep(wait)
                continue
            raise Unsent(f"{_base()}{path}: no answer ({e})") from None


def write(path: str, body: dict, key: str, carrier: str, token: str, timeout: float = 10.0) -> dict:
    """`post` for a write one invocation sends under its own key. `post` itself resends a lost
    answer under that key within its budget, so the write needs no loop of its own. Not for a
    write that is a fresh request each time, or that has a deadline or a recovery of its own."""
    return post(path, body, key, carrier, token, timeout)


def _problem(e: urllib.error.HTTPError) -> dict:
    try:
        got = json.loads(e.read() or b"{}")
    except ValueError:
        return {}
    return got if isinstance(got, dict) else {}


def _request(e: urllib.error.HTTPError, problem: dict) -> str:
    """The id Go refused the request under, which its journal names, so a refusal is found there."""
    got = (e.headers.get(requestlog.HEADER) if e.headers else None) or problem.get("request")
    return got if isinstance(got, str) and requestlog.REQUEST_ID.fullmatch(got) else ""


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


def _current_machine_refusal(e: Refused) -> str:
    return refusal.resend(str(e), "from the session's current machine, or have the brain rebind the session to this one if its host was admitted again")


def _incarnation(provider: str, psession: str, runtime: str) -> dict:
    return {"harness": provider, "provider_session": psession, "runtime_id": runtime}


def observe(provider: str, psession: str, runtime: str) -> None:
    """The machine records an incarnation it saw: a harness with no hook of its own."""
    post("/incarnations", _incarnation(provider, psession, runtime), uuid.uuid4().hex,
         MACHINE_CARRIER, _machine_token())


def pending(ws: Path, session: str) -> bool:
    """Whether an enrolment of `session` was sent and not settled."""
    return _pending_path(ws, session).exists()


def pending_name(ws: Path, psession: str) -> str | None:
    """The name an unsettled enrolment already holds for this provider session."""
    for path in sorted(_pending_path(ws, "").parent.glob("*.enrolling")):
        try:
            request = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        if not isinstance(request, dict) or not isinstance(request.get("key"), str) or not request["key"]:
            continue
        body = request.get("body")
        if not isinstance(body, dict) or body.get("provider_session") != psession:
            continue
        name = body.get("name")
        if isinstance(name, str) and name and _pending_path(ws, name) == path:
            return name
    return None


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
    body = request["body"]
    if "provider" in body:   # written before #6550; Go hashes the decoded request, so its key still answers
        body = {("harness" if k == "provider" else k): v for k, v in body.items()}
    try:
        answer = post("/sessions", body, request["key"], MACHINE_CARRIER, _machine_token())
    except Refused as e:
        if settled(e):
            # Nothing was issued, so the request is not one to recover: the next enrolment is a
            # new one, from the process that asks then.
            pending.unlink(missing_ok=True)
        raise
    import ack  # noqa: E402
    ack.store(ws, session, answer["credential"], request["body"]["provider_session"],
              authority="coordination", epoch=answer.get("epoch"))
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


def bind(session: str, token: str, provider: str, psession: str, runtime: str,
         account_id: str | None = None) -> str:
    """The session binds the process it now runs in, and the account it spends from when it has
    one; the answer reads as the door's did."""
    try:
        post(f"/sessions/{session}/bindings",
             _incarnation(provider, psession, runtime) | ({"account_id": account_id} if account_id else {}),
             uuid.uuid4().hex, SESSION_CARRIER, token)
    except Unsent as e:
        return refusal.retry(str(e))
    except Refused as e:
        if e.code == "session-binding-moved":
            return _current_machine_refusal(e)
        if not settled(e):  # Go asked to wait past the retries, or did not say: the binding may land later
            return refusal.retry(str(e))
        if e.code in INVALID_TOKEN:
            return refusal.reconnect(f"unknown, detached or stale token for {session}")
        return reconnect(e)
    return f"bound: {psession} to {session}"


def detach(session: str, token: str, key: str | None = None, handover: str = "") -> str:
    """The session ends its own epoch, its handover checkpoint kept with it when it names one. A retry
    after a lost answer finds its token refused, which says only that the session can no longer act,
    never that it detached."""
    body = {"exit": "handover", "text": handover} if handover else {}
    try:
        post(f"/sessions/{session}/detachment", body, key or uuid.uuid4().hex, SESSION_CARRIER, token)
    except Unsent as e:
        return refusal.retry(str(e))
    except Refused as e:
        if e.code == "session-binding-moved":
            return _current_machine_refusal(e)
        if e.remedy == "update-plugin":
            return refusal.outdated(str(e))
        if e.code in INVALID_TOKEN:
            return refusal.reconnect(f"{session} can no longer act: its token is not valid")
        if e.status == 400 and handover:  # a handover Go does not take: sent again it fails again
            return refusal.resend(str(e), "a --handover that is the exit checkpoint's id")
        if e.status == 400:  # no handover was sent, so what Go refused is the session it names
            return reconnect(e)
        return refusal.retry(str(e))
    return f"detached: {session}"


def checkpoint(token: str, body: dict) -> tuple[str, bool]:
    """The session records its checkpoint (`POST /checkpoints`), its text or the pointer to a note,
    keyed by its occurrence, so a resend under `--retry` is answered from the first: the answer,
    naming the checkpoint's own id, and whether it may have been recorded unanswered, which only a
    resend under the occurrence settles."""
    kept = {"text": body["text"]} if "text" in body else {"note": body["note"], "note_sha256": body["note_sha256"]}
    sent = {"occurrence": body["id"], "boundary": body["boundary"], **kept,
            **({"learned": body["learned"]} if body.get("learned") else {})}
    try:
        answer = write("/checkpoints", sent, body["id"], SESSION_CARRIER, token)
    except Unsent as e:
        return refusal.retry(str(e)), True
    except Refused as e:
        if e.code == "session-binding-moved":
            return _current_machine_refusal(e), False
        if e.remedy == "update-plugin":
            return refusal.outdated(str(e)), False
        if e.code in INVALID_TOKEN:
            return refusal.reconnect("the session's token is not valid"), False
        if not settled(e):
            return refusal.retry(str(e)), True
        if e.code == "checkpoint-credential":
            # The refusal's own words say what rides the credential: str(e) already ends with the
            # line Go named, so no suffix repeats it.
            return refusal.resend(f"{e}: the checkpoint text contains credential-shaped content",
                                  "remove or reword that content, then send the checkpoint again"), False
        return refusal.use("checkpoint", str(e)), False
    got = answer.get("checkpoint") or {}
    dropped = got.get("dropped") or 0
    return f"checkpointed: {got['id']} {body['boundary']}" + (f" dropped: {dropped}" if dropped else ""), False


def checkpoint_now(session: str, token: str, timeout: float) -> dict | None:
    """The session's latest checkpoint at its current epoch, or None: one ask, for a hook that must
    answer within `timeout` whatever the door does. Raises `Refused` or `Unsent` as `get` does."""
    req = urllib.request.Request(_base() + f"/sessions/{urllib.parse.quote(session, safe='')}/checkpoints?epoch=current&limit=1",
                                 headers=session_headers(token))
    try:
        with door.open_direct(req, timeout) as r:
            newest = json.loads(r.read() or b"{}").get("checkpoints") or []
    except urllib.error.HTTPError as e:
        problem = _problem(e)
        raise Refused(e.code, problem.get("code") or "unexplained", problem.get("field") or "",
                      request=_request(e, problem), remedy=_remedy(problem)) from None
    except (urllib.error.URLError, OSError, TimeoutError, http.client.HTTPException, ValueError) as e:
        raise Unsent(f"{_base()}: no answer ({e})") from None
    return newest[0] if newest else None


def checkpoint_read(session: str, token: str, which: str) -> tuple[str, dict | None]:
    """A checkpoint as this session is shown it: `latest`, its own newest at any epoch, or one by its
    id. The checkpoint, or why there is none to show and None."""
    path = (f"/sessions/{urllib.parse.quote(session, safe='')}/checkpoints?limit=1" if which == "latest"
            else f"/checkpoints/{urllib.parse.quote(which, safe='')}")
    try:
        got = get(path, token)
    except Unsent as e:
        return refusal.retry(str(e)), None
    except Refused as e:
        if e.code == "session-binding-moved":
            return _current_machine_refusal(e), None
        if e.remedy == "update-plugin":
            return refusal.outdated(str(e)), None
        if e.code in INVALID_TOKEN:
            return refusal.reconnect("the session's token is not valid"), None
        if not settled(e):
            return refusal.retry(str(e)), None
        return refusal.use("checkpoint", str(e)), None
    if which != "latest":
        return "", got
    newest = got.get("checkpoints") or []
    return ("", newest[0]) if newest else (f"{session} has no checkpoint recorded", None)


def acknowledge(session: str, token: str, directive: str) -> str:
    """The session acknowledges a directive delivered to it; the answer reads as the door's did. The
    key is the acknowledgement's own, so a retry after a lost answer is answered from the first."""
    key = hashlib.sha256(f"ack\n{session}\n{directive}".encode()).hexdigest()[:32]
    try:
        got = write(f"/sessions/{session}/directives/{directive}/acknowledgement", {}, key, SESSION_CARRIER, token)
    except Unsent as e:
        return refusal.retry(str(e))
    except Refused as e:
        if e.code == "session-binding-moved":
            return _current_machine_refusal(e)
        if e.remedy == "update-plugin":
            return refusal.outdated(str(e))
        if e.code in INVALID_TOKEN:
            return refusal.reconnect(f"unknown, detached or stale token for {session}")
        if not settled(e):
            return refusal.retry(str(e))
        if e.code == "directive-missing":
            # Nothing was sent to this session under that id: a mistyped id, which the brain cannot fix.
            return refusal.resend(f"no directive {directive} was sent to {session}: {e}",
                                  "with the id copied from the frame's directive field")
        # Absent, lapsed or another epoch's: settled where sending it again cannot change, and the
        # brain's to follow up.
        return refusal.refuse(f"directive {directive}: {e}",
                              hand_to=("the brain", f"blocked: {session} on directive {directive} refused"))
    if got.get("status") == "already-acknowledged":
        return f"already acknowledged: {directive}"
    return f"acked: {directive} by {session}"


def answer(session: str, token: str, request: str, generation: int, text: str) -> dict:
    """The seat's holder answers a member's request it was given as a say (EL3,
    go-el3-brain-bootstrap-design.md §2.5), on its own carrier, under the seat generation it held
    when it was given it. The key is the answer's own, so a retry after a lost answer is answered
    from the first. Raises `Refused` or `Unsent`."""
    key = hashlib.sha256(f"answer\n{session}\n{request}\n{generation}".encode()).hexdigest()[:32]
    return post(f"/brain/requests/{request}/answer", {"session": session, "generation": generation, "answer": text},
                key, SESSION_CARRIER, token)


def reply(session: str, token: str, generation: int, text: str, occurrence: str, request: str | None = None) -> dict:
    """The seat's holder speaks to the owner on the Direct line (documentation/steering/plans/5555-direct-line):
    unprompted, or about the request `request` names. The first reply about an unanswered request
    completes it; any later one is another message. The occurrence is the message's own name, so a
    resend after a lost answer is answered from the first. Raises `Refused` or `Unsent`."""
    body: dict = {"session": session, "generation": generation, "text": text}
    if request is not None:
        body["request"] = request
    return post("/brain/replies", body, _own_key("reply", session, occurrence), SESSION_CARRIER, token)


def _own_key(*parts: str) -> str:
    """A write's own key, from what makes it that write, so a retry after a lost answer is answered
    from the first rather than done twice."""
    return hashlib.sha256("\n".join(parts).encode()).hexdigest()[:32]


def _paged(path: str, limit: int | None, after: str | None) -> str:
    query = {k: v for k, v in (("limit", limit), ("next", after)) if v is not None}
    return path + ("?" + urllib.parse.urlencode(query) if query else "")


def timer_list(token: str, limit: int | None = None, after: str | None = None) -> dict:
    """The workspace's timers, a page of them. Raises `Refused` or `Unsent`."""
    return get(_paged("/timers", limit, after), token)


def timer_read(token: str, timer: str) -> dict:
    """One timer as it stands, with the obligation it carries. Raises `Refused` or `Unsent`."""
    return get(f"/timers/{timer}", token)


def timer_history(token: str, timer: str, limit: int | None = None, after: str | None = None) -> dict:
    """The firings of one timer, with their legs and any answer, a page of them. Raises `Refused`
    or `Unsent`."""
    return get(_paged(f"/timers/{timer}/firings", limit, after), token)


def timer_lodge(session: str, token: str, body: dict, key: str, seat: bool = False) -> dict:
    """A session lodges a timer on its own command route, or — `seat` — the holder lodges one on the
    seat's route at the generation it holds, which `body` must not name itself. `key` is the
    command's occurrence, so a retry after a lost answer is answered from the first. Raises
    `NotSeated`, `Refused` or `Unsent`."""
    if seat:
        body = {"generation": seat_generation(session, token), **body}
        return post(f"/sessions/{session}/seat/timers", body, key, SESSION_CARRIER, token)
    return post(f"/sessions/{session}/timers", body, key, SESSION_CARRIER, token)


def timer_change(session: str, token: str, timer: str, body: dict, key: str, seat: bool = False) -> dict:
    """A session cancels or snoozes its timer on the change route, or — `seat` — the holder does at
    the generation it holds. Raises `NotSeated`, `Refused` or `Unsent`."""
    if seat:
        body = {"generation": seat_generation(session, token), **body}
        return post(f"/sessions/{session}/seat/timers/{timer}/changes", body, key, SESSION_CARRIER, token)
    return post(f"/sessions/{session}/timers/{timer}/changes", body, key, SESSION_CARRIER, token)


def timer_answer(session: str, token: str, body: dict, key: str, seat: bool = False) -> dict:
    """The recipient answers a delivery by its exact id on the answers route, or — `seat` — the
    holder does at the generation it holds. Raises `NotSeated`, `Refused` or `Unsent`."""
    if seat:
        body = {"generation": seat_generation(session, token), **body}
        return post(f"/sessions/{session}/seat/timers/answers", body, key, SESSION_CARRIER, token)
    return post(f"/sessions/{session}/timers/answers", body, key, SESSION_CARRIER, token)


def seat_acquire(session: str, token: str, vacancy_generation: int) -> dict:
    """The session takes the seat while it is vacant, naming the vacancy it saw
    (go-unit9-authority-wire-design.md). Raises `Refused` or `Unsent`."""
    return post(f"/sessions/{session}/seat/acquisition", {"vacancy_generation": vacancy_generation},
                _own_key("seat.acquire", session, str(vacancy_generation)), SESSION_CARRIER, token)


def seat_hand(session: str, token: str, generation: int, to: str, to_epoch: int) -> dict:
    """The holder hands the seat to the session `to`, standing at `to_epoch`. Raises `Refused` or `Unsent`."""
    return post(f"/sessions/{session}/seat/handover", {"generation": generation, "to": to, "to_epoch": to_epoch},
                _own_key("seat.hand", session, str(generation), to, str(to_epoch)), SESSION_CARRIER, token)


def seat_renew(session: str, token: str, generation: int) -> dict:
    return post(f"/sessions/{session}/seat/renewal", {"generation": generation}, uuid.uuid4().hex, SESSION_CARRIER, token)


def seat_read(session: str, token: str, directives: list[str], seat_generation: int | None, ws: Path) -> None:
    """Record foreground frames this native session returned to its main loop.

    The Go authority, unlike the incumbent, records each semantic proof under a stable key. A
    returned seat generation is its frame's identity and must not be replaced by a newer read.
    """
    import ack  # noqa: E402
    if (ack.records(ws).get(session) or {}).get("authority") != "coordination":
        return
    ids = sorted({str(uuid.UUID(directive)) for directive in directives})
    if not ids and seat_generation is None:
        return
    generation = seat_generation if seat_generation is not None else globals()["seat_generation"](session, token)
    chunks = [ids[i:i + 16] for i in range(0, len(ids), 16)] or [[]]
    for i, directives in enumerate(chunks):
        seat = seat_generation is not None and i == 0
        body = {"generation": generation, "read": {"directives": directives, "seat": seat}}
        key = _own_key("seat.read", session, str(generation), str(seat), *directives)
        post(f"/sessions/{session}/seat/renewal", body, key, SESSION_CARRIER, token)


def seat_release(session: str, token: str, generation: int) -> dict:
    """The holder gives the seat up. Raises `Refused` or `Unsent`."""
    return post(f"/sessions/{session}/seat/release", {"generation": generation},
                _own_key("seat.release", session, str(generation)), SESSION_CARRIER, token)


def issue_directive(session: str, token: str, directive: str, to: str, to_epoch: int, generation: int, text: str,
                    checkpoint: str | None = None) -> dict:
    """The holder, at the seat generation it holds, directs the session `to` at `to_epoch`, handing it
    `checkpoint`'s text when it names one. `directive` is a UUID the caller mints once and sends again
    with a retry, which Go answers from the first. Raises `Refused` or `Unsent`."""
    body = {"id": directive, "to": to, "to_epoch": to_epoch, "generation": generation, "body": text}
    if checkpoint:
        body["checkpoint"] = checkpoint
    return write(f"/sessions/{session}/directives", body, _own_key("directive.issue", session, directive), SESSION_CARRIER, token)


def claim_name(token: str, key: str) -> dict:
    """The session claims the permanent name `key`; its own repeat is granted, another's is refused
    `name-claimed`. Raises `Refused` or `Unsent`."""
    return write("/names", {"key": key}, _own_key("name.claim", key), SESSION_CARRIER, token)


def card_repo(ws: Path | None = None) -> str:
    """`owner/name` of the workspace checkout's origin. Go's wire names the repository an anchor
    is in, which the incumbent's door filled in from the workspace's one repository; the client
    names it from the checkout itself, where the branch and the issue numbers come from too."""
    from local_workspace import checkout_slug
    repo = None
    try:
        repo = checkout_slug(ws if ws is not None else Path.cwd(), timeout=5)
    except (OSError, subprocess.SubprocessError):
        pass
    if not repo or not re.fullmatch(r"[^/:\s]+/[^/:\s]+", repo):
        # The origin may embed a credential; its shape is said, never its value.
        raise SystemExit("declare needs the checkout's origin as owner/name to name an anchor's "
                         "repository; this checkout's origin is not in that form")
    return repo


def declare_card(card: str, name: str, anchors: dict, token: str, repo: str = "", triage: dict | None = None,
                 goal: dict | None = None, session: str = "") -> str:
    """The session declares its own card (`POST /cards`), which engages it as executor; Go answers
    a minted id whose declaration is the same one with that card. The key is the declaration's own,
    so a retry after a lost answer never mints a second card. `repo` names the anchors'
    repository, as `card_repo` reads it; `goal`, a birth's `{id, why, source?}`, contributes the
    card to its goal in the same command (#7005). A card `session` already executes that carries
    every anchor is joined instead of declared beside (#7634): its triage is the declaration's, and
    a goal, which joins a card only by an edge, is refused naming it. Raises `Refused` or `Unsent`."""
    if session and anchors:
        joined = _join_own_card(session, card, anchors, repo, token, triage, goal)
        if joined:
            return joined
    body: dict = {"id": card, "name": name}
    if anchors:
        body["anchors"] = [{"verb": verb, "repo": repo, "n": n}
                           for verb, ns in sorted(anchors.items()) for n in ns]
    if triage:
        body["triage"] = triage
    if goal:
        body["goal"] = goal
    answer = write("/cards", body, _own_key("declare", card, json.dumps(body, sort_keys=True)),
                  SESSION_CARRIER, token)
    line = f"declared: {card}" + (" (replayed)" if answer.get("replayed") else "")
    if warning := answer.get("warning"):
        line += f"; warned: {warning}"
    return line


def _join_own_card(session: str, minted: str, anchors: dict, repo: str, token: str,
                   triage: dict | None, goal: dict | None) -> str:
    """The line for a declaration that joined the live card `session` already executes with every
    anchor it names, or "" when there is none to join. The card a retry of this very declaration
    minted is no card to join: it falls through to the post, which Go answers replayed."""
    wanted = {(verb, n) for verb, ns in anchors.items() for n in ns}
    held = [c for c in executing_cards(session, token) if c["id"] != minted and wanted <= {
        (a.get("verb"), a.get("n")) for a in c.get("anchors") or []
        if not a.get("ended") and str(a.get("repo")).lower() == repo.lower()}]
    if not held:
        return ""
    ids = ", ".join(c["id"] for c in held)
    if len(held) > 1:
        return refusal.refuse(f"not declared: {session} already executes {len(held)} live cards carrying these anchors ({ids})",
                              hand_to=("the brain", f"say: {session} token <t> which of {ids} is my card for these anchors"))
    joined = held[0]["id"]
    if goal:
        return refusal.refuse(f"not declared: {session} already executes {joined}, which carries these anchors; "
                              f"a goal joins a card by an edge: edge.py {session} link {joined} part-of {goal['id']} <why>",
                              send="/2mw2lt:connect")
    if triage:
        write(f"/cards/{joined}/triage",
              {**triage, "why": f"declared by {session} on the card it already executes"},
              _own_key("declare.triage", joined, triage["criticality"], triage["size"]), SESSION_CARRIER, token)
    return f"bound: {joined}, the card {session} already executes for these anchors"


def edge_card(card: str, unlink: bool, kind: str, to: dict, why: str, token: str,
              how: str = "", source: str = "") -> str:
    """The session links, or ends a link on, the card it executes (`POST /cards/{card}/links` or
    `.../unlinks`). The key is the edge's own, so a retry after a lost answer is answered from the
    first rather than drawn twice. Raises `Refused` or `Unsent`."""
    body: dict = {"kind": kind, "why": why}
    body.update(to)
    if how:
        body["how"] = how
    if source:
        body["source"] = source
    verb = "unlink" if unlink else "link"
    write(f"/cards/{card}/{'unlinks' if unlink else 'links'}", body,
         _own_key(verb, card, json.dumps(body, sort_keys=True)), SESSION_CARRIER, token)
    return f"{verb}ed: {card} {kind}"


def get(path: str, token: str, timeout: float = 10.0) -> dict:
    """One read on the session's carrier, asked again while Go answers 429 or 503, under the
    bounded backoff a write uses and honouring Retry-After: a read repeats safely, and a wait
    Go names is waited out before the answer is called refused. A read the wire lost is asked
    again within one budget the call owns, one line of progress per ask (#5974). Raises
    `Refused` or `Unsent` as a write does."""
    started, attempts = time.monotonic(), 0
    again = door.resender(started, f"session: {path}", action="asking again")
    renewed = False
    while True:
        req = urllib.request.Request(_base() + path, headers=session_headers(token))
        try:
            with door.open_direct(req, timeout) as r:
                return json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            problem = _problem(e)
            if not renewed and _renewed(e, problem, req.get_header(MACHINE_CARRIER.capitalize())):
                renewed = True
                continue
            paced = (e.headers.get("Retry-After") or "").strip() if e.headers else ""
            pause = _retry_after(e) if e.code in (429, 503) and paced else None
            if pause is None:
                if e.code in GATEWAY_DOWN and not (e.code == 503 and paced) \
                        and (wait := again(f"the gateway answered {e.code}")) is not None:
                    time.sleep(wait)
                    continue
            else:
                attempts += 1
                if attempts < RETRY_ATTEMPTS and time.monotonic() - started + pause <= RETRY_LIMIT:
                    time.sleep(pause)
                    continue
            raise Refused(e.code, problem.get("code") or "unexplained", problem.get("field") or "",
                          request=_request(e, problem), remedy=_remedy(problem)) from None
        except (urllib.error.URLError, OSError, TimeoutError, http.client.HTTPException, ValueError) as e:
            if (wait := again(f"no answer ({e})")) is not None:
                time.sleep(wait)
                continue
            raise Unsent(f"{_base()}{path}: no answer ({e})") from None


def machines_here(token: str) -> str:
    """The workspace's machines as a launch's `on` may name them, for a refusal of one it named
    wrongly: each by its name and registration, or its registration alone when it reports no name."""
    try:
        items = get("/machines", token).get("items") or []
    except (Refused, Unsent) as e:
        return f"the machines here could not be read ({e})"
    named = [f"{m['name']} ({m['id']})" if m.get("name") else m["id"] for m in items if m.get("id")]
    return f"machines here: {', '.join(named)}" if named else "no machine reaches this workspace"


def cards_in(state: str, token: str):
    """Every card of a state, page by page: a hundred to a page, and the cards route has no filter
    for a session's own. A read that stops at the first page misses every card past it."""
    page = f"/cards?state={state}"
    while page:
        listed = get(page, token)
        yield from listed.get("cards") or []
        nxt = listed.get("next")
        page = f"/cards?state={state}&next=" + urllib.parse.quote(nxt, safe="") if nxt else ""


def live_cards(token: str):
    return cards_in("live", token)


def executing_cards(session: str, token: str) -> list[dict]:
    """Every live card this session executes — its executor engagements, not merely planned ones —
    in the cards read's order: what a branch claim chooses among when it names no card."""
    out = []
    for card in live_cards(token):
        # Live, not merely unended: an earlier epoch's engagement stays unended. An engagement
        # read without `live` is not taken, so no card's title is guessed (#4469).
        if any(e.get("session") == session and e.get("role") == "executor" and e.get("live") is True
               for e in card.get("executors") or []):
            out.append(card)
    return out


class ConcludedClaim(ValueError):
    """A claim found no live card because the card its session executes is concluded; the message
    already says what to do."""


def concluded_executing(session: str, token: str) -> list[dict]:
    """The concluded cards a session still holds a live executor engagement on: the cards read
    behind a claim leaves a concluded card out, and it is the only reason a session that executes
    a card can find none to claim on."""
    return [c for c in cards_in("concluded", token)
            if any(e.get("session") == session and e.get("role") == "executor" and e.get("live") is True
                   for e in c.get("executors") or [])]


def executing_card(session: str, token: str) -> dict | None:
    """The live card this session executes — its executor engagement, not a merely planned one —:
    what a branch claim's announce defaults its doing to, the brain having ruled there is no
    separate claim primitive (2026-10-08). Of several, the first the cards read answers."""
    cards = executing_cards(session, token)
    return cards[0] if cards else None


def status(session: str, token: str, key: str, body: dict) -> dict:
    """The session's own status on Go's route (EL1a slice 2), under the invocation's occurrence id:
    a resend through `--retry` is answered from the first, and a fresh invocation writes a new row.
    Raises `Refused` or `Unsent`."""
    return write(f"/sessions/{session}/status", body, key, SESSION_CARRIER, token)


def wait(session: str, token: str, key: str, body: dict) -> dict:
    """The session's own wait on Go's route (go-waits-design.md decision 8), under the invocation's
    occurrence id: a resend through `--retry` answers the wait the first registered. Raises
    `Refused` or `Unsent`."""
    return write(f"/sessions/{session}/waits", body, key, SESSION_CARRIER, token)


def cancel_wait(session: str, token: str, key: str, wait: str) -> dict:
    """The session ends one of its own open waits on Go's cancellation route, under the invocation's
    occurrence id: a resend through `--retry` is answered from the first. Raises `Refused` or `Unsent`."""
    return write(f"/sessions/{session}/waits/{wait}/cancellation", {}, key, SESSION_CARRIER, token)


def raise_need(token: str, key: str, body: dict) -> dict:
    """The session's ask or recommendation on Go's needs route (EL4 decision 7), under the
    invocation's occurrence id. Raises `Refused` or `Unsent`."""
    return write("/needs", body, key, SESSION_CARRIER, token)


def announce_branch(session: str, token: str, branch: str, ws: Path, key: str,
                    harness: str = "", account: str = "", card: dict | None = None,
                    resolved: dict | None = None, repo: str = "") -> str:
    """A branch claim is an announce (the brain's ruling, 2026-10-08): the doing defaults to the
    executed card's title, and the repository names the branch, which binds it to the card. A card
    given (`card`, one this session executes as the cards read answers) is named to the claim,
    which binds the branch to it over the card the session engaged last (#5352); `resolved` is the
    same read's answer handed in so the cards are read once, naming no card to the claim. `repo`
    names a branch in another of the team's repositories, the checkout's origin otherwise. There
    is no separate claim primitive on the wire. Answers the card it bound and the branch it
    displaced, or `REJECTED` with why nothing bound (#5084). Raises `ValueError` when no card names
    doing, `Refused` or `Unsent` as a write does."""
    named = card is not None
    card = card or resolved or executing_card(session, token)
    if card is None:
        concluded = concluded_executing(session, token)
        if concluded:
            raise ConcludedClaim("; ".join(
                f"{c.get('id')} ({c.get('name')}) is concluded, so a branch cannot be claimed on it" for c in concluded)
                + ": the seat can unconclude it, or declare a new card")
        raise ValueError(f"no live card of {session}'s names doing to announce")
    body: dict = {"state": "announce", "doing": card["name"], "branch": branch, "repo": repo or card_repo(ws)}
    if named:
        body["card"] = card["id"]
    if harness:
        body["harness"] = harness
    if account:
        body["account"] = account
    answer = status(session, token, key, body)
    bound = answer.get("bound")
    if bound:
        return f"bound: {bound['branch']} to card {bound['card']}" + (
            f", displacing {bound['displaced']}" if bound.get("displaced") else "")
    why = answer.get("unbound")
    if why == "held-by-a-live-session":
        # The card is this session's now, but another session still enrolled bound its branch and
        # works it (#7678): name that session, and the claim on a card of this session's own.
        holders = sorted({b["by"]["session"] for b in card.get("branches") or []
                          if not b.get("ended") and (b.get("by") or {}).get("kind") == "session"
                          and (b.get("by") or {}).get("session") not in (None, session)})
        who = ", ".join(holders) or "another session"
        return refusal.refuse(f"not bound: card {card['id']} ({card['name']}) holds a branch {who} bound and still works",
                              send=[(f"taking: branch {branch} --card <id> token <t>",
                                     "naming a card of your own, which the brain engages you on if you have none")])
    if why:
        if why in UNBOUND:
            return refusal.refuse(f"not bound: {why}", send=UNBOUND[why])
        return refusal.refuse(f"not bound: {why}",
                              hand_to=("the brain", f"say: {session} token <t> my branch claim bound no card, {why}"))
    return f"announced on {branch}; the daemon did not say what it bound"


# Why an announce's branch claim bound nothing, and what the session sends instead (#5084). The
# others, the owner or the seat having taken the branch from the card or another card holding it,
# are the brain's to settle.
UNBOUND = {
    "no-executed-card": [("declare: <session> token <t> card <ulid> <name> [<verb>:<n>[,<n>] …]",
                          "first, then claim the branch again")],
    "trunk": [("taking: branch <name> token <t>", "for the branch the work is on, since a trunk is never a card's")],
    "card-unknown": [("taking: branch <name> --card <id> token <t>",
                      "with a card id as the cards read answers it, for no card holds the one named")],
    "card-not-executed": [("taking: branch <name> --card <id> token <t>",
                           "with a card this session executes, declaring the card first if it should execute another")],
    "repository-unheld": [("taking: branch <name> in <owner>/<name> token <t>",
                           "naming a repository one of the team's workspaces serves or its sources read")],
}


def issue_cards(cards, repo: str, n: int) -> list[dict]:
    """The live cards among `cards` that resolve `repo#n`, by an anchor still standing."""
    return [c for c in cards
            if any(a.get("verb") == "resolves" and a.get("n") == n and str(a.get("repo")).lower() == repo.lower()
                   and not a.get("ended") for a in c.get("anchors") or [])]


def _executing(card: dict, session: str | None = None) -> bool:
    return any(e.get("role") == "executor" and e.get("live") is True and (session is None or e.get("session") == session)
               for e in card.get("executors") or [])


def claim_issue(session: str, token: str, repo: str, n: int, key: str = "") -> str:
    """An issue claim engages the session on the issue's live card, taking it as its executor: the
    card the session already executes is reported bound, and the one live card resolving the issue
    is taken when no session executes it — a seat-directed worker joins the card it was sent to
    rather than minting a duplicate beside it (#6846, #7188). Taking a card from its present
    executor is the seat's or the owner's, never a claim's (Go's `card-executed`), so a card
    another session executes is named with its executor for the brain to place, and with several
    live cards resolving the issue the claim names them and refuses, guessing none. Only when no
    live card resolves the issue does the claim declare one with `resolves:<n>`, named for the
    issue and the session its executor; the declared card's id and
    key are the claim's own, so a rerun after a lost answer is answered from the first rather
    than minting a second permanent card. A take is keyed by its invocation (`key`, the occurrence
    id), never by the pair: a take, the seat's disengagement and a take again are three commands,
    and a permanent key would replay the first and report a binding that no longer stands. Raises
    `Refused` or `Unsent`."""
    resolving = issue_cards(live_cards(token), repo, n)
    mine = next((c for c in resolving if _executing(c, session)), None)
    if mine:
        return f"bound: {mine['id']}, the live card of {repo}#{n}, which {session} already executes"
    if len(resolving) == 1:
        card = resolving[0]
        executors = ", ".join(str(e.get("session")) for e in card.get("executors") or []
                              if e.get("role") == "executor" and e.get("live") is True)
        if executors:
            return refusal.refuse(f"not taken: {card['id']}, the live card of {repo}#{n}, is executed by {executors}",
                                  hand_to=("the brain", f"say: {session} token <t> the card of {repo}#{n} is {card['id']}, "
                                                       f"executed by {executors}; place me on it or leave it theirs"))
        write(f"/cards/{card['id']}/engagements", {"session": session, "role": "executor"},
              _own_key("claim.take", session, card["id"], key), SESSION_CARRIER, token)
        return f"bound: {card['id']}, the live card of {repo}#{n}"
    if resolving:
        ids = ", ".join(c["id"] for c in resolving)
        return refusal.refuse(f"not taken: {len(resolving)} live cards resolve {repo}#{n} ({ids})",
                              hand_to=("the brain", f"say: {session} token <t> which of {ids} do I take for {repo}#{n}"))
    card = _own_key("claim.card", session, repo, str(n))[:24]
    body = {"id": card, "name": f"{repo}#{n}",
            "anchors": [{"verb": "resolves", "repo": repo, "n": n}]}
    answer = write("/cards", body, _own_key("claim", session, repo, str(n)), SESSION_CARRIER, token)
    return f"declared: {card}" + (" (replayed)" if answer.get("replayed") else "")


def read_seat(token: str) -> dict:
    """The seat as Go holds it, for #4404's client verbs."""
    return get("/seat", token)


class NotSeated(Exception):
    """The session does not hold the seat on Go: the holder is another, or nobody."""


def seat_generation(session: str, token: str) -> int:
    """The seat generation `session` holds, as Go reads it now. Go has no lease token: a seat verb
    is the holder's own credential at this generation, and a command at a stale one is refused.
    Raises `NotSeated`, `Refused` or `Unsent`."""
    seat = read_seat(token)
    if seat.get("holder") != session:
        raise NotSeated("nobody holds the seat" if seat.get("holder") is None
                        else "this token does not hold the lease")
    return int(seat["generation"])


# A goal's steward is lent these card verbs on what its stewardship covers (doc 198 §3): a scope
# only when born part of a goal (#8121).
STEWARD_CARD_VERBS = ("retier", "scope")
# Go's refusal of a session that steers nothing covering what it acts on: the refusal each lent
# operation keeps for a session holding neither the seat nor a stewardship.
_NOT_STEWARD = frozenset({"seat-not-holder", "executor-required", "seat-required"})


def lent(session: str, token: str, send):
    """`send(generation)` for a verb doc 198 lends a goal's steward. The holder sends it at the
    generation it holds; any other session sends it at 0 for Go to judge its stewardship, and Go's
    refusal of one that steers nothing is raised as `NotSeated`, as the seat check would have."""
    try:
        generation = seat_generation(session, token)
    except NotSeated as why:
        try:
            return send(0)
        except Refused as e:
            if e.code in _NOT_STEWARD:
                raise NotSeated(f"{why}, and it steers no goal covering this ({e.code})") from None
            raise
    return send(generation)


def standing_epoch(token: str, name: str) -> int | None:
    """The epoch `name` stands at, from the fleet the brain reads, or None when it is not enrolled."""
    for item in get("/sessions", token).get("items") or []:
        if item.get("name") == name:
            return int(item["epoch"])
    return None


def relay(session: str, token: str, to: str, epoch: int | None, text: str, key: str, checkpoint: str | None = None) -> str:
    """The holder's directive to `to`, at `epoch` or the one it stands at, or a goal's steward's to a
    session its launches started, at generation 0, which no seat holds (doc 198 D6), handing it
    `checkpoint`'s text when it names one. `key` is the send's occurrence id, so a retry after a lost
    answer is answered from the first. Raises `NotSeated`, `Refused` or `Unsent`."""
    if epoch is None:
        epoch = standing_epoch(token, to)
        if epoch is None:
            raise Refused(404, "session-not-enrolled", "to")
    directive = str(uuid.UUID(_own_key("relay.directive", session, key)))
    answer = lent(session, token, lambda generation: issue_directive(session, token, directive, to, epoch, generation, text, checkpoint))
    # A steward's directive to a busy session waits behind the one it holds (doc 198 D6).
    if answer.get("status") == "queued":
        return f"queued: {answer.get('id', directive)} for {to}@{epoch}, behind the directive it holds"
    return f"relayed: {answer.get('id', directive)} to {to}@{epoch}"


def seat_need(session: str, token: str, kind: str, need: str, reason: str = "", words: str = "", channel: str = "") -> str:
    """The holder promotes one of the brain's Needs You rows to the owner, disposes of it with a
    reason the owner can read, or records on one of the owner's rows the answer the owner gave
    somewhere else, as `words` given `channel` (#6325) (`POST /needs/{need}/transitions`). The key
    is the transition's own, so a rerun after a lost answer is answered from the first. Raises
    `NotSeated`, `Refused` or `Unsent`."""
    seat_generation(session, token)
    body = {"kind": kind, "reason": reason}
    if kind == "out-of-band":
        body = {"kind": kind, "words": words, "channel": channel}
    write(f"/needs/{need}/transitions", body, _own_key("seat.need", session, need, kind, reason, words, channel), SESSION_CARRIER, token)
    if kind == "out-of-band":
        return f"answered out of band: {need}"
    return f"{kind}d: {need}"


def seat_launch(session: str, token: str, body: dict, key: str) -> str:
    """The holder, or a goal's steward by floor for its goal's cards, asks capacity for a worker
    (`POST /launches`), keyed by the line's own key so a rerun after a lost answer is answered from
    the first. Raises `NotSeated`, `Refused` or `Unsent`."""
    answer = lent(session, token, lambda _: write("/launches", body, key, SESSION_CARRIER, token))
    account = answer.get("account")
    harness = f" {body['harness']}" if body.get("harness") else ""
    on = f" on {account['vendor']} account {account['number']} ({account['account_id']}, brought by {account['brought_by']})" if account else ""
    preferred = answer.get("preferred_harness")
    nudge = (f" — {preferred['machine']} prefers {preferred['harness']} for {preferred['vendor']}"
             "; leave the harness out to let placement choose") if preferred else ""
    if answer.get("outcome") == "refused":
        return f"launch refused: {answer['id']}{harness}{on}: {answer.get('refused')}"
    if answer.get("outcome") == "session":
        return f"launched: {answer['id']}{harness}{on}{_overloaded(answer.get('host_overloaded'))}{nudge}"
    if answer.get("state") == "admitted":
        return f"launching: {answer['id']}{harness}{on}{_overloaded(answer.get('host_overloaded'))}{nudge}"
    if answer.get("state") == "withdrawn":
        # Ended at its publish, a pinned account barred or off, its pinned machine gone: not waiting.
        return f"launch withdrawn: {answer['id']}{harness}{on}: {answer.get('reason')}"
    return f"launch waiting: {answer['id']} {answer.get('reason') or answer.get('state')}{on}{_reclaimable(answer.get('reclaimable'))}"


def _overloaded(host: dict | None) -> str:
    """The measured load of an overloaded host the launch was placed on, as the brain reads it."""
    if not host:
        return ""
    said = []
    if host.get("load_five") is not None and host.get("logical_cores"):
        said.append(f"load {host['load_five']:.1f}/{host['logical_cores']}")
    if host.get("memory_used") is not None:
        said.append(f"memory {host['memory_used'] * 100:.0f}%")
    return "; host-overloaded: " + ", ".join(said)


def _reclaimable(sessions: list | None) -> str:
    """The sessions a launch waiting for room names, each as its standing reads: kept, with until and
    why, or what it is doing since when, and stale."""
    if not sessions:
        return ""
    said = []
    for s in sessions:
        st = s["standing"]
        if st.get("keep"):
            keep = st["keep"]
            words = f"kept until {keep['until'][11:16]} UTC{', over' if keep['over'] else ''}: {keep['reason']}"
        else:
            words = st.get("activity") or "never read"
            if st.get("since"):
                words += f" since {st['since'][11:16]} UTC"
            if st.get("stale"):
                words += ", stale"
        said.append(f"{s['name']} ({words})")
    return "; reclaimable: " + ", ".join(said)


def seat_withdraw(session: str, token: str, launch: str, key: str) -> str:
    """The holder withdraws a launch still waiting (`POST /launches/{launch}/withdrawal`), keyed by the
    line's own key. Raises `NotSeated`, `Refused` or `Unsent`."""
    seat_generation(session, token)
    answer = write(f"/launches/{launch}/withdrawal", {}, key, SESSION_CARRIER, token)
    return f"withdrawn: {answer.get('id', launch)}"


def seat_cancel(session: str, token: str, action: str, key: str) -> str:
    """The holder withdraws an action still only offered (`POST /actions/{action}/cancel`), keyed by
    the line's own key. Raises `NotSeated`, `Refused` or `Unsent`."""
    seat_generation(session, token)
    answer = write(f"/actions/{action}/cancel", {}, key, SESSION_CARRIER, token)
    return f"cancelled: {answer['kind']} {answer.get('id', action)}"


def seat_action(session: str, token: str, body: dict, key: str) -> str:
    """The holder asks the machine a session runs on to wake, control, retire, rehome, park or interrupt it
    (`POST /actions`), keyed by the line's own key so a rerun after a lost answer is answered from
    the first. A goal's steward wakes, parks, retires and interrupts the sessions its launches started. Raises
    `NotSeated`, `Refused` or `Unsent`."""
    answer = lent(session, token, lambda _: write("/actions", body, key, SESSION_CARRIER, token))
    doing = {"wake": "waking", "control": "controlling", "retire": "retiring", "rehome": "rehoming", "park": "parking", "interrupt": "interrupting"}[body["kind"]]
    value = f" {body['value']}" if body.get("value") else ""
    # A retire whose machine's credential no longer reaches the workspace is ended by the seat's
    # own command (#7227), not offered to a machine: the answer's state says which happened.
    state = {"pending": "offered", "retired": "ended: its machine is gone"}.get(answer["state"], answer["state"])
    return f"{doing}: {answer['id']} {body['to']}{value}, {state}"


def seat_keep(session: str, token: str, verb: str, body: dict, key: str) -> str:
    """The holder keeps a session idle on purpose (`POST /sessions/{session}/keep`) or clears its
    keep (`.../unkeep`), under its own session and keyed by the line's own key, so a rerun after a
    lost answer is answered from the first. A goal's steward keeps the sessions its launches started.
    Raises `NotSeated`, `Refused` or `Unsent`."""
    answer = lent(session, token, lambda _: write(f"/sessions/{session}/{verb}", body, key, SESSION_CARRIER, token))
    if verb == "unkeep":
        return f"unkept: {answer['session']}, kept for {answer['reason']}"
    return f"kept: {answer['session']} until {answer['until']} because {answer['reason']}"


def seat_regroup(session: str, token: str, reason: str, request: str, key: str) -> str:
    """The holder asks for a regroup by hand (`POST /sessions/{session}/regroups`). `request` names
    the invocation, so a resend under it returns the regroup the first raised or joined. Raises
    `NotSeated`, `Refused` or `Unsent`."""
    seat_generation(session, token)
    answer = write(f"/sessions/{session}/regroups", {"reason": reason, "request_id": request}, key, SESSION_CARRIER, token)
    how = "raised" if answer["status"] == "created" else "joined the day's pending"
    return (f"regroup: {how} regroup {answer['regroup']}; run the regroup, then "
            f"`answer: token @lease {answer['episode']} acted|considered because <why>`")


def seat_answer(session: str, token: str, episode: str, body: dict, key: str) -> str:
    """The holder answers a snapshot (`POST /sessions/{session}/signals/{episode}/answer`), under its
    own session and keyed by the line's own key, so a rerun after a lost answer is answered from the
    first. Raises `NotSeated`, `Refused` or `Unsent`."""
    seat_generation(session, token)
    answer = write(f"/sessions/{session}/signals/{episode}/answer", body, key, SESSION_CARRIER, token)
    if answer["status"] == "superseded":
        return f"answered: {episode}, recorded; a newer snapshot is the one still to answer"
    return f"answered: {episode}, {body['verdict']}"


def intervened(session: str, token: str, card: str, cause: str, why: str, key: str) -> str:
    """The holder records an act no verb sees (`POST /interventions`) at its seat generation.
    `key` is the line's occurrence, so a resend after a lost answer records nothing twice.
    Raises `NotSeated`, `Refused` or `Unsent`."""
    body = {"card": card, "cause": cause, "why": why, "generation": seat_generation(session, token)}
    write("/interventions", body, _own_key("intervention.record", session, key), SESSION_CARRIER, token)
    return f"intervened: {card} {cause}"


def lift(session: str, token: str, kind: str, repo: str, at: int | str, reason: str, key: str) -> str:
    """The holder lifts a gate series one round past its ceiling (`POST /gates/lifts`): a review's
    by its pull request, a critique's by its branch, at the generation it holds. `key` is the line's
    occurrence id, so a resend after a lost answer is answered from the first rather than lifting
    twice. Raises `NotSeated`, `Refused` or `Unsent`."""
    generation = seat_generation(session, token)
    body = {"kind": kind, "repo": repo, "generation": generation, "reason": reason,
            **({"pr": at} if kind == "review" else {"branch": at})}
    write("/gates/lifts", body, _own_key("gate.lift", session, key), SESSION_CARRIER, token)
    return f"lifted: review {repo}#{at}" if kind == "review" else f"lifted: critic {repo} {at}"


def abandon(session: str, token: str, target: str, epoch: int, reason: str, key: str) -> str:
    """The holder abandons an ended epoch no machine observed running a model
    (`POST /gates/abandonments`) at the generation it holds. `key` is the line's occurrence id, so a
    resend after a lost answer is answered from the first. Raises `NotSeated`, `Refused` or `Unsent`."""
    body = {"session": target, "epoch": epoch, "generation": seat_generation(session, token), "reason": reason}
    write("/gates/abandonments", body, _own_key("gate.abandon", session, key), SESSION_CARRIER, token)
    return f"abandoned: {target} epoch {epoch}"


# The parts a bench is kept per (go-gate-failure-attribution-design.md §2), as a lift names them.
BENCH_DIMENSIONS = ("model", "route", "account", "machine", "signed-out")


def bench_lift(session: str, token: str, first: str, second: str, reason: str, key: str) -> str:
    """The holder ends a bench (`POST /gates/bench-lifts`) at the generation it holds: one part's, as
    `<dimension> <key>` names it in the bench's reason, or a judge's by its vendor and model. `key` is
    the line's occurrence id, so a resend after a lost answer is answered from the first. Raises
    `NotSeated`, `Refused` or `Unsent`."""
    names = {"dimension": first, "key": second} if first in BENCH_DIMENSIONS else {"vendor": first, "model": second}
    body = {**names, "generation": seat_generation(session, token), "reason": reason}
    write("/gates/bench-lifts", body, _own_key("gate.bench.lift", session, key), SESSION_CARRIER, token)
    return f"lifted: bench {first} {second}"


def raise_owner(session: str, token: str, title: str, text: str, need: str = "", about: str = "") -> int:
    """The holder raises every member's subscribed browser (`POST /sessions/{session}/push/raises`)
    at the generation it holds, and answers how many pushes Go queued. Each raise is its own: a
    resend after a lost answer is refused for the window the first spent, never sent twice.
    Raises `NotSeated`, `Refused` or `Unsent`."""
    body = {"generation": seat_generation(session, token), "title": title, "text": text,
            **({"need": need} if need else {}), **({"session": about} if about else {})}
    return int(post(f"/sessions/{session}/push/raises", body, uuid.uuid4().hex, SESSION_CARRIER, token)["enqueued"])


# A card verb on Go: the work route it posts to, and its body from the verb's own fact
# (`card_grammar.built`). A verb absent here is one this client sends no write for.
def _card_write(fact: dict, repo: str) -> tuple[str, dict] | None:
    state, card = fact["state"], fact["card"]
    if state == "card-scoped":
        body: dict = {"id": card, "name": fact["name"], "track": fact["track"]}
        anchors = fact.get("anchors") or {}
        if anchors:
            body["anchors"] = [{"verb": v, "repo": repo, "n": n} for v, ns in sorted(anchors.items()) for n in ns]
        if fact.get("major"):
            body["major"] = True
        if fact.get("triage"):
            body["triage"] = fact["triage"]
        if fact.get("goal"):
            body["goal"] = fact["goal"]
        return "/cards", body
    if state in ("card-branch", "card-unbranch"):
        # An ending carries the reason every ending records; Go refuses its absence.
        return (f"/cards/{card}/{'associations' if state == 'card-branch' else 'dissociations'}",
                {"branch": {"repo": fact["repo"], "branch": fact["branch"]},
                 **({"why": fact["why"]} if fact.get("why") else {})})
    if state == "card-session":
        return (f"/cards/{card}/engagements",
                {"session": fact["session"], "role": fact["role"],
                 **({"stage": fact["stage"]} if fact.get("stage") else {}),
                 **({"why": fact["why"]} if fact.get("why") else {})})
    if state == "card-retiered":
        return f"/cards/{card}/stages/{fact['stage']}/retier", {"floor": fact["floor"], "why": fact["why"]}
    if state == "card-unsession":
        return f"/cards/{card}/disengagements", {"session": fact["session"],
                                                 **({"why": fact["why"]} if fact.get("why") else {})}
    if state == "card-concluded":
        return f"/cards/{card}/conclusion", {"evidence": fact["evidence"], **({"keep_branch": True} if fact.get("kept") else {})}
    if state == "card-unconcluded":
        return f"/cards/{card}/unconclusion", {"why": fact.get("why", "")}
    if state == "card-retired":
        return f"/cards/{card}/retirement", {"why": fact["why"]}
    if state == "card-reclassified" and fact["field"] == "state" and fact["after"] == "live":
        return f"/cards/{card}/revival", {"why": fact["why"]}
    if state == "card-reclassified" and fact["field"] == "track":
        return f"/cards/{card}/track", {"track": fact["after"], "why": fact["why"]}
    if state == "card-reclassified" and fact["field"] == "major" and fact["after"] in ("major", "ordinary"):
        return (f"/cards/{card}/corrections",
                {"field": "major", "after": "true" if fact["after"] == "major" else "false", "why": fact["why"]})
    if state == "card-reclassified" and fact["field"] in ("name", "priority"):
        return f"/cards/{card}/corrections", {"field": fact["field"], "after": fact["after"], "why": fact["why"]}
    if state in ("card-link", "card-unlink"):
        body = {k: fact[k] for k in ("kind", "to_card", "to_issue", "to_goal", "why", "how", "source") if k in fact}
        return f"/cards/{card}/{'links' if state == 'card-link' else 'unlinks'}", body
    return None


# What the seat ruled Go will not keep (2026-10-09, #4812): refused with why, never as a verb Go
# is said to lack.
RULED_OUT = {
    "card reclassify significance": "a Go card has no significance, and nothing on Go reads one (#4812)",
    "card outcome": "Go has no outcome brief (#4812)",
    "effort": "a Go card has no effort (#4812)",
    "authorship establish": "Go attests a model only from what the machine observed (#4812)",
}


def ruled_out(what: str) -> str | None:
    """The seat's refusal of `what`, a verb Go will not keep, or None. The seat cannot add what Go
    does not hold; only the owner reverses the ruling."""
    reason = RULED_OUT.get(what)
    return None if reason is None else refusal.refuse(
        reason, hand_to=("the owner", f"ask: <session> token <t> reopen {reason}"), to="seat")


def seat_card(session: str, token: str, verb: str, fact: dict, repo: str, key: str) -> str:
    """The seat's card verb on Go's work routes, which run it as the seat while this session holds
    it. `key` is the invocation's occurrence: a resend under `--retry` is answered from the first,
    and a fresh invocation is a fresh write, so the same verb after its undo is made again.
    Raises `NotSeated`, `Refused`, `Unsent`, or `LookupError` for a verb this client sends no write for."""
    if fact["state"] == "card-reanchored":
        return seat_reanchor(session, token, fact, repo)
    planned = _card_write(fact, repo)
    if planned is None:
        what = f"{verb} {fact['field']}" if fact["state"] == "card-reclassified" else verb
        raise LookupError(f"card {what}: this client sends Go no write for it")
    path, body = planned
    send = lambda _: write(path, body, _own_key("seat.card", session, key), SESSION_CARRIER, token)
    if verb == "scope" and fact.get("goal"):
        # A scope into a goal from a session not holding the seat is its goal's steward's, or else
        # Go makes it a declaration (doc 198 §3): the card's author is said, not assumed.
        try:
            send(seat_generation(session, token))
        except NotSeated:
            by = (send(0).get("card") or {}).get("by") or {}
            if by.get("kind") != "steward":
                return f"declared: card {fact['card']}, by {by.get('kind', 'unknown')} {by.get('session', '')}".rstrip()
    elif verb in STEWARD_CARD_VERBS and verb != "scope":
        lent(session, token, send)
    else:
        seat_generation(session, token)
        send(0)
    return f"carded: card {fact['card']}"


def over_branches(token: str, card: str, rest: list[str]) -> str:
    """Go's `conclusion-over-branches`, said as Python said it (cards.py _conclusion_over_branches):
    the branches the card still holds, and the lines that conclude it. A merge Go has not read yet
    reads as no merge, so concluding with the branch kept is the line for that too: the card lands
    once Go reads the merge."""
    try:
        branches = [b for b in get(f"/cards/{card}", token).get("card", {}).get("branches") or []
                    if not b.get("ended") and b.get("repo") and b.get("branch")]
    except (Refused, Unsent):
        branches = []
    held = ", ".join(f"{b['repo']} {b['branch']}" for b in branches) or "a branch"
    return refusal.refuse(
        f"card {card} holds {held}, and Go reads it as not landed: no merge read, or a pull request still open",
        send=[(f"card: token <lease token> conclude {shlex.join(rest)} --branch-is-the-work",
               "if its pull request merged before Go read it, or the branch is still the work"),
              *((f"card: token <lease token> unbranch {card} {b['repo']} {b['branch']} <why>", "if the work landed another way")
                for b in branches)],
        to="seat")


def seat_reanchor(session: str, token: str, fact: dict, repo: str) -> str:
    """The seat's reanchor on Go, which has no replacement of a card's anchors: each live anchor the
    fact does not name is ended with its why, and each it names that the card lacks is added. A
    rerun reads the card again and sends only what is still missing, so each send is its own
    request: a key from the body alone would answer a later reanchor that restores an anchor from
    the record of the first, leaving it ended. Raises `NotSeated`, `Refused` or `Unsent`."""
    seat_generation(session, token)
    card = fact["card"]
    anchors = get(f"/cards/{card}", token).get("card", {}).get("anchors") or []
    live = {(a["verb"], a["repo"], int(a["n"])) for a in anchors if not a.get("ended")}
    wanted = {(verb, repo, int(n)) for verb, ns in (fact.get("after") or {}).items() for n in ns}
    for verb, in_repo, n in sorted(live - wanted):
        post(f"/cards/{card}/dissociations", {"anchor": {"verb": verb, "repo": in_repo, "n": n}, "why": fact["why"]},
             uuid.uuid4().hex, SESSION_CARRIER, token)
    for verb, in_repo, n in sorted(wanted - live):
        post(f"/cards/{card}/associations", {"anchor": {"verb": verb, "repo": in_repo, "n": n}},
             uuid.uuid4().hex, SESSION_CARRIER, token)
    return f"carded: card {card}"


def _need_line(audience: str, need: dict) -> str:
    what = " ".join(f"{need.get('session') or ''} {need.get('kind')}: {need.get('question') or ''}".split())
    return f"{audience} {need['id']} {what if len(what) <= 200 else what[:199] + '…'}"


def backlog(session: str, token: str) -> str:
    """Every Needs You row Go holds open for the seat and for the owner, one line each, as the
    incumbent's `backlog:` answers: `<brain|owner> <id> <what>`. Go's seat queue is the brain's.
    Raises `NotSeated`, `Refused` or `Unsent`."""
    seat_generation(session, token)
    lines = [_need_line(marker, need) for audience, marker in (("seat", "brain"), ("owner", "owner"))
             for need in get(f"/needs?audience={audience}", token).get("needs") or []]
    return "\n".join(lines) or "backlog: nothing open"


def _roster_line(item: dict) -> str:
    cards = item.get("cards") or []
    told = f"; executing {', '.join(cards)}" if cards else ""
    delivery = ""
    if item["state"] == "enrolled":
        presence = item.get("presence") if item.get("detached_since") is None else None
        if presence == "stream":
            delivery = "; stream held"
        elif presence == "reach":
            delivery = "; reached"
        else:
            from machine_harness import of
            harness = of(item.get("harness"))
            delivery = "; stream down" if harness and harness.holds else "; not reached"
    return f"{item['name']} {item['epoch']} {item['state']} - on {item.get('machine_registration') or item.get('machine') or '-'}{delivery}{told}"


def roster(session: str, token: str, include_gone: bool) -> str:
    """Raises `NotSeated`, `Refused` or `Unsent`."""
    seat_generation(session, token)
    items = get("/sessions?gone=1" if include_gone else "/sessions", token).get("items") or []
    return "\n".join(_roster_line(item) for item in items) or "roster: empty"


def say(session: str, token: str, occurrence: str, text: str, to: str | None = None, *, inform: bool = False) -> str:
    """The session's say on Go (go-session-say-design.md): to `to`, or to the brain when none is
    named. The occurrence makes a resend the same say. The answer names what happened to the
    words: handed to a stream that stood, or spooled for the session's next hold (#5674). Raises
    `Refused` or `Unsent`."""
    body = {"occurrence": occurrence, "text": text, **({"to": to} if to else {}),
            **({"inform": True} if inform else {})}
    answer = write(f"/sessions/{session}/says", body, _own_key("say", session, occurrence), SESSION_CARRIER, token)
    if answer.get("state") == "unheard":
        return f"nobody holds the seat; kept on the brain's Needs You as unheard:{answer.get('id')}"
    if answer.get("held"):
        return f"delivered to {answer.get('to')}@{answer.get('epoch')} as {answer.get('id')}: their stream holds"
    return f"queued for {answer.get('to')}@{answer.get('epoch')} as {answer.get('id')}: queued until they hold"
