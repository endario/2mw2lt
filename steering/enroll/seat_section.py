#!/usr/bin/env python3
"""`seat_section.py [--provider <harness>] [--provider-session <id>]`: the standing rulings
a session reads while it holds the seat (#4551, step 3).

Exits 0 with the text when this session holds the seat, 1 with nothing when it does not, and 3 when
it cannot tell: a caller keeps what it last had rather than drop the rulings over a door that did
not answer. The plugin's module places the text in the brain's system prompt; any harness can print
it. What the rulings are is the knowledge's: the active decisions the daemon serves at `/knowledge/units`.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parents[1]))
import python_floor  # noqa: E402

python_floor.require()

sys.path.insert(0, str(Path(__file__).resolve().parent))
import connect  # noqa: E402
from verb_help import error, help_requested, script_help  # noqa: E402

HEADING = (
    "# Standing rulings while this session holds the seat\n"
    "These decisions are the workspace's knowledge: each binds the brain while it holds the seat. Read one in full with\n"
    "`rest.py /knowledge/units/<id>`, which names its file."
)


def compose(units: list[dict]) -> str:
    """The section: the knowledge's active decisions, by title and id, in id order."""
    decisions = sorted((u for u in units if u.get("kind") == "decision" and u.get("status") == "active"),
                       key=lambda u: str(u.get("id")))
    return "\n".join([HEADING, "", *(f"- {u.get('title')} ({u.get('id')})" for u in decisions)])


def go_read(path: str, flags: dict[str, str]) -> tuple[str, dict] | None:
    """(This session's name, Go's answer to `path` on its own carrier); None when the read did not
    answer."""
    import session_routes
    try:
        ws = connect.required_workspace_root(connect.project_dir(), timeout=2.0)
        session, token = connect.own_enrolment(ws, connect.provider_session(flags))
        return session, session_routes.get(path, token)
    except (connect.Refused, session_routes.Refused, session_routes.Unsent):
        return None


def holds(flags: dict[str, str]) -> bool | None:
    got = go_read("/seat", flags)
    if got is None or "holder" not in got[1]:
        return None
    return got[1]["holder"] == got[0]


def units(flags: dict[str, str]) -> list[dict] | None:
    got = go_read("/knowledge/units", flags)
    items = got[1].get("items") if got else None
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
        print("seat_section: the knowledge units did not answer", file=sys.stderr)
        return 3
    print(compose(found))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
