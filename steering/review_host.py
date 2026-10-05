"""A review host's side of doc 136 §5: the team's clusters it serves for gates, with no install.

`~/.config/2mw2lt/review-host.json`, mode 0600, holds the console, the door base and the review
host's secret, written by `agent_secret.py --review-host`. The team's clusters are listed at most
every `LIST_EVERY` seconds, and each one gets a credential of its own on first sight, stored as any
enrolment is (`credential.enrol` with its cluster named). A door this machine already holds an
enrolment for is left to that enrolment: an installed workspace takes precedence.
"""
from __future__ import annotations

import json
import os
import threading
import time
import urllib.parse
from pathlib import Path
from typing import Callable

import credential
import spool

LIST_EVERY = 300.0


class Key(str):
    """The key a review uplink is served under: its door, told apart from a registry root."""


def path() -> Path:
    return Path(os.environ.get("STEERING_REVIEW_HOST") or Path.home() / ".config" / "2mw2lt" / "review-host.json")


def read(p: Path | None = None) -> dict | None:
    """The declaration, or None when this machine is not a review host."""
    try:
        d = json.loads((p or path()).read_text())
    except (OSError, ValueError):
        return None
    return d if isinstance(d, dict) and all(isinstance(d.get(k), str) and d[k] for k in ("console", "door", "secret")) else None


def write(console: str, door: str, secret: str, p: Path | None = None) -> Path:
    p = p or path()
    p.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    spool.write_atomic(p, json.dumps({"console": console.rstrip("/"), "door": door.rstrip("/"), "secret": secret}),
                       mode=0o600)
    return p


def list_clusters(d: dict, timeout: float = 10.0) -> list[str]:
    """The team's clusters, as the console lists them to this review host's secret."""
    try:
        got = credential.post(d["console"], "/api/agents/review-host/clusters", {"secret": d["secret"]}, timeout)
    except credential.NoCredential as e:
        raise RuntimeError(str(e))
    clusters = got.get("clusters")
    if not isinstance(clusters, list) or not all(isinstance(c, str) and c for c in clusters):
        raise RuntimeError("the console's listing names no clusters")
    return clusters


def _minted_with(door: str, secret: str) -> bool:
    """Whether this machine holds an enrolment for `door` that it may keep: one an install made,
    or one this review host made with the secret it holds now. A review enrolment made with an
    earlier secret is not kept: that secret has been replaced, and is likely revoked."""
    try:
        held = json.loads(credential.path_for(door).read_text())
    except (OSError, ValueError):
        return False
    return not held.get("cluster") or held.get("secret") == secret


class Listing:
    """The team's doors to serve for reviews. The listing is read on a thread of its own at most
    every `LIST_EVERY` seconds, so the registry read never waits on the console, and a failed
    read waits the same before the next: the console's per-address limit is shared with every
    credential renewal on this machine. A listing that fails keeps the last one read: an outage
    at the console is not a removal."""

    ENROL_PER_READ = 5   # under the console's ten a minute per address, with room for renewals

    def __init__(self, log: Callable[[str], None], lister=list_clusters, enrol=credential.enrol,
                 clock=time.monotonic, background: bool = True):
        self.log, self.lister, self.enrol, self.clock = log, lister, enrol, clock
        self.background = background
        self.doors: list[str] = []
        self.read_at: float | None = None
        self._reading = threading.Lock()

    def refresh(self, d: dict) -> None:
        """Read the listing and enrol what it names that this machine holds no enrolment for."""
        if not self._reading.acquire(blocking=False):
            return
        try:
            self.read_at = self.clock()
            try:
                clusters = self.lister(d)
            except RuntimeError as e:
                self.log(f"review host: {e}; serving the last listing")
                return
            doors, enrolled = [], 0
            for cluster in clusters:
                door = f"{d['door']}/w/{urllib.parse.quote(cluster, safe='')}"
                if not _minted_with(door, d["secret"]):
                    if enrolled >= self.ENROL_PER_READ:
                        continue   # the next read enrols it
                    enrolled += 1
                    try:
                        self.enrol(door, d["console"], d["secret"], cluster=cluster)
                        self.log(f"review host: enrolled for {door}")
                    except credential.NoCredential as e:
                        self.log(f"review host: {door} not served: {e}")
                        continue
                doors.append(door)
            self.doors = doors
        finally:
            self._reading.release()

    def doors_now(self, d: dict) -> list[str]:
        if self.read_at is None or self.clock() - self.read_at >= LIST_EVERY:
            if self.background:
                threading.Thread(target=self.refresh, args=(d,), daemon=True, name="review-listing").start()
            else:
                self.refresh(d)
        return self.doors

    def entries(self, registered: dict, headers: dict, installed: set[str]) -> dict:
        """`registered` with a review entry added for each listed door no registry entry names.
        `installed` is every door the registry names, served this time or not: one whose checkout
        could not be read this poll is still installed, and a review uplink to it would replace
        the installed agent's at the daemon (#2340)."""
        d = read()
        if d is None:
            return registered
        installed = {door.rstrip("/") for door in installed}
        return {**registered, **{Key(door): (door, dict(headers))
                                 for door in self.doors_now(d) if door not in installed}}


def park_home(door: str) -> Path:
    """Where a review host's gate runs for `door` park: the agent's own directory, one per door."""
    import agentjob
    import hashlib
    return agentjob.root(Path.home()) / "review" / hashlib.sha256(door.rstrip("/").encode()).hexdigest()[:16]
