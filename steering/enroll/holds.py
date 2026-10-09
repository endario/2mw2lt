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


def _executors(card: dict) -> list[str]:
    """Each session that executes `card`, with its engagement's state: `enrolled` while it names the
    session as enrolled now, `ended` once ended, and `superseded` when the session enrolled again."""
    out = []
    for e in card.get("executors") or []:
        if e.get("role") != "executor":
            continue
        state = "ended" if e.get("ended") else "enrolled" if e.get("live") is True else "superseded"
        out.append(f"{e.get('session')} ({state})")
    return out


def _on_go(ws: Path, token: str, asked: str) -> int:
    """Who executes the card a branch is bound to, or the cards resolving an issue, read on Go's
    card routes with the session's own token: a card's executors are who is on its work."""
    import urllib.parse
    import session_routes
    repo = session_routes.card_repo(ws)
    who: list[str] = []
    try:
        if asked.startswith("issue "):
            n = int(asked.split()[1])
            page = "/cards?state=live"
            while page:
                listed = session_routes.get(page, token)
                for card in listed.get("cards") or []:
                    if any(a.get("verb") == "resolves" and a.get("repo") == repo and a.get("n") == n and not a.get("ended")
                           for a in card.get("anchors") or []):
                        who += _executors(card)
                nxt = listed.get("next")
                page = "/cards?state=live&next=" + urllib.parse.quote(nxt, safe="") if nxt else ""
            print(f"issue {n} is claimed by {', '.join(who)}" if who else f"issue {n} is claimed by nobody")
            return 0
        try:
            card = session_routes.get("/cards?branch=" + urllib.parse.quote(f"{repo}:{asked}", safe=""), token)["card"]
            # The alias outlives the binding: only a live card still bound to the branch holds it.
            if card.get("state") == "live" and any(b.get("repo") == repo and b.get("branch") == asked and not b.get("ended")
                                                   for b in card.get("branches") or []):
                who = _executors(card)
        except session_routes.Refused as e:
            if e.code != "card-absent":
                raise
    except (session_routes.Refused, session_routes.Unsent) as e:
        print(str(e), file=sys.stderr)
        return 1
    print(f"{asked} is held by {', '.join(who)}" if who else f"{asked} is held by nobody")
    return 0


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
    ws = required_workspace_root(Path(os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()), timeout=2.0)
    import session_routes
    if session_routes.on_coordination(ws):
        try:
            session, token = speaking_as(ws, session, flags)
        except Refused as why:
            print(str(why), file=sys.stderr)
            return 1
        return _on_go(ws, token, branch)
    # The verb lives on the remote door only (doc 68 §9). Sending it to the loopback door would
    # queue it as an ordinary message for the owner to read, which looks like being ignored.
    if not remote():
        field = "takings" if branch.startswith("issue ") else "holdings"
        print(f"this is the brain machine's own door, which does not answer `holds:`. Read its "
              f"registry field `{field}` with:\n"
              f"  {help_invocation('door', None)} --get /steering/registry", file=sys.stderr)
        return 1
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
