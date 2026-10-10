#!/usr/bin/env python3
"""`rest.py [--provider <harness>] [--provider-session <id>] [--lease] [--post [--json <body>] [--key <idempotency
key>]] <path> [--all]`: a call to the steering API (doc 126) from any machine.

The path is relative to `/api/v1`, as `/knowledge/units` or `/cards?state=live`. It goes
to the door this workspace names. A session's enrolment token on stdin goes with the current
machine credential on Go's paired session carriers. It stays out of the process table; `--lease`
sends the seat holder's own enrolment, which Go judges at each call. With nothing on stdin, this
session's own enrolment token goes, found as `say.py` finds it (#3551). `--all` follows a page's
`next` until the head. `--post` sends the path a POST with no body, or with `--json`'s body and
`--key` as its `Idempotency-Key`, as a room's messages take (doc 156).

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

# What the person does about a refusal, for each remedy the contract's Problem names (#5058). A
# remedy this client does not know is read as `none`, as the contract asks.
REMEDY_ADVICE: dict[str, str] = {
    "reconnect": "run /2mw2lt:connect, then send again",
    "fix-input": "change the request, then send it",
    "resend": "send the same request again under a fresh key",
    "wait": "send the same request again in a minute",
    "update-plugin": "update the plugin: this client and the server disagree on the contract",
    "none": "",
}

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
          key: str | None = None) -> tuple[int, dict]:
    headers = {"Accept": "application/json", "User-Agent": door.USER_AGENT}
    if token:
        headers.update(session_routes.session_headers(token))
    if body is not None:
        headers["Content-Type"] = "application/json"
    if key:
        headers["Idempotency-Key"] = key
    req = urllib.request.Request(url, headers=headers, method=method,
                                 data=(body if body is not None else b"") if method == "POST" else None)
    try:
        with door.open_direct(req, timeout=door.SEND_TIMEOUT) as r:
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
    if "--lease" in rest:
        import lease
        try:
            held = lease.held(speaker)
        except ValueError as why:
            return error("rest", str(why))
        if held is None:
            print(lease.NONE, file=sys.stderr)
            return 1
        # The seat is the holder's own enrolment, judged by Go at each call.
        token = held["token"]
    else:
        token = None if stdin.isatty() else (stdin.read().strip() or None)
    if token is None:
        token, why_none = own_bearer(speaker)
    url = f"{base}/api/v1{args[0]}"
    pages, out = [], None
    while True:
        try:
            status, answer = fetch(url, token, method, body, valued.get("--key"))
        except (urllib.error.URLError, OSError) as e:
            print(f"the door at {base} did not answer: {e}", file=sys.stderr)
            return 1
        if not isinstance(answer, dict):
            print(f"{status} the door answered something that is not an object", file=sys.stderr)
            return 1
        if not 200 <= status < 300:
            remedy = REMEDY_ADVICE.get(answer.get("remedy") if isinstance(answer.get("remedy"), str) else "", "")
            print(f"{status} {answer.get('title') or answer.get('refused') or answer}"
                  + (f" [{answer['code']}]" if answer.get("code") else "")
                  + (f" — {remedy}" if remedy else ""), file=sys.stderr)
            hint = next((h for prefix, h in GO_UNSERVED if args[0].startswith(prefix)), None)
            if status == 404 and hint:
                print(f"{args[0].split('?')[0]} is not a route on Go: {hint}", file=sys.stderr)
            if why_none and status in (401, 403):
                print(f"no credential was sent: {why_none}", file=sys.stderr)
            return 1
        # The facts page says when it is exhausted; the machines page ends with no `next`.
        if "--all" not in argv or answer.get("exhausted") is True or not answer.get("next"):
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
