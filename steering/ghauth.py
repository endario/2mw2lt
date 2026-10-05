"""GitHub identities for workspace launches and standalone legacy callers."""
from __future__ import annotations

import os
import subprocess
import sys
import json
from pathlib import Path

import machine_workspaces


class NoIdentity(RuntimeError):
    """The pinned account named no token, so nothing may run as it."""


def account() -> str:
    """The legacy standalone process pin, or an empty string when it pins none."""
    return os.environ.get("STEERING_GH_ACCOUNT", "").strip()


def pinned_token(name: str, run=subprocess.run) -> str:
    """The named account's token, or a refusal.

    `gh` ignores an empty `GH_TOKEN` and uses whichever account is active, so a pin that
    resolves to nothing is worse than no pin: it looks configured and is not.
    """
    try:
        r = run(["gh", "auth", "token", "-h", "github.com", "-u", name],
                capture_output=True, text=True, timeout=30)
    except (subprocess.TimeoutExpired, OSError) as e:
        raise NoIdentity(f"gh auth token for {name!r}: {type(e).__name__}") from e
    token = r.stdout.strip()
    if r.returncode != 0 or not token:
        raise NoIdentity(f"no token for account {name!r}: {r.stderr.strip() or 'gh printed nothing'}")
    return token


def pin_into(env: dict, root: Path | None = None) -> str | None:
    """Apply this workspace's identity; a registered pin that cannot resolve is refused.

    Standalone legacy launches retain their warning-and-fallback behavior (doc 08).
    """
    entry = machine_workspaces.at(root) if root is not None else None
    if entry is not None:
        for key in ("GH_TOKEN", "GITHUB_TOKEN", "STEERING_GH_ACCOUNT"):
            env.pop(key, None)
        name = entry.gh_account or ""
    else:
        name = account()
    if not name:
        return None
    try:
        env["GH_TOKEN"] = pinned_token(name)
    except NoIdentity as e:
        if entry is not None:
            raise NoIdentity(f"workspace {entry.root} cannot run as GitHub account {name!r}: {e}") from e
        why = f"gh identity {name!r} unresolved, this session runs as the active account: {e}"
        print(why, file=sys.stderr, flush=True)
        return why
    return None


def readable_accounts(repo: str) -> list[str]:
    """Signed-in GitHub logins whose own token can read `repo`, without switching gh's login."""
    try:
        got = subprocess.run(["gh", "auth", "status", "--hostname", "github.com", "--json", "hosts"],
                             capture_output=True, text=True, timeout=30,
                             env={k: v for k, v in os.environ.items() if k not in ("GH_TOKEN", "GITHUB_TOKEN")})
        if got.returncode:
            raise NoIdentity("gh could not list its accounts; run `gh auth status`")
        rows = json.loads(got.stdout)["hosts"].get("github.com", [])
        names = list(dict.fromkeys(row["login"] for row in rows if row.get("login")))
        readable = []
        for name in names:
            try:
                token = pinned_token(name)
            except NoIdentity:
                continue
            result = subprocess.run(["gh", "api", "--hostname", "github.com", f"repos/{repo}"], capture_output=True, text=True,
                                    timeout=30, env={**os.environ, "GH_TOKEN": token})
            if result.returncode == 0:
                readable.append(name)
        return readable
    except (OSError, subprocess.SubprocessError, ValueError, KeyError) as e:
        raise NoIdentity(f"GitHub accounts could not be checked ({type(e).__name__}); run `gh auth status`") from None
