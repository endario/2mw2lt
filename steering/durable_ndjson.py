"""Crash-consistent append and torn-tail recovery shared by durable NDJSON stores."""
from __future__ import annotations

import fcntl
import json
import os
import time
from pathlib import Path


class AppendFailure(Exception):
    def __init__(self, error: OSError, path: Path, rollback: OSError | None = None):
        self.error = error
        self.path = path
        self.rollback = rollback

    def refusal(self, store: str) -> str:
        cause = f" ({self.error.strerror})" if self.error.strerror else ""
        out = f"{store} store failed: errno {self.error.errno}{cause} at {self.path}"
        if self.rollback is not None:
            rollback_cause = f" ({self.rollback.strerror})" if self.rollback.strerror else ""
            out += f"; rollback failed: errno {self.rollback.errno}{rollback_cause}"
        return out


def append(path: Path, root: Path, schema: dict, row: dict) -> None:
    """Append one row under an exclusive lock and make a newly created name durable."""
    created = not path.exists()
    failed_at = path
    try:
        with open(path, "a") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            before = os.fstat(handle.fileno()).st_size
            try:
                if before == 0:
                    handle.write(json.dumps(schema, sort_keys=True) + "\n")
                handle.write(json.dumps(row, sort_keys=True) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
                if created:
                    failed_at = root
                    directory = os.open(root, os.O_RDONLY)
                    try:
                        os.fsync(directory)
                    finally:
                        os.close(directory)
            except OSError as error:
                try:
                    handle.truncate(before)
                    if created:
                        path.unlink(missing_ok=True)
                except OSError as rollback:
                    raise AppendFailure(error, failed_at, rollback) from None
                raise AppendFailure(error, failed_at) from None
    except AppendFailure:
        raise
    except OSError as error:
        raise AppendFailure(error, failed_at) from None


def tail(path: Path, at: int) -> tuple[bool, int, list[bytes]]:
    """Read complete lines appended after ``at`` under the append lock.

    The offset stays before an incomplete final line. A missing or shorter file resets it, and a
    shorter replacement is returned from its beginning in the same read.
    """
    try:
        if path.stat().st_size == at:
            return False, at, []
        with open(path, "rb") as handle:
            fcntl.flock(handle, fcntl.LOCK_SH)
            size = os.fstat(handle.fileno()).st_size
            reset = size < at
            if reset:
                at = 0
            if size == at:
                return reset, at, []
            handle.seek(at)
            raw = handle.read(size - at)
    except FileNotFoundError:
        return at > 0, 0, []
    cut = raw.rfind(b"\n")
    if cut == -1:
        return reset, at, []
    return reset, at + cut + 1, raw[:cut].splitlines()


def recover(path: Path, root: Path) -> tuple[bytes, Path | None]:
    """Cut a torn final line after durably preserving it; return the complete prefix."""
    raw = path.read_bytes()
    if not raw or raw.endswith(b"\n"):
        return raw, None
    cut = raw.rfind(b"\n") + 1
    number = 0
    while True:
        aside = path.with_name(
            f"{path.name}.torn-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}-{number}")
        try:
            fd = os.open(aside, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            break
        except FileExistsError:
            number += 1
    with os.fdopen(fd, "wb") as torn:
        torn.write(raw[cut:])
        torn.flush()
        os.fsync(torn.fileno())
    directory = os.open(root, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)
    with open(path, "r+b") as source:
        source.truncate(cut)
        source.flush()
        os.fsync(source.fileno())
    return raw[:cut], aside
