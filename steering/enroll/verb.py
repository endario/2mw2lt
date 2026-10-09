#!/usr/bin/env python3
"""`verb.py <verb> <session> <text>`: put a status verb on the board, from any node.

The board verbs are a session's own and the door admits them from the node the session enrolled
from, which is what makes this a client rather than a request to run one somewhere else.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))
from door import display_reply, occurrence, OCCURRENCE  # noqa: E402
import ack  # noqa: E402
from local_workspace import workspace_root  # noqa: E402

# `recommend:` and `ask:` share the session-owned status path and door admission. `say:` remains
# deliberately absent — it is speech routed by `say.py`.
VERBS = ("announce", "blocked", "wait", "done", "recommend", "ask")


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
    """The workspace whose stored token this session's writes are sent on. None only outside any
    checkout; a lookup that could not be made raises."""
    try:
        return workspace_root(Path(os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()), timeout=1.0)
    except subprocess.CalledProcessError as e:
        if "not a git repository" not in (e.stderr or "") or os.environ.get("CLAUDE_PROJECT_DIR"):
            raise
        return None


from verb_help import _invocation as help_invocation, error, help_requested, script_help  # noqa: E402


def _on_go(ws: Path, verb: str, session: str, text: str, retry_id: str | None) -> int:
    """A board verb on a Go workspace: the fields its route names, on the session's carrier, under
    the invocation's occurrence id. An announce may give no doing and take the card it executes'
    title, the brain having ruled there is no separate claim primitive (2026-10-08)."""
    import json as _json
    import refusal
    import session_routes
    try:
        token = _json.loads(ack.token_path(ws, session).read_text())["token"]
    except (OSError, ValueError, KeyError):
        print(f"no stored token for {session}: /2mw2lt:connect first", file=sys.stderr)
        return 1

    def said(run, key: str | None = None):
        """`run`'s answer, with Go's refusal said: a settled one names the verb's remedy, an
        unsettled one is sent again under the id. `False` when the refusal was said and nothing
        came back to act on; the answer itself, `None` included, otherwise. A write under `key`
        that may have been registered unanswered ends with its resend, last on stdout (#3303)."""
        try:
            return run()
        except session_routes.Refused as e:
            unsure = not session_routes.settled(e)
            print(display_reply(refusal.retry(str(e)) if unsure else refusal.use(verb, str(e))), file=sys.stderr)
        except session_routes.Unsent as e:
            unsure = True
            print(display_reply(refusal.retry(str(e))), file=sys.stderr)
        if unsure and key:
            print(f"if it may have been registered, resend with --retry={key}")
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
                    session, token, m["branch"], ws, key, harness=harness, account=account), key)
            except ValueError as e:
                print(f"{e}: declare the work first, or give doing", file=sys.stderr)
                return 1
            if answer is False:
                return 1
            print(answer)
            return 1 if answer.startswith("REJECTED") else 0
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
    elif verb in ("ask", "recommend"):
        return _need_on_go(verb, session, token, text, retry_id, said)
    elif verb == "wait":
        return _wait_on_go(ws, session, token, text, retry_id, said)
    else:
        body = status_body(verb, text)
        if body is None:
            print("malformed blocked: on <what>", file=sys.stderr)
            return 1
    key = retry_id or occurrence()
    print(f"id {key}", file=sys.stderr)
    if said(lambda: session_routes.status(session, token, key, body), key) is False:
        return 1
    print(f"registered: {verb}" + (f" {text}" if text else ""))
    return 0


def status_body(verb: str, text: str) -> dict | None:
    """The status route's body for a `done` or a `blocked` (`on <what>`); None for a malformed one."""
    if verb == "done":
        return {"state": "done", "what": text}
    m = re.fullmatch(r"on (?P<what>.+)", text, re.S)
    return {"state": "blocked", "blocker": m["what"].strip()} if m else None


def _wait_on_go(ws: Path, session: str, token: str, text: str, retry_id: str | None, said) -> int:
    """A wait on Go's waits route: a pull request's merge or verdict in the checkout's repository,
    or a time, told once it holds."""
    import session_routes
    import verb_grammar
    status = verb_grammar.parse_status(line("wait", session, text))
    body: dict = {"kind": status["kind"]}
    if status["kind"] == "at":
        body["at"] = status["on"]
    else:
        body.update(repo=session_routes.card_repo(ws), pr=int(status["on"]))
    if "recheck" in status:
        body["recheck"] = status["recheck"]
    key = retry_id or occurrence()
    print(f"id {key}", file=sys.stderr)
    answer = said(lambda: session_routes.wait(session, token, key, body), key)
    if answer is False:
        return 1
    print(f"registered: wait {session} {answer['id']}")
    return 0


def _need_on_go(verb: str, session: str, token: str, text: str, retry_id: str | None, said) -> int:
    """An ask or a recommendation raised on Go's needs route, about the card the session executes
    when it executes one. A structured ask's fields are the route's own."""
    import session_routes
    import verb_grammar
    body: dict = {"kind": verb, "question": text}
    if verb == "ask" and text.startswith("json {"):
        body = {"kind": verb, **verb_grammar.structured_ask(text)}
    card = said(lambda: session_routes.executing_card(session, token))
    if card is False:
        return 1
    if card is not None:
        body["card"] = card["id"]
    key = retry_id or occurrence()
    print(f"id {key}", file=sys.stderr)
    need = said(lambda: session_routes.raise_need(token, key, body), key)
    if need is False:
        return 1
    print(f"registered: {verb} {need['need']['id']}")
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
    # boundary. An announce with no doing, which the status grammar refuses, is Go's to admit.
    unparsed = False
    if verb in ("announce", "blocked", "wait"):
        import verb_grammar
        unparsed = verb_grammar.parse_status(line(verb, session, text)) is None
        go_shape = (verb == "announce"
                    and re.fullmatch(r"as \S+/\S+(?: on \S+)?(?: doing .+)?", text) is not None)
        if unparsed and not go_shape:
            return error("verb", f"malformed {verb}")
    if verb == "ask" and text.startswith("json {"):
        import verb_grammar
        if verb_grammar.structured_ask(text) is None:
            return error("verb", "malformed structured ask: name a non-empty question and valid options")
    try:
        ws = _workspace()
    except (subprocess.SubprocessError, OSError) as e:
        print(f"the workspace lookup failed ({e}); nothing was sent — send it again", file=sys.stderr)
        return 1
    if ws is None:
        print("not in a checkout: a board verb is sent on the session's stored token, which the "
              "checkout holds; nothing was sent", file=sys.stderr)
        return 1
    return _on_go(ws, verb, session, text, retry_id)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
