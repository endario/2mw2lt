---
description: Commission an independent review of your pull request, or a critique of your design, from steering. In a workspace 2mw2lt steers, this is the review and critic gate, not the personal runner.
---

Run it from the checkout on your branch, after pushing: steering reviews the commit at `origin`,
never your working tree. Use the session name connect printed. A session in a linked worktree
claims the branch before each gate: a session holds one branch at a time.

If connect needed explicit `--provider`/`--provider-session`, keep those flags immediately after
`taking.py` or `gate.py`, for every gate action. For Codex without session exports:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/steering/enroll/taking.py" --provider codex --provider-session <thread-id> <your session> branch
python3 "${CLAUDE_PLUGIN_ROOT}/steering/enroll/gate.py" --provider codex --provider-session <thread-id> <your session> review <pr number>
python3 "${CLAUDE_PLUGIN_ROOT}/steering/enroll/gate.py" --provider codex --provider-session <thread-id> <your session> status <commission id>
```

Replace `<thread-id>` with the thread id used to connect, and keep that prefix for `critic`,
`cancel`, `pr` and `carry` too. The [explicit-identity reference](../connect/stream-reference.md#explicit-speaker-identity)
shows the same rule for speech and holds.

For the client's forms and flag examples, without commissioning:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/steering/enroll/gate.py" --help
```

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/steering/enroll/taking.py" <your session> branch
python3 "${CLAUDE_PLUGIN_ROOT}/steering/enroll/gate.py" <your session> review <pr number>
python3 "${CLAUDE_PLUGIN_ROOT}/steering/enroll/gate.py" <your session> critic <doc path>
```

`commissioned: <id> review round <n>/<cap> by <vendor> on <node> (<tier>, <harness> harness[, final][, deeper])` — a
reviewer from a vendor that wrote none of the branch has the run. Steering chooses it; there is no
provider to pass.

`--tier heavy` runs the series on each vendor's heavy model (Sol, Opus, GLM-5.3) — for a large
diff, a new mechanism, or a security, concurrency or attestation property; later rounds keep the
tier unless they name one. `--final` declares this round the last: it runs deeper than the finding
rounds, from a vendor no earlier round used where one can take it, and after its verdict steering
refuses another round, as at the ceiling. The round at the ceiling is final without asking. The
review's run details say the tier and how the final round ran.

The reviewer holds its own full tool set — shell, network, builds and tests — in a clone with no
remote, under a cage that keeps the machine's credentials and every other checkout out of its
reach — all but the reviewing CLI's own sign-in, which only Codex keeps from its tools.
`--sandbox` gives it the reading tools only; later rounds keep it until one names `--full`. It reads the repository's own persona,
`.claude/agents/code-reviewer.md` (or the one `.claude/independent-gates.map` routes the change
to) or `architecture-critic.md`, from the base commit, so a change cannot rewrite its own
reviewer; the bundled persona is the fallback. The run details name the harness and the persona.

## Beside the personal runner

In a repository 2mw2lt steers this gate replaces `/independent-review` and `/independent-critic`.

| | this gate | the runner |
|---|---|---|
| reviewer | a vendor that wrote none of the branch, chosen by usage | routed by round and usage, away from the host |
| tier, final round | `--tier`, `--final`; the final round deeper, from a vendor no earlier round used | `--tier`, `--complement-of` |
| tools | full inside a cage that withholds every credential but the reviewer's own; `--sandbox` to read only | full with the host's access; `--sandbox` |
| persona | the project's, read from the base commit | the project's, read from the checkout |
| record | a fact per round, published by `2mw2lt[bot]` with a status pinned to the commit | a local artifact, published as the user |
| rounds | capped centrally, lifted by the seat, briefed with earlier findings | counted by the caller |
| scope | a pushed commit | also a local branch or uncommitted work |

The result comes to you as a `say` frame `from: gate <id>` on the stream you hold: the verdict,
each finding as `severity file:line claim`, and the reviewer's report, in several frames when it is
long. A review is also published to the pull request by `2mw2lt[bot]`, pinned to the commit it
read, with a `2mw2lt/review` status on that commit, unless the pull request closed or that commit
left it first. A critique is on the stream only.

A harness that holds no stream, Codex, is never sent that frame. It asks instead, and gets the same
text, or `pending: …` while the run is out:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/steering/enroll/gate.py" <your session> status <commission id>
```

When every eligible reviewer is busy, the commission waits instead of being refused: you are
told `queued: <id> <priority>, <n> ahead`, and it is offered, highest priority first and then the
oldest, as reviewers free.
Do not commission it again: a second commission on that series is refused while it waits. One
still queued after an hour is refused, and `cancel` withdraws a queued one.

Fix what you verify, push, and commission again: the next round is briefed with every earlier
round's findings. A `ship it` closes the gate. At the ceiling steering refuses another round and
the owner hears it: build or decompose.

`main` merges only a head holding the App's `2mw2lt/review`, which a pass sets on the commit it
judged. After pushing the round's own fixes past `ship it`, carry the pass to the new head, from
the branch's worktree:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/steering/enroll/gate.py" <your session> carry <pr number>
```

It is refused for a rebase, a resolved conflict, or a commit this checkout did not make: commission
a round instead. A docs-only pull request needs a review too, since a critique sets no status.

When the answer is lost, the carry says it may have landed: send the same command again with the
`--retry=<id>` it printed, and the door answers from its record rather than carrying twice.

Discovered the artifact a round is running against is wrong before it answers? Withdraw it —
this succeeds only while the run is still out, refused as too late otherwise:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/steering/enroll/gate.py" <your session> cancel <commission id>
```

Whether a pull request was ever gated, by any session on this workspace, and where each review
ended and was published (#1986):

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/steering/enroll/gate.py" <your session> pr <pr number>
```

Keep the pull request a draft through every round. A draft runs no steering suite, so before each
round run the focused guards for what changed, and a full suite only when the change warrants one,
locally (a machine many sessions share) or on the hosted lane (`gh workflow run steering.yml --ref
<branch>`, paid Actions minutes). Mark it ready only after `ship it`: that is its one required CI
run, then the merge.

`REJECTED … does not hold <branch>` — claim it with `taking.py`, above. `unresolved: …` — no
reviewer could take it; report the blocker rather than reviewing your own work:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/steering/enroll/verb.py" blocked <your session> on "<gate blocker>"
```
