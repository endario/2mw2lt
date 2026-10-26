"""Merge a passed pull request, or join its base's merge queue, and read back that it did (#8458).

The gh CLI's merge, on a branch whose ruleset requires a queue, prints only "The merge strategy for
main is set by the merge queue" and exits 0 without enqueuing while a required check has not
reported on the head. So this reads the head's required checks first, says which have yet to report
rather than polling for them, enqueues through steering's native command with the expected head, and
confirms the entry.
"""
from __future__ import annotations

import json
import subprocess
from typing import Callable

# The gate's own status: a head without it has no pass to wait for (doc 162).
REVIEW = "2mw2lt/review"
PASSING = {"SUCCESS", "SKIPPED", "NEUTRAL"}

READ = """query($owner:String!,$name:String!,$n:Int!){repository(owner:$owner,name:$name){
 defaultBranchRef{name}
 pullRequest(number:$n){id state isDraft headRefOid baseRefName mergeCommit{oid}
  mergeQueueEntry{position}
  baseRef{rules(first:50){nodes{type parameters{
   ... on RequiredStatusChecksParameters{requiredStatusChecks{context integrationId}}}}}}
  commits(last:1){nodes{commit{oid statusCheckRollup{contexts(first:100){nodes{__typename
   ... on CheckRun{name status conclusion checkSuite{app{databaseId}}}
   ... on StatusContext{context state}}}}}}}}}}"""
READY = "mutation($id:ID!){markPullRequestReadyForReview(input:{pullRequestId:$id}){pullRequest{isDraft}}}"
MERGE = ("mutation($id:ID!,$head:GitObjectID!){mergePullRequest(input:{pullRequestId:$id,"
         "expectedHeadOid:$head,mergeMethod:SQUASH}){pullRequest{state}}}")


class Refused(Exception):
    """GitHub refused, or the pull request is not one to merge; the message says which and why."""


class Pending(Exception):
    """A required check has yet to report on the head; the message names them."""


def graphql(query: str, **variables) -> dict:
    args = ["gh", "api", "graphql", "-f", f"query={query}"]
    for k, v in variables.items():
        args += ["-F" if isinstance(v, int) else "-f", f"{k}={v}"]
    try:
        p = subprocess.run(args, capture_output=True, text=True, timeout=60)
    except subprocess.TimeoutExpired:
        raise Refused("GitHub did not answer within 60 seconds: merge again to read what it holds") from None
    if p.returncode != 0:
        raise Refused(f"GitHub refused: {p.stderr.strip() or p.returncode}")
    return json.loads(p.stdout)["data"]


def standing(repo: dict, expected: str | None) -> tuple[str, str]:
    """What the pull request read as `repo` is waiting on: `refuse`, `merged` or `queued`, each with
    its sentence; `wait` naming the checks yet to report; or `go`."""
    pull = repo["pullRequest"]
    head = pull["headRefOid"]
    if pull["state"] == "MERGED":
        return "merged", f"merged as {((pull.get('mergeCommit') or {}).get('oid') or '')[:12]}"
    if pull["state"] != "OPEN":
        return "refuse", f"the pull request is {pull['state'].lower()}"
    if expected and head != expected:
        return "refuse", f"its head is {head[:12]}, not {expected[:12]}: carry the pass to the head you mean"
    if pull["baseRefName"] != repo["defaultBranchRef"]["name"]:
        return "refuse", f"its base is {pull['baseRefName']}, not {repo['defaultBranchRef']['name']}: it would merge there"
    # An entry is the head's: a push dequeues it. Its headCommit is the queue's own, null until built.
    if entry := pull.get("mergeQueueEntry"):
        return "queued", f"queued at position {entry['position']} on {head[:12]}"
    reported = pull["commits"]["nodes"][0]["commit"].get("statusCheckRollup") or {}
    contexts = (reported.get("contexts") or {}).get("nodes") or []
    pending = []
    for rule in pull["baseRef"]["rules"]["nodes"]:
        for check in (rule.get("parameters") or {}).get("requiredStatusChecks") or []:
            state = check_state(contexts, check["context"], check.get("integrationId"))
            if state is None and check["context"] == REVIEW:
                return "refuse", f"{head[:12]} holds no {REVIEW}: carry the pass to it, or commission a round"
            if state is None or state == "PENDING":
                pending.append(check["context"])
            elif state not in PASSING:
                return "refuse", f"{check['context']} is {state.lower()} on {head[:12]}"
    return ("wait", ", ".join(pending)) if pending else ("go", "")


def check_state(contexts: list[dict], name: str, app: int | None) -> str | None:
    """The required check's state on the head: a conclusion, PENDING, or None when it never reported."""
    for c in reversed(contexts):
        if c["__typename"] == "StatusContext" and c["context"] == name:
            return "PENDING" if c["state"] in ("PENDING", "EXPECTED") else c["state"]
        if (c["__typename"] == "CheckRun" and c["name"] == name
                and (app is None or ((c.get("checkSuite") or {}).get("app") or {}).get("databaseId") == app)):
            return c["conclusion"] if c["status"] == "COMPLETED" else "PENDING"
    return None


def queued(repo: dict) -> bool:
    return any(r["type"] == "MERGE_QUEUE" for r in repo["pullRequest"]["baseRef"]["rules"]["nodes"])


def merge(slug: str, n: int, expected: str | None, *, say: Callable[[str], None] = print,
          ask: Callable[..., dict] = graphql, native: Callable[[str, int, str], None]) -> str:
    """Merge #n of `slug`, or join its base's queue, on the head that holds the pass, and say which
    happened as GitHub reads it back; Refused when it did not, Pending when a required check has yet
    to report."""
    owner, name = slug.split("/", 1)
    read = lambda: ask(READ, owner=owner, name=name, n=n)["repository"]  # noqa: E731
    repo = read()
    verdict, said = standing(repo, expected)
    if verdict in ("merged", "queued"):
        return said
    if verdict == "refuse":
        raise Refused(said)
    pull = repo["pullRequest"]
    head = pull["headRefOid"]
    if pull["isDraft"]:
        ask(READY, id=pull["id"])
        say(f"marked #{n} ready for review")
    if verdict == "wait":
        raise Pending(f"{said} has not reported on {head[:12]}")
    into = queued(repo)
    if into:
        native(slug, n, head)
    else:
        ask(MERGE, id=pull["id"], head=head)
    verdict, said = standing(read(), head)
    if verdict in ("merged", "queued"):
        return said
    raise Refused(f"GitHub accepted the {'enqueue' if into else 'merge'} of {head[:12]}, and #{n} "
                  f"{'holds no queue entry' if into else 'is not merged'}")
