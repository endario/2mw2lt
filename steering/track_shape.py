"""What a card's track and a tracks document's lane may be: shape only, no authority.

A tracks document's validator and a card's facts both ask this, so a lane the document admits is
one a card can name (go-client-extraction-plan C4).
"""
from __future__ import annotations

TRACK_MAX = 60
# A card's answer that it is no track's: the owner ruled every track a product capability, so
# repository upkeep is drawn apart from the lanes and counted in none (the cluster review of
# 2026-09-25, decision 3). Offered to cards only, which is where the upkeep lands.
OFF_TRACK = "off-track"


def bounded_text(v: object, limit: int) -> bool:
    return isinstance(v, str) and bool(v.strip()) and len(v) <= limit and "\n" not in v


def valid_track(name: object) -> bool:
    """A lane name a `card-scoped` fact may carry. `register.validate` calls this rather than
    restating it, so a lane the register admits is one a card can name."""
    return bounded_text(name, TRACK_MAX)
