"""Durable whole-file publication without daemon or enrollment dependencies."""
from __future__ import annotations

import os
from pathlib import Path


def fsync_dir(d: Path, *, observe=None) -> None:
    fd = os.open(d, os.O_RDONLY)
    try:
        if observe is not None:
            observe("fsync_dir")
        os.fsync(fd)
    finally:
        os.close(fd)


def write_atomic(path: Path, data: str | bytes, mode: int | None = None,
                 *, overwrite: bool = True, observe=None) -> None:
    """Publish complete data durably; exclusive publication raises FileExistsError on a winner.

    `mode` is exact; when absent, the umask decides. `observe` reports each sync boundary.
    """
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{os.urandom(4).hex()}.tmp")
    try:
        fd = os.open(tmp, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o666 if mode is None else mode)
        with os.fdopen(fd, "wb") as f:
            if mode is not None:
                os.fchmod(f.fileno(), mode)
            f.write(data.encode() if isinstance(data, str) else data)
            f.flush()
            if observe is not None:
                observe("write_atomic.fsync")
            os.fsync(f.fileno())
        if overwrite:
            os.replace(tmp, path)
        else:
            os.link(tmp, path)
            tmp.unlink()
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    fsync_dir(path.parent, observe=observe)
