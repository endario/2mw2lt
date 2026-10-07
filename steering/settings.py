"""Capacity tunables read at the point of use from a machine-local file (#2394), so changing one
takes effect at the next gate offer, not the next agent restart. The environment stays the
fallback: an unset file changes nothing. A malformed file is refused loudly and the last good
value is kept — `host_limit` alone falls to a literal default rather than its own env var on a
fresh process's first-ever malformed read, so a staggered restart can't reopen the disagreement
this file exists to close (design.md §3, round 3 critic finding).
"""
from __future__ import annotations

import os
import sys
import tomllib
from pathlib import Path

import agentjob

# `STEERING_AGENT_SETTINGS` moves it for a test: a guard reading the machine's own file saw that
# machine's capacity rather than the defaults it asserts (#2667).
DEFAULT_PATH = Path(os.environ.get("STEERING_AGENT_SETTINGS") or agentjob.tunables(Path.home()))
KEYS = {"vendor_limit", "agent_limit", "host_limit", "max_workers"}


def _label() -> str:
    return os.environ.get("STEERING_AGENT_LABEL", "agent")


class _Cache:
    def __init__(self) -> None:
        self.key: tuple[str, int, int] | None = None
        self.table: dict = {}
        self.error: str | None = None
        self.parsed: set[str] = set()


_cache = _Cache()


def validate(data) -> dict:
    if not isinstance(data, dict):
        raise ValueError("is not a table")
    extra = set(data) - {"schema", "capacity", "placement", "routing"}
    if extra:
        raise ValueError(f"has an unknown top-level key ({', '.join(sorted(extra))})")
    if "schema" in data and data["schema"] != 1:
        raise ValueError(f"schema {data['schema']!r} is not 1")
    table = data.get("capacity", {})
    if not isinstance(table, dict):
        raise ValueError("[capacity] is not a table")
    extra = set(table) - KEYS
    if extra:
        raise ValueError(f"[capacity] has an unknown key ({', '.join(sorted(extra))})")
    # Zero is a limit, not a malformed one: it drains the work that limit bounds. Refusing it threw
    # the whole file out for the defaults, so a machine set to 0 everywhere kept taking reviews.
    for key, value in table.items():
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise ValueError(f"[capacity].{key}={value!r} is not a whole number of at least 0")
    placing = data.get("placement", {})
    if not isinstance(placing, dict):
        raise ValueError("[placement] is not a table")
    extra = set(placing) - {"role", "soft"}
    if extra:
        raise ValueError(f"[placement] has an unknown key ({', '.join(sorted(extra))})")
    if "role" in placing and placing["role"] not in ("dedicated", "workstation"):
        raise ValueError(f"[placement].role={placing['role']!r} is not dedicated or workstation")
    soft = placing.get("soft", 0.75)
    if not isinstance(soft, (int, float)) or isinstance(soft, bool) or not 0 < soft <= 1:
        raise ValueError(f"[placement].soft={soft!r} is not in (0, 1]")
    routing = data.get("routing", {})
    if not isinstance(routing, dict):
        raise ValueError("[routing] is not a table")
    extra = set(routing) - {"judges_exclude", "claude_launchers", "worker_forge"}
    if extra:
        raise ValueError(f"[routing] has an unknown key ({', '.join(sorted(extra))})")
    if "judges_exclude" in routing and not (isinstance(routing["judges_exclude"], list)
                                            and all(isinstance(v, str) for v in routing["judges_exclude"])):
        raise ValueError("[routing].judges_exclude is not a list of vendor names")
    # Only the shape: whether each entry names a launcher is `launchers.declared()`'s to judge, so a
    # missing binary refuses the launch it would serve and not the limits beside it.
    if "claude_launchers" in routing and not (isinstance(routing["claude_launchers"], list)
                                              and routing["claude_launchers"]):
        raise ValueError("[routing].claude_launchers is not a non-empty list")
    if routing.get("worker_forge", "login") not in ("login", "app"):
        raise ValueError(f"[routing].worker_forge={routing['worker_forge']!r} is not login or app")
    # Kept beside the limits under keys no `[capacity]` entry can take, so one cache holds them all.
    return {**table, **({"placement": placing} if placing else {}),
            **({"routing": routing} if routing else {})}


def _state(path: Path) -> tuple[dict, str | None]:
    """`([capacity] table, current error)`, mtime-cached by `(path, size, mtime_ns)` — the path
    is part of the key because a test points different fixtures at this one shared cache within a
    single process. `capacity()` exposes the table alone; `gate_host_limit()` also needs the
    error, to tell "file absent, {}, no error" apart from "malformed, {}, error set"."""
    try:
        st = path.stat()
    except OSError:
        # An absent file is not an error to remember, and it says nothing about a *different*
        # path's own cached state — leaving `_cache` untouched here is what lets a later read of
        # that other path still recognize its own last good value as its own (see the
        # `same_path` check below); clobbering it unconditionally on every absent-file read was
        # the bug a code review caught (#2394).
        return {}, None
    key = (str(path), st.st_size, st.st_mtime_ns)
    if _cache.key == key:
        return _cache.table, _cache.error
    try:
        with path.open("rb") as f:
            data = tomllib.load(f)
        table = validate(data)
    except Exception as e:
        err = str(e)
        # A "good value to keep" is only real when the last successful parse this process has
        # cached was for THIS path — reusing whatever the cache holds regardless of path would
        # leak an unrelated path's last-good table into a different path's first-ever failure.
        same_path = _cache.key is not None and _cache.key[0] == key[0]
        table = _cache.table if same_path else {}
        kept = "the last good values" if table else "the environment and hardcoded defaults"
        # Stderr, where an agent's job sends it all the same: `machine_inventory`'s probe reads a
        # child's stdout as the JSON `harness.judges()` answered, and this line broke it.
        print(f"{_label()}: agent.toml {err}; keeping {kept}", file=sys.stderr, flush=True)
        _cache.key, _cache.table, _cache.error = key, table, err
        return table, err
    _cache.key, _cache.table, _cache.error = key, table, None
    _cache.parsed.add(key[0])
    return table, None


def capacity(path: Path = DEFAULT_PATH) -> dict:
    """The raw `[capacity]` table (with `[placement]` and `[routing]` under the keys `placement` and
    `routing` when set), or `{}` when the file is absent or malformed with nothing yet
    cached for it — otherwise the last good table this process parsed for it (the keep-last-good
    discipline this module implements, round 2 review)."""
    return _state(path)[0]


def _positive(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError:
        print(f"{_label()}: {name}={raw!r} is not an integer; using {default}", file=sys.stderr, flush=True)
        return default
    if value < 1:
        print(f"{_label()}: {name}={raw!r} is below 1; using 1", file=sys.stderr, flush=True)
        return 1
    return value


def gate_vendor_limit(path: Path = DEFAULT_PATH) -> int:
    table = capacity(path)
    return table["vendor_limit"] if "vendor_limit" in table else _positive("STEERING_GATE_VENDOR_LIMIT", 2)


def gate_agent_limit(path: Path = DEFAULT_PATH) -> int:
    table = capacity(path)
    return table["agent_limit"] if "agent_limit" in table else _positive("STEERING_GATE_AGENT_LIMIT", 3)


def gate_host_limit(path: Path = DEFAULT_PATH) -> int:
    """Every agent on this machine together, not just this one. A host can run several side by
    side, and a per-agent limit bounds none of them jointly, so each fills its own and they
    oversubscribe one machine's CPU (#1780).
    Defaulting to the per-agent limit rather than a number of its own: a host running one agent
    then behaves exactly as it did, and a host running two is held to what one was already
    allowed instead of twice it."""
    table, error = _state(path)
    if "host_limit" in table:
        return table["host_limit"]
    if error is not None and str(path) not in _cache.parsed:
        return 3
    return _positive("STEERING_GATE_HOST_LIMIT", gate_agent_limit(path))


def max_workers(path: Path = DEFAULT_PATH) -> int:
    table = capacity(path)
    return table["max_workers"] if "max_workers" in table else _positive("STEERING_MAX_WORKERS", 8)


def gate_drain(path: Path = DEFAULT_PATH) -> str | None:
    """Why this machine takes no new gate run, or None: a gate limit at 0. A running one finishes."""
    for key, limit in (("vendor_limit", gate_vendor_limit), ("agent_limit", gate_agent_limit),
                       ("host_limit", gate_host_limit)):
        if limit(path) == 0:
            return f"drained: [capacity].{key} is 0"
    return None


def launch_drain(path: Path = DEFAULT_PATH) -> str | None:
    """Why this machine starts no new worker, or None. A running one is left alone."""
    return "drained: [capacity].max_workers is 0" if max_workers(path) == 0 else None


def placement(path: Path = DEFAULT_PATH, review_host: bool | None = None) -> dict:
    """What this machine tells the orchestrator it is for (doc 146 §2.1): its role, its threshold,
    `host_limit` and `max_workers`. A declared review host is dedicated unless its file says
    otherwise."""
    if review_host is None:
        import review_host as review_host_mod
        review_host = review_host_mod.read() is not None
    table = capacity(path).get("placement", {})
    return {"role": table.get("role", "dedicated" if review_host else "workstation"),
            "soft": float(table.get("soft", 0.75)), "capacity": gate_host_limit(path),
            "workers": max_workers(path)}


def judges_exclude(path: Path = DEFAULT_PATH) -> set[str] | None:
    """The vendors this machine withholds from judging, or None when the file holds no opinion and
    `STEERING_JUDGES_EXCLUDE` decides. An empty list is an opinion: nothing is withheld."""
    routing = capacity(path).get("routing", {})
    if "judges_exclude" not in routing:
        return None
    return {v.strip() for v in routing["judges_exclude"] if v.strip()}


def claude_launchers(path: Path = DEFAULT_PATH) -> list | None:
    """The declared launchers as the file writes them (doc 121 §2), or None when it declares none
    and `STEERING_CLAUDE_LAUNCHERS` decides."""
    return capacity(path).get("routing", {}).get("claude_launchers")


def worker_forge(path: Path = DEFAULT_PATH) -> str:
    """Whose GitHub credential a worker launched here holds (doc 181 §5): `login`, the machine's
    own, until the App's is proven on one worker; `app` for the App's installation token."""
    return capacity(path).get("routing", {}).get("worker_forge", "login")
