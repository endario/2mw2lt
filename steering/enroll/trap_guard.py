#!/usr/bin/env python3
"""Refuse three shell commands that each caused an incident (#4551, step 4).

A PreToolUse hook on the shell tool, for Claude Code and Codex alike, beside `delete_guard.py`; both
read a command through `shell_words.py`. Each rule matches one shape at the command's own position
and names where it is written: `pkill -f` by a pattern that names neither the worktree nor a
temporary directory, starting `2mw2lt-follow`, and an auto-merge (`gh pr merge -R <repo> --auto`).
A command it cannot read, and its own failure, let the call through: these stop a reflex, they are
not a control.

It also answers `allow`, and only that, for two grants: a project's exact SOP merge, and a launched
session's forge writes on its own repository and its enrolment clients (#7528). Either one reads a
command exactly or gives no answer.
"""
from __future__ import annotations

import fnmatch
import json
import os
import re
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
ADMIN = ("refused: an admin merge bypasses main's ruleset, which admits only a head holding "
         "2mw2lt/review. Merge with `gh pr merge <n> -R <repo> --squash --match-head-commit <sha>`.")

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
    if args[i:i + 2] != ["pr", "merge"]:
        return None
    if any(a == "--auto" or a.startswith("--auto=") for a in args[i:]):
        return AUTO
    # The workspace's merge allow rule ends in a glob, so a flag after the head reaches here (#7039).
    return ADMIN if any(a == "--admin" or a.startswith("--admin=") for a in args[i:]) else None


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


_top: dict[str | None, str | None] = {}


def toplevel(cwd: str | None) -> str | None:
    """The git worktree cwd stands in, once per process: the guard's scoping and the grant's lookup
    read it alike, and one subprocess answers both inside the hook's budget."""
    if cwd not in _top:
        try:
            found = subprocess.run(["git", "-C", cwd or os.getcwd(), "rev-parse", "--show-toplevel"],
                                   capture_output=True, text=True, timeout=2).stdout.strip()
            _top[cwd] = found or None
        except (OSError, subprocess.SubprocessError):
            _top[cwd] = None
    return _top[cwd]


def ground(cwd: str | None) -> list[str]:
    """What a `pkill -f` pattern may name to be scoped: the worktree it runs in, and the temporary
    directories a test daemon runs from."""
    roots = ["/tmp/", "/private/tmp/", "/private/var/folders/",
             os.path.join(os.path.realpath(tempfile.gettempdir()), "")]
    if top := toplevel(cwd):
        roots.append(os.path.join(top, ""))
    return roots


GRANT_FILE = os.path.join(".claude", "merge-grant")
GRANT_SHAPE = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
# Only the SOP merge's own shape pays for the grant file's read.
MERGE_SHAPED = re.compile(r"\bgh pr merge [1-9][0-9]* -R ")


def granted_repo(payload) -> str | None:
    """The repo this tree grants the SOP merge for, read from a grant file instead of a flag the
    invoking settings carried (#7467): a host checkout older than the settings that name a script's
    arguments refused every shell call, while data an older script ignores only loses the grant.
    The tree the command works in carries the grant, then the project dir; a file that is absent or
    not one owner/repo line grants nothing."""
    command = None
    if isinstance(payload, dict):
        tool = payload.get("tool_input")
        command = tool.get("command") if isinstance(tool, dict) else None
    if not isinstance(command, str) or not MERGE_SHAPED.search(command):
        return None
    roots = []
    if isinstance(payload.get("cwd"), str):
        if top := toplevel(payload["cwd"]):
            roots.append(top)
    if project := os.environ.get("CLAUDE_PROJECT_DIR"):
        roots.append(project)
    for root in roots:
        try:
            first = Path(root, GRANT_FILE).read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        if len(first) >= 1 and GRANT_SHAPE.fullmatch(first[0].strip()):
            return first[0].strip()
    return None


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


def merge_grant(payload, repo: str) -> dict | None:
    """A project's opt-in grant uses a literal command shape, not shell glob matching."""
    if not isinstance(payload, dict) or payload.get("tool_name") != "Bash":
        return None
    tool = payload.get("tool_input")
    command = tool.get("command") if isinstance(tool, dict) else None
    pattern = (r"gh pr merge [1-9][0-9]* -R " + re.escape(repo)
               + r" --squash --match-head-commit [0-9a-fA-F]{40}")
    if not isinstance(command, str) or re.fullmatch(pattern, command) is None:
        return None
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "allow"}}


# The launch's forge grant (#7528, documentation/steering/plans/2026-10-19-launch-forge-approval-design.md).
# A launched Claude Code session is granted its SOP's forge writes as prefix rules
# (machine/internal/interactive/start.go claudeSettings), and Claude Code matches none against a
# quoted body holding a line that starts with `#`: an OpenAI-backed launch's classifier then
# refused the write. The launch names its repository in FORGE_GRANT_ENV, which only it sets; an
# older script ignores the variable and only loses the grant (#7467).
FORGE_GRANT_ENV = "STEERING_FORGE_GRANT"
# Each verb of that grant: the options it may carry, those taking a value and those not, and
# whether it names its pull request or issue by number. An option not here gets no answer: the
# `--auto` and `--admin` merges, and the editor and browser forms, on purpose.
FORGE_VERBS = {
    ("pr", "create"): ({"-R", "--repo", "-a", "--assignee", "-B", "--base", "-b", "--body", "-F", "--body-file", "-H", "--head",
                        "-l", "--label", "-m", "--milestone", "-p", "--project", "-r", "--reviewer", "-t", "--title",
                        "-T", "--template", "--recover"},
                       {"-d", "--draft", "-f", "--fill", "--fill-first", "--fill-verbose", "--no-maintainer-edit", "--dry-run"}, False),
    ("pr", "comment"): ({"-R", "--repo", "-b", "--body", "-F", "--body-file"},
                        {"--edit-last", "--create-if-none", "--delete-last", "--yes"}, True),
    ("pr", "ready"): ({"-R", "--repo"}, {"--undo"}, True),
    ("pr", "merge"): ({"-R", "--repo", "-b", "--body", "-F", "--body-file", "-t", "--subject", "-A", "--author-email",
                       "--match-head-commit"},
                      {"-s", "--squash", "-m", "--merge", "-r", "--rebase", "-d", "--delete-branch", "--disable-auto"}, True),
    ("issue", "comment"): ({"-R", "--repo", "-b", "--body", "-F", "--body-file"},
                           {"--edit-last", "--create-if-none", "--delete-last", "--yes"}, True),
    ("issue", "create"): ({"-R", "--repo", "-a", "--assignee", "-b", "--body", "-F", "--body-file", "-l", "--label",
                           "-m", "--milestone", "-p", "--project", "-t", "--title", "-T", "--template", "--recover"}, set(), False),
}
# The enrolment clients the connect, gate and checkpoint skills have a worker run; ghtoken.py,
# which prints a credential, and the seat's own are not among them.
FORGE_CLIENTS = {"ack", "bulletin", "checkpoint", "connect", "declare", "disconnect", "edge", "gate", "rest", "say",
                 "taking", "verb"}
# The one command shape the grant reads: words of literal text, separated by spaces, with nothing
# a shell expands. Unquoted text holds no glob, brace, tilde, comment or separator; a single-quoted
# run is literal; a double-quoted run holds no `$` or backquote and only the escapes the shell
# removes there. Quoted runs may span lines, so a body passes whatever its headings. Anything else
# (a second command, a pipe, a redirect or here-doc, an assignment prefix, a substitution) does not
# match and gets no answer.
# Possessive throughout: a word is read once, so a command that does not match fails in linear time.
_PART = r"[A-Za-z0-9_./:@+,=-]++|'[^']*+'|" + r'"(?:[^"\\$`]++|\\[\\"$`])*+"'
_WORD = re.compile(rf"(?:{_PART})++", re.S)
_CALL = re.compile(rf"[ \t]*+(?:{_PART})++(?:[ \t]++(?:{_PART})++)*+[ \t]*+", re.S)


def literal_words(command: str) -> list[str] | None:
    """The words of a command in the grant's one shape, as the shell would pass them, or None."""
    if not _CALL.fullmatch(command):
        return None
    words = []
    for match in _WORD.finditer(command):
        word = match.group(0)
        # zsh expands a word that starts with an unquoted `=` to a command's path.
        if word.startswith("="):
            return None
        out = []
        for part in re.finditer(_PART, word, re.S):
            text = part.group(0)
            if text.startswith("'"):
                out.append(text[1:-1])
            elif text.startswith('"'):
                out.append(re.sub(r'\\([\\"$`])', r"\1", text[1:-1]))
            else:
                out.append(text)
        words.append("".join(out))
    return words


def _gh_granted(args: list[str], repo: str) -> bool:
    spec = FORGE_VERBS.get(tuple(args[:2]))
    if spec is None:
        return False
    values, bools, positional = spec
    named, numbers, i = False, 0, 2
    while i < len(args):
        arg = args[i]
        flag, glued, value = arg.partition("=")
        if not arg.startswith("-"):
            if not positional or not arg.isdigit():
                return False
            numbers += 1
        elif arg in bools:
            pass
        elif flag in values and (glued and flag.startswith("--") or not glued and i + 1 < len(args)):
            if not glued:
                i, value = i + 1, args[i + 1]
            if flag in ("-R", "--repo"):
                if value.lower() != repo.lower():
                    return False
                named = True
        else:
            return False
        i += 1
    return named and numbers <= 1


def _client_granted(path: str) -> bool:
    """A client of the list, a regular file directly in this plugin's own enroll directory or the
    same directory of another version in its cache, read through every link."""
    script = Path(path)
    if not script.is_absolute() or script.suffix != ".py" or script.stem not in FORGE_CLIENTS:
        return False
    try:
        real = script.resolve(strict=True)
    except (OSError, RuntimeError):
        return False
    if real.name != script.name or not real.is_file():
        return False
    own = Path(__file__).resolve().parent
    if real.parent == own:
        return True
    cache = own.parent.parent.parent
    return (cache.parts[-3:] == ("cache", "2mw2lt", "2mw2lt") and real.parent.name == "enroll"
            and real.parent.parent.name == "steering" and real.parent.parent.parent.parent == cache)


def forge_grant(payload, environ=None) -> dict | None:
    """Allow for a literal call of a forge verb the launch grants, on its repository, or of an
    enrolment client; None for anything else."""
    repo = (os.environ if environ is None else environ).get(FORGE_GRANT_ENV, "")
    if not GRANT_SHAPE.fullmatch(repo) or not isinstance(payload, dict) or payload.get("tool_name") != "Bash":
        return None
    tool = payload.get("tool_input")
    command = tool.get("command") if isinstance(tool, dict) else None
    words = literal_words(command) if isinstance(command, str) else None
    if not words or len(words) < 2:
        return None
    granted = (words[0] == "gh" and _gh_granted(words[1:], repo)) or (words[0] == "python3" and _client_granted(words[1]))
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "allow"}} if granted else None


def main() -> int:
    argv = sys.argv[1:]
    granting = (len(argv) == 2 and argv[0] == "--merge-grant"
                and re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", argv[1]) is not None)
    if argv and not granting:
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
        import verb_help
        if verb_help.help_requested("trap_guard", argv, bare=False):
            print(verb_help.script_help("trap_guard"))
            return 0
        if argv[0] == "--merge-grant":
            print("trap_guard: expected owner/repo for --merge-grant; no command granted", file=sys.stderr)
            return 0
        return verb_help.error("trap_guard", f"unexpected arguments: {' '.join(argv)}")
    try:
        payload = json.load(sys.stdin)
        if granting:
            if grant := merge_grant(payload, argv[1]):
                print(json.dumps(grant))
            return 0
        # A tree's grant file is read by the script itself, so the grant rides the release of the
        # script that knows it (#7467): an older script beside newer settings ignores the file and
        # only loses the grant, where the flag it did not know refused every call.
        if (repo := granted_repo(payload)) and (grant := merge_grant(payload, repo)):
            print(json.dumps(grant))
            return 0
        why = verdict(payload) if isinstance(payload, dict) else None
        if why is None:
            if grant := forge_grant(payload):
                print(json.dumps(grant))
            return 0
    except Exception:
        return 0
    print(why, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
