"""What a wake attempt pins, and the states it passes through."""
from __future__ import annotations


# What an attempt pins. All six are compared at the agent before anything is typed, because each
# can turn over underneath a frame that was queued, retried or delivered late — and a successor
# session enrols under the same name, which is exactly the case a name alone cannot tell apart.
PINNED = ("session", "epoch", "provider_session", "runtime", "node", "os_user")

OPENED = "opened"                    # claimed; nothing has been typed
SENT = "sent"                        # input posted, awaiting the nonce
RECEIVED = "closed-received"
TIMEOUT = "closed-timeout"
REFUSED = "closed-refused"
UNCERTAIN = "closed-uncertain"       # the paste may have landed; never retried
CLOSED = frozenset({RECEIVED, TIMEOUT, REFUSED, UNCERTAIN})
STATES = frozenset({OPENED, SENT}) | CLOSED
