#!/usr/bin/env python3
"""Run the fast-track the owner granted, past the merge queue (doc 180 §4.2–4.5).

    TOKEN=<lease token> fast-track.py --repo <owner/name> --grant <grant> [--ruleset <id>] [--end] <pr>...
    TOKEN=<lease token> fast-track.py --repo <owner/name> --grant <grant> [--ruleset <id>] --restore

The ruleset is the one holding the default branch's `merge_queue` rule unless `--ruleset` names it.

Run from a checkout of the repository, with a `gh` login that administers it (the owner's, by
their ruling of 2026-10-07). The lease token is read from the environment, never an argument.

It records the lowering before it lowers anything, adds the administrator role as a bypass actor
on the gate's ruleset, and for each pull request in order: confirms the head holds the App's
`2mw2lt/review` pass and its base is the trunk, squashes it onto the trunk's tip in a scratch
worktree and runs the cheap structural guards there, then merges it by `--admin`. The first that
fails stops the batch and goes back to its author. The bypass is removed on every exit this
process survives; `--restore` removes it after one it did not.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import shlex
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

ADMIN = {"actor_id": 5, "actor_type": "RepositoryRole", "bypass_mode": "always"}
REVIEW, BOT = "2mw2lt/review", "2mw2lt[bot]"
# What a run writes on the commit it tested and passed. A pull request's head carries its required
# suites as "Deferred to the merge group" while main has a queue, which no fast-track may read as a
# pass: only these say a suite ran (documentation/ci/07).
TESTED = ("steering/tested", "steering/tested-mini", "steering/tested-merge")
# Each suite a head must pass: its status context, the description prefix of a verdict that ran
# (never "Deferred…"), and the workflow that runs it. A dispatched run writes no required status,
# so a successful run of the workflow on the head is the other proof. `checks` runs on every
# pull request event, so it is waited for, never dispatched. A repository without a workflow has
# no such suite.
SUITES = (("checks", "checks", "The checks verdict", "checks.yml", False),
          ("steering", None, None, "steering.yml", True),
          ("coordination", "The coordination guards", "The coordination verdict", "coordination.yml", True))
GOING = ("queued", "in_progress", "pending", "waiting", "requested")
# The runs that test what they ran on. A pull request's own run completes successfully after only
# deferring to the merge group, so its success proves nothing.
TESTING = ("workflow_dispatch", "merge_group", "push")
# The group `checks` run's steps (doc 177 §3), and the module check only the suite runs (#4209).
# A repository without one of these scripts has no such guard, and is told which ran.
RANGE_GUARDS = (["bash", ".githooks/check-identity.sh"], ["bash", ".githooks/check-secrets.sh"],
                ["python3", "steering/protected_names.py"])
TREE_GUARDS = (["bash", ".githooks/check-docnum.sh"], ["bash", ".githooks/check-modules.sh"],
               ["bash", "steering/test/identities_test.sh"])


# Every call is bounded, so a hung `gh` or `git` cannot keep the batch from its restore.
CALL_SECONDS, GUARD_SECONDS = 120, 600
# Guards that cannot run refuse here rather than pass: nothing checks a fast-tracked tree after us.
GUARD_ENV = {**os.environ, "CHECK_MODULES_STRICT": "1"}


class Refused(Exception):
    def __init__(self, text: str, occurrence: str | None = None):
        super().__init__(text)
        self.occurrence = occurrence


class Unavailable(Refused):
    """GitHub could not be asked: no fault of a pull request's, so it stops the batch and is never
    recorded as a refusal."""


class Unresolved(Refused):
    """GitHub was asked to merge and its answer could not be read back: the merge may have landed.
    Never recorded as a refusal; its reservation stands, so a confirmed merge can still be recorded."""


def run(args: list[str], *, cwd: str | None = None, stdin: str | None = None, env: dict | None = None,
        timeout: float = CALL_SECONDS) -> str:
    try:
        p = subprocess.run(args, cwd=cwd, input=stdin, capture_output=True, text=True, env=env, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise Refused(f"{' '.join(args[:3])} did not answer within {int(timeout)}s") from None
    if p.returncode != 0:
        raise Refused(f"{' '.join(args[:3])} failed (exit {p.returncode}): {(p.stderr or p.stdout).strip()[-400:]}")
    return p.stdout


def door_say(line: str, attempts: int = 3, sleep=time.sleep) -> str:
    """One record, sent until the door answers: a send whose answer was lost goes again under the
    same occurrence id, which the door answers from its record rather than writing twice."""
    import door
    key = door.occurrence()
    for attempt in range(attempts):
        state, text = door.line_from(line, key)
        if state != door.UNSENT:
            break
        sleep(2 * (attempt + 1))
    if state != door.SETTLED or text.startswith(door.REJECTED):
        # The id goes with the refusal: a send whose answer was lost may have landed, and only a
        # resend under the same id is answered from the door's record.
        raise Refused(text, key if state == door.UNSENT else None)
    return text


def is_admin(actor: dict) -> bool:
    """The bypass this tool adds, and no other: an administrator bypass the owner set in another
    mode is theirs, and is neither refused nor removed."""
    return all(actor.get(k) == v for k, v in ADMIN.items())


def queue_ruleset(gh, repo: str) -> int:
    """The ruleset holding the default branch's merge queue: the one a bypass must sit in for an
    administrator's merge to go past the queue (doc 177 §1.4)."""
    for rs in gh("api", f"repos/{repo}/rulesets") or []:
        full = gh("api", f"repos/{repo}/rulesets/{rs['id']}")
        if any(r.get("type") == "merge_queue" for r in full.get("rules") or []):
            return rs["id"]
    raise Refused(f"{repo} has no ruleset with a merge queue: there is nothing to fast-track past")


class FastTrack:
    SCOPE = Path(".github/scripts/steering-scope.sh")
    def __init__(self, repo: str, grant: str, ruleset: int | None, token: str, *, run=run, say=door_say,
                 sleep=time.sleep):
        self.repo, self.grant, self.token = repo, grant, token
        self.run, self._say, self.sleep = run, say, sleep
        self.ruleset = ruleset if ruleset is not None else queue_ruleset(self.gh, repo)
        self.unrestored: str | None = None
        self.unresolved: int | None = None
        self.unrecorded: int | None = None
        self._has: dict[str, bool] = {}

    def say(self, act: str) -> str:
        return self._say(f"fasttrack: token {self.token} {act}")

    def record(self, act: str) -> bool:
        """An outcome GitHub already settled, sent to the ledger; on failure, the exact line to send
        by hand, and the batch stops so nothing further goes unrecorded."""
        try:
            self.say(act)
            return True
        except Refused as e:
            retry = f" --retry={e.occurrence}" if e.occurrence else ""
            line = shlex.quote(f"fasttrack: token TOKEN {act}").replace("TOKEN", "'\"$TOKEN\"'", 1)
            print(f"fast-track: GitHub's outcome is settled but not recorded ({e}). The batch stops here. "
                  f"Record it with: printf '%s' {line} | python3 {Path(__file__).resolve().parent / 'door.py'} --say{retry}",
                  file=sys.stderr)
            return False

    def gh(self, *args: str, stdin: str | None = None):
        try:
            out = self.run(["gh", *args], stdin=stdin)
        except Refused as e:
            raise Unavailable(str(e)) from None
        try:
            return json.loads(out) if out.strip() else None
        except ValueError:
            raise Refused(f"gh {' '.join(args[:2])} answered something that is not JSON: {out[:200]}") from None

    def rules(self) -> dict:
        return self.gh("api", f"repos/{self.repo}/rulesets/{self.ruleset}")

    def put(self, rs: dict, bypass: list) -> None:
        body = {k: rs[k] for k in ("name", "target", "enforcement", "conditions", "rules")}
        self.gh("api", "-X", "PUT", f"repos/{self.repo}/rulesets/{self.ruleset}", "--input", "-",
                stdin=json.dumps({**body, "bypass_actors": bypass}))

    def restore(self) -> None:
        rs = self.rules()
        held = [a for a in rs.get("bypass_actors") or [] if isinstance(a, dict)]
        if any(is_admin(a) for a in held):
            self.put(rs, [a for a in held if not is_admin(a)])
        if any(is_admin(a) for a in self.rules().get("bypass_actors") or []):
            raise Refused(f"ruleset {self.ruleset} still lets an administrator bypass it after the restore")
        self.say(f"restored {self.grant} ruleset {self.ruleset}")

    def trunk(self) -> str:
        return self.gh("repo", "view", self.repo, "--json", "defaultBranchRef")["defaultBranchRef"]["name"]

    def statuses(self, sha: str) -> list:
        return self.gh("api", f"repos/{self.repo}/commits/{sha}/statuses?per_page=100") or []

    def has(self, workflow: str) -> bool:
        if workflow not in self._has:
            # Only GitHub's own "not found" waives a suite: any other failure to ask stops the batch,
            # rather than reading as a repository without the suite.
            try:
                self.gh("api", f"repos/{self.repo}/contents/.github/workflows/{workflow}")
                self._has[workflow] = True
            except Refused as e:
                if "(HTTP 404)" not in str(e):
                    raise Refused(f"could not tell whether {self.repo} has {workflow}: {e}") from None
                self._has[workflow] = False
        return self._has[workflow]

    def runs(self, sha: str, workflow: str) -> list:
        """The runs of `workflow` on `sha` that test it, newest first."""
        runs = self.gh("run", "list", "--repo", self.repo, "--workflow", workflow, "--commit", sha,
                       "--json", "status,conclusion,url,event", "--limit", "10") or []
        return [r for r in runs if r.get("event") in TESTING]

    def main_is_green(self, trunk: str) -> None:
        """Refuse a batch until the trunk's tip has passed its suite: a break two pull requests made
        together shows on the tip, and stops the next batch rather than stacking under it."""
        if not self.has("steering.yml"):
            return
        tip = self.gh("api", f"repos/{self.repo}/commits/{trunk}")["sha"]
        if any(st.get("context") in TESTED and st.get("state") == "success" for st in self.statuses(tip)):
            return
        runs = self.runs(tip, "steering.yml")
        if runs and runs[0].get("status") == "completed" and runs[0].get("conclusion") == "success":
            return
        failed = next((r for r in runs if r.get("conclusion") == "failure"), None)
        if failed:
            raise Refused(f"{trunk} is red at {tip[:12]} ({failed.get('url')}): fix it forward before the next batch")
        going = next((r for r in runs if r.get("status") in GOING), None)
        raise Refused(f"{trunk}'s suite has not passed on {tip[:12]} yet"
                      + (f" ({going.get('url')}); run the batch again once it has" if going else
                         "; no run is going for it: dispatch steering.yml on it"))

    def proven(self, pr: int, head: str, branch: str, held: list, trunk: str) -> tuple[str, str] | None:
        """None when `head`'s own suites have passed; otherwise ("red" | "wait", why). A suite with
        no run going is dispatched on the head's branch; one whose run failed is not run again."""
        def status(context, real):
            return next((st.get("state") for st in held if st.get("context") == context
                         and str(st.get("description") or "").startswith(real)), None)
        waiting = []
        for name, context, real, workflow, dispatch in SUITES:
            if not self.has(workflow):
                continue
            if name == "steering" and (any(st.get("context") in TESTED and st.get("state") == "success" for st in held)
                                       or not self.reads(pr, head, trunk)):
                continue
            if context and status(context, real) == "success":
                continue
            if context and status(context, real) in ("failure", "error"):
                return "red", f"its {name} verdict failed"
            runs = self.runs(head, workflow)
            last = runs[0] if runs else {}
            if last.get("status") == "completed" and last.get("conclusion") == "success":
                continue
            if last.get("status") == "completed" and last.get("conclusion") in ("failure", "timed_out"):
                return "red", f"its {name} run failed ({last.get('url')})"
            if last.get("status") in GOING:
                waiting.append(f"{name} running")
            elif dispatch:
                self.run(["gh", "workflow", "run", workflow, "--repo", self.repo, "--ref", branch,
                          *(["-f", "lane=auto"] if workflow == "steering.yml" else [])])
                waiting.append(f"{name} dispatched")
            else:
                waiting.append(f"{name} not reported")
        return ("wait", ", ".join(waiting)) if waiting else None

    def reads(self, pr: int, head: str, trunk: str) -> bool:
        """Whether the steering suite reads anything `head` changes, by the scope script the workflow
        uses, read from this checkout's root; a head that changes only what the suite never reads
        needs no run of it."""
        root = Path(self.run(["git", "rev-parse", "--show-toplevel"]).strip() or ".")
        script = root / self.SCOPE
        if not script.exists():
            return True
        ref = f"refs/fast-track/scope-{pr}"
        self.run(["git", "fetch", "--quiet", "origin", f"+refs/heads/{trunk}:refs/fast-track/trunk",
                  f"+refs/pull/{pr}/head:{ref}"])
        base = self.run(["git", "merge-base", "refs/fast-track/trunk", ref]).strip()
        return "run=false" not in self.run(["bash", str(script), base, head]).split()

    def head_of(self, pr: int, trunk: str) -> tuple[str, str, list]:
        """The head, its branch and its statuses, refused unless it is open, on the trunk and holds
        the gate's pass."""
        info = self.gh("pr", "view", str(pr), "--repo", self.repo, "--json",
                       "headRefOid,headRefName,baseRefName,state,isDraft")
        if info.get("state") != "OPEN" or info.get("isDraft"):
            raise Refused(f"#{pr} is {'a draft' if info.get('isDraft') else info.get('state', '').lower()}")
        if info.get("baseRefName") != trunk:
            raise Refused(f"#{pr}'s base is {info.get('baseRefName')}, not {trunk}")
        head = info["headRefOid"]
        statuses = self.statuses(head)
        review = next((s.get("state") for s in statuses if s.get("context") == REVIEW
                       and (s.get("creator") or {}).get("login") == BOT), None)
        if review != "success":
            raise Refused(f"#{pr}'s head {head[:12]} holds no {REVIEW} pass from the App ({review or 'none'})")
        return head, info.get("headRefName") or "", statuses

    def guard(self, pr: int, head: str, trunk: str) -> None:
        """The cheap structural guards on the squash of `head` onto the trunk's tip."""
        ref = f"refs/fast-track/pr-{pr}"
        self.run(["git", "fetch", "--quiet", "origin", f"+refs/heads/{trunk}:refs/fast-track/trunk", f"+refs/pull/{pr}/head:{ref}"])
        if self.run(["git", "rev-parse", ref]).strip() != head:
            raise Refused(f"#{pr}'s head moved past {head[:12]} while it was read")
        scratch = tempfile.mkdtemp(prefix=f"fast-track-{pr}-")
        try:
            self.run(["git", "worktree", "add", "--quiet", "--detach", scratch, "refs/fast-track/trunk"])
            base = self.run(["git", "merge-base", "refs/fast-track/trunk", ref], cwd=scratch).strip()
            ran = []
            for g in RANGE_GUARDS:
                if Path(scratch, g[1]).exists():
                    self.run([*g, f"{base}..{head}"], cwd=scratch, env=GUARD_ENV, timeout=GUARD_SECONDS)
                    ran.append(g[1])
            try:
                self.run(["git", "merge", "--squash", "--quiet", ref], cwd=scratch)
            except Refused as e:
                raise Refused(f"#{pr} does not squash cleanly onto {trunk}: {e}") from None
            for g in TREE_GUARDS:
                if Path(scratch, g[1]).exists():
                    self.run(list(g), cwd=scratch, env=GUARD_ENV, timeout=GUARD_SECONDS)
                    ran.append(g[1])
            print(f"#{pr} passed {', '.join(ran) or 'no guard this repository has'}")
        finally:
            for cleanup in (["git", "worktree", "remove", "--force", scratch], ["git", "update-ref", "-d", ref]):
                try:
                    subprocess.run(cleanup, capture_output=True, timeout=CALL_SECONDS)
                except subprocess.TimeoutExpired:
                    print(f"fast-track: {' '.join(cleanup[:3])} did not finish; remove {scratch} by hand", file=sys.stderr)

    def merge(self, pr: int, head: str) -> str:
        """The merge commit, read back from GitHub whatever the merge command answered: a command that
        failed or timed out may still have merged, and `gh` enqueues rather than merges when the
        bypass did not take. Only a merged state counts as a merge."""
        failed = None
        try:
            self.run(["gh", "pr", "merge", str(pr), "--repo", self.repo, "--squash", "--admin",
                      "--match-head-commit", head])
        except Refused as e:
            failed = e
        info = None
        for attempt in range(3):
            try:
                info = self.gh("pr", "view", str(pr), "--repo", self.repo, "--json", "state,mergeCommit") or {}
                break
            except Refused as e:
                last = e
                self.sleep(2 * (attempt + 1))
        if info is None:
            raise Unresolved(f"#{pr} was asked to merge{f' ({failed})' if failed else ''} and its state could not be "
                             f"read back ({last})")
        sha = (info.get("mergeCommit") or {}).get("oid")
        if info.get("state") == "MERGED" and sha:
            return sha
        if failed:
            raise Refused(f"#{pr} did not merge: {failed}")
        raise Refused(f"#{pr} was not merged past the queue (state {info.get('state')}): check whether it was queued instead")

    def ready(self, prs: list[int], trunk: str) -> list[tuple[int, str]]:
        """The leading run of `prs` whose heads carry the gate's pass and their own suites' pass, each
        with the head that was proven. A head refused outright is recorded and goes back to its
        author; one still waiting on its suites is left for a later batch. Every head is checked, so
        the suites of those behind the first unready one run side by side with it."""
        out, stop = [], False
        for pr in prs:
            try:
                head, branch, held = self.head_of(pr, trunk)
            except Unavailable:
                raise
            except Refused as e:
                verdict = ("red", str(e))
            else:
                # Not caught here: a failure to ask GitHub about the suites is no fault of the pull
                # request's, so it stops the batch rather than handing the head back.
                verdict = self.proven(pr, head, branch, held, trunk)
            if verdict and verdict[0] == "red":
                why = " ".join(verdict[1].split())[:300]
                print(f"#{pr} refused: {why}. Hand it back to its author.")
                if not self.record(f"refused {self.grant} pr {pr} {why}"):
                    # Nothing is lowered behind a record the ledger did not take.
                    self.unrecorded = pr
                    raise Refused(f"#{pr}'s refusal was not recorded; nothing was lowered or merged")
                stop = True
            elif verdict:
                print(f"#{pr} is not ready: {verdict[1]}; run the batch again once its suites pass")
                stop = True
            elif not stop:
                out.append((pr, head))
        return out

    def batch(self, prs: list[int]) -> list[int]:
        """Merge `prs` in order, stopping at the first that cannot go; the merged ones. Only the
        leading run whose own suites passed is merged, and only while the trunk's tip is green."""
        trunk, merged = self.trunk(), []
        self.main_is_green(trunk)
        proven = self.ready(prs, trunk)
        if not proven:
            print("nothing is ready: run the batch again once the dispatched suites pass")
            return merged
        rs = self.rules()
        before = [a for a in rs.get("bypass_actors") or [] if isinstance(a, dict)]
        if any(is_admin(a) for a in before):
            raise Refused(f"ruleset {self.ruleset} already lets an administrator bypass it: run --restore first")
        # Recorded first: a crash between the two still leaves the record the kick reads (doc 180 §4.4).
        self.say(f"lowered {self.grant} ruleset {self.ruleset} {json.dumps(before, separators=(',', ':'))}")
        try:
            # Inside the restore's reach: a PUT that GitHub applied but `gh` reported as failed
            # still leaves a bypass, which the restore reads and removes.
            self.put(rs, before + [ADMIN])
            for pr, was in proven:
                try:
                    head = self.head_of(pr, trunk)[0]
                    if head != was:
                        raise Refused(f"#{pr}'s head moved from {was[:12]} to {head[:12]} after its suites were checked")
                    self.guard(pr, head, trunk)
                    # Reserved before GitHub is asked: the door refuses it past the grant's count or
                    # after it lapsed, so nothing merges that the grant did not admit.
                    self.say(f"merging {self.grant} pr {pr} head {head}")
                    sha = self.merge(pr, head)
                except Unresolved as e:
                    print(f"#{pr}: {e}. The batch stops here. Look at #{pr} on GitHub: if it merged, record it with "
                          f"`fasttrack: token <lease token> merged {self.grant} pr {pr} head {head} as <merge sha>`; "
                          f"if not, with `refused`.", file=sys.stderr)
                    self.unresolved = pr
                    break
                except Unavailable as e:
                    print(f"#{pr}: GitHub could not be asked ({e}). The batch stops here; run it again.", file=sys.stderr)
                    break
                except Refused as e:
                    why = " ".join(str(e).split())[:300]
                    print(f"#{pr} refused, and the batch stops here: {why}. Hand it back to its author.")
                    if not self.record(f"refused {self.grant} pr {pr} {why}"):
                        self.unrecorded = pr
                    break
                # Merged on GitHub, whatever the ledger says next: counted before it is recorded.
                merged.append(pr)
                print(f"#{pr} merged as {sha[:12]}")
                if not self.record(f"merged {self.grant} pr {pr} head {head} as {sha}"):
                    self.unrecorded = pr
                    break
        finally:
            print(f"merged: {', '.join(f'#{p}' for p in merged) or 'none'}")
            try:
                self.restore()
            except Refused as e:
                self.unrestored = str(e)
                print(f"fast-track: RULESET {self.ruleset} NOT RESTORED: {e}. Run --restore now.", file=sys.stderr)
        return merged


def main(argv: list[str]) -> int:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from verb_help import help_requested, script_help
    if help_requested("fast-track", argv, bare=False):
        print(script_help("fast-track", topic=argv[0] if len(argv) == 2 else None))
        return 0
    ap = argparse.ArgumentParser(prog="fast-track.py", description=__doc__.split("\n\n")[0])
    ap.add_argument("--repo", required=True)
    ap.add_argument("--grant", required=True)
    ap.add_argument("--ruleset", type=int, help="the ruleset holding the merge queue, when it cannot be found")
    ap.add_argument("--restore", action="store_true", help="remove a bypass a crashed run left, and record it")
    ap.add_argument("--end", action="store_true", help="end the grant once the batch is done")
    ap.add_argument("prs", nargs="*", type=int)
    a = ap.parse_args(argv)
    token = os.environ.get("TOKEN") or os.environ.get("STEERING_LEASE_TOKEN")
    if not token:
        print("fast-track: set TOKEN to the lease token promote.py printed", file=sys.stderr)
        return 2
    if a.restore == bool(a.prs):
        print("fast-track: name the pull requests to merge, or --restore alone", file=sys.stderr)
        return 2
    try:
        ft = FastTrack(a.repo, a.grant, a.ruleset, token)
        if a.restore:
            ft.restore()
            print(f"ruleset {ft.ruleset} restored")
            return 0
        merged = ft.batch(a.prs)
        if ft.unrestored:
            return 3
        if ft.unresolved or ft.unrecorded:
            return 4
        # Only a batch that merged everything it named ends its grant: one that left heads for their
        # suites is run again under the same grant.
        if a.end and merged == a.prs:
            ft.say(f"ended {a.grant} batch done")
    except Refused as e:
        print(f"fast-track: {e}", file=sys.stderr)
        return 1
    print(f"{len(merged)} merged. main's push run tests the batch's tip: "
          f"gh run list -R {a.repo} --workflow steering.yml --branch main --limit 1")
    return 0 if len(merged) == len(a.prs) else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
