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


# git asks `gh` for a GitHub credential, which answers with `GH_TOKEN` when one is set.
GIT_HELPER = "!gh auth git-credential"


def only_helper(scope: str = "") -> list[tuple[str, str]]:
    """git config entries making `GIT_HELPER` the one helper git asks, for `scope` (a URL, or every
    host): the empty value first resets every helper configured before it, the machine's included."""
    key = f"credential.{scope}.helper" if scope else "credential.helper"
    return [(key, ""), (key, GIT_HELPER)]


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
    """Signed-in GitHub logins whose own token can read `repo`, without switching gh's login.
    GitHub answers 404 for a repository a login cannot see; any other failure is not an answer
    about the repository, so it is raised naming the login and what gh said."""
    try:
        got = subprocess.run(["gh", "auth", "status", "--hostname", "github.com", "--json", "hosts"],
                             capture_output=True, text=True, timeout=30,
                             env={k: v for k, v in os.environ.items() if k not in ("GH_TOKEN", "GITHUB_TOKEN")})
        if got.returncode:
            raise NoIdentity(f"gh could not list its accounts ({_said(got)}); run `gh auth status`")
        rows = json.loads(got.stdout)["hosts"].get("github.com", [])
        names = list(dict.fromkeys(row["login"] for row in rows if row.get("login")))
        if not names:
            raise NoIdentity("gh is signed in to no GitHub account; run `gh auth login`, then run install again")
        readable = []
        for name in names:
            try:
                token = pinned_token(name)
            except NoIdentity as e:
                raise NoIdentity(f"{e}; run `gh auth login` for {name}, or `gh auth logout -u {name}`") from None
            result = subprocess.run(["gh", "api", "--hostname", "github.com", f"repos/{repo}"], capture_output=True, text=True,
                                    timeout=30, env={**os.environ, "GH_TOKEN": token})
            if result.returncode == 0:
                readable.append(name)
            elif "HTTP 404" not in result.stderr:
                raise NoIdentity(f"gh could not check whether {name} reads {repo} ({_said(result)}); "
                                 f"run `gh api repos/{repo}` as {name} to see why, then run install again")
        return readable
    except FileNotFoundError:
        raise NoIdentity("install the GitHub CLI (gh), then run install again") from None
    except subprocess.TimeoutExpired as e:
        raise NoIdentity(f"gh did not answer within {e.timeout:.0f}s ({' '.join(e.cmd[:3])}); "
                         "check the network, then run install again") from None
    except (OSError, ValueError, KeyError) as e:
        raise NoIdentity(f"GitHub accounts could not be checked ({type(e).__name__}: {e}); run `gh auth status`") from None


def _said(done: subprocess.CompletedProcess) -> str:
    """gh's own last line of complaint, or its exit status when it printed none."""
    lines = (done.stderr or "").strip().splitlines()
    return lines[-1] if lines else f"exit {done.returncode}"
