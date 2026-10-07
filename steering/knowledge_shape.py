"""The tracks document's knowledge-stage and enforcement-level grammar (doc 148 §5)."""

# The knowledge obligations' enforcement ramp (doc 148 §5), per workspace and per stage, set in
# the tracks document. `tracked` — a new tenant's start — keeps a stage on the card alone;
# `surfaced` raises it as the brain's row; `required` belongs to #2998.
LEVELS = ("tracked", "surfaced", "required")
STAGES = ("design", "record", "harvest")
