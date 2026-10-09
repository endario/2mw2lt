#!/usr/bin/env python3
"""`checkpoint.py <session> <boundary> <note url> [--learned-file <f.json>] [--retry=<id>]`: record
this session's checkpoint (doc 154 §5), on its stored token, through the session's outbox.

The note is the `## Checkpoint` comment already posted. Its body is read back from GitHub and
hashed here, so a later reader can tell whether it was edited; the daemon reads no GitHub.
"""
from __future__ import annotations

import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))
import outbox  # noqa: E402
import rebrief  # noqa: E402
import verb  # noqa: E402
from door import OCCURRENCE, SETTLED, REFUSED, display_reply, occurrence  # noqa: E402

_COMMENT = re.compile(r"https://github\.com/(?P<repo>[^/\s]+/[^/\s]+)/(?:issues|pull)/\d+#issuecomment-(?P<id>\d+)")


def note_sha256(url: str) -> str:
    """The sha256 of the comment's body as GitHub holds it now."""
    m = _COMMENT.fullmatch(url)
    if not m:
        raise ValueError(f"not a GitHub issue or pull request comment: {url}")
    body = subprocess.run(["gh", "api", f"repos/{m['repo']}/issues/comments/{m['id']}", "--jq", ".body"],
                          capture_output=True, text=True, check=True, timeout=30).stdout
    # `--jq` ends the string it prints with a newline the body does not hold.
    return hashlib.sha256(body.removesuffix("\n").encode()).hexdigest()


from verb_help import error, help_requested, script_help  # noqa: E402


def main(argv: list[str]) -> int:
    if help_requested("checkpoint", argv):
        print(script_help("checkpoint", topic=argv[0] if len(argv) == 2 else None))
        return 0
    retry_flags = [arg for arg in argv if arg.startswith("--retry")]
    if len(retry_flags) > 1:
        return error("checkpoint", "--retry=<id> may appear once")
    retry = None
    if retry_flags:
        flag = retry_flags[0]
        retry = flag.partition("=")[2]
        if not flag.startswith("--retry=") or not OCCURRENCE.fullmatch(retry):
            return error("checkpoint", "--retry=<id> needs the id a previous send printed")
        argv = [arg for arg in argv if arg != flag]
    unknown = next((arg for arg in argv if arg.startswith("--") and arg != "--learned-file"), None)
    if unknown is not None:
        return error("checkpoint", f"unknown checkpoint option {unknown!r}")
    learned: list = []
    if argv.count("--learned-file") > 1:
        return error("checkpoint", "--learned-file may appear once")
    if "--learned-file" in argv:
        i = argv.index("--learned-file")
        if i + 1 >= len(argv) or argv[i + 1].startswith("--"):
            return error("checkpoint", "--learned-file requires a JSON file")
        try:
            learned = json.loads(Path(argv[i + 1]).read_text())
        except (OSError, ValueError) as e:
            return error("checkpoint", f"--learned-file could not be read: {e}")
        argv = argv[:i] + argv[i + 2:]
    if len(argv) < 3:
        return error("checkpoint", "checkpoint requires a session, boundary, and note URL")
    if len(argv) > 3:
        return error("checkpoint", "checkpoint takes exactly a session, boundary, and note URL")
    session, boundary, note = argv
    if boundary not in {"spec-settled", "unit-done", "context", "handover", "exit"}:
        return error("checkpoint", f"unknown checkpoint boundary {boundary!r}")
    if not _COMMENT.fullmatch(note):
        return error("checkpoint", f"not a GitHub issue or pull request comment: {note}")
    import checkpoint_grammar
    probe = {"id": retry or "0" * 32, "boundary": boundary, "note": note,
             "note_sha256": "0" * 64, "learned": learned}
    parsed = checkpoint_grammar.parse(f"checkpoint: {session} token <t> json {json.dumps(probe)}")
    if isinstance(parsed, str):
        return error("checkpoint", parsed)
    try:
        sha = note_sha256(note)
    except (ValueError, subprocess.SubprocessError, OSError) as e:
        print(f"the note could not be read back: {e}", file=sys.stderr)
        return 1
    this = retry or occurrence()
    print(f"id {this}", file=sys.stderr)
    body = {"id": this, "boundary": boundary, "note": note, "note_sha256": sha,
            **({"learned": learned} if learned else {})}
    sent = f"checkpoint: {session} json {json.dumps(body, ensure_ascii=False)}"
    try:
        ws = verb._workspace()
    except (subprocess.SubprocessError, OSError) as e:
        print(f"the workspace lookup failed ({e}); nothing was sent", file=sys.stderr)
        return 1
    if ws is None:
        print("not in a checkout: a checkpoint is sent through the session's outbox", file=sys.stderr)
        return 1
    import session_routes
    if session_routes.on_coordination(ws):
        return _on_go(ws, session, body, boundary, note, this)
    if len(verb.signed(sent, session, ws)) > outbox.LIMIT:  # the door measures the signed line
        print(f"this line signed is over the {outbox.LIMIT} characters the door takes; "
              f"shorten the lessons", file=sys.stderr)
        return 2
    try:
        state, reply = verb._through_outbox(ws, sent, this, session)
    except outbox.Conflict as e:
        print(str(e), file=sys.stderr)
        return 2
    if reply is None:
        return 1
    print(display_reply(reply))
    if reply.startswith("checkpointed:"):
        try:
            rebrief.remember(ws, session, note, boundary)  # what the SessionStart hook points at
        except OSError as e:  # the door has it; only the rebrief's pointer is missing
            print(f"recorded, but the rebrief's pointer was not kept: {e}", file=sys.stderr)
    if state not in (SETTLED, REFUSED):
        # Last on stdout, so it is the line a piped reader keeps (#3303).
        print(f"kept in {outbox.outbox_dir(ws)}; resend with --retry={this}")
    return 0 if reply.startswith("checkpointed:") else 1


def _on_go(ws: Path, session: str, body: dict, boundary: str, note: str, this: str) -> int:
    """Go records the checkpoint on the session's own token; a resend under `--retry` is its replay."""
    import session_routes
    from ack import token_path
    try:
        token = json.loads(token_path(ws, session).read_text())["token"]
    except (OSError, ValueError, KeyError):
        print(f"no stored token for {session}: /2mw2lt:connect first", file=sys.stderr)
        return 1
    reply = session_routes.checkpoint(token, body)
    print(reply)
    if not reply.startswith("checkpointed:"):
        print(f"resend with --retry={this}", file=sys.stderr)
        return 1
    try:
        rebrief.remember(ws, session, note, boundary)
    except OSError as e:
        print(f"recorded, but the rebrief's pointer was not kept: {e}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
