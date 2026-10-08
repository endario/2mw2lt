#!/usr/bin/env python3
"""Refuse a recursive delete that reaches outside the session's own ground (#3909).

A PreToolUse hook on the shell tool, for Claude Code and Codex alike. It finds every recursive
delete in the command (`rm -r`, `find -delete` or `-exec rm -r`, `rsync --delete`), resolves each
target as the shell would (`..`, symlinked parents, a variable that is empty, a glob), and blocks
the call when a target falls outside the working tree, the session's scratchpad or its task
directory. A target it cannot resolve, such as a command substitution, is refused too. Anything
else, including its own failure, lets the call through: the harness's own warnings stay the
other line of defence.
"""
from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from shell_words import (ASSIGNMENT, SHELLS, Unresolvable, assigned as _assigned,  # noqa: E402
                         command_name as _command_name, expand as _expand, segments as _segments,
                         heredocs, shell_script as _shell_script)

GLOB = re.compile(r"[*?[]")





def _resolve(target: str, cwd: Path) -> tuple[Path, bool]:
    """The path a delete reaches, and whether it is the directory a glob expands in."""
    match = GLOB.search(target)
    if match:
        head = target[: match.start()]
        target = head[: head.rfind("/") + 1] if "/" in head else "."
    full = os.path.join(str(cwd), target)
    name = os.path.basename(full)
    # A trailing slash, `.` or `..` reaches through to the directory itself. Otherwise the parent
    # is followed as the kernel follows it, symlinks and `..` alike, and a symlink as the last
    # component is what rm removes, not what it points at.
    if name in ("", ".", ".."):
        return Path(os.path.realpath(full)), bool(match)
    return Path(os.path.realpath(os.path.dirname(full))) / name, bool(match)


def _rm_targets(args: list[str]) -> list[str] | None:
    recursive, targets, options = False, [], True
    for arg in args:
        if options and arg == "--":
            options = False
        elif options and arg.startswith("--"):
            recursive |= arg in ("--recursive", "--dir")
        elif options and arg.startswith("-") and len(arg) > 1:
            recursive |= "r" in arg[1:] or "R" in arg[1:]
        else:
            targets.append(arg)
    return targets if recursive else None


def _find_targets(args: list[str]) -> list[str] | None:
    starts = []
    for arg in args:
        if arg.startswith(("-", "(", "!")) or arg in ("(", ")", "!"):
            break
        starts.append(arg)
    deletes = "-delete" in args
    for i, arg in enumerate(args):
        if arg in ("-exec", "-execdir", "-ok", "-okdir") and i + 1 < len(args) and args[i + 1] == "rm":
            deletes |= _rm_targets(args[i + 2:]) is not None
    return (starts or ["."]) if deletes else None


def _rsync_targets(args: list[str]) -> list[str] | None:
    if not any(arg.startswith("--delete") for arg in args):
        return None
    plain = [arg for arg in args if not arg.startswith("-")]
    if len(plain) < 2 or re.match(r"^[^/]*:", plain[-1]):
        return []
    return [plain[-1]]


HANDLERS = {"rm": _rm_targets, "find": _find_targets, "rsync": _rsync_targets}


def targets(command: str, cwd: Path, env: dict[str, str]) -> list[tuple[str, Path, bool]]:
    """Each recursive delete's (as written, resolved, is-a-glob's-directory)."""
    env: dict[str, str | None] = dict(env)
    found = []
    # A here-document is words unless a shell or ssh reads it as its script: a commit message that
    # describes a delete is not one.
    command, scripts = heredocs(command)
    for script in scripts:
        found.extend(targets(script, cwd, env))
    for words in _segments(command):
        while words and ASSIGNMENT.match(words[0]):
            name, value = ASSIGNMENT.match(words[0]).groups()
            env[name] = _assigned(value, env)
            words = words[1:]
        if not words:
            continue
        if words[0] == "export":
            for word in words[1:]:
                if ASSIGNMENT.match(word):
                    key, value = ASSIGNMENT.match(word).groups()
                    env[key] = _assigned(value, env)
            continue
        if words[0] == "cd":
            try:
                cwd = Path(os.path.normpath(cwd / _expand(words[1] if len(words) > 1 else "~", env)))
            except Unresolvable:
                pass
            continue
        # The delete may sit behind a wrapper and its options (`sudo -u x`, `nice -n 5`, `xargs
        # -0`), be named through a variable, or be a script handed to a shell: each word is
        # tried as the command.
        for i, word in enumerate(words):
            name = _command_name(word, env)
            if name == "eval":
                found.extend(targets(" ".join(words[i + 1:]), cwd, env))
                break
            if name in SHELLS:
                script = _shell_script(words[i + 1:])
                if script is not None:
                    found.extend(targets(script, cwd, env))
                    break
            written = HANDLERS.get(name, lambda _a: None)(words[i + 1:])
            if written is None:
                continue
            if any(_command_name(w, env) == "xargs" for w in words[:i]):
                raise Unresolvable("xargs " + name)
            for target in written:
                path = _expand(target, env)
                if path:
                    resolved, is_glob = _resolve(path, cwd)
                    found.append((target, resolved, is_glob))
            break
    return found




def roots(payload: dict, cwd: Path) -> list[Path]:
    """The worktree holding the command's directory, and this Claude session's scratch."""
    allowed = []
    try:
        top = subprocess.run(["git", "-C", str(cwd), "rev-parse", "--show-toplevel"],
                             capture_output=True, text=True, timeout=2).stdout.strip()
        if top:
            allowed.append(Path(os.path.realpath(top)))
    except (OSError, subprocess.SubprocessError):
        pass
    session, transcript = payload.get("session_id"), payload.get("transcript_path")
    if isinstance(session, str) and session and isinstance(transcript, str) and transcript:
        project = Path(transcript).parent.name
        allowed.append(Path(os.path.realpath(f"/tmp/claude-{os.getuid()}/{project}/{session}")))
    else:
        # A harness that names no scratch of its own, Codex among them, works under the temp
        # directory: what is inside it passes, the directory and everything above it do not.
        allowed.append(Path(os.path.realpath(tempfile.gettempdir())))
    return allowed


def _inside(path: Path, root: Path, is_glob: bool) -> bool:
    return path == root and is_glob or root in path.parents


def verdict(payload: dict, env: dict[str, str] | None = None) -> str | None:
    """None to let the call through, otherwise the sentence that refuses it."""
    tool = payload.get("tool_input") or {}
    command = tool.get("command") if isinstance(tool, dict) else None
    if isinstance(command, list):
        command = shlex.join(str(part) for part in command)
    if not isinstance(command, str):
        return None
    cwd = Path(payload.get("cwd") or os.getcwd())
    env = os.environ.copy() if env is None else env
    try:
        found = targets(command, cwd, env)
    except Unresolvable as error:
        return (f"Refused: a recursive delete here has a target the guard cannot resolve ({error}). "
                f"Name the paths literally, inside {_names(roots(payload, cwd))}.")
    except ValueError:
        return None
    if not found:
        return None
    allowed = roots(payload, cwd)
    for word, path, is_glob in found:
        if not any(_inside(path, root, is_glob) for root in allowed):
            return (f"Refused: `{word}` is a recursive delete of {path}, outside {_names(allowed)}. "
                    "Delete only inside those, and remove a worktree with `git worktree remove`.")
    return None


def _names(allowed: list[Path]) -> str:
    return " and ".join(str(root) for root in allowed) or "this session's worktree and scratch directories"


def main() -> int:
    argv = sys.argv[1:]
    if argv:
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
        import verb_help
        if verb_help.help_requested("delete_guard", argv, bare=False):
            print(verb_help.script_help("delete_guard"))
            return 0
        return verb_help.error("delete_guard", f"unexpected arguments: {' '.join(argv)}")
    try:
        payload = json.load(sys.stdin)
        refusal = verdict(payload) if isinstance(payload, dict) else None
    except Exception:
        return 0
    if refusal is None:
        return 0
    print(refusal, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
