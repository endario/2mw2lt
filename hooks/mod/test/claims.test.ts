import { register } from '../register'
import { test, expect } from 'claude-code/testing'
import { SEAT_TOOLS, TIMER_TOOLS } from '../frames'
import { world, begin, never } from './world'

// #9751: the `relay` tool was answered by the harness's own fallback ("registered the tool relay
// but no tool.call hook answered this call") on a seated session, while the tools claimed by an
// exact-string matcher (frames, checkpoint) were answered beside it. No seated reproduction
// settles which of the issue's candidates did it — the matcher's form, or no handler registered
// in that process — so each tool name is now claimed by its own registration under the exact
// string the harness calls: the form the engine's fallback message itself prescribes. These
// tests hold that: every name the module registers as a tool has a tool.call hook — and a
// tool.describe hook — whose matcher is that exact string, and every name is answered as a call.
// The kit's own dispatch matches patterns too, so the form test is the sole guard of the fix:
// under a RegExp matcher it alone fails, while the call test still passes — it guards a dropped
// registration, not the matcher's form.

// One call's arguments the tool's own validation accepts, per registered name.
const SEAT_ARGS: Record<string, Record<string, unknown>> = {
  relay: { to: 'amber', text: 'take #12' },
  dispose: { item: '01ROW', reason: 'the row was answered' },
  'out-of-band': { item: '01ROW', channel: 'the owner\'s chat, 2026-11-13T09:00Z', words: 'ship it' },
  retire: { worker: 'teal', reason: 'its card landed' },
  wake: { target: 'amber', reason: 'dark for an hour' },
  rehome: { session: 's-1', account: '2', reason: 'the pool needs it' },
  park: { session: 's-1', reason: 'the slot is needed' },
  keep: { session: 's-1', reason: 'work is expected in its territory' },
  unkeep: { session: 's-1' },
  series_floor: { kind: 'review', repo: 'example-org/example-repo', pr: 12, floor: 'uncommon', reason: 'a docs-only change' },
  card_reclassify: { card: '01CARD', field: 'priority', value: 'high', why: 'the seat moved it' },
  card_retire: { card: '01CARD', why: 'duplicate of 01OTHER' },
  card_scope: { name: 'The work', track: 'rt' },
  card_session: { card: '01CARD', session: 'teal', role: 'executor' },
  answer: { episode: 'e1', verdict: 'considered', because: 'nothing moved' },
  say: { text: 'the deploy is out' },
}
const TIMER_ARGS: Record<string, Record<string, unknown>> = {
  timer_list: {},
  timer_read: { timer: '1b6f6a8e-3c1d-4f7e-9a2b-5d4c3b2a1f00' },
  timer_lodge: { text: 'ship the release notes', target: 'self', at: '2026-11-01T09:00:00Z' },
  timer_cancel: { timer: '1b6f6a8e-3c1d-4f7e-9a2b-5d4c3b2a1f00', revision: 2 },
  timer_snooze: { timer: '1b6f6a8e-3c1d-4f7e-9a2b-5d4c3b2a1f00', revision: 2, at: '2026-11-01T10:00:00Z' },
  timer_answer: { delivery: '2c8e7b9d-4e2f-4a90-8b1c-6d5e4f3a2b10', verdict: 'acted', because: 'it was time' },
}

test('every registered tool name is claimed by tool hooks under its exact name', async () => {
  const claims: { event: string; tool: unknown }[] = []
  register((((event: string, matcher: { tool?: unknown }) => {
    claims.push({ event, tool: matcher?.tool })
    return { catch: () => undefined }
  }) as never))
  for (const name of ['frames', 'checkpoint', ...Object.keys(SEAT_TOOLS), ...Object.keys(TIMER_TOOLS)]) {
    const tool = `mcp__2mw2lt__${name}`
    // A string, not a pattern over the names: the exact matcher is the form a tool.call dispatch
    // compares by equality, and the one a seated session's `relay` call was left unanswered
    // without (#9751).
    expect(claims.some(c => c.event === 'tool.call' && c.tool === tool)).toBe(true)
    expect(claims.some(c => c.event === 'tool.describe' && c.tool === tool)).toBe(true)
  }
  // No tool hook is left claiming its names only through a pattern.
  for (const c of claims) if (c.event === 'tool.call' || c.event === 'tool.describe') {
    expect(typeof c.tool === 'string').toBe(true)
  }
})

test('every seat tool and timer tool answers a call in its own name', async ($, on) => {
  const w = world(on, { rounds: [{ lines: [], code: 0, hold: never }] })
  await begin($)
  await w.clock.settle()
  for (const [table, args] of [[SEAT_TOOLS, SEAT_ARGS], [TIMER_TOOLS, TIMER_ARGS]] as const) {
    for (const name of Object.keys(table)) {
      const given = args[name]
      expect(given !== undefined).toBe(true)
      const got = await $.tool.call({ tool: `mcp__2mw2lt__${name}`, ...given } as never)
      const answered = got as { result?: unknown; deny?: unknown }
      // The module always answers its own tools — a result or one of its refusals — so the call
      // never falls through to the harness's "no tool.call hook answered" fallback.
      expect(answered.result !== undefined || answered.deny !== undefined).toBe(true)
    }
  }
  expect(w.runs.filter(argv => /\/(lease|card|timer)\.py$/.test(String(argv[1]))).length)
    .toBe(Object.keys(SEAT_TOOLS).length + Object.keys(TIMER_TOOLS).length)
})
