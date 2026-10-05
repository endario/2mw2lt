"""The targeting record (doc 113): a wake's pid, VS Code instance and entrypoint, captured
while a session is alive and handed to whoever will need them before a wake is ever attempted.

Distinct from vitals (doc 30): vitals states what a harness says about itself for the board to
draw; this states what a typed wake needs to find the right window, and it is read by exactly
one thing — `waking.wake_session()`, after `pin_for()` resolves the session/epoch it is stored
under — never projected for display.

A session in a tmux pane, of any harness, is a second route (doc 118 §3.1): `connect.py` hands over
the pane it runs in, and the wake types into that pane instead of a tab.
"""
from __future__ import annotations

import re

import machine_harness as harness
import records

V = 1
SOURCE = "claude"
TAB_ENTRYPOINT = "claude-vscode"
TMUX_ENTRYPOINT = "tmux"

_FIELDS = {"v", "source", "provider_session", "runtime_id", "observed_at",
           "pid", "user_data_dir", "app_pid", "entrypoint", "cwd", "tmux_socket", "tmux_pane"}
_REQUIRED = {"v", "source", "provider_session", "runtime_id", "observed_at", "entrypoint"}
_TMUX = ("tmux_socket", "tmux_pane")
_INSTANCE = ("user_data_dir", "app_pid")
_PANE = re.compile(r"%[0-9]+")


def build(*, provider_session: str, runtime_id: str, observed_at: str, entrypoint: str,
         pid: int | None = None, instance: tuple[str, int] | None = None,
         cwd: str | None = None, tmux: tuple[str, str] | None = None,
         source: str = SOURCE) -> dict:
    """The record's field shape, assembled from already-known values — the one place a
    targeting record is built, whether captured by the Stop hook (`read`, below), handed over by
    `connect.py` from the pane it runs in, or self-backfilled from a wake's own local discovery
    (`wakeexec._backfill`)."""
    out = {"v": V, "source": source, "provider_session": provider_session,
           "runtime_id": runtime_id, "observed_at": observed_at, "entrypoint": entrypoint}
    if cwd:
        out["cwd"] = cwd
    if pid is not None:
        out["pid"] = pid
    if instance is not None:
        out["user_data_dir"], out["app_pid"] = instance
    if tmux is not None:
        out["tmux_socket"], out["tmux_pane"] = tmux
    return out


def read(hook: dict, entry: dict, rid: str | None, observed_at: str, *,
        pid: int | None, instance: tuple[str, int] | None) -> dict | None:
    """One targeting record, or None when this session is not a route this design addresses
    (not `claude-vscode`) or carries no session id to key the record on."""
    sid = hook.get("session_id")
    entrypoint = entry.get("entrypoint")
    if not isinstance(sid, str) or not sid or entrypoint != TAB_ENTRYPOINT or not rid:
        return None
    cwd = entry.get("cwd")
    return build(provider_session=sid, runtime_id=rid, observed_at=observed_at,
                entrypoint=entrypoint, pid=pid, instance=instance,
                cwd=cwd if isinstance(cwd, str) and cwd else None)


def complete(rec: dict) -> bool:
    """Whether `rec` carries everything `wakeexec.target()` needs to skip the registry
    entirely — not just a found VS Code instance (`user_data_dir`/`app_pid`, the fields only a
    successful `ps` search fills in), but `pid`/`cwd` too: neither is in `_REQUIRED`, so a
    validator-accepted capture can carry an instance and still be missing one of them, and
    `target()` would fall to `row_for()` for exactly the field this record exists to spare it."""
    if rec.get("entrypoint") == TMUX_ENTRYPOINT:
        return all(k in rec for k in ("pid", "cwd", *_TMUX))
    return all(k in rec for k in ("pid", "cwd", *_INSTANCE))


def should_replace(existing: dict | None, new: dict) -> bool:
    """Whether a fresh capture should overwrite what write-time resolution already has stored
    for this (session, epoch). Never when `existing` is complete, `new` is not, and both name
    the same runtime: a transient `ps` failure that produced a partial capture must not erase
    the one complete record a dark session's wake will ever get to use again. A different
    runtime_id means a new incarnation regardless of completeness — the old record is for a
    process that no longer exists, and `waking.wake_session()`'s own runtime fence would refuse
    it either way."""
    if existing is None:
        return True
    same = existing.get("runtime_id") == new.get("runtime_id")
    # A pane is handed over once, at connect, and the Stop hook captures a claude-vscode record
    # every turn for a CLI session whose tmux server inherited VS Code's environment (doc 118
    # §2.10). Only another tmux record may replace the incarnation's pane.
    if same and existing.get("entrypoint") == TMUX_ENTRYPOINT and new.get("entrypoint") != TMUX_ENTRYPOINT:
        return False
    if complete(new):
        return True
    return not complete(existing) or not same


def validate(rec: object) -> str | None:
    """Why this is not a targeting record the collector could have produced, or None."""
    why = records.shape(rec, _REQUIRED, _FIELDS)
    if why:
        return why
    if isinstance(rec["v"], bool) or rec["v"] != V or rec["source"] not in harness.PROVIDERS:
        return "unknown version or source"
    why = (records.short_strings(rec, ("provider_session", "runtime_id", "observed_at", "entrypoint"), 500)
           or records.short_strings(rec, ("user_data_dir", "cwd", "tmux_socket", "tmux_pane"), 500,
                                    optional=True))
    if why:
        return why
    if rec["entrypoint"] == TMUX_ENTRYPOINT:
        if not {"pid", "cwd", *_TMUX} <= set(rec):
            return "a tmux record names its pid, cwd, socket and pane"
        if set(_INSTANCE) & set(rec):
            return "a tmux record carries no VS Code instance"
        if not rec["tmux_socket"].startswith("/"):
            return "tmux_socket must be an absolute path"
        if not _PANE.fullmatch(rec["tmux_pane"]):
            return "tmux_pane must be % and digits"
    elif rec["entrypoint"] != TAB_ENTRYPOINT:
        return "entrypoint must be claude-vscode or tmux"
    elif set(_TMUX) & set(rec):
        return "a claude-vscode record carries no tmux pane"
    why = records.integers(rec, ("pid", "app_pid"), least=1, what="a positive integer")
    if why:
        return why
    if ("user_data_dir" in rec) != ("app_pid" in rec):
        return "user_data_dir and app_pid travel together or not at all"
    return None
