#!/usr/bin/env python3
"""`rotate.py [--as <account>] [--doing <text>] [<session>]`: replace this session's enrolment token.

For a token that has been disclosed. The enrollment is made again under the same name, which mints
a fresh token on a new epoch and leaves the old one resolving to nothing.

At the incumbent's door it is detached first: that door refuses a re-enrolment over a standing one
unless it can show the caller is that session, and it shows that from the transcript, which a
remote enrollment has none of. On Go (`STEERING_AUTHORITY=coordination`) the enrolment itself ends
the standing credential, so nothing is detached first.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parents[1]))
import python_floor  # noqa: E402

python_floor.require()


HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))
from local_workspace import required_workspace_root  # noqa: E402
from ack import token_path  # noqa: E402
from bind import Refused, bind, incarnation, records  # noqa: E402
from process_probe import Undetermined  # noqa: E402
from door import say  # noqa: E402
from connect import account, enrol, harness_of, options, project_dir, reached  # noqa: E402
import hooks  # noqa: E402
from verb_help import current_args, error, help_requested, script_help  # noqa: E402



def connect_recovery(reason: str) -> int:
    print(reason, file=sys.stderr)
    print(script_help("connect"), file=sys.stderr)
    return 1


def main(argv: list[str]) -> int:
    try:
        args = current_args("rotate", argv)
    except ValueError as why:
        return error("rotate", str(why))
    if help_requested("rotate", argv):
        print(script_help("rotate"))
        return 0
    try:
        o = options(args)
        h = harness_of(o.provider)
    except Refused as why:
        return error("rotate", str(why))
    session, acct, doing = o.session, account(o, h), o.doing

    ws = required_workspace_root(project_dir(), timeout=2.0)
    # Rotation reopens the hold through the same workspace launcher connect uses, and a session
    # rotating mid-seat may not have run connect since a plugin update moved past it (#1875
    # round 2) — `repin` is a no-op where this harness carries no hooks to repair. A malformed
    # settings.local.json or unreadable launcher must not turn a refresh nobody asked for into
    # the reason rotation dies before it says anything (#1875 round 3).
    if h.hooks:
        try:
            hooks.repin(ws)
        except Exception as e:
            print(f"launcher refresh skipped: {e}", file=sys.stderr)
    known = records(ws)
    try:
        psession, rid = incarnation(o.psession, o.pid, h.provider)
    except (Refused, Undetermined) as why:
        print(str(why), file=sys.stderr); return 1
    if session is None:  # only an enrollment minted for this session, as `disconnect.py` picks it
        mine = [n for n, rec in known.items() if rec.get("provider_session") == psession]
        if len(mine) != 1:
            print(f"name the session to rotate: {ws} holds no enrollment minted for this one"
                  f" ({', '.join(sorted(known)) or 'none stored'})", file=sys.stderr); return 1
        session = mine[0]
    if session not in known:
        return connect_recovery(f"no stored token for {session} in {ws}")
    minted_for = known[session].get("provider_session")
    if minted_for != psession:
        # A record stored by hand names no session it was minted for, and rotation ends an
        # enrollment before it makes one: unproven ownership would hand any session here the
        # power to end another's.
        print(f"{session} is not this session's enrollment"
              f" ({minted_for or 'it names no session it was minted for'})", file=sys.stderr); return 1
    import session_routes  # noqa: E402
    on_go = session_routes.on_coordination(ws)
    if not acct and not on_go:
        return connect_recovery("this harness keeps no account in a directory")

    if not on_go:  # Go's enrolment ends the standing credential itself
        ended = say(f"detach: {session} token {known[session]['token']}")
        # A token already revoked is the case this command exists for, so its refusal is not one.
        if not ended.startswith("detached:") and "stale token" not in ended:
            print(f"{ended}\nthe standing token was not ended, and still stands", file=sys.stderr); return 1

    try:
        token = enrol(ws, session, acct, doing, psession, h, rid=rid)
    except Refused as why:
        if on_go:
            return connect_recovery(str(why))
        return connect_recovery(f"{why}\n{session} is now detached")
    print(f"{session}: rotated, the previous token proves nothing now")
    print(f"the new token is the `token` field of {token_path(ws, session)}")
    try:
        answer, rid = bind(session, token, o.psession, o.pid, h.provider)
    except (Refused, Undetermined) as why:
        print(f"{why}", file=sys.stderr); return 1
    print(f"{session}: {answer}")
    if not answer.startswith("bound:"):
        return 1
    if h.holds:
        print("the hold the previous token held was revoked with it: reopen the stream on the new one")
    else:
        # The same three facts revoke the standing hold here as in a connect, and a harness that
        # holds no stream has nothing to reopen — the reach is what it has instead (#663).
        print(reached(session, psession, ws))
    print(f"STEERING_RUNTIME_ID={rid}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
