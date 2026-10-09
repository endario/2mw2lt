#!/usr/bin/env python3
"""`lease.py [--provider <harness> --provider-session <id>] say [--retry=<id>]` (a verb line on stdin)
or `lease.py [...] post <path>` (a JSON body on stdin): a lease verb as the seat's holder.

The seat is the holding session's own enrolment, so no model's context, transcript or command line
carries a lease (#4551, step 2), and `card.py --lease` sends on it too. Go decides at each verb
whether the session holds the seat.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parents[1]))
import python_floor  # noqa: E402

python_floor.require()

import json  # noqa: E402
import re  # noqa: E402
import uuid  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

PLACEHOLDER = "@lease"
NONE = "refused: this session holds no enrolment here; /2mw2lt:connect first"


def held(flags: dict[str, str]) -> dict | None:
    """The enrolment of the provider session the flags name, or that this process runs in, with its
    workspace beside it under `ws`; None when it has none. ValueError when the workspace or the
    provider session cannot be told. There is no lease token: Go judges at each verb whether the
    session holds the seat."""
    import connect
    try:
        ws = connect.required_workspace_root(connect.project_dir(), timeout=2.0)
        psession = connect.provider_session(flags)
    except (connect.Refused, SystemExit) as why:
        raise ValueError(f"--lease needs this session's workspace and provider session: {why}") from None
    try:
        session, token = connect.own_enrolment(ws, psession)
    except connect.Refused:
        return None
    return {"provider_session": psession, "session": session, "token": token, "ws": ws}


GO_NOT_SEATED = "refused: this session does not hold the seat ({why})"
# Go's refusals of a holder whose seat moved between its read of the seat and its command.
SEAT_LOST = frozenset({"seat-not-holder", "seat-generation-stale"})
_NEED = re.compile(r"^(?P<verb>dispose|promote):\s*token\s+@lease\s+(?P<item>\S+)\s+(?P<reason>.+)$", re.S)
# A launch on Go: the machine is the `machine` `GET /sessions` lists (not its `machine_registration`),
# and the account the number its team gave it or the vendor's own id, never one machine's launcher.
_LAUNCH = re.compile(r"^launch:\s*token\s+@lease\s+(?P<harness>[a-z][a-z0-9-]{0,31})"
                     r"(?:\s+on\s+(?P<machine>[0-9a-f-]{36}))?(?:\s+vendor\s+(?P<vendor>[a-z0-9][a-z0-9-]{0,31}))?"
                     r"(?:\s+account\s+(?P<account>\S{1,256}))?(?:\s+model\s+(?P<model>\S{1,128}))?"
                     r"(?:\s+thinking\s+(?P<thinking>[a-z]{1,32}))?(?:\s+effort\s+(?P<effort>low|medium|high|xhigh|max))?"
                     r"(?:\s+(?P<window>window))?\s+because\s+(?P<reason>\S.*?)\s*$", re.S)
_BACKLOG = re.compile(r"^backlog:\s*token\s+@lease\s*$")
_ROSTER = re.compile(r"^roster:\s*token\s+@lease(?P<gone>\s+with\s+gone)?\s*$")
# The seat's machine actions on Go, each naming the session it acts on.
_ACTION = {
    "wake": re.compile(r"^wake:\s*token\s+@lease\s+(?P<to>\S+)\s+because\s+(?P<reason>\S.*?)\s*$", re.S),
    "control": re.compile(r"^control:\s*token\s+@lease\s+(?P<to>\S+)\s+"
                          r"(?P<value>effort\s+(?:low|medium|high|xhigh|max)|model\s+[a-z]+-[0-9]+(?:\.[0-9]+)?|compact(?:\s+without\s+checkpoint)?)"
                          r"\s+because\s+(?P<reason>\S.*?)\s*$", re.S),
    "retire": re.compile(r"^retire:\s*token\s+@lease\s+(?P<to>\S+)\s+because\s+(?P<reason>\S.*?)\s*$", re.S),
}
_ACTION_USAGE = {
    "wake": "wake: token @lease <session> because <reason>",
    "control": "control: token @lease <session> effort <level> | model <name>-<version> | compact [without checkpoint] because <reason>",
    "retire": "retire: token @lease <session> because <reason>",
}
_RELAY = re.compile(r"^relay:\s*token\s+@lease\s+to\s+(?P<target>[^\s@]+)(?:@(?P<epoch>[0-9]{1,9}))?\s+(?P<text>.+)$", re.S)
# `lift:` of a review series by its pull request or a critic series by its branch (speak.py _LIFT).
_LIFT = re.compile(r"^lift:\s*token\s+@lease\s+(?P<kind>review|critic)\b(?P<rest>.*)$", re.S)
_LIFT_AT = {"review": re.compile(r"\s+(?P<repo>\S+)\s+pr\s+(?P<at>[0-9]{1,9})\s+(?P<reason>\S.*)$", re.S),
            "critic": re.compile(r"\s+(?P<repo>\S+)\s+branch\s+(?P<at>\S+)\s+(?P<reason>\S.*)$", re.S)}


def go_say(rec: dict, line: str, key: str) -> tuple[int, str, bool]:
    """A lease line, on the session's own carrier: (exit code, answer, whether it
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
    if verb in _ACTION:
        return _go_action(rec, verb, line, key)
    lift = _LIFT.match(line)
    if lift:
        return _go_lift(rec, lift, key)
    if verb in ("backlog", "roster"):
        return _go_read(rec, verb, line)
    ruled = session_routes.ruled_out("lift bench" if re.match(r"^lift:\s*token\s+@lease\s+bench\b", line) else verb)
    if ruled:
        return 1, ruled, False
    if verb != "relay":
        return 1, refusal.escalate(f"{verb}: not served by Go yet", to="seat"), False
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
        return 2, refusal.use(verb, f"{m['item']} is not a Needs You row's id"), False
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
        return 2, refusal.use("launch", "malformed launch: its clauses go in the order on, vendor, account, model, thinking, effort, window; on Go, `on` names a machine as /sessions lists it in `machine`, not its `machine_registration`"), False
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


def _go_action(rec: dict, verb: str, line: str, key: str) -> tuple[int, str, bool]:
    """`wake:`, `control:` or `retire:` on Go: the brain asks the session's machine to act on it."""
    import refusal
    import session_routes
    m = _ACTION[verb].match(line)
    if not m:
        return 2, refusal.use(verb, f"malformed {verb}: on Go it is `{_ACTION_USAGE[verb]}`"), False
    body = {"kind": verb, "to": m["to"], "reason": " ".join(m["reason"].split())}
    if verb == "control":
        body["value"] = " ".join(m["value"].split())
    try:
        said = session_routes.seat_action(rec["session"], rec["token"], body, key)
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


def _go_read(rec: dict, verb: str, line: str) -> tuple[int, str, bool]:
    """`backlog:` or `roster:` on Go: a read the brain makes as the seat's holder — the Needs You
    rows open for it and for the owner, or the workspace's sessions."""
    import refusal
    import session_routes
    m = (_BACKLOG if verb == "backlog" else _ROSTER).match(line)
    if not m:
        return 2, refusal.use(verb, f"malformed {verb}: invocation"), False
    try:
        if verb == "backlog":
            return 0, session_routes.backlog(rec["session"], rec["token"]), False
        return 0, session_routes.roster(rec["session"], rec["token"], bool(m["gone"])), False
    except session_routes.NotSeated as why:
        return 1, GO_NOT_SEATED.format(why=why), False
    except session_routes.Unsent as e:
        return 1, refusal.retry(str(e)), False
    except session_routes.Refused as e:
        if e.code in SEAT_LOST:
            return 1, GO_NOT_SEATED.format(why=e), False
        return 1, refusal.use(verb, str(e)) if session_routes.settled(e) else refusal.retry(str(e)), False


def go_post(rec: dict, target: str, body: bytes) -> tuple[int, str]:
    """A lease post: (exit code, answer). The seat's reply to a say answers the
    request the say was (`session_routes.answer`), at the generation the holder holds; Go has no
    reply that answers no request, so one without the say's key is refused. Handing the seat on
    and handing it back are the holder's own seat commands at that generation, and so is a raise."""
    import refusal
    import session_routes
    target = target.rstrip("/")
    if target not in ("/steering/brain/reply", "/steering/brain/attach", "/steering/brain/detach", "/steering/push/raise"):
        return 1, refusal.escalate(f"post {target}: not served by Go yet", to="seat")
    try:
        obj = json.loads(body or b"null")
    except ValueError:
        obj = None
    if not isinstance(obj, dict) or obj.get("lease_token") != PLACEHOLDER:
        return 2, f'refused: a lease post is a JSON object with "lease_token": "{PLACEHOLDER}"'
    if target == "/steering/push/raise":
        return go_raise(rec, obj)
    if target != "/steering/brain/reply":
        return go_seat(rec, target, obj)
    if not isinstance(obj.get("text"), str) or not obj["text"].strip():
        return 2, f'refused: a reply is a JSON object with "lease_token": "{PLACEHOLDER}" and a text'
    if not isinstance(obj.get("key"), str) or not obj["key"]:
        return 1, "refused: a reply answers a say, so name the say's key"
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


def go_raise(rec: dict, obj: dict) -> tuple[int, str]:
    """The holder raises the owner's phone: Go queues one push per subscribed browser, and sends
    each later."""
    import refusal
    import session_routes
    title, text = obj.get("title"), obj.get("text")
    need, about = obj.get("need") or "", obj.get("session") or ""
    if not all(isinstance(v, str) for v in (title, text, need, about)) or not title or not text:
        return 2, f'refused: a raise is a JSON object with "lease_token": "{PLACEHOLDER}", a title and a text'
    try:
        n = session_routes.raise_owner(rec["session"], rec["token"], title, text, need, about)
    except session_routes.NotSeated as why:
        return 1, GO_NOT_SEATED.format(why=why)
    except session_routes.Unsent as e:
        return 1, refusal.retry(str(e))
    except session_routes.Refused as e:
        if e.code == "push-raise-window":
            wait = f"in {e.wait} seconds" if e.wait else "later"
            return 1, f"refused: the owner was raised less than fifteen minutes ago; raise again {wait} (push-raise-window)"
        if e.code == "push-nobody-subscribed":
            return 1, "refused: nobody is subscribed to this workspace's push; the owner turns the bell on in the desk"
        if e.code == "push-unconfigured":
            return 1, "refused: this deployment sends no push (push-unconfigured)"
        return 1, f"refused: {e}" if session_routes.settled(e) else refusal.retry(str(e))
    return 0, f"raised: queued for {n} subscribed browser{'' if n == 1 else 's'}"


def go_seat(rec: dict, target: str, obj: dict) -> tuple[int, str]:
    """The holder hands the seat to the session `obj` names (attach), or gives it up (detach)."""
    import refusal
    import session_routes
    to = obj.get("session")
    if target.endswith("/attach") and (not isinstance(to, str) or not to):
        return 2, "refused: the holder hands the seat on to a session: name it as \"session\""
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
        key = retry or door.occurrence()
        print(f"id {key}", file=sys.stderr)
        code, said, unsure = go_say(rec, sys.stdin.read().strip(), key)
        print(said)
        if unsure:
            print(f"if this line may have been registered, resend with --retry={key}")
        return code
    if rest[:1] == ["post"] and len(rest) == 2 and retry is None:
        if rec is None:
            print(NONE)
            return 1
        code, said = go_post(rec, rest[1], sys.stdin.buffer.read())
        print(said)
        return code
    return error("lease", "say [--retry=<id>], or post <path>")


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
