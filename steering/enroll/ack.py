#!/usr/bin/env python3
"""`ack.py <session> <directive id>`: record receipt of a relayed directive."""
from __future__ import annotations

import hashlib
import json
import os
import sys
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
from local_workspace import required_workspace_root  # noqa: E402
from door import display_reply  # noqa: E402


def valid_token(value: object) -> bool:
    """One run of non-whitespace. Anything else cannot make a verb line: the door's grammar reads
    the token as the last field, so a blank or spaced one silently becomes a plain message."""
    return isinstance(value, str) and bool(value) and not any(c.isspace() for c in value)


def token_path(ws: Path, session: str) -> Path:
    return ws / ".claude" / "steering-tokens" / hashlib.sha256(session.encode()).hexdigest()[:32]


def store(ws: Path, session: str, token: str, provider_session: str | None = None,
          authority: str | None = None, epoch: int | None = None) -> Path:
    """`provider_session` is what the enrollment was minted for; a reader that has one of its own
    can then tell a name it derived from a name that merely collided with it. `authority` and
    `epoch` mark a credential Go issued (`session_routes`)."""
    p = token_path(ws, session); p.parent.mkdir(parents=True, exist_ok=True)
    rec = {"session": session, "token": token}
    if provider_session:
        rec["provider_session"] = provider_session
    if authority:
        rec["authority"], rec["epoch"] = authority, epoch
    fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(json.dumps(rec))
    return p


def records(ws: Path) -> dict[str, dict]:
    """The workspace's stored `enroll:` records by session name. A file whose name is not the
    hash of the session it claims is ignored: the name is the only thing tying the two."""
    out: dict[str, dict] = {}
    d = ws / ".claude" / "steering-tokens"
    for p in sorted(d.glob("*")) if d.is_dir() else []:
        try:
            rec = json.loads(p.read_text())
            session, token = rec["session"], rec["token"]
        except (OSError, ValueError, KeyError, TypeError):
            continue
        if isinstance(session, str) and valid_token(token) and token_path(ws, session) == p:
            out[session] = rec
    return out


def minted_for(ws: Path, provider_session: object) -> str | None:
    """The token of the enrollment stored here for this harness session, picked as
    `connect.own_enrolment` picks it when no name is given: the record minted for it, else a lone
    record that names no session. None otherwise; the door checks whatever is sent."""
    known = records(ws)
    mine = [rec for _, rec in sorted(known.items())
            if provider_session and rec.get("provider_session") == provider_session]
    if not mine and len(known) == 1 and not next(iter(known.values())).get("provider_session"):
        mine = list(known.values())
    return mine[0]["token"] if mine else None


from secret_input import read_secret  # noqa: E402
from verb_help import error, help_requested, script_help  # noqa: E402


def main(argv: list[str]) -> int:
    if help_requested("ack", argv):
        print(script_help("ack", topic=argv[0] if len(argv) == 2 else None))
        return 0
    if argv[:1] == ["--store"]:
        if not (len(argv) == 2 or (len(argv) == 4 and argv[2] == "--provider-session")):
            return error("ack", "--store requires a session, with optional --provider-session <id>, "
                         "and the token on stdin")
        if not valid_token(argv[1]) or argv[1].startswith("-"):
            return error("ack", "--store requires a session name")
        if len(argv) == 4 and (not valid_token(argv[3]) or argv[3].startswith("-")):
            return error("ack", "--provider-session requires an id")
        token = read_secret("token: ")
        if not valid_token(token):
            return error("ack", "--store requires a non-whitespace token on stdin, read from the enrol reply")
        ws = required_workspace_root(Path(os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()), timeout=2.0)
        print(f"stored: {store(ws, argv[1], token, argv[3] if len(argv) == 4 else None)}"); return 0
    if len(argv) != 2:
        return error("ack", "ack requires a session and directive id")
    if any(arg.startswith("-") for arg in argv):
        return error("ack", "unknown ack option")
    if not valid_token(argv[0]):
        return error("ack", "ack requires a session name")
    if not go_directive(argv[1]):
        return error("ack", "ack requires a valid directive id")
    ws = required_workspace_root(Path(os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()), timeout=2.0)
    session, ulid = argv
    try:
        reply = acknowledge(ws, session, ulid)
    except LookupError as e:
        print(e, file=sys.stderr); return 1
    print("acked" if reply.startswith("acked:") else f"not acked: {display_reply(reply, 200)}")
    return 0 if reply.startswith("acked:") else 1


def valid_directive(ws: Path, directive: str) -> bool:
    """A directive's id as Go mints it: a UUID."""
    return go_directive(directive)


def go_directive(directive: str) -> bool:
    try:
        return str(uuid.UUID(directive)) == directive
    except ValueError:
        return False


def acknowledge(ws: Path, session: str, ulid: str) -> str:
    """Acknowledge a directive on the session's stored token, on Go's acknowledgement route.
    Raises LookupError when no usable token is stored."""
    try:
        token = json.loads(token_path(ws, session).read_text())["token"]
    except (OSError, ValueError, KeyError):
        raise LookupError(f"no stored token for {session}: enroll first") from None
    if not valid_token(token):
        raise LookupError(f"the stored token for {session} is not one: enroll again")
    import session_routes  # noqa: E402
    return session_routes.acknowledge(session, token, ulid)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
