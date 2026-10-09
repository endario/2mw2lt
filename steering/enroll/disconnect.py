#!/usr/bin/env python3
"""`disconnect.py [--handover <note url>] [<session>]`: end this session's enrollment and forget its
token. The handover is the exit checkpoint's note (doc 154 §4), recorded on the detach (doc 123)."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parents[1]))
import os  # noqa: E402

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))
from local_workspace import required_workspace_root  # noqa: E402
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
    # A machine's sessions share the workspace's token store, and a brain once ended a peer's
    # enrolment this way while the peer was waiting on a review round (#4295). The rule is
    # `connect.own_enrolment`'s; a process whose session is not named here cannot be told from a peer.
    minted = known[session].get("provider_session")
    if psession and (minted != psession if minted else len(known) > 1):
        print(f"{session} is not this session's enrolment: disconnect ends only its own; "
              "the brain ends another session with `retire:`", file=sys.stderr); return 1
    import session_routes  # noqa: E402
    answer = session_routes.detach(session, known[session]["token"], handover=handover or "")
    if answer.startswith("detached:"):
        token_path(ws, session).unlink(missing_ok=True)  # the token died with the epoch
    print(f"{session}: {answer}")
    return 0 if answer.startswith("detached:") else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
