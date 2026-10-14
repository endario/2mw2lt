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
    import ack
    try:
        ws = connect.required_workspace_root(connect.project_dir(), timeout=2.0)
        psession = connect.provider_session(flags)
    except (connect.Refused, SystemExit) as why:
        raise ValueError(f"--lease needs this session's workspace and provider session: {why}") from None
    try:
        session, token = connect.own_enrolment(ws, psession)
    except connect.Refused:
        return None
    authority = (ack.records(ws).get(session) or {}).get("authority")
    return {"provider_session": psession, "session": session, "token": token, "ws": ws, "authority": authority}


GO_NOT_SEATED = "refused: this session does not hold the seat ({why})"
# Go's refusals of a holder whose seat moved between its read of the seat and its command.
SEAT_LOST = frozenset({"seat-not-holder", "seat-generation-stale"})
_NEED = re.compile(r"^(?P<verb>dispose|promote):\s*token\s+@lease\s+(?P<item>\S+)\s+(?P<reason>.+)$", re.S)
# A launch on Go: the machine is the `machine` `GET /sessions` lists, a registration `GET /machines`
# lists (`m-<id>`), or the name a machine's census reports (quoted when it has a space), and the account the number its team gave
# it or the vendor's own id, never one machine's launcher.
_LAUNCH = re.compile(r"^launch:\s*token\s+@lease\s+(?P<harness>[a-z][a-z0-9-]{0,31})"
                     r"(?:\s+on\s+(?:(?:m-)?(?P<machine>[0-9a-f-]{36})|\"(?P<quoted>[^\"\n]{1,64})\"|(?P<machine_name>[^\s\"]{1,64})))?"
                     r"(?:\s+vendor\s+(?P<vendor>[a-z0-9][a-z0-9-]{0,31}))?"
                     r"(?:\s+account\s+(?P<account>\S{1,256}))?"
                     # A floor names the least rarity its model may have, in place of the model (#6499).
                     r"(?:\s+model\s+(?P<model>\S{1,128})|\s+floor\s+(?P<floor>uncommon|rare|epic|legendary))?"
                     r"(?:\s+thinking\s+(?P<thinking>[a-z]{1,32}))?(?:\s+effort\s+(?P<effort>low|medium|high|xhigh|max))?"
                     r"(?:\s+(?P<window>window))?(?:\s+card\s+(?P<card>[0-9a-f]{24})"
                     # Or the card by the issue it resolves, which the seat holds as a number.
                     r"|\s+resolves\s+(?P<repo>[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100})#(?P<issue>[1-9][0-9]{0,9}))?"
                     r"\s+because\s+(?P<reason>\S.*?)"
                     # The launched session's first directive follows on its own line (doc 192).
                     r"(?:\nbrief:[ \t]*(?P<brief>.*?))?\s*$", re.S)
_WITHDRAW = re.compile(r"^withdraw:\s*token\s+@lease\s+(?P<launch>[0-9a-f-]{36})\s*$")
_CANCEL = re.compile(r"^cancel:\s*token\s+@lease\s+(?P<action>[0-9a-f-]{36})\s*$")
_BACKLOG = re.compile(r"^backlog:\s*token\s+@lease\s*$")
_ROSTER = re.compile(r"^roster:\s*token\s+@lease(?P<gone>\s+with\s+gone)?\s*$")
# The seat's machine actions on Go, each naming the session it acts on.
_ACTION = {
    "wake": re.compile(r"^wake:\s*token\s+@lease\s+(?P<to>\S+)\s+because\s+(?P<reason>\S.*?)\s*$", re.S),
    "control": re.compile(r"^control:\s*token\s+@lease\s+(?P<to>\S+)\s+"
                          r"(?P<value>effort\s+(?:low|medium|high|xhigh|max)|model\s+[a-z][a-z0-9]*(?:-[a-z0-9]+(?:\.[0-9]+)?)+|compact(?:\s+without\s+checkpoint)?)"
                          r"\s+because\s+(?P<reason>\S.*?)\s*$", re.S),
    "retire": re.compile(r"^retire:\s*token\s+@lease\s+(?P<to>\S+)(?:\s+to\s+(?P<successor>\S+))?"
                         r"\s+because\s+(?P<reason>\S.*?)\s*$", re.S),
    "rehome": re.compile(r"^rehome:\s*token\s+@lease\s+(?P<to>\S+)\s+to(?:\s+vendor\s+(?P<vendor>\S+))?\s+(?P<account>\S+)"
                         r"(?:\s+model\s+(?P<model>\S{1,128}))?(?:\s+effort\s+(?P<effort>low|medium|high|xhigh|max))?"
                         r"(?P<compact>\s+compact(?:\s+without\s+checkpoint)?)?"
                         r"\s+because\s+(?P<reason>\S.*?)\s*$", re.S),
    # A park stops an idle session's process and keeps its enrolment, for a wake to resume it.
    "park": re.compile(r"^park:\s*token\s+@lease\s+(?P<to>\S+)(?:\s+until\s+(?P<until>\S+))?\s+because\s+(?P<reason>\S.*?)\s*$", re.S),
}
_ACTION_USAGE = {
    "wake": "wake: token @lease <session> because <reason>",
    "control": "control: token @lease <session> effort <level> | model <model> | compact [without checkpoint] because <reason>",
    "retire": "retire: token @lease <session> [to <successor>] because <reason>",
    "rehome": "rehome: token @lease <session> to [vendor <vendor>] <account> [model <model>] [effort <level>] [compact [without checkpoint]] because <reason>",
    "park": "park: token @lease <session> [until <time or duration>] because <reason>",
}
# The seat's keep of a session it leaves idle on purpose, with its plan, and the keep's clearing
# (doc 189 §2). The until is a time or a duration, Go's half hour when the line names none.
_KEEP = re.compile(r"^keep:\s*token\s+@lease\s+(?P<to>\S+)(?:\s+until\s+(?P<until>\S+))?\s+because\s+(?P<reason>\S.*?)\s*$", re.S)
_UNKEEP = re.compile(r"^unkeep:\s*token\s+@lease\s+(?P<to>\S+)\s*$")
_KEEP_USAGE = {"keep": "keep: token @lease <session> [until <time or duration>] because <reason>",
               "unkeep": "unkeep: token @lease <session>"}
# Every lease verb `go_say` sends to Go, so a refusal of another names what this client takes.
GO_VERBS = tuple(sorted({"authorship", "backlog", "cancel", "dispose", "launch", "lift", "promote", "relay", "roster",
                         "withdraw", *_ACTION, *_KEEP_USAGE}))
# The paths `go_post` takes: each is the seat's own command, translated to the route Go serves.
POST_TARGETS = ("/steering/seat/read", "/steering/brain/reply", "/steering/brain/attach",
                "/steering/brain/detach", "/steering/push/raise")
_RELAY = re.compile(r"^relay:\s*token\s+@lease\s+to\s+(?P<target>[^\s@]+)(?:@(?P<epoch>[0-9]{1,9}))?\s+(?P<text>.+)$", re.S)
# `lift:` of a review series by its pull request or a critic series by its branch (speak.py _LIFT).
_LIFT = re.compile(r"^lift:\s*token\s+@lease\s+(?P<kind>review|critic)\b(?P<rest>.*)$", re.S)
# `lift:` of a bench, by the dimension and key its bench reason names, or a judge's vendor and model
# (speak.py _LIFT).
_BENCH = re.compile(r"^lift:\s*token\s+@lease\s+bench\s+(?P<first>\S+)\s+(?P<second>\S+)\s+(?P<reason>\S.*)$", re.S)
# `authorship: abandon` of an ended epoch no machine observed running a model (registry.py abandon).
_ABANDON = re.compile(r"^authorship:\s*token\s+@lease\s+abandon\s+(?P<session>\S+)\s+epoch\s+(?P<epoch>[0-9]{1,9})"
                      r"\s+because\s+(?P<reason>\S.*?)\s*$", re.S)
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
    if verb == "withdraw":
        return _go_withdraw(rec, line, key)
    if verb == "cancel":
        return _go_cancel(rec, line, key)
    if verb in _ACTION:
        return _go_action(rec, verb, line, key)
    if verb in _KEEP_USAGE:
        return _go_keep(rec, verb, line, key)
    lift = _LIFT.match(line)
    if lift:
        return _go_lift(rec, lift, key)
    if re.match(r"^lift:\s*token\s+@lease\s+bench\b", line):
        return _go_bench_lift(rec, line, key)
    if verb in ("backlog", "roster"):
        return _go_read(rec, verb, line)
    if re.match(r"^authorship:\s*token\s+@lease\s+abandon\b", line):
        return _go_abandon(rec, line, key)
    ruled = session_routes.ruled_out("authorship establish" if verb == "authorship" else verb)
    if ruled:
        return 1, ruled, False
    if verb == "lift":
        return 2, refusal.use("lift", "malformed lift: invocation"), False
    if verb != "relay":
        return 1, refusal.refuse(f"{verb[:40]} is not a lease verb this client sends to Go; it sends {', '.join(GO_VERBS)}",
                                 send=[("/2mw2lt:brain", "for each verb's grammar")], to="seat"), False
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
        if (stale := session_routes.outdated(e)) is not None:
            return 1, stale, False
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
        if (stale := session_routes.outdated(e)) is not None:
            return 1, stale, False
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
        return 2, refusal.use("launch", "malformed launch: its clauses go in the order on, vendor, account, model or floor, thinking, effort, window, card or resolves; on Go, `on` names a machine as /sessions lists it in `machine`, a registration as /machines lists it, or the machine's name"), False
    if m["harness"] == "claude" and not ((m["model"] or m["floor"]) and m["effort"]):
        return 2, refusal.use("launch", "a claude launch names its model (as Claude Code takes it) or a floor, and its effort"), False
    if m["floor"] and (m["machine"] or m["quoted"] or m["machine_name"] or m["vendor"] or m["account"]):
        return 2, refusal.use("launch", "a floor is placed by the platform: leave out on, vendor and account, or name the model"), False
    body = {k: m[k] for k in ("harness", "machine", "machine_name", "vendor", "account", "model", "floor", "thinking", "effort") if m[k]}
    if m["quoted"]:
        body["machine_name"] = m["quoted"]
    body["reason"] = " ".join(m["reason"].split())
    if m["brief"] is not None:
        if not m["brief"].strip():
            return 2, refusal.use("launch", "an empty brief: write the launched session's first directive after `brief:`, or leave the line out"), False
        if m["harness"] == "claude-brain":
            return 2, refusal.use("launch", "a seat brain is briefed by its host, not by a launch"), False
        body["brief"] = m["brief"]
    if m["window"]:
        body["window"] = True
    if m["card"]:
        if m["brief"] is None:
            return 2, refusal.use("launch", "a launch naming a card hands it over: write the brief after `brief:`, pointing at the handover note"), False
        body["card"] = m["card"]
    if m["repo"]:
        if m["brief"] is None:
            return 2, refusal.use("launch", "a launch naming the issue its card resolves hands the card over: write the brief after `brief:`, pointing at the handover note"), False
        body["resolves"] = {"repo": m["repo"], "n": int(m["issue"])}
    try:
        said = session_routes.seat_launch(rec["session"], rec["token"], body, key)
    except session_routes.NotSeated as why:
        return 1, GO_NOT_SEATED.format(why=why), False
    except session_routes.Unsent as e:
        return 1, refusal.retry(str(e)), True
    except session_routes.Refused as e:
        if (stale := session_routes.outdated(e)) is not None:
            return 1, stale, False
        if e.code in SEAT_LOST:
            return 1, GO_NOT_SEATED.format(why=e), False
        if e.code in ("launch-machine-unknown", "launch-machine-ambiguous"):
            return 1, refusal.use("launch", f"{e}; {session_routes.machines_here(rec['token'])}"), False
        if session_routes.settled(e):
            return 1, refusal.use("launch", str(e)), False
        return 1, refusal.retry(str(e)), True
    return 0, said, False


def _go_withdraw(rec: dict, line: str, key: str) -> tuple[int, str, bool]:
    """`withdraw:` on Go: the brain withdraws a launch still waiting, so it is never placed."""
    import refusal
    import session_routes
    m = _WITHDRAW.match(line)
    if not m:
        return 2, refusal.use("withdraw", "malformed withdraw: withdraw: token @lease <launch id>"), False
    try:
        said = session_routes.seat_withdraw(rec["session"], rec["token"], m["launch"], key)
    except session_routes.NotSeated as why:
        return 1, GO_NOT_SEATED.format(why=why), False
    except session_routes.Unsent as e:
        return 1, refusal.retry(str(e)), True
    except session_routes.Refused as e:
        if (stale := session_routes.outdated(e)) is not None:
            return 1, stale, False
        if e.code in SEAT_LOST:
            return 1, GO_NOT_SEATED.format(why=e), False
        if session_routes.settled(e):
            return 1, refusal.use("withdraw", str(e)), False
        return 1, refusal.retry(str(e)), True
    return 0, said, False


def _go_cancel(rec: dict, line: str, key: str) -> tuple[int, str, bool]:
    """`cancel:` on Go: the seat withdraws an action still only offered, so the ask that superseded
    it goes again."""
    import refusal
    import session_routes
    m = _CANCEL.match(line)
    if not m:
        return 2, refusal.use("cancel", "malformed cancel: cancel: token @lease <action id>"), False
    try:
        said = session_routes.seat_cancel(rec["session"], rec["token"], m["action"], key)
    except session_routes.NotSeated as why:
        return 1, GO_NOT_SEATED.format(why=why), False
    except session_routes.Unsent as e:
        return 1, refusal.retry(str(e)), True
    except session_routes.Refused as e:
        if (stale := session_routes.outdated(e)) is not None:
            return 1, stale, False
        if e.code in SEAT_LOST:
            return 1, GO_NOT_SEATED.format(why=e), False
        if session_routes.settled(e):
            return 1, refusal.use("cancel", str(e)), False
        return 1, refusal.retry(str(e)), True
    return 0, said, False


def _go_action(rec: dict, verb: str, line: str, key: str) -> tuple[int, str, bool]:
    """`wake:`, `control:`, `retire:`, `rehome:` or `park:` on Go: the brain asks the session's machine to act on it."""
    import refusal
    import session_routes
    m = _ACTION[verb].match(line)
    if not m:
        return 2, refusal.use(verb, f"malformed {verb}: on Go it is `{_ACTION_USAGE[verb]}`"), False
    body = {"kind": verb, "to": m["to"], "reason": " ".join(m["reason"].split())}
    if verb == "control":
        body["value"] = " ".join(m["value"].split())
    if verb == "rehome":
        body["value"] = " ".join([*(["vendor", m["vendor"]] if m["vendor"] else []), m["account"],
                                  *(["model", m["model"]] if m["model"] else []),
                                  *(["effort", m["effort"]] if m["effort"] else []), *(m["compact"] or "").split()])
    if verb == "park" and m["until"]:
        body["value"] = m["until"]
    if verb == "retire" and m["successor"]:
        body["value"] = m["successor"]
    try:
        said = session_routes.seat_action(rec["session"], rec["token"], body, key)
    except session_routes.NotSeated as why:
        return 1, GO_NOT_SEATED.format(why=why), False
    except session_routes.Unsent as e:
        return 1, refusal.retry(str(e)), True
    except session_routes.Refused as e:
        if (stale := session_routes.outdated(e)) is not None:
            return 1, stale, False
        if e.code in SEAT_LOST:
            return 1, GO_NOT_SEATED.format(why=e), False
        if session_routes.settled(e):
            return 1, refusal.use(verb, str(e)), False
        return 1, refusal.retry(str(e)), True
    return 0, said, False


def _go_keep(rec: dict, verb: str, line: str, key: str) -> tuple[int, str, bool]:
    """`keep:` or `unkeep:` on Go: the brain records why it leaves a session idle and until when, or
    clears that. A keep goes to no machine; it is the brain's stated intent."""
    import refusal
    import session_routes
    m = (_KEEP if verb == "keep" else _UNKEEP).match(line)
    if not m:
        return 2, refusal.use(verb, f"malformed {verb}: on Go it is `{_KEEP_USAGE[verb]}`"), False
    body = {"to": m["to"]}
    if verb == "keep":
        body.update({**({"until": m["until"]} if m["until"] else {}), "reason": " ".join(m["reason"].split())})
    try:
        said = session_routes.seat_keep(rec["session"], rec["token"], verb, body, key)
    except session_routes.NotSeated as why:
        return 1, GO_NOT_SEATED.format(why=why), False
    except session_routes.Unsent as e:
        return 1, refusal.retry(str(e)), True
    except session_routes.Refused as e:
        if (stale := session_routes.outdated(e)) is not None:
            return 1, stale, False
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
        if (stale := session_routes.outdated(e)) is not None:
            return 1, stale, False
        if e.code in SEAT_LOST:
            return 1, GO_NOT_SEATED.format(why=e), False
        if session_routes.settled(e):
            return 1, refusal.use("lift", str(e)), False
        return 1, refusal.retry(str(e)), True
    return 0, said, False


def _go_abandon(rec: dict, line: str, key: str) -> tuple[int, str, bool]:
    """`authorship: abandon` on Go: the seat's final word that an ended epoch no machine observed
    running a model authored nothing a gate may pass. `key` is the line's occurrence, so a resend
    under `--retry` is answered from the first."""
    import refusal
    import session_routes
    m = _ABANDON.match(line)
    if not m:
        return 2, refusal.use("authorship", "malformed authorship: abandon invocation; "
                              "authorship: token @lease abandon <session> epoch <n> because <reason>"), False
    try:
        said = session_routes.abandon(rec["session"], rec["token"], m["session"], int(m["epoch"]),
                                      " ".join(m["reason"].split()), key)
    except session_routes.NotSeated as why:
        return 1, GO_NOT_SEATED.format(why=why), False
    except session_routes.Unsent as e:
        return 1, refusal.retry(str(e)), True
    except session_routes.Refused as e:
        if (stale := session_routes.outdated(e)) is not None:
            return 1, stale, False
        if e.code in SEAT_LOST:
            return 1, GO_NOT_SEATED.format(why=e), False
        if session_routes.settled(e):
            return 1, refusal.use("authorship", str(e)), False
        return 1, refusal.retry(str(e)), True
    return 0, said, False


def _go_bench_lift(rec: dict, line: str, key: str) -> tuple[int, str, bool]:
    """`lift: bench` on Go: ends a benched judge's streak, as the seat. `key` is the line's occurrence,
    so a resend under `--retry` is answered from the first lift."""
    import refusal
    import session_routes
    m = _BENCH.match(line)
    if not m:
        return 2, refusal.use("lift", "malformed lift: bench invocation"), False
    try:
        said = session_routes.bench_lift(rec["session"], rec["token"], m["first"], m["second"],
                                         " ".join(m["reason"].split()), key)
    except session_routes.NotSeated as why:
        return 1, GO_NOT_SEATED.format(why=why), False
    except session_routes.Unsent as e:
        return 1, refusal.retry(str(e)), True
    except session_routes.Refused as e:
        if (stale := session_routes.outdated(e)) is not None:
            return 1, stale, False
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
        if (stale := session_routes.outdated(e)) is not None:
            return 1, stale, False
        if e.code in SEAT_LOST:
            return 1, GO_NOT_SEATED.format(why=e), False
        return 1, refusal.use(verb, str(e)) if session_routes.settled(e) else refusal.retry(str(e)), False


def go_read(rec: dict, body: bytes) -> tuple[int, str]:
    """Record frames the module actually returned, or leave an incumbent workspace alone."""
    import refusal
    import session_routes
    try:
        proof = json.loads(body or b"null")
    except ValueError:
        proof = None
    if not isinstance(proof, dict) or set(proof) - {"directives", "seat_generation"}:
        return 2, "refused: a seat read is a JSON object with directives and an optional seat_generation"
    directives = proof.get("directives")
    generation = proof.get("seat_generation")
    if not isinstance(directives, list) or not all(isinstance(directive, str) for directive in directives):
        return 2, "refused: a seat read names directives as strings"
    if generation is not None and (not isinstance(generation, int) or isinstance(generation, bool)):
        return 2, "refused: a seat read's seat_generation is a whole number"
    if rec.get("authority") != "coordination":
        return 0, "read: no native authority"
    try:
        session_routes.seat_read(rec["session"], rec["token"], directives, generation, rec["ws"])
    except session_routes.NotSeated as why:
        if generation is None:
            return 0, "read: this session does not hold the seat"
        return 1, GO_NOT_SEATED.format(why=why)
    except session_routes.Unsent as e:
        return 1, refusal.retry(str(e))
    except session_routes.Refused as e:
        if (stale := session_routes.outdated(e)) is not None:
            return 1, stale
        if e.code in SEAT_LOST:
            return 1, GO_NOT_SEATED.format(why=e)
        return 1, f"refused: {e}" if session_routes.settled(e) else refusal.retry(str(e))
    except ValueError as e:
        return 2, f"refused: a seat read's directive id is invalid ({e})"
    return 0, "read: frames"


def go_post(rec: dict, target: str, body: bytes, occurrence: str | None = None) -> tuple[int, str]:
    """A lease post: (exit code, answer). Handing the seat on and handing it back are the holder's
    own seat commands at the generation it holds, and so is a raise; a reply is `go_reply`'s."""
    import refusal
    import session_routes
    target = target.rstrip("/")
    if target == "/steering/seat/read":
        return go_read(rec, body)
    if target not in POST_TARGETS:
        return 1, refusal.refuse(f"{target[:80]} is not a path this client posts to Go; it posts to {', '.join(POST_TARGETS)}",
                                 send=[("/2mw2lt:brain", "for the lease commands")], to="seat")
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
    return go_reply(rec, obj, occurrence)


# The line a send that may have been registered unanswered ends with, which the plugin's seat tools
# read to send it once more as itself (`resendId`).
RESEND = "\nif this reply may have been registered, resend with --retry={}"


def go_reply(rec: dict, obj: dict, occurrence: str | None) -> tuple[int, str]:
    """The brain speaks to the owner: whenever it has something to say, about a say or about
    nothing (the owner's ruling of 2026-10-13 on #5555). `request` names the say it answers, or is
    absent; the older form's `key` names it the same way. The occurrence names the message, so a
    resend under `--retry` is answered from the first and two messages are two, whatever they say.
    An older post with a key and no `--retry` is named by its say and words, so the resend that form
    was told to make stays one message."""
    import door
    import refusal
    import session_routes
    text = obj.get("text")
    if not isinstance(text, str) or not text.strip():
        return 2, f'refused: a reply is a JSON object with "lease_token": "{PLACEHOLDER}" and a text'
    named = "request" if "request" in obj else "key" if "key" in obj else None
    request = obj.get(named) if named else None
    if request is not None:
        try:
            request = str(uuid.UUID(request)) if isinstance(request, str) else None
        except ValueError:
            request = None
        if request is None:
            return 2, f'refused: a reply\'s "{named}" is the id of the say it answers, or is left out'
    if occurrence is None:
        occurrence = (session_routes._own_key("reply", request, text) if named == "key" else door.occurrence())
        print(f"id {occurrence}", file=sys.stderr)
    try:
        generation = session_routes.seat_generation(rec["session"], rec["token"])
        got = session_routes.reply(rec["session"], rec["token"], generation, text, occurrence, request)
    except session_routes.NotSeated as why:
        return 1, GO_NOT_SEATED.format(why=why)
    except session_routes.Unsent as e:
        return 1, refusal.retry(str(e)) + RESEND.format(occurrence)
    except session_routes.Refused as e:
        if (stale := session_routes.outdated(e)) is not None:
            return 1, stale
        return 1, f"refused: {e}" if session_routes.settled(e) else refusal.retry(str(e)) + RESEND.format(occurrence)
    return 0, f"answered: {request}" if got.get("completed") else f"replied: {got.get('id')}"


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
        if (stale := session_routes.outdated(e)) is not None:
            return 1, stale
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
        if (stale := session_routes.outdated(e)) is not None:
            return 1, stale
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
    # Only a reply is a message a resend names; every other post is named by what it does.
    if rest[:1] == ["post"] and len(rest) == 2 and (retry is None or rest[1].rstrip("/") == "/steering/brain/reply"):
        if rec is None:
            print(NONE)
            return 1
        code, said = go_post(rec, rest[1], sys.stdin.buffer.read(), retry)
        print(said)
        return code
    return error("lease", "say [--retry=<id>], post <path>, or post /steering/brain/reply [--retry=<id>]")


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
