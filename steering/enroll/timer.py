#!/usr/bin/env python3
"""`timer.py <session> <action> …`: the brain's timers, on the session's own credential.

Reads (`list`, `read`, `history`) page the workspace's timers; mutations (`lodge`, `cancel`,
`snooze`, `answer`) take the exact JSON the routes take, from a file or `-` for stdin, so no
timer's text is ever passed through a shell. `--seat` sends a mutation on the seat's route at the
generation this session holds, which conveys the holder's authority and nothing else: a session
that does not hold the seat is told so. This build has at and after timers only — cron,
conditions, move and re-address are not implemented, and a body that names one is refused here,
before anything is sent.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))
import ack  # noqa: E402
from door import OCCURRENCE, occurrence  # noqa: E402
from verb_help import _invocation as help_invocation, error, help_requested, script_help  # noqa: E402

import verb  # noqa: E402

READS = ("list", "read", "history")
ACTIONS = (*READS, "lodge", "cancel", "snooze", "answer")
UUID = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
# Shapes a later unit builds; a body naming one is refused here rather than sent to a door that
# would take it and leave it inert.
NOT_IMPLEMENTED = ("cron", "conditions", "condition", "move", "re-address", "readdress")
VERDICTS = ("acted", "considered")
TARGETS = ("self", "seat", "session")


def invocation() -> str:
    """How to spell this script for somebody who is not in its directory."""
    return help_invocation("timer", None)


def _when(body: dict, where: str) -> str | None:
    """Why the `when` a body carries is not one an at/after timer takes, or None when it is."""
    when = body.get("when")
    if not isinstance(when, dict):
        return f"{where} takes one of at or after_seconds"
    if set(when) - {"at", "after_seconds"}:
        return f"{where}'s when takes one of at or after_seconds only, not {', '.join(sorted(set(when) - {'at', 'after_seconds'}))}"
    if ("at" in when) == ("after_seconds" in when):
        return f"{where} takes one of at or after_seconds, not both and not neither"
    if "at" in when:
        at = when["at"]
        if not isinstance(at, str):
            return "at is a time (RFC 3339)"
        try:
            datetime.fromisoformat(at.replace("Z", "+00:00"))
        except ValueError:
            return "at is a time (RFC 3339)"
    else:
        seconds = when["after_seconds"]
        if not isinstance(seconds, int) or isinstance(seconds, bool) or seconds < 1:
            return "after_seconds is a whole number of seconds, at least one"
    return None


def _lodge_shape(body: dict) -> str | None:
    """Why a lodge body is not one this build sends, or None when it is."""
    if set(body) - {"text", "target", "when"}:
        named = ", ".join(sorted(set(body) - {"text", "target", "when"}))
        later = [f for f in NOT_IMPLEMENTED if f in body]
        if later:
            return f"{', '.join(later)} is not implemented in this build: a timer is one at or after time"
        return f"a lodge takes text, target and when only, not {named}"
    if not isinstance(body.get("text"), str) or not body["text"].strip() or len(body["text"]) > 4000:
        return "text is the timer's own words, 1 to 4000 characters"
    target = body.get("target")
    if not isinstance(target, dict) or set(target) - {"kind", "session"}:
        return "target is a kind, and a session's name when the kind is session"
    if target.get("kind") not in TARGETS:
        return f"target kind is one of {', '.join(TARGETS)}"
    if target["kind"] == "session" and not isinstance(target.get("session"), str):
        return "a session target names the session it addresses"
    if target["kind"] != "session" and "session" in target:
        return "only a session target names a session"
    return _when(body, "a lodge")


def _change_shape(body: dict) -> str | None:
    """Why a change body is not one this build sends, or None when it is."""
    later = [f for f in NOT_IMPLEMENTED if f in body]
    if later:
        return f"{', '.join(later)} is not implemented in this build: a change cancels or snoozes"
    if isinstance(body.get("action"), str) and body["action"] in NOT_IMPLEMENTED:
        return f"{body['action']} is not implemented in this build: a change cancels or snoozes"
    if set(body) - {"revision", "action", "when"}:
        return f"a change takes revision, action and when only, not {', '.join(sorted(set(body) - {'revision', 'action', 'when'}))}"
    revision = body.get("revision")
    if not isinstance(revision, int) or isinstance(revision, bool) or revision < 1:
        return "revision is the revision the read named, a whole number"
    action = body.get("action")
    if action == "cancel":
        if "when" in body:
            return "a cancel takes no when"
        return None
    if action != "snooze":
        return "action is cancel or snooze"
    return _when(body, "a snooze")


def _answer_shape(body: dict) -> str | None:
    """Why an answer body is not one this build sends, or None when it is."""
    if set(body) - {"delivery", "verdict", "because"}:
        return f"an answer takes delivery, verdict and because only, not {', '.join(sorted(set(body) - {'delivery', 'verdict', 'because'}))}"
    if not isinstance(body.get("delivery"), str) or not UUID.fullmatch(body["delivery"]):
        return "delivery is the exact delivery id the history named"
    if body.get("verdict") not in VERDICTS:
        return f"verdict is one of {', '.join(VERDICTS)}"
    because = body.get("because")
    if not isinstance(because, str) or not because.strip() or len(because) > 4000:
        return "because is what you did, or why nothing needed you, 1 to 4000 characters"
    return None


SHAPES = {"lodge": _lodge_shape, "cancel": _change_shape, "snooze": _change_shape, "answer": _answer_shape}


def _token(ws: Path, session: str) -> str:
    """The session's stored token: a timer is sent on the enrolment's own credential, never one a
    caller names."""
    try:
        return json.loads(ack.token_path(ws, session).read_text())["token"]
    except (OSError, ValueError, KeyError):
        raise LookupError(f"no stored token for {session}: /2mw2lt:connect first") from None


def run(session: str, token: str, rest: list[str], retry: str | None, ws: Path | None = None) -> int:
    """One action, on a credential the caller already holds (`lease.py` hands its enrolment's in;
    `main` reads the session's own). Nothing is sent until the action and its body are judged."""
    import refusal
    import session_routes
    action, args = rest[0], rest[1:]
    seat = False
    if "--seat" in args:
        if action in READS:
            return error("timer", f"a {action} is a workspace read and takes no --seat")
        seat = True
        args.remove("--seat")
    limit = after = None
    if "--limit" in args or "--next" in args:
        if action not in READS:
            return error("timer", f"{action} takes no --limit/--next")
        if "--limit" in args:
            i = args.index("--limit")
            if i + 1 >= len(args) or not args[i + 1].isdigit():
                return error("timer", "--limit is a number of rows")
            limit = int(args.pop(i + 1)); args.pop(i)
        if "--next" in args:
            i = args.index("--next")
            if i + 1 >= len(args):
                return error("timer", "--next is the cursor the previous page named")
            after = args.pop(i + 1); args.pop(i)
    if action in READS and any(a.startswith("--") for a in args):
        return error("timer", f"{action} takes the timer's id and --limit/--next only")
    if action in ("read", "history") and (len(args) != 1 or not UUID.fullmatch(args[0] or "")):
        return error("timer", f"{action} takes one timer id")
    timer_id = None
    if action in ("cancel", "snooze"):
        if not args or not UUID.fullmatch(args[0] or ""):
            return error("timer", f"{action} takes one timer id before --json-file")
        timer_id = args[0]
        args = args[1:]

    key: str | None = retry
    if action in READS:
        reads = {"list": lambda: session_routes.timer_list(token, limit, after),
                 "read": lambda: session_routes.timer_read(token, args[0]),
                 "history": lambda: session_routes.timer_history(token, args[0], limit, after)}
        got = _said(reads[action], key)
        if got is False:
            return 1
        print(json.dumps(got, indent=2, sort_keys=True))
        return 0

    if len(args) != 2 or args[0] != "--json-file":
        return error("timer", f"{action} takes {'a timer id and ' if timer_id else ''}--json-file <file> (or - for stdin)")
    try:
        text = sys.stdin.read() if args[1] == "-" else Path(args[1]).read_text()
        body = json.loads(text)
        if not isinstance(body, dict):
            raise ValueError("not an object")
    except (OSError, ValueError) as e:
        return error("timer", f"{action} needs one JSON object: {e}")
    if "generation" in body:
        return error("timer", "the generation is the seat's own, sent by the client; the body names none")
    why = SHAPES[action](body)
    if why:
        return error("timer", why)
    key = retry or occurrence()
    print(f"id {key}", file=sys.stderr)
    sends = {"lodge": lambda: session_routes.timer_lodge(session, token, body, key, seat),
             "cancel": lambda: session_routes.timer_change(session, token, timer_id, body, key, seat),
             "snooze": lambda: session_routes.timer_change(session, token, timer_id, body, key, seat),
             "answer": lambda: session_routes.timer_answer(session, token, body, key, seat)}
    got = _said(sends[action], key)
    if got is False:
        return 1
    print(json.dumps(got, indent=2, sort_keys=True))
    return 0


def _said(send, key: str | None):
    """The send's answer, with Go's refusal said; False when nothing came back to act on. A send
    that may have been registered unanswered ends with its resend, last on stdout."""
    import refusal
    import session_routes
    unsure = False
    try:
        return send()
    except session_routes.NotSeated as why:
        print(f"refused: this session does not hold the seat ({why})", file=sys.stderr)
    except session_routes.Refused as e:
        unsure = not session_routes.settled(e)
        print(refusal.retry(str(e)) if unsure else f"refused: {e}", file=sys.stderr)
    except session_routes.Unsent as e:
        unsure = True
        print(refusal.retry(str(e)), file=sys.stderr)
    if unsure and key:
        print(f"if it may have been registered, resend with --retry={key}")
    return False


def main(argv: list[str]) -> int:
    if help_requested("timer", argv):
        print(script_help("timer", topic=argv[0] if len(argv) == 2 else None))
        return 0
    if len(argv) < 2:
        return error("timer", "timer requires a session and an action")
    session, action, rest = argv[0], argv[1], argv[2:]
    if action not in ACTIONS:
        return error("timer", f"an action is one of {', '.join(ACTIONS)}")
    if not ack.valid_token(session) or session.startswith("-"):
        return error("timer", "timer requires a session name")
    for flag in rest:
        if flag.startswith("--") and flag.split("=")[0] not in ("--json-file", "--retry", "--limit", "--next", "--seat"):
            named = flag.split("=")[0]
            if named in ("--provider", "--provider-session", "--as", "--token"):
                return error("timer", f"a timer goes on the enrolment's own credential: {named} names one, and none is taken")
            return error("timer", f"unknown flag {named}")
    retry = None
    if rest and rest[-1].startswith("--retry"):
        retry = rest[-1].partition("=")[2]
        if not OCCURRENCE.fullmatch(retry):
            return error("timer", "--retry=<id> needs the id a previous send printed")
        rest = rest[:-1]
    try:
        ws = verb._workspace()
    except (subprocess.SubprocessError, OSError) as e:
        print(f"the workspace lookup failed ({e}); nothing was sent — send it again", file=sys.stderr)
        return 1
    if ws is None:
        print("not in a checkout: a timer is sent on the session's stored token, which the "
              "checkout holds; nothing was sent", file=sys.stderr)
        return 1
    try:
        token = _token(ws, session)
    except LookupError as e:
        print(e, file=sys.stderr)
        return 1
    return run(session, token, [action, *rest], retry, ws)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
