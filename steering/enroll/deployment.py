#!/usr/bin/env python3
"""`deployment.py [--provider <harness>] [--provider-session <id>] <sha> <environment>
pending|success|failure [<deployment id>]`: the seat's deploy reports a Deployment to GitHub
(7186-deployments design.md, D4).

It posts on the seat holder's own enrolment, as `rest.py --lease` does; coordination records the
report on GitHub with a token that never leaves it. `pending` with no id creates the deployment;
an ending state posts its status on the id given, or creates the deployment first when none is.

Exits 0 printing the deployment's id; 2 for a call it cannot make; otherwise 1, the problem on stderr.
Each call carries one idempotency key of its own, which the door requires.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parents[1]))
import json
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parent))
import connect  # noqa: E402
import door  # noqa: E402
import rest  # noqa: E402
from verb_help import error, help_requested, script_help  # noqa: E402

STATES = ("pending", "success", "failure")


def main(argv: list[str]) -> int:
    if help_requested("deployment", argv):
        print(script_help("deployment"))
        return 0
    args, speaker = list(argv), {}
    for flag in connect.SPEAKER_FLAGS:
        if flag in args:
            i = args.index(flag)
            if i + 1 >= len(args):
                return error("deployment", f"{flag} requires a value")
            speaker[flag] = args[i + 1]
            del args[i:i + 2]
    if len(args) not in (3, 4) or args[2] not in STATES or (len(args) == 4 and not args[3].isdigit()):
        return error("deployment", "name a sha, an environment, pending|success|failure, and the deployment id if one was answered")
    body = {"sha": args[0], "environment": args[1], "state": args[2]}
    if len(args) == 4:
        body["deployment"] = int(args[3])
    import lease
    try:
        held = lease.held(speaker)
    except ValueError as why:
        return error("deployment", str(why))
    if held is None:
        print(lease.NONE, file=sys.stderr)
        return 1
    base = door.door_url()
    if door.split(base)[1] is None:
        print("this workspace's door names no workspace; set STEERING_DOOR to …/<team>/<workspace>", file=sys.stderr)
        return 2
    try:
        status, answer = rest.fetch(f"{base}/api/v1/deployments", held["token"], "POST",
                                    json.dumps(body).encode(), key=str(uuid.uuid4()))
    except Exception as e:  # noqa: BLE001 - any failure to reach the door is said, never raised
        print(f"the door at {base} did not answer: {e}", file=sys.stderr)
        return 1
    if not 200 <= status < 300 or not isinstance(answer.get("deployment"), int):
        print(f"{status} {answer.get('title') or answer}" + (f" [{answer['code']}]" if answer.get("code") else ""), file=sys.stderr)
        return 1
    print(answer["deployment"])
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
