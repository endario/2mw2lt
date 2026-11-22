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

# The variable the agent exports to every session it launches, naming its bundled interpreter
# (#10540): the command forms resolve `${STEERING_PYTHON:-python3}`, and this module resolves the
# same interpreter so what the forms run is what is checked. A variable survives the login shells
# macOS path_helper reorders; a PATH prepend does not.
ENV = "STEERING_PYTHON"


def _sentence(found: tuple) -> str:
    return (f"2mw2lt needs Python {NEEDS[0]}.{NEEDS[1]} or later; this is {found[0]}.{found[1]}. "
            "Install a newer one (`brew install python` on macOS) and run it again.")


def reexec() -> None:
    """Re-run the calling script under the interpreter `STEERING_PYTHON` names when this one is
    under the floor. That is what lets a bare `python3` command form still work on a machine whose
    PATH python3 is macOS's 3.9 (#10540): the agent's export survives the login shell the PATH
    reorder happens in. A form that is not a file on argv — `-c`, `-m`, a REPL — is left to the
    refusal, and a variable that names this same interpreter or nothing executable is treated as
    absent, which ends the chain at this refusal rather than looping it."""
    import os
    if tuple(sys.version_info[:2]) >= NEEDS:
        return
    target = os.environ.get(ENV)
    if not target or not os.access(target, os.X_OK):
        return
    if not sys.argv or sys.argv[0] in ("", "-c", "-m"):
        return
    try:
        if os.path.realpath(target) == os.path.realpath(sys.executable or ""):
            return
        os.execv(target, [target] + sys.argv)
    except OSError:
        return


def require(found=None) -> None:
    found = tuple((found or sys.version_info)[:2])
    if found < NEEDS:
        if found == tuple(sys.version_info[:2]):
            reexec()
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
    """The interpreter a hook command resolves — `STEERING_PYTHON` where the environment names
    one, else the `python3` PATH resolves (#10540): (its version, its path), or (None, why it
    could not be asked). Install and connect check this interpreter rather than the one running
    them (#9888)."""
    import os
    import shutil
    import subprocess
    exe = os.environ.get(ENV) or shutil.which("python3")
    if not exe:
        return None, f"{ENV} names no interpreter and PATH names no python3"
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
    form = f"`${{{ENV}:-python3}}`"
    if found is None:
        return (f"2mw2lt's hooks run {form}, and {exe}; install Python {need} or later "
                f"(`brew install python` on macOS), then {again}.")
    return (f"2mw2lt's hooks run {form}, which here resolves to {exe} at "
            f"{'.'.join(str(v) for v in found)}, and the workspace needs Python {need} or later. "
            f"Install a newer one (`brew install python` on macOS), then {again}.")


def on_path_refusal(again: str = "run this again") -> str | None:
    """Why the interpreter the hooks will run is refused — the version found, the one needed and
    the fix — or None when it meets the floor."""
    found, exe = on_path()
    if found is None or tuple(found[:2]) < NEEDS:
        return path_why(found, exe, again)
    return None
