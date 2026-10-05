"""A refusal a session reads names what to do next (doc 97).

`refuse` builds the whole answer, `REJECTED <reason> — <remedy>`, once, where the refusal is
made. What travels after that is a plain string, so it survives JSON, an f-string and the
inbox's replay unchanged. `conforms` is the check made where answers are given.
"""
from __future__ import annotations

import difflib
import re

from verb_help import VERBS

PREFIX = "REJECTED "
_SEP = " — "

# Compatibility views for callers and guards that have not yet moved to the canonical table.
AUDIENCE = {verb: entry["audience"] for verb, entry in VERBS.items()}
# The first form remains the legacy grammar view; multi-form callers use VERBS directly.
GRAMMAR = {verb: entry["forms"][0]["grammar"] for verb, entry in VERBS.items()}
# A skill a remedy may point at, and who may run it.
SKILLS = {"connect": "session", "install": "session", "disconnect": "session", "rotate": "session",
          "gate": "session", "checkpoint": "session", "brain": "seat", "tracks": "seat"}
_RANK = {"session": 0, "seat": 1, "operator": 2}
_VERB = re.compile(r"^([a-z]+):")
_AGAIN = "send the same line again, "
_STUCK = "blocked: <session> on the door not answering"
_FORMS = ("send `", "wait until ", "only ", _AGAIN)


def _line(line: str, to: str = "session") -> str:
    """`line`, if a reader of audience `to` may send it: a verb of the door, or a `/2mw2lt:` skill.
    The seat is a session that holds the lease, so it may send both; the operator all three."""
    if line.startswith("/2mw2lt:"):
        audience = SKILLS.get(line[len("/2mw2lt:"):].split(" ")[0])
    else:
        m = _VERB.match(line)
        audience = AUDIENCE.get(m[1]) if m else None
    if audience is None or _RANK[audience] > _RANK[to]:
        raise ValueError(f"{line!r} is not a line a {to} may send")
    return line


def _prose(text: str) -> str:
    """Words in a remedy that `conforms` reads back: neither its separator nor a stray backtick."""
    if _SEP in text or "`" in text:
        raise ValueError(f"{text!r} carries a separator or backtick, which the remedy is parsed by")
    return text


def _sends(send, to: str) -> str:
    items = [send] if isinstance(send, str) else list(send)
    if not items:
        raise ValueError("send names nothing")
    parts = []
    for item in items:
        line, when = (item, None) if isinstance(item, str) else item
        parts.append(f"`{_line(line, to)}`" + (f" {_prose(when)}" if when else ""))
    return "send " + ", or ".join(parts)


class Refusal(str):
    """A refusal's text, which every caller already treats as a string, carrying its parts so the
    API can answer them as data (doc 126 §2.5): `reason`, and `remedy` as `{kind, to, text}`."""
    reason: str
    remedy: dict

    def __new__(cls, text: str, reason: str, remedy: dict):
        s = super().__new__(cls, text)
        s.reason, s.remedy = reason, remedy
        return s


def refuse(reason: str, *, send=None, wait=None, hand_to=None, to: str = "session") -> str:
    """The whole refusal. Exactly one remedy:

    `send`     a line the reader may send instead, or a list of `(line, "if …")`;
    `wait`     `(condition, line)`: what to wait for, and the line that shows it has happened;
    `hand_to`  `(actor, line)`: only that actor can act, and the line that raises it with them.

    `to` is who reads the refusal: `session`, `seat` (holds the lease) or `operator`. A remedy may
    send only what that reader may.
    """
    given = [k for k, v in (("send", send), ("wait", wait), ("hand_to", hand_to)) if v is not None]
    if len(given) != 1:
        raise TypeError(f"a refusal names exactly one remedy, not {len(given)}")
    if to not in _RANK:
        raise ValueError(f"no reader called {to!r}")
    if not reason.strip() or reason.startswith(PREFIX):
        raise ValueError("the reason is the condition, without the REJECTED prefix")
    if send is not None:
        remedy = _sends(send, to)
    elif wait is not None:
        until, observe = wait
        remedy = f"wait until {_prose(until)}; check with `{_line(observe, to)}`"
    else:
        actor, line = hand_to
        remedy = f"only {_prose(actor)} can act; raise it with `{_line(line, to)}`"
    return Refusal(f"{PREFIX}{reason}{_SEP}{remedy}", reason, {"kind": given[0], "to": to, "text": remedy})


def use(verb: str, why: str) -> str:
    """The commonest refusal: `verb` was sent wrong or cannot be taken now, and the remedy is the
    same verb sent right. Its reader is whoever may send that verb."""
    if verb not in VERBS:
        raise ValueError(f"{verb}: has no grammar to send again")
    forms = VERBS[verb]["forms"]
    return refuse(why, send=[item for form in forms for item in (form["grammar"], form["example"])],
                  to=AUDIENCE[verb])


def unmatched(text: str) -> str:
    """A line no verb's grammar matched (#3384). Steering queues no unaddressed text, which reached
    nobody who could act on it, so the reader is shown where words go — and, for a head that is
    a session verb misspelt, that verb. The line itself is not echoed: it may carry a credential."""
    head, sep, _ = text.partition(":")
    sessions = [verb for verb, audience in AUDIENCE.items() if audience == "session"]
    near = difflib.get_close_matches(head.strip().lower(), sessions, n=1) if sep else []
    send = [(VERBS[near[0]]["forms"][0]["grammar"], f"if you meant {near[0]}:")] if near else []
    return refuse("no verb matched this line, and steering queues nothing unaddressed", send=send + [
        ("say: <session> token <t> <text>", "to speak to the seat"),
        ("say: <session> to <peer> token <t> <text>", "to speak to a peer"),
        ("ask: <session> token <t> <question>", "for a decision only the owner can make"),
        ("recommend: <session> token <t> <text>", "for a proposal the seat triages"),
    ])


def reconnect(reason: str, to: str = "session") -> str:
    """The session's enrolment is missing, stale or not its own: `/2mw2lt:connect` sets it right."""
    return refuse(reason, send="/2mw2lt:connect", to=to)


def take_the_seat(reason: str) -> str:
    """The lease is held but nothing is attached to it: `/2mw2lt:brain` takes the role, which
    `/2mw2lt:connect` (enrol, bind, hold) does not."""
    return refuse(reason, send="/2mw2lt:brain", to="seat")


def resend(reason: str, change: str) -> str:
    """The line was refused for something about how it was sent, not what it said: send it again
    with `change`. For a protocol fault such as a reused message id, where no verb is the fix."""
    remedy = _AGAIN + _prose(change)
    return Refusal(f"{PREFIX}{reason}{_SEP}{remedy}", reason,
                   {"kind": "send", "to": "session", "text": remedy})


def retry(reason: str) -> str:
    """The door could not be reached or gave no answer, which no verb repairs: send the line again —
    under the id the send printed, for one the door settles by its id — and `blocked:` is what a
    failure that keeps happening is raised with."""
    return (f"{PREFIX}{reason}{_SEP}{_AGAIN}under the id the send printed, if it printed one, "
            f"in a minute, or `{_line(_STUCK)}` if it keeps failing")


def claim_branch(reason: str, branch: str) -> str:
    """The branch is not the session's until it says so: `taking: branch …` claims it."""
    # A name with a backtick or the separator would break the line it is quoted in.
    name = branch if "`" not in branch and _SEP not in branch else "<name>"
    return refuse(reason, send=f"taking: branch {name} token <t>")


def gate_status(reason: str) -> str:
    """A commission is asked about by its id, with `gate: status`."""
    return refuse(reason, send="gate: status <commission> token <t>")


def gate_cancel(reason: str) -> str:
    """A commission not yet resolved is withdrawn by its id, with `gate: cancel` (doc 98)."""
    return refuse(reason, send="gate: cancel <commission> token <t>")


def gate_forms(reason: str) -> str:
    """The forms `gate:` takes."""
    return use("gate", reason)


def wire_up(reason: str) -> str:
    """Steering has seen nothing from this incarnation: `/2mw2lt:install` wires the workspace."""
    return refuse(reason, send="/2mw2lt:install")


def escalate(reason: str, actor: str = "the operator", to: str = "session") -> str:
    """Only `actor` can act: the reader raises it with a `recommend:` that says what is wanted."""
    return refuse(reason, hand_to=(actor, f"recommend: <session> token <t> {_prose(reason)}"), to=to)


def conforms(reply: str, to: str = "seat") -> bool:
    """Whether a `REJECTED` answer carries one of the three remedies, and every line it offers is
    one its reader `to` may send — the seat unless the caller knows better: a string put together by
    hand cannot slip an operator verb past it. The reader is not in the string, so a session-facing
    refusal naming a seat verb passes here, and is caught where it is built, by `refuse`. A door
    answering the operator says so, since its refusals send the operator's own verbs (#2654)."""
    if not reply.startswith(PREFIX):
        return False
    _, _, remedy = reply.rpartition(_SEP)
    if not remedy.startswith(_FORMS):
        return False
    lines = re.findall(r"`([^`]+)`", remedy)
    try:
        if remedy.startswith(_AGAIN):
            return all(_line(line, to) for line in lines)
        return bool(lines) and all(_line(line, to) for line in lines)
    except ValueError:
        return False


def reason_of(reply: str) -> str | None:
    """Read a conforming refusal's reason at the final remedy separator."""
    if not conforms(reply, to="operator"):
        return None
    return reply[len(PREFIX):].rpartition(_SEP)[0]


def audit(reply: str, rejected: bool = False, to: str = "seat") -> None:
    """Where answers are given (`Inbox.commit`, and the routes that answer from memory): a
    `REJECTED` answer that names no remedy is a defect and raises. A caller that knows the answer
    is a refusal says so with `rejected`, and one without the prefix fails."""
    if (rejected or reply.startswith(PREFIX)) and not conforms(reply, to):
        raise AssertionError(f"a refusal names no remedy: {reply[:160]!r}")
