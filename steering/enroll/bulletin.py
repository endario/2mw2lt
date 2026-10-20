#!/usr/bin/env python3
"""`bulletin.py [--provider <harness>] [--provider-session <id>] <session> read [--all]`,
`… <session> post <key> "<text>" [--until <UTC time | +2h>] [--standing] [--seat]`,
`… <session> retract <key> "<reason>"` and `… <session> history [--since <UTC time | -24h>]`:
the workspace bulletin over the steering API (#6473) — the keyed, attributed lines that say what
this workspace holds true right now. `read` prints the snapshot a session is shown, `--all` every
live line; `post` upserts a line by key and `retract` ends one with its reason; `history` reads the
window behind it. The request goes on this session's own enrollment token, never a hand-written
author: the daemon attributes the write.

A refusal prints its check and one line on what would satisfy it, and exits 1."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parents[1]))
import json
import re
import urllib.parse
from datetime import datetime, timedelta, timezone

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))
from bind import Refused  # noqa: E402
import connect  # noqa: E402
from connect import speaker_flags, speaking_as  # noqa: E402
from door import occurrence  # noqa: E402
from local_workspace import required_workspace_root  # noqa: E402
import session_routes  # noqa: E402
from verb_help import error, help_requested, script_help  # noqa: E402

import os

# What would satisfy each refusal, one line, as the checks name them (design §6).
SATISFIES = {
    "bulletin-shape": "the key is 1-40 bytes of lowercase letters, digits and dashes, starting with a "
                      "letter or digit; the text or reason is 1-200 bytes of UTF-8 on one line; until is a time still ahead",
    "bulletin-tier": "a session posts to everyone with an until within 24 hours; standing lines and the "
                     "seat's own audience are the seat's and the owner's",
    "bulletin-full": "the workspace holds 32 live lines, or this session 3: retract one first",
    "bulletin-standing-full": "standing lines would render past 1024 bytes for the seat audience: graduate "
                              "one into knowledge or retract it",
    "bulletin-not-live": "no live line holds that key; post it first",
    "bulletin-credential": "the text carries a credential-shaped string; it is not posted",
    "bulletin-unscannable": "the credential scan could not run; send it again",
    "bulletin-protected-name": "the text names another team or tenant; blank it or say it another way",
    "bulletin-history-window-invalid": "the history window spans at most the workspace's retention "
                                       "(the Kept for setting) and ends after it starts",
}

UNITS = {"s": timedelta(seconds=1), "m": timedelta(minutes=1), "h": timedelta(hours=1), "d": timedelta(days=1)}
RELATIVE = re.compile(r"([+-])(\d+)([smhd])$")


def resolve_when(spec: str, now: datetime) -> datetime:
    """A `+2h`-style offset from now, or an ISO 8601 instant, read as UTC when it names no zone."""
    if match := RELATIVE.fullmatch(spec):
        sign = -1 if match.group(1) == "-" else 1
        return now + sign * int(match.group(2)) * UNITS[match.group(3)]
    if spec.endswith(("Z", "z")):
        spec = spec[:-1] + "+00:00"
    when = datetime.fromisoformat(spec)
    return when.replace(tzinfo=timezone.utc) if when.tzinfo is None else when


def wire_time(when: datetime) -> str:
    """The wire's instant: UTC, milliseconds, a trailing Z."""
    utc = when.astimezone(timezone.utc)
    return utc.strftime("%Y-%m-%dT%H:%M:%S.") + f"{utc.microsecond // 1000:03d}Z"


def _get(path: str, token: str) -> dict:
    """One read on the session's paired credentials, on session_routes' own transport."""
    return session_routes.get(path, token)


def _refusal(e: Exception) -> int:
    print(f"refused: {e}", file=sys.stderr)
    if isinstance(e, session_routes.Refused):
        advice = SATISFIES.get(e.code)
        print(f"  {e.code}: {advice}" if advice else f"  {e.code}", file=sys.stderr)
    return 1


def _until_line(line: dict) -> str:
    until = line.get("until")
    end = "standing" if until is None else f"until {until}"
    return f"- {line.get('key', '?')}: {line.get('text', '')} ({line.get('author', '?')}, {end})"


def _section(flags: dict[str, str]) -> int:
    """The bulletin section every connected session keeps: the snapshot this session is shown,
    marked as shown. Exit 0 with the text, 1 when the bulletin is empty, and 3 when it cannot
    tell, so a caller keeps what it last had rather than drop the lines over a door that did not
    answer."""
    try:
        ws = required_workspace_root(connect.project_dir(), timeout=2.0)
        _, token = connect.own_enrolment(ws, connect.provider_session(flags))
        answer = _get("/bulletin?render=snapshot&mark=1", token)
    except (Refused, session_routes.Refused, session_routes.Unsent) as e:
        print(f"bulletin section: could not tell ({e})", file=sys.stderr)
        return 3
    text = answer.get("text", "")
    if not text:
        return 1
    print(text, end="")
    return 0


def main(argv: list[str]) -> int:
    if help_requested("bulletin", argv):
        print(script_help("bulletin", topic=argv[0] if len(argv) == 2 else None))
        return 0
    parsed = speaker_flags(argv, also=())
    if parsed is None:
        return error("bulletin", "malformed speaker flags")
    flags, args = parsed
    if args == ["section"]:
        try:
            return _section(flags)
        except Exception as e:
            print(f"bulletin section: could not tell ({type(e).__name__}: {e})", file=sys.stderr)
            return 3
    FLAGS = {"read": ("--all",), "post": ("--until", "--standing", "--seat"),
             "retract": (), "history": ("--since",)}
    valued, plain = {}, []
    i = 0
    while i < len(args):
        arg = args[i]
        if arg in ("--until", "--since"):
            if i + 1 >= len(args) or args[i + 1].startswith("--"):
                return error("bulletin", f"{arg} requires a value")
            if arg in valued:
                return error("bulletin", f"{arg} may appear once")
            valued[arg] = args[i + 1]
            i += 2
            continue
        if arg in ("--all", "--standing", "--seat"):
            if arg in valued:
                return error("bulletin", f"{arg} may appear once")
            valued[arg] = True
            i += 1
            continue
        plain.append(arg)
        i += 1
    if len(plain) < 2:
        return error("bulletin", "bulletin requires a session and a command: read, post, retract or history")
    session, command, trailing = plain[0], plain[1], plain[2:]
    if command not in FLAGS:
        return error("bulletin", f"unknown command {command}")
    if stray := set(valued) - set(FLAGS[command]):
        return error("bulletin", f"{command} takes no {', '.join(sorted(stray))}")
    if valued.get("--standing") and "--until" in valued:
        return error("bulletin", "a line is standing or has an until, not both")
    ws = required_workspace_root(Path(os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()), timeout=2.0)
    try:
        enrolled, token = speaking_as(ws, session, flags)
    except Refused as why:
        print(str(why), file=sys.stderr)
        return 1
    now = datetime.now(timezone.utc)
    try:
        for spec in (valued.get("--until"), valued.get("--since")):
            if spec is not None:
                resolve_when(spec, now)
    except ValueError:
        return error("bulletin", f"an instant is a UTC time or an offset like +2h: {spec!r} is neither")
    try:
        if command == "read":
            if trailing:
                return error("bulletin", "read takes no arguments but --all")
            if valued.get("--all"):
                answer = _get("/bulletin", token)
                for line in answer.get("lines", []):
                    print(_until_line(line))
                return 0
            print(_get("/bulletin?render=snapshot", token).get("text", ""), end="")
            return 0
        if command == "post":
            if len(trailing) != 2:
                return error("bulletin", "post requires a key and a text")
            key, text = trailing
            body = {"text": text}
            if valued.get("--seat"):
                body["audience"] = "seat"
            if "--until" in valued:
                body["until"] = wire_time(resolve_when(valued["--until"], now))
            answer = session_routes.post(f"/bulletin/{urllib.parse.quote(key, safe='')}", body, occurrence(),
                                         session_routes.SESSION_CARRIER, token, method="PUT")
            print(f"posted {answer.get('key')} as revision {answer.get('revision')}")
            return 0
        if command == "retract":
            if len(trailing) != 2:
                return error("bulletin", "retract requires a key and a reason")
            key, reason = trailing
            answer = session_routes.post(f"/bulletin/{urllib.parse.quote(key, safe='')}/retraction", {"reason": reason},
                                         occurrence(), session_routes.SESSION_CARRIER, token)
            print(f"retracted {answer.get('key')} as revision {answer.get('revision')}")
            return 0
        # history
        if trailing:
            return error("bulletin", "history takes no arguments but --since")
        path = "/bulletin/history"
        if "--since" in valued:
            path += "?since=" + urllib.parse.quote(wire_time(resolve_when(valued["--since"], now)))
        answer = _get(path, token)
        for line in answer.get("opening", []):
            print(f"standing at the window's start: {_until_line(line)}")
        for revision in answer.get("revisions", []):
            words = revision.get("text") or f"retracted: {revision.get('reason', '')}"
            print(f"r{revision.get('revision', '?')} {revision.get('kind', '?')} {revision.get('key', '?')}: {words} "
                  f"({revision.get('author', '?')}, at {revision.get('at', '?')})")
        return 0
    except session_routes.Refused as e:
        return _refusal(e)
    except session_routes.Unsent as e:
        print(f"no answer: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
