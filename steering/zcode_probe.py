"""ZCode rollout, version and the account db's own readings."""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import time
from pathlib import Path


def base() -> Path:
    """The native base ZCode writes under: the home, or the base `ZCODE_DATA_BASE_DIR` names
    (the variable moves the whole native layout — provider and credential storage included —
    so the configuration directory is `<base>/.zcode`, measured 2026-10-13)."""
    moved = os.environ.get("ZCODE_DATA_BASE_DIR", "").strip()
    return Path(moved) if moved else Path.home()


def config_dir() -> Path:
    """The configuration directory, in the registry's own terms: `Harness.config_dir()` answers
    the env value directly when set, and this answers the same, so census roots and account
    labels never disagree about where the account lives. The layout under it uses `base()`."""
    moved = os.environ.get("ZCODE_DATA_BASE_DIR", "").strip()
    return Path(moved) if moved else Path.home() / ".zcode"


def rollout(session: str, home: Path | None = None) -> Path | None:
    """The session's own record — one `model_io` line per model call, the session id in the
    filename — or None. This is the reading source, never the door's proof: the door trusts the
    platform's managed record, and this file is what ZCode itself observed."""
    root = (home or base() / ".zcode") / "cli" / "rollout"
    p = root / f"model-io-{session}.jsonl"
    return p if p.is_file() else None


def version(binary: str = "zcode", timeout: float = 10.0) -> str | None:
    """Both numbers the product prints, labelled — the CLI's and the app's — saying which is
    which, because two version numbers name one product and are not interchangeable (doc 49)."""
    try:
        r = subprocess.run([binary, "--version"], capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return None
    if r.returncode != 0:
        return None
    lines = [l.strip() for l in (r.stdout or "").splitlines() if l.strip()]
    return "; ".join(lines[:2]) if lines else None


# The account db holds its content under WAL while the runtime is open: a read-only open sees a
# consistent answer, `immutable=1` does not (doc 49 measured a near-empty main file whose content
# was all in the -wal), and a busy store retries rather than going blind exactly when the worker
# is busiest.
_BUSY_RETRIES, _BUSY_BACKOFF = 3, 0.2


def ro_uri(path: Path) -> str:
    """The read-only URI the reading opens. `mode=ro`, never `immutable=1`: the account db
    carries its content under WAL while the runtime holds it open, and an immutable open of a
    WAL-backed store answers a near-empty file (doc 49 measured exactly that on the v2 index)."""
    return f"file:{path}?mode=ro"


def _connect(path: Path) -> sqlite3.Connection:
    db = sqlite3.connect(ro_uri(path), uri=True, timeout=_BUSY_BACKOFF)
    db.execute("PRAGMA busy_timeout = 500")
    return db


def reading(session: str, home: Path | None = None) -> dict | None:
    """What the account db says the session is: the model its last completed request served and
    the tokens its turns spent, read from `model_usage` and `turn_usage` — per-request and
    per-turn records including reasoning and cache, richer than any transcript parse. None when
    the db or the session's rows say nothing.

    A locked store retries on a short backoff; a db that stays locked or absent answers None
    rather than raising — a reading that cannot be taken is not a reading the worker lacks."""
    path = (home or base() / ".zcode") / "cli" / "db" / "db.sqlite"
    if not path.is_file():
        return None
    for attempt in range(_BUSY_RETRIES):
        try:
            db = _connect(path)
            try:
                rows = db.execute(
                    "SELECT model_id, provider_id, status, input_tokens, output_tokens,"
                    " reasoning_tokens, cache_read_input_tokens"
                    " FROM model_usage WHERE session_id = ? ORDER BY started_at DESC LIMIT 1",
                    (session,)).fetchone()
                turns = db.execute(
                    "SELECT status, input_tokens, output_tokens, reasoning_tokens,"
                    " cache_read_input_tokens FROM turn_usage WHERE session_id = ?"
                    " ORDER BY started_at DESC LIMIT 1", (session,)).fetchone()
            finally:
                db.close()
            break
        except sqlite3.Error:
            if attempt == _BUSY_RETRIES - 1:
                return None
            time.sleep(_BUSY_BACKOFF)
    out: dict = {}
    if rows and rows[2] in ("completed", "running"):
        out.update({"model": rows[0], "provider": rows[1]})
    if turns:
        out.update({"tokens": {"input": turns[1] or 0, "output": turns[2] or 0,
                               "reasoning": turns[3] or 0, "cache_read": turns[4] or 0},
                    "status": turns[0]})
    return out or None
