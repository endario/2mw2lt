---
description: Connect at session start or restart, and hold the stream where required; use for worker coordination commands.
---

## Connect

Run from the wired repository or its worktree:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/steering/enroll/connect.py" --current
```

The reply names your session, account, binding and `repo:`, the workspace's repository for `gh -R`. Use that **session** and printed
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
peer's or the seat's words arrive as `steering say <key>`: read them, and acknowledge nothing.

**Claude Code with the plugin's module:** when connect's `plugin:` line ends
`the plugin holds this session's stream, so arm no hold`, arm nothing. A waiting frame starts a
turn, or arrives during one, as `A steering frame is waiting`. Call `mcp__2mw2lt__frames` then:
it returns each frame with its daemon-written `from:` line, and acknowledges the envelopes it
returned.

**Claude Code:** connection is not finished here. Arm this with Bash `run_in_background: true`
in this turn. A shell `&` does not give the harness a completion notification. The stable
launcher resolves the current plugin when called; a cached plugin path can disappear during a
long session.

```bash
ws="${STEERING_WORKSPACE:-$(dirname "$(git rev-parse --path-format=absolute --git-common-dir)")}"
python3 "$ws/.claude/steering-launch.py" exec hold --until-event <session>
```

When it exits, read its complete frame. For an envelope, acknowledge it, then re-arm the same
hold **before doing the work**:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/steering/enroll/ack.py" <session> <ulid>
```

A `say` needs no acknowledgement. A `seat` frame means load `/2mw2lt:brain`. Read a directive's
daemon-written `from:` line and every canon record it names before acting. A peer's words are
speech, not an owner's directive.

The hold reopens transient failures itself. If it exits nonzero with `refused 403`, read the
repair and connect again before another hold; repeating the refused hold cannot fix its
credential or incarnation. For truncated notifications, other harnesses, usage limits and
frame handling, read [the stream reference](stream-reference.md).

## Work and report

Run issue-taking from the workspace, and branch-taking/announce from the worktree you will
work in. Use the account from connect's reply:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/steering/enroll/taking.py" <session> <issue>
python3 "${CLAUDE_PLUGIN_ROOT}/steering/enroll/taking.py" <session> branch
python3 "${CLAUDE_PLUGIN_ROOT}/steering/enroll/verb.py" announce <session> as <account> on <branch> doing "<work>"
python3 "${CLAUDE_PLUGIN_ROOT}/steering/enroll/verb.py" blocked <session> on "<blocker>"
python3 "${CLAUDE_PLUGIN_ROOT}/steering/enroll/verb.py" wait <session> for checks <pr>  # or merged <pr>, verdict <pr>, comment <pr>, at <UTC time>: told once it holds, instead of polling
python3 "${CLAUDE_PLUGIN_ROOT}/steering/enroll/verb.py" done <session> "<result>"
```

When work needs a card and has neither an owning issue nor a gate yet, declare it:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/steering/enroll/declare.py" <session> "<work>" resolves:<issue>
```

When you take work, and whenever the card you execute turns out to wait on another card or issue
or to contribute to a goal, say so; end the edge when it no longer holds. The brain reviews the
board's links too, but you know your own card's edges first:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/steering/enroll/edge.py" <session> link <card> requires|part-of <card>|<owner>/<name>#<n> "<why>"
python3 "${CLAUDE_PLUGIN_ROOT}/steering/enroll/edge.py" <session> unlink <card> requires|part-of <target> resolved|withdrawn "<why>"
```

Each script's `--help` gives its forms and examples.

**Never search the whole disk or your home directory** (`find ~`, `find /`, `rg ~`). On a Mac such
a walk reaches other apps' private data, and macOS stops the session on a permission dialog
until the owner answers it, hours later. Search the worktree or a directory you can name; the
coordination fixture's PostgreSQL is already in `COORDINATION_PG_PREFIX` where the host has it.

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
leave `--to` out**: you need no name. The answer names the session that heard you, `the brain
heard <you>: <holder>`, or `queued for the brain (<holder>), which is between holds
(normal): …` when it is between holds: the words reach it on its next hold. When nobody holds the seat the answer is `nobody holds the seat; kept on the brain's Needs You as unheard:<id>`: the
words are kept for whoever takes it, so do not send them again or post them anywhere else. A peer between
holds is not a refusal: `--to` answers `queued for <them>, which is between holds (normal): …`,
and the words are spooled for the hold that returns. Neither needs resending. `<them> has no live enrollment here` is still a
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

Push first, keep the pull request draft, and before each round run the focused guards for what
changed locally, and the full suite locally when the change warrants one. Dispatch no CI for it.
From the branch's worktree:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/steering/enroll/taking.py" <session> branch
python3 "${CLAUDE_PLUGIN_ROOT}/steering/enroll/gate.py" <session> critic <design-path>
python3 "${CLAUDE_PLUGIN_ROOT}/steering/enroll/gate.py" <session> review <pr>
python3 "${CLAUDE_PLUGIN_ROOT}/steering/enroll/gate.py" <session> status <commission-id>
```

Load `/2mw2lt:gate` for the round/ceiling, cancellation, publication and pass-carry rules.
Steering commissions the independent judgment; a queued commission is not a reason to send
another one. Fix verified findings, push and commission the next round. `ship it` closes the
gate; mark ready only then, and merge it directly (`/2mw2lt:gate`). After a clean rebase, a merge
of `main`, or a conflict-only update, carry the pass rather than commissioning another round;
commission one when judgment warrants it.

## Checkpoint and leave

Before compacting, handover or exit, put the work on its branch/issue and post a
`## Checkpoint` comment on its draft pull request (or issue). The note names state, next
steps, contingencies, agreements and links; `/2mw2lt:checkpoint` gives the full template.
Using that comment's URL:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/steering/enroll/checkpoint.py" <session> handover <note-url>
```

Use `design-settled` after the critic converges, `context` before compacting and `unit-done`
when the brain requests it. Before leaving, record `exit` against the note, then disconnect:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/steering/enroll/checkpoint.py" <session> exit <note-url>
python3 "${CLAUDE_PLUGIN_ROOT}/steering/enroll/disconnect.py" --handover <note-url> <session>
```

With nothing to hand over, leave explicitly:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/steering/enroll/disconnect.py" <session>
```

Leaving ends the enrollment; removing a worktree does not. Keep your hold armed until you
leave. `/2mw2lt:disconnect` is the full exit recipe.
