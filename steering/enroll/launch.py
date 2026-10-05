#!/usr/bin/env python3
"""The hook's stable entry point — the one path a workspace's settings name (#406). A session's
own skill-driven commands share the same problem the hook was written to solve — a plugin path
frozen where it is invoked, moving under it while a long session runs (#1875) — so this is also
the stable entry point they exec through:

    python3 <workspace>/.claude/steering-launch.py observe|readout        (a hook, reads stdin)
    python3 <workspace>/.claude/steering-launch.py exec hold [args...]    (a direct run)

The account is resolved per invocation from the hook's own payload, or from `CLAUDE_CONFIG_DIR`
for a direct run, because one workspace is worked by several accounts and each keeps its own
plugin cache.

Kept deliberately small: it is the one piece that cannot be updated by updating the plugin, so
its contract is "read the record, hand over" and nothing more.
"""
from __future__ import annotations

import importlib.util
import json
import os
import shlex
import sys
import tempfile
from pathlib import Path

PLUGIN = "2mw2lt@2mw2lt"
ENTRY = {"observe": "steering/enroll/observe.py", "readout": "steering/enroll/readout.py",
         "armed": "steering/enroll/armed.py", "hold": "steering/enroll/hold.py",
         "rebrief": "steering/enroll/rebrief.py"}
# Names `exec` may run directly. Every hook entry point reads a hook's stdin payload and
# always exits 0 (`main`'s contract, below); running one this way would hang reading a stdin
# nobody feeds it, or swallow a failure a direct run must surface. `hold` reads no stdin and
# exits meaningfully on its own, so it is the one entry safe to run either way.
EXEC = frozenset({"hold"})
# The pack this file was installed from, written in by `hooks.py install`. A workspace wired
# from a checkout has no plugin cache to search, and one whose account cannot be resolved has
# no record to read; either way the pack that installed the launcher is still there.
SOURCE = ""


def config_dir_of(transcript: str | None) -> Path | None:
    """The harness config directory a transcript sits under, or None.

    A transcript is `<config>/projects/<slug>/<id>.jsonl`, so the config directory is two
    parents up from its own directory. `CLAUDE_CONFIG_DIR` wins where the harness sets it.
    """
    named = os.environ.get("CLAUDE_CONFIG_DIR")
    if named:
        return Path(named)
    if not transcript:
        return None
    p = Path(transcript)
    for parent in p.parents:
        if parent.name == "projects":
            return parent.parent
    return None


# What a version must carry to be worth running at all. Not `ENTRY`: a pack that gains an
# entry point would make every version cached before it incomplete, so a workspace on one of
# those would fall back past every cached install to whichever directory installed its
# launcher — and once an update replaced that, run nothing, the observations and the readings
# stopping with it and saying nothing. A version missing only a newer entry still serves the
# ones it has, and `main` already returns 0 for a script that is not there.
REQUIRED = ("steering/enroll/observe.py", "steering/enroll/readout.py")


def complete(d: Path) -> bool:
    """Whether a version directory carries the pack's long-standing entry points. An update
    writes the record and unpacks; a directory caught between the two would otherwise be run
    as if it were there."""
    return all((d / e).is_file() for e in REQUIRED)


def recorded(config: Path) -> Path | None:
    """The install path the harness records for this plugin, when it is there and complete."""
    try:
        doc = json.loads((config / "plugins" / "installed_plugins.json").read_text())
    except (OSError, ValueError):
        return None
    for entry in (doc.get("plugins") or {}).get(PLUGIN) or []:
        p = entry.get("installPath")
        if isinstance(p, str) and p and Path(p).is_dir() and complete(Path(p)):
            return Path(p)
    return None


def _key(name: str) -> tuple:
    return tuple(int(part) if part.isdigit() else -1 for part in name.split("."))


def newest(config: Path) -> Path | None:
    """The highest complete version in the cache — the fallback for a record that is missing,
    names a directory that is gone, or names one an update has not finished writing."""
    root = config / "plugins" / "cache" / "2mw2lt" / "2mw2lt"
    try:
        versions = sorted((d for d in root.iterdir() if d.is_dir()), key=lambda d: _key(d.name))
    except OSError:
        return None
    for d in reversed(versions):
        if complete(d):
            return d
    return None


def current_pack(config: Path) -> Path | None:
    """The pack this invocation would use for an entry point."""
    return recorded(config) or newest(config)


def _render_from_pack(pack: Path, invocation: str, topic: str | None = None) -> str | None:
    """Ask the current pack for its static help without making its imports part of this launcher."""
    path = pack / "steering" / "verb_help.py"
    try:
        spec = importlib.util.spec_from_file_location("steering_launch_help", path)
        if spec is None or spec.loader is None:
            return None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module.script_help("launch", invocation=invocation, topic=topic)
    except Exception:
        return None


def help_text(config: Path, invocation: str, topic: str | None = None) -> str:
    """Static direct-run help, even when no updateable pack can be resolved."""
    pack = current_pack(config)
    rendered = _render_from_pack(pack, invocation, topic) if pack is not None else None
    if rendered is not None:
        return rendered
    names = "|".join(ENTRY)
    if topic == "exec":
        lines = [f"usage: {invocation} exec <{'|'.join(sorted(EXEC))}> [args...]",
                 f"example: {invocation} exec hold --current"]
    elif topic in ENTRY:
        lines = [f"usage: {invocation} {topic}", f"example: {invocation} {topic}"]
    else:
        lines = [f"usage: {invocation} <{names}>", f"example: {invocation} observe",
                 f"usage: {invocation} exec <{'|'.join(sorted(EXEC))}> [args...]",
                 f"example: {invocation} exec hold --current"]
    lines.append("install  Run /2mw2lt:install to install the current 2mw2lt pack.")
    return "\n".join(lines)


def error(reason: str) -> int:
    config = config_dir_of(None) or Path(os.environ.get("HOME", "~")).expanduser() / ".claude"
    print(f"error: {reason}", file=sys.stderr)
    print(help_text(config, "python3 " + shlex.quote(str(Path(sys.argv[0]).resolve()))), file=sys.stderr)
    return 2


def script_for(name: str, config: Path) -> Path | None:
    """The file to run for `name`, or None when nothing can serve it.

    The pack the harness resolves wins. A pack entry newer than every cached version resolves
    to a file that is not there — `armed` shipped while the newest cache was 0.5.6 — and a
    workspace on that cache would run the entry never, silently, which is the one outcome a
    hook installed to enforce something cannot have. `SOURCE` is the install this launcher was
    written from, so it carries the entry by construction.
    """
    install = current_pack(config)
    if install is not None:
        cached = install / ENTRY[name]
        if cached.is_file():
            return cached
    if not SOURCE:
        return None
    own = Path(SOURCE) / f"{name}.py"
    return own if own.is_file() else None


def main_exec(argv: list[str]) -> int:
    """`exec <name> [args...]`: run the current install's `<name>.py` directly, argv and exit
    code passed straight through. Not a hook — nothing here is entitled to swallow a failure
    the way `main`'s hook path always exits 0, since there is no turn to keep alive past it."""
    if not argv or argv[0] not in EXEC:
        return error("exec requires a supported command: " + "|".join(sorted(EXEC)))
    name, rest = argv[0], argv[1:]
    config = config_dir_of(None) or Path(os.environ.get("HOME", "~")).expanduser() / ".claude"
    script = script_for(name, config)
    if script is None:
        print(f"no complete 2mw2lt install found to run {name} from; run /2mw2lt:connect", file=sys.stderr)
        return 1
    os.execv(sys.executable, [sys.executable, str(script), *rest])


def main() -> int:
    argv = sys.argv[1:]
    config = config_dir_of(None) or Path(os.environ.get("HOME", "~")).expanduser() / ".claude"
    topic_help = (len(argv) == 2 and argv[1] in ("-h", "--help") and
                  argv[0] in (*ENTRY, "exec"))
    if not argv or argv in (["-h"], ["--help"]) or topic_help:
        print(help_text(config, "python3 " + shlex.quote(str(Path(sys.argv[0]).resolve())), argv[0] if topic_help else None))
        return 0
    if argv[:1] == ["exec"]:
        return main_exec(argv[1:])
    if argv[0] not in ENTRY:
        return error(f"unknown command {argv[0]!r}")
    if len(argv) > 1 and argv[0] not in EXEC:
        return error(f"{argv[0]} does not accept arguments")
    payload = sys.stdin.buffer.read()
    try:
        transcript = json.loads(payload or b"{}").get("transcript_path")
    except ValueError:
        transcript = None
    config = config_dir_of(transcript if isinstance(transcript, str) else None) \
        or Path(os.environ.get("HOME", "~")).expanduser() / ".claude"
    script = script_for(sys.argv[1], config)
    if script is None:
        return 0
    # A file and not a pipe: a payload larger than the pipe buffer would block here forever,
    # and there is no reader until after the exec.
    fd, path = tempfile.mkstemp(prefix="steering-hook-")
    try:
        os.write(fd, payload)
        os.lseek(fd, 0, os.SEEK_SET)
        os.unlink(path)  # the descriptor outlives the name, and the exec inherits it
        os.dup2(fd, 0)
    finally:
        if fd > 2:
            os.close(fd)
    os.execv(sys.executable, [sys.executable, str(script), *sys.argv[2:]])


if __name__ == "__main__":
    raise SystemExit(main())
