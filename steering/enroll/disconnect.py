#!/usr/bin/env python3
"""`disconnect.py [--handover <note url>] [<session>]`: end this session's enrollment and forget its
token. The handover is the exit checkpoint's note (doc 154 §4), recorded on the detach (doc 123)."""
from __future__ import annotations

import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))
from local_workspace import required_workspace_root  # noqa: E402
from door import say  # noqa: E402
from ack import token_path  # noqa: E402
from bind import records  # noqa: E402
from verb_help import current_args, error, help_requested, script_help  # noqa: E402


def main(argv: list[str]) -> int:
    try:
        args = current_args("disconnect", argv)
    except ValueError as why:
        return error("disconnect", str(why))
    if help_requested("disconnect", argv):
        print(script_help("disconnect"))
        return 0
    handover = None
    if args[:1] == ["--handover"]:
        if len(args) < 2 or args[1].startswith("-"):
            return error("disconnect", "--handover takes a note URL")
        handover, args = args[1], args[2:]
    if args and args[0].startswith("-"):
        return error("disconnect", f"no such flag: {args[0]}")
    if len(args) > 1:
        return error("disconnect", "one session name at most")
    ws = required_workspace_root(Path(os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()), timeout=2.0)
    known = records(ws)
    psession = os.environ.get("CLAUDE_CODE_SESSION_ID", "").strip()
    if args:
        session = args[0]
    else:  # only an enrollment minted for this session, never whichever one happens to be stored
        mine = [n for n, rec in known.items() if psession and rec.get("provider_session") == psession]
        if len(mine) != 1:
            print(f"name the session to disconnect: {ws} holds no enrollment minted for this one"
                  f" ({', '.join(sorted(known)) or 'none stored'})", file=sys.stderr); return 1
        session = mine[0]
    if session not in known:
        print(f"no stored token for {session} in {ws}", file=sys.stderr); return 1
    answer = say(f"detach: {session} token {known[session]['token']}" + (f" handover {handover}" if handover else ""))
    if answer.startswith("detached:"):
        token_path(ws, session).unlink(missing_ok=True)  # the token died with the epoch
    print(f"{session}: {answer}")
    return 0 if answer.startswith("detached:") else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
