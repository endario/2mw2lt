#!/usr/bin/env python3
"""`lease.py [--provider <harness> --provider-session <id>] say [--retry=<id>]` (a verb line on stdin)
or `lease.py [...] post <path>` (a JSON body on stdin): a lease verb on the seat's stored lease.

The lease is kept on disk for the session that holds the seat, so no model's context, transcript
or command line carries it (#4551, step 2). `promote.py` and `hold.py`'s seat frame store it; this
fills the one credential slot a lease verb has and nothing else, and `card.py --lease` reads it
too. The daemon decides whether a token holds the lease; a client that hears it does not removes
the stored copy and says so.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parents[1]))
import python_floor  # noqa: E402

python_floor.require()

import hashlib  # noqa: E402
import json  # noqa: E402
import os  # noqa: E402
import re  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from atomic_file import write_atomic  # noqa: E402

PLACEHOLDER = "@lease"
# The daemon's words for a token that holds no lease: another holds the seat or nobody does
# (`registry.held_by`), or, at the API, a bearer that is neither a lease nor an enrolment
# (`api.principal.resolve`).
DEAD = ("this token does not hold the lease", "nobody holds the lease",
        "the token is neither the lease the seat holds nor a live enrolment")
NONE = "refused: this session holds no stored lease; promote.py stores one"
GONE = ("refused: this session no longer holds the seat ({why}); its stored lease is removed. "
        "Take the seat again only if the owner asks.")
# A lease verb's line opens `<verb>: token <token> `: the slot is that field and nothing after it.
SLOT = re.compile(r"^([a-z][a-z-]*): token @lease(?=\s|$)")


def path(ws: Path, provider_session: str) -> Path:
    """Under `steering-tokens/`, so every confinement that denies the enrolment tokens by subpath
    denies the lease too."""
    return ws / ".claude" / "steering-tokens" / "leases" / hashlib.sha256(provider_session.encode()).hexdigest()[:32]


def store(ws: Path, provider_session: str, session: str, attachment_id: str, token: str) -> Path:
    p = path(ws, provider_session)
    p.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(p.parent, 0o700)
    write_atomic(p, json.dumps({"provider_session": provider_session, "session": session,
                                "attachment_id": attachment_id, "lease_token": token}), mode=0o600)
    return p


def load(ws: Path, provider_session: str) -> dict | None:
    """The stored lease of this provider session, or None. A record that names another provider
    session than its file's is not this session's."""
    try:
        rec = json.loads(path(ws, provider_session).read_text())
    except (OSError, ValueError):
        return None
    ok = isinstance(rec, dict) and rec.get("provider_session") == provider_session \
        and isinstance(rec.get("lease_token"), str) and rec["lease_token"].strip() == rec["lease_token"] != ""
    return rec if ok else None


def remove(ws: Path, provider_session: str) -> bool:
    try:
        path(ws, provider_session).unlink()
        return True
    except FileNotFoundError:
        return False


def fill_say(line: str, token: str) -> str:
    """The verb line with its credential slot filled. Refused unless the line opens
    `<verb>: token @lease `: a `@lease` anywhere else, in a relay's words say, is sent as written."""
    if not SLOT.match(line):
        raise ValueError("a --lease line opens `<verb>: token @lease ` and fills nothing else")
    return SLOT.sub(lambda m: f"{m.group(1)}: token {token}", line, count=1)


def fill_post(target: str, body: bytes, token: str) -> bytes:
    """The body with its `lease_token` filled, for a path on this workspace's door only: the stored
    token never goes to another host."""
    if not target.startswith("/") or target.startswith("//"):
        raise ValueError("--lease posts only to a path on this workspace's door, never to a URL")
    try:
        obj = json.loads(body or b"null")
    except ValueError:
        raise ValueError("a --lease body is a JSON object") from None
    if not isinstance(obj, dict) or obj.get("lease_token") != PLACEHOLDER:
        raise ValueError(f'a --lease body is a JSON object whose "lease_token" is "{PLACEHOLDER}"')
    return json.dumps({**obj, "lease_token": token}).encode()


def dead(answer: str) -> bool:
    """Whether a refusal says the lease is dead. Only a refusal: an answer that carries the words
    as data, a ledger fact quoting an old refusal, is an answer."""
    refused = answer.lstrip().lower().startswith(("refused", "rejected"))
    return refused and any(words in answer for words in DEAD)


def held(flags: dict[str, str]) -> dict | None:
    """The stored lease of the provider session the flags name, or that this process runs in, with
    its workspace beside it under `ws`; None when it stores none. ValueError when the workspace or
    the provider session cannot be told."""
    import connect
    try:
        ws = connect.required_workspace_root(connect.project_dir(), timeout=2.0)
        psession = connect.provider_session(flags)
    except (connect.Refused, SystemExit) as why:
        raise ValueError(f"--lease needs this session's workspace and provider session: {why}") from None
    rec = load(ws, psession)
    return None if rec is None else {**rec, "ws": ws}


def gone(rec: dict, answer: str) -> str:
    """The daemon refused the stored lease as dead: the seat is another's now. The copy goes, and
    the session is told why, in the daemon's own words (C5)."""
    remove(rec["ws"], rec["provider_session"])
    return GONE.format(why=answer.strip())


def seat_frame(raw: bytes) -> tuple[bytes, dict | None]:
    """A `seat` frame's line with its lease token replaced by `stored`, and the frame as it came;
    any other line unchanged, and None. What is recorded and printed is the first."""
    if not raw.startswith(b"data: "):
        return raw, None
    try:
        frame = json.loads(raw[6:])
    except ValueError:
        return raw, None
    if not isinstance(frame, dict) or frame.get("kind") != "seat" or not isinstance(frame.get("lease_token"), str):
        return raw, None
    return b"data: " + json.dumps({**frame, "lease_token": "stored"}).encode() + b"\n", frame


def main(argv: list[str]) -> int:
    import connect
    import door
    from verb_help import error, help_requested, script_help
    if help_requested("lease", argv):
        print(script_help("lease", topic=argv[0] if len(argv) == 2 else None))
        return 0
    parsed = connect.speaker_flags(argv)
    if parsed is None:
        return error("lease", "the speaker flags are --provider <harness> and --provider-session <id>")
    flags, rest = parsed
    try:
        rest, retry = door.retry_args(rest)
        rec = held(flags)
    except ValueError as why:
        return error("lease", str(why))
    if rest[:1] == ["say"] and len(rest) == 1:
        if rec is None:
            print(NONE)
            return 1
        try:
            line = fill_say(sys.stdin.read().strip(), rec["lease_token"])
        except ValueError as why:
            return error("lease", str(why))
        # `door.py --say`'s own send, on the filled line: the id first, a lost answer resent under it.
        key = retry or door.occurrence()
        print(f"id {key}", file=sys.stderr)
        state, text = door.line_from(line, key)
        if dead(text):
            print(gone(rec, text))
            return 1
        print(text)
        if state == door.UNSENT and not door.unkeyed(line):
            print(f"if this line may have been registered, resend with --retry={key}")
        return 0 if state == door.SETTLED and not text.startswith(door.REJECTED) else 1
    if rest[:1] == ["post"] and len(rest) == 2 and retry is None:
        if rec is None:
            print(NONE)
            return 1
        try:
            body = fill_post(rest[1], sys.stdin.buffer.read(), rec["lease_token"])
            code, text = door.post(rest[1], body)
        except ValueError as why:
            return error("lease", str(why))
        except OSError as e:
            print(f"000 {door.failure(e)}")
            return 1
        if not 200 <= code < 300 and dead(f"refused: {text}"):
            print(gone(rec, text))
            return 1
        # Handed back, or handed on: the token is dead either way, and nothing is left to hold.
        if 200 <= code < 300 and re.search(r"/steering/brain/(detach|attach)/?$", rest[1]):
            remove(rec["ws"], rec["provider_session"])
        print(f"{code} {text}")
        return 0 if 200 <= code < 300 else 1
    return error("lease", "say [--retry=<id>], or post <path>")


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
