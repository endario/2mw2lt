"""claude-code-proxy on this machine: which one a session speaks to, where its Codex login is.

A Claude Code session driving GPT through claude-code-proxy spends the Codex login in that
proxy's own home, never `~/.codex` (#2082). The proxy has no home flag, so a second plan is a
second proxy on another port with its own `XDG_CONFIG_HOME`; the port a session's
`ANTHROPIC_BASE_URL` names is what says which proxy, and that process's environment is what says
which home. The bind reads the account from here, and the usage reader reads each home's
account from here, so the two cannot name different homes.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path
from urllib.parse import urlsplit

NAME = "claude-code-proxy"
ACCOUNT_ID = re.compile(r"[A-Za-z0-9_-]{1,200}")


# lsof walks every process's descriptors: measured on one host on 2026-09-25 at up to 4.1 s idle, and
# past the 5 s this had under the load an agent restart brings, when it answered nothing and a
# Codex launcher was offered no account (#2336's live test).
LSOF_TIMEOUT = 30


def _run(argv: list[str]) -> str:
    timeout = LSOF_TIMEOUT if Path(argv[0]).name == "lsof" else 5
    try:
        return subprocess.run(argv, capture_output=True, text=True, timeout=timeout).stdout
    except (OSError, subprocess.SubprocessError):
        return ""


def _tool(name: str) -> str | None:
    return shutil.which(name) or next((p for p in (f"/usr/sbin/{name}", f"/bin/{name}", f"/usr/bin/{name}")
                                       if os.access(p, os.X_OK)), None)


def loopback_port(base_url: str | None) -> int | None:
    """The port of an `ANTHROPIC_BASE_URL` on this machine's loopback, or None."""
    try:
        u = urlsplit(base_url or "")
        port = u.port
    except ValueError:
        return None
    return port if u.scheme in ("http", "https") and u.hostname in ("127.0.0.1", "localhost", "::1") and port else None


def listener(port: int, run=None) -> int | None:
    """The one process listening on `port`. Two, or none, is no answer."""
    return listening(port, run)[0]


def listening(port: int, run=None) -> tuple[int | None, str]:
    """`listener`, and what was found when it is None."""
    lsof = _tool("lsof")
    if not lsof:
        return None, "no lsof on this machine"
    argv = [lsof, "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-t"]
    began = time.monotonic()
    out = (run or _run)(argv)
    pids = {int(x) for x in out.split() if x.isdigit()}
    if len(pids) == 1:
        return next(iter(pids)), ""
    if pids:
        return None, f"{len(pids)} processes listen on port {port}"
    if time.monotonic() - began >= LSOF_TIMEOUT:
        return None, f"lsof did not answer within {LSOF_TIMEOUT} s"
    return None, f"nothing listens on port {port}"


def _env_word(words: list[str], name: str) -> str:
    """The last `name=` word, or empty: `ps -E` prints the environment after the arguments, so
    an argument cannot come after it."""
    got = [w.split("=", 1)[1] for w in words if w.startswith(name + "=")]
    return got[-1] if got else ""


def proxy_home(pid: int, run=None, home: Path | None = None) -> Path | None:
    """The configuration root of the claude-code-proxy running as `pid`, from its own environment:
    its `XDG_CONFIG_HOME`, else its own `HOME`'s `.config`, else the reader's. None when the
    process is not claude-code-proxy, or names a relative root."""
    run, ps = run or _run, _tool("ps")
    words = (run([ps, "-Eww", "-o", "command=", "-p", str(pid)]) if ps else "").split()
    if not words or Path(words[0]).name != NAME:
        return None
    xdg, own = _env_word(words[1:], "XDG_CONFIG_HOME"), _env_word(words[1:], "HOME")
    root = Path(xdg) if xdg else (Path(own) if own else (home or Path.home())) / ".config"
    return root if root.is_absolute() else None


def auth_path(root: Path) -> Path:
    return root / NAME / "codex" / "auth.json"


def auth_account(path: Path) -> str | None:
    """`tokens.account_id` of a Codex-shaped auth.json, or None."""
    try:
        got = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    tokens = got.get("tokens") if isinstance(got, dict) else None
    account_id = tokens.get("account_id") if isinstance(tokens, dict) else None
    return account_id if isinstance(account_id, str) and ACCOUNT_ID.fullmatch(account_id) else None


def proxy_credential(root: Path) -> tuple[str, str] | None:
    """`(access token, account id)` from a proxy home's `auth.json`, or None.

    The proxy keeps its own shape — top-level `access`, `refresh`, `expires`, `accountId` —
    not Codex's `tokens` object. Measured on one host on 2026-09-23, where a reader of the Codex shape
    found no account behind a working proxy, and a stand-in written in the Codex shape had passed."""
    try:
        got = json.loads(auth_path(root).read_text())
    except (OSError, ValueError):
        return None
    access = got.get("access") if isinstance(got, dict) else None
    account_id = got.get("accountId") if isinstance(got, dict) else None
    if isinstance(access, str) and access and isinstance(account_id, str) and ACCOUNT_ID.fullmatch(account_id):
        return access, account_id
    return None


def home_of(base_url: str | None, run=None, home: Path | None = None) -> Path | None:
    """The home of the claude-code-proxy a base URL names."""
    port = loopback_port(base_url)
    pid = listener(port, run) if port else None
    return proxy_home(pid, run, home) if pid else None


def proxied_account(base_url: str | None, run=None, home: Path | None = None) -> str | None:
    """The OpenAI account a Claude Code session spends through the proxy its base URL names."""
    root = home_of(base_url, run, home)
    held = proxy_credential(root) if root else None
    return held[1] if held else None


def homes(run=None, home: Path | None = None) -> list[Path]:
    """The home of every claude-code-proxy running on this machine, each once."""
    run, ps = run or _run, _tool("ps")
    out: list[Path] = []
    for line in (run([ps, "-Aww", "-o", "pid=,command="]) if ps else "").splitlines():
        pid, _, command = line.strip().partition(" ")
        if pid.isdigit() and command.split() and Path(command.split()[0]).name == NAME:
            root = proxy_home(int(pid), run, home)
            if root and root not in out:
                out.append(root)
    return out


def label(root: Path) -> Path:
    """The directory a home's reading is labelled with: stable per home, distinct from Codex's own
    `default`, and carrying nothing of the path onto the wire."""
    return Path(f"{NAME}-{hashlib.sha256(str(root).encode()).hexdigest()[:8]}")
