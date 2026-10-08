"""The session commands on a workspace whose authority is Go (C5,
documentation/architecture/go-c5-session-client-design.md).

The workspace's `.env` says so: `STEERING_AUTHORITY=coordination`. Without that line nothing here
is used and the door's lines are spoken as before. The machine observes and enrols on its own
carrier; the session binds and detaches on its own, and no request carries both.
"""
from __future__ import annotations

import email.utils
import hashlib
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
# A refusal of an invalid token: unknown, expired, or one a detachment revoked, which Go answers
# alike, so the client cannot tell which, only that the token can no longer act; or a stored token
# not shaped as one, which can never act. Both are repaired by connecting again.
INVALID_TOKEN = frozenset({"session-token-locator-read", "session-credential-carrier-shape"})
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
        if e.code in INVALID_TOKEN:
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
        if e.code in INVALID_TOKEN:
            return refusal.reconnect(f"{session} can no longer act: its token is not valid")
        return refusal.retry(str(e))
    return f"detached: {session}"


def acknowledge(session: str, token: str, directive: str) -> str:
    """The session acknowledges a directive delivered to it; the answer reads as the door's did. The
    key is the acknowledgement's own, so a retry after a lost answer is answered from the first."""
    key = hashlib.sha256(f"ack\n{session}\n{directive}".encode()).hexdigest()[:32]
    try:
        post(f"/sessions/{session}/directives/{directive}/acknowledgement", {}, key, SESSION_CARRIER, token)
    except Unsent as e:
        return refusal.retry(str(e))
    except Refused as e:
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
    """The holder moves its lease. Every renewal is its own request: one key per generation would be
    answered from the first and never move the lease again. Raises `Refused` or `Unsent`."""
    return post(f"/sessions/{session}/seat/renewal", {"generation": generation}, uuid.uuid4().hex, SESSION_CARRIER, token)


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
    for left in ("git@github.com:", "https://github.com/", "ssh://git@github.com/"):
        if url.startswith(left):
            url = url[len(left):]
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
    req = urllib.request.Request(_base() + path, headers={SESSION_CARRIER: token, "User-Agent": door.USER_AGENT})
    try:
        with door.open_direct(req, timeout) as r:
            return json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        problem = _problem(e)
        raise Refused(e.code, problem.get("code") or "unexplained", problem.get("field") or "") from None
    except (urllib.error.URLError, OSError, TimeoutError) as e:
        raise Unsent(f"{_base()}{path}: no answer ({e})") from None


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


def announce_branch(session: str, token: str, branch: str, ws: Path, key: str,
                    harness: str = "", account: str = "") -> str:
    """A branch claim is an announce (the brain's ruling, 2026-10-08): the doing defaults to the
    executed card's title, and the repository names the branch, which binds it to the card. There
    is no separate claim primitive on the wire. Raises `ValueError` when no card names doing,
    `Refused` or `Unsent` as a write does."""
    card = executing_card(session, token)
    if card is None:
        raise ValueError(f"no live card of {session}'s names doing to announce")
    body: dict = {"state": "announce", "doing": card["name"], "branch": branch, "repo": card_repo(ws)}
    if harness:
        body["harness"] = harness
    if account:
        body["account"] = account
    status(session, token, key, body)
    return f"registered: announce on {branch} doing {card['name']}"


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
