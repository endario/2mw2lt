"""Blocking-boundary observations shared by daemon diagnostics and the loop guard."""
from __future__ import annotations

import itertools
import os
import sys
import threading
import time

MEASUREMENT = f'{os.getpid()}:{time.monotonic_ns()}'
LIMIT = 200
RETAINED_LIMIT = 4096
_sites: dict[tuple, tuple[str, dict]] = {}
_overflow = {'on_loop': 0, 'off_loop': 0}
_lock = threading.Lock()


def report(site: str, stack: str) -> None:
    print(f'loopio: on-loop blocking boundary {site}\n{stack}', file=sys.stderr, flush=True)


def blocking(site: str) -> bool:
    asyncio = sys.modules.get('asyncio')
    on_loop = False
    if asyncio is not None:
        try:
            asyncio.get_running_loop()
            on_loop = True
        except RuntimeError:
            pass
    # Frame locations only: traceback.format_stack also reads source lines, which can carry
    # literal message bodies or credentials. Neither source text nor locals belong in the log.
    frames, frame = [], sys._getframe(1)
    try:
        while frame is not None:
            code = frame.f_code
            frames.append((code.co_filename, frame.f_lineno, getattr(code, 'co_qualname', code.co_name)))
            frame = frame.f_back
    finally:
        del frame
    signature = (site, tuple(frames))
    field = 'on_loop' if on_loop else 'off_loop'
    with _lock:
        known = _sites.get(signature)
        overflow = known is None and len(_sites) >= RETAINED_LIMIT
        if overflow:
            first = not any(_overflow.values())
            _overflow[field] += 1
        else:
            if known is None:
                stack = '\n'.join(f'  {filename}:{line} in {name}' for filename, line, name in reversed(frames))
                key = str(len(_sites))
                row = {'site': site, 'stack': stack, 'on_loop': 0, 'off_loop': 0}
                _sites[signature] = (key, row)
            else:
                _, row = known
            first = on_loop and row['on_loop'] == 0
            row[field] += 1
    if first:
        try:
            if overflow:
                print('loopio: caller-signature ceiling reached; boundary coverage is incomplete',
                      file=sys.stderr, flush=True)
            else:
                report(site, row['stack'])
        except (OSError, ValueError):
            pass   # diagnostics must not interrupt the write they observe
    return on_loop


_role = threading.local()


def disk_job(fn):
    """`fn()`, marked as a disk-only job for its duration: the lane that persists the loop's
    frames runs these, and a ledger entry reached from one raises (#3401)."""
    previous = getattr(_role, 'disk_only', False)
    _role.disk_only = True
    try:
        return fn()
    finally:
        _role.disk_only = previous


def require_ledger_thread() -> None:
    """Refuse the ledger to a disk-only job. A once-settlement holds the ledger's writer while it
    waits on the loop, and the loop may be waiting on this lane: a lane job waiting for that
    writer would wait for good."""
    if getattr(_role, 'disk_only', False):
        blocking('loopio.disk_job.ledger')
        raise RuntimeError('a disk-only job reached the ledger')


def snapshot() -> dict:
    with _lock:
        return {'measurement': MEASUREMENT,
                'sites': {key: dict(row) for key, row in itertools.islice(_sites.values(), LIMIT)},
                'overflow': dict(_overflow),
                'truncated': max(0, len(_sites) - LIMIT)}
