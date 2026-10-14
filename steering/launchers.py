"""The launchers a machine declares for interactive Claude sessions (doc 121 §2), and the argv and
environment a launched session runs with (§4).

A launcher runs its arguments with one account's configuration. Nothing here knows which: the
account is the usage reading of the launcher's usage source, its own `auth status`'s
`configDirectory` or the claude-code-proxy it declares, and no vendor is named in this module.
An entry may instead name its account — `{"account": "<vendor>/<id>"}` (#6458) — and the command
is found on this machine through `discovered`, which pairs `unlimited accounts` with the
machine's executables and probes what each is.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parent))
import python_floor  # noqa: E402

python_floor.require()

import asyncio
import contextlib
import json
import os
import re
import subprocess
import tempfile
import time
from typing import Callable

import codex_proxy
import envfile
import spool
import storelock

ENV = "STEERING_CLAUDE_LAUNCHERS"
SOCKET = "2mw2lt-launch"          # tmux -L: the platform's own server, apart from the login's
PREFIX = "2mw2lt-launch-"         # every session this module starts, and nothing else
STATUS_TIMEOUT = 20.0
LAUNCH_DEADLINE_S = 300.0
PLUGIN = "2mw2lt@2mw2lt"
KEYS = {"path", "direct", "proxy", "vendor", "unrouted", "account"}
VENDOR = re.compile(r"[a-z0-9][a-z0-9-]{0,31}")
ACCOUNT_MAX = 128
# Each account entry's last resolved command, so a read of `unlimited accounts` that missed never
# withdraws what the entry offered (#3046): the entry keeps the command it last resolved to in
# this process, keyed by the `<vendor>/<id>` the entry names.
_resolved: dict[str, dict] = {}
# What a launched session inherits (§4): the headless brain's list less its token, and the door.
KEPT = ("HOME", "USER", "LOGNAME", "SHELL", "PATH", "TMPDIR", "LANG", "LC_ALL", "LC_CTYPE", "TERM",
        "STEERING_DOOR", "STEERING_PORT", "COORDINATION_PG_PREFIX")


# The usage read's word, as `accounts.wire` words it, for a home holding Claude Code's own record of
# a failed refresh: the login has lapsed, while `auth status` still answers it signed in (2026-10-12,
# M1). An expired token is not one: an idle account's expires within hours, and the session
# refreshes it at start.
SIGNED_OUT = "signed out"


class Unread(Exception):
    """The machine's accounts have not been read yet, so no launcher can be joined to one."""


class Undeclared(Exception):
    """The declaration is missing or names something that is not an executable."""


def declared(env: dict | None = None, log: Callable[[str], None] | None = None,
             commands: Callable[[Callable[[str], None]], dict[str, list[dict]] | None] | None = None) -> list[dict]:
    """The declared launchers (doc 121 §2), each `{path, direct, proxy, vendor, unrouted}` with `path`
    an absolute executable. An entry is a path, or an object saying what the launcher cannot; an
    unrouted one starts only launches naming its vendor, never one routed by its model. An entry
    may instead name its account — `{"account": "<vendor>/<id>"}` (#6458) — which resolves to this
    machine's command for it, the first `discovered.commands()` finds, its harness from the probe
    and its vendor from the key, and carries `account` and the `found_by` name (this machine's own
    key to its command, never sent) beside the path entries' shape. The resolution is the
    caller's to give — this module is in the enrollment client's boundary, and `discovered`
    reaches the harness stack, which stays out of it — so an account entry with no `commands`
    given resolves to nothing, said through `log`. So does one a read names no command for; when
    `unlimited accounts` itself cannot be asked, the entry keeps the command it last resolved to
    in this process, so a missed read never withdraws an offer (#3046). A path entry may carry
    `account` too, which then asserts where it must land (`accounts`). The agent.toml's `[routing]`
    declares them where it does, read at the call so a change needs no restart (#2394); an `env`
    given is judged alone."""
    items, source = None, ENV
    if env is None:
        import settings
        items, source = settings.claude_launchers(), "agent.toml [routing].claude_launchers"
    if items is None:
        source = ENV
        raw = (os.environ if env is None else env).get(ENV)
        if not raw:
            raise Undeclared(f"this machine declares no launcher ({ENV})")
        try:
            items = json.loads(raw)
        except ValueError:
            raise Undeclared(f"{ENV} is not a JSON list") from None
        if not isinstance(items, list) or not items:
            raise Undeclared(f"{ENV} is not a JSON list of launchers")
    log = log or (lambda line: None)
    found = commands(log) if any(isinstance(i, dict) and isinstance(i.get("account"), str)
                                 and "path" not in i for i in items) and commands else None
    out = []
    for item in items:
        d = {"path": item} if isinstance(item, str) else item
        if not isinstance(d, dict) or set(d) - KEYS \
                or not (isinstance(d.get("path"), str) or ("account" in d and "path" not in d)):
            raise Undeclared(f"{source} entry {json.dumps(item)[:80]} is not a path or "
                             f"an object of {', '.join(sorted(KEYS))}")
        account = None
        if "account" in d:
            vendor, sep, ident = (d["account"] if isinstance(d["account"], str) else "").partition("/")
            if not sep or not VENDOR.fullmatch(vendor) or not 0 < len(ident) <= ACCOUNT_MAX:
                raise Undeclared(f"{d.get('account')!r}: account is not <vendor>/<account id>")
            if d.get("vendor") not in (None, vendor):
                raise Undeclared(f"{d['account']}: account names vendor {vendor}, declared {d['vendor']}")
            account = d["account"]
        if not isinstance(d.get("direct", False), bool):
            raise Undeclared(f"{d.get('path', account)}: direct is true or false")
        if not isinstance(d.get("unrouted", False), bool):
            raise Undeclared(f"{d.get('path', account)}: unrouted is true or false")
        if "path" not in d:
            # An account entry runs the command that is found for it: never a proxy of its own,
            # and never a `direct` the probe would override — what the command is, is probed.
            if "proxy" in d:
                raise Undeclared(f"{account}: an account entry declares no proxy")
            if "direct" in d:
                raise Undeclared(f"{account}: an account entry declares no direct; the probe decides")
            cmd = None
            if found is not None:
                got = found.get(account)
                if got:
                    _resolved[account] = got[0]
                    cmd = got[0]
                else:
                    # A read that names no command for the account is an answer, not a miss: what
                    # it last resolved to must not come back on a later read that fails.
                    _resolved.pop(account, None)
            else:
                cmd = _resolved.get(account)
            if cmd is None:
                log(f"launcher account {account} resolves to no command"
                    + (", and no commands were given to resolve it with" if commands is None
                       else ", and unlimited could not be asked" if found is None else ""))
                continue
            out.append({"path": cmd["path"], "direct": cmd["harness"] == "direct", "proxy": None,
                        "vendor": account.partition("/")[0], "unrouted": d.get("unrouted", False),
                        "account": account, "found_by": Path(cmd["path"]).name})
            continue
        if "proxy" in d and not (isinstance(d["proxy"], str) and codex_proxy.loopback_port(d["proxy"])):
            raise Undeclared(f"{d['path']}: proxy is not a loopback URL")
        if "vendor" in d and not (isinstance(d["vendor"], str) and VENDOR.fullmatch(d["vendor"])):
            raise Undeclared(f"{d['path']}: vendor is not a vendor name")
        p = Path(d["path"])
        if not p.is_absolute():
            raise Undeclared(f"{d['path']} is not an absolute path")
        if not p.is_file() or not os.access(p, os.X_OK):
            raise Undeclared(f"{d['path']} is not an executable file")
        # The path as declared, never resolved: a launcher may be one program under several names,
        # choosing its account by the name it is run as, and resolving the link runs every name as
        # the target's one account (#2368).
        out.append({"path": str(p), "direct": d.get("direct", False),
                    "proxy": d.get("proxy"), "vendor": d.get("vendor"), "unrouted": d.get("unrouted", False),
                    **({"account": account} if account else {})})
    return out


def _run(argv: list[str]) -> str:
    return subprocess.run(argv, capture_output=True, text=True, stdin=subprocess.DEVNULL,
                          timeout=STATUS_TIMEOUT, check=False).stdout


def _claude(launcher: dict) -> list[str]:
    """How the launcher runs its account's `claude`: itself when direct, as a wrapper otherwise."""
    return [launcher["path"]] if launcher.get("direct") else [launcher["path"], "claude"]


def _login_lapsed(where: str) -> bool:
    import claude_signin
    return claude_signin.login_lapsed(Path(where))


def status(launcher: dict, run: Callable[[list[str]], str] = _run) -> dict:
    """The launcher's `auth status --json`, or {} when it does not answer one. It answers signed in
    on a claude.ai sign-in whose refresh has lapsed, which a launched session cannot use (#6477):
    that reads as signed out, `loginLapsed` saying why. A token, a key or a proxy is not that
    sign-in, whatever record the home still holds."""
    try:
        got = json.loads(run([*_claude(launcher), "auth", "status", "--json"]))
    except (ValueError, OSError, subprocess.SubprocessError):
        return {}
    if not isinstance(got, dict):
        return {}
    where = got.get("configDirectory")
    if got.get("loggedIn") is True and got.get("authMethod") == "claude.ai" \
            and not launcher.get("proxy") and isinstance(where, str) and where and _login_lapsed(where):
        got["loggedIn"], got["loginLapsed"] = False, True
        print(f"launchers: {launcher['path']}: login in {where} has lapsed: the sign-in's refresh "
              "expiry has passed", file=sys.stderr, flush=True)
    return got


def plugin_installed(config_dir: str) -> bool:
    """Whether `config_dir`'s `installed_plugins.json` names an installed record for the 2mw2lt
    plugin (#2724). Malformed or unexpected shapes count as absent, never raise."""
    try:
        doc = json.loads((Path(config_dir) / "plugins" / "installed_plugins.json").read_text())
    except (OSError, ValueError):
        return False
    plugins = doc.get("plugins") if isinstance(doc, dict) else None
    records = plugins.get(PLUGIN) if isinstance(plugins, dict) else None
    return isinstance(records, list) and bool(records)


def accounts(launchers: list[dict], readings: list[dict], run: Callable[[list[str]], str] = _run,
             proxy_label: Callable[[str], tuple[tuple[str, str] | None, str]] = None,
             why: dict | None = None, dead: set[str] | None = None) -> list[dict]:
    """`{vendor, account_id, launcher}` for each launcher signed in first-party, the account being
    the usage reading (`readings`, as the agent sends them) labelled by the launcher's usage
    source; with none, its declared vendor and its directory's label, an account with no verdict.
    `why`, when given, takes the one cause for each launcher offering nothing, by its path. `dead`
    takes the paths whose read answered that the launcher is signed out — an answer about the
    account, not a read that missed (#3046) — by its `auth status` or by its usage reading."""
    why = {} if why is None else why
    import transcript_proof
    proxy_label = proxy_label or _proxy_label
    # By harness as well as label: Claude's own directory and Codex's are both `default`.
    by = {}
    lapsed = set()
    for r in readings:
        if isinstance(r.get("directory"), str) and r.get("why") == SIGNED_OUT:
            lapsed.add((r.get("provider"), r["directory"]))
        if isinstance(r.get("directory"), str) and r.get("vendor") and r.get("account_id"):
            by.setdefault((r.get("provider"), r["directory"]), (r["vendor"], r["account_id"]))
    out = []
    for launcher in launchers:
        s = status(launcher, run)
        where = s.get("configDirectory")
        if not s:
            why[launcher["path"]] = "its auth status gave no answer"
            continue
        if s.get("loggedIn") is not True or s.get("apiProvider") != "firstParty":
            if dead is not None:
                dead.add(launcher["path"])
            why[launcher["path"]] = (f"not signed in first-party (loggedIn {s.get('loggedIn')}, "
                                     f"apiProvider {s.get('apiProvider')})")
            if s.get("loginLapsed") is True:
                why[launcher["path"]] = ("its login has lapsed: the sign-in's refresh expiry has passed, "
                                         "so a session would stop at its login screen")
            continue
        if not isinstance(where, str) or not where:
            why[launcher["path"]] = "its auth status names no configDirectory"
            continue
        own = transcript_proof.account_of(Path(where))
        if launcher.get("proxy"):
            key, probed = proxy_label(launcher["proxy"])
        else:
            key, probed = ("claude", own), f"directory {own}"
        read = by.get(key) if key else None
        if launcher.get("account"):
            # An entry that names its account asserts where it must land (#6458): only a reading
            # of that very account, or — with no reading for its home — a home labelled by the
            # name the command was found by, places it. Anything else offers nothing, both
            # identities said.
            claimed = tuple(launcher["account"].split("/", 1))
            if read is not None and read != claimed:
                why[launcher["path"]] = (f"its reading names {read[0]} account {read[1]}, "
                                         f"its entry claims {launcher['account']}")
                continue
            if read is None:
                found_by = launcher.get("found_by")
                if not found_by or found_by != own:
                    why[launcher["path"]] = (f"no reading for directory {own}, and it was found as "
                                             f"{found_by or 'a path'}, not a name of that account")
                    continue
            read = claimed
        # A proxy whose reading is not found now has an account all the same, only unread this
        # time: naming it after the directory would give one launcher two identities (#2325's
        # live test), so it offers nothing until its reading is found again.
        elif read is None and launcher.get("vendor") and not launcher.get("proxy"):
            read = (launcher["vendor"], own)
        if key is None:
            why[launcher["path"]] = probed
            continue
        if key in lapsed:
            if dead is not None:
                dead.add(launcher["path"])
            why[launcher["path"]] = f"its usage read answers {SIGNED_OUT}, though its auth status answers signed in"
            continue
        if read is None:
            held = sorted(d for p, d in by if p == (key or ("",))[0]) if key else []
            why[launcher["path"]] = (f"no reading for {probed}" + (f" (readings under {key[0]}: "
                                     f"{', '.join(held) or 'none'})" if key else "")
                                     + ("" if launcher.get("proxy") or launcher.get("vendor")
                                        else ", and no vendor declared"))
            continue
        if launcher.get("vendor") and launcher["vendor"] != read[0]:
            why[launcher["path"]] = f"its reading names vendor {read[0]}, declared {launcher['vendor']}"
            continue
        out.append({"vendor": read[0], "account_id": read[1], "launcher": launcher, "where": where})
    # One home is offered once where an entry names its account (#6458): a path entry and an
    # `account` entry for it are one launcher in two forms, kept side by side while a machine
    # moves between them, and offering both is a choice the launch would make for no reason. Two
    # plain path entries on one home stay both offered — the owner's `aN` beside a direct
    # launcher is a deliberate pair — and the first entry that places a home wins.
    placed: set[str] = set()
    placed_by_account: set[str] = set()
    once = []
    for a in out:
        if not a["launcher"].get("synthesized"):
            names_account = bool(a["launcher"].get("account"))
            if a["where"] in placed and (names_account or a["where"] in placed_by_account):
                why[a["launcher"]["path"]] = f"another launcher already placed directory {a['where']}"
                continue
            placed.add(a["where"])
            if names_account:
                placed_by_account.add(a["where"])
        once.append(a)
    out = once
    # A launcher the agent wrote for an env file steps aside for a declared one placing the same
    # home (#6001): the declaration is the owner's word, and two launchers for one account is a
    # choice the launch would make for no reason.
    return [a for a in out if not a["launcher"].get("synthesized") or a["where"] not in placed]


def offer(declared_: list[dict], readings: list[dict] | None, log: Callable[[str], None],
          run: Callable[[list[str]], str] = _run, proxy_label=None,
          dead: set[str] | None = None) -> list[dict]:
    """`accounts()` against the readings the agent sends, each launcher's offer logged, and each
    offering nothing with its cause. Joined only against those: a reading taken here instead races the agent's
    own first one, and the census after a restart offered no Codex launcher because of it
    (#2336's live test). Before the first reading, `Unread`: the census after it makes the offer."""
    if readings is None:
        raise Unread("this agent has not read its accounts yet")
    why: dict = {}
    found = accounts(declared_, readings, run, proxy_label, why, dead)
    for a in found:
        log(f"launcher {Path(a['launcher']['path']).name} offers {a['vendor']} account {a['account_id']}")
    for path, cause in why.items():
        log(f"launcher {Path(path).name} offers no account: {cause}")
    return found


def declared_paths(env: dict | None = None) -> set[str]:
    """The launcher paths as declared, held as declared (#2368)."""
    return {d["path"] for d in declared(env)}


def census_entries(offered: list[dict]) -> list[dict]:
    """The offer a census carries as its `interactive` array: each account by its vendor and id,
    never a machine's own name for its command or its path (#6458 §2.6) — two machines whose
    commands for one account are named differently send the same entry, and coordination keys by
    the id both name."""
    return [{"vendor": a["vendor"], "account_id": a["account_id"],
             **({"unrouted": True} if a["launcher"].get("unrouted") else {})} for a in offered]


def census_offer(kept: dict, found: list[dict] | None, declared_paths: set[str],
                 dead: set[str] | None = None) -> list[dict] | None:
    """The offer a census carries, and `kept` — the machine's offer, kept across censuses by
    launcher path — brought up to date with this read (`found`, None when the read failed).

    A launcher is withdrawn when it is no longer declared, and one whose read answered that it is
    signed out (`dead`, an answer about the account); a read that merely missed it — no answer, a
    timeout, a reading not found — does not withdraw it (#3046): that must not take away the offer
    the daemon may already have chosen from. None when a failed read leaves nothing kept: before
    the agent's own first reading there is nothing to offer and the census carries none at all,
    leaving the daemon's declaration as it stands (#2336's live test)."""
    for path in [p for p in kept if p not in declared_paths]:
        del kept[path]
    for path in dead or ():
        kept.pop(path, None)
    if found is not None:
        kept.update({a["launcher"]["path"]: a for a in found})
    if found is None and not kept and declared_paths:
        return None
    return list(kept.values())


def withdrawn(kept: dict, dead: set[str] | None) -> list[dict]:
    """The accounts a read withdrew, as a census carries them beside the offer (#6031): each dead
    launcher's kept offer, read before `census_offer` withdraws it, deduped and sorted by account.
    A home offered while its account lived can hold a login that has since lapsed, and the seat
    hears the withdrawal with the sessions it strands. A launcher signed out before it ever
    offered is not here: nothing was withdrawn. `dead` None is a read that failed, which
    withdraws nothing."""
    out: list[dict] = []
    for a in sorted((a for path, a in kept.items() if path in (dead or ())),
                    key=lambda a: (a["vendor"], a["account_id"])):
        if not any(o["vendor"] == a["vendor"] and o["account_id"] == a["account_id"] for o in out):
            out.append(a)
    return out


def for_launch(offered: list[dict], fresh: list[dict], run: Callable[[list[str]], str] = _run,
               dead: set[str] = frozenset()) -> list[dict]:
    """The accounts a launch may start: a fresh join (`fresh`, `accounts()` now), and an account
    offered earlier (`offered`) only for a launcher the fresh join could not place at all, still
    signed in to the same directory. A proxy probe that did not answer once dropped the launcher
    from the join and the daemon's chosen account was refused (#2325's live test). A launcher the
    join found signed out (`dead`) is not one it could not place."""
    placed = {a["launcher"]["path"] for a in fresh} | set(dead)
    return fresh + still_signed_in([a for a in offered if a["launcher"]["path"] not in placed], run)


def still_signed_in(offered: list[dict], run: Callable[[list[str]], str] = _run) -> list[dict]:
    """Of the accounts offered, those whose launcher still answers signed in first-party to the
    directory it answered with when offered."""
    out = []
    for a in offered:
        s = status(a["launcher"], run)
        if s.get("loggedIn") is True and s.get("apiProvider") == "firstParty" \
                and s.get("configDirectory") == a.get("where"):
            out.append(a)
    return out


def _proxy_label(base_url: str) -> tuple[tuple[str, str] | None, str]:
    """The harness and wire label of the reading of the claude-code-proxy at `base_url`, as
    `accounts.readings` labels it, and what the probe found on the way."""
    import transcript_proof
    port = codex_proxy.loopback_port(base_url)
    if port is None:
        return None, f"proxy {base_url}: not a loopback URL"
    pid, missing = codex_proxy.listening(port)
    if pid is None:
        return None, f"proxy {base_url}: {missing}"
    root = codex_proxy.proxy_home(pid)
    if root is None:
        return None, f"proxy {base_url}: pid {pid} is not {codex_proxy.NAME}, or names no home"
    label = transcript_proof.account_of(codex_proxy.label(root), "codex")
    return ("codex", label), f"proxy {base_url} pid {pid} home {root} label {label}"


def _token_root() -> Path:
    return Path.home() / ".config" / "2mw2lt" / "launch-env"


def sweep_token_files() -> None:
    root = _token_root()
    if not root.is_dir() or root.is_symlink():
        return
    root.chmod(0o700)
    # Creation and pinning are one operation against a sweep. After a producer dies, its
    # independent pane may still consume the handoff until the launch's recorded deadline.
    with storelock.locked(root / ".create"):
        entries = []
        now = time.time()
        for p in root.glob("launch-*"):
            if p.is_symlink():
                continue
            if p.is_dir():
                try:
                    expires = float(p.with_name(p.name + ".lock").read_text())
                except (OSError, ValueError):
                    # Handoffs written before expiry was recorded get a full launch budget.
                    expires = p.stat().st_mtime + LAUNCH_DEADLINE_S
                if expires <= now:
                    entries.append(p)
            elif p.name.endswith(".lock") and not p.with_suffix("").exists():
                entries.append(p.with_suffix(""))
        storelock.sweep(entries, now=float("inf"), days=0)


@contextlib.asynccontextmanager
async def token_file(workspace_env: dict, offload=asyncio.to_thread, *, lifetime_s=LAUNCH_DEADLINE_S):
    token = workspace_env.get("GH_TOKEN")
    if not token:
        yield None
        return

    def write():
        root = _token_root()
        if root.is_symlink():
            raise ValueError("launch environment root is a symlink")
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        root.chmod(0o700)
        held = contextlib.ExitStack()
        try:
            with storelock.locked(root / ".create"):
                scratch = tempfile.TemporaryDirectory(prefix="launch-", dir=root)
                path = Path(scratch.name) / "env"
                lock = held.enter_context(storelock.locked(path.parent))
                held.callback(lock.unlink, missing_ok=True)
                held.callback(scratch.cleanup)
                lock.chmod(0o600)
                lock.write_text(str(time.time() + max(0, lifetime_s)))
                spool.write_atomic(path, f"GH_TOKEN={token}\n", mode=0o600)
        except BaseException:
            held.close()
            raise
        return held, path

    held, path = await offload(write)
    try:
        yield path
    finally:
        await offload(held.close)


def environment(env: dict | None = None) -> list[str]:
    """`KEY=value` for `env -i`: the generic allowlist."""
    src = os.environ if env is None else env
    return [f"{k}={src[k]}" for k in KEPT if k in src]


def argv(launcher: dict, workspace: str, model: str | None, effort: str | None, name: str, env: dict | None = None,
         tmux: str = "tmux", scope: list[str] | None = None, *, token_file: Path | None = None,
         forge_env: dict[str, str] | None = None, resume: str | None = None,
         cwd: str | None = None) -> list[str]:
    """The `tmux new-session` that starts the session: argv, never a shell line, with its
    environment replaced whole, since a tmux server copies its own into every pane.

    A rehome (#5430) names the conversation to `resume` and the checkout it ran in, `cwd`, since
    the harness finds a transcript by the directory it is started in. Its model and effort are
    the account's own when none is named. The prompt is the literal connect either way: a resumed
    session's enrolment refuses any other first prompt.

    Under `scope`, by default the agent's own: the first `new-session` forks the tmux server,
    which in a systemd unit would otherwise die with the unit's next stop (#2968)."""
    if scope is None:
        import agentjob
        scope = agentjob.scope_prefix(name)
    bootstrap = [sys.executable, str(Path(envfile.__file__).resolve()), str(token_file)] if token_file else []
    settings = ["--settings", json.dumps({"permissions": {"deny": [
        "Read(~/Library/Containers)", "Read(~/Library/Containers/**)",
        "Read(~/Library/Group Containers)", "Read(~/Library/Group Containers/**)",
    ]}})] if sys.platform == "darwin" else []
    level = [*(["--model", model] if model else []), *(["--effort", effort] if effort else [])]
    return [*scope, tmux, "-L", SOCKET, "new-session", "-d", "-s", name, "-c", cwd or workspace, "--",
            "/usr/bin/env", "-i", *[kv for kv in environment(env) if kv.partition("=")[0] not in (forge_env or {})],
            *[f"{k}={v}" for k, v in (forge_env or {}).items()], *bootstrap,
            *_claude(launcher), *settings, *(["--resume", resume] if resume else []), *level, "/2mw2lt:connect"]


def _suggest() -> int:
    """The `suggest` subcommand (#6458 §2.5): this machine's suggestion lines, printed. The
    logic is `launcher_suggest`'s own, run as its own process — a launch the boundary policy
    declares — because importing it here would pull the harness stack into the enrollment
    client's closure (the plan's unit 2 ruling)."""
    import subprocess
    script = Path(__file__).parent / "launcher_suggest.py"
    return subprocess.run([sys.executable, str(script), "suggest"]).returncode


if __name__ == "__main__":
    raise SystemExit(_suggest() if sys.argv[1:] == ["suggest"] else
                     (print("usage: launchers.py suggest", file=sys.stderr), 2)[1])
