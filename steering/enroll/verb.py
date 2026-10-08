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
VERBS = ("announce", "blocked", "wait", "done", "recommend", "ask")

ARTICLE = {"announce": "an", "blocked": "a", "wait": "a", "done": "a", "recommend": "a", "ask": "an"}


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


def _on_go(ws: Path, verb: str, session: str, text: str, retry_id: str | None) -> int:
    """A status verb on a Go workspace: the fields its route names, on the session's carrier, under
    the invocation's occurrence id. An announce may give no doing and take the card it executes'
    title, the seat having ruled there is no separate claim primitive (2026-10-08)."""
    import json as _json
    import re
    import refusal
    import session_routes
    try:
        token = _json.loads(ack.token_path(ws, session).read_text())["token"]
    except (OSError, ValueError, KeyError):
        print(f"no stored token for {session}: /2mw2lt:connect first", file=sys.stderr)
        return 1

    def said(run):
        """`run`'s answer, with Go's refusal said: a settled one names the verb's remedy, an
        unsettled one is sent again under the id. `False` when the refusal was said and nothing
        came back to act on; the answer itself, `None` included, otherwise."""
        try:
            return run()
        except session_routes.Refused as e:
            print(display_reply(refusal.use(verb, str(e)) if session_routes.settled(e)
                                else refusal.retry(str(e))), file=sys.stderr)
        except session_routes.Unsent as e:
            print(display_reply(refusal.retry(str(e))), file=sys.stderr)
        return False

    body: dict = {}
    if verb == "announce":
        m = re.fullmatch(r"as (?P<agent>\S+/\S+)(?: on (?P<branch>\S+))?(?: doing (?P<work>.+))?", text)
        if m is None:
            print("malformed announce: as <harness>/<account> [on <branch>] [doing <text>]", file=sys.stderr)
            return 1
        harness, _, account = m["agent"].partition("/")
        doing = (m["work"] or "").strip()
        if m["branch"] and not doing:
            # The branch claim: the announce takes the executed card's title as its doing.
            key = retry_id or occurrence()
            print(f"id {key}", file=sys.stderr)
            try:
                answer = said(lambda: session_routes.announce_branch(
                    session, token, m["branch"], ws, key, harness=harness, account=account))
            except ValueError as e:
                print(f"{e}: declare the work first, or give doing", file=sys.stderr)
                return 1
            if answer is False:
                return 1
            print(answer)
            return 0
        body = {"state": "announce", "harness": harness, "account": account}
        if m["branch"]:
            body["branch"] = m["branch"]
            body["repo"] = session_routes.card_repo(ws)
        if not doing:
            card = said(lambda: session_routes.executing_card(session, token))
            if card is False:
                return 1   # the read's refusal is said; the no-card repair would be false here
            if card is None:
                print("announce carries no doing, and no live card of this session's names one to "
                      "take it from: declare the work first, or give doing", file=sys.stderr)
                return 1
            doing = card["name"]
        body["doing"] = doing
    elif verb == "blocked":
        m = re.fullmatch(r"on (?P<what>.+)", text)
        if m is None:
            print("malformed blocked: on <what>", file=sys.stderr)
            return 1
        body = {"state": "blocked", "blocker": m["what"].strip()}
    else:
        body = {"state": "done", "what": text}
    key = retry_id or occurrence()
    print(f"id {key}", file=sys.stderr)
    if said(lambda: session_routes.status(session, token, key, body)) is False:
        return 1
    print(f"registered: {verb}" + (f" {text}" if text else ""))
    return 0


def main(argv: list[str]) -> int:
    if help_requested("verb", argv):
        print(script_help("verb", topic=argv[0] if len(argv) == 2 else None))
        return 0
    if len(argv) < 3:
        return error("verb", "verb requires a board verb, session, and text")
    verb, session, rest = argv[0], argv[1], argv[2:]
    if verb not in VERBS:
        return error("verb", f"{verb!r} is not a board verb. To speak to a session or the brain, use say.py")
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
    # The grammar is judged before anything is looked up: a malformed invocation crosses no
    # boundary. An announce shape the door's grammar refuses but Go's admits — no doing — is
    # judged by the authority once the workspace is known, never admitted on the incumbent's.
    import re
    door_refused = False
    if verb in ("announce", "blocked", "wait"):
        import verb_grammar
        door_refused = verb_grammar.parse_status(line(verb, session, text)) is None
        go_shape = (verb == "announce"
                    and re.fullmatch(r"as \S+/\S+(?: on \S+)?(?: doing .+)?", text) is not None)
        if door_refused and not go_shape:
            return error("verb", f"malformed {verb}")
    if verb == "ask" and text.startswith("json {"):
        import verb_grammar
        if verb_grammar.structured_ask(text) is None:
            return error("verb", "malformed structured ask: name a non-empty question and valid options")
    # The workspace's authority decides the wire: a Go status takes the fields its own route names.
    try:
        ws = _workspace()
    except (subprocess.SubprocessError, OSError) as e:
        print(f"the workspace lookup failed ({e}), so this verb cannot be written down; nothing "
              f"was sent — send it again", file=sys.stderr)
        return 1
    import session_routes
    if verb in ("announce", "blocked", "done") and ws is not None and session_routes.on_coordination(ws):
        return _on_go(ws, verb, session, text, retry_id)
    if door_refused:
        return error("verb", f"malformed {verb}")
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
