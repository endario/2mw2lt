import { test, expect } from 'claude-code/testing'
import { POINTER } from '../frames'
import { Outbox, agentOf } from '../surface'
import { world, line, begin, never, AGENT } from './world'

// The live view's half in the module (#4823): what reaches the machine agent, and when.

const HELD = { lines: [], code: 0 as const, hold: never }
const posted = (w: ReturnType<typeof world>) => w.fetches.filter(f => f.method === 'POST')
const events = (w: ReturnType<typeof world>) => posted(w).flatMap(f => (JSON.parse(f.body ?? '{}') as { events: any[] }).events)
// The surface channel's own polls: the hold's frames polls ride the same agent beside them (#7838).
const polls = (w: ReturnType<typeof world>) => w.fetches.filter(f => f.method === 'GET' && f.url.includes('/surface/poll'))

const prompt = { message: { type: 'user', role: 'user', content: [{ type: 'text', text: 'list the files' }] }, door: 'prompt', origin: { kind: 'composer' }, uuid: 'u1' }
const call = { message: { type: 'assistant', role: 'assistant', content: [{ type: 'tool_use', id: 'toolu_1', name: 'Bash', input: { command: 'ls' } }] },
  door: 'response', origin: { kind: 'model', model: 'claude-test' }, uuid: 'u2' }
const result = { message: { type: 'user', role: 'user', content: [{ type: 'tool_result', tool_use_id: 'toolu_1', content: 'a\nb' }] },
  door: 'tool-result', origin: { kind: 'tool', tool: 'Bash' }, uuid: 'u3' }

// Review Focus 1: a session nobody watches costs nothing past the poll that says so.
test('nothing is sent while nobody watches', async ($, on) => {
  const w = world(on, { agent: true, rounds: [HELD], polls: [{ watch: false }] })
  await begin($)
  await w.clock.settle()
  await $.turn.start({ turnId: 't1', text: 'list the files' })
  for (const row of [prompt, call, result]) await $.session.append(row as never)
  await w.clock.advance(1000)
  expect(polls(w).length).toBeGreaterThan(0)          // it asked, and was told nobody watches
  expect(posted(w)).toEqual([])
})

test('a watched session\'s rows arrive in order with rising seq', async ($, on) => {
  const w = world(on, { agent: true, rounds: [HELD], polls: [{ watch: true, cursor: 'c1' }] })
  await begin($)
  await w.clock.settle()
  await $.turn.start({ turnId: 't1', text: 'list the files' })
  for (const row of [prompt, call, result]) await $.session.append(row as never)
  await w.clock.advance(50)
  const got = events(w)
  expect(got.map(e => e.kind)).toEqual(['turn', 'row', 'row', 'row'])
  expect(got.map(e => e.seq)).toEqual([1, 2, 3, 4])
  expect(got[0]).toEqual({ v: 1, session: 's1', seq: 1, kind: 'turn', turn: { state: 'started' } })
  expect(got.slice(1).map(e => e.row)).toEqual([
    { id: 'u1', role: 'user', origin: 'person', blocks: [{ type: 'text', text: 'list the files' }] },
    { id: 'u2', role: 'assistant', origin: 'model', blocks: [{ type: 'tool-use', id: 'toolu_1', tool: 'Bash', input: { command: 'ls' } }] },
    { id: 'u3', role: 'tool-result', origin: 'engine', blocks: [{ type: 'tool-result', id: 'toolu_1', tool: 'Bash', text: 'a\nb' }] },
  ])
  // Rows within one window leave together, on the agent's key and naming the workspace.
  expect(posted(w).length).toBe(1)
  const sent = posted(w)[0]!
  expect(sent.url).toBe(`http://127.0.0.1:${AGENT.port}/steering/surface/events`)
  expect(sent.headers['X-Steering-Agent-Key']).toBe(AGENT.key)
  expect(sent.headers['X-Steering-Workspace']).toBe('/ws')
  expect(JSON.parse(sent.body!).session).toBe('s1')
  expect(JSON.parse(sent.body!).producer).toBe('ps-1')    // the conversation that numbered them
  // The next poll resumes from the cursor the agent gave.
  expect(polls(w).map(p => p.url)).toEqual([
    `http://127.0.0.1:${AGENT.port}/steering/surface/poll?session=s1&producer=ps-1&after=`,
    `http://127.0.0.1:${AGENT.port}/steering/surface/poll?session=s1&producer=ps-1&after=c1`,
  ])
})

// The agent advertises the session's rows live only while the conversation its hold pins is polling
// (plan 1b.1): the poll names the conversation it asks for, the one the events name as their
// producer, read again at each poll so a conversation that changed under the module is not the old.
test('the poll names its producer', async ($, on) => {
  let second!: (p: { watch: boolean; cursor: string }) => void
  const w = world(on, { agent: true, rounds: [HELD], polls: [{ watch: true, cursor: 'c1' }, new Promise(r => { second = r })] })
  await begin($)
  await w.clock.settle()
  await $.turn.start({ turnId: 't1', text: 'list the files' })
  await w.clock.advance(50)
  const producer = (JSON.parse(posted(w)[0]!.body!) as { producer: string }).producer
  expect(new URL(polls(w)[0]!.url).searchParams.get('producer')).toBe(producer)
  w.psession = 'ps-2'
  second({ watch: true, cursor: 'c2' })
  await w.clock.settle()
  expect(polls(w).map(p => new URL(p.url).searchParams.get('producer'))).toEqual(['ps-1', 'ps-1', 'ps-2'])
})

// Compaction's boundary and summary never pass `session.append` (spike A): the summary is the
// compaction hook's result.
test('compaction is emitted from session.compact, not session.append', async ($, on) => {
  const w = world(on, { agent: true, rounds: [HELD], polls: [{ watch: true }] })
  await begin($)
  await w.clock.settle()
  await $.session.compact({ trigger: 'manual', messages: [{ role: 'user', text: 'before', toolUses: [] }] } as never)
  await w.clock.advance(50)
  expect(events(w)).toEqual([
    { v: 1, session: 's1', seq: 1, kind: 'compaction', row: { role: 'system', origin: 'engine', blocks: [{ type: 'text', text: 'hello' }] } },
  ])
})

// A plugin's own submit never re-enters its own hooks, so the module emits the pointer itself.
test('the module\'s own pointer row is origin plugin', async ($, on) => {
  let wake!: () => void
  const w = world(on, { agent: true, polls: [{ watch: true }],
    rounds: [{ lines: [new Promise<void>(r => { wake = r }), line('f1', 'say', true)], code: 0, hold: never }] })
  await begin($)
  await w.clock.settle()
  wake()
  await w.clock.settle()
  await w.clock.advance(50)
  expect(w.submits).toEqual([POINTER])
  expect(w.asUser).toEqual([false])                // the pointer stays framed as the plugin's
  expect(events(w).map(e => e.row)).toEqual([{ role: 'user', origin: 'plugin', blocks: [{ type: 'text', text: POINTER }] }])
})

const turn = { kind: 'turn' as const, turn: { state: 'started' as const } }

// A reload drops what the last process had queued: the stream numbers on and opens with a gap, so
// the reader re-reads history and never takes a later seq for one it already holds.
test('a reloaded module numbers on from the kept seq, opening with a gap', async () => {
  const o = new Outbox()
  expect(o.add('s1', turn)).toBe(false)                  // nothing is queued before the kept seq is read
  o.resume(7)
  o.add('s1', turn)
  expect(o.take().map(e => [e.seq, e.kind, 'gap' in e ? e.gap : undefined])).toEqual([[8, 'gap', { from: 7, to: 8 }], [9, 'turn', undefined]])
  o.resume(0)                                            // read back late: the live count stands
  o.add('s1', turn)
  expect(o.take().map(e => e.seq)).toEqual([10])
})

test('a batch the agent did not take is followed by a gap from before it', async () => {
  const o = new Outbox()
  o.resume(0)
  o.add('s1', turn); o.add('s1', turn)
  o.failed(o.take())
  o.add('s1', turn)
  const next = o.take()
  expect(next.map(e => [e.seq, e.kind, 'gap' in e ? e.gap : undefined])).toEqual([[3, 'gap', { from: 0, to: 3 }], [4, 'turn', undefined]])
  o.failed(next)                                         // refused too: the hole still runs from 0
  o.add('s1', turn)
  expect(o.take().map(e => [e.seq, e.kind, 'gap' in e ? e.gap : undefined])).toEqual([[5, 'gap', { from: 0, to: 5 }], [6, 'turn', undefined]])
  o.add('s1', turn)                                      // that one landed: nothing more is owed
  expect(o.take().map(e => e.kind)).toEqual(['turn'])
})

test('the agent\'s key is the one its record names for its port, else its own under the config root', async () => {
  const record = 'STEERING_AGENT_PORT=9991\nSTEERING_AGENT_KEY_FILE=/keys/9991.key\n'
  expect(agentOf(record, { home: '/h' })).toEqual({ port: '9991', keyFiles: ['/keys/9991.key'] })
  expect(agentOf(record, { port: '9992', xdg: '/x', home: '/h' })).toEqual({ port: '9992', keyFiles: ['/x/2mw2lt/agents/9992.key', '/h/.config/2mw2lt/agents/9992.key'] })
  expect(agentOf('', { xdg: 'relative', home: '/h' })).toEqual({ port: '9990', keyFiles: ['/h/.config/2mw2lt/agents/9990.key'] })
})

// The agent may not be there when the session connects; its key appears when it starts, and the
// poll finds it without a reload.
test('a key that appears after the session connected is found, and rows flow', async ($, on) => {
  const w = world(on, { agent: true, rounds: [HELD], polls: [{ watch: true }] })
  w.agentKey = undefined
  await begin($)
  await w.clock.advance(1000)
  expect(polls(w)).toEqual([])
  w.agentKey = AGENT.key
  await w.clock.advance(5000)
  expect(polls(w).length).toBeGreaterThan(0)
  await $.turn.start({ turnId: 't1', text: 'hi' })
  await w.clock.advance(50)
  expect(events(w).map(e => e.kind)).toEqual(['turn'])
})

// A batch taken while an earlier one is still out learns of that one's loss before it is numbered.
test('a batch sent behind one the agent refused opens with a gap over the refused one\'s seqs', async ($, on) => {
  let refuse!: (status: number) => void
  const w = world(on, { agent: true, rounds: [HELD], polls: [{ watch: true }], posts: [new Promise<number>(r => { refuse = r })] })
  await begin($)
  await w.clock.settle()
  await $.turn.start({ turnId: 't1', text: 'one' })
  await w.clock.advance(50)                              // A is out, and held
  await $.turn.start({ turnId: 't2', text: 'two' })
  await w.clock.advance(50)                              // B is queued behind it
  refuse(500)
  await w.clock.settle()
  const sent = posted(w).map(f => (JSON.parse(f.body!) as { events: any[] }).events)
  expect(sent.length).toBe(2)
  expect(sent[0]!.map(e => e.seq)).toEqual([1])
  expect(sent[1]!.map(e => [e.seq, e.kind, e.gap])).toEqual([[2, 'gap', { from: 0, to: 2 }], [3, 'turn', undefined]])
})

// A reconciled name is a new identity (#5661): from the moment the hold is admitted under it, while
// its stream stays open, the
// old name's watch says nothing about it, so nothing is shown until the agent says the new name is
// watched, and then under that name alone. What the old name had queued is still the old name's,
// and the agent refuses a batch that names two sessions.
test('a session renamed by its hold shows nothing until its new name is watched, then sends under it alone', async ($, on) => {
  let admit!: () => void
  let watchS2!: (p: { watch: boolean }) => void
  const reconciled = JSON.stringify({ id: null, kind: 'held', wakes: false, hold: 'h7', session: 's2' }) + '\n'
  const w = world(on, { agent: true,
    rounds: [{ lines: [new Promise<void>(r => { admit = r }), reconciled], code: 0, hold: never }],
    polls: [{ watch: true, cursor: 'c1' }, never as Promise<never>, new Promise(r => { watchS2 = r })] })
  await begin($)
  await w.clock.settle()                                 // s1 is watched; its next poll is held
  await $.turn.start({ turnId: 't1', text: 'one' })      // s1's, and the batch opens
  admit()
  await w.clock.settle()                                 // admitted as s2; s1's poll is still held
  expect(w.state.get('session')).toBe('s2')
  expect(w.spawns.length).toBe(1)                        // the admitted stream is still the one open
  await $.turn.start({ turnId: 't2', text: 'two' })      // before anyone said s2 is watched: not shown
  watchS2({ watch: true })
  await w.clock.settle()
  await $.turn.start({ turnId: 't3', text: 'three' })    // s2's, in the same window as s1's
  await w.clock.advance(50)
  await w.clock.settle()
  const sent = posted(w).map(f => JSON.parse(f.body!) as { session: string; events: any[] })
  expect(sent.map(b => [b.session, b.events.map(e => [e.session, e.seq, e.kind])])).toEqual([
    ['s1', [['s1', 1, 'turn']]],
    ['s2', [['s2', 2, 'gap'], ['s2', 3, 'turn']]],       // the rows s2 made while unwatched are owed as a gap
  ])
  expect(polls(w).map(p => p.url.split('?')[1]).slice(0, 3)).toEqual(['session=s1&producer=ps-1&after=', 'session=s1&producer=ps-1&after=c1', 'session=s2&producer=ps-1&after='])
})

// Unit 3.2: the agent sees a session's dialogs in its hooks, and hands them to the module on the
// poll to number among its own rows, so the session's events keep one numbering.
test('an ask the agent emits is numbered among the module\'s rows', async ($, on) => {
  const ask = { id: 'toolu_1', kind: 'permission', tool: 'Bash' }
  let second!: (p: { watch: boolean; cursor: string; commands: unknown[] }) => void
  const w = world(on, { agent: true, rounds: [HELD], polls: [{ watch: true, cursor: 'c1' }, new Promise(r => { second = r })] })
  await begin($)
  await w.clock.settle()
  await $.turn.start({ turnId: 't1', text: 'list the files' })
  await w.clock.advance(50)
  second({ watch: true, cursor: 'c1', commands: [{ kind: 'emit', body: { kind: 'ask', ask } }] })
  await w.clock.settle()
  await w.clock.advance(50)
  expect(events(w).map(e => [e.seq, e.kind])).toEqual([[1, 'turn'], [2, 'ask']])
  expect(events(w)[1]).toEqual({ v: 1, session: 's1', seq: 2, kind: 'ask', ask })
})

test('a command the module does not know is ignored and said', async ($, on) => {
  const w = world(on, { agent: true, rounds: [HELD], polls: [{ watch: true, cursor: 'c1',
    commands: [{ kind: 'teleport', body: {} }, { kind: 'emit', body: { kind: 'row', row: {} } }] }] })
  await begin($)
  await w.clock.settle()
  await w.clock.advance(50)
  expect(posted(w)).toEqual([])
  expect(w.logs.some(l => l.includes('ignored the agent\'s command of kind "teleport"'))).toBe(true)
  expect(w.logs.some(l => l.includes('emit was not taken: an emit of "row"'))).toBe(true)
})

// Unit 2.2: a desk prompt lands as the module's own pointer does, and the module tells the agent
// where, whose receipt settles the act. The words open with the from: line the agent wrote.
const FROM = 'console user alice, member of the team'
const WORDS = `from: ${FROM}\n\nlist the files`
const deskPrompt = { kind: 'prompt', body: { id: 'prompt-1', text: WORDS, from: FROM } }
// The rows the module sent the agent: a row it appended carries the id the session stored it under.
const sentRows = (w: ReturnType<typeof world>) => posted(w).filter(f => f.url.endsWith('/steering/surface/events')).flatMap(f => JSON.parse(f.body!).events).filter(e => e.kind === 'row').map(e => e.row)
const landings = (w: ReturnType<typeof world>) => posted(w).filter(f => f.url.endsWith('/steering/surface/landed')).map(f => JSON.parse(f.body!))

// The poll after the first is answered with the desk's prompt once `ready` has run.
async function prompted(opts: { submitDrops?: boolean } = {}, ready: ($: any, w: ReturnType<typeof world>) => Promise<void> = async () => {}) {
  return async ($: any, on: any) => {
    let second!: (p: { watch: boolean; cursor: string; commands: unknown[] }) => void
    const w = world(on, { agent: true, rounds: [HELD], polls: [{ watch: true, cursor: 'c1' }, new Promise(r => { second = r })], ...opts })
    await begin($)
    await w.clock.settle()
    await ready($, w)
    second({ watch: true, cursor: 'c1', commands: [deskPrompt] })
    await w.clock.settle()
    await w.clock.advance(50)
    return w
  }
}

test('a desk prompt to an idle session starts a turn and is reported submitted', async ($, on) => {
  const w = await (await prompted())($, on)
  expect(w.submits).toEqual([WORDS])
  expect(w.asUser).toEqual([true])                 // the model reads the from: line first, unframed
  expect(w.appends).toEqual([])
  expect(landings(w)).toEqual([{ session: 's1', producer: 'ps-1', id: 'prompt-1', landed: 'new-turn' }])
  const sent = posted(w).find(f => f.url.endsWith('/steering/surface/landed'))!
  expect(sent.headers['X-Steering-Agent-Key']).toBe(AGENT.key)
  // Its row is the speaker's, not the plugin's: the desk says who spoke.
  expect(sentRows(w)).toEqual([{ role: 'user', origin: FROM, blocks: [{ type: 'text', text: WORDS }] }])
})

test('a desk prompt to a busy session is appended to the running turn and reported so', async ($, on) => {
  const w = await (await prompted({}, async $ => { await $.turn.start({ turnId: 't1', text: 'list the files' }) }))($, on)
  expect(w.submits).toEqual([])
  expect(landings(w)).toEqual([{ session: 's1', producer: 'ps-1', id: 'prompt-1', landed: 'running-turn' }])
  // The session stored the row, under an id of its own, and that row is the speaker's.
  expect(sentRows(w).filter(r => r.origin === FROM)).toEqual([{ id: expect.any(String), role: 'user', origin: FROM, blocks: [{ type: 'text', text: WORDS }] }])
})

// #7827: after the person's Esc the engine drops every submit until they type again. The words are
// appended for the session's next request, never lost, and the receipt says so and why.
test('a desk prompt after the person\'s Esc is appended for the next request and reported with why', async ($, on) => {
  const w = await (await prompted({ submitDrops: true }, async $ => {
    await $.turn.start({ turnId: 't1', text: 'sleep 20' })
    await $.turn.complete({ turnId: 't1', text: '', reason: 'aborted' } as never)
  }))($, on)
  expect(w.submits).toEqual([WORDS])
  expect(landings(w)).toEqual([{ session: 's1', producer: 'ps-1', id: 'prompt-1', landed: 'queued',
    why: 'the session was stopped and nobody has sent input since' }])
  expect(sentRows(w).filter(r => r.origin === FROM)).toEqual([{ id: expect.any(String), role: 'user', origin: FROM, blocks: [{ type: 'text', text: WORDS }] }])
})

test('a desk prompt without a speaker is refused, not landed', async ($, on) => {
  let second!: (p: { watch: boolean; cursor: string; commands: unknown[] }) => void
  const w = world(on, { agent: true, rounds: [HELD], polls: [{ watch: true, cursor: 'c1' }, new Promise(r => { second = r })] })
  await begin($)
  await w.clock.settle()
  second({ watch: true, cursor: 'c1', commands: [{ kind: 'prompt', body: { id: 'prompt-1', text: 'list the files' } }] })
  await w.clock.settle()
  expect(w.submits).toEqual([])
  expect(w.appends).toEqual([])
  expect(landings(w)).toEqual([{ session: 's1', producer: 'ps-1', id: 'prompt-1', refused: 'the agent\'s prompt carried no words or no speaker' }])
})
