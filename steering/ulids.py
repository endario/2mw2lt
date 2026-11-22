"""The envelope id: what the client and the daemon both mint and check (#2123)."""
from __future__ import annotations

import os
import re
import time

# The envelope id reaches the filesystem as a name, so it is validated as a whole string
# before any path is built from it rather than sanitized in pieces.
ULID_RE = re.compile(r"[0-9a-f]{24}")
# A card id as a caller may take it: the full wire id, or any non-empty prefix of one. The desk
# prints the first eight, and the cards minted within one 65-second window share them (#10543).
CARD_REF_RE = re.compile(r"[0-9a-f]{1,24}")


def new_ulid() -> str:
    ms = int(time.time() * 1000)
    return f"{ms:012x}{os.urandom(6).hex()}"


def valid_ulid(u: object) -> bool:
    return isinstance(u, str) and bool(ULID_RE.fullmatch(u))


def valid_card_ref(c: object) -> bool:
    """A card id taken from a caller: the full wire id, or a prefix the platform's output named."""
    return isinstance(c, str) and bool(CARD_REF_RE.fullmatch(c))
