"""Which of the daemon's action notices a hold may record without waking its session (#2455).

The texts are the ones `actions.said` builds; this module holds only their classifier, so an
installed client reads a notice without importing the daemon's ledger readers.
"""
from __future__ import annotations

import re

# What `actions.said` builds for an ending that asks nothing of the brain: a retire that retired,
# and a wake or control whose verdict is `confirmed`. Every other ending — a refusal, a
# contradiction, an unjudged verdict, and a `typed` a verdict is still owed on — is one the brain
# may have to act on.
ROUTINE = re.compile(r"retire of \S+: retired \(.*\)|(?:wake|control)\b[^—]* on \S+: confirmed(?:, the reading shows [^—]*)?")


def routine(sender: object, text: object) -> bool:
    """Whether a say is `said`'s telling of an ending the brain need not act on, which a hold
    records without waking the session for it (#2455). Only the daemon's own `action <id>` says."""
    return (isinstance(sender, str) and sender.startswith("action ") and isinstance(text, str)
            and ROUTINE.fullmatch(text) is not None)
