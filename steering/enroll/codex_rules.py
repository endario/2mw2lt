"""The owner's standing grant of a worker's own forge writes, as one Codex execpolicy rule (#7940,
documentation/knowledge/decisions/workers-open-their-own-pull-requests.md).

Codex approves a command without asking when a `prefix_rule` in a `.rules` file under
`$CODEX_HOME/rules` allows it, and loads those files only when a session starts. A prefix cannot
refuse a trailing argument, so the rule grants no `gh` words: it names the workspace's stable
launcher running the forge verb, which checks every word itself (forge.py). Install writes the file
and connect refreshes it.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import local_workspace  # noqa: E402


def shared(workspace: Path) -> Path:
    """The checkout every worktree of workspace shares, by absolute path: the rule trusts the launcher
    in its `.claude/`, which only a writer of that checkout can replace, never a worker caged in a
    worktree (#7940)."""
    try:
        return local_workspace.common_root(workspace, timeout=2).resolve()
    except Exception:
        return workspace.resolve()


def launcher(workspace: Path) -> str:
    return str(shared(workspace) / ".claude" / "steering-launch.py")


def render(workspace: Path) -> str:
    """The rule granting the forge verb through workspace's launcher."""
    pattern = json.dumps(["python3", launcher(workspace), "exec", "forge"])
    why = json.dumps("2mw2lt: the forge verb checks every word of a worker's own pull request and issue writes")
    return f"prefix_rule(pattern = {pattern}, decision = \"allow\", justification = {why})\n"


def path_for(workspace: Path, home: Path | None = None) -> Path:
    home = home or Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")
    root = shared(workspace)
    key = hashlib.sha256(str(root).encode()).hexdigest()[:12]
    return home / "rules" / f"2mw2lt-forge-{root.name}-{key}.rules"


def write(workspace: Path, home: Path | None = None) -> str:
    """Write workspace's rule when it differs, and say which: a written rule reaches only a session
    Codex starts after it."""
    path = path_for(workspace, home)
    text = render(workspace)
    try:
        if path.read_text() == text:
            return f"forge grant: in place, run as python3 {launcher(workspace)} exec forge <gh words>"
    except OSError:
        pass
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return f"forge grant: written to {path}, reaches the next session; run as python3 {launcher(workspace)} exec forge <gh words>"
