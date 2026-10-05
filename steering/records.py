"""The checks every record validator here shares (#2044): a record is an object with its required
fields and nothing outside its allow-list, and its string and integer fields are bounded. Each
module keeps its own rules and its own order; these are the steps they all spell the same way,
with the words a refusal has always carried."""
from __future__ import annotations


def shape(rec: object, required: set[str], fields: set[str]) -> str | None:
    """Why `rec` is not an object with `required` and only `fields`, or None."""
    if not isinstance(rec, dict):
        return "not an object"
    if not required <= set(rec):
        return "required fields missing"
    return allowed(rec, fields)


def allowed(rec: dict, fields: set[str]) -> str | None:
    return None if set(rec) <= fields else "fields outside the allow-list"


def short_strings(rec: dict, keys, limit: int, *, optional: bool = False) -> str | None:
    """Each of `keys` a non-empty string of at most `limit` characters; with `optional`, a key
    absent or None passes."""
    for key in keys:
        v = rec.get(key)
        if optional and v is None:
            continue
        if not isinstance(v, str) or not v or len(v) > limit:
            return f"{key} must be a short string"
    return None


def integers(rec: dict, keys, *, least: int, what: str) -> str | None:
    """Each of `keys`, where present and not None, an int (never a bool) of at least `least`."""
    for key in keys:
        v = rec.get(key)
        if v is not None and (not isinstance(v, int) or isinstance(v, bool) or v < least):
            return f"{key} must be {what}"
    return None


def machine_of(fact: dict) -> str | None:
    """The machine a fact or observation names: `machine_id`, or `tailnet_node` on one written
    before doc 143 §7, which the ledger and the observation store keep as written."""
    return fact.get("machine_id") or fact.get("tailnet_node")


def naming_machine(machine: str | None) -> dict:
    """The fields a fact or observation written now names its machine by."""
    return {"machine_id": machine}
