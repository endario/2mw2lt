#!/usr/bin/env python3
"""`promote.py`: take the steering role for this session.

It takes no session name. Doc 28: this session's identity is the provider session the
harness exports, the enrollment minted for it is found by that, and the name on the
attachment is the one the door resolves from its credential. Naming the holder by hand is
how one session came to show two identities (#193).

The seat is taken through the door (doc 38 §4): the daemon detaches whatever holds it and
seats this session, from this machine or another node. The attachment is NOT answerable when
this returns. Only a held stream makes it so, so the session has to hold its stream at the
local agent — the recipe in `skills/brain/SKILL.md` — before the console can route anything
here.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parent))
import python_floor  # noqa: E402

python_floor.require()

import json  # noqa: E402
import os  # noqa: E402
import urllib.error  # noqa: E402
import urllib.request  # noqa: E402

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE / "enroll"))
from door import door, send  # noqa: E402
from bind import records  # noqa: E402
from local_workspace import required_workspace_root  # noqa: E402
import hooks  # noqa: E402
import lease  # noqa: E402

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

    base, _remote = door()
    # The enrolment `connect.py` stored for this session lives in the workspace this session
    # runs in — on this machine, whichever machine the daemon is on.
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
    req = urllib.request.Request(f"{base}/steering/brain/attach", data=json.dumps({"token": rec["token"]}).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    try:
        with send(req, 10.0) as r:
            out = json.load(r)
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")
        try:
            why = json.loads(body).get("error") or body
        except ValueError:
            why = body
        refuse(f"the door at {base} did not seat this session: {why}")
    except (urllib.error.URLError, OSError, ValueError) as e:
        refuse(f"no steering daemon answered at {base} ({e}). The role only exists where one runs.")
    # The lease is stored for this session, never printed: a printed token sits in the model's
    # context and every command it writes after (#4551). The lease clients read it back with
    # `--lease`.
    lease.store(ws, rec["provider_session"], out["session"], out["attachment_id"], out["lease_token"])
    print(f"LEASE=stored\nATTACHMENT_ID={out['attachment_id']}\nSESSION={out['session']}")
    print(f"\nSeated as {out['session']} — the name the door resolved from your enrollment.")
    print("Not answerable yet. Hold your stream at the local agent before the daemon will "
          "route to you — the recipe is in the /2mw2lt:brain skill, and the stream has to "
          "stay open for as long as you hold the role.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
