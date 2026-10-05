#!/usr/bin/env python3
"""`bind.py [<session>]`: bind this incarnation to its enrollment (DOOR.md `bind:`).

A restarted process is a new incarnation, and a directive queued for the old one can only be
claimed by the old one. What `bind:` needs is derived from the running process — the session
id the harness exports and the runtime id of the harness process, which is the same identity
the observation hooks send — so this reads no registry and works through the remote door,
where the registry does not exist.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))
import codex_proxy  # noqa: E402
import machine_harness as harness  # noqa: E402
from process_probe import Undetermined  # noqa: E402
import runtime_id  # noqa: E402
from local_workspace import required_workspace_root  # noqa: E402
from door import say  # noqa: E402
from ack import records, token_path, valid_token  # noqa: E402
from verb_help import current_args, error, help_requested, script_help  # noqa: E402

# The one harness that exports its own session id and pid, and so needs neither flag.
_SELF_DESCRIBING = harness.DEFAULT   # the default when no provider is named


class Refused(Exception):
    """What the caller should be told, and why nothing was sent."""


def pid_arg(value: str) -> int:
    try:
        if not value.isascii() or not value.isdecimal():
            raise ValueError
        pid = int(value)
    except ValueError:
        raise Refused("--pid takes the harness process's own pid") from None
    return pid


def enrollments(ws: Path) -> dict[str, str]:
    return {name: rec["token"] for name, rec in records(ws).items()}


# A listing call fails transiently on a loaded machine — a fork refused by a full process
# table, a `ps` starved past its timeout — and one that used to end the walk silently came
# back empty on #3248's CI, a refusal with nothing to pick from. Retried, and what still
# cannot be listed is carried as a line, so the walk is never silently shorter than the tree.
PS_ATTEMPTS = 3
PS_PAUSE = 0.2


def ancestry(limit: int = 8) -> list[str]:
    """This process and its forebears as `<pid> <command>`, for an operator picking a `--pid`.

    An ancestor that is gone when listed (exit 1, no output) ends the walk; a listing that
    failed some other way is retried, then carried as `<pid> not listed: <why>`."""
    out, pid = [], os.getpid()
    for _ in range(limit):
        entry, ppid, why = "", None, ""
        for attempt in range(PS_ATTEMPTS):
            try:
                r = subprocess.run(["ps", "-o", "ppid=,args=", "-p", str(pid)], capture_output=True, text=True, timeout=1.0)
            except (OSError, subprocess.TimeoutExpired) as e:
                why = f"{type(e).__name__}: {e}"
            else:
                if r.returncode == 1 and not r.stdout.strip():
                    why = r.stderr.strip() or "no such process"  # the walk's natural end; no retry
                    break
                parts = r.stdout.strip().split(None, 1)
                if len(parts) == 2 and parts[0].isdigit():
                    entry, ppid = parts[1][:100], int(parts[0])
                    break
                why = f"exit {r.returncode}: {(r.stderr or r.stdout).strip()[:120]}"
            if attempt + 1 < PS_ATTEMPTS:
                time.sleep(PS_PAUSE)
        out.append(f"    {pid} {entry}" if entry else f"    {pid} not listed: {why}")
        if ppid is None or ppid <= 1:
            break
        pid = ppid
    return out


def identity(pid: int, provider: str = _SELF_DESCRIBING) -> str | None:
    """The runtime id of `pid`, derived the way `observe.py` and `readout.py` derive it."""
    return runtime_id.derive(provider, pid=pid)


def fail(message: str) -> int:
    print(message, file=sys.stderr)
    return 1


def incarnation(psession: str | None = None, pid: int | None = None,
                provider: str = _SELF_DESCRIBING) -> tuple[str, str]:
    """(provider session, runtime id) of the process this runs inside — what `bind:` names."""
    psession, rid, _ = incarnation_of(psession, pid, provider)
    return psession, rid


def incarnation_of(psession: str | None = None, pid: int | None = None,
                   provider: str = _SELF_DESCRIBING) -> tuple[str, str, int]:
    """`incarnation`, with the harness pid the runtime id was derived from."""
    if provider not in harness.PROVIDERS:
        raise Refused(f"{provider} is not a provider: one of {', '.join(harness.PROVIDERS)}")
    # Asked of the harness rather than read here: which session this process is, and which
    # process serves it, are facts about the harness and it is the one place that owns them.
    h = harness.of(provider)
    mine, its_pid = h.whoami()
    exports = h.exports
    psession = psession or mine or ""
    if not psession:
        # Every argument this names is one both commands accept. A message that asks for a flag
        # the entry point rejects is a dead end, and that is what the owner walked into (#543).
        raise Refused(
            "this harness exports no CLAUDE_CODE_SESSION_ID, so it is not Claude Code: pass "
            f"--provider <{'|'.join(p for p in harness.PROVIDERS if p != provider)}> with "
            "--provider-session <this session's id> and --pid <the harness process>"
            if exports else
            f"a {provider} session is not exported to this process: pass --provider-session "
            f"<the thread id> and --pid <the harness process>")
    pid = pid or its_pid or (h.harness_pid(psession) if h.harness_pid is not None else None)
    if pid is None:
        # Named per harness rather than by Claude's variable: every self-describing harness
        # reached this branch and was told to look for `CLAUDE_PID`.
        raise Refused((f"no live CLAUDE_PID: " if provider == _SELF_DESCRIBING else
                       f"no live {provider} harness process for {psession}: ")
                      + "pass --pid <the harness process>, one of\n" + "\n".join(ancestry()))
    rid = identity(pid, provider)
    if rid is None:
        raise Refused(f"no runtime id for pid {pid}: it is not the harness process, or this node has no id")
    return psession, rid, int(pid)


def account_identity(provider: str) -> str | None:
    """The Codex account the active configuration says this process spends from, or for a Claude
    Code session proxied to Codex, the proxy's.

    The bearer credential stays in Codex's auth file.  Its opaque account identifier is already
    the key usage readings carry, and is the only part a bind needs to make a cross-node join.
    """
    if provider == "claude":
        return codex_proxy.proxied_account(os.environ.get("ANTHROPIC_BASE_URL"))
    if provider != "codex":
        return None
    return codex_proxy.auth_account(harness.of(provider).config_dir() / "auth.json")


def bind(session: str, token: str, psession: str | None = None,
         pid: int | None = None, provider: str = _SELF_DESCRIBING) -> tuple[str, str]:
    """(the door's answer to `bind:`, the runtime id it named). The caller gets the id back
    because the stream this session holds at its agent carries it too (doc 35 §2), and nothing
    else in the recipe derives it."""
    ps, rid = incarnation(psession, pid, provider)
    account_id = account_identity(provider)
    suffix = f" account-id {account_id}" if account_id else ""
    return say(f"bind: {session} provider-session {ps} runtime {rid}{suffix} token {token}"), rid


def main(argv: list[str]) -> int:
    try:
        args = current_args("bind", argv)
    except ValueError as why:
        return error("bind", str(why))
    if help_requested("bind", argv):
        print(script_help("bind"))
        return 0
    psession = pid = session = None
    provider = _SELF_DESCRIBING
    while args and args[0].startswith("-"):
        flag = args.pop(0)
        if flag not in ("--provider-session", "--provider", "--pid"):
            return error("bind", f"no such flag: {flag}")
        if not args or args[0].startswith("-"):
            return error("bind", f"{flag} takes a value")
        value = args.pop(0)
        if flag == "--provider-session":
            psession = value
        elif flag == "--provider":
            if value not in harness.PROVIDERS:
                return error("bind", f"{value} is not a provider: one of {', '.join(harness.PROVIDERS)}")
            provider = value
        elif flag == "--pid":
            try:
                pid = pid_arg(value)
            except Refused as why:
                return error("bind", str(why))
    if args:
        session = args.pop(0)
    if args:
        return error("bind", "one session name at most")

    ws = required_workspace_root(Path(os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()), timeout=2.0)
    known = enrollments(ws)
    if session is None:
        if len(known) != 1:
            return fail(f"{ws} has no enrolled session; enroll first" if not known
                        else f"name the session: {ws} holds tokens for {', '.join(sorted(known))}")
        session = next(iter(known))
    if session not in known:
        return fail(f"no stored token for {session} in {ws}: enroll first, storing it with ack.py --store")

    try:
        answer, rid = bind(session, known[session], psession, pid, provider)
    except (Refused, Undetermined) as why:
        return fail(str(why))
    print(answer)
    if not answer.startswith("bound:"):
        if "not an observed candidate" in answer:
            print(f"steering has seen no observation from this incarnation: check the hooks "
                  f"(python3 {HERE / 'hooks.py'} show {ws}) and run this again.", file=sys.stderr)
        elif "that incarnation is bound to" in answer:
            print(f"restart this session, then run python3 {HERE / 'bind.py'} {session}",
                  file=sys.stderr)
        return 1
    print(f"STEERING_RUNTIME_ID={rid}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
