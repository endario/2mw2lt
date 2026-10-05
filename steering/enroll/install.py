#!/usr/bin/env python3
"""`/2mw2lt:install`'s engine (doc 129): a repository becomes a working, observed workspace.

    install.py [workspace] [--codex] [--team <id>]
                                        install, or resume one half done; --codex writes no hooks;
                                        --team names the team when the person owns several
    install.py uninstall [workspace]    remove what an install added here
    install.py uninstall --workspace    and have the platform retire the workspace too

The person acts only where a grant is theirs to give: signing in, and giving the App
the repository. For each it prints the page, opens it, and waits.
"""
from __future__ import annotations

import asyncio
import contextlib
import getpass
import json
import os
import platform
import socket
import subprocess
import tarfile
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from pathlib import Path

HERE = Path(__file__).resolve().parent
STEERING = HERE.parent
sys.path.insert(0, str(STEERING))
sys.path.insert(0, str(HERE))

import agentjob  # noqa: E402
import credential  # noqa: E402
import door as door_mod  # noqa: E402
import machine_workspaces  # noqa: E402
import checkout_binding  # noqa: E402
import spool  # noqa: E402
import tracksdoc  # noqa: E402
import ghauth  # noqa: E402
import selfupdate  # noqa: E402
import verb_help  # noqa: E402
from local_workspace import DEFAULT_AGENT_PORT, common_root, origin_slug, write_agent_port  # noqa: E402

CLIENT = "2mw2lt-install"
DEVICE_GRANT = "urn:ietf:params:oauth:grant-type:device_code"
DEFAULT_TRACKS = tracksdoc.DEFAULT_SOURCE
POLL = 5.0
ENROLMENT = 600.0  # how long a new team's silo may take to start before the run stops to resume
GRANT = 900.0  # how long the person may take to connect the App before the run stops to resume


class Stop(SystemExit):
    """A step that cannot go on, and what the person does about it."""


def say(step: str, text: str) -> None:
    print(f"{step:<10}  {text}", flush=True)


def platform_url() -> str:
    """The console this plugin serves. `STEERING_PLATFORM` names another deployment as
    `<console url> <door url>`; the door comes from the console's workspace answer."""
    named = os.environ.get("STEERING_PLATFORM", "").split()
    if named:
        return named[0].rstrip("/")
    return "https://console.2mw2lt.com"


# --- HTTP, with the agent's own opener: no proxy, no redirect, a user agent Cloudflare admits

def call(method: str, url: str, body: dict | None = None, token: str | None = None,
         timeout: float = 60) -> tuple[int, dict]:
    req = urllib.request.Request(url, method=method,
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Content-Type": "application/json", "Accept": "application/json"})
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with door_mod.send(req, timeout) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read() or b"{}")
        except ValueError:
            return e.code, {}
    except (urllib.error.URLError, OSError) as e:
        raise Stop(f"{url} did not answer: {e}") from None


def open_page(url: str) -> None:
    """The browser, where this machine has one to open; the URL is printed either way."""
    try:
        if platform.system() == "Darwin":
            subprocess.run(["open", url], capture_output=True, timeout=10)
        else:
            webbrowser.open(url)
    except Exception:  # printing the URL is the fallback, not an error
        pass


# --- the steps

def repository(root: Path) -> str:
    repo = origin_slug(root, timeout=5)
    if not repo:
        raise Stop(f"{root} has no GitHub origin; add one (`git remote add origin …`) and run this again")
    return repo


def tracks_source(root: Path, repo: str) -> str:
    """The repository's tracks document, if one is committed; otherwise where the skill drafts one."""
    listed = subprocess.run(["git", "-C", str(root), "ls-files", "*tracks*.json"],
                            capture_output=True, text=True, timeout=10).stdout.split()
    for path in listed:
        try:
            doc = json.loads((root / path).read_text())
            if not tracksdoc.validate(doc, repo):
                return path
        except (OSError, ValueError):
            continue
    return DEFAULT_TRACKS


def sign_in(console: str, repo: str, label: str) -> str:
    """A console session from a device grant, held in memory for this run only."""
    status, got = call("POST", f"{console}/api/auth/device/code",
                       {"client_id": CLIENT, "scope": f"install {repo} on {label}"})
    if status != 200 or "device_code" not in got:
        raise Stop(f"the console would not start a sign-in ({status}): {got}")
    page = got.get("verification_uri_complete") or got["verification_uri"]
    say("sign in", f"open {got['verification_uri']} and enter {got['user_code']}")
    say("", "(opened in your browser; waiting…)")
    open_page(page)
    interval, deadline = float(got.get("interval") or POLL), time.monotonic() + float(got.get("expires_in") or 1800)
    while time.monotonic() < deadline:
        time.sleep(interval)
        status, answer = call("POST", f"{console}/api/auth/device/token",
                              {"grant_type": DEVICE_GRANT, "device_code": got["device_code"], "client_id": CLIENT})
        if status == 200 and answer.get("access_token"):
            return answer["access_token"]
        error = answer.get("error")
        if error == "slow_down":
            interval += 5
        elif error != "authorization_pending":
            raise Stop(f"sign-in ended: {answer.get('error_description') or error or status}")
    raise Stop("the sign-in code expired; run this again")


def workspace(console: str, token: str, repo: str, source: str, team: str | None = None) -> dict:
    """The workspace for `repo`. A team with no silo is enrolled first: the console answers
    `enrolling` until its silo serves, and the same request is asked again until then."""
    body = {"repo": repo, "tracks_source": source, **({"team": team} if team else {})}
    told, deadline = False, time.monotonic() + ENROLMENT
    while True:
        status, got = call("POST", f"{console}/api/install/workspace", body, token)
        if status != 200:
            if told:
                print(flush=True)
            raise Stop(f"the console would not make the workspace ({status}): {got.get('error') or got}")
        enrolling = got.get("enrolling")
        if not isinstance(enrolling, dict):
            if told:
                print(" ready", flush=True)
            return got
        if not told:
            print(f"{'team':<10}  {enrolling.get('team')} is new here; making its silo…", end="", flush=True)
            told = True
        if time.monotonic() > deadline:
            print(flush=True)
            raise Stop("the team's silo is still being made; run this again to wait for it")
        time.sleep(POLL)


def checked_door(value: object, authority: str) -> str:
    """A console-returned or recorded workspace door, valid for the authority it names."""
    if not isinstance(value, str):
        raise Stop("the workspace answer carried no door")
    previous = os.environ.get("STEERING_DOOR")
    try:
        os.environ["STEERING_DOOR"] = value
        door, _ = door_mod.door()
    except door_mod.InvalidDoor as e:
        raise Stop(str(e)) from None
    finally:
        if previous is None:
            os.environ.pop("STEERING_DOOR", None)
        else:
            os.environ["STEERING_DOOR"] = previous
    if door_mod.split(door)[1] != authority:
        raise Stop(f"the workspace door does not name {authority}")
    return door


def recorded_door(root: Path, authority: str) -> str | None:
    try:
        lines = (root / ".env").read_text().splitlines()
    except FileNotFoundError:
        return None
    except OSError as e:
        raise Stop(f"could not read {root / '.env'}: {e.strerror or e}") from None
    for line in lines:
        key, _, value = line.strip().partition("=")
        if key == "STEERING_DOOR" and value.strip():
            return checked_door(value.strip().strip('"').strip("'"), authority)
    return None


def admit_machine(console: str, token: str, door: str, wid: str, label: str) -> None:
    status, got = call("POST", f"{console}/api/install/agent", {"workspace": wid, "label": label}, token)
    if status != 200 or "secret" not in got:
        raise Stop(f"the console would not admit this machine ({status}): {got.get('error') or got}")
    try:
        credential.enrol(door, console, got["secret"])
    except credential.NoCredential as e:
        raise Stop(f"this machine's credential was refused: {e}") from None


def admitted(door: str) -> bool:
    """Whether this machine holds a credential the door takes for this workspace."""
    try:
        if not credential.for_url(f"{door}/steering/authorities"):
            return False
    except credential.NoCredential:
        return False
    try:
        status, _ = call("GET", f"{door}/steering/authorities")
    except Stop:
        return False
    return status == 200


def serves(door: str) -> str | None:
    """The repository the door's workspace serves, as its authorities view names it."""
    try:
        status, got = call("GET", f"{door}/steering/authorities")
    except Stop:
        return None
    rows = got.get("authorities") if status == 200 and isinstance(got, dict) else None
    return rows[0].get("repo") if isinstance(rows, list) and len(rows) == 1 and isinstance(rows[0], dict) else None


def reach(door: str) -> dict:
    status, got = call("GET", f"{door}/api/v1/forge")
    if status != 200:
        raise Stop(f"the platform could not say whether the App reaches the repository ({status}): {got}")
    return got


def wait_for_grant(door: str, got: dict, repo: str, console: str, authority: str) -> None:
    if got.get("covered"):
        say("github", f"the 2mw2lt App reaches {repo}")
        return
    # GitHub's own install page records no team, so nothing would bind what it installs to this
    # one. The desk's Connect GitHub installs the App or authorizes the installation, and claims it.
    desk = f"{console}/?workspace={urllib.parse.quote(authority, safe='')}"
    say("github", f"the 2mw2lt App cannot read {repo} yet")
    say("", f"open {desk}")
    say("", "and choose Connect GitHub in the account menu (opened in your browser; waiting…)")
    say("", "only the team's owner, signed in with GitHub, has it: anyone else asks them to")
    open_page(desk)
    deadline = time.monotonic() + GRANT
    while not (got := reach(door)).get("covered"):
        if time.monotonic() >= deadline:
            raise Stop(f"the App does not reach {repo} yet: the team's owner, signed in with GitHub, chooses "
                       f"Connect GitHub on {desk}; then run this again")
        time.sleep(POLL)
    say("", f"{repo} is readable")


def agent_answers(port: int, root: Path) -> bool:
    """Whether the agent on `port` serves this workspace, among however many it serves."""
    try:
        req = urllib.request.Request(f"http://127.0.0.1:{port}/status")
        with door_mod.send(req, 3) as r:
            served = json.loads(r.read() or b"{}").get("workspaces") or []
        return any(Path(w).resolve() == root.resolve() for w in served)
    except (OSError, ValueError, urllib.error.URLError):
        return False


def machine_port() -> int:
    """The port the machine's one agent job listens on, or the default when it has none yet."""
    import plistlib
    try:
        doc = plistlib.loads((Path.home() / "Library" / "LaunchAgents" / f"{agentjob.MACHINE_LABEL}.plist").read_bytes())
    except (OSError, plistlib.InvalidFileException):
        return int(DEFAULT_AGENT_PORT)
    named = str((doc.get("EnvironmentVariables") or {}).get("STEERING_AGENT_PORT") or "") if isinstance(doc, dict) else ""
    return int(named) if named.isdecimal() else int(DEFAULT_AGENT_PORT)


def release(door: str) -> Path:
    """The agent release the workspace's door states (doc 147 §3–4), fetched with this machine's
    credential and built when the machine has none. It never re-points the machine's other
    agents: the one job written below is the only one touched."""
    try:
        return agentjob.active_release(Path.home())[0]
    except RuntimeError:
        pass
    status, target = call("GET", f"{door}/steering/agent/target")
    if status == 404:
        raise Stop("the workspace's door states no agent release; ask its operator to deploy one")
    if status != 200 or not selfupdate.valid(target):
        raise Stop(f"the workspace's door answered {status} for its agent release")
    base = agentjob.root(Path.home())
    found = selfupdate.built(base, target["sha"])
    if found is None:
        say("agent", f"fetching agent release {target['sha'][:12]} from the workspace's door…")
        try:
            part = asyncio.run(selfupdate.fetch(_stream(door), door, target, base))
            found = selfupdate.build(part, base)
        except (selfupdate.Refused, selfupdate.BuildFailed, OSError, urllib.error.URLError, tarfile.TarError) as e:
            raise Stop(f"the agent release could not be installed: {e}") from None
    agentjob.select(found, Path.home())
    settings = agentjob.settings(Path.home())
    if not settings.exists():
        settings.parent.mkdir(parents=True, exist_ok=True)
        spool.write_atomic(settings, "", mode=0o600)
    return found


def _stream(door: str):
    """`selfupdate.fetch`'s stream over this client's opener, which presents the machine's
    credential to the door."""
    class Body:
        def __init__(self, r):
            self.status_code, self._r = getattr(r, "status", None) or r.code, r

        async def aiter_bytes(self):
            while chunk := await asyncio.to_thread(self._r.read, 1 << 16):
                yield chunk

    @contextlib.asynccontextmanager
    async def stream(path: str):
        req = urllib.request.Request(f"{door}{path}")
        try:
            r = await asyncio.to_thread(door_mod.send, req, 300)
        except urllib.error.HTTPError as e:
            r = e
        try:
            yield Body(r)
        finally:
            r.close()
    return stream


def run_installer(installed: Path, root: Path, door: str, port: int) -> None:
    """The release's own installer, under the release's own interpreter, so the job is written
    by the closure it will run rather than by this plugin's copy."""
    steering = installed / "steering"
    # The machine job's own port, so an install leaves the job as it is when it already runs.
    done = subprocess.run([str(steering / ".venv" / "bin" / "python"), str(steering / "install-agent.py"),
                           str(root), door], env={**os.environ, "STEERING_AGENT_PORT": str(port)},
                          capture_output=True, text=True)
    if done.returncode:
        raise Stop(f"the agent release's installer failed: {(done.stderr or done.stdout).strip()[-400:]}")


def ensure_agent(root: Path, authority: str, door: str) -> int:
    """The port of the machine's one agent, serving this workspace (doc 130). The workspace must
    already name `door` in its `.env`: the agent serves a registered workspace only then."""
    port = machine_port()
    if agent_answers(port, root):
        write_agent_port(root, str(port))
        return port
    if platform.system() != "Darwin":
        raise Stop(f"the agent is started by launchd, and this machine runs {platform.system()}; "
                   f"only macOS is supported so far")
    # The job is written by the installer of the release it runs, not by this plugin's copy.
    run_installer(release(door), root, door, port)
    write_agent_port(root, str(port))
    end = time.monotonic() + 60
    while not agent_answers(port, root):
        if time.monotonic() > end:
            raise Stop(f"the agent did not answer on 127.0.0.1:{port}; its log is under ~/Library/Logs/2mw2lt/")
        time.sleep(1)
    return port


def wire(root: Path, door: str, authority: str, codex: bool = False) -> None:
    """`STEERING_DOOR`, the workspace's id and Claude's hook pack; nothing else local. Codex runs
    no hooks: its sessions share only the enrolment tokens' directory."""
    import hooks
    env = root / ".env"
    lines = env.read_text().splitlines() if env.exists() else []
    lines = [line for line in lines if not line.startswith("STEERING_DOOR=")] + [f"STEERING_DOOR={door}"]
    spool.write_atomic(env, "\n".join(lines) + "\n", mode=0o600)
    checkout_binding.bind(root, authority)
    os.environ["STEERING_DOOR"] = door
    if codex:
        hooks.tokens_directory(root)
    else:
        hooks.hooks_install(root, local=False, workspace_id=authority)


def github_account(root: Path, repo: str) -> str:
    try:
        names = ghauth.readable_accounts(repo)
    except ghauth.NoIdentity as e:
        raise Stop(str(e)) from None
    if not names:
        raise Stop(f"no gh account can read {repo}; run `gh auth login` with an account that can, then install again")
    held = machine_workspaces.at(root)
    if held and held.gh_account in names:
        return held.gh_account
    if len(names) == 1:
        return names[0]
    say("github", f"accounts that can read {repo}: {', '.join(names)}")
    try:
        name = input("GitHub login for this workspace: ").strip()
    except (EOFError, KeyboardInterrupt):
        raise Stop("choose a GitHub login in a terminal, then run install again") from None
    if name not in names:
        raise Stop(f"choose one of {', '.join(names)}, then run install again")
    return name


def install(root: Path, codex: bool = False, team: str | None = None) -> int:
    console = platform_url()
    repo = repository(root)
    say("repository", repo)
    name = github_account(root, repo)
    label = f"{socket.gethostname().split('.')[0]} as {getpass.getuser()}"
    bound = checkout_binding.id_at(root)
    door = recorded_door(root, bound) if bound else None
    got: dict | None = None
    if door and admitted(door) and (other := serves(door)) != repo:
        # Before any write: a checkout whose origin moved would otherwise keep reporting to the
        # workspace of the repository it came from (#2618).
        raise Stop(f"{root} is bound to {bound}, which serves {other or 'no repository it names'}, and its "
                   f"origin is {repo}; run `install.py uninstall` here, then install again")
    if not (door and admitted(door)):
        source = tracks_source(root, repo)
        token = sign_in(console, repo, label)
        try:
            got = workspace(console, token, repo, source, team)
            authority = got.get("id")
            if not isinstance(authority, str):
                raise Stop("the workspace answer carried no id")
            door = checked_door(got.get("door"), authority)
            say("workspace", f"{authority} {'created' if got.get('created') else 'found'}")
            admit_machine(console, token, door, authority, label)
            say("machine", f"this machine is admitted to {authority}")
        finally:
            end_session(console, token)
    authority = door_mod.split(door)[1]
    if authority is None:
        raise Stop("the workspace door names no workspace")
    wait_for_grant(door, (got or {}).get("forge") or reach(door), repo, console, authority)
    wire(root, door, authority, codex)
    say("hooks", "none for Codex" if codex else "written")
    held = machine_workspaces.at(root)
    machine_workspaces.write(machine_workspaces.directory(Path.home()), authority, machine_workspaces.Entry(
        root.resolve(), door, held.capability_file if held else None, held.port if held else None, name))
    port = ensure_agent(root, authority, door)
    say("agent", f"serving {authority} on 127.0.0.1:{port}")
    source = tracks_source(root, repo)
    say("tracks", f"missing {source}" if not (root / source).exists() else f"{source}")
    print(json.dumps({"workspace": authority, "door": door, "port": port, "tracks": source,
                      "tracks_missing": not (root / source).exists()}))
    return 0


def end_session(console: str, token: str) -> None:
    """End the console session; one that cannot be ended lapses on its own, and must not hide
    whatever stopped the run."""
    try:
        call("DELETE", f"{console}/api/install/session", token=token)
    except Stop:
        pass


def uninstall(root: Path, retire: bool) -> int:
    import hooks
    console = platform_url()
    bound = checkout_binding.id_at(root)
    try:
        door = recorded_door(root, bound) if bound else None
    except Stop as e:
        door = None
        say("credential", f"kept: {e}")
    if retire and bound:
        token = sign_in(console, repository(root), f"retire {bound}")
        try:
            status, got = call("DELETE", f"{console}/api/install/workspace/{bound}", token=token)
            if status != 200:
                raise Stop(f"the console would not retire {bound} ({status}): {got.get('error') or got}")
            say("workspace", f"{bound} retired; its state is kept on the platform")
        finally:
            end_session(console, token)
    hooks.uninstall(root)
    say("hooks", "removed")
    env = root / ".env"
    if env.exists():
        kept = [line for line in env.read_text().splitlines()
                if not line.startswith(("STEERING_DOOR=", "STEERING_AGENT_PORT="))]
        spool.write_atomic(env, "\n".join(kept) + ("\n" if kept else ""), mode=0o600)
    for leftover in (root / ".claude" / "steering-workspace", root / ".claude" / "steering-launch.py"):
        leftover.unlink(missing_ok=True)
    if bound:
        # The machine's agent stops serving it on its next read of the registry; the job serves
        # the machine's other workspaces and stays.
        (machine_workspaces.directory(Path.home()) / f"{bound}.json").unlink(missing_ok=True)
        if door:
            credential.path_for(door).unlink(missing_ok=True)
        say("agent", f"{bound} deregistered from this machine's agent")
    return 0


def main(argv: list[str]) -> int:
    if verb_help.help_requested("install", argv):
        print(verb_help.script_help("install", topic=argv[0] if len(argv) == 2 else None)); return 0
    args = list(argv)
    action = "uninstall" if args[:1] == ["uninstall"] else "install"
    if action == "uninstall":
        args.pop(0)
    retire = False
    codex = False
    team = None
    paths: list[str] = []
    i = 0
    while i < len(args):
        arg = args[i]
        if arg == "--workspace":
            if action != "uninstall":
                return verb_help.error("install", "--workspace is only for uninstall")
            retire = True; i += 1; continue
        if arg == "--codex":
            if action != "install":
                return verb_help.error("install", "--codex is only for install")
            codex = True; i += 1; continue
        if arg == "--team":
            if action != "install":
                return verb_help.error("install", "--team is only for install")
            if i + 1 >= len(args) or args[i + 1].startswith("-"):
                return verb_help.error("install", "missing team value")
            if team is not None:
                return verb_help.error("install", "team may appear once")
            team = args[i + 1]; i += 2; continue
        if arg.startswith("-"):
            return verb_help.error("install", f"unknown option {arg!r}")
        paths.append(arg); i += 1
    if action == "uninstall" and (codex or team is not None):
        return verb_help.error("install", "uninstall does not accept install options")
    if len(paths) > 1:
        return verb_help.error("install", "too many workspace paths")
    anchor = Path(paths[0]) if paths else Path.cwd()
    try:
        root = common_root(anchor, timeout=5)
        dirs = subprocess.run(["git", "-C", str(anchor), "rev-parse", "--path-format=absolute",
                               "--git-dir", "--git-common-dir"], capture_output=True, text=True,
                              check=True, timeout=5).stdout.splitlines()
    except Exception:
        raise Stop("this is not a Git checkout; run it from the repository to install") from None
    if Path(dirs[0]).resolve() != Path(dirs[1]).resolve():
        # What either writes is the shared checkout's, so one worktree's uninstall took every
        # sibling's hooks and door (#3431).
        raise Stop(f"{anchor} is a linked worktree; {action} acts on the checkout its worktrees "
                   f"share, so run it from {root}")
    return uninstall(root, retire) if action == "uninstall" else install(root, codex, team)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
