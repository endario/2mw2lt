"""The call a permission ask names (#7837), shared by the hooks that read it: the observe hook keys
its record of the ask by it, and the deciding hook polls for its decision by it."""
from __future__ import annotations

import fcntl
import hashlib
import json
from pathlib import Path

import spool


# Calls kept, of every thread together: a permission request follows its own call's PreToolUse
# by tens of milliseconds, but other threads' calls, and auto-allowed ones of its own, land between.
PRETOOL_KEPT = 32


def stamp_tool(ws: Path, hook: dict) -> None:
    """Claude Code sends no `tool_use_id` on `PermissionRequest` (2.1.304, #7837), so one turn's
    dialogs would fold into one item that closes only at its end. A thread asks one call at a
    time, and the call's own PreToolUse fired just before with the same tool and input: its id is
    the ask's. One file for the workspace, so nothing piles up per session. Best effort; a request
    with nothing to match stays keyed by its prompt."""
    event = hook.get("hook_event_name")
    if event not in ("PreToolUse", "PermissionRequest") or hook.get("tool_use_id") and event == "PermissionRequest":
        return
    try:
        call = [hook.get("session_id"), hook.get("agent_id") or "", hook.get("tool_name"),
                hashlib.sha256(json.dumps(hook.get("tool_input"), sort_keys=True).encode()).hexdigest()]
        path = spool.spool_dir(ws) / ".pretool"
        if event == "PermissionRequest":
            kept = json.loads(path.read_text())
            hook["tool_use_id"] = next(i for i, *c in reversed(kept) if c == call)
            return
        if not isinstance(hook.get("tool_use_id"), str) or not hook["tool_use_id"]:
            return
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with path.with_suffix(".lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                kept = json.loads(path.read_text())
            except (OSError, ValueError):
                kept = []
            spool.write_atomic(path, json.dumps((kept + [[hook["tool_use_id"], *call]])[-PRETOOL_KEPT:]))
    except (OSError, ValueError, TypeError, StopIteration):
        pass
