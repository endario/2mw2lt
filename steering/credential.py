"""An agent's credential on the client side (doc 85 §3): the secret this OS user was enrolled
with, and the short-lived credential it is exchanged for, kept fresh for whichever process on
the machine asks first.

One file per door under `~/.config/2mw2lt/credentials/`, mode 0600, holding the secret, the
pair the console bound it to, and the last credential issued. The machine is never stated from
that file: every exchange states the hardware it is made from (doc 143 §4). The agent and the door scripts
run as the same OS user and read the same file.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import spool

HEADER = "X-Steering-Agent-Credential"   # the door's `principal.AGENT_HEADER`


class NoCredential(Exception):
    """This pair is enrolled and has no usable credential. The request is not sent: sent bare
    it would reach whois and succeed, and the broken renewal would say nothing (doc 85 §3).
    `status` is the console's, when it answered with a refusal."""

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


def home() -> Path:
    return Path(os.environ.get("STEERING_CREDENTIALS_DIR") or Path.home() / ".config" / "2mw2lt" / "credentials")


def path_for(door: str) -> Path:
    return home() / f"{hashlib.sha256(door.rstrip('/').encode()).hexdigest()[:16]}.json"


def _read(p: Path) -> dict | None:
    try:
        d = json.loads(p.read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(d, dict) or not d.get("door"):
        return None
    # A native enrolment spends its approval's secret at the exchange and keeps the credential.
    return d if d.get("secret") or (d.get("native") and d.get("credential")) else None


def _write(p: Path, d: dict) -> None:
    p.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    spool.write_atomic(p, json.dumps(d), mode=0o600)


def entry_for(url: str) -> Path | None:
    path = urllib.parse.urlsplit(url).path
    decoded = urllib.parse.unquote(path)
    if "%2f" in path.lower() or "%5c" in path.lower() or any(part in (".", "..") for part in decoded.split("/")):
        return None
    import sys
    enroll_dir = str(Path(__file__).resolve().parent / "enroll")
    if enroll_dir not in sys.path:
        sys.path.insert(0, enroll_dir)
    from door import split
    for p in sorted(home().glob("*.json")) if home().is_dir() else []:
        d = _read(p)
        if d is None:
            continue
        door = d["door"].rstrip("/")
        if not (url == door or url.startswith(door + "/")):
            continue
        root, workspace = split(door)
        root_path = urllib.parse.urlsplit(root).path
        if workspace is None and any(decoded == f"{root_path}/{namespace}" or decoded.startswith(f"{root_path}/{namespace}/")
                                     for namespace in ("w", "t")):
            continue
        return p
    return None


def post(console: str, path: str, body: dict, timeout: float = 10.0) -> dict:
    """A JSON POST to the console on a secret alone, as the exchange and a review host's listing
    make it. `NoCredential` on any refusal or silence."""
    req = urllib.request.Request(f"{console.rstrip('/')}{path}", data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json"})
    # The door's opener, which never takes a proxy: a system proxy drops TLS to the console and
    # the exchange then reads as no usable credential. Not `keyset`'s copy: the door scripts run
    # on the system Python, which has no `jwt`.
    import sys
    enroll_dir = str(Path(__file__).resolve().parent / "enroll")
    if enroll_dir not in sys.path:
        sys.path.insert(0, enroll_dir)
    from door import USER_AGENT, open_direct
    req.add_header("User-Agent", USER_AGENT)
    try:
        with open_direct(req, timeout) as r:
            got = json.loads(r.read())
    except urllib.error.HTTPError as e:
        why = e.read().decode(errors="replace")[:200]
        raise NoCredential(f"the console refused {path} ({e.code}): {why}", e.code)
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise NoCredential(f"the console did not answer {path}: {e}")
    if not isinstance(got, dict):
        raise NoCredential(f"the console's answer to {path} is not an object")
    return got


def _exchange(d: dict, os_node: str, timeout: float = 10.0) -> dict:
    import hardware
    try:
        # Read on this machine, never carried in the file, so a copied file states other hardware.
        body = {"secret": d["secret"], "os_node": os_node, "hardware": hardware.fingerprint()}
    except hardware.Unreadable as e:
        raise NoCredential(f"this machine's hardware identity cannot be read: {e}")
    if d.get("cluster"):
        # A review host's secret is its team's, so it names the cluster it asks for (doc 136 §3).
        body["cluster"] = d["cluster"]
    got = post(d["console"], "/api/agents/credential", body, timeout)
    if not isinstance(got.get("credential"), str):
        raise NoCredential("the console's answer carried no credential")
    return got


def _own_os_node() -> str:
    import runtime_id
    return runtime_id.node_id() or ""


def current(p: Path, clock=time.time) -> str:
    """A credential with a third of its life left, renewing it if not. A renewal that fails
    keeps the one held until it expires; past that, `NoCredential`."""
    d = _read(p)
    if d is None:
        raise NoCredential(f"{p} is not an enrolment")
    if d.get("native"):
        return _native_current(p, d, clock)
    if _fresh(d, clock):
        return d["credential"]
    lock = os.open(p.with_suffix(".lock"), os.O_WRONLY | os.O_CREAT, 0o600)
    try:
        fcntl.flock(lock, fcntl.LOCK_EX)
        d = _read(p) or d            # another process may have renewed it while this one waited
        if _fresh(d, clock):
            return d["credential"]
        os_node = _own_os_node()
        if d.get("os_node") and os_node != d["os_node"]:
            # `runtime_id` mints a new one if its file is lost, and a credential still naming the
            # old one would split this machine's census from its observations (doc 85 §3).
            raise NoCredential(f"this OS user's node is {os_node or 'unreadable'}, and the enrolment "
                               f"is bound to {d['os_node']}: enrol again")
        try:
            got = _exchange(d, os_node)
        except NoCredential:
            if d.get("credential") and d.get("exp", 0) > clock():
                return d["credential"]
            raise
        claims = _claims(got["credential"])
        d.update(credential=got["credential"], exp=claims.get("exp"), iat=claims.get("iat"),
                 machine=got.get("machine") or d.get("machine"), os_node=got.get("os_node") or os_node)
        _write(p, d)
        return d["credential"]
    finally:
        os.close(lock)


def _claims(token: str) -> dict:
    """The credential's own times, read unverified: the door verifies it, and this side only
    needs to know when to renew. Taken from the token so a skewed clock here cannot stretch it."""
    import base64
    try:
        body = token.split(".")[1]
        c = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
    except (IndexError, ValueError):
        raise NoCredential("the console's credential is not a JWT")
    if not isinstance(c, dict) or not isinstance(c.get("exp"), (int, float)) or not isinstance(c.get("iat"), (int, float)):
        raise NoCredential("the console's credential carries no times")
    return c


def lifetime(token: str) -> float:
    """How long a credential is issued for, from its own times: renewed with a third of it left. A
    native credential is opaque, so its times are the ones Go answered with, kept beside it."""
    try:
        c = _claims(token)
    except NoCredential:
        for f in home().glob("*.json") if home().is_dir() else ():
            d = _read(f)
            if d and d.get("native") and d.get("credential") == token and isinstance(d.get("exp"), int) and isinstance(d.get("iat"), int):
                return d["exp"] - d["iat"]
        raise
    return c["exp"] - c["iat"]


def _fresh(d: dict, clock) -> bool:
    exp, iat = d.get("exp"), d.get("iat")
    if not d.get("credential") or not isinstance(exp, (int, float)) or not isinstance(iat, (int, float)):
        return False
    left = exp - clock()
    return left > 0 and left > (exp - iat) / 3


def for_url(url: str) -> str | None:
    """The credential to present to this URL, or None when this OS user holds no enrolment for
    it — the door then judges the request as it always has."""
    p = entry_for(url)
    return current(p) if p is not None else None


def enrol(door: str, console: str, secret: str, cluster: str | None = None) -> dict:
    """Store an enrolment and make its first exchange, which binds this OS user and the machine
    its hardware resolves to."""
    p = path_for(door)
    d = {"door": door.rstrip("/"), "console": console.rstrip("/"), "secret": secret}
    if cluster:
        d["cluster"] = cluster
    _write(p, d)
    try:
        current(p)
    except NoCredential:
        p.unlink(missing_ok=True)
        raise
    return _read(p) or d


try:
    import httpx

    class Auth(httpx.Auth):
        """Present this OS user's credential on every request the agent makes to its door."""

        def sync_auth_flow(self, request):
            token = for_url(str(request.url))
            if token:
                request.headers[HEADER] = token
            yield request

        async def async_auth_flow(self, request):
            import asyncio
            # Off the loop: a renewal is a file lock and a request to the console.
            token = await asyncio.to_thread(for_url, str(request.url))
            if token:
                request.headers[HEADER] = token
            yield request
except ImportError:    # the door scripts run on the system Python, which has no httpx
    pass


# ---- Go's native lifecycle (go-dc2-onboarding-design.md §5) -------------------------------------
# A machine is approved by a person (here, the install engine on that person's terminal session),
# exchanges the approval's one-time secret for its own credential, and renews that credential
# from `renew_at`. An exchange or renewal carries an idempotency key written to the file before it
# is sent, so a retry after a lost answer is replayed by Go rather than minting twice.

def _native_post(origin: str, path: str, body: dict, headers: dict, timeout: float = 10.0) -> tuple[int, dict]:
    """A JSON POST to Go; the status and object for a 200 or 202, `NoCredential` naming Go's own
    check for a refusal, and its silence for none."""
    import sys
    enroll_dir = str(Path(__file__).resolve().parent / "enroll")
    if enroll_dir not in sys.path:
        sys.path.insert(0, enroll_dir)
    from door import USER_AGENT, open_direct
    req = urllib.request.Request(f"{origin.rstrip('/')}{path}", data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json", "User-Agent": USER_AGENT, **headers})
    try:
        with open_direct(req, timeout) as r:
            status, got = r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        try:
            code = json.loads(e.read()).get("code") or f"status-{e.code}"
        except ValueError:
            code = f"status-{e.code}"
        raise NoCredential(f"coordination refused {path} ({e.code}): {code}", e.code)
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise NoCredential(f"coordination did not answer {path}: {e}")
    if not isinstance(got, dict):
        raise NoCredential(f"coordination's answer to {path} is not an object")
    return status, got


def _holder() -> dict:
    import hardware
    try:
        return {"hardware": hardware.fingerprint(), "os_node": _own_os_node()}
    except hardware.Unreadable as e:
        raise NoCredential(f"this machine's hardware identity cannot be read: {e}")


def approve(origin: str, workspace: str, terminal_token: str, key: str, profile: str = "worker", reach: str = "workspace") -> dict:
    """This machine's approval to `workspace`, asked on the person's terminal session: its
    `approval_id` and one-time `secret`, which `enrol_native` exchanges. `key` makes a retry the
    same approval."""
    body = {**_holder(), "profile": profile, "reach": reach}
    _, got = _native_post(origin, f"/w/{workspace}/api/v1/device-approvals", body,
                          {"Authorization": f"Bearer {terminal_token}", "Idempotency-Key": key})
    if not isinstance(got.get("secret"), str) or not got["secret"]:
        raise NoCredential("coordination's approval carried no secret")
    return got


def enrol_native(door: str, origin: str, workspace: str, secret: str) -> dict:
    """Store a native enrolment and make its exchange, which binds this machine's hardware and
    OS user. An earlier exchange whose answer was lost is resumed on its own key first: Go may
    have spent that approval on it, and only that key recovers what it issued."""
    p = path_for(door)
    held = _read(p)
    if held and held.get("native") and held.get("workspace") == workspace and held.get("secret") and not held.get("credential"):
        try:
            current(p)
            return _read(p) or {}
        except NoCredential as e:
            if not _refused(e):
                raise
    _write(p, {"door": door.rstrip("/"), "origin": origin.rstrip("/"), "workspace": workspace, "secret": secret, "native": 1})
    try:
        current(p)
    except NoCredential as e:
        # Unanswered or failed in Go, the exchange may have been spent: keep its secret and key for the next run.
        if _refused(e):
            p.unlink(missing_ok=True)
        raise
    return _read(p) or {}


def _refused(e: NoCredential) -> bool:
    """Go judged the request and refused it. Its 5xx is not that: Go commits an issuance before
    answering it, so the approval may be spent."""
    return e.status is not None and e.status < 500


def _native_report(p: Path, d: dict, why: str) -> None:
    """A renewal that did not renew while the held credential stands, said once per reason on
    stderr, which the agent's log keeps (C5): the caller is handed the credential, not the reason."""
    if d.get("renewal") != why:
        import sys
        d["renewal"] = why
        _write(p, d)
        print(f"credential for {d['workspace']}: {why}", file=sys.stderr, flush=True)


def _native_fresh(d: dict, clock) -> bool:
    now = clock()
    return bool(d.get("credential")) and now < d.get("exp", 0) and now < max(d.get("renew_at", 0), d.get("recheck_at", 0))


def _native_current(p: Path, d: dict, clock) -> str:
    if _native_fresh(d, clock):
        return d["credential"]
    lock = os.open(p.with_suffix(".lock"), os.O_WRONLY | os.O_CREAT, 0o600)
    try:
        fcntl.flock(lock, fcntl.LOCK_EX)
        d = _read(p) or d
        if _native_fresh(d, clock):
            return d["credential"]
        holder = _holder()
        if d.get("os_node") and holder["os_node"] != d["os_node"]:
            raise NoCredential(f"this OS user's node is {holder['os_node'] or 'unreadable'}, and the enrolment "
                               f"is bound to {d['os_node']}: enrol again")
        if not d.get("pending"):
            import uuid
            d["pending"] = str(uuid.uuid4())
            _write(p, d)
        exchanging = not d.get("credential")
        path = f"/w/{d['workspace']}/api/v1/" + ("device-exchanges" if exchanging else "machine-renewals")
        try:
            status, got = _native_post(d["origin"], path, holder if exchanging else {},
                                       {HEADER: d["secret"] if exchanging else d["credential"], "Idempotency-Key": d["pending"]})
        except NoCredential as e:
            if d.get("credential") and d.get("exp", 0) > clock():
                _native_report(p, d, f"not renewed, holding the credential until {d['exp']}: {e}")
                return d["credential"]
            raise
        if status == 202:
            # Not due yet: Go names when to ask again, and the credential held stands until then.
            d["recheck_at"] = got.get("recheck_at", 0)
            d.pop("pending", None)
            _write(p, d)
            if d.get("credential") and d.get("exp", 0) > clock():
                _native_report(p, d, f"renewal deferred by coordination ({got.get('reason') or 'no reason given'}) "
                                     f"until {d['recheck_at']}")
                return d["credential"]
            raise NoCredential("coordination deferred the renewal of a credential that has expired")
        if not isinstance(got.get("credential"), str) or not isinstance(got.get("exp"), int) or not isinstance(got.get("renew_at"), int):
            raise NoCredential(f"coordination's answer to {path} carried no credential and times")
        d.update(credential=got["credential"], credential_id=got.get("credential_id"), iat=got.get("iat"), exp=got["exp"],
                 renew_at=got["renew_at"], machine=got.get("machine") or d.get("machine"), os_node=got.get("os_node") or holder["os_node"])
        for spent in ("secret", "pending", "recheck_at", "renewal"):
            d.pop(spent, None)
        _write(p, d)
        return d["credential"]
    finally:
        os.close(lock)
