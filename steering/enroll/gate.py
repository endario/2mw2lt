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
import python_floor  # noqa: E402

python_floor.require()

import os
import re
import subprocess

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))
from bind import Refused  # noqa: E402
from connect import branch, head, speaker_flags, speaking_as  # noqa: E402
from door import display_reply, occurrence, retry_args  # noqa: E402
from local_workspace import origin_slug, required_workspace_root  # noqa: E402

# A reflog subject that records a commit this checkout made, rather than one it was handed by a
# fetch, a fast-forward or a reset. `HEAD`'s reflog holds each rebase step; a branch's, only the tip.
# A resolved merge and a continued rebase are locally made, while the daemon proves their replay.
# Not `rebase (finish)`: it names the tip whatever made it, and a rebase that only moves forward
# makes it another writer's commit.
MADE = re.compile(r"^(?:commit(?: \((?:amend|merge)\))?:|cherry-pick:|revert:|"
                  r"rebase \((?:pick|reword|edit|squash|fixup|continue)\):|merge [^:]+: Merge made by )")


def gate_flags(argv: list[str]) -> tuple[list[str], str] | None:
    """The arguments without `--tier`, `--sandbox`, `--exclude` and `--final`, and what they add to
    a commission's line (doc 127): the tier a series runs at, the read-only harness in place of the
    full one, vendors that wrote the branch though no session held it (doc 161 §6), and a round
    declared final."""
    rest, tier, harness, exclude, final = [], "", "", "", ""
    it = iter(argv)
    for a in it:
        if a == "--final":
            if final:
                return None
            final = " final"
        elif a in ("--sandbox", "--full"):
            if harness:
                return None
            harness = f" harness {a[2:]}"
        elif a == "--tier" or a.startswith("--tier="):
            named = a.split("=", 1)[1] if "=" in a else next(it, "")
            if named not in ("standard", "heavy") or tier:
                return None
            tier = f" tier {named}"
        elif a == "--exclude" or a.startswith("--exclude="):
            named = a.split("=", 1)[1] if "=" in a else next(it, "")
            if not named or named.startswith("-") or re.search(r"\s", named) or exclude:
                return None
            import gate_grammar
            if any(vendor not in gate_grammar.REVIEWERS for vendor in named.split(",")):
                return None
            exclude = f" exclude {named}"
        else:
            rest.append(a)
    return rest, tier + harness + exclude + final


from verb_help import error, help_requested, script_help  # noqa: E402


def main(argv: list[str]) -> int:
    if help_requested("gate", argv):
        print(script_help("gate", topic=argv[0] if len(argv) == 2 else None))
        return 0
    try:
        argv, retry_id = retry_args(argv)
    except ValueError as why:
        return error("gate", str(why))
    split = gate_flags(argv)
    if split is None:
        return error("gate", "malformed gate flags")
    argv, extra = split
    parsed = speaker_flags(argv)
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
            answer = session_routes.post(f"/gates/{gate}/cancellation", {}, session_routes._own_key("gate.cancel", gate),
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
    tier = re.search(r" tier (\S+)", extra)
    final = " final" in extra
    if extra.replace(tier[0] if tier else "", "").replace(" harness full", "").replace(" final", ""):
        print(f"a {kind} on a Go workspace takes --tier, --final and the full harness only, so far", file=sys.stderr)
        return 1
    here = Path.cwd()
    repo = origin_slug(here, timeout=5)
    own = origin_slug(ws, timeout=5)
    if not repo or own and own != repo:
        print(f"this workspace gates {own}, not {repo}" if repo else "this checkout names no origin to gate", file=sys.stderr)
        return 1
    try:
        session, token = speaking_as(ws, session, flags)
    except Refused as why:
        print(str(why), file=sys.stderr)
        return 1
    tier = tier[1] if tier else "standard"
    # Go pins what it observed of the pull request, or a critique at the branch head its mirror holds;
    # naming this checkout's head refuses a stale one.
    at = head(here)
    if kind == "critic":
        on = branch(here)
        if not on or not at:
            print("this checkout names no branch or no commit to critique", file=sys.stderr)
            return 1
        body = {"kind": "critic", "repo": repo, "branch": on, "doc": what, "tier": tier, "head": at}
    else:
        body = {"repo": repo, "pr": int(what.lstrip("#")), "tier": tier}
        if at:
            body["head"] = at
    if final:
        body["final"] = True
    this = retry_id or occurrence()
    print(f"id {this}", file=sys.stderr)
    uncertain = False
    try:
        answer = session_routes.post("/gates", body, this, session_routes.SESSION_CARRIER, token)
        if all(answer.get(k) for k in ("id", "round", "state")):
            why = f" ({answer['reason']})" if answer.get("reason") else ""
            reply = f"commissioned: {answer['id']} {kind} round {answer['round']}, {answer['state']}{why}"
        else:
            uncertain, reply = True, refusal.retry("Go answered the commission without naming it")
    except session_routes.Refused as e:
        uncertain = not session_routes.settled(e)
        reply = refusal.retry(str(e)) if uncertain else refusal.refuse(str(e), send="/2mw2lt:gate")
    except session_routes.Unsent as e:
        uncertain, reply = True, refusal.retry(str(e))
    print(display_reply(reply))
    if uncertain:
        # Go answers the same key with the commission it made, or makes it once if it never arrived.
        print(f"the commission may have landed: send it again with --retry={this} to learn which", file=sys.stderr)
    return 1 if reply.startswith("REJECTED") else 0


# How many pages of a pull request's gates a read walks, so a cursor that repeats cannot loop.
GO_READ_PAGES = 20


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
        repo = origin_slug(Path.cwd(), timeout=5) or origin_slug(ws, timeout=5)
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


def gate_row(r: dict) -> str:
    """One commission as a line: where it stands, its round and the head it judged."""
    said = f"{r.get('phase')}: {r.get('id')} {r.get('kind')} round {r.get('round')}/{r.get('cap')} at {str(r.get('sha') or '')[:12]}"
    if r.get("verdict"):
        said += f", {r['verdict']}"
    if r.get("why"):
        said += f" ({r['why']})"
    return said + (f" {r['review']}" if r.get("review") else "")


def gate_read(r: dict) -> str:
    """A commission and, once judged, its findings, each under its id."""
    lines = [gate_row(r)]
    result = r.get("result") or {}
    if result.get("declared"):
        lines.append(f"declared: {result['declared']}")
    lines += [f"- {f.get('severity')} {f.get('file')}:{f.get('line')} {f.get('claim')} [{f.get('id')}, {f.get('status')}]"
              for f in result.get("findings") or []]
    return "\n".join(lines)


def go_carry(ws: Path, session: str, flags, pr: int, retry_id: str | None) -> int:
    """Carry the pass on `pr` to this checkout's `HEAD` through Go's `POST /gates/carries`. Go proves
    the head itself; `equivalent` only forces its replay, sent unless every commit the branch holds
    beyond the trunk was made in this checkout."""
    import refusal
    import session_routes
    here = Path.cwd()
    at = head(here)
    repo, own = origin_slug(here, timeout=5), origin_slug(ws, timeout=5)
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
    trunk = next((r for r in ("refs/remotes/origin/HEAD", "refs/remotes/origin/main", "refs/heads/main")
                  if _git(here, "rev-parse", "-q", "--verify", f"{r}^{{commit}}")), None)
    walked, after = _git_result(here, "rev-list", at, *([f"^{trunk}"] if trunk else []))
    made = made_here(_git(here, "reflog", "--format=%H %gs", "HEAD"))
    equivalent = not walked or not trunk or any(c not in made for c in after.split())
    this = retry_id or occurrence()
    print(f"id {this}", file=sys.stderr)
    uncertain = False
    try:
        answer = session_routes.post("/gates/carries", {"repo": repo, "pr": pr, "head": at, "equivalent": equivalent},
                                     this, session_routes.SESSION_CARRIER, token)
        if answer.get("id") and answer.get("proof"):
            reply = f"carried: the pass on #{pr} to {at[:12]}, proved {answer['proof']}"
        else:
            uncertain, reply = True, refusal.retry("Go answered the carry without naming it")
    except session_routes.Refused as e:
        uncertain = not session_routes.settled(e)
        reply = refusal.retry(str(e)) if uncertain else refusal.refuse(str(e), send="/2mw2lt:gate")
    except session_routes.Unsent as e:
        uncertain, reply = True, refusal.retry(str(e))
    print(display_reply(reply))
    if uncertain:
        print(f"the carry may have landed: send it again with --retry={this} to learn which", file=sys.stderr)
    return 1 if reply.startswith("REJECTED") else 0


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
