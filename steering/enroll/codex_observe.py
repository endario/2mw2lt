#!/usr/bin/env python3
"""Admit a completed Codex turn from the plugin's native Stop hook."""
from __future__ import annotations

import hashlib
import json
import math
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

import codex_probe as codex  # noqa: E402
import runtime_id  # noqa: E402
import spool  # noqa: E402
import supervision  # noqa: E402
import verb_help  # noqa: E402
from local_workspace import workspace_root  # noqa: E402


LOOKUP_TIMEOUT = 0.5
ROLLOUT_HEAD = 256 * 1024
ROLLOUT_TAIL = 1024 * 1024
MARKER_CAP = 20
MARKER_DIR = "steering-hook-failures"


def _terminal(path: Path, session: str, turn: str) -> tuple[str, str] | None:
    """The rollout writer version and stable completion instant for this exact turn."""
    version = None
    completed = None
    try:
        size = path.stat().st_size
        with path.open("rb") as f:
            head = f.read(ROLLOUT_HEAD)
            if size <= ROLLOUT_HEAD:
                tail = b""
            else:
                f.seek(max(0, size - ROLLOUT_TAIL))
                if f.tell():
                    f.readline()
                tail = f.read()
    except OSError:
        return None
    for block in (head, tail):
        for raw in block.splitlines():
            if b'"session_meta"' not in raw and b'"task_complete"' not in raw:
                continue
            try:
                rec = json.loads(raw)
            except ValueError:
                continue
            payload = rec.get("payload")
            if not isinstance(payload, dict):
                continue
            if rec.get("type") == "session_meta" and payload.get("session_id") == session:
                candidate = payload.get("cli_version")
                if isinstance(candidate, str) and candidate:
                    version = candidate[:200]
            elif (rec.get("type") == "event_msg" and payload.get("type") == "task_complete"
                  and payload.get("turn_id") == turn):
                candidate = payload.get("completed_at")
                if isinstance(candidate, (int, float)) and not isinstance(candidate, bool):
                    try:
                        instant = float(candidate)
                        if math.isfinite(instant):
                            completed = time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                                      time.gmtime(instant))
                    except (OverflowError, OSError, ValueError):
                        completed = None
                if completed is None:
                    candidate = rec.get("timestamp")
                    if isinstance(candidate, str) and candidate.endswith("Z"):
                        try:
                            instant = datetime.fromisoformat(candidate[:-1] + "+00:00")
                            completed = instant.astimezone(timezone.utc).strftime(
                                "%Y-%m-%dT%H:%M:%SZ")
                        except ValueError:
                            completed = None
    return (version, completed) if version and completed else None


def reduce(hook: dict, *, parent_pid: int | None = None,
           serving_pid=codex.serving_pid, derive=runtime_id.derive) -> tuple[dict | None, str | None]:
    """Map one supported hook payload to its observation, or a fixed refusal reason."""
    if hook.get("hook_event_name") != "Stop":
        return None, "unsupported-event"
    session, turn, named = hook.get("session_id"), hook.get("turn_id"), hook.get("transcript_path")
    if not all(isinstance(v, str) and v for v in (session, turn, named)):
        return None, "payload-invalid"
    transcript = Path(named)
    terminal = _terminal(transcript, session, turn)
    if terminal is None:
        return None, "terminal-record-missing"
    pid = parent_pid or os.getppid()
    try:
        writer = serving_pid(transcript, timeout=LOOKUP_TIMEOUT)
    except Exception:
        writer = None
    if writer is not None and writer != pid:
        return None, "runtime-identity-ambiguous"
    try:
        runtime = derive("codex", pid=pid, timeout=LOOKUP_TIMEOUT)
    except Exception:
        runtime = None
    if not runtime:
        return None, "runtime-identity-missing"
    version, observed_at = terminal
    o = {"v": 4, "source": "codex", "source_version": version, "session": session,
         "epoch": None, "event": "Stop", "occurrence": f"codex:{session}:{turn}:Stop",
         "observed_at": observed_at, "prompt_id": turn, "runtime_id": runtime}
    reply = supervision.head(hook.get("last_assistant_message"))
    if reply:
        o["reply_head"] = reply
    return o, None


def _mark(plugin_data: Path, session: object, turn: object, reason: str) -> Path:
    directory = plugin_data / MARKER_DIR
    spool.ensure_dir(directory)
    sid = session if isinstance(session, str) and session else "unknown-session"
    tid = turn if isinstance(turn, str) and turn else "unknown-turn"
    name = hashlib.sha256(f"{sid}\0{tid}".encode()).hexdigest() + ".json"
    final = directory / name
    spool.write_atomic(final, json.dumps({"v": 1, "session": sid, "turn": tid, "reason": reason},
                                         sort_keys=True))
    dated = []
    for path in directory.glob("*.json"):
        try:
            dated.append((path.stat().st_mtime_ns, path.name, path))
        except FileNotFoundError:
            continue
    markers = [row[2] for row in sorted(dated, reverse=True)]
    for old in markers[MARKER_CAP:]:
        old.unlink(missing_ok=True)
    if len(markers) > MARKER_CAP:
        spool.fsync_dir(directory)
    return final


def _plugin_data() -> Path | None:
    named = os.environ.get("PLUGIN_DATA")
    if named:
        return Path(named)
    try:
        return Path.cwd() / ".claude"
    except OSError:
        return None


def handle(hook: dict, *, workspace: Path | None = None, plugin_data: Path | None = None,
           parent_pid: int | None = None, serving_pid=codex.serving_pid,
           derive=runtime_id.derive) -> dict:
    """Durably admit a Stop and return the Codex hook output."""
    o, why = reduce(hook, parent_pid=parent_pid, serving_pid=serving_pid, derive=derive)
    session, turn = hook.get("session_id"), hook.get("turn_id")
    if why in {"unsupported-event", "payload-invalid"} or not (
            isinstance(session, str) and session and isinstance(turn, str) and turn):
        return {}
    if why is None and o is not None:
        try:
            named_cwd = hook.get("cwd")
            cwd = Path(named_cwd) if isinstance(named_cwd, str) and named_cwd else Path.cwd()
            ws = workspace or workspace_root(cwd, timeout=0.5)
            if spool.admit(ws, o) is not None:
                return {}
            why = "spool-full"
        except Exception:
            why = "spool-admission-failed"
    root = plugin_data or _plugin_data()
    if root is None:
        return {"systemMessage": "2mw2lt did not record this Codex turn end or its diagnostic"}
    try:
        _mark(root, session, turn, why or "unknown")
        message = "2mw2lt did not record this Codex turn end; diagnostic saved"
    except Exception:
        message = "2mw2lt did not record this Codex turn end or its diagnostic"
    return {"systemMessage": message}


def main() -> int:
    argv = sys.argv[1:]
    if verb_help.help_requested("codex_observe", argv, bare=False):
        print(verb_help.script_help("codex_observe")); return 0
    if argv:
        return verb_help.error("codex_observe", f"unexpected arguments: {' '.join(argv)}")
    try:
        hook = json.load(sys.stdin)
    except ValueError:
        hook = {}
    if not isinstance(hook, dict):
        hook = {}
    try:
        out = handle(hook)
    except Exception:
        session, turn = hook.get("session_id"), hook.get("turn_id")
        if isinstance(session, str) and session and isinstance(turn, str) and turn:
            try:
                root = _plugin_data()
                if root is None:
                    raise OSError("no diagnostic directory")
                _mark(root, session, turn, "hook-failed")
                out = {"systemMessage":
                       "2mw2lt did not record this Codex turn end; diagnostic saved"}
            except Exception:
                out = {"systemMessage":
                       "2mw2lt did not record this Codex turn end or its diagnostic"}
        else:
            out = {}
    sys.stdout.write(json.dumps(out) + "\n")
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
