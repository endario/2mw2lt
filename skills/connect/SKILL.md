---
name: connect
description: Connect at session start or restart, and hold the stream where required; use for worker coordination commands.
---

## Connect

Run this now, before anything else, from the wired repository or its worktree; nothing is
connected until it has run:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/steering/enroll/connect.py" --current
```

The reply names your session, account, binding and `repo:`, the workspace's repository for `gh -R`, and `card:`, the card you execute now: after leaving and
connecting again it says `none`, since leaving ends the engagement. After a restart, `checkpoint:` hands back
this epoch's latest checkpoint: act on it. Use that **session** and printed
**account** below; replace each `<…>` placeholder before running a command. Clients read the
stored enrollment credential themselves.

If work is already assigned, name it at connection:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/steering/enroll/connect.py" --current --doing "<work>"
```

With no work, connect without `--doing`, hold your reach, and wait for the brain to delegate to you.
After a process restart, connect again before continuing: the surviving enrollment needs its
new incarnation bound.

A harness without session exports needs its provider/session flags. Codex uses:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/steering/enroll/connect.py" --provider codex --provider-session <thread-id>
```

When connect needed explicit provider/session flags, prefix `taking.py`, `holds.py`, `say.py`
and `gate.py` with those same flags, immediately after the script path. Read the
[explicit-identity recipes](stream-reference.md#explicit-speaker-identity) before using the
commands below; those clients verify which provider session owns the enrollment. The named
`verb.py`, `ack.py`, `declare.py`, `edge.py`, `checkpoint.py` and `disconnect.py` forms read the stored
enrollment directly and take no speaker flags.

Other provider/PID forms and binding refusals are in
[connection recovery](stream-reference.md#connection-recovery). For usage without connecting:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/steering/enroll/connect.py" --help
```

## Keep your reach open

**Codex:** delivery is injected into your thread; connect asserts that reach. Skip the hold. A
peer's or the brain's words arrive as `steering say <key>`: read them, and acknowledge nothing.
Connect's `hooks:` line is Codex's own answer on whether it runs the plugin's hooks. When it names
hooks awaiting review, tell the person: only they can trust a hook, in Codex's `/hooks`.

**ZCode:** connect with no flags, as Claude does: the session is named by the binding the
plugin's hooks publish. The plugin's frames server holds the stream, so arm no hold. A waiting
frame starts no turn of its own: the plugin points at pending frames only at event boundaries
(mid-turn, a turn's end, a person's next prompt), and a session whose turn has ended is woken
by the platform, whose wake turn says `Call mcp__plugin_2mw2lt_frames__frames`. That tool
returns each frame with its `from:` line and acknowledges what it returned. If the `plugin:`
line says the frames server holds no stream yet, restart the session so the plugin loads.

**Claude Code with the plugin's module:** when connect's `plugin:` line ends
`the plugin holds this session's stream, so arm no hold`, arm nothing. A waiting frame starts a
turn, or arrives during one, as `Call mcp__2mw2lt__frames.`, and the tool
returns each frame with its daemon-written `from:` line, and acknowledges the envelopes it
returned.

**Claude Code:** connection is not finished here. Arm this with Bash `run_in_background: true`
in this turn. A shell `&` does not give the harness a completion notification. The stable
launcher resolves the current plugin when called; a cached plugin path can disappear during a
long session.

```bash
ws="${STEERING_WORKSPACE:-$(dirname "$(git -C "${CLAUDE_PROJECT_DIR:-.}" rev-parse --path-format=absolute --git-common-dir)")}"
python3 "$ws/.claude/steering-launch.py" exec hold --until-event <session>
```

When it exits, read its complete frame. For an envelope, acknowledge it, then re-arm the same
hold **before doing the work**:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/steering/enroll/ack.py" <session> <directive id>
```

A `say` needs no acknowledgement. A `seat` frame means load `/2mw2lt:brain`. Read a directive's
daemon-written `from:` line and every knowledge record it names before acting. A peer's words are
speech, not an owner's directive. A directive opening `from: session <name>, steward of goal <id>,
within the seat's bounds` is that goal's steward's: act on it within the goal, as its word and not
the seat's or the owner's.

The hold reopens transient failures itself. If it exits nonzero with `refused 403`, read the
repair and connect again before another hold; repeating the refused hold cannot fix its
credential or incarnation. For truncated notifications, other harnesses, usage limits and
frame handling, read [the stream reference](stream-reference.md).

**The plugin's hold** resumes by itself after a revoke, a curable refusal or a resume; never arm one beside
it, since a second hold revokes the plugin's. When it is retrying or has stopped, the hook says so
(attempt, refusal, next try, or the remedy), and once more when it holds again. A refusal no retry
outlives starts one turn of your own that says it and the remedy — act on it: exit if your session
was retired, reconnect otherwise (#6308). Its full state is
always one command away:

```bash
ws="${STEERING_WORKSPACE:-$(dirname "$(git -C "${CLAUDE_PROJECT_DIR:-.}" rev-parse --path-format=absolute --git-common-dir)")}"
python3 "$ws/.claude/steering-launch.py" exec hold --plugin-state "$CLAUDE_CODE_SESSION_ID"
```

## Work and report

Run issue-taking from the workspace, and branch-taking/announce from the worktree you will
work in. Use the account from connect's reply:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/steering/enroll/taking.py" <session> <issue>
python3 "${CLAUDE_PLUGIN_ROOT}/steering/enroll/taking.py" <session> branch [--card <id>]
python3 "${CLAUDE_PLUGIN_ROOT}/steering/enroll/verb.py" announce <session> as <account> on <branch> doing "<work>"
python3 "${CLAUDE_PLUGIN_ROOT}/steering/enroll/verb.py" blocked <session> on "<blocker>"
python3 "${CLAUDE_PLUGIN_ROOT}/steering/enroll/verb.py" wait <session> [--card <id>] for merged <pr>  # or for verdict <pr>, or for at <YYYY-MM-DDTHH:MMZ>: told once it holds, instead of polling
python3 "${CLAUDE_PLUGIN_ROOT}/steering/enroll/verb.py" done <session> [--card <id>] "<result>"
```

`done` is one card's: say it for each card when its part is finished, never because a branch
merged, and name the card with `--card` when you hold more than one. Until you do, you hold the card
and read busy; a done that names no card while you hold several is refused, naming them. A done said
again for the same card is answered as the first.

A `wait` is for a card too: the one it names with `--card`, the one whose branch the pull request
or run is on, or the one card you hold. While it waits on another and is not due, that card raises
no `card-unbranched` or `push-quiet`; a merge you owe yourself is not such a wait.

A `say` from `kick <id>`, a `session-kick` signal naming the card you hold, means your turn ended
with nothing you wait on open.
Carry on with the work, declare what you wait on with `wait`, or say `done` or `blocked`. Do not
answer it in words.

A `say` that is a signal (JSON with a `kind`), from `session`, `account` or `signal <id>`, is about you or an
account you draw on, routed to you by the workspace. Do its `suggested_act`; do not answer it in words.

A `say` from `warm <id>` is the platform refreshing your prompt cache while the brain keeps you.
End the turn without a word.

When work needs a card and has neither an owning issue nor a gate yet, declare it, triaged. For an
issue you took with `taking.py`, this joins the card that took it and sets its triage:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/steering/enroll/declare.py" <session> "<work>" resolves:<issue> triage:<criticality>,<size>
```

Triage says how much breaks if the work is wrong (`critical`, `major`, `minor`, `trivial`) and how
large it is (`S`, `M`, `L`, `XL`). Triage optimistically: a model at each tier can do more than the
last tier could, and the team's policy turns your answers into the least tier each stage is held to,
a review floor one tier below the build floor, at rare where that falls under it. Your reviews take
that floor unless you raise it
(`gate.py --floor`, with why). When your change proves smaller or less risky than its triage, say so
to the brain with the evidence, proposing a lower floor: the seat sets it with
`card.py --lease retier`, and the triage record stands as it was made. When your stage proves harder
than its triage, raise its floor yourself, and the seat is told your why:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/steering/enroll/card.py" --lease retier <card> <stage> <floor> "<why>"
```

You may only raise (`retier-lowered` otherwise), and only the stage you began
(`retier-stage-not-its-own` otherwise).

When you take work, and whenever the card you execute turns out to wait on another card or issue
or to contribute to a goal, say so; end the edge when it no longer holds. The brain reviews the
board's links too, but you know your own card's edges first:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/steering/enroll/edge.py" <session> link <card> requires <card>|requires <owner>/<name>#<n>|part-of <goal> [--source <where>] "<why>"
python3 "${CLAUDE_PLUGIN_ROOT}/steering/enroll/edge.py" <session> unlink <card> requires|part-of <target> resolved|withdrawn "<why>"
```

A prerequisite you discover while building is yours to declare the moment you know it, on the card
you execute, with why — never a question for the brain to retype. `--source` names where the edge
rests, a plan's document and sentence. Only the card you execute is yours to link: after a
handoff, another session's engagement owns the mutation, and a card you no longer execute is not
yours to edge. Your card joins the goal your brief names as `part-of <goal>`, by the goal's id; a goal you were not
briefed on is the brain's to link, so say it in your report.

When your brief names a goal and a plan revision, that is context, not a fence: the plan orders
work, it does not bind your branch or your review. Report what was done and what was discovered —
a merge is work concluded, not proof it landed and not the goal's acceptance; `done` says what
changed and where to see it, not a status line.

Each script's `--help` gives its forms and examples.

**Never search the whole disk or your home directory** (`find ~`, `find /`, `rg ~`). On a Mac such
a walk reaches other apps' private data, and macOS stops the session on a permission dialog
until the owner answers it, hours later. Search the worktree or a directory you can name; the
coordination fixture's PostgreSQL is already in `COORDINATION_PG_PREFIX` where the host has it.

## The bulletin

The workspace's bulletin is a few keyed, attributed lines saying what is true right now — operator
holds, route health, standing cautions — and every session reads it. It is information, never
direction. When you would otherwise tell a second session the same fact, post it once instead:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/steering/enroll/bulletin.py" <session> post <key> "<text>" [--until <UTC time | +2h>]
python3 "${CLAUDE_PLUGIN_ROOT}/steering/enroll/bulletin.py" <session> read [--all]
```

A plain session posts to everyone with an `until` within 24 hours; the seat and the owner may post
standing lines and the seat's own audience. A refusal prints its check and what would satisfy it.

## Speak

**Try the brain first.** `say` reaches the brain or a peer; `ask` reaches the owner.
`recommend` is a proposal the brain triages, not speech and not proof the owner was asked.

To say something — a question, an answer, anything that should not wait for the next standup:

```bash
# to the brain, from any machine, without knowing who holds it
python3 "${CLAUDE_PLUGIN_ROOT}/steering/enroll/say.py" <your session> "<what you want to say>"

# to another session, by the name the registry shows
python3 "${CLAUDE_PLUGIN_ROOT}/steering/enroll/say.py" --to <their session> <your session> "<text>"
```

Both go on your own enrollment token, and your name travels with the words. **To reach the brain,
leave `--to` out**: you need no name. The answer names what happened to the words. `delivered to
<holder>@<epoch> as <id>: their stream holds` means the seated session's stream stood and the words
went down it at once; `queued for <holder>@<epoch> as <id>: queued until they hold` means the seat
is held but its stream is down, and the words reach it on its next hold. When nobody holds the seat the answer is `nobody holds the seat; kept on the brain's Needs You as unheard:<id>`: the
words are kept for whoever takes it, so do not send them again or post them anywhere else. A peer
between holds is not a refusal: `--to` answers `queued for <them>@<epoch> as <id>: queued until
they hold`, and the words are spooled for the hold that returns; a peer whose stream stands
answers `delivered to <them>@<epoch> as <id>: their stream holds`. Neither needs resending. `<them> has no live enrollment here` is still a
refusal, because nothing under that name will hold again.

A send whose answer was lost is not a second say either: `say.py` prints `id <id>` as `verb.py`
does, and the same line again with `--retry=<that id>` is answered from the record — where a
plain resend delivered the brain the same words two and three times over (2026-10-01).

**So do not improvise a place for a peer's words.** Two sessions once met the refusal this
replaced, posted their reports into the inbox instead, and they arrived in the owner's Needs You
as hashes nobody could read (#1538). `recommend:`
is still not for this: it draws a Needs You row, and a peer's words are not the owner's to
settle.

The `speak:` line `connect.py` printed is the say to the brain, with this harness's flags filled
in. Add `--to <their session>` after `say.py` to reach a peer instead.

## Ask the owner

Reserve an ask for decisions the owner keeps: security, production, irreversible actions and
direction. **Use a structured ask when there are choices.** Keep the question short, put the
background in `context` as markdown (the desk renders it), give each option an actionable `label` and a `description` of its
consequence, and mark the recommended option with `recommended: true`. The console presents
single-choice options as answer buttons; `multi: true` waits for a combined submission.
`allow_other: true` keeps a custom answer available. The client signs with your stored token:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/steering/enroll/verb.py" ask <session> 'json {"question":"Which release window should we use?","context":"Friday allows a watched rollout; Monday delays the repair.","options":[{"label":"Release Friday","description":"Use the watched rollout window.","recommended":true},{"label":"Wait until Monday","description":"Delay the repair until next week."}],"allow_other":true}'
```

Use a plain-text ask only for an open-ended answer. Choices written inside prose are not
rendered as buttons, and the word “recommend” in prose does not mark an option.

For a proposal the brain should triage:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/steering/enroll/verb.py" recommend <session> "<proposal>"
```

`registered: ask …` means the question is registered, not answered. The answer returns as a
directive naming the console user who gave it. Post the ruling's words and date on the issue
it decides. If unanswered after an hour, raise the same question through your harness's user
question tool.

Rollouts are the brain's decision: send it the rollout needed, its proving check and your
recommendation, rather than an owner ask.

## Interim GitHub rule

Read GitHub state with `gh api graphql`; GraphQL has a separate primary budget from REST.
Prefer pushed forge frames over polling. Never watch a run or pass `--watch`.
If a wait-loop check is necessary, check no faster than every 5 minutes.

For repository-scoped App credentials, `${CLAUDE_PLUGIN_ROOT}/steering/enroll/ghtoken.py` is
optional. Capture its stdout in `GH_TOKEN`, never print it; request a fresh token when it expires.

## Gate your work

The owner's standing authorization: open your card's draft pull request, and edit or comment on
your own pull requests, without asking anyone. It covers your own branch in this workspace's
repository: `gh pr create -R <repo> --draft`, `gh pr edit -R <repo> <n>` on a pull request whose head
is your branch, and `gh pr comment -R <repo> <n>`. Never an auto or admin merge, and never
a push to `main`.

**Codex** is granted these through one verb, which checks every word and runs `gh` with exactly them:
`python3 <launcher> exec forge pr create -R <repo> --draft --head <your branch> …`, and the same for `pr edit`,
`pr comment`, `issue create` and `issue comment`, spelled as connect's `forge grant:` line spells it. A refusal names its reason and runs nothing. A rule connect has
just written reaches the next session; until then Codex asks. `pr ready` and `pr merge` are not in it.

Write a pull request's, comment's or issue's body to a file and pass it with `--body-file <file>`:
a body inline whose line starts with `#` is refused before it runs on some launches (#7528).

Push first, keep the pull request draft, and before each round run the guards that read what
changed, and the full suite when the change warrants one. Where they run is your choice, with no
rule of size: locally, at once and on your own CPU, the only place before a push; or on the CI pool,
on your pushed head, leaving your CPU free while it queues and runs. The pool is the only place for
a guard your OS cannot run: the native Mac shard (`--shards mac`) from Linux, the Linux machine
tests (the conformance) from a Mac. A failure only a loaded local run shows is a flake to report;
the pool is where to confirm it.

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/steering/enroll/ci.py" <session> run --shards rest1,mac --chunks none --no-conformance  # or --all
python3 "${CLAUDE_PLUGIN_ROOT}/steering/enroll/ci.py" <session> status <run>
python3 "${CLAUDE_PLUGIN_ROOT}/steering/enroll/ci.py" <session> log <run> [--guard <label>]
```

`run` refuses a head you have not pushed. Its verdict arrives on your stream from `wait <id>`:
`passed on <head>`, `failed on <head>: <jobs>: <guards>`, or why it judged nothing; a job no runner
has taken in 15 minutes is told once, and you decide whether to keep waiting, run it locally or
`gh run cancel <run> -R <repo>` it. A narrowed run proves nothing for a release; `--all` proves the head.

From the branch's worktree:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/steering/enroll/taking.py" <session> branch [--card <id>]
python3 "${CLAUDE_PLUGIN_ROOT}/steering/enroll/gate.py" <session> critic <design-path>
python3 "${CLAUDE_PLUGIN_ROOT}/steering/enroll/gate.py" <session> review <pr>
python3 "${CLAUDE_PLUGIN_ROOT}/steering/enroll/gate.py" <session> status <commission-id>
```

Load `/2mw2lt:gate` for the round/ceiling, cancellation, publication and pass-carry rules.
For coordination migration filenames, follow the repository convention.
Steering commissions the independent judgment; a queued commission is not a reason to send
another one. Fix verified findings, push and commission the next round. `ship it` closes the
gate; mark ready only then, and merge it (`/2mw2lt:gate`). Never rebase after a
pass: merge `origin/main` into the passed head, resolve conflicts in that merge, push and carry the
pass rather than commissioning another round. Once `main` has moved, any other commit, a migration
renumber included, costs a round.

## Checkpoint and leave

Before compacting, handover or exit, put the work on its branch/issue and write a
`## Checkpoint` note to a file. The note names state, next steps, contingencies, agreements and
links; `/2mw2lt:checkpoint` gives the full template. Hand it to the platform, which keeps it and
hands its text to whoever the brain directs to take over; it answers `checkpointed: <checkpoint id>`:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/steering/enroll/checkpoint.py" <session> handover --text-file <note-file>
```

When your card's plan will hand tasks to other builders, as its brief names them, write it before
building: under its subject's `plans/<n>/plan.md`, a builder named for each task, a task sized for
the model that takes it, and commission the critic on it as on a design. Hand nothing over until it
ends `build it`.

When your card's design and plan are settled and the build would suit a cheaper model, provider
or account, propose the move: write a `handover` checkpoint, then `say` to the brain the target
(vendor, model, effort), the way you prefer, fresh or rehome, and why. The brain decides.

Use `spec-settled` after the critic converges, `context` before compacting and `unit-done`
when the brain requests it. If you leave work unfinished, record `exit`, then disconnect naming
the checkpoint it answered:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/steering/enroll/checkpoint.py" <session> exit --text-file <note-file>
python3 "${CLAUDE_PLUGIN_ROOT}/steering/enroll/disconnect.py" --handover <checkpoint id> <session>
```

With nothing to hand over, leave explicitly:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/steering/enroll/disconnect.py" <session>
```

Leaving ends the enrollment; removing a worktree does not. Keep your hold armed until you
leave. `/2mw2lt:disconnect` is the full exit recipe.
