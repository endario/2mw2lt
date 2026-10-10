"""The alias window's door rewrite (go-alias-removal-design.md D5, "The door rewrite"): each
workspace this OS user registered at an alias door, `<root>/w/<alias>`, moves to its address door,
`<root>/<team>/<workspace>`, and is keyed by its uuid. Its two callers are an agent's activation and
`/2mw2lt:install` run again. The cut release deletes it.

A workspace whose door cannot be resolved is left on its alias door, which the window's serve still
answers, and the reason is said; the next activation or install tries again. Each file is replaced
whole (`spool.write_atomic`), and the steps run in an order a run stopped between any two of them
resumes from (a workspace whose door is on DEAD_HOST is instead forgotten):

1. ask the alias door for its authorities, on the credential this machine holds for it;
2. the credential written again under the address door, the old file kept;
3. the checkout's `.env` STEERING_DOOR;
4. the checkout's binding, as the uuid;
5. `<uuid>.json` in the registry, then the alias's entry removed;
6. the old credential and its lock removed.

Until step 5 the alias entry stands, so the next run starts again at step 1. After it, an alias
credential no entry names and whose address copy exists is the one step 6 did not reach.

The credential's `workspace` field keeps the alias (D8): the release before the window renews at
`/w/{workspace}`, so a machine rolled back to it still renews.
"""
from __future__ import annotations

import contextlib
import fcntl
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from typing import Callable

import checkout_binding
import credential
import machine_workspaces
import spool

_ENROLL = str(Path(__file__).resolve().parent / "enroll")
if _ENROLL not in sys.path:
    sys.path.insert(0, _ENROLL)
import door as door_mod  # noqa: E402

# The whole question of serve, which an activation waits on for every workspace it moves.
TIMEOUT = 5.0
# Its DNS record and tunnel were deleted on 2026-10-10, so no door on it answers again.
DEAD_HOST = "door.2mw2lt.com"


class Kept(Exception):
    """Why a workspace stays on the door it has: one check, named, and what it was answered."""


def alias_door(url: str) -> tuple[str, str] | None:
    """(root, alias) for a door that names its workspace by alias at the platform's root, and None
    for any other: an address door, or a Python silo's `/t/<silo>/w/<id>`, which no address replaces."""
    root, workspace = door_mod.split(url)
    if workspace is None or "/" in workspace or urllib.parse.urlsplit(root).path:
        return None
    return (root, workspace) if url.rstrip("/") == f"{root}/w/{workspace}" else None


def _address_door(url: str) -> bool:
    _, workspace = door_mod.split(url)
    return workspace is not None and "/" in workspace


def is_uuid(value: object) -> bool:
    try:
        return isinstance(value, str) and str(uuid.UUID(value)) == value.lower()
    except ValueError:
        return False


def resolve(door: str) -> tuple[str, str, str | None]:
    """The workspace `door` opens, as (uuid, address, alias), from its authorities on this machine's
    credential (D6). `Kept` names the check that failed."""
    url = f"{door}/steering/authorities"
    held = credential.entry_for(url)
    if held is None:
        raise Kept("this OS user holds no credential for it")
    # The credential as held, never renewed for this: a renewal is its own request to coordination,
    # with its own wait, and would take the ask past TIMEOUT. One that has lapsed is refused below,
    # and the agent that starts next renews it.
    token = (_read(held) or {}).get("credential")
    if not isinstance(token, str) or not token:
        raise Kept(f"its credential {held} holds no issued credential yet")
    req = urllib.request.Request(url, headers={"Accept": "application/json", credential.HEADER: token,
                                               "User-Agent": door_mod.USER_AGENT})
    try:
        with door_mod.open_direct(req, TIMEOUT) as r:
            raw = r.read()
    except urllib.error.HTTPError as e:
        raise Kept(f"its authorities answered {e.code}: {e.read(200).decode(errors='replace').strip()}") from None
    except OSError as e:  # unanswered, or the held credential refused before it was sent
        raise Kept(f"its authorities did not answer: {e}") from None
    try:
        view = json.loads(raw)
    except ValueError:
        raise Kept(f"its authorities answer is not JSON: {raw[:200].decode(errors='replace')}") from None
    if not isinstance(view, dict) or view.get("coordination") is not True:
        raise Kept("its authorities answer is not coordination's")
    rows = view.get("authorities")
    if not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], dict):
        raise Kept(f"its authorities answer names {len(rows) if isinstance(rows, list) else 'no'} workspaces, not one")
    workspace_id, address = rows[0].get("workspace_id"), rows[0].get("address")
    if not is_uuid(workspace_id):
        raise Kept(f"its authorities answer names no workspace_id ({workspace_id!r}): serve predates the alias window")
    root, _ = door_mod.split(door)
    if not isinstance(address, str) or door_mod.split(f"{root}/{address}")[1] != address:
        raise Kept(f"its authorities answer names no address ({address!r})")
    alias = rows[0].get("id")
    return workspace_id, address, alias if isinstance(alias, str) and alias else None


def _env_door(env: Path) -> str | None:
    try:
        lines = env.read_text().splitlines()
    except FileNotFoundError:
        return None
    for line in lines:
        key, _, value = line.strip().partition("=")
        if key == "STEERING_DOOR" and value.strip():
            return value.strip().strip('"').strip("'").rstrip("/")
    return None


def _write_env(env: Path, door: str) -> None:
    lines = env.read_text().splitlines()
    out, placed = [], False
    for line in lines:
        if line.strip().partition("=")[0] == "STEERING_DOOR":
            if not placed:
                out.append(f"STEERING_DOOR={door}")
                placed = True
            continue
        out.append(line)
    spool.write_atomic(env, "\n".join(out) + "\n", mode=0o600)


def _bind(root: Path, workspace_id: str) -> None:
    """The checkout's binding, as `checkout_binding.bind` writes it, replaced whole."""
    p = Path(root) / checkout_binding.BINDING
    p.parent.mkdir(parents=True, exist_ok=True)
    spool.write_atomic(p, workspace_id + "\n")


@contextlib.contextmanager
def _locked(p: Path):
    """The credential's own lock, which a renewal takes, so the copy is never of a half-renewed file."""
    fd = os.open(p.with_suffix(".lock"), os.O_WRONLY | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def _read(p: Path) -> dict | None:
    try:
        d = json.loads(p.read_text())
    except (OSError, ValueError):
        return None
    return d if isinstance(d, dict) else None


def _move(where: Path, name: str, entry: machine_workspaces.Entry, held_by: dict[str, Path]) -> tuple[str, str]:
    """One registry entry moved to its address door, keyed by its uuid: what was done and the door it
    is at, or `Kept`.
    `held_by` is the checkout each registered door is served for."""
    old = entry.door
    aliased = alias_door(old)
    workspace_id, address, alias = resolve(old)
    new = f"{aliased[0]}/{address}" if aliased else old
    other = held_by.get(new)
    if other is not None and other.resolve() != entry.root.resolve():
        # Two entries on one door are served by neither (`machine_workspaces.entries`).
        raise Kept(f"{other} is already served at {new}, so {entry.root} moving there would leave "
                   f"neither checkout served; uninstall one of them")
    env = entry.root / ".env"
    named = _env_door(env)
    if named not in (old, new):
        raise Kept(f"{env} names {named or 'no door'}, neither its entry's door nor {new}")
    bound = checkout_binding.id_at(entry.root)
    if bound not in (None, workspace_id, alias):
        raise Kept(f"{entry.root} is bound to {bound}, and its door opens {alias or workspace_id}")
    if aliased:
        held = credential.path_for(old)
        with _locked(held):
            d = _read(held)
            if d is None or str(d.get("door", "")).rstrip("/") != old:
                raise Kept(f"its credential {held} cannot be read")
            spool.write_atomic(credential.path_for(new), json.dumps({**d, "door": new}), mode=0o600)   # 2
            _write_env(env, new)                                                                        # 3
            _bind(entry.root, workspace_id)                                             # 4
            machine_workspaces.write(where, workspace_id, machine_workspaces.Entry(                     # 5
                entry.root, new, entry.capability_file, entry.port, entry.gh_account))
            if name != f"{workspace_id}.json":
                (where / name).unlink(missing_ok=True)
            held.unlink(missing_ok=True)                                                                # 6
        held.with_suffix(".lock").unlink(missing_ok=True)
        return f"moved from {old} to {new} as {workspace_id}", new
    _bind(entry.root, workspace_id)
    machine_workspaces.write(where, workspace_id, entry)
    if name != f"{workspace_id}.json":
        (where / name).unlink(missing_ok=True)
    return f"keyed by {workspace_id} at {old}", old


def _forget(where: Path, name: str, entry: machine_workspaces.Entry) -> None:
    """An entry whose door died with DEAD_HOST: its registry file and its credential removed, under
    the credential's lock as `_move` takes it. Its checkout's `.env` and binding are the person's."""
    held = credential.path_for(entry.door)
    with _locked(held):
        (where / name).unlink(missing_ok=True)
        held.unlink(missing_ok=True)
    held.with_suffix(".lock").unlink(missing_ok=True)


def _done(name: str, entry: machine_workspaces.Entry) -> bool:
    """An address door already keyed by its uuid, which needs no question of serve."""
    stem = name.removesuffix(".json")
    return is_uuid(stem) and checkout_binding.id_at(entry.root) == stem


def rewrite(where: Path, say: Callable[[str], None], only: Path | None = None) -> None:
    """Every workspace in the registry at `where` moved to its address door and keyed by its uuid,
    or only the one whose checkout is `only`. Each move and each refusal is said, with its reason
    (C5); none raises, so the caller goes on serving what it has."""
    try:
        files = sorted(f for f in where.iterdir() if f.suffix == ".json")
    except FileNotFoundError:
        return
    parsed: list[tuple[Path, machine_workspaces.Entry]] = []
    for f in files:
        try:
            parsed.append((f, machine_workspaces.parse(f.read_text())))
        except (OSError, ValueError) as e:
            say(f"door rewrite: {f.name} not read: {e}")
    held_by = {entry.door: entry.root for _, entry in parsed}
    doors = set(held_by)
    for f, entry in parsed:
        if only is not None and entry.root.resolve() != Path(only).resolve():
            continue
        if urllib.parse.urlsplit(entry.door).hostname == DEAD_HOST:
            try:
                _forget(where, f.name, entry)
            except OSError as e:
                say(f"door rewrite: {entry.root} kept at {entry.door}: a file could not be removed: {e}")
                continue
            doors.discard(entry.door)
            held_by.pop(entry.door, None)
            say(f"door rewrite: {entry.root} forgotten at {entry.door}, which {DEAD_HOST} no longer answers; "
                f"its .env still names that door, so run /2mw2lt:install again in {entry.root}")
            continue
        # A loopback brain's door carries its capability: it is the Python daemon's, not serve's.
        if entry.capability_file is not None or not (alias_door(entry.door) or _address_door(entry.door)):
            continue
        if not alias_door(entry.door) and _done(f.name, entry):
            continue
        try:
            said, now = _move(where, f.name, entry, held_by)
        except Kept as why:
            say(f"door rewrite: {entry.root} kept at {entry.door}: {why}")
            continue
        except OSError as e:
            say(f"door rewrite: {entry.root} kept at {entry.door}: a file could not be written: {e}")
            continue
        doors.discard(entry.door)
        held_by.pop(entry.door, None)
        held_by[now] = entry.root
        say(f"door rewrite: {entry.root} {said}")
    _sweep(doors, say)


def _sweep(doors: set[str], say: Callable[[str], None]) -> None:
    """The alias credentials a run stopped after step 5 left: one no registry entry names, whose
    copy under an address door holds the same workspace on the same origin."""
    home = credential.home()
    held = {p: d for p in sorted(home.glob("*.json")) if (d := _read(p)) is not None} if home.is_dir() else {}
    moved = {(d.get("origin"), d.get("workspace")) for d in held.values()
             if d.get("native") and _address_door(str(d.get("door", "")))}
    for p, d in held.items():
        door = str(d.get("door", "")).rstrip("/")
        if (d.get("native") and alias_door(door) and door not in doors
                and (d.get("origin"), d.get("workspace")) in moved):
            p.unlink(missing_ok=True)
            p.with_suffix(".lock").unlink(missing_ok=True)
            say(f"door rewrite: the credential for {door} removed, its address copy holding it")
