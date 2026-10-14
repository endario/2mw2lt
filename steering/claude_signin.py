"""Where a Claude config directory's sign-in lives, and whether a session started on it could still
renew it: the agent's harness and the launchers both read it, so it stands apart from either."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Callable

KEYCHAIN_SERVICE = "Claude Code-credentials"
SECURITY = "/usr/bin/security"


def _account_in(path: Path) -> str | None:
    try:
        return (json.loads(path.read_text()).get("oauthAccount") or {}).get("accountUuid") or None
    except (OSError, ValueError, AttributeError):
        return None


def keychain_services(config: Path) -> list[str]:
    """The keychain entries that may hold the sign-in of `CLAUDE_CONFIG_DIR=config`.

    The one named from a digest of the path as spelled, which is the spelling the enumeration
    reads. For the default directory also the unnamed entry, which sessions run with no
    `CLAUDE_CONFIG_DIR` refresh while the named one goes stale (doc 08, #943) — but only where
    that entry's identity, `~/.claude.json`, names the same account as the directory's own
    `.claude.json` the join named: spending it otherwise could bill another account (#1633)."""
    import hashlib
    named = f"{KEYCHAIN_SERVICE}-{hashlib.sha256(str(config).encode()).hexdigest()[:8]}"
    home = Path.home()
    same = (config == home / ".claude"
            and _account_in(home / ".claude.json") is not None
            and _account_in(home / ".claude.json") == _account_in(config / ".claude.json"))
    return [named, KEYCHAIN_SERVICE] if same else [named]


def _read_keychain(service: str, timeout: float = 10.0) -> str | None:
    """The keychain's entry for `service`, or None when it holds none. Raises OSError, saying why,
    for a keychain that could not be read, which says nothing of the sign-in."""
    import subprocess
    try:
        r = subprocess.run([SECURITY, "find-generic-password", "-s", service, "-w"],
                           capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise OSError(f"keychain entry {service}: no answer in {timeout:.0f}s") from None
    if r.returncode == 44:   # errSecItemNotFound, as `security` exits for it
        return None
    if r.returncode != 0:
        raise OSError(f"keychain entry {service}: exit {r.returncode}: {r.stderr.strip()[:80]}")
    return r.stdout


def login_lapsed(config: Path, now: float | None = None,
                 read: Callable[[str], str | None] = _read_keychain, darwin: bool | None = None) -> bool:
    """Whether the sign-in a session started on `config` would use can no longer be renewed
    (#6477): the keychain's on macOS where it holds one, as an interactive session reads it, else
    the directory's `.credentials.json`; lapsed when every such sign-in's refresh expiry has
    passed. `auth status` answers signed in on a lapsed one, and the session then stops at its
    login screen. A sign-in that states no refresh expiry, or none read, is not a lapse (#3046), nor
    is any while the keychain could not be read, since the session may hold a current one there."""
    import time

    def signins(blobs):
        out = []
        for blob in blobs:
            try:
                o = json.loads(blob).get("claudeAiOauth")
            except (ValueError, AttributeError):
                continue
            if isinstance(o, dict) and isinstance(o.get("accessToken"), str):
                out.append(o)
        return out

    darwin = sys.platform == "darwin" if darwin is None else darwin
    try:
        used = signins(b for b in map(read, keychain_services(config)) if b) if darwin else []
    except OSError as e:
        print(f"login of {config} not judged: {e}", file=sys.stderr)
        return False
    if not used:
        try:
            used = signins([(config / ".credentials.json").read_text()])
        except OSError:
            return False
    at = time.time() if now is None else now
    expiries = [o.get("refreshTokenExpiresAt") for o in used]
    return bool(used) and all(isinstance(e, (int, float)) and not isinstance(e, bool) and e / 1000 <= at
                              for e in expiries)
