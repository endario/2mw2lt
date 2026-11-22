#!/usr/bin/env python3
"""The hook's stable entry point — the one path a workspace's settings name (#406). A session's
own skill-driven commands share the same problem the hook was written to solve — a plugin path
frozen where it is invoked, moving under it while a long session runs (#1875) — so this is also
the stable entry point they exec through:

    "${STEERING_PYTHON:-python3}" <workspace>/.claude/steering-launch.py observe|readout        (a hook, reads stdin)
    "${STEERING_PYTHON:-python3}" <workspace>/.claude/steering-launch.py exec hold|forge|checkpoint [args...]    (a direct run)

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
         "rebrief": "steering/enroll/rebrief.py", "forge": "steering/enroll/forge.py",
         "decide": "steering/enroll/decide.py", "checkpoint": "steering/enroll/checkpoint.py",
         "verb": "steering/enroll/verb.py"}
# Names `exec` may run directly. Every hook entry point reads a hook's stdin payload and
# always exits 0 (`main`'s contract, below); running one this way would hang reading a stdin
# nobody feeds it, or swallow a failure a direct run must surface. `hold` reads no stdin and
# exits meaningfully on its own, so it is safe to run either way; `forge` is a direct run only, the
# stable path a Codex session's one prefix rule names (#7940); `checkpoint` likewise — the stable
# path a harness with no `CLAUDE_PLUGIN_ROOT` saves by, which a Codex worker's own rule grants
# so no dialog it cannot answer stands between (#8571). `verb` is a direct run too — the
# stable path a Stop gate's runnable line spells (doc 203 D9), which a session that owes a word
# can run without hunting for the plugin's cache.
EXEC = frozenset({"hold", "forge", "checkpoint", "verb"})
# A session's own acts a hook event must never fire: `main`'s hook path refuses them.
DIRECT = frozenset({"forge", "checkpoint"})
# The pack this file was installed from, written in by `hooks.py install`. A workspace wired
# from a checkout has no plugin cache to search, and one whose account cannot be resolved has
# no record to read; either way the pack that installed the launcher is still there.
SOURCE = ""
# The variable the agent exports to every session it launches, naming its bundled interpreter
# (#10540): an inherited variable survives the login shell whose /etc/zprofile path_helper puts
# /usr/bin's python3 ahead of an inherited PATH, which is why it is carried by name and not on
# PATH. The command forms resolve `${STEERING_PYTHON:-python3}`; this file resolves the same.
ENV_PYTHON = "STEERING_PYTHON"


def _steered() -> str | None:
    """The interpreter `STEERING_PYTHON` names when it is executable; a variable set but broken is
    treated as absent, which is what the command forms' `:-python3` fallback says."""
    target = os.environ.get(ENV_PYTHON)
    return target if target and os.access(target, os.X_OK) else None


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
    """The install path the harness records for this plugin, when it is there and complete.
    A user-scope and a project-scope record can name different versions, and one lags the
    other for hours (#5619), so the newest complete record wins rather than the first."""
    try:
        doc = json.loads((config / "plugins" / "installed_plugins.json").read_text())
    except (OSError, ValueError):
        return None
    found = [Path(p) for entry in (doc.get("plugins") or {}).get(PLUGIN) or []
             if isinstance(p := entry.get("installPath"), str) and p
             and Path(p).is_dir() and complete(Path(p))]
    return max(found, key=lambda d: _key(d.name)) if found else None


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
    names = "|".join(e for e in ENTRY if e not in DIRECT)
    if topic == "exec":
        lines = [f"usage: {invocation} exec <{'|'.join(sorted(EXEC))}> [args...]",
                 f"example: {invocation} exec hold --current"]
    elif topic in ENTRY:
        # A direct-run verb runs only through exec; its help names the form that runs, never the
        # bare form main refuses as a hook.
        how = f"exec {topic} [args...]" if topic in DIRECT else topic
        lines = [f"usage: {invocation} {how}", f"example: {invocation} {how}"]
    else:
        lines = [f"usage: {invocation} <{names}>", f"example: {invocation} observe",
                 f"usage: {invocation} exec <{'|'.join(sorted(EXEC))}> [args...]",
                 f"example: {invocation} exec hold --current"]
    lines.append("install  Run /2mw2lt:install to install the current 2mw2lt pack.")
    return "\n".join(lines)


def error(reason: str) -> int:
    config = config_dir_of(None) or Path(os.environ.get("HOME", "~")).expanduser() / ".claude"
    print(f"error: {reason}", file=sys.stderr)
    print(help_text(config, '"${STEERING_PYTHON:-python3}" ' + shlex.quote(str(Path(sys.argv[0]).resolve()))), file=sys.stderr)
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


def missing(name: str, config: Path) -> str:
    return (f"steering-launch: no 2mw2lt plugin under {config} carries {name}; "
            f"install the 2mw2lt plugin (claude plugin install {PLUGIN})")


def plugin_gates_rearm(root: str | None) -> bool:
    """Whether the plugin at `root` (the one this session loaded) runs the re-arm from its own
    Stop entry (doc 203 D10), beside which a pack entry would only duplicate it."""
    if not root:
        return False
    try:
        gates = json.loads((Path(root) / "hooks" / "hooks.json").read_text())
        stops = gates.get("hooks", {}).get("Stop", [])
        return any("armed.py" in h.get("command", "") for g in stops if isinstance(g, dict)
                   for h in g.get("hooks", []) if isinstance(h, dict))
    except (OSError, ValueError):
        return False


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
        print(missing(name, config), file=sys.stderr)
        return 1
    python = _steered() or sys.executable
    os.execv(python, [python, str(script), *rest])


def main() -> int:
    # python_floor.NEEDS, inlined: .claude holds this file alone, so it imports nothing. Run under
    # an under-floor python3 — macOS's 3.9 is the one that reaches it — the interpreter the export
    # names runs this file again instead; it survives the login shell that put python3 first
    # (#10540). A variable naming this same interpreter ends there rather than looping.
    if tuple(sys.version_info[:2]) < (3, 12):
        steered = _steered()
        if steered and os.path.realpath(steered) != os.path.realpath(sys.executable or ""):
            try:
                os.execv(steered, [steered, os.path.abspath(__file__), *sys.argv[1:]])
            except OSError:
                pass
    argv = sys.argv[1:]
    config = config_dir_of(None) or Path(os.environ.get("HOME", "~")).expanduser() / ".claude"
    topic_help = (len(argv) == 2 and argv[1] in ("-h", "--help") and
                  argv[0] in (*ENTRY, "exec"))
    if not argv or argv in (["-h"], ["--help"]) or topic_help:
        print(help_text(config, '"${STEERING_PYTHON:-python3}" ' + shlex.quote(str(Path(sys.argv[0]).resolve())), argv[0] if topic_help else None))
        return 0
    if argv[:1] == ["exec"]:
        return main_exec(argv[1:])
    if argv[0] not in ENTRY or argv[0] in DIRECT:
        return error(f"unknown command {argv[0]!r}")
    # The plugin's own Stop entry owns the re-arm wherever the plugin loaded in this session
    # carries it (doc 203 D10): a pack entry a workspace wired before it would run armed.py a
    # second time on one stop. Read from the plugin root this session actually loaded, not the
    # newest cache — a session on an older plugin has no entry to fire and keeps this one.
    if argv[0] == "armed" and plugin_gates_rearm(os.environ.get("CLAUDE_PLUGIN_ROOT")):
        return 0
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
        print(missing(sys.argv[1], config), file=sys.stderr)
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
    python = _steered() or sys.executable
    os.execv(python, [python, str(script), *sys.argv[2:]])


if __name__ == "__main__":
    raise SystemExit(main())
