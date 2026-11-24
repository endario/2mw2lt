#!/usr/bin/env python3
"""`/2mw2lt:install`'s engine (doc 129): a repository becomes a working, observed workspace.

    install.py [workspace] [invite-code] [--codex] [--team <id>] [--gh-account <login>] [--login <login>]
                                        install, or resume one half done; --codex writes no hooks;
                                        --team names the team when the person may install in
                                        several, a new team of their own among them;
                                        --gh-account names the workspace's GitHub login when
                                        several signed-in logins can read the repository;
                                        --login names the GitHub account to sign in as, when it
                                        is not the workspace's; --move-root moves this machine's
                                        served root for the repository to this checkout even with
                                        sessions live in the one it serves now, which the move
                                        strands
    install.py --machine-only <owner>/<repo> [invite-code] [--team <id>] [--login <login>]
                                        install this login as a lent machine's service user
                                        (doc 209): no GitHub login and no checkout are read; an
                                        empty served root under this home names the repository,
                                        and every launch runs sandboxed
    install.py lanes [workspace] [--merge] [--gh-account <login>]
                                        the open lanes pull request and how it would merge;
                                        --merge merges it as the workspace's login, then waits
                                        for the daemon to sync the merged lanes
    install.py uninstall [workspace]    remove what an install added here: leaving a workspace's
                                        machine is this alone, and the workspace keeps serving
                                        its other machines
    install.py uninstall --machine      and take this machine's agent down with it: its jobs, its
                                        release root and tools, its keys, logs and proxies — the
                                        one act for a machine whose last workspace this is
    install.py create <team>/<workspace>
                                        create the workspace at that address on the platform,
                                        joining no machine — `install` in its repository joins one
    install.py delete <team>/<workspace>
                                        have the platform retire the workspace at that address, for
                                        every machine, its state kept (#3742)

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
import pwd
import re
import shutil
import signal
import socket
import subprocess
import tarfile
import threading
import time
import tomllib
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
import contract  # noqa: E402
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
_service = False  # this run installs a lent machine's service user, which has no browser (doc 209)
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
    (#6327). A service user's run opens none either: its approver signs in from their own desk."""
    if _service:
        return "this login is a service user, with no browser of its own"
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
    revoke_held(credential.path_for(left_door))
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
    """The machine job's port, or a free listener port for its first install."""
    doc = agentjob.load(agentjob.job_path(agentjob.jobs_directory(Path.home()), agentjob.MACHINE_LABEL))
    if doc is None:
        # Keep the listener below the normal ephemeral range: outbound connections can otherwise
        # briefly claim its port while activation has the agent stopped.
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.bind(("127.0.0.1", 0))
            first = 20000 + probe.getsockname()[1] % 10000
        for offset in range(10000):
            candidate = 20000 + (first - 20000 + offset) % 10000
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
                try:
                    listener.bind(("127.0.0.1", candidate))
                except OSError:
                    continue
                return candidate
        raise Stop("no free loopback port below the ephemeral range for the machine agent")
    named = str((doc.get("EnvironmentVariables") or {}).get("STEERING_AGENT_PORT") or "") if isinstance(doc, dict) else ""
    return int(named) if named.isdecimal() else int(DEFAULT_AGENT_PORT)


def live_sessions(port: int, root: Path) -> list[str] | None:
    """The session names the agent on `port` holds or asserts reach for at `root`, or None where no
    agent answers: the sessions moving the served root away from `root` would strand, as the agent
    that serves them knows them (#10535). A port nothing listens on strands nothing."""
    try:
        req = urllib.request.Request(f"http://127.0.0.1:{port}/status")
        with door_mod.send(req, 3) as r:
            served = json.loads(r.read() or b"{}").get("served")
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, ConnectionError,
            http.client.HTTPException, ValueError, AttributeError, OSError):
        return None
    one = served.get(str(root)) if isinstance(served, dict) else None
    if not isinstance(one, dict):
        return []
    names = set(one.get("held") or {}) | set(one.get("reached") or {})
    return sorted(str(n) for n in names if n)


def served_elsewhere(where: Path, repo: str, mine: Path) -> list[tuple[str, Path]]:
    """The registry's entries serving `repo` from another checkout than `mine`, as (authority,
    root): the served roots this install would move, one workspace at a time. A machine serves one
    root per workspace, so a second clone of a served repository re-points it (#10535)."""
    others: list[tuple[str, Path]] = []
    if not where.exists():
        return others
    for path in sorted(where.glob("*.json")):
        try:
            entry = machine_workspaces.parse(path.read_text())
        except (ValueError, OSError):
            continue
        if entry.root.resolve() == mine.resolve():
            continue
        if (origin_slug(entry.root, timeout=5) or "").lower() != repo.lower():
            continue
        others.append((path.stem, entry.root))
    return others


def move_served_root(root: Path, repo: str, chosen: bool) -> None:
    """Say every served root this install moves, and refuse one with sessions live in it unless the
    person chose the move (#10535): a re-point strands each session in the old checkout — the agent
    stops serving its root — and may not do so silently."""
    moves = served_elsewhere(machine_workspaces.directory(Path.home()), repo, root)
    if not moves:
        return
    port = machine_port()
    for authority, old in moves:
        live = live_sessions(port, old)
        if live is None:
            say("machine", f"{authority}: no agent answers on 127.0.0.1:{port}, so nothing it holds in {old} "
                           f"can be told live; moving the served root to {root}")
            continue
        if live and not chosen:
            raise Stop(f"this machine serves {repo} from {old}, where {len(live)} session{'s are' if len(live) > 1 else ' is'} "
                       f"live ({', '.join(live)}); installing here would move the served root to {root} and strand "
                       "them. Let those sessions end first, or run install again with --move-root to move the "
                       "served root anyway, stranding them")
        if live:
            say("machine", f"{authority}: moving the served root from {old} to {root}, stranding its live "
                           f"sessions ({', '.join(live)})")
        else:
            say("machine", f"{authority}: no session is live in {old}; moving the served root to {root}")


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


STALL = 120.0  # seconds without a word from the installer that end the install's wait


def run_installer(installed: Path, root: Path, door: str, port: int) -> None:
    """The release's own installer — its `2mw2lt install` — so the job is written by the closure
    it will run rather than by this plugin's copy. Bounded by silence, not by total time (#9728):
    a first install downloads the machine's whole tool kit, which takes as long as its link takes,
    so the wait ends only when the installer has said nothing for `STALL` seconds — a working
    install keeps saying where it is — and the refusal carries what it did say, since a first
    install killed here has written no log to point at."""
    go = require_installer(installed)
    # The machine job's own port, so an install leaves the job as it is when it already runs.
    child = subprocess.Popen([str(go), "install", str(root), door],
                             env={**os.environ, "STEERING_AGENT_PORT": str(port),
                                  **({"STEERING_NO_GH_ACCOUNT": "1"} if _service else {})},
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    said: list[str] = []
    word = threading.Event()

    def read(stream, relay: bool) -> None:
        for line in stream:
            said.append(line)
            word.set()
            if relay:  # its progress voice, as a continuation of this transcript's `agent` step
                try:
                    say("", line.removeprefix("agent: ").rstrip())
                except Exception:  # the relay is cosmetic: a reader that died here would leave the
                    pass           # pipe undrained, and the installer it blocks be killed as stalled

    readers = [threading.Thread(target=read, args=(stream, stream is child.stdout), daemon=True)
               for stream in (child.stdout, child.stderr)]
    for reader in readers:
        reader.start()
    quiet = time.monotonic()
    try:
        while True:
            if word.is_set():
                word.clear()
                quiet = time.monotonic()
            try:
                child.wait(timeout=max(0.0, STALL - (time.monotonic() - quiet)))
            except subprocess.TimeoutExpired:
                if word.is_set():
                    continue  # it spoke while we waited: the stall is measured from that word
                last = "".join(said).strip()[-400:]
                raise Stop(f"the agent release's installer has said nothing for {STALL:.0f}s"
                           f" ({f'last saying: {last}' if last else 'it had said nothing'})") from None
            else:
                break
    finally:
        if child.poll() is None:
            child.kill()
            child.wait()
        for reader in readers:
            reader.join(timeout=STALL)  # the pipes close with the child; only its own children hold them
    if child.returncode:
        raise Stop(f"the agent release's installer failed: {''.join(said).strip()[-400:]}")


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


# What install writes into a checkout, relative to its root, and where its sessions' worktrees go:
# a rescue's `git add -A` in a repository that does not ignore them would commit each as a gitlink.
LOCAL_FILES = (".env", ".claude/settings.local.json", ".claude/.settings.local.json.lock", ".claude/steering-launch.py",
               ".claude/steering-workspace", ".claude/steering-tokens", ".claude/worktrees")


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


# A repository as GitHub names one, `owner/name`, which a machine-only install is given.
REPO = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9]|-(?=[A-Za-z0-9])){0,38}/[A-Za-z0-9._-]{1,100}")


def login_home() -> Path:
    """This login's own home, as the account database names it, whatever `HOME` says."""
    return Path(pwd.getpwuid(os.getuid()).pw_dir)


def machine_only_refusal() -> str | None:
    """Why this login cannot be installed as a lent machine's service user (doc 209 §4.1–4.3), or
    None. Everything the install writes goes under `HOME`, so a `HOME` that is not this login's own
    would put it in another person's home: `sudo -u` keeps the caller's on some machines."""
    if platform.system() != "Darwin":
        return f"a machine-only install sandboxes every launch in an APFS disk image, which only macOS makes, and this is {platform.system()}"
    if os.geteuid() == 0:
        return "it runs as root; run it as the service user (`sudo -H -u _2mw2lt …`), whose agent it installs"
    own = login_home()
    if Path.home().resolve() != own.resolve():
        return (f"HOME is {Path.home()}, and this login's home is {own}; run it with that home "
                "(`sudo -H -u <service user> …`), so nothing is written in another login's")
    return None


def served_root(repo: str) -> Path:
    """The served root of doc 209 §4.6: an empty repository under this home whose origin is
    `repo`'s https address, holding only what the agent reads there. Nothing is fetched into it: a
    launch clones from the machine's mirror. One already made for `repo` is kept, so a run resumes."""
    root = Path.home() / "served" / repo
    url = f"https://github.com/{repo}.git"
    if root.exists():
        if not (root / ".git").is_dir():
            raise Stop(f"{root} exists and is not a repository made by install; move it aside, then run install again")
        held = origin_slug(root, timeout=5)
        if (held or "").lower() != repo.lower():
            raise Stop(f"{root} names {held or 'no GitHub repository'} as its origin, not {repo}; "
                       "move it aside, then run install again")
        say("served", f"{root}")
        return root
    root.parent.mkdir(parents=True, exist_ok=True)
    for args in (["init", "-q", str(root)], ["-C", str(root), "remote", "add", "origin", url]):
        done = subprocess.run(["git", *args], capture_output=True, text=True, timeout=10)
        if done.returncode:
            raise Stop(f"could not make the served root at {root}: git {args[0] if args[0] != '-C' else args[2]} "
                       f"said {(done.stderr or done.stdout).strip() or done.returncode}")
    say("served", f"{root}, origin {url}")
    return root


# The commit identity a sandboxed launch's clone is given (machine/internal/sandbox.Clone): the
# served root's own user.name and user.email, which a service user has no global config to supply.
IDENTITY_KEYS = ("user.name", "user.email")


def identity_key(root: Path, key: str) -> str | None:
    """The value `git config --get key` reads in `root` — every scope and every include, as the
    launcher reads it — None where unset. A key set to nothing, or to blanks, is a value the launcher
    refuses and an unset key the installer would overwrite, so it stops here naming the key; so does
    a read git itself failed (#10239)."""
    try:
        done = subprocess.run(["git", "-C", str(root), "config", "--get", key], capture_output=True,
                              text=True, timeout=10)
    except (OSError, subprocess.SubprocessError) as e:
        raise Stop(f"could not read {key} in {root}: {e}") from None
    if done.returncode == 1 and not done.stderr.strip():
        return None
    if done.returncode:
        raise Stop(f"could not read {key} in {root}: git said {(done.stderr or done.stdout).strip() or done.returncode}")
    if not done.stdout.strip():
        raise Stop(f"{key} is set to nothing in {root}, and a launch refuses a blank one; set it with "
                   f"`git -C {root} config {key} <value>` or remove it with `git -C {root} config --unset-all {key}`, "
                   "then run install again")
    return done.stdout.strip()


def served_identity(root: Path) -> list[str]:
    """The identity keys `root` lacks, read as the launcher reads them, so the installer and the
    launcher cannot disagree on what is configured."""
    return [key for key in IDENTITY_KEYS if identity_key(root, key) is None]


def noreply_identity(access: dict, console: str) -> dict[str, str]:
    """The commit identity coordination's access answer names the sign-in as: the login, and its
    GitHub noreply address `<id>+<login>@users.noreply.github.com`, which carries no one's own email.
    Each way the answer can fail to name one says so."""
    login = access.get("github_login")
    if not login:
        raise Stop("this sign-in is not linked to a GitHub account, so it names no commit author; run install "
                   "again and approve it in a browser profile signed in to GitHub")
    if not isinstance(login, str) or not LOGIN.fullmatch(login):
        raise Stop(f"coordination named the sign-in's GitHub login {login!r}, which is not one; ask its operator")
    if "github_id" not in access:
        raise Stop(f"{console} does not say the sign-in's GitHub id, which its noreply address needs; it runs a "
                   "release before this plugin's, so ask its operator to update it")
    ident = access["github_id"]
    if isinstance(ident, bool) or not isinstance(ident, int) or ident < 1:
        raise Stop(f"coordination named the sign-in's GitHub id {ident!r} (login @{login}), which is not a positive "
                   "number; the account's id could not be read, so run install again, or ask the operator")
    return {"user.name": login, "user.email": f"{ident}+{login}@users.noreply.github.com"}


def give_identity(root: Path, missing: list[str], identity: dict[str, str]) -> None:
    """The keys `root` lacked, set from `identity` and no other: a usable value it held is kept."""
    for key in missing:
        try:
            done = subprocess.run(["git", "-C", str(root), "config", key, identity[key]], capture_output=True,
                                  text=True, timeout=10)
        except (OSError, subprocess.SubprocessError) as e:
            raise Stop(f"could not set {key} in {root}: {e}") from None
        if done.returncode:
            raise Stop(f"could not set {key} in {root}: git said {(done.stderr or done.stdout).strip() or done.returncode}")
    # Read back rather than naming `identity`: a key the root kept is announced as what the launcher's
    # own read will find, not as the approver's value it was never given.
    say("identity", f"{root} commits as {identity_key(root, 'user.name')} <{identity_key(root, 'user.email')}>")


def repair_identity(console: str, root: Path, authority: str, missing: list[str], scope: str,
                    login: str | None) -> None:
    """A served root made before it was given an identity, given one on a fresh sign-in. The sign-in
    is trusted as the approver only where its account can select the workspace this root is bound to,
    so a browser profile that cannot reach it never becomes the Git author."""
    say("identity", f"{root} has no {' or '.join(missing)}; sign in to give it one")
    token = sign_in(console, scope, None, login, named=True)
    try:
        access = go_access(console, token)
        checked_login(access, login, login is not None)
        reaches = any(isinstance(w, dict) and str(w.get("workspace_id", "")).lower() == authority.lower()
                      for t in access["teams"] if isinstance(t, dict) for w in t.get("workspaces") or [])
        if not reaches:
            raise Stop(f"this sign-in cannot select the workspace {authority} that {root} serves, so it is not the "
                       "approver's; run install again and approve it as the account that admitted this machine, "
                       f"or set the identity yourself: `git -C {root} config user.name <name>` and "
                       f"`git -C {root} config user.email <email>`")
        give_identity(root, missing, noreply_identity(access, console))
    finally:
        end_session(console, token)


def sandboxed(home: Path) -> None:
    """`[sandbox] enabled = true` in this login's agent.toml, at the image size the settings name
    by default, so every launch the agent starts here is sandboxed from its first (doc 209 §4.3).
    The table is added to the file as written and the result parsed back, as `agentjob` adds a
    limit; a file that already turns sandboxing off, or that its readers refuse, is not rewritten."""
    import settings
    path = agentjob.tunables(home)
    try:
        text = path.read_text()
    except FileNotFoundError:
        text = ""
    try:
        existing = tomllib.loads(text)
        settings.validate(existing)
    except (tomllib.TOMLDecodeError, ValueError) as e:
        raise Stop(f"{path} cannot be read as the agent's settings ({e}); fix it, then run install again") from None
    table = existing.get("sandbox")
    if table is not None:
        if table.get("enabled") is not True:
            raise Stop(f"{path} has a [sandbox] table that does not turn sandboxing on, and a machine-only "
                       "install sandboxes every launch; set `enabled = true` there, then run install again")
        say("sandbox", f"on, images of {table.get('image_gib', settings.SANDBOX_IMAGE_GIB)} GiB ({path})")
        return
    added = f"[sandbox]\nenabled = true\nimage_gib = {settings.SANDBOX_IMAGE_GIB}\n"
    base, text = (existing, f"{text.rstrip()}\n\n{added}") if text.strip() else ({"schema": 1}, f"schema = 1\n\n{added}")
    wanted = {**base, "sandbox": {"enabled": True, "image_gib": settings.SANDBOX_IMAGE_GIB}}
    if tomllib.loads(text) != wanted:
        raise Stop(f"{path}: the [sandbox] table cannot be added to it as written; add "
                   f"`[sandbox] enabled = true` by hand, then run install again")
    agentjob.write(path, text.encode())
    say("sandbox", f"on, images of {settings.SANDBOX_IMAGE_GIB} GiB ({path})")


PLUGIN = "2mw2lt@2mw2lt"
MARKETPLACE = ("2mw2lt", "endario/2mw2lt")  # its name in known_marketplaces.json, and its source


def _claude_json(claude_home: Path, name: str) -> dict:
    """One of the Claude home's JSON files, `{}` where it is absent, unreadable or not an object."""
    try:
        doc = json.loads((claude_home / name).read_text())
    except (OSError, ValueError):
        return {}
    return doc if isinstance(doc, dict) else {}


def plugin_installed(claude_home: Path) -> bool:
    """Whether `claude_home`'s install record names a record for the plugin. A shape that is not an
    object of lists counts as absent, as Go's accounts.PluginInstalled reads it."""
    plugins = _claude_json(claude_home, "plugins/installed_plugins.json").get("plugins")
    records = plugins.get(PLUGIN) if isinstance(plugins, dict) else None
    return isinstance(records, list) and bool(records)


def plugin_loads(claude_home: Path) -> bool:
    """Whether `claude_home` holds the plugin's install record and the setting that loads it, the
    two the agent's launch check reads (launchready.Runs)."""
    enabled = _claude_json(claude_home, "settings.json").get("enabledPlugins")
    return plugin_installed(claude_home) and isinstance(enabled, dict) and enabled.get(PLUGIN) is True


def marketplace_source(entry: object) -> str | None:
    """The repository a known_marketplaces.json entry names as its source, `owner/name`, or the url
    it names where that is not GitHub's; None for an entry that names neither."""
    source = entry.get("source") if isinstance(entry, dict) else None
    if not isinstance(source, dict):
        return None
    repo, url = source.get("repo"), source.get("url")
    if isinstance(repo, str) and repo:
        return repo
    if isinstance(url, str) and url:
        found = re.fullmatch(r"https://github\.com/([^/]+/[^/]+?)(?:\.git)?/?", url)
        return found.group(1) if found else url
    return None


def added_plugin_record(home: Path) -> Path:
    """What this install added to the service user's Claude home, which `uninstall --machine` takes
    out again and nothing else: a person's own marketplace or plugin there is never ours to remove."""
    return home / ".config" / "2mw2lt" / "service-plugin.json"


def _record_added(home: Path, **added: bool) -> None:
    path = added_plugin_record(home)
    try:
        held = json.loads(path.read_text())
    except (OSError, ValueError):
        held = {}
    held = held if isinstance(held, dict) else {}
    held.update({k: True for k, v in added.items() if v})
    path.parent.mkdir(parents=True, exist_ok=True)
    spool.write_atomic(path, json.dumps(held))


def service_plugin(home: Path) -> None:
    """The service user's own Claude home loads the 2mw2lt plugin: each token launcher's home
    shares it from there, and nobody runs /2mw2lt:install inside Claude on a lent machine."""
    claude_home = home / ".claude"
    if plugin_loads(claude_home):
        say("plugin", f"{PLUGIN} ({claude_home})")
        return
    claude = shutil.which("claude")
    if claude is None:
        raise Stop("this login has no Claude Code CLI (claude) on PATH, and its Claude home must load the 2mw2lt "
                   "plugin; install Claude Code as this user, then run install again")
    env = {**os.environ, "CLAUDE_CONFIG_DIR": str(claude_home)}
    known = _claude_json(claude_home, "plugins/known_marketplaces.json")
    name, repo = MARKETPLACE
    held = marketplace_source(known.get(name))
    # A marketplace of the name that comes from elsewhere is not ours to replace or to install from.
    if held is not None and held.lower() != repo.lower():
        raise Stop(f"{claude_home} already has a marketplace named {name}, and it comes from {held}, not {repo}; "
                   f"remove it with `CLAUDE_CONFIG_DIR={claude_home} claude plugin marketplace remove {name}` as this "
                   "user, then run install again")
    adds = name not in known or held is None
    # What was there before is a person's: only what this run adds is recorded for the decommission.
    had_plugin = plugin_installed(claude_home)
    steps = [["plugin", "marketplace", "add", repo]] if adds else []
    for args in [*steps, ["plugin", "install", PLUGIN, "--scope", "user"]]:
        try:
            done = subprocess.run([claude, *args], capture_output=True, text=True, timeout=300, env=env)
        except subprocess.TimeoutExpired:
            raise Stop(f"`claude {' '.join(args)}` did not finish in 300s; run it as this user, then run install again") from None
        if done.returncode:
            said = (done.stderr or done.stdout).strip()[-400:] or f"exit {done.returncode}"
            raise Stop(f"`claude {' '.join(args)}` refused: {said}; run it as this user, then run install again")
        _record_added(home, marketplace=args[1:3] == ["marketplace", "add"], plugin=args[1] == "install" and not had_plugin)
    if not plugin_loads(claude_home):
        raise Stop(f"`claude plugin install {PLUGIN}` finished, and {claude_home} still does not load it; run "
                   f"`claude plugin enable {PLUGIN}` as this user, then run install again")
    say("plugin", f"{PLUGIN} installed ({claude_home})")


def unprovision_plugin(home: Path) -> bool:
    """The marketplace and plugin this install added to the service user's Claude home, taken out
    through the CLI that added them; what the record does not name stays. Each step that fails is said
    and stays recorded for the next run. Whether nothing is left of it."""
    path = added_plugin_record(home)
    try:
        held = json.loads(path.read_text())
    except FileNotFoundError:
        return True  # a home this install added nothing to
    except (OSError, ValueError) as e:
        say("plugin", f"kept {path}: it cannot be read ({e})")
        return False
    held = held if isinstance(held, dict) else {}
    steps = [(key, args) for key, args in (("plugin", ["plugin", "uninstall", PLUGIN, "--scope", "user"]),
                                           ("marketplace", ["plugin", "marketplace", "remove", MARKETPLACE[0]]))
             if held.get(key) is True]
    if not steps:
        path.unlink(missing_ok=True)
        return True
    claude_home = home / ".claude"
    claude = shutil.which("claude")
    if claude is None:
        say("plugin", f"not removed from {claude_home}: this login has no claude on PATH; run "
                      + "; ".join(f"`claude {' '.join(a)}`" for _, a in steps) + " as this user")
        return False
    env = {**os.environ, "CLAUDE_CONFIG_DIR": str(claude_home)}
    whole = True
    for key, args in steps:
        try:
            done = subprocess.run([claude, *args], capture_output=True, text=True, timeout=300, env=env)
            failed = (done.stderr or done.stdout).strip()[-400:] or f"exit {done.returncode}" if done.returncode else None
        except (OSError, subprocess.SubprocessError) as e:
            failed = str(e)
        if failed:
            whole = False
            say("plugin", f"`claude {' '.join(args)}` refused: {failed}; it stays recorded in {path}")
        else:
            held.pop(key, None)
            say("plugin", f"{key} removed from {claude_home}")
    if whole:
        path.unlink(missing_ok=True)
    else:
        path.write_text(json.dumps(held))
    return whole


def hook_python() -> None:
    """The `python3` the wired hooks run meets the floor before any work starts (#9888): the hook
    commands name it bare, so PATH's interpreter — not the one running install — is what install
    checks."""
    found, exe = python_floor.on_path()
    if found is None or tuple(found[:2]) < python_floor.NEEDS:
        raise Stop(python_floor.path_why(found, exe, "run install again"))
    say("python", f"{exe} ({'.'.join(str(v) for v in found)})")


def install(root: Path | None, codex: bool = False, team: str | None = None,
            gh_account: str | None = None, invite: str | None = None, login: str | None = None,
            machine: str | None = None, move_root: bool = False) -> int:
    """`machine`, an `owner/name`, installs this login as a lent machine's service user (doc 209)
    in place of a checkout at `root`: the served root is made here, and no GitHub login is read.
    The login was only ever the sign-in's hint and the registry's pin for a full host's sessions;
    the machine is admitted on the console sign-in its approver gives, as any machine is."""
    # A direct call, as the guards make, starts its transcript afresh — not off the import-time clock.
    global _at, _at_line, _service
    _at, _at_line, _service = "", time.monotonic(), machine is not None
    if machine is not None:
        if why := machine_only_refusal():
            raise Stop(f"this login cannot be installed machine-only: {why}")
    hook_python()
    console = platform_url()
    unnamed: list[str] = []  # the identity keys a machine-only install's served root still lacks
    if machine is not None:
        root = served_root(machine)
        # Read before anything is written, so a value the launcher would refuse stops the run early.
        unnamed = served_identity(root)
        sandboxed(Path.home())
        service_plugin(Path.home())
    repo = repository(root)
    say("repository", repo)
    move_served_root(root, repo, move_root)
    name = None if machine is not None else github_account(root, repo, gh_account)
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
        # --team rides the scope so the approval page names the team, not a guess (#9989).
        token = sign_in(console, f"install {repo} on {label}" + (f" in team {team}" if team else ""),
                        invite, login or name, named=named)
        try:
            access = go_access(console, token)
            checked_login(access, login or name, named)
            # Named before admission, written after it: a sign-in that cannot admit leaves the root bare.
            author = noreply_identity(access, console) if unnamed else {}
            door, authority, team_id, owner = admit_on_go(console, token, access, repo, team)
            if unnamed:
                give_identity(root, unnamed, author)
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
        if machine is not None and unnamed:
            repair_identity(console, root, authority, unnamed, f"install {repo} on {label}", login)
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
    if machine is not None:
        # A lent machine drafts no lanes: they are the repository's, from a checkout of it.
        print(json.dumps({"workspace": authority, "door": door, "port": port, "root": str(root)}))
        return 0
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
        status, got = call("DELETE", f"{console}/api/install/session", token=token)
    except Stop as e:
        say("session", f"the console session could not be ended ({e}); it lapses on its own")
        return
    if not 200 <= status < 300:
        say("session", f"the console would not end the session ({status}); it lapses on its own")


def uninstall(root: Path, machine: bool = False) -> int:
    bound = checkout_binding.id_at(root)
    try:
        door = recorded_door(root, bound) if bound else None
    except Stop as e:
        door = None
        say("credential", f"kept: {e}")
    held = credential.path_for(door) if bound and door else None
    off_platform = True
    if held is not None:
        off_platform = revoke_held(held)
    _remove_local(root, bound, held)
    swept = True
    if machine:
        swept = unprovision_machine(Path.home())
    # A revocation that failed leaves the machine admitted on the platform however clean this
    # checkout is, so the exit says it: a scripted decommission learns it was not whole.
    return 0 if off_platform and swept else 1


def _remove_local(root: Path, bound: str | None, held: Path | None) -> None:
    """What an install added to this checkout and this machine's agent for `bound`."""
    import hooks
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
        if held is not None:
            held.unlink(missing_ok=True)
        stopped = _stop_gate_runs(root / ".claude" / "gate-runs")
        if stopped:
            say("agent", f"{stopped} gate run{'s' if stopped != 1 else ''} of this workspace stopped")
        say("agent", f"{bound} deregistered from this machine's agent")
        _remove_checkout_credentials(root)
        registry = machine_workspaces.directory(Path.home())
        if not (registry.is_dir() and any(registry.glob("*.json"))):
            say("machine", "this was the last workspace this machine's agent served; the job stays, "
                           "serving nothing and following no release — `install.py uninstall --machine` "
                           "takes it down")


def _gate_run_pids(runs: Path) -> list[tuple[int, str]]:
    """The live gate-run processes a run directory's records name: detached from the agent, they
    outlive the workspace they were commissioned for, delivering to a door that now refuses them.
    A pid whose command no longer names a gate run is not one: the kernel reuses pids."""
    pids: list[tuple[int, str]] = []
    if not runs.is_dir():
        return pids
    for record in sorted(runs.glob("*/*/run.json")):
        try:
            named = json.loads(record.read_text()).get("pid")
            pid = int(named[0]) if isinstance(named, list) and named and isinstance(named[0], (int, float)) else 0
        except (OSError, ValueError, TypeError):
            continue
        if pid <= 1:
            continue
        try:
            command = subprocess.run(["ps", "-p", str(pid), "-o", "command="], capture_output=True,
                                     text=True, timeout=5).stdout
        except (OSError, subprocess.TimeoutExpired):
            continue
        if "gate-run" in command or "gate_child" in command or "gate-child" in command:
            pids.append((pid, command.strip()))
    return pids


def _stop_gate_runs(runs: Path) -> int:
    """Stop the gate runs parked under `runs`, TERM first — a run parks its judges on it (doc 132
    §4.2) — then KILL what a short wait leaves. How many were stopped."""
    return _terminate([pid for pid, _ in _gate_run_pids(runs)])


def _stop_gate_runs_by_binary(base: Path) -> int:
    """The gate runs still running from this machine's release root, stopped wherever their
    workspaces' checkouts have gone: their pids are found by the binary they run, not by any
    registry. How many were stopped."""
    if not base.is_dir():
        return 0
    try:
        found = subprocess.run(["pgrep", "-f", re.escape(str(base)) + r".*gate-run"], capture_output=True,
                               text=True, timeout=10).stdout.split()
    except (OSError, subprocess.TimeoutExpired):
        return 0
    pids = [pid for pid in (int(n) for n in found if n.isdigit()) if pid != os.getpid() and _alive(pid)]
    return _terminate(pids)


def _terminate(pids: list[int]) -> int:
    """Stop `pids`, TERM first — a gate run parks its judges on it (doc 132 §4.2) — then KILL
    what a short wait leaves. How many this user signalled: another user's process is neither
    signalled nor counted, though it may have been waited out."""
    signalled = [pid for pid in pids if _signal(pid, signal.SIGTERM)]
    end = time.monotonic() + 10
    while time.monotonic() < end and any(_alive(pid) for pid in signalled):
        time.sleep(0.2)
    for pid in signalled:
        if _alive(pid):
            _signal(pid, signal.SIGKILL)
    return len(signalled)


def _signal(pid: int, how: int) -> bool:
    """Signal `pid`, saying whether this user could: a permission failure is not a stop."""
    try:
        os.kill(pid, how)
        return True
    except OSError:
        return False


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True   # another user's process: alive, and not ours to stop


def unprovision_machine(home: Path) -> bool:
    """Take this machine's agent down: its jobs, the release root and tools kit its activations
    provisioned, the keys, settings, logs and proxies it wrote, the session-kit links into
    ~/.local/bin, and the credentials left for workspaces no checkout names any more — each
    revoked before its file goes, as this checkout's was (#10135). The one act to unprovision a
    machine; `uninstall` names it when the last workspace goes. Whether the platform holds no
    live admission for this machine after it: a credential that would not revoke leaves one."""
    where = machine_workspaces.directory(home)
    left = sorted(p.name.removesuffix(".json") for p in where.glob("*.json")) if where.is_dir() else []
    if left:
        say("machine", "not taken down: its agent still serves " + ", ".join(left)
                       + "; uninstall each of their checkouts first")
        return False
    base = agentjob.root(home)
    runs = _stop_gate_runs_by_binary(base)
    if runs:
        say("machine", f"{runs} gate run{'s' if runs != 1 else ''} still running from this machine's release stopped")
    _take_down_jobs(home)
    shutil.rmtree(base, ignore_errors=True)
    say("machine", f"the agent's release root removed ({base}: its releases, tools kit and launcher)")
    _take_down_proxies(home)
    links = _remove_tool_links(home, base)
    if links:
        say("machine", f"{links} session-kit link{'s' if links != 1 else ''} removed from ~/.local/bin")
    plugin_gone = unprovision_plugin(home)
    config = home / ".config" / "2mw2lt"
    for gone in (agentjob.settings(home), config / "worker-credentials.env", config / "machine.key",
                 config / "agents", config / "reaper", config / "launchers", where,
                 agentjob.log_directory(home)):
        if gone.is_dir():
            shutil.rmtree(gone, ignore_errors=True)
        else:
            gone.unlink(missing_ok=True)
    say("machine", "the agent's settings, keys, worker credentials and logs removed")
    return _sweep_machine_credentials() and plugin_gone


# The updater's own job, beside the agent's (follow/job.go): a transient unit under systemd, a
# plist under launchd. Stopping the agent's job does not stop it, so a decommission must.
UPDATE_LABEL = "com.2mw2lt.agent-update"


def _take_down_jobs(home: Path) -> None:
    """Stop the machine's agent jobs and take their files with them: the machine job, the
    updater's, and any an install older than the registry left per workspace. A job left loaded
    would start the agent again at the next login, serving a registry that names nothing."""
    jobs = agentjob.jobs_directory(home)
    labels = {agentjob.MACHINE_LABEL, UPDATE_LABEL}
    for path, document in agentjob.steering_jobs(jobs):
        labels.add(str((document or {}).get("Label") or path.stem))
    domain = agentjob.launch_domain()
    said: list[str] = []
    for label in sorted(labels):
        loaded = agentjob.loaded(domain, label)
        if loaded:
            agentjob.stop(domain, label)
            try:
                agentjob.wait_unloaded(domain, label)
            except RuntimeError as e:
                say("machine", f"{label} did not stop in time ({e}); it may still be running")
        if agentjob.init_system() == "systemd":
            agentjob.command(["disable", "--now", f"{label}.service"])
            agentjob.command(["daemon-reload"])
        path = agentjob.job_path(jobs, label)
        held = path.exists()
        path.unlink(missing_ok=True)
        for stale in jobs.glob(f"{path.name}.bak-*"):
            stale.unlink(missing_ok=True)
        if loaded or held:
            said.append(label)
    if said:
        say("machine", "the agent jobs " + ", ".join(said) + " stopped and removed")


def _take_down_proxies(home: Path) -> None:
    """The claude-code-proxy instances the agent kept, one per Codex login (#8623): their units
    stopped and removed, their env files and the wrappers under the launcher root gone. A wrapper
    a person built by hand — under /usr/local/bin, before the pass owned them — is theirs and is
    only named."""
    config = home / ".config"
    units = config / "systemd" / "user" if agentjob.init_system() == "systemd" else agentjob.jobs_directory(home)
    named = re.compile(r"^claude-code-proxy(-[0-9]+)?\.service$"
                       if agentjob.init_system() == "systemd" else r"^com\.2mw2lt\.claude-code-proxy(-[0-9]+)?\.plist$")
    stopped: list[str] = []
    slots: list[str] = []
    for unit in sorted(units.glob("claude-code-proxy*.service" if agentjob.init_system() == "systemd"
                                  else "com.2mw2lt.claude-code-proxy*.plist")):
        if not named.fullmatch(unit.name):
            continue
        label = unit.name.removesuffix(".service") if agentjob.init_system() == "systemd" else unit.name.removesuffix(".plist")
        # The slot's own suffix names its env file and wrapper: those go with the unit, and a
        # person's file of the same prefix that no removed unit owns stays.
        slots.append(next(label[len(p):] for p in ("claude-code-proxy", "com.2mw2lt.claude-code-proxy")
                          if label.startswith(p)))
        if agentjob.init_system() == "systemd":
            agentjob.command(["disable", "--now", unit.name])
            agentjob.command(["daemon-reload"])
        else:
            domain = agentjob.launch_domain()
            agentjob.stop(domain, label)
        unit.unlink(missing_ok=True)
        stopped.append(label)
    for slot in slots:
        (config / f"claude-codex{slot}.env").unlink(missing_ok=True)
        if (config / "2mw2lt" / "launchers").is_dir():
            (config / "2mw2lt" / "launchers" / f"claude-codex{slot}").unlink(missing_ok=True)
    if stopped:
        say("machine", "the claude-code-proxy instances " + ", ".join(stopped) + " stopped and removed; "
                       "the Codex sign-ins themselves are the person's and stay")
    hand_kept = sorted(p.name for p in Path("/usr/local/bin").glob("claude-codex*")) \
        if Path("/usr/local/bin").is_dir() else []
    if hand_kept:
        say("machine", "wrappers a hand built stay: " + ", ".join(hand_kept))


def _remove_tool_links(home: Path, base: Path) -> int:
    """The session-kit links the tools kit placed in ~/.local/bin, each pointing into the agent's
    root (agent-tools.sh): a person's own install of the same name, or a link to somewhere else,
    stays. How many were removed."""
    links = 0
    bin = home / ".local" / "bin"
    if not bin.is_dir():
        return 0
    inside = os.path.realpath(base) + os.sep
    for held in sorted(bin.iterdir()):
        try:
            if held.is_symlink() and os.path.realpath(held).startswith(inside):
                held.unlink()
                links += 1
        except OSError:
            continue
    return links


def _sweep_machine_credentials() -> bool:
    """Each credential this machine still holds for a workspace no checkout names any more,
    revoked before its file goes as this checkout's was (#10135); one that will not revoke
    stays, for a copy of it held elsewhere could still renew, and deleting it would hide that.
    Whether none is left live on the platform."""
    clean = True
    held = credential.home()
    if not held.is_dir():
        return True
    for p in sorted(held.glob("*.json")):
        try:
            credential.revoke_native(p)
        except (credential.NoCredential, OSError) as e:
            clean = False
            say("credential", f"kept {p.name}: not revoked on coordination: {e}; a copy of it held "
                              "elsewhere could still renew, so it is left to be named")
            continue
        p.unlink(missing_ok=True)
        p.with_suffix(".lock").unlink(missing_ok=True)
        say("credential", f"{p.name} revoked on coordination and removed")
    return clean


def _remove_checkout_credentials(root: Path) -> None:
    """The credentials a bound checkout held: the per-session enrollment records every harness
    shares (hooks.tokens_directory), the observe token a local wiring copied in, and the Codex
    forge rule naming this checkout's launcher. A checkout no binding places keeps whatever it
    holds, and the daemon's own workspace keeps its own tokens (hooks.is_daemon_workspace) — its
    forge rule still goes, for the launcher it names goes with the uninstall."""
    import codex_rules
    import hooks
    removed: list[str] = []
    if not hooks.is_daemon_workspace(root):
        tokens = root / ".claude" / "steering-tokens"
        if tokens.is_dir():
            shutil.rmtree(tokens, ignore_errors=True)
            if not tokens.exists():
                removed.append("the sessions' enrollment tokens")
            else:
                say("credential", f"the sessions' enrollment tokens under {tokens} were not all removed; "
                                  "remove them by hand")
        for name in hooks.TOKENS:
            held = root / ".claude" / name
            if held.exists():
                try:
                    held.unlink()
                    removed.append(f"the {name.removeprefix('steering-').replace('-', ' ')}")
                except OSError as e:
                    say("credential", f"{held} was not removed ({e}); remove it by hand")
    if codex_rules.remove(root):
        removed.append("the Codex forge rule")
    if removed:
        say("credential", f"removed from the checkout: {', '.join(removed)}")


def revoke_held(held: Path) -> bool:
    """End this machine's admission on the credential `held` keeps, before the file goes (#10135):
    the file deleted alone left its binding live, and a copy of it renewing for a week past expiry.
    Whether the platform holds no live admission for this machine after it: a revocation that
    failed leaves one a copy of the credential could still renew."""
    try:
        said = credential.revoke_native(held)
    except credential.NoCredential as e:
        moved = ""
        if e.status == 404:
            # The route the credential was enrolled at is gone — its workspace moved, its alias was
            # cut, its team renamed — so this machine's own way to end its admission went with it.
            moved = ("; the door it was enrolled at no longer routes there, the workspace having moved or "
                     "been renamed, so end this machine's admission from the desk if one still stands")
        say("credential", f"not revoked on coordination: {e}{moved}; removed from this machine all the "
                          "same, so a copy of it held elsewhere could still renew")
        return False
    except OSError as e:
        say("credential", f"not revoked on coordination: {e}; removed from this machine all the same, "
                          "so a copy of it held elsewhere could still renew")
        return False
    if said:
        say("credential", said)
    return True


def workspace_address(value: str) -> bool:
    """Whether `value` is a workspace's `<team>/<workspace>` address: both slugs in the contract's
    grammar, its team none of the reserved ones a door's address route never carries."""
    team, _, slug = value.partition("/")
    return (re.fullmatch(contract.TEAM_SLUG, team) is not None and team not in contract.RESERVED_TEAM_SLUGS
            and re.fullmatch(contract.WORKSPACE_SLUG, slug) is not None)


def retire_on(console: str, address: str) -> None:
    """Retire the workspace `address` names on coordination, its state kept."""
    # Its own scope: an install's carries the approval page on to GitHub for the repository (#4015).
    token = sign_in(console, f"delete {address}")
    try:
        status, got = call("POST", f"{console}{door_mod.routed(address)}/api/v1/workspace/retirement", {}, token,
                           key=str(uuid.uuid4()))
        if status != 200:
            if got.get("code") == "workspace-retired":
                # A re-run, or another person's delete: the state asked for stands, so this is the
                # outcome the caller wanted, not a refusal.
                say("workspace", f"{address} is retired already; its state is kept on the platform")
                return
            raise Stop(f"coordination would not retire {address} ({status}): {got.get('code') or got}")
        say("workspace", f"{address} retired; its state is kept on the platform")
    finally:
        end_session(console, token)


def delete_workspace(address: str) -> int:
    """Retire the workspace `address` names, for every machine — from anywhere, for one no checkout
    here is bound to as for one that is (#3742). What an install added on a machine is still that
    machine's `uninstall` to remove."""
    retire_on(platform_url(), address)
    return 0


def create_at(address: str) -> int:
    """Create the workspace `address` names — the repository `<team>/<workspace>` in the team
    slugged `<team>` — on the platform, joining no machine. A plain `install` in its repository
    does the same creation and then admits the machine it runs on."""
    console = platform_url()
    token = sign_in(console, f"create {address}")
    try:
        access = go_access(console, token)
        slug = address.split("/")[0]
        team = next((t for t in access["teams"] if isinstance(t, dict) and t.get("slug") == slug), None)
        if team is None:
            slugs = sorted(t["slug"] for t in access["teams"] if isinstance(t, dict) and t.get("slug"))
            raise Stop(f"you are in no team addressed {slug}"
                       + (f"; your teams' addresses begin {', '.join(slugs)}" if slugs else ""))
        if team.get("owner") is not True:
            raise Stop(f"only a team's owner creates its workspaces; you are a member of {slug}")
        got = create_workspace(console, token, team["id"], address)
        print(json.dumps({"workspace": got.get("address") or address}))
        return 0
    finally:
        end_session(console, token)


def main(argv: list[str]) -> int:
    if verb_help.help_requested("install", argv):
        print(verb_help.script_help("install", topic=argv[0] if len(argv) == 2 else None)); return 0
    args = list(argv)
    if args[:1] == ["retire"]:
        return verb_help.error("install", "`retire` is gone: `install.py delete <team>/<workspace>` "
                                          "retires the workspace for every machine")
    action = args[0] if args[:1] in (["uninstall"], ["lanes"], ["create"], ["delete"]) else "install"
    if action != "install":
        args.pop(0)
    merge = False
    codex = False
    machine = False
    move_root = False
    team = None
    gh_account = None
    invite = None
    login = None
    machine_only = None
    paths: list[str] = []
    i = 0
    while i < len(args):
        arg = args[i]
        if arg in ("--workspace", "--retire-workspace"):
            # Both read as "this machine's part of the workspace" while retiring the workspace for
            # every machine — a leave ask that ended the workspace. Uninstall leaves; delete retires.
            return verb_help.error("install", "uninstall removes this machine from the workspace; "
                                              "`install.py delete <team>/<workspace>` retires the "
                                              "workspace for every machine")
        if arg == "--machine":
            if action != "uninstall":
                return verb_help.error("install", "--machine is only for uninstall")
            machine = True; i += 1; continue
        if arg == "--merge":
            if action != "lanes":
                return verb_help.error("install", "--merge is only for lanes")
            merge = True; i += 1; continue
        if arg == "--codex":
            if action != "install":
                return verb_help.error("install", "--codex is only for install")
            codex = True; i += 1; continue
        if arg == "--move-root":
            if action != "install":
                return verb_help.error("install", "--move-root is only for install")
            if move_root:
                return verb_help.error("install", "move-root may appear once")
            move_root = True; i += 1; continue
        if arg == "--team":
            if action != "install":
                return verb_help.error("install", "--team is only for install")
            if i + 1 >= len(args) or args[i + 1].startswith("-") or not args[i + 1].strip() or len(args[i + 1].split()) > 1:
                return verb_help.error("install", "missing team value")
            if team is not None:
                return verb_help.error("install", "team may appear once")
            team = args[i + 1]; i += 2; continue
        if arg == "--gh-account":
            if action not in ("install", "lanes"):
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
        if arg == "--machine-only":
            if action != "install":
                return verb_help.error("install", "--machine-only is only for install")
            if i + 1 >= len(args) or not REPO.fullmatch(args[i + 1]):
                return verb_help.error("install", "--machine-only takes the repository as owner/name")
            if machine_only is not None:
                return verb_help.error("install", "machine-only may appear once")
            machine_only = args[i + 1]; i += 2; continue
        if arg.startswith("-"):
            return verb_help.error("install", f"unknown option {arg!r}")
        # An invitation code is taken as one wherever it stands, so `/2mw2lt:install <code>` runs as typed.
        if INVITE.fullmatch(arg) and action == "install":
            if invite is not None:
                return verb_help.error("install", "invite code may appear once")
            invite = arg; i += 1; continue
        paths.append(arg); i += 1
    if action != "install" and (codex or team is not None
                                or (action != "lanes" and gh_account is not None)):
        return verb_help.error("install", f"{action} does not accept install options")
    if len(paths) > 1:
        return verb_help.error("install", "too many workspace paths")
    if action in ("create", "delete") and (len(paths) != 1 or not workspace_address(paths[0])):
        reason = f"{action} takes one explicit `<team>/<workspace>` address"
        if action == "delete":
            reason += " — never `.`, a workspace id, or the checkout's binding"
        return verb_help.error("install", reason)
    if machine_only is not None:
        # Each its own refusal: a served root is made, not a checkout named, and no gh login is read.
        if paths:
            return verb_help.error("install", "--machine-only makes its own served root and takes no workspace path")
        if gh_account is not None:
            return verb_help.error("install", "--machine-only reads no GitHub login, so it takes no --gh-account")
        if codex:
            return verb_help.error("install", "--machine-only wires the hooks a sandboxed session's connect needs, "
                                              "which --codex does not write")
    root = None
    if action not in ("create", "delete") and machine_only is None:
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
        if action == "create":
            return create_at(paths[0])
        if action == "delete":
            return delete_workspace(paths[0])
        if action == "lanes":
            return adopt_lanes(root, merge, gh_account)
        return uninstall(root, machine) if action == "uninstall" else install(root, codex, team, gh_account, invite,
                                                                              login, machine_only, move_root)
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
