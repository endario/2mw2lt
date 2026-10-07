"""The wire names that changed (#4164), accepted under both names for one release.

Every sender writes the new name; a receiver takes the old one beside it. A process started before
the upgrade, such as a brain launched with the old environment and loopback address, or an agent a
release behind the daemon, keeps working through the release that ships this. The release after
refuses the old names: delete each table's old entries and the aliases they serve (#4242).
"""
from __future__ import annotations

ACCEPTED_THROUGH = "0.8.23"

HARNESS = {"claude-seat": "claude-brain"}
WAKE_ROLE = {"seat": "brain"}
ROUTE = {"/steering/seat": "/steering/brain"}
ENV = {"STEERING_SEAT_NONCE": "STEERING_BRAIN_NONCE", "STEERING_SEAT_AGENT": "STEERING_BRAIN_AGENT"}


def current(table: dict, name):
    """The present name of `name` in `table`; anything not renamed is itself."""
    return table.get(name, name) if isinstance(name, str) else name


def env(name: str) -> str | None:
    """The environment variable `name`, or the old name it replaced when only that is set."""
    import os
    return os.environ.get(name) or next((os.environ.get(o) for o, n in ENV.items() if n == name), None)
