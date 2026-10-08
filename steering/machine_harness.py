"""Harness metadata and machine discovery, independent of native execution authority."""
from __future__ import annotations

import functools
import glob
import json
import os
import re
import shlex
import shutil
import subprocess
import time
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import runtime_id
from codex_probe import Refused


CLI_DIRS = (Path.home() / ".local" / "bin", Path("/opt/homebrew/bin"))
MIN_CLI = (0, 153)
_VERSIONS: dict[str, tuple[int, ...] | None] = {}


_SYSTEM_BIN = ("/usr/bin", "/bin", "/usr/sbin", "/sbin")


@functools.cache
def _developer_bin() -> str | None:
    """Where macOS's developer tools really are, asked outside any cage. `/usr/bin/git` and
    `/usr/bin/python3` are xcrun shims: caged, they cannot write their cache in the per-user
    temporary directory, and the owner is shown the dialog offering to install the tools (#2418)."""
    import subprocess
    try:
        p = subprocess.run(["/usr/bin/xcode-select", "-p"], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return None
    d = Path(p.stdout.strip()) / "usr" / "bin"
    return str(d) if p.returncode == 0 and p.stdout.strip() and d.is_dir() else None


def _cli_path() -> str:
    """The environment's PATH, with where the CLIs install themselves and the developer tools'
    own directory ahead of the system's. The agent runs under launchd, whose PATH is the system's
    alone: measured on 2026-09-18, both agents reported no judge at all while `claude-glm` and
    `codex` sat in `~/.local/bin`; and there a bare `git` or `python3` is the xcrun shim."""
    have = (os.environ.get("PATH") or os.defpath).split(os.pathsep)
    prefer = [str(d) for d in (*CLI_DIRS, _developer_bin()) if d]
    rest = [d for d in have if d not in prefer]
    at = next((i for i, d in enumerate(rest) if d in _SYSTEM_BIN), len(rest))
    return os.pathsep.join([*rest[:at], *prefer, *rest[at:]])


def codex_cli(named: str | None = None) -> str:
    """The codex binary to run, or `Refused` naming every place that was looked.

    A refusal here is read by whoever is deploying the machine, not by the session, so it says
    where it looked rather than that it failed: "codex not found" with nothing after it sends
    the operator to install what is already there.
    """
    override = named or os.environ.get("STEERING_CODEX_CLI") or ""
    if override:
        # Named outright, so a fallback would run a different binary than the one asked for.
        found = shutil.which(override)
        if found:
            return found
        raise Refused(f"codex: {override} was named but is not an executable this can run")
    tried = [f"PATH ({os.environ.get('PATH') or 'empty'})"]
    found = shutil.which("codex")
    if found:
        return found
    for d in CLI_DIRS:
        c = d / "codex"
        tried.append(str(c))
        # `is_file` as well as the mode: `os.access` answers X_OK for a directory too, and
        # taking one would end the search on something that cannot run.
        if c.is_file() and os.access(c, os.X_OK):
            return str(c)
    raise Refused("codex not found: looked on " + ", ".join(tried)
                  + "; STEERING_CODEX_CLI names it outright")


def codex_cli_version(path: str) -> tuple[int, ...] | None:
    """`codex --version` as numbers — it prints `codex-cli 0.153.4` — or None when it cannot
    be read at all.

    None rather than a refusal, because a version that would not come back is not evidence of
    a version below the floor: refusing on it would turn a CLI that merely does not answer
    `--version` into a machine that delivers nothing.

    An answer at or above the floor is cached per path, and one below it is not: the binary is
    reached through a stable path whose target is replaced in place, so a cached refusal would
    outlive the upgrade its message asks for.
    """
    if path in _VERSIONS:
        return _VERSIONS[path]
    v: tuple[int, ...] | None = None
    try:
        r = subprocess.run([path, "--version"], capture_output=True, text=True, timeout=10.0)
    except (OSError, subprocess.SubprocessError):
        r = None
    if r is not None and r.returncode == 0:
        line = ((r.stdout or r.stderr).strip().splitlines() or [""])[0]
        word = (line.split() or [""])[-1]
        try:
            v = tuple(int(x) for x in word.split(".")[:3])
        except ValueError:
            v = None
    if v is None or v >= MIN_CLI:
        _VERSIONS[path] = v
    return v


def usable_codex_cli(named: str | None = None) -> str:
    """The binary this module will run: resolved, and not one it can see is too old."""
    path = codex_cli(named)
    v = codex_cli_version(path)
    if v is not None and v < MIN_CLI:
        raise Refused(f"codex at {path} is {'.'.join(map(str, v))}, below the "
                      f"{'.'.join(map(str, MIN_CLI))} that `queue --thread` needs")
    return path


def opencode_cli() -> str:
    """The binary, named outright or found where it installs: a launchd job has no PATH (#1179)."""
    named = os.environ.get("STEERING_OPENCODE_CLI")
    found = shutil.which(named or "opencode", path=None if named else _cli_path())
    if not found:
        raise RuntimeError("opencode not found: STEERING_OPENCODE_CLI names it outright")
    return found


def opencode_version() -> str | None:
    try:
        import subprocess
        out = subprocess.run([opencode_cli(), "--version"], capture_output=True, text=True, timeout=10).stdout
    except Exception:
        return None
    return out.strip().splitlines()[0] if out.strip() else None


GOOSE_CLI = os.environ.get("STEERING_GOOSE_CLI", "goose")


def goose_version() -> str | None:
    try:
        binary = shutil.which(GOOSE_CLI, path=_cli_path())
        if not binary:
            return None
        out = subprocess.run([binary, "--version"], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout.strip() or None


@dataclass(frozen=True)
class Harness:
    provider: str
    # The prefix an `enroll:` line names this harness by, which `enroll.provider_of` reads back.
    # A session enrolled under another harness's prefix is refused its own `bind:`, because the
    # door compares the enrolment's provider against the runtime id's.
    agent: str = ""
    # Which session this process is running inside, when the harness tells it. A harness that
    # does not needs the id passed, and it cannot be guessed at: a guessed account was #139 and
    # a guessed session would bind one session's work to another.
    session_id: Callable[[], str | None] | None = None
    # The harness process behind a session of this provider, asked of its session id, and what
    # a runtime identity is derived from. Separate from `session_id` because a harness can name
    # its session without naming the process serving it.
    harness_pid: Callable[[str], int | None] | None = None
    # Its default configuration directory under $HOME, and the variable that moves it. Which
    # directory is in use is which account this is (`enroll.account_of`).
    config: str = ""
    config_env: str = ""
    # Where under that directory this harness writes transcripts — `projects` for Claude,
    # `sessions` for the other two. A relative path rather than one name, because a harness
    # that keeps them further down (doc 49 measured one at `cli/rollout`) is otherwise a
    # harness this door cannot be told about. Empty where none are written.
    transcript_root: str = ""
    # Discovery advertises delivery; executable callbacks belong to the native adapter.
    delivery: str = "stream"
    # Where this harness writes the session's transcript, asked of its provider session. An
    # `enroll:` line names one, and the door validates it against the harness roots it knows.
    transcript: Callable[[str], object] | None = None
    # Whether a path relative to a harness root is one of this harness's transcripts, and the
    # session id that path names. `(False, None)` when it is not one of them.
    shape: Callable[[Path], tuple[bool, str | None]] | None = None
    # Why a transcript does not belong to the workspace named, or None when it does. Called with
    # the descriptor the door has already opened and the path relative to the root, because the
    # guarantee is that the workspace is proved from that handle and not from a second lookup.
    #
    # A harness that cannot tell has no callable here, and enrolment refuses it. That is the
    # deliberate answer rather than a gap: doc 49 measured a harness whose transcript names no
    # workspace in any key of its own, and admitting one on other evidence widens what the door
    # accepts, which is the owner's to decide and not this seam's.
    workspace: Callable[[int, Path, str], str | None] | None = None
    # Whether this harness's own hooks are installed in the workspace, and so whether a
    # connect has anything to repin (#340). Only the harness that produces observations
    # through workspace hooks has.
    hooks: bool = False
    # What this harness calls itself and at what version, which an observation carries as its
    # `source_version`. Read from the harness itself rather than written down here.
    version: Callable[[], str | None] | None = None
    # The reading of a session of this provider, taken by the agent on the session's machine from
    # the harness's own record, asked of its provider session. A held harness is asked with the
    # config directory its stream states, since the agent runs outside the session's environment.
    # What this attests is the model in the harness's own record, read from outside the session.
    # It does not hold against a session that forges its own transcript.
    reading: Callable[..., dict | None] | None = None
    # Whether a process currently loads this provider session. A harness whose sessions hold a
    # stream needs none of this: the stream ending is what says the session went. Hookless
    # harnesses also use it when proving which process supplies the runtime identity.
    loaded: Callable[[str], bool] | None = None
    # Whether an admitted injection destination remains valid. Usually that is the same as a
    # loaded process. Codex is different: its queue accepts an idle thread after the writer
    # closes; its process discovery separately asks for the current writer.
    reachable: Callable[[str], bool] | None = None
    # Census callbacks keep exact command and provider registry interpretation at this seam.
    process_kind: Callable[[list[str]], str | None] | None = None
    process_names: Callable[[], tuple[str, ...]] | None = None
    live_workers: Callable[..., tuple[list[dict], list[str]]] | None = None

    @property
    def exports(self) -> bool:
        """This harness tells its own process which session it is, so nothing has to be typed."""
        return self.session_id is not None

    def whoami(self) -> tuple[str | None, int | None]:
        """(session, harness pid) of the process this runs inside, as far as the harness says."""
        from process_probe import Undetermined
        sid = self.session_id() if self.session_id is not None else None
        try:
            pid = self.harness_pid(sid) if sid and self.harness_pid is not None else None
        except Undetermined:
            pid = None  # not said, which is what this answers for; the witness asks again and refuses
        return sid, pid

    def config_dir(self) -> Path:
        """The configuration directory this harness is using, which is which account it is."""
        moved = os.environ.get(self.config_env, "").strip() if self.config_env else ""
        return Path(moved) if moved else Path.home() / self.config

    @property
    def holds(self) -> bool:
        """A session of this provider opens a stream and holds it, so a directive is written
        into the stream and nothing carries it. Codex is the measured exception (doc 47 §2)."""
        return self.delivery == "stream"


def _env(*names: str):
    def read() -> str | None:
        for n in names:
            v = os.environ.get(n, "").strip()
            if v:
                return v
        return None
    read.names = names
    return read


def _claude_pid(_psession: str) -> int | None:
    import runtime_id
    v = os.environ.get("CLAUDE_PID", "").strip()
    return int(v) if v.isdigit() and runtime_id.process_start(int(v)) else None


def _codex_rollout(psession: str):
    # The directory is resolved here and handed over on every call. `codex` reads its own home
    # once at import, so a caller that moves `CODEX_HOME` afterwards would be answered about
    # the directory that was in use when something first imported it.
    import codex_probe as codex
    return codex.rollout(psession, HARNESSES["codex"].config_dir())


def _codex_pid(psession: str) -> int | None:
    """The process serving the thread, by the writing-descriptor rule of doc 47 §5. Codex names
    its thread in the environment and not the process behind it, and a sandboxed session cannot
    run `ps` to look, so the descriptor is what answers."""
    import codex_probe as codex
    path = _codex_rollout(psession)
    return codex.serving_pid(path) if path is not None else None


def _cli_version(*argv: str):
    def read() -> str | None:
        import subprocess
        try:
            binary = codex_cli() if argv[0] == "codex" else shutil.which(argv[0], path=_cli_path())
            if not binary:
                return None
            out = subprocess.run([binary, *argv[1:]], capture_output=True, text=True, timeout=5.0)
        except (Refused, OSError, subprocess.SubprocessError):
            return None
        line = (out.stdout or out.stderr).strip().splitlines()
        return line[0].strip()[:200] if line and out.returncode == 0 else None
    return read


def _codex_transcript(psession: str):
    return _codex_rollout(psession)


def claude_transcript_path(config: Path, psession: str, project: Path | None = None) -> Path | None:
    """The directory the session runs in names its folder; failing that, every project folder of
    `config` is searched by the session id alone, because Claude Code files a transcript under the
    directory the session was launched in, which a caller in a linked worktree is not in."""
    if project is not None:
        p = config / "projects" / claude_project_dir(str(project)) / f"{psession}.jsonl"
        if p.exists():
            return p
    return next((config / "projects").glob(f"*/{glob.escape(psession)}.jsonl"), None)


def _claude_transcript(psession: str):
    config = Path(os.environ.get("CLAUDE_CONFIG_DIR") or (Path.home() / ".claude"))
    return claude_transcript_path(config, psession, Path(os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()))


def _claude_reading(psession: str, config: str | None = None) -> dict | None:
    """What the session's transcript under `config` says it last ran, stated as of the entry that
    says it, so an unchanged transcript reads the same at every poll. `Refused` says why there is
    none."""
    import vitals
    if not config:
        raise Refused("the session's stream named no Claude config directory")
    path = claude_transcript_path(Path(config), psession)
    if path is None:
        raise Refused(f"no transcript of {psession} under {config}")
    rec = vitals.of_entry(vitals.last_assistant(vitals.tail(path)), {"session_id": psession}, restated=True)
    if rec is None:
        raise Refused(f"no assistant entry with an instant in {path}")
    return rec


def _codex_reading(psession: str) -> dict | None:
    """What the thread's own rollout says it is (doc 47 §4). Codex runs no hook, so nothing is
    published from inside the session and the agent on its machine reads it from outside."""
    import codex_probe as codex
    import vitals
    path = _codex_rollout(psession)
    return vitals.stated("codex", codex.reading(path, psession) if path is not None else None)


def _codex_loaded(psession: str) -> bool:
    """A thread is loaded while its writer is serving it, including the bounded gap (#677)."""
    import codex_probe as codex
    path = _codex_rollout(psession)
    return path is not None and codex.still_served(path, psession)


def _codex_reachable(psession: str) -> bool:
    """An idle thread remains a queue destination while its rollout is on this machine and an
    app-server is there to drain the queue (#1967)."""
    import codex_probe as codex
    return _codex_rollout(psession) is not None and codex.app_server()


_SESSION_ID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I)


def _claude_shape(rel: Path) -> tuple[bool, str | None]:
    """`projects/<encoded cwd>/<session>.jsonl`. A stem that is not a session id is still one
    of ours — the file is observable and simply names no identity (doc 28 §5)."""
    if len(rel.parts) != 2:
        return False, None
    return True, rel.stem if _SESSION_ID.fullmatch(rel.stem) else None


def _codex_shape(rel: Path) -> tuple[bool, str | None]:
    """`sessions/YYYY/MM/DD/rollout-<ISO timestamp>-<id>.jsonl`. The timestamp's own groups are
    too short to match, so the id is the only span of this shape in the name."""
    if not (len(rel.parts) == 4 and rel.parts[3].startswith("rollout-")):
        return False, None
    m = _SESSION_ID.search(rel.stem)
    return True, m.group(0) if m else None


def _grok_shape(rel: Path) -> tuple[bool, str | None]:
    """`sessions/<url-encoded cwd>/<session>/updates.jsonl`."""
    if not (len(rel.parts) == 3 and rel.parts[2] == "updates.jsonl"):
        return False, None
    return True, rel.parts[1] if _SESSION_ID.fullmatch(rel.parts[1]) else None


def claude_project_dir(workspace: str) -> str:
    """Claude keys a session's transcripts by the directory it runs in, with the separators
    flattened. One implementation: `enroll.encoded_cwd` reads it back, and the encoding is
    Claude's layout rather than anything steering chooses."""
    return workspace.replace("/", "-")


def _claude_workspace(_fd: int, rel: Path, workspace: str) -> str | None:
    encoded = claude_project_dir(workspace)
    if rel.parts[0] != encoded:
        return f"transcript is not under the enrolled workspace's project directory {encoded}"
    return None


def _codex_workspace(fd: int, _rel: Path, workspace: str) -> str | None:
    """A rollout's path names no workspace; its first line (session_meta) does."""
    try:
        with os.fdopen(os.dup(fd), "rb") as f:
            head = f.readline(65536)
        cwd = json.loads(head).get("payload", {}).get("cwd")
    except (OSError, ValueError, AttributeError):
        return "rollout has no readable session_meta"
    if cwd != workspace:
        return f"rollout belongs to workspace {cwd!r}, not {workspace!r}"
    return None


def _grok_workspace(_fd: int, rel: Path, workspace: str) -> str | None:
    named = urllib.parse.unquote(rel.parts[0])
    if named != workspace:
        return f"grok session belongs to workspace {named!r}, not {workspace!r}"
    return None


def _goose_pid(psession: str) -> int | None:
    import worker_probe
    return worker_probe.goose_pid_of(psession)


def _goose_loaded(psession: str) -> bool:
    import worker_probe
    return worker_probe.goose_loaded(psession)


def _goose_transcript(psession: str):
    import worker_probe
    return worker_probe.goose_path_of(psession)


def _goose_shape(rel):
    import worker_probe
    return worker_probe.shape(rel)


def _goose_workspace(fd: int, rel, workspace: str) -> str | None:
    import worker_probe
    return worker_probe.goose_workspace_of(fd, rel, workspace)


def _opencode_pid(psession: str) -> int | None:
    import worker_probe
    return worker_probe.opencode_pid_of(psession)


def _opencode_loaded(psession: str) -> bool:
    import worker_probe
    return worker_probe.opencode_loaded(psession)


def _opencode_transcript(psession: str):
    import worker_probe
    return worker_probe.opencode_path_of(psession)


def _opencode_workspace(fd: int, rel, workspace: str) -> str | None:
    import worker_probe
    return worker_probe.opencode_workspace_of(fd, rel, workspace)


def _goose_reading(psession: str) -> dict | None:
    """What the worker's own handshake answered (doc 58 §4). Goose runs no hook, and unlike a
    Codex rollout there is nothing on disk to read it from, so only the process running the
    worker can answer and the census asks the agent that launched it."""
    import worker_probe
    return worker_probe.goose_reading(psession)


CENSUS_REGISTRY_LIMIT = 256
CENSUS_RECORD_BYTES = 65536
CENSUS_BUDGET_SECONDS = 5.0


class CensusBudgetExhausted(TimeoutError):
    """The local worker sample exhausted its cooperative elapsed budget."""


def census_deadline() -> float:
    return time.monotonic() + CENSUS_BUDGET_SECONDS


def check_census_budget(deadline: float) -> None:
    # Cooperative: the call already in progress is not preempted.
    if time.monotonic() >= deadline:
        raise CensusBudgetExhausted("worker census sampling budget exhausted")


def _command(argv: list[str], binary: str | tuple[str, ...], node_script: str = "") -> list[str] | None:
    if not argv:
        return None
    if Path(argv[0]).name in ((binary,) if isinstance(binary, str) else binary):
        return argv[1:]
    if (Path(argv[0]).name in ("node", "nodejs") and len(argv) > 1
            and node_script and argv[1].endswith(node_script)):
        return argv[2:]
    return None


def _claude_process(argv: list[str]) -> str | None:
    args = _command(argv, "claude", "/@anthropic-ai/claude-code/cli.js")
    if args is None and len(argv) > 1 and Path(argv[0]).name in ("node", "nodejs"):
        if "/anthropic.claude-" in argv[1] and argv[1].endswith("/resources/claude-code/cli.js"):
            args = argv[2:]
    if args is None or any(x in args for x in ("--version", "--help", "-v", "-h")):
        return None
    if args and args[0] in ("mcp", "update", "install", "doctor", "auth", "plugin"):
        return None
    return "worker"


def _codex_process(argv: list[str]) -> str | None:
    args = _command(argv, "codex", "/@openai/codex/bin/codex.js")
    if args is None or any(x in args for x in ("--version", "--help", "-V", "-h")):
        return None
    if args and args[0] in ("login", "logout", "mcp", "mcp-server", "completion", "debug"):
        return None
    shared = "app-server" in args
    if Path(argv[0]).name in ("node", "nodejs"):
        return "shared-wrapper" if shared else "wrapper"
    return "shared" if shared else "worker"


def _goose_command_prefix() -> list[str]:
    return shlex.split(GOOSE_CLI)


def _goose_process_names() -> tuple[str, ...]:
    prefix = _goose_command_prefix()
    return ("goose", Path(prefix[0]).name) if prefix else ("goose",)


def _goose_process(argv: list[str]) -> str | None:
    prefix = _goose_command_prefix()
    if prefix and argv[:len(prefix)] == prefix:
        args = argv[len(prefix):]
        if len(prefix) > 1 and not args:
            return None
    else:
        args = _command(argv, "goose")
    return "worker" if args is not None and (not args or args[0] in ("acp", "session", "run")) else None


def _opencode_process_names() -> tuple[str, ...]:
    return "opencode", Path(os.environ.get("STEERING_OPENCODE_CLI") or "opencode").name


def _opencode_process(argv: list[str]) -> str | None:
    args = _command(argv, _opencode_process_names())
    if args is None or any(x in args for x in ("--version", "--help", "-v", "-h")):
        return None
    if args and args[0] in ("auth", "mcp", "upgrade", "debug", "models"):
        return None
    return "shared" if args and args[0] == "serve" else "worker"


def census_process(pid: int, command: str, *, deadline: float | None = None
                   ) -> tuple[Harness | None, str | None, list[str], str | None]:
    # The executable name is only a prefilter. Birth brackets the exact argv capture, so a
    # reused PID cannot acquire the previous process's harness identity.
    names = {"node", "nodejs", *HARNESSES}
    names.update(name for h in HARNESSES.values() if h.process_names for name in h.process_names())
    if Path(command).name not in names:
        return None, None, [], None
    deadline = census_deadline() if deadline is None else deadline
    check_census_budget(deadline)
    since = runtime_id.started_at(pid)
    check_census_budget(deadline)
    if since is None:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return None, None, [], None
        except OSError:
            pass
        return None, None, ["process-birth-unreadable"], None
    argv = runtime_id.process_argv(pid)
    check_census_budget(deadline)
    current = runtime_id.started_at(pid)
    check_census_budget(deadline)
    if current != since:
        return None, None, ["process-changed-during-capture"], None
    if argv is None:
        return None, None, ["process-argv-unreadable"], None
    for h in HARNESSES.values():
        kind = h.process_kind(argv) if h.process_kind else None
        if kind:
            return h, kind, [], since
        if h.process_kind is None and Path(argv[0]).name == h.provider:
            return h, "shared", [], since
    return None, None, [], None


def _claude_live_workers(config: Path, *, deadline: float | None = None) -> tuple[list[dict], list[str]]:
    from datetime import datetime, timezone
    import math
    deadline = census_deadline() if deadline is None else deadline
    check_census_budget(deadline)
    out, gaps = [], set()
    directory = config / "sessions"
    try:
        with os.scandir(directory) as entries:
            for index, entry in enumerate(entries):
                check_census_budget(deadline)
                if index >= CENSUS_REGISTRY_LIMIT:
                    gaps.add("claude-registry-overflow")
                    break
                if not entry.name.endswith(".json") or not entry.is_file(follow_symlinks=False):
                    continue
                try:
                    check_census_budget(deadline)
                    with open(entry.path, "rb") as f:
                        check_census_budget(deadline)
                        data = f.read(CENSUS_RECORD_BYTES + 1)
                    check_census_budget(deadline)
                    if len(data) > CENSUS_RECORD_BYTES:
                        gaps.add("claude-registry-overflow")
                        continue
                    rec = json.loads(data)
                    pid, sid, start = rec.get("pid"), rec.get("sessionId"), rec.get("procStart")
                    if (not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0
                            or not isinstance(sid, str) or not sid or len(sid) > 256
                            or not isinstance(start, str) or len(start) > 64):
                        raise ValueError("invalid registry witness")
                    check_census_budget(deadline)
                    since = runtime_id.started_at(pid)
                    check_census_budget(deadline)
                    epoch = runtime_id.started_epoch(pid)
                    check_census_budget(deadline)
                    current = runtime_id.started_at(pid)
                    check_census_budget(deadline)
                    if current != since:
                        gaps.add("process-changed-during-capture")
                        continue
                    if epoch is None or since is None:
                        # Exited records are common; the process table independently reports
                        # unreadable live candidates instead of trusting a historical record.
                        continue
                    stated = datetime.strptime(start, "%a %b %d %H:%M:%S %Y").replace(tzinfo=timezone.utc).timestamp()
                    if math.floor(epoch) != stated:
                        continue
                    out.append({"harness": "claude", "pid": pid, "since": since, "psession": sid})
                except (ValueError, TypeError, AttributeError, UnicodeError, OverflowError):
                    gaps.add("claude-registry-malformed")
                except TimeoutError:
                    raise
                except OSError:
                    gaps.add("claude-registry-unreadable")
    except FileNotFoundError:
        pass
    except TimeoutError:
        raise
    except OSError:
        gaps.add("claude-registry-unreadable")
    check_census_budget(deadline)
    return out, sorted(gaps)


HARNESSES = {h.provider: h for h in (
    Harness("claude", agent="claude-code", config=".claude", config_env="CLAUDE_CONFIG_DIR",
            transcript_root="projects",
            session_id=_env("CLAUDE_CODE_SESSION_ID"), harness_pid=_claude_pid,
            transcript=_claude_transcript, version=_cli_version("claude", "--version"),
            shape=_claude_shape, workspace=_claude_workspace, hooks=True,
            reading=_claude_reading, process_kind=_claude_process, live_workers=_claude_live_workers),
    Harness("codex", agent="codex", config=".codex", config_env="CODEX_HOME",
            transcript_root="sessions",
            session_id=_env("CODEX_THREAD_ID", "CODEX_SESSION_ID"), harness_pid=_codex_pid,
            transcript=_codex_transcript, version=_cli_version("codex", "--version"),
            shape=_codex_shape, workspace=_codex_workspace,
            delivery="inject", loaded=_codex_loaded, reachable=_codex_reachable,
            reading=_codex_reading, process_kind=_codex_process),
    Harness("grok", agent="grok", config=".grok", transcript_root="sessions",
            shape=_grok_shape, workspace=_grok_workspace),
    # A worker rather than a session someone is sitting at: this platform launches it, so it
    # names neither its session nor its process to anyone but its manager, and both are told
    # to `connect.py`. Its transcript is the file the manager hands it to hold open.
    Harness("goose", agent="goose", config=".local/share/goose",
            transcript_root="steering-workers",
            harness_pid=_goose_pid, transcript=_goose_transcript, version=goose_version,
            shape=_goose_shape, workspace=_goose_workspace,
            delivery="inject", loaded=_goose_loaded, reading=_goose_reading,
            process_kind=_goose_process, process_names=_goose_process_names),
    # Launched enrolled and reached as Goose is (docs 83, 89).
    Harness("opencode", agent="opencode", config=".local/share/opencode",
            transcript_root="steering-workers",
            harness_pid=_opencode_pid, transcript=_opencode_transcript, version=opencode_version,
            shape=_goose_shape, workspace=_opencode_workspace,
            delivery="inject", loaded=_opencode_loaded, process_kind=_opencode_process,
            process_names=_opencode_process_names),
)}


def of(provider: str | None) -> Harness | None:
    return HARNESSES.get(provider or "")


def provider_of(agent: str) -> str | None:
    """The harness a name belongs to, from the prefix an `enroll:` line carries
    (`claude-code/acct`) or from the harness's own name. Read off `Harness.agent`, so a
    registration is the whole of teaching this: a second table would be a second answer.
    """
    name = str(agent or "").split("/", 1)[0]
    for hs in HARNESSES.values():
        if name == hs.provider or (hs.agent and name == hs.agent):
            return hs.provider
    return None


def here() -> list[Harness]:
    """The harnesses that say this process is one of their sessions. More than one answers when
    a session of one harness was started from inside another, and then nothing here can tell
    whose enrolment is being asked for — the caller has to name it."""
    return [h for h in HARNESSES.values() if h.exports and h.session_id() is not None]


def of_runtime(runtime: str | None) -> Harness | None:
    """The harness a runtime id names: `runtime_id` builds the id from the provider, so the
    provider is the id's first field."""
    return of((runtime or "").split(":", 1)[0] or None)


# Every variable by which a process says it is some harness's session; a child that speaks as a
# session it names in full is started without them, or `speaking_as` meets two identities.
IDENTITY_ENV = (*(n for h in HARNESSES.values() for n in getattr(h.session_id, "names", ())), "CLAUDE_PID")
PROVIDERS = tuple(HARNESSES)       # every provider this machine answers for, and the only list of them
DEFAULT = "claude"                 # the harness assumed when a caller names none
