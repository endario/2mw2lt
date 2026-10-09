#!/usr/bin/env python3
"""`rest.py [--provider <harness>] [--provider-session <id>] [--lease] [--post [--json <body>] [--key <idempotency
key>]] <path> [--all]`: a call to the steering API (doc 126) from any machine.

The path is relative to `/api/v1`, as `/launches/<id>` or `/facts?state=worker-launched`. It goes
to the door this workspace names, with the agent credential a remote door requires. A token on
stdin — the seat's lease token, or a session's enrolment token — goes as `Authorization: Bearer`,
and stays out of the process table; `--lease` sends the seat's stored lease instead. With nothing on stdin, this session's own enrolment token goes,
found as `say.py` finds it (#3551). `--all` follows a page's `next` until the head. `--post` sends
the path a POST with no body, as `/tracks/syncs` takes, or with `--json`'s body and `--key` as its
`Idempotency-Key`, as a room's messages take (doc 156). On a Go workspace the token goes on Go's
session carrier instead, and alone: Go refuses a bearer, or a machine credential beside it.

Exits 0 with the JSON on stdout; otherwise 1, with the problem's title and remedy on stderr.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parents[1]))
import python_floor  # noqa: E402

python_floor.require()

import json
import urllib.error
import urllib.parse
import urllib.request

sys.path.insert(0, str(Path(__file__).resolve().parent))
import connect  # noqa: E402
import door  # noqa: E402
import session_routes  # noqa: E402
from verb_help import error, help_requested, script_help  # noqa: E402

# The paths this client was taught on the incumbent's door that Go has no route for, with what to
# read there instead; said only when Go answers 404, so a route Go comes to serve is reached.
GO_UNSERVED = (
    ("/tracks/syncs", "Go syncs the lanes as the workspace's mirror moves; GET /tracks names the "
                      "commit synced, awaited or failed"),
    ("/knowledge/brief", "read /knowledge/units, or /knowledge/units/<id>"),
    ("/launches", "a launch is not read on Go yet"),
    ("/pulls/", "a pull request is not read on Go yet"),
)


def fetch(url: str, token: str | None, method: str = "GET", body: bytes | None = None,
          key: str | None = None, go: bool = False) -> tuple[int, dict]:
    headers = {"Accept": "application/json"}
    if token:
        headers.update({session_routes.SESSION_CARRIER: token} if go else {"Authorization": f"Bearer {token}"})
    if body is not None:
        headers["Content-Type"] = "application/json"
    if key:
        headers["Idempotency-Key"] = key
    req = urllib.request.Request(url, headers=headers, method=method,
                                 data=(body if body is not None else b"") if method == "POST" else None)
    try:
        if go:
            req.add_header("User-Agent", door.USER_AGENT)
        with (door.open_direct if go else door.send)(req, timeout=door.SEND_TIMEOUT) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read() or b"{}")
        except ValueError:
            return e.code, {"title": f"the door answered {e.code}"}


def own_bearer(flags: dict[str, str]) -> tuple[str | None, str | None]:
    """(token, None) for this session's own enrolment, or (None, why there is none)."""
    try:
        ws = connect.required_workspace_root(connect.project_dir(), timeout=2.0)
        return connect.speaking_as(ws, None, flags)[1], None
    except (connect.Refused, SystemExit) as why:
        return None, str(why)


def main(argv: list[str], stdin) -> int:
    if help_requested("rest", argv):
        print(script_help("rest", topic=argv[0] if len(argv) == 2 else None))
        return 0
    valued: dict[str, str] = {}
    rest = list(argv)
    for flag in ("--json", "--key", "--post", "--all", "--lease", *connect.SPEAKER_FLAGS):
        if rest.count(flag) > 1:
            return error("rest", f"{flag} may appear once")
    for flag in ("--json", "--key", *connect.SPEAKER_FLAGS):
        if flag in rest:
            i = rest.index(flag)
            if i + 1 >= len(rest) or rest[i + 1].startswith("--"):
                return error("rest", f"{flag} requires a value")
            valued[flag] = rest[i + 1]
            del rest[i:i + 2]
    speaker = {f: valued.pop(f) for f in connect.SPEAKER_FLAGS if f in valued}
    if speaker.get("--provider", connect.harness_mod.DEFAULT) not in connect.harness_mod.PROVIDERS:
        return error("rest", "--provider names no harness this client knows")
    args = [a for a in rest if a not in ("--all", "--post", "--lease")]
    method = "POST" if "--post" in rest else "GET"
    if method == "POST" and "--all" in rest:
        return error("rest", "--post cannot be combined with --all")
    if valued and method != "POST":
        return error("rest", "--json and --key require --post")
    if len(args) != 1 or not args[0].startswith("/"):
        return error("rest", "name one API path beginning with /")
    if "--json" in valued:
        try:
            json.loads(valued["--json"])
        except ValueError:
            return error("rest", "--json requires valid JSON")
    body = valued["--json"].encode() if "--json" in valued else None
    base = door.door_url()
    if "/w/" not in base:
        print("this workspace's door names no authority; set STEERING_DOOR to …/w/<authority>", file=sys.stderr)
        return 2
    why_none = None
    go = session_routes.on_coordination()
    if "--lease" in rest:
        # The seat's stored lease, never on stdin or a command line (#4551).
        import lease
        try:
            held = lease.held(speaker)
        except ValueError as why:
            return error("rest", str(why))
        if held is None:
            print(lease.NONE, file=sys.stderr)
            return 1
        # On Go the seat is the holder's own enrolment, judged by Go at each call.
        token = held["token"] if go else held["lease_token"]
    else:
        token = None if stdin.isatty() else (stdin.read().strip() or None)
    if token is None:
        token, why_none = own_bearer(speaker)
    url = f"{base}/api/v1{args[0]}"
    pages, out = [], None
    while True:
        try:
            status, answer = fetch(url, token, method, body, valued.get("--key"), go)
            # Only the API's refusal of the bearer itself says the lease is dead: an answer's data may
            # quote the same words.
            why = f"{answer.get('title') or ''} {answer.get('detail') or ''}".strip() if isinstance(answer, dict) else ""
            if "--lease" in rest and not go and status in (401, 403) and lease.dead(f"refused: {why}"):
                print(lease.gone(held, why), file=sys.stderr)
                return 1
        except (urllib.error.URLError, OSError) as e:
            print(f"the door at {base} did not answer: {e}", file=sys.stderr)
            return 1
        if not isinstance(answer, dict):
            print(f"{status} the door answered something that is not an object", file=sys.stderr)
            return 1
        if not 200 <= status < 300:
            remedy = (answer.get("remedy") or {}).get("text")
            print(f"{status} {answer.get('title') or answer.get('refused') or answer}"
                  + (f" [{answer['code']}]" if go and answer.get("code") else "")
                  + (f" — {remedy}" if remedy else ""), file=sys.stderr)
            hint = next((h for prefix, h in GO_UNSERVED if args[0].startswith(prefix)), None)
            if go and status == 404 and hint:
                print(f"{args[0].split('?')[0]} is not a route on Go: {hint}", file=sys.stderr)
            if why_none and status in (401, 403):
                print(f"no credential was sent: {why_none}", file=sys.stderr)
            return 1
        if "--all" not in argv or answer.get("exhausted", True) or not answer.get("next"):
            out = answer if not pages else {**answer, "items": [i for p in pages for i in p] + answer.get("items", [])}
            break
        pages.append(answer.get("items", []))
        parts = urllib.parse.urlsplit(url)
        query = [(k, v) for k, v in urllib.parse.parse_qsl(parts.query) if k != "cursor"]
        url = urllib.parse.urlunsplit(parts._replace(query=urllib.parse.urlencode(query + [("cursor", answer["next"])])))
    print(json.dumps(out, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:], sys.stdin))
