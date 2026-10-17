"""The current install, taken at every call (#7185).

A long-lived session's own skill text pins the plugin-cache path it loaded, so a script invoked
from it keeps running the release the session started on however many installs landed since. The
launcher resolves the current install for the hooks and for `exec`; this module is the same
resolution for every script a session runs directly: an entry script invoked from a stale
versioned cache path hands its turn to the same script in the newest complete install, argv,
stdin and exit code kept. Nothing forwards from a checkout or the launcher's own resolved path,
and only a strictly newer version is taken, so no two installs hand the turn to each other.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

_PLUGIN = "2mw2lt@2mw2lt"
_REQUIRED = ("steering/enroll/observe.py", "steering/enroll/readout.py")

# How each harness refreshes this plugin from its marketplace (#1795): the action a session is
# told when the release outruns the pack it runs.
UPDATE = {"claude": "claude plugin update 2mw2lt@2mw2lt", "codex": "codex plugin add 2mw2lt@2mw2lt"}


def _key(name: str) -> tuple:
    return tuple(int(part) if part.isdigit() else -1 for part in name.split("."))


def _config_dir() -> Path:
    if named := os.environ.get("CLAUDE_CONFIG_DIR"):
        return Path(named)
    return Path(os.environ.get("HOME", "~")).expanduser() / ".claude"


def _complete(pack: Path) -> bool:
    return all((pack / e).is_file() for e in _REQUIRED)


def _current(config: Path) -> Path | None:
    """The launcher's own rule (launch.py): the newest complete install the harness records, else
    the highest complete version in the cache."""
    found: list[Path] = []
    try:
        doc = json.loads((config / "plugins" / "installed_plugins.json").read_text())
    except (OSError, ValueError):
        doc = {}
    for entry in (doc.get("plugins") or {}).get(_PLUGIN) or []:
        if isinstance(p := entry.get("installPath"), str) and p:
            found.append(Path(p))
    found = [p for p in found if p.is_dir() and _complete(p)]
    if found:
        return max(found, key=lambda d: _key(d.name))
    root = config / "plugins" / "cache" / "2mw2lt" / "2mw2lt"
    try:
        versions = sorted((d for d in root.iterdir() if d.is_dir()), key=lambda d: _key(d.name))
    except OSError:
        return None
    for d in reversed(versions):
        if _complete(d):
            return d
    return None


def _cache_pack(path: Path) -> Path | None:
    """The versioned cache pack a running script sits in — plugins/cache/2mw2lt/2mw2lt/<version> —
    or None: a checkout, a workspace launcher's resolved pack without a version name, or anything
    else forwards nothing."""
    for pack in (path, *path.parents):
        rungs = [rung.name for rung in list(pack.parents)[:3]]
        if rungs == ["2mw2lt", "2mw2lt", "cache"]:
            return pack
    return None


def take() -> None:
    """Hand the running entry script to the current install, when that is a newer versioned cache
    pack holding the same script. Called at door's import, before any script's own work."""
    if os.environ.get("STEERING_PLUGIN_PIN"):
        return  # a run that says which install it means keeps it
    try:
        mine = Path(sys.modules["__main__"].__file__).resolve()
    except (AttributeError, TypeError, OSError):
        return
    pack = _cache_pack(mine)
    if pack is None:
        return
    current = _current(_config_dir())
    if current is None or not current.is_dir() or current == pack:
        return
    if not _cache_pack(current) or _key(current.name) <= _key(pack.name) or not _complete(current):
        return
    same = current / mine.relative_to(pack)
    if not same.is_file():
        return
    # What this process was invoked from survives the exec: the harness pinned the pack it loaded,
    # so the pack a script was invoked from is the session's own plugin version, whatever install
    # the scripts run on after this (#7185).
    os.environ["STEERING_PLUGIN_FROM"] = pack.name
    os.execv(sys.executable, [sys.executable, str(same), *sys.argv[1:]])


def invoked() -> str | None:
    """The version of the plugin pack this process was invoked from: the pack the harness pinned
    when it loaded the plugin, which a long-lived session keeps after a release lands (#7185).
    take() names it when it forwards; an unforwarded run sits in the pack still. None outside a
    versioned cache pack — a checkout, or a script the launcher resolved."""
    if named := os.environ.get("STEERING_PLUGIN_FROM"):
        return named
    try:
        mine = Path(sys.modules["__main__"].__file__).resolve()
    except (AttributeError, TypeError, OSError):
        return None
    pack = _cache_pack(mine)
    return pack.name if pack is not None else None


def newer(release: str, mine: str) -> bool:
    """Whether release names a version newer than mine (#7185); False when either names none —
    a tell against an unreadable release states no version worth acting on."""
    def keyed(v: str) -> tuple[int, ...] | None:
        parts = v.split(".")
        return tuple(int(p) for p in parts) if parts and all(p.isdigit() for p in parts) else None
    a, b = keyed(release), keyed(mine)
    return a is not None and b is not None and a > b
