"""Supervision (doc 15 §6): the Claude adapter's allow-list and the attention reducer."""
from __future__ import annotations

ADMITTED = {"Notification", "PermissionRequest", "PermissionDenied", "PreToolUse", "PostToolUse",
            "PostToolUseFailure", "Elicitation", "ElicitationResult", "Stop", "StopFailure", "UserPromptSubmit",
            "PreCompact"}  # doc 154 §8: a compaction, recorded against the session's checkpoint
TRIGGERS = {"manual", "auto"}  # what `PreCompact` says started it
# What a harness that installs no hook can state about itself (doc 47 §12). Kept apart from
# `ADMITTED`, which is Claude Code's own hook events and is what `hooks.py` wires: this one is
# not a hook and is not wired, and `_boundary` has no rule for it, so it makes an incarnation a
# candidate and says nothing about what the session is doing.
WITNESSED = {"Present"}
HEAD_BYTES = 480
_KEEP = ("prompt_id", "tool_use_id", "elicitation_id", "agent_id", "notification_type", "tool_name")
# Claude's documented StopFailure error types; anything else is text and is not kept.
ERROR_CLASSES = {"rate_limit", "overloaded", "authentication_failed", "oauth_org_not_allowed", "account_on_hold",
                 "billing_error", "invalid_request", "model_not_found", "server_error", "max_output_tokens", "unknown"}
NOTIFICATION_TYPES = {"permission_prompt", "idle_prompt", "auth_success", "elicitation_dialog", "elicitation_url_dialog",
                      "elicitation_complete", "elicitation_response", "agent_needs_input", "agent_completed",
                      "quota_auto_resume_fired", "quota_auto_resume_stale", "quota_auto_resume_disabled"}


MIN_VERSION = (2, 1, 196)  # prompt_id on every hook (doc 15 §3)
# What a permission dialog is about, in the order a tool names it. The owner is being asked to
# allow one act, and this is the argument that says which (#812).
ASK_KEYS = ("command", "file_path", "path", "url", "pattern")
ASK_OPTIONS = 8  # labels kept from one question; the tool offers four and an "Other"


def _version(v: str) -> tuple[int, ...]:
    try:
        return tuple(int(x) for x in v.split(".")[:3])
    except ValueError:
        return ()


def _cut(text: object) -> bool:
    """Whether `head` would leave any of this behind."""
    return isinstance(text, str) and len(text.encode()) > HEAD_BYTES


def head(text: object) -> str | None:
    if not isinstance(text, str) or not text:
        return None
    b = text.encode()[:HEAD_BYTES]
    return b.decode(errors="ignore")


def ask(tool_name: object, tool_input: object) -> dict | None:
    """What a person is being asked, from the tool input the hook delivered (#812).

    `{ask, options?, truncated?}`, kept apart rather than composed into a line: a console
    drawing this in a narrow column elides the command and keeps the tool, and a question's
    options are a list there and a count elsewhere — neither is recoverable from prose.
    """
    if not isinstance(tool_input, dict):
        return None
    if tool_name == "AskUserQuestion":
        questions = tool_input.get("questions")
        q = questions[0] if isinstance(questions, list) and questions and isinstance(questions[0], dict) else {}
        text = q.get("question")
        options = q.get("options") if isinstance(q.get("options"), list) else []
        named = [o["label"] for o in options
                 if isinstance(o, dict) and isinstance(o.get("label"), str) and o["label"]]
        labels = [head(label) for label in named[:ASK_OPTIONS]]
        out = {"ask": head(text)} if isinstance(text, str) and text else {}
        if labels:
            out["options"] = labels
        if out and (_cut(text) or len(labels) < len(named) or any(_cut(label) for label in named)
                    or len(questions if isinstance(questions, list) else []) > 1):
            out["truncated"] = True
        return out or None
    for k in ASK_KEYS:
        v = tool_input.get(k)
        if isinstance(v, str) and v:
            return {"ask": head(v), **({"truncated": True} if _cut(v) else {})}
    return None


def observe(hook: dict, occurrence: str, observed_at: str, source_version: str, epoch: int) -> dict | None:
    """The allow-listed observation from one Claude hook payload — identifiers and enums only.
    None for an event this slice does not consume, a payload without its session, or a
    Claude Code below the version floor (coverage `none`, not a guess)."""
    event = hook.get("hook_event_name")
    sid = hook.get("session_id")
    if event not in ADMITTED or not isinstance(sid, str) or not sid or _version(source_version) < MIN_VERSION:
        return None
    o = {"v": 4, "source": "claude", "source_version": source_version, "session": sid, "epoch": epoch,
         "event": event, "occurrence": occurrence, "observed_at": observed_at}
    for k in _KEEP:
        v = hook.get(k)
        if isinstance(v, str) and v:
            if k == "notification_type":
                o[k] = v if v in NOTIFICATION_TYPES else "unknown"
            elif k == "tool_name":
                # AskUserQuestion is the name the reducer distinguishes; a permission's tool is
                # what the owner is being asked to allow (#812). No other event keeps one.
                if v == "AskUserQuestion" or event == "PermissionRequest":
                    o[k] = v
            else:
                o[k] = v
    if event == "PreCompact":
        # v6: a daemon older than the event answers `future version`, and the sender parks it
        # until that daemon is replaced, rather than refusing it for good.
        o["v"] = 6
        if hook.get("trigger") in TRIGGERS:
            o["trigger"] = hook["trigger"]  # never `custom_instructions`, which is a person's words
    if event == "StopFailure":
        e = hook.get("error")
        o["error_class"] = e if e in ERROR_CLASSES else "unknown"
    if event == "UserPromptSubmit":
        h = head(hook.get("prompt"))
        if h:
            o["prompt_head"] = h
    # The two events that open a dialog somebody has to answer. Every other `PreToolUse` is an
    # ordinary tool call, and keeping its arguments would put every command this session runs
    # in the store for good.
    if event == "PermissionRequest" or (event == "PreToolUse" and hook.get("tool_name") == "AskUserQuestion"):
        a = ask(hook.get("tool_name"), hook.get("tool_input"))
        if a:
            # The version is the floor this record needs, not the adapter's own: a rollout
            # parks what a sink cannot read, and an observation carrying no ask is still v4.
            o["v"] = 5
            for field, k in (("ask_head", "ask"), ("ask_options", "options"), ("ask_truncated", "truncated")):
                if a.get(k):
                    o[field] = a[k]
    return o


_BASIS_RANK = {"claude:Notification": 0, "claude:PermissionRequest": 1, "claude:PreToolUse": 1, "claude:Elicitation": 1}


def _boundary(o: dict) -> dict | None:
    """The attention item an observation opens, or None — including when the boundary lacks
    the id it would be correlated by, since an item nothing can close is not evidence."""
    ev, actor, pid = o["event"], o.get("agent_id") or "session", o.get("prompt_id")
    if ev == "Notification" and o.get("notification_type") == "permission_prompt" and pid:
        return {"kind": "permission", "correlation": "prompt", "id": pid, "actor": actor}
    if ev == "Notification" and o.get("notification_type") == "agent_needs_input" and pid:
        return {"kind": "question", "correlation": "prompt", "id": pid, "actor": actor}
    if ev == "PermissionRequest":
        if o.get("tool_use_id"):
            return {"kind": "permission", "correlation": "tool", "id": o["tool_use_id"], "actor": actor}
        return {"kind": "permission", "correlation": "prompt", "id": pid, "actor": actor} if pid else None
    if ev == "PreToolUse" and o.get("tool_name") == "AskUserQuestion" and o.get("tool_use_id"):
        return {"kind": "question", "correlation": "tool", "id": o["tool_use_id"], "actor": actor}
    if ev == "Elicitation":
        if o.get("elicitation_id"):
            return {"kind": "elicitation", "correlation": "elicitation", "id": o["elicitation_id"], "actor": actor}
        return {"kind": "elicitation", "correlation": "prompt", "id": pid, "actor": actor} if pid else None
    return None


def state() -> dict:
    """What one incarnation's observations have been folded into, kept across calls (doc 56 §9).

    The closed ids are carried rather than applied, because a fold cannot know whether the item
    they close is in the bytes it has read or in bytes it has not.
    """
    return {"open": {}, "closed_tools": set(), "closed_elic": set(), "ended": set(), "submitted": set(), "count": 0}


def encode(st: dict) -> dict:
    """A state as JSON holds it, for a fold kept across a restart."""
    return {"open": [[list(k), b] for k, b in st["open"].items()], "closed_tools": list(st["closed_tools"]),
            "closed_elic": list(st["closed_elic"]), "ended": list(st["ended"]),
            "submitted": [list(x) for x in st["submitted"]], "count": st["count"]}


def decode(d: dict) -> dict:
    return {"open": {tuple(k): b for k, b in d["open"]}, "closed_tools": set(d["closed_tools"]),
            "closed_elic": set(d["closed_elic"]), "ended": set(d["ended"]),
            "submitted": {tuple(x) for x in d["submitted"]}, "count": d["count"]}


def _keep(keyed: dict, key: tuple, b: dict) -> None:
    """One boundary into the open items. Seen again: earliest sighting, strongest basis,
    whichever order they came in — which is what lets disjoint folds be merged."""
    cur = keyed.get(key)
    if cur is None:
        keyed[key] = b
        return
    cur["observed_at"] = min(cur["observed_at"], b["observed_at"])
    cur["basis"] = min(cur["basis"], b["basis"], key=lambda x: (_BASIS_RANK.get(x, 9), x))
    # One of the two sightings carries the ask: `Notification` names no tool, and the
    # `PermissionRequest` for the same prompt does.
    for k in ("tool", "ask", "options", "truncated", "prompt_id"):
        if k not in cur and k in b:
            cur[k] = b[k]


def fold(st: dict, o: dict) -> None:
    """One observation into an incarnation's state."""
    ev = o["event"]
    if ev in ("PostToolUse", "PostToolUseFailure", "PermissionDenied") and o.get("tool_use_id"):
        st["closed_tools"].add(o["tool_use_id"])
    if ev == "ElicitationResult" and o.get("elicitation_id"):
        st["closed_elic"].add(o["elicitation_id"])
    if ev in ("Stop", "StopFailure") and o.get("prompt_id"):
        st["ended"].add(o["prompt_id"])
    if ev == "UserPromptSubmit" and o.get("prompt_id") and not o.get("agent_id"):
        st["submitted"].add((o["prompt_id"], o["observed_at"]))
    st["count"] += 1
    b = _boundary(o)
    if b is None:
        return
    b["basis"] = f"claude:{o['event']}"; b["observed_at"] = o["observed_at"]
    if o.get("prompt_id"):
        b["prompt_id"] = o["prompt_id"]
    # What is being asked, where the observation that opened the item carried it (#812): a
    # dialog the owner cannot see the content of is one they have to go and look at anyway.
    for k, field in (("tool", "tool_name"), ("ask", "ask_head"),
                     ("options", "ask_options"), ("truncated", "ask_truncated")):
        if o.get(field):
            b[k] = o[field]
    _keep(st["open"], (b["kind"], b["correlation"], b["id"], b["actor"]), b)


def moves_attention(o: dict, opened: set) -> bool:
    """Whether folding `o` can change what `finish` reports open, given `opened`, the
    `(correlation, id)` of every item any incarnation has opened, which this extends. A boundary
    can; a close can only when it names an item one of them opened. Most observations are
    neither, so a reader that follows this follows what a person is asked, not every hook."""
    b = _boundary(o)
    if b is not None:
        opened.add((b["correlation"], b["id"]))
        if b["actor"] == "session" and o.get("prompt_id"):
            # What a turn's end or a new prompt closes (#3518): this turn's, or any main-thread item.
            opened.update({("turn", o["prompt_id"]), ("main", None)})
        return True
    ev = o["event"]
    if ev in ("PostToolUse", "PostToolUseFailure", "PermissionDenied"):
        return ("tool", o.get("tool_use_id")) in opened
    if ev == "ElicitationResult":
        return ("elicitation", o.get("elicitation_id")) in opened
    if ev in ("Stop", "StopFailure"):
        return ("prompt", o.get("prompt_id")) in opened or ("turn", o.get("prompt_id")) in opened
    if ev == "UserPromptSubmit":
        return ("main", None) in opened and not o.get("agent_id")
    return False


def _latest_two(submitted: set) -> tuple:
    """The newest submission's prompt and time, and the newest time of any other prompt: what
    `new-turn` needs of every submission, read once rather than once per item."""
    first = max(submitted, key=lambda s: s[1], default=(None, ""))
    return first[0], first[1], max((at for p, at in submitted if p != first[0]), default="")


def _closed_by(b: dict, tools: set, elic: set, ended: set, newest: tuple) -> str | None:
    """Which rule closes an item, or None while it stands (#3518 §4.4). Its own close first; the
    two turn rules hold only for the main thread, which cannot take a turn while at a dialog."""
    if ((b["correlation"] == "tool" and b["id"] in tools) or (b["correlation"] == "elicitation" and b["id"] in elic)
            or (b["correlation"] == "prompt" and b["id"] in ended)):
        return "hook-closed"
    pid = b.get("prompt_id")
    if b["actor"] != "session" or not pid:
        return None
    if pid in ended:
        return "turn-ended"
    p1, at1, at2 = newest
    if (at1 if p1 != pid else at2) > b["observed_at"]:
        return "new-turn"
    return None


def finish(states: list[dict]) -> dict:
    """One record from the states of the incarnations one enrollment holds.

    The closed sets are united across all of them before anything is filtered, so a close folded
    from one incarnation's bytes closes the item another opened. The held state is copied on the
    way out and never edited. `attention.closed` names each item a rule closed, and which.
    """
    tools: set = set()
    elic: set = set()
    ended: set = set()
    submitted: set = set()
    for st in states:
        tools |= st["closed_tools"]
        elic |= st["closed_elic"]
        ended |= st["ended"]
        submitted |= st["submitted"]
    keyed: dict[tuple, dict] = {}
    for st in states:
        for key, b in st["open"].items():
            _keep(keyed, key, dict(b))
    kept, closed = [], []
    newest = _latest_two(submitted)
    for b in keyed.values():
        rule = _closed_by(b, tools, elic, ended, newest)
        if rule is None:
            kept.append(b)
        else:
            closed.append({**{k: b[k] for k in ("kind", "correlation", "id", "actor", "prompt_id") if k in b},
                           "rule": rule})
    order = lambda b: (b["kind"], b["correlation"], str(b["id"]), b["actor"])
    open_items = sorted(kept, key=order)
    return {"liveness": {"value": "unknown", "coverage": "none", "basis": "no-probe"},
            "activity": {"value": "unknown", "coverage": "none", "basis": "unordered-source"},
            "attention": {"coverage": "observed-transitions", "open": open_items, "closed": sorted(closed, key=order),
                          "value": "possibly_open" if open_items else "none"}}


def without_closed(record: dict | None) -> dict | None:
    """A record as the registry serves it, without the closes only the needs tick reads."""
    if record is None:
        return None
    return {**record, "attention": {k: v for k, v in record["attention"].items() if k != "closed"}}


def reduce(observations: list[dict]) -> dict:
    """One record from the set of an enrollment's observations. Every rule is a set operation,
    so delivery order and duplicates cannot change the result.

    One pass over everything, which is what an incremental fold of the same observations has to
    equal — so it is the same fold, run from empty.
    """
    st = state()
    for o in observations:
        fold(st, o)
    return finish([st])


def waiting(record: dict) -> dict | None:
    """The newest open attention item as a session row carries it, or None (#812).

    Newest rather than oldest: a person answers the dialog in front of them, and that is the
    one that opened last. The rest stay in `attention.open`, which nothing here summarises away.

    A permission's tool and its argument travel apart, a question's options travel as a list,
    and `text` carries anything that is neither — an elicitation has no tool and no options,
    and a reader with one line to draw needs something in it.
    """
    items = record.get("attention", {}).get("open") or []
    if not items:
        return None
    newest = max(items, key=lambda b: (b.get("observed_at") or "", b["kind"], str(b["id"])))
    # The id is what a row names this dialog by, so dismissing it hides this one and not the
    # next the session opens (doc 119 §2.1).
    out = {"kind": newest["kind"], "since": newest.get("observed_at"), "id": str(newest["id"])}
    if newest["kind"] == "question":
        named = {"question": newest.get("ask"), "options": newest.get("options")}
    elif newest["kind"] == "permission":
        named = {"tool": newest.get("tool"), "detail": newest.get("ask")}
    else:
        named = {"text": newest.get("ask")}
    out.update({k: v for k, v in named.items() if v})
    if newest.get("truncated"):
        out["truncated"] = True
    return out


def project(binding: dict, live_epochs: set[tuple[str, int]], states_of) -> dict[tuple[str, int], dict]:
    """One record per (steering session, epoch).

    `states_of()` gives the state of every incarnation the store holds, keyed by
    `(source, provider session, runtime)`. The grouping is assembled here, from the ledger, at
    every call — so a bind, rebind or detach changes the answer with no observation appended
    (doc 56 §9).
    """
    held = states_of()
    per: dict[tuple[str, int], dict] = {}
    for key, val in binding.items():
        if val is None or val not in live_epochs:  # conflicts and enrollment history attribute nothing live
            continue
        provider, psession, runtime = key
        rec = per.setdefault(val, {"incarnations": 0, "states": []})
        rec["incarnations"] += 1
        st = held.get((provider, psession, runtime))
        if st is not None:
            rec["states"].append(st)
    return {k: {"epoch": k[1], "incarnations": rec["incarnations"],
                "observations": sum(st["count"] for st in rec["states"]),
                **finish(rec["states"])} for k, rec in per.items()}
