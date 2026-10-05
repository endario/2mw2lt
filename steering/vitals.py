"""Session vitals (doc 30): what a harness says about itself, right now.

Distinct from supervision, which establishes whether a session is *answerable*. Vitals
establish what it *is* — which model, how many tokens it is carrying, on what branch — so the
board can draw a session rather than a placeholder.

Every figure is one the harness itself wrote: Claude Code into its transcript, Codex into the
rollout the agent reads from outside the session (doc 47 §4). Nothing is estimated. The
occupancy denominator is published only where the harness states it — Codex names the window on
every turn and Claude Code names it nowhere — and where it is absent the console supplies one
(doc 30 §5).
"""

from __future__ import annotations

import json
import os
import socket
import time
from pathlib import Path

from wire_instants import is_instant
import records

V = 1
SOURCE = "claude"
# What may state a reading. A harness with a hook states its own; one without it is read from
# outside by the agent on its machine (doc 47 §4), and the source names the harness either way
# because the binding that attributes a reading is keyed on the provider.
SOURCES = ("claude", "codex", "goose")
TAIL = 256 * 1024  # bytes read from the end of a transcript; a long session's file is unbounded

_FIELDS = {"v", "source", "source_version", "provider_session", "runtime_id", "observed_at",
           "model", "vendor", "tokens", "branch", "cwd", "permission_mode", "effort",
           "context_window",
           "stop_reason", "background_tasks", "session_crons", "entrypoint", "agent_type",
           "machine_id", "hostname", "turn"}
_REQUIRED = {"v", "source", "source_version", "provider_session", "observed_at"}
_TOKENS = ("input", "output", "thinking", "cache_read", "cache_creation")
# Claude Code's own permission modes; anything else is a string this does not recognise.
PERMISSION_MODES = {"acceptEdits", "auto", "bypassPermissions", "default", "dontAsk", "plan"}
# `off` is Goose's, resolved as `thinking_effort` (doc 58 §5). The set refuses garbage rather
# than naming one harness's levels, and a whole record is refused for a value outside it.
EFFORTS = {"off", "low", "medium", "high", "xhigh", "max"}
# Where a reading's session stands in its turn, for a harness that posts no turn-end observation of
# its own (#1795): read from its transcript by the agent that reaches it.
TURNS = {"open", "ended", "aborted"}


def tail(path: Path, limit: int = TAIL) -> str:
    """The last `limit` bytes of a file, from the first line boundary inside them.

    A tail that starts mid-record would parse as garbage, so the leading partial line is
    dropped — unless the whole file fits, where there is no partial line to drop.
    """
    with open(path, "rb") as f:
        size = f.seek(0, os.SEEK_END)
        f.seek(max(0, size - limit))
        blob = f.read()
    text = blob.decode("utf-8", "replace")
    if size > limit:
        _, sep, rest = text.partition("\n")
        return rest if sep else ""
    return text


def last_assistant(text: str) -> dict | None:
    """The last complete assistant entry in a transcript tail, or None.

    Read backwards so a session with thousands of entries costs the same as one with ten,
    and so a torn final line — a transcript being appended to as this runs — is skipped
    rather than failing the read.
    """
    # `split`, never `splitlines`: the latter also breaks on U+2028, U+2029, U+0085 and \r,
    # and JavaScript's JSON.stringify leaves those unescaped inside a string — so a transcript
    # line containing one would be torn in half here while `tail` and the file itself still
    # counted it as one, and the entry before it would be published as the current reading.
    for line in reversed(text.split("\n")):
        if '"assistant"' not in line:
            continue  # cheaper than parsing every user entry and attachment on the way past
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if e.get("type") == "assistant" and isinstance(e.get("message"), dict):
            return e
    return None


def _tokens(usage: dict) -> dict:
    """The turn's token figures, each as the harness reported it.

    `input` is Claude Code's own definition of what occupies the context window — the sum of
    the uncached input, the cache it wrote and the cache it read — and the cache halves are
    kept beside it because that sum hides which of them moved.
    """
    out = {"input": (usage.get("input_tokens") or 0)
                    + (usage.get("cache_creation_input_tokens") or 0)
                    + (usage.get("cache_read_input_tokens") or 0),
           "output": usage.get("output_tokens") or 0,
           "cache_read": usage.get("cache_read_input_tokens") or 0,
           "cache_creation": usage.get("cache_creation_input_tokens") or 0}
    thinking = (usage.get("output_tokens_details") or {}).get("thinking_tokens")
    if isinstance(thinking, int):
        out["thinking"] = thinking
    return {k: v for k, v in out.items() if isinstance(v, int)}


def _enum(value: object, allowed: set[str]) -> str | None:
    return value if isinstance(value, str) and value in allowed else None


def _count(value: object) -> int | None:
    return len(value) if isinstance(value, list) else None


def _hostname() -> str | None:
    """This machine's name, trimmed of the `.local` a Bonjour name carries."""
    try:
        name = socket.gethostname()
    except OSError:
        return None
    name = (name or "").removesuffix(".local")
    return name if name and len(name) <= 200 else None


def read(entry: dict | None, hook: dict, observed_at: str, source_version: str) -> dict | None:
    """One vitals record from a transcript's last assistant entry and the hook that found it.

    None when the payload carries no session, or when the transcript yielded no assistant
    entry — a session that has not answered yet has no vitals, which is not the same fact as
    a session with zero tokens.
    """
    sid = hook.get("session_id")
    if not isinstance(sid, str) or not sid or entry is None:
        return None
    message = entry.get("message") or {}
    usage = message.get("usage") or {}
    out = {"v": V, "source": SOURCE, "source_version": source_version,
           "provider_session": sid, "observed_at": observed_at}
    # The machine's own name. A node id tells two machines apart; it does not tell the owner
    # which of theirs they are looking at, and only the machine itself knows.
    host = _hostname()
    if host:
        out["hostname"] = host
    for key, value in (("model", message.get("model")),
                       ("branch", entry.get("gitBranch")),
                       ("cwd", entry.get("cwd")),
                       ("entrypoint", entry.get("entrypoint")),
                       ("stop_reason", message.get("stop_reason")),
                       ("agent_type", hook.get("agent_type"))):
        if isinstance(value, str) and value and len(value) <= 200:
            out[key] = value
    if isinstance(usage, dict) and usage:
        out["tokens"] = _tokens(usage)
    mode = _enum(hook.get("permission_mode"), PERMISSION_MODES)
    if mode:
        out["permission_mode"] = mode
    effort = _enum((hook.get("effort") or {}).get("level") if isinstance(hook.get("effort"), dict)
                   else entry.get("effort"), EFFORTS)
    if effort:
        out["effort"] = effort
    for key, value in (("background_tasks", _count(hook.get("background_tasks"))),
                       ("session_crons", _count(hook.get("session_crons")))):
        if isinstance(value, int):
            out[key] = value
    return out


def stated(source: str, facts: dict | None) -> dict | None:
    """A reading of a harness that publishes none of its own, in the record's own shape.

    None while what was read is not yet a whole one — a rollout whose opening record the poll
    has not reached names no session and no version. Anything else is handed on as it is, so a
    record that is wrong rather than incomplete is refused where every record is, with a reason.
    """
    if not facts:
        return None
    rec = {"v": V, "source": source, **facts}
    host = _hostname()
    if host:
        rec["hostname"] = host
    return rec if _REQUIRED <= set(rec) else None


def validate(rec: object) -> str | None:
    """Why this is not a vitals record the sender could have produced, or None."""
    why = records.shape(rec, _REQUIRED, _FIELDS)
    if why:
        return why
    if rec["v"] != V or rec["source"] not in SOURCES:
        return "unknown version or source"
    why = records.short_strings(rec, ("provider_session", "observed_at", "source_version"), 200)
    if why:
        return why
    # `Store.admit` and `project` both keep the newest by comparing these, and doc 41 §5's join
    # takes an age from one: a stamp that is not an instant is newest for good and ageless.
    if not is_instant(rec["observed_at"]):
        return "observed_at must be an instant"
    why = records.short_strings(rec, ("model", "vendor", "branch", "cwd", "entrypoint", "stop_reason",
                                      "agent_type", "runtime_id", "machine_id", "hostname"),
                                200, optional=True)
    if why:
        return why
    if rec.get("permission_mode") is not None and rec["permission_mode"] not in PERMISSION_MODES:
        return "permission_mode outside the closed set"
    if rec.get("effort") is not None and rec["effort"] not in EFFORTS:
        return "effort outside the closed set"
    if rec.get("turn") is not None and rec["turn"] not in TURNS:
        return "turn outside the closed set"
    why = records.integers(rec, ("background_tasks", "session_crons", "context_window"),
                           least=0, what="a non-negative integer")
    if why:
        return why
    t = rec.get("tokens")
    if t is not None:
        if not isinstance(t, dict) or not set(t) <= set(_TOKENS):
            return "tokens outside the allow-list"
        if any(not isinstance(v, int) or isinstance(v, bool) or v < 0 for v in t.values()):
            return "token counts must be non-negative integers"
    return None


class Store:
    """The latest reading per incarnation, in memory.

    Vitals state what is true now, so a persisted one is worthless by the time a restarted
    daemon could replay it: a restart starts blank and refills at each live session's next
    turn end. Keyed by the full identity tuple doc 17 keeps apart rather than by the provider
    session alone — two incarnations of one session are two runtimes, and a late record from
    the dead one must not overwrite the live one's reading.
    """

    def __init__(self, cap: int = 512):
        self._cap = cap
        self._by_key: dict[tuple[str, str, str], dict] = {}
        self.revision = 0

    def admit(self, rec: object) -> dict:
        why = validate(rec)
        if why:
            return {"refused": why}
        key = (rec["source"], rec["provider_session"], rec.get("runtime_id") or "")
        held = self._by_key.get(key)
        if held is not None and held["observed_at"] > rec["observed_at"]:
            return {"stored": False, "why": "a later reading is held"}
        self._by_key[key] = rec
        if len(self._by_key) > self._cap:  # oldest reading first; the cap is the only eviction
            for k in sorted(self._by_key, key=lambda k: self._by_key[k]["observed_at"])[:len(self._by_key) - self._cap]:
                del self._by_key[k]
        self.revision += 1
        return {"stored": True}

    def latest(self) -> dict[tuple[str, str, str], dict]:
        return dict(self._by_key)

    def counts(self) -> dict:
        return {"vitals_held": len(self._by_key)}


def project(binding: dict, latest: dict, enrolled: dict) -> dict[str, dict]:
    """One reading per steering session: the newest the session's enrollment still owns.

    The binding is what maps an incarnation to an enrollment (doc 17 §5), so a record whose
    tuple nothing has bound projects to nothing rather than to a guess about whose it is. On
    top of that a reading must pass the enrollment it claims, on three counts, because being
    bound once is not the same as being current:

    - **State.** A detached enrollment owns nothing, so a session that has ended shows no
      reading rather than the last one it happened to send.
    - **Epoch.** A binding at any other epoch is a finished incarnation's. Newest-timestamp
      alone let one of those win — a delayed send, or a clock running ahead — and put a dead
      incarnation's model and branch on the board as current.
    - **Node.** `rebind:` moves an enrollment to another tailnet node and does NOT rotate its
      epoch (`enroll.fold`), so the node a session has left keeps a bound tuple at the same
      epoch and would otherwise go on publishing over the node it moved to.
    - **Instant.** A rebound tuple passes all three of the above, so a reading older than the
      enrollment claiming it is refused as well.

    A local enrollment and a loopback reading both carry no node, which compares equal, so the
    node test is the same test in both directions rather than a remote-only special case.
    """
    per: dict[str, dict] = {}
    for key, val in binding.items():
        if val is None:  # a conflict tuple attributes nothing
            continue
        session, epoch = val
        rec, e = latest.get(key), enrolled.get(session)
        if rec is None or e is None or e.get("state") != "enrolled" or e.get("epoch") != epoch:
            continue
        if rec.get("machine_id") != e.get("machine_id"):
            continue
        # - **Instant.** The binding is last-write-wins per tuple and a successor may rebind the
        #   very tuple its predecessor ran on, at which point the epoch no longer separates them.
        #   A reading taken before the enrollment claiming it is the predecessor's, and admitting
        #   it would put a dead incarnation's model and branch on the successor's pill. One-second
        #   resolution and a remote clock make this a floor rather than a proof, and it errs
        #   toward showing no reading (doc 41 §5).
        if rec["observed_at"] < (e.get("ts") or ""):
            continue
        held = per.get(session)
        if held is None or rec["observed_at"] > held["observed_at"]:
            per[session] = rec
    return per


def utc() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def instant_of(entry: dict) -> str | None:
    """The instant a transcript entry was written, to the second, or None when it names none."""
    ts = entry.get("timestamp")
    at = ts[:19] + "Z" if isinstance(ts, str) else None
    return at if is_instant(at) else None
