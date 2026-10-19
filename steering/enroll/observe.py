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

python_floor.require()

import fcntl
import hashlib
import json
import os
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
import verb_help  # noqa: E402
from local_workspace import workspace_root  # noqa: E402
from door import door_url, failure, record, remote, brain_route, send  # noqa: E402
import current  # noqa: E402


BUDGET = 4.0  # seconds, end to end


def claude_version(ws: Path, deadline: float) -> str:
    """`claude --version`, cached in the spool directory for a day; on a cache miss the lookup
    gets what is left of the budget minus what admission needs, never more than one second."""
    cache = spool.spool_dir(ws) / ".claude-version"
    try:
        if time.time() - cache.stat().st_mtime < 86400:
            cached = cache.read_text().strip()
            if supervision._version(cached) >= supervision.MIN_VERSION:
                return cached  # anything else — empty, torn, below the floor — is a miss
    except OSError:
        pass
    v = os.environ.get("STEERING_CLAUDE_VERSION")
    if not v:
        left = min(1.0, deadline - time.monotonic() - 1.5)
        try:
            v = subprocess.run(["claude", "--version"], capture_output=True, text=True, timeout=max(left, 0.1)).stdout.split()[0]
        except (OSError, subprocess.TimeoutExpired, IndexError):
            return "0.0.0"  # below the floor, so the event is not admitted — and never cached
    try:  # published atomically: a hook killed mid-write must not leave a truncated cache behind
        cache.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        spool.write_atomic(cache, v)
    except OSError:
        pass
    return v


# The variable each non-Claude hook runtime states its version in, read from the hook's own env
# rather than by running the CLI inside the hook's budget.
HOOK_VERSION_ENV = {"zcode": "ZCODE_APP_VERSION"}


def hook_version(source: str) -> str:
    return os.environ.get(HOOK_VERSION_ENV.get(source, ""), "").strip()


def harness_pid(source: str, ws: Path, hook: dict) -> int | None:
    """The process the observation's runtime id names: None for Claude, whose hook parent
    `runtime_id` finds itself; for zcode, the pid of the binding the glue published just before
    this ran, which is the process `connect` resolves too. No binding, no runtime id."""
    if source == "claude":
        return None
    import zcode_binding
    try:
        return int(zcode_binding.resolve(ws, cwd=str(hook.get("cwd") or os.getcwd()))["pid"])
    except (zcode_binding.Refused, OSError, ValueError, KeyError, TypeError):
        return None


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
        ws = workspace_root(anchor, timeout=1.0)
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
    try:
        path = spool.admit(ws, o)
    except OSError:
        return finish()
    if path is None:
        return finish()
    left = deadline - time.monotonic()
    if left < 0.2:
        record("observe", f"admitted, no time left to send ({left:.2f}s of {BUDGET})", ws)
        return finish()  # admitted; the sweep delivers it
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
    if ride == AGENT_REFUSED:
        notice = None  # an answering agent is final; the spool and its drain own a refusal
    elif ride is not None:
        said, release = ride
        claimed = spool.claim(path)
        if claimed:
            claimed.unlink()
        keep_release(ws, release)
        notice = received_notice(ws, o, said, time.time())
    else:
        notice = door_send(seated, ws, o, path, left)
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


def agent_send(ws: Path, o: dict, left: float):
    """The observation through this machine's agent, admitted by its key and naming its
    workspace, as the reach and turn routes are; None when no agent answered at all, so the
    caller sends to the door as it always has — an agent that answered is final, its 2xx carrying
    the door's answer and anything else AGENT_REFUSED. The loopback is cheap to fail, so an agent
    that is absent costs the hook milliseconds of its budget, not the send."""
    port = local_workspace.agent_port(ws, timeout=1.0)
    key = local_workspace.agent_key(ws, port)
    if not key:
        return None
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/steering/observe", data=json.dumps(o).encode(),
        headers={"Content-Type": "application/json",
                 local_workspace.KEY_HEADER: key,
                 **local_workspace.workspace_header(str(ws))},
        method="POST")
    try:
        with send(req, timeout=min(2.0, left)) as r:
            return answer_in(r)
    except urllib.error.HTTPError as e:
        record("observe", f"the agent relayed the door's refusal: {failure(e)}", ws)
        return AGENT_REFUSED


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
    stamp = spool.spool_dir(ws) / (".notice-" + hashlib.sha256(key).hexdigest())
    try:
        stamp.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with stamp.open("a+") as file:
            fcntl.flock(file, fcntl.LOCK_EX)
            file.seek(0)
            try:
                previous = float(file.read())
            except ValueError:
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


def answer_in(r) -> tuple[str | None, str | None]:
    """What the daemon told this session about itself in the answer (doc 71), and the release
    the orchestrator runs, as the authorities state it: the release rides the observe answer so
    a hook never spends its budget on a second read for it (#7185). The release is None when the
    answer could not be read at all, and "" when a readable one names none — this orchestrator
    states no release, and no cached one is kept to tell against."""
    try:
        doc = json.loads(r.read() or b"{}")
    except (ValueError, AttributeError, OSError):
        return None, None
    n = doc.get("notice")
    release = doc.get("release")
    return (n if isinstance(n, str) and n else None,
            release if isinstance(release, str) else "")


def keep_release(ws: Path, release: str | None) -> None:
    """The spool's copy of the release the door last stated: written when an answer names one,
    dropped when a readable answer names none, so a build that stamps no release leaves nothing
    stale behind to tell against (#7185). An answer that could not be read keeps what is there."""
    try:
        cache = spool.spool_dir(ws) / ".release"
        if release:
            cache.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            spool.write_atomic(cache, release)
        elif release == "":
            cache.unlink()
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
        release = (spool.spool_dir(ws) / ".release").read_text().strip()
    except OSError:
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
    mark = spool.spool_dir(ws) / (".plugin-told-" + hashlib.sha256(
        json.dumps([o.get("source"), psession, o.get("runtime_id")]).encode()).hexdigest())
    try:
        told = mark.read_text().strip()
    except OSError:
        told = None  # no mark yet: this is the first tell at this version
    if told == mine:
        return notice
    try:
        mark.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        spool.write_atomic(mark, mine)
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
