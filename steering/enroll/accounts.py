#!/usr/bin/env python3
"""`accounts.py [--provider <harness>] [--provider-session <id>] <word> [...]`: the team's
provider accounts as the seat acts on them for the owner, on the seat's delegated bearer
(#9910). Every write goes as the owner the seat's grant names; the grant needs the Access class at
reach "Everything you own", which the owner sets in the console's Team settings, on the delegation
row for this workspace's seat.

Words:
  access                                        the live grant's classes and reach, and what the
                                                owner grants where when they do not cover these words
  list [vendor]                                 the team's accounts, each at its number with its
                                                alias beside it; the owner's removed accounts say
                                                `removed`
  carry <vendor> <legacy> <qualified>           fold the account at <legacy> into the account it
                                                became at <qualified>, which keeps the lower number
  remove <vendor> <account>                     retire the account; its number frees for another
  restore <vendor> <account>                    bring a removed account back
  number <vendor> <account> <to>                move the account to a free number <to>

Arguments name vendor and the account's number in the team or the alias the team owner set on it
(the label the desk's nav bar shows), never a raw id: each is resolved through GET /accounts
before any write, within this team only. `--dry-run` on a write resolves and shows the before and
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


def alias_of(account: dict) -> str | None:
    """The alias a session names the account by: the team owner's label, else the grantor's own."""
    return account.get("team_owner_label") or account.get("label") or None


def shown(account: dict) -> str:
    """An account as the seat names it: vendor#number with the alias beside it, as the nav bar."""
    name = f"{account.get('vendor')}#{account.get('number')}"
    if alias_of(account):
        name += f" ({alias_of(account)})"
    return name


def named(vendor: str, selector: str, items: list[dict], removed: list[dict],
          gone: bool = False) -> dict:
    """The one account vendor's number or the team owner's alias names — among the owner's removed
    when `gone` asks. An alias naming more than one account, or a selector naming none, is refused
    with the accounts it could have meant."""
    pool = [a for a in (removed if gone else items) if a.get("vendor") == vendor]
    where = "removed" if gone else "standing"
    try:
        found = [a for a in pool if a.get("number") == int(selector)]
    except ValueError:
        found = [a for a in pool if alias_of(a) == selector]
        if len(found) > 1:
            print(f"accounts: alias {selector!r} names more than one {vendor} account: "
                  f"{', '.join(shown(a) for a in found)} — name it by number instead",
                  file=sys.stderr)
            raise SystemExit(1)
    if len(found) != 1:
        said = ", ".join(shown(a) for a in pool) or "none"
        print(f"accounts: no {where} {vendor} account named {selector!r} ({where}: {said})",
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
                 "remove": "<vendor> <account>", "restore": "<vendor> <account>",
                 "number": "<vendor> <account> <to>"}[word]
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
                print(f"{shown(a)}\tstanding\t{a.get('account_id', '')}")
        for a in removed:
            if vendor is None or a.get("vendor") == vendor:
                print(f"{shown(a)}\tremoved\t{a.get('account_id', '')}")
        return 0

    vendor = at[0]
    if word == "carry":
        legacy = named(vendor, at[1], items, removed)
        qualified = named(vendor, at[2], items, removed)
        body = {"vendor": vendor, "account_id": legacy.get("account_id"),
                "to": qualified.get("account_id")}
        before = f"{shown(legacy)} standing beside {shown(qualified)}"
        after = f"{shown(legacy)} folded away; the qualified account keeps the lower number"
    elif word == "number":
        to = whole(at[2], "the number it moves to")
        account = named(vendor, at[1], items, removed)
        body = {"vendor": vendor, "account_id": account.get("account_id"), "number": to}
        before, after = shown(account), f"{vendor}#{to}"
    else:
        gone = word == "restore"
        account = named(vendor, at[1], items, removed, gone=gone)
        body = {"vendor": vendor, "account_id": account.get("account_id")}
        before = f"{shown(account)} {'removed' if gone else 'standing'}"
        after = {"remove": f"{shown(account)} removed; its number frees for another",
                 "restore": f"{shown(account)} standing again"}[word]

    if dry:
        show("dry run — nothing was sent",
             [("before", before), ("after", after), ("body", json.dumps(body))])
        return 0
    answer = call(base, delegated, "POST", f"/accounts/{word}", body, key=str(uuid.uuid4()))
    print(json.dumps(answer, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:], sys.stdin))
