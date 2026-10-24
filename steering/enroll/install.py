#!/usr/bin/env python3
"""`/2mw2lt:install`'s engine (doc 129): a repository becomes a working, observed workspace.

    install.py [workspace] [invite-code] [--codex] [--team <id>] [--gh-account <login>] [--login <login>]
                                        install, or resume one half done; --codex writes no hooks;
                                        --team names the team when the person may install in
                                        several, a new team of their own among them;
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
import shutil
import socket
import subprocess
import tarfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import webbrowser

HERE = Path(__file__).resolve().parent
STEERING = HERE.parent
sys.path.insert(0, str(STEERING))
sys.path.insert(0, str(HERE))

import agentjob  # noqa: E402
import credential  # noqa: E402
import door as door_mod  # noqa: E402
import door_rewrite  # noqa: E402
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
GRANT = 900.0  # how long the person may take to connect the App before the run stops to resume
SAID = 1.0  # how long the work between two lines must take before a step's line says it


class Stop(SystemExit):
    """A step that cannot go on, and what the person does about it."""


_at = ""  # the step last shown, which a stop names
_at_line = time.monotonic()  # when the previous line was said, which a slow step's line names


def say(step: str, text: str) -> None:
    """One line of the transcript. A line opening a step says how long the work since the previous
    line took, once that is at least `SAID`: the work between lines is that step's. A continuation
    line (blank step) never does — the waits it belongs to carry their own elapsed."""
    global _at, _at_line
    now = time.monotonic()
    if step:
        _at = step
        if now - _at_line >= SAID:
            text += f" ({elapsed(now - _at_line)})"
    _at_line = now
    print(f"{step:<10}  {text}", flush=True)


def platform_url() -> str:
    """The console this plugin serves; a workspace's door is `<console url>/<team>/<workspace>`."""
    named = os.environ.get("STEERING_PLATFORM", "").split()
    if named:
        return named[0].rstrip("/")
    return "https://console.2mw2lt.com"


# --- HTTP, with the agent's own opener: no proxy, no redirect, a user agent Cloudflare admits

TRANSIENT = frozenset({502, 503, 504})  # a gateway's answer while what is behind it restarts
# Coordination's 503s for an outage that passes, unlike its capacity deferral or a missing
# configuration, which waiting does not mend.
PASSING = frozenset({"verifier-unavailable", "authority-elapsed", "deadline", "upstream"})


class Unanswered(Exception):
    """The console or a door did not answer, or a gateway answered for it: worth asking again."""

    def __init__(self, reason: str, after: float | None = None, limited: bool = False):
        super().__init__(reason)
        self.after = after
        self.limited = limited  # it did answer: a rate limit asking for a pause (#6457)


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
        # Better Auth's limiter names its pause X-Retry-After.
        after = headers.get("Retry-After") or headers.get("X-Retry-After") if headers is not None else None
        return max(0.0, float(after)) if after is not None else None
    except (TypeError, ValueError):
        return None


def ask(method: str, url: str, body: dict | None = None, token: str | None = None,
        timeout: float = 60, key: str | None = None) -> tuple[int, dict]:
    """One request and its answer; `Unanswered` when nothing answered or a gateway answered. `key`
    is coordination's Idempotency-Key, which makes a retry the same request."""
    req = urllib.request.Request(url, method=method,
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Content-Type": "application/json", "Accept": "application/json"})
    if key:
        req.add_header("Idempotency-Key", key)
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        # A terminal session is the request's one credential: coordination refuses a machine's agent
        # credential beside it as ambiguous, which `door.send` adds for any door this machine holds.
        if token:
            req.add_header("User-Agent", door_mod.USER_AGENT)
        with (door_mod.open_direct(req, timeout) if token else door_mod.send(req, timeout)) as r:
            status, raw = r.status, r.read()
    except urllib.error.HTTPError as e:
        try:
            got = json.loads(e.read() or b"{}")
        except ValueError:
            got = {}
        # A gateway's 502-504 carries no reason of its own; the console's own refusal does.
        # Coordination's refusal names its check as `code`, a capacity deferral's 503 among them;
        # of those, only a passing outage is asked again.
        code = got.get("code") if isinstance(got, dict) else None
        if e.code == 429:
            raise Unanswered("429, too many requests", _retry_after(e.headers), limited=True) from None
        if e.code in TRANSIENT and (code in PASSING or not (isinstance(got, dict) and (got.get("error") or code))):
            raise Unanswered(str(code or e.code), _retry_after(e.headers)) from None
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
         timeout: float = 60, key: str | None = None) -> tuple[int, dict]:
    """A request outside any wait: one that goes unanswered stops the run."""
    try:
        return ask(method, url, body, token, timeout, key)
    except Unanswered as e:
        raise Stop(f"{_who(url)} at {_origin(url)} did not answer ({e}); "
                   "check the network, then run install again") from None


def waited(method: str, url: str, deadline: float, body: dict | None = None,
           token: str | None = None, key: str | None = None) -> tuple[int, dict]:
    """A request inside a wait: one that goes unanswered is asked again, with backoff, until
    `deadline`, and each retry is shown rather than ending the run."""
    delay = POLL
    while True:
        try:
            return ask(method, url, body, token, key=key)
        except Unanswered as e:
            pause = max(delay, e.after or 0)
            if time.monotonic() + pause > deadline:
                raise Stop(f"{_who(url)} at {_origin(url)} did not answer ({e}); "
                           "check the network, then run install again") from None
            say("", f"{_who(url)} {'asked for a pause' if e.limited else 'did not answer'} ({e}); retrying")
            time.sleep(pause)
            delay = min(delay * 2, 60.0)


def unopened() -> str | None:
    """Why this run opens no browser, or None. Over SSH a browser opened here is the far machine's,
    signed in as whoever uses it, and the console binds the code to the first account that opens it
    (#6327)."""
    return "this run is over SSH" if os.environ.get("SSH_CONNECTION") or os.environ.get("SSH_TTY") else None


def open_page(url: str) -> None:
    """The browser, where this machine has one to open and `BROWSER` names none other; the URL is
    printed either way."""
    if unopened():
        return
    try:
        if platform.system() == "Darwin" and not os.environ.get("BROWSER"):
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
# A workspace id or alias, as coordination routes one: one path segment of at most 64.
WORKSPACE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")


def ended(said: object, login: str | None) -> str:
    """The console's words for an ended sign-in. Its refusal for another account than the machine's
    cannot name the login (#3954), which this run carried, so it is named here."""
    said = str(said)
    return f"{said} This machine uses @{login}." if login and "than the one this machine uses" in said else said


def sign_in(console: str, scope: str, invite: str | None = None, login: str | None = None,
            named: bool = False) -> str:
    """A console session from a device grant, held in memory for this run only. `scope` is what
    the /device page shows the person this grant does."""
    status, got = call("POST", f"{console}/api/auth/device/code?handoff=1",
                       {"client_id": CLIENT, "scope": scope})
    if status != 200 or "device_code" not in got:
        raise Stop(f"the console would not start a sign-in ({status}): {got}")
    page = got.get("verification_uri_complete") or got["verification_uri"]
    code = re.sub(r"[^A-Za-z0-9]", "", got["user_code"]).upper()
    shown = f"{code[:4]}-{code[4:]}" if len(code) == 8 else got["user_code"]
    lifetime = float(got.get("expires_in") or 1800)
    interval, deadline = float(got.get("interval") or POLL), time.monotonic() + lifetime
    say("sign in", f"open {got['verification_uri']}")
    say("code", f"{shown} (expires in {elapsed(lifetime)})")
    if invite:
        # The approval page says which this invitation is, a team of their own or a seat in another's (doc 176).
        say("invite", "the approval page says whether it makes your own team or joins you to one")
    # An invitation decides the account; the machine's own login is only a guess at it (#4015).
    who = f"@{login}" if login and (named or not invite) else "the account your invitation is for" if invite else None
    carried = (f"&invite={invite.upper()}" if invite else "") + (f"&login={login}" if login else "") \
        + ("&named=1" if login and named else "")
    # A copied link must keep the same account constraint as the browser we open. Invitation
    # codes remain out of a local run's output; SSH's existing manual handoff below carries them.
    if not invite and page != got["verification_uri"]:
        say("link", f"{page}{'#' + carried[1:] if carried else ''}")
    say("", "only continue if you started this request in your terminal")
    if why := unopened():
        # Opened by hand, the page still carries the invitation and the login, never the proof.
        say("", f"(nothing opened: {why}; open {page}{'#' + carried[1:] if carried else ''} in your own browser"
                + (f", signed in to GitHub as {who}" if who else "") + "; waiting…)")
    else:
        # A person at a terminal gets time to copy the code. A tool driving install must never
        # wait for stdin; headless and SSH runs keep their existing handoff and polling.
        if sys.stdin.isatty():
            input("            Copy the code, then press Enter to open your browser… ")
        say("", f"(opened in your browser; sign in to GitHub as {who}; waiting…)" if who else "(opened in your browser; waiting…)")
    # Only the browser opened here holds the proof, so only it can end this grant when its sign-in
    # is refused (#3586). It is never printed: a printed proof is as public as the user code.
    # An invitation code rides the same fragment, for the sign-in door to carry to GitHub; like the
    # proof, a fragment reaches no server log. So does the login this machine signs in as (#3954),
    # which lets an invite waiting for that account admit it with no code. A login the person named
    # with --login is marked, so the approval page refuses another account outright (#4015).
    open_page(f"{page}#refusal={refusal_proof(got['device_code'])}{carried}")
    start = told = time.monotonic()
    while time.monotonic() < deadline:
        time.sleep(interval)
        if time.monotonic() >= deadline:
            break
        if time.monotonic() - told >= PROGRESS:
            told = time.monotonic()
            say("", f"waiting for the sign-in to be approved ({elapsed(told - start)})")
        status, answer = waited("POST", f"{console}/api/auth/device/token", deadline,
                                {"grant_type": DEVICE_GRANT, "device_code": got["device_code"], "client_id": CLIENT})
        if status == 200 and answer.get("access_token"):
            say("", f"the sign-in is approved ({elapsed(time.monotonic() - start)})")
            return answer["access_token"]
        error = answer.get("error")
        if error == "slow_down":
            interval += 5
        elif error != "authorization_pending":
            reason = {"access_denied": "the request was denied; the terminal will stop",
                      "expired_token": "the code expired; run this again for a fresh code"}.get(error)
            raise Stop(f"sign-in ended: {ended(answer.get('error_description') or reason or error or status, login)}")
    raise Stop("the sign-in code expired; run this again")


PROGRESS = 60.0  # how often a wait that needs a person says it is still waiting
def elapsed(seconds: float) -> str:
    return f"{int(seconds) // 60}m{int(seconds) % 60:02d}s"


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
    named = door_mod.split(door)[1]
    # A door by address names the workspace by its slugs, not the uuid the checkout is bound to:
    # the door's own authorities view, which names the uuid, is checked against the binding.
    if named != authority and (named is None or "/" not in named):
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


def go_access(console: str, token: str) -> dict:
    """What coordination says the terminal's person may select."""
    status, got = call("GET", f"{console}/api/v1/access", token=token)
    if status == 404:
        raise Stop(f"{console} serves no coordination routes, and install speaks only coordination; "
                   "check STEERING_PLATFORM names the platform, or ask its operator")
    if status != 200 or not isinstance(got.get("teams"), list):
        raise Stop(f"coordination refused this terminal's access ({status}): {got.get('code') or got}; "
                   "run install again to sign in afresh")
    return got


def checked_login(access: dict, carried: str | None, named: bool) -> str | None:
    """The GitHub login the console's access answer names the sign-in as, said in the terminal so a
    wrong browser profile is announced rather than silently landing the repository in that account's
    team (#6216). Where the login this run carried was the one to sign in as — not an invitation's
    account — a mismatch is refused here, before any team or workspace step, because the approval
    page's own guard never saw a page opened without its fragment. None when the answer names no
    login, which a release before this one does."""
    login = access.get("github_login")
    if not isinstance(login, str) or not LOGIN.fullmatch(login):
        return None
    say("signed in", f"@{login}")
    if named and isinstance(carried, str) and LOGIN.fullmatch(carried) and login.lower() != carried.lower():
        raise Stop(f"you signed in to the console as @{login}, and this machine's GitHub account is "
                   f"@{carried}; run install again and approve it as that account, or name the "
                   "account to install with --login")
    return login


def let_go(prior: tuple[str, str] | None, authority: str, door: str) -> bool:
    """The door this checkout was bound to before, once coordination's admitted it to another: its
    credential and registration on this machine go, and each that went is said. Whether it moved."""
    if prior is None or prior[1].rstrip("/") == door.rstrip("/"):
        return False
    left_id, left_door = prior
    gone = []
    with contextlib.suppress(FileNotFoundError):
        credential.path_for(left_door).unlink()
        gone.append("credential")
    credential.path_for(left_door).with_suffix(".lock").unlink(missing_ok=True)
    if left_id != authority:
        with contextlib.suppress(FileNotFoundError):
            (machine_workspaces.directory(Path.home()) / f"{left_id}.json").unlink()
            gone.append("registration")
            machine_workspaces.poke()
    say("machine", f"{left_door}: {' and '.join(gone)} removed from this machine" if gone
        else f"{left_door}: nothing of it was left on this machine")
    return True


def admit_on_go(console: str, token: str, access: dict, repo: str, team: str | None) -> tuple[str, str, str, bool]:
    """This machine admitted to the workspace serving `repo`, on the person's terminal session, by
    what coordination says the repository is (go-dc2-onboarding-design.md §5): a team of theirs
    serves it; they own a team, which installs it; or they hold an operator's team of their own,
    which they create with it. Its door, workspace uuid, team id, and whether the person owns that team.

    The door is the workspace's address, whatever door the checkout recorded before
    (go-alias-removal-design.md D2): the alias door a checkout held is replaced, not kept."""
    # A team without an id is passed over, as a malformed workspace is; one without a name is named by its id.
    teams = [{**t, "name": t.get("name") or t["id"]} for t in access["teams"]
             if isinstance(t, dict) and isinstance(t.get("id"), str) and (team is None or t["id"] == team)]
    # An operator's team of one's own does not exist yet; --team names it by the id it will take.
    intents = [i for i in access.get("invitations") or [] if isinstance(i, dict) and i.get("kind") == "team"
               and (team is None or i.get("team_id") == team)]
    if team is not None and not teams and not intents:
        raise Stop(f"you are not a member of team {team}; name one of yours with --team, or leave it out")
    serving = [(t, w) for t in teams for w in t.get("workspaces") or []
               # GitHub reads a repository's name in any case, as coordination compares it.
               if isinstance(w, dict) and isinstance(w.get("repo"), str) and w["repo"].lower() == repo.lower()
               and isinstance(w.get("id"), str)]
    if len(serving) > 1:
        raise Stop(f"several of your teams serve {repo}; name one with --team: "
                   + ", ".join(f"{t['name']} ({t['id']})" for t, _ in serving))
    owned = [t for t in teams if t.get("owner")]
    created = False
    if serving:
        t, w = serving[0]
        say("team", f"{t['name']}{'' if t.get('owner') else ' (you joined it; installing a new repository is its owner’s)'}")
        say("workspace", f"{w['id']} found")
    elif owned and intents:
        raise Stop(f"none of your teams serves {repo}, and you could install it in a team you own or in the "
                   "team of your own an operator gave you; name one with --team: "
                   + ", ".join([f"{t['name']} ({t['id']})" for t in owned]
                               + [f"a new team of your own ({i.get('team_id')})" for i in intents]))
    elif owned or teams and not intents:
        if not owned:
            raise Stop(f"none of your teams serves {repo}, and only a team's owner installs a new repository; "
                       f"ask the owner of {', '.join(t['name'] for t in teams)} to install it")
        if len(owned) > 1:
            raise Stop(f"none of your teams serves {repo}, and you own several; name the one to install it in "
                       "with --team: " + ", ".join(f"{t['name']} ({t['id']})" for t in owned))
        t = owned[0]
        say("team", f"{t['name']} (yours)")
        w = create_workspace(console, token, t["id"], repo)
    else:
        # No team of theirs can take it; an operator's team of their own can, whatever they joined.
        if not intents:
            if access.get("invitations"):
                raise Stop("you are in no team yet; redeem your invitation from the desk, then run install again")
            raise Stop("you are in no team yet, and no team of your own was given to your GitHub account; "
                       "ask the platform's operator for one, or a team's owner for an invitation")
        t, w = create_team(console, token, intents[0], repo)
        created = True
    workspace_id, address = w.get("workspace_id"), w.get("address")
    if not door_rewrite.is_uuid(workspace_id):
        raise Stop(f"coordination named {w['id']} with no workspace_id ({workspace_id!r}), which this "
                   "plugin binds a checkout to; it runs a release before the alias window, so ask its operator")
    if not isinstance(address, str) or door_mod.split(f"{console}/{address}")[1] != address:
        raise Stop(f"coordination named {w['id']} with no address ({address!r}) to reach it at; ask its operator")
    door = f"{console}/{address}"
    try:
        approval = credential.approve(console, door, token, key=str(uuid.uuid4()))
        # The credential names the alias while the window is open, so a rollback renews (D8).
        credential.enrol_native(door, console, w["id"], approval["secret"])
    except credential.NoCredential as e:
        raise Stop(f"coordination would not admit this machine: {e}") from None
    # The browser follows this sign-in's actual admission outcome. A new machine in an existing
    # one-workspace team must not be asked to name it again. Older consoles have no such route.
    status, outcome = waited("POST", f"{console}/api/install/admitted", time.monotonic() + 60,
                            {"repo": repo, "team": t["id"], "created": created}, token)
    if status not in (200, 404):
        raise Stop(f"the console could not carry on this install in your browser ({status}): "
                   f"{outcome.get('error') or status}; run install again to resume")
    if status == 200 and outcome.get("recorded") is not True:
        raise Stop("the console did not record this install's outcome, so your browser cannot carry on; "
                   "run install again to resume")
    return door, workspace_id, t["id"], t.get("owner") is True


def go_created(console: str, path: str, body: dict, token: str, what: str) -> dict:
    """A creation on the person's terminal session under one key, so a lost answer's retry is the
    same creation; a capacity deferral is waited through, as progress."""
    key, start = str(uuid.uuid4()), time.monotonic()
    deadline = start + GRANT
    while True:
        status, got = waited("POST", f"{console}{path}", deadline, body, token, key)
        if status == 200:
            return got
        code = got.get("code") or got.get("error") or status
        if status == 503 and str(code).endswith("-cap-reached") and time.monotonic() < deadline:
            say("", f"the platform is at its {code.removesuffix('-reached')}; waiting to {what} ({elapsed(time.monotonic() - start)})")
            time.sleep(POLL * 6)
            continue
        raise Stop(f"coordination would not {what} ({status}): {code}")


def create_workspace(console: str, token: str, team: str, repo: str) -> dict:
    """`repo` installed in the owner's team (§5, case 2): its new workspace."""
    # No alias: coordination mints the workspace's, and answers its address, the door's.
    got = go_created(console, f"/api/v1/teams/{team}/workspaces", {"repository": repo}, token, f"install {repo}")
    if not isinstance(got.get("id"), str):
        raise Stop(f"coordination installed {repo} and named no workspace id: {got}")
    say("workspace", f"{got['id']} created" + (", reading the team's GitHub installation" if got.get("source") else ""))
    return got


def create_team(console: str, token: str, intent: dict, repo: str) -> tuple[dict, dict]:
    """The team an operator gave this person, created with `repo` as its first workspace (§5,
    case 5): the team, and the workspace."""
    owner = repo.split("/")[0]
    got = go_created(console, "/api/v1/teams", {"authorization": intent.get("id"), "team_name": owner,
                                                "workspace_name": repo}, token, "create your team")
    made = got.get("workspace")
    if not isinstance(got.get("id"), str) or not isinstance(made, dict) or not isinstance(made.get("id"), str):
        raise Stop(f"coordination created your team and named no team or workspace id: {got}")
    say("team", f"{owner} created (yours)")
    say("workspace", f"{got['workspace']['id']} created")
    return {"id": got["id"], "name": owner, "owner": True}, got["workspace"]


# What the person does about a Connect GitHub claim coordination refused, by the refusal's check.
SIGN_IN_AGAIN = "your console sign-in ended or could not be verified again: sign in on {console}/ again, then choose Connect GitHub again"
CLAIM_REFUSED = {
    "installation-not-visible": "GitHub shows your account no 2mw2lt App installation on {owner}: install the App "
                                "there with {repo} among its repositories, then choose Connect GitHub again",
    "repository-not-in-installation": "the App is installed on {owner} without {repo}: add {repo} to the "
                                      "installation's repositories on GitHub, then choose Connect GitHub again",
    "not-admin": "your GitHub account is not an admin of {owner}: an admin of {owner} connects it, or makes you one",
    "github-user-mismatch": "GitHub answered for another account than the one you signed in to 2mw2lt with: "
                            "sign in to GitHub as that account, then choose Connect GitHub again",
    "github-unlinked": "your console account is not linked to GitHub: sign in on {console}/ with GitHub, "
                       "then choose Connect GitHub again",
    "exchange-failed": "GitHub's sign-in code was spent or had expired: choose Connect GitHub again",
    "owner-required": "it was asked by a member, and only the team's owner connects GitHub",
    # The webhook's own claim when Connect GitHub's return never arrived (#7912).
    "membership-unreadable": "GitHub would not say whether you administer {owner}: choose Connect GitHub again, and if "
                             "it repeats tell the operator",
    "installation-revoked": "the App was uninstalled from {owner}, or released, after you installed it: install it "
                            "again, then choose Connect GitHub again",
    "installation-held-elsewhere": "another team already holds the App's installation on {owner}: its owner releases it "
                                   "in its console first",
    "owner-replaced": "the team's owner changed since you installed the App: its owner now chooses Connect GitHub",
    "owner-unverified": "your console account no longer verifies as the GitHub account that installed the App: sign "
                        "in on {console}/ with that account, then choose Connect GitHub again",
    "reconcile-timeout": "GitHub did not answer in time to finish the connection: choose Connect GitHub again",
    "member-removed": "the account that asked is no longer a member of the team: the team's owner connects GitHub",
    "principal-membership-removed": "the account that asked is no longer a member of the team: the team's owner connects GitHub",
    **{check: SIGN_IN_AGAIN for check in ("not-authenticated", "principal-expired", "identity-stale", "binding-expired",
                                          "binding-revoked", "session-refused", "session-credential-superseded",
                                          "session-epoch-stale")},
}


def claim_refused(refused: dict, repo: str, console: str) -> str:
    """A refused Connect GitHub claim, in the words the person waiting on it acts on."""
    check, request = refused.get("check") or "unknown", refused.get("request") or "none"
    remedy = CLAIM_REFUSED.get(check)
    if remedy:
        return f"Connect GitHub was refused ({check}): " + remedy.format(owner=repo.split("/")[0], repo=repo, console=console)
    return (f"Connect GitHub was refused ({check}), a refusal this install does not recognise: choose Connect GitHub "
            f"again, and if it repeats tell the operator, quoting reference {request}")


def reconciling(got: dict, repo: str, console: str) -> str | None:
    """Where GitHub's notice of the installation stands while it keeps the repository unreadable: a
    failure retried each minute, or the check it settled on without the repository's source."""
    r = got.get("reconciling")
    if not isinstance(r, dict):
        return None
    check = r.get("check") or "unknown"
    if r.get("settled"):
        if check in CLAIM_REFUSED:
            return claim_refused({"check": check, "request": r.get("at")}, repo, console)
        return f"the App is connected, but {repo} was not set up to be read ({check}): choose Connect GitHub again on {console}/"
    return f"GitHub's notice of the installation is being retried each minute ({check})"


def wait_for_go_grant(console: str, token: str, team: str, repo: str, owner: bool) -> None:
    """The grant waited for on the person's terminal session, as a first admission does."""
    url = f"{console}/api/v1/teams/{team}/installations?{urllib.parse.urlencode({'repository': repo})}"

    def read(deadline: float) -> dict:
        status, got = waited("GET", url, deadline, token=token)
        if status != 200:
            raise Stop(f"coordination could not say whether the App reaches {repo} ({status}): "
                       f"{got.get('code') or got}; run install again, or ask the operator if it repeats")
        return got
    wait_for_installation(read, console, repo, owner)


def wait_for_door_grant(door: str, console: str, repo: str) -> None:
    """The grant waited for on the machine's credential, as the door's authorities view answers it,
    so a run that resumes on an admitted checkout signs nobody in (#7928)."""
    def read(deadline: float) -> dict:
        status, got = waited("GET", f"{door}/steering/authorities", deadline)
        installation = got.get("installation")
        if status != 200 or not isinstance(installation, dict):
            raise Stop(f"the workspace's door could not say whether the App reaches {repo} ({status}): "
                       f"{got.get('code') or got.get('error') or got}; run install again, or ask the operator if it repeats")
        return installation
    wait_for_installation(read, console, repo, None)


def wait_for_installation(read, console: str, repo: str, owner: bool | None) -> None:
    """Until the team holds the App's installation on `repo`, which is when its workspace's source
    is read: the owner connects GitHub from the desk; anyone else asks them to. A claim coordination
    refuses is said once, with what to do about it, and the wait goes on: a retry may hold. `owner`
    is None where the run cannot tell whether the person is the team's owner."""
    deadline = time.monotonic() + GRANT
    seen: set = set()
    last: list = []

    def held() -> bool:
        got = read(deadline)
        refused = got.get("refused")
        if isinstance(refused, dict) and (refused.get("request"), refused.get("at")) not in seen:
            seen.add((refused.get("request"), refused.get("at")))
            last[:] = [refused]
            say("github", claim_refused(refused, repo, console) + " (still waiting…)")
        # A held installation whose notice is still retrying, or settled unsourced, is not readable yet.
        if (pending := reconciling(got, repo, console)) is not None:
            if pending not in seen:
                seen.add(pending)
                say("github", pending + " (still waiting…)")
            return False
        return got.get("held") is True

    if held():
        say("github", f"the 2mw2lt App reaches {repo}")
        return
    say("github", f"the 2mw2lt App cannot read {repo} yet")
    if owner:
        say("", f"open {console}/ and choose Connect GitHub in the account menu "
                + (f"(nothing opened: {why}; waiting…)" if (why := unopened()) else "(opened in your browser; waiting…)"))
        open_page(f"{console}/")
    elif owner is None:
        say("", f"the team's owner, signed in with GitHub, opens {console}/ and chooses Connect GitHub in the "
                "account menu; anyone else asks them to (waiting…)")
    else:
        say("", "only the team's owner, signed in with GitHub, connects it: ask them to (waiting…)")
    start = told = time.monotonic()
    while not held():
        if time.monotonic() - told >= PROGRESS:
            told = time.monotonic()
            say("", f"waiting for the App to reach {repo} ({elapsed(told - start)})")
        if time.monotonic() >= deadline:
            if last:
                raise Stop(f"the App does not reach {repo} yet. {claim_refused(last[0], repo, console)}; then run this again")
            raise Stop(f"the App does not reach {repo} yet: the team's owner, signed in with GitHub, chooses "
                       f"Connect GitHub on {console}/; then run this again")
        time.sleep(POLL)
    say("", f"{repo} is readable after ({elapsed(time.monotonic() - start)})")


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
    if status == 404:
        # A door that serves no authorities view: the machine is admitted again.
        say("machine", "the workspace's door has no authorities view; admitting this machine again")
        return None
    if status != 200:
        raise Stop(f"the workspace's door answered {status} for its authorities: {got.get('error') or got}; "
                   "run install again, or ask the operator if it repeats")
    return got


def serves(view: dict) -> str | None:
    """The repository the door's workspace serves, as its authorities view names it."""
    rows = view.get("authorities")
    return rows[0].get("repo") if isinstance(rows, list) and len(rows) == 1 and isinstance(rows[0], dict) else None


def opened(view: dict) -> str | None:
    """The uuid of the workspace the door opens, as its authorities view names it."""
    rows = view.get("authorities")
    got = rows[0].get("workspace_id") if isinstance(rows, list) and len(rows) == 1 and isinstance(rows[0], dict) else None
    return got if door_rewrite.is_uuid(got) else None


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
                   f"its log is under {log_directory()}") from None
    except (TimeoutError, ConnectionError, http.client.HTTPException) as e:
        raise Stop(f"the agent on 127.0.0.1:{port} did not answer ({_reason(e)}); "
                   f"its log is under {log_directory()}") from None
    except (ValueError, AttributeError):
        served = None
    if not isinstance(served, list):
        raise Stop(f"127.0.0.1:{port} answers, but not as this machine's 2mw2lt agent; "
                   "stop what holds the port, then run install again")
    return any(Path(w).resolve() == root.resolve() for w in served)


def machine_port() -> int:
    """The port the machine's one agent job listens on, or the default when it has none yet."""
    doc = agentjob.load(agentjob.job_path(agentjob.jobs_directory(Path.home()), agentjob.MACHINE_LABEL))
    if doc is None:
        return int(DEFAULT_AGENT_PORT)
    named = str((doc.get("EnvironmentVariables") or {}).get("STEERING_AGENT_PORT") or "") if isinstance(doc, dict) else ""
    return int(named) if named.isdecimal() else int(DEFAULT_AGENT_PORT)


def go_installer(release: Path) -> Path | None:
    """The release's own `2mw2lt` when it has `install` (#6919), which installs with no Python
    environment. It is probed as the Go updater probes a subcommand: run bare, one that has it
    refuses with its own usage line and exit 2, touching nothing."""
    binary = agentjob.go_agent(release)
    if not binary.is_file() or not os.access(binary, os.X_OK):
        return None
    try:
        done = subprocess.run([str(binary), "install"], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return binary if done.returncode == 2 and done.stderr.strip().startswith("usage: 2mw2lt install ") else None


def _ready(release: Path, sha: str | None = None) -> bool:
    """Whether `release` is the sha the door stated and can be installed as it is: its own Go
    installer, which is what `run_installer` requires. A built Python environment also passes,
    a layout older activations wrote; `run_installer` refuses one, so `release` re-fetches."""
    try:
        revision = (release / "REVISION").read_text().strip()
    except OSError:
        return False
    if sha is not None and revision != sha:
        return False
    return go_installer(release) is not None or (release / "steering" / ".venv" / "bin" / "python").exists()


def release(door: str) -> Path:
    """The agent release the workspace's door states (doc 147 §3–4), fetched with this machine's
    credential. A fetched part without its own Go installer is refused before it is placed —
    there is nothing else to install it with. It never re-points the machine's other agents:
    the one job written below is the only one touched. A release the machine already holds is
    kept only while it is the one the door states, or the door states none: one left by an
    earlier install would otherwise run its own, older installer."""
    base = agentjob.root(Path.home())
    try:
        active = (base / "current").resolve(strict=True)
    except OSError:
        active = None
    if active is not None and not (active.parent == (base / "releases").resolve() and _ready(active)):
        active = None
    status, target = call("GET", f"{door}/steering/agent/target")
    if status == 404:
        if active is not None:
            return active
        raise Stop("the workspace's door states no agent release; ask its operator to deploy one")
    if status != 200 or not selfupdate.valid(target):
        raise Stop(f"the workspace's door answered {status} for its agent release; run install again, "
                   "or ask the operator if it repeats")
    if active is not None and _ready(active, target["sha"]):
        return active
    found = base / "releases" / target["sha"]
    if not _ready(found, target["sha"]):
        say("agent", f"fetching agent release {target['sha'][:12]} from the workspace's door…")
        try:
            part = asyncio.run(selfupdate.fetch(_stream(door), door, target, base))
            require_installer(part)
            found = place(part, found)
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


def place(part: Path, release: Path) -> Path:
    """A fetched release that carries its own Go installer, renamed into place with nothing built."""
    if release.exists():  # a hand deploy staging it now: its build is not this one's to remove
        shutil.rmtree(part, ignore_errors=True)
        raise selfupdate.BuildFailed(f"{release} is being staged by another activation")
    part.rename(release)
    return release


def require_installer(installed: Path) -> Path:
    """The release's own `2mw2lt install`, or a Stop naming the probe that found none: a release
    without one has no installer, since releases carry no steering/ to fall back to (#6922)."""
    go = go_installer(installed)
    if go is None:
        raise Stop(f"the agent release at {installed} is not installable: its `2mw2lt` does not answer "
                   "`install` with its own usage line, so the release has no Go installer (#6919); "
                   "ask the workspace's operator to deploy a current release")
    return go


def run_installer(installed: Path, root: Path, door: str, port: int) -> None:
    """The release's own installer — its `2mw2lt install` — so the job is written by the closure
    it will run rather than by this plugin's copy."""
    go = require_installer(installed)
    try:
        # The machine job's own port, so an install leaves the job as it is when it already runs.
        done = subprocess.run([str(go), "install", str(root), door],
                              env={**os.environ, "STEERING_AGENT_PORT": str(port)},
                              capture_output=True, text=True, timeout=120)
    except subprocess.TimeoutExpired:
        raise Stop(f"the agent release's installer did not answer within 120s; its log is under "
                   f"{log_directory()}") from None
    if done.returncode:
        raise Stop(f"the agent release's installer failed: {(done.stderr or done.stdout).strip()[-400:]}")


def ensure_agent(root: Path, authority: str, door: str) -> int:
    """The port of the machine's one agent, serving this workspace (doc 130). The workspace must
    already name `door` in its `.env`: the agent serves a registered workspace only then."""
    port = machine_port()
    if agent_answers(port, root):
        write_agent_port(root, str(port))
        return port
    # The job is written by the installer of the release it runs, not by this plugin's copy.
    run_installer(release(door), root, door, port)
    write_agent_port(root, str(port))
    # The release's installer runs silent, so the wait names the step a run is in while it lasts.
    say("agent", f"waiting for the agent to answer on 127.0.0.1:{port}")
    end = time.monotonic() + 60
    while True:
        try:
            if agent_answers(port, root):
                return port
            why = None
        except Stop as e:  # an agent starting up may drop a connection before it serves
            why = e
        if time.monotonic() > end:
            raise why or Stop(f"the agent did not answer on 127.0.0.1:{port}; its log is under {log_directory()}")
        time.sleep(1)


def log_directory() -> Path:
    return agentjob.log_directory(Path.home())


def record(root: Path, door: str, authority: str) -> None:
    """`STEERING_DOOR` and the workspace's id, written once the machine is admitted, so a run
    stopped after that resumes there rather than signing in and admitting it again. The
    `STEERING_AUTHORITY` line an earlier install wrote is dropped: no client reads it."""
    env = root / ".env"
    lines = env.read_text().splitlines() if env.exists() else []
    lines = [line for line in lines if not line.startswith(("STEERING_DOOR=", "STEERING_AUTHORITY="))]
    lines.append(f"STEERING_DOOR={door}")
    spool.write_atomic(env, "\n".join(lines) + "\n", mode=0o600)
    checkout_binding.bind(root, authority)
    os.environ["STEERING_DOOR"] = door


# What install writes into a checkout, relative to its root.
LOCAL_FILES = (".env", ".claude/settings.local.json", ".claude/.settings.local.json.lock", ".claude/steering-launch.py",
               ".claude/steering-workspace", ".claude/steering-tokens")


def exclude_local(root: Path) -> None:
    """Install's files are this machine's, so a `git add -A` in a team's repository must not
    commit them: each goes in the clone's own exclude file, which is never pushed."""
    try:
        common = subprocess.run(["git", "-C", str(root), "rev-parse", "--path-format=absolute", "--git-common-dir"],
                                capture_output=True, text=True, timeout=10, check=True).stdout.strip()
        exclude = Path(common) / "info" / "exclude"
        held = exclude.read_text() if exclude.exists() else ""
        missing = [f"/{f}" for f in LOCAL_FILES if f"/{f}" not in held.splitlines()]
        if missing:
            exclude.parent.mkdir(exist_ok=True)
            spool.write_atomic(exclude, held + ("" if held.endswith("\n") or not held else "\n") + "\n".join(missing) + "\n",
                               mode=0o644)
    except (OSError, subprocess.SubprocessError) as e:
        raise Stop(f"could not keep install's files out of {root}'s commits: {e}") from None


def wire(root: Path, door: str, authority: str, codex: bool = False) -> None:
    """The door and id `record` writes, and Claude's hook pack; nothing else local. Codex runs
    no hooks: its sessions share only the enrolment tokens' directory."""
    import hooks
    exclude_local(root)
    record(root, door, authority)
    if codex:
        hooks.tokens_directory(root)
        # The forge verb's one rule names the launcher, which a Codex install has no hooks to write.
        hooks.install_launcher(root)
    else:
        hooks.hooks_install(root, local=False, workspace_id=authority)


def github_account(root: Path, repo: str, chosen: str | None = None) -> str:
    try:
        names, refused = ghauth.readable_accounts(repo)
    except ghauth.NoIdentity as e:
        raise Stop(str(e)) from None
    if not names and refused:
        said = "; ".join(f"{name} ({why})" for name, why in refused.items())
        raise Stop(f"GitHub refused {repo} to {said}, and no other signed-in login reads it; grant that access, "
                   f"or run `gh auth login` with an account that can, then install again")
    if not names:
        raise Stop(f"no gh account can read {repo}; run `gh auth login` with an account that can, then install again")
    if chosen is not None:
        if chosen not in names:
            why = f" (GitHub refused it: {refused[chosen]})" if chosen in refused else ""
            raise Stop(f"{chosen} cannot read {repo}{why}; name one of {', '.join(names)} with --gh-account")
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
    # A direct call, as the guards make, starts its transcript afresh — not off the import-time clock.
    global _at, _at_line
    _at, _at_line = "", time.monotonic()
    console = platform_url()
    repo = repository(root)
    say("repository", repo)
    name = github_account(root, repo, gh_account)
    label = f"{socket.gethostname().split('.')[0]} as {getpass.getuser()}"
    bound = checkout_binding.id_at(root)
    door = recorded_door(root, bound) if bound else None
    if door:
        # A workspace held at an alias door moves to its address door on the credential it holds,
        # with no sign-in, while the window's serve still answers the alias (D5).
        door_rewrite.rewrite(machine_workspaces.directory(Path.home()), lambda line: say("door", line), only=root)
        bound = checkout_binding.id_at(root)
        door = recorded_door(root, bound) if bound else None
    # A door the rewrite could not move is admitted again at the address, as a new checkout is.
    aliased = door is not None and door_rewrite.alias_door(door) is not None
    view = authorities(door) if door and not aliased else None
    if view is not None and view.get("coordination") is True and bound not in (opened(view), view.get("primary")):
        # A door by address is checked here, by the uuid its authorities view names (checked_door).
        raise Stop(f"{root} is bound to {bound}, and its door {door} opens {opened(view) or view.get('primary')}; "
                   f"run `install.py uninstall` here, then install again")
    if view is not None and ((other := serves(view)) or "").lower() != repo.lower():
        # Before any write: a checkout whose origin moved would otherwise keep reporting to the
        # workspace of the repository it came from (#2618). GitHub reads a name in any case.
        raise Stop(f"{root} is bound to {bound}, which serves {other or 'no repository it names'}, and its "
                   f"origin is {repo}; run `install.py uninstall` here, then install again")
    # A coordination door answering on this machine's credential has admitted it: the run goes on
    # to the hooks and the agent, as after a first admission below. A checkout the incumbent's door
    # serves moves to coordination: admitted there as a first install is, and the incumbent's door
    # let go of on this machine.
    moved = view is not None and view.get("coordination") is not True
    # The door this checkout was bound to before this run, which a move lets go of once Go holds it.
    prior = (bound, door) if bound and door else None
    left = False
    if moved:
        say("platform", f"moving {bound} from {door} to coordination")
        view = None
    if aliased:
        say("machine", f"{door} is the workspace's alias door, which the alias cut retires; admitting this "
                       "machine at its address")
    if view is None:
        # The workspace's GitHub login is the account the person most likely signs in as (#3954). With no
        # invitation to decide it, the account that signs in decides the team the workspace lands in, so
        # the approval page refuses any other: a team of another account's is chosen with --login.
        named = login is not None or invite is None
        token = sign_in(console, f"install {repo} on {label}", invite, login or name, named=named)
        try:
            access = go_access(console, token)
            checked_login(access, login or name, named)
            door, authority, team_id, owner = admit_on_go(console, token, access, repo, team)
            record(root, door, authority)
            say("machine", f"this machine is admitted to {authority}")
            wait_for_go_grant(console, token, team_id, repo, owner)
            left = let_go(prior, authority, door)
        finally:
            end_session(console, token)
    else:
        # The recorded door is this workspace's address (checked_door); the checkout is bound to its
        # uuid, which the registry and the hooks key by.
        authority = opened(view)
        if authority is None:
            raise Stop(f"the workspace's door {door} names no workspace_id in its authorities; it runs a "
                       "release before the alias window, so ask its operator")
        # A run that stopped at the grant recorded the door first, so the grant is checked again here.
        if isinstance(view.get("installation"), dict):
            wait_for_door_grant(door, console, repo)
        else:
            say("github", f"the workspace's door does not say whether the App reaches {repo}: it runs a release "
                          "before this plugin's, so this run cannot check the grant")
    wire(root, door, authority, codex)
    say("hooks", "none for Codex" if codex else "written")
    if codex:
        import codex_rules
        try:
            say("forge", codex_rules.write(root).removeprefix("forge grant: "))
        except OSError as e:
            say("forge", f"grant not written, so Codex asks for each pull request write: {e}")
    held = machine_workspaces.at(root)
    machine_workspaces.replace(machine_workspaces.directory(Path.home()), authority, machine_workspaces.Entry(
        root.resolve(), door, held.capability_file if held else None, held.port if held else None, name))
    machine_workspaces.poke()
    port = ensure_agent(root, authority, door)
    say("agent", f"serving {authority} on 127.0.0.1:{port}")
    if left:
        say("sessions", "each session on this checkout runs /2mw2lt:connect again: its enrolment was the old door's")
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


NO_TRACKS = "the workspace keeps no lanes; it binds no tracks document to sync"


def tracks_now(door: str) -> tuple[int | None, dict]:
    """Go's tracks read (`protocol.TracksRead`) as this machine asks for it, with the status; None
    while it does not answer."""
    try:
        return ask("GET", f"{door}/api/v1/tracks", timeout=30)
    except Unanswered:
        return None, {}


UNREAD = object()  # the lanes before the merge could not be read, so no later sync is known new


def wait_for_lanes(door: str, before, commit: str | None, deadline: float) -> dict:
    """Until Go holds lanes synced at the merge or after it, or refuses the merged document.
    The mirror and the tracks sync move on GitHub's push; nothing here asks them to."""
    while True:
        status, t = tracks_now(door)
        if status is not None and 400 <= status < 500 and status != 429:
            # Waiting does not change what this machine's credential may read.
            why = f"{status} {t.get('code') or t.get('title') or 'refused'}"
            say("lanes", f"the lanes are merged, and the door refused to show this machine the board's lanes ({why})")
            return {"synced": False, "reason": f"the board's lanes could not be read: {why}"}
        if status == 200 and t.get("unbound"):
            say("lanes", NO_TRACKS)
            return {"synced": False, "reason": NO_TRACKS}
        failed = t.get("failed") or {}
        if commit and failed.get("commit") == commit:
            say("lanes", f"the merged document was refused: {failed.get('reason')}")
            return {"synced": False, "reason": f"the merged document was refused: {failed.get('reason')}"}
        synced = (t.get("synced") or {}).get("commit")
        if t.get("lanes") and synced and (synced == commit or before is not UNREAD and synced != before):
            say("lanes", f"the board holds {len(t['lanes'])} lanes, synced at {synced[:12]}")
            return {"synced": True}
        if time.monotonic() + POLL > deadline:
            say("lanes", "the lanes are merged; the board shows them once the platform syncs the repository")
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
    if status == 200 and t.get("unbound"):
        raise Stop(NO_TRACKS)
    before = (t.get("synced") or {}).get("commit") if status == 200 else UNREAD
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
        # By the bound id, which retire_on routes through the person's access: a door's path is how
        # the console was reached, not always an address coordination knows (#7657).
        retire_on(console, bound)
    elif retire:
        say("workspace", "not retired: this checkout is bound to no workspace; "
                         "name the workspace with `install.py retire <id>`")
    hooks.uninstall(root)
    say("hooks", "removed")
    env = root / ".env"
    if env.exists():
        kept = [line for line in env.read_text().splitlines()
                if not line.startswith(("STEERING_DOOR=", "STEERING_AUTHORITY=", "STEERING_AGENT_PORT="))]
        spool.write_atomic(env, "\n".join(kept) + ("\n" if kept else ""), mode=0o600)
    for leftover in (root / ".claude" / "steering-workspace", root / ".claude" / "steering-launch.py"):
        leftover.unlink(missing_ok=True)
    if bound:
        # The machine's agent stops serving it on its next read of the registry; the job serves
        # the machine's other workspaces and stays.
        (machine_workspaces.directory(Path.home()) / f"{bound}.json").unlink(missing_ok=True)
        machine_workspaces.poke()
        if door:
            credential.path_for(door).unlink(missing_ok=True)
        say("agent", f"{bound} deregistered from this machine's agent")
    return 0


def retire_on(console: str, wid: str) -> None:
    """Retire `wid` on coordination, its state kept."""
    # Its own scope: an install's carries the approval page on to GitHub for the repository (#4015).
    token = sign_in(console, f"retire {wid}")
    try:
        # A workspace's uuid, as install prints it, is no route: its address, which the person's access names, is.
        route = wid
        if door_rewrite.is_uuid(wid):
            route = next((w["address"] for t in go_access(console, token)["teams"] if isinstance(t, dict)
                          for w in t.get("workspaces") or [] if isinstance(w, dict) and w.get("workspace_id") == wid
                          and isinstance(w.get("address"), str)), None)
            if route is None:
                raise Stop(f"none of the teams you signed in to holds workspace {wid}; sign in as its team's owner")
        status, got = call("POST", f"{console}{door_mod.routed(route)}/api/v1/workspace/retirement", {}, token,
                           key=str(uuid.uuid4()))
        if status != 200:
            raise Stop(f"coordination would not retire {wid} ({status}): {got.get('code') or got}")
        say("workspace", f"{wid} retired; its state is kept on the platform")
    finally:
        end_session(console, token)


def retire_workspace(wid: str) -> int:
    """Retire a workspace by its id, the way to reach one no checkout here is bound to (#3742).
    What an install added on a machine is still that machine's `uninstall` to remove."""
    retire_on(platform_url(), wid)
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
    global _at, _at_line
    _at = ""
    _at_line = time.monotonic()
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
