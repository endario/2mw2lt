"""A card's line grammar: what a declaration or an edge builds, and whether a card is valid (doc 32 §4)."""
from __future__ import annotations

from track_shape import bounded_text, valid_track
from ulids import new_ulid, valid_ulid
from wire_instants import now


# Strongest first: the order settles a conflict between two trailers, and decides which
# anchored issue speaks for the card.
VERBS = ("resolves", "subsumes", "advances", "spawned", "ref")
ROLES = ("executor", "planned")

# What a line that claims the verb and does not parse is refused with. A card write carries a
# credential, so a line the grammar did not match is refused rather than queued as a message
# for the standup — which reads as success and is how other verbs' malformed lines were lost.
USAGE_REFUSAL = "not a card verb"
STATES = ("card-scoped", "card-branch", "card-unbranch", "card-session", "card-unsession",
          "card-concluded", "card-unconcluded", "card-retired",
          # The keeper's (doc 125 §3), written by the daemon alone.
          "card-observed", "card-pr",
          # A correction, with what it replaced (doc 125 §5).
          "card-reclassified", "card-reanchored",
          # What a card waits on or contributes to (doc 167).
          "card-link", "card-unlink")
SIGNS = ("gate", "anchor")
FIELDS = ("track", "significance", "state", "priority", "major")

# What a correction spells the card's weight in (doc 148 §5): major is the brain's declaration,
# which the knowledge stages' `owes-design` reads.
MAJOR_AFTER = ("major", "ordinary")
SIGNIFICANCE = ("card", "minor")

# How soon a card's work is wanted (#2510), highest first; `normal` until a correction says else.
PRIORITY = ("high", "normal", "low")
EVIDENCE_MAX = 300

# A conclusion's word that a held branch with no merged pull request is still the work (doc 48 §8).
KEEP_BRANCH = "--branch-is-the-work"
NAME_MAX = 80

# An edge's kinds and how one ends (doc 167). `requires` is acyclic; `part-of` is not a wait.
LINKS = ("requires", "part-of")
ENDINGS = ("resolved", "withdrawn")


def _text(v: object, limit: int = NAME_MAX) -> bool:
    return bounded_text(v, limit)


def _valid_anchors(anch: object) -> bool:
    return isinstance(anch, dict) and not set(anch) - set(VERBS) and all(
        isinstance(v, list) and all(isinstance(n, int) and not isinstance(n, bool) and n > 0 for n in v)
        for v in anch.values())


def validate(fact: dict) -> str | None:
    """The refusal, or None. The ledger is append-only, so a fact that reaches it is one every
    later fold has to survive."""
    state = fact.get("state")
    if state not in STATES:
        return f"unknown card state {state!r}"
    if not valid_ulid(fact.get("card")):
        return "card must be a ULID"
    if state == "card-scoped":
        if not _text(fact.get("name")):
            return "name must be one line of at most 80 characters"
        # A session's declaration carries no track: placement is derived (doc 125 §4).
        if fact.get("track") is not None and not valid_track(fact.get("track")):
            return "track must name a lane"
        if fact.get("by") is not None and not _text(fact.get("by"), 120):
            return "by must name who declared it"
        units = fact.get("units")
        if units is not None and not (isinstance(units, list) and all(
                isinstance(u, list) and len(u) == 2 and u[0] in ("d", "s", "b") and _text(u[1], 120)
                for u in units)):
            return "units must be [state, text] pairs with state in d|s|b"
        anch = fact.get("anchors")
        if anch is not None:
            if not isinstance(anch, dict) or set(anch) - set(VERBS):
                return f"anchors must be keyed by {'|'.join(VERBS)}"
            if not _valid_anchors(anch):
                return "an anchor is a positive issue number"
        if "major" in fact and not isinstance(fact["major"], bool):
            return "major is true or false"
    elif state in ("card-branch", "card-unbranch"):
        if not _text(fact.get("repo"), 120) or not _text(fact.get("branch"), 200):
            return "repo and branch are required"
    elif state == "card-session":
        if not _text(fact.get("session"), 120):
            return "session is required"
        if fact.get("role") not in ROLES:
            return f"role must be one of {ROLES}"
    elif state == "card-concluded":
        if not _text(fact.get("by"), 120):
            return "by must name who concluded it"
        if not _text(fact.get("evidence"), EVIDENCE_MAX):
            return f"evidence must be one line of at most {EVIDENCE_MAX} characters"
        if "kept" in fact and fact["kept"] is not True:
            return "kept is true where the conclusion says its branch is the work"
    elif state == "card-unsession":
        if not _text(fact.get("session"), 120):
            return "session is required"
    elif state == "card-retired":
        if not _text(fact.get("why"), 200):
            return "a retirement says why"
        if fact.get("by") is not None and not _text(fact.get("by"), 120):
            return "by must name who retired it"
    elif state in ("card-reclassified", "card-reanchored"):
        if not _text(fact.get("why"), EVIDENCE_MAX):
            return f"a correction says why, in one line of at most {EVIDENCE_MAX} characters"
        if not _text(fact.get("by"), 120):
            return "by must name who corrected it"
        if not isinstance(fact.get("before"), dict):
            return "before must say what it replaced"
        if state == "card-reclassified":
            if fact.get("field") not in FIELDS:
                return f"field must be one of {FIELDS}"
            if fact["field"] == "track" and not valid_track(fact.get("after")):
                return "track must name a lane"
            if fact["field"] == "significance" and fact.get("after") not in SIGNIFICANCE:
                return f"significance must be one of {SIGNIFICANCE}"
            if fact["field"] == "priority" and fact.get("after") not in PRIORITY:
                return f"priority must be one of {PRIORITY}"
            if fact["field"] == "major" and fact.get("after") not in MAJOR_AFTER:
                return f"a card's weight is one of {', '.join(MAJOR_AFTER)}"
            if fact["field"] == "state" and fact.get("after") != "live":
                return "a state is corrected only to live, which undoes the keeper's retirement"
        elif not _valid_anchors(fact.get("after")):
            return f"anchors must be keyed by {'|'.join(VERBS)}, each a positive issue number"
    elif state == "card-observed":
        if fact.get("sign") not in SIGNS:
            return f"sign must be one of {SIGNS}"
        if not _text(fact.get("repo"), 120) or not _text(fact.get("branch"), 200):
            return "repo and branch are required"
    elif state == "card-pr":
        if not _text(fact.get("repo"), 120):
            return "repo is required"
        if not (isinstance(fact.get("pr"), int) and not isinstance(fact.get("pr"), bool) and fact["pr"] > 0):
            return "pr is a positive number"
    elif state in ("card-link", "card-unlink"):
        return _edge_refusal(fact)
    return None


def _edge_refusal(fact: dict) -> str | None:
    if fact.get("kind") not in LINKS:
        return f"kind must be one of {LINKS}"
    if ("to_card" in fact) == ("to_issue" in fact):
        return "an edge names exactly one of to_card or to_issue"
    if "to_card" in fact:
        if not valid_ulid(fact["to_card"]):
            return "to_card must be a ULID"
        if fact["to_card"] == fact["card"]:
            return "a card does not link to itself"
    else:
        i = fact["to_issue"]
        if not (isinstance(i, dict) and set(i) == {"repo", "n"} and _text(i["repo"], 120) and "/" in i["repo"]
                and isinstance(i["n"], int) and not isinstance(i["n"], bool) and i["n"] > 0):
            return "to_issue is {repo: owner/name, n: a positive issue number}"
    if not _text(fact.get("by"), 120):
        return "by must name who drew it"
    if not _text(fact.get("why"), EVIDENCE_MAX):
        return f"an edge says why, in one line of at most {EVIDENCE_MAX} characters"
    if fact["state"] == "card-link":
        if fact.get("provenance") != "declared":
            return "a written edge is declared; an observed one is the forge's and is never written"
        if fact.get("source") is not None and not _text(fact.get("source"), EVIDENCE_MAX):
            return f"source is one line of at most {EVIDENCE_MAX} characters"
    elif fact.get("how") not in ENDINGS:
        return f"how must be one of {ENDINGS}"
    return None


def scoped(name: str, track: str | None, units: list | None = None, card: str | None = None,
           anchors: dict | None = None, by: str | None = None, major: bool = False) -> dict:
    """A card, minted. `card` is the client's id for a new one, or an existing one's to re-scope.

    `units` is omitted rather than written empty: a card that has not said what its blocks are
    is not a card that has said it has none, and the fold falls back to the PR's task list for
    the first while the second would erase it. `major` is the brain's declaration (doc 148 §5),
    so it is written only when true: a session's `declare:` never passes it, and absence reads
    as ordinary.
    """
    fact = {"state": "card-scoped", "card": card or new_ulid(), "name": name}
    if track is not None:
        fact["track"] = track
    if by is not None:
        fact["by"] = by
    if units is not None:
        fact["units"] = units
    if anchors is not None:
        fact["anchors"] = {v: list(anchors.get(v, [])) for v in VERBS if anchors.get(v)}
    if major:
        fact["major"] = True
    return {**fact, "ts": now()}


def _anchors(args: list[str]) -> dict | None:
    """`resolves:398 ref:12,13` as the card's anchors, or None when the caller named none."""
    if not args:
        return None
    out: dict[str, list[int]] = {}
    for a in args:
        verb, _, ns = a.partition(":")
        if verb not in VERBS or not ns:
            raise ValueError(f"anchor must be <verb>:<n>[,<n>], not {a!r}")
        out.setdefault(verb, []).extend(int(n) for n in ns.split(","))
    return out


def _fact(state: str, card: str, **kw) -> dict:
    return {"state": state, "card": card, **kw, "ts": now()}


def declaration(session: str, card: str, words: list[str]) -> tuple[dict | None, str | None]:
    """Build the declaration's first fact without consulting the ledger."""
    tail = len(words)
    while tail and _ANCHOR_ARG.match(words[tail - 1]):
        tail -= 1
    try:
        first = scoped(" ".join(words[:tail]), None, card=card,
                       anchors=_anchors(words[tail:]), by=f"session {session}")
    except ValueError as e:
        return None, str(e)
    refusal = validate(first)
    return (None, refusal) if refusal else (first, None)


def built(verb: str, rest: list[str]) -> tuple[list[dict], str | None]:
    """The fact a verb names, from the arguments alone, or the refusal its grammar earned.

    Separate from `facts` because the grammar can be judged without a ledger, and the CLI
    judges it before sending anything: a verb this file would refuse must not become a round
    trip that comes back refused in someone else's words.
    """
    try:
        if verb == "scope" and len(rest) >= 2 and rest[0] != "--card":
            return [], "scope names its new card with --card <ulid>, so a retry cannot mint a second"
        if verb == "scope" and len(rest) >= 4:
            if not valid_ulid(rest[1]):
                return [], "--card must be a ULID"
            weight, tail = _weight(rest[4:])
            return [scoped(rest[2], rest[3], card=rest[1], anchors=_anchors(tail),
                           major=weight)], None
        if verb == "rescope" and len(rest) >= 3:
            weight, tail = _weight(rest[3:])
            return [scoped(rest[1], rest[2], card=rest[0], anchors=_anchors(tail),
                           major=weight)], None
        if verb in ("branch", "unbranch") and len(rest) == 3:
            return [_fact(f"card-{verb}", rest[0], repo=rest[1], branch=rest[2])], None
        if verb == "session" and len(rest) == 3:
            return [_fact("card-session", rest[0], session=rest[1], role=rest[2])], None
        if verb == "unsession" and len(rest) == 2:
            return [_fact("card-unsession", rest[0], session=rest[1])], None
        if verb == "conclude" and len(rest) in (3, 4):
            if len(rest) == 4 and rest[3] != KEEP_BRANCH:
                return [], USAGE_REFUSAL
            return [_fact("card-concluded", rest[0], by=rest[1], evidence=rest[2],
                          **({"kept": True} if len(rest) == 4 else {}))], None
        if verb == "unconclude" and len(rest) == 1:
            return [_fact("card-unconcluded", rest[0])], None
        if verb == "retire" and len(rest) == 2:
            return [_fact("card-retired", rest[0], why=rest[1])], None
        if verb == "reclassify" and len(rest) >= 4 and rest[1] in FIELDS:
            return [_fact("card-reclassified", rest[0], field=rest[1], after=rest[2],
                          why=" ".join(rest[3:]))], None
        if verb == "link" and len(rest) >= 4:
            # What the edge rests on — a plan's document and sentence — rides before the reason.
            source = {"source": rest[4]} if rest[3] == "--source" and len(rest) >= 6 else {}
            return [_fact("card-link", rest[0], kind=rest[1], **_to(rest[2]), provenance="declared",
                          **source, why=" ".join(rest[5:] if source else rest[3:]))], None
        if verb == "unlink" and len(rest) >= 5:
            return [_fact("card-unlink", rest[0], kind=rest[1], **_to(rest[2]), how=rest[3],
                          why=" ".join(rest[4:]))], None
        if verb == "reanchor" and len(rest) >= 2:
            named = [a for a in rest[1:] if _ANCHOR_ARG.match(a)]
            why = " ".join(a for a in rest[1:] if not _ANCHOR_ARG.match(a))
            if why:
                return [_fact("card-reanchored", rest[0], after=_anchors(named) or {}, why=why)], None
    except (IndexError, ValueError) as e:
        return [], str(e) or USAGE_REFUSAL
    return [], USAGE_REFUSAL


def _to(word: str) -> dict:
    """An edge's target as its argument names it: a card's id, or `owner/name#n`."""
    if valid_ulid(word):
        return {"to_card": word}
    repo, hash_, n = word.rpartition("#")
    if not hash_ or "/" not in repo or not n.isdigit():
        raise ValueError(f"a target is a card's id or <owner>/<name>#<n>, not {word!r}")
    return {"to_issue": {"repo": repo, "n": int(n)}}


# Only the anchor verbs, so a reason's own `pr:123` stays in the reason.
_ANCHOR_ARG = __import__("re").compile(rf"^(?:{'|'.join(VERBS)}):\d[\d,]*$")


def _weight(args: list[str]) -> tuple[bool, list[str]]:
    """The trailing arguments with the `major` keyword pulled out of them, and whether it was
    there: everything else is still an anchor's to name, and one that is neither is refused by
    `_anchors` as before."""
    return "major" in args, [a for a in args if a != "major"]
