---
name: tracks
description: Use when placing a card or issue in a track, triaging an uncertain classification, or proposing a change to a workspace's track lanes.
---

# Tracks

A track is a product capability, not a code ownership bucket. Read these principles before judging a placement or changing the taxonomy.

## Track principles

A track names a product capability by its purpose, not an internal research project, code directory or team. Taken together, a workspace's tracks must cover its product's purpose end to end. A gap in that coverage is a reason to revise the tracks, not to hide work under a convenient existing lane.

Each product lane's definition must stand alone for someone who has never seen the code (Unassigned is a holding place, not a capability):

- **Outcome:** What the product enables a person to do, or what result it guarantees.
- **Not:** The nearest plausible work that belongs elsewhere. Draw the boundary in the reader's terms, not by implementation path.
- **Ladder:** The outcome this capability moves toward. State it in the lane's ladder line (`ms` in the tracks document), not just in its name.

A track is primary for a card when it owns the card's outcome; other capabilities the card genuinely advances are secondary labels. Choose the primary from the card's declared track, then its anchored issues' tracks, then the classifier's reading of the card's descriptions. The diff is a last resort only if no description exists. File paths and branch prefixes do not choose a track. A low-confidence result is a judgment for the brain to check, not a reason to invent a lane. Repository upkeep (CI, releases, dependency updates, repository guards and developer docs) is off-track; it does not become a product capability because it touches one.

Each lane has a stable `id` distinct from its changeable name: keep the id when renaming a capability, and use a new id for a different capability. A pull request changes the tracks document; sync brings the merged definition into the server's copy, which is what the brain reads. A stale or drifted copy is investigated, never edited by hand.

## Read this workspace's tracks

The repository document named by the authority manifest's `tracks_source` is canonical (`source` in a checkout-mode workspace definition). The daemon serves its synced copy at `GET /w/<authority>/api/v1/tracks`, including lane ids, source commit, staleness and drift, and the commit last synced, awaited or failed. Read it **before** placing or classifying work. From the workspace, the client resolves the authority and door for you:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/steering/enroll/rest.py" /tracks </dev/null
```

The read goes on your own enrolment. Never put a token in a URL or a response. If tracks are stale or drifted, inspect the source and sync result before treating the server's lanes as current.

## Place and curate

Read the card's declaration, owning issue anchors and descriptions in that order. Compare their intended outcome with the lanes' outcome, Not and ladder lines; secondary capabilities remain labels, not competing primary lanes. File paths and branch prefixes are not evidence of a capability. When the classifier marks a placement low-confidence or refers it, inspect the issue or card. The brain can correct a **card** to a product lane with `reclassify` (see `/2mw2lt:brain`); resolve an issue's track through its issue label or lane epic. A card classified off-track belongs outside the product lanes when its work is repository upkeep; if it belongs in a capability, reclassify it to that lane. If upkeep was placed in a product lane, the brain corrects it with `reclassify <card> track off-track`. The classifier does not offer off-track for issues.

Propose a lane only if it names a distinct capability serving the product north star, has an outcome a newcomer can understand, a Not boundary and a ladder line, and is not repository upkeep or a cross-cutting label. Change the repository tracks document by pull request, retaining stable ids across renames. Never change tracks through a board write. After merge, read `/tracks` to check that the server synced the intended commit. Go syncs the lanes as the workspace's mirror moves, so there is no sync to request.
