"""`gate.py <your session> review <pr>` or `gate.py <your session> critic <doc path>`: ask
steering for an independent review of your pull request, or a critique of your design (doc 72).

The branch is the one this checkout is on, and the repository is the one its `origin` names.
Steering pins the commit, chooses a reviewer from a vendor that wrote none of the branch, and
records the verdict; the answer here says who was asked.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parents[1]))
import os
import re
import subprocess
import time
from urllib.parse import quote, unquote

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))
from bind import Refused  # noqa: E402
from connect import branch, head, speaker_flags, speaking_as  # noqa: E402
from door import display_reply, occurrence, retry_args  # noqa: E402
from local_workspace import checkout_slug, required_workspace_root  # noqa: E402

# A reflog subject that records a commit this checkout made, rather than one it was handed by a
# fetch, a fast-forward or a reset. `HEAD`'s reflog holds each rebase step; a branch's, only the tip.
# A resolved merge and a continued rebase are locally made, while the daemon proves their replay.
# Not `rebase (finish)`: it names the tip whatever made it, and a rebase that only moves forward
# makes it another writer's commit.
MADE = re.compile(r"^(?:commit(?: \((?:amend|merge)\))?:|cherry-pick:|revert:|"
                  r"rebase \((?:pick|reword|edit|squash|fixup|continue)\):|merge [^:]+: Merge made by )")


RUNGS = ("uncommon", "rare", "epic", "legendary")


def gate_flags(argv: list[str]) -> tuple[list[str], str] | None:
    """The arguments without `--floor`, `--why`, `--exclude`, `--judge`, `--effort`, `--final` and
    `--resolution`, and
    what they add to a commission's line (doc 127): the least rarity the round's judge may have and why
    it was asked for (#6499), vendors that wrote the branch though no session held it (doc 161 §6), a
    judge the owner or the brain asked for (5907 slice-2 D3), the effort the round runs at in place of
    its floor's (#7851), a round declared final, and a round on the resolution alone (#8286). A floor needs its reason, and a reason its floor;
    an effort is named once, by `--effort` or a pin's `@effort`, or both agree."""
    rest, floor, why, exclude, judge, effort, final, resolution = [], "", "", "", "", "", "", ""
    it = iter(argv)
    for a in it:
        if a == "--resolution":
            if resolution:
                return None
            resolution = " resolution"
        elif a == "--final":
            if final:
                return None
            final = " final"
        elif a == "--floor" or a.startswith("--floor="):
            named = a.split("=", 1)[1] if "=" in a else next(it, "")
            if named not in RUNGS or floor:
                return None
            floor = f" floor {named}"
        elif a == "--why" or a.startswith("--why="):
            said = a.split("=", 1)[1] if "=" in a else next(it, "")
            if not said.strip() or said.startswith("--") or len(said) > 500 or why:
                return None
            why = f" why {quote(said, safe='')}"
        elif a == "--exclude" or a.startswith("--exclude="):
            named = a.split("=", 1)[1] if "=" in a else next(it, "")
            if not named or named.startswith("-") or re.search(r"\s", named) or exclude:
                return None
            import gate_grammar
            if any(vendor not in gate_grammar.REVIEWERS for vendor in named.split(",")):
                return None
            exclude = f" exclude {named}"
        elif a == "--judge" or a.startswith("--judge="):
            named = a.split("=", 1)[1] if "=" in a else next(it, "")
            if judge or parse_judge(named) is None:
                return None
            judge = f" judge {named}"
        elif a == "--effort" or a.startswith("--effort="):
            named = a.split("=", 1)[1] if "=" in a else next(it, "")
            if effort or not re.fullmatch(r"[a-z]{1,32}", named):
                return None
            effort = f" effort {named}"
        else:
            rest.append(a)
    if bool(floor) != bool(why):
        return None
    pinned = (parse_judge(judge[len(" judge "):]) or {}).get("effort") if judge else None
    if effort and pinned and pinned != effort[len(" effort "):]:
        return None
    return rest, floor + why + exclude + judge + effort + final + resolution


# A judge pin is `<vendor>[/<model>][@<effort>]`: no empty field, no whitespace, nothing past the
# commission row's 128-char cap, and `@` only as a separator, so the parse is deterministic.
JUDGE_SPEC = re.compile(r"(?P<vendor>[^/@\s]{1,128})(?:/(?P<model>[^@\s]{1,128}))?(?:@(?P<effort>[^@\s]{1,128}))?")


def parse_judge(spec: str) -> dict | None:
    """The pin as the commission body's judge object, only the fields it names, None when the spec is
    malformed."""
    if not spec:
        return None
    named = JUDGE_SPEC.fullmatch(spec)
    if not named:
        return None
    return {k: v for k, v in named.groupdict().items() if v}


from verb_help import error, help_requested, script_help  # noqa: E402


def main(argv: list[str]) -> int:
    if help_requested("gate", argv):
        print(script_help("gate", topic=argv[0] if len(argv) == 2 else None))
        return 0
    try:
        argv, retry_id = retry_args(argv)
    except ValueError as why:
        return error("gate", str(why))
    if gone := [a for a in argv if a in ("--sandbox", "--full")]:
        print(f"{gone[0]} is gone: every round runs the full harness, inside its cage", file=sys.stderr)
        return 1
    split = gate_flags(argv)
    if split is None:
        return error("gate", "malformed gate flags")
    argv, extra = split
    parsed = speaker_flags(argv)
    if parsed and len(parsed[1]) > 1 and parsed[1][1] == "merge":
        return merge(parsed[1][0], parsed[1][2:], parsed[0], extra, retry_id)
    if parsed is None or len(parsed[1]) != 3 or parsed[1][1] not in ("review", "critic", "status", "cancel", "pr", "carry"):
        return error("gate", "gate requires a supported action and its argument")
    flags, (session, kind, what) = parsed
    if not session or session.startswith("-") or re.search(r"\s", session):
        return error("gate", "gate requires a session name")
    if kind in ("review", "pr", "carry") and not re.fullmatch(r"#?[0-9]+", what):
        return error("gate", f"gate {kind} requires a pull request number")
    if not what or what.startswith("-") or re.search(r"\s", what):
        return error("gate", f"gate {kind} requires its argument")
    if retry_id is not None and kind not in ("review", "critic", "carry"):
        return error("gate", f"gate {kind} does not take --retry=<id>")
    if extra and kind not in ("review", "critic"):
        return error("gate", f"gate {kind} does not take commission flags")
    # A commission id is diagnosed before the workspace is read: Python's or a Go one.
    if kind in ("status", "cancel") and not re.fullmatch(r"[0-9A-Za-z]{8,40}|[0-9a-fA-F]{8}(-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}", what):
        return error("gate", f"gate {kind} requires a commission id")
    ws = required_workspace_root(Path(os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()), timeout=2.0)
    return go_gate(ws, session, flags, kind, what, extra, retry_id)


def _commission_id(detail: str) -> str | None:
    import uuid
    try:
        return str(uuid.UUID(detail))
    except ValueError:
        return None


def _settled_refusal(e) -> str:
    """A refusal Go settled. One branch has a step that repairs it, so it names that; every branch
    says the reason Go gave, which `str(e)` carries (#9022)."""
    import refusal
    if e.code == "not-holding-branch":
        return refusal.refuse(str(e), send=[("taking: branch <name> token <t>",
                                             "to claim the branch this checkout is on, then send this again")])
    if e.code == "resolution-carries":
        return refusal.refuse(str(e), send=[("/2mw2lt:gate carry <n>", "since a carry admits this head and costs no round")])
    if e.code == "serial-not-served":
        return refusal.refuse(str(e), send=[("/2mw2lt:gate review <n> --resolution",
                                          "where a merge adapted the passed change: a round on that adaptation alone"),
                                         ("gate: review <repo> <branch> pr <n> token <t>",
                                          "to commission a round on this head, and carry its pass once it ships")])
    return refusal.refuse(str(e), send="/2mw2lt:gate")


def go_gate(ws: Path, session: str, flags, kind: str, what: str, extra: str, retry_id: str | None) -> int:
    """The gate verbs on Go: a review or a critique is `POST /gates`, a withdrawal
    `POST /gates/<id>/cancellation`, each on the session's own carrier."""
    import refusal
    import session_routes
    if kind == "cancel":
        import uuid
        try:
            gate = str(uuid.UUID(what))
        except ValueError:
            return error("gate", "gate cancel requires a commission id")
        try:
            session, token = speaking_as(ws, session, flags)
        except Refused as why:
            print(str(why), file=sys.stderr)
            return 1
        try:
            answer = session_routes.write(f"/gates/{gate}/cancellation", {}, session_routes._own_key("gate.cancel", gate),
                                         session_routes.SESSION_CARRIER, token)
            where = {"queue": "the queue", "run": "its run"}.get(answer.get("from"))
            reply = (f"cancelled: {gate}, withdrawn from {where}" if where
                     else refusal.retry(f"Go answered the withdrawal of {gate} without saying what it withdrew"))
        except session_routes.Refused as e:
            reply = refusal.refuse(str(e), send="/2mw2lt:gate") if session_routes.settled(e) else refusal.retry(str(e))
        except session_routes.Unsent as e:
            reply = refusal.retry(str(e))
        print(display_reply(reply))
        return 1 if reply.startswith("REJECTED") else 0
    if kind == "carry":
        return go_carry(ws, session, flags, int(what.lstrip("#")), retry_id)
    if kind in ("status", "pr"):
        return go_read(ws, session, flags, kind, what)
    floor = re.search(r" floor (\S+)", extra)
    why = re.search(r" why (\S+)", extra)
    judge = re.search(r" judge (\S+)", extra)
    effort = re.search(r" effort (\S+)", extra)
    final = " final" in extra
    resolution = " resolution" in extra
    rest = extra
    for piece in (floor[0] if floor else "", why[0] if why else "", f" judge {judge[1]}" if judge else "", effort[0] if effort else "",
                  " final" if final else "", " resolution" if resolution else ""):
        rest = rest.replace(piece, "", 1)
    if rest.strip():
        print(f"a {kind} on a Go workspace takes --floor, --why, --judge, --effort, --final and --resolution only, so far", file=sys.stderr)
        return 1
    if resolution and kind != "review":
        print("--resolution is a review's: a critique reads its whole design", file=sys.stderr)
        return 1
    here = Path.cwd()
    repo = checkout_slug(here, timeout=5)
    own = checkout_slug(ws, timeout=5)
    if not repo or own and own != repo:
        print(f"this workspace gates {own}, not {repo}" if repo else "this checkout names no origin to gate", file=sys.stderr)
        return 1
    try:
        session, token = speaking_as(ws, session, flags)
    except Refused as why:
        print(str(why), file=sys.stderr)
        return 1
    # A round that names no floor keeps the one its series was last commissioned at (Go decides).
    # Go pins what it observed of the pull request, or a critique at the branch head its mirror holds;
    # naming this checkout's head refuses a stale one.
    at = head(here)
    if kind == "critic":
        on = branch(here)
        if not on or not at:
            print("this checkout names no branch or no commit to critique", file=sys.stderr)
            return 1
        body = {"kind": "critic", "repo": repo, "branch": on, "doc": what, "head": at}
    else:
        body = {"repo": repo, "pr": int(what.lstrip("#"))}
        if at:
            body["head"] = at
        if resolution:
            # Go proves the resolution at this head, forcing the replay as for a carry, for the
            # commits this checkout did not make.
            body.update(scope="resolution", **replayed(here, at))
    if floor:
        body["floor"], body["why"] = floor[1], unquote(why[1])
    if judge:
        body["judge"] = parse_judge(judge[1])
    if effort:
        body["judge"] = {**body.get("judge", {}), "effort": effort[1]}
    if final:
        body["final"] = True
    this = retry_id or occurrence()
    print(f"id {this}", file=sys.stderr)
    uncertain, lever, request_ended = False, "", False
    try:
        answer = session_routes.write("/gates", body, this, session_routes.SESSION_CARRIER, token)
        # Awaiting records the request, not yet a round pinned to an observed head.
        if answer.get("id") and answer.get("state") == "awaiting":
            reply = (f"awaiting: {answer['id']} {kind}, waiting to pin the requested head; "
                     f"no round spent. Read /2mw2lt:gate status {answer['id']}")
        elif answer.get("id") and answer.get("uncommissioned") and answer.get("state") in ("refused", "cancelled"):
            request_ended = True
            why = f" ({wait_reason(answer['reason'])})" if answer.get("reason") else ""
            reply = f"{answer['state']}: {answer['id']} {kind}, not commissioned; no round spent{why}"
        elif answer.get("id") and answer.get("state"):
            why = f" ({wait_reason(answer['reason'])})" if answer.get("reason") else ""
            round_ = f" round {answer['round']}" if answer.get("round") else ""
            state = answer["state"]
            if state == "queued" and answer.get("priority") is not None:
                # The queue's face: where it stands and how many wait ahead (#6850).
                state = f"queued: {answer['priority']}, {answer.get('ahead', 0)} ahead"
            reply = f"commissioned: {answer['id']} {kind}{round_}, {state}{why}"
            # What frees a commission no judge's room frees, said whole (#7181).
            lever = answer.get("lever") or ""
        else:
            uncertain, reply = True, refusal.retry("Go answered the commission without naming it")
    except session_routes.Refused as e:
        uncertain = not session_routes.settled(e)
        holding = _commission_id(e.detail) if e.code in ("already-commissioned", "queued-already") else None
        if holding:
            # A first reply that was lost leaves the commission made: Go names it, so its status can be read.
            reply = refusal.refuse(str(e), send=f"/2mw2lt:gate status {holding}")
        else:
            reply = refusal.retry(str(e)) if uncertain else _settled_refusal(e)
    except session_routes.Unsent as e:
        uncertain, reply = True, refusal.retry(str(e))
    print(display_reply(reply))
    if lever:
        print(lever)
    if uncertain:
        # Go answers the same key with the commission it made, or makes it once if it never arrived.
        print(f"the commission may have landed: send it again with --retry={this} to learn which", file=sys.stderr)
    return 1 if request_ended or reply.startswith("REJECTED") else 0


# How many pages of a pull request's gates a read walks, so a cursor that repeats cannot loop.
GO_READ_PAGES = 20
# How long a carry waits for GitHub to deliver a head it was refused as unobserved, and how often it asks.
CARRY_UNOBSERVED_WAIT, CARRY_UNOBSERVED_POLL = 60.0, 5.0
# The most foreign commits a carry names, Go's CarryForeignMax; with more it names none.
CARRY_FOREIGN_MAX = 256
# How long a carry waits for its status's decision, and how often it asks: the decision takes a pass
# of the effect queue, or half a minute more while a read names the pull request's base.
CARRY_DECISION_WAIT, CARRY_DECISION_POLL = 60.0, 3.0


def go_read(ws: Path, session: str, flags, kind: str, what: str) -> int:
    """`status` reads one commission, `pr` every commission of a pull request here, from Go's gate
    reads on the session's carrier."""
    import urllib.parse
    import uuid
    import refusal
    import session_routes
    if kind == "status":
        try:
            path = f"/gates/{uuid.UUID(what)}"
        except ValueError:
            return error("gate", "gate status requires a commission id")
    else:
        repo = checkout_slug(Path.cwd(), timeout=5) or checkout_slug(ws, timeout=5)
        if not repo:
            print("this checkout names no origin to read gates for", file=sys.stderr)
            return 1
        path = "/gates?" + urllib.parse.urlencode({"repo": repo, "pr": what.lstrip("#")})
    try:
        session, token = speaking_as(ws, session, flags)
    except Refused as why:
        print(str(why), file=sys.stderr)
        return 1
    try:
        if kind == "status":
            print(gate_read(session_routes.get(path, token)))
            return 0
        rows, page, pages = [], path, 0
        while page and pages < GO_READ_PAGES:
            listed = session_routes.get(page, token)
            rows += listed.get("items") or []
            pages += 1
            page = f"{path}&{urllib.parse.urlencode({'next': listed['next']})}" if listed.get("next") else None
    except session_routes.Refused as e:
        reply = refusal.refuse(str(e), send="/2mw2lt:gate") if session_routes.settled(e) else refusal.retry(str(e))
        print(display_reply(reply), file=sys.stderr)
        return 1
    except session_routes.Unsent as e:
        print(display_reply(refusal.retry(str(e))), file=sys.stderr)
        return 1
    print("\n".join(gate_row(r) for r in rows) if rows else f"no gate was commissioned for #{what.lstrip('#')} here")
    if page:
        print(f"…and more: the newest {len(rows)} are shown", file=sys.stderr)
    return 0


def wait_reason(reason: str) -> str:
    """A floor wait names what is missing, on commissioning and every later status read."""
    return {"floor-unserved": "no available judge reaches this gate's floor"}.get(reason, reason)


def gate_row(r: dict) -> str:
    """One commission as a line: where it stands, its round and the head it judged. A queued one
    answers the queue's face first: its priority and how many wait ahead (#6850)."""
    if r.get("uncommissioned"):
        said = (f"{r.get('phase')}: {r.get('id')} {r.get('kind')} request "
                f"at {str(r.get('sha') or '')[:12]}, no round spent")
        return said + (f" ({wait_reason(r['why'])})" if r.get("why") and r.get("phase") != "awaiting" else "")
    place = ""
    if r.get("phase") == "queued" and r.get("priority"):
        place = f" {r['priority']}, {r.get('ahead', 0)} ahead —"
    said = f"{r.get('phase')}: {r.get('id')}{place} {r.get('kind')} round {r.get('round')}/{r.get('cap')} at {str(r.get('sha') or '')[:12]}"
    if r.get("verdict"):
        said += f", {r['verdict']}"
    if r.get("why"):
        said += f" ({wait_reason(r['why'])})"
    if r.get("passed_over"):
        said += "; passed over: " + passed_over(r["passed_over"])
    return said + (f" {r['review']}" if r.get("review") else "")


def passed_over(by_vendor: dict) -> str:
    """Why each vendor was not chosen, the vendors grouped under each reason: `codex, glm: host-full`."""
    vendors: dict[str, list[str]] = {}
    for vendor, reasons in sorted(by_vendor.items()):
        for reason in reasons:
            vendors.setdefault(reason, []).append(vendor)
    return "; ".join(f"{', '.join(v)}: {wait_reason(reason)}" for reason, v in sorted(vendors.items()))


def gate_read(r: dict) -> str:
    """A commission and, once judged, its findings, each under its id."""
    lines = [gate_row(r)]
    result = r.get("result") or {}
    if result.get("declared"):
        lines.append(f"declared: {result['declared']}")
    lines += [f"- {f.get('severity')} {f.get('file')}:{f.get('line')} {f.get('claim')} [{f.get('id')}, {f.get('status')}]"
              for f in result.get("findings") or []]
    return "\n".join(lines)


def merge(session: str, argv: list[str], flags, extra: str, retry_id: str | None) -> int:
    """`merge <pr> [--head <sha>]`: use the workspace's effective landing policy.
    A local required check yet to report is waited for as a `wait`, not a poll (#4203)."""
    import datetime
    import pr_merge
    import session_routes
    if extra:
        return error("gate", "gate merge takes only --head <sha> and --retry=<id>")
    if len(argv) == 3 and argv[1] == "--head" and re.fullmatch(r"[0-9a-f]{40}", argv[2]):
        expected = argv[2]
    elif len(argv) == 1:
        expected = None
    else:
        return error("gate", "gate merge requires a pull request number and at most --head <sha>")
    if not re.fullmatch(r"#?[1-9][0-9]*", argv[0]):
        return error("gate", "gate merge requires a pull request number")
    repo = checkout_slug(Path.cwd(), timeout=5)
    if not repo:
        print("this checkout's origin names no GitHub repository to merge in", file=sys.stderr)
        return 1
    n = int(argv[0].lstrip("#"))
    this = retry_id or occurrence()

    def native(slug: str, pr: int, head: str) -> str:
        print(f"id {this}", file=sys.stderr)
        answer = session_routes.write("/gates/merges", {"repo": slug, "pr": pr, "head": head}, this,
                                     session_routes.SESSION_CARRIER, token)
        if not isinstance(answer, dict) or answer.get("status") not in ("merged", "enqueued") or answer.get("head") != head:
            raise session_routes.Unsent(f"Go answered the native merge without confirming the landing of {head[:12]}")
        return answer["status"]

    try:
        ws = required_workspace_root(Path(os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()), timeout=2.0)
        _, token = speaking_as(ws, session, flags)
        # A retry must reach the native receipt even if policy changed or its read is unavailable.
        route = {"native_only": True}
        if retry_id is None:
            try:
                listed = session_routes.get("/settings", token)
            except (session_routes.Refused, session_routes.Unsent) as why:
                raise pr_merge.Refused(f"landing.integration_ci could not be read: {why}") from None
            rows = listed.get("settings") if isinstance(listed, dict) else None
            policies = [row.get("value") for row in rows
                        if isinstance(row, dict) and row.get("key") == "landing.integration_ci"] if isinstance(rows, list) else []
            if len(policies) != 1 or policies[0] not in ("required", "skip"):
                raise pr_merge.Refused("settings did not name an effective required or skip value for landing.integration_ci")
            route = {"integration_ci": policies[0]}
        print(f"#{n}: " + pr_merge.merge(repo, n, expected, say=lambda line: print(line, file=sys.stderr),
                                       native=native, **route))
    except pr_merge.Pending as why:
        at = (datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%MZ")
        print(f"not yet: {why}; it reports minutes after a push. Declare `verb.py wait {session} for at {at}`, "
              f"and merge again when it tells you", file=sys.stderr)
        return 75
    except (session_routes.Refused, session_routes.Unsent) as why:
        if isinstance(why, session_routes.Refused) and session_routes.settled(why):
            print(f"not merged: {why}", file=sys.stderr)
        else:
            print(f"merge outcome unknown: {why}; the merge may have landed. "
                  f"Send it again with --retry={this} to learn which", file=sys.stderr)
        return 1
    except (Refused, pr_merge.Refused) as why:
        print(f"not merged: {why}", file=sys.stderr)
        return 1
    return 0


def go_carry(ws: Path, session: str, flags, pr: int, retry_id: str | None) -> int:
    """Carry the pass on `pr` to this checkout's `HEAD` through Go's `POST /gates/carries`. Go proves
    the head itself; `equivalent` only forces its replay, sent unless every commit the branch holds
    beyond the trunk was made in this checkout. Those it did not make go as `foreign`, so Go forces
    the replay only for one the review did not hold: a branch continued from another session's."""
    import refusal
    import session_routes
    here = Path.cwd()
    at = head(here)
    repo, own = checkout_slug(here, timeout=5), checkout_slug(ws, timeout=5)
    if not at or not repo:
        print("this checkout names no commit or no origin to carry", file=sys.stderr)
        return 1
    if own and own != repo:
        print(f"this workspace gates {own}, not {repo} — carry it from the workspace that owns {repo}", file=sys.stderr)
        return 1
    try:
        session, token = speaking_as(ws, session, flags)
    except Refused as why:
        print(str(why), file=sys.stderr)
        return 1
    body = {"repo": repo, "pr": pr, "head": at, **replayed(here, at)}
    this = retry_id or occurrence()
    waited_until = time.monotonic() + CARRY_UNOBSERVED_WAIT
    while True:
        print(f"id {this}", file=sys.stderr)
        uncertain = False
        try:
            answer = session_routes.write("/gates/carries", body,
                                         this, session_routes.SESSION_CARRIER, token)
            if answer.get("id") and answer.get("proof"):
                reply = carry_decided(pr, at, answer, token)
            else:
                uncertain, reply = True, refusal.retry("Go answered the carry without naming it")
        except session_routes.Refused as e:
            # A head pushed a moment ago reaches Go when GitHub delivers it: wait that out under a new
            # key, since Go answers a refused key with the same refusal.
            if e.code == "carry-head-unobserved" and time.monotonic() < waited_until:
                drift = observed_head(e.detail)
                print(f"waiting for GitHub to deliver {at[:12]} to the gate"
                      + (f", which observes the pull request at {drift[:12]}" if drift and drift != at else ""),
                      file=sys.stderr)
                time.sleep(CARRY_UNOBSERVED_POLL)
                this = occurrence()
                continue
            uncertain = not session_routes.settled(e)
            if not uncertain and e.code == "carry-head" and "holds the pass already" in (e.detail or ""):
                # The pass already covers this head, most often the platform's own carry of a pure
                # merge: the thing this send asked for holds, so nothing is refused (#9022).
                print(display_reply(f"nothing to carry: the pass on #{pr} already covers {at[:12]}"))
                return 0
            if not uncertain and e.code == "carry-head-unobserved" and (drift := observed_head(e.detail)) and drift != at:
                # A checkout the pull request moved past, most often by the platform's pure merge:
                # the observed head is where the pass sits, so this checkout goes to it (#9022).
                reply = refusal.refuse(f"your checkout is at {at[:12]}, the pull request is at {drift[:12]}",
                                       send=[("/2mw2lt:gate carry <n>",
                                              "once this checkout is fast-forwarded to the pull request's head")])
            else:
                reply = refusal.retry(str(e)) if uncertain else _settled_refusal(e)
        except session_routes.Unsent as e:
            uncertain, reply = True, refusal.retry(str(e))
        break
    print(display_reply(reply))
    if uncertain:
        print(f"the carry may have landed: send it again with --retry={this} to learn which", file=sys.stderr)
    return 1 if reply.startswith("REJECTED") else 0


def observed_head(detail: str) -> str | None:
    """The pull request's observed head, from a carry-head-unobserved refusal's detail, where Go
    names it so the client can say how the checkout drifted (#9022). The head the observed-at
    wording names wins where the detail carries more than one hash, and a hash is 40 hex alone:
    a longer digest run is not a head, and neither is uppercase."""
    text = detail or ""
    named = re.search(r"observed at ([0-9a-f]{40})(?![0-9a-f])", text)
    if named:
        return named[1]
    alone = re.search(r"(?<![0-9a-f])([0-9a-f]{40})(?![0-9a-f])", text)
    return alone[1] if alone else None


def carry_decided(pr: int, at: str, answer: dict, token: str) -> str:
    """What became of a carry Go admitted: its status posted, not posted with the check its decision
    named, or still awaiting that decision when the wait is spent. Go admits a carry before it
    decides its status, so an admission alone is not a carry (#6112)."""
    import refusal
    import session_routes
    said = f"the pass on #{pr} to {at[:12]}, proved {answer['proof']}"
    told = "you are told when it is posted or refused"
    until, state = time.monotonic() + CARRY_DECISION_WAIT, "pending"
    print(f"waiting for the decision on {at[:12]}'s review status", file=sys.stderr)
    while True:
        try:
            read = session_routes.get(f"/gates/carries/{answer['id']}", token)
        except (session_routes.Refused, session_routes.Unsent) as e:
            return f"carried: {said}; its status could not be read ({e}): {told}"
        state = read.get("state") or state
        if state == "settled":
            return f"carried: {said}; review status posted"
        if state == "failed":
            return refusal.refuse(f"review status not posted on {at[:12]}: {read.get('check')}", send="/2mw2lt:gate")
        if time.monotonic() >= until:
            return f"carried: {said}; its status awaits its decision ({state}): {told}"
        time.sleep(CARRY_DECISION_POLL)


def replayed(here: Path, at: str) -> dict:
    """What a carry or a resolution round tells Go of `at`: `equivalent` unless every commit the branch
    holds beyond the trunk was made in this checkout, and those it did not make as `foreign`."""
    trunk = next((r for r in ("refs/remotes/origin/HEAD", "refs/remotes/origin/main", "refs/heads/main")
                  if _git(here, "rev-parse", "-q", "--verify", f"{r}^{{commit}}")), None)
    walked, after = _git_result(here, "rev-list", at, *([f"^{trunk}"] if trunk else []))
    made = made_here(_git(here, "reflog", "--format=%H %gs", "HEAD"))
    foreign = [c for c in after.split() if c not in made]
    told = {"equivalent": not walked or not trunk or bool(foreign)}
    if walked and trunk and foreign and len(foreign) <= CARRY_FOREIGN_MAX:
        told["foreign"] = foreign
    return told


def made_here(reflog: str) -> set[str]:
    """The commits `git reflog --format='%H %gs'` records this checkout making."""
    return {sha for sha, _, subject in (l.partition(" ") for l in reflog.splitlines()) if MADE.search(subject)}


def _git_result(here: Path, *args: str) -> tuple[bool, str]:
    try:
        p = subprocess.run(["git", "-C", str(here), *args], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return False, ""
    return p.returncode == 0, p.stdout.strip()


def _git(here: Path, *args: str) -> str:
    ok, output = _git_result(here, *args)
    return output if ok else ""


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
