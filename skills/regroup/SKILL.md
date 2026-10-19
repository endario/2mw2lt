---
name: regroup
description: As the seat, reconsider what the fleet works on — when a `work-regroup` signal arrives, or by hand whenever something has shifted enough that the next work should be chosen again.
---

A regroup asks the seat to choose the next actionable work again, not to review the backlog
(the design §3). The platform
raises one each day at the workspace's `work_regroup_time`, ahead of each known peak window, and
ahead of each window's end; a
`work-regroup` signal names its reasons and the `episode` you answer it by. One pending regroup
stands per day: anything that asks again that day joins it, and one answer settles every reason.

**By hand.** When something has shifted — the owner reordered priorities, a blocker cleared, a
plan changed — ask for one with why:

```bash
printf '%s' "regroup: token @lease because <what shifted>" | python3 "${CLAUDE_PLUGIN_ROOT}/steering/enroll/lease.py" say
```

It prints `id <id>` first and answers with the regroup and its episode. The id names this request:
if the answer was lost, send the same line again with `say --retry=<id>` and the platform returns
the same regroup rather than raising another. A new line is a new request, which joins the day's
pending regroup if one stands. `GET /work/regroups` lists what is pending, with every reason.

**The routine.** Read the reasons' current facts — a late signal's windows may have moved — then
goal execution, unassigned work, and the pending launches, gates, actions and waits. A bounded
read that says it omitted something is not the whole set.

1. Honor owner holds, authorization and any freeze; urgent work takes an open eligible route.
2. Keep useful work running: do not move it just to improve a cost, and stop at a safe turn.
3. Among ready, authorized work, follow correctness first, card priority and the adopted plan's
   order. A prerequisite may advance the work it unlocks; say so rather than raising its criticality.
4. Choose work that fits routes open now at its stage floor. A ladder rung is capability, not room.
5. Take a knowledge, test, efficiency or housekeeping gap only with a concrete benefit and a bound.
6. If nothing valuable fits, stop. If an alternative's value or authority is uncertain, leave it and
   raise one Needs You question with your recommendation; carry on with the rest.

Weigh each move against continuing unchanged: context transfer, checkpoint cost, gate
independence and risk to work already held. Reuse a suitable session before launching another.

**Ahead of a peak** (`pre-peak.v1`). A workhorse vendor's peak is not for
throughput: line up only the
must-do and critical work for premium providers, and hold what the workhorse suits until the end. A
route whose peak is unknown still launches.
Before the buffer, use the named accounts' expiring quota for useful, authorized work that fits.
Their quota reasons carry remaining quota and reset time, not permission to invent work or bypass the
freeze (#7541).

**Ahead of a peak's end** (`post-peak.v1`). Release what the peak held: line up the work frozen for
that vendor so it starts at the boundary, wake or relaunch its parked sessions then, lift the
freezes you placed by hand, and move back the sessions that went to dearer vendors for the window,
each at a safe turn.

**Answer it** once you have decided, by the episode:

```
answer: token @lease <episode> acted because <the decisions you issued>
answer: token @lease <episode> considered because <why nothing should change>
```

`acted` means decisions were issued, not that they landed. An answer referencing an ask leaves
that ask open where it lives. A regroup from an earlier day still pending is answered
`considered`, naming what you settled. Answering is never a report to the owner.
