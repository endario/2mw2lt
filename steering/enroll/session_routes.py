"""The session commands on Go (C5, documentation/architecture/go-c5-session-client-design.md).

The machine observes and enrols on its own carrier. Session requests present their bearer with this
machine's credential.
"""
from __future__ import annotations

import email.utils
import hashlib
import json
import os
import re
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
import requestlog  # noqa: E402

MACHINE_CARRIER = "X-Steering-Agent-Credential"
SESSION_CARRIER = "X-Steering-Session-Credential"
# A refusal of an invalid token: unknown, expired, or one a detachment revoked, which Go answers
# alike, so the client cannot tell which, only that the token can no longer act; or a stored token
# not shaped as one, which can never act. Both are repaired by connecting again.
INVALID_TOKEN = frozenset({"session-token-locator-read", "session-credential-carrier-shape"})
# How long, and how many times, a throttled or unavailable answer is sent again: past either the
# refusal is reported rather than held, whatever wait Go asked for.
RETRY_LIMIT = 30.0
RETRY_ATTEMPTS = 5


class Refused(Exception):
    """Go refused the request. `code`, `field` and `remedy` are its problem's."""

    def __init__(self, status: int, code: str, field: str = "", wait: int | None = None, request: str = "",
                 remedy: str = ""):
        super().__init__(f"refused ({status}): {code}" + (f" [{field}]" if field else "")
                         + (f" request {request}" if request else ""))
        self.status, self.code, self.field, self.wait, self.request = status, code, field, wait, request
        self.remedy = remedy


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


def post(path: str, body: dict, key: str, carrier: str, token: str, timeout: float = 10.0) -> dict:
    """One write under `key`, sent again under the same key while Go asks it to wait."""
    data = json.dumps(body).encode()
    started, attempts = time.monotonic(), 0
    while True:
        headers = (session_headers(token) if carrier == SESSION_CARRIER
                   else {carrier: token, "User-Agent": door.USER_AGENT})
        headers.update({"Content-Type": "application/json", "Idempotency-Key": key})
        req = urllib.request.Request(_base() + path, data=data, method="POST", headers=headers)
        try:
            with door.open_direct(req, timeout) as r:
                return json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            problem = _problem(e)
            pause = _retry_after(e) if e.code in (429, 503) else None
            attempts += 1
            if pause is None or attempts >= RETRY_ATTEMPTS or time.monotonic() - started + pause > RETRY_LIMIT:
                wait = problem.get("wait_seconds")
                raise Refused(e.code, problem.get("code") or "unexplained", problem.get("field") or "",
                              wait if isinstance(wait, int) else None, _request(e, problem), _remedy(problem)) from None
            time.sleep(pause)
        except (urllib.error.URLError, OSError, TimeoutError) as e:
            raise Unsent(f"{_base()}{path}: no answer ({e})") from None


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
    return refusal.resend(str(e), "from the session's current machine")


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
    """The session ends its own epoch, its handover note kept with it when it names one. A retry
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
            return refusal.resend(str(e), "a --handover that is a https://github.com/ comment URL")
        if e.status == 400:  # no handover was sent, so what Go refused is the session it names
            return reconnect(e)
        return refusal.retry(str(e))
    return f"detached: {session}"


def checkpoint(token: str, body: dict) -> tuple[str, bool]:
    """The session records its checkpoint (`POST /checkpoints`), keyed by its occurrence, so a resend
    under `--retry` is answered from the first: the answer, and whether it may have been recorded
    unanswered, which only a resend under the id settles."""
    sent = {"occurrence": body["id"], "boundary": body["boundary"], "note": body["note"],
            "note_sha256": body["note_sha256"], **({"learned": body["learned"]} if body.get("learned") else {})}
    try:
        answer = post("/checkpoints", sent, body["id"], SESSION_CARRIER, token)
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
        return refusal.use("checkpoint", str(e)), False
    got = answer.get("checkpoint") or {}
    dropped = got.get("dropped") or 0
    return f"checkpointed: {body['id']} {body['boundary']}" + (f" dropped: {dropped}" if dropped else ""), False


def acknowledge(session: str, token: str, directive: str) -> str:
    """The session acknowledges a directive delivered to it; the answer reads as the door's did. The
    key is the acknowledgement's own, so a retry after a lost answer is answered from the first."""
    key = hashlib.sha256(f"ack\n{session}\n{directive}".encode()).hexdigest()[:32]
    try:
        post(f"/sessions/{session}/directives/{directive}/acknowledgement", {}, key, SESSION_CARRIER, token)
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
        # Absent, lapsed or another epoch's: settled where sending it again cannot change, and the
        # brain's to follow up.
        return refusal.refuse(f"directive {directive}: {e}",
                              hand_to=("the brain", f"blocked: {session} on directive {directive} refused"))
    return f"acked: {directive} by {session}"


def answer(session: str, token: str, request: str, generation: int, text: str) -> dict:
    """The seat's holder answers a member's request it was given as a say (EL3,
    go-el3-brain-bootstrap-design.md §2.5), on its own carrier, under the seat generation it held
    when it was given it. The key is the answer's own, so a retry after a lost answer is answered
    from the first. Raises `Refused` or `Unsent`."""
    key = hashlib.sha256(f"answer\n{session}\n{request}\n{generation}".encode()).hexdigest()[:32]
    return post(f"/brain/requests/{request}/answer", {"session": session, "generation": generation, "answer": text},
                key, SESSION_CARRIER, token)


def _own_key(*parts: str) -> str:
    """A write's own key, from what makes it that write, so a retry after a lost answer is answered
    from the first rather than done twice."""
    return hashlib.sha256("\n".join(parts).encode()).hexdigest()[:32]


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


def issue_directive(session: str, token: str, directive: str, to: str, to_epoch: int, generation: int, text: str) -> dict:
    """The holder, at the seat generation it holds, directs the session `to` at `to_epoch`. `directive`
    is a UUID the caller mints once and sends again with a retry, which Go answers from the first.
    Raises `Refused` or `Unsent`."""
    return post(f"/sessions/{session}/directives",
                {"id": directive, "to": to, "to_epoch": to_epoch, "generation": generation, "body": text},
                _own_key("directive.issue", session, directive), SESSION_CARRIER, token)


def claim_name(token: str, key: str) -> dict:
    """The session claims the permanent name `key`; its own repeat is granted, another's is refused
    `name-claimed`. Raises `Refused` or `Unsent`."""
    return post("/names", {"key": key}, _own_key("name.claim", key), SESSION_CARRIER, token)


def card_repo(ws: Path | None = None) -> str:
    """`owner/name` of the workspace checkout's origin. Go's wire names the repository an anchor
    is in, which the incumbent's door filled in from the workspace's one repository; the client
    names it from the checkout itself, where the branch and the issue numbers come from too."""
    url = ""
    try:
        url = subprocess.run(["git", "-C", str(ws) if ws else os.getcwd(), "remote", "get-url", "origin"],
                             capture_output=True, text=True, timeout=5).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        pass
    for left in ("https://github.com/", "ssh://git@github.com/"):
        if url.startswith(left):
            url = url[len(left):]
    # scp-like, as `git@github.com:` or an ssh config alias such as `github.com-work:` writes it.
    scp = re.fullmatch(r"(?:[^@/:\s]+@)?[^@/:\s]+:(?P<path>[^/:\s]+/[^/:\s]+)", url)
    if scp:
        url = scp["path"]
    if url.endswith(".git"):
        url = url[:-len(".git")]
    if url.count("/") != 1 or not all(url.split("/")):
        # The origin may embed a credential; its shape is said, never its value.
        raise SystemExit("declare needs the checkout's origin as owner/name to name an anchor's "
                         "repository; this checkout's origin is not in that form")
    return url


def declare_card(card: str, name: str, anchors: dict, token: str, repo: str = "") -> str:
    """The session declares its own card (`POST /cards`), which engages it as executor; Go answers
    a minted id whose declaration is the same one with that card. The key is the declaration's own,
    so a retry after a lost answer never mints a second card. `repo` names the anchors'
    repository, as `card_repo` reads it. Raises `Refused` or `Unsent`."""
    body: dict = {"id": card, "name": name}
    if anchors:
        body["anchors"] = [{"verb": verb, "repo": repo, "n": n}
                           for verb, ns in sorted(anchors.items()) for n in ns]
    answer = post("/cards", body, _own_key("declare", card, json.dumps(body, sort_keys=True)),
                  SESSION_CARRIER, token)
    return f"declared: {card}" + (" (replayed)" if answer.get("replayed") else "")


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
    post(f"/cards/{card}/{'unlinks' if unlink else 'links'}", body,
         _own_key(verb, card, json.dumps(body, sort_keys=True)), SESSION_CARRIER, token)
    return f"{verb}ed: {card} {kind}"


def get(path: str, token: str, timeout: float = 10.0) -> dict:
    """One read on the session's carrier. Raises `Refused` or `Unsent` as a write does."""
    req = urllib.request.Request(_base() + path, headers=session_headers(token))
    try:
        with door.open_direct(req, timeout) as r:
            return json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        problem = _problem(e)
        raise Refused(e.code, problem.get("code") or "unexplained", problem.get("field") or "",
                      request=_request(e, problem), remedy=_remedy(problem)) from None
    except (urllib.error.URLError, OSError, TimeoutError) as e:
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


def executing_card(session: str, token: str) -> dict | None:
    """The live card this session executes — its executor engagement, not a merely planned one —:
    what a branch claim's announce defaults its doing to, the brain having ruled there is no
    separate claim primitive (2026-10-08)."""
    cards = get("/cards?state=live", token).get("cards") or []
    for card in cards:
        # Live, not merely unended: an earlier epoch's engagement stays unended. An engagement
        # read without `live` is not taken, so no card's title is guessed (#4469).
        if any(e.get("session") == session and e.get("role") == "executor" and e.get("live") is True
               for e in card.get("executors") or []):
            return card
    return None


def status(session: str, token: str, key: str, body: dict) -> dict:
    """The session's own status on Go's route (EL1a slice 2), under the invocation's occurrence id:
    a resend through `--retry` is answered from the first, and a fresh invocation writes a new row.
    Raises `Refused` or `Unsent`."""
    return post(f"/sessions/{session}/status", body, key, SESSION_CARRIER, token)


def wait(session: str, token: str, key: str, body: dict) -> dict:
    """The session's own wait on Go's route (go-waits-design.md decision 8), under the invocation's
    occurrence id: a resend through `--retry` answers the wait the first registered. Raises
    `Refused` or `Unsent`."""
    return post(f"/sessions/{session}/waits", body, key, SESSION_CARRIER, token)


def raise_need(token: str, key: str, body: dict) -> dict:
    """The session's ask or recommendation on Go's needs route (EL4 decision 7), under the
    invocation's occurrence id. Raises `Refused` or `Unsent`."""
    return post("/needs", body, key, SESSION_CARRIER, token)


def announce_branch(session: str, token: str, branch: str, ws: Path, key: str,
                    harness: str = "", account: str = "") -> str:
    """A branch claim is an announce (the brain's ruling, 2026-10-08): the doing defaults to the
    executed card's title, and the repository names the branch, which binds it to the card. There
    is no separate claim primitive on the wire. Answers the card it bound and the branch it
    displaced, or `REJECTED` with why nothing bound (#5084). Raises `ValueError` when no card names
    doing, `Refused` or `Unsent` as a write does."""
    card = executing_card(session, token)
    if card is None:
        raise ValueError(f"no live card of {session}'s names doing to announce")
    body: dict = {"state": "announce", "doing": card["name"], "branch": branch, "repo": card_repo(ws)}
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
}


def claim_issue(session: str, token: str, repo: str, n: int) -> str:
    """An issue claim is a card declared with `resolves:<n>` (the brain's ruling), named for the
    issue, the session its executor. The card id and the request key are the claim's own, so a
    rerun after a lost answer is answered from the first rather than minting a second permanent
    card. Raises `Refused` or `Unsent`."""
    card = _own_key("claim.card", session, repo, str(n))[:24]
    body = {"id": card, "name": f"{repo}#{n}",
            "anchors": [{"verb": "resolves", "repo": repo, "n": n}]}
    answer = post("/cards", body, _own_key("claim", session, repo, str(n)), SESSION_CARRIER, token)
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


def standing_epoch(token: str, name: str) -> int | None:
    """The epoch `name` stands at, from the fleet the brain reads, or None when it is not enrolled."""
    for item in get("/sessions", token).get("items") or []:
        if item.get("name") == name:
            return int(item["epoch"])
    return None


def relay(session: str, token: str, to: str, epoch: int | None, text: str, key: str) -> str:
    """The holder's directive to `to`, at `epoch` or the one it stands at. `key` is the send's
    occurrence id, so a retry after a lost answer is answered from the first. Raises `NotSeated`,
    `Refused` or `Unsent`."""
    generation = seat_generation(session, token)
    if epoch is None:
        epoch = standing_epoch(token, to)
        if epoch is None:
            raise Refused(404, "session-not-enrolled", "to")
    directive = str(uuid.UUID(_own_key("relay.directive", session, key)))
    answer = issue_directive(session, token, directive, to, epoch, generation, text)
    return f"relayed: {answer.get('id', directive)} to {to}@{epoch}"


def seat_need(session: str, token: str, kind: str, need: str, reason: str) -> str:
    """The holder promotes one of the brain's Needs You rows to the owner, or disposes of it, with
    a reason the owner can read (`POST /needs/{need}/transitions`). The key is the transition's
    own, so a rerun after a lost answer is answered from the first. Raises `NotSeated`, `Refused`
    or `Unsent`."""
    seat_generation(session, token)
    body = {"kind": kind, "reason": reason}
    post(f"/needs/{need}/transitions", body, _own_key("seat.need", session, need, kind, reason), SESSION_CARRIER, token)
    return f"{kind}d: {need}"


def seat_launch(session: str, token: str, body: dict, key: str) -> str:
    """The holder asks capacity for a worker (`POST /launches`), keyed by the line's own key so a
    rerun after a lost answer is answered from the first. Raises `NotSeated`, `Refused` or `Unsent`."""
    seat_generation(session, token)
    answer = post("/launches", body, key, SESSION_CARRIER, token)
    account = answer.get("account")
    on = f" on {account['vendor']} account {account['number']} ({account['account_id']}, brought by {account['brought_by']})" if account else ""
    if answer.get("outcome") == "refused":
        return f"launch refused: {answer['id']} {body['harness']}{on}: {answer.get('refused')}"
    if answer.get("outcome") == "session":
        return f"launched: {answer['id']} {body['harness']}{on}"
    if answer.get("state") == "admitted":
        return f"launching: {answer['id']} {body['harness']}{on}"
    return f"launch waiting: {answer['id']} {answer.get('reason') or answer.get('state')}{on}"


def seat_withdraw(session: str, token: str, launch: str, key: str) -> str:
    """The holder withdraws a launch still waiting (`POST /launches/{launch}/withdrawal`), keyed by the
    line's own key. Raises `NotSeated`, `Refused` or `Unsent`."""
    seat_generation(session, token)
    answer = post(f"/launches/{launch}/withdrawal", {}, key, SESSION_CARRIER, token)
    return f"withdrawn: {answer.get('id', launch)}"


def seat_action(session: str, token: str, body: dict, key: str) -> str:
    """The holder asks the machine a session runs on to wake, control, retire or rehome it
    (`POST /actions`), keyed by the line's own key so a rerun after a lost answer is answered from
    the first. Raises `NotSeated`, `Refused` or `Unsent`."""
    seat_generation(session, token)
    answer = post("/actions", body, key, SESSION_CARRIER, token)
    doing = {"wake": "waking", "control": "controlling", "retire": "retiring", "rehome": "rehoming"}[body["kind"]]
    value = f" {body['value']}" if body.get("value") else ""
    return f"{doing}: {answer['id']} {body['to']}{value}, offered"


def lift(session: str, token: str, kind: str, repo: str, at: int | str, reason: str, key: str) -> str:
    """The holder lifts a gate series one round past its ceiling (`POST /gates/lifts`): a review's
    by its pull request, a critique's by its branch, at the generation it holds. `key` is the line's
    occurrence id, so a resend after a lost answer is answered from the first rather than lifting
    twice. Raises `NotSeated`, `Refused` or `Unsent`."""
    generation = seat_generation(session, token)
    body = {"kind": kind, "repo": repo, "generation": generation, "reason": reason,
            **({"pr": at} if kind == "review" else {"branch": at})}
    post("/gates/lifts", body, _own_key("gate.lift", session, key), SESSION_CARRIER, token)
    return f"lifted: review {repo}#{at}" if kind == "review" else f"lifted: critic {repo} {at}"


# The parts a bench is kept per (go-gate-failure-attribution-design.md §2), as a lift names them.
BENCH_DIMENSIONS = ("model", "route", "account", "machine", "signed-out")


def bench_lift(session: str, token: str, first: str, second: str, reason: str, key: str) -> str:
    """The holder ends a bench (`POST /gates/bench-lifts`) at the generation it holds: one part's, as
    `<dimension> <key>` names it in the bench's reason, or a judge's by its vendor and model. `key` is
    the line's occurrence id, so a resend after a lost answer is answered from the first. Raises
    `NotSeated`, `Refused` or `Unsent`."""
    names = {"dimension": first, "key": second} if first in BENCH_DIMENSIONS else {"vendor": first, "model": second}
    body = {**names, "generation": seat_generation(session, token), "reason": reason}
    post("/gates/bench-lifts", body, _own_key("gate.bench.lift", session, key), SESSION_CARRIER, token)
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
# (`card_grammar.built`). A verb absent here has no Go route yet.
def _card_write(fact: dict, repo: str) -> tuple[str, dict] | None:
    state, card = fact["state"], fact["card"]
    if state == "card-scoped":
        body: dict = {"id": card, "name": fact["name"], "track": fact["track"]}
        anchors = fact.get("anchors") or {}
        if anchors:
            body["anchors"] = [{"verb": v, "repo": repo, "n": n} for v, ns in sorted(anchors.items()) for n in ns]
        if fact.get("major"):
            body["major"] = True
        return "/cards", body
    if state in ("card-branch", "card-unbranch"):
        # An ending carries the reason every ending records; Go refuses its absence.
        return (f"/cards/{card}/{'associations' if state == 'card-branch' else 'dissociations'}",
                {"branch": {"repo": fact["repo"], "branch": fact["branch"]},
                 **({"why": fact["why"]} if fact.get("why") else {})})
    if state == "card-session":
        return f"/cards/{card}/engagements", {"session": fact["session"], "role": fact["role"]}
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
    if state in ("card-link", "card-unlink"):
        body = {k: fact[k] for k in ("kind", "to_card", "to_issue", "why", "how", "source") if k in fact}
        return f"/cards/{card}/{'links' if state == 'card-link' else 'unlinks'}", body
    return None


# What the seat ruled Go will not keep (2026-10-09, #4812): refused with why, never as a verb Go
# does not serve yet.
RULED_OUT = {
    "card reclassify significance": "a Go card has no significance, and nothing on Go reads one (#4812)",
    "card reclassify priority": "a Go card has no priority, and nothing on Go reads one (#4812)",
    "card outcome": "Go has no outcome brief (#4812)",
    "effort": "a Go card has no effort (#4812)",
    "authorship": "Go attests a model only from what the machine observed (#4812)",
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
    Raises `NotSeated`, `Refused`, `Unsent`, or `LookupError` for a verb Go does not serve yet."""
    if fact["state"] == "card-reanchored":
        return seat_reanchor(session, token, fact, repo)
    write = _card_write(fact, repo)
    if write is None:
        what = f"{verb} {fact['field']}" if fact["state"] == "card-reclassified" else verb
        raise LookupError(f"card {what}: not served by Go yet")
    seat_generation(session, token)
    path, body = write
    post(path, body, _own_key("seat.card", session, key), SESSION_CARRIER, token)
    return f"carded: {verb} {fact['card']}"


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
    return f"carded: reanchor {card}"


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
    return f"{item['name']} {item['epoch']} {item['state']} - on {item.get('machine_registration') or item.get('machine') or '-'}{told}"


def roster(session: str, token: str, include_gone: bool) -> str:
    """Every session Go lists at its current epoch, one line each in the incumbent's `roster:`
    columns: `<session> <epoch> <state> - on <machine>`. Go keeps no basis for a state, so that
    column is `-`; what the incumbent told as sent is the live cards the epoch executes.
    Raises `NotSeated`, `Refused` or `Unsent`."""
    seat_generation(session, token)
    items = get("/sessions?gone=1" if include_gone else "/sessions", token).get("items") or []
    return "\n".join(_roster_line(item) for item in items) or "roster: empty"


def say(session: str, token: str, occurrence: str, text: str, to: str | None = None) -> str:
    """The session's say on Go (go-session-say-design.md): to `to`, or to the brain when none is
    named. The occurrence makes a resend the same say. Raises `Refused` or `Unsent`."""
    body = {"occurrence": occurrence, "text": text, **({"to": to} if to else {})}
    answer = post(f"/sessions/{session}/says", body, _own_key("say", session, occurrence), SESSION_CARRIER, token)
    if answer.get("state") == "unheard":
        return f"nobody holds the seat; kept on the brain's Needs You as unheard:{answer.get('id')}"
    return f"sent to {answer.get('to')}@{answer.get('epoch')} as {answer.get('id')}: it reaches them on their stream"
