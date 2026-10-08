"""A directory kept on this machine for later runs, under a lock of its own beside it: the
repository caches (doc 136 §6) and the prepared gate environments (#2800)."""
from __future__ import annotations

import contextlib
import fcntl
import os
import shutil
from pathlib import Path
from typing import Callable, Iterable


@contextlib.contextmanager
def locked(entry: Path, block: bool = True):
    """The entry's lock, held. The lock file is removed with its entry, so one opened before that
    removal is not the lock any more: it is taken again until the file held is the one named."""
    path = entry.parent / f"{entry.name}.lock"
    while True:
        lock = open(path, "a")
        try:
            fcntl.flock(lock, fcntl.LOCK_EX if block else fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            lock.close()
            raise
        try:
            same = os.fstat(lock.fileno()).st_ino == os.stat(path).st_ino
        except FileNotFoundError:
            same = False
        if same:
            break
        lock.close()
    try:
        yield path
    finally:
        lock.close()


def sweep(entries: Iterable[Path], now: float, days: int) -> list[Path]:
    """Remove the entries not used for `days`, with their locks. An entry whose lock is held is in
    use and kept."""
    removed = []
    for entry in entries:
        try:
            with locked(entry, block=False) as path:
                if not entry.exists():
                    # Another sweep removed it while this one waited; the lock it left is this one's.
                    path.unlink(missing_ok=True)
                    continue
                if now - entry.stat().st_mtime <= days * 86400:
                    continue
                if entry.is_dir():
                    shutil.rmtree(entry)
                else:
                    entry.unlink()
                path.unlink()
                removed.append(entry)
        except (BlockingIOError, OSError):
            continue
    return removed


def evict(entries: Iterable[Path], enough: Callable[[], bool]) -> list[Path]:
    """Remove the least recently used of `entries`, with their locks, until `enough()` holds. One
    whose lock is held is in use and kept."""
    def used(p: Path) -> float:
        try:
            return p.stat().st_mtime
        except OSError:
            return 0.0
    removed: list[Path] = []
    for entry in sorted(entries, key=used):
        if enough():
            break
        removed += sweep([entry], now=float("inf"), days=0)
    return removed
