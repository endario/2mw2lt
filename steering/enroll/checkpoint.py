#!/usr/bin/env python3
"""`checkpoint.py <session> <boundary> [--text-file <f>] [--learned-file <f.json>] [--mirror <url>]
[--retry=<id>]`: record this session's checkpoint (doc 154 §5), on its stored token, with its text
read from the file or stdin; the platform keeps the text (#6324). `--mirror` posts the same text as
a comment on a pull request or issue, only once the platform has scanned and kept it.

`checkpoint.py <session> read [latest|<id>]` prints a checkpoint's text as this session is shown it.

The pointer form, until every client sends text: `checkpoint.py <session> <boundary> <note url>`, the
`## Checkpoint` comment already posted, whose body is read back from GitHub and hashed here.
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
import rebrief  # noqa: E402
import verb  # noqa: E402
from door import OCCURRENCE, occurrence  # noqa: E402

_COMMENT = re.compile(r"https://github\.com/(?P<repo>[^/\s]+/[^/\s]+)/(?:issues|pull)/\d+#issuecomment-(?P<id>\d+)")
_THREAD = re.compile(r"https://github\.com/(?P<repo>[^/\s]+/[^/\s]+)/(?:issues|pull)/(?P<n>\d+)")
_CHECKPOINT_ID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
_NEEDS = {"--learned-file": "a JSON file", "--text-file": "a file", "--mirror": "a URL"}
_OPTIONS = tuple(_NEEDS)


def note_sha256(url: str) -> str:
    """The sha256 of the comment's body as GitHub holds it now."""
    m = _COMMENT.fullmatch(url)
    if not m:
        raise ValueError(f"not a GitHub issue or pull request comment: {url}")
    body = subprocess.run(["gh", "api", f"repos/{m['repo']}/issues/comments/{m['id']}", "--jq", ".body"],
                          capture_output=True, text=True, check=True, timeout=30).stdout
    # `--jq` ends the string it prints with a newline the body does not hold.
    return hashlib.sha256(body.removesuffix("\n").encode()).hexdigest()


def mirror(url: str, text: str) -> str:
    """Post `text` as a comment on the pull request or issue `url` names: the comment's URL."""
    m = _THREAD.fullmatch(url)
    return subprocess.run(["gh", "api", f"repos/{m['repo']}/issues/{m['n']}/comments", "--method", "POST",
                           "--input", "-", "--jq", ".html_url"], input=json.dumps({"body": text}),
                          capture_output=True, text=True, check=True, timeout=30).stdout.strip()


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
    unknown = next((arg for arg in argv if arg.startswith("--") and arg not in _OPTIONS), None)
    if unknown is not None:
        return error("checkpoint", f"unknown checkpoint option {unknown!r}")
    opts: dict[str, str] = {}
    for name in _OPTIONS:
        if argv.count(name) > 1:
            return error("checkpoint", f"{name} may appear once")
        if name in argv:
            i = argv.index(name)
            if i + 1 >= len(argv) or argv[i + 1].startswith("--"):
                return error("checkpoint", f"{name} requires {_NEEDS[name]}")
            opts[name] = argv[i + 1]
            argv = argv[:i] + argv[i + 2:]
    if len(argv) >= 2 and argv[1] == "read":
        if opts or retry or len(argv) > 3:
            return error("checkpoint", "checkpoint read takes a session and at most latest or a checkpoint id")
        which = argv[2] if len(argv) == 3 else "latest"
        if which != "latest" and not _CHECKPOINT_ID.fullmatch(which):
            return error("checkpoint", f"not a checkpoint id: {which}")
        return _with_token(argv[0], lambda ws, token: _read(argv[0], token, which))
    if len(argv) < 2:
        return error("checkpoint", "checkpoint requires a session and a boundary")
    if len(argv) > 3:
        return error("checkpoint", "checkpoint takes a session, a boundary, and at most a note URL")
    session, boundary, note = (*argv, None) if len(argv) == 2 else argv
    if boundary not in {"spec-settled", "unit-done", "context", "handover", "exit"}:
        return error("checkpoint", f"unknown checkpoint boundary {boundary!r}")
    learned: list = []
    if "--learned-file" in opts:
        try:
            learned = json.loads(Path(opts["--learned-file"]).read_text())
        except (OSError, ValueError) as e:
            return error("checkpoint", f"--learned-file could not be read: {e}")
    import checkpoint_grammar
    if note is not None:
        if "--text-file" in opts or "--mirror" in opts:
            return error("checkpoint", "a note URL is the pointer form, which takes no --text-file or --mirror")
        if not _COMMENT.fullmatch(note):
            return error("checkpoint", f"not a GitHub issue or pull request comment: {note}")
        kept = {"note": note, "note_sha256": "0" * 64}
    else:
        if "--mirror" in opts and not _THREAD.fullmatch(opts["--mirror"]):
            return error("checkpoint", f"--mirror takes a GitHub pull request or issue URL: {opts['--mirror']}")
        if "--text-file" in opts:
            try:
                raw = Path(opts["--text-file"]).read_bytes()
            except OSError as e:
                return error("checkpoint", f"--text-file could not be read: {e}")
        elif sys.stdin is None or sys.stdin.isatty():
            return error("checkpoint", "the checkpoint's text comes from --text-file or stdin")
        else:
            raw = sys.stdin.buffer.read() if hasattr(sys.stdin, "buffer") else sys.stdin.read().encode()
        if why := checkpoint_grammar.text_refusal(raw):
            return error("checkpoint", why)
        kept = {"text": raw.decode("utf-8")}
    probe = {"id": retry or "0" * 32, "boundary": boundary, **kept, "learned": learned}
    parsed = checkpoint_grammar.parse(f"checkpoint: {session} token <t> json {json.dumps(probe)}")
    if isinstance(parsed, str):
        return error("checkpoint", parsed)
    if note is not None:
        try:
            kept["note_sha256"] = note_sha256(note)
        except (ValueError, subprocess.SubprocessError, OSError) as e:
            print(f"the note could not be read back: {e}", file=sys.stderr)
            return 1
    this = retry or occurrence()
    print(f"id {this}", file=sys.stderr)
    body = {"id": this, "boundary": boundary, **kept, **({"learned": learned} if learned else {})}
    return _with_token(session, lambda ws, token: _record(ws, session, token, body, opts.get("--mirror")))


def _with_token(session: str, then) -> int:
    """`then(workspace, token)` on the session's stored token, or why it cannot be read."""
    from ack import token_path
    try:
        ws = verb._workspace()
    except (subprocess.SubprocessError, OSError) as e:
        print(f"the workspace lookup failed ({e}); nothing was sent", file=sys.stderr)
        return 1
    if ws is None:
        print("not in a checkout: a checkpoint is sent on the session's stored token", file=sys.stderr)
        return 1
    try:
        token = json.loads(token_path(ws, session).read_text())["token"]
    except (OSError, ValueError, KeyError):
        print(f"no stored token for {session}: /2mw2lt:connect first", file=sys.stderr)
        return 1
    return then(ws, token)


def _record(ws: Path, session: str, token: str, body: dict, mirror_to: str | None) -> int:
    """Go records the checkpoint on the session's own token; a resend under `--retry` is its replay.
    Only text Go accepted, after its scan, is mirrored."""
    import session_routes
    reply, unsure = session_routes.checkpoint(token, body)
    print(reply)
    if not reply.startswith("checkpointed:"):
        # Last on stdout, so a reader of `2>&1 | tail -1` keeps the id (#3303).
        if unsure:
            print(f"if it may have been recorded, resend with --retry={body['id']}")
        return 1
    try:
        if "note" in body:
            rebrief.remember(ws, session, body["boundary"], note=body["note"])
        else:
            rebrief.remember(ws, session, body["boundary"], checkpoint=reply.split()[1])
    except OSError as e:
        print(f"recorded, but the rebrief's pointer was not kept: {e}", file=sys.stderr)
    if mirror_to:
        try:
            print(f"mirrored: {mirror(mirror_to, body['text'])}")
        except subprocess.CalledProcessError as e:
            print(f"mirror failed: {(e.stderr or '').strip() or f'gh exited {e.returncode}'}")
        except (subprocess.SubprocessError, OSError) as e:
            print(f"mirror failed: {e}")
    return 0


def _read(session: str, token: str, which: str) -> int:
    """The checkpoint's text on stdout, and what it is on stderr; or why there is no text to show."""
    import session_routes
    why, got = session_routes.checkpoint_read(session, token, which)
    if got is None:
        print(why)
        return 1
    print(f"checkpoint {got.get('id')} {got.get('boundary')} by {got.get('session')} at {got.get('at')}",
          file=sys.stderr)
    if got.get("text") is not None:
        print(got["text"], end="" if got["text"].endswith("\n") else "\n")
        return 0
    if got.get("expired"):
        print(f"expired on {got.get('expires_at')}")
    elif got.get("expires_at") is None:
        print(f"pointer: {got.get('note')}")
        return 0
    else:
        print("not shown: only its own session, the seat and a member read a checkpoint's text")
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
