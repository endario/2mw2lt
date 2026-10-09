#!/usr/bin/env python3
"""`card.py --lease [--provider <harness> --provider-session <id>] <verb> <args…>`: a brain's board
write (doc 32 §4), sent to Go as the seat's holder on the session's own enrolment. `cards.py --token -
<verb> <args…>` writes the ledger where it is this machine's."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parents[1]))
import python_floor  # noqa: E402

python_floor.require()

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))
import door  # noqa: E402
from card_grammar import USAGE_REFUSAL, built  # noqa: E402
from secret_input import read_secret  # noqa: E402
from ulids import new_ulid  # noqa: E402
from verb_help import help_requested, script_help  # noqa: E402


def main(argv: list[str], local=None, prog: str = "card.py") -> int:
    """`local(token, verb, rest, usage)` is the writer a caller holding the ledger passes, for
    `--token -`; it answers None when the ledger is not this machine's."""
    usage = ("usage: card.py --lease scope [--card <ulid>] <name> <track> [<verb>:<n>[,<n>] ...] [major]\n"
             "       card.py --lease rescope <card> <name> <track> [<verb>:<n>[,<n>] ...] [major]\n"
             "       card.py --lease branch <card> <repo> <branch>\n"
             "       card.py --lease unbranch <card> <repo> <branch> [<why>]  (Go requires the why)\n"
             "       card.py --lease session <card> <session> executor|planned\n"
             "       card.py --lease unsession <card> <session> [<why>]  (Go requires the why)\n"
             "       card.py --lease conclude <card> <by> <evidence> [--branch-is-the-work]\n"
             "       card.py --lease unconclude <card> [<why>]  (Go requires the why)\n"
             "       card.py --lease retire <card> <why>\n"
             "       card.py --lease reclassify <card> track <lane>|off-track <why>\n"
             "       card.py --lease reclassify <card> significance|state|priority <value> <why>\n"
             "       card.py --lease reclassify <card> major major|ordinary <why>\n"
             "       card.py --lease reanchor <card> [<verb>:<n>[,<n>] ...] <why>\n"
             "       card.py --lease link <card> requires|part-of <card>|<owner>/<name>#<n> [--source <where>] <why>\n"
             "       card.py --lease unlink <card> requires|part-of <card>|<owner>/<name>#<n> resolved|withdrawn <why>\n"
             "       card.py --lease outcome <card> need <how it is handled now> "
             "[reading facts <state> [<field>=<value>] per <state> [<field>=<value>]] target <comparison> "
             "window <n>d [uses <n>] starts merge|rollout [baseline stated <value and how measured>|baseline none] "
             "[stop <rule>]\n"
             "\n"
             "       `major` is the brain's declaration that a card's work is major (doc 148 §5): "
             "a major card owes a spec until a critic-passed knowledge record names it.\n"
             "       `outcome` is the brain's alone (doc 185): each text field is one quoted argument, and a "
             "brief with a reading has the daemon take its baseline as it is written.")
    usage = usage.replace("card.py", prog)
    if prog == "card.py" and help_requested("card", argv, bare=False):
        print(script_help("card"))
        return 0
    if argv[:1] == ["--lease"]:
        import connect
        import lease as lease_mod
        parsed = connect.speaker_flags(argv[1:])
        if parsed is None:
            print(usage, file=sys.stderr)
            return 2
        flags, rest = parsed
        try:
            held = lease_mod.held(flags)
        except ValueError as why:
            print(f"refused: {why}", file=sys.stderr)
            return 2
        if held is None:
            print(lease_mod.NONE, file=sys.stderr)
            return 1
        if len(rest) < 1:
            print(usage, file=sys.stderr)
            return 2
        try:
            rest, retry = door.retry_args(rest)
        except ValueError as why:
            print(f"refused: {why}", file=sys.stderr)
            return 2
        if not rest:
            print(usage, file=sys.stderr)
            return 2
        return _on_go(held, rest[0], rest[1:], usage, retry)
    if len(argv) < 3 or argv[0] != "--token":
        print(usage, file=sys.stderr)
        return 2
    token, verb, rest = argv[1], argv[2], argv[3:]
    if token != "-":
        print("refused: --token takes only -, and reads the lease token from stdin", file=sys.stderr)
        return 2
    if local is None:
        print("refused: no lease token is kept; send the verb with card.py --lease", file=sys.stderr)
        return 2
    token = read_secret("lease token: ")
    if not token:
        print("refused: --token - reads the lease token from stdin, and none arrived", file=sys.stderr)
        return 2
    return _scoped(token, verb, rest, usage, local)


def _on_go(held: dict, verb: str, rest: list[str], usage: str, retry: str | None = None) -> int:
    """The seat's card verb on a Go workspace: the work route it maps to, on the session's own
    carrier, which Go runs as the seat while this session holds it. Each invocation is its own
    write, named by the id it prints; `--retry=<id>` sends that write again."""
    import refusal
    import session_routes
    ruled = session_routes.ruled_out(f"card {verb} {rest[1]}" if verb == "reclassify" and len(rest) > 1 else f"card {verb}")
    if ruled:
        print(ruled)
        return 1
    if verb == "scope" and rest[:1] != ["--card"]:
        rest = ["--card", new_ulid(), *rest]
    facts, bad_grammar = built(verb, rest)
    if bad_grammar:
        print(usage if bad_grammar == USAGE_REFUSAL else f"refused: {bad_grammar}", file=sys.stderr)
        return 2
    fact = facts[0]
    key = retry or door.occurrence()
    print(f"id {key}", file=sys.stderr)
    try:
        anchored = fact.get("anchors") or fact["state"] == "card-reanchored" and fact["after"]
        repo = session_routes.card_repo(held["ws"]) if anchored else ""
        print(session_routes.seat_card(held["session"], held["token"], verb, fact, repo, key))
        return 0
    except session_routes.NotSeated as why:
        print(f"refused: this session does not hold the seat ({why})")
    except LookupError as why:
        print(refusal.escalate(str(why), to="seat"))
    except session_routes.Unsent as e:
        print(refusal.retry(str(e)))
        print(f"if this write may have been made, resend it with --retry={key}", file=sys.stderr)
    except session_routes.Refused as e:
        print(refusal.use("card", str(e)) if session_routes.settled(e) else refusal.retry(str(e)))
        if not session_routes.settled(e):
            print(f"if this write may have been made, resend it with --retry={key}", file=sys.stderr)
    if verb == "scope":
        print(f"to retry this scope under the same card, send it with --card {rest[1]}", file=sys.stderr)
    return 1


def _scoped(token: str, verb: str, rest: list[str], usage: str, local) -> int:
    if verb == "scope" and rest[:1] != ["--card"]:
        # The id is minted here, before anything is sent, so the same write can be made again
        # under it: a rerun with it is answered from the record (doc 125 §3).
        rest = ["--card", new_ulid(), *rest]
        code = _send(token, verb, rest, usage, local)
        if code:
            print(f"to retry this scope under the same card, send it with --card {rest[1]}", file=sys.stderr)
        return code
    return _send(token, verb, rest, usage, local)


def _send(token: str, verb: str, rest: list[str], usage: str, local) -> int:
    code = local(token, verb, rest, usage)
    if code is not None:
        return code
    print("refused: the ledger is not this machine's; send the verb with card.py --lease", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
