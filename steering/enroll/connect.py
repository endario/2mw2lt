#!/usr/bin/env python3
"""`connect.py [--as <account>] [--doing <text>] [<session>]`: this session, talking to steering.

Run it at the start of a session and after every restart of its process. The account is derived
from the config directory the harness is running under, so `--as` is an override rather than
something to supply.

A harness that does not tell its own process which session it is takes `--provider` and
`--provider-session`, and `--pid` for the process to derive a runtime identity from. What
differs between harnesses is asked of `harness`.
"""
from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))
from local_workspace import KEY_HEADER, agent_key, agent_port, common_root, required_workspace_root, workspace_header  # noqa: E402
import door  # noqa: E402
from door import say  # noqa: E402
from ack import store, valid_token  # noqa: E402
from dataclasses import dataclass  # noqa: E402
from bind import Refused, bind, incarnation, incarnation_of, pid_arg, records  # noqa: E402
from transcript_proof import account_of, identify_transcript  # noqa: E402
import machine_harness as harness_mod  # noqa: E402
from process_probe import Undetermined  # noqa: E402
import hooks  # noqa: E402
import witness as witness_mod  # noqa: E402
from verb_help import current_args, error, help_requested, script_help  # noqa: E402

STALE = ("unknown, detached or stale token", "is not enrolled")


def project_dir() -> Path:
    return Path(os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd())


@dataclass
class Options:
    """What both commands take. `rotate.py` shares this: it enrols too, and one that could
    only name Claude's environment would put a Codex session's disclosed token beyond
    replacing."""
    named: str = ""                             # `--as`, an account or `<harness>/<account>`
    doing: str = ""
    session: str | None = None
    provider: str | None = None                 # `--provider`, or whichever says this is its own
    psession: str | None = None
    pid: int | None = None


def options(argv: list[str]) -> Options:
    """Raises `Refused` on anything the grammar does not hold, for the caller to print usage."""
    o, args = Options(), list(argv)
    while args and args[0].startswith("-"):
        flag = args.pop(0)
        if flag not in ("--as", "--doing", "--provider", "--provider-session", "--pid"):
            raise Refused(f"no such flag: {flag}")
        if not args or (flag != "--doing" and args[0].startswith("-")):
            raise Refused(f"{flag} takes a value")
        value = args.pop(0)
        if flag == "--as":
            o.named = value
        elif flag == "--doing":
            o.doing = value
        elif flag == "--provider":
            o.provider = value
        elif flag == "--provider-session":
            o.psession = value
        elif flag == "--pid":
            o.pid = pid_arg(value)
    if args:
        o.session = args.pop(0)
    if args:
        raise Refused("one session name at most")
    return o


def account(o: Options, h: harness_mod.Harness) -> str:
    """Derived, not asked for: the configuration directory in use is which account this is, and
    it is the same value the brain derives for a local session (#139). `--as` and
    `STEERING_ACCOUNT` stay as overrides for a harness that keeps no account in a directory."""
    a = o.named or os.environ.get("STEERING_ACCOUNT", "").strip() \
        or account_of(h.config_dir(), h.provider)
    return f"{h.agent}/{a}" if a and "/" not in a else a


def harness_of(provider: str | None) -> harness_mod.Harness:
    """The harness named, or the one that says this process is one of its sessions."""
    if provider is None:
        found = harness_mod.here()
        if len(found) > 1:
            raise Refused("more than one harness says this is its session ("
                          + ", ".join(h.provider for h in found)
                          + "): name the one you mean with --provider")
        return found[0] if found else harness_mod.HARNESSES[harness_mod.DEFAULT]
    h = harness_mod.of(provider)
    if h is None:
        raise Refused(f"{provider} is not a provider: one of {', '.join(harness_mod.PROVIDERS)}")
    return h


def transcript(h: harness_mod.Harness, psession: str) -> str:
    """Where this harness writes the session, asked of the harness."""
    path = h.transcript(psession) if h.transcript is not None else None
    if path is None:
        raise Refused(f"no transcript for {psession}: {h.provider} keeps none under "
                      f"{h.config_dir()}. Enrol by hand, naming the transcript")
    return str(path)


def name_for(psession: str, taken: dict[str, dict] | None = None,
             project: Path | None = None) -> str:
    """This workspace and eight hex of the session id. Not the identity: the record holds the
    provider session it was minted for, and that is what `connect` compares.

    Eight hex of a Codex session id is a millisecond clock, its ids being UUIDv7, so two
    threads started within a minute of each other in one workspace derive the same eight (#895).
    The name takes four more hex at a time until it names nothing another session holds. A name
    that does not collide is the same eight it has always been, so nothing enrolled is renamed.
    """
    flat = psession.replace("-", "")
    base = (project or project_dir()).name
    for n in range(8, len(flat), 4):
        name = f"{base}-{flat[:n]}"
        held = ((taken or {}).get(name) or {}).get("provider_session")
        if not held or held == psession:
            return name
    return f"{base}-{flat}"


def minted_name(taken: dict[str, dict], psession: str) -> str | None:
    """The name this workspace already holds a record for this provider session under.

    Asked before the name is derived, because a derived name is only stable while the sessions
    it was derived against stay enrolled: the session that had to grow its name would otherwise
    shrink back to the base once the collider detached, and enrol again under a second name
    while its own enrolment still stood.
    """
    for name, rec in sorted(taken.items()):
        if rec.get("provider_session") == psession:
            return name
    return None


def own_enrolment(ws: Path, psession: str, session: str | None = None) -> tuple[str, str]:
    """(name, token) of the enrolment this provider session acts as: the one minted for it, or the
    name given, which is refused when the workspace minted it for another session. Every session
    in a workspace can read every stored token, so the name is the only thing standing between a
    typo and speaking with somebody else's credential (#1077)."""
    known = records(ws)
    name = session or minted_name(known, psession)
    minted_for = (known.get(name) or {}).get("provider_session")
    if minted_for and minted_for != psession:
        raise Refused(f"{name} is another session's enrollment ({minted_for}): pass a name of your own")
    if session and name in known and not minted_for and len(known) > 1:
        # Stored by hand, with nothing saying whose it is, beside records that are somebody's.
        raise Refused(f"{name} was stored without the session it belongs to, so nothing shows it is "
                      f"yours: store it again with ack.py --store {name} <token> --provider-session <id>")
    if name is None:
        if len(known) == 1:
            name = next(iter(known))
        else:
            raise Refused("no enrollment is minted for this session in " + str(ws)
                          + (f"; it holds tokens for {', '.join(sorted(known))}" if known else "")
                          + ": run /2mw2lt:connect first")
    token = (known.get(name) or {}).get("token")
    if not valid_token(token):
        raise Refused(f"no stored token for {name}: enroll first")
    return name, token


SPEAKER_FLAGS = ("--provider", "--provider-session")


def say_invocation(h: harness_mod.Harness, psession: str, session: str) -> str:
    """The one `say.py` this machine and this harness can actually run, ready to paste: to the
    seat, which either door takes (#3381); `--to <session>` reaches a peer instead.

    The provider pair is named unconditionally rather than only where the harness
    is silent: `speaking_as` accepts the pair that agrees with the process it runs in, and
    leaving it out is refused outright wherever two harnesses claim the session, which is the
    ordinary case on a machine running both. Printed whole and absolute, and the path quoted
    because an install under a directory with a space in it would otherwise run something else,
    because an invocation that has to be repaired before it runs is worth less than none.
    """
    return (f'python3 {shlex.quote(str(HERE / "say.py"))} --provider {h.provider} '
            f'--provider-session {psession} {session} "<text>"')


def speaker_flags(argv: list[str], also: tuple[str, ...] = ()) -> tuple[dict[str, str], list[str]] | None:
    """The leading `--flag value` pairs a speaking script takes, and what follows them; None when
    one is not a flag it takes or has no value."""
    args, flags = list(argv), {}
    while args and args[0].startswith("-"):
        flag = args.pop(0)
        if flag not in SPEAKER_FLAGS + also or flag in flags or not args:
            return None
        value = args.pop(0)
        if not value.strip() or value.startswith("-"):
            return None
        if flag == "--provider" and value not in harness_mod.PROVIDERS:
            return None
        flags[flag] = value
    return flags, args


def speaking_as(ws: Path, session: str | None, flags: dict[str, str]) -> tuple[str, str]:
    """(name, token) a script sends as: `own_enrolment` for the provider session this process
    runs in, or the one the flags name where no harness identifies the process (#1105). A
    harness that does say is not overruled by the flag, and a named provider that is silent
    while another harness identifies this process does not stand in for it."""
    provider = flags.get("--provider")
    own = harness_of(provider).whoami()[0]
    named = flags.get("--provider-session")
    if named and own and named != own:
        raise Refused(f"this harness is session {own}, not {named}: a session speaks only as itself")
    if named and not own:
        # A silent named provider is not this process's silence to claim: which harness runs it
        # is a fact of the process, not a flag.
        found = harness_mod.here()
        if found:
            raise Refused("this process is a session of " + ", ".join(h.provider for h in found)
                          + ", which names its sessions itself: a session speaks only as itself")
    psession = own or named
    if not psession:
        raise Refused("this harness does not say which session this is: pass "
                      "--provider <harness> --provider-session <id>")
    return own_enrolment(ws, psession, session)


def branch(project: Path | None = None) -> str:
    import subprocess
    try:
        out = subprocess.run(["git", "-C", str(project or project_dir()), "branch", "--show-current"],
                             capture_output=True, text=True, timeout=2).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return out


def head(project: Path | None = None) -> str:
    """The commit this checkout is on, or "" — what a gate asserts it means to have reviewed.

    GitHub can answer an older `headRefOid` for a moment after a push, and the branch name alone
    does not say which commit the asking session has (#1552).
    """
    import subprocess
    try:
        out = subprocess.run(["git", "-C", str(project or project_dir()), "rev-parse", "HEAD"],
                             capture_output=True, text=True, timeout=2)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return out.stdout.strip() if out.returncode == 0 else ""


def workspace_of(h: harness_mod.Harness, project: Path, path: str) -> Path:
    """The workspace the door's own proof admits `path` under: the directory the session runs in,
    else the repository's main checkout, where a session launched there and connecting from a
    linked worktree keeps its transcript. The proof is the door's, run here, so nothing is
    admitted that it would refuse; one that neither admits is left to the door to refuse."""
    roots = [(h.provider, h.config_dir() / h.transcript_root)]
    try:
        candidates = [project, common_root(project, timeout=2.0)]
    except (OSError, subprocess.SubprocessError):
        candidates = [project]
    for candidate in candidates:
        if identify_transcript(path, str(candidate), roots, h.provider)[0] is None:
            return candidate
    return project


def enrol(ws: Path, session: str, account: str, doing: str, psession: str,
          h: harness_mod.Harness, project: Path | None = None) -> str:
    project = project or project_dir()
    on = branch(project)
    path = transcript(h, psession)
    line = (f"enroll: {session} as {account} workspace {workspace_of(h, project, path)} transcript {path}"
            + (f" on {on}" if on else "") + (f" doing {doing}" if doing else ""))
    reply = say(line)
    if not reply.startswith("enrolled:"):
        raise Refused(reply)
    token = reply.split(" token ", 1)[1].split()[0] if " token " in reply else ""
    if not valid_token(token):
        raise Refused(f"the door's reply carried no token: {reply[:120]}")
    store(ws, session, token, psession)
    return token


def connect(ws: Path, session: str | None, account: str, doing: str,
            h: harness_mod.Harness, psession: str | None = None,
            pid: int | None = None, project: Path | None = None) -> tuple[str, str, str, str]:
    """(session, the door's answer to `bind:`, the runtime id it named, the provider session).

    `project` is the directory the session is working in — the workspace an `enroll:` line
    names, the base its name is derived from, and the checkout its branch is read from. It is
    an argument rather than the ambient one because the agent enrolling a worker of its own
    holds its workspace explicitly and must not rest on where launchd started it.
    """
    project = project or project_dir()
    psession, rid = incarnation(psession, pid, h.provider)
    if not h.hooks:
        # A harness that installs no hook posts no observation, and `bind:` is admitted only
        # for an incarnation the store has seen. This is what stands in (doc 47 §12); it is
        # refused rather than skipped, because a bind that follows would be refused anyway and
        # would say the wrong thing about why.
        try:
            witness_mod.witness(ws, h, psession, rid)
        except witness_mod.Unwitnessed as why:
            raise Refused(f"{h.provider} could not be observed here: {why}")
        except OSError as why:
            raise Refused(f"the observation for {psession} could not be sent: {why}")
    known = records(ws)
    session = session or minted_name(known, psession) or name_for(psession, known, project)
    rec = known.get(session) or {}
    minted_for = rec.get("provider_session")
    if minted_for and minted_for != psession:
        raise Refused(f"{session} is another session's enrollment ({minted_for}): pass a name of your own")
    token = rec.get("token")
    if token is not None:
        answer, rid = bind(session, token, psession, pid, h.provider)
        if not any(why in answer for why in STALE):
            return session, answer, rid, psession
    if not account:  # only a harness whose account cannot be derived and was not named
        raise Refused(f"{session} is not enrolled here: re-run with --as <harness>/<account>")
    token = enrol(ws, session, account, doing, psession, h, project)
    return session, *bind(session, token, psession, pid, h.provider), psession


def hand_over_pane(ws: Path, provider: str, psession: str, rid: str, pid: int, project: Path, *,
                   env=os.environ, post=None, find=None) -> str | None:
    """Post the tmux pane this process runs in, for a wake to type into (doc 118 §3.1), or
    answer why it was not. Nothing outside a pane. Every harness connects through here, inside
    its own process tree: the terminal's `TMUX`/`TMUX_PANE` name the pane where the harness
    hands them down, and the harness pid's ancestry finds it on the default server where not."""
    import targeting
    # `TMUX` names the server to search, never the pane itself: a harness started from a
    # terminal inside a pane (`code .`) inherits a `TMUX_PANE` its own process is not in, and a
    # pane handed over that way would take precedence over its real route (doc 118 §3.1).
    tmux = env.get("TMUX")
    if find is None:
        import wakeexec
        find = wakeexec.pane_of
    where = find(pid, socket=tmux.split(",", 1)[0] if tmux else None)
    if where is None:
        return None
    rec = targeting.build(provider_session=psession, runtime_id=rid, source=provider,
                          observed_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                          entrypoint=targeting.TMUX_ENTRYPOINT, pid=pid, cwd=str(project),
                          tmux=where)
    why = targeting.validate(rec)
    if why:
        return f"tmux pane not handed over: {why}"
    if post is None:
        import observe_post
        post = observe_post.post
    try:
        post(rec, ws, 3.0, route="targeting")
    except Exception as e:
        return f"tmux pane not handed over: {e}"
    return None


AGENT_PORT = os.environ.get("STEERING_AGENT_PORT") or "9990"
# Seconds: past the agent's own wait for an uplink (UPLINK_WAIT) and for the orchestrator to answer
# the reach it commits (COMMIT_WAIT), or a reach that landed is reported here as none (#1961).
REACH_WAIT = 105.0


RETRIES = 1        # #1690: one refused connection is not "no agent," it may be one mid-restart
RETRY_PAUSE = 0.5  # seconds, giving a restarting listener a moment to answer again


def serving(port: str, timeout: float = 2.0) -> list[str] | None:
    """The workspaces the agent on this port says it serves: their paths, `[]` when it answers
    without naming one, and None when nothing answered on any attempt. An agent serving several
    lists them in `workspaces`; one older than doc 130 names its one in `workspace`.

    An agent older than #1073 names none, so an unnamed workspace is not a mismatch — it is an
    agent that cannot say. Refusing on it would leave every session without a port until each
    machine's agent had been restarted.

    A single refused connection is retried once before it is believed: an agent mid-restart
    refuses for a moment on a port it is about to answer on again, and the caller's remedy for
    "nothing here" is to start one — dangerous exactly when the diagnosis is this one, transient
    kind of wrong (#1690).
    """
    for attempt in range(RETRIES + 1):
        try:
            with door.send(urllib.request.Request(f"http://127.0.0.1:{port}/status"), timeout=timeout) as r:
                said = json.loads(r.read() or b"{}")
            served = said.get("workspaces")
            if not isinstance(served, list):
                served = [said.get("workspace")]
            return [str(w) for w in served if w]
        except Exception:
            if attempt < RETRIES:
                time.sleep(RETRY_PAUSE)
                continue
            return None


def agent_says(port: str, workspace: Path, session: str) -> tuple[str | None, str | None]:
    """(the port to print, what to say about it). A port is printed only when the agent on it
    serves this workspace or cannot say which it serves.

    The refusal a session gets for holding a stream at another workspace's agent is the
    documented one for a session that is not enrolled, so it reads as a bad credential and
    sends the session to re-check a token that is correct (#1073). This is where that is
    cheapest to catch: the port is being printed for the session to carry.
    """
    # Where the port came from. Telling an operator to name it in the variable it already came
    # from is the advice that sent one looking in the workspace record it was overriding.
    whence = (" from STEERING_AGENT_PORT" if os.environ.get("STEERING_AGENT_PORT") == port
              else f", recorded by {workspace}")
    served = serving(port)
    if served is None:
        # Not "no agent serves this workspace" and not "was not found": nothing answered on the
        # one port asked, after a retry, and an agent on another is exactly what this workspace
        # failed to record. "Not found" reads as a standing condition and its own remedy —
        # start one — is unsafe on the diagnosis it would actually be wrong about: an agent
        # already serving this workspace, mid-restart or briefly loaded, that a second one
        # would then share a port and a hold with (#1690).
        return None, (f"127.0.0.1:{port}{whence} did not answer, even after a retry. An agent "
                      f"may already be serving this workspace and briefly unable to; starting a "
                      f"second one when it is would give the workspace two, so check what is "
                      f"actually listening first: lsof -nP -iTCP -sTCP:LISTEN | grep -F -- "
                      f"{shlex.quote(port)}. If nothing is, start {workspace}'s agent, or name "
                      f"the right port in STEERING_AGENT_PORT")
    if served and workspace.resolve() not in {Path(w).resolve() for w in served}:
        return None, (f"the agent on 127.0.0.1:{port}{whence} serves {', '.join(served)}, not {workspace}: "
                      f"a stream held there is refused `no session named {session} is enrolled "
                      f"here`, which reads as a bad token. Start this workspace's own agent, "
                      f"or correct the port")
    return port, None


def reached(session: str, psession: str, workspace: Path | None = None) -> str:
    """Tell this machine's agent it can reach this session, and say how it went.

    A harness that holds no stream is reached by injection, and nothing else asserts that: until
    it does, the session is enrolled, bound, and receives nothing (#663). Never raises — the
    connect above it has already succeeded, and a session told its connect failed re-runs it,
    which mints a new epoch and orphans the reach this was trying to make.
    """
    body = json.dumps({"session": session, "provider_session": psession}).encode()
    url = f"http://127.0.0.1:{AGENT_PORT}/steering/reach"
    req = urllib.request.Request(url, data=body, method="POST",
                                 headers={"Content-Type": "application/json",
                                          KEY_HEADER: agent_key(workspace, AGENT_PORT) or "",
                                          **(workspace_header(workspace) if workspace else {})})
    try:
        # Clear of the agent's own `UPLINK_WAIT`: at the same value, an agent whose uplink is
        # down answers its 409 exactly as this gives up, and the operator is told to start an
        # agent that is already running instead of being told the uplink is the fault.
        with door.send(req, timeout=REACH_WAIT) as r:
            return f"reach: asserted for {psession} at {url}"
    except urllib.error.HTTPError as e:
        why = (e.read() or b"").decode(errors="ignore")[:200]
        return f"reach: {url} refused it ({e.code}) {why}; this session receives nothing until it is asserted"
    except Exception as e:
        return (f"reach: no agent answered at {url} ({type(e).__name__}); "
                f"start one and POST {{\"session\": \"{session}\", \"provider_session\": \"{psession}\"}} to it "
                f"with {KEY_HEADER} from the key file the workspace's STEERING_AGENT_KEY_FILE record names, "
                f"or this session receives nothing")


# How each harness refreshes this plugin from its marketplace (#1795).
UPDATE = {"claude": "claude plugin update 2mw2lt@2mw2lt", "codex": "codex plugin add 2mw2lt@2mw2lt"}


def _release(v: str) -> tuple[int, ...] | None:
    parts = v.split(".")
    return tuple(int(p) for p in parts) if parts and all(p.isdigit() for p in parts) else None


def plugin_line(provider: str | None, mine_file: Path = HERE.parent.parent / "version.txt") -> str | None:
    """This plugin's version, and whether the orchestrator runs a newer release (#1795).

    A plugin is fetched once and then runs as it was: Codex ran 0.5.88 for weeks while the
    orchestrator moved on, and nothing said so. None when this copy names no version."""
    try:
        mine = mine_file.read_text().strip()
    except OSError:
        return None
    try:
        code, body = door.get("/steering/authorities", timeout=5)
        release = json.loads(body).get("release") if code == 200 else None
    except Exception:  # after a bind that succeeded: a failed comparison must not read as a failed connect
        release = None
    a, b = _release(mine), _release(str(release or ""))
    if a is not None and b is not None and a < b:
        fix = UPDATE.get(provider or "")
        return (f"plugin: {mine} is behind the orchestrator's {release}"
                + (f"; update it: {fix}" if fix else ""))
    return f"plugin: {mine}"


def main(argv: list[str]) -> int:
    try:
        args = current_args("connect", argv)
    except ValueError as why:
        return error("connect", str(why))
    if help_requested("connect", argv):
        print(script_help("connect"))
        return 0
    try:
        o = options(args)
        h = harness_of(o.provider)
    except Refused as why:
        return error("connect", str(why))
    session, acct = o.session, account(o, h)

    ws = required_workspace_root(project_dir(), timeout=2.0)
    # A plugin update moves the scripts and leaves the workspace's hooks pinned to the version
    # it replaced, whose observations this connect does not recognise as its own (#340). Only
    # the harness whose hooks they are has any to repin.
    repinned = hooks.repin(ws) if h.hooks else None
    if repinned:
        print(f"repinned: {repinned}")
    if h.hooks:
        settings = hooks.load(hooks.settings_path(ws))
        wired = any(hooks.wired_by(entry)
                    for entries in settings.get("hooks", {}).values() for entry in entries)
        if not wired:
            print(f"this workspace has no observe hook; run python3 {HERE / 'hooks.py'} install {ws}",
                  file=sys.stderr)
            return 1
    try:
        session, answer, rid, psession = connect(ws, session, acct, o.doing, h, o.psession, o.pid)
    except (Refused, Undetermined) as why:
        print(str(why), file=sys.stderr); return 1
    print(f"{session}: {answer}")
    if not answer.startswith("bound:"):
        return 1
    # The account a board verb names this session by. Derived here and nowhere a session can
    # read, so one that had to announce itself guessed — which is the defect #139 replaced.
    # Not printed as `STEERING_ACCOUNT=`: that variable is read back above as an override, so a
    # session exporting what it was told would pin a derivation rather than repeat it.
    if acct:
        print(f"account: {acct}")
    print(f"speak: {say_invocation(h, psession, session)}")
    # The stream this session holds at its agent names the incarnation it belongs to, and this
    # is the only place the recipe derives it.
    print(f"STEERING_RUNTIME_ID={rid}")
    try:
        _, now_rid, pid = incarnation_of(psession, o.pid, h.provider)
    except (Refused, Undetermined):
        now_rid = pid = None
    if now_rid == rid:
        why = hand_over_pane(ws, h.provider, psession, rid, pid, project_dir())
        if why:
            print(why, file=sys.stderr)
    global AGENT_PORT
    AGENT_PORT = agent_port(ws)
    port, why = agent_says(AGENT_PORT, ws, session)
    if port:
        print(f"STEERING_AGENT_PORT={port}")
    if why:
        print(why, file=sys.stderr)
    line = plugin_line(h.provider)
    if line:
        print(line, file=sys.stderr if " is behind " in line else sys.stdout)
    if not h.holds:
        print(reached(session, psession, ws) if port else
              f"reach: not asserted, there being no agent here for {ws}; "
              f"this session receives nothing until there is")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
