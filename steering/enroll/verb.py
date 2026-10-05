#!/usr/bin/env python3
"""`verb.py <verb> <session> <text>`: put a status verb on the board, from any node.

The board verbs are a session's own and the door admits them from the node the session enrolled
from, which is what makes this a client rather than a request to run one somewhere else.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))
from door import display_reply, outcome, occurrence, remote, door_url, OCCURRENCE, SETTLED, REFUSED  # noqa: E402
import ack  # noqa: E402
import outbox  # noqa: E402
import requestlog  # noqa: E402
from local_workspace import workspace_root  # noqa: E402
from refusal import resend  # noqa: E402

# `recommend:` and `ask:` share the session-owned status path and door admission. `say:` remains
# deliberately absent — it is speech routed by `say.py`.
VERBS = ("announce", "blocked", "done", "recommend", "ask")

ARTICLE = {"announce": "an", "blocked": "a", "done": "a", "recommend": "a", "ask": "an"}


def invocation() -> str:
    """How to spell this script for somebody who is not in its directory."""
    return help_invocation("verb", None)


def line(verb: str, session: str, text: str) -> str:
    """The verb line as it is written down: without the token, which `signed` adds only at the
    moment of sending, so the outbox never holds a credential."""
    return f"{verb}: {session} {text}".strip()


def signed(sent: str, session: str, ws: Path | None) -> str:
    """`sent` with the session's own token after its name, where the door proves who wrote the row
    (doc 131 §5). A session with no stored token sends it unsigned, and the door's refusal says
    to connect."""
    if ws is None:
        return sent
    try:
        token = json.loads(ack.token_path(ws, session).read_text())["token"]
    except (OSError, ValueError, KeyError):
        return sent
    verb, _, rest = sent.partition(": ")
    name, _, text = rest.partition(" ")
    if name != session or not ack.valid_token(token):
        return sent
    return f"{verb}: {session} token {token} {text}".strip()


def _workspace() -> Path | None:
    """The workspace whose outbox holds this session's writes. None only outside any checkout,
    where the verb is sent unrecorded as it was before the outbox; a lookup that could not be made
    raises, since sending unrecorded then is the loss the outbox exists to prevent."""
    try:
        return workspace_root(Path(os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()), timeout=1.0)
    except subprocess.CalledProcessError as e:
        if "not a git repository" not in (e.stderr or "") or os.environ.get("CLAUDE_PROJECT_DIR"):
            raise
        print("not in a checkout, so no outbox: sending unrecorded", file=sys.stderr)
        return None


def _through_outbox(ws: Path, sent: str, this: str, session: str) -> tuple[str, str | None]:
    """Write the verb down, send what this session still has pending ahead of it in order, then
    it. Held under the outbox's lock throughout, so no later verb of the session overtakes an
    earlier one. `(state, reply)`; reply None when an earlier write did not settle, which leaves
    this one queued behind it unsent. A write no resend can deliver is set aside with its reason
    and stops holding the rest back."""
    here = door_url()
    with outbox.locked(ws):
        outbox.sweep(ws)
        mine = outbox.admit(ws, sent, this, session, here)
        recorded = json.loads(mine.read_text()).get("door")
        if recorded != here:  # a --retry of a write made for a door this workspace no longer names
            where = outbox.reject(mine, f"recorded for {recorded}; this workspace's door is now {here}")
            return REFUSED, resend(
                f"{this} was written for {recorded}, not this door {here}; it was set aside in {where}",
                "correct this workspace's STEERING_DOOR, then use --retry=<id>")
        for p, o in outbox.pending(ws, session):
            if p == mine:
                break
            if o.get("door") != here:
                where = outbox.reject(p, f"recorded for {o.get('door')}; this workspace's door is now {here}")
                print(f"earlier {o['id']} was for another door; set aside in {where}", file=sys.stderr)
                continue
            state, reply = outcome(signed(o["line"], session, ws), occurrence_id=o["id"])
            print(f"earlier {o['id']}: {reply[:400]}", file=sys.stderr)
            if state == REFUSED:
                print(f"set aside in {outbox.reject(p, reply)}", file=sys.stderr)
                continue
            if state != SETTLED:
                print(f"queued behind {o['id']}, which the door did not settle", file=sys.stderr)
                return state, None
            outbox.settle(p)
        state, reply = outcome(signed(sent, session, ws), occurrence_id=this)
        if state == SETTLED:
            outbox.settle(mine)
        elif state == REFUSED:
            outbox.reject(mine, reply)
        return state, reply


from verb_help import _invocation as help_invocation, error, help_requested, script_help  # noqa: E402


def main(argv: list[str]) -> int:
    if help_requested("verb", argv):
        print(script_help("verb", topic=argv[0] if len(argv) == 2 else None))
        return 0
    if len(argv) < 3:
        return error("verb", "verb requires a board verb, session, and text")
    verb, session, rest = argv[0], argv[1], argv[2:]
    if verb not in VERBS:
        return error("verb", f"{verb!r} is not a board verb. To speak to a session or the seat, use say.py")
    if not ack.valid_token(session) or session.startswith("-"):
        return error("verb", "verb requires a session name")
    # One token, `--retry=<id>`, so nothing has to guess where the text stops. The two-word form
    # could not: a status line whose own last words are `--retry <id-shaped token>` is
    # indistinguishable from the flag, and silently truncating the text there would write the row
    # under an id the operator never chose.
    retry_id = None
    if rest and rest[-1].startswith("--retry"):
        flag = rest[-1]
        rest = rest[:-1]
        retry_id = flag.partition("=")[2]
        if not OCCURRENCE.fullmatch(retry_id):
            return error("verb", "--retry=<id> needs the id a previous send printed")
    text = (sys.stdin.read() if rest == ["-"] else " ".join(rest)).strip()
    if not text:
        return error("verb", f"nothing to say for {verb}")
    if verb in ("announce", "blocked"):
        import verb_grammar
        if verb_grammar.parse_status(line(verb, session, text)) is None:
            return error("verb", f"malformed {verb}")
    if verb == "ask" and text.startswith("json {"):
        import verb_grammar
        if verb_grammar.structured_ask(text) is None:
            return error("verb", "malformed structured ask: name a non-empty question and valid options")
    # A fresh id per invocation, because an invocation that does not say otherwise is a new write
    # — two identical `done:` lines are two events, not one repeated (doc 103 §4.1). It is printed
    # before the send, so an answer lost in transit still leaves the operator the id to resend on.
    this = retry_id or occurrence()
    print(f"id {this}", file=sys.stderr)
    if remote():  # the name the door logs and records this write under
        print(f"request {requestlog.of_key(this)}", file=sys.stderr)
    sent = line(verb, session, text)
    if len(sent) > outbox.LIMIT:
        print(f"this line is {len(sent)} characters and the door takes at most {outbox.LIMIT}; "
              f"no resend would change that, so nothing was sent", file=sys.stderr)
        return 2
    try:
        ws = _workspace()
    except (subprocess.SubprocessError, OSError) as e:
        print(f"the workspace lookup failed ({e}), so this verb cannot be written down; nothing "
              f"was sent — send it again", file=sys.stderr)
        return 1
    if ws is None:
        state, reply = outcome(sent, occurrence_id=this)
    else:
        try:
            state, reply = _through_outbox(ws, sent, this, session)
        except outbox.Conflict as e:
            print(f"{e}; --retry=<id> resends the line that id was sent with, and nothing else",
                  file=sys.stderr)
            return 2
        if reply is None:
            return 1
    print(display_reply(reply))
    if state not in (SETTLED, REFUSED) and ws is not None:
        print(f"kept in {outbox.outbox_dir(ws)}: the next verb this session sends goes after it",
              file=sys.stderr)
    # A door from before #3384 answers a line its grammar did not match by queueing it, which
    # reads as success. A status verb that registered says so, and nothing else counts.
    # Only for an answer that never came: a refusal the door gave is final, and a resend hint
    # printed after it would be the line a piped reader keeps instead of its repair. On stdout,
    # after the refusal, so it is the last line read however the streams are piped (#3303).
    if reply.startswith("REJECTED"):
        if state not in (SETTLED, REFUSED):
            print(f"if it may have been registered, resend the same line with --retry={this} rather "
                  f"than plain — the door answers a repeat from its record, a new send writes a new row")
        return 1
    if not reply.startswith(f"registered: {verb}"):
        print(f"the door took this as a message, not {ARTICLE[verb]} {verb}. Check the shape:\n"
              f"{script_help('verb', topic=verb)}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
