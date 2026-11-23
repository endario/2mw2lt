"""The Antigravity desktop app's hook dialect (10357 design §3.5). The app runs the plugin's hooks as
converted from Claude Code's, unchanged, so a hook reads its payload through `claude()` and answers
through `refuse()`, `granted()` and `stop()`, which speak whichever dialect asked.

What the app sends and takes was recorded from it at 2.34.0 (`steering/test/fixtures/
antigravity-hooks-2.34.0.ndjson`, RECORD R-P2–R-P10): a camelCase
payload naming its `conversationId`, a `toolCall` of `run_command` with its `CommandLine`, and a
`Stop` counting its continuations in `executionNum`; a refusal is `deny` with its reason, a pass is
nothing at all (`{}` refuses every call), and a refused turn's end is `continue`.
"""
from __future__ import annotations

import json
import sys

# The app's tool names, as Claude Code's guards know them.
TOOLS = {"run_command": "Bash"}


def is_app(payload: object) -> bool:
    """Whether payload is the app's."""
    return isinstance(payload, dict) and "conversationId" in payload and "hookEventName" in payload


def claude(payload: dict) -> dict:
    """payload as Claude Code would have sent it; Claude Code's own as it is."""
    if not is_app(payload):
        return payload
    out = {"hook_event_name": payload.get("hookEventName"), "session_id": payload.get("conversationId"),
           "cwd": payload.get("cwd"), "transcript_path": payload.get("transcriptPath")}
    call = payload.get("toolCall")
    if isinstance(call, dict):
        args = call.get("args") if isinstance(call.get("args"), dict) else {}
        out["tool_name"] = TOOLS.get(call.get("name"), call.get("name"))
        if isinstance(args.get("CommandLine"), str):
            out["tool_input"] = {"command": args["CommandLine"]}
    if "executionNum" in payload:
        # The app never sets its documented continuation flag; it counts continuations (R-P10).
        num = payload.get("executionNum")
        out["stop_hook_active"] = isinstance(num, int) and num > 0
    return out


def refuse(payload: object, why: str) -> int:
    """Refuse a tool call, the exit code the hook returns: the app reads `deny` and its reason on
    stdout (R-P8); Claude Code the reason on stderr beside exit 2."""
    if is_app(payload):
        json.dump({"decision": "deny", "reason": why}, sys.stdout)
        return 0
    print(why, file=sys.stderr)
    return 2


def granted(payload: object, grant: dict) -> None:
    """Say a guard's grant. The app has no grant to say: its own policy decides once a guard passes
    a call, and a pass is said by nothing (R-P7)."""
    if not is_app(payload):
        print(json.dumps(grant))


def stop(payload: object, why: str) -> dict:
    """A Stop's refusal of the turn's end: `continue` re-enters the app's loop (R-P10), `block`
    Claude Code's."""
    return {"decision": "continue" if is_app(payload) else "block", "reason": why}
