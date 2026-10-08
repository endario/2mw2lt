---
description: Write down what this session knows before it forgets — before a compact, a handover or leaving — as a note on the card's pull request or issue, and record it with steering.
---

A checkpoint puts the work where it lives and indexes it with one note.
The note is not a second copy of the work.

**When.** At each boundary: `design-settled` once your critic converges; `unit-done` when the
brain's directive says `checkpoint: <card>`; `context` when the brain asks before a compact;
`handover` on `pack up:`; `exit` before `/2mw2lt:disconnect`.

**1. Put the work where it lives.**

- Decisions, with the rejected alternatives and one line on why each lost: the design record.
- The plan: the plan file beside it.
- Follow-ups: issues, by the global rule O5.
- Measurements: the record, or the issue they decide.
- Lessons: the `learned` list below.

**2. Post the note** as a comment on the card's draft pull request, or on its issue when there is
no pull request yet:

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

**Links.** The record, the plan, the issues filed, and the lessons proposed.
```

**3. Record it.** Lessons are optional: at most eight, each one rule and the mistake it prevents
in 500 characters or fewer, scoped to repository paths or `"*"`.

```bash
learned=$(mktemp)
cat > "$learned" <<'JSON'
[{"text": "<rule, and the mistake it prevents>", "scope": ["steering/"]}]
JSON
python3 "${CLAUDE_PLUGIN_ROOT}/steering/enroll/checkpoint.py" <your session> <boundary> <note url> [--learned-file "$learned"]
```

It reads the comment back from GitHub and sends its hash with it. `checkpointed: <id> <boundary>`
is the answer; `dropped: <n>` names a lesson that carried something credential-shaped and was
left out, and the rest stand. A lost answer goes again with `--retry=<the id it printed>`.

After it, the brain reads the checkpoint as current on your delegation row until you next
acknowledge a directive.
