#!/usr/bin/env python3
"""The zcode plugin's hook glue (#6257): publish the binding, observe, and point at waiting
frames.

The hooks are the only processes a zcode session gives that know its id (measured:
`ZCODE_SESSION_ID` reaches hooks and stops there — tool children and the plugin's MCP server
get none), so every event publishes the binding the rest of the glue resolves by. The pointer
rides the event boundaries: mid-turn as PostToolUse context, at a turn's end as a Stop
continuation (the harness bounds continuations at three, measured), and on a person's next
prompt as UserPromptSubmit context. A dark session is the platform's to wake — no hook or
server can. Every failure exits 0 with no output: a hook that answers nothing costs a pointer,
never a turn.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ROOT = Path(os.environ.get("ZCODE_PLUGIN_ROOT") or Path(__file__).resolve().parents[1])
sys.path.insert(0, str(ROOT / "steering"))
sys.path.insert(0, str(ROOT / "steering" / "enroll"))

import holder  # noqa: E402
from local_workspace import workspace_root  # noqa: E402
import zcode_binding  # noqa: E402

POINTER = ("A steering frame is waiting. Call mcp__plugin_2mw2lt_frames__frames to read it.")


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


def main() -> int:
    try:
        payload = json.load(sys.stdin)
        if not isinstance(payload, dict):
            return 0
        event = event_of(payload)
        if event is None:
            return 0
        psession = psession_of(payload)
        if psession is None:
            return 0
        cwd = payload.get("cwd") or os.environ.get("ZCODE_PROJECT_DIR") or os.getcwd()
        try:
            ws = workspace_root(Path(cwd), timeout=1.0)
        except Exception:
            ws = None                       # outside a workspace the glue has nothing to say
        if ws is not None:
            try:
                zcode_binding.publish(ws, psession, str(cwd))
            except Exception:
                pass                        # the observation below stands on its own
        notice = observe(["--provider", "zcode"], payload)
        if event == "SessionStart" and ws is not None:
            notice = "\n".join(x for x in (trailer(payload), notice) if x) or None
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
    except Exception:
        pass                                # a hook that answers nothing costs a pointer only
    return 0


if __name__ == "__main__":
    sys.exit(main())
