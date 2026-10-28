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
# The daemon's launch-reason-is-a-brief, told with where the work belongs.
REASON_IS_A_BRIEF = "a launch without a brief takes a reason of at most 300 characters: put the work on the line after the launch, as `brief: <the work>`, and say only why in the reason, since the session the launch starts is never told it"
_NEED = re.compile(r"^(?P<verb>dispose|promote):\s*token\s+@lease\s+(?P<item>\S+)\s+(?P<reason>.+)$", re.S)
# An owner's row the owner answered somewhere else (#6325): where they gave it, and their decision
# as the seat received it.
# The first ": " separates where the answer was given from the answer itself: a channel's own
# times ("13:20Z") hold a colon but no colon-space, and the owner's words, which the ruling keeps
# for good, read whole whatever prose they carry.
_OUT_OF_BAND = re.compile(r"^out-of-band:\s*token\s+@lease\s+(?P<item>\S+)\s+via\s+(?P<rest>.+)$", re.S)
# A launch on Go: the machine is the `machine` `GET /sessions` lists, a registration `GET /machines`
# lists (`m-<id>`), or the name a machine's census reports (quoted when it has a space). Within an
# explicit vendor, the account is its team number, the team owner's label, or the vendor's own id,
# never one machine's launcher.
_LAUNCH = re.compile(r"^launch:\s*token\s+@lease(?:\s+(?P<harness>[a-z][a-z0-9-]{0,31}))?"
                     r"(?:\s+on\s+(?:(?:m-)?(?P<machine>[0-9a-f-]{36})|\"(?P<quoted>[^\"\n]{1,64})\"|(?P<machine_name>[^\s\"]{1,64})))?"
                     r"(?:\s+vendor\s+(?P<vendor>[a-z0-9][a-z0-9-]{0,31}))?"
                     r"(?:\s+account\s+(?P<account>\S{1,256}))?"
                     # A floor names the least rarity its model may have, in place of the model (#6499).
                     r"(?:\s+model\s+(?P<model>\S{1,128})|\s+floor\s+(?P<floor>uncommon|rare|epic|legendary))?"
                     r"(?:\s+thinking\s+(?P<thinking>[a-z]{1,32}))?(?:\s+effort\s+(?P<effort>low|medium|high|xhigh|max))?"
                     r"(?:\s+(?P<window>window))?(?:\s+card\s+(?P<card>[0-9a-f]{24})"
                     # Or the card by the issue it resolves, which the seat holds as a number.
                     r"|\s+resolves\s+(?P<repo>[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100})#(?P<issue>[1-9][0-9]{0,9}))?"
                     r"(?:\s+notify\s+(?P<notify>[A-Za-z0-9][A-Za-z0-9._-]{0,119}))?"
                     # The harness, named after the clauses, constrains the launch to it; a floor
                     # or named model left without one lets placement choose the harness.
                     r"(?:\s+harness\s+(?P<named_harness>[a-z][a-z0-9-]{0,31}))?"
                     r"\s+because\s+(?P<reason>\S.*?)"
                     # The launched session's first directive follows on its own line (doc 192).
                     r"(?:\nbrief:[ \t]*(?P<brief>.*?))?\s*$", re.S)
# A brief may open with the checkpoint it hands over, whose text the daemon appends at delivery.
_BRIEF_CHECKPOINT = re.compile(r"^checkpoint\s+(?P<checkpoint>[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})"
                               r"(?:\s+(?P<brief>.*))?$", re.S)
_WITHDRAW = re.compile(r"^withdraw:\s*token\s+@lease\s+(?P<launch>[0-9a-f-]{36})\s*$")
_CANCEL = re.compile(r"^cancel:\s*token\s+@lease\s+(?P<action>[0-9a-f-]{36})\s*$")
_BACKLOG = re.compile(r"^backlog:\s*token\s+@lease\s*$")
_ROSTER = re.compile(r"^roster:\s*token\s+@lease(?P<gone>\s+with\s+gone)?\s*$")
# The seat's machine actions on Go, each naming the session it acts on.
_ACTION = {
    "interrupt": re.compile(r"^interrupt:\s*token\s+@lease\s+(?P<to>\S+)(?P<working>\s+working)?\s+because\s+(?P<reason>\S.*?)\s*$", re.S),
    "wake": re.compile(r"^wake:\s*token\s+@lease\s+(?P<to>\S+)\s+because\s+(?P<reason>\S.*?)\s*$", re.S),
    "control": re.compile(r"^control:\s*token\s+@lease\s+(?P<to>\S+)\s+"
                          r"(?P<value>effort\s+(?:low|medium|high|xhigh|max)|model\s+[a-z][a-z0-9]*(?:-[a-z0-9]+(?:\.[0-9]+)?)+|compact(?:\s+without\s+checkpoint)?)"
                          r"\s+because\s+(?P<reason>\S.*?)\s*$", re.S),
    "retire": re.compile(r"^retire:\s*token\s+@lease\s+(?P<to>\S+)(?:\s+to\s+(?P<successor>\S+))?"
                         r"\s+because\s+(?P<reason>\S.*?)\s*$", re.S),
    "rehome": re.compile(r"^rehome:\s*token\s+@lease\s+(?P<to>\S+)\s+to(?:\s+vendor\s+(?P<vendor>\S+))?\s+(?P<account>\S+)"
                         r"(?:\s+machine\s+(?P<machine>\"[^\"\n]{1,64}\"|'[^'\n]{1,64}'|[^\s\"']{1,64}))?"
                         r"(?:\s+model\s+(?P<model>\S{1,128}))?(?:\s+effort\s+(?P<effort>low|medium|high|xhigh|max))?"
                         r"(?P<compact>\s+compact(?:\s+without\s+checkpoint)?)?"
                         r"\s+because\s+(?P<reason>\S.*?)\s*$", re.S),
    # A park stops an idle session's process and keeps its enrolment, for a wake to resume it.
    "park": re.compile(r"^park:\s*token\s+@lease\s+(?P<to>\S+)(?:\s+until\s+(?P<until>\S+))?\s+because\s+(?P<reason>\S.*?)\s*$", re.S),
}
_ACTION_USAGE = {
    "interrupt": "interrupt: token @lease <session> [working] because <reason>",
    "wake": "wake: token @lease <session> because <reason>",
    "control": "control: token @lease <session> effort <level> | model <model> | compact [without checkpoint] because <reason>",
    "retire": "retire: token @lease <session> [to <successor>] because <reason>",
    "rehome": "rehome: token @lease <session> to [vendor <vendor>] <account> [machine <name>] [model <model>] [effort <level>] [compact [without checkpoint]] because <reason>",
    "park": "park: token @lease <session> [until <time or duration>] because <reason>",
}
# The seat's keep of a session it leaves idle on purpose, with its plan, and the keep's clearing
# (doc 189 §2). The until is a time or a duration, Go's half hour when the line names none.
_KEEP = re.compile(r"^keep:\s*token\s+@lease\s+(?P<to>\S+)(?:\s+until\s+(?P<until>\S+))?\s+because\s+(?P<reason>\S.*?)\s*$", re.S)
_UNKEEP = re.compile(r"^unkeep:\s*token\s+@lease\s+(?P<to>\S+)\s*$")
_KEEP_USAGE = {"keep": "keep: token @lease <session> [until <time or duration>] because <reason>",
               "unkeep": "unkeep: token @lease <session>"}
# The seat's answer to a snapshot it was told (#6693): it acted on it, or considered it and chose not
# to, and why. Choosing not to act is an answer.
_ANSWER = re.compile(r"^answer:\s*token\s+@lease\s+(?P<episode>[0-9a-fA-F-]{36})\s+(?P<verdict>acted|considered)\s+because\s+(?P<why>\S.*?)\s*$", re.S)
_ANSWER_USAGE = "answer: token @lease <episode> acted because <why>` or `answer: token @lease <episode> considered because <why>"
# The seat asking for a regroup by hand (work-orchestration-design.md §4): the line's occurrence is
# the request id, so `say --retry=<id>` returns the regroup the first send raised or joined.
_REGROUP = re.compile(r"^regroup:\s*token\s+@lease\s+because\s+(?P<reason>\S.*?)\s*$", re.S)
_REGROUP_USAGE = "regroup: token @lease because <why reconsider now>"
# Acts no verb sees (doc 202 D16), recorded by the holder, never by a named actor in the prose.
_INTERVENED = re.compile(r"^intervened:\s*token\s+@lease\s+(?P<card>[0-9a-f]{24})\s+(?P<cause>\S+)\s+(?P<why>\S.*?)\s*$", re.S)
_INTERVENED_CAUSES = ("unheard", "stalled", "dead", "conflict", "queue-red", "ceiling", "stale-card", "release-red", "other")
# Every lease verb `go_say` sends to Go, so a refusal of another names what this client takes.
GO_VERBS = tuple(sorted({"authorship", "backlog", "cancel", "dispose", "intervened", "launch", "lift", "out-of-band", "promote", "relay", "roster",
                         "withdraw", "answer", "regroup", *_ACTION, *_KEEP_USAGE}))
# The paths `go_post` takes: each is the seat's own command, translated to the route Go serves.
POST_TARGETS = ("/steering/seat/read", "/steering/brain/reply", "/steering/brain/attach",
                "/steering/brain/detach", "/steering/push/raise")
# `checkpoint <id>` hands the session a checkpoint's text, which the daemon appends at delivery.
_RELAY = re.compile(r"^relay:\s*token\s+@lease\s+to\s+(?P<target>[^\s@]+)(?:@(?P<epoch>[0-9]{1,9}))?"
                    r"(?:\s+checkpoint\s+(?P<checkpoint>[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}))?\s+(?P<text>.+)$", re.S)
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
    if verb == "out-of-band":
        return _go_out_of_band(rec, line)
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
    if verb == "answer":
        return _go_answer(rec, line, key)
    if verb == "regroup":
        return _go_regroup(rec, line, key)
    if verb == "intervened":
        return _go_intervened(rec, line, key)
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
                                    int(m["epoch"]) if m["epoch"] else None, m["text"].strip(), key,
                                    checkpoint=m["checkpoint"] and m["checkpoint"].lower())
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


def _go_out_of_band(rec: dict, line: str) -> tuple[int, str, bool]:
    """`out-of-band:` on Go: one of the owner's rows closed with the answer the owner gave
    somewhere else (#6325), recorded as a ruling on their behalf."""
    import refusal
    import session_routes
    m = _OUT_OF_BAND.match(line)
    channel, sep, words = (m["rest"].partition(": ") if m else ("", "", ""))
    if not sep or not words.strip() or not channel.strip():
        return 2, refusal.use("out-of-band", "malformed out-of-band: invocation"), False
    try:
        need = str(uuid.UUID(m["item"]))
    except ValueError:
        return 2, refusal.use("out-of-band", f"{m['item']} is not a Needs You row's id"), False
    try:
        said = session_routes.seat_need(rec["session"], rec["token"], "out-of-band", need,
                                        words=" ".join(words.split()), channel=" ".join(channel.split()))
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
            return 1, refusal.use("out-of-band", str(e)), False
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
        return 2, refusal.use("launch", "malformed launch: its clauses go in the order on, vendor, account, model or floor, thinking, effort, window, card or resolves, notify, harness; on Go, `on` names a machine as /sessions lists it in `machine`, a registration as /machines lists it, or the machine's name"), False
    if m["harness"] and m["named_harness"] and m["harness"] != m["named_harness"]:
        return 2, refusal.use("launch", "a launch names its harness once: bare before its clauses, or as `harness <harness>` after them"), False
    harness = m["named_harness"] or m["harness"]
    if not harness and not (m["model"] or m["floor"]):
        return 2, refusal.use("launch", "a launch names a model or a floor"), False
    if harness == "claude" and not (m["model"] or m["floor"]):
        return 2, refusal.use("launch", "a claude launch names its vendor's model or a floor; its effort, left out, is the placed model's rarity's: legendary low, epic medium, rare high, uncommon max"), False
    if m["floor"] and (m["machine"] or m["quoted"] or m["machine_name"] or m["account"]):
        return 2, refusal.use("launch", "a floor is placed by the platform: leave out on and account, or name the model"), False
    body = {k: m[k] for k in ("machine", "machine_name", "vendor", "account", "model", "floor", "thinking", "effort", "notify") if m[k]}
    if harness:
        body["harness"] = harness
    if m["quoted"]:
        body["machine_name"] = m["quoted"]
    body["reason"] = " ".join(m["reason"].split())
    if m["brief"] is not None:
        brief = m["brief"]
        if c := _BRIEF_CHECKPOINT.match(brief):
            body["checkpoint"], brief = c["checkpoint"].lower(), c["brief"] or ""
        if not brief.strip():
            return 2, refusal.use("launch", "an empty brief: write the launched session's first directive after `brief:`, or leave the line out"), False
        if harness == "claude-brain":
            return 2, refusal.use("launch", "a seat brain is briefed by its host, not by a launch"), False
        body["brief"] = brief
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
        if e.code == "launch-reason-is-a-brief":
            return 1, refusal.use("launch", REASON_IS_A_BRIEF), False
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
    """`wake:`, `control:`, `retire:`, `rehome:`, `park:` or `interrupt:` on Go: the brain asks the session's machine to act on it."""
    import refusal
    import session_routes
    m = _ACTION[verb].match(line)
    if not m:
        return 2, refusal.use(verb, f"malformed {verb}: on Go it is `{_ACTION_USAGE[verb]}`"), False
    body = {"kind": verb, "to": m["to"], "reason": " ".join(m["reason"].split())}
    if verb == "interrupt" and m["working"]:
        body["interrupt_working"] = True
    if verb == "control":
        body["value"] = " ".join(m["value"].split())
    if verb == "rehome":
        if m["machine"] and m["machine"].strip("\"'") == "auto":
            return 2, refusal.use(verb, "phase 0 names a machine, not auto"), False
        body["value"] = " ".join([*(["vendor", m["vendor"]] if m["vendor"] else []), m["account"],
                                  *(["machine", m["machine"]] if m["machine"] else []),
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
        if e.code == "interrupt-working":
            return 1, f"refused: {e.detail}; use `interrupt: token @lease <session> working because <reason>` to stop it on purpose", False
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


def _go_answer(rec: dict, line: str, key: str) -> tuple[int, str, bool]:
    """`answer:` on Go: the brain answers a snapshot by its episode, acted or considered, and why."""
    import refusal
    import session_routes
    m = _ANSWER.match(line)
    if not m:
        return 2, refusal.use("answer", f"malformed answer: on Go it is `{_ANSWER_USAGE}`"), False
    body = {"verdict": m["verdict"], "disposition": " ".join(m["why"].split())}
    try:
        said = session_routes.seat_answer(rec["session"], rec["token"], m["episode"].lower(), body, key)
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
            return 1, refusal.use("answer", str(e)), False
        return 1, refusal.retry(str(e)), True
    return 0, said, False


def _go_regroup(rec: dict, line: str, key: str) -> tuple[int, str, bool]:
    """`regroup:` on Go: the brain asks to reconsider work now, why, under the line's occurrence."""
    import refusal
    import session_routes
    m = _REGROUP.match(line)
    if not m:
        return 2, refusal.use("regroup", f"malformed regroup: on Go it is `{_REGROUP_USAGE}`"), False
    try:
        said = session_routes.seat_regroup(rec["session"], rec["token"], " ".join(m["reason"].split()), key, key)
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
            return 1, refusal.use("regroup", str(e)), False
        return 1, refusal.retry(str(e)), True
    return 0, said, False


def _go_intervened(rec: dict, line: str, key: str) -> tuple[int, str, bool]:
    """Record a manual act on a card as the seat, under the line's occurrence for safe retries."""
    import refusal
    import session_routes
    m = _INTERVENED.match(line)
    if not m:
        return 2, refusal.use("intervened", "malformed intervened: invocation"), False
    if m["cause"] not in _INTERVENED_CAUSES:
        return 2, refusal.use("intervened", f"a cause is one of {', '.join(_INTERVENED_CAUSES)}"), False
    why = " ".join(m["why"].split())
    try:
        bounded = len(why.encode("utf-8")) <= 1000
    except UnicodeEncodeError:
        bounded = False
    if not bounded:
        return 2, refusal.use("intervened", "a why is at most 1000 UTF-8 bytes"), False
    try:
        said = session_routes.intervened(rec["session"], rec["token"], m["card"], m["cause"], why, key)
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
            return 1, refusal.use("intervened", str(e)), False
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
    # A timer verb is the same client the enrolment runs (timer.py), on the lease's own enrolment:
    # every mutation is the seat's, at the generation it holds; a read is the workspace's.
    if rest[:1] == ["timer"] and len(rest) > 1:
        if rec is None:
            print(NONE)
            return 1
        import timer
        sub = list(rest[1:])
        if sub[0] not in timer.READS and "--seat" not in sub:
            sub.append("--seat")
        return timer.run(rec["session"], rec["token"], sub, retry, rec["ws"])
    return error("lease", "say [--retry=<id>], post <path>, or post /steering/brain/reply [--retry=<id>]")


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
