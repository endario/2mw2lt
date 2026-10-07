"""Where the door is, for the enroll-side scripts: `STEERING_DOOR` names a base URL (a brain on
another machine, reached through its HTTPS ingress); otherwise the loopback daemon on
`STEERING_PORT` — a caller serving a workspace that names neither refuses rather than guess."""
from __future__ import annotations

import contextlib
import contextvars
import ipaddress
import json
import socket
import os
import subprocess
import sys
import uuid
from pathlib import Path
import re
import time
import urllib.error
import urllib.parse
import urllib.request


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # a redirect would carry the door's headers to another origin


# The proxy environment on a developer machine hijacks loopback and tailnet traffic (doc 08), and
# a system proxy drops TLS to the console's own hosts; so the door and the console are always
# reached directly, like the mirror's homeserver. Every client of either uses this opener.
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
# A door or console behind Cloudflare refuses urllib's default `Python-urllib/<version>` with a
# 403 (`error code: 1010`) before the request reaches it.
USER_AGENT = "2mw2lt-agent/1"


class CredentialRefused(OSError):
    """The console refused to issue this machine's agent credential: re-admitting the machine is
    the remedy, not a resend."""

    def __init__(self, reason: str):
        super().__init__(f"{reason}; run /2mw2lt:install to admit this machine again")
        self.reason = reason


def open_direct(req: urllib.request.Request, timeout: float):
    """A request to the door or console on this module's opener, with no credential added: the
    exchange that mints one cannot present one."""
    return _OPENER.open(req, timeout=timeout)


def send(req: urllib.request.Request, timeout: float):
    if not is_loopback(urllib.parse.urlsplit(req.full_url).hostname or ""):
        return send_agent(req, timeout)
    req.add_header("User-Agent", USER_AGENT)
    return open_direct(req, timeout)


def send_agent(req: urllib.request.Request, timeout: float, *, required: bool = False):
    parent = str(Path(__file__).resolve().parent.parent)
    if parent not in sys.path:
        sys.path.insert(0, parent)
    import credential  # noqa: E402
    try:
        token = credential.for_url(req.full_url)
    except credential.NoCredential as e:
        # The console refusing this machine's enrolment (revoked, bound elsewhere) is not
        # changed by sending again; a console that did not answer, or throttled, may be.
        if e.status is not None and 400 <= e.status < 500 and e.status != 429:
            raise CredentialRefused(f"the console refused this machine's agent credential: {e}") from None
        raise OSError(f"no usable agent credential: {e}") from None
    if required and not token:
        raise CredentialRefused("this OS user holds no agent credential for this door")
    if token:
        req.add_header(credential.HEADER, token)
    req.add_header("User-Agent", USER_AGENT)
    return open_direct(req, timeout)


def is_loopback(host: str) -> bool:
    """Whether a door's host is this machine: the 127/8 block and ::1, `localhost` and anything
    under it, the root label stripped. The door routes on this and the config refuses on it."""
    h = host.rstrip(".").lower().strip("[]")
    if h == "localhost" or h.endswith(".localhost"):
        return True
    try:
        return ipaddress.ip_address(h).is_loopback
    except ValueError:
        pass
    try:
        # `127.1`, `2130706433` and `0x7f000001` are all this machine to the resolver and are
        # not addresses to `ipaddress`; `inet_aton` reads the same legacy forms the resolver
        # does, and takes no name, so nothing here is a lookup.
        return ipaddress.ip_address(socket.inet_aton(h)).is_loopback
    except OSError:
        return False
_LABEL = r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
_HOST = rf"{_LABEL}(?:\.{_LABEL})*\.?"  # a name or a dotted IPv4; no bracketed literal is a door
# The ingress may name a silo and the one workspace it opens, not an endpoint.
_WS = r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}"
_SILO = r"(?!(?:console|bridge)(?:/|$)|tunnel-)[a-z][a-z0-9_-]{0,15}"
_PATH = rf"(?-i:(?P<prefix>/t/{_SILO})?(?:/w/(?P<workspace>{_WS}))?/?)"
_DOOR = re.compile(
    rf"^(?P<scheme>https?)://(?P<host>{_HOST})(?::(?P<port>[0-9]{{1,5}}))?(?P<path>{_PATH})$",
    re.IGNORECASE)


# The workspace a caller in a process serving several is acting for (doc 130 §5). Set, it names
# the door ahead of the process's own `STEERING_DOOR` and cwd, which are no workspace's.
# A context variable, so each task and each thread `asyncio.to_thread` starts reads its own.
ANCHOR: contextvars.ContextVar[Path | None] = contextvars.ContextVar("door_anchor", default=None)


@contextlib.contextmanager
def anchored(workspace: Path | None):
    """`ANCHOR` set to `workspace` for the calls inside, and put back after."""
    token = ANCHOR.set(workspace)
    try:
        yield
    finally:
        ANCHOR.reset(token)


def configured_door() -> str:
    """`STEERING_DOOR` from the environment, else from the workspace's `.env` — the hooks run
    with the harness's environment, which does not read that file. Under `ANCHOR`, that
    workspace's `.env` alone."""
    anchored = ANCHOR.get()
    base = "" if anchored is not None else os.environ.get("STEERING_DOOR", "").strip()
    if base:
        return base
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from local_workspace import workspace_root  # noqa: E402
    anchor = anchored or Path(os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd())
    # Absence and failure are kept apart (#370): a workspace that names no door falls back
    # to the loopback daemon, but a lookup that could not be made refuses, because the
    # fallback would file this workspace's sessions in the primary authority's ledger. A
    # caller serving the workspace asks under ANCHOR and, with no STEERING_PORT in its
    # environment to fall back on, refuses instead (below).
    try:
        ws = workspace_root(anchor, timeout=1.0)
    except subprocess.TimeoutExpired:
        raise SystemExit(f"door: the workspace lookup from {anchor} timed out; refusing to fall back to the loopback daemon")
    except subprocess.CalledProcessError as e:
        raise SystemExit(f"door: {anchor} is not in a git checkout ({(e.stderr or '').strip()}); refusing to fall back to the loopback daemon")
    except OSError as e:
        raise SystemExit(f"door: the workspace lookup from {anchor} failed ({e}); refusing to fall back to the loopback daemon")
    try:
        text = (ws / ".env").read_text()
    except FileNotFoundError:
        text = ""
    except OSError as e:
        raise SystemExit(f"door: could not read {ws / '.env'} ({e.strerror or e}); refusing to fall back to the loopback daemon")
    for line in text.splitlines():
        k, _, v = line.strip().partition("=")
        value = v.strip().strip('"').strip("'")
        if k == "STEERING_DOOR" and value:
            return value
    if anchored is not None and "STEERING_PORT" not in os.environ:
        # Under ANCHOR the caller serves `ws` in-process — an agent enrolling a worker it
        # launched, or forwarding a brain's words. The agent drops STEERING_PORT from its own
        # environment at start (doc 130 §5), so the loopback fallback would be the default
        # port; the worker just launched was retired for it (#2874). Refuse naming the line
        # the install writes.
        raise SystemExit(f"door: {ws / '.env'} names no STEERING_DOOR and no STEERING_PORT is "
                         f"set here; refusing to fall back to the loopback daemon — name the "
                         f"workspace's door in {ws / '.env'} (STEERING_DOOR=…)")
    return ""


class InvalidDoor(SystemExit):
    """The configured door was read but is not a valid door URL."""


def door() -> tuple[str, bool]:
    """`STEERING_DOOR_REMOTE=1` lets a loopback stub stand in for a remote door in tests."""
    base = configured_door().rstrip("/")
    if not base:
        return f"http://127.0.0.1:{os.environ.get('STEERING_PORT', '9999')}", False
    m = _DOOR.match(base)
    if not m or (m["port"] is not None and not 1 <= int(m["port"]) <= 65535):
        raise InvalidDoor(f"STEERING_DOOR {base}: a door is scheme://host[:port][/t/<silo>][/w/<workspace>]")
    loopback = is_loopback(m["host"])
    if m["scheme"].lower() != "https" and not loopback:
        raise InvalidDoor(f"STEERING_DOOR {base}: off this machine the door is https or nothing")
    return base, (not loopback) or os.environ.get("STEERING_DOOR_REMOTE") == "1"


def split(url: str) -> tuple[str, str | None]:
    """A door as (root, workspace): the base every authority shares, and the one it names.

    The root is what `GET /steering/authorities` is asked on and what a workspace prefix is
    joined to. Appending to the door as given produced `…/w/x/w/x` when it already named
    one.
    """
    m = _DOOR.match(url.rstrip("/"))
    if not m:
        return url.rstrip("/"), None
    root = url.rstrip("/")[:m.start("path")] + (m["prefix"] or "")
    return root, m["workspace"]


def door_url() -> str:
    return door()[0]


def remote() -> bool:
    return door()[1]


def brain_route(kind: str) -> tuple[str, dict] | None:
    """Where a caged brain's hook posts instead of the door directly (#1455): its own loopback
    route on the agent that launched it, named by the bearer and address its invocation carries
    (`seathost.argv`), since the cage denies it the machine's own credential the direct path
    needs (#1427). None for an ordinary session's hook, which posts to the door as it always has."""
    nonce = os.environ.get("STEERING_BRAIN_NONCE")
    base = os.environ.get("STEERING_BRAIN_AGENT")
    if not nonce or not base:
        return None
    return f"{base}/steering/brain/{kind}", {"Authorization": f"Bearer {nonce}"}


sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from refusal import PREFIX as REJECTED, reason_of, retry, wire_up  # noqa: E402
from verb_help import error, help_requested, script_help  # noqa: E402
import requestlog  # noqa: E402

NO_ANSWER = retry("the door answered with no reply")


def display_reply(reply: str, limit: int = 400) -> str:
    """Keep an actionable refusal intact; ordinary door chatter stays bounded."""
    return reply if reply.startswith(REJECTED) else reply[:limit]


def failure(e: BaseException) -> str:
    """One line for a hook's stderr: what the send raised, and the door's reason when it
    answered with one."""
    if isinstance(e, urllib.error.HTTPError):
        try:
            body = e.read(200).decode(errors="replace").strip()
        except Exception:
            body = ""
        return f"{e.code} from the door" + (f": {body}" if body else "")
    return f"{type(e).__name__}: {e}"


HOOK_LOG = "steering-hooks.log"
HOOK_LOG_MAX = 1_000_000  # bytes; past it the log rolls once, so a chatty session cannot fill a disk


def record(hook: str, line: str, workspace: Path | None = None) -> None:
    """Why a hook answered what it did: on stderr, and appended with a timestamp to the
    workspace's `.claude/steering-hooks.log`, the one record of a hook run that outlives it.
    Never raises — a note is not worth the answer it explains."""
    text = f"{hook}: {line}"
    try:
        sys.stderr.write(text + "\n"); sys.stderr.flush()
    except Exception:
        pass
    if workspace is None:
        return
    try:
        p = Path(workspace) / ".claude" / HOOK_LOG
        p.parent.mkdir(parents=True, exist_ok=True)
        if p.exists() and p.stat().st_size > HOOK_LOG_MAX:
            os.replace(p, p.with_suffix(".log.1"))
        with open(p, "a") as f:
            f.write(f"{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())} {text}\n")
    except Exception:
        pass


def _dig(node: object, *keys: str) -> object:
    for key in keys:
        if not isinstance(node, dict):
            return None
        node = node.get(key)
    return node


def answer(reply: object) -> str:
    """The door's answer to one verb line: `reply` from the remote door, the task's artifact from
    the loopback one. Nothing else in the response is ever returned — the task's `history` holds
    the verb line that was sent, token and all, so a response this cannot read becomes a constant,
    never a rendering of itself."""
    remote_reply = _dig(reply, "reply")
    if isinstance(remote_reply, str):
        return remote_reply
    artifacts = _dig(reply, "result", "task", "artifacts")
    texts = [part["text"]
             for artifact in (artifacts if isinstance(artifacts, list) else [])
             for part in (artifact.get("parts") if isinstance(artifact, dict) and isinstance(artifact.get("parts"), list) else [])
             if isinstance(part, dict) and isinstance(part.get("text"), str)]
    return "\n".join(texts).strip() or NO_ANSWER


# The waits before each resend. A send that timed out may have been settled, so it goes again
# under the same id, which the door answers from its record of the first (doc 73 §3.2).
RETRY_WAITS = (1.0, 2.0)


SEND_TIMEOUT = 5.0

# The door's own shape for a client-chosen request id (`remote.py`), narrowed to what this side
# mints so a later change here cannot remint an id a retry is still carrying.
OCCURRENCE = re.compile(r"[A-Za-z0-9-]{16,64}")


def occurrence() -> str:
    """A name for one write, minted per event. Every automated caller gets a fresh one, which is
    what keeps two identical lines two writes: `ocwatch` emits the same `done:` text for two
    different turns ending, so the *text* must never be the name."""
    return uuid.uuid4().hex


def retry_args(argv: list[str]) -> tuple[list[str], str | None]:
    """A standalone `--retry=<id>` and the arguments left for the client."""
    flags = [a for a in argv if a.startswith("--retry")]
    if not flags:
        return list(argv), None
    if len(flags) != 1 or not flags[0].startswith("--retry=") \
            or not OCCURRENCE.fullmatch(flags[0].partition("=")[2]):
        raise ValueError("--retry=<id> needs the id a previous send printed")
    return [a for a in argv if a != flags[0]], flags[0].partition("=")[2]


def unkeyed(line: str) -> bool:
    """The brain's `note:` and `needs:` keep no record a resend could be answered from, so they
    are sent once. A `say:` is settled once by the door's message id (doc 73 §3.1) like every
    keyed verb: its lost answer goes again under the id the sender printed."""
    return line.startswith(("note:", "needs:"))


SETTLED, UNSENT, REFUSED = "settled", "unsent", "refused"
# What the request itself is refused for: its method or shape, or a line too long. 401, 403, 429
# and 5xx are the credential's or the door's state, and pass; so does 404, which the remote door
# also answers for an agent credential of another cluster (`principal.WrongCluster`), a fault
# fixed by correcting the credential, not the line. A wrong door is the recorded-door check's.
PERMANENT = frozenset({400, 405, 410, 413, 414, 422})


# Refusals `remote.py` makes of a request that never fully arrived: the same request sent whole
# again can be admitted, whatever status they carry.
TRANSIENT = frozenset({"client left", "body did not arrive in time"})
# The remote door's refusal of a request that carried no agent credential (`remote.py`): this
# machine holds none for the door, which only admitting it gives.
# remote.py writes this refusal; door_retry_test pins the two halves together.
NO_CREDENTIAL = "an agent credential is required"
# What the gateway answers when the silo behind it does not.
SILO_DOWN = frozenset({502, 503, 504})


def _refused(code: int) -> str:
    return REFUSED if code in PERMANENT else UNSENT


def say(line: str, timeout: float = SEND_TIMEOUT, occurrence_id: str | None = None) -> str:
    """The door's answer to one verb line as text; `outcome` says whether it settled."""
    return outcome(line, timeout, occurrence_id)[1]


def outcome(line: str, timeout: float = SEND_TIMEOUT,
            occurrence_id: str | None = None) -> tuple[str, str]:
    """One verb line to whichever door this workspace has, and the door's answer as text.

    `occurrence_id` names the write. Passing the one a previous attempt used is how a resend
    after a lost answer is settled once rather than twice: the door answers it from its record
    (doc 73 §3.1). The id must therefore come from something that outlives this process —
    doc 103 — and the default is a fresh one, because a caller that does not say otherwise is
    making a new write.

    `SETTLED` only when the door dispatched the line and answered it. A failure to reach it, and a
    refusal made before dispatch (`{"refused": …}` with no `reply`), come back as a `REJECTED …
    again` line all the same, which is why the text cannot tell them apart (#1798): `UNSENT` is one
    a resend may yet deliver, `REFUSED` one no resend will — a status the request itself earns,
    not the credential or the door's state.
    """
    base = door_url()
    if occurrence_id is not None and not OCCURRENCE.fullmatch(occurrence_id):
        raise ValueError(f"an occurrence is 16 to 64 letters, digits or dashes, not {occurrence_id!r}")
    key = occurrence_id or occurrence()
    far = remote()
    if far:
        req = urllib.request.Request(f"{base}/steering/door", data=json.dumps({"text": line, "id": key}).encode(),
                                     headers={"Content-Type": "application/json",
                                              requestlog.HEADER: requestlog.of_key(key)}, method="POST")
    else:
        verb = line.split(":", 1)[0].strip() or "say"
        msg = {"jsonrpc": "2.0", "id": "1", "method": "SendMessage",
               "params": {"message": {"messageId": f"{verb}-{key}", "role": "ROLE_USER", "parts": [{"text": line}]}}}
        req = urllib.request.Request(f"{base}/", data=json.dumps(msg).encode(),
                                     headers={"Content-Type": "application/json", "A2A-Version": "1.0"}, method="POST")
    waits = () if unkeyed(line) else RETRY_WAITS
    for wait in (*waits, None):
        try:
            with send(req, timeout=timeout) as r:
                text = answer(json.load(r))
                return (SETTLED if text != NO_ANSWER else UNSENT), text
        except urllib.error.HTTPError as e:  # a refusal answers 403 with its reason
            body = e.read().decode(errors="replace")
            try:
                parsed = json.loads(body or "{}")
            except ValueError:
                parsed = None
            # The remote door's own reason for a refusal it answers without a reply: a limit, its
            # admission, a restart. Dropped, it reads as the door having said nothing.
            why = parsed.get("refused") if isinstance(parsed, dict) else None
            if _dig(parsed, "reply") is None and isinstance(why, str) and why.strip():
                why = why.strip()
                if why == NO_CREDENTIAL:
                    return UNSENT, wire_up(f"the door at {base} answered {e.code}: {why}, and this machine holds none for it")
                reason = reason_of(why)
                state = UNSENT if (reason if reason is not None else why) in TRANSIENT else _refused(e.code)
                if reason is not None:
                    return state, why
                return state, retry(f"{e.code} from the door: {why[:200]}")
            if parsed is not None:
                text = answer(parsed)
                if text != NO_ANSWER:
                    return SETTLED, text
            # Answered without the daemon's word: the gateway in front of a remote door, or a
            # route the daemon does not have.
            text = body.strip() if parsed is None else ""
            detail = f"{e.code} at {base}" + (f": {text[:200]}" if text else "")
            if e.code == 404:
                return _refused(e.code), wire_up(f"the door does not know this workspace ({detail})")
            if far and e.code in SILO_DOWN:
                if wait is None:
                    return UNSENT, retry(f"the team's silo did not answer behind the door ({detail})")
                print(f"door: the team's silo did not answer behind the door ({detail}); retrying",
                      file=sys.stderr, flush=True)
                time.sleep(wait)
                continue
            return _refused(e.code), retry(f"the door answered {detail} with no reply")
        except ValueError as e:
            return UNSENT, retry(f"the door at {base} did not answer: {e}")
        except CredentialRefused as e:
            return UNSENT, wire_up(e.reason)
        except (urllib.error.URLError, OSError) as e:
            if wait is None:
                if not configured_door():
                    return UNSENT, wire_up(f"this checkout names no STEERING_DOOR, and no loopback daemon "
                                           f"answered at {base} ({e})")
                return UNSENT, retry(f"the door at {base} did not answer: {e}")
            time.sleep(wait)


def post(target: str, body: bytes, timeout: float = SEND_TIMEOUT) -> tuple[int, str]:
    """A JSON POST to a door route (a path under this workspace's door, or a whole URL), through
    `send`, so it carries the credential a remote door requires: the skills' curl did not."""
    url = target if target.startswith(("http://", "https://")) else door_url() + target
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"}, method="POST")
    try:
        with send(req, timeout=timeout) as r:
            return r.status, r.read().decode(errors="replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode(errors="replace")


def get(target: str, timeout: float = SEND_TIMEOUT) -> tuple[int, str]:
    """A GET of a door route, through `send`, so it carries the agent credential the remote
    door requires of a read as it does of a write. Without this a read across the boundary has
    no caller: `curl` holds no credential and the routes would close no gap (#1728)."""
    url = target if target.startswith(("http://", "https://")) else door_url() + target
    req = urllib.request.Request(url, headers={"Accept": "application/json"}, method="GET")
    try:
        with send(req, timeout=timeout) as r:
            return r.status, r.read().decode(errors="replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode(errors="replace")


def line_from(stdin: str, occurrence_id: str | None = None) -> tuple[str, str]:
    """`--say`: one verb line from stdin to this workspace's door, as `outcome` sends it: the
    state a caller derives its answer from — `SETTLED` for an answer that is not a refusal,
    `UNSENT` for one the door never gave, `REFUSED` for a refusal the request itself earned —
    with the text. A blank line is refused here, as `"blank"`."""
    line = stdin.strip()
    if not line:
        return "blank", "nothing to say: the line on stdin is blank"
    state, text = outcome(line, occurrence_id=occurrence_id)
    return state, text


if __name__ == "__main__":
    argv = sys.argv[1:]
    if help_requested("door", argv):
        print(script_help("door", topic=argv[0] if len(argv) == 2 else None))
        sys.exit(0)
    if argv == ["--url"]:
        print(door_url())
        sys.exit(0)
    if argv[:1] == ["--say"]:
        # The line is read from stdin, so a lease token in it stays out of the process table.
        # `--retry=<id>` resends under the id an earlier send printed, so a lost answer is answered
        # from the door's record rather than written twice.
        if len(argv) > 2:
            sys.exit(error("door", "--say takes at most one --retry=<id> flag"))
        retry_id = argv[1].removeprefix("--retry=") if len(argv) == 2 else None
        if retry_id is not None and (not argv[1].startswith("--retry=") or not OCCURRENCE.fullmatch(retry_id)):
            sys.exit(error("door", "--retry=<id> needs the id a previous send printed"))
        key = retry_id or occurrence()
        print(f"id {key}", file=sys.stderr)
        line = sys.stdin.read()
        state, text = line_from(line, key)
        if not line.strip():
            sys.exit(error("door", text))
        print(text)
        # `UNSENT`, and not one of the unkeyed lines: a line the door answered was registered,
        # and its answer is final however it reads (#1798) — the words a resend may yet deliver
        # are the ones worth naming the id for.
        if state == UNSENT and not unkeyed(line.strip()):
            # Last on stdout, so it is the line a piped reader keeps (#3303).
            print(f"if this line may have been registered, resend with --retry={key}")
        sys.exit(0 if state == SETTLED and not text.startswith(REJECTED) else 1)
    if argv[:1] in (["--get"], ["--post"]):
        flag = argv[0]
        if len(argv) != 2 or argv[1].startswith("--"):
            sys.exit(error("door", f"{flag} requires one target path or URL"))
        try:
            target = urllib.parse.urlsplit(argv[1])
            target.port
        except ValueError as exc:
            sys.exit(error("door", f"invalid target URL: {exc}"))
        if not (argv[1].startswith("/") or (target.scheme in ("http", "https") and target.netloc)):
            sys.exit(error("door", "target must be a path beginning with / or an HTTP URL"))
        try:
            if flag == "--get":
                code, text = get(argv[1])
            else:
                # The body is read from stdin, so a lease token in it stays out of the process table.
                code, text = post(argv[1], sys.stdin.buffer.read())
        except (urllib.error.URLError, OSError) as e:
            print(f"000 {failure(e)}")
            sys.exit(1)
        print(f"{code} {text}")
        sys.exit(0)
    if len(argv) == 1 and not argv[0].startswith("-"):
        os.environ["CLAUDE_PROJECT_DIR"] = str(Path(argv[0]).resolve())
        print(door_url())
        sys.exit(0)
    sys.exit(error("door", "name --url, --say, --get, --post, or one workspace path"))
