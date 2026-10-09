#!/usr/bin/env python3
"""`lease.py [--provider <harness> --provider-session <id>] say [--retry=<id>]` (a verb line on stdin)
or `lease.py [...] post <path>` (a JSON body on stdin): a lease verb on the seat's stored lease.

The lease is kept on disk for the session that holds the seat, so no model's context, transcript
or command line carries it (#4551, step 2). `promote.py` and `hold.py`'s seat frame store it; this
fills the one credential slot a lease verb has and nothing else, and `card.py --lease` reads it
too. The daemon decides whether a token holds the lease; a client that hears it does not removes
the stored copy and says so.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parents[1]))
import python_floor  # noqa: E402

python_floor.require()

import hashlib  # noqa: E402
import json  # noqa: E402
import os  # noqa: E402
import re  # noqa: E402
import uuid  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from atomic_file import write_atomic  # noqa: E402

PLACEHOLDER = "@lease"
# The daemon's words for a token that holds no lease: another holds the seat or nobody does
# (`registry.held_by`), or, at the API, a bearer that is neither a lease nor an enrolment
# (`api.principal.resolve`).
DEAD = ("this token does not hold the lease", "nobody holds the lease",
        "the token is neither the lease the seat holds nor a live enrolment")
NONE = "refused: this session holds no stored lease; promote.py stores one"
GONE = ("refused: this session no longer holds the seat ({why}); its stored lease is removed. "
        "Take the seat again only if the owner asks.")
# A lease verb's line opens `<verb>: token <token> `: the slot is that field and nothing after it.
SLOT = re.compile(r"^([a-z][a-z-]*): token @lease(?=\s|$)")


def path(ws: Path, provider_session: str) -> Path:
    """Under `steering-tokens/`, so every confinement that denies the enrolment tokens by subpath
    denies the lease too."""
    return ws / ".claude" / "steering-tokens" / "leases" / hashlib.sha256(provider_session.encode()).hexdigest()[:32]


def store(ws: Path, provider_session: str, session: str, attachment_id: str, token: str) -> Path:
    p = path(ws, provider_session)
    p.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(p.parent, 0o700)
    write_atomic(p, json.dumps({"provider_session": provider_session, "session": session,
                                "attachment_id": attachment_id, "lease_token": token}), mode=0o600)
    return p


def load(ws: Path, provider_session: str) -> dict | None:
    """The stored lease of this provider session, or None. A record that names another provider
    session than its file's is not this session's."""
    try:
        rec = json.loads(path(ws, provider_session).read_text())
    except (OSError, ValueError):
        return None
    ok = isinstance(rec, dict) and rec.get("provider_session") == provider_session \
        and isinstance(rec.get("lease_token"), str) and rec["lease_token"].strip() == rec["lease_token"] != ""
    return rec if ok else None


def remove(ws: Path, provider_session: str) -> bool:
    try:
        path(ws, provider_session).unlink()
        return True
    except FileNotFoundError:
        return False


def fill_say(line: str, token: str) -> str:
    """The verb line with its credential slot filled. Refused unless the line opens
    `<verb>: token @lease `: a `@lease` anywhere else, in a relay's words say, is sent as written."""
    if not SLOT.match(line):
        raise ValueError("a --lease line opens `<verb>: token @lease ` and fills nothing else")
    return SLOT.sub(lambda m: f"{m.group(1)}: token {token}", line, count=1)


def fill_post(target: str, body: bytes, token: str) -> bytes:
    """The body with its `lease_token` filled, for a path on this workspace's door only: the stored
    token never goes to another host."""
    if not target.startswith("/") or target.startswith("//"):
        raise ValueError("--lease posts only to a path on this workspace's door, never to a URL")
    try:
        obj = json.loads(body or b"null")
    except ValueError:
        raise ValueError("a --lease body is a JSON object") from None
    if not isinstance(obj, dict) or obj.get("lease_token") != PLACEHOLDER:
        raise ValueError(f'a --lease body is a JSON object whose "lease_token" is "{PLACEHOLDER}"')
    return json.dumps({**obj, "lease_token": token}).encode()


def dead(answer: str) -> bool:
    """Whether a refusal says the lease is dead. Only a refusal: an answer that carries the words
    as data, a ledger fact quoting an old refusal, is an answer."""
    refused = answer.lstrip().lower().startswith(("refused", "rejected"))
    return refused and any(words in answer for words in DEAD)


def held(flags: dict[str, str]) -> dict | None:
    """The stored lease of the provider session the flags name, or that this process runs in, with
    its workspace beside it under `ws`; None when it stores none. ValueError when the workspace or
    the provider session cannot be told. On a Go workspace there is no lease token: the record is
    the session's own enrolment, marked `coordination`, and Go judges at each verb whether it holds
    the seat."""
    import connect
    import session_routes
    try:
        ws = connect.required_workspace_root(connect.project_dir(), timeout=2.0)
        psession = connect.provider_session(flags)
    except (connect.Refused, SystemExit) as why:
        raise ValueError(f"--lease needs this session's workspace and provider session: {why}") from None
    if session_routes.on_coordination(ws):
        try:
            session, token = connect.own_enrolment(ws, psession)
        except connect.Refused:
            return None
        return {"provider_session": psession, "session": session, "token": token,
                "authority": session_routes.COORDINATION, "ws": ws}
    rec = load(ws, psession)
    return None if rec is None else {**rec, "ws": ws}


GO_NOT_SEATED = "refused: this session does not hold the seat ({why})"
# Go's refusals of a holder whose seat moved between its read of the seat and its command.
SEAT_LOST = frozenset({"seat-not-holder", "seat-generation-stale"})
_NEED = re.compile(r"^(?P<verb>dispose|promote):\s*token\s+@lease\s+(?P<item>\S+)\s+(?P<reason>.+)$", re.S)
# A launch on Go: the machine is its registration id as the fleet lists it, and the account the
# vendor's own id, not an index into one machine's launchers.
_LAUNCH = re.compile(r"^launch:\s*token\s+@lease\s+(?P<harness>[a-z][a-z0-9-]{0,31})"
                     r"(?:\s+on\s+(?P<machine>[0-9a-f-]{36}))?(?:\s+vendor\s+(?P<vendor>[a-z0-9][a-z0-9-]{0,31}))?"
                     r"(?:\s+account\s+(?P<account>\S{1,256}))?(?:\s+model\s+(?P<model>\S{1,128}))?"
                     r"(?:\s+thinking\s+(?P<thinking>[a-z]{1,32}))?(?:\s+effort\s+(?P<effort>low|medium|high|xhigh|max))?"
                     r"(?:\s+(?P<window>window))?\s+because\s+(?P<reason>\S.*?)\s*$", re.S)
_BACKLOG = re.compile(r"^backlog:\s*token\s+@lease\s*$")
_RELAY = re.compile(r"^relay:\s*token\s+@lease\s+to\s+(?P<target>[^\s@]+)(?:@(?P<epoch>[0-9]{1,9}))?\s+(?P<text>.+)$", re.S)
# `lift:` of a review series by its pull request or a critic series by its branch (speak.py _LIFT).
_LIFT = re.compile(r"^lift:\s*token\s+@lease\s+(?P<kind>review|critic)\b(?P<rest>.*)$", re.S)
_LIFT_AT = {"review": re.compile(r"\s+(?P<repo>\S+)\s+pr\s+(?P<at>[0-9]{1,9})\s+(?P<reason>\S.*)$", re.S),
            "critic": re.compile(r"\s+(?P<repo>\S+)\s+branch\s+(?P<at>\S+)\s+(?P<reason>\S.*)$", re.S)}


def go_say(rec: dict, line: str, key: str) -> tuple[int, str, bool]:
    """A lease line on a Go workspace, on the session's own carrier: (exit code, answer, whether it
    may have been registered unanswered). Only the
    verbs Go serves are sent; another is refused here, naming it, rather than sent as a line Go's
    door refuses."""
    import refusal
    import session_routes
    verb = line.partition(":")[0]
    if verb in ("dispose", "promote"):
        return _go_need(rec, verb, line)
    if verb == "launch":
        return _go_launch(rec, line, key)
    lift = _LIFT.match(line)
    if lift:
        return _go_lift(rec, lift, key)
    if verb == "backlog":
        return _go_backlog(rec, line)
    ruled = session_routes.ruled_out("lift bench" if re.match(r"^lift:\s*token\s+@lease\s+bench\b", line) else verb)
    if ruled:
        return 1, ruled, False
    if verb != "relay":
        return 1, refusal.escalate(f"{verb}: not served on a Go workspace yet", to="seat"), False
    m = _RELAY.match(line)
    if not m or not m["text"].strip():
        return 2, refusal.use("relay", "malformed relay: invocation"), False
    try:
        said = session_routes.relay(rec["session"], rec["token"], m["target"],
                                    int(m["epoch"]) if m["epoch"] else None, " ".join(m["text"].split()), key)
    except session_routes.NotSeated as why:
        return 1, GO_NOT_SEATED.format(why=why), False
    except session_routes.Unsent as e:
        return 1, refusal.retry(str(e)), True
    except session_routes.Refused as e:
        if e.code in SEAT_LOST:
            return 1, GO_NOT_SEATED.format(why=e), False
        if session_routes.settled(e):
            return 1, refusal.use("relay", str(e)), False
        return 1, refusal.retry(str(e)), True
    return 0, said, False


def _go_need(rec: dict, verb: str, line: str) -> tuple[int, str, bool]:
    """`dispose:` or `promote:` on Go: a transition of the brain's Needs You row, as the seat."""
    import refusal
    import session_routes
    m = _NEED.match(line)
    if not m or m["verb"] != verb or not m["reason"].strip():
        return 2, refusal.use(verb, f"malformed {verb}: invocation"), False
    try:
        need = str(uuid.UUID(m["item"]))
    except ValueError:
        return 2, refusal.use(verb, f"{m['item']} is not a Needs You row's id on a Go workspace"), False
    try:
        said = session_routes.seat_need(rec["session"], rec["token"], verb, need, " ".join(m["reason"].split()))
    except session_routes.NotSeated as why:
        return 1, GO_NOT_SEATED.format(why=why), False
    except session_routes.Unsent as e:
        return 1, refusal.retry(str(e)), True
    except session_routes.Refused as e:
        if e.code in SEAT_LOST:
            return 1, GO_NOT_SEATED.format(why=e), False
        if session_routes.settled(e):
            return 1, refusal.use(verb, str(e)), False
        return 1, refusal.retry(str(e)), True
    return 0, said, False


def _go_launch(rec: dict, line: str, key: str) -> tuple[int, str, bool]:
    """`launch:` on Go: the brain asks capacity for a worker, placed where it says when it says."""
    import refusal
    import session_routes
    m = _LAUNCH.match(line)
    if not m:
        return 2, refusal.use("launch", "malformed launch: its clauses go in the order on, vendor, account, model, thinking, effort, window; on Go, `on` names a machine's registration id"), False
    if m["harness"] == "claude" and not (m["model"] and m["effort"]):
        return 2, refusal.use("launch", "a claude launch names its model (as Claude Code takes it) and its effort"), False
    body = {k: m[k] for k in ("harness", "machine", "vendor", "account", "model", "thinking", "effort") if m[k]}
    body["reason"] = " ".join(m["reason"].split())
    if m["window"]:
        body["window"] = True
    try:
        said = session_routes.seat_launch(rec["session"], rec["token"], body, key)
    except session_routes.NotSeated as why:
        return 1, GO_NOT_SEATED.format(why=why), False
    except session_routes.Unsent as e:
        return 1, refusal.retry(str(e)), True
    except session_routes.Refused as e:
        if e.code in SEAT_LOST:
            return 1, GO_NOT_SEATED.format(why=e), False
        if session_routes.settled(e):
            return 1, refusal.use("launch", str(e)), False
        return 1, refusal.retry(str(e)), True
    return 0, said, False


def _go_lift(rec: dict, lift: re.Match, key: str) -> tuple[int, str, bool]:
    """`lift:` on Go: one more round of a review or critic series past its ceiling, as the seat.
    `key` is the line's occurrence, so a resend under `--retry` is answered from the first lift."""
    import refusal
    import session_routes
    m = _LIFT_AT[lift["kind"]].match(lift["rest"])
    if not m:
        return 2, refusal.use("lift", f"malformed lift: {lift['kind']} invocation"), False
    at = int(m["at"]) if lift["kind"] == "review" else m["at"]
    try:
        said = session_routes.lift(rec["session"], rec["token"], lift["kind"], m["repo"], at,
                                   " ".join(m["reason"].split()), key)
    except session_routes.NotSeated as why:
        return 1, GO_NOT_SEATED.format(why=why), False
    except session_routes.Unsent as e:
        return 1, refusal.retry(str(e)), True
    except session_routes.Refused as e:
        if e.code in SEAT_LOST:
            return 1, GO_NOT_SEATED.format(why=e), False
        if session_routes.settled(e):
            return 1, refusal.use("lift", str(e)), False
        return 1, refusal.retry(str(e)), True
    return 0, said, False


def _go_backlog(rec: dict, line: str) -> tuple[int, str, bool]:
    """`backlog:` on Go: the brain reads, as the seat's holder, the Needs You rows open for it and for the owner."""
    import refusal
    import session_routes
    if not _BACKLOG.match(line):
        return 2, refusal.use("backlog", "malformed backlog: invocation"), False
    try:
        return 0, session_routes.backlog(rec["session"], rec["token"]), False
    except session_routes.NotSeated as why:
        return 1, GO_NOT_SEATED.format(why=why), False
    except session_routes.Unsent as e:
        return 1, refusal.retry(str(e)), False
    except session_routes.Refused as e:
        return 1, refusal.use("backlog", str(e)) if session_routes.settled(e) else refusal.retry(str(e)), False


def gone(rec: dict, answer: str) -> str:
    """The daemon refused the stored lease as dead: the seat is another's now. The copy goes, and
    the session is told why, in the daemon's own words (C5)."""
    remove(rec["ws"], rec["provider_session"])
    return GONE.format(why=answer.strip())


def seat_frame(raw: bytes) -> tuple[bytes, dict | None]:
    """A `seat` frame's line with its lease token replaced by `stored`, and the frame as it came;
    any other line unchanged, and None. What is recorded and printed is the first."""
    if not raw.startswith(b"data: "):
        return raw, None
    try:
        frame = json.loads(raw[6:])
    except ValueError:
        return raw, None
    if not isinstance(frame, dict) or frame.get("kind") != "seat" or not isinstance(frame.get("lease_token"), str):
        return raw, None
    return b"data: " + json.dumps({**frame, "lease_token": "stored"}).encode() + b"\n", frame


def go_post(rec: dict, target: str, body: bytes) -> tuple[int, str]:
    """A lease post on a Go workspace: (exit code, answer). The seat's reply to a say answers the
    request the say was (`session_routes.answer`), at the generation the holder holds; Go has no
    reply that answers no request, so one without the say's key is refused. Handing the seat on
    and handing it back are the holder's own seat commands at that generation."""
    import refusal
    import session_routes
    target = target.rstrip("/")
    if target not in ("/steering/brain/reply", "/steering/brain/attach", "/steering/brain/detach"):
        return 1, refusal.escalate(f"post {target}: not served on a Go workspace yet", to="seat")
    try:
        obj = json.loads(body or b"null")
    except ValueError:
        obj = None
    if not isinstance(obj, dict) or obj.get("lease_token") != PLACEHOLDER:
        return 2, f'refused: a lease post is a JSON object with "lease_token": "{PLACEHOLDER}"'
    if target != "/steering/brain/reply":
        return go_seat(rec, target, obj)
    if not isinstance(obj.get("text"), str) or not obj["text"].strip():
        return 2, f'refused: a reply is a JSON object with "lease_token": "{PLACEHOLDER}" and a text'
    if not isinstance(obj.get("key"), str) or not obj["key"]:
        return 1, "refused: on a Go workspace a reply answers a say, so name the say's key"
    try:
        generation = session_routes.seat_generation(rec["session"], rec["token"])
        session_routes.answer(rec["session"], rec["token"], obj["key"], generation, obj["text"])
    except session_routes.NotSeated as why:
        return 1, GO_NOT_SEATED.format(why=why)
    except session_routes.Unsent as e:
        return 1, refusal.retry(str(e))
    except session_routes.Refused as e:
        return 1, f"refused: {e}" if session_routes.settled(e) else refusal.retry(str(e))
    return 0, f"answered: {obj['key']}"


def go_seat(rec: dict, target: str, obj: dict) -> tuple[int, str]:
    """The holder hands the seat to the session `obj` names (attach), or gives it up (detach)."""
    import refusal
    import session_routes
    to = obj.get("session")
    if target.endswith("/attach") and (not isinstance(to, str) or not to):
        return 2, "refused: on a Go workspace the holder hands the seat on to a session: name it as \"session\""
    try:
        generation = session_routes.seat_generation(rec["session"], rec["token"])
        if target.endswith("/detach"):
            session_routes.seat_release(rec["session"], rec["token"], generation)
            return 0, f"released: the seat, at generation {generation}"
        epoch = session_routes.standing_epoch(rec["token"], to)
        if epoch is None:
            return 1, f"refused: {to} is not enrolled here"
        session_routes.seat_hand(rec["session"], rec["token"], generation, to, epoch)
    except session_routes.NotSeated as why:
        return 1, GO_NOT_SEATED.format(why=why)
    except session_routes.Unsent as e:
        return 1, refusal.retry(str(e))
    except session_routes.Refused as e:
        return 1, f"refused: {e}" if session_routes.settled(e) else refusal.retry(str(e))
    return 0, f"handed: the seat to {to}@{epoch}"


def main(argv: list[str]) -> int:
    import connect
    import door
    from verb_help import error, help_requested, script_help
    if help_requested("lease", argv):
        print(script_help("lease", topic=argv[0] if len(argv) == 2 else None))
        return 0
    parsed = connect.speaker_flags(argv)
    if parsed is None:
        return error("lease", "the speaker flags are --provider <harness> and --provider-session <id>")
    flags, rest = parsed
    try:
        rest, retry = door.retry_args(rest)
        rec = held(flags)
    except ValueError as why:
        return error("lease", str(why))
    if rest[:1] == ["say"] and len(rest) == 1:
        if rec is None:
            print(NONE)
            return 1
        if rec.get("authority"):
            key = retry or door.occurrence()
            print(f"id {key}", file=sys.stderr)
            code, said, unsure = go_say(rec, sys.stdin.read().strip(), key)
            print(said)
            if unsure:
                print(f"if this line may have been registered, resend with --retry={key}")
            return code
        try:
            line = fill_say(sys.stdin.read().strip(), rec["lease_token"])
        except ValueError as why:
            return error("lease", str(why))
        # `door.py --say`'s own send, on the filled line: the id first, a lost answer resent under it.
        key = retry or door.occurrence()
        print(f"id {key}", file=sys.stderr)
        state, text = door.line_from(line, key)
        if dead(text):
            print(gone(rec, text))
            return 1
        print(text)
        if state == door.UNSENT and not door.unkeyed(line):
            print(f"if this line may have been registered, resend with --retry={key}")
        return 0 if state == door.SETTLED and not text.startswith(door.REJECTED) else 1
    if rest[:1] == ["post"] and len(rest) == 2 and retry is None:
        if rec is None:
            print(NONE)
            return 1
        if rec.get("authority"):
            code, said = go_post(rec, rest[1], sys.stdin.buffer.read())
            print(said)
            return code
        try:
            body = fill_post(rest[1], sys.stdin.buffer.read(), rec["lease_token"])
            code, text = door.post(rest[1], body)
        except ValueError as why:
            return error("lease", str(why))
        except OSError as e:
            print(f"000 {door.failure(e)}")
            return 1
        if not 200 <= code < 300 and dead(f"refused: {text}"):
            print(gone(rec, text))
            return 1
        # Handed back, or handed on: the token is dead either way, and nothing is left to hold.
        if 200 <= code < 300 and re.search(r"/steering/brain/(detach|attach)/?$", rest[1]):
            remove(rec["ws"], rec["provider_session"])
        print(f"{code} {text}")
        return 0 if 200 <= code < 300 else 1
    return error("lease", "say [--retry=<id>], or post <path>")


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
