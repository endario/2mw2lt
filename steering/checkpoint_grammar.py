"""A session's checkpoint line and its note, as the client writes it and the daemon reads it (doc 154 §5)."""
from __future__ import annotations

import json
import re

import verb_grammar


BOUNDARIES = ("spec-settled", "unit-done", "context", "handover", "exit")
MAX_LEARNED = 8
MAX_TEXT = 500
MAX_NOTE = 300
_LINE = re.compile(r"\Acheckpoint:\s*(?P<session>\S+)\s+token\s+\S+\s+json\s+(?P<body>.+?)\s*\Z", re.S)

# The characters a URL carries, less `<` and `>`: a note redaction reached reads `<redacted>`.
_NOTE = re.compile(r"https://github\.com/[A-Za-z0-9._~:/?#@!$&'()*+,;=%-]+")
_SHA = re.compile(r"[0-9a-f]{64}")
_ID = re.compile(r"[A-Za-z0-9-]{16,64}")  # the client's occurrence id (`door.OCCURRENCE`)
_FIELDS = {"id", "boundary", "note", "note_sha256", "learned"}


def _scope_ok(scope) -> bool:
    return (isinstance(scope, list) and bool(scope)
            and all(isinstance(p, str) and (p == "*" or (p and len(p) <= 200 and not p.startswith("/")
                                                        and ".." not in p.split("/") and not re.search(r"\s", p)))
                    for p in scope))


def parse(text: str) -> dict | str:
    """`checkpoint: <session> token <t> json <object>`, redacted by ingress before this sees it:
    the parsed checkpoint, or why it is malformed. A lesson redaction changed is set aside under
    `dropped`, by its place in the list from 1, and the rest stand."""
    m = _LINE.match(text)
    if not m:
        return "malformed checkpoint"
    try:
        body = json.loads(m["body"])
    except ValueError:
        return "the checkpoint's body is not JSON"
    if not isinstance(body, dict) or set(body) - _FIELDS:
        return f"a checkpoint's body is an object of {', '.join(sorted(_FIELDS))}"
    if not isinstance(body.get("id"), str) or not _ID.fullmatch(body["id"]):
        return "id is 16 to 64 letters, digits or dashes"
    if body.get("boundary") not in BOUNDARIES:
        return f"boundary is one of {', '.join(BOUNDARIES)}"
    note = body.get("note")
    if not isinstance(note, str) or len(note) > MAX_NOTE or not _NOTE.fullmatch(note):
        return f"note is a https://github.com/ URL of at most {MAX_NOTE} characters"
    if not isinstance(body.get("note_sha256"), str) or not _SHA.fullmatch(body["note_sha256"]):
        return "note_sha256 is 64 lowercase hex characters"
    learned = body.get("learned", [])
    if not isinstance(learned, list) or len(learned) > MAX_LEARNED:
        return f"learned is a list of at most {MAX_LEARNED} items"
    kept, dropped = [], []
    for n, item in enumerate(learned, 1):
        if not (isinstance(item, dict) and set(item) == {"text", "scope"} and isinstance(item["text"], str)
                and 0 < len(item["text"]) <= MAX_TEXT and _scope_ok(item["scope"])):
            return (f"learned item {n} is {{text, scope}}: text of 1 to {MAX_TEXT} characters, scope a "
                    f"list of repository paths or [\"*\"]")
        if verb_grammar.REDACTED in json.dumps(item):
            dropped.append(n)
        else:
            kept.append({"text": item["text"], "scope": item["scope"]})
    return {"session": m["session"], "id": body["id"], "boundary": body["boundary"], "note": note,
            "note_sha256": body["note_sha256"], "learned": kept, "dropped": dropped}
