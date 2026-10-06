#!/usr/bin/env python3
"""`say.py [--to <session>] [--provider <harness>] [--provider-session <id>] <session> <text|->`:
say something on this session's own enrollment token — to the session `--to` names, or to whoever
holds the steering role. From another machine the untargeted form reaches the seated session, or
the seat's Needs You when nobody holds the seat, never the resident brain (#3381). A name the
workspace minted for another session is refused (#1077)."""
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
from verb import VERBS as BOARD_VERBS, invocation as board_invocation  # noqa: E402
from door import SETTLED, UNSENT, display_reply, occurrence, outcome, retry_args  # noqa: E402
from refusal import PREFIX as REJECTED  # noqa: E402
from local_workspace import required_workspace_root  # noqa: E402


from verb_help import error, help_requested, script_help  # noqa: E402


def main(argv: list[str]) -> int:
    if help_requested("say", argv):
        print(script_help("say", topic=argv[0] if len(argv) == 2 else None))
        return 0
    parsed = speaker_flags(argv, also=("--to",))
    if parsed is None:
        return error("say", "malformed speaker flags")
    flags, args = parsed
    target = flags.get("--to")
    # The flag is its own word wherever it stands: speech is arbitrary prose, and a flag the
    # scan missed would ride in the text as words under a fresh id — the delivery the retry
    # exists to prevent. One that is not an occurrence is refused, never swallowed.
    try:
        args, retry_id = retry_args(args)
    except ValueError as why:
        return error("say", str(why))
    if len(args) < 2:
        return error("say", "say requires a session and text")
    session = args[0]
    if not session or any(c.isspace() for c in session):
        return error("say", "say requires a session name")
    if target is not None and any(c.isspace() for c in target):
        return error("say", "--to requires one peer name")
    text = (sys.stdin.read() if args[1:] == ["-"] else " ".join(args[1:])).strip()
    if not text:
        return error("say", "nothing to say")
    verb = text.split(":", 1)[0].strip().lower() if ":" in text.split(None, 1)[0] else ""
    if verb in BOARD_VERBS:
        print(f"that is a board verb, and this sends speech: it would go as the text of a `say:`, "
              f"and the board would stay as it was. Use:\n"
              f"  {board_invocation()} {verb} {session} <text>", file=sys.stderr)
        return 2
    ws = required_workspace_root(Path(os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()), timeout=2.0)
    try:
        session, token = speaking_as(ws, session, flags)
    except Refused as why:
        print(str(why), file=sys.stderr)
        return 1
    addressed = f"{session} to {target}" if target else session
    this = retry_id or occurrence()
    print(f"id {this}", file=sys.stderr)
    state, reply = outcome(f"say: {addressed} token {token} {text}", occurrence_id=this)
    print(display_reply(reply))
    # `UNSENT`, not the text: a did-not-answer carries the refusal prefix too, which is why the
    # state exists (#1798). A refusal the door answered is final, and no resend improves it.
    # The last line on stdout, after the refusal: a sender reading `2>&1 | tail -1` saw only the
    # refusal, since a piped stdout is flushed after stderr, and resent plain (#3303).
    if state == UNSENT:
        print(f"if the words may have been delivered, resend with --retry={this}; a plain resend is a second say")
    return 0 if state == SETTLED and not reply.startswith(REJECTED) else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
