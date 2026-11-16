"""The Python the plugin's scripts need, refused in words a person can act on.

An entrypoint that can reach code needing a newer Python calls `require()` before importing
anything else, and a script a harness's hook runs calls `hook()` the same way — saying whether its
event reads an exit 2 as blocking — so a stock macOS `python3` (3.9), or one whose `ssl` cannot
load, gets a sentence rather than a traceback; one that cannot reach it does not call it
(python_floor_test.py). It must itself parse and run on the oldest Python a person may have.
"""
from __future__ import annotations

import sys

# steering/pyproject.toml's requires-python: the version a machine installs for this repository
# (the owner's ruling, 2026-11-16 — no backward compatibility with an older one).
NEEDS = (3, 12)


def _sentence(found: tuple) -> str:
    return (f"2mw2lt needs Python {NEEDS[0]}.{NEEDS[1]} or later; this is {found[0]}.{found[1]}. "
            "Install a newer one (`brew install python` on macOS) and run it again.")


def require(found=None) -> None:
    found = tuple((found or sys.version_info)[:2])
    if found < NEEDS:
        sys.exit(_sentence(found))
    try:
        import ssl  # noqa: F401
    except ImportError as e:
        # Seen 2026-10-17: a Homebrew Python whose OpenSSL belongs to another login on the Mac.
        sys.exit(f"2mw2lt needs a Python that can speak HTTPS; {sys.executable} cannot load `ssl` ({e}). "
                 "Use another, such as `uv python install 3.13 --default`, and run it again.")


def hook(*, blocking: bool, found=None) -> None:
    """`require()` for a script a harness's hook runs. `blocking` is the call site's word for
    what its own event does with an exit 2 — never inferred, since the refusal fires before the
    payload that would name the event is read. A PreToolUse guard says True: there 2 refuses the
    tool call, which is the point (#9888). Every other event says False, because a 2 means
    something else on it — PermissionRequest denies the ask, Stop loops the turn,
    UserPromptSubmit erases the prompt — so the refusal goes to stderr and exits 0, which the
    harness notes and proceeds past. A crash or a bare non-zero is only ever noted — how the
    trap guard died on every Bash call on a 3.9 Mac and was never missed."""
    try:
        require(found)
    except SystemExit as e:
        print(e.code, file=sys.stderr)
        sys.exit(2 if blocking else 0)


def on_path() -> tuple[tuple[int, ...] | None, str]:
    """The `python3` a hook command resolves on PATH: (its version, its path), or (None, why it
    could not be asked). The hook commands name it bare, so install and connect check this
    interpreter rather than the one running them (#9888)."""
    import shutil
    import subprocess
    exe = shutil.which("python3")
    if exe is None:
        return None, "PATH names no python3"
    try:
        run = subprocess.run([exe, "-c", "import sys; print('%d.%d.%d' % sys.version_info[:3])"],
                             capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError) as e:
        return None, f"{exe} would not say its version ({e})"
    if run.returncode != 0:
        return None, f"{exe} exited {run.returncode} when asked its version"
    try:
        return tuple(int(part) for part in run.stdout.strip().split(".")), exe
    except ValueError:
        return None, f"{exe} answered {run.stdout.strip()[:80]!r}, not a version"


def path_why(found: tuple | None, exe: str, again: str) -> str:
    need = f"{NEEDS[0]}.{NEEDS[1]}"
    if found is None:
        return (f"2mw2lt's hooks run `python3`, and {exe}; install Python {need} or later "
                f"(`brew install python` on macOS), then {again}.")
    return (f"2mw2lt's hooks run `python3`, which here is {exe} at {'.'.join(str(v) for v in found)}, "
            f"and the workspace needs Python {need} or later. Install a newer one "
            f"(`brew install python` on macOS), then {again}.")


def on_path_refusal(again: str = "run this again") -> str | None:
    """Why the `python3` the hooks will run is refused — the version found, the one needed and the
    fix — or None when it meets the floor."""
    found, exe = on_path()
    if found is None or tuple(found[:2]) < NEEDS:
        return path_why(found, exe, again)
    return None
