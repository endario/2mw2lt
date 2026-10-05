"""Checkout workspace binding and deployment configuration."""
from __future__ import annotations

import json
import os
from pathlib import Path

HERE = Path(__file__).resolve().parent
MANIFEST = HERE / "workspaces.json"

# Where a checkout records which workspace of this deployment it is. Written by
# `hooks.py install`, read by everything else.
BINDING = Path(".claude") / "steering-workspace"


def workspaces() -> list[dict]:
    """Every workspace this deployment knows how to draw a board for."""
    try:
        return list(json.loads(MANIFEST.read_text())["workspaces"])
    except (OSError, ValueError, KeyError, TypeError):
        return []


def entry(workspace_id: str) -> dict | None:
    return next((w for w in workspaces() if w.get("id") == workspace_id), None)


def id_for_repo(slug: str) -> str | None:
    """The workspace id this deployment gives a repository, or None if it names none."""
    return next((w["id"] for w in workspaces()
                 if w.get("repo") == slug and w.get("id")), None)


def id_at(root: Path) -> str | None:
    """The workspace id this checkout is bound to, or None when nothing has bound it.

    Read rather than derived. The id used to be the root's directory name, which made the
    binding a coincidence: the same repository is checked out under a different name on
    the next machine, and the daemon then drew another workspace's board or none at all.
    `hooks.py install` resolves it once, from the checkout's own `origin` and the manifest
    above, and writes it here — one file read on the card write path, no git.
    """
    try:
        wid = (Path(root) / BINDING).read_text().strip()
    except OSError:
        return None
    return wid or None


def bind(root: Path, workspace_id: str) -> Path:
    """Record which workspace this checkout is. Idempotent; the caller resolved the id."""
    p = Path(root) / BINDING
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(workspace_id + "\n")
    return p


def tracks_path(workspace_id: str | None) -> Path | None:
    """The tracks file for a workspace, or None when this deployment names none for it.

    `STEERING_TRACKS` overrides, which is how a guard points a real appender at a fixture.
    A workspace the manifest does not name gets no lanes rather than another workspace's:
    lanes are what card admission checks against, and the wrong ones would file a card
    under a track that does not own it.
    """
    override = os.environ.get("STEERING_TRACKS")
    if override:
        return Path(override)
    w = entry(workspace_id) if workspace_id else None
    if not w or not w.get("tracks"):
        return None
    return HERE / w["tracks"]
