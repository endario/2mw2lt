---
name: checkpoint
description: Write down what this session knows before it forgets — before a compact, a handover or leaving — as a note the platform keeps.
---

A checkpoint puts the work where it lives and indexes it with one note.
The note is not a second copy of the work.

**When.** At each boundary: `spec-settled` once your critic converges; `unit-done` when the
brain's directive says `checkpoint: <card>`; `context` when the brain asks before a compact;
`handover` on `pack up:`; `exit` before `/2mw2lt:disconnect` when you leave work unfinished; a card that merged and
concluded leaves nothing to hand over, and needs none.

**1. Put the work where it lives.**

- Decisions, with the rejected alternatives and one line on why each lost: the design record.
- The plan: the plan file beside it.
- Goals and plans: when your card serves a goal, the goal and its plan revision are the live
  record — point at them, and copy no inventory of cards or links into the note. Keep what they
  cannot hold: unresolved command receipts (the principal, exact body and key of an uncertain
  write), decisions not yet written to a ruling, and reasoning the native records do not carry.
- Follow-ups: issues, by the global rule O5.
- Measurements: the record, or the issue they decide.
- Lessons: the `learned` list below.

**2. Hand the note to the platform**, written to a file, at most 8192 bytes:

```markdown
## Checkpoint

Boundary: `<boundary>` — session <your session>.

**State.** Where the work stands, the branch and its head sha.

**Next.**
1. The action list, in order.

**If.**
- If X, do Y.

**Agreements.**
- What the brain, the owner or a sibling agreed with this session, restated in words: an
  instruction given only in the conversation does not survive a compact.

**Links.** The record, the plan, the goal and plan revision the card serves, the issues filed,
and the lessons proposed.
```

A `handover` note is read by a builder with none of your context, perhaps a smaller model: make
**Next** the plan's path and the task to start from, the acceptance criteria, and the traps this
work has already hit. Push the branch first: the builder starts from the remote, and a commit only
in your worktree never reaches it.

Lessons are optional: at most eight, each one rule and the mistake it prevents in 500 characters
or fewer, scoped to repository paths or `"*"`.

```bash
learned=$(mktemp)
cat > "$learned" <<'JSON'
[{"text": "<rule, and the mistake it prevents>", "scope": ["steering/"]}]
JSON
python3 "${CLAUDE_PLUGIN_ROOT}/steering/enroll/checkpoint.py" <your session> <boundary> --text-file <note file> [--learned-file "$learned"] [--mirror <pull request or issue URL>]
```

A Claude Code session with the plugin hands the note over with the `mcp__2mw2lt__checkpoint` tool
(`{boundary, text, learned?, mirror?}`) instead of this command. A session whose shell has no
`CLAUDE_PLUGIN_ROOT` — Codex and every harness besides Claude Code — runs the same verb through the
workspace's stable launcher, the one path every harness shares and a Codex session's grant allows
without a dialog:

```bash
python3 <workspace>/.claude/steering-launch.py exec checkpoint <your session> <boundary> --text-file <note file> [--learned-file "$learned"] [--mirror <pull request or issue URL>]
```

The platform scans the note for credentials and keeps it for the workspace's retention.
`checkpointed: <checkpoint id> <boundary>` is the answer; `dropped: <n>` names a lesson that
carried something credential-shaped and was left out, and the rest stand. A lost answer goes again
with `--retry=<the id on its id line>`. `checkpoint.py <your session> read [latest|<checkpoint id>]`
prints a note back. `--mirror` also posts the note as a comment on the card's pull request or issue,
only once the platform has kept it, for people who read GitHub; a `handover` or `exit` note is
mirrored, since its next reader reads it there.

After it, the brain reads the checkpoint as current on your delegation row until you next
acknowledge a directive.
