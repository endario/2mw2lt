#!/usr/bin/env python3
"""`declare.py <session> [--card <ulid>] <name> [<verb>:<n>[,<n>] ...]`: make this session's work a
card before it shows any other sign (doc 125 §4), on the session's stored token."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))
from ack import token_path, valid_token  # noqa: E402
from ulids import new_ulid, valid_ulid  # noqa: E402
from local_workspace import required_workspace_root  # noqa: E402


from verb_help import error, help_requested, script_help  # noqa: E402


def main(argv: list[str]) -> int:
    if help_requested("declare", argv):
        print(script_help("declare", topic=argv[0] if len(argv) == 2 else None))
        return 0
    if len(argv) < 2:
        return error("declare", "declare requires a session and name")
    session, rest = argv[0], argv[1:]
    if not valid_token(session) or session.startswith("-"):
        return error("declare", "declare requires a session name")
    # Minted here, before the send, so a rerun under it is answered from the record rather than
    # minting a second card (doc 125 §3).
    card = new_ulid()
    if rest[0] == "--card":
        if len(rest) < 3 or not valid_ulid(rest[1]):
            return error("declare", "--card requires a valid card id and name")
        card, rest = rest[1], rest[2:]
    if rest[0].startswith("-"):
        return error("declare", f"unknown declare option {rest[0]!r}")
    if not " ".join(rest).strip():
        return error("declare", "declare requires a name")
    import card_grammar
    fact, why = card_grammar.declaration(session, card, rest)
    if why:
        return error("declare", why)
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
    try:
        reply = session_routes.declare_card(card, fact["name"], fact.get("anchors") or {}, token,
                                            repo=session_routes.card_repo(ws) if fact.get("anchors") else "")
    except session_routes.Refused as e:
        reply = refusal.use("declare", f"{card}: {e}")
    except session_routes.Unsent as e:
        reply = refusal.retry(str(e))
    print(reply)
    if not reply.startswith("declared:"):
        print(f"to retry this declaration under the same card, send it with --card {card}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
