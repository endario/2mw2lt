#!/usr/bin/env python3
"""`taking.py <your session> <issue>`: say what you are taking, before a branch exists.

`taking.py <your session> branch [<name>]`: claim the branch you work on, by default the one this
checkout is on. A session in a linked worktree needs it: what it is observed to hold is the
project checkout's branch, and a gate is commissioned only on a branch its session holds
(doc 68 §11).
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parents[1]))
import python_floor  # noqa: E402

python_floor.require()

import os
import shlex

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))
from bind import Refused  # noqa: E402
from connect import branch as branch_of, speaker_flags, speaking_as  # noqa: E402
from door import display_reply, occurrence, remote, say  # noqa: E402
from local_workspace import required_workspace_root  # noqa: E402


from verb_help import _invocation as help_invocation, error, help_requested, script_help  # noqa: E402


def main(argv: list[str]) -> int:
    if help_requested("taking", argv):
        print(script_help("taking", topic=argv[0] if len(argv) == 2 else None))
        return 0
    parsed = speaker_flags(argv)
    if parsed is None:
        return error("taking", "malformed speaker flags")
    flags, args = parsed
    if not args or not args[0] or args[0].startswith("-") or any(c.isspace() for c in args[0]):
        return error("taking", "taking requires a session name")
    if len(args) in (2, 3) and args[1] == "branch":
        if len(args) == 3 and (not args[2] or args[2].startswith("-") or
                              any(c.isspace() for c in args[2]) or len(args[2]) > 200):
            return error("taking", "taking requires an issue number or branch")
        name = args[2] if len(args) == 3 else branch_of(Path.cwd())
        if not name:
            print("this checkout is on no branch to claim", file=sys.stderr)
            return 1
        what = f"branch {name}"
    elif len(args) == 2 and args[1].removeprefix("#").isascii() and args[1].removeprefix("#").isdigit():
        what = args[1].removeprefix("#")
    else:
        return error("taking", "taking requires an issue number or branch")
    session = args[0]
    ws = required_workspace_root(Path(os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()), timeout=2.0)
    import session_routes
    if session_routes.on_coordination(ws):
        # The brain's ruling, 2026-10-08: no separate claim primitive on Go. A branch claim is an
        # announce, whose doing defaults to the executed card's title; an issue claim is a card
        # declared with resolves:<n>, this session its executor, whose id is the claim's own so a
        # rerun after a lost answer replays instead of minting a second card.
        import refusal as refusal_mod
        try:
            _, token = speaking_as(ws, session, flags)
        except Refused as why:
            print(str(why), file=sys.stderr)
            return 1
        try:
            if what.startswith("branch "):
                # A lost answer can only be rerun as a new announce, and the CLI takes no --retry:
                # no id is printed for one nobody can resend.
                reply = session_routes.announce_branch(session, token, what.removeprefix("branch ").strip(),
                                                       ws, occurrence())
            else:
                reply = session_routes.claim_issue(session, token, session_routes.card_repo(ws),
                                                   int(what))
        except ValueError as e:
            print(f"{e}: declare the work first, or give doing", file=sys.stderr)
            return 1
        except session_routes.Refused as e:
            said = refusal_mod.use("taking", str(e)) if session_routes.settled(e) \
                else refusal_mod.retry(str(e))
            reply = said
        except session_routes.Unsent as e:
            reply = refusal_mod.retry(str(e))
        print(display_reply(reply))
        return 0 if not reply.startswith("REJECTED") else 1
    if not what.startswith("branch ") and not remote():
        speaker = [item for flag, value in flags.items() for item in (flag, value)] + [session]
        request = f"Please assign issue {what} to a worker enrolled on the authenticated remote door for this workspace."
        print("this is the brain machine's own door, which does not take issue claims. A worker "
              "enrolled on this workspace's authenticated remote door can claim it. Ask the brain "
              f"to assign it:\n  {help_invocation('say', None)} {shlex.join(speaker + [request])}",
              file=sys.stderr)
        return 1
    try:
        session, token = speaking_as(ws, session, flags)
    except Refused as why:
        print(str(why), file=sys.stderr)
        return 1
    reply = say(f"taking: {what} token {token}")
    print(display_reply(reply))
    return 1 if reply.startswith("REJECTED") else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
