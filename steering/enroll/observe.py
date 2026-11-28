#!/usr/bin/env python3
"""The sender: a Claude Code command hook (doc 16 §11.2).

Configured for every event doc 15 §3 names, e.g.
  {"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "python3 <repo>/steering/enroll/observe.py", "timeout": 5}]}]}}
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parents[1]))
import python_floor  # noqa: E402
python_floor.hook(blocking=False)  # every admitted event runs it; on PreToolUse a 2 would block the call it only notes

import contextlib
import fcntl
import hashlib
import json
import os
import stat
import subprocess
import time
import urllib.error
import urllib.request
import uuid

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))
import holder  # noqa: E402
import hooks  # noqa: E402
import local_workspace  # noqa: E402
import readout  # noqa: E402
import runtime_id  # noqa: E402
import spool  # noqa: E402
import supervision  # noqa: E402
from pretool import stamp_tool  # noqa: E402
import verb_help  # noqa: E402
from local_workspace import workspace_root  # noqa: E402
from door import door_url, failure, record, remote, brain_route, send  # noqa: E402
import current  # noqa: E402


BUDGET = 4.0  # seconds, end to end


def observe_cache_dir(ws: Path) -> Path:
    """The observe hook's own cache directory: beside the spool, never in it, since a sandboxed
    launch's cage is denied the spool (#10265) while the rest of the workspace's `.claude` stays
    writable. The directory ignores itself, as `steering-pretool` does, so no wired workspace's
    `git add` takes the hook's runtime state in. A symlinked `.claude` is refused rather than
    followed, and every entry is a hint a writable session can shape, never authority."""
    return ws / ".claude" / "steering-observe-cache"


def observe_cache(ws: Path, name: str) -> Path:
    return observe_cache_dir(ws) / name


def _child_dir(parent: int, name: str, create: bool) -> int:
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    try:
        return os.open(name, flags, dir_fd=parent)
    except FileNotFoundError:
        if not create:
            raise
    try:
        os.mkdir(name, 0o700, dir_fd=parent)
    except FileExistsError:
        pass  # a real directory wins the race; a symlink is refused by the open below
    return os.open(name, flags, dir_fd=parent)


@contextlib.contextmanager
def observe_cache_fd(ws: Path, create: bool = False):
    """The cache directory held open as an fd after a no-follow walk. Operations relative to
    it cannot be redirected by a caged writer replacing either directory afterwards."""
    root = os.open(ws, os.O_RDONLY | os.O_DIRECTORY)
    try:
        claude = _child_dir(root, ".claude", create)
        try:
            cache = _child_dir(claude, "steering-observe-cache", create)
        finally:
            os.close(claude)
    finally:
        os.close(root)
    try:
        yield cache
    finally:
        os.close(cache)


def open_cache_file_at(dfd: int, name: str, *, create: bool = False):
    """A cache file as its own regular inode, never the target of a planted link. `O_NOFOLLOW`
    decides at the open, so replacement afterwards cannot move this handle."""
    flags = (os.O_RDWR if create else os.O_RDONLY) | os.O_NOFOLLOW | os.O_NONBLOCK
    if create:
        flags |= os.O_CREAT
    fd = os.open(name, flags, 0o600, dir_fd=dfd)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1:
            raise OSError("the observe cache entry is not one regular file")
        return os.fdopen(fd, "r+" if create else "r")
    except BaseException:
        os.close(fd)
        raise


def write_cache_atomic(dfd: int, name: str, data: str) -> None:
    spool.write_atomic_at(dfd, name, data, mode=0o600)


def read_cache_file_at(dfd: int, name: str) -> str:
    with open_cache_file_at(dfd, name) as file:
        return file.read()


def read_cache_file(ws: Path, name: str) -> str:
    with observe_cache_fd(ws) as dfd:
        return read_cache_file_at(dfd, name)


def ensure_cache_ignore(dfd: int) -> None:
    try:
        if read_cache_file_at(dfd, ".gitignore") == "*\n":
            return
    except (OSError, UnicodeError):
        pass
    write_cache_atomic(dfd, ".gitignore", "*\n")


def write_cache(ws: Path, name: str, data: str) -> None:
    with observe_cache_fd(ws, create=True) as dfd:
        ensure_cache_ignore(dfd)
        write_cache_atomic(dfd, name, data)


def claude_version(ws: Path, deadline: float) -> str:
    """`claude --version`, cached beside the spool for a day; on a cache miss the lookup
    gets what is left of the budget minus what admission needs, never more than one second."""
    try:
        with observe_cache_fd(ws) as dfd, open_cache_file_at(dfd, ".claude-version") as file:
            age = time.time() - os.fstat(file.fileno()).st_mtime
            if 0 <= age < 86400:
                cached = file.read().strip()
                if supervision._version(cached) >= supervision.MIN_VERSION:
                    return cached  # anything else — empty, torn, below the floor — is a miss
    except (OSError, UnicodeError):
        pass
    v = os.environ.get("STEERING_CLAUDE_VERSION")
    if not v:
        left = min(1.0, deadline - time.monotonic() - 1.5)
        try:
            v = subprocess.run(["claude", "--version"], capture_output=True, text=True, timeout=max(left, 0.1)).stdout.split()[0]
        except (OSError, subprocess.TimeoutExpired, IndexError):
            return "0.0.0"  # below the floor, so the event is not admitted — and never cached
    try:  # published atomically: a hook killed mid-write must not leave a truncated cache behind
        write_cache(ws, ".claude-version", v)
    except OSError:
        pass
    return v

# The variable each non-Claude hook runtime this adapter admits states its version in, read
# from the hook's own env rather than by running the CLI inside the hook's budget. The registry
# names no variable, so this assumes `<NAME>_APP_VERSION`: a harness stating it elsewhere has
# every observation refused until the registry declares its variable.
HOOK_VERSION_ENV = {name: f"{name.upper()}_APP_VERSION"
                    for name in supervision.FLOORS if name != "claude"}
_REGISTRY = json.loads(
    (HERE / "harness_identity.json").read_text())["harnesses"]


def hook_version(source: str) -> str:
    return os.environ.get(HOOK_VERSION_ENV.get(source, ""), "").strip()


def _hook_bound_pid(source: str, ws: Path, hook: dict) -> int | None:
    """The pid of the binding the glue published just before this ran — the process `connect`
    resolves too."""
    import zcode_binding
    try:
        return int(zcode_binding.resolve(ws, cwd=str(hook.get("cwd") or os.getcwd()))["pid"])
    except (zcode_binding.Refused, OSError, ValueError, KeyError, TypeError):
        return None


def _lock_rows_pid(source: str, ws: Path, hook: dict) -> int | None:
    """The process holding the lock the source's registry rows name for the hook's session."""
    import machine_harness
    h = machine_harness.of(source)
    sid = hook.get("session_id")
    if h is None or h.harness_pid is None or not isinstance(sid, str) or not sid:
        return None
    try:
        pid = h.harness_pid(sid)
    except Exception:
        return None
    return pid if isinstance(pid, int) else None


# How a source's observation names its runtime's process, by the session binding the registry
# declares for it. No binding, no runtime id.
_BINDING_PID = {"hook-binding": _hook_bound_pid, "lock-rows": _lock_rows_pid}


def harness_pid(source: str, ws: Path, hook: dict) -> int | None:
    """The process the observation's runtime id names: None for a source the registry declares
    no session binding for — Claude's hook parent `runtime_id` finds itself — and the binding
    kind's own answer otherwise."""
    kind = next((e.get("session_binding") for e in _REGISTRY if e["name"] == source), None)
    reader = _BINDING_PID.get(kind or "")
    return reader(source, ws, hook) if reader else None


def main() -> int:
    argv = sys.argv[1:]
    if verb_help.help_requested("observe", argv, bare=False):
        print(verb_help.script_help("observe")); return 0
    source = "claude"
    if argv[:1] == ["--provider"] and len(argv) == 2 and argv[1] in supervision.FLOORS:
        source, argv = argv[1], []
    if argv:
        return verb_help.error("observe", f"unexpected arguments: {' '.join(argv)}")
    deadline = time.monotonic() + BUDGET
    try:
        hook = json.load(sys.stdin)
    except ValueError:
        return finish()
    anchor = Path(os.environ.get("CLAUDE_PROJECT_DIR") or hook.get("cwd") or os.getcwd())
    try:
        ws = workspace_root(anchor, timeout=2.0)
    except Exception:
        return finish()
    try:
        # Every hook event reaches here, which makes this the highest-frequency point to keep
        # the workspace launcher current — self-healing the #1875 round-3 gap where a launcher
        # wired before `exec` existed would otherwise sit stale until something else happened to
        # repin it. Best-effort: a malformed settings.local.json or unreadable launcher must not
        # cost the observation this hook exists to send.
        hooks.repin(ws)
    except Exception:
        pass
    stamp_tool(ws, hook)
    version = claude_version(ws, deadline) if source == "claude" else hook_version(source)
    o = supervision.observe(hook, str(uuid.uuid4()), time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                            version, None, source)
    if o is None:
        return finish()
    if o["event"] == "Stop":
        try:
            # zcode's transcript is synthesized per hook and gone after it; its Stop payload
            # carries the reply inline.
            h = supervision.head(readout.last_assistant_text(hook) if source == "claude"
                                 else hook.get("last_assistant_message"))
            if h:
                o["reply_head"] = h
        except Exception:
            pass  # no reply head: the observation still stands
    # Only this side sees whether the plugin's module holds the stream; the daemon's stream-down
    # notice follows what it says (#5722). A zcode session has no hold of its own to arm: its
    # plugin's server is the only holder, so a stream that is down is still the plugin's.
    o["holder"] = ("plugin" if source == "zcode" or holder.fresh(ws, str(o.get("session") or ""))
                   else "session")
    try:
        pid = harness_pid(source, ws, hook)
        rid = (runtime_id.derive(source, pid, timeout=min(1.0, max(0.1, deadline - time.monotonic() - 1.5)))
               if source == "claude" or pid is not None else None)
        if rid:
            o["runtime_id"] = rid
    except Exception:
        pass  # no runtime: admitted, unattributed
    # A sandboxed launch's cage is denied the spool (#10265): its observation goes to its agent on
    # the launch's bearer, which spools it once it is this launch's own, and never to the door.
    launch = bool(os.environ.get("STEERING_LAUNCH_BEARER"))
    path = None
    if not launch:
        try:
            path = spool.admit(ws, o)
        except OSError:
            return finish()
        if path is None:
            return finish()
    left = deadline - time.monotonic()
    if left < 0.2:
        record("observe", f"{'not sent' if launch else 'admitted'}, no time left to send ({left:.2f}s of {BUDGET})", ws)
        return finish()  # admitted, the sweep delivers it; a launch's had no spool to wait in
    seated = brain_route("observe")
    notice = None
    # This machine's agent relays an enrolled session's observation to the door on its warm
    # pooled connection (#7491): the hook stops paying startup, DNS, TCP and a fresh TLS handshake
    # inside its budget. No agent answering — none installed, between releases, or refusing —
    # falls through to the door exactly as this hook always sent.
    ride = None
    if seated is None:
        try:
            ride = agent_send(ws, o, left)
        except Exception as e:  # no HTTP answer at all: the door is the fallback it always was
            record("observe", f"agent relay not taken: {failure(e)}", ws)
    if ride in (AGENT_REFUSED, AGENT_HELD):
        notice = None  # an agent that took the post is final; the spool and its drain own the rest
    elif ride is not None:
        said, release, relaying = ride
        if not relaying and path is not None:  # a relay still sending owns the file, and puts it back if it fails
            claimed = spool.claim(path)
            if claimed:
                claimed.unlink()
        keep_release(ws, release)
        notice = received_notice(ws, o, said, time.time())
    elif launch:
        record("observe", "no agent took a sandboxed launch's observation, which reaches only its agent", ws)
    else:
        notice = door_send(seated, ws, o, path, deadline - time.monotonic())
    # A turn that runs long reaches no Stop, so its tool calls carry the reading (#1159).
    left = deadline - time.monotonic()
    if source == "claude" and o["event"] == "PostToolUse" and left > 0.3:
        try:
            if readout.due(ws, hook, time.time()):
                readout.midturn(hook, ws, o.get("runtime_id"), min(1.5, left))
        except Exception as e:  # a reading is worth one attempt; the next window brings another
            record("readout", f"mid-turn post failed: {failure(e)}", ws)
    notice = plugin_notice(ws, o, notice)
    notice = behind_notice(ws, o, notice)
    return finish(o["event"], notice) if notice else finish()


# An answering agent's non-2xx: its status is the door's, relayed verbatim, and posting the same
# occurrence again through the door would duplicate a refusal the ledger already keeps. The
# observation stays spooled, and the drain owns refusals as it always has.
AGENT_REFUSED = "agent-relay-refused"
# An agent that took the post but had not answered when this hook's patience ran out: falling back
# cost the session two more seconds per hook on the M4's degraded route and raced the relay with a
# second write of the same occurrence.
AGENT_HELD = "agent-relay-held"
# The events whose answer is told in the turn that raised them. Every other observation asks the
# agent not to wait on the door (#8259): its lane sends it in order, and a notice its answer
# carries reaches the session's next hook of the same event, so a tool call costs no door round
# trip.
ANSWERED_IN_TURN = ("UserPromptSubmit",)


def agent_send(ws: Path, o: dict, left: float):
    """The observation through this machine's agent, admitted by its key and naming its
    workspace, as the reach and turn routes are, or a sandboxed launch's on its bearer, which the
    agent spools once it is the launch's own (#10265); None when no agent answered at all, so the
    caller sends to the door as it always has — an agent that answered is final, its 2xx carrying
    the door's answer and anything else AGENT_REFUSED, and one still forwarding when the wait ends
AGENT_HELD. A 202 is the agent still sending, with the door's last answer to this event. The
    loopback is cheap to fail, so an agent that is absent costs the hook milliseconds of its
    budget, not the send."""
    # A launch's observation rides its bearer, which the agent spools once it is the launch's own
    # (#10265, §6 step 2); any other hook's, on the agent's key, was spooled before it posted.
    req = local_workspace.agent_request(ws, "/steering/observe", o, timeout=1.0)
    if req is None:
        return None
    if o.get("event") not in ANSWERED_IN_TURN:
        req.add_header("Prefer", "respond-async")
    try:
        with send(req, timeout=min(2.0, left)) as r:
            relaying = r.status == 202
            return (*answer_in(r, unstated=None if relaying else ""), relaying)
    except urllib.error.HTTPError as e:
        record("observe", f"the agent relayed the door's refusal: {failure(e)}", ws)
        return AGENT_REFUSED
    except TimeoutError as e:  # urllib wraps a connect's timeout in URLError; a bare one is the answer's
        record("observe", f"the agent held the observation past the hook's wait; it stays spooled: {failure(e)}", ws)
        return AGENT_HELD


def door_send(seated, ws: Path, o: dict, path, left: float) -> str | None:
    """The observation to the door itself, as this hook always sent it: a caged brain's own
    loopback route, else the door on the workspace's observe token or the machine's credential."""
    base = seated[0] if seated else f"{door_url()}/steering/observe"
    sent_at = time.monotonic(); notice = None
    try:
        headers = {"Content-Type": "application/json"}
        if seated:
            headers.update(seated[1])
        elif not remote():
            token = (ws / ".claude" / "steering-observe-token").read_text().strip()
            if not token:
                return None
            headers["X-Steering-Observe"] = token
        req = urllib.request.Request(base, data=json.dumps(o).encode(), headers=headers, method="POST")
        with send(req, timeout=min(2.0, left)) as r:
            if 200 <= r.status < 300:
                claimed = spool.claim(path)
                if claimed:
                    claimed.unlink()
                said, release = answer_in(r)
                keep_release(ws, release)
                notice = received_notice(ws, o, said, time.time())
    except Exception as e:  # the sweep owns whatever stays; the hook log keeps why
        record("observe", f"send to {base} failed after {time.monotonic() - sent_at:.2f}s: {failure(e)}", ws)
        try:
            from ack import records
            connected = any(rec.get("provider_session") == o["session"] for rec in records(ws).values())
            if connected and path.exists():
                notice = received_notice(ws, o, f"The observation send to steering failed for this session ({type(e).__name__}); it remains spooled for retry.", time.time())
        except Exception:
            pass  # The observation stays spooled even when its local enrolment cannot be read.
    return notice


def received_notice(ws: Path, o: dict, notice: str | None, now: float) -> str | None:
    """The hook's notice window; an unseen spool answer never runs this."""
    if not notice or o.get("event") not in ("UserPromptSubmit", "PostToolUse"):
        return None
    key = json.dumps([o.get("source"), o.get("session"), o.get("runtime_id")]).encode()
    stamp = ".notice-" + hashlib.sha256(key).hexdigest()
    try:
        with observe_cache_fd(ws, create=True) as dfd:
            ensure_cache_ignore(dfd)
            with open_cache_file_at(dfd, stamp, create=True) as file:
                fcntl.flock(file, fcntl.LOCK_EX | fcntl.LOCK_NB)
                file.seek(0)
                try:
                    previous = float(file.read())
                except (ValueError, UnicodeError):
                    previous = None
                if o["event"] != "UserPromptSubmit" and previous is not None and 0 <= now - previous < 300:
                    return None
                file.seek(0)
                file.truncate()
                file.write(str(now))
    except OSError:
        pass  # The notice still reaches the session when its local window cannot be kept.
    return notice


STREAM_DOWN = "Your steering stream is down as "


def plugin_notice(ws: Path, o: dict, notice: str | None) -> str | None:
    """For a session the plugin holds, its own account in place of the daemon's stream-down
    notice, whose remedy (arm a hold) would revoke the plugin's. It speaks on a bad state, when
    that changes or a person prompts, and once on recovery; it is silent while the plugin holds
    (owner ruling, 2026-10-11, #5641). `hold.py --plugin-state` gives the state on demand."""
    psession = str(o.get("session") or "")
    if o.get("event") not in ("UserPromptSubmit", "PostToolUse"):
        return notice
    hold = holder.state(ws, psession)
    if hold is None:
        if notice and notice.startswith(STREAM_DOWN) and holder.fresh(ws, psession):
            return ("Your steering stream is down; messages remain queued. "
                    + (holder.unstarted(ws, psession) or "The plugin owns reconnection; do not arm another hold. "
                                                         "Its hold state has not been reported yet."))
        return notice
    if notice and notice.startswith(STREAM_DOWN):
        notice = None
    bad = hold.get("state") in ("retrying", "stopped")
    key = f"{hold.get('state')}:{hold.get('refusal') or ''}" if bad else "holding"
    mark = holder.claim_path(ws, psession).with_suffix(".said")
    try:
        last = mark.read_text()
    except OSError:
        last = ""
    if bad and (key != last or o["event"] == "UserPromptSubmit"):
        line = holder.said(hold)
    elif not bad and last and last != "holding" and hold.get("state") == "holding":
        line = holder.said(hold).replace("holds this session's stream", "holds this session's stream again", 1)
    else:
        line = None
    if line:
        try:
            mark.write_text(key)
        except OSError:
            pass   # said again next time: a missed mark costs a repeat, never a silence
    return "\n".join(x for x in (line, notice) if x) or None


def answer_in(r, unstated: str | None = "") -> tuple[str | None, str | None]:
    """What the daemon told this session about itself in the answer (doc 71), and the release
    the orchestrator runs, as the authorities state it: the release rides the observe answer so
    a hook never spends its budget on a second read for it (#7185). The release is None when the
    answer could not be read at all, and `unstated` when a readable one names none: "" from the
    door — this orchestrator states no release, and no cached one is kept to tell against — and
    None from a relay that has not yet heard the door's."""
    try:
        doc = json.loads(r.read() or b"{}")
    except (ValueError, AttributeError, OSError):
        return None, None
    n = doc.get("notice")
    release = doc.get("release")
    return (n if isinstance(n, str) and n else None,
            release if isinstance(release, str) else unstated)


def keep_release(ws: Path, release: str | None) -> None:
    """The hook cache's copy of the release the door last stated: written when an answer names one,
    dropped when a readable answer names none, so a build that stamps no release leaves nothing
    stale behind to tell against (#7185). An answer that could not be read keeps what is there."""
    try:
        if release:
            write_cache(ws, ".release", release)
        elif release == "":
            with observe_cache_fd(ws) as dfd:
                os.unlink(".release", dir_fd=dfd)
    except OSError:
        pass  # a stale cached release names a release still ahead of this pack


def behind_notice(ws: Path, o: dict, notice: str | None) -> str | None:
    """A session the agent cannot restart is told, once, its plugin is behind (#7185). The
    harness pinned the pack it loaded at start, so however current the scripts a release
    forwards to, the session's own module and skill text stay the version it began on until it
    is resumed on a newer install. This hook is invoked from that pack, so it is the one that
    knows the version; a session the machine's launch record holds is the agent's to restart —
    parked and woken at its next idle moment — and is not this tell's. Once per the version the
    session runs: it is told again only once it has updated and still trails the release."""
    if o.get("event") not in ("UserPromptSubmit", "PostToolUse"):
        return notice
    fix = current.UPDATE.get(str(o.get("source") or ""))
    mine = None if os.environ.get("STEERING_PLUGIN_PIN") else current.invoked()
    if not fix or not mine:
        return notice
    try:
        release = read_cache_file(ws, ".release").strip()
    except (OSError, UnicodeError):
        return notice
    if not current.newer(release, mine):
        return notice
    psession = str(o.get("session") or "")
    try:
        from ack import records
        enrolled = [name for name, rec in records(ws).items()
                    if rec.get("provider_session") == psession]
    except Exception:
        enrolled = []
    try:
        launched = json.loads((ws / ".claude" / "steering-launched.json").read_text())
    except (OSError, ValueError):
        launched = {}
    for name in enrolled:
        # A row keyed by the session's own name is a launch that enrolled; a key that is the
        # tmux name (its row's own tmux) is still starting, and no session's to restart yet.
        row = launched.get(name)
        if isinstance(row, dict) and name != row.get("tmux"):
            return notice
    mark = ".plugin-told-" + hashlib.sha256(
        json.dumps([o.get("source"), psession, o.get("runtime_id")]).encode()).hexdigest()
    try:
        told = read_cache_file(ws, mark).strip()
    except (OSError, UnicodeError):
        told = None  # no mark yet: this is the first tell at this version
    if told == mine:
        return notice
    try:
        write_cache(ws, mark, mine)
    except OSError:
        pass  # a missed mark costs a repeat, never a silence
    line = (f"This session's plugin is behind: it runs {mine} while the release is {release}. "
            f"One action takes it — {fix} — then resume this session; until it does, its skill "
            f"text and module stay {mine}.")
    return "\n".join(x for x in (notice, line) if x) or None


def finish(event: str | None = None, notice: str | None = None) -> int:
    out = ({"hookSpecificOutput": {"hookEventName": event, "additionalContext": notice}}
           if event and notice else {})
    sys.stdout.write(json.dumps(out) + "\n"); sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
