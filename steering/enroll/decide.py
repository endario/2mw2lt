#!/usr/bin/env python3
"""The deciding hook: a Claude Code `PermissionRequest` command hook beside the observe hook
(#4823, design §6.1 and §8.2). The terminal draws its dialog while this waits, and whichever
answers first wins (spike B). It long-polls this machine's agent for the ask's decision:

  GET /steering/surface/ask?session=<the hook's session_id>&ask=<the call's tool_use_id>

and the agent answers one of

  {"decision": "pending"}                                     poll again
  {"decision": "settled"}                                     answered elsewhere: decide nothing
  {"decision": "allow"}
  {"decision": "deny", "message": "...", "interrupt": true}   interrupt is the terminal's "No"
  {"decision": "answer", "answers": {"<question>": "<answer>"}}   AskUserQuestion only

Anything else, an agent that does not answer and the wait running out included, exits 0 with no
output, which leaves the dialog to the person. Never exit 2 here: for this event it denies.
"""
from __future__ import annotations

import json
import os
import sys
import socket
import time
import urllib.parse
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE / "enroll"))
import local_workspace  # noqa: E402
import pretool  # noqa: E402
import verb_help  # noqa: E402
import urllib.error  # noqa: E402
from door import record, send  # noqa: E402
from local_workspace import workspace_root  # noqa: E402

# Under the settings timeout `hooks.DECIDE_TIMEOUT`, so the hook ends by itself rather than by
# the harness's kill. Phase 3.6 measures the longest workable wait.
WAIT = 590.0
POLL = 60.0  # one request's bound: the agent holds a poll for less and answers `pending`
PACE = 1.0   # the least time between polls, so an agent that answers `pending` at once is not spun


def decision(hook: dict, answer: object) -> tuple[dict | None, str]:
    """The hook output for the agent's answer, or None and the check it failed."""
    if not isinstance(answer, dict):
        return None, f"the answer is a JSON {type(answer).__name__}, not an object"
    kind = answer.get("decision")
    asking = hook.get("tool_name") == "AskUserQuestion"
    if kind == "allow":
        if asking:  # allowing a question with no answers would answer it empty
            return None, "an allow for AskUserQuestion carries no answers; it takes an answer decision"
        out = {"behavior": "allow"}
    elif kind == "deny":
        message = answer.get("message")
        out = {"behavior": "deny",
               "message": message if isinstance(message, str) and message else "Denied from the console."}
        if answer.get("interrupt") is True:
            out["interrupt"] = True
    elif kind == "answer":
        if not asking:
            return None, f"an answer decision for {hook.get('tool_name')!r}, which asks no question"
        tool_input = hook.get("tool_input") if isinstance(hook.get("tool_input"), dict) else {}
        asked = {q.get("question") for q in tool_input.get("questions") or [] if isinstance(q, dict)}
        answers = answer.get("answers")
        if not isinstance(answers, dict):
            return None, "answers is not an object"
        if any(not isinstance(v, str) for v in answers.values()):
            return None, "an answer is not text"
        # Every question the dialog asks, and only those: the model reads exactly this map.
        if set(answers) != asked or not asked:
            return None, (f"answers name {len(set(answers) - asked)} question(s) the dialog does not ask "
                          f"and leave {len(asked - set(answers))} of its {len(asked)} unanswered")
        out = {"behavior": "allow", "updatedInput": {**tool_input, "answers": answers}}
    else:
        return None, f"decision {str(kind)[:40]!r} is none this hook knows"
    return {"hookSpecificOutput": {"hookEventName": "PermissionRequest", "decision": out}}, ""


def wait(ws: Path, hook: dict, ask: str, deadline: float) -> dict | None:
    """Poll the agent until it decides, says the ask settled, or cannot be reached."""
    query = urllib.parse.urlencode({"session": hook["session_id"], "ask": ask})
    try:
        req = local_workspace.agent_request(ws, f"/steering/surface/ask?{query}", None, timeout=1.0)
    except Exception as e:
        record("decide", f"ask {ask}: the agent's port could not be read: {type(e).__name__}: {e}", ws)
        return None
    if req is None:
        record("decide", "no agent key for this workspace; the dialog is the person's", ws)
        return None
    while (left := deadline - time.monotonic()) > 0:
        polled = time.monotonic()
        try:
            with send(req, timeout=min(POLL, left)) as r:
                answer = json.loads(r.read() or b"{}")
        except socket.timeout:  # TimeoutError from 3.10; a poll held past its bound is asked again while the wait lasts
            continue
        except urllib.error.HTTPError as e:
            record("decide", f"ask {ask}: the agent refused the poll: {e.code} {e.read(200).decode(errors='replace')}", ws)
            return None
        except Exception as e:  # unreachable or unreadable: the dialog stays the person's
            record("decide", f"ask {ask}: the agent gave no decision: {type(e).__name__}: {e}", ws)
            return None
        if isinstance(answer, dict) and answer.get("decision") == "pending":
            time.sleep(max(0.0, min(PACE - (time.monotonic() - polled), deadline - time.monotonic())))
            continue
        if isinstance(answer, dict) and answer.get("decision") == "settled":
            return None
        out, why = decision(hook, answer)
        if out is None:
            record("decide", f"ask {ask}: the agent's answer decides nothing: {why}", ws)
        return out
    record("decide", f"ask {ask}: no decision within {WAIT:.0f}s; the dialog is the person's", ws)
    return None


def main() -> int:
    argv = sys.argv[1:]
    if verb_help.help_requested("decide", argv, bare=False):
        print(verb_help.script_help("decide")); return 0
    if argv:
        verb_help.error("decide", f"unexpected arguments: {' '.join(argv)}")
        return 0
    deadline = time.monotonic() + WAIT
    try:
        hook = json.load(sys.stdin)
    except ValueError as e:
        record("decide", f"the hook's input is not JSON: {e}")
        return 0
    if not isinstance(hook, dict) or hook.get("hook_event_name") != "PermissionRequest" \
            or not isinstance(hook.get("session_id"), str) or not hook["session_id"]:
        return 0
    anchor = Path(os.environ.get("CLAUDE_PROJECT_DIR") or hook.get("cwd") or os.getcwd())
    try:
        ws = workspace_root(anchor, timeout=1.0)
    except Exception as e:
        record("decide", f"no workspace at {anchor}: {type(e).__name__}: {e}", anchor)
        return 0
    # The request carries no tool id (#7837); the observe hook's record of the thread's PreToolUse
    # names the call, and an ask it cannot name is not polled for.
    pretool.stamp_tool(ws, hook)
    ask = hook.get("tool_use_id")
    if not isinstance(ask, str) or not ask:
        record("decide", "a permission request no PreToolUse named; the dialog is the person's", ws)
        return 0
    out = wait(ws, hook, ask, deadline)
    if out is not None:
        sys.stdout.write(json.dumps(out) + "\n"); sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
