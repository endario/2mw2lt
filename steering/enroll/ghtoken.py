"""`ghtoken.py`: this session's GitHub token, an installation token of the workspace's App (doc 181
§4), printed for the `gh` wrapper a launched worker runs. Kept in a 0600 file until five minutes
before the life Go answered runs out, then asked of Go again on the session's own credential."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parents[1]))
import python_floor  # noqa: E402

python_floor.require()

import hashlib
import json
import os
import time
import uuid

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))
from bind import Refused  # noqa: E402
from connect import speaking_as  # noqa: E402
from local_workspace import required_workspace_root  # noqa: E402
import session_routes  # noqa: E402
import spool  # noqa: E402
import verb_help  # noqa: E402

MARGIN = 300
TIMEOUT = 30.0


def cache_path(ws: Path, session: str) -> Path:
    state = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state")
    key = hashlib.sha256(str(ws.resolve()).encode()).hexdigest()[:16]
    return state / "2mw2lt" / "forge-token" / f"{key}-{session}.json"


def _kept(path: Path, now: float) -> str | None:
    try:
        held = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    token = held.get("token") if isinstance(held, dict) else None
    until = held.get("until") if isinstance(held, dict) else None
    return token if isinstance(token, str) and token and isinstance(until, (int, float)) and until > now else None


def _keep(path: Path, token: str, until: float) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    spool.write_atomic(path, json.dumps({"token": token, "until": until}), mode=0o600)


def ask(session: str, enrolment: str) -> dict:
    """Go's answer to a mint on the session's own credential. Go records nothing to replay, so the
    key is fresh; a paced answer is waited out by `session_routes.post`."""
    return session_routes.post(f"/sessions/{session}/forge-token", {}, uuid.uuid4().hex,
                               session_routes.SESSION_CARRIER, enrolment, TIMEOUT)


def token(ws: Path, now=time.time, asked=ask) -> str:
    """The session's token, kept or minted. Raises `Refused` with Go's reason."""
    session, enrolment = speaking_as(ws, None, {})
    path = cache_path(ws, session)
    kept = _kept(path, now())
    if kept:
        _configure(kept)
        return kept
    try:
        out = asked(session, enrolment)
    except session_routes.Refused as e:
        raise Refused(f"Go minted no GitHub token for {session}: {e}") from None
    if not isinstance(out, dict):
        raise Refused(f"Go answered {session}'s GitHub token with a {type(out).__name__}, not an object")
    minted, life = out.get("token"), out.get("fresh_for")
    if not isinstance(minted, str) or not minted:
        raise Refused(f"Go answered {session}'s GitHub token with no token")
    if not isinstance(life, int) or isinstance(life, bool) or life <= 0:
        raise Refused(f"Go answered {session}'s GitHub token with fresh_for {life!r}, not a positive count of seconds")
    _keep(path, minted, now() + life - MARGIN)
    _configure(minted)
    return minted


def _configure(minted: str) -> None:
    """The token in the worker's own `gh` configuration (`GH_CONFIG_DIR`), as the only login there,
    so a `gh` found ahead of the wrapper acts as the App, or is refused once the token is stale."""
    d = os.environ.get("GH_CONFIG_DIR")
    if not d:
        return
    d = Path(d)
    d.mkdir(parents=True, exist_ok=True, mode=0o700)
    # Written already migrated: gh migrates a bare token by asking `/user`, which an installation
    # token cannot read.
    for name, text in (("config.yml", 'version: "1"\n'),
                       ("hosts.yml", "github.com:\n    users:\n        x-access-token:\n"
                                     f"            oauth_token: {minted}\n    git_protocol: https\n"
                                     f"    oauth_token: {minted}\n    user: x-access-token\n")):
        try:
            if (d / name).read_text() == text:
                continue
        except OSError:
            pass
        spool.write_atomic(d / name, text, mode=0o600)


def main(argv: list[str]) -> int:
    if verb_help.help_requested("ghtoken", argv, bare=False):
        print(verb_help.script_help("ghtoken")); return 0
    if argv:
        return verb_help.error("ghtoken", f"unexpected arguments: {' '.join(argv)}")
    try:
        ws = required_workspace_root(Path(os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()), timeout=2.0)
        print(token(ws))
    except (Refused, OSError) as e:
        print(f"ghtoken: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
