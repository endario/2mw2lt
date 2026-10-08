#!/usr/bin/env python3
"""`seat_section.py [--provider <harness>] [--provider-session <id>]`: the standing rulings
a session reads while it holds the seat (#4551, step 3).

Exits 0 with the text when this session holds the seat, 1 with nothing when it does not, and 3 when
it cannot tell: a caller keeps what it last had rather than drop the rulings over a door that did
not answer. The plugin's module places the text in the seat's system prompt; any harness can print
it. What the rulings are is canon's: the active decisions the daemon serves at `/knowledge/units`.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parents[1]))
import python_floor  # noqa: E402

python_floor.require()

import urllib.error  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
import connect  # noqa: E402
import door  # noqa: E402
import rest  # noqa: E402
from verb_help import error, help_requested, script_help  # noqa: E402

HEADING = (
    "# Standing rulings while this session holds the seat\n"
    "These decisions are canon: each binds the seat while it holds it. Read one in full with\n"
    "`rest.py /knowledge/units/<id>`, which names its file."
)


def compose(units: list[dict]) -> str:
    """The section: canon's active decisions, by title and id, in id order."""
    decisions = sorted((u for u in units if u.get("kind") == "decision" and u.get("status") == "active"),
                       key=lambda u: str(u.get("id")))
    return "\n".join([HEADING, "", *(f"- {u.get('title')} ({u.get('id')})" for u in decisions)])


def read(path: str, flags: dict[str, str]) -> dict | None:
    """An API read on this session's own enrolment token, at whichever door the workspace names:
    the remote door serves the API, and the seat is remote."""
    base = door.door_url()
    if "/w/" not in base:
        return None
    token, _why = rest.own_bearer(flags)
    if token is None:
        return None
    try:
        code, answer = rest.fetch(f"{base}/api/v1{path}", token)
    except (urllib.error.URLError, OSError, ValueError):
        return None
    return answer if code == 200 and isinstance(answer, dict) else None


def holds(flags: dict[str, str]) -> bool | None:
    answer = read("/seat", flags)
    held = answer.get("holds") if answer else None
    return held if isinstance(held, bool) else None


def units(flags: dict[str, str]) -> list[dict] | None:
    answer = read("/knowledge/units", flags)
    items = answer.get("items") if answer else None
    return items if isinstance(items, list) else None


def main(argv: list[str]) -> int:
    # 1 means a session that does not hold the seat, and its rulings are dropped on it; a failure
    # nobody foresaw, which Python would also end with 1, is unknown instead.
    try:
        return _main(argv)
    except Exception as e:
        print(f"seat_section: could not tell ({type(e).__name__}: {e})", file=sys.stderr)
        return 3


def _main(argv: list[str]) -> int:
    if help_requested("seat_section", argv):
        print(script_help("seat_section"))
        return 0
    flags: dict[str, str] = {}
    rest_args = list(argv)
    for flag in connect.SPEAKER_FLAGS:
        if flag in rest_args:
            i = rest_args.index(flag)
            if i + 1 >= len(rest_args):
                return error("seat_section", f"{flag} requires a value")
            flags[flag] = rest_args[i + 1]
            del rest_args[i:i + 2]
    if rest_args:
        return error("seat_section", f"unexpected arguments: {' '.join(rest_args)}")
    held = holds(flags)
    if held is None:
        print("seat_section: the door did not say whether this session holds the seat", file=sys.stderr)
        return 3
    if not held:
        return 1
    found = units(flags)
    if found is None:
        print("seat_section: canon's units did not answer", file=sys.stderr)
        return 3
    print(compose(found))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
