"""The immutable release and the launchd or systemd jobs of this machine's steering agents."""
from __future__ import annotations

import contextlib
import fcntl
import os
import plistlib
import re
import shlex
import shutil
import stat
import subprocess
import sys
import time
import tomllib
from pathlib import Path
from typing import Callable

import launcher
import spool
import envfile


SHA = re.compile(r"[0-9a-f]{40}")
# The one job that serves every workspace on this machine (doc 130).
MACHINE_LABEL = "com.2mw2lt.agent"
MACHINE_PORT = "9990"
# What a job serving one workspace names that is that workspace's, or is written for the job by
# whatever installs it; the rest of its environment is the machine's.
JOB_OWN = frozenset({"STEERING_AGENT_ORCH", "STEERING_AGENT_WORKSPACE", "STEERING_AGENT_CAPABILITY_FILE",
                     "STEERING_AGENT_PORT", "STEERING_AGENT_LABEL", "STEERING_AGENT_LOG_ACTIVE",
                     "STEERING_AGENT_LOG_STARTUP", "STEERING_AGENT_REVISION", "STEERING_AGENT_ENV_FILE",
                     "STEERING_DOOR", "STEERING_PORT", "STEERING_WORKSPACE", "STEERING_GH_ACCOUNT"})
Run = Callable[[list[str]], subprocess.CompletedProcess[str]]
# How long a stopping agent has before its init system kills it: time to park its judges and
# report them (doc 132 §4.2). launchd's own grace on a bootout, measured at 5 s on 2026-09-28, ends
# before uvicorn has even begun the shutdown that parks, so every restart killed its runs (#2785).
STOP_SECONDS = 90
# Every rewrite of a job backs it up; only the newest few are worth keeping (#3498).
BACKUPS_KEPT = 3


def init_system() -> str:
    """`systemd` on Linux, where the job is a user unit (plans/2473 §4), and `launchd` elsewhere.
    `STEERING_AGENT_INIT` names one, so a guard can drive either on any machine."""
    return os.environ.get("STEERING_AGENT_INIT") or ("systemd" if sys.platform.startswith("linux") else "launchd")


def jobs_directory(home: Path) -> Path:
    if init_system() == "systemd":
        return Path(os.environ.get("STEERING_AGENT_SYSTEMD_UNITS") or home / ".config" / "systemd" / "user")
    return Path(os.environ.get("STEERING_AGENT_LAUNCH_AGENTS") or home / "Library" / "LaunchAgents")


def job_path(directory: Path, label: str) -> Path:
    return directory / f"{label}.{'service' if init_system() == 'systemd' else 'plist'}"


def _unit_escape(value: str) -> str:
    if "\n" in value:
        raise RuntimeError("a systemd unit value cannot hold a newline")
    return value.replace("%", "%%")


def scope_prefix(unit: str, env=None) -> list[str]:
    """What starts a process outside this agent's systemd unit, whose restart kills everything left
    in its cgroup (`KillMode=mixed`): a transient scope of its own, named `unit` (doc 149 §2, §5).
    Elsewhere a session of its own is enough; launchd leaves one running when the job stops."""
    env = os.environ if env is None else env
    if "INVOCATION_ID" not in env or not shutil.which("systemd-run"):
        return []
    return ["env", f"XDG_RUNTIME_DIR=/run/user/{os.getuid()}", "systemd-run", "--user", "--scope",
            "--quiet", "--collect", f"--unit={unit}", "--"]

def _unit_quote(value: str) -> str:
    return '"' + _unit_escape(value).replace("\\", "\\\\").replace('"', '\\"') + '"'


def dump(document: dict) -> bytes:
    """A job in the init system's own form. The document is launchd's shape either way: a unit is
    rendered from it, and `load` reads back what this writes."""
    if init_system() != "systemd":
        return plistlib.dumps({**document, "ExitTimeOut": STOP_SECONDS}, sort_keys=True)
    arguments = " ".join(_unit_quote(a).replace("$", "$$") for a in document.get("ProgramArguments") or [])
    # Let the agent park its judges before systemd kills the rest of its service cgroup.
    lines = ["[Unit]", "Description=2mw2lt steering agent", "", "[Service]",
             f"ExecStart={arguments}", "KillMode=mixed", f"TimeoutStopSec={STOP_SECONDS}"]
    if document.get("WorkingDirectory"):
        lines.append(f"WorkingDirectory={_unit_escape(str(document['WorkingDirectory']))}")
    for name, value in sorted((document.get("EnvironmentVariables") or {}).items()):
        lines.append(f"Environment={_unit_quote(f'{name}={value}')}")
    if document.get("KeepAlive"):
        lines += ["Restart=always", "RestartSec=5"]
    for key, stream in (("StandardOutPath", "StandardOutput"), ("StandardErrorPath", "StandardError")):
        if document.get(key):
            lines.append(f"{stream}=append:{_unit_escape(str(document[key]))}")
    lines += ["", "[Install]", "WantedBy=default.target"]
    return ("\n".join(lines) + "\n").encode()


def load(path: Path) -> dict | None:
    """The job at `path` as `dump` was given it, or None when it is not one."""
    try:
        raw = path.read_bytes()
    except OSError:
        return None
    if path.suffix == ".plist":
        try:
            document = plistlib.loads(raw)
        except plistlib.InvalidFileException:
            return None
        return document if isinstance(document, dict) else None
    document: dict = {"Label": path.stem, "EnvironmentVariables": {}}
    try:
        for line in raw.decode().splitlines():
            key, _, value = line.partition("=")
            if key == "ExecStart":
                document["ProgramArguments"] = [a.replace("%%", "%") for a in shlex.split(value.replace("$$", "$"))]
            elif key == "WorkingDirectory":
                document["WorkingDirectory"] = value.replace("%%", "%")
            elif key == "Environment":
                name, _, held = shlex.split(value)[0].replace("%%", "%").partition("=")
                document["EnvironmentVariables"][name] = held
            elif key == "Restart" and value == "always":
                document["KeepAlive"] = document["RunAtLoad"] = True
            elif key in ("StandardOutput", "StandardError") and value.startswith("append:"):
                document[f"{key.removesuffix('put')}Path"] = value.removeprefix("append:").replace("%%", "%")
    except (UnicodeDecodeError, ValueError):
        return None
    return document


def root(home: Path) -> Path:
    return home / ".local" / "share" / "2mw2lt-agent"


def settings(home: Path) -> Path:
    return home / ".config" / "2mw2lt" / "agent.env"


def tunables(home: Path) -> Path:
    return home / ".config" / "2mw2lt" / "agent.toml"


def logs(home: Path, label: str) -> tuple[Path, Path]:
    directory = (home / ".local" / "state" / "2mw2lt" if init_system() == "systemd"
                 else home / "Library" / "Logs" / "2mw2lt")
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(directory, 0o700)
    startup = directory / f"{label}.startup.log"
    prepare_startup_log(startup)
    return directory / f"{label}.log", startup


def prepare_startup_log(startup: Path) -> None:
    startup.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = os.open(startup, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    os.close(descriptor)
    os.chmod(startup, 0o600)


def revision(release: Path) -> str:
    try:
        value = (release / "REVISION").read_text().strip()
    except OSError as error:
        raise RuntimeError(f"agent release has no readable REVISION: {error}") from None
    if not SHA.fullmatch(value):
        raise RuntimeError("agent release REVISION must be one full lowercase commit SHA")
    return value


def active_release(home: Path) -> tuple[Path, str]:
    current = root(home) / "current"
    try:
        release = current.resolve(strict=True)
    except OSError as error:
        raise RuntimeError(f"no active agent release; deploy one first ({error})") from None
    if release.parent != (root(home) / "releases").resolve():
        raise RuntimeError(f"active agent release is outside {root(home) / 'releases'}")
    wanted = revision(release)
    python = release / "steering" / ".venv" / "bin" / "python"
    agent = release / "steering" / "agent.py"
    if not python.is_file() or not os.access(python, os.X_OK) or not agent.is_file():
        raise RuntimeError(f"active agent release {wanted} is not built")
    return release, wanted


def program(home: Path, release: Path) -> list[str]:
    """The job's arguments for `release`: under launchd, behind the launcher whose signature the
    owner's privacy grant is keyed to (doc 141), when one can be had."""
    arguments = [str(release / "steering" / ".venv" / "bin" / "python"), str(release / "steering" / "agent.py")]
    front = launcher.ensure(root(home), release) if init_system() == "launchd" else None
    return [str(front), *arguments] if front else arguments


def private_file(path: Path) -> bool:
    try:
        facts = path.stat()
    except OSError:
        return False
    return stat.S_ISREG(facts.st_mode) and facts.st_uid == os.getuid() and not stat.S_IMODE(facts.st_mode) & 0o077


def write(path: Path, document: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    spool.write_atomic(path, document, mode=0o600)


def command(args: list[str]) -> subprocess.CompletedProcess[str]:
    if init_system() == "systemd":
        return subprocess.run([os.environ.get("STEERING_AGENT_SYSTEMCTL", "systemctl"), "--user", *args],
                              text=True, capture_output=True)
    launchctl = os.environ.get("STEERING_AGENT_LAUNCHCTL", "launchctl")
    return subprocess.run([launchctl, *args], text=True, capture_output=True)


def launch_domain(run: Run = command) -> str:
    """The launchd domain the job belongs in: the login's GUI session when it has one, else its
    background session, which is all a login reached only over ssh has. `gui/<uid>` refuses a
    bootstrap there (125) ever since that login last logged out at the screen, or never has."""
    uid = os.getuid()
    if init_system() == "systemd" or run(["print", f"gui/{uid}"]).returncode == 0:
        return f"gui/{uid}"
    return f"user/{uid}"


def for_domain(document: dict, domain: str) -> dict:
    """The job as `domain` takes it. launchd bootstraps into a background session only a job that
    says it is for one, and answers anything else with an I/O error (5), measured 2026-09-28."""
    placed = {k: v for k, v in document.items() if k != "LimitLoadToSessionType"}
    if domain.startswith("user/"):
        placed["LimitLoadToSessionType"] = "Background"
    return placed


def loaded(domain: str, label: str, run: Run = command) -> bool:
    if init_system() == "systemd":
        return run(["is-active", "--quiet", f"{label}.service"]).returncode == 0
    return run(["print", f"{domain}/{label}"]).returncode == 0


def stop(domain: str, label: str, run: Run = command) -> None:
    run(["stop", f"{label}.service"] if init_system() == "systemd" else ["bootout", f"{domain}/{label}"])


def restart(domain: str, label: str, plist: Path, run: Run = command) -> None:
    if init_system() == "systemd":
        # Enabled, so the lingering login starts it at boot with nobody logged in (plans/2473 §4).
        for step in (["daemon-reload"], ["enable", f"{label}.service"], ["restart", f"{label}.service"]):
            done = run(step)
            if done.returncode:
                raise RuntimeError(f"systemctl --user {' '.join(step)} refused {label}: {done.stderr.strip()}")
        if not loaded(domain, label, run):
            raise RuntimeError(f"systemd did not start {label}")
        return
    # Both of the login's sessions: a job that moves between them would otherwise keep running
    # in the one it left, on the old release and the same port.
    uid = domain.split("/", 1)[1]
    for session in (f"gui/{uid}", f"user/{uid}"):
        run(["bootout", f"{session}/{label}"])
        wait_unloaded(session, label, run)
    booted = run(["bootstrap", domain, str(plist)])
    if booted.returncode:
        raise RuntimeError(f"launchctl bootstrap refused {label}: {booted.stderr.strip()}")
    kicked = run(["kickstart", "-k", f"{domain}/{label}"])
    if kicked.returncode:
        raise RuntimeError(f"launchctl kickstart refused {label}: {kicked.stderr.strip()}")
    if not loaded(domain, label, run):
        raise RuntimeError(f"launchd did not load {label}")


def backup(path: Path) -> None:
    copy = path.with_name(f"{path.name}.bak-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}")
    shutil.copy2(path, copy)
    series = re.compile(re.escape(path.name) + r"\.bak-\d{8}T\d{6}Z")
    # The stamp sorts as it dates, so the name orders the series; the copy just taken stays even
    # when a clock stepped back sorts it below older ones.
    backups = sorted(p for p in path.parent.iterdir() if series.fullmatch(p.name) and p != copy)
    for stale in backups[:-(BACKUPS_KEPT - 1)]:
        stale.unlink(missing_ok=True)


def steering_jobs(directory: Path) -> list[tuple[Path, dict]]:
    found: list[tuple[Path, dict]] = []
    pattern = f"{MACHINE_LABEL}*.service" if init_system() == "systemd" else "*.plist"
    for path in sorted(directory.glob(pattern)):
        document = load(path)
        if document is None:
            continue
        arguments = document.get("ProgramArguments") or []
        environment = document.get("EnvironmentVariables") or {}
        if ((environment.get("STEERING_AGENT_WORKSPACE") or document.get("Label") == MACHINE_LABEL)
                and arguments and str(arguments[-1]).endswith("steering/agent.py")):
            found.append((path, document))
    return found


def wait_unloaded(domain: str, label: str, run: Run = command) -> None:
    """launchd lists a job until its process has exited, which a parking agent takes up to
    `STOP_SECONDS` to do."""
    for _ in range(2 * (STOP_SECONDS + 10)):
        if not loaded(domain, label, run):
            return
        time.sleep(.5)
    raise RuntimeError(f"{label} is still loaded {STOP_SECONDS + 10}s after bootout")


def migrate_jobs(home: Path, launch_agents: Path, run: Run = command) -> list[str]:
    """One job for the machine in place of one per workspace (doc 130 §8): an entry in the
    registry for each workspace job, which is backed up and booted out, and `MACHINE_LABEL`
    written with the jobs' shared environment. Nothing is changed when two jobs set a variable
    of the machine's differently. Answers the labels booted out; none on a machine already moved."""
    import machine_workspaces
    jobs = [(p, d) for p, d in steering_jobs(launch_agents)
            if (d.get("EnvironmentVariables") or {}).get("STEERING_AGENT_WORKSPACE")]
    if not jobs:
        return []
    entries: dict[str, machine_workspaces.Entry] = {}
    shared: dict[str, str] = {}
    for path, document in jobs:
        label = str(document.get("Label") or path.stem)
        env = document.get("EnvironmentVariables") or {}
        door = str(env.get("STEERING_AGENT_ORCH") or "").rstrip("/")
        if not door:
            raise RuntimeError(f"{label} names no STEERING_AGENT_ORCH, so there is no door to serve it at")
        root = Path(str(env["STEERING_AGENT_WORKSPACE"])).resolve()
        named = re.search(r"/w/([A-Za-z0-9._-]+)$", door)
        authority = named.group(1) if named else root.name
        if authority in entries:
            raise RuntimeError(f"two jobs serve {authority}; remove one before migrating")
        port = str(env.get("STEERING_AGENT_PORT") or MACHINE_PORT)
        cap = env.get("STEERING_AGENT_CAPABILITY_FILE")
        entries[authority] = machine_workspaces.Entry(root, door, Path(cap) if cap else None,
                                                      None if port == MACHINE_PORT else int(port),
                                                      env.get("STEERING_GH_ACCOUNT") or
                                                      envfile.value(settings(home), "STEERING_GH_ACCOUNT"))
        for name, value in env.items():
            if name in JOB_OWN:
                continue
            if name in shared and shared[name] != value:
                raise RuntimeError(f"the agent jobs set {name} differently; make them agree before migrating")
            shared[name] = value
    template = next((d for p, d in jobs if d.get("Label") == MACHINE_LABEL), jobs[0][1])
    active, startup = logs(home, MACHINE_LABEL)
    machine = {"Label": MACHINE_LABEL, "ProgramArguments": template.get("ProgramArguments"),
               "WorkingDirectory": template.get("WorkingDirectory"),
               "EnvironmentVariables": {**shared, "STEERING_AGENT_PORT": MACHINE_PORT,
                                        "STEERING_AGENT_LOG_ACTIVE": str(active),
                                        "STEERING_AGENT_LOG_STARTUP": str(startup)},
               "RunAtLoad": True, "KeepAlive": True,
               "StandardOutPath": str(startup), "StandardErrorPath": str(startup)}
    # Activation moves this pin into rollback settings after the jobs are consolidated.
    if pin := (template.get("EnvironmentVariables") or {}).get("STEERING_GH_ACCOUNT"):
        machine["EnvironmentVariables"]["STEERING_GH_ACCOUNT"] = pin
    where = machine_workspaces.directory(home)
    for authority, entry in entries.items():
        machine_workspaces.write(where, authority, entry)
    domain = launch_domain(run)
    booted = []
    for path, document in jobs:
        label = str(document.get("Label") or path.stem)
        backup(path)
        stop(domain, label, run)
        wait_unloaded(domain, label, run)
        booted.append(label)
    # The machine's job before the old ones go: a run stopped in between leaves a workspace job
    # the next activation migrates again, never entries with no job to serve them.
    target = job_path(launch_agents, MACHINE_LABEL)
    write(target, dump(for_domain(machine, domain)))
    for path, _ in jobs:
        if path != target:
            path.unlink()
    return booted


def migrate_github(home: Path, jobs: list[tuple[Path, dict]]) -> None:
    """Move a consolidated job's legacy GitHub pin into its existing workspace entries."""
    import machine_workspaces
    name = next(((d.get("EnvironmentVariables") or {}).get("STEERING_GH_ACCOUNT")
                 for _, d in jobs if not (d.get("EnvironmentVariables") or {}).get("STEERING_AGENT_WORKSPACE")
                 and (d.get("EnvironmentVariables") or {}).get("STEERING_GH_ACCOUNT")), None)
    path = settings(home)
    if name and not path.exists():
        path = migrate_settings(home, jobs)
    lines = path.read_text().splitlines(keepends=True) if path.exists() else []
    stored = next((pair[1] for line in lines if (pair := envfile._pair(line))
                   and pair[0] == "STEERING_GH_ACCOUNT"), None)
    name = name or stored
    machine_workspaces.migrate_account(machine_workspaces.directory(home), name)
    # Pre-workspace-pin releases read the legacy account from settings on rollback (#3593).
    if not name or stored == name:
        return
    kept = [line for line in lines if not (pair := envfile._pair(line)) or pair[0] != "STEERING_GH_ACCOUNT"]
    text = "".join(kept)
    if text and not text.endswith("\n"):
        text += "\n"
    write(path, f"{text}STEERING_GH_ACCOUNT={name}\n".encode())


def migrate_settings(home: Path, jobs: list[tuple[Path, dict]]) -> Path:
    destination = settings(home)
    if destination.exists():
        if not private_file(destination):
            raise RuntimeError(f"agent settings must be an owner-only regular file: {destination}")
        return destination
    if not jobs:
        # A machine with no agent yet has nothing to migrate: its first activation starts it
        # with no settings of its own, and its install writes its job.
        write(destination, b"")
        return destination
    roots = [Path(str(directory)) for _, document in jobs
             if (directory := document.get("WorkingDirectory"))]
    sources = {root / ".env" for root in roots if (root / ".env").exists()}
    if len(sources) != 1:
        raise RuntimeError("installed steering agents do not share one legacy settings source")
    source = next(iter(sources))
    if not private_file(source):
        raise RuntimeError(f"no private legacy agent settings at {source}")
    write(destination, source.read_bytes())
    return destination


def _legacy_positive(raw: str | None, default: int) -> int:
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value >= 1 else 1


def migrate_host_limit(home: Path, jobs: list[tuple[Path, dict]]) -> Path:
    """`[capacity].host_limit` in `tunables(home)`, reconciled once from whatever every installed
    job's own legacy `STEERING_GATE_HOST_LIMIT`/`STEERING_GATE_AGENT_LIMIT` agree on — left unset
    when none of them has an opinion, so `settings.gate_host_limit()`'s own `agent_limit`/env
    derivation applies exactly as if this migration had never run (a literal default here would
    pin `host_limit` away from an already-configured `agent_limit`, #2394 code review; round 2's
    original concern — re-deriving "has this run before" on every activation — does not apply,
    since the existing `host_limit`-already-set check below is that idempotency check whether or
    not this call ever wrote anything). A no-op once the key is set; refuses, naming every
    disagreeing job's label and derived value, rather than picking one arbitrarily."""
    destination = tunables(home)
    try:
        with destination.open("rb") as f:
            existing = tomllib.load(f)
    except FileNotFoundError:
        existing = {}
    except (OSError, tomllib.TOMLDecodeError) as e:
        # A file that exists but cannot be parsed is refused, not silently rebuilt: overwriting
        # it would discard whatever real config — vendor_limit, agent_limit, max_workers — it
        # already held, the same "never a silent fall back" discipline settings.py's own reader
        # applies to a running agent (#2394 code review).
        raise RuntimeError(f"{destination} exists but cannot be read: {e}") from e
    # Judged by the reader's own rules, so a file an agent reads is one activation accepts: a
    # copy of them here refused the file's own [placement] and [routing] tables (#2394). Refused
    # rather than rewritten: whatever it held would be erased or corrupted.
    import settings
    try:
        settings.validate(existing)
    except ValueError as e:
        raise RuntimeError(f"{destination} holds content this migration does not recognize "
                           f"({e}); refusing rather than rewriting it") from None
    if "host_limit" in existing.get("capacity", {}):
        return destination
    derived: dict[str, int] = {}
    for path, document in jobs:
        env = document.get("EnvironmentVariables") or {}
        # A job with neither var set has no opinion — it accepts whatever the others (or the
        # ultimate default) resolve to, and must not be treated as if it had explicitly asked
        # for the derived default. Skipping it is what lets a "sparse" host (one job customized,
        # one silent) reconcile instead of spuriously refusing on a difference the silent job
        # never actually asserted.
        if "STEERING_GATE_HOST_LIMIT" not in env and "STEERING_GATE_AGENT_LIMIT" not in env:
            continue
        agent_limit = _legacy_positive(env.get("STEERING_GATE_AGENT_LIMIT"), 3)
        label = str(document.get("Label") or path.stem)
        derived[label] = _legacy_positive(env.get("STEERING_GATE_HOST_LIMIT"), agent_limit)
    distinct = set(derived.values())
    if len(distinct) > 1:
        named = ", ".join(f"{label}={value}" for label, value in sorted(derived.items()))
        raise RuntimeError(f"installed steering agents do not agree on a host limit: {named}")
    if not distinct:
        # No installed job has a legacy opinion — including the "zero jobs installed yet" shape —
        # so there is nothing to reconcile: leave the key unset rather than write a literal
        # default that could disagree with an already-configured agent_limit.
        return destination
    value = next(iter(distinct))
    # The one line added to the file as written, so its other tables and its comments stay; the
    # result is parsed back and refused unless it is the file with that key alone added.
    base, text = (existing, destination.read_text()) if existing else ({"schema": 1}, "schema = 1\n")
    header = re.search(r"(?m)^\[capacity\][ \t]*(#.*)?$", text)
    if header:
        text = f"{text[:header.end()]}\nhost_limit = {value}{text[header.end():]}"
    else:
        text = f"{text.rstrip()}\n\n[capacity]\nhost_limit = {value}\n"
    wanted = {**base, "capacity": {**base.get("capacity", {}), "host_limit": value}}
    try:
        written = tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        written = None
    if written != wanted:
        raise RuntimeError(f"{destination}: host_limit = {value} cannot be added to it as written; "
                           "refusing rather than rewriting it")
    write(destination, text.encode())
    return destination


def select(release: Path, home: Path) -> None:
    current = root(home) / "current"
    current.parent.mkdir(parents=True, exist_ok=True)
    temporary = current.with_name("current.next")
    try:
        temporary.unlink()
    except FileNotFoundError:
        pass
    temporary.symlink_to(release)
    os.replace(temporary, current)  # not `write_atomic`: a link swapped, holding no data to sync


def prune(release: Path, previous: Path | None = None) -> None:
    """Keep the active release, the one that served before it, which a rollback runs (doc 147 §7),
    and the most recently activated others, three in all."""
    os.utime(release, None)
    candidates = [path for path in release.parent.iterdir()
                  if path.is_dir() and not path.is_symlink() and SHA.fullmatch(path.name)]
    # A release removed by a deploy running beside this one is one fewer to consider, not an
    # error: the stat happens inside `sort`, so one that cannot answer takes the whole prune
    # down with it, the same window `spool.pending_at` tolerates (#1870).
    dated = []
    for path in candidates:
        try:
            dated.append(((path == release, path == previous, path.stat().st_mtime_ns), path))
        except OSError:
            continue
    for _, path in sorted(dated, key=lambda x: x[0], reverse=True)[3:]:
        if _pinned(path):
            print(f"agent release: kept {path.name}: a gate run still runs from it", flush=True)
            continue
        shutil.rmtree(path)


# A gate run holds its release's pin shared for as long as it runs (doc 149 §7): it imports lazily,
# so a removed tree would fail it mid-review. The lock goes with its process, however that ends.
PIN = ".pin"


def pin(release: Path) -> int:
    """Hold `release` against a prune for this process's life: the descriptor holding it. Making the
    pin leaves the release's time alone, which `prune` orders releases by as when last activated."""
    try:
        fd = os.open(release / PIN, os.O_RDONLY)
    except FileNotFoundError:
        st = os.stat(release)
        fd = os.open(release / PIN, os.O_RDONLY | os.O_CREAT, 0o644)
        os.utime(release, ns=(st.st_atime_ns, st.st_mtime_ns))
    fcntl.flock(fd, fcntl.LOCK_SH)
    return fd


def _pinned(release: Path) -> bool:
    try:
        fd = os.open(release / PIN, os.O_RDONLY)
    except FileNotFoundError:
        return False
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return False
    except BlockingIOError:
        return True
    finally:
        os.close(fd)


@contextlib.contextmanager
def locked(home: Path):
    """One activation at a time on a machine: a hand deploy and the self-updater both come here
    (doc 147 §7, §10)."""
    path = root(home) / "activate.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    handed = handed_lock(path)
    with (os.fdopen(handed, "a", closefd=False) if handed is not None else open(path, "a")) as held:
        try:
            # On the updater's own descriptor this succeeds: a flock belongs to the open file.
            fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError(f"another activation holds {path}") from None
        yield


LOCK_FD = "STEERING_AGENT_LOCK_FD"


def handed_lock(path: Path) -> int | None:
    """The descriptor of `path` an updater holding the lock passed down (doc 147 §7), only when it
    is that file: any other is ignored, and the lock is then taken as anyone would take it."""
    try:
        fd = int(os.environ.get(LOCK_FD) or "")
        mine, theirs = os.fstat(fd), os.stat(path)
    except (ValueError, OSError):
        return None
    return fd if (mine.st_dev, mine.st_ino) == (theirs.st_dev, theirs.st_ino) else None


def activate(release: Path, home: Path, launch_agents: Path, run: Run = command,
             domain: str | None = None) -> list[str]:
    """`domain` is the one the agent is loaded in, from an updater that read it (doc 147 §7);
    without it the login's domain is probed."""
    with locked(home):
        return _activate(release, home, launch_agents, run, domain)


def provision_tools(release: Path, wait: bool = False) -> None:
    """The pinned Go and PostgreSQL coordination/'s guards run on, on a Mac, where nothing else
    installs them (#3706), into the agent's own `tools`. An activation starts it detached, since a
    first build outlasts what an updater waits for, and an install waits for it, behind any build
    already running. One at a time,
    under a lock the kernel lets go of with its holder. Never what either stops on: a host without
    the tools reports the kind NOT RUN, and the next activation tries again."""
    if sys.platform != "darwin":
        return
    script = release / "steering" / "host" / "darwin" / "agent-tools.sh"
    if not script.is_file():  # a release from before #3706, rolled back to
        return
    tools = root(Path.home()) / "tools"
    lockf = os.environ.get("STEERING_AGENT_LOCKF", "/usr/bin/lockf")
    # An activation leaves a build already running to it; an install waits its turn.
    argv = [lockf, *([] if wait else ["-t", "0"]), str(tools / ".lock"), "/bin/bash", str(script)]
    try:
        tools.mkdir(parents=True, exist_ok=True)
        if not wait:
            with open(tools / "install.log", "ab") as log:
                subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True)
            return
        done = subprocess.run(argv, capture_output=True, text=True)
        why = (done.stderr or done.stdout).strip()[-400:] if done.returncode else None
    except OSError as e:
        why = str(e)[:400]
    if why:
        print(f"agentjob: coordination tools not installed: {why}", file=sys.stderr)


def _activate(release: Path, home: Path, launch_agents: Path, run: Run, domain: str | None) -> list[str]:
    release = release.resolve()
    if release.parent != (root(home) / "releases").resolve():
        raise RuntimeError(f"agent release is outside {root(home) / 'releases'}")
    wanted = revision(release)
    python = release / "steering" / ".venv" / "bin" / "python"
    agent = release / "steering" / "agent.py"
    if not python.is_file() or not os.access(python, os.X_OK) or not agent.is_file():
        raise RuntimeError(f"agent release {wanted} is not built")
    jobs = steering_jobs(launch_agents)
    machine_settings = migrate_settings(home, jobs)
    migrate_host_limit(home, jobs)
    if migrate_jobs(home, launch_agents, run):
        jobs = steering_jobs(launch_agents)
    migrate_github(home, jobs)
    try:
        previous = (root(home) / "current").resolve(strict=True)
    except OSError:
        previous = None
    provision_tools(release)
    select(release, home)
    arguments = program(home, release)
    domain = domain or launch_domain(run)
    labels: list[str] = []
    for path, document in jobs:
        label = str(document.get("Label") or path.stem)
        labels.append(label)
        desired = for_domain(document, domain)
        environment = dict(desired.get("EnvironmentVariables") or {})
        environment.pop("STEERING_GH_ACCOUNT", None)
        environment.update({"STEERING_AGENT_REVISION": wanted,
                            "STEERING_AGENT_ENV_FILE": str(machine_settings),
                            "STEERING_AGENT_LABEL": label})
        desired["EnvironmentVariables"] = environment
        desired["ProgramArguments"] = arguments
        desired["WorkingDirectory"] = str(release)
        encoded = dump(desired)
        try:
            unchanged = path.read_bytes() == encoded
        except OSError:
            unchanged = False
        if unchanged and loaded(domain, label, run):
            continue
        backup(path)
        write(path, encoded)
        restart(domain, label, path, run)
    prune(release, previous)
    return labels


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        print("usage: agentjob.py <release>", file=sys.stderr)
        return 2
    home = Path.home()
    release = Path(argv[0])
    labels = activate(release, home, jobs_directory(home), domain=os.environ.get("STEERING_AGENT_DOMAIN") or None)
    wanted = revision(release)
    if labels:
        for label in labels:
            print(f"{label}: serving {wanted}")
    else:
        print(f"agent release: serving {wanted}, 0 jobs")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
