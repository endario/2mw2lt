#!/usr/bin/env python3
"""`card.py --token - <verb> <args…>`: a brain's board write (doc 32 §4), sent through the door as a
`card:` line on its lease token. `cards.py` is the same command where the ledger is this machine's."""
from __future__ import annotations

import shlex
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))
import door  # noqa: E402
from card_grammar import USAGE_REFUSAL, built  # noqa: E402
from secret_input import read_secret  # noqa: E402
from ulids import new_ulid  # noqa: E402
from verb_help import help_requested, script_help  # noqa: E402


def main(argv: list[str], local=None, prog: str = "card.py") -> int:
    """`local(token, verb, rest, usage)` is the writer a caller holding the ledger passes; it
    answers None when the write belongs to the door after all."""
    usage = ("usage: card.py --token - scope [--card <ulid>] <name> <track> [<verb>:<n>[,<n>] ...] [major]\n"
             "       card.py --token - rescope <card> <name> <track> [<verb>:<n>[,<n>] ...] [major]\n"
             "       card.py --token - branch|unbranch <card> <repo> <branch>\n"
             "       card.py --token - session <card> <session> executor|planned\n"
             "       card.py --token - unsession <card> <session>\n"
             "       card.py --token - conclude <card> <by> <evidence> [--branch-is-the-work]\n"
             "       card.py --token - unconclude <card>\n"
             "       card.py --token - retire <card> <why>\n"
             "       card.py --token - reclassify <card> track <lane>|off-track <why>\n"
             "       card.py --token - reclassify <card> significance|state|priority <value> <why>\n"
             "       card.py --token - reclassify <card> major major|ordinary <why>\n"
             "       card.py --token - reanchor <card> [<verb>:<n>[,<n>] ...] <why>\n"
             "       card.py --token - link <card> requires|part-of <card>|<owner>/<name>#<n> [--source <where>] <why>\n"
             "       card.py --token - unlink <card> requires|part-of <card>|<owner>/<name>#<n> resolved|withdrawn <why>\n"
             "\n"
             "       `major` is the brain's declaration that a card's work is major (doc 148 §5): "
             "a major card owes a design until a critic-passed canon record names it.")
    usage = usage.replace("card.py", prog)
    if prog == "card.py" and help_requested("card", argv, bare=False):
        print(script_help("card"))
        return 0
    if len(argv) < 3 or argv[0] != "--token":
        print(usage, file=sys.stderr)
        return 2
    token, verb, rest = argv[1], argv[2], argv[3:]
    if token != "-":
        print("refused: --token takes only -, and reads the lease token from stdin", file=sys.stderr)
        return 2
    token = read_secret("lease token: ")
    if not token:
        print("refused: --token - reads the lease token from stdin, and none arrived", file=sys.stderr)
        return 2
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
    if local is not None:
        code = local(token, verb, rest, usage)
        if code is not None:
            return code
    # Judged here first: a verb this file would refuse is refused in this file's words and
    # with its own exit code, rather than becoming a round trip answered by the door.
    _built, bad_grammar = built(verb, rest)
    if bad_grammar:
        print(usage if bad_grammar == USAGE_REFUSAL else f"refused: {bad_grammar}", file=sys.stderr)
        return 2
    # The verb crosses as a line; the token rides in the request body, never in a URL or an argument.
    # `shlex` both ways: a card's name has spaces in it, so the door's copy of the line
    # has to be split the way a shell would split the argv this file was given.
    reply = door.say(" ".join(["card:", "token", token, verb, shlex.join(rest)]).strip())
    print(reply)
    return 0 if reply.startswith("carded:") else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
