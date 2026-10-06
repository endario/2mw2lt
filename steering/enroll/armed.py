#!/usr/bin/env python3
"""The re-arm: a Claude Code `Stop` hook that refuses a turn ending with no hold armed (#1506).

A Claude Code session is reachable only while it holds its own stream. `goose.py` and
`codex.py` each define `deliver`, so the daemon can push into those; `agent.py` has no
equivalent, and there is no socket, pipe or watch by which anything outside can wake an idle
one. So a turn that ends without re-arming takes the session off the board silently, and
nothing but the session itself is in a position to notice.

The skill says to re-arm every time. That is a rule the model has to remember at the moment it
is finishing and least likely to, and four of eleven sessions did not on 2026-09-20. A `Stop`
hook is where the rule can be enforced rather than recalled: `{"decision": "block", "reason": …}`
refuses the stop and hands the reason to the model, which keeps working. That is the shape this
repository probed against a real harness rather than the one the reference also documents
(doc 18 §12a, `steering/test/fixtures/claude-stop-probe-2.1.259.ndjson`).

**Everything uncertain answers by letting the turn end.** A session that is not enrolled, a
workspace that cannot be read, a harness that does not say which session it is — none of those
is evidence the session is dark, and blocking on one would wedge a turn for a session steering
never expected to hold anything. The cost of the two mistakes is not symmetric: a missed block
loses one session until someone looks, a wrong block costs every turn in every workspace.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parents[1]))
import python_floor  # noqa: E402

python_floor.require()

import json
import os
import shlex

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))
import hooks  # noqa: E402
import verb_help  # noqa: E402
from connect import minted_name, records  # noqa: E402
from hold import holding  # noqa: E402
from local_workspace import workspace_root  # noqa: E402

DONE = 0    # every answer is exit 0; what the turn does is said on stdout


def already_blocked(hook: dict) -> bool:
    """That this stop is already a hook's continuation, so the refusal has been made.

    The one loop-breaker, and it needs no second: the probe recorded `stop_hook_active` false
    on the first stop of a turn and true on the stop that followed the block (doc 18 §12a), and
    the harness caps consecutive blocks besides. An earlier version of this file kept its own
    per-turn marker, which bought nothing and left a file per refused turn.
    """
    return bool(hook.get("stop_hook_active"))


def reason(session: str, ws: Path) -> str:
    """What the model is told, which is the whole of what it gets: one runnable command.

    Spelled out because the two ways this goes wrong are both ways of appearing to comply —
    a shell `&`, which backgrounds a process the harness is not watching and does not wake the
    session, and a foreground hold, which never returns.

    Named through the workspace launcher when one is installed, so a plugin update between this
    block and the model actually running the command does not hand it a path a surviving cache
    dir keeps alive but no longer current (#1875 round 3) — the same class this PR removes from
    every skill and the daemon's own notice. `HERE/hold.py` stays the fallback for a workspace
    the launcher is not (yet) installed in, so the block itself never depends on it.
    """
    launcher = hooks.launcher_path(ws)
    if launcher.is_file():
        cmd = f'python3 {shlex.quote(str(launcher))} exec hold --until-event {shlex.quote(session)}'
    else:
        cmd = (f'python3 {shlex.quote(str(HERE / "hold.py"))} --until-event '
               f'{shlex.quote(session)}')
    return (f"This turn would end with no hold armed, and a Claude Code session is reachable "
            f"only while it holds its own stream: nothing outside it can wake it again. Arm "
            f"one now, then finish:\n\n  {cmd}\n\n"
            f"Run it as a background command — in Claude Code, Bash with run_in_background "
            f"true. A shell `&` inside a Bash call is not the same thing: it detaches the "
            f"process from the harness, so its output never wakes the session and the hold is "
            f"no use to you. Do not run it in the foreground either; it does not return.\n\n"
            f"If you are leaving instead — the seat released you, or your work is done and "
            f"nothing is left for you — run /2mw2lt:disconnect. It ends this session's "
            f"enrolment, and this hook stops asking.")


def main() -> int:
    argv = sys.argv[1:]
    if verb_help.help_requested("armed", argv, bare=False):
        print(verb_help.script_help("armed")); return 0
    if argv:
        return verb_help.error("armed", f"unexpected arguments: {' '.join(argv)}")
    try:
        hook = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return DONE
    if not isinstance(hook, dict) or already_blocked(hook):
        return DONE
    psession = hook.get("session_id")
    if not isinstance(psession, str) or not psession:
        return DONE
    try:
        # `CLAUDE_PROJECT_DIR` over the payload's cwd, as `observe.py` and `readout.py` anchor:
        # a session whose cwd has left the project reaches a different common root, finds no
        # record there, and would be let go every turn while dark.
        anchor = os.environ.get("CLAUDE_PROJECT_DIR") or hook.get("cwd") or os.getcwd()
        ws = workspace_root(Path(anchor), timeout=1.0)
        # The enrolment minted for *this* provider session, and no other. `own_enrolment` would
        # fall back to a name the workspace holds for somebody else, which in a workspace with
        # more than one session refuses a stranger's turn and names another session's hold.
        session = minted_name(records(ws), psession)
    except Exception:
        return DONE   # not a workspace, or not a session steering knows: nothing is owed
    if not session or holding(ws, session):
        return DONE
    json.dump({"decision": "block", "reason": reason(session, ws)}, sys.stdout)
    return DONE


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:       # a hook that raises must not be the reason a turn cannot end
        sys.exit(DONE)
