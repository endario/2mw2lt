"""One safe, joinable vocabulary for agent-to-daemon request records (#1867)."""
from __future__ import annotations

import hashlib
import json
import re
import secrets
import time
from typing import Callable


HEADER = "X-Steering-Request"
REQUEST_ID = re.compile(r"[A-Za-z0-9-]{16,64}")
PLAIN = re.compile(r"[A-Za-z0-9_./:@+-]+")


def new_id() -> str:
    return secrets.token_hex(12)


def of_key(key: str) -> str:
    """The name a keyed door write travels, is logged and is recorded under. The key itself is a
    bearer secret — a resent `enroll:` is reissued its credential on it alone (#1122) — so what
    is written down is its digest, and one digest greps across the client, the request log, the
    ledger and the journal."""
    return hashlib.sha256(key.encode()).hexdigest()[:32]


def accepted(raw: str | None) -> tuple[str, str]:
    if isinstance(raw, str) and REQUEST_ID.fullmatch(raw):
        return raw, "agent"
    return new_id(), "daemon"


def _value(value: object) -> str:
    rendered = str(value)
    return rendered if PLAIN.fullmatch(rendered) else json.dumps(rendered, ensure_ascii=True)


def fields(**values: object) -> str:
    return " ".join(f"{key}={_value(value)}" for key, value in sorted(values.items()))


def method_of(request) -> str:
    return str(getattr(request, "method", None) or "-")


def path_of(request) -> str:
    return str(getattr(getattr(request, "url", None), "path", None) or "-")


def age(request, now: Callable[[], float] = time.monotonic) -> str:
    started = getattr(getattr(request, "state", None), "started", None)
    return "?" if not isinstance(started, (int, float)) else f"{now() - started:.2f}s"


def timed(app: Callable, emit: Callable[[str], None]) -> Callable:
    """`app`, with a request record emitted when its response starts: for a stream that is the
    wait before its first frame, not how long it was held open."""
    async def wrapped(scope, receive, send):
        if scope.get("type") != "http":
            return await app(scope, receive, send)
        started = time.monotonic()
        done = False

        def record(status: object) -> None:
            nonlocal done
            done = True
            emit("request " + fields(elapsed=f"{time.monotonic() - started:.2f}s",
                                     method=scope.get("method", "-"), path=scope.get("path", "-"),
                                     status=status))

        async def sending(message):
            if message.get("type") == "http.response.start" and not done:
                record(message.get("status", "-"))
            await send(message)
        try:
            await app(scope, receive, sending)
        finally:
            if not done:
                record("-")
    return wrapped
