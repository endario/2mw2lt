#!/usr/bin/env python3
"""`accounts.py [--provider <harness>] [--provider-session <id>] <word> [...]`: the team's
provider accounts as the seat acts on them for the owner, on the seat's delegated bearer
(#9910). Every write goes as the owner the seat's grant names; the grant needs the Access class at
reach "Everything you own", which the owner sets in the console's Team settings, on the delegation
row for this workspace's seat.

Words:
  access                                        the live grant's classes and reach, and what the
                                                owner grants where when they do not cover these words
  list [vendor]                                 the team's accounts, each at its number; the
                                                owner's removed accounts say `removed`
  carry <vendor> <legacy> <qualified>           fold the account at <legacy> into the account it
                                                became at <qualified>, which keeps the lower number
  remove <vendor> <number>                      retire the account; its number frees for another
  restore <vendor> <number>                     bring a removed account back
  number <vendor> <number> <to>                 move the account at <number> to a free number <to>

Arguments name vendor and the account's number in the team, never a raw id: each is resolved
through GET /accounts before any write. `--dry-run` on a write resolves and shows the before and
after, sending nothing. A refusal prints its check and what to do about it, as rest.py does.
"""
from __future__ import annotations

import json
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.append(str(Path(__file__).resolve().parents[1]))
import connect  # noqa: E402
import door  # noqa: E402
import rest  # noqa: E402
from verb_help import error, help_requested, script_help  # noqa: E402

# The grant these words need: the Access class ("whose identity workers push as"), at the reach
# that covers the whole team — the account words act on the team's accounts, not this workspace's.
NEEDED_CLASS, NEEDED_REACH = "access", "grantor"

GRANT_ADVICE = (
    "the owner grants it in the console: Team settings, the delegation row for this workspace's "
    "seat — enable the Access class with reach “Everything you own”")


def enrolment(speaker: dict[str, str]) -> str:
    """The enrolment to exchange — the seat holder's own, as --lease names it, or this session's
    own found as say.py finds it."""
    import lease
    try:
        held = lease.held(speaker)
    except ValueError as why:
        raise SystemExit(error("accounts", str(why)))
    if held is not None:
        return held["token"]
    token, why = rest.own_bearer(speaker)
    if token is None:
        print(f"no enrolment to exchange: {why}", file=sys.stderr)
        raise SystemExit(1)
    return token


def call(base: str, delegated: str | None, method: str, path: str, body: dict | None = None,
         carrier: str | None = None, key: str | None = None) -> dict:
    """One call on the delegated bearer — the session carrier for the claims word — with the
    refusal's check and remedy printed as rest.py prints them."""
    status, answer = rest.fetch(
        f"{base}/api/v1{path}", carrier, method,
        json.dumps(body).encode() if body is not None else None, key, bearer=delegated)
    if 200 <= status < 300:
        return answer
    remedy = rest.REMEDY_ADVICE.get(answer.get("remedy") if isinstance(answer.get("remedy"), str) else "", "")
    print(f"{status} {answer.get('title') or answer.get('refused') or answer}"
          + (f" [{answer['code']}]" if answer.get("code") else "")
          + (f" field {answer['field']}" if isinstance(answer.get("field"), str) and answer["field"] else "")
          + (f": {answer['detail']}" if isinstance(answer.get("detail"), str) and answer["detail"] else "")
          + (f" — {remedy}" if remedy else ""), file=sys.stderr)
    raise SystemExit(1)


def read(base: str, delegated: str) -> tuple[list[dict], list[dict]]:
    """(standing accounts, the owner's removed accounts), each entry as GET /accounts lists it."""
    answer = call(base, delegated, "GET", "/accounts")
    items = [a for a in answer.get("items", []) if isinstance(a, dict)]
    removed = [a for a in answer.get("removed", []) if isinstance(a, dict)]
    return items, removed


def named(vendor: str, number: int, items: list[dict], removed: list[dict],
          gone: bool = False) -> dict:
    """The one account vendor's number names — among the owner's removed when `gone` asks."""
    pool = removed if gone else items
    found = [a for a in pool if a.get("vendor") == vendor and a.get("number") == number]
    if len(found) != 1:
        where = "removed" if gone else "standing"
        said = ", ".join(f"{a.get('vendor')}#{a.get('number')}" for a in pool) or "none"
        print(f"accounts: no {where} {vendor} account stands at number {number} ({where}: {said})",
              file=sys.stderr)
        raise SystemExit(1)
    return found[0]


def whole(said: str, which: str) -> int:
    try:
        return int(said)
    except ValueError:
        raise SystemExit(error("accounts", f"{which} is a whole number, got {said!r}"))


def show(heading: str, rows: list[tuple[str, object]]) -> None:
    print(heading)
    for word, value in rows:
        print(f"  {word}: {value}")


def main(argv: list[str], stdin) -> int:  # noqa: ARG001 — no caller hands a bearer on stdin
    if help_requested("accounts", argv):
        print(script_help("accounts", topic=argv[0] if len(argv) == 2 else None))
        return 0
    words = ("access", "list", "carry", "remove", "restore", "number")
    dry = "--dry-run" in argv
    args = [a for a in argv if a != "--dry-run"]
    speaker: dict[str, str] = {}
    for flag in connect.SPEAKER_FLAGS:
        if flag in args:
            i = args.index(flag)
            if i + 1 >= len(args):
                return error("accounts", f"{flag} requires a value")
            speaker[flag] = args[i + 1]
            del args[i:i + 2]
    if not args or args[0] not in words:
        return error("accounts", "name a word: " + ", ".join(words))
    word, at = args[0], args[1:]
    arities = {"access": (0,), "list": (0, 1), "carry": (3,), "remove": (2,), "restore": (2,),
               "number": (3,)}
    if len(at) not in arities[word]:
        usage = {"access": "", "list": "[vendor]", "carry": "<vendor> <legacy> <qualified>",
                 "remove": "<vendor> <number>", "restore": "<vendor> <number>",
                 "number": "<vendor> <number> <to>"}[word]
        return error("accounts", f"{word} takes: {usage}".rstrip(": "))
    if dry and word in ("access", "list"):
        return error("accounts", f"--dry-run is a write word's, not {word}'s")

    base = door.door_url()
    root, workspace = door.split(base)
    if workspace is None:
        print("this workspace's door names no workspace; set STEERING_DOOR to …/<team>/<workspace>",
              file=sys.stderr)
        return 2
    token = enrolment(speaker)

    if word == "access":
        # The claims word asks coordination what the seat's grant stands at now, on the carrier.
        answer = call(base, None, "POST", "/delegation/claims", carrier=token)
        classes, reach = answer.get("classes") or [], answer.get("reach") or ""
        show("the seat's grant", [("classes", ", ".join(classes) or "none"),
                                  ("reach", reach or "none"), ("grantor", answer.get("grantor"))])
        if NEEDED_CLASS in classes and reach == NEEDED_REACH:
            print(f"the grant covers these words: {NEEDED_CLASS} at reach {reach}")
            return 0
        missing = []
        if NEEDED_CLASS not in classes:
            missing.append("the Access class")
        if reach != NEEDED_REACH:
            missing.append("reach “Everything you own”")
        print("the grant does not cover these words — " + " and ".join(missing)
              + " missing. " + GRANT_ADVICE, file=sys.stderr)
        return 1

    try:
        delegated, _ = rest.exchange(root, workspace, token)
    except rest.ExchangeRefused as why:
        print(why, file=sys.stderr)
        return 1
    items, removed = read(base, delegated)

    if word == "list":
        vendor = at[0] if at else None
        for a in items:
            if vendor is None or a.get("vendor") == vendor:
                print(f"{a.get('vendor')}#{a.get('number')}\tstanding\t{a.get('account_id', '')}")
        for a in removed:
            if vendor is None or a.get("vendor") == vendor:
                print(f"{a.get('vendor')}#{a.get('number')}\tremoved\t{a.get('account_id', '')}")
        return 0

    vendor = at[0]
    number = whole(at[1], "the account's number")
    if word == "carry":
        legacy = named(vendor, number, items, removed)
        qualified = named(vendor, whole(at[2], "the qualified account's number"), items, removed)
        body = {"vendor": vendor, "account_id": legacy.get("account_id"),
                "to": qualified.get("account_id")}
        before = f"{vendor}#{number} standing beside {vendor}#{at[2]}"
        after = f"{vendor}#{number} folded away; the qualified account keeps the lower number"
    elif word == "number":
        to = whole(at[2], "the number it moves to")
        account = named(vendor, number, items, removed)
        body = {"vendor": vendor, "account_id": account.get("account_id"), "number": to}
        before, after = f"{vendor}#{number}", f"{vendor}#{to}"
    else:
        gone = word == "restore"
        account = named(vendor, number, items, removed, gone=gone)
        body = {"vendor": vendor, "account_id": account.get("account_id")}
        before = f"{vendor}#{number} {'removed' if gone else 'standing'}"
        after = {"remove": f"{vendor}#{number} removed; its number frees for another",
                 "restore": f"{vendor}#{number} standing again"}[word]

    if dry:
        show("dry run — nothing was sent",
             [("before", before), ("after", after), ("body", json.dumps(body))])
        return 0
    answer = call(base, delegated, "POST", f"/accounts/{word}", body, key=str(uuid.uuid4()))
    print(json.dumps(answer, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:], sys.stdin))
