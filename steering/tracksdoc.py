"""A tracks document's shape, checked before a sync applies it (doc 126 §3.1).

A lane's `id` is declared in the document because only its author can say a lane was renamed
rather than removed and replaced. It is opaque: nothing parses it, and one seeded from a lane's
name keeps working after the lane is renamed.
"""
from __future__ import annotations

import re

import knowledge_shape
import track_shape

# Where a repository's tracks document is drafted when none is committed (doc 129 §2.G).
DEFAULT_SOURCE = ".2mw2lt/tracks.json"

# No longer than a lane name a card may carry: a card stores its lane's id (`cards.admissible`).
ID = re.compile(rf"[A-Za-z0-9._-]{{1,{track_shape.TRACK_MAX}}}")
REPO = re.compile(r"[\w.-]+/[\w.-]+")


def former(doc: dict) -> tuple[str, ...]:
    """The names the repository had before its current one (#3700). A fact keeps the name it
    was written under, so a branch or pull request recorded before a rename is still this
    repository's under the old name."""
    return tuple(doc.get("former_repos") or ())


def validate(doc: object, repo: str | None) -> list[str]:
    """Every reason `doc` is refused as `repo`'s tracks document; empty when it is not."""
    if not isinstance(doc, dict):
        return ["the document is not a JSON object"]
    why = []
    if doc.get("repo") != repo:
        why.append(f"the document names repository {doc.get('repo')!r}, not {repo!r}")
    lanes = doc.get("lanes")
    if not isinstance(lanes, list) or not lanes:
        return why + ["`lanes` is not a non-empty list"]
    ids, names = set(), set()
    for i, lane in enumerate(lanes):
        if not isinstance(lane, dict):
            why.append(f"lane {i} is not an object")
            continue
        lid, name = lane.get("id"), lane.get("name")
        if not isinstance(lid, str) or not ID.fullmatch(lid):
            why.append(f"lane {i} has no id matching {ID.pattern}")
        elif lid in ids:
            why.append(f"lane id {lid!r} appears twice")
        elif lid == track_shape.OFF_TRACK:
            why.append(f"lane id {lid!r} is what a card corrected to no lane carries")
        else:
            ids.add(lid)
        if not isinstance(name, str) or not name.strip():
            why.append(f"lane {i} has no name")
        elif not track_shape.valid_track(name):
            why.append(f"lane {i}'s name is not one a card may carry")
        elif name.lower() == track_shape.OFF_TRACK:
            why.append(f"lane name {name!r} is what a card corrected to no lane carries")
        elif name in names:
            why.append(f"lane name {name!r} appears twice")
        else:
            names.add(name)
        # What the board's fold reads from every lane, so a document without them is refused
        # here rather than applied and failing each fold after it.
        if not isinstance(lane.get("hue"), str):
            why.append(f"lane {i} has no hue")
        prefixes = lane.get("prefixes")
        if not isinstance(prefixes, list) or not prefixes or not all(isinstance(p, str) for p in prefixes):
            why.append(f"lane {i}'s prefixes are not a non-empty list of strings")
    for key in ("index", "ownership"):
        if key in doc and not (isinstance(doc[key], str) and doc[key].strip()):
            why.append(f"`{key}` is not a path")
    former = doc.get("former_repos")
    if former is not None and not (isinstance(former, list) and all(
            isinstance(f, str) and REPO.fullmatch(f) and f != doc.get("repo") for f in former)):
        why.append("`former_repos` is not a list of other owner/name repositories")
    kn = doc.get("knowledge")
    if kn is not None:
        # The knowledge obligations' enforcement (doc 148 §5), per stage. A stage the block does
        # not name is `tracked`; a name it does, that nothing reads — a typo'd stage key — is
        # refused here rather than silently unenforced.
        if not isinstance(kn, dict):
            why.append("`knowledge` is not an object")
        else:
            for stage, level in kn.items():
                if stage not in knowledge_shape.STAGES:
                    why.append(f"`knowledge.{stage}` is not a stage; the stages are "
                               f"{', '.join(knowledge_shape.STAGES)}")
                elif level not in knowledge_shape.LEVELS:
                    why.append(f"`knowledge.{stage}` is {level!r}, not one of "
                               f"{', '.join(knowledge_shape.LEVELS)}")
    return why
