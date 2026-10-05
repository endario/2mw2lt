"""An agent moves itself to the release its orchestrator states (doc 147 §5-§7): which door's
answer to take, whether to move, and the fetch and build of the new release beside the one that
runs, so nothing the agent is doing waits on them."""
from __future__ import annotations

import asyncio
import hashlib
import os
import random
import re
import shutil
import subprocess
import tarfile
import time
from pathlib import Path

BUILD_TRIES = 3


class Refused(Exception):
    """A package that is not the release its door stated."""


class BuildFailed(Exception):
    """A fetched release that did not build into a working agent."""


def vote(answers: list[tuple[str, dict | None]]) -> tuple[str, dict] | str | None:
    """Every door that answered names one sha, and the first of them is the source: its digest
    is the one checked against the bytes that same door serves. Doors that disagree are `split`."""
    voters = [(door, target) for door, target in answers if valid(target)]
    if not voters:
        return None
    if len({target["sha"] for _, target in voters}) > 1:
        return "split"
    return voters[0]


SHA = re.compile(r"[0-9a-f]{40}")
DIGEST = re.compile(r"sha256:[0-9a-f]{64}")


def valid(target) -> bool:
    """A target whose sha may name a path and whose digest and size can be checked; anything else
    a door says is no vote."""
    return (isinstance(target, dict) and isinstance(target.get("sha"), str) and bool(SHA.fullmatch(target["sha"]))
            and isinstance(target.get("digest"), str) and bool(DIGEST.fullmatch(target["digest"]))
            and type(target.get("bytes")) is int and target["bytes"] > 0)


def skip(target: dict, own: str | None, root: Path) -> str | None:
    """Why not to move to `target`, or None to move."""
    if own is None:
        return "checkout"
    if target["sha"] == own:
        return "current"
    if (root / "failed" / target["sha"]).exists():
        return "failed"
    return None


def record_build_failure(root: Path, sha: str) -> bool:
    """A build fails on a network fault as readily as on a real one, so a sha is marked failed
    only on its third. Answers whether this one marked it."""
    failed = root / "failed"
    failed.mkdir(parents=True, exist_ok=True)
    tries = failed / f"{sha}.tries"
    try:
        count = int(tries.read_text().strip() or 0) + 1
    except (OSError, ValueError):
        count = 1
    tries.write_text(f"{count}\n")
    if count < BUILD_TRIES:
        return False
    (failed / sha).write_text(f"the build failed {count} times\n")
    return True


def built(root: Path, sha: str) -> Path | None:
    """`releases/<sha>` when it is that release and has its interpreter: a release the agents'
    command already staged and built is taken as it is."""
    release = root / "releases" / sha
    try:
        ok = (release / "REVISION").read_text().strip() == sha
    except OSError:
        return None
    return release if ok and (release / "steering" / ".venv" / "bin" / "python").exists() else None


def _clear(*paths: Path) -> None:
    for path in paths:
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path, ignore_errors=True)
        else:
            path.unlink(missing_ok=True)


async def fetch(stream, door: str, target: dict, root: Path) -> Path:
    """The package `door` stated, checked against its digest and size, unpacked beside the
    releases as `.<sha>.part`, which is never taken for a release. `stream(path)` opens a GET on
    that door with the workspace's own credential."""
    sha = target["sha"]
    releases = root / "releases"
    releases.mkdir(parents=True, exist_ok=True)
    part, archive = releases / f".{sha}.part", releases / f".{sha}.tar.gz.part"
    _clear(part, archive)
    digest, size = hashlib.sha256(), 0
    try:
        async with stream(f"/steering/agent/package/{sha}") as r:
            if r.status_code != 200:
                raise Refused(f"{door} answered {r.status_code} for the package of {sha}")
            with open(archive, "wb") as out:
                async for chunk in r.aiter_bytes():
                    digest.update(chunk); size += len(chunk)
                    out.write(chunk)
        if size != target["bytes"] or f"sha256:{digest.hexdigest()}" != target["digest"]:
            raise Refused(f"the package of {sha} is not the one {door} stated")
        await asyncio.to_thread(_unpack, archive, part)
        try:
            revision = (part / "REVISION").read_text().strip()
        except OSError:
            revision = ""
        if revision != sha:
            raise Refused(f"the package of {sha} has REVISION {revision or 'missing'}")
    except BaseException:
        _clear(part)
        raise
    finally:
        _clear(archive)
    return part


def _unpack(archive: Path, part: Path) -> None:
    part.mkdir()
    with tarfile.open(archive) as tar:
        tar.extractall(part, filter="data")  # no member lands outside the part


def uv_binary() -> str:
    return (shutil.which("uv") or shutil.which("uv", path="/opt/homebrew/bin:/usr/local/bin") or "")


def build(part: Path, root: Path, run=subprocess.run, uv: str | None = None) -> Path:
    """Build the fetched release's environment with the root's shared interpreter and cache, as
    `activate-agent.sh` does, check it imports the agent, and rename it into `releases/<sha>`."""
    sha = part.name.removeprefix(".").removesuffix(".part")
    if (done := built(root, sha)) is not None:
        _clear(part)
        return done
    uv = uv_binary() if uv is None else uv
    if not uv:
        raise BuildFailed("the agent needs uv")
    env = {**os.environ, "UV_PYTHON_INSTALL_DIR": str(root / "python"), "UV_CACHE_DIR": str(root / "uv-cache")}
    steering = part / "steering"
    for argv, what in (([uv, "sync", "--frozen", "--quiet"], "uv sync"),
                       ([str(steering / ".venv" / "bin" / "python"), "-c", "import agent"], "import agent")):
        done = run(argv, cwd=steering, env=env, capture_output=True, text=True)
        if done.returncode:
            raise BuildFailed(f"{what} failed: {(done.stderr or '').strip()[-400:]}")
    release = root / "releases" / sha
    if release.exists():   # a hand deploy staging it now: its build is not this one's to remove
        _clear(part)
        raise BuildFailed(f"{release} is being staged by another activation")
    part.rename(release)
    return release


DRAIN_BOUND = 1800.0     # how long an agent stops taking work before it swaps or gives up (§6)
DRAIN_POLL = 5.0
REPLACED_WITHIN = 900.0  # an updater that has not replaced this agent by then never will
DRAINING = "draining for update"


def idle(gates: int, reports: int, workers: list[str]) -> str:
    """What stands between a drained agent and its swap (§6): a worker outranks gate runs, since
    an update never ends one, and a frame handler in flight is as busy as a run."""
    if workers:
        return "workers"
    if gates or reports:
        return "gates"
    return "idle"


async def follow_once(m, served: list, own: str | None, root: Path, *, workers, start,
                      sleep=asyncio.sleep, clock=time.monotonic, log=print) -> str:
    """One pass of the follower (§5, §6). `served` are the workspaces this agent serves, each
    with its door (`orch`), `target()`, `stream(path)` and `busy()` → (gate runs, reports);
    `workers()` names the workers an update must not end; `start(release)` starts the updater."""
    answers = [(s.orch, await s.target()) for s in served]
    got = vote(answers)
    if got is None:
        return "no door states a release"
    if got == "split":
        said = ", ".join(f"{d} {t['sha'] if t else 'none'}" for d, t in answers)
        log(f"follow: the doors disagree: {said}")
        return "split"
    door, target = got
    sha = target["sha"]
    if (why := skip(target, own, root)) is not None:
        return why
    try:
        release = built(root, sha)
        if release is None:
            source = next(s for s in served if s.orch == door)
            release = await asyncio.to_thread(build, await fetch(source.stream, door, target, root), root)
    except Refused as e:
        log(f"follow: {sha} refused: {e}")
        return "refused"
    except BuildFailed as e:
        marked = record_build_failure(root, sha)
        log(f"follow: {sha} did not build{', and is marked failed' if marked else ''}: {e}")
        return "build failed"
    held = set(await workers())
    if held & m.update_held:
        # The workers that held the last attempt still run: draining again only idles the machine.
        return "held"
    m.draining = DRAINING
    log(f"follow: {sha} built; draining for update")
    try:
        bound = clock() + DRAIN_BOUND
        while True:
            busy = [s.busy() for s in served]
            state = idle(sum(g for g, _ in busy), sum(r for _, r in busy), held := await workers())
            if state == "idle" or (state == "gates" and clock() >= bound):
                break
            if state == "workers" and clock() >= bound:
                m.update_held = set(held)
                log(f"follow: update held by {', '.join(held)}")
                return "held"
            await sleep(DRAIN_POLL)
        m.update_held = set()
        # Cleared first, so whatever is there once the updater ends is this updater's.
        (root / "update" / "result").unlink(missing_ok=True)
        try:
            await asyncio.to_thread(start, release)
        except Exception as e:
            log(f"follow: the updater did not start: {e}")
            return "updater refused"
        log(f"follow: the updater is moving this agent to {sha}{'' if state == 'idle' else ', with work in flight'}")
        # The updater stops this process. One that ends without doing so says why, and this
        # agent takes work again at once rather than drained for nothing.
        waited = 0.0
        while waited < REPLACED_WITHIN:
            await sleep(DRAIN_POLL); waited += DRAIN_POLL
            if (said := updater_said(root, sha)) is not None:
                log(f"follow: the updater ended without replacing this agent: {said}")
                return "not replaced"
        log("follow: the updater did not replace this agent; taking work again")
        return "not replaced"
    finally:
        m.draining = None


def updater_said(root: Path, sha: str) -> str | None:
    """What the updater for `sha` ended with, once it has."""
    try:
        named, _, outcome = (root / "update" / "result").read_text().strip().partition(" ")
    except OSError:
        return None
    return outcome if named == sha else None


FOLLOW_EVERY = 300.0
FOLLOW_JITTER = 60.0
FOLLOW_GAP = 60.0   # a 426 on every reconnect wakes the follower at most this often


async def follow_forever(m, one_pass, *, sleep=asyncio.sleep, jitter=random.uniform, log=print) -> None:
    """The follower's schedule (§5): every five minutes and a jittered minute, and at once after a
    door refuses this agent's protocol, but never twice within a minute."""
    while True:
        try:
            await asyncio.wait_for(m.refused_protocol.wait(), FOLLOW_EVERY + jitter(0, FOLLOW_JITTER))
        except TimeoutError:
            pass
        m.refused_protocol.clear()
        try:
            outcome = await one_pass()
        except Exception as e:   # the next pass tries again; a dead follower never would
            outcome = None
            log(f"follow: {e!r}")
        await sleep(DRAIN_BOUND if outcome == "held" else FOLLOW_GAP)


UPDATE_LABEL = "com.2mw2lt.agent-update"


def loaded_domain(label: str, run=None) -> str:
    """The launchd domain `label` is loaded in, read rather than probed for: a login with a GUI
    session may still run its agent in its background session. The user manager under systemd."""
    import agentjob
    if agentjob.init_system() == "systemd":
        return "user"
    run = run or agentjob.command
    uid = os.getuid()
    for domain in (f"gui/{uid}", f"user/{uid}"):
        if run(["print", f"{domain}/{label}"]).returncode == 0:
            return domain
    raise RuntimeError(f"{label} is loaded in no domain")


def start_updater(old: Path, new: Path, port: int, workspaces: int, run=None, spawn=subprocess.run) -> None:
    """Start the one-shot updater beside the agent's job (§7): the old release's `agentupdate.py`,
    in a job of its own, so stopping the agent's job does not stop it."""
    import agentjob
    label = os.environ.get("STEERING_AGENT_LABEL") or agentjob.MACHINE_LABEL
    domain = loaded_domain(label, run)
    argv = [str(old / "steering" / ".venv" / "bin" / "python"), str(old / "steering" / "agentupdate.py"),
            str(new), str(old), str(port), str(workspaces), domain]
    if agentjob.init_system() == "systemd":
        done = spawn([os.environ.get("STEERING_AGENT_SYSTEMD_RUN", "systemd-run"), "--user",
                      f"--unit={UPDATE_LABEL}", "--collect", *argv], capture_output=True, text=True)
        if done.returncode:
            raise RuntimeError(f"systemd-run refused {UPDATE_LABEL}: {(done.stderr or '').strip()}")
        return
    run = run or agentjob.command
    home = Path.home()
    _, startup = agentjob.logs(home, UPDATE_LABEL)
    document = agentjob.for_domain({"Label": UPDATE_LABEL, "ProgramArguments": argv, "RunAtLoad": True,
                                    "StandardOutPath": str(startup), "StandardErrorPath": str(startup)}, domain)
    run(["bootout", f"{domain}/{UPDATE_LABEL}"])
    agentjob.wait_unloaded(domain, UPDATE_LABEL, run)
    plist = agentjob.job_path(agentjob.jobs_directory(home), UPDATE_LABEL)
    agentjob.write(plist, agentjob.dump(document))
    booted = run(["bootstrap", domain, str(plist)])
    if booted.returncode:
        raise RuntimeError(f"launchctl bootstrap refused {UPDATE_LABEL}: {booted.stderr.strip()}")
