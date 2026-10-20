---
name: steward
description: Run a goal the seat or the owner appointed you to steward — launch its workers by floor, retier, rescue and direct them, within the bounds you were given, and hand the goal back when it is done.
---

You were appointed this goal's steward.
You run its cards so the seat no longer relays each call: you launch their workers, retier them,
rescue the sessions your launches started and direct them. Everything else stays the seat's:
shaping the goal's cards, engaging a session you did not launch, judging its criteria, and
anything outside the goal. Ask the brain for those with `say.py`.

Your authority is the stewardship and nothing else. It stands only while you are enrolled at the
epoch you were appointed at and its `until` has not passed. Leaving ends it, and so does an
enrolment under a new epoch, so do not disconnect mid-goal; hand it back instead.

Every command below runs from this workspace. `say` is the lease client the brain uses:

```bash
say() { printf '%s' "$1" | python3 "${CLAUDE_PLUGIN_ROOT}/steering/enroll/lease.py" say; }
rest() { python3 "${CLAUDE_PLUGIN_ROOT}/steering/enroll/rest.py" "$@"; }
```

## Read your bounds

```bash
rest "/goals/<goal>/steward"
```

The answer names your ceiling (the highest rarity you may launch at), your concurrency (how many
sessions may run the goal's cards at once, counting launches not yet started), the accounts
(`vendor/account`) and machines placement may use (empty means any), and the `until`. The seat or
the owner may change them at any time; read them again before a launch that would reach a bound.

## Steer by the execution read

```bash
rest "/goals/<goal>/execution"
rest "/placement?floor=<rarity>&effort=<level>"
rest "/sessions"
```

The execution read is the goal's working set: each unit's `next_act` and its basis, as the brain
reads it (`/2mw2lt:brain`, Run a goal). Read before each decision rather than acting on the last
notice. A card in the goal is yours; a card outside it is refused `stewardship-not-covering`.

## Launch by floor

Ask by floor only: placement chooses the model, harness, account and machine within your bounds.
Name the card by its id or by the issue it resolves, and brief the worker on its own line:

```bash
say "launch: token @lease floor <rarity> effort <level> card <card id> because <why>
brief: <what the worker does, the issue, the design and its section, and what done is to report>"
```

The launch tells you its outcome unless you name another to `notify`. The worker reads your brief
as your own directive, under the line `from: session <you>, steward of goal <goal>, within the
seat's bounds`. A brief names commands, never intentions, as the brain's do: write
`/2mw2lt:gate`, `taking.py <session> <issue>` and `/2mw2lt:disconnect`, not "get it reviewed".

Refusals name the bound you met:

| Refusal | Meaning |
|---|---|
| `stewardship-pin` | you named a model, vendor, harness, account or machine; ask by floor |
| `stewardship-ceiling` | the floor is above your ceiling; ask the brain to raise it, or ask lower |
| `stewardship-concurrency-full` | your concurrency is taken, by sessions running or launches still waiting |
| `stewardship-not-covering` | the card, session or launch is not this goal's |
| `stewardship-ended`, `stewardship-epoch-stale` | your stewardship no longer stands; stop and tell the brain |

## Retier a card

When a card proves harder or easier than its floor:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/steering/enroll/card.py" --lease retier <card> <stage> <floor> <why>
```

The new floor is at or below your ceiling.

## Rescue the sessions you launched

Only a session one of your launches started is yours to rescue:

```bash
say "wake: token @lease <session> because <why>"
say "park: token @lease <session> [until <time or duration>] because <the work that needs its slot>"
say "retire: token @lease <session> because <why>"
say "keep: token @lease <session> [until <time or duration>] because <the work expected in its territory>"
say "unkeep: token @lease <session>"
```

The same verbs are the `mcp__2mw2lt__wake`, `park`, `retire`, `keep` and `unkeep` tools; load them
with ToolSearch. Changing a session's model or moving it to another account (`control`, `rehome`)
stays the seat's.

## Direct them

```bash
say "relay: token @lease to <session> <the directive, with the context it needs>"
```

It reaches only a session your launches started. A busy session queues it, one at a time: a
second while the first still waits is refused `directive-busy`. A directive still waiting when
your stewardship ends lapses rather than arriving under your name.

## Have workers report to you

Ask each worker in its brief to report to you, not the brain:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/steering/enroll/say.py" --to <you> <their session> "<report>"
```

Report the goal's progress to the brain at the points it asked for, with `say.py <you> "<report>"`.

## Hand the goal back

When the goal's work is done, or you can no longer run it:

```bash
rest --post "/goals/<goal>/steward/end" --json '{"reason": "<why, and where the goal stands>"}' --key "<new uuid>"
```

Then tell the brain what is done, what is open and what it should judge. Launches still waiting
under your stewardship lapse as it ends; the sessions you started keep running and report their
own outcomes.
