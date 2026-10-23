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
AUTO = ("refused: an auto-merge (the gate skill: \"Not --auto\"). Merge with `gate.py <session> merge <n> "
        "--head <sha>` once the head holds 2mw2lt/review.")
ADMIN = ("refused: an admin merge bypasses main's ruleset, which admits only a head holding "
         "2mw2lt/review. Merge with `gate.py <session> merge <n> --head <sha>`.")

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
MERGE_SHAPED = re.compile(r"\bgh pr merge (?:[1-9][0-9]* )?-R ")


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
    scoped = re.escape(repo)
    pattern = (r"gh pr merge (?:[1-9][0-9]* -R " + scoped + r"|-R " + scoped + r" [1-9][0-9]*)"
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
# `--auto` and `--admin` merges, the editor and browser forms, and a create's `--recover`, which
# reloads its draft from a file after the words are read, on purpose.
FORGE_VERBS = {
    ("pr", "create"): ({"-R", "--repo", "-a", "--assignee", "-B", "--base", "-b", "--body", "-F", "--body-file", "-H", "--head",
                        "-l", "--label", "-m", "--milestone", "-p", "--project", "-r", "--reviewer", "-t", "--title",
                        "-T", "--template"},
                       {"-d", "--draft", "-f", "--fill", "--fill-first", "--fill-verbose", "--no-maintainer-edit", "--dry-run"}, False),
    ("pr", "comment"): ({"-R", "--repo", "-b", "--body", "-F", "--body-file"},
                        {"--edit-last", "--create-if-none", "--delete-last", "--yes"}, True),
    ("pr", "edit"): ({"-R", "--repo", "-t", "--title", "-b", "--body", "-F", "--body-file"}, set(), True),
    ("pr", "ready"): ({"-R", "--repo"}, {"--undo"}, True),
    ("pr", "merge"): ({"-R", "--repo", "-b", "--body", "-F", "--body-file", "-t", "--subject", "-A", "--author-email",
                       "--match-head-commit"},
                      {"-s", "--squash", "-m", "--merge", "-r", "--rebase", "-d", "--delete-branch", "--disable-auto"}, True),
    ("issue", "comment"): ({"-R", "--repo", "-b", "--body", "-F", "--body-file"},
                           {"--edit-last", "--create-if-none", "--delete-last", "--yes"}, True),
    ("issue", "create"): ({"-R", "--repo", "-a", "--assignee", "-b", "--body", "-F", "--body-file", "-l", "--label",
                           "-m", "--milestone", "-p", "--project", "-t", "--title", "-T", "--template", "--recover"}, set(), False),
    # Sessions perform first (the owner's ruling, 2026-10-23): a worker closes, edits and reads issues
    # and dispatches a workflow without asking.
    ("issue", "close"): ({"-R", "--repo", "-c", "--comment", "-r", "--reason"}, set(), True),
    ("issue", "edit"): ({"-R", "--repo", "-t", "--title", "-b", "--body", "-F", "--body-file", "--add-label",
                         "--remove-label", "--add-assignee", "--remove-assignee", "-m", "--milestone"}, set(), True),
    ("issue", "view"): ({"-R", "--repo", "--json", "-q", "--jq"}, {"-c", "--comments"}, True),
    # Its positional is the workflow's name or file, not a number.
    ("workflow", "run"): ({"-R", "--repo", "-r", "--ref", "-f", "--raw-field", "-F", "--field"}, {"--json"}, "name"),
}
# A verb that passes only with one of these options: a worker opens its pull request as a draft (the
# owner's ruling, documentation/knowledge/decisions/workers-open-their-own-pull-requests.md).
FORGE_REQUIRED = {("pr", "create"): {"-d", "--draft"}}
# A verb that passes only on the session's own pull request, one whose head is the checkout's branch.
FORGE_OWN = {("pr", "edit")}
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


def gh_refusal(args: list[str], repo: str, check=None, verbs=FORGE_VERBS, owned=None) -> str | None:
    """Why the gh words do not pass a grant on repo, naming the check and the word it failed on, or
    None when they pass. verbs and owned are the grant's policy, the hook's by default: owned maps a
    verb to the kind of ownership it needs, `pull` for a numbered pull request, `issue` for a number
    that may be an issue or a pull request, `head` for a create's head branch. check(kind, value)
    answers None when the session owns value, else why not."""
    owned = {verb: "pull" for verb in FORGE_OWN} if owned is None else owned
    verb = tuple(args[:2])
    spec = verbs.get(verb)
    if spec is None:
        return f"forge-verb-not-granted: gh {' '.join(args[:2])} is not in the grant"
    values, bools, positional = spec
    named, numbers, i, seen, number, head = False, 0, 2, set(), None, None
    while i < len(args):
        arg = args[i]
        flag, glued, value = arg.partition("=")
        if not arg.startswith("-"):
            if not positional or positional is True and not arg.isdigit():
                return f"forge-flag-not-granted: {arg!r} is no option or number gh {' '.join(verb)} is granted"
            numbers, number = numbers + 1, arg
        elif arg in bools:
            seen.add(arg)
        elif flag in values and (glued and flag.startswith("--") or not glued and i + 1 < len(args)):
            if not glued:
                i, value = i + 1, args[i + 1]
            if flag in ("-R", "--repo"):
                if value.lower() != repo.lower():
                    return f"forge-repo-not-granted: {flag} names {value}, and the grant covers {repo}"
                named = True
            elif flag in ("-H", "--head"):
                head = value
        elif flag in values:
            return f"forge-flag-not-granted: {flag} wants a value"
        else:
            return f"forge-flag-not-granted: {flag} is not granted on gh {' '.join(verb)}"
        i += 1
    if not named:
        return f"forge-repo-not-granted: no -R names the repository; name -R {repo}"
    if numbers > 1:
        return "forge-flag-not-granted: more than one number"
    if verb in FORGE_REQUIRED and not seen & FORGE_REQUIRED[verb]:
        return "forge-draft-required: a worker opens its pull request as a draft; add --draft"
    kind = owned.get(verb)
    if kind is None:
        return None
    if kind in ("pull", "issue") and number is None:
        return f"forge-not-own-pull: gh {' '.join(verb)} names no number to check"
    if check is None:
        return "forge-not-own-pull: no ownership check was given"
    return check(kind, head if kind == "head" else number)


def _gh_granted(args: list[str], repo: str, own=None) -> bool:
    """Whether the gh words pass the hook's grant on repo; own(number) answers whether a pull
    request is the session's own."""
    return gh_refusal(args, repo, lambda kind, n: None if own is not None and own(n) else "forge-not-own-pull") is None


def branch_of(cwd: str | None) -> tuple[str, str]:
    """The branch the checkout at cwd is on, or empty and why it could not be read."""
    try:
        run = subprocess.run(["git", "-C", cwd or os.getcwd(), "branch", "--show-current"],
                             capture_output=True, text=True, timeout=2)
    except subprocess.TimeoutExpired:
        return "", "forge-ownership-unknown: git took over 2s to name this checkout's branch"
    except (OSError, subprocess.SubprocessError) as e:
        return "", f"forge-ownership-unknown: git could not run: {e}"
    if run.returncode != 0:
        return "", f"forge-ownership-unknown: git branch exited {run.returncode}: {run.stderr.strip()[:200]}"
    if not run.stdout.strip():
        return "", "forge-ownership-unknown: this checkout is on no branch"
    return run.stdout.strip(), ""


def current_branch(cwd: str | None) -> str:
    """The branch the checkout at cwd is on, or empty when it is on none or cannot be read."""
    return branch_of(cwd)[0]


def _gh(args: list[str], what: str) -> tuple[str | None, str]:
    """gh's answer to args within the hook's budget, or None and why: its exit and GitHub's words, or
    the timeout."""
    try:
        run = subprocess.run(["gh", *args], capture_output=True, text=True, timeout=3)
    except subprocess.TimeoutExpired:
        return None, f"forge-ownership-unknown: gh took over 3s to answer {what}"
    except (OSError, subprocess.SubprocessError) as e:
        return None, f"forge-ownership-unknown: gh could not run: {e}"
    if run.returncode != 0:
        return None, f"forge-ownership-unknown: gh exited {run.returncode} on {what}: {run.stderr.strip()[:200]}"
    return run.stdout.strip(), ""


def pull_refusal(number: str, repo: str, cwd: str | None) -> str | None:
    """None when the pull request's head is the branch the checkout at cwd is on, as GitHub answers
    it; otherwise why not, a lookup that failed kept apart from a head that is another branch's."""
    branch, why = branch_of(cwd)
    if not branch:
        return why
    head, why = _gh(["pr", "view", number, "-R", repo, "--json", "headRefName", "-q", ".headRefName"],
                    f"pull request #{number}'s head")
    if head is None:
        return why
    return None if head == branch else f"forge-not-own-pull: #{number}'s head is {head}, and this checkout is on {branch}"


def issue_refusal(number: str, repo: str, cwd: str | None) -> str | None:
    """None for a number that is an issue, or a pull request of the session's own: gh comments on
    either through `issue comment`."""
    pull, why = _gh(["api", f"repos/{repo}/issues/{number}", "--jq", ".pull_request != null"], f"whether #{number} is a pull request")
    if pull is None:
        return why
    return pull_refusal(number, repo, cwd) if pull == "true" else None


def own_pull(number: str, repo: str, cwd: str | None) -> bool:
    """Whether the pull request's head is the branch the checkout at cwd is on, as GitHub answers it
    within the hook's budget; a lookup that fails or is slow is not the session's own."""
    return pull_refusal(number, repo, cwd) is None


BINDING = os.path.join(".claude", "steering-workspace")


def grant_repo(payload: dict, environ) -> str | None:
    """The repository the forge grant covers: the launch's, else that of a checkout bound to a
    workspace, whose binding install writes only when the workspace serves its `origin` (#2618).
    It only reads; anything it cannot read grants nothing."""
    return grant_repo_why(payload, environ)[0]


def _redacted(text: str) -> str:
    """git's words with any URL's userinfo taken out, since an origin may carry a credential."""
    return re.sub(r"(?<=://)[^/@\s]+@", "***@", text.strip())[:200]


def grant_repo_why(payload: dict, environ) -> tuple[str | None, str]:
    """grant_repo's answer, and why it is none: each failed check named apart, its tool's status and
    words kept, a URL's credential never."""
    repo = environ.get(FORGE_GRANT_ENV, "")
    if repo:
        return (repo, "") if GRANT_SHAPE.fullmatch(repo) else (None, f"{FORGE_GRANT_ENV} is not owner/name")
    cwd = payload.get("cwd") if isinstance(payload.get("cwd"), str) else None
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    try:
        import local_workspace
    except ImportError as e:
        return None, f"the workspace reader could not load: {e}"

    def git(*args: str) -> tuple[str | None, str]:
        try:
            run = subprocess.run(["git", "-C", cwd or os.getcwd(), *args], capture_output=True, text=True, timeout=2)
        except subprocess.TimeoutExpired:
            return None, f"git {args[0]} took over 2s"
        except (OSError, subprocess.SubprocessError) as e:
            return None, f"git could not run: {e}"
        if run.returncode != 0:
            return None, f"git {' '.join(args)} exited {run.returncode}: {_redacted(run.stderr)}"
        return run.stdout.strip(), ""

    common, why = git("rev-parse", "--path-format=absolute", "--git-common-dir")
    if common is None:
        return None, why
    root = Path(common).parent
    if not root.joinpath(BINDING).is_file():
        return None, f"{root} is bound to no workspace ({BINDING} is absent)"
    remote, why = git("remote", "get-url", "origin")
    if remote is None:
        return None, f"the checkout's origin could not be read: {why}"
    slug, why = local_workspace.github_slug_why(remote, timeout=2)
    if slug is None:
        return None, f"origin is no GitHub repository: {why}"
    return (slug, "") if GRANT_SHAPE.fullmatch(slug) else (None, "origin's owner/name is not one the grant reads")


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
    """Allow for a literal call of a forge verb the grant covers, on its repository, or of an
    enrolment client; None for anything else."""
    if not isinstance(payload, dict) or payload.get("tool_name") != "Bash":
        return None
    tool = payload.get("tool_input")
    command = tool.get("command") if isinstance(tool, dict) else None
    words = literal_words(command) if isinstance(command, str) else None
    if not words or len(words) < 2 or words[0] not in ("gh", "python3"):
        return None
    repo = grant_repo(payload, os.environ if environ is None else environ)
    if repo is None:
        return None
    cwd = payload.get("cwd") if isinstance(payload.get("cwd"), str) else None
    granted = ((words[0] == "gh" and _gh_granted(words[1:], repo, lambda n: own_pull(n, repo, cwd)))
               or (words[0] == "python3" and _client_granted(words[1])))
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
