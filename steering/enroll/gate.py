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
from door import UNSENT, display_reply, occurrence, outcome, remote, retry_args, say  # noqa: E402
import requestlog  # noqa: E402
from local_workspace import origin_slug, required_workspace_root  # noqa: E402

# A client that gives up before the daemon has pinned sends the line again, and a commission it
# was told failed may stand.
PIN_WAIT = 330.0
# The other gate verbs read GitHub or wait on the mirror's lock on the daemon's side, so they are
# given longer than a plain verb's send: a client that gives up first reports a write that landed
# as one that failed.
VERB_WAIT = 60.0
# A reflog subject that records a commit this checkout made, rather than one it was handed by a
# fetch, a fast-forward or a reset. `HEAD`'s reflog holds each rebase step; a branch's, only the tip.
# A resolved merge and a continued rebase are locally made, while the daemon proves their replay.
# Not `rebase (finish)`: it names the tip whatever made it, and a rebase that only moves forward
# makes it another writer's commit.
MADE = re.compile(r"^(?:commit(?: \((?:amend|merge)\))?:|cherry-pick:|revert:|"
                  r"rebase \((?:pick|reword|edit|squash|fixup|continue)\):|merge [^:]+: Merge made by )")
# A row of `gate: pr`'s answer whose round shipped: `<id> round <n>/<cap> at <sha12> <ts> <by>: ship it, by …`.
# A derived pass whose reviewer said otherwise reads `ship it (the reviewer said …)` (doc 150 §4.1).
SHIPPED = re.compile(r"^\S+ round \d+/\d+ at (?P<sha>[0-9a-f]{12}) \S+ [^:]*: ship it(?: \([^)]*\))?, by .*?(?:; source (?P<source>[0-9a-f]{40}))?$")


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
    if kind in ("status", "cancel"):
        import gate_grammar
        parser = gate_grammar.parse_status if kind == "status" else gate_grammar.parse_cancel
        if parser(f"gate: {kind} {what} token <t>") is None:
            return error("gate", f"gate {kind} requires a commission id")
    if not what or what.startswith("-") or re.search(r"\s", what):
        return error("gate", f"gate {kind} requires its argument")
    if retry_id is not None and kind not in ("review", "critic", "carry"):
        return error("gate", f"gate {kind} does not take --retry=<id>")
    if extra and kind not in ("review", "critic"):
        return error("gate", f"gate {kind} does not take commission flags")
    if kind == "carry":
        return carry(session, flags, what.lstrip("#"), retry_id)
    if kind in ("status", "cancel", "pr"):
        # A harness that holds no stream is never told how its gate ended; it asks (#1405).
        # A cancel needs no stream either, and is answered synchronously the same way (doc 98).
        ws = required_workspace_root(Path(os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()), timeout=2.0)
        try:
            session, token = speaking_as(ws, session, flags)
        except Refused as why:
            print(str(why), file=sys.stderr)
            return 1
        # `pr` asks what was ever commissioned for a pull request, by any session here (#1986).
        reply = say(f"gate: {kind} {what.lstrip('#') if kind == 'pr' else what} token {token}", timeout=VERB_WAIT)
        print(reply)
        return 1 if reply.startswith("REJECTED") else 0
    ws = required_workspace_root(Path(os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()), timeout=2.0)
    # The checkout the command runs in, not the project directory: a session working in a linked
    # worktree still carries the shared checkout as its project, and that is another branch.
    here = Path.cwd()
    on, repo = branch(here), origin_slug(here, timeout=5)
    if not on or not repo:
        print("this checkout names no branch or no origin to gate", file=sys.stderr)
        return 1
    # A session whose project directory is one workspace and whose checkout is another
    # repository sends a verb the workspace must refuse, and learns it only after a round
    # trip (#2750): say so here, where the split is visible.
    own = origin_slug(ws, timeout=5)
    if own and own != repo:
        print(f"this workspace gates {own}, not {repo} — commission it from the workspace that owns {repo}",
              file=sys.stderr)
        return 1
    try:
        session, token = speaking_as(ws, session, flags)
    except Refused as why:
        print(str(why), file=sys.stderr)
        return 1
    tail = f"pr {what.lstrip('#')}" if kind == "review" else f"doc {what}"
    # The commit this checkout is on travels with the branch: steering pins what GitHub answers,
    # and GitHub can answer an older head for a moment after a push (#1552).
    at = head(here)
    this = retry_id or occurrence()
    print(f"id {this}", file=sys.stderr)
    if remote():  # the name the door logs, records and journals this commission under
        print(f"request {requestlog.of_key(this)}", file=sys.stderr)
    reply = say(f"gate: {kind} {repo} {on} {tail}{f' head {at}' if at else ''}{extra} token {token}",
                timeout=PIN_WAIT, occurrence_id=this)
    print(display_reply(reply))
    return 1 if reply.startswith("REJECTED") else 0


def shipped(answer: str) -> re.Match[str] | None:
    rows = [line for line in answer.splitlines() if " round " in line]
    return SHIPPED.match(rows[-1]) if rows else None


def judged(answer: str) -> str | None:
    match = shipped(answer)
    return match["sha"] if match else None


def accepted_source(answer: str) -> str | None:
    match = shipped(answer)
    return (match["source"] or match["sha"]) if match else None


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


def carry(session: str, flags, pr: str, retry_id: str | None = None) -> int:
    """Carry the newest pass on `pr` to this checkout's `HEAD` (doc 162 §3). Locally made
    follow-ups retain the author-attested path; history from elsewhere asks the daemon to prove
    equivalence."""
    ws = required_workspace_root(Path(os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()), timeout=2.0)
    here = Path.cwd()
    on, at = branch(here), head(here)
    if not on or not at:
        print("this checkout names no branch or no commit to carry", file=sys.stderr)
        return 1
    own, repo = origin_slug(ws, timeout=5), origin_slug(here, timeout=5)
    if own and repo and own != repo:
        print(f"this workspace gates {own}, not {repo} — carry it from the workspace that owns {repo}",
              file=sys.stderr)
        return 1
    try:
        session, token = speaking_as(ws, session, flags)
    except Refused as why:
        print(str(why), file=sys.stderr)
        return 1
    answer = say(f"gate: pr {pr} token {token}", timeout=VERB_WAIT)
    if answer.startswith("REJECTED"):
        print(display_reply(answer), file=sys.stderr)   # the door's refusal, not a round that did not ship
        return 1
    short = judged(answer)
    source = accepted_source(answer)
    if short is None or source is None:
        print(f"the newest review of #{pr} did not ship, so there is no pass to carry", file=sys.stderr)
        return 1
    full = _git(here, "rev-parse", "-q", "--verify", f"{source}^{{commit}}")
    equivalent = not full
    if full:
        walked, after = _git_result(here, "rev-list", at, f"^{full}")
        made = made_here(_git(here, "reflog", "--format=%H %gs", "HEAD"))
        equivalent = not walked or any(c not in made for c in after.split())
    this = retry_id or occurrence()
    print(f"id {this}", file=sys.stderr)
    state, reply = outcome(f"gate: carry {pr} head {at}{' equivalent' if equivalent else ''} token {token}",
                           VERB_WAIT, this)
    print(display_reply(reply))
    if state == UNSENT:
        # The door may have carried it and lost only the answer: a resend under the same id is
        # answered from its record, or makes the carry once if it never arrived.
        print(f"the carry may have landed: send it again with --retry={this} to learn which", file=sys.stderr)
    return 1 if reply.startswith("REJECTED") else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
