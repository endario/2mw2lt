"""The workspace: one directory per repository, shared by every worktree of it."""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import spool


def workspace_root(anchor: Path | None = None, timeout: float | None = None) -> Path:
    """The parent of the Git common directory reached from `anchor` (default: this file's
    directory). `STEERING_WORKSPACE` overrides it so a guard can point a real daemon at a
    scratch workspace."""
    override = os.environ.get("STEERING_WORKSPACE")
    if override:
        return Path(override)
    return common_root(anchor if anchor is not None else Path(__file__).resolve().parent, timeout)


def required_workspace_root(anchor: Path, timeout: float | None = None,
                            configured: bool = True) -> Path:
    """The workspace an operator command must run inside.

    An installer validates its named target rather than the daemon workspace a test or local
    deployment selected through `STEERING_WORKSPACE`.
    """
    try:
        return workspace_root(anchor, timeout) if configured else common_root(anchor, timeout)
    except subprocess.CalledProcessError as error:
        if "not a git repository" not in error.stderr:
            raise
        raise SystemExit(
            "this is not a workspace; run it from one that `/2mw2lt:install` has wired"
        ) from None
    except subprocess.TimeoutExpired as expired:
        # A loaded machine, named as one rather than left as a traceback (#3064).
        raise SystemExit(f"the workspace lookup from {anchor} took longer than {expired.timeout:g}s; "
                         "the machine is busy, so run it again") from None


def common_root(anchor: Path, timeout: float | None = None) -> Path:
    """The parent of the Git common directory: one root for a repository and all its worktrees.

    Read from the files git itself reads where their shape is the ordinary one. A `git` process
    under a budget waited seconds to be scheduled on a loaded machine, and each budget was a
    cliff: a hook dropped its observation and the Stop hook stopped asking for a hold (#3064)."""
    common = _common_dir(anchor, timeout)
    if common is not None:
        return common.parent
    out = subprocess.run(["git", "-C", str(anchor), "rev-parse", "--path-format=absolute", "--git-common-dir"],
                         capture_output=True, text=True, check=True, timeout=timeout)
    return Path(out.stdout.strip()).parent


# Each changes where git looks or what it accepts, so a lookup that honours none is asked of git.
_GIT_ENV = ("GIT_DIR", "GIT_COMMON_DIR", "GIT_WORK_TREE", "GIT_CEILING_DIRECTORIES",
            "GIT_DISCOVERY_ACROSS_FILESYSTEM")


def _is_gitdir(d: Path) -> bool:
    """HEAD, objects and refs, with a HEAD git would read: a symbolic ref or an object name."""
    try:
        head = _pointer((d / "HEAD").read_bytes().decode())
    except OSError:
        return False
    if head is None:
        return False
    named = head.startswith("ref: refs/") or (len(head) in (40, 64) and all(c in "0123456789abcdef" for c in head))
    return named and (d / "objects").is_dir() and (d / "refs").is_dir()


def _pointer(text: str) -> str | None:
    """A pointer file's content when the file is that and one newline, else None: git reads
    padding otherwise than stripping it would, so anything else is left to git."""
    path = text[:-1] if text.endswith("\n") else text
    return path if path and path == path.strip() and "\n" not in path else None


def _mine(p: Path) -> bool:
    return p.stat().st_uid == os.geteuid()


def _read_common_dir(anchor: Path) -> Path | None:
    if any(name in os.environ for name in _GIT_ENV):
        return None
    here = anchor.resolve()
    if not here.is_dir():
        return None   # git -C refuses it, and so must the lookup
    device = here.stat().st_dev
    for d in (here, *here.parents):
        if d.stat().st_dev != device:
            return None   # git stops at a filesystem boundary
        dotgit = d / ".git"
        if dotgit.is_symlink():
            return None
        if dotgit.is_dir():
            gitdir = dotgit
        elif dotgit.is_file():
            line = dotgit.read_bytes().decode()
            target = _pointer(line[len("gitdir: "):]) if line.startswith("gitdir: ") else None
            if target is None or not _mine(dotgit):
                return None
            gitdir = (d / target).resolve()
        elif (d / "HEAD").exists():
            return None   # a bare repository, or inside a git directory by another road
        else:
            continue
        if not (_mine(d) and _mine(gitdir)):
            return None   # git's safe.directory decides a checkout another user owns
        pointer = gitdir / "commondir"
        if pointer.is_file():
            if not (gitdir / "HEAD").is_file():
                return None
            target = _pointer(pointer.read_bytes().decode())
            if target is None:
                return None
            common = (gitdir / target).resolve()
        else:
            common = gitdir
        return common if _is_gitdir(common) else None
    return None


def _common_dir(anchor: Path, timeout: float | None = None) -> Path | None:
    """The common directory read from `.git`, a worktree's pointers and HEAD on the way up from
    `anchor`, or None where `common_root` asks git instead: an anchor that is not a directory, a
    symlinked or foreign-owned `.git`, a padded pointer, a bare repository, a filesystem boundary,
    a HEAD git would not read, or an environment that redirects git. The reads run on a thread
    held to `timeout`, so a stalled mount is refused as a slow `git` would be."""
    def read():
        try:
            return _read_common_dir(anchor)
        except (OSError, ValueError, RuntimeError):
            return None
    if timeout is None:
        return read()
    import threading
    got: list = []
    # A daemon thread, which a stalled read cannot keep the process alive for.
    t = threading.Thread(target=lambda: got.append(read()), daemon=True)
    t.start()
    t.join(timeout)
    if not got:
        raise subprocess.TimeoutExpired(["read", str(anchor)], timeout)
    return got[0]


def origin_slug(anchor: Path, timeout: float | None = None) -> str | None:
    """`owner/name` from this checkout's `origin`, or None when it has no usable one.

    The one fact about a checkout that is the same on every machine holding it. A path is
    not: the same repository is cloned to a different directory, under a different name,
    on the next machine.
    """
    out = subprocess.run(["git", "-C", str(anchor), "remote", "get-url", "origin"],
                         capture_output=True, text=True, timeout=timeout)
    tail = out.stdout.strip().removesuffix(".git").replace(":", "/").rsplit("/", 2)[-2:]
    return "/".join(tail) if len(tail) == 2 and all(tail) else None


DEFAULT_AGENT_PORT = "9990"
# How long a server keeps an idle connection: longer than anything in front of it keeps one to
# reuse (Caddy, 2 minutes), so the client always gives a connection up first. At uvicorn's 5
# seconds a request sent as the server closed its connection was reset (#3037).
KEEP_ALIVE_SECONDS = 180
KEY_HEADER = "X-Steering-Agent-Key"
# Which workspace a local request to the agent is for, by its common root (doc 130 §4).
WORKSPACE_HEADER = "X-Steering-Workspace"


def workspace_header(root) -> dict[str, str]:
    """`WORKSPACE_HEADER` naming `root`, its filesystem bytes percent-encoded: a header carries
    Latin-1 at most, and a path is whatever bytes the filesystem allows."""
    from urllib.parse import quote_from_bytes
    return {WORKSPACE_HEADER: quote_from_bytes(os.fsencode(root), safe="/")}


def header_workspace(value: str) -> str:
    """The root a `workspace_header` names."""
    from urllib.parse import unquote_to_bytes
    return os.fsdecode(unquote_to_bytes(value))


def _key_name(port) -> str:
    try:
        return str(int(str(port).strip()))   # the port the agent binds, however it was written
    except ValueError:
        return str(port)


def key_file(port) -> Path:
    """Where the agent on `port` keeps the key its worker routes admit (#1306): the config root
    is `XDG_CONFIG_HOME` when that names an absolute directory, else the owner's home, whose
    `.config` a confined worker's profile refuses to read (doc 84 §4). Where the file actually
    is, `agent_key_file` reads from the agent's record — the agent's environment and a session's
    need not agree (#2667)."""
    xdg = os.environ.get("XDG_CONFIG_HOME")
    config = Path(xdg) if xdg and os.path.isabs(xdg) else Path.home() / ".config"
    return config / "2mw2lt" / "agents" / f"{_key_name(port)}.key"


def legacy_key_file(port) -> Path:
    """Where an agent older than the record wrote its key: the owner's `.config`, whatever the
    reader's `XDG_CONFIG_HOME` names, as `key_file` resolved before it honoured it (#2667). A
    session that finds no record is reading for such an agent, so this is its fallback."""
    return Path.home() / ".config" / "2mw2lt" / "agents" / f"{_key_name(port)}.key"


def agent_key(anchor, port) -> str | None:
    try:
        return agent_key_file(anchor, port).read_text().strip() or None
    except OSError:
        return None


def agent_port(anchor: Path, timeout: float | None = None) -> str:
    """The port the agent serving this workspace records, before an environment override.

    The record is per workspace because the port is: a machine running an agent for each of
    two workspaces has one of them on something other than the default, and a session that
    reads the default reaches the other workspace's agent (#1073).
    """
    named = os.environ.get("STEERING_AGENT_PORT")
    if named:
        return named
    try:
        lines = (common_root(anchor, timeout or 5) / ".env").read_text().splitlines()
    except OSError:
        return DEFAULT_AGENT_PORT
    return next((line.strip().split("=", 1)[1] for line in lines
                 if line.strip().startswith("STEERING_AGENT_PORT=")), DEFAULT_AGENT_PORT)


def _record_env_line(anchor: Path, name: str, value: str, timeout: float | None = None) -> Path:
    root = common_root(anchor, timeout or 5)
    env = root / ".env"
    lines = env.read_text().splitlines() if env.exists() else []
    lines = [line for line in lines if not line.strip().startswith(f"{name}=")]
    lines.append(f"{name}={value}")
    spool.write_atomic(env, "\n".join(lines) + "\n", mode=0o600)
    return env


def _port_key(port) -> str:
    """The port as the key file's name spells it, so `09991` matches `9991`."""
    try:
        return str(int(str(port).strip()))
    except ValueError:
        return str(port).strip()


def _key_file_without_record(port) -> Path:
    """Where a record-less agent's key is: its own resolution's file when something wrote it —
    an agent of this change that named no workspace, or one a session reaches on a port the
    record was not written for — else the legacy path, where agents before the record did."""
    xdg = key_file(port)
    return xdg if xdg.exists() else legacy_key_file(port)


def agent_key_file(anchor: Path, port) -> Path:
    """The key file the agent serving `anchor` on `port` says it wrote, from the workspace record
    beside the port; `_key_file_without_record` otherwise — no record, as for an agent older than
    it or one that named no workspace, or a port the record was not written for, as when
    `STEERING_AGENT_PORT` points a session at a legacy agent still serving the workspace (#2667).
    Not the session's own `key_file` alone: that follows the session's `XDG_CONFIG_HOME`, where
    an agent of another environment wrote nothing (round-4 review)."""
    try:
        lines = (common_root(anchor, timeout=5) / ".env").read_text().splitlines()
    except Exception:
        return _key_file_without_record(port)
    named = next((line.strip().split("=", 1)[1] for line in lines
                  if line.strip().startswith("STEERING_AGENT_KEY_FILE=")), "")
    served = next((line.strip().split("=", 1)[1] for line in lines
                   if line.strip().startswith("STEERING_AGENT_PORT=")), "")
    if named and served and _port_key(served) == _port_key(port):
        return Path(named)
    return _key_file_without_record(port)


def write_agent_port(anchor: Path, port: str, timeout: float | None = None) -> Path:
    return _record_env_line(anchor, "STEERING_AGENT_PORT", port, timeout)


def write_agent_key_file(anchor: Path, key_path: Path, timeout: float | None = None) -> Path:
    """Leave where the agent wrote its key, beside the port record (#2667)."""
    return _record_env_line(anchor, "STEERING_AGENT_KEY_FILE", str(key_path), timeout)
