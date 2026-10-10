#!/usr/bin/env python3
"""Install a workspace into the per-machine steering agent (#288, doc 130 §8): its entry in the
machine's registry, and the one launchd job or systemd unit that serves every entry, written if it is missing or
has changed.

`hooks.py install` owns the public entry point. This helper receives the already-resolved
workspace door so it writes the same entry on the brain and remote machines without guessing
where the workspace was wired.
"""
from __future__ import annotations

import sys
from pathlib import Path

import python_floor  # noqa: E402

python_floor.require()

import argparse  # noqa: E402
import os  # noqa: E402
import re  # noqa: E402
import shutil  # noqa: E402

import agentjob
import machine_workspaces


DEFAULT_PORT = 9990


def value(name: str, default: str | None = None) -> str | None:
    return os.environ.get(name) or default


def port() -> str:
    raw = value("STEERING_AGENT_PORT", str(DEFAULT_PORT))
    try:
        number = int(raw or "")
    except ValueError:
        raise RuntimeError("STEERING_AGENT_PORT must be a TCP port") from None
    if not 1 <= number <= 65535:
        raise RuntimeError("STEERING_AGENT_PORT must be a TCP port")
    return str(number)


def label() -> str:
    """The machine's one job, whatever the port: a job under any other label is one `agentjob`
    neither pins to a release nor migrates (doc 130 §8)."""
    return agentjob.MACHINE_LABEL


def launch_agents() -> Path:
    return agentjob.jobs_directory(Path.home())


def entry(workspace: Path, orch: str, local: bool) -> tuple[str, machine_workspaces.Entry]:
    """The workspace's registry entry, under the authority its door names."""
    workspace = workspace.resolve()
    capability = None
    if local:
        capability = workspace / ".claude" / "steering-capability"
        if not capability.is_file():
            raise RuntimeError(f"{capability} is required for an agent against the local brain door")
    door = orch.rstrip("/")
    named = re.search(r"/w/([A-Za-z0-9._-]+)$", door)
    held = machine_workspaces.at(workspace)
    name = held.gh_account if held is not None else value("STEERING_GH_ACCOUNT")
    # The registry keys a workspace by the uuid its checkout is bound to (go-alias-removal-design.md
    # D2); an alias door's own name stands for a checkout bound to nothing yet.
    import checkout_binding
    authority = checkout_binding.id_at(workspace) or (named.group(1) if named else workspace.name)
    return authority, machine_workspaces.Entry(
        workspace, door, capability, held.port if held else None, name)


def facts(domain: str) -> tuple[Path, bytes]:
    release, revision = agentjob.active_release(Path.home())
    python = release / "steering" / ".venv" / "bin" / "python"
    agent = release / "steering" / "agent.py"
    if not python.is_file() or not os.access(python, os.X_OK):
        raise RuntimeError(f"{python} is not an executable active-release Python")
    if not agent.is_file():
        raise RuntimeError(f"{agent} is missing")
    named = label()
    overridden_log = value("STEERING_AGENT_LOG")
    if overridden_log:
        active_log, startup_log = Path(overridden_log), Path(overridden_log + ".startup")
        agentjob.prepare_startup_log(startup_log)
    else:
        active_log, startup_log = agentjob.logs(Path.home(), named)
    # No workspace here: the job serves every one the registry names, and a workspace variable
    # would make it serve that one alone. What the machine's job already holds that no install
    # names — a gate limit, the launchers, a pin — is the machine's and stays.
    plist = agentjob.job_path(launch_agents(), named)
    held = (agentjob.load(plist) or {}).get("EnvironmentVariables") or {}
    environment = {**{k: v for k, v in held.items() if k not in agentjob.JOB_OWN},
                   "STEERING_AGENT_PORT": port(),
                   "STEERING_AGENT_REVISION": revision,
                   "STEERING_AGENT_ENV_FILE": str(agentjob.settings(Path.home())),
                   "STEERING_AGENT_LABEL": named,
                   "STEERING_AGENT_LOG_ACTIVE": str(active_log),
                   "STEERING_AGENT_LOG_STARTUP": str(startup_log)}
    codex = value("STEERING_CODEX_CLI") or shutil.which("codex")
    if codex:
        environment["STEERING_CODEX_CLI"] = codex
    document = {"Label": named, "ProgramArguments": agentjob.program(Path.home(), release),
                "WorkingDirectory": str(release), "EnvironmentVariables": environment,
                "RunAtLoad": True, "KeepAlive": True,
                "StandardOutPath": str(startup_log),
                "StandardErrorPath": str(startup_log)}
    return plist, agentjob.dump(agentjob.for_domain(document, domain))


def loaded(domain: str, named: str) -> bool:
    return agentjob.loaded(domain, named)


def write(path: Path, document: bytes) -> None:
    agentjob.write(path, document)


def install(workspace: Path, orch: str, local: bool = False) -> Path:
    agentjob.migrate_github(Path.home(), agentjob.steering_jobs(launch_agents()))
    authority, found = entry(workspace, orch, local)
    domain = agentjob.launch_domain()
    plist, document = facts(domain)
    # What an activation gives a Mac, given to one installed without one, before its first report.
    agentjob.provision_tools(agentjob.active_release(Path.home())[0], wait=True)
    # The entry first: a job started below finds it, and one already running reads it within
    # its poll, with no restart (doc 130 §2).
    machine_workspaces.replace(machine_workspaces.directory(Path.home()), authority, found)
    machine_workspaces.poke()
    print(f"agent: {found.root} registered as {authority}")
    named = label()
    try:
        unchanged = plist.read_bytes() == document
    except OSError:
        unchanged = False
    if unchanged and loaded(domain, named):
        print(f"agent: {agentjob.init_system()} job {named} already matches {plist}")
        return plist
    if not unchanged:
        write(plist, document)
    agentjob.restart(domain, named, plist)
    print(f"agent: {agentjob.init_system()} job {named} on 127.0.0.1:{port()}")
    return plist


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("workspace", type=Path)
    parser.add_argument("orch")
    parser.add_argument("--local", action="store_true", help="add the workspace capability for a loopback brain door")
    args = parser.parse_args(argv)
    try:
        install(args.workspace, args.orch, args.local)
    except RuntimeError as error:
        parser.error(str(error))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
