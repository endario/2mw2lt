#!/usr/bin/env python3
"""`steering-launch.py exec forge <gh words…>`: run one forge write the owner grants a worker, after
checking every word of it (#7940, documentation/knowledge/decisions/workers-open-their-own-pull-requests.md).

A Codex session is granted this verb by one prefix rule on its stable path (codex_rules.py), so a
prefix that cannot refuse a trailing argument hands every argument here instead. The check is
trap_guard's own, under the verb's policy below; a refused command runs nothing.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))
import trap_guard  # noqa: E402
from verb_help import help_requested, script_help  # noqa: E402

# The verb grants what the ruling grants, and no merge: ready and merge stay out, so none rests on a
# hook Codex runs only once a person trusts it. A create names its head, which must be the session's
# branch: left out, gh picks it from the push configuration, which can name another branch. An edit
# or comment is held to the session's own pull request, and an issue comment too when its number is
# a pull request's, since gh comments on either.
VERBS = {verb: spec for verb, spec in trap_guard.FORGE_VERBS.items() if verb not in {("pr", "ready"), ("pr", "merge")}}
OWNED = {("pr", "create"): "head", ("pr", "edit"): "pull", ("pr", "comment"): "pull", ("issue", "comment"): "issue"}


def owns(kind: str, value: str | None, repo: str, cwd: str) -> str | None:
    """None when the session owns value, of kind as OWNED names it, else why not."""
    if kind == "pull":
        return trap_guard.pull_refusal(value, repo, cwd)
    if kind == "issue":
        return trap_guard.issue_refusal(value, repo, cwd)
    branch, why = trap_guard.branch_of(cwd)
    if value is None:
        return f"forge-head-required: name --head {branch or '<your branch>'}; gh would otherwise choose it"
    if not branch:
        return why
    return None if value == branch else f"forge-not-own-pull: --head names {value}, and this checkout is on {branch}"


def refusal(args: list[str], cwd: str, environ) -> str | None:
    """Why the gh words are not granted from the checkout at cwd, or None."""
    repo, why = trap_guard.grant_repo_why({"cwd": cwd}, environ)
    if repo is None:
        return f"forge-repo-not-granted: {why}"
    return trap_guard.gh_refusal(args, repo, lambda kind, value: owns(kind, value, repo, cwd), verbs=VERBS, owned=OWNED)


def main(argv: list[str]) -> int:
    if help_requested("forge", argv):
        print(script_help("forge"))
        return 0
    why = refusal(argv, os.getcwd(), os.environ)
    if why is not None:
        print(f"forge: refused, {why}; nothing ran", file=sys.stderr)
        return 2
    try:
        os.execvp("gh", ["gh", *argv])
    except OSError as e:
        print(f"forge: gh could not run: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
