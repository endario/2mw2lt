#!/usr/bin/env python3
"""The zcode plugin's hook glue (#6257): publish the binding, observe, and point at waiting
frames.

The hooks are the only processes a zcode session gives that know its id (measured:
`ZCODE_SESSION_ID` reaches hooks and stops there — tool children and the plugin's MCP server
get none), so every event publishes the binding the rest of the glue resolves by. The pointer
rides the event boundaries: mid-turn as PostToolUse context, at a turn's end as a Stop
continuation (the harness bounds continuations at three, measured), and on a person's next
prompt as UserPromptSubmit context. A dark session is the platform's to wake — no hook or
server can. A failure exits 0: a hook that answers nothing costs a pointer, never a turn. A
sandboxed launch's SessionStart says why its session is not enrolled, in the session and on stderr.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "steering"))
import python_floor  # noqa: E402
python_floor.hook(blocking=False)  # UserPromptSubmit among its events: a 2 erases the prompt

import json
import os

ROOT = Path(os.environ.get("ZCODE_PLUGIN_ROOT") or Path(__file__).resolve().parents[1])
sys.path.insert(0, str(ROOT / "steering"))
sys.path.insert(0, str(ROOT / "steering" / "enroll"))

import holder  # noqa: E402
from local_workspace import workspace_root  # noqa: E402
import zcode_binding  # noqa: E402

POINTER = ("A steering frame is waiting. Call mcp__plugin_2mw2lt_frames__frames to read it.")


def proof_failed(why: str) -> None:
    """Writes why a restricted SessionStart could not publish its model proof where the launch reads it,
    whole or not at: the launch refuses on it at once rather than waiting out its deadline (#9799). The
    reason names a kind and a step, never a value read from the session or its error's text. The first
    reason said stands: a later catch-all never hides the step that failed."""
    target = Path(os.environ["STEERING_MODEL_PROOF"]) / "zcode-hook-error.json"
    if target.exists():
        return
    staged = target.with_name(f".{target.name}.{os.getpid()}")
    try:
        staged.write_text(json.dumps({"error": why}))
        os.replace(staged, target)
    except OSError as err:
        # Nowhere the launch reads is left to say it; ZCode keeps a hook's stderr in its own log.
        print(f"2mw2lt: the model proof's failure could not be written ({type(err).__name__}): {why}", file=sys.stderr)


def error_kind(err: BaseException) -> str:
    """An error by its kind, and an OS error's reason, never its message: a message may carry a path or a value."""
    why = type(err).__name__
    if isinstance(err, OSError) and err.strerror:
        why += ": " + err.strerror
    return why


def trailer(payload: dict) -> str | None:
    """The commit trailer naming the model this session runs on, from the SessionStart payload's
    `model` (`<provider>/<model>`, the only event that carries it). ZCode credits no model itself,
    so a session copied the Claude trailer it read and a GLM commit named Claude Opus (#7071)."""
    model = payload.get("model")
    if not isinstance(model, str) or not model.rpartition("/")[2].strip():
        return None
    name = model.rpartition("/")[2].strip().lower()
    return (f"Credit your commits here with the trailer `Assisted-by: ZCode ({name})` on its own "
            "line, never a Co-Authored-By naming a model this session is not.")


def event_of(payload: dict) -> str | None:
    """The firing event, from either spelling the payload carries (measured: both, always)."""
    named = payload.get("hook_event_name") or payload.get("hookEventName")
    return named if isinstance(named, str) else None


def psession_of(payload: dict) -> str | None:
    for key in ("session_id", "sessionId"):
        value = payload.get(key)
        if isinstance(value, str) and value:
            return value
    env = os.environ.get("ZCODE_SESSION_ID")
    return env or None


LAUNCH_BEARER = "STEERING_LAUNCH_BEARER"
ENROL_WAIT = 20.0   # with observe's 6s, inside the SessionStart hook's own 30s
ENROL_FAILED_MAX = 1024   # what the agent reads of it (zcode.EnrolFailedMax)


def enrol_launch(ws: Path, psession: str) -> str | None:
    """A sandboxed launch names no conversation before its first turn, whose SessionStart holds the
    turn until this hook returns: so the hook asks its agent, on the launch's bearer,
    to enrol the session the binding it just published names, and the connect that turn carries
    finds its session enrolled. None once enrolled, or for a launch with no bearer; otherwise why
    not, which the session is told."""
    bearer = os.environ.get(LAUNCH_BEARER)
    if not bearer:
        return None
    import urllib.error
    import urllib.request
    from local_workspace import agent_port
    port = agent_port(ws)
    if not port.isdigit():
        return "this sandboxed launch's agent could not be asked to enrol it: STEERING_AGENT_PORT names no port"
    req = urllib.request.Request(f"http://127.0.0.1:{port}/steering/launch-enrolment",
                                 data=json.dumps({"session": psession}).encode(), method="POST",
                                 headers={"Authorization": f"Bearer {bearer}", "Content-Type": "application/json"})
    # Loopback, never a proxy's: a host's system proxy captures it (502). Never redirected either:
    # urllib would carry the bearer to wherever a redirect points.
    class Unredirected(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *_):
            return None
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), Unredirected())
    asking = "this sandboxed launch's agent could not be asked to enrol it: "
    try:
        with opener.open(req, timeout=ENROL_WAIT) as answered:
            raw = answered.read()
    except urllib.error.HTTPError as err:
        if 300 <= err.code < 400:
            return f"{asking}it answered a redirect ({err.code}), which the bearer is never sent after"
        try:
            said = json.loads(err.read())
        except OSError as read:
            return f"this sandboxed launch's agent refused to enrol it ({err.code}), and its reason could not be read: {error_kind(read)}"
        except ValueError:
            return f"this sandboxed launch's agent refused to enrol it ({err.code}), with a reason that is not JSON"
        why = said.get("refused") if isinstance(said, dict) else None
        return f"this sandboxed launch's agent refused to enrol it ({err.code}): {why or 'it gave no reason'}"
    except urllib.error.URLError as err:
        cause = err.reason if isinstance(err.reason, BaseException) else None
        return asking + (error_kind(cause) if cause else "its address could not be reached")
    except OSError as err:
        return asking + error_kind(err)
    try:
        enrolled = json.loads(raw).get("session")
    except (ValueError, AttributeError):
        enrolled = None
    if not isinstance(enrolled, str) or not enrolled:
        return "this sandboxed launch's agent answered without naming the session it enrolled"
    return None


def launch_failed(why: str) -> str | None:
    """Says why a sandboxed launch's hook could not have its session enrolled, in ZCode's log of the
    hook's stderr, and answers it for the session; None, saying nothing, outside such a launch, where
    a hook that answers nothing costs a pointer only."""
    if not os.environ.get(LAUNCH_BEARER):
        return None
    why = "2mw2lt: this sandboxed launch's session is not enrolled: " + why
    print(why, file=sys.stderr)
    # The start waits for a hold, and its agent may never have heard: the reason is left beside the
    # binding, under the served root, for the start to end on. Keyed by the parent as the binding is.
    served = os.environ.get("STEERING_WORKSPACE")
    if served:
        try:
            path = zcode_binding.enrol_failed_path(Path(served), os.getppid())
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            path.write_text(why[:ENROL_FAILED_MAX])
        except Exception as err:
            print("2mw2lt: why could not be left for the launch's start: " + error_kind(err), file=sys.stderr)
    return why


def say(what: dict) -> None:
    sys.stdout.write(json.dumps(what) + "\n")
    sys.stdout.flush()


def observe(argv: list[str], payload: dict) -> str | None:
    """The payload through the sender, as the Claude plugin's own hooks run it, and the notice
    it answered (a stream-down state, its remedy) for the glue to speak beside its own."""
    import subprocess
    try:
        answered = subprocess.run(["python3", str(ROOT / "steering/enroll/observe.py"), *argv],
                                  input=json.dumps(payload), capture_output=True, text=True,
                                  timeout=6)
    except (OSError, subprocess.TimeoutExpired):
        return None
    try:
        out = json.loads(answered.stdout)
    except ValueError:
        return None
    notice = out.get("hookSpecificOutput", {}).get("additionalContext") if isinstance(out, dict) else None
    return notice if isinstance(notice, str) and notice else None


def main(argv: list[str] | None = None) -> int:
    # The event a hook command names in its own argv, as hooks.json writes SessionStart's: what the
    # glue was invoked for before any input is read, so an input that cannot be read still names
    # the step a restricted launch's proof failed at.
    invoked = (sys.argv[1:] if argv is None else argv)[:1]
    proving = False
    try:
        proving = invoked == ["SessionStart"] and bool(os.environ.get("STEERING_MODEL_PROOF"))
        try:
            payload = json.load(sys.stdin)
        except ValueError:
            payload = None
        event = event_of(payload) if isinstance(payload, dict) else None
        if invoked == ["SessionStart"] and event not in (None, "SessionStart") and os.environ.get(LAUNCH_BEARER):
            say({"additionalContext": launch_failed(f"the hook was invoked for SessionStart and given a {event}")})
            return 0
        if event is None:
            if proving:
                proof_failed("the SessionStart's input could not be read")
            if invoked == ["SessionStart"] and (said := launch_failed("the SessionStart's input could not be read")):
                say({"additionalContext": said})
            return 0
        # A restricted launch waits on this SessionStart's proof: each step that keeps it from being
        # published says why, so the launch is refused at once rather than at its deadline. No other
        # event speaks for the proof, so none can mask one already published.
        proving = event == "SessionStart" and bool(os.environ.get("STEERING_MODEL_PROOF"))
        psession = psession_of(payload)
        if psession is None:
            if proving:
                proof_failed("the SessionStart named no session")
            if event == "SessionStart" and (said := launch_failed("the SessionStart named no session")):
                say({"additionalContext": said})
            return 0
        # Why a sandboxed launch's session is not enrolled: its SessionStart alone asks.
        unenrolled, starting = None, event == "SessionStart"
        cwd = payload.get("cwd") or os.environ.get("ZCODE_PROJECT_DIR") or os.getcwd()
        try:
            ws = workspace_root(Path(cwd), timeout=1.0)
        except Exception as err:
            ws = None                       # outside a workspace the glue has nothing to say
            if proving:
                proof_failed("the session's workspace could not be read: " + error_kind(err))
            if starting:
                unenrolled = launch_failed("the session's workspace could not be read: " + error_kind(err))
        else:
            if ws is None and proving:
                proof_failed("the session's directory is in no workspace")
            if ws is None and starting:
                unenrolled = launch_failed("the session's directory is in no workspace")
        if ws is not None:
            proof = None
            if proving:
                proof = {key: payload[key] for key in ("model", "source")
                         if isinstance(payload.get(key), str) and payload[key].strip()}
                for key in ("model", "source"):
                    if key not in proof:
                        proof_failed(f"the SessionStart named no {key}")
                        break
            try:
                # A sandboxed launch's cage runs no ps to walk ancestors with, and a hook is a direct
                # child of zcode-cli (measured, 4.0.0-49): its parent is the session's process, which
                # its agent checks outside the cage is the runtime the launch registered.
                parent = os.getppid() if os.environ.get(LAUNCH_BEARER) else None
                zcode_binding.publish(ws, psession, str(cwd), pid=parent, proof=proof)
            except Exception as err:
                # The observation below stands on its own.
                if proving:
                    proof_failed("the binding could not be published: " + error_kind(err))
                if starting:
                    unenrolled = launch_failed("the binding could not be published: " + error_kind(err))
            # Whatever follows, the proof's step is over: a later failure never speaks for it.
            proving = False
        if starting and ws is not None and unenrolled is None:
            if (refused := enrol_launch(ws, psession)) is not None:
                unenrolled = launch_failed(refused)
        notice = observe(["--provider", "zcode"], payload)
        if starting:
            notice = "\n".join(x for x in (unenrolled, trailer(payload) if ws is not None else None, notice) if x) or None
        if ws is None or event not in ("UserPromptSubmit", "PostToolUse", "Stop"):
            if notice:
                say({"additionalContext": notice})
            return 0
        ledger = holder.claim_path(ws, psession).with_suffix(".ledger.json")
        try:
            state = json.loads(ledger.read_text())
            pending = state.get("pending") or []
            inform = state.get("inform") or []
        except (OSError, ValueError):
            pending, inform = [], []
        context = notice
        if any(fid not in inform for fid in pending):
            context = "\n".join(x for x in (POINTER, notice) if x) or None
        if not context:
            return 0
        out = {"additionalContext": context}
        if event == "Stop":
            out["continue"] = True          # bounded by the harness at three (measured)
        say(out)
    except Exception as err:
        # A hook that answers nothing costs a pointer only — and, proving, says what stopped it.
        if proving:
            proof_failed("the hook failed: " + error_kind(err))
        if invoked == ["SessionStart"] and (said := launch_failed("the hook failed: " + error_kind(err))):
            say({"additionalContext": said})
    return 0


if __name__ == "__main__":
    sys.exit(main())
