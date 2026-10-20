---
name: discover
description: Use when the brain is asked to explore a workspace's work and say what its goals should be, to catch the owner up on a goal, or to change one — bring a sourced position, not a questionnaire.
---

# Discover the goals

You do the legwork and bring a defensible position; the owner supplies judgment, not project
administration. Discovery, agreement, catch-up and changing direction are one continuing
conversation, anchored to the live Ledger. The design and its reasoning are
goal-discovery.md
(#6394); this is the practice.

## When it starts

- The owner asks. The console's **Explore the work already under way** sends exactly:
  *"Explore the work already under way in this workspace and bring me a position on what its goals
  should be, using the discover skill."*
- A delegated onboarding or catch-up task, or a material contradiction you meet while steering.

Never on a page load, a timer or a schedule: there is no goal-report ceremony. A repeated request
joins the conversation already open rather than starting a second. A direct instruction that
already names its intent is not one of these starts: the brain takes it straight into its goal
routine, and discovery joins only when the direction itself is in question.

A message opening *"About the goal “…” (goal <id>, revision <n>):"* or *"About plan <n> of the goal
“…” (goal <id>):"* comes from the Ledger's **Discuss** buttons. It is about that adopted goal or
plan: apply [Catch-up and revision](#catch-up-and-revision), not a new discovery.

## Read before you ask

Read admitted sources, keeping each one's revision or time:

- **Charter and current rulings**: what matters, what must not happen, what was deferred.
- **Code, tests and proof**: what exists, what is only merged, what has been demonstrated. A
  screenshot fixture is not a production reading.
- **Issues, designs and abandoned attempts**: why work began and why alternatives lost. Check
  current evidence before repeating an old "open" claim.
- **Engagements and history**: who knows the work, what is blocked or unheld, what serves more
  than one outcome.
- **People**: workers and trusted collaborators first, the owner only for intent the record
  cannot establish.

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/steering/enroll/rest.py" --lease /goals
python3 "${CLAUDE_PLUGIN_ROOT}/steering/enroll/rest.py" --lease /rulings
python3 "${CLAUDE_PLUGIN_ROOT}/steering/enroll/rest.py" --lease "/cards?state=live"
```

Bound the first pass by the current undertaking and the decisions near at hand; expand only where a
contradiction matters. State the evidence boundary (the commit and time you read) and name every
gap: missing access is a gap, not proof that no work exists.

For each candidate, prepare a short argument: what would be different, for whom; why it matters
now; what would demonstrate it, what would disprove this framing, what is outside it, which existing
work helps; the strongest source and the unresolved disagreement. Keep **recorded direction**,
**observed fact**, **inference** and **contested claim** apart, and explain confidence rather than
give a percentage. Look for intent with no work, work with no defensible outcome, and duplicates.

Propose a goal only when its finish is describable, its value distinct and a decision useful now.
Otherwise keep it as intent, policy, evidence or an explicitly deferred discussion. Fewer goals is
a valid recommendation; an observation is not an obligation.

**Ask people who can falsify you**, through the speech they already have (`say.py`, a room, their
own records): "What would this miss?", "Which proof is not a pass?", "What does this deferral
protect?" Keep attribution and dissent, and reconcile them rather than forward the interviews. A
majority of agents is not corroboration, and a missing answer is named with what depends on it.
Do not use `recommend` or a structured ask for discovery: both fill the owner's attention queue.

## The owner's part

Open with a position in three short sections:

1. **What I think we are trying to achieve** — the outcome and where that intent comes from.
2. **What I recommend** — finish line, exclusions and next useful move; the alternative that would
   change the decision, and why it loses.
3. **What only you can settle** — one consequential question, or a ready proposal when none is left.

Offer the recommended choice, real alternatives, free correction and **Leave open**. When intent
is unclear, ask about a real moment rather than confine the owner to your menu. "Neither; you
misunderstood" is a first-class answer. One question at a time is a reading discipline, not a
wizard: independent topics may be answered in any order; group coupled choices; never hide consent
to exclusions or widened scope inside "agree all". Never walk the owner through title, context and
criteria as successive questions: you draft the structure.

Stop asking once the outcome, a checkable finish, the exclusions and the next safe step are clear
enough to propose. Choose reversible mechanics yourself.

**Where it goes.** The position, open questions and later decisions live in a thread in an
existing workspace room (a message and its replies, `reply_to`), never a new room per goal. Answer
the brain request with the brain skill's reply (`lease.py post /steering/brain/reply`), pointing to that thread. When the discussion happens in the
direct conversation instead, write its conclusion and source into the thread as your own account,
never as the owner's words. Needs You is only for a decision that unblocks an actual next action,
linked to the thread; answering it settles that action, not the strategy.

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/steering/enroll/rest.py" --lease --post "/rooms/<room>/messages" \
  --json '{"text": "…", "reply_to": "<the opening message id>"}' --key <a key of your own>
```

## Agreement

Present the proposed title, criteria, non-goals, sources and uncertainties together. The first plan
is your proposed next work, not evidence the goal is met. Before a revision, show the scope delta and
say that it reopens the whole goal's judgment, retained criteria included; never rewrite criteria
for prose alone.

Name every outcome as you draft it: a short name — one line, forty characters at most, plain enough
to say aloud — rides beside each new statement, and a reworded one is prefilled from the wording it
replaces. A name is presentation of the criterion's identity: it reopens no judgment, and a retained
outcome's name is the name command's to change, never a revision's.

A conversation does not write the goal. Once the owner agrees or corrects, read the current goal and
policy, then send each as its own command, each on the seat's enrolment and its own key:

1. **The ruling**, with its source and reason. Tell the owner it was **recorded by the brain on
   your behalf** for audit, not an action they took: none needs their acknowledgement, they can
   correct it in their own words, and only what is theirs to settle is escalated to them.
2. **The goal**: `POST /goals`, judgment pending. Report its id and revision with the ruling.
3. **Its contributions**: `POST /cards/<card>/links` with `{"kind": "part-of", "to_goal": "<goal>",
   "why": "…"}` for each piece of existing work. No duplicate cards.
4. **The first plan**: `goal.py --lease prepare plan <goal> --policy <unit> --decision <file>`,
   then `submit` the file it prints (`/2mw2lt:brain` § Run a goal). Use criterion ids, never
   display numbers.
5. **Imports**: `goal.py --lease prepare import <goal> --source <source> --revision <revision>
   --rows <file>`, the rows a JSON list of `key`, `statement`, `disposition` and `criteria`, then
   `submit`. The import is fenced by the goal revision whose criteria the rows map to.

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/steering/enroll/rest.py" --lease --post /rulings \
  --json '{"text": "<the decision>", "reason": "<source and why>"}' --key <key>
python3 "${CLAUDE_PLUGIN_ROOT}/steering/enroll/rest.py" --lease --post /goals \
  --json '{"title": "…", "context": "…", "criteria": [{"statement": "…", "name": "…"}]}' --key <key>
```

**Write the principal, the exact body, the key and every known id into your checkpoint before
sending** (`/2mw2lt:checkpoint`). An uncertain write is replayed only by the same principal with
the same body and key; a replacement seat never resends another's create as itself. If a result
cannot be established from the goal and ruling reads, leave it unresolved and stop its dependent
writes rather than create another goal. Partial setup stays visible, and its repair is yours.

An owner correction of a ruling does not change the goal: reconcile the goal and plan to it, and show
any mismatch until it is repaired. Uncertainty pauses the affected change, not unrelated authorized
work. Adoption is not outcome acceptance, production approval or a spending grant.

## Catch-up and revision

On the owner's return, lead with what changed since the last decision and what that changes about
your recommendation. Say "Nothing needs a new decision" when that is true. Routine progress updates
the live reading; it never restarts discovery or becomes a status report.

A disproven assumption, new scope or a changed trade-off amends the existing thread, with the
adopted goal and plan beside the proposal. Replan within existing scope and policy without
ceremony; a changed exclusion or direction needs agreement. Work the last agreement permits
continues while a dependent choice stays open. A withdrawn outcome is a scope decision, not
progress. When you judge a goal met, assemble the evidence for an explicit judgment through the
goal contract: agreeing to pursue an outcome is not judging that it happened.

## Push back

- **Agentic is not unilateral.** The backlog shows hypotheses about intent, not consent; work
  being expensive or under way is no reason to keep doing it.
- **Silence is not agreement.** An unanswered strategic item may stay unanswered; waiting
  authorizes no speculative scope.
- **A track is not a goal.** Tracks organize work; a goal has an observable finish. A standing
  policy is not a permanently failing criterion.
- **A finished goal is not a clean-looking backlog.** Reconciling facts, adopting intent, judging
  an outcome met and permission to deploy are four different acts.
