#!/usr/bin/env python3
"""`agent_secret.py <console>`: enrol this OS user on this machine for the workspace's remote door,
with the secret the owner was shown once by `pnpm enrol-agent` (doc 85 §3).

`agent_secret.py --review-host <console> <door base>`: declare this OS user a review host for its
team, with the secret `pnpm enrol-review-host` showed once (doc 136 §5). Its agent then serves
every workspace of the team for gates, from any directory.

Either way the secret is one line on stdin, never an argument.

It states this machine's hardware (doc 143 §2) and this OS user's node at the first exchange, and
the console fixes the pair to the secret. From then on the agent and the door scripts present a
credential to that door. Run it as the OS user being enrolled, from the workspace.
"""
from __future__ import annotations

import sys
import urllib.parse
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))
import credential  # noqa: E402
import verb_help  # noqa: E402
from door import door  # noqa: E402
from secret_input import read_secret  # noqa: E402


def review_host(console: str, door_base: str, secret: str) -> int:
    import review_host as review_mod
    d = {"console": console.rstrip("/"), "door": door_base.rstrip("/"), "secret": secret}
    try:
        clusters = review_mod.list_clusters(d)
    except RuntimeError as e:
        print(f"not declared: {e}", file=sys.stderr)
        return 1
    p = review_mod.write(console, door_base, secret)
    print(f"a review host for {', '.join(clusters)}; declared in {p}. The agent enrols for each "
          f"within {int(review_mod.LIST_EVERY)}s, or at its next start.")
    return 0


def main(argv: list[str]) -> int:
    if verb_help.help_requested("agent_secret", argv):
        print(verb_help.script_help("agent_secret", topic=argv[0] if len(argv) == 2 else None))
        return 0
    review = argv[:1] == ["--review-host"]
    args = argv[1:] if review else argv
    if any(arg.startswith("-") for arg in args):
        return verb_help.error("agent_secret", "unknown option; only --review-host is supported")
    if len(args) != (2 if review else 1):
        return verb_help.error("agent_secret", "--review-host requires console and door base, and the "
                               "secret on stdin" if review else "enrollment requires the console, and "
                               "the secret on stdin")
    urls = zip(("console", "door base"), args)
    for label, value in urls:
        try:
            parsed = urllib.parse.urlsplit(value)
            valid = parsed.scheme in ("http", "https") and parsed.hostname and parsed.port != 0
        except ValueError:
            valid = False
        if not valid:
            return verb_help.error("agent_secret", f"{label} requires an http or https URL with a host")
    if not review:
        base, remote = door()
        if not remote:
            print(f"this workspace's door is {base}, on this machine; a credential is for a remote door. "
                  "Local doors need no agent credential; for remote enrollment, set this workspace's "
                  "STEERING_DOOR to its remote door and run again.", file=sys.stderr)
            print(verb_help.script_help("agent_secret", topic="secret"), file=sys.stderr)
            return 1
    secret = read_secret("secret: ")
    if not secret:
        return verb_help.error("agent_secret", "missing secret: it is read from stdin")
    if review:
        return review_host(*args, secret)
    try:
        d = credential.enrol(base, args[0], secret)
    except credential.NoCredential as e:
        print(f"not enrolled: {e}", file=sys.stderr)
        return 1
    print(f"enrolled for {base} as {d['machine']} / {d['os_node']}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
