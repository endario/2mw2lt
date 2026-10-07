"""The status verbs a session sends, and a structured ask's shape."""
from __future__ import annotations

import json
import re
import time


REDACTED = "<redacted>"

# A status line carries the session's own token after its name (doc 131 §5), as `ask:` does;
# by the time it parses the token has been redacted, and the credential arrives beside the line.
_TOKEN_SLOT = r"(?:\s+token\s+<redacted>)?"
_ANNOUNCE = re.compile(r"^announce:\s*(?P<session>\S+)" + _TOKEN_SLOT + r"\s+as\s+(?P<agent>\S+)(?:\s+on\s+(?P<on>\S+))?\s+doing\s+(?P<work>.+)$", re.S)
_BLOCKED = re.compile(r"^blocked:\s*(?P<session>\S+)" + _TOKEN_SLOT + r"\s+on\s+(?P<what>.+)$", re.S)
_DONE = re.compile(r"^done:\s*(?P<session>\S+)" + _TOKEN_SLOT + r"\s*(?P<what>.*)$", re.S)

# The brain proposes; the owner clears (doc 13 R-B). Never self-dispatched.
_RECOMMEND = re.compile(r"^recommend:\s*(?P<session>\S+)" + _TOKEN_SLOT + r"\s+(?P<text>.+)$", re.S)

# A session's question for the owner (doc 51), on the session's own token as every status verb is.
# The token is redacted by the time this parses; the credential arrives beside the line.
_ASK = re.compile(r"^ask:\s*(?P<session>\S+)\s+token\s+(?P<token>\S+)\s+(?P<text>.+)$", re.S)

# A structured ask (doc 51 §9, #1309): `json ` followed by an object naming the question, an
# optional longer context and, optionally, options to choose from. Plain text stays plain text
# — text opening `json {` is the only thing that turns it into a choice.
_ASK_JSON_PREFIX = "json "


def _ask_options(raw) -> list[dict] | None:
    """A structured ask's options, validated, or None if the shape is wrong. Each option needs
    a non-empty label; a description and a recommended mark are both optional."""
    if not isinstance(raw, list) or not raw:
        return None
    out = []
    for o in raw:
        if not isinstance(o, dict) or not isinstance(o.get("label"), str) or not o["label"].strip():
            return None
        opt = {"label": o["label"].strip()}
        if isinstance(o.get("description"), str) and o["description"].strip():
            opt["description"] = o["description"].strip()
        if o.get("recommended") is True:
            opt["recommended"] = True
        out.append(opt)
    return out


def structured_ask(raw: str) -> dict | None:
    """The structured fields of an `ask:` whose text opens `json {`, or None when the payload
    fails to validate. A text that does not open that way is prose, so `json schema or protobuf?`
    stays a plain question."""
    try:
        payload = json.loads(raw[len(_ASK_JSON_PREFIX):])
    except ValueError:
        return None
    if not isinstance(payload, dict) or not isinstance(payload.get("question"), str) \
            or not payload["question"].strip():
        return None
    out = {"question": payload["question"].strip()}
    if isinstance(payload.get("context"), str) and payload["context"].strip():
        out["context"] = payload["context"].strip()
    if "options" in payload:
        options = _ask_options(payload["options"])
        if options is None:
            return None
        out["options"] = options
        out["multi"] = payload.get("multi") is True
        out["allow_other"] = payload.get("allow_other") is True
    return out


def parse_status(text: str) -> dict | None:
    """An inbox message's registry entry, or None when it is not a status message."""
    ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    m = _ANNOUNCE.match(text)
    if m:
        return {"state": "announce", "session": m["session"], "agent": m["agent"],
                "work": m["work"].strip(), "ts": ts, **({"on": m["on"]} if m["on"] else {})}
    m = _BLOCKED.match(text)
    if m:
        return {"state": "blocked", "session": m["session"], "on": m["what"].strip(), "ts": ts}
    m = _DONE.match(text)
    if m:
        return {"state": "done", "session": m["session"], "what": m["what"].strip(), "ts": ts}
    m = _RECOMMEND.match(text)
    if m:
        return {"state": "recommend", "session": m["session"], "text": m["text"].strip(), "ts": ts}
    m = _ASK.match(text)
    if m and m["text"].strip():
        return {"state": "ask", "session": m["session"], "text": m["text"].strip(), "ts": ts}
    return None
