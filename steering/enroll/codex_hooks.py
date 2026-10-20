"""Whether Codex will run this plugin's hooks, as Codex itself answers it (#7877).

Codex runs a plugin's hook only once a person has trusted its definition, and on newer builds the
whole plugin's contents too, so an update can put a trusted hook back to review. Codex computes
those hashes; this asks `codex app-server` for `hooks/list` rather than computing them again.
"""
from __future__ import annotations

import json
import queue
import subprocess
import threading
import time
from pathlib import Path

PLUGIN = "2mw2lt@2mw2lt"
TIMEOUT = 10.0
REVIEW = "open /hooks in Codex and trust them"
SCRIPTS = ("codex_observe.py", "delete_guard.py", "trap_guard.py")


def listed(cwd: Path, codex: str = "codex", timeout: float = TIMEOUT) -> dict:
    """The `hooks/list` entry for cwd, from a private `codex app-server` that is stopped after."""
    try:
        server = subprocess.Popen([codex, "app-server"], cwd=cwd, stdin=subprocess.PIPE,
                                  stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    except OSError as e:
        raise RuntimeError(f"codex could not start: {e}") from e
    lines: queue.Queue[str | None] = queue.Queue()

    def read() -> None:
        for line in server.stdout:
            lines.put(line)
        lines.put(None)

    threading.Thread(target=read, daemon=True).start()
    deadline = time.monotonic() + timeout

    def call(n: int, method: str, params: dict) -> dict:
        server.stdin.write(json.dumps({"id": n, "method": method, "params": params}) + "\n")
        server.stdin.flush()
        while True:
            try:
                line = lines.get(timeout=max(0.0, deadline - time.monotonic()))
            except queue.Empty:
                raise RuntimeError(f"codex app-server did not answer {method} within {timeout:g}s") from None
            if line is None:
                raise RuntimeError(f"codex app-server ended before answering {method}")
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            if msg.get("id") == n:
                if "error" in msg:
                    raise RuntimeError(f"{method}: {msg['error'].get('message', msg['error'])}")
                return msg["result"]

    try:
        call(1, "initialize", {"clientInfo": {"name": "2mw2lt-connect", "version": "0"}})
        data = call(2, "hooks/list", {"cwds": [str(cwd)]})["data"]
    except (OSError, KeyError, TypeError) as e:
        raise RuntimeError(f"codex app-server answered unexpectedly: {e}") from e
    finally:
        server.kill()
        server.wait()
    if len(data) != 1:
        raise RuntimeError(f"hooks/list answered {len(data)} entries for one directory")
    return data[0]


def _name(hook: dict) -> str:
    command = str(hook.get("command") or "")
    return next((s for s in SCRIPTS if s in command), f"the {hook.get('eventName', 'unnamed')} hook")


def line(entry: dict, update: str) -> tuple[str, bool]:
    """(the `hooks:` line, whether it is a warning) for one `hooks/list` entry."""
    ours = [h for h in entry.get("hooks") or [] if h.get("pluginId") == PLUGIN]
    problems = [str(w) for w in (entry.get("warnings") or []) + [e.get("message", "") for e in entry.get("errors") or []]
                if "/2mw2lt/" in str(w)]
    if not ours:
        if problems:
            return f"hooks: Codex loads none of the plugin's hooks ({problems[0]}); update it: {update}", True
        return "hooks: Codex lists none of the plugin's hooks; is the plugin enabled?", True
    off = [h for h in ours if not h.get("enabled", True)]
    held = [h for h in ours if h.get("enabled", True) and h.get("trustStatus") not in ("trusted", "managed")]
    if not off and not held and not problems:
        return f"hooks: Codex runs the plugin's {len(ours)} hooks", False
    parts = []
    if held:
        parts.append(", ".join(f"{_name(h)} ({h.get('trustStatus')})" for h in held) + f": {REVIEW}")
    if off:
        parts.append(", ".join(_name(h) for h in off) + " turned off in /hooks")
    if problems:
        parts.append(f"Codex warns: {problems[0]}")
    return f"hooks: Codex runs {len(ours) - len(held) - len(off)} of the plugin's {len(ours)} hooks; " + "; ".join(parts), True


def report(cwd: Path, update: str, codex: str = "codex") -> tuple[str, bool]:
    """Never raises: the line is a report, and an answer of a shape Codex has not sent before must
    not fail the connect it rides on."""
    try:
        return line(listed(cwd, codex), update)
    except Exception as e:  # noqa: BLE001
        return f"hooks: could not ask Codex whether it runs the plugin's hooks: {type(e).__name__}: {e}", True
