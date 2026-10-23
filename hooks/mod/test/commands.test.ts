import { test, expect } from 'claude-code/testing'
import { classified, commandNote, ranResult, RESULT_MAX } from '../surface'
import { world, begin, never } from './world'

// Slash commands from the desk (#8432 U1), the module's half: the list it tells the agent, and a
// run through the engine's own command route, attributed beside the command, never inside it.

const HELD = { lines: [], code: 0 as const, hold: never }
const FROM = 'console user alice, member of the team'
const NOTE = commandNote(FROM, 'u0who', 'alpha')
const OURS = { kind: 'plugin', name: '2mw2lt' }

const LIST = [
  { name: 'u0who', description: 'Names the asker.', source: 'plugin', plugin: 'u0' },
  { name: 'compact', description: 'Free up context', source: 'builtin' },
  { name: 'context', description: 'Visualize context usage', source: 'builtin' },
  { name: 'permissions', description: 'Manage rules', source: 'builtin' },
  { name: 'brand-new-builtin', description: 'Something a later Claude Code ships', source: 'builtin' },
]

type World = ReturnType<typeof world>
const postedTo = (w: World, route: string) => w.fetches.filter(f => f.method === 'POST' && f.url.endsWith(`/steering/surface/${route}`)).map(f => JSON.parse(f.body!))
const rowsOf = (w: World) => postedTo(w, 'events').flatMap(b => b.events).filter((e: any) => e.kind === 'row').map((e: any) => e.row)

// The engine beneath the module: its command list, and each run it was asked for, answered when
// the test says.
function engine(on: any, list: unknown[] = LIST) {
  const e = { list, runs: [] as { command: string; args: string; origin: unknown }[], answers: [] as ((r: unknown) => void)[], fails: [] as ((err: Error) => void)[] }
  on('command.list', async () => ({ value: e.list }) as never)
  on('command.run', async ($: unknown, run: { command: string; args: string; origin: unknown }) => {
    e.runs.push({ command: run.command, args: run.args, origin: run.origin })
    return new Promise((resolve, reject) => { e.answers.push(resolve); e.fails.push(reject) })
  })
  return e
}

// A watched session whose second poll hands the module one desk command once `ready` has run.
async function commanded($: any, on: any, body: Record<string, unknown> = { id: 'cmd-1', name: 'u0who', args: 'alpha', from: FROM },
                         ready: (w: World) => Promise<void> = async () => {}) {
  let second!: (p: unknown) => void
  const w = world(on, { agent: true, rounds: [HELD], polls: [{ watch: true, cursor: 'c1' }, new Promise(r => { second = r })] })
  const e = engine(on)
  await begin($)
  await w.clock.settle()
  await ready(w)
  second({ watch: true, cursor: 'c1', commands: [{ kind: 'command', body }] })
  await w.clock.settle()
  return { w, e }
}

// The engine dispatching the module's run of a prompt command: its prompt.submit, under the
// module's own origin, then its echo row, as U0 measured on 2.1.308.
async function dispatched($: any, w: World, name = 'u0who', args = 'alpha', origin: unknown = OURS) {
  await $.prompt.submit({ text: `/${name} ${args}`, command: { name, args }, origin, wait: false } as never)
  await w.clock.settle()
}
const echo = (uuid: string, name = 'u0who', args = 'alpha') => ({ message: { type: 'user', role: 'user',
  content: [{ type: 'text', text: `<command-message>${name}</command-message>\n<command-name>/${name}</command-name>\n<command-args>${args}</command-args>` }] },
  door: 'command', origin: OURS, uuid })
const answer = (uuid: string) => ({ message: { type: 'assistant', role: 'assistant', content: [{ type: 'text', text: 'ASKER=alice' }] },
  door: 'response', origin: { kind: 'model', model: 'claude-test' }, uuid })

// The agent keeps only what the desk can be offered: every command not built in expands into the
// model's input, a built-in is classed by its name, and a built-in the map does not know (a newer
// Claude Code's) is left out, so nothing unclassified ever reaches the desk.
test('the list classes every non-built-in as a prompt, a built-in by name, and leaves an unknown built-in out', async () => {
  expect(classified(LIST)).toEqual([
    { name: 'u0who', description: 'Names the asker.', hint: '', source: 'plugin', class: 'prompt' },
    { name: 'compact', description: 'Free up context', hint: '', source: 'builtin', class: 'session' },
    { name: 'context', description: 'Visualize context usage', hint: '', source: 'builtin', class: 'session' },
    { name: 'permissions', description: 'Manage rules', hint: '', source: 'builtin', class: 'control' },
  ])
  // A name an object inherits is no class.
  expect(classified([{ name: 'toString', description: '', source: 'builtin' }])).toEqual([])
})

// A repo's skill or markdown command can carry a built-in's name, and the engine may run either under
// it: the name keeps the built-in's class, so a desk member cannot run `exit` as a prompt.
test('a command named like a built-in takes the built-in\'s class, whoever lists it', () => {
  const got = classified([{ name: 'exit', description: 'My own exit', source: 'projectSettings' }, { name: 'u0who', description: '', source: 'plugin' }])
  expect(got.map(c => [c.name, c.class])).toEqual([['exit', 'control'], ['u0who', 'prompt']])
})

test('a local command\'s result is its output and its notes, cut at 4 KiB', async () => {
  expect(ranResult({})).toBeUndefined()
  expect(ranResult({ context: ['ctx'] })).toBe('ctx')
  expect(ranResult({ text: 'out', context: ['a', 'b'] })).toBe('out\n\na\n\nb')
  expect(ranResult({ text: 'x'.repeat(RESULT_MAX + 10) })!.length).toBe(RESULT_MAX)
})

// The agent keeps the list per session and conversation, so it is told it once the poll stands,
// and again only when it changed: a list of a hundred entries is not resent every turn.
test('the list is told the agent once the poll stands, and again at a turn\'s end only when it changed', async ($, on) => {
  const w = world(on, { agent: true, rounds: [HELD], polls: [{ watch: false, cursor: 'c1' }] })
  const e = engine(on)
  await begin($)
  await w.clock.settle()
  const told = () => postedTo(w, 'commands')
  expect(told()).toEqual([{ session: 's1', producer: 'ps-1', commands: classified(LIST) }])
  await $.turn.start({ turnId: 't1', text: 'hi' })
  await $.turn.complete({ turnId: 't1', text: '', reason: 'answer' } as never)
  await w.clock.settle()
  expect(told().length).toBe(1)
  e.list = [...LIST, { name: 'new-skill', description: 'Loaded mid-turn', source: 'plugin' }]
  await $.turn.start({ turnId: 't2', text: 'hi' })
  await $.turn.complete({ turnId: 't2', text: '', reason: 'answer' } as never)
  await w.clock.settle()
  expect(told().length).toBe(2)
  expect(told()[1].commands.at(-1)).toEqual({ name: 'new-skill', description: 'Loaded mid-turn', hint: '', source: 'plugin', class: 'prompt' })
})

// U0 finding 3: a note appended before the run is read by whatever turn reads next. Appended as
// the engine dispatches the command, it is read by the command's own turn.
test('an idle session\'s prompt command runs through the engine, its note appended at dispatch, and is reported a new turn', async ($, on) => {
  const { w, e } = await commanded($, on)
  expect(e.runs.map(r => [r.command, r.args])).toEqual([['u0who', 'alpha']])
  expect(w.submits).toEqual([])                         // never typed as text with a from: line in it
  expect(w.appends).toEqual([])                         // not yet: no turn has been dispatched
  await dispatched($, w)
  expect(w.appends).toEqual([NOTE])
  expect(NOTE).toBe(`from: ${FROM}\n\nThe next command, /u0who alpha, is theirs.`)
  e.answers[0]!({})
  await w.clock.settle()
  expect(postedTo(w, 'ran')).toEqual([{ session: 's1', producer: 'ps-1', id: 'cmd-1', landed: 'new-turn' }])
})

// The busy turn must never read the command's speaker as its own: the run is queued by the engine
// until that turn ends, the act is reported queued at once, and the note waits for the dispatch.
test('a busy session\'s command is reported queued at once, and its note waits for the command\'s own turn', async ($, on) => {
  const { w, e } = await commanded($, on, undefined, async () => { await $.turn.start({ turnId: 't1', text: 'a long job' }) })
  expect(postedTo(w, 'ran')).toEqual([{ session: 's1', producer: 'ps-1', id: 'cmd-1', landed: 'queued',
    why: 'the session is running a turn; the command runs when it ends' }])
  expect(e.runs.length).toBe(1)
  await $.turn.complete({ turnId: 't1', text: '', reason: 'answer' } as never)
  await w.clock.settle()
  expect(w.appends).toEqual([])                         // the busy turn ended without it
  await dispatched($, w)
  expect(w.appends).toEqual([NOTE])
  e.answers[0]!({})
  await w.clock.settle()
  expect(postedTo(w, 'ran').at(-1)).toEqual({ session: 's1', producer: 'ps-1', id: 'cmd-1', landed: 'new-turn' })
  await $.turn.start({ turnId: 'queued-command', text: '' })
  await $.session.append(echo('queued-echo') as never)
  await $.turn.complete({ turnId: 'queued-command', answer: '', durationMs: 1, isAborted: false, reason: 'answer' })
  await w.clock.settle()
  expect(postedTo(w, 'ran').map(r => r.landed)).toEqual(['queued', 'new-turn', 'completed'])
})

// A person typing the same command is not the desk's speaker: only the module's own run takes the
// note, and a note whose run resolved without a dispatch is never read by a later one.
test('only the module\'s own dispatch takes the note, and a run that resolved without one drops it', async ($, on) => {
  const { w, e } = await commanded($, on, { id: 'cmd-1', name: 'context', args: '', from: FROM })
  await dispatched($, w, 'context', '', { kind: 'composer' })
  expect(w.appends).toEqual([])
  e.answers[0]!({ context: ['Context: 12k of 200k tokens'] })
  await w.clock.settle()
  expect(postedTo(w, 'ran')).toEqual([{ session: 's1', producer: 'ps-1', id: 'cmd-1', landed: 'ran', result: 'Context: 12k of 200k tokens' }])
  await dispatched($, w, 'context', '')
  expect(w.appends).toEqual([])
})

// A test's throwing hook is skipped, so the rejection here is the kit's engine finding nothing to
// run the command: its own words reach the refusal.
test('a run the engine rejects is refused, naming the engine\'s reason', async ($, on) => {
  const { w, e } = await commanded($, on)
  e.fails[0]!(new Error('Unknown command: /u0who'))
  await w.clock.settle()
  expect(postedTo(w, 'ran')).toEqual([{ session: 's1', producer: 'ps-1', id: 'cmd-1',
    refused: expect.stringMatching(/^the engine refused \/u0who: .*no implementation for command\.run/) }])
})

test('a queued command reports the engine\'s later refusal', async ($, on) => {
  const { w, e } = await commanded($, on, undefined, async () => { await $.turn.start({ turnId: 'busy', text: 'working' }) })
  e.fails[0]!(new Error('Unknown command: /u0who'))
  await w.clock.settle()
  expect(postedTo(w, 'ran')).toEqual([
    { session: 's1', producer: 'ps-1', id: 'cmd-1', landed: 'queued', why: 'the session is running a turn; the command runs when it ends' },
    { session: 's1', producer: 'ps-1', id: 'cmd-1', refused: expect.stringMatching(/^the engine refused \/u0who:/) },
  ])
})

test('a prompt command reports completion after its dispatch promise resolved', async ($, on) => {
  const { w, e } = await commanded($, on)
  await dispatched($, w)
  e.answers[0]!({})
  await w.clock.settle()
  await $.turn.start({ turnId: 'command-turn', text: '' })
  await $.session.append(echo('command-echo') as never)
  await $.turn.complete({ turnId: 'subagent', agentId: 'child', answer: '', durationMs: 1, isAborted: false, reason: 'answer' })
  expect(postedTo(w, 'ran').length).toBe(1)
  await $.turn.complete({ turnId: 'command-turn', answer: '', durationMs: 1, isAborted: false, reason: 'answer' })
  await w.clock.settle()
  expect(postedTo(w, 'ran')).toEqual([
    { session: 's1', producer: 'ps-1', id: 'cmd-1', landed: 'new-turn' },
    { session: 's1', producer: 'ps-1', id: 'cmd-1', landed: 'completed' },
  ])
})

test('a failed command completion is retried while the next poll is held', async ($, on) => {
  const { w, e } = await commanded($, on)
  await dispatched($, w)
  e.answers[0]!({})
  await w.clock.settle()
  await $.turn.start({ turnId: 'command-turn', text: '' })
  await $.session.append(echo('command-echo') as never)
  w.posts.push(503)
  await $.turn.complete({ turnId: 'command-turn', answer: '', durationMs: 1, isAborted: false, reason: 'answer' })
  await w.clock.settle()
  expect(postedTo(w, 'ran').map(r => r.landed)).toEqual(['new-turn', 'completed'])
  await w.clock.advance(5000)
  expect(postedTo(w, 'ran').map(r => r.landed)).toEqual(['new-turn', 'completed', 'completed'])
  await w.clock.advance(5000)
  expect(postedTo(w, 'ran').length).toBe(3)
})

test('a reloaded module sends the command receipt its previous instance retained', async ($, on) => {
  const w = world(on, { agent: true, rounds: [HELD], polls: [{ watch: false, cursor: 'c1' }] })
  engine(on)
  w.state.set('commandReceipts', [{ session: 's1', id: 'cmd-old', outcome: { landed: 'completed' } }])
  await begin($)
  await w.clock.settle()
  expect(postedTo(w, 'ran')).toEqual([{ session: 's1', producer: 'ps-1', id: 'cmd-old', landed: 'completed' }])
  expect(w.state.get('commandReceipts')).toEqual([])
})

test('a failed receipt does not hold back another command\'s receipt', async ($, on) => {
  const w = world(on, { agent: true, rounds: [HELD], polls: [{ watch: false, cursor: 'c1' }], posts: Array(20).fill(503) })
  engine(on)
  w.state.set('commandReceipts', [
    { session: 's1', id: 'cmd-old', outcome: { landed: 'completed' } },
    { session: 's1', id: 'cmd-next', outcome: { landed: 'ran', result: 'Context usage' } },
  ])
  await begin($)
  await w.clock.settle()
  expect(postedTo(w, 'ran').some(r => r.id === 'cmd-next' && r.result === 'Context usage')).toBe(true)
  expect((w.state.get('commandReceipts') as any[]).map(r => r.id)).toEqual(['cmd-old', 'cmd-next'])
  w.posts.length = 0
  await w.clock.advance(5000)
  expect(w.state.get('commandReceipts')).toEqual([])
})

test('a queued local command reports its result when it runs', async ($, on) => {
  const { w, e } = await commanded($, on, { id: 'cmd-1', name: 'context', args: '', from: FROM },
    async () => { await $.turn.start({ turnId: 'busy', text: 'working' }) })
  e.answers[0]!({ context: ['Context usage'] })
  await w.clock.settle()
  expect(postedTo(w, 'ran').map(r => [r.landed, r.result])).toEqual([['queued', undefined], ['ran', 'Context usage']])
})

for (const reason of ['aborted', 'error'] as const) {
  test(`a command whose turn ended ${reason} reports a refusal`, async ($, on) => {
    const { w, e } = await commanded($, on)
    await dispatched($, w)
    e.answers[0]!({})
    await w.clock.settle()
    await $.turn.start({ turnId: 'failed', text: '' })
    await $.session.append(echo('failed-echo') as never)
    await $.turn.complete({ turnId: 'failed', answer: '', durationMs: 1, isAborted: reason === 'aborted', reason })
    await w.clock.settle()
    expect(postedTo(w, 'ran').at(-1)).toEqual({ session: 's1', producer: 'ps-1', id: 'cmd-1', refused: `the command's turn ended: ${reason}` })
  })
}

test('a command without a speaker is refused, never run', async ($, on) => {
  const { w, e } = await commanded($, on, { id: 'cmd-1', name: 'u0who', args: '' })
  expect(e.runs).toEqual([])
  expect(postedTo(w, 'ran')).toEqual([{ session: 's1', producer: 'ps-1', id: 'cmd-1', refused: 'the agent\'s command carried no name or no speaker' }])
})

// U0 finding 2: a prompt command's answer is stored under the model's origin, so only its turn
// tells it from the session's own words. The desk draws the turn under its speaker by the act its
// rows carry, from the echo to the turn's end, and nothing after.
test('a desk command\'s rows carry its act from the echo row to its turn\'s end', async ($, on) => {
  const { w, e } = await commanded($, on)
  await dispatched($, w)
  e.answers[0]!({})
  await $.turn.start({ turnId: 't9', text: '' })
  await $.session.append(echo('echo-1') as never)
  await $.session.append(answer('answer-1') as never)
  await $.turn.complete({ turnId: 't9', text: '', reason: 'answer' } as never)
  await $.session.append(answer('after-1') as never)
  await w.clock.advance(50)
  const rows = rowsOf(w)
  expect(rows.find((r: any) => r.origin === FROM)).toEqual({ id: expect.any(String), role: 'user', origin: FROM, act: 'cmd-1',
    blocks: [{ type: 'text', text: 'The next command, /u0who alpha, is theirs.' }] })
  expect(rows.filter((r: any) => r.origin !== FROM).map((r: any) => [r.id, r.act])).toEqual([['echo-1', 'cmd-1'], ['answer-1', 'cmd-1'], ['after-1', undefined]])
})

// The same command typed at the terminal stores an echo too, under the person's origin: it is not
// the desk's, and starts no stamping.
test('an echo the module did not run stamps nothing', async ($, on) => {
  const w = world(on, { agent: true, rounds: [HELD], polls: [{ watch: true, cursor: 'c1' }] })
  engine(on)
  await begin($)
  await w.clock.settle()
  await $.session.append({ ...echo('echo-1'), origin: { kind: 'composer' } } as never)
  await $.session.append(answer('answer-1') as never)
  await w.clock.advance(50)
  expect(rowsOf(w).map((r: any) => r.act)).toEqual([undefined, undefined])
})

// A desk command's turn stamps its rows with the desk speaker's act, and a person who types at the
// terminal meanwhile is not that speaker: their prompt stays theirs.
test('a prompt typed at the terminal during a desk command\'s turn is not stamped as the desk\'s', async ($, on) => {
  const { w } = await commanded($, on)
  await dispatched($, w)
  await $.session.append({ ...echo('echo-1'), origin: OURS } as never)
  await $.session.append({ message: { type: 'user', role: 'user', content: [{ type: 'text', text: 'wait, one more thing' }] }, door: 'prompt', origin: { kind: 'composer' }, uuid: 'typed-1' } as never)
  await $.session.append(answer('answer-1') as never)
  await w.clock.advance(50)
  expect(rowsOf(w).filter((r: any) => r.origin !== FROM).map((r: any) => [r.id, r.act])).toEqual([['echo-1', 'cmd-1'], ['typed-1', undefined], ['answer-1', 'cmd-1']])
})

// An echo the engine never stored (a built-in skill's, say) must not wait for the next run of the
// same name and stamp its turn with the earlier speaker.
test('a dispatch whose echo never came does not stamp the next run of that name', async ($, on) => {
  const { w } = await commanded($, on)
  await dispatched($, w)
  await $.turn.start({ turnId: 't1', text: 'x' })
  await $.turn.complete({ turnId: 't1', text: '', reason: 'answer' } as never)
  await w.clock.settle()
  // The same command, typed by a person under the module's origin, would only match a stale entry.
  await $.session.append({ ...echo('echo-2'), origin: OURS } as never)
  await w.clock.advance(50)
  expect(rowsOf(w).filter((r: any) => r.origin !== FROM).map((r: any) => [r.id, r.act])).toEqual([['echo-2', undefined]])
})
