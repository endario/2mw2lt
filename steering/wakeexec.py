"""The machine that can type executes a wake (#1772).

The daemon runs on the orchestrator VM; the keyboard and the transcript are on the session's own
Mac, so the orchestrator decides and this acts — the same split the socket work settled on
(doc 99 §3a).

Typing is reached through a `Typist`, so the sequence can be driven and asserted without a
keystroke leaving the test. The default one is the only thing in this module that types.
"""
from __future__ import annotations

import contextlib
import errno
import fcntl
import glob
import json
import os
import re
import sqlite3
import subprocess
import threading
import time
from pathlib import Path
from urllib.parse import unquote, urlparse

import composer
import runtime_id
import targeting
import wake_states

CODE = "/Applications/Visual Studio Code.app/Contents/Resources/app/bin/code"

# A rebranded per-account bundle (`~/Applications/Code <label>.app`) carries the same layout as
# the stock app under its own `Contents/`, so the bundle root is all `_bundle_cli` needs — never
# assumed from the label, which is chosen freely and not addressable by name (#2091, doc 08).
_APP_BUNDLE = re.compile(r"^(.*\.app)/Contents/")


def _bundle_cli(app_pid: int, *, ps=None) -> str | None:
    """The `code` CLI inside the bundle `app_pid` is actually running from, or None where that
    can't be read or the bundle carries no CLI of its own — the caller falls back to `CODE` then.
    """
    try:
        r = (ps or subprocess.run)(["ps", "-ww", "-o", "comm=", "-p", str(app_pid)],
                                   capture_output=True, text=True, timeout=5)
    except Exception:
        return None
    if r.returncode != 0:
        return None
    m = _APP_BUNDLE.match(r.stdout.strip())
    if not m:
        return None
    cli = Path(m.group(1)) / "Contents" / "Resources" / "app" / "bin" / "code"
    return str(cli) if cli.exists() else None

# What is typed. It becomes a real user turn the session pays for and reasons about, so it is
# stated here rather than composed at the call site. The trailer is shared so a wording change
# to it is made once rather than risking the two texts silently diverging.
_WAKE_TRAILER = "Re-arm it with /2mw2lt:connect; this message carries nothing else."
WAKE_TEXT = "steering wake {nonce}: your hold is down and a directive is waiting. " + _WAKE_TRAILER

# The issue's own wording ask: the holder is told the BRAIN has gone silent, not merely that a
# hold is down (#1773) — used only when the frame names role == "seat"; every other frame keeps
# WAKE_TEXT, including one from before this shipped, which carries no "role" key at all.
SEAT_WAKE_TEXT = ("steering wake {nonce}: the brain has gone silent and a directive is waiting. "
                 + _WAKE_TRAILER)

# The one entrypoint this route addresses. Anything else is refused rather than attempted: the
# sequence below activates a *tab*, and a session that is not one has no tab to activate. Owned
# by `targeting.py`, not restated here, so the two can't drift.
TAB_ENTRYPOINT = targeting.TAB_ENTRYPOINT


class Refused(Exception):
    """A precondition that did not hold. Every one is a disposition, not an error."""


def row_for(psession: str) -> dict:
    """The harness's own session registry row. `sessionId` there is the enrolment's
    `provider_session` unchanged, which is what makes a session addressable by id at all."""
    for f in glob.glob(os.path.expanduser("~/.claude*/sessions/*.json")):
        try:
            with open(f) as fh:
                row = json.load(fh)
        except (OSError, ValueError):
            continue
        if row.get("sessionId") == psession:
            return row
    raise Refused(f"no registry row for {psession}")


def tab_for(udd: str, psession: str) -> tuple[str, str]:
    if not Path(udd).is_absolute():
        raise Refused("the app's user-data-dir is relative; launch VS Code with an absolute --user-data-dir")
    matches = []
    for store in (Path(udd) / "User" / "workspaceStorage").glob("*/workspace.json"):
        try:
            metadata = json.loads(store.read_text())
            folder = metadata.get("folder")
            uri = urlparse(folder or metadata.get("workspace") or "")
            if uri.scheme != "file" or uri.netloc not in ("", "localhost"):
                continue
            path = Path(unquote(uri.path))
            name = path.name if folder else path.stem
            database = store.parent / "state.vscdb"
            try:
                database.stat()
            except FileNotFoundError:
                continue
            with contextlib.closing(sqlite3.connect(database.as_uri() + "?mode=ro",
                                                   uri=True, timeout=1)) as db:
                rows = dict(db.execute("select key, value from ItemTable where key in (?, ?)",
                    ("Anthropic.claude-code", "memento/workbench.parts.editor")))
            if "Anthropic.claude-code" not in rows:
                continue
            panels = json.loads(rows["Anthropic.claude-code"]).get("panelTabSessions", [])
            target_panels = [p for p in panels if p.get("sessionId") == psession]
            if not target_panels:
                continue
            if (len(target_panels) != 1 or target_panels[0].get("inferred")
                    or not isinstance(target_panels[0].get("title"), str) or not target_panels[0]["title"]):
                raise Refused("the provider session has an ambiguous stored tab binding")
            title = target_panels[0]["title"]
            if sum(p.get("title") == title for p in panels) != 1:
                raise Refused("the provider session's stored tab title is ambiguous")
            state = json.loads(rows["memento/workbench.parts.editor"])
            pending = [state["editorpart.state"]["serializedGrid"]["root"]]
            found = []
            while pending:
                node = pending.pop()
                if node.get("type") == "branch":
                    pending.extend(node.get("data", []))
                elif node.get("type") == "leaf":
                    for editor in node.get("data", {}).get("editors", []):
                        if editor.get("id") != "workbench.editors.webviewInput":
                            continue
                        value = json.loads(editor["value"])
                        if (value.get("providedId") == "claudeVSCodePanel" and value.get("title") == title
                                and json.loads(value.get("state") or "{}").get("sessionID") == psession):
                            found.append((name, title))
            matches.extend(found)
        except (OSError, ValueError, TypeError, KeyError, AttributeError, sqlite3.Error):
            raise Refused("the instance's stored tab bindings could not be read unambiguously") from None
    if len(matches) != 1:
        raise Refused("the instance has no unique stored editor binding for this provider session (#2183)")
    return matches[0]


def session_pid(frame: dict) -> int | None:
    """The session's pid: the frame's, else the harness registry's. A session with no targeting
    record at the orchestrator is offered with no pid, and a session the agent launched is one
    (#2267's live test), so without the registry neither a wake nor a control finds its pane."""
    pid = frame.get("pid")
    if not isinstance(pid, int) or isinstance(pid, bool):
        with contextlib.suppress(Refused, KeyError, TypeError):
            pid = row_for(frame["psession"])["pid"]
    return pid if isinstance(pid, int) and not isinstance(pid, bool) else None


def session_gone(psession: str, runtime: str) -> bool:
    """Whether the Claude Code process `runtime` names, on this machine, is known to have ended:
    its registry row is gone, or names a pid that no longer derives to it (a pid is reused by the
    OS; the derived id is not). False wherever this machine cannot say: another provider, another
    machine, or a row whose process is still that runtime."""
    if runtime_id.provider_of(runtime) != "claude" or runtime_id.node_of(runtime) != runtime_id.node_id():
        return False
    try:
        pid = row_for(psession).get("pid")
    except Refused:
        return True
    return not isinstance(pid, int) or runtime_id.derive("claude", pid=pid) != runtime


def instance_of(pid: int) -> tuple[str, int]:
    """(user-data-dir, the AX-addressable app pid). Raises `Refused` where the shared primitive
    answers None, since every caller here needs a target or an explicit reason it has none."""
    found = runtime_id.instance_of(pid)
    if found is None:
        raise Refused(f"no VS Code instance in the ancestry of {pid}")
    return found


def target(psession: str, provider: str, frame: dict | None = None, *, ps=None) -> dict:
    """Everything the sequence needs, with the two staleness checks that make it safe to use.

    The frame is read first when it carries targeting fields (doc 113) — pre-handed while the
    session was alive, and cheaper than discovering them again at the one moment reliability
    matters most. `row_for()` (a live read of the local harness registry) is only ever consulted
    for a field the frame does not carry, and never for `user_data_dir`/`app_pid` specifically —
    those fall to `instance_of()`'s ancestor walk instead, the same as before this frame existed.
    This is what makes local discovery a true fallback rather than an always-run first step: a
    session whose registry row is gone (the exact population #1753 exists to reach) but whose
    frame is complete must not be refused before its frame is even read.
    """
    frame = frame or {}
    entrypoint, pid, cwd = frame.get("entrypoint"), frame.get("pid"), frame.get("cwd")
    discovered = False
    if not (entrypoint and pid and cwd):
        row = row_for(psession)
        entrypoint = entrypoint or row.get("entrypoint")
        pid = pid or row["pid"]
        cwd = cwd or row["cwd"]
        discovered = True
    if entrypoint != TAB_ENTRYPOINT:
        raise Refused(f"entrypoint is {entrypoint!r}; this route addresses a tab")
    # The provider is folded into the identity, so a derive under the wrong one reproduces
    # nothing however alive the pid is. A pid is reused by the OS; the derived id is not.
    live = runtime_id.derive(provider, pid=pid)
    if live is None:
        raise Refused(f"pid {pid} names no live process")
    if frame.get("user_data_dir") and frame.get("app_pid"):
        udd, app_pid = frame["user_data_dir"], frame["app_pid"]
    else:
        udd, app_pid = instance_of(pid)
        discovered = True
    # `discovered` is the one fact of whether local discovery contributed anything, computed
    # once here rather than re-derived by the caller from a second, partial re-inspection of
    # `frame` — the two independently-written copies of that re-inspection is how a field this
    # function falls back to (entrypoint/pid/cwd, not just udd/app_pid) could silently stop
    # triggering `execute()`'s self-backfill.
    return {"psession": psession, "pid": pid, "cwd": cwd, "udd": udd,
            "app_pid": app_pid, "runtime": live, "discovered": discovered,
            "cli": _bundle_cli(app_pid, ps=ps) or CODE}


def _backfill(frame: dict, tgt: dict, workspace: Path | None) -> None:
    """Post what local discovery just found, so the next wake for this session does not need
    to discover it again (doc 113 §4's self-backfill)."""
    import observe_post
    rec = targeting.build(provider_session=tgt["psession"], runtime_id=tgt["runtime"],
                          observed_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                          entrypoint=TAB_ENTRYPOINT, pid=tgt["pid"],
                          instance=(tgt["udd"], tgt["app_pid"]), cwd=tgt["cwd"])
    observe_post.post(rec, workspace, 3.0, route="targeting")


# One machine has one keyboard and one frontmost window, so one wake types at a time on it. The
# per-session claim of #1771 does not bound this: two wakes for two *different* sessions are both
# legitimate and, run together, interleave focus, clear, paste and submit. `frontmost_pid` cannot
# save them — it compares the VS Code instance, and sibling tabs share one. So a wake for session
# A could clear session B's composer and submit A's text into it, which is the one outcome the
# owner ruled out (2026-09-22).
#
# Both locks, because there are two ways to get a second typist: another task in this agent, and
# another agent on this machine (one host ran two side by side, #1780).
_TYPING = threading.Lock()
# The typing lock lives at /tmp, whose file is world-writable on purpose: the two agents it
# sequences can be different logins. Where /tmp itself is refused — a judge cage (#2816) — it
# falls back under the login's TMPDIR, which the cage grants; there the agents are one login.
_LOGIN_TMP = (os.environ.get("TMPDIR") or "/tmp").rstrip("/")
TYPING_LOCK_PATH = "/tmp/2mw2lt-wake-typing.lock"
_TYPING_LOCK_FALLBACK = f"{_LOGIN_TMP}/2mw2lt-wake-typing.lock"
# How long a wake waits for the keyboard before giving up. Bounded, not unbounded: the sequence
# focuses a window and types into it, and one that started ten minutes after the orchestrator
# decided to would act against a session whose state has moved on. Long enough to sequence
# behind a wake that is running now (the spike's receipt closed in about two seconds), short
# enough that what it does is still the thing that was decided.
TYPING_WAIT_SECONDS = 90.0
_POLL = 0.05


@contextlib.contextmanager
def _typing(path: str | None = None, wait: float = TYPING_WAIT_SECONDS):
    """Hold the machine's keyboard for the body: one wake types at a time on this machine.

    Waited for rather than refused outright, because a wake is the last thing that reaches a
    session nothing else can reach — dropping it because another session was being woken leaves
    the first one dark. Both locks are taken: another task in this agent, and another agent on
    this machine, which the orchestrator cannot sequence because it does not know the two share
    a keyboard.
    """
    deadline = time.monotonic() + max(0.0, wait)
    if not _acquire_until(lambda: _TYPING.acquire(blocking=False), deadline):
        raise Refused(f"another wake held this machine's keyboard for {wait:.0f}s")
    fd = None
    try:
        try:
            # World-writable, and widened again after the create because the umask narrows
            # the mode the create asks for: the two agents this lock sequences can be different
            # logins, and a mode only the first can open leaves the second with nothing to take.
            lock = path or TYPING_LOCK_PATH
            fd = os.open(lock, os.O_CREAT | os.O_RDWR, 0o666)
            with contextlib.suppress(OSError):
                os.fchmod(fd, 0o666)
        except PermissionError:
            # A create that failed where no lock file stands is the directory refusing this
            # login — a judge cage (#2816) — and the lock falls back under the login's TMPDIR,
            # which the cage grants. A lock file that exists but cannot be opened is not that:
            # the refusal stands, since falling back would hand this wake a second lock and
            # the interleave the first exists to prevent.
            if path is None and not os.path.lexists(lock):
                try:
                    lock = _TYPING_LOCK_FALLBACK
                    fd = os.open(lock, os.O_CREAT | os.O_RDWR, 0o666)
                except OSError as e:
                    raise Refused(f"{lock} is not writable by this login: {e.strerror or e}") from None
                with contextlib.suppress(OSError):
                    os.fchmod(fd, 0o666)
            else:
                # Not the fail-open case. The mode is widened by `fchmod` after the create, so
                # a second login arriving inside that window is refused here rather than typing
                # without the lock: failing open on exactly the two-login case the lock exists
                # for is how the round-3 mode fix could still interleave (#1772 review).
                # Named, because the remedy is to remove the file and there is no safe automatic
                # one: unlinking a lock another agent holds gives both a new inode to take,
                # which is the interleave with extra steps.
                raise Refused(f"{lock} is not writable by this login; remove it") from None
        except OSError as e:
            # Every failure, not only a permission one. The fail-open that used to stand here
            # reasoned from the single-agent case, where the in-process lock is the whole of
            # the exclusion; but EMFILE, ENOSPC or an I/O error on a machine running two agents
            # yields exactly the interleave the cross-agent lock exists to prevent, and a wake
            # not sent costs a turn where a wake typed into the wrong tab costs the owner's
            # trust (#1772 review).
            raise Refused(f"this machine's keyboard lock could not be taken: {e.strerror or e}") from None
        if not _acquire_until(lambda: _flock_nb(fd), deadline):
            raise Refused(f"another agent on this machine held the keyboard for {wait:.0f}s")
        yield
    finally:
        if fd is not None:
            with contextlib.suppress(OSError):
                os.close(fd)
        _TYPING.release()


def _flock_nb(fd: int) -> bool:
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as e:
        if e.errno in (errno.EAGAIN, errno.EACCES, errno.EWOULDBLOCK):
            return False
        raise
    return True


def _acquire_until(take, deadline: float) -> bool:
    """Poll `take` until it succeeds or the deadline passes. Polled rather than blocking because
    `flock` has no timeout of its own, and a thread blocked in it cannot be told that the wake it
    is waiting to send has gone stale."""
    while True:
        if take():
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(_POLL)


def execute(frame: dict, *, typist=None, ps=None, lock_path: str | None = None,
            wait: float = TYPING_WAIT_SECONDS, still_offered=None,
            workspace: Path | None = None, tmux=None, find_pane=None) -> dict:
    """Wake the session the frame names, or say why not. Never raises at the caller: an agent
    that dies on a refusal stops answering for every session on the machine.
    """
    t = typist or Typist()
    typed = False
    attempt, nonce = frame.get("attempt"), frame.get("nonce")
    if not attempt or not nonce:
        return {"attempt": attempt, "state": wake_states.REFUSED, "why": "a wake names an attempt and a nonce"}
    if frame.get("tmux_pane") or frame.get("tmux_socket"):
        return execute_tmux(frame, pane=tmux, still_offered=still_offered)
    # A session that connected before its pane was handed over (doc 118 §3.1) has none in its
    # frame, and a leaked VS Code environment can make it look like a tab. Its pid's ancestry says
    # whether it runs in a pane on this login's default server; where it does, that is the route.
    pid = session_pid(frame)
    via_pane = None
    if pid is not None:
        found = (find_pane or pane_of)(pid)
        if found is not None:
            via_pane = execute_tmux({**frame, "pid": pid, "tmux_socket": found[0], "tmux_pane": found[1]},
                                    pane=tmux, still_offered=still_offered)
            # Ancestry says the process descends from a pane, not that the pane shows it: VS Code
            # started from a pane's shell (`code .`) is such a process. So a pane route that typed
            # nothing (`refused`, as `_failed` keeps it) hands the wake to the tab route.
            if via_pane["state"] != wake_states.REFUSED:
                return via_pane
    if frame.get("role") == "seat":
        # The tab route opens a window through the instance's CLI and a `vscode://` URI. For the
        # brain on 2026-09-24 that started a second main process on the brain's own user-data-dir,
        # which resumed the brain's session in a new window behind a URI consent dialog, beside the
        # brain still running (#2171). Until the route can show it reaches the running instance,
        # the brain is not typed into this way; a pane route above is unaffected.
        why = "the VS Code tab route cannot be shown to reach the brain's running instance (#2171)"
        if via_pane is not None:
            why = f"{via_pane['why']}; and as a tab: {why}"
        return {"attempt": attempt, "state": wake_states.REFUSED, "why": why}
    try:
        tgt = target(frame["psession"], frame.get("provider") or "claude", frame, ps=ps)
    except (Refused, KeyError) as e:
        why = str(e) if via_pane is None else f"{via_pane['why']}; and as a tab: {e}"
        return {"attempt": attempt, "state": wake_states.REFUSED, "why": why}
    if tgt["discovered"]:
        # The frame was missing something target() had to fall back to local discovery for —
        # post what it found so the next wake for this session does not need to discover it
        # again (doc 113 §4's self-backfill). Best-effort: the next Stop hook tries again
        # regardless, so a failure here costs nothing but this one head start.
        try:
            _backfill(frame, tgt, workspace)
        except Exception:
            pass
    mismatch = fence_here(frame, tgt)
    if mismatch:
        return {"attempt": attempt, "state": wake_states.REFUSED, "why": mismatch}
    # Recheck the frame's app address against native ancestry and exact argv.
    if not runtime_id.verify_instance(tgt["pid"], tgt["app_pid"], tgt["udd"]):
        return {"attempt": attempt, "state": wake_states.REFUSED,
                "why": f"app_pid {tgt['app_pid']} is not a verified ancestor of pid {tgt['pid']}"}
    def sequence() -> str | None:
        """The keys, under the machine's keyboard lock. Returns why it stopped, or None."""
        nonlocal typed
        if still_offered is not None and not still_offered():
            return "the uplink that carried this wake ended while it waited for the keyboard"
        t.verify_app(tgt["pid"], tgt["app_pid"], tgt["udd"], tgt["psession"])
        t.preflight()
        t.focus_window(tgt["udd"], tgt["cwd"], cli=tgt["cli"])
        if not runtime_id.verify_instance(tgt["pid"], tgt["app_pid"], tgt["udd"]):
            return f"app_pid {tgt['app_pid']} no longer verifies before tab activation"
        t.activate_tab(tgt["udd"], tgt["psession"], cli=tgt["cli"])
        t.step_focus()
        front = t.frontmost_pid()
        if front != tgt["app_pid"]:
            # Before a key reaches a composer, not after. This bounds the blast radius to the
            # one instance; it cannot say which tab holds focus, which is why the receipt of
            # #1771 is what actually establishes where the text went.
            return f"frontmost is {front}, the session's instance is {tgt['app_pid']}"
        text = WAKE_TEXT  # the brain is refused above
        held = t.take_pasteboard(text.format(nonce=nonce))
        try:
            t.clear_composer()
            typed = True          # from here on a paste may have reached a composer
            t.paste()
            t.submit()
        finally:
            t.restore_pasteboard(held)
        return None

    try:
        with _typing(lock_path, wait):
            why = sequence()
        if why is not None:
            return {"attempt": attempt, "state": wake_states.REFUSED, "why": why}
    except Refused as e:
        return {"attempt": attempt, "state": _failed(typed), "why": str(e)}
    except Exception as e:                     # an agent does not die on one session's wake
        return {"attempt": attempt, "state": _failed(typed),
                "why": f"{type(e).__name__}: {e}"}
    # `sent`, never `received`: what was typed is not a receipt, and only the nonce turning up
    # as a user turn in the session's own transcript is (#1771 §6).
    return {"attempt": attempt, "state": wake_states.SENT, "instance": tgt["app_pid"]}


# What this machine can establish for itself. The other three of `wake_states.PINNED` — `session`,
# `epoch` and `node` — are the orchestrator's: it offers a wake down the uplink of the node the
# enrolment names, and re-checking them here against values taken from the frame would compare
# the frame with itself and pass whatever it said.
FENCED_HERE = ("provider_session", "runtime", "os_user")


def fence_here(frame: dict, tgt: dict) -> str | None:
    """Why this frame must not be typed on this machine now, or None.

    `os_user` is misleadingly named on the wake (doc 28, `waking.pin_for`): it carries the
    per-OS-user node id an agent's uplink registered with, `runtime_id.node_id()`'s own value,
    never `$USER` — a login name repeats across machines and this fence exists to tell logins
    on one machine apart, not spell one (#2091).
    """
    live = {"provider_session": tgt["psession"], "runtime": tgt["runtime"],
            "os_user": runtime_id.node_id() or ""}
    for k in FENCED_HERE:
        if frame.get(k) != live[k]:
            return f"{k} is {live[k]!r}, the wake pinned {frame.get(k)!r}"
    return None


PANE_LOCK_DIR = _LOGIN_TMP


@contextlib.contextmanager
def pane_lock(socket: str, pane_id: str, *, wait: float | None = None, lock_dir: str | None = None):
    """Hold one pane from the first read of its screen to the last key (doc 120 §5). A wake and a
    control each read, then type, and neither holds the other's claim, so without this one could
    type into the picker the other left up. One login's panes are only typed by its own agent
    (the socket's owner is checked), so the file needs no widening — and the directory is the
    login's one TMPDIR, which its agents share."""
    import hashlib
    wait = TYPING_WAIT_SECONDS if wait is None else wait
    digest = hashlib.sha256(f"{socket}\0{pane_id}".encode()).hexdigest()[:16]
    path = os.path.join(lock_dir or PANE_LOCK_DIR, f"2mw2lt-pane-{digest}.lock")
    try:
        fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    except OSError as e:
        raise Refused(f"the pane's lock could not be taken: {e.strerror or e}") from None
    try:
        if not _acquire_until(lambda: _flock_nb(fd), time.monotonic() + max(0.0, wait)):
            raise Refused(f"another action held pane {pane_id} for {wait:.0f}s")
        yield
    finally:
        os.close(fd)


def open_pane(frame: dict, *, pane=None, still_offered=None, what: str = "wake"):
    """The pane a tmux wake or control types into, checked, or `(None, why)`: the frame names it,
    its harness has a measured composer, its pid derives to the pinned runtime, its socket is this
    login's, the pane's process is the session's or an ancestor, and it is not in copy mode."""
    provider = frame.get("provider") or "claude"
    if not frame.get("tmux_socket") or not frame.get("tmux_pane"):
        return None, f"a tmux {what} names both its socket and its pane"
    if not composer.measured(provider):
        return None, f"no measured composer for {provider}"
    pid = frame.get("pid")
    if not isinstance(pid, int) or isinstance(pid, bool):
        return None, f"a tmux {what} names the session's pid"
    live = runtime_id.derive(provider, pid=pid)
    if live is None:
        return None, f"pid {pid} names no live process"
    mismatch = fence_here(frame, {"psession": frame.get("psession"), "runtime": live})
    if mismatch:
        return None, mismatch
    socket = frame["tmux_socket"]
    with contextlib.suppress(OSError):
        if pane is None and os.stat(socket).st_uid != os.getuid():   # a real socket, not a test's
            return None, f"{socket} belongs to another login; its agent answers for that session"
    t = (pane or Tmux)(socket, frame["tmux_pane"])
    pane_pid, in_mode = t.pane()
    if pane_pid is None or pane_pid not in runtime_id._ancestors(pid):
        return None, f"pane {frame['tmux_pane']} runs {pane_pid}, not session pid {pid} or an ancestor of it"
    if in_mode:
        return None, f"pane {frame['tmux_pane']} is in copy mode, where keys drive tmux"
    if still_offered is not None and not still_offered():
        return None, f"the uplink that carried this {what} ended before it was typed"
    return t, ""


def past_consent(t) -> tuple[str, list[str]]:
    """The screen once any recognised first-run consent prompt has been answered, and which were
    (doc 120 §5, the owner's ruling that the default is autonomous). Only the measured folder-trust
    prompt is recognised; an answer that does not land raises `Refused` before Enter is sent."""
    screen = t.screen()
    if composer.trust_prompt(screen) is None:
        return screen, []
    if composer.trust_prompt(screen) == "no":
        t.down()
        screen = t.screen()
    if composer.trust_prompt(screen) != "yes":
        raise Refused("the folder-trust prompt's cursor did not reach its yes")
    t.confirm()
    return t.screen(), ["folder-trust"]


def _foot(screen: str) -> str:
    return " / ".join(composer._plain(line).strip() for line in screen.strip().splitlines()[-4:])[:300]


def execute_tmux(frame: dict, *, pane=None, still_offered=None, lock_dir: str | None = None) -> dict:
    """Wake the session in the tmux pane the frame names (doc 118 §3.2–3.3), or say why not.

    Nothing machine-wide is touched — no focus, keyboard or pasteboard — so `_typing` is not
    taken; one attempt per session is the wake store's claim, and the pane's lock keeps a control
    from typing into it at the same time. What stands in for the VS Code route's focus checks is
    what is read first: the pane still belongs to the session, and its screen shows the harness's
    idle composer. A draft is stashed rather than cleared, and the stash is never pressed on an
    empty composer, where Claude Code's toggle would pull an older draft back and the wake would
    submit it (§2.6).
    """
    attempt, nonce = frame.get("attempt"), frame.get("nonce")
    provider = frame.get("provider") or "claude"
    typed, consents = False, []

    def refused(why: str) -> dict:
        return {"attempt": attempt, "state": wake_states.REFUSED, "why": why, "consents": consents}

    try:
        t, why = open_pane(frame, pane=pane, still_offered=still_offered)
        if t is None:
            return refused(why)
        with pane_lock(frame["tmux_socket"], frame["tmux_pane"], lock_dir=lock_dir):
            screen, answered = past_consent(t)
            consents = [{"prompt": c, "folder": frame.get("cwd")} for c in answered]
            before = composer.read(provider, screen, t.cursor())
            if before is None:
                # The screen's foot goes into the record: a harness update that moves its
                # composer shows up here as every wake refusing with the new layout in view.
                return refused(f"pane {frame['tmux_pane']} shows no idle {provider} composer: {_foot(screen)}")
            if not before.empty:
                t.stash()
                after = composer.read(provider, t.screen(), t.cursor())
                if after is None or not after.empty:
                    # Not pressed again: the stash is a toggle whose effect here is not known,
                    # and a second press could move the draft rather than restore it.
                    return refused("the composer held a draft the stash did not clear")
            text = SEAT_WAKE_TEXT if frame.get("role") == "seat" else WAKE_TEXT
            typed = True             # from here on a paste may have reached the composer
            t.paste(text.format(nonce=nonce), f"steering-wake-{attempt}")
            t.submit()
    except Refused as e:
        return {"attempt": attempt, "state": _failed(typed), "why": str(e), "consents": consents}
    except Exception as e:
        return {"attempt": attempt, "state": _failed(typed), "why": f"{type(e).__name__}: {e}",
                "consents": consents}
    return {"attempt": attempt, "state": wake_states.SENT, "pane": frame["tmux_pane"], "consents": consents}


# How long a pane is given to draw what a key asked for. `capture-pane` straight after
# `send-keys` can read the frame before; the reader polls rather than sleeping a fixed time.
SETTLE_TRIES, SETTLE_GAP_S = 20, 0.1


def _settled(t, ready) -> str:
    """The pane's screen once `ready(screen)` holds, or the last one read when it never does."""
    screen = t.screen()
    for _ in range(SETTLE_TRIES - 1):
        if ready(screen):
            break
        time.sleep(SETTLE_GAP_S)
        screen = t.screen()
    return screen


def _composer(t, screen: str):
    """The Claude Code composer on `screen`, read with the pane's cursor: a pane that draws no
    attributes shows a suggestion as text, and only the cursor says it is not typed (#3020)."""
    return composer.read("claude", screen, t.cursor())


CONTROL_TYPED, CONTROL_REFUSED, CONTROL_UNCERTAIN = "typed", "refused", "uncertain"
_MODEL = re.compile(r"[a-z]+-[0-9]+(?:\.[0-9]+)?")
# How long a compact is given to answer (doc 134 §5): 88 s was measured at 162k tokens, and a
# larger context takes longer. The pane stays locked for the wait.
COMPACT_WAIT_S, COMPACT_POLL_S = 600.0, 1.0


def _waited(t, ready, seconds: float) -> str:
    """The pane's screen once `ready(screen)` holds, or the last one read when `seconds` pass."""
    deadline = time.monotonic() + seconds
    screen = t.screen()
    while not ready(screen) and time.monotonic() < deadline:
        time.sleep(COMPACT_POLL_S)
        screen = t.screen()
    return screen


def control(frame: dict, *, pane=None, find_pane=None, still_offered=None, lock_dir: str | None = None) -> dict:
    """Set a tmux-hosted Claude Code session's effort or model for this session only, or compact
    it (doc 120 §5, doc 134 §5), or say why not. Never raises at the caller.

    Effort and model go through the slider's and the picker's `s`, never a typed value: a typed
    value, or Enter on the picker, saves the account's default, and nothing here writes an
    account's settings (the owner's ruling, doc 120 §1). Before `s` every failure is answered
    with Esc and is `refused`; from `s` on it is `uncertain`, because `s` is what applies the
    change and the echo only reports it. No Enter is sent while the slider or picker is up."""
    rid = frame.get("id")
    # A frame from before doc 134 names only `effort`.
    kind, _, target = str(frame.get("value") or f"effort {frame.get('effort')}").partition(" ")
    consents: list[dict] = []

    def out(state: str, why: str = "", **kw) -> dict:
        return {"id": rid, "state": state, **({"why": why} if why else {}), "consents": consents, **kw}

    if kind == "effort" and target not in composer.LEVELS:
        return out(CONTROL_REFUSED, f"no effort level {target!r}")
    if kind == "model" and not _MODEL.fullmatch(target):
        return out(CONTROL_REFUSED, f"no model {target!r}")
    if kind not in ("effort", "model", "compact") or (kind == "compact" and target):
        return out(CONTROL_REFUSED, f"no control {frame.get('value')!r}")
    if (frame.get("provider") or "claude") != "claude":
        return out(CONTROL_REFUSED, "only Claude Code's controls are measured")
    if not (frame.get("tmux_socket") and frame.get("tmux_pane")):
        pid = session_pid(frame)
        found = (find_pane or pane_of)(pid) if pid is not None else None
        if found is None:
            return out(CONTROL_REFUSED, "the session runs in no tmux pane this agent can find")
        frame = {**frame, "pid": pid, "tmux_socket": found[0], "tmux_pane": found[1]}
    applied, name = False, None
    try:
        t, why = open_pane(frame, pane=pane, still_offered=still_offered, what="control")
        if t is None:
            return out(CONTROL_REFUSED, why)
        with pane_lock(frame["tmux_socket"], frame["tmux_pane"], lock_dir=lock_dir):
            screen, answered = past_consent(t)
            consents[:] = [{"prompt": c, "folder": frame.get("cwd")} for c in answered]
            before = _composer(t, screen)
            if before is None:
                return out(CONTROL_REFUSED, f"pane {frame['tmux_pane']} shows no idle claude composer: {_foot(screen)}")
            if not before.empty:
                return out(CONTROL_REFUSED, f"the composer holds a draft ({before.shape}); a control never stashes")

            def back_out(why: str) -> dict:
                t.escape()
                back = _composer(t, _settled(t, lambda sc: composer.read("claude", sc) is not None))
                if back is None:
                    return out(CONTROL_UNCERTAIN, f"{why}, and Esc did not bring the composer back")
                return out(CONTROL_REFUSED, why if back.empty else f"{why}, and left a draft in the composer")

            def past_switch(to: str, done, **kw) -> str | dict:
                """The screen once `done(screen)` holds or the switch's confirmation is drawn, with
                that confirmation answered Yes — or the result when it could not be (#3100).
                Nothing answered it once, and the session sat at the dialog until the brain pressed
                Enter in its pane by hand. The cursor names the row Enter would take, and the trust
                prompt is answered the same way (§5 of doc 120): Down onto Yes, a read that it is
                there, then Enter. What confirms the switch is still `done`, so a No here does not
                read as done."""
                screen = _settled(t, lambda sc: done(sc) or composer.switch_confirm(sc, to) is not None)
                answer = composer.switch_confirm(screen, to)
                if answer is None:
                    return screen
                if answer == "no":
                    t.down()
                    screen = _settled(t, lambda sc: composer.switch_confirm(sc, to) == "yes")
                    if composer.switch_confirm(screen, to) != "yes":
                        t.escape()
                        back = _composer(t, _settled(t, lambda sc: composer.read("claude", sc) is not None))
                        return out(CONTROL_UNCERTAIN, "the confirmation's cursor did not reach its Yes"
                                   + ("" if back is not None else ", and Esc did not bring the composer back"), **kw)
                t.confirm()
                # Until it is gone: the first read after Enter can still show the dialog.
                return _settled(t, lambda sc: done(sc) or composer.switch_confirm(sc, to) is None)

            if kind == "compact":
                t.literal("/compact ")
                screen = _settled(t, composer.compact_ready)
                if not composer.compact_ready(screen) or (still_offered is not None and not still_offered()):
                    why = ("the uplink that carried this control ended before it was applied"
                           if composer.compact_ready(screen) else f"/compact did not stand alone in the composer: {_foot(screen)}")
                    t.keys("C-u")
                    back = _composer(t, _settled(t, lambda sc: composer.read("claude", sc) is not None))
                    if back is None or not back.empty:
                        return out(CONTROL_UNCERTAIN, f"{why}, and C-u did not empty the composer")
                    return out(CONTROL_REFUSED, why)
                applied = True
                t.submit()
                # Only once Enter has emptied the composer: before that, an older compact's answer
                # above it would read as this one's.
                def answered(sc: str) -> bool:
                    now = _composer(t, sc)
                    return now is not None and now.empty and composer.compacted(sc) is not None

                screen = _waited(t, answered, COMPACT_WAIT_S)
                got = composer.compacted(screen) if answered(screen) else None
                if got is None:
                    return out(CONTROL_UNCERTAIN, f"no answer to /compact within {COMPACT_WAIT_S:.0f}s: {_foot(screen)}")
                if got[0] == "ok":
                    return out(CONTROL_TYPED, echo="Compacted")
                return out(CONTROL_REFUSED, f"compaction failed: {got[1]}")

            if kind == "model":
                t.literal("/model")
                t.submit()
                screen = _settled(t, lambda sc: composer.picker(sc) is not None)
                rows = composer.picker(screen)
                if rows is None:
                    return back_out(f"/model did not open the measured picker: {_foot(screen)}")
                match = [r for r in rows if composer.model_key(r.name) == target]
                if len(match) != 1:
                    return back_out(f"no row on the picker's screen is {target}" if not match
                                    else f"{len(match)} rows on the picker are {target}")
                row = match[0]
                if row.current:
                    return back_out(f"the session's model is already {row.name}")

                def cursor(sc: str) -> int | None:
                    drawn = composer.picker(sc)
                    return next((r.number for r in drawn if r.cursor), None) if drawn else None

                step = row.number - cursor(screen)
                t.keys(*(["Down"] if step > 0 else ["Up"]) * abs(step))
                screen = _settled(t, lambda sc: cursor(sc) == row.number)
                if cursor(screen) != row.number:
                    return back_out(f"the picker's cursor is on row {cursor(screen)}, not {row.number}")
                if still_offered is not None and not still_offered():
                    return back_out("the uplink that carried this control ended before it was applied")
                applied, name = True, row.name
                t.keys("s")
                # Matched as a prefix: the echo adds `with <level> effort` when the picker applies one.
                want = f"Set model to {row.name} for this session only"

                def said_model(sc: str) -> str | None:
                    said = composer.last_echo(sc)
                    return said[1][:300] if said is not None and said[0] == "/model" and said[1].startswith(want) else None

                screen = past_switch(row.name, lambda sc: said_model(sc) is not None, name=name)
                if isinstance(screen, dict):
                    return screen
                if said_model(screen) is not None:
                    return out(CONTROL_TYPED, echo=said_model(screen), name=name)
                if composer.switch_confirm(screen, row.name) is not None:
                    # Its Enter did not land: Esc'd back to the composer, as the picker is below,
                    # so no later wake or control refuses on a pane that shows no idle composer.
                    t.escape()
                    back = _composer(t, _settled(t, lambda sc: composer.read("claude", sc) is not None))
                    return out(CONTROL_UNCERTAIN, "s was pressed and the confirmation stayed; "
                               + ("Esc closed it" if back is not None else "Esc did not close it"), name=name)
                if composer.picker(t.screen()) is not None:
                    t.escape()
                    back = composer.read("claude", _settled(t, lambda sc: composer.read("claude", sc) is not None))
                    return out(CONTROL_UNCERTAIN, "s was pressed and the picker stayed; "
                               + ("Esc closed it" if back is not None else "Esc did not close it"), name=name)
                return out(CONTROL_UNCERTAIN, f"s was pressed and no echo followed: {_foot(screen)}", name=name)

            t.literal("/effort")
            t.submit()
            screen = _settled(t, lambda sc: composer.slider(sc) is not None)
            now = composer.slider(screen)
            if now is None:
                return back_out(f"/effort did not open the measured slider: {_foot(screen)}")
            if now == target:
                return back_out(f"the session's effort is already {target}")
            order = composer.stops(screen)
            step = order.index(target) - order.index(now)
            t.keys(*(["Right"] if step > 0 else ["Left"]) * abs(step))
            screen = _settled(t, lambda sc: composer.slider(sc) == target)
            if composer.slider(screen) != target:
                return back_out(f"the slider's ▲ reads {composer.slider(screen)}, not {target}")
            if still_offered is not None and not still_offered():
                return back_out("the uplink that carried this control ended before it was applied")
            applied = True
            t.keys("s")
            want = f"Set effort level to {target} (this session only)"

            def echoed(sc: str) -> str | None:
                said = composer.last_echo(sc)
                if said is not None and said[0] == "/effort" and said[1].startswith(want):
                    return said[1][:300]
                # The echo is not always drawn (#2267's live test on 2.1.282); the indicator is.
                # Before `s` it named another level, since a control to the level it has refuses.
                return f"the effort indicator reads {target}" if composer.indicator(sc) == target else None

            screen = past_switch(target, lambda sc: echoed(sc) is not None)
            if isinstance(screen, dict):
                return screen
            if echoed(screen) is not None:
                return out(CONTROL_TYPED, echo=echoed(screen))
            # A confirmation still up on that screen — ours whose Enter did not land, or one
            # naming another level, which is not the screen this control asked for — is Esc'd
            # back to the composer, as the slider below is, so no later wake or control refuses
            # on a pane that shows no idle composer. Ours falls to the read-back below; another
            # level's is `uncertain`.
            stuck = next((lv for lv in composer.LEVELS if composer.switch_confirm(screen, lv)), None)
            if stuck is not None:
                t.escape()
                back = _composer(t, _settled(t, lambda sc: composer.read("claude", sc) is not None))
                if back is None:
                    return out(CONTROL_UNCERTAIN, "the confirmation stayed, and Esc did not bring the composer back")
                if stuck != target:
                    return out(CONTROL_UNCERTAIN, f"s was pressed and the screen asked about {stuck}")
            # A slider still up means `s` never reached the harness, and a pane showing it is
            # no idle composer: every later wake and control would refuse, no turn would come,
            # and no reading would ever settle this. Esc on the slider changes nothing (§2.5).
            if composer.slider(t.screen()) is not None:
                t.escape()
                back = composer.read("claude", _settled(t, lambda sc: composer.read("claude", sc) is not None))
                return out(CONTROL_UNCERTAIN, "s was pressed and the slider stayed; "
                           + ("Esc closed it" if back is not None else "Esc did not close it"))
            # Neither drawn: the slider, opened again and closed with Esc, says where it is.
            now = _read_back(t)
            if now == target:
                return out(CONTROL_TYPED, echo=f"the slider reads {target} when opened again")
            if now is not None:
                return out(CONTROL_REFUSED, f"s was pressed and the slider still reads {now}")
            return out(CONTROL_UNCERTAIN, f"s was pressed and no echo followed: {_foot(screen)}")
    except Exception as e:
        why = str(e) if isinstance(e, Refused) else f"{type(e).__name__}: {e}"
        return out(CONTROL_UNCERTAIN if applied else CONTROL_REFUSED, why, **({"name": name} if name else {}))


def _read_back(t) -> str | None:
    """The level the `/effort` slider shows, opened at an idle empty composer and closed with Esc
    (§2.5: Esc on the slider changes nothing), or None when any step does not land."""
    screen = t.screen()
    before = _composer(t, screen)
    if before is None or not before.empty:
        if composer.slider(screen) is not None:
            t.escape()
        return None
    t.literal("/effort")
    t.submit()
    screen = _settled(t, lambda sc: composer.slider(sc) is not None)
    level = composer.slider(screen)
    # Esc whether or not it was read: a slider drawn after the last look would otherwise stay up,
    # and a pane showing it refuses every later wake and control (§2.5).
    t.escape()
    back = composer.read("claude", _settled(t, lambda sc: composer.read("claude", sc) is not None))
    return level if back is not None else None


TMUX_BIN = ("/opt/homebrew/bin/tmux", "/usr/local/bin/tmux", "/usr/bin/tmux")


def tmux_bin() -> str | None:
    import shutil
    return shutil.which("tmux") or next((b for b in TMUX_BIN if Path(b).exists()), None)


def pane_of(pid: int, *, run=subprocess.run, socket: str | None = None) -> tuple[str, str] | None:
    """(socket, pane) of the pane whose process is `pid` or an ancestor of it, or None. For a
    harness that does not hand `TMUX` down to what it runs. Without `socket`, the login's default
    server and then the platform's own (`launchers.SOCKET`), where every session the agent
    launches runs (#2267); a server on any other socket is not searched."""
    import launchers
    b = tmux_bin()
    if not b:
        return None
    chain = set(runtime_id._ancestors(pid))
    for server in ([["-S", socket]] if socket else [[], ["-L", launchers.SOCKET]]):
        try:
            r = run([b, *server, "list-panes", "-a", "-F", "#{pane_pid} #{pane_id} #{socket_path}"],
                    capture_output=True, text=True, timeout=5)
        except Exception:
            continue
        if r.returncode != 0:
            continue  # no server on this socket
        for line in r.stdout.splitlines():
            parts = line.split(" ", 2)
            if len(parts) == 3 and parts[0].isdigit() and int(parts[0]) in chain:
                return parts[2], parts[1]
    return None


class Tmux:
    """One pane, addressed by its socket and id. Every call names the socket: the agent runs
    under launchd with none of the terminal's `TMUX`, and a pane id is only unique within its
    server."""

    def __init__(self, socket: str, pane: str, run=subprocess.run):
        self.socket, self.pane_id, self._run = socket, pane, run
        self.bin = tmux_bin()

    def _tmux(self, *args: str, input: str | None = None) -> str:
        if not self.bin:
            raise Refused("no tmux on this machine")
        try:
            r = self._run([self.bin, "-S", self.socket, *args], input=input,
                          capture_output=True, text=True, timeout=10)
        except subprocess.TimeoutExpired:
            raise Refused(f"`tmux {args[0]}` did not return") from None
        if r.returncode != 0:
            raise Refused(f"`tmux {args[0]}` exited {r.returncode}: {r.stderr.strip() or 'no detail'}")
        return r.stdout

    def pane(self) -> tuple[int | None, bool]:
        out = self._tmux("display-message", "-p", "-t", self.pane_id, "#{pane_pid} #{pane_in_mode}")
        pid, _, mode = out.strip().partition(" ")
        return (int(pid) if pid.isdigit() else None), mode == "1"

    def screen(self) -> str:
        # With `-e`: the composer is read with its attributes, so a dim suggestion is not a draft.
        return self._tmux("capture-pane", "-p", "-e", "-t", self.pane_id)

    def cursor(self) -> tuple[int, int] | None:
        """The cursor's (column, row) on the pane, or None when tmux did not say."""
        out = self._tmux("display-message", "-p", "-t", self.pane_id, "#{cursor_x} #{cursor_y}").split()
        return (int(out[0]), int(out[1])) if len(out) == 2 and all(o.isdigit() for o in out) else None

    def stash(self) -> None:
        self._tmux("send-keys", "-t", self.pane_id, "C-s")

    def paste(self, text: str, buffer: str) -> None:
        """Bracketed, so a newline in the text is not an Enter (doc 118 §2.1–2.2); the buffer is
        deleted by the paste that uses it."""
        self._tmux("load-buffer", "-b", buffer, "-", input=text)
        self._tmux("paste-buffer", "-p", "-d", "-b", buffer, "-t", self.pane_id)

    def submit(self) -> None:
        self._tmux("send-keys", "-t", self.pane_id, "Enter")

    # A prompt's answer. Named apart from `submit` so a reader of a sequence sees which Enter
    # confirms a dialog and which sends text.
    def confirm(self) -> None:
        self._tmux("send-keys", "-t", self.pane_id, "Enter")

    def down(self) -> None:
        self._tmux("send-keys", "-t", self.pane_id, "Down")

    def keys(self, *names: str) -> None:
        self._tmux("send-keys", "-t", self.pane_id, *names)

    def literal(self, text: str) -> None:
        self._tmux("send-keys", "-t", self.pane_id, "-l", text)

    def escape(self) -> None:
        self._tmux("send-keys", "-t", self.pane_id, "Escape")


def _failed(typed: bool) -> str:
    """What a broken sequence was. `uncertain` never retries, so it is reserved for a run that
    may have put text in front of a session — everything before the first keystroke of the paste
    is a refusal, which can be tried again. Calling those uncertain made a missing `osascript`
    enough to leave a session permanently unwakeable."""
    return wake_states.UNCERTAIN if typed else wake_states.REFUSED


class Typist:
    """The only thing here that types. Every step refuses rather than carrying on half done: a
    sequence that continues past a failed step sends half a wake, and half a wake is a paid turn
    with no nonce in it."""

    def _osa(self, script: str, lang: str | None = None) -> str:
        cmd = ["osascript"] + (["-l", lang] if lang else []) + ["-e", script]
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
        except subprocess.TimeoutExpired:
            raise Refused("osascript timed out") from None
        if r.returncode != 0:
            raise Refused(f"osascript exited {r.returncode}: {r.stderr.strip() or 'no detail'}")
        return r.stdout.strip()

    def verify_app(self, pid: int, app_pid: int, udd: str, psession: str) -> None:
        if instance_of(pid) != (udd, app_pid):
            raise Refused("the target's process ancestry no longer names this VS Code instance")
        try:
            info = json.loads(self._osa(
                'ObjC.import("AppKit"); const app = $.NSRunningApplication.'
                f'runningApplicationWithProcessIdentifier({int(app_pid)}); '
                f'Number(app.processIdentifier) !== {int(app_pid)} || app.isTerminated ? "null" : '
                'JSON.stringify({bundle:ObjC.unwrap(app.bundleURL.path), '
                'exe:ObjC.unwrap(app.executableURL.path)})',
                lang="JavaScript"))
            bundle = Path(info["bundle"])
            product = json.loads((bundle / "Contents" / "Resources" / "app" / "product.json").read_text())
            valid = (Path(info["exe"]).parent == bundle / "Contents" / "MacOS"
                     and Path(info["exe"]).name == "Code"
                     and product.get("darwinBundleIdentifier") in
                     ("com.microsoft.VSCode", "com.microsoft.VSCodeInsiders"))
        except (OSError, ValueError, TypeError, KeyError):
            valid = False
        if not valid:
            raise Refused(f"app_pid {app_pid} is not a verified running VS Code main process")
        tab_for(udd, psession)

    def preflight(self) -> None:
        trusted = self._osa('ObjC.import("ApplicationServices"); $.AXIsProcessTrusted()',
                            lang="JavaScript")
        if trusted != "true":
            raise Refused("Accessibility is not granted to the signed 2mw2lt agent; enable it "
                          "in System Settings > Privacy & Security > Accessibility")

    def _code(self, cli: str, udd: str, *args: str) -> None:
        raise Refused("the CLI/URI tab wake route is retired pending the native route (#2183)")

    def focus_window(self, udd: str, cwd: str, *, cli: str = CODE) -> None:
        self._code(cli, udd, cwd)

    def activate_tab(self, udd: str, psession: str, *, cli: str = CODE) -> None:
        self._code(cli, udd, "--open-url", f"vscode://anthropic.claude-code/open?session={psession}")

    def step_focus(self) -> None:
        self._osa('tell application "System Events" to key code 124 using {command down, option down}')
        self._osa('tell application "System Events" to key code 123 using {command down, option down}')

    def frontmost_pid(self) -> int | None:
        out = self._osa('tell application "System Events" to return unix id of '
                        '(first application process whose frontmost is true)')
        return int(out) if out.isdigit() else None

    def take_pasteboard(self, text: str) -> dict:
        """Put `text` on the board and answer what is needed to give the board back. Only text
        survives `pbpaste`/`pbcopy`, so a board holding an image is not read: what it held is
        gone the moment the wake text goes on, and the choice left is what to leave behind."""
        count, types = self._board()
        restorable = bool(types) and set(types) <= _TEXT_TYPES
        saved = subprocess.run(["pbpaste"], capture_output=True, text=True,
                               timeout=10).stdout if restorable else None
        subprocess.run(["pbcopy"], input=text, text=True, timeout=10, check=True)
        mine, _ = self._board()
        return {"saved": saved, "restorable": restorable, "mine": mine, "was": count}

    def restore_pasteboard(self, held: dict) -> None:
        """Given back only if the wake's own value is still on it. Ownership by change count,
        not by comparing text: two identical strings are indistinguishable that way.

        A board that held something unreadable — an image, or nothing at all — is cleared
        rather than left holding the wake text. A board whose change count could not be read
        after the copy is left alone instead, because without it nothing distinguishes this
        wake's text from something the owner copied since."""
        now, _ = self._board()
        if held["mine"] is None or now != held["mine"]:
            return
        if held["restorable"]:
            subprocess.run(["pbcopy"], input=held["saved"], text=True, timeout=10, check=True)
        else:
            self._osa('ObjC.import("AppKit"); $.NSPasteboard.generalPasteboard.clearContents',
                      lang="JavaScript")

    def clear_composer(self) -> None:
        self._osa('tell application "System Events" to keystroke "a" using {command down}')
        self._osa('tell application "System Events" to key code 51')

    def paste(self) -> None:
        self._osa('tell application "System Events" to keystroke "v" using {command down}')

    def submit(self) -> None:
        self._osa('tell application "System Events" to key code 36')

    def _board(self) -> tuple[int | None, list[str]]:
        try:
            out = self._osa('ObjC.import("AppKit"); const p = $.NSPasteboard.generalPasteboard; '
                            'JSON.stringify([p.changeCount, ObjC.deepUnwrap(p.types)])',
                            lang="JavaScript")
            count, types = json.loads(out)
        except (Refused, ValueError):
            return None, []
        return int(count), list(types)


_TEXT_TYPES = {"public.utf8-plain-text", "public.plain-text", "NSStringPboardType",
               "public.utf16-external-plain-text"}
