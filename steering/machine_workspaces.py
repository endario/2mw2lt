"""The workspaces this machine's agent serves (doc 130 §2): one owner-only JSON file per workspace
under `~/.config/2mw2lt/workspaces/`, named for its authority, written by an install or a
migration and read by the agent while it runs."""
from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import spool


@dataclass(frozen=True)
class Entry:
    root: Path
    door: str
    capability_file: Path | None = None
    port: int | None = None
    gh_account: str | None = None


def directory(home: Path) -> Path:
    return home / ".config" / "2mw2lt" / "workspaces"


def parse(text: str) -> Entry:
    o = json.loads(text)
    if not isinstance(o, dict):
        raise ValueError("an entry is a JSON object")
    root, door = o.get("root"), o.get("door")
    if not isinstance(root, str) or not Path(root).is_absolute():
        raise ValueError("root is an absolute path")
    if not isinstance(door, str) or not door.startswith(("https://", "http://")):
        raise ValueError("door is a URL")
    cap, port = o.get("capability_file"), o.get("port")
    if cap is not None and (not isinstance(cap, str) or not Path(cap).is_absolute()):
        raise ValueError("capability_file is an absolute path or null")
    if port is not None and (not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535):
        raise ValueError("port is a TCP port or null")
    name = o.get("gh_account")
    if name is not None and (not isinstance(name, str) or not name.strip()):
        raise ValueError("gh_account is a nonempty login or null")
    return Entry(Path(root), door.rstrip("/"), Path(cap) if cap else None, port, name)


def entries(where: Path, log: Callable[[str], None] = print) -> dict[Path, Entry]:
    """Every entry that parses, by root. A file that does not, or one naming a root or a door
    another file names, is said and left out, and the others are served regardless. A file that
    cannot be read raises, since that says nothing about whether it is still wanted."""
    found: dict[str, Entry] = {}
    # A directory that cannot be listed, or is gone, raises: that is not a registry naming nothing,
    # and the agent keeps what it serves (a registry never yet written serves nothing either way).
    files = sorted(f for f in where.iterdir() if f.suffix == ".json")
    for f in files:
        text = f.read_text()
        try:
            found[f.name] = parse(text)
        except ValueError as e:
            log(f"workspaces: {f.name} not served: {e}")
    out: dict[Path, Entry] = {}
    for name, e in found.items():
        clash = [n for n, o in found.items() if n != name and (o.root == e.root or o.door == e.door)]
        if clash:
            log(f"workspaces: {name} not served: {', '.join(clash)} names the same root or door")
            continue
        out[e.root] = e
    return out


def write(where: Path, authority: str, entry: Entry) -> Path:
    """The entry for `authority`, replaced whole and owner-only."""
    where.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = where / f"{authority}.json"
    spool.write_atomic(path, json.dumps({
        "root": str(entry.root), "door": entry.door,
        "capability_file": str(entry.capability_file) if entry.capability_file else None,
        "port": entry.port, "gh_account": entry.gh_account}, indent=1) + "\n", mode=0o600)
    return path


def at(root: Path) -> Entry | None:
    """The registered checkout, including a cwd in one of its worktrees."""
    from local_workspace import common_root
    where = Path(os.environ.get("STEERING_AGENT_REGISTRY") or directory(Path.home()))
    if not where.exists():
        return None
    root = root.resolve()
    try:
        root = common_root(root, timeout=5).resolve()
    except (OSError, subprocess.SubprocessError):
        pass
    return entries(where).get(root)


def migrate_account(where: Path, name: str | None) -> None:
    """Backfill legacy entries before removing a job-wide pin; explicit entries stay as written."""
    if not name or not where.exists():
        return
    for path in sorted(where.glob("*.json")):
        text = path.read_text()
        try:
            parse(text)
            doc = json.loads(text)
        except ValueError as e:
            print(f"workspaces: {path.name} not migrated: {e}")
            continue
        if "gh_account" not in doc:
            spool.write_atomic(path, json.dumps({**doc, "gh_account": name}, indent=1) + "\n", mode=0o600)
