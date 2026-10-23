"""`ci.py <your session> run …`: dispatch the parts of your pushed head's guards that read your
change to the CI pool, and be told the verdict on your stream (documentation/ci/14).

A dispatch names its parts — shards, chunks, the conformance — and never the whole suite: the
nightly and a release own that, so `--all` and `all` are refused. The tests your change touches
you spot-run where you sit (a test file, a `-run` pattern, one guard); the pool takes the parts
too wide for that and the guards your OS cannot run. `status` reads a run once; `log` prints a
failed run's output, cut to one guard's when `--guard` names it. Each goes through `gh` on this
machine's own credential.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parents[1]))
import json
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
from verb_help import error, help_requested, script_help  # noqa: E402

WORKFLOW = "steering.yml"
# A failed job's annotations name the guards that failed; GitHub's own line on a failed step names none.
EXIT_LINE = "Process completed with exit code"
TIMESTAMP = re.compile(r"^\d{4}-\d\d-\d\dT[\d:.]+Z ")
# The suite colours its output; a log is read as text.
ESCAPE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def gh(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["gh", *args], capture_output=True, text=True, timeout=60)


def gh_json(path: str):
    p = gh("api", path)
    if p.returncode != 0:
        raise OSError(f"gh api {path.split('?')[0]}: {p.stderr.strip() or p.returncode}")
    return json.loads(p.stdout)


def known_parts(root: Path) -> tuple[list[str], list[str]]:
    """The steering shards and coordination chunks this checkout's workflow knows, as the
    workflow's own scope reads them."""
    scope = (root / ".github/scripts/steering-scope.sh").read_text()
    m = re.search(r"^ALL='\[(.*)\]'$", scope, re.M)
    shards = [s.strip('"') for s in m[1].split(",")] if m else []
    p = subprocess.run(["bash", str(root / ".github/scripts/coordination-chunk.sh"), "--list"],
                       capture_output=True, text=True, timeout=30)
    chunks = p.stdout.split() if p.returncode == 0 else []
    # The merge tier is a shard and a chunk a run may name, which `all` never holds (documentation/ci/18);
    # a workflow from before it refuses the name itself.
    return shards + ["merge"] * bool(shards), chunks + ["merge"] * bool(chunks)


def pushed_head(here: Path, on: str) -> str:
    """The commit this checkout last saw its branch pushed to, or ""."""
    return subprocess.run(["git", "-C", str(here), "rev-parse", "-q", "--verify", f"refs/remotes/origin/{on}"],
                          capture_output=True, text=True, timeout=10).stdout.strip()


def parse_run(argv: list[str]) -> dict | str:
    """`run`'s flags as the dispatch's inputs, or why they are malformed. A list is names
    separated by commas or `none`; the whole suite — `--all`, or `all` among the names — is the
    nightly's and a release's, never a dispatch's, whose parts it names (documentation/ci/14)."""
    picked: dict = {}
    it = iter(argv)
    for a in it:
        if a == "--all":
            return "the whole suite is the nightly's and a release's: name the parts your change reads"
        elif a in ("--conformance", "--no-conformance"):
            picked["conformance"] = "true" if a == "--conformance" else "false"
        elif a in ("--shards", "--chunks") or a.startswith(("--shards=", "--chunks=")):
            name, _, value = a.partition("=")
            value = value or next(it, "")
            names = value.split(",")
            if not re.fullmatch(r"[a-z0-9-]+(,[a-z0-9-]+)*", value) or name[2:] in picked \
                    or len(names) > 1 and ("all" in names or "none" in names):
                return f"{name} takes names separated by commas, or none"
            if "all" in names:
                return f"{name} all is the whole suite: name the {name[2:]} your change reads, or none"
            picked[name[2:]] = value
        else:
            return f"run does not take {a}"
    if not picked:
        return "run needs the parts your change reads: --shards, --chunks or --conformance"
    return picked


def main(argv: list[str]) -> int:
    if help_requested("ci", argv):
        print(script_help("ci", topic=argv[0] if len(argv) == 2 else None))
        return 0
    try:
        argv, retry_id = retry_args(argv)
    except ValueError as why:
        return error("ci", str(why))
    parsed = speaker_flags(argv)
    if parsed is None or len(parsed[1]) < 2 or parsed[1][1] not in ("run", "status", "log"):
        return error("ci", "ci requires a session and run, status or log")
    flags, (session, kind, *rest) = parsed
    if not session or session.startswith("-") or re.search(r"\s", session):
        return error("ci", "ci requires a session name")
    if retry_id is not None and kind != "run":
        return error("ci", f"ci {kind} does not take --retry=<id>")
    here = Path.cwd()
    repo = origin_slug(here, timeout=5)
    if not repo:
        print("this checkout names no origin to run on", file=sys.stderr)
        return 1
    if kind == "run":
        picked = parse_run(rest)
        if isinstance(picked, str):
            return error("ci", picked)
        ws = required_workspace_root(Path(os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()), timeout=2.0)
        return run(ws, here, repo, session, flags, picked, retry_id)
    if not rest or not re.fullmatch(r"[0-9]+", rest[0]):
        return error("ci", f"ci {kind} requires a run id")
    run_id = int(rest[0])
    if kind == "status" and len(rest) == 1:
        return status(repo, run_id)
    if kind == "log" and (len(rest) == 1 or len(rest) == 3 and rest[1] == "--guard" and rest[2]):
        return log(repo, run_id, rest[2] if len(rest) == 3 else None)
    return error("ci", f"ci {kind} takes a run id" + (" and --guard <label>" if kind == "log" else ""))


def run(ws: Path, here: Path, repo: str, session: str, flags, picked: dict, retry_id: str | None) -> int:
    """Dispatch the run on this checkout's pushed head and register its wait."""
    import refusal
    import session_routes
    on, at = branch(here), head(here)
    if not on or not at:
        print("this checkout is on no branch, or no commit", file=sys.stderr)
        return 1
    pushed = pushed_head(here, on)
    if pushed != at:
        print(f"{at[:12]} is not pushed: origin/{on} is at {pushed[:12] or 'nothing'}. "
              "The pool runs what the branch holds; push, then run", file=sys.stderr)
        return 1
    shards, chunks = known_parts(here)
    inputs = {"head": at, "session": session}
    named = []
    for part, known in (("shards", shards), ("chunks", chunks)):
        asked = picked.get(part, "none")
        unknown = [n for n in asked.split(",") if n not in ("all", "none") and n not in known]
        if unknown:
            print(f"no such {part[:-1]}: {', '.join(unknown)}; this checkout's are {', '.join(known)}", file=sys.stderr)
            return 1
        inputs[part] = asked
        named.append(f"{part} {asked}")
    if picked.get("conformance") == "true":
        inputs["conformance"] = "true"
        named.append("conformance")
    else:
        inputs["conformance"] = "false"
    if picked.get("shards", "none") == "none" and picked.get("chunks", "none") == "none" and picked.get("conformance", "false") != "true":
        print("nothing is selected: no shard, no chunk and no conformance", file=sys.stderr)
        return 1
    try:
        session, token = speaking_as(ws, session, flags)
    except Refused as why:
        print(str(why), file=sys.stderr)
        return 1
    fields = ["-f", f"ref={on}", "-F", "return_run_details=true"]
    for k, v in inputs.items():
        fields += ["-f", f"inputs[{k}]={v}"]
    p = gh("api", "-X", "POST", f"repos/{repo}/actions/workflows/{WORKFLOW}/dispatches", *fields)
    try:
        dispatched = json.loads(p.stdout) if p.returncode == 0 else {}
    except ValueError:
        dispatched = {}
    run_id = dispatched.get("workflow_run_id")
    if not isinstance(run_id, int) or run_id < 1:
        why = p.stderr.strip() or "GitHub answered the dispatch without naming its run"
        print(f"not dispatched: {why}", file=sys.stderr)
        return 1
    said = f"dispatched {run_id} on {at[:12]}: {', '.join(named)} {dispatched.get('html_url') or ''}".rstrip()
    key = retry_id or occurrence()
    print(f"id {key}", file=sys.stderr)
    body = {"kind": "run", "repo": repo, "run": run_id, "head": at, "branch": on}
    try:
        answer = session_routes.wait(session, token, key, body)
        print(f"{said}\nregistered: wait {session} {answer['id']}, told on your stream when it ends")
        return 0
    except (session_routes.Refused, session_routes.Unsent) as e:
        print(said)
        print(display_reply(refusal.retry(f"its wait was not registered ({e}); read its verdict with ci.py {session} status {run_id}")))
        return 1


def failures(repo: str, jobs: list[dict]) -> list[str]:
    """Each failed job, with the guards its annotations name."""
    lines = []
    for job in jobs:
        if job.get("conclusion") not in ("failure", "timed_out"):
            continue
        try:
            notes = gh_json(f"repos/{repo}/check-runs/{job['id']}/annotations?per_page=50")
        except (OSError, ValueError):
            notes = []
        guards = [n["title"].strip() for n in notes if n.get("annotation_level") == "failure" and (n.get("title") or "").strip()
                  and not n["title"].startswith(EXIT_LINE)]
        lines.append(f"- {job.get('name')}" + (f": {', '.join(guards)}" if guards else ""))
    return lines


def status(repo: str, run_id: int) -> int:
    """The run read once: where it stands, which jobs run or wait, or its verdict with what failed."""
    try:
        r = gh_json(f"repos/{repo}/actions/runs/{run_id}")
        jobs = gh_json(f"repos/{repo}/actions/runs/{run_id}/jobs?filter=latest&per_page=100").get("jobs") or []
    except (OSError, ValueError) as e:
        print(f"run {run_id} was not read: {e}", file=sys.stderr)
        return 1
    said = f"run {run_id} on {str(r.get('head_sha') or '')[:12]}: "
    if r.get("status") != "completed":
        running = [j["name"] for j in jobs if j.get("status") == "in_progress"]
        waiting = [j["name"] for j in jobs if j.get("status") in ("queued", "waiting", "pending")]
        said += r.get("status") or "unknown"
        said += f"; running {', '.join(running)}" if running else ""
        said += f"; queued {', '.join(waiting)}" if waiting else ""
        print(f"{said} {r.get('html_url') or ''}".rstrip())
        return 0
    print(f"{said}{r.get('conclusion')} {r.get('html_url') or ''}".rstrip())
    for line in failures(repo, jobs):
        print(line)
    return 0


def guard_section(text: str, guard: str) -> list[str]:
    """The suite's output for one failed guard: its `FAIL <label>:` line and the lines it indented."""
    out, inside = [], False
    for raw in text.splitlines():
        line = TIMESTAMP.sub("", raw)
        if inside and line.startswith("     "):
            out.append(line)
            continue
        inside = line == f"FAIL {guard}:"
        if inside:
            out.append(line)
    return out


def log(repo: str, run_id: int, guard: str | None) -> int:
    """Each failed job's log, or the named guard's section of it."""
    try:
        jobs = gh_json(f"repos/{repo}/actions/runs/{run_id}/jobs?filter=latest&per_page=100").get("jobs") or []
    except (OSError, ValueError) as e:
        print(f"run {run_id} was not read: {e}", file=sys.stderr)
        return 1
    failed = [j for j in jobs if j.get("conclusion") in ("failure", "timed_out")]
    if not failed:
        print(f"run {run_id} has no failed job", file=sys.stderr)
        return 1
    found = False
    for job in failed:
        # gh refuses a body holding terminal escapes unless it is told to pass them (gh 2.99).
        p = gh("api", "--allow-escape-sequences", f"repos/{repo}/actions/jobs/{job['id']}/logs")
        if p.returncode != 0 and "unknown flag" in p.stderr:
            p = gh("api", f"repos/{repo}/actions/jobs/{job['id']}/logs")
        if p.returncode != 0:
            print(f"{job.get('name')}: its log was not read: {p.stderr.strip()}", file=sys.stderr)
            continue
        text = ESCAPE.sub("", p.stdout)
        lines = guard_section(text, guard) if guard else [TIMESTAMP.sub("", l) for l in text.splitlines()]
        if lines:
            found = True
            print(f"== {job.get('name')}")
            print("\n".join(lines))
    if guard and not found:
        print(f"no failed job's log holds the guard {guard!r}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
