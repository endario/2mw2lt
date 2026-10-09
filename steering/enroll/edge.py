#!/usr/bin/env python3
"""`edge.py <session> link|unlink <card> …`: say what the card this session executes waits on or
contributes to, or end such an edge (doc 167), on the session's stored token. Any other card's
edges are the brain's, through `card.py`."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))
from ack import token_path, valid_token  # noqa: E402
from local_workspace import required_workspace_root  # noqa: E402

from verb_help import error, help_requested, script_help  # noqa: E402


def main(argv: list[str]) -> int:
    if help_requested("edge", argv) or (len(argv) == 3 and argv[2] in ("-h", "--help")):
        print(script_help("edge", topic=argv[1] if len(argv) == 3 else None))
        return 0
    if len(argv) < 2 or argv[1] not in ("link", "unlink"):
        return error("edge", "link requires a session and link or unlink")
    session, verb, rest = argv[0], argv[1], argv[2:]
    if not valid_token(session) or session.startswith("-"):
        return error("edge", "link requires a session name")
    import card_grammar
    built, why = card_grammar.built(verb, rest)
    if why:
        return error("edge", why)
    # The grammar alone, `by` aside: the door names who drew the edge from the token.
    why = card_grammar.validate({**built[0], "by": f"session {session}"})
    if why:
        return error("edge", why)
    ws = required_workspace_root(Path(os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()), timeout=2.0)
    try:
        token = json.loads(token_path(ws, session).read_text())["token"]
    except (OSError, ValueError, KeyError):
        print(f"no stored token for {session}: /2mw2lt:connect first", file=sys.stderr)
        return 1
    if not valid_token(token):
        print(f"the stored token for {session} is not one: /2mw2lt:connect again", file=sys.stderr)
        return 1
    import refusal
    import session_routes
    fact = built[0]
    to = {key: fact[key] for key in ("to_card", "to_issue") if key in fact}
    try:
        reply = session_routes.edge_card(fact["card"], verb == "unlink", fact["kind"], to,
                                         fact["why"], token, how=fact.get("how", ""),
                                         source=fact.get("source", ""))
    except session_routes.Refused as e:
        reply = refusal.use("declare", f"{verb} {fact['card']}: {e}")
    except session_routes.Unsent as e:
        reply = refusal.retry(str(e))
    print(reply)
    return 0 if reply.startswith(f"{verb}ed:") else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
