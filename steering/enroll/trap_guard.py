#!/usr/bin/env python3
"""Refuse three shell commands that each caused an incident (#4551, step 4).

A PreToolUse hook on the shell tool, for Claude Code and Codex alike, beside `delete_guard.py`; both
read a command through `shell_words.py`. Each rule matches one shape at the command's own position
and names where it is written: `pkill -f` by a pattern that names neither the worktree nor a
temporary directory, starting `2mw2lt-follow`, and an auto-merge (`gh pr merge -R <repo> --auto`).
A command it cannot read, and its own failure, let the call through: these stop a reflex, they are
not a control.
"""
from __future__ import annotations

import fnmatch
import json
import os
import shlex
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from shell_words import (ASSIGNMENT, SHELLS, Unresolvable, assigned, command_name, expand, heredocs,  # noqa: E402
                         segments, shell_script)

PKILL = ("refused: `pkill -f` by a pattern that names neither this worktree nor a temporary directory can "
         "kill another checkout's process (doc 08, \"Never pkill -f by module path\"). Kill the pid you "
         "captured, or match the worktree's absolute path.")
FOLLOW = ("refused: starting 2mw2lt-follow deploys untested main to production, and it stays stopped by "
          "policy (doc 08, doc 138 §1). Production moves by a deploy the brain makes.")
AUTO = ("refused: an auto-merge (the gate skill: \"Not --auto\"). Merge with `gh pr merge -R <repo> "
        "--squash --match-head-commit <sha>` once the head holds 2mw2lt/review.")

FOLLOW_UNITS = ("2mw2lt-follow", "2mw2lt-follow.service", "2mw2lt-follow.timer")
STARTS = {"start", "restart", "enable", "reenable", "try-restart", "reload-or-restart", "try-reload-or-restart"}
# systemctl's options that take their value as the next word.
SYSTEMCTL_VALUED = {"-H", "--host", "-M", "--machine", "-p", "--property", "-t", "--type", "-s", "--signal",
                    "-n", "--lines", "-o", "--output", "--root", "--state", "--job-mode", "--what"}
# ssh's single-letter options that take a value: the host is the first word after them.
SSH_VALUED = set("BbcDEeFIiJLlmOoPpQRSWw")
# Words that run the word after them, with options of their own: the command is further along.
WRAPPERS = {"sudo", "env", "timeout", "nice", "nohup", "exec", "command", "time", "doas"}
WRAPPER_VALUED = {"-u", "-g", "-n", "-s", "-k", "-C", "-D", "-p", "-r", "-t", "-U", "-h"}
DEPTH = 3


class Known(dict):
    """The environment as far as the hook can see it: a name it never saw is unknown (None), not
    empty, so a pattern built on it is let through rather than read as `/server.py`."""

    def get(self, key, default=None):
        return super().get(key)


def _pkill(args: list[str], env: Known, ground: list[str]) -> str | None:
    full = any(a == "--full" or (a.startswith("-") and not a.startswith("--") and "f" in a[1:]) for a in args)
    plain = [a for a in args if not a.startswith("-")]
    if not full or not plain:
        return None
    # A pattern built from what the guard cannot see (a variable the hook's shell never set, a
    # substitution) is let through: only a pattern known to name no ground of its own is the trap.
    try:
        pattern = expand(plain[-1], env)
    except Unresolvable:
        return None
    return None if any(root in pattern for root in ground) else PKILL


def _systemctl(args: list[str], _env: Known, _ground: list[str]) -> str | None:
    plain, i = [], 0
    while i < len(args):
        if args[i] in SYSTEMCTL_VALUED:
            i += 2
            continue
        if not args[i].startswith("-"):
            plain.append(args[i])
        i += 1
    if not plain or plain[0] not in STARTS:
        return None
    units = [os.path.basename(u) for u in plain[1:]]
    return FOLLOW if any(fnmatch.fnmatchcase(name, u) for u in units for name in FOLLOW_UNITS) else None


def _gh(args: list[str], _env: Known, _ground: list[str]) -> str | None:
    # gh's own options may come before its command: `gh -R o/r pr merge`.
    i = 0
    while i < len(args) and args[i].startswith("-"):
        i += 2 if args[i] in ("-R", "--repo", "--hostname") else 1
    auto = any(a == "--auto" or a.startswith("--auto=") for a in args[i:])
    return AUTO if args[i:i + 2] == ["pr", "merge"] and auto else None


RULES = {"pkill": _pkill, "systemctl": _systemctl, "gh": _gh}


def _ssh_command(args: list[str]) -> str | None:
    """The remote command of `ssh [options] host command...`, or None."""
    i = 0
    while i < len(args) and args[i].startswith("-"):
        flags = args[i][1:]
        i += 2 if len(flags) == 1 and flags in SSH_VALUED else 1
    rest = args[i + 1:]
    return " ".join(rest) if rest else None


def _command(words: list[str], env: Known) -> int | None:
    """Where the command is among a segment's words, past any wrapper and its options."""
    i = 0
    while i < len(words):
        name = command_name(words[i], env)
        if name not in WRAPPERS:
            return i
        i += 1
        if name == "timeout":
            while i < len(words) and words[i].startswith("-"):
                i += 2 if words[i] in ("-k", "-s", "--kill-after", "--signal") else 1
            i += 1                                   # the duration
            continue
        while i < len(words) and (words[i].startswith("-") or (name == "env" and ASSIGNMENT.match(words[i]))):
            i += 2 if words[i] in WRAPPER_VALUED else 1
    return None


def refusal(command: str, env: Known, ground: list[str], depth: int = 0) -> str | None:
    """The sentence that refuses `command`, or None."""
    if depth > DEPTH:
        return None
    command, scripts = heredocs(command)
    for script in scripts:
        why = refusal(script, env, ground, depth + 1)
        if why:
            return why
    env = Known(env)
    for words in segments(command):
        while words and ASSIGNMENT.match(words[0]):
            name, value = ASSIGNMENT.match(words[0]).groups()
            env[name] = assigned(value, env)
            words = words[1:]
        at = _command(words, env)
        if at is None:
            continue
        name, args = command_name(words[at], env), words[at + 1:]
        if name == "eval":
            why = refusal(" ".join(args), env, ground, depth + 1)
        elif name in SHELLS or name == "ssh":
            nested = shell_script(args) if name in SHELLS else _ssh_command(args)
            why = refusal(nested, env, ground, depth + 1) if nested is not None else None
        else:
            rule = RULES.get(name)
            why = rule(args, env, ground) if rule else None
        if why:
            return why
    return None


def ground(cwd: str | None) -> list[str]:
    """What a `pkill -f` pattern may name to be scoped: the worktree it runs in, and the temporary
    directories a test daemon runs from."""
    roots = ["/tmp/", "/private/tmp/", "/private/var/folders/",
             os.path.join(os.path.realpath(tempfile.gettempdir()), "")]
    try:
        top = subprocess.run(["git", "-C", cwd or os.getcwd(), "rev-parse", "--show-toplevel"],
                             capture_output=True, text=True, timeout=2).stdout.strip()
        if top:
            roots.append(os.path.join(top, ""))
    except (OSError, subprocess.SubprocessError):
        pass
    return roots


def verdict(payload: dict, env: dict | None = None) -> str | None:
    """None to let the call through, otherwise the sentence that refuses it."""
    tool = payload.get("tool_input") or {}
    command = tool.get("command") if isinstance(tool, dict) else None
    if isinstance(command, list):
        command = shlex.join(str(part) for part in command)
    if not isinstance(command, str):
        return None
    try:
        return refusal(command, Known(os.environ if env is None else env), ground(payload.get("cwd")))
    except (ValueError, Unresolvable):
        return None


def main() -> int:
    argv = sys.argv[1:]
    if argv:
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
        import verb_help
        if verb_help.help_requested("trap_guard", argv, bare=False):
            print(verb_help.script_help("trap_guard"))
            return 0
        return verb_help.error("trap_guard", f"unexpected arguments: {' '.join(argv)}")
    try:
        payload = json.load(sys.stdin)
        why = verdict(payload) if isinstance(payload, dict) else None
    except Exception:
        return 0
    if why is None:
        return 0
    print(why, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
