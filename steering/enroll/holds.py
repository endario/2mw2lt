#!/usr/bin/env python3
"""`holds.py <your session> <branch|issue N>`: who is on it, on this session's own token."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parents[1]))
import python_floor  # noqa: E402

python_floor.require()

import os

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))
from bind import Refused  # noqa: E402
from connect import speaker_flags, speaking_as  # noqa: E402
from door import display_reply, remote, say  # noqa: E402
from local_workspace import required_workspace_root  # noqa: E402


from verb_help import _invocation as help_invocation, error, help_requested, script_help  # noqa: E402


def main(argv: list[str]) -> int:
    if help_requested("holds", argv):
        print(script_help("holds", topic=argv[0] if len(argv) == 2 else None))
        return 0
    parsed = speaker_flags(argv)
    if parsed is None or len(parsed[1]) < 2:
        return error("holds", "holds requires a session and branch or issue")
    flags, args = parsed
    session = args[0]
    if not session or session.startswith("-") or any(c.isspace() for c in session):
        return error("holds", "holds requires a session name")
    if len(args) == 3 and args[1] == "issue" and args[2].removeprefix("#").isascii() \
            and args[2].removeprefix("#").isdigit():
        branch = f"issue {args[2].removeprefix('#')}"
    elif len(args) == 2 and args[1] and args[1] != "issue" and not args[1].startswith("-") \
            and not any(c.isspace() for c in args[1]):
        branch = args[1]
    else:
        return error("holds", "holds requires a branch or issue number")
    # The verb lives on the remote door only (doc 68 §9). Sending it to the loopback door would
    # queue it as an ordinary message for the owner to read, which looks like being ignored.
    if not remote():
        field = "takings" if branch.startswith("issue ") else "holdings"
        print(f"this is the brain machine's own door, which does not answer `holds:`. Read its "
              f"registry field `{field}` with:\n"
              f"  {help_invocation('door', None)} --get /steering/registry", file=sys.stderr)
        return 1
    ws = required_workspace_root(Path(os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()), timeout=2.0)
    try:
        session, token = speaking_as(ws, session, flags)
    except Refused as why:
        print(str(why), file=sys.stderr)
        return 1
    reply = say(f"holds: {branch} token {token}")
    print(display_reply(reply, 2000))
    return 1 if reply.startswith("REJECTED") else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
