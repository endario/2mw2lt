#!/usr/bin/env python3
"""`/2mw2lt:install`'s engine (doc 129): a repository becomes a working, observed workspace.

    install.py [workspace] [invite-code] [--codex] [--team <id>] [--gh-account <login>] [--login <login>]
                                        install, or resume one half done; --codex writes no hooks;
                                        --team names the team when the person owns several;
                                        --gh-account names the workspace's GitHub login when
                                        several signed-in logins can read the repository;
                                        --login names the GitHub account to sign in as, when it
                                        is not the workspace's
    install.py lanes [workspace] [--merge] [--gh-account <login>]
                                        the open lanes pull request and how it would merge;
                                        --merge merges it as the workspace's login, then waits
                                        for the daemon to sync the merged lanes
    install.py uninstall [workspace]    remove what an install added here
    install.py uninstall --workspace    and have the platform retire the workspace too
    install.py retire <workspace id>    have the platform retire a workspace by its id, for one
                                        no checkout here is bound to (#3742)

The person acts only where a grant is theirs to give: signing in, and giving the App
the repository. For each it prints the page, opens it, and waits.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parents[1]))
import python_floor  # noqa: E402

python_floor.require()

import asyncio
import base64
import contextlib
import getpass
import hashlib
import http.client
import json
import os
import platform
import re
import socket
import subprocess
import tarfile
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser

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


_at = ""  # the step last shown, which a stop names


def say(step: str, text: str) -> None:
    global _at
    _at = step or _at
    print(f"{step:<10}  {text}", flush=True)


def platform_url() -> str:
    """The console this plugin serves. `STEERING_PLATFORM` names another deployment as
    `<console url> <door url>`; the door comes from the console's workspace answer."""
    named = os.environ.get("STEERING_PLATFORM", "").split()
    if named:
        return named[0].rstrip("/")
    return "https://console.2mw2lt.com"


# --- HTTP, with the agent's own opener: no proxy, no redirect, a user agent Cloudflare admits

TRANSIENT = frozenset({502, 503, 504})  # a gateway's answer while what is behind it restarts


class Unanswered(Exception):
    """The console or a door did not answer, or a gateway answered for it: worth asking again."""

    def __init__(self, reason: str, after: float | None = None):
        super().__init__(reason)
        self.after = after


def _who(url: str) -> str:
    return "the console" if url.startswith(f"{platform_url()}/api/") else "the workspace's door"


def _origin(url: str) -> str:
    parts = urllib.parse.urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}"


def _reason(e: BaseException) -> str:
    r = getattr(e, "reason", e)
    if isinstance(r, (TimeoutError, socket.timeout)):
        return "timeout"
    return str(getattr(r, "strerror", None) or r) or type(r).__name__


def _retry_after(headers) -> float | None:
    try:
        return max(0.0, float(headers.get("Retry-After"))) if headers is not None else None
    except (TypeError, ValueError):
        return None


def ask(method: str, url: str, body: dict | None = None, token: str | None = None,
        timeout: float = 60) -> tuple[int, dict]:
    """One request and its answer; `Unanswered` when nothing answered or a gateway answered."""
    req = urllib.request.Request(url, method=method,
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Content-Type": "application/json", "Accept": "application/json"})
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with door_mod.send(req, timeout) as r:
            status, raw = r.status, r.read()
    except urllib.error.HTTPError as e:
        try:
            got = json.loads(e.read() or b"{}")
        except ValueError:
            got = {}
        # A gateway's 502-504 carries no reason of its own; the console's own refusal does.
        if e.code in TRANSIENT and not (isinstance(got, dict) and got.get("error")):
            raise Unanswered(str(e.code), _retry_after(e.headers)) from None
        return e.code, got if isinstance(got, dict) else {}
    except (urllib.error.URLError, TimeoutError, ConnectionError, http.client.HTTPException) as e:
        raise Unanswered(_reason(e)) from None
    except OSError as e:  # `door.send` refusing a credential it holds and cannot use
        raise Stop(f"{_who(url)} at {_origin(url)} was not asked: {e}; run install again to admit "
                   "this machine afresh") from None
    try:
        got = json.loads(raw or b"{}")
    except ValueError:
        got = None
    if not isinstance(got, dict):
        raise Stop(f"{_who(url)} at {_origin(url)} answered {status} with something that is not a JSON object; "
                   "run install again, or ask the operator if it repeats")
    return status, got


def call(method: str, url: str, body: dict | None = None, token: str | None = None,
         timeout: float = 60) -> tuple[int, dict]:
    """A request outside any wait: one that goes unanswered stops the run."""
    try:
        return ask(method, url, body, token, timeout)
    except Unanswered as e:
        raise Stop(f"{_who(url)} at {_origin(url)} did not answer ({e}); "
                   "check the network, then run install again") from None


def waited(method: str, url: str, deadline: float, body: dict | None = None,
           token: str | None = None) -> tuple[int, dict]:
    """A request inside a wait: one that goes unanswered is asked again, with backoff, until
    `deadline`, and each retry is shown rather than ending the run."""
    delay = POLL
    while True:
        try:
            return ask(method, url, body, token)
        except Unanswered as e:
            pause = max(delay, e.after or 0)
            if time.monotonic() + pause > deadline:
                raise Stop(f"{_who(url)} at {_origin(url)} did not answer ({e}); "
                           "check the network, then run install again") from None
            say("", f"{_who(url)} did not answer ({e}); retrying")
            time.sleep(pause)
            delay = min(delay * 2, 60.0)


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


def refusal_proof(device_code: str) -> str:
    """The proof the console's refusal route checks against this grant's device code
    (console/src/lib/device-refusal.ts)."""
    digest = hashlib.sha256(b"2mw2lt device refusal v1\0" + device_code.encode()).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


# An invitation code as the console mints it (console/src/lib/invite.ts), typed in any case (#3930).
INVITE = re.compile(r"2MW(-[2-9A-HJKMNP-TV-Z]{4}){4}", re.IGNORECASE)
# A GitHub login, as the console reads the one install carries (#3954).
LOGIN = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9]|-(?=[A-Za-z0-9])){0,38}")
# A workspace id, as the console mints one (console/src/lib/workspace-id.ts): one path segment.
WORKSPACE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,31}")


def ended(said: object, login: str | None) -> str:
    """The console's words for an ended sign-in. Its refusal for another account than the machine's
    cannot name the login (#3954), which this run carried, so it is named here."""
    said = str(said)
    return f"{said} This machine uses @{login}." if login and "than the one this machine uses" in said else said


def sign_in(console: str, scope: str, invite: str | None = None, login: str | None = None,
            named: bool = False) -> str:
    """A console session from a device grant, held in memory for this run only. `scope` is what
    the /device page shows the person this grant does."""
    status, got = call("POST", f"{console}/api/auth/device/code",
                       {"client_id": CLIENT, "scope": scope})
    if status != 200 or "device_code" not in got:
        raise Stop(f"the console would not start a sign-in ({status}): {got}")
    page = got.get("verification_uri_complete") or got["verification_uri"]
    say("sign in", f"open {got['verification_uri']} and enter {got['user_code']}")
    if invite:
        # An invitation makes a team of the person's own; joining another's is not one yet (#4015).
        say("invite", "this creates a team of your own")
    # An invitation decides the account; the machine's own login is only a guess at it (#4015).
    who = f"@{login}" if login and (named or not invite) else "the account your invitation is for" if invite else None
    say("", f"(opened in your browser; sign in to GitHub as {who}; waiting…)" if who else "(opened in your browser; waiting…)")
    # Only the browser opened here holds the proof, so only it can end this grant when its sign-in
    # is refused (#3586). It is never printed: a printed proof is as public as the user code.
    # An invitation code rides the same fragment, for the sign-in door to carry to GitHub; like the
    # proof, a fragment reaches no server log. So does the login this machine signs in as (#3954),
    # which lets an invite waiting for that account admit it with no code. A login the person named
    # with --login is marked, so the approval page refuses another account outright (#4015).
    carried = (f"&invite={invite.upper()}" if invite else "") + (f"&login={login}" if login else "") \
        + ("&named=1" if login and named else "")
    open_page(f"{page}#refusal={refusal_proof(got['device_code'])}{carried}")
    interval, deadline = float(got.get("interval") or POLL), time.monotonic() + float(got.get("expires_in") or 1800)
    start = told = time.monotonic()
    while time.monotonic() < deadline:
        time.sleep(interval)
        if time.monotonic() - told >= PROGRESS:
            told = time.monotonic()
            say("", f"waiting for the sign-in to be approved ({elapsed(told - start)})")
        status, answer = waited("POST", f"{console}/api/auth/device/token", deadline,
                                {"grant_type": DEVICE_GRANT, "device_code": got["device_code"], "client_id": CLIENT})
        if status == 200 and answer.get("access_token"):
            return answer["access_token"]
        error = answer.get("error")
        if error == "slow_down":
            interval += 5
        elif error != "authorization_pending":
            raise Stop(f"sign-in ended: {ended(answer.get('error_description') or error or status, login)}")
    raise Stop("the sign-in code expired; run this again")


HOST_STATES = {"pending": "waiting for the host to allocate it", "allocated": "starting the silo"}
PROGRESS = 60.0  # how often a wait that needs a person says it is still waiting
WATCHER = 120.0  # how long a request may sit pending before the host's watcher is the likely cause


def elapsed(seconds: float) -> str:
    return f"{int(seconds) // 60}m{int(seconds) % 60:02d}s"


def workspace(console: str, token: str, repo: str, source: str, team: str | None = None) -> dict:
    """The workspace for `repo`. A team with no silo is enrolled first: the console answers
    `enrolling` until its silo serves, and the same request is asked again until then."""
    body = {"repo": repo, "tracks_source": source, **({"team": team} if team else {})}
    start = time.monotonic()
    deadline, shown, told, watched = start + ENROLMENT, None, -60.0, False
    while True:
        status, got = waited("POST", f"{console}/api/install/workspace", deadline, body, token)
        if status != 200:
            raise Stop(f"the console would not make the workspace ({status}): {got.get('error') or got}")
        enrolling = got.get("enrolling")
        if not isinstance(enrolling, dict):
            if shown is not None:
                say("team", "its silo is ready")
            return got
        state, waited_for = enrolling.get("state"), time.monotonic() - start
        if state != shown or waited_for - told >= PROGRESS:
            say("team", f"{enrolling.get('team')} is new here; {HOST_STATES.get(state, state)} "
                        f"({elapsed(waited_for)})")
            shown, told = state, waited_for
        since = enrolling.get("since")
        if (not watched and state == "pending" and isinstance(since, (int, float))
                and time.time() - since > WATCHER):
            say("", "waiting on the host's enrolment watcher; if this lasts, ask the operator")
            watched = True
        if time.monotonic() > deadline:
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


def authorities(door: str) -> dict | None:
    """The door's authorities view on this machine's credential, or None when this machine is not
    admitted to the workspace: it holds no credential for the door, or the door refuses it."""
    try:
        if not credential.for_url(f"{door}/steering/authorities"):
            return None
    except credential.NoCredential as e:
        say("machine", f"this machine's credential is not usable ({e}); admitting it again")
        return None
    status, got = call("GET", f"{door}/steering/authorities")
    if status in (401, 403):
        say("machine", f"the workspace's door refused this machine ({status}); admitting it again")
        return None
    if status != 200:
        raise Stop(f"the workspace's door answered {status} for its authorities: {got.get('error') or got}; "
                   "run install again, or ask the operator if it repeats")
    return got


def serves(view: dict) -> str | None:
    """The repository the door's workspace serves, as its authorities view names it."""
    rows = view.get("authorities")
    return rows[0].get("repo") if isinstance(rows, list) and len(rows) == 1 and isinstance(rows[0], dict) else None


def reach(door: str, deadline: float | None = None) -> dict:
    """Whether the App reaches the workspace's repository; a door that does not answer is waited
    through until `deadline`, a minute when no wait names one."""
    url = f"{door}/api/v1/forge"
    status, got = waited("GET", url, deadline if deadline is not None else time.monotonic() + 60)
    if status != 200:
        raise Stop(f"the platform could not say whether the App reaches the repository ({status}): "
                   f"{got.get('error') or got}; run install again, or ask the operator if it repeats")
    return got


def wait_for_grant(door: str, got: dict, repo: str, desk: str, authority: str, approved_here: bool = False) -> None:
    """`approved_here`: this run's sign-in was approved in a browser, which carries on to GitHub
    itself (#3930). A run that resumed without one opens the desk instead."""
    if got.get("covered"):
        say("github", f"the 2mw2lt App reaches {repo}")
        return
    # GitHub's own install page records no team, so nothing would bind what it installs to this
    # one. The browser that approved this terminal carries on to the console's claim, which installs
    # the App or authorizes the installation and claims it (#3930); the desk's Connect GitHub is the
    # same claim, for a browser that did not.
    say("github", f"the 2mw2lt App cannot read {repo} yet")
    # Without the console's answer the desk's address is unknown, and `/` opens the one last shown.
    where = f"{desk}" + ("" if desk.endswith(f"/{authority}") else f", switch to the workspace {authority}")
    if approved_here:
        say("", "your browser carries on to GitHub's App page; install it there (waiting…)")
        say("", f"from another browser: open {where} and choose Connect GitHub in the account menu")
    else:
        say("", f"open {where}")
        say("", "and choose Connect GitHub in the account menu (opened in your browser; waiting…)")
    say("", "only the team's owner, signed in with GitHub, can: anyone else asks them to")
    if isinstance(got.get("grant"), str):
        # The claim is for a team holding no installation; one that holds an installation adds
        # the repository to it on GitHub's own page.
        say("", f"if the team's App is installed already, add {repo} to it at {got['grant']}")
    if not approved_here:
        open_page(desk)
    start = told = time.monotonic()
    deadline = start + GRANT
    while not (got := reach(door, deadline)).get("covered"):
        if time.monotonic() - told >= PROGRESS:
            told = time.monotonic()
            say("", f"waiting for the App to reach {repo} ({elapsed(told - start)})")
        if time.monotonic() >= deadline:
            raise Stop(f"the App does not reach {repo} yet: the team's owner, signed in with GitHub, chooses "
                       f"Connect GitHub on {desk}; then run this again")
        time.sleep(POLL)
    say("", f"{repo} is readable")


def agent_answers(port: int, root: Path) -> bool:
    """Whether the agent on `port` serves this workspace, among however many it serves; False
    when nothing listens there. Anything else on the port is a stop naming what answered."""
    try:
        req = urllib.request.Request(f"http://127.0.0.1:{port}/status")
        with door_mod.send(req, 3) as r:
            served = json.loads(r.read() or b"{}").get("workspaces")
    except urllib.error.HTTPError as e:
        raise Stop(f"127.0.0.1:{port} answered {e.code} to /status, which this machine's 2mw2lt agent "
                   f"does not; stop what holds the port, then run install again") from None
    except urllib.error.URLError as e:
        if isinstance(e.reason, ConnectionRefusedError):
            return False
        raise Stop(f"the agent on 127.0.0.1:{port} did not answer ({_reason(e)}); "
                   "its log is under ~/Library/Logs/2mw2lt/") from None
    except (TimeoutError, ConnectionError, http.client.HTTPException) as e:
        raise Stop(f"the agent on 127.0.0.1:{port} did not answer ({_reason(e)}); "
                   "its log is under ~/Library/Logs/2mw2lt/") from None
    except (ValueError, AttributeError):
        served = None
    if not isinstance(served, list):
        raise Stop(f"127.0.0.1:{port} answers, but not as this machine's 2mw2lt agent; "
                   "stop what holds the port, then run install again")
    return any(Path(w).resolve() == root.resolve() for w in served)


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
        raise Stop(f"the workspace's door answered {status} for its agent release; run install again, "
                   "or ask the operator if it repeats")
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
        raise unsupported()
    # The job is written by the installer of the release it runs, not by this plugin's copy.
    run_installer(release(door), root, door, port)
    write_agent_port(root, str(port))
    end = time.monotonic() + 60
    while True:
        try:
            if agent_answers(port, root):
                return port
            why = None
        except Stop as e:  # an agent starting up may drop a connection before it serves
            why = e
        if time.monotonic() > end:
            raise why or Stop(f"the agent did not answer on 127.0.0.1:{port}; its log is under ~/Library/Logs/2mw2lt/")
        time.sleep(1)


def unsupported() -> Stop:
    return Stop(f"the agent is started by launchd, and this machine runs {platform.system()}; only macOS "
                "installs here. A Linux host is provisioned with steering/host/provision-agent-host.sh")


def record(root: Path, door: str, authority: str) -> None:
    """`STEERING_DOOR` and the workspace's id, written once the machine is admitted, so a run
    stopped after that resumes there rather than signing in and admitting it again."""
    env = root / ".env"
    lines = env.read_text().splitlines() if env.exists() else []
    lines = [line for line in lines if not line.startswith("STEERING_DOOR=")] + [f"STEERING_DOOR={door}"]
    spool.write_atomic(env, "\n".join(lines) + "\n", mode=0o600)
    checkout_binding.bind(root, authority)
    os.environ["STEERING_DOOR"] = door


def wire(root: Path, door: str, authority: str, codex: bool = False) -> None:
    """The door and id `record` writes, and Claude's hook pack; nothing else local. Codex runs
    no hooks: its sessions share only the enrolment tokens' directory."""
    import hooks
    record(root, door, authority)
    if codex:
        hooks.tokens_directory(root)
    else:
        hooks.hooks_install(root, local=False, workspace_id=authority)


def github_account(root: Path, repo: str, chosen: str | None = None) -> str:
    try:
        names = ghauth.readable_accounts(repo)
    except ghauth.NoIdentity as e:
        raise Stop(str(e)) from None
    if not names:
        raise Stop(f"no gh account can read {repo}; run `gh auth login` with an account that can, then install again")
    if chosen is not None:
        if chosen not in names:
            raise Stop(f"{chosen} cannot read {repo}; name one of {', '.join(names)} with --gh-account")
        return chosen
    held = machine_workspaces.at(root)
    if held and held.gh_account in names:
        return held.gh_account
    if len(names) == 1:
        return names[0]
    # The engine runs under an agent's shell, where no prompt can be answered.
    raise Stop(f"several GitHub logins can read {repo}; name one with --gh-account: {', '.join(names)}")


def install(root: Path, codex: bool = False, team: str | None = None,
            gh_account: str | None = None, invite: str | None = None, login: str | None = None) -> int:
    console = platform_url()
    repo = repository(root)
    say("repository", repo)
    if platform.system() != "Darwin" and not agent_answers(machine_port(), root):
        raise unsupported()
    name = github_account(root, repo, gh_account)
    label = f"{socket.gethostname().split('.')[0]} as {getpass.getuser()}"
    bound = checkout_binding.id_at(root)
    door = recorded_door(root, bound) if bound else None
    got: dict | None = None
    view = authorities(door) if door else None
    if view is not None and (other := serves(view)) != repo:
        # Before any write: a checkout whose origin moved would otherwise keep reporting to the
        # workspace of the repository it came from (#2618).
        raise Stop(f"{root} is bound to {bound}, which serves {other or 'no repository it names'}, and its "
                   f"origin is {repo}; run `install.py uninstall` here, then install again")
    if view is None:
        source = tracks_source(root, repo)
        # The workspace's GitHub login is the account the person most likely signs in as (#3954).
        token = sign_in(console, f"install {repo} on {label}", invite, login or name, named=login is not None)
        try:
            got = workspace(console, token, repo, source, team)
            authority = got.get("id")
            if not isinstance(authority, str):
                raise Stop("the workspace answer carried no id")
            door = checked_door(got.get("door"), authority)
            for step, said in (("signed in", got.get("user")), ("team", got.get("team"))):
                if isinstance(said, str):
                    say(step, said)
            say("workspace", f"{authority} {'created' if got.get('created') else 'found'}")
            admit_machine(console, token, door, authority, label)
            record(root, door, authority)
            say("machine", f"this machine is admitted to {authority}")
        finally:
            end_session(console, token)
    authority = door_mod.split(door)[1]
    if authority is None:
        raise Stop("the workspace door names no workspace")
    # The desk's address is the console's to say (desk-address.md); a checkout installed before
    # has no answer to read it from.
    desk = (got or {}).get("desk")
    wait_for_grant(door, (got or {}).get("forge") or reach(door), repo,
                   desk if isinstance(desk, str) and desk.startswith(console + "/") else f"{console}/", authority,
                   approved_here=got is not None)
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


# --- the lanes the person accepted, merged and synced (doc 174 §2–3)

LANES_BRANCH = "2mw2lt/tracks"
METHODS = (("allow_squash_merge", "squash"), ("allow_merge_commit", "merge"), ("allow_rebase_merge", "rebase"))
SYNC_WAIT = 120.0


def gh(args: list[str], token: str) -> tuple[bool, str]:
    """`gh` as the workspace's login: (whether it succeeded, what it printed or why it refused)."""
    try:
        done = subprocess.run(["gh", *args], capture_output=True, text=True, timeout=60,
                              env={**os.environ, "GH_TOKEN": token})
    except subprocess.TimeoutExpired:
        return False, "gh did not answer in 60s"
    except OSError as e:
        return False, f"gh could not run: {e.strerror or e}"
    said = (done.stdout if done.returncode == 0 else done.stderr or done.stdout).strip()
    return done.returncode == 0, said


def gh_state(args: list[str], token: str) -> dict:
    """A pull request's state, or {} when gh could not say: once a merge was asked for, what
    was asked is reported whether or not GitHub then answers."""
    ok, said = gh(args, token)
    try:
        got = json.loads(said) if ok else {}
    except ValueError:
        got = {}
    return got if isinstance(got, dict) else {}


def gh_json(args: list[str], token: str) -> object:
    ok, said = gh(args, token)
    if not ok:
        raise Stop(f"gh {' '.join(args[:2])} refused: {said.splitlines()[-1] if said else 'no reason given'}")
    try:
        return json.loads(said or "null")
    except ValueError:
        raise Stop(f"gh {' '.join(args[:2])} answered something that is not JSON") from None


def lanes_plan(root: Path, repo: str, token: str) -> dict:
    """The open pull request from the repository's own `2mw2lt/tracks`, and how it would merge."""
    listed = gh_json(["pr", "list", "-R", repo, "--head", LANES_BRANCH, "--state", "open",
                      "--json", "number,url,headRefOid,isCrossRepository"], token)
    # A fork's branch of the same name is not the lanes the person drafted here.
    own = [p for p in listed or [] if isinstance(p, dict) and not p.get("isCrossRepository")]
    if not own:
        raise Stop(f"{repo} has no open pull request from {LANES_BRANCH}; draft the lanes again")
    pr = own[0]
    local = subprocess.run(["git", "-C", str(root), "rev-parse", "-q", "--verify", f"refs/heads/{LANES_BRANCH}"],
                           capture_output=True, text=True, timeout=10).stdout.strip()
    if local and local != pr["headRefOid"]:
        raise Stop(f"pull request #{pr['number']} holds {pr['headRefOid'][:12]}, not the lanes committed here "
                   f"({local[:12]}); push {LANES_BRANCH}, then run this again")
    settings = gh_json(["api", f"repos/{repo}"], token)
    settings = settings if isinstance(settings, dict) else {}
    method = next((m for key, m in METHODS if settings.get(key)), None)
    if method is None:
        raise Stop(f"{repo} allows no merge method gh can use")
    return {"pr": pr["number"], "url": pr["url"], "head": pr["headRefOid"], "method": method,
            "auto": bool(settings.get("allow_auto_merge"))}


def merge_refusal(state: dict, said: str) -> tuple[str, bool]:
    """Why GitHub would not merge, in the person's words, and whether it is only a check still
    running, the one refusal that passes without a person."""
    if state.get("reviewDecision") in ("REVIEW_REQUIRED", "CHANGES_REQUESTED"):
        return "the repository requires an approving review first", False
    if state.get("mergeable") == "CONFLICTING" or state.get("mergeStateStatus") == "DIRTY":
        return "it conflicts with the default branch", False
    checks = [c for c in state.get("statusCheckRollup") or [] if isinstance(c, dict)]
    failed = any(c.get("conclusion") in ("FAILURE", "TIMED_OUT", "CANCELLED", "ACTION_REQUIRED")
                 or c.get("state") in ("FAILURE", "ERROR") for c in checks)
    running = any(c.get("status") not in (None, "COMPLETED") or c.get("state") in ("PENDING", "EXPECTED")
                  for c in checks)
    if failed:
        return "a check failed", False
    if running and state.get("mergeStateStatus") == "BLOCKED":
        return "a required check is still running", True
    last = said.splitlines()[-1] if said else "no reason given"
    return f"GitHub refused it: {last.removeprefix('X ').strip()}", False


def merge_lanes(repo: str, plan: dict, token: str) -> dict:
    """Merge the lanes the person accepted, never with `--admin`: a protected branch's rule is the
    team's. A merge waiting on a running check is set to merge itself where the repository allows."""
    n, method = str(plan["pr"]), f"--{plan['method']}"
    ok, said = gh(["pr", "merge", n, "-R", repo, method, "--match-head-commit", plan["head"]], token)
    if not ok:
        state = gh_state(["pr", "view", n, "-R", repo, "--json",
                          "reviewDecision,mergeable,mergeStateStatus,statusCheckRollup"], token)
        reason, running = merge_refusal(state, said)
        say("lanes", f"#{n} was not merged: {reason}")
        if not (running and plan["auto"]):
            return {"merged": False, "reason": reason}
        ok, said = gh(["pr", "merge", n, "-R", repo, method, "--auto", "--match-head-commit", plan["head"]], token)
        if not ok:
            reason = f"{reason}, and it could not be set to merge itself: {merge_refusal({}, said)[0]}"
            say("lanes", f"#{n} {reason}")
            return {"merged": False, "reason": reason}
    state = {}
    for _ in range(3):
        state = gh_state(["pr", "view", n, "-R", repo, "--json", "state,mergeCommit"], token)
        if state:
            break
        time.sleep(POLL)
    if not state:
        say("lanes", f"#{n} was sent to merge, and GitHub did not say whether it landed")
        return {"merged": False, "reason": "GitHub did not say whether it merged", "queued": True}
    if state.get("state") != "MERGED":
        say("lanes", f"#{n} merges itself once its checks pass")
        return {"merged": False, "reason": "it merges itself once its checks pass", "queued": True}
    commit = (state.get("mergeCommit") or {}).get("oid")
    say("lanes", f"#{n} merged ({plan['method']}) as {str(commit)[:12]}")
    return {"merged": True, "commit": commit}


NO_TRACKS = "the workspace keeps no lanes; its door names no tracks document to sync"


def tracks_now(door: str) -> tuple[int | None, dict]:
    """The daemon's lanes as this machine reads them, with the status; None while it does not answer."""
    try:
        status, got = ask("GET", f"{door}/api/v1/tracks", timeout=30)
    except Unanswered:
        return None, {}
    return status, got if status == 200 else {}


UNREAD = object()  # the lanes before the merge could not be read, so no later sync is known new


def wait_for_lanes(door: str, before, commit: str | None, deadline: float) -> dict:
    """Until the daemon holds lanes synced at the merge or after it, or refuses the merged document.
    The mirror and the tracks fold move on GitHub's push; nothing here asks them to."""
    while True:
        status, t = tracks_now(door)
        if status == 404:
            say("lanes", NO_TRACKS)
            return {"synced": False, "reason": NO_TRACKS}
        drift = t.get("drift") or {}
        if commit and drift.get("commit") == commit:
            say("lanes", f"the daemon refused the merged document: {drift.get('reason')}")
            return {"synced": False, "reason": f"the merged document was refused: {drift.get('reason')}"}
        synced = (t.get("source") or {}).get("commit")
        if t.get("items") and synced and (synced == commit or before is not UNREAD and synced != before):
            say("lanes", f"the board holds {len(t['items'])} lanes, synced at {synced[:12]}")
            return {"synced": True}
        if time.monotonic() + POLL > deadline:
            say("lanes", "the lanes are merged; the board shows them once the daemon syncs the repository")
            return {"synced": False}
        time.sleep(POLL)


def adopt_lanes(root: Path, merge: bool, gh_account: str | None = None) -> int:
    repo = repository(root)
    bound = checkout_binding.id_at(root)
    door = recorded_door(root, bound) if bound else None
    if door is None:
        raise Stop(f"{root} is not installed yet; run install first")
    name = github_account(root, repo, gh_account)
    try:
        token = ghauth.pinned_token(name)
    except ghauth.NoIdentity as e:
        raise Stop(str(e)) from None
    plan = lanes_plan(root, repo, token)
    if not merge:
        say("lanes", f"#{plan['pr']} would merge by {plan['method']} as {name}")
        print(json.dumps(plan))
        return 0
    status, t = tracks_now(door)
    if status == 404:
        raise Stop(NO_TRACKS)
    before = (t.get("source") or {}).get("commit") if status == 200 else UNREAD
    out = {"pr": plan["pr"], "url": plan["url"], **merge_lanes(repo, plan, token)}
    if out["merged"]:
        out.update(wait_for_lanes(door, before, out["commit"], time.monotonic() + SYNC_WAIT))
    print(json.dumps(out))
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
        # Its own scope: an install's carries the approval page on to GitHub for the repository (#4015).
        token = sign_in(console, f"retire {bound}")
        try:
            status, got = call("DELETE", f"{console}/api/install/workspace/{bound}", token=token)
            if status != 200:
                raise Stop(f"the console would not retire {bound} ({status}): {got.get('error') or got}")
            say("workspace", f"{bound} retired; its state is kept on the platform")
        finally:
            end_session(console, token)
    elif retire:
        say("workspace", "not retired: this checkout is bound to no workspace; "
                         "name the workspace with `install.py retire <id>`")
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


def retire_workspace(wid: str) -> int:
    """Retire a workspace by its id, the way to reach one no checkout here is bound to (#3742).
    What an install added on a machine is still that machine's `uninstall` to remove."""
    console = platform_url()
    token = sign_in(console, f"retire {wid}")
    try:
        status, got = call("DELETE", f"{console}/api/install/workspace/{wid}", token=token)
        if status != 200:
            raise Stop(f"the console would not retire {wid} ({status}): {got.get('error') or got}")
        say("workspace", f"{wid} retired; its state is kept on the platform")
    finally:
        end_session(console, token)
    return 0


def main(argv: list[str]) -> int:
    if verb_help.help_requested("install", argv):
        print(verb_help.script_help("install", topic=argv[0] if len(argv) == 2 else None)); return 0
    args = list(argv)
    action = args[0] if args[:1] in (["uninstall"], ["lanes"], ["retire"]) else "install"
    if action != "install":
        args.pop(0)
    retire = False
    merge = False
    codex = False
    team = None
    gh_account = None
    invite = None
    login = None
    paths: list[str] = []
    i = 0
    while i < len(args):
        arg = args[i]
        if arg == "--workspace":
            if action != "uninstall":
                return verb_help.error("install", "--workspace is only for uninstall")
            retire = True; i += 1; continue
        if arg == "--merge":
            if action != "lanes":
                return verb_help.error("install", "--merge is only for lanes")
            merge = True; i += 1; continue
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
        if arg == "--gh-account":
            if action == "uninstall":
                return verb_help.error("install", "--gh-account is only for install and lanes")
            if i + 1 >= len(args) or args[i + 1].startswith("-"):
                return verb_help.error("install", "missing gh-account value")
            if gh_account is not None:
                return verb_help.error("install", "gh-account may appear once")
            gh_account = args[i + 1]; i += 2; continue
        if arg == "--login":
            if action != "install":
                return verb_help.error("install", "--login is only for install")
            if i + 1 >= len(args) or not LOGIN.fullmatch(args[i + 1].removeprefix("@")):
                return verb_help.error("install", "--login takes a GitHub login")
            if login is not None:
                return verb_help.error("install", "login may appear once")
            login = args[i + 1].removeprefix("@"); i += 2; continue
        if arg.startswith("-"):
            return verb_help.error("install", f"unknown option {arg!r}")
        # An invitation code is taken as one wherever it stands, so `/2mw2lt:install <code>` runs as typed.
        if INVITE.fullmatch(arg) and action == "install":
            if invite is not None:
                return verb_help.error("install", "invite code may appear once")
            invite = arg; i += 1; continue
        paths.append(arg); i += 1
    if action != "install" and (codex or team is not None
                                or (action in ("uninstall", "retire") and gh_account is not None)):
        return verb_help.error("install", f"{action} does not accept install options")
    if len(paths) > 1:
        return verb_help.error("install", "too many workspace paths")
    if action == "retire" and (len(paths) != 1 or not WORKSPACE_ID.fullmatch(paths[0])):
        return verb_help.error("install", "retire takes one workspace id, as the install answer printed it")
    if action != "retire":
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
    global _at
    _at = ""
    try:
        if action == "retire":
            return retire_workspace(paths[0])
        if action == "lanes":
            return adopt_lanes(root, merge, gh_account)
        return uninstall(root, retire) if action == "uninstall" else install(root, codex, team, gh_account, invite, login)
    except KeyboardInterrupt:
        raise Stop(f"stopped at {_at or action}; run {action} again to resume") from None
    except subprocess.TimeoutExpired as e:
        raise Stop(f"stopped at {_at or action}: {Path(str(e.cmd[0])).name} did not answer in "
                   f"{e.timeout:g}s; run {action} again") from None
    except OSError as e:
        where = f" ({e.filename})" if e.filename else ""
        raise Stop(f"stopped at {_at or action}: {e.strerror or e}{where}; "
                   f"fix that, then run {action} again") from None


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
