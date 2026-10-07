#!/usr/bin/env python3
"""A workspace's observation hook pack and enrollment state (doc 16 §11). A repository is
installed with `/2mw2lt:install` (doc 129), which calls the hook pack here; what is left as a
command is the brain machine's own form and the operator's tools:

  python3 steering/enroll/hooks.py install   [workspace] local
  python3 steering/enroll/hooks.py install-codex [workspace] local
  python3 steering/enroll/hooks.py uninstall [workspace]
  python3 steering/enroll/hooks.py show      [workspace]

`local` wires a workspace to this machine's own daemon. `install-codex` writes the shared
workspace state without Claude's hooks.
`uninstall` takes out the hook entries; taking back the door, the recipes and the copied tokens
needs the ownership manifest (#138).
"""
from __future__ import annotations

import ast
import contextlib
import fcntl
import json
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
import checkout_binding  # noqa: E402
import spool  # noqa: E402
import supervision  # noqa: E402
import verb_help  # noqa: E402
from local_workspace import (agent_port, common_root, origin_slug, required_workspace_root,  # noqa: E402
                       workspace_root, write_agent_port)
import door as door_mod  # noqa: E402
from door import door  # noqa: E402
import urllib.error  # noqa: E402
import urllib.request  # noqa: E402

SENDER = HERE / "observe.py"
READOUT = HERE / "readout.py"
LAUNCHER = HERE / "launch.py"      # the file installed into a workspace, not the one it runs
LAUNCH_NAME = "steering-launch.py"  # what it is called once it is there
TOKENS = ("steering-observe-token",)
TIMEOUT = 5  # seconds; the sender's own budget is 4


def launcher_path(workspace: Path) -> Path:
    """Where a workspace keeps the one path its hooks name."""
    return Path(workspace) / ".claude" / LAUNCH_NAME


def command(workspace: Path, verb: str) -> str:
    return f"python3 {shlex.quote(str(launcher_path(workspace)))} {verb}"


def handler(workspace: Path, verb: str) -> dict:
    return {"type": "command", "command": command(workspace, verb), "timeout": TIMEOUT}


# `<…>/plugins/cache/<marketplace>/<plugin>/<version>/steering/enroll/<script>.py`. Everything
# before the version names one plugin install; the version directory under it is what
# `claude plugin update` replaces.
_PLUGIN = re.compile(r"^(?P<install>.*/plugins/cache/[^/]+/[^/]+)/(?P<version>[^/]+)/steering/enroll/[^/]+\.py$")


def plugin_install(path: Path) -> str | None:
    """The version-independent root of the plugin a script belongs to; None for a checkout."""
    m = _PLUGIN.match(str(path))
    return m["install"] if m else None


def _command_path(h: object) -> str | None:
    """The script a handler runs, or None when it is not one of ours to read."""
    if not isinstance(h, dict) or h.get("type") != "command" or not isinstance(h.get("command"), str):
        return None
    try:
        parts = shlex.split(h["command"])
    except ValueError:
        return None
    # `python3 <script>` and `python3 <launcher> <verb>` alike: the script is what identifies
    # the handler, and the verb is which of the pack's entry points it asks for.
    return parts[1] if len(parts) in (2, 3) and parts[0] == "python3" else None


def siblings(sender: Path) -> tuple[Path, ...]:
    """Every other script an install of this sender owns: the readout it packs."""
    return (sender.parent / "readout.py",)


def is_ours(h: object, sender: Path = SENDER) -> bool:
    """Whether this handler is this installation's to replace.

    A handler running a script beside this sender is this install's, whatever the script is
    named: the pack has gained and lost scripts (the Stop-hook relay was one until 2026-09),
    and an install that only recognised the names it currently writes left every retired one
    firing on a file that is no longer shipped. Another checkout's directory is left alone.

    Every version of the SAME plugin install counts too: `claude plugin update` moves the
    scripts to a new version directory, and the pack left behind under the old one is not
    another checkout's. Left in place it observed every event twice and raced two handlers
    for one turn end (#226).
    """
    path = _command_path(h)
    if path is None:
        return False
    if Path(path).name == LAUNCH_NAME:
        return True
    if Path(path).parent == sender.parent:
        return True
    install = plugin_install(sender)
    return install is not None and plugin_install(Path(path)) == install


def has_ours(entry: object, sender: Path = SENDER) -> bool:
    return isinstance(entry, dict) and any(is_ours(h, sender) for h in entry.get("hooks", []))


_ENROLL = re.compile(r"^(?:.*/)?(?:steering/enroll/[^/]+\.py|steering-launch\.py)$")


def wired_by(entry: object) -> set[str]:
    """Every steering pack a hook entry runs, whoever installed it.

    `is_ours` answers what this install may replace and is narrow on purpose. Reporting
    through it made a workspace another install had wired read as having no hooks at all.

    All of them, not the first: one group holds several handlers, and two packs in one is
    the condition `show` reports these directories to expose (#226).
    """
    if not isinstance(entry, dict):
        return set()
    return {str(Path(p).parent) for h in entry.get("hooks", [])
            if (p := _command_path(h)) and _ENROLL.match(p)}


def stale_pins(doc: dict, sender: Path = SENDER) -> set[str]:
    """The directories of this install's pack that are not this sender's: what a plugin
    update left pinned under the version directory it replaced (#340)."""
    # A handler on the launcher pins nothing, so it is never stale.
    return {str(Path(p).parent) for entries in doc.get("hooks", {}).values() for e in entries
            for h in (e.get("hooks", []) if isinstance(e, dict) else [])
            if is_ours(h, sender) and (p := _command_path(h))
            and Path(p).name != LAUNCH_NAME and Path(p).parent != sender.parent}


def short(doc: dict, workspace: Path, sender: Path = SENDER) -> set[str]:
    """The `<event>:<entry point>` pairs this pack declares that a workspace does not run.

    A workspace on the launcher pins no version, so `stale_pins` is empty for it forever. A
    pack that gains a handler would therefore reach every workspace except the ones already
    using it, which is all of them (#1506). A workspace we have not wired at all is nobody's
    to repair here: `hooks.py install` is what wires one, and answering with the whole pack
    would turn a repin into an install.
    """
    if not any(has_ours(e, sender) for groups in doc.get("hooks", {}).values() for e in groups):
        return set()
    # By the entry point each handler asks the launcher for, never by the command string: the
    # workspace path is in that string, and one reached through a symlink spells it differently
    # (`/var` and `/private/var` on macOS), which read a fully wired workspace as running none
    # of the pack and rewrote its settings on every connect.
    def verbs(groups: object) -> set[str]:
        out = set()
        for e in groups if isinstance(groups, list) else []:
            for h in (e.get("hooks", []) if isinstance(e, dict) else []):
                # Read through the same defensive parser the rest of this module uses: a
                # workspace may carry anything another tool wrote, and a command that does not
                # lex must leave that entry alone rather than stop the connect.
                script = _command_path(h)
                if script is None or Path(script).name != LAUNCH_NAME:
                    continue
                parts = shlex.split(h["command"])   # `_command_path` lexed it already
                if len(parts) == 3:
                    out.add(parts[2])
        return out
    want = pack(workspace)
    return {f"{ev}:{v}" for ev, groups in want.items()
            for v in verbs(groups) - verbs(doc.get("hooks", {}).get(ev))}


def launcher_stale(doc: dict, workspace: Path, sender: Path = SENDER) -> bool:
    """Whether a workspace this pack has wired needs its launcher refreshed — its content
    behind this pack's copy, or the file gone entirely though `doc` still names it.

    `stale_pins` and `short` both read the *wiring* — which hook events name which entry
    points — because that used to be the only staleness a launcher-based workspace had: the
    launcher itself carries no version of its own, so a plugin update never left it pinned to
    anything, and a change to its own logic had always shipped alongside a new entry point
    `short` would catch. A change confined to the launcher's own contract, with no new hook
    entry to wire — `exec`, #1890 — leaves both of those empty on an already-wired workspace,
    so `repin` refreshed nothing and every session on it ran an `exec` an old launcher does not
    know, exiting 0 on the unknown verb the same way a hook always does: no hold armed, nothing
    printed, and the restart-after-every-exit rule spinning it forever (#1875, recurring). A
    workspace this pack has never wired is not repin's to fill in here either, the same rule
    `short` states — including one whose launcher file happens to exist regardless, which is
    nobody's orphan to adopt.
    """
    if not any(has_ours(e, sender) for groups in doc.get("hooks", {}).values() for e in groups):
        return False
    p = launcher_path(workspace)
    if not p.is_file():
        return True
    return p.read_text() != LAUNCHER.read_text().replace('SOURCE = ""', f"SOURCE = {str(HERE)!r}", 1)


_SOURCE = re.compile(r"^SOURCE = (?P<value>.+)$", re.M)


def launcher_source(text: str) -> str | None:
    """The pack a workspace's launcher was written from, as it records it; None when unreadable."""
    m = _SOURCE.search(text)
    try:
        value = ast.literal_eval(m["value"]) if m else None
    except (ValueError, SyntaxError):
        return None
    return value if isinstance(value, str) and value else None


def _cached(source: str | Path) -> tuple[int, ...] | None | bool:
    """A cached pack's version, None when it does not read as one, False for a checkout."""
    m = _PLUGIN.match(str(Path(source) / LAUNCHER.name))
    if not m:
        return False
    try:
        return tuple(int(x) for x in m["version"].split("."))
    except ValueError:
        return None


def may_replace(theirs: str | None, mine: Path = HERE) -> bool:
    """Whether the pack at `mine` may rewrite a workspace launcher written from `theirs`.

    Every session in a workspace runs its hooks through that one file, and repin runs on each
    hook event, so the rule decides who it follows. A launcher this pack wrote, or one that
    records no source, is refreshed. A checkout (a `--plugin-dir` session) never takes over
    another pack's launcher: the whole workspace followed a throwaway worktree until a sibling's
    connect pointed it back (#3969). A cached pack heals one a checkout wrote, and never moves
    one back to an earlier version of its own install, which two sessions on two versions
    otherwise traded on every hook, from one config home or two. A launcher whose pack is gone
    serves nothing, so anyone may replace it.
    """
    if theirs is None or Path(theirs) == mine or not Path(theirs).is_dir():
        return True
    ours = _cached(mine)
    if ours is False:
        return False
    other = _cached(theirs)
    if other is False or other is None or ours is None:
        return True
    return other < ours


def repin(workspace: Path, sender: Path = SENDER) -> Path | None:
    """Move a workspace onto its launcher and onto this pack; the settings file it rewrote, or
    None when it was already on both. Tokens and the door are the workspace's already, and
    after this there is nothing left to repin: the launcher names no version."""
    p = settings_path(workspace)
    if not p.exists():  # a workspace with no settings pins nothing, and gets no lock file either
        return None
    with settings_transaction(p):
        doc = load(p)
        if not stale_pins(doc, sender) and not short(doc, workspace, sender) \
                and not launcher_stale(doc, workspace, sender):
            return None
        held = launcher_path(workspace)
        if held.is_file() and not may_replace(launcher_source(held.read_text())):
            return None
        install_launcher(workspace)
        write(p, merge(doc, workspace, sender))
    return p


def pack(workspace: Path) -> dict:
    out = {ev: [{"hooks": [handler(workspace, "observe")]}] for ev in sorted(supervision.ADMITTED)}
    out["Stop"].append({"hooks": [handler(workspace, "readout")]})  # doc 30 §3: its own handler, not a branch in the adapter
    out["Stop"].append({"hooks": [handler(workspace, "armed")]})     # #1506
    # doc 154 §8: a compacted or cleared session is pointed back at its checkpoint.
    out["SessionStart"] = [{"matcher": "compact|clear", "hooks": [handler(workspace, "rebrief")]}]
    out["SessionEnd"] = [{"matcher": "clear", "hooks": [handler(workspace, "rebrief")]}]  # which session was cleared
    return out


def install_launcher(workspace: Path) -> Path:
    """The one file a workspace's hooks name, copied in from whichever version is installing.

    It is written on every install, so a launcher whose own contract changed is replaced by the
    same act that wires the workspace. It carries no version of its own and reads the record the
    harness keeps, so nothing here has to be told about an update.
    """
    dst = launcher_path(workspace)
    dst.parent.mkdir(parents=True, exist_ok=True)
    # The pack doing the installing, so a workspace with no plugin cache to search still has
    # somewhere to hand the turn to.
    spool.write_atomic(dst, LAUNCHER.read_text().replace('SOURCE = ""', f"SOURCE = {str(HERE)!r}", 1),
                       mode=0o700)
    return dst


def settings_path(workspace: Path) -> Path:
    return workspace / ".claude" / "settings.local.json"


def load(path: Path) -> dict:
    try:
        doc = json.loads(path.read_text())
    except FileNotFoundError:
        return {}
    if not isinstance(doc, dict):
        raise ValueError(f"{path}: not a JSON object")
    return doc


def _without(entry: object, sender: Path) -> object:
    """The group minus this checkout's handler; None when nothing else was in it, its matcher
    included: a group with no handler left runs nothing, and the rebrief's is matched."""
    if not has_ours(entry, sender):
        return entry
    rest = [h for h in entry["hooks"] if not is_ours(h, sender)]
    if not rest:
        return None
    return {**entry, "hooks": rest}


def strip(doc: dict, sender: Path = SENDER) -> dict:
    """The document without this checkout's handler: every other handler, group and key in place."""
    out = {k: v for k, v in doc.items() if k != "hooks"}
    # An empty group too: a version whose strip kept a matched group without its handler left
    # one behind on every repin, and a group with no handler runs nothing.
    hooks = {ev: [g for g in (_without(e, sender) for e in entries)
                  if g is not None and not (isinstance(g, dict) and g.get("hooks") == [])]
             for ev, entries in doc.get("hooks", {}).items()}
    hooks = {ev: entries for ev, entries in hooks.items() if entries}
    if hooks:
        out["hooks"] = hooks
    return out


def merge(doc: dict, workspace: Path, sender: Path = SENDER) -> dict:
    out = strip(doc, sender)
    hooks = dict(out.get("hooks", {}))
    for ev, entries in pack(workspace).items():
        hooks[ev] = list(hooks.get(ev, [])) + entries
    out["hooks"] = hooks
    return out


def write(path: Path, doc: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        mode = path.stat().st_mode & 0o777
    except FileNotFoundError:
        mode = 0o600
    spool.write_atomic(path, json.dumps(doc, indent=2) + "\n", mode=mode)  # the mode it had


@contextlib.contextmanager
def settings_transaction(path: Path):
    """Hold this settings file's sibling lock across its read-merge-replace."""
    path.parent.mkdir(parents=True, exist_ok=True)
    lock = path.with_name(f".{path.name}.lock")
    with open(lock, "a+") as file:
        fcntl.flock(file, fcntl.LOCK_EX)
        yield


def authorities(base: str, wid: str | None = None) -> dict:
    """What the daemon holds authority over, or an empty document when it says nothing.

    An older daemon has no such route, and to it every workspace is a wired one — which is
    what this returning nothing means downstream. A caller that would act differently on
    "no authorities" than on "could not ask" must not use that answer for both, which is
    why both install paths check the workspace's own binding before trusting it.

    Sent on the door's own transport, which reaches the loopback and remote doors directly. A
    proxy on the machine hijacks both (doc 08): `urllib` honours the system one where `curl`
    does not, and on a machine running one the daemon answered `curl` while this call came
    back 502 — read as an older daemon, which wired an authority as a plain repository,
    tokens and all.
    """
    root, _ = door_mod.split(base)
    # A remote credential is scoped to one `/w/<id>` and discovers only that authority.
    # Never fall back from a qualified probe to the root: that would turn a missing or wrong
    # credential into foreign discovery. The unqualified form remains for loopback
    # administration and legacy local installation.
    urls = [f"{root}/w/{wid}/steering/authorities"] if wid else [f"{root}/steering/authorities"]
    for url in urls:
        req = urllib.request.Request(url)
        try:
            with door_mod.send(req, timeout=5) as r:
                doc = json.load(r)
        except (urllib.error.URLError, OSError, ValueError):
            continue
        if isinstance(doc, dict) and ("authorities" in doc or "primary" in doc):
            return doc
    return {}


def bind(workspace: Path) -> str | None:
    """Record which workspace of this deployment a checkout is, from its own `origin`.

    The manifest says which repository each workspace draws; a checkout says which
    repository it is. That pair is the same on every machine, which a path is not, and it
    is resolved here — once, at install — so nothing on a read path runs git for it.
    """
    try:
        root = common_root(Path(workspace), timeout=5)
        slug = origin_slug(root, timeout=5)
    except Exception:
        return None
    wid = checkout_binding.id_for_repo(slug) if slug else None
    if wid:
        checkout_binding.bind(root, wid)
    return wid


def bound_id(workspace: Path) -> str | None:
    """The workspace id this checkout already carries, without writing one."""
    try:
        return checkout_binding.id_at(common_root(Path(workspace), timeout=5))
    except Exception:
        return None


def authority_of(doc: dict, workspace: Path) -> str | None:
    """The id of the authority this workspace is, or None.

    A workspace the daemon holds authority over already has its own ledger, brain and
    tokens; it is addressed at `/w/<id>` and must not be handed another authority's
    credentials, which the daemon refuses to start with. A wired repository — one no
    authority holds — names the primary and takes a copy of its tokens, as it always has.

    Matched on the id the workspace is bound to, and only then on where it sits. The daemon
    reports its own machine's paths, so a second machine's checkout of the same repository
    matched nothing and was wired to the primary — the air gap open exactly where it was
    needed. The path is still what places a workspace no binding can name: one whose
    repository this deployment does not name, or which has no `origin` to be asked. Callers
    bind first; this only reads.
    """
    wid = bound_id(workspace)
    if wid and any(a.get("id") == wid for a in doc.get("authorities", [])):
        return wid
    try:
        mine = common_root(Path(workspace), timeout=5).resolve()
    except Exception:
        return None
    for a in doc.get("authorities", []):
        try:
            if Path(a["workspace"]).resolve() == mine:
                return a["id"]
        except (KeyError, TypeError, OSError):
            continue
    return None


def refuse_unplaced(base: str, doc: dict, workspace: Path) -> None:
    """Refuse when a workspace this deployment names is not one of the daemon's authorities.

    Wiring is the only alternative, and for a named workspace it is always wrong: the door
    goes to the primary, the sessions file in the primary's ledger, and a local install
    copies the primary's tokens. That is the air gap open, and it looks exactly like a
    correct install. So the two ways to arrive here are both refused — the daemon could not
    be asked, and the daemon answered without this workspace in it.

    A repository this deployment names nothing for is unaffected. Those are the wired
    repositories one brain has always served, and the primary is where they belong.
    """
    wid = bound_id(workspace)
    if not wid:
        return
    held = [a.get("id") for a in doc.get("authorities", [])]
    if not held:
        raise SystemExit(
            f"{base} did not say which workspaces it holds, and {workspace} is {wid!r} in "
            f"this deployment. Wiring it to the bare door would file its sessions in "
            f"another workspace's ledger. Check the daemon is up and reachable, then run "
            f"this again.")
    if wid not in held:
        raise SystemExit(
            f"{base} holds {', '.join(sorted(str(h) for h in held))}, and {workspace} is "
            f"{wid!r} in this deployment. Name its root in STEERING_WORKSPACES and restart "
            f"the daemon, or take it out of steering/workspaces.json if it is not a "
            f"workspace of this deployment.")


def primary_root(doc: dict) -> Path | None:
    """Where the authority a wired workspace names keeps the tokens it will be handed."""
    for a in doc.get("authorities", []):
        if a.get("id") == doc.get("primary"):
            try:
                return Path(a["workspace"])
            except (KeyError, TypeError):
                return None
    return None


def token_source(doc: dict) -> Path | None:
    """Where an install reads the daemon's tokens from.

    The daemon answers for the deployment it belongs to. When STEERING_WORKSPACE redirects this
    run at a scratch workspace, the primary named in that answer is still the real deployment's,
    and reading tokens from it copies live credentials into the fixture (#375). The override is
    the whole statement of which workspace this run is about, so it decides the source too.
    """
    override = os.environ.get("STEERING_WORKSPACE")
    return Path(override) if override else primary_root(doc)


def read_tokens(workspace: Path, source: Path | None = None) -> dict[str, str]:
    """The daemon's tokens, read before anything is written. Refused when one is missing or
    empty, so an install never leaves a hook that can only answer {}.

    `source` is the root of the authority serving this workspace. Without it this reads the
    workspace of the checkout the code lives in, which under a configured set of workspaces
    is not necessarily any authority the daemon holds.
    """
    src = (Path(source) if source is not None else workspace_root()) / ".claude"
    if is_daemon_workspace(workspace) or src.resolve() == (Path(workspace) / ".claude").resolve():
        return {}
    values = {}
    for name in TOKENS:
        try:
            values[name] = (src / name).read_text().strip()
        except OSError:
            values[name] = ""
        if not values[name]:
            raise RuntimeError(f"{src / name} is missing or empty: start the daemon once so it mints it, then install")
    return values


def copy_tokens(workspace: Path, source: Path | None = None) -> None:
    """The serving authority's tokens into a wired workspace's .claude, 0600."""
    dst = Path(workspace) / ".claude"
    values = read_tokens(workspace, source)
    if not values:
        return
    dst.mkdir(parents=True, exist_ok=True)
    for name, value in values.items():
        spool.write_atomic(dst / name, value, mode=0o600)


def tokens_directory(workspace: Path) -> Path:
    """The per-session enrollment records shared by every harness in a workspace."""
    path = Path(workspace) / ".claude" / "steering-tokens"
    path.mkdir(parents=True, exist_ok=True)
    return path


def planned_settings(workspace: Path) -> dict:
    """Build the preflight document so an unfoldable settings file refuses before any write."""
    return merge(load(settings_path(workspace)), workspace)


def hooks_install(workspace: Path, local: bool | None = None, planned: dict | None = None,
                  workspace_id: str | None = None, source: Path | None = None) -> Path:
    """`local` says which mode install chose; without it the door in the environment decides.

    `planned` preserves the caller's preflight validation, but not its settings snapshot: a
    competing installer may have replaced it before this write takes the lock.

    `workspace_id` names this workspace as one the daemon holds authority over. It mints its
    own tokens, so copying the daemon's in would be handing it another authority's — which
    is the shape the daemon refuses to start with.
    """
    if local is None:
        local = not door()[1]
    if planned is None:
        planned_settings(workspace)
    if local and workspace_id is None:
        copy_tokens(workspace, source)
    install_launcher(workspace)
    p = settings_path(workspace)
    with settings_transaction(p):
        write(p, merge(load(p), workspace))
    return p


def installed_door(workspace: Path) -> str | None:
    """The door this workspace's repository already names, if any."""
    try:
        lines = (common_root(Path(workspace), timeout=5) / ".env").read_text().splitlines()
    except Exception:
        return None
    return next((l.strip().split("=", 1)[1] for l in lines if l.strip().startswith("STEERING_DOOR=")), None)


def local_install(workspace: Path, with_hooks: bool = True) -> dict:
    """A workspace on the brain's own machine, wired to the loopback daemon."""
    named = installed_door(workspace)
    if named:  # a door in .env wins over local mode at hook time, whatever this reports
        raise SystemExit(f"{workspace} already names {named}: local mode is for a workspace with no door")
    base = f"http://127.0.0.1:{os.environ.get('STEERING_PORT', '9999')}"
    bind(workspace)  # from the checkout's own origin, before the daemon is asked anything
    doc = authorities(base)
    refuse_unplaced(base, doc, workspace)
    wid = authority_of(doc, workspace)
    source = token_source(doc)
    if wid is None:
        read_tokens(workspace, source)
    planned = planned_settings(workspace) if with_hooks else None
    if not with_hooks:
        if wid is None:
            copy_tokens(workspace, source)
        tokens = tokens_directory(workspace)
        return {"door": base if wid is None else f"{base}/w/{wid}", "remote": False,
                "authority": wid, "tokens": str(tokens)}
    return {"door": base if wid is None else f"{base}/w/{wid}", "remote": False,
            "authority": wid,
            "settings": str(hooks_install(workspace, local=True, planned=planned,
                                          workspace_id=wid, source=source))}


LOCAL = "local"


def install_agent(workspace: Path, result: dict) -> dict:
    """Start this workspace's per-machine agent after its door is fully resolved."""
    # The hook installer also wires arbitrary application workspaces. Only a 2mw2lt checkout
    # owns the venv and entrypoint a per-machine agent can run.
    if not (workspace / "steering" / "agent.py").is_file():
        return result
    root = common_root(workspace, timeout=5)
    port = agent_port(root)
    write_agent_port(root, port)
    command = [sys.executable, str(HERE.parent / "install-agent.py"), str(root), result["door"]]
    if not result["remote"]:
        command.append("--local")
    subprocess.run(command, check=True, env={**os.environ, "STEERING_AGENT_PORT": port})
    return result


def install(workspace: Path, door_url: str | None = None, with_hooks: bool = True) -> dict:
    """Wire the workspace's shared state, and its observation hooks when requested."""
    required_workspace_root(workspace, timeout=5, configured=False)
    if door_url != LOCAL:
        verb = "install" if with_hooks else "install-codex"
        raise SystemExit(f"a workspace is installed with /2mw2lt:install (doc 129); `hooks.py {verb} "
                         "<workspace> local` wires one to this machine's own daemon")
    return install_agent(workspace, local_install(workspace, with_hooks))


def is_daemon_workspace(workspace: Path) -> bool:
    """The workspace the daemon runs from, whose `.claude` holds the canonical tokens rather than
    a copy. `copy_tokens` will not write there; nothing here may delete from there."""
    try:
        return (workspace_root() / ".claude").resolve() == (Path(workspace) / ".claude").resolve()
    except OSError:
        return False


def uninstall(workspace: Path) -> Path:
    p = settings_path(workspace)
    if p.exists():  # stripping nothing must not leave a file where there was none
        with settings_transaction(p):
            write(p, strip(load(p)))
    return p


def main(argv: list[str]) -> int:
    if verb_help.help_requested("hooks", argv):
        print(verb_help.script_help("hooks", topic=argv[0] if len(argv) == 2 else None)); return 0
    if not argv:
        return verb_help.error("hooks", "missing command")
    action = argv[0]
    if action not in ("install", "install-codex", "uninstall", "show"):
        return verb_help.error("hooks", f"unknown command {action!r}")
    if any(arg.startswith("-") for arg in argv[1:]):
        return verb_help.error("hooks", "options are not accepted")
    rest = argv[1:]
    if action in ("install", "install-codex"):
        if len(rest) > 2:
            return verb_help.error("hooks", "extra argument")
        if not rest or rest[-1] != LOCAL:
            return verb_help.error("hooks", "install requires the local mode")
        workspace = rest[0] if len(rest) == 2 else None
    else:
        if len(rest) > 1:
            return verb_help.error("hooks", "extra argument")
        workspace = rest[0] if rest else None
    ws = Path(workspace or os.getcwd()).resolve()
    if action == "show":
        doc = load(settings_path(ws))
        hooks = doc.get("hooks", {})
        packs = {p for entries in hooks.values() for e in entries for p in wired_by(e)}
        wired = sorted(ev for ev, entries in hooks.items() if any(wired_by(e) for e in entries))
        os.environ["CLAUDE_PROJECT_DIR"] = str(ws)  # every field reported is the named workspace's
        print(json.dumps({"settings": str(settings_path(ws)), "hooks": wired,
                          "packs": sorted(packs), "workspace": bound_id(ws),
                          "door": door()[0]}, indent=2)); return 0
    if action in ("install", "install-codex"):
        print(json.dumps(install(ws, LOCAL, with_hooks=action == "install"), indent=2)); return 0
    print(f"uninstall: {uninstall(ws)}"); return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
