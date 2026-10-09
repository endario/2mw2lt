#!/usr/bin/env python3
"""`promote.py`: take the steering role for this session.

It takes no session name. Doc 28: this session's identity is the provider session the
harness exports, the enrollment minted for it is found by that, and the name on the
attachment is the one the door resolves from its credential. Naming the holder by hand is
how one session came to show two identities (#193).

The seat is Go's, taken only while it is vacant (go-unit8-seat-design.md §2). The session is
NOT answerable when this returns. Only a held stream makes it so, so the session holds its stream
at the local agent — the recipe in
`skills/brain/SKILL.md` — for as long as it keeps the seat.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parent))
import os  # noqa: E402

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE / "enroll"))
from bind import records  # noqa: E402
from local_workspace import required_workspace_root  # noqa: E402
import hooks  # noqa: E402
import session_routes  # noqa: E402

USAGE = "usage: promote.py"


def refuse(why: str) -> None:
    print(f"refused: {why}", file=sys.stderr)
    raise SystemExit(1)


def enrollment(ws: Path) -> dict:
    """The stored enrollment this session may attach under — its own, and no other.

    The provider session the harness exports is the identity; `connect.py` writes it beside
    the token it stored, so the record that names it is the one minted for this session.
    Never whichever record happens to be stored — the same rule `disconnect.py` follows, and
    for the same reason: a stored token that does not say which session it was minted for
    could belong to any of them, and attaching under it is #193 exactly.
    """
    psession = os.environ.get("CLAUDE_CODE_SESSION_ID", "").strip()
    known = records(ws)
    if not known:
        refuse(f"{ws} holds no enrollment. The role is taken by a session steering knows: "
               f"run /2mw2lt:connect first.")
    mine = [r for r in known.values() if psession and r.get("provider_session") == psession]
    if len(mine) == 1:
        return mine[0]
    whose = psession or "this harness exports no session id"
    refuse(f"no enrollment in {ws} was minted for this session ({whose}); it holds "
           f"{', '.join(sorted(known))}. Run /2mw2lt:connect. If a stored token predates "
           f"this and names no session, /2mw2lt:disconnect then /2mw2lt:connect replaces it "
           f"with one that does.")


def main(argv: list[str]) -> int:
    if argv:
        print(USAGE, file=sys.stderr)
        return 2

    # The enrolment `connect.py` stored for this session lives in the workspace this session
    # runs in.
    ws = required_workspace_root(Path(os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()), timeout=2.0)
    # A brain can hold hold.py's `exec` for as long as it runs the fleet without ever running
    # connect again — the skill only requires it once, on enrolling (#1875 round 2). `repin` is
    # its own no-op on a workspace with no settings to repair, so this costs nothing where the
    # harness carries no hooks. A malformed settings.local.json or unreadable launcher must not
    # turn a refresh nobody asked for into the reason this attach dies before it says anything
    # (#1875 round 3) — connect.py already carries this exposure; best-effort here too.
    try:
        hooks.repin(ws)
    except Exception as e:
        print(f"launcher refresh skipped: {e}", file=sys.stderr)
    rec = enrollment(ws)
    session, token = rec["session"], rec["token"]
    try:
        seat = session_routes.read_seat(token)
        holder = seat.get("holder")
        if holder is not None and holder != session:
            refuse(f"{holder} holds the seat; the owner hands it on from the "
                   f"console, or {holder} hands it on itself")
        if holder is None:
            session_routes.seat_acquire(session, token, int(seat["vacancy_generation"]))
    except session_routes.Refused as e:
        refuse(f"Go did not seat {session}: {e}")
    except session_routes.Unsent as e:
        refuse(f"Go did not answer ({e}), so {session} may hold the seat: run promote.py again, "
               f"which reads the seat before taking it")
    print(f"SESSION={session}")
    print(f"\nSeated as {session}.")
    print("Not answerable yet. Hold your stream at the local agent before the daemon will "
          "route to you — the recipe is in the /2mw2lt:brain skill, and the stream has to "
          "stay open for as long as you hold the role.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
