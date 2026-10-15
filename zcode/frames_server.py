#!/usr/bin/env python3
"""The zcode plugin's frames server (#6257): the one long-lived child a zcode session gives a
plugin, holding the session's steering stream and serving the `frames` tool.

It is the Claude module's counterpart in a harness with no module API: `hold.py --wake plugin`
runs as this server's child and speaks the same one-line protocol; the holder claim, the
pending/read ledger and the ack-after-return rule are the module's, kept in
`steering-holders/` beside the claim (one holder location). Two things the module does that no
zcode plugin can: submit a prompt to wake the session (no such channel exists — the hooks point
at event boundaries instead), and refuse a subagent's tool call (MCP carries no caller identity;
the pointer is only ever spoken to the session's own context, and the ack still follows the
frames reaching the model). Measured ground: `spikes/zcode-glue/probe.py`, 2026-10-13.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path(os.environ.get("ZCODE_PLUGIN_ROOT") or Path(__file__).resolve().parents[1])
sys.path.insert(0, str(ROOT / "steering"))
sys.path.insert(0, str(ROOT / "steering" / "enroll"))

import holder  # noqa: E402
import zcode_binding  # noqa: E402
from local_workspace import workspace_root  # noqa: E402

PROTOCOL = "2025-06-18"
KEEP_S = 2.0          # the keeper's cadence: the connect watch and the stopped check
SESSION_S = 30.0      # how often a held session is asked it is still the enrolled one
REFRESH_S = 60.0      # the module's claim refresh
WAIT_FIRST, WAIT_MAX = 1.0, 60.0   # the module's backoff for a hold that ended
READ_KEPT = 256       # ids of read frames kept to recognise a replay by

TOOLS = [{
    "name": "frames",
    "description": "Read the steering frames waiting for this session, each with its "
                   "daemon-written from: line. Acknowledges the envelopes it returns.",
    "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
}]


def _client(argv: list[str], stdin: str | None = None, timeout: float = 10.0):
    """One steering client, run from the workspace the way the module runs them."""
    return subprocess.run(argv, capture_output=True, text=True, timeout=timeout, input=stdin)


def _version() -> str:
    try:
        named = json.loads((ROOT / ".claude-plugin" / "plugin.json").read_text()).get("version")
        return str(named) if isinstance(named, str) else ""
    except (OSError, ValueError):
        return ""


class Server:
    """Everything the session's glue owns: the binding, the claim, the hold child, the ledger."""

    def __init__(self, run=_client, now=time.time, spawn=None):
        self.run = run
        self.now = now
        # the hold child factory: argv -> (reader, lines); tests hand back a scripted child
        self._spawn = spawn or self._hold_child
        self.lock = threading.Lock()
        self.ws: Path | None = None          # the workspace of this session's cwd
        self.cwd: str | None = None          # the session's cwd, which its binding names
        self.psession: str | None = None     # the binding's session
        self.binding_error: str | None = None
        self.session = ""                    # the enrolment name, once connect mints one
        self.pending: list[str] = []
        self.read: list[str] = []
        self.hold_state: dict = {"state": "starting"}
        self.child_gen = 0
        self.stopping = threading.Event()
        self._last_claim = 0.0
        self._last_session_ask = 0.0
        self._last_connect_at: float | None = None

    # -- the ledger and the claim, one holder location -------------------------------

    def ledger_path(self) -> Path:
        return holder.claim_path(self.ws, self.psession).with_suffix(".ledger.json")

    def _write_ledger(self) -> None:
        path = self.ledger_path()
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps({
            "session": self.session, "pending": self.pending, "read": self.read[-READ_KEPT:],
            "hold": self.hold_state, "at": self.now()}))
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)

    def claim(self) -> None:
        """The holder claim, as the module writes it: live while this server is, carrying its
        own account of the hold for `holder.state`/`holder.describe` to read."""
        path = holder.claim_path(self.ws, self.psession)
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"provider_session": self.psession, "module": _version(),
                                   "at": self.now(), "hold": self.hold_state}))
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
        self._last_claim = self.now()

    def enrolment_name(self) -> str:
        """`hold.py --session-of <psession>`: the enrolment name connect minted, or ''."""
        asked = self.run(["python3", str(ROOT / "steering/enroll/hold.py"), "--session-of",
                          self.psession])
        return asked.stdout.strip() if asked.returncode == 0 else ""

    # -- the hold child ---------------------------------------------------------------

    def _hold_child(self, argv: list[str]):
        proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                text=True, cwd=self.cwd)
        return proc, lambda: iter(lambda: proc.stdout.readline(), "")

    def start_hold(self, gen: int) -> None:
        self.hold_state = {"state": "starting"}
        self.claim()
        self._write_ledger()
        argv = ["python3", str(ROOT / "steering/enroll/hold.py"), "--wake", "plugin",
                "--provider", "zcode", "--provider-session", self.psession]
        try:
            proc, lines = self._spawn(argv)
        except OSError as e:
            self.hold_state = {"state": "retrying", "attempt": 1,
                               "refusal": f"the hold child would not start: {e}"}
            self.claim()
            self._write_ledger()
            return
        threading.Thread(target=self._read_hold, args=(proc, lines, gen), daemon=True).start()

    def _read_hold(self, proc, lines, gen: int) -> None:
        for line in lines():
            if gen != self.child_gen or self.stopping.is_set():
                break
            try:
                noted = json.loads(line)
            except ValueError:
                continue
            if not isinstance(noted, dict) or not isinstance(noted.get("kind"), str):
                continue
            kind = noted["kind"]
            if kind == "held":
                self._note({"state": "holding", "hold": noted.get("hold"), "since": self.now()})
            elif kind == "refused":
                retry = bool(noted.get("retry"))
                self._note({"state": "retrying" if retry else "stopped",
                           "refusal": noted.get("why"),
                           **({} if retry else {"remedy": noted.get("remedy")
                                                 or "run /skill 2mw2lt:connect"})})
                if not retry:
                    return
            elif kind == "seat":
                self._seat_generation(noted)
            elif noted.get("wakes"):
                self._frame(noted)
        if gen != self.child_gen or self.stopping.is_set():
            return
        # a stream that ended is held again: the module reopens every transport failure
        self._note({"state": "retrying", "refusal": f"the hold ended ({proc.poll()})"})

    def _note(self, hold: dict) -> None:
        with self.lock:
            self.hold_state = hold
            self.claim()
            self._write_ledger()

    def _seat_generation(self, noted: dict) -> None:
        with self.lock:
            if isinstance(noted.get("id"), str) and isinstance(noted.get("generation"), int):
                self.read.append(noted["id"])
                self._write_ledger()

    def _frame(self, noted: dict) -> None:
        """A waking frame the session has neither been pointed at nor read joins the pending
        set (the module's `owed`); the hooks answer the pointer from the ledger."""
        fid = noted.get("id")
        with self.lock:
            if isinstance(fid, str) and fid not in self.pending and fid not in self.read:
                self.pending.append(fid)
                self._write_ledger()

    # -- the keeper: claim refresh, connect watch, hold lifecycle ---------------------

    def keeper(self) -> None:
        """The module's timing, as a poll: hold the claim, start the hold once connect mints a
        session, end it when the session is gone, retry the curable ends on the module's
        backoff. A stopped hold stays stopped: the session is told the remedy."""
        wait = WAIT_FIRST
        while not self.stopping.is_set():
            now = self.now()
            if now - self._last_claim >= REFRESH_S:
                try:
                    self.claim()
                except OSError:
                    pass
            state = self.hold_state.get("state")
            if state == "stopped":
                self.stopping.wait(KEEP_S)
                continue
            if not self.session:
                # the connect record changes without a subprocess: connect.py writes it the
                # moment it binds, and only then is a name worth asking for. A record that
                # never lands (a connect from before this server started, on a machine whose
                # holder directory was not there to take it) is still asked for, at the
                # session cadence, so the hold is late rather than never.
                at = holder.connected_at(self.ws, self.psession)
                fresh_record = at is not None and at != self._last_connect_at
                if fresh_record:
                    self._last_connect_at = at
                if fresh_record or (at is None and now - self._last_session_ask >= SESSION_S):
                    self._last_session_ask = now
                    named = self.enrolment_name()
                    if named:
                        with self.lock:
                            self.session = named
                        self.child_gen += 1
                        self.start_hold(self.child_gen)
                        wait = WAIT_FIRST
                self.stopping.wait(KEEP_S)
                continue
            if state == "retrying":
                self.child_gen += 1
                self.start_hold(self.child_gen)
                wait = min(wait * 2, WAIT_MAX)
                self.stopping.wait(wait)
                continue
            if now - self._last_session_ask >= SESSION_S:
                self._last_session_ask = now
                if self.enrolment_name() == "":
                    with self.lock:
                        self.session = ""
                        self.pending, self.read = [], []
                        self.child_gen += 1        # ends the reader; its lines stop counting
                        self.hold_state = {"state": "stopped", "refusal": "this session "
                                                "disconnected", "remedy": "run /skill 2mw2lt:connect"}
                        self.claim()
                        self._write_ledger()
                    continue
            self.stopping.wait(KEEP_S)

    # -- the frames tool ---------------------------------------------------------------

    def frames(self) -> str:
        """The module's `tool.call` for frames: the pending frames' text, their envelopes
        acked only once the text is on its way back, a refused ack leaving every frame
        pending to be returned — and acked — again."""
        with self.lock:
            pending, session = list(self.pending), self.session
        if not session or not pending:
            return "No steering frame is waiting."
        asked = self.run(["python3", str(ROOT / "steering/enroll/hold.py"),
                          "--frame-path", session])
        recording = ""
        if asked.returncode == 0:
            try:
                recording = Path(asked.stdout.strip()).read_text()
            except OSError:
                recording = ""
        text, envelopes, found, directives, generation = _pick(recording, pending)
        failures = []
        for ulid in envelopes:
            acked = self.run(["python3", str(ROOT / "steering/enroll/ack.py"), session, ulid])
            if acked.returncode != 0:
                failures.append(f"ack.py {ulid} exited {acked.returncode}")
        with self.lock:
            if not failures:
                self.pending = [i for i in self.pending if i not in found]
                self.read = (self.read + found)[-READ_KEPT:]
            self._write_ledger()
        if failures:
            return f"refused: foreground frame acknowledgement: {'; '.join(failures)}"
        if directives or generation is not None:
            body = json.dumps({"directives": directives,
                               **({"seat_generation": generation} if generation is not None
                                  else {})})
            posted = self.run(["python3", str(ROOT / "steering/enroll/lease.py"),
                               "--provider", "zcode", "--provider-session", self.psession,
                               "post", "/steering/seat/read"], stdin=body)
            if posted.returncode != 0:
                # The envelopes are acked, so the text is shown whatever the seat post said:
                # dropping it here would lose a directive nobody can return again.
                why = posted.stdout.strip() or posted.stderr.strip()
                return f"{text}\n\n(refused: foreground seat read: {why})" if text else \
                    f"refused: foreground seat read: {why}"
        return text or "No steering frame is waiting."

    # -- lifecycle ---------------------------------------------------------------------

    def start(self) -> bool:
        """Resolve the binding and claim; False when this process serves no zcode session —
        which it still answers, saying why, so the model is never left with a silent tool."""
        try:
            self.ws = workspace_root(Path.cwd(), timeout=2.0)
        except Exception:
            self.binding_error = "this directory is no steering workspace"
            return False
        try:
            self.cwd = os.getcwd()
            binding = zcode_binding.resolve(self.ws, cwd=self.cwd)
        except zcode_binding.Refused as why:
            self.binding_error = str(why)
            return False
        self.psession = binding["psession"]
        self.claim()
        threading.Thread(target=self.keeper, daemon=True).start()
        return True


def _pick(recording: str, pending: list[str]):
    """The recording's pending frames: their text, envelope ulids, found ids, the directives
    and seat generation the seat read carries.

    The twin of the module's `frames.ts pick` — the same recording fixtures fail both. The
    recording holds `data: {json}` lines, every frame carrying the daemon's `id`; an envelope
    contributes its ulid, a say its directive id, a seat its generation."""
    want = set(pending)
    out, envelopes, found, directives = [], [], [], []
    generation = None
    for raw in recording.splitlines():
        if not raw.startswith("data: "):
            continue
        try:
            frame = json.loads(raw[6:])
        except ValueError:
            continue
        if not isinstance(frame, dict) or frame.get("id") not in want:
            continue
        want.discard(frame.get("id"))
        found.append(frame["id"])
        out.append(raw[6:])
        if frame.get("kind") == "envelope" and isinstance(frame.get("ulid"), str):
            envelopes.append(frame["ulid"])
            directives.append(frame["ulid"])
        elif frame.get("kind") == "say" and isinstance(frame.get("directive"), str):
            directives.append(frame["directive"])
        elif frame.get("kind") == "seat" and isinstance(frame.get("generation"), int):
            generation = frame["generation"]
    return "\n".join(out), envelopes, found, directives, generation


def handle(method: str, params: dict, server: Server) -> dict:
    if method == "initialize":
        return {"protocolVersion": params.get("protocolVersion", PROTOCOL),
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "2mw2lt", "version": _version()}}
    if method == "ping":
        return {}
    if method == "tools/list":
        return {"tools": TOOLS}
    if method == "tools/call":
        if params.get("name") != "frames":
            return {"content": [{"type": "text", "text": f"no tool {params.get('name')}"}],
                    "isError": True}
        if server.psession is None:
            return {"content": [{"type": "text", "text": f"refused: this plugin serves no "
                                                        f"zcode session here: "
                                                        f"{server.binding_error}"}]}
        return {"content": [{"type": "text", "text": server.frames()}]}
    raise ValueError(method)


# How often a server that found no binding at its start looks again. The runtime starts the
# plugin's MCP server with the session, before any hook has published the binding (measured live,
# M4 2026-10-14: the first frames call refused "the binding … is unreadable"), so the start is
# retried until a hook has run.
RETRY_S = 3.0


def keep_starting(server: Server) -> None:
    """Start the server, retrying until a binding resolves or the session ends. A start that
    raises (an unreadable binding, a claim it cannot write) is retried like one that refused."""
    while True:
        try:
            if server.start():
                return
        except OSError as why:
            server.binding_error = f"the binding could not be read or claimed: {why}"
        if server.stopping.wait(RETRY_S):
            return


def main() -> int:
    server = Server()
    threading.Thread(target=keep_starting, args=(server,), daemon=True).start()
    for line in sys.stdin:
        if not line.strip():
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        if "method" not in msg or msg.get("id") is None:   # a notification is never answered
            continue
        try:
            result = handle(msg["method"], msg.get("params") or {}, server)
            answer = {"jsonrpc": "2.0", "id": msg["id"], "result": result}
        except ValueError:
            answer = {"jsonrpc": "2.0", "id": msg["id"],
                      "error": {"code": -32601, "message": f"no method {msg['method']}"}}
        sys.stdout.write(json.dumps(answer) + "\n")
        sys.stdout.flush()
    server.stopping.set()
    return 0


if __name__ == "__main__":
    sys.exit(main())
