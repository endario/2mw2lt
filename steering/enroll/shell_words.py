"""How the plugin's shell guards read a command: its segments, its words and its variables, as far as
they can be known without running the shell (#3909, #4551). `delete_guard.py` and `trap_guard.py`
both read a command through here."""
from __future__ import annotations

import os
import re
import shlex
from pathlib import Path

VARIABLE = re.compile(r"\$(?:\{([A-Za-z_][A-Za-z0-9_]*)(?::?-([^}]*))?\}|([A-Za-z_][A-Za-z0-9_]*))")
ASSIGNMENT = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$", re.S)
SHELLS = {"sh", "bash", "zsh", "dash", "ksh"}


class Unresolvable(Exception):
    pass


def segments(command: str) -> list[list[str]]:
    lexer = shlex.shlex(command.replace("\n", " ; "), posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    segments, current, redirected = [], [], False
    for token in lexer:
        if redirected:
            redirected = False
        elif token and set(token) <= set(";&|()"):
            if current:
                segments.append(current)
            current = []
        elif token and set(token) <= set("<>&") and set(token) & set("<>"):
            # A redirection's file is not a delete target, nor is the descriptor before it.
            redirected = True
            if current and current[-1].isdigit():
                current.pop()
        else:
            current.append(token)
    if current:
        segments.append(current)
    return segments


def expand(word: str, env: dict[str, str | None]) -> str:
    if "$(" in word or "`" in word or "$((" in word:
        raise Unresolvable(word)

    def value(match: re.Match) -> str:
        name = match.group(1) or match.group(3)
        got = env.get(name, "")
        if got is None:
            raise Unresolvable(word)
        if not got and match.group(2) is not None:
            return match.group(2)
        return got

    expanded = VARIABLE.sub(value, word)
    if "$" in expanded.replace("$$", ""):
        raise Unresolvable(word)
    if expanded == "~" or expanded.startswith("~/"):
        expanded = str(Path.home()) + expanded[1:]
    return expanded


def assigned(value: str, env: dict[str, str | None]) -> str | None:
    """A variable's new value, or None when only running the shell would tell: a delete that
    later names it is unresolvable, and nothing else is judged by it."""
    try:
        return expand(value, env)
    except Unresolvable:
        return None


def command_name(word: str, env: dict[str, str | None]) -> str:
    try:
        return os.path.basename(expand(word, env))
    except Unresolvable:
        return os.path.basename(word)


def shell_script(args: list[str]) -> str | None:
    """The script of `sh -c script`, or None."""
    for i, arg in enumerate(args):
        if not arg.startswith("-") or arg.startswith("--"):
            return None
        if "c" in arg[1:]:
            return args[i + 1] if i + 1 < len(args) else None
    return None


HEREDOC = re.compile(r"<<(-?)\s*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\2")


def heredocs(command: str) -> tuple[str, list[str]]:
    """The command with each here-document's body taken out, and the bodies a shell or ssh reads as
    its script. A body handed to `git commit -F -` or a pull request's `--body-file -` is words, and a
    guard that read them as commands would refuse a message that describes it."""
    lines, out, scripts, i = command.split("\n"), [], [], 0
    while i < len(lines):
        line = lines[i]
        out.append(line)
        i += 1
        for m in HEREDOC.finditer(line):
            dash, _, word = m.groups()
            body = []
            while i < len(lines):
                text = lines[i]
                i += 1
                if (text.lstrip("\t") if dash else text) == word:
                    break
                body.append(text)
            # Whatever reads the body on this line, piped or not: a shell or ssh anywhere on it runs it.
            if any(os.path.basename(w) in SHELLS | {"ssh", "eval"} for w in re.findall(r"[^\s;&|()<>]+", line)):
                scripts.append("\n".join(body))
    return "\n".join(out), scripts
