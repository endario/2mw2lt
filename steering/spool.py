"""The workspace spool (doc 16 §11.2): where a hook admits an observation durably before the
daemon has it. One file per occurrence; a file is claimed by rename before it is read, so the
sender's delete and the sweep's move cannot race over one file."""
from __future__ import annotations

import json
import os
from pathlib import Path

import atomic_file
import loopio

CAP = 2000  # advisory: parallel hooks can overshoot it by a few; the sweep's count is the signal

# The frame kinds the orchestrator's say spool keeps for a session (`link.keep`), and so the ones
# a reader receipts by id: a kept frame leaves the spool only on its reader's receipt. A kind kept
# but not receipted was handed to every hold until `REPLAY_LIMIT` dropped it, one per hold.
OWED = frozenset({"say", "room"})


def owed_id(frame: object) -> str | None:
    """The id a reader receipts for this frame, or None when the spool cannot be owing it."""
    if isinstance(frame, dict) and frame.get("kind") in OWED and isinstance(frame.get("id"), str):
        return frame["id"] or None
    return None


def spool_dir(workspace: Path) -> Path:
    return workspace / ".claude" / "steering-spool"


def _blocking(site: str) -> None:
    loopio.blocking(f"spool.{site}")


def fsync_dir(d: Path) -> None:
    atomic_file.fsync_dir(d, observe=_blocking)


def write_atomic(path: Path, data: str | bytes, mode: int | None = None, *, overwrite: bool = True) -> None:
    atomic_file.write_atomic(path, data, mode, overwrite=overwrite, observe=_blocking)


def ensure_dir(d: Path) -> None:
    """Create a directory durably: on first use its parent's entry for it is synced too."""
    if d.is_dir():
        return
    d.mkdir(parents=True, exist_ok=True, mode=0o700)
    fsync_dir(d.parent)


def admit_at(d: Path, key: str, value: dict, cap: int | None = CAP) -> Path | None:
    """Write one keyed claim durably in `d`; None when that spool is over its cap."""
    ensure_dir(d)
    if cap is not None and sum(1 for _ in d.glob("*.json")) >= cap:
        return None
    final = d / f"{key}.json"
    write_atomic(final, json.dumps(value, sort_keys=True))
    return final


def admit(workspace: Path, o: dict) -> Path | None:
    """Write the observation durably. None lets the hook answer while counts expose the cap."""
    return admit_at(spool_dir(workspace), o["occurrence"], o)


def claim(path: Path) -> Path | None:
    """Take a spool file for delivery by renaming it; None if someone else already did."""
    claimed = path.with_suffix(".claimed")
    try:
        os.rename(path, claimed)
    except FileNotFoundError:
        return None
    return claimed


def release(claimed: Path) -> None:
    """Put a claimed file back for a later attempt."""
    try:
        os.rename(claimed, claimed.with_suffix(".json"))
    except FileNotFoundError:
        pass


def accept(claimed: Path) -> None:
    """Remove a claim after its sink durably accepted it."""
    try:
        claimed.unlink()
    except FileNotFoundError:
        return
    fsync_dir(claimed.parent)


def reject(claimed: Path, why: str) -> None:
    """Quarantine a refused file with its reason beside it."""
    q = claimed.parent / "rejected"
    q.mkdir(exist_ok=True, mode=0o700)
    dest = q / claimed.with_suffix(".json").name
    os.replace(claimed, dest)
    dest.with_suffix(".why").write_text(why + "\n")


def pending_at(d: Path) -> list[Path]:
    """Unclaimed files in one spool, oldest first.

    A file claimed or delivered between the glob and its stat is gone, not an error: this spool
    exists to be drained concurrently, and sorting by a stat that may not answer raised through
    the caller instead (#1896).
    """
    if not d.exists():
        return []
    dated = []
    for p in d.glob("*.json"):
        try:
            dated.append((p.stat().st_mtime, p))
        except OSError:
            continue
    return [p for _, p in sorted(dated, key=lambda x: x[0])]


def pending(workspace: Path) -> list[Path]:
    return pending_at(spool_dir(workspace))


def recover_at(d: Path) -> int:
    """A `.claimed` file belongs to a delivery that never finished — a daemon that died between
    the claim and the store. Put every one back so the next sweep retries it."""
    n = 0
    for p in d.glob("*.claimed") if d.exists() else []:
        release(p); n += 1
    return n


def recover(workspace: Path) -> int:
    return recover_at(spool_dir(workspace))


def drain_at(d: Path, deliver) -> dict:
    """Offer every pending claim to `deliver`, oldest first, and dispose of each by its
    answer: `retry` leaves it for the next pass, `refused` quarantines it with the reason, and
    anything else is delivered and gone. Counted by the key the answer leads with."""
    n: dict[str, int] = {}
    def counted(key: str) -> None:
        n[key] = n.get(key, 0) + 1
    for p in pending_at(d):
        claimed = claim(p)
        if claimed is None:
            continue
        try:
            o = json.loads(claimed.read_text())
        except ValueError as e:  # not JSON: poison, quarantined with its reason
            reject(claimed, f"unreadable: {e}"); counted("rejected")
            continue
        except OSError:  # the file, not the content: try again next pass
            release(claimed); counted("released")
            continue
        try:
            res = deliver(o)
        except Exception:  # the delivery failed, the observation did not: keep it pending
            release(claimed); counted("released")
            continue
        if res.get("retry"):  # a newer sender than this sink: wait for a newer sink
            release(claimed); counted("released")
        elif "refused" in res:
            reject(claimed, res["refused"]); counted("rejected")
        else:
            accept(claimed); counted(next(iter(res)))
    return n


def drain(workspace: Path, deliver) -> dict:
    return drain_at(spool_dir(workspace), deliver)


def counts_at(d: Path) -> dict:
    """How much this spool is holding. A reading, so a file that goes while it is being taken
    is one fewer, never an error.

    `pending_at` tolerates the same window and this stats the very list it just returned, so a
    drain claiming a file in between raised `FileNotFoundError` out of whatever asked — which
    here is the health endpoint, reading every workspace's spool off the loop while the agent
    drains it (#1870)."""
    files = pending_at(d)
    rejected = sum(1 for _ in (d / "rejected").glob("*.json")) if (d / "rejected").exists() else 0
    ages = []
    for p in files:
        try:
            ages.append(p.stat().st_mtime)
        except OSError:
            continue
    return {"spooled": len(ages), "rejected": rejected, "oldest_spooled": min(ages, default=None)}


def counts(workspace: Path) -> dict:
    return counts_at(spool_dir(workspace))
