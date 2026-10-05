"""The envelope id: what the client and the daemon both mint and check (#2123)."""
from __future__ import annotations

import os
import re
import time

# The envelope id reaches the filesystem as a name, so it is validated as a whole string
# before any path is built from it rather than sanitized in pieces.
ULID_RE = re.compile(r"[0-9a-f]{24}")


def new_ulid() -> str:
    ms = int(time.time() * 1000)
    return f"{ms:012x}{os.urandom(6).hex()}"


def valid_ulid(u: object) -> bool:
    return isinstance(u, str) and bool(ULID_RE.fullmatch(u))
