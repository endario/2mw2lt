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
    it would reach whois and succeed, and the broken renewal would say nothing (doc 85 §3)."""


def home() -> Path:
    return Path(os.environ.get("STEERING_CREDENTIALS_DIR") or Path.home() / ".config" / "2mw2lt" / "credentials")


def path_for(door: str) -> Path:
    return home() / f"{hashlib.sha256(door.rstrip('/').encode()).hexdigest()[:16]}.json"


def _read(p: Path) -> dict | None:
    try:
        d = json.loads(p.read_text())
    except (OSError, ValueError):
        return None
    return d if isinstance(d, dict) and d.get("door") and d.get("secret") else None


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
        seat, workspace = split(door)
        seat_path = urllib.parse.urlsplit(seat).path
        if workspace is None and any(decoded == f"{seat_path}/{namespace}" or decoded.startswith(f"{seat_path}/{namespace}/")
                                     for namespace in ("w", "t")):
            continue
        return p
    return None


def post(console: str, path: str, body: dict, timeout: float = 10.0) -> dict:
    """A JSON POST to the console on a secret alone, as the exchange and a review host's listing
    make it. `NoCredential` on any refusal or silence."""
    req = urllib.request.Request(f"{console.rstrip('/')}{path}", data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json"})
    # The door's rule for what is this machine, so a loopback console skips the proxy. Not
    # `keyset`'s copy: the door scripts run on the system Python, which has no `jwt`.
    import sys
    enroll_dir = str(Path(__file__).resolve().parent / "enroll")
    if enroll_dir not in sys.path:
        sys.path.insert(0, enroll_dir)
    from door import USER_AGENT, is_loopback
    req.add_header("User-Agent", USER_AGENT)
    host = urllib.parse.urlsplit(console).hostname or ""
    opener = (urllib.request.build_opener(urllib.request.ProxyHandler({})) if is_loopback(host)
              else urllib.request.build_opener())
    try:
        with opener.open(req, timeout=timeout) as r:
            got = json.loads(r.read())
    except urllib.error.HTTPError as e:
        why = e.read().decode(errors="replace")[:200]
        raise NoCredential(f"the console refused {path} ({e.code}): {why}")
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
    """How long a credential is issued for, from its own times: renewed with a third of it left."""
    c = _claims(token)
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
