import type { On } from 'claude-code'
import { test, expect, mock } from 'claude-code/testing'
import { lines, atLeast, pick, invokes, answerable, nextSeat, asksSeat, seatCall, entryRefusal, resendId, POINTER, CHECKPOINT, CONNECT_AGAIN, SEAT_SECTION } from '../frames'

test('lines are whole only once their newline arrives', async () => {
  const a = lines('{"id":"f1","kind":"say","wakes":true}\n{"id":"f2","ki')
  expect(a.complete).toEqual([{ id: 'f1', kind: 'say', wakes: true }])
  const b = lines(a.rest + 'nd":"usage","wakes":false}\nnot json\n')
  expect(b.complete).toEqual([{ id: 'f2', kind: 'usage', wakes: false }])
  expect(b.rest).toBe('')
})

test('the floor admits 2.1.289 and later, and nothing older', async () => {
  expect(atLeast('2.1.289')).toBe(true)
  expect(atLeast('2.1.290')).toBe(true)
  expect(atLeast('2.2.0')).toBe(true)
  expect(atLeast('2.1.242')).toBe(false)
  expect(atLeast('2.0.999')).toBe(false)
})

const ENVELOPE = '{"kind":"envelope","id":"e1","ulid":"01JENV","from":"from: the seat","text":"do it"}'
const SAY = '{"kind":"say","id":"f1","from":"session peer","text":"hello"}'
const USAGE = '{"kind":"usage","id":"u1","account":1}'
const SEAT = '{"kind":"seat","id":"s9","name":"new"}'
const RECORDING = [USAGE, SAY, ENVELOPE, SEAT, '{"kind":"say","id":"f2","text":"not pending"}']
  .map(f => `data: ${f}`).join('\n') + '\n'

test('pick returns the pending frames verbatim, in recording order', async () => {
  const { text, envelopes } = pick(RECORDING, ['s9', 'e1', 'f1'])
  expect(text).toBe([SAY, ENVELOPE, SEAT].join('\n'))
  expect(envelopes).toEqual(['01JENV'])
  expect(pick(RECORDING, ['s9', 'e1', 'gone']).found).toEqual(['e1', 's9'])
})

test('a frame of any kind is picked by its id, and only an envelope is acked', async () => {
  const { text, envelopes } = pick(RECORDING, ['s9'])
  expect(text).toBe(SEAT)
  expect(envelopes).toEqual([])
  expect(pick(RECORDING, ['01JENV']).text).toBe('')
  expect(pick(RECORDING, ['nothing-recorded']).text).toBe('')
})

// The behaviour tests fake the engine beneath the plugin: the session, the clock, the files,
// host commands and hold.py's child. Kit gap (2.1.290): a plugin's own `$.session.append` never
// reaches a test's hook beneath it ("no implementation for session.append"), whatever the test
// registers, so a row is observed through the debug line the module writes as it appends.

// A round is one child: its lines in order (a promise in among them holds it there), then its
// exit code, or a throw.
type Round = { lines: (string | Promise<void> | { stderr: string })[]; code: number | null | 'throw'; hold?: Promise<void>; holdMs?: number }

const TOOL = 'mcp__2mw2lt__frames'
const FRAME_PATH = '/ws/.claude/steering-frames/s1.ndjson'
const CLAIM_PATH = '/ws/.claude/steering-holders/ps-1.json'
const ROOT = '/ws/checkout'
const line = (id: string | null, kind: string, wakes: boolean) => JSON.stringify({ id, kind, wakes }) + '\n'

function world(on: On, { version = '2.1.290', rounds = [] as Round[], recording = '', keep = 'Keep verbatim: steering session s1; branch b; checkpoint https://github.com/example-org/example-repo/pull/1.\n',
                         enrolled = true, submitRejects = false, ackFails = false,
                         // The engine's own answer to `$.session.usage({ breakdown })`: the window it measures
                         // against, the token count it auto-compacts at, and who settled the window.
                         context = { point: 167_000 as number | undefined, raw: 200_000, source: 'auto', model: 'claude-test' } } = {}) {
  const w = {
    clock: mock.clock(on, { now: 1_000_000 }),
    root: '',
    writes: [] as { path: string; text: string }[],
    tools: [] as string[],
    runs: [] as string[][],
    cwds: [] as (string | undefined)[],
    spawns: [] as string[][],
    submits: [] as string[],
    appends: [] as string[],
    logs: [] as string[],
    psession: 'ps-1',
    claimFails: false,
    compacts: [] as { trigger: string; instructions?: string }[],
    recording,
    enrolled,
    readGate: undefined as Promise<void> | undefined,
    state: new Map<string, unknown>(),
    // The next read of this key is answered with what it held when asked, once `release` runs.
    holdNextGet: undefined as { key: string; release: Promise<void> } | undefined,
    context,
    usageCalls: 0,
    stepUsage: 0,
    // What seat_section.py answers: 0 with the rulings, 1 for a session that does not hold the seat.
    seat: { code: 1, stdout: '' },
    // Held until released: a seat_section.py run still out.
    seatGate: undefined as Promise<void> | undefined,
    // What each lease client was handed on stdin, and what it answers in turn.
    stdins: [] as (string | undefined)[],
    leaseOut: [] as string[],
    check: { decision: 'allow' } as { decision: 'allow' | 'ask' | 'deny'; reason?: string },
  }
  on('state.get', async ($, e) => {
    const value = w.state.get(e.key)
    const held = w.holdNextGet
    if (held?.key === e.key) { w.holdNextGet = undefined; await held.release }
    return { value: { value, version: 0 } } as never
  })
  on('state.set', async ($, e) => { w.state.set(e.key, e.value); return { value: { isSet: true, version: 0 } } as never })
  on('session.start', async ($, e) => ({ cwd: e.cwd }))
  on('session.version', async () => ({ value: { version } }))
  on('session.root', async () => ({ value: ROOT }))
  on('session.id', async () => ({ value: w.psession }))
  on('turn.start', async ($, e) => ({ turnId: e.turnId }))
  on('turn.complete', async ($, e) => ({ text: e.text }))
  on('fs.read', async ($, e) => {
    if (e.path.endsWith('/.claude-plugin/plugin.json')) {
      w.root = e.path.slice(0, -'/.claude-plugin/plugin.json'.length)
      return { value: '{"version":"0.8.99"}' }
    }
    if (e.path === FRAME_PATH) { if (w.readGate) await w.readGate; return { value: w.recording } }
    throw new Error('ENOENT')
  })
  on('fs.write', async ($, e) => { w.writes.push(e); return { value: undefined } })
  on('tool.register', async ($, e) => { w.tools.push(e.name); return { value: { tool: `mcp__2mw2lt__${e.name}` } } })
  on('process.run', async ($, e) => {
    w.runs.push([...e.argv])
    w.cwds.push(e.init?.cwd)
    if (/\/(lease|card)\.py$/.test(String(e.argv[1]))) {
      w.stdins.push(e.init?.stdin)
      const out = w.leaseOut.shift() ?? 'relayed: 01AB to amber'
      return { value: { exitCode: /^(REJECTED|refused)/.test(out) ? 1 : 0, stdout: out, stderr: '', isStdoutTruncated: false, isStderrTruncated: false } }
    }
    if (String(e.argv[1]).endsWith('/seat_section.py')) {
      if (w.seatGate) await w.seatGate
      return { value: { exitCode: w.seat.code, stdout: w.seat.stdout, stderr: '', isStdoutTruncated: false, isStderrTruncated: false } }
    }
    const out = e.argv.includes('--session-of') ? (w.enrolled ? 's1\n' : '')
      : e.argv.includes('--claim-path') ? (w.claimFails ? '' : `/ws/.claude/steering-holders/${e.argv[3]}.json\n`)
      : e.argv.includes('--frame-path') ? `${FRAME_PATH}\n`
      : e.argv.includes('--keep') ? keep : ''
    const exitCode = (e.argv.includes('--claim-path') && w.claimFails) || (e.argv.some(a => a.endsWith('/ack.py')) && ackFails) ? 1 : 0
    return { value: { exitCode, stdout: out, stderr: '', isStdoutTruncated: false, isStderrTruncated: false } }
  })
  on('process.spawn', async function* ($, e) {
    w.spawns.push([...e.argv])
    w.cwds.push(e.cwd)
    const round = rounds.shift()
    if (!round) throw new Error('no more children')
    for (const text of round.lines) {
      if (typeof text === 'string') yield { stream: 'stdout' as const, text }
      else if ('stderr' in text) yield { stream: 'stderr' as const, text: text.stderr }
      else await text
    }
    if (round.hold) await round.hold
    if (round.holdMs) await w.clock.sleep(round.holdMs)
    if (round.code === 'throw') throw new Error('the child broke')
    return { value: { code: round.code, signal: null } }
  })
  on('prompt.submit', async ($, e) => {
    w.submits.push(e.text)
    if (submitRejects) throw new Error('the prompt was refused')
    return { text: e.text }
  })
  on('tool.call', { tool: 'Bash' }, async () => ({ result: { stdout: '', stderr: '', interrupted: false } }) as never)
  on('ui.log', async ($, e) => {
    w.logs.push(e.text)
    const appended = /^2mw2lt: appends "(.*)"$/s.exec(e.text)
    if (appended) w.appends.push(appended[1] ?? '')
    return { value: undefined }
  })
  on('session.compact', async ($, e) => {
    w.compacts.push({ trigger: e.trigger, instructions: e.instructions })
    return { messages: [MESSAGE] }
  })
  on('session.measure', async ($, e) => ({ changed: e.changed }))
  on('session.usage', async () => {
    w.usageCalls++
    const c = w.context
    return { value: { startedAt: 0, context: { window: 1_000_000, breakdown: {
      autoCompactThreshold: c.point, isAutoCompactEnabled: c.point !== undefined, rawMaxTokens: c.raw,
      autocompactSource: c.source, model: c.model } } } } as never
  })
  on('turn.step', async function* ($, e) { return { turnId: e.turnId, index: e.index, answer: '', toolUses: [], stopReason: 'tool_use', usage: { model: 'claude-test', input_tokens: 1, output_tokens: 1, cache_read_input_tokens: w.stepUsage - 1, cache_creation_input_tokens: 0 } } as never })
  on('session.end', async ($, e) => ({ sessionId: e.sessionId }))
  on('classic.SessionStart', async () => ({}) as never)
  on('prompt.compose', async () => ({ sections: [{ id: 'intro', text: 'engine', scope: 'shared' as const }] }))
  on('tool.check', async () => w.check as never)
  return w
}

const MESSAGE = { role: 'user' as const, text: 'hello', toolUses: [] }
const never = new Promise<void>(() => {})
const begin = async ($: any) => $.session.start({ cwd: '/ws/checkout', surface: 'terminal', isInteractive: true })

test('an idle session gets one pointer for two waking frames in a row', async ($, on) => {
  const w = world(on, { rounds: [{ lines: [line('f1', 'say', true), line('e1', 'envelope', true)], code: 0, hold: never }] })
  await begin($)
  await w.clock.settle()
  expect(w.submits).toEqual([POINTER])
  expect(w.appends).toEqual([])
})

test('a frame that does not wake submits nothing', async ($, on) => {
  const w = world(on, { rounds: [{ lines: [line(null, 'usage', false), line('f9', 'say', false)], code: 0, hold: never }] })
  await begin($)
  await w.clock.settle()
  expect(w.spawns.length).toBe(1)
  expect(w.submits).toEqual([])
  expect(w.appends).toEqual([])
})

test('presence, fleet and usage frames alone submit no pointer', async ($, on) => {
  const w = world(on, { rounds: [{ lines: [line('p1', 'presence', false), line('g1', 'fleet', false), line('u1', 'usage', false), line('u2', 'usage', false)], code: 0, hold: never }] })
  await begin($)
  await w.clock.settle()
  expect(w.submits).toEqual([])
  expect(w.appends).toEqual([])
  expect(w.state.get('pending') ?? []).toEqual([])
})

test('a waking frame with no id submits nothing: the tool finds frames by id and would answer empty', async ($, on) => {
  const w = world(on, { rounds: [{ lines: [line(null, 'say', true)], code: 0, hold: never }] })
  await begin($)
  await w.clock.settle()
  expect(w.submits).toEqual([])
  expect(w.state.get('pending') ?? []).toEqual([])
})

// The stream reopens and the daemon replays the frames it still holds, so one id arrives again.
async function replays($: any, on: On, first: string, opts: { ackFails?: boolean } = {}) {
  let again!: () => void
  const w = world(on, {
    ...opts,
    rounds: [{ lines: [line('f1', first, true), new Promise<void>(r => { again = r }), line('f1', first, true)], code: 0, hold: never }],
    recording: `data: {"kind":"${first}","id":"f1","ulid":"U1"}\n`,
  })
  await begin($)
  await w.clock.settle()
  const read = await $.tool.call({ tool: TOOL } as never)
  again()
  await w.clock.settle()
  return { w, read: (read as { result: unknown }).result }
}

test('a frame replayed after the model read it submits no pointer', async ($, on) => {
  const { w, read } = await replays($, on, 'say')
  expect(read).toBe('{"kind":"say","id":"f1","ulid":"U1"}')
  expect(w.submits).toEqual([POINTER])
  expect(w.state.get('pending')).toEqual([])
})

test('an envelope whose ack failed is not read: its replay wakes the session again', async ($, on) => {
  const { w } = await replays($, on, 'envelope', { ackFails: true })
  expect(w.submits).toEqual([POINTER, POINTER])
})

test('an envelope replayed after its ack landed submits no pointer', async ($, on) => {
  const { w } = await replays($, on, 'envelope')
  expect(w.submits).toEqual([POINTER])
})

test('a frame replayed while it is still pending is added once and pointed once', async ($, on) => {
  const w = world(on, { rounds: [{ lines: [line('f1', 'say', true), line('f1', 'say', true)], code: 0, hold: never }] })
  await begin($)
  await w.clock.settle()
  await $.tool.call({ tool: TOOL } as never)       // the recording does not hold it yet: unpointed, still pending
  await w.clock.settle()
  expect(w.state.get('pending')).toEqual(['f1'])
  expect(w.submits).toEqual([POINTER])
})

const KICK = { at: '2026-10-06T08:00:00Z', interval: 1800 }
const routine = (id: string) => JSON.stringify({ id, kind: 'kick', wakes: false, routine: KICK }) + '\n'
const answers = (w: { runs: string[][] }) => w.runs.filter(argv => argv.includes('--answer-kick'))

test('a routine kick is answered by the client, with no turn bought', async ($, on) => {
  const w = world(on, { rounds: [{ lines: [routine('k1')], code: 0, hold: never }] })
  await begin($)
  await w.clock.settle()
  expect(answers(w)).toEqual([['python3', `${w.root}/steering/enroll/hold.py`, '--answer-kick', KICK.at,
                               '--provider', 'claude', '--provider-session', 'ps-1', 's1']])
  expect(w.submits).toEqual([])
  expect(w.appends).toEqual([])
})

test('a kick that moved something wakes the session and is not answered by the module', async ($, on) => {
  const w = world(on, { rounds: [{ lines: [line('k1', 'kick', true)], code: 0, hold: never }] })
  await begin($)
  await w.clock.settle()
  expect(w.submits).toEqual([POINTER])
  expect(answers(w)).toEqual([])
})

// A kick arrives after a turn has been running for `ran` ms.
async function kickedAfter($: any, on: On, ran: number) {
  let arrive!: () => void
  const gate = new Promise<void>(r => { arrive = r })
  const w = world(on, { rounds: [{ lines: [gate, routine('k1')], code: 0, hold: never }] })
  await begin($)
  await w.clock.settle()
  await $.turn.start({ text: 'working', turnId: 't-1' } as never)
  await w.clock.advance(ran)
  arrive()
  await w.clock.settle()
  return w
}

test('a routine kick is answered in a turn that has run less than its interval', async ($, on) => {
  expect(answers(await kickedAfter($, on, (KICK.interval - 1) * 1000)).length).toBe(1)
})

test('a routine kick is not answered in a turn that has run past its interval', async ($, on) => {
  const w = await kickedAfter($, on, (KICK.interval + 1) * 1000)
  expect(answers(w)).toEqual([])
  expect(w.logs.some(l => l.includes('goes unanswered'))).toBe(true)
})

test('answerable: a turn is hung once it has run past the interval, and an idle session never is', async () => {
  expect(answerable(true, 0, 1800_000, 1800)).toBe(true)
  expect(answerable(true, 0, 1801_000, 1800)).toBe(false)
  expect(answerable(false, 0, 99_999_000, 1800)).toBe(true)
  expect(answerable(true, undefined, 99_999_000, 1800)).toBe(true)
})

test('envelopes are acked only after the frames tool, once each, and the tool returns them verbatim', async ($, on) => {
  const ENV = '{"kind":"envelope","id":"e1","ulid":"01JENV","text":"from: the seat, session brain, on the owner\'s behalf\\nship it"}'
  const SAID = '{"kind":"say","id":"f1","from":"session peer","text":"from: session peer\\nhello"}'
  const w = world(on, {
    rounds: [{ lines: [line('e1', 'envelope', true), line('f1', 'say', true)], code: 0, hold: never }],
    recording: `data: ${ENV}\ndata: ${SAID}\n`,
  })
  const acks = () => w.runs.filter(argv => String(argv[1]).endsWith('/steering/enroll/ack.py'))
  await begin($)
  await w.clock.settle()
  await w.clock.advance(600_000)
  expect(acks()).toEqual([])
  const answered = await $.tool.call({ tool: TOOL } as never)
  expect(acks()).toEqual([['python3', `${w.root}/steering/enroll/ack.py`, 's1', '01JENV']])
  expect((answered as { result: unknown }).result).toBe(`${ENV}\n${SAID}`)
})

test('every child and client the module runs is its own version\'s, never the launcher', async ($, on) => {
  const w = world(on, {
    rounds: [{ lines: [line('e1', 'envelope', true)], code: 0 }, { lines: [], code: 0, hold: never }],
    recording: 'data: {"kind":"envelope","id":"e1","ulid":"01JENV"}\n',
  })
  await begin($)
  await w.clock.settle()
  await w.clock.advance(5_000)
  await $.tool.call({ tool: TOOL } as never)
  expect(w.root).not.toBe('')
  expect(w.spawns.length).toBe(2)
  for (const argv of w.spawns) expect(argv.slice(0, 4)).toEqual(['python3', `${w.root}/steering/enroll/hold.py`, '--wake', 'plugin'])
  expect(w.runs.length).toBeGreaterThan(0)
  for (const argv of w.runs) expect(argv[0]).toBe('python3')
  for (const argv of w.runs) expect([`${w.root}/steering/enroll/hold.py`, `${w.root}/steering/enroll/ack.py`, `${w.root}/steering/enroll/seat_section.py`, `${w.root}/steering/enroll/lease.py`, `${w.root}/steering/enroll/card.py`]).toContain(argv[1])
  // Python finds the workspace itself, STEERING_WORKSPACE included: every client runs in the session's root.
  for (const cwd of w.cwds) expect(cwd).toBe(ROOT)
})

test('below the version floor nothing registers', async ($, on) => {
  const w = world(on, { version: '2.1.242', rounds: [{ lines: [line('f1', 'say', true)], code: 0, hold: never }] })
  await begin($)
  await w.clock.settle()
  await w.clock.advance(120_000)
  expect(w.writes).toEqual([])
  expect(w.tools).toEqual([])
  expect(w.spawns).toEqual([])
})

test('the claim is written where hold.py says it lives', async ($, on) => {
  const w = world(on, { rounds: [{ lines: [], code: 0, hold: never }] })
  await begin($)
  await w.clock.settle()
  expect(w.runs).toContainEqual(['python3', `${w.root}/steering/enroll/hold.py`, '--claim-path', 'ps-1'])
  expect(w.writes.map(x => x.path)).toEqual([CLAIM_PATH])
  expect(JSON.parse(w.writes[0]?.text ?? '{}')).toEqual({ provider_session: 'ps-1', module: '0.8.99', at: 1000 })
})

test('a compaction keeps the line rebrief.py gives it, and a precompute passes through', async ($, on) => {
  const w = world(on, { rounds: [{ lines: [], code: 0, hold: never }] })
  await begin($)
  await w.clock.settle()
  await $.session.compact({ trigger: 'manual', messages: [MESSAGE], instructions: 'the plan' } as never)
  await $.session.compact({ trigger: 'precompute', messages: [MESSAGE], instructions: 'the plan' } as never)
  expect(w.runs).toContainEqual(['python3', `${w.root}/steering/enroll/rebrief.py`, '--keep', 's1'])
  expect(w.compacts[0]?.instructions).toBe('the plan\nKeep verbatim: steering session s1; branch b; checkpoint https://github.com/example-org/example-repo/pull/1.')
  expect(w.compacts[1]).toEqual({ trigger: 'precompute', instructions: 'the plan' })
})

test('a compaction with no keep line from rebrief.py still keeps the session', async ($, on) => {
  const w = world(on, { rounds: [{ lines: [], code: 0, hold: never }], keep: '' })
  await begin($)
  await w.clock.settle()
  await $.session.compact({ trigger: 'manual', messages: [MESSAGE] } as never)
  expect(w.compacts[0]?.instructions).toBe('Keep verbatim: steering session s1.')
})

test('a revoked stream asks the model to connect again, and is not held again', async ($, on) => {
  const w = world(on, { rounds: [{ lines: [line(null, 'closed', true)], code: 3 }, { lines: [], code: 0, hold: never }] })
  await begin($)
  await w.clock.settle()
  await w.clock.advance(120_000)
  expect(w.submits).toEqual([CONNECT_AGAIN])   // idle: nothing would read a row until the person typed
  expect(w.appends).toEqual([])
  expect(w.spawns.length).toBe(1)
})

test('a busy session is asked to connect again in the turn it is in', async ($, on) => {
  const w = world(on, { rounds: [{ lines: [line(null, 'closed', true)], code: 3, hold: undefined }] })
  w.enrolled = false
  await begin($)
  await w.clock.settle()
  await $.turn.start({ text: 'working', turnId: 't-1' } as never)
  w.enrolled = true
  await $.tool.call({ tool: 'Bash', command: 'python3 /p/steering/enroll/connect.py --current' } as never)
  await w.clock.settle()
  expect(w.appends).toEqual([CONNECT_AGAIN])
  expect(w.submits).toEqual([])
})

test('a refused connect-again prompt is appended instead', async ($, on) => {
  const w = world(on, { submitRejects: true, rounds: [{ lines: [], code: 3 }] })
  await begin($)
  await w.clock.settle()
  expect(w.submits).toEqual([CONNECT_AGAIN])
  expect(w.appends).toEqual([CONNECT_AGAIN])
})

test('a hold that ends badly says why in the debug log, and one that broke its recording is held again', async ($, on) => {
  const w = world(on, { enrolled: false, rounds: [
    { lines: [{ stderr: 'cannot record stream frames at /ws/x: disk full\n' }], code: 4 },
    { lines: [{ stderr: 'refused: no fresh holder claim for ps-1; the plugin\'s module starts this hold\n' }], code: 1 },
  ] })
  await begin($)
  await w.clock.settle()
  w.enrolled = true
  await $.tool.call({ tool: 'Bash', command: 'python3 /p/steering/enroll/connect.py --current' } as never)
  await w.clock.settle()
  await w.clock.advance(5_000)
  expect(w.spawns.length).toBe(2)
  expect(w.logs.some(l => /exited 4/.test(l) && l.includes('disk full'))).toBe(true)
  expect(w.logs.some(l => /exited 1/.test(l) && l.includes('no fresh holder claim'))).toBe(true)
})

test('a project with no workspace is not asked for a claim path every minute', async ($, on) => {
  const w = world(on, { enrolled: false })
  w.claimFails = true
  await begin($)
  await w.clock.settle()
  await w.clock.advance(300_000)
  expect(w.runs.filter(argv => argv.includes('--claim-path')).length).toBe(1)
  expect(w.writes).toEqual([])
})

test('exit releases the claim and holds nothing more', async ($, on) => {
  const w = world(on, { rounds: [{ lines: [], code: 0 }, { lines: [], code: 0 }, { lines: [], code: 0 }] })
  await begin($)
  await w.clock.settle()
  expect(w.spawns.length).toBe(1)
  await $.session.end({ reason: 'prompt_input_exit', sessionId: 'ps-1' } as never)
  await w.clock.advance(300_000)
  expect(w.spawns.length).toBe(1)                       // the loop was stopped, not left to respawn
  const last = w.writes[w.writes.length - 1]
  expect(last?.path).toBe(CLAIM_PATH)
  expect(JSON.parse(last?.text ?? '{}').at).toBe(0)
  expect(w.writes.length).toBe(2)                       // and the refresh no longer writes it live
})

test('/clear releases the old claim, claims the new id and forgets what was pending', async ($, on) => {
  const w = world(on, { rounds: [{ lines: [line('f1', 'say', true)], code: 0, hold: never }] })
  await begin($)
  await w.clock.settle()
  expect(w.state.get('pending')).toEqual(['f1'])
  await $.session.end({ reason: 'clear', sessionId: 'ps-1' } as never)
  w.psession = 'ps-2'
  await $.classic.SessionStart({ source: 'clear', session_id: 'ps-2' } as never)
  const ats = (path: string) => w.writes.filter(x => x.path === path).map(x => JSON.parse(x.text).at)
  expect(ats(CLAIM_PATH).slice(-1)).toEqual([0])
  expect(ats('/ws/.claude/steering-holders/ps-2.json')).toEqual([1000])
  expect(w.state.get('pending')).toEqual([])
  expect(w.state.get('read')).toEqual([])
  expect(w.state.get('pointed')).toBe(false)
  await w.clock.advance(60_000)
  expect(ats('/ws/.claude/steering-holders/ps-2.json').length).toBe(2)   // the refresh goes on for the new id
})

test('no row the module writes carries a frame\'s from: line', async ($, on) => {
  let release: () => void = () => {}
  const w = world(on, {
    rounds: [
      { lines: [line('f1', 'say', true)], code: 0, hold: new Promise<void>(r => { release = r }) },
      { lines: [line('f2', 'say', true)], code: 3 },
    ],
    recording: 'data: {"kind":"say","id":"f1","text":"from: session peer\\nhi"}\ndata: {"kind":"say","id":"f2","text":"from: session peer\\nagain"}\n',
  })
  await begin($)
  await w.clock.settle()
  const first = await $.tool.call({ tool: TOOL } as never)
  await $.turn.start({ text: 'busy', turnId: 't-1' } as never)
  release()
  await w.clock.advance(5_000)
  await $.session.measure({ context: { window: 200_000, tokens: 150_000, percent: 75 }, rateLimits: [], changed: ['context'] } as never)
  expect(String((first as { result: unknown }).result)).toContain('from: session peer')
  expect(w.submits).toEqual([POINTER])
  expect(w.appends).toEqual([POINTER, CONNECT_AGAIN, CHECKPOINT])
  for (const text of [...w.submits, ...w.appends]) expect(text).not.toMatch(/from:/)
})

test('only an invocation of the client is one, never a read of it or its help', async () => {
  expect(invokes('python3 "${CLAUDE_PLUGIN_ROOT}/steering/enroll/connect.py" --current', 'connect')).toBe(true)
  expect(invokes('cd /x && python /p/steering/enroll/connect.py --current --doing "a b"', 'connect')).toBe(true)
  expect(invokes('python3 /p/steering/enroll/disconnect.py s1', 'connect')).toBe(false)
  expect(invokes('python3 /p/steering/enroll/disconnect.py s1', 'disconnect')).toBe(true)
  expect(invokes('python3 /p/steering/enroll/connect.py --help', 'connect')).toBe(false)
  expect(invokes('python3 /p/steering/enroll/connect.py -h', 'connect')).toBe(false)
  expect(invokes('cat /p/steering/enroll/connect.py', 'connect')).toBe(false)
  expect(invokes('grep -n plugin_line /p/steering/enroll/connect.py', 'connect')).toBe(false)
  expect(invokes('python3 /p/steering/enroll/connect.py.bak', 'connect')).toBe(false)
  expect(invokes('python3 "$(dirname "$x")/steering/enroll/connect.py" --current', 'connect')).toBe(true)
  expect(invokes('python3 "$ws/my plugins/steering/enroll/connect.py" --current', 'connect')).toBe(true)
  expect(invokes('python3 "$(dirname "$x")/steering/enroll/connect.py" --help', 'connect')).toBe(false)
  expect(invokes('python3 "$(dirname "$x")/steering/enroll/disconnect.py" s1', 'connect')).toBe(false)
})

const ack = (w: ReturnType<typeof world>) => w.runs.filter(argv => String(argv[1]).endsWith('/steering/enroll/ack.py')).map(argv => argv[3])

test('a frame delivered while the tool runs is acked once, never re-added', async ($, on) => {
  let emit: () => void = () => {}
  const w = world(on, {
    rounds: [{ lines: [line('e1', 'envelope', true), new Promise<void>(r => { emit = r }), line('e2', 'envelope', true)], code: 0, hold: never }],
    recording: 'data: {"kind":"envelope","id":"e1","ulid":"U1"}\ndata: {"kind":"envelope","id":"e2","ulid":"U2"}\n',
  })
  await begin($)
  await w.clock.settle()
  let release: () => void = () => {}
  w.holdNextGet = { key: 'pending', release: new Promise<void>(r => { release = r }) }
  emit()
  await w.clock.settle()                       // the loop has read pending and waits there
  const first = $.tool.call({ tool: TOOL } as never)
  await w.clock.settle()
  release()
  await first
  await w.clock.settle()
  await $.tool.call({ tool: TOOL } as never)
  expect(ack(w).filter(u => u === 'U1')).toEqual(['U1'])
  expect(ack(w).filter(u => u === 'U2')).toEqual(['U2'])
})

test('a frame that arrives while the tool runs gets its own pointer', async ($, on) => {
  let emit: () => void = () => {}
  const w = world(on, {
    rounds: [{ lines: [line('f1', 'say', true), new Promise<void>(r => { emit = r }), line('f2', 'say', true)], code: 0, hold: never }],
    recording: 'data: {"kind":"say","id":"f1"}\n',
  })
  await begin($)
  await w.clock.settle()
  await $.turn.start({ text: 'the pointer', turnId: 't-1' } as never)   // the model calls the tool in a turn
  let open: () => void = () => {}
  w.readGate = new Promise<void>(r => { open = r })
  const reading = $.tool.call({ tool: TOOL } as never)
  await w.clock.settle()
  emit()
  await w.clock.settle()
  open()
  await reading
  await w.clock.settle()
  expect(w.submits).toEqual([POINTER])
  expect(w.appends).toEqual([POINTER])
})

// A frame arrives in a turn that is running, and the pointer is appended to it.
async function arrivesInTurn($: any, on: On, recording = '') {
  let arrive!: () => void
  const gate = new Promise<void>(r => { arrive = r })
  const w = world(on, { rounds: [{ lines: [gate, line('e1', 'envelope', true)], code: 0, hold: never }], recording })
  await begin($)
  await w.clock.settle()
  await $.turn.start({ text: 'connecting', turnId: 't-1' } as never)
  arrive()
  await w.clock.settle()
  return w
}

test('a pointer appended into a turn that ends without reading it is sent again as a prompt, once', async ($, on) => {
  const w = await arrivesInTurn($, on)
  expect(w.appends).toEqual([POINTER])
  await $.turn.complete({ turnId: 't-1', text: 'done' } as never)
  await w.clock.advance(2_000)
  expect(w.submits).toEqual([POINTER])
  await $.turn.start({ text: 'the pointer', turnId: 't-2' } as never)   // the model ignores this one too
  await $.turn.complete({ turnId: 't-2', text: 'done' } as never)
  await w.clock.advance(2_000)
  expect(w.submits).toEqual([POINTER])
})

test('a pointer is sent again only once the session is idle, and not after the session disconnected', async ($, on) => {
  const w = await arrivesInTurn($, on)
  await $.turn.complete({ turnId: 't-1', text: 'done' } as never)
  await $.turn.start({ text: 'the person typed', turnId: 't-2' } as never)   // a turn starts inside the window
  await w.clock.advance(2_000)
  expect(w.submits).toEqual([])
  expect(w.appends).toEqual([POINTER])   // nothing new is put into the turn that began
  await $.turn.complete({ turnId: 't-2', text: 'done' } as never)
  await w.clock.advance(2_000)
  expect(w.submits).toEqual([POINTER])
})

test('a pointer appended into a turn is not sent again after the session disconnected', async ($, on) => {
  const w = await arrivesInTurn($, on)
  await $.tool.call({ tool: 'Bash', command: 'python3 "/p/steering/enroll/disconnect.py" s1' } as never)
  await $.turn.complete({ turnId: 't-1', text: 'done' } as never)
  await w.clock.advance(2_000)
  expect(w.submits).toEqual([])
})

test('a turn that read its appended pointer sends nothing more', async ($, on) => {
  const w = await arrivesInTurn($, on, 'data: {"kind":"envelope","id":"e1","ulid":"01JENV"}\n')
  await $.tool.call({ tool: TOOL } as never)
  await $.turn.complete({ turnId: 't-1', text: 'done' } as never)
  await w.clock.advance(2_000)
  expect(w.submits).toEqual([])
})

test('a pointer submitted as a prompt is not sent again by the turn it started', async ($, on) => {
  const w = world(on, { rounds: [{ lines: [line('e1', 'envelope', true)], code: 0, hold: never }] })
  await begin($)
  await w.clock.settle()
  expect(w.submits).toEqual([POINTER])
  await $.turn.start({ text: POINTER, turnId: 't-1' } as never)
  await $.turn.complete({ turnId: 't-1', text: 'done' } as never)
  await w.clock.advance(2_000)
  expect(w.submits).toEqual([POINTER])
})

// A pointer sent as a prompt whose turn dies on an API error (a 429) was never read. It must not
// stand in the way of the next one, and must not be sent again into the same refusal.
test('a pointer whose turn ended on an API error does not block the next frame, and is not sent again', async ($, on) => {
  let next!: () => void
  const gate = new Promise<void>(r => { next = r })
  const w = world(on, { rounds: [{ lines: [line('e1', 'envelope', true), gate, line('f2', 'say', true)], code: 0, hold: never }] })
  await begin($)
  await w.clock.settle()
  expect(w.submits).toEqual([POINTER])
  await $.turn.start({ text: POINTER, turnId: 't-1' } as never)
  await $.turn.complete({ turnId: 't-1', text: '', reason: 'error' } as never)
  await w.clock.advance(60_000)
  expect(w.submits).toEqual([POINTER])
  next()
  await w.clock.settle()
  expect(w.submits).toEqual([POINTER, POINTER])
})

test('a pending frame the recording does not hold yet stays pending', async ($, on) => {
  const w = world(on, { rounds: [{ lines: [line('f1', 'say', true)], code: 0, hold: never }] })
  await begin($)
  await w.clock.settle()
  const early = await $.tool.call({ tool: TOOL } as never)
  w.recording = 'data: {"kind":"say","id":"f1","text":"hi"}\n'
  const later = await $.tool.call({ tool: TOOL } as never)
  expect((early as { result: unknown }).result).toBe('No steering frame is waiting.')
  expect((later as { result: unknown }).result).toBe('{"kind":"say","id":"f1","text":"hi"}')
  expect(w.submits).toEqual([POINTER])          // a frame still missing does not wake the session again
})

test('a session that disconnects is held no more, and a later refusal adds no row', async ($, on) => {
  let end: () => void = () => {}
  const w = world(on, { enrolled: false, rounds: [{ lines: [], code: 1, hold: new Promise<void>(r => { end = r }) }] })
  await begin($)
  await w.clock.settle()
  w.enrolled = true
  await $.tool.call({ tool: 'Bash', command: 'python3 "/p/steering/enroll/connect.py" --current' } as never)
  await w.clock.settle()
  expect(w.spawns.length).toBe(1)
  await $.tool.call({ tool: 'Bash', command: 'python3 "/p/steering/enroll/disconnect.py" s1' } as never)
  end()
  await w.clock.settle()
  await w.clock.advance(120_000)
  expect(w.appends).toEqual([])
  expect(w.spawns.length).toBe(1)
  expect(w.state.get('connected')).toBe(false)
  expect(w.state.get('session')).toBe('')
  expect(w.writes.map(x => JSON.parse(x.text).at).every(at => at > 0)).toBe(true)   // the claim stays live
})

test('a child that breaks is held again after a backoff, not given up', async ($, on) => {
  const w = world(on, { rounds: [{ lines: [], code: 'throw' }, { lines: [], code: 0, hold: never }] })
  await begin($)
  await w.clock.settle()
  await w.clock.advance(5_000)
  expect(w.spawns.length).toBe(2)
})

test('the checkpoint prompt goes only to a connected session', async ($, on) => {
  const w = world(on, { enrolled: false })
  await begin($)
  await w.clock.settle()
  await $.session.measure({ context: { window: 200_000, tokens: 150_000, percent: 75 }, rateLimits: [], changed: ['context'] } as never)
  expect(w.appends).toEqual([])
})

test('only an invocation of connect.py starts a hold', async ($, on) => {
  const w = world(on, { rounds: [{ lines: [], code: 0, hold: never }] })
  w.enrolled = false
  await begin($)
  await w.clock.settle()
  w.enrolled = true
  for (const command of ['cat /p/steering/enroll/connect.py', 'python3 /p/steering/enroll/connect.py --help', 'grep -n x /p/steering/enroll/connect.py'])
    await $.tool.call({ tool: 'Bash', command } as never)
  await w.clock.settle()
  expect(w.spawns.length).toBe(0)
  await $.tool.call({ tool: 'Bash', command: 'python3 "/p/steering/enroll/connect.py" --current' } as never)
  await w.clock.settle()
  expect(w.spawns.length).toBe(1)
})

test('a child that held for over a minute is followed at once, not after the grown backoff', async ($, on) => {
  const w = world(on, { rounds: [
    { lines: [], code: 0 }, { lines: [], code: 0 }, { lines: [], code: 0 },
    { lines: [], code: 0, holdMs: 61_000 }, { lines: [], code: 0, hold: never },
  ] })
  await begin($)
  await w.clock.settle()
  await w.clock.advance(1_000)
  await w.clock.advance(2_000)
  await w.clock.advance(4_000)
  expect(w.spawns.length).toBe(4)
  await w.clock.advance(61_000)
  await w.clock.advance(1_000)
  expect(w.spawns.length).toBe(5)
})

test('a pointer the session refused to take as a prompt is appended instead', async ($, on) => {
  const w = world(on, { submitRejects: true, rounds: [{ lines: [line('f1', 'say', true), line('f2', 'say', true)], code: 0, hold: never }] })
  await begin($)
  await w.clock.settle()
  expect(w.submits).toEqual([POINTER])
  expect(w.appends).toEqual([POINTER])
  expect(w.state.get('pointed')).toBe(true)
})

test('a hold refused after a connect gives the stream back to the recipe, so connecting again cannot loop', async ($, on) => {
  const w = world(on, { enrolled: false, rounds: [
    { lines: [{ stderr: 'refused 403: a Claude stream must declare event\n' }], code: 1 },
    { lines: [], code: 1 },
  ] })
  await begin($)
  await w.clock.settle()
  w.enrolled = true
  await $.tool.call({ tool: 'Bash', command: 'python3 /p/steering/enroll/connect.py --current' } as never)
  await w.clock.settle()
  expect(w.submits).toEqual([CONNECT_AGAIN])
  expect(JSON.parse(w.writes[w.writes.length - 1]?.text ?? '{}').at).toBe(0)   // connect now prints today's reply
  const written = w.writes.length
  await $.tool.call({ tool: 'Bash', command: 'python3 /p/steering/enroll/connect.py --current' } as never)
  await w.clock.settle()
  await w.clock.advance(300_000)
  expect(w.spawns.length).toBe(1)                       // the model's own hold is the holder now
  expect(w.submits).toEqual([CONNECT_AGAIN])
  expect(w.writes.length).toBe(written)                 // and the refresh does not claim it back
})

test('a /clear after the stream was handed back claims nothing for the new id', async ($, on) => {
  const w = world(on, { enrolled: false, rounds: [{ lines: [], code: 1 }] })
  await begin($)
  await w.clock.settle()
  w.enrolled = true
  await $.tool.call({ tool: 'Bash', command: 'python3 /p/steering/enroll/connect.py --current' } as never)
  await w.clock.settle()
  await $.session.end({ reason: 'clear', sessionId: 'ps-1' } as never)
  w.psession = 'ps-2'
  await $.classic.SessionStart({ source: 'clear', session_id: 'ps-2' } as never)
  await w.clock.advance(120_000)
  expect(w.writes.filter(x => x.path.endsWith('/ps-2.json'))).toEqual([])
})

// The checkpoint prompt is keyed to the engine's own auto-compact point, not to the window: an
// account that sets CLAUDE_CODE_AUTO_COMPACT_WINDOW=500000 on a 1M model compacts at 47% of the
// window, and a prompt at 85% of the window never came first (three compactions with no
// checkpoint, 2026-10-07).
const HELD = { lines: [], code: 0 as const, hold: never }
const measure = ($: any, tokens: number, window = 1_000_000) =>
  $.session.measure({ context: { window, tokens, percent: Math.round(tokens / window * 100) }, rateLimits: [], changed: ['context'] } as never)
const stepped = async ($: any, w: { stepUsage: number }, tokens: number, agentId?: string) => {
  w.stepUsage = tokens
  const input = { turnId: 't-1', index: 0, model: 'claude-test', messageCount: 3, ...(agentId ? { agentId } : {}) }
  for await (const _ of $.turn.step(input as never)) { void _ }
}

test('a session that compacts far below its window is asked to checkpoint at 85% of the way to compaction', async ($, on) => {
  const w = world(on, { rounds: [HELD], context: { point: 467_000, raw: 500_000, source: 'env', model: 'claude-opus' } })
  await begin($)
  await w.clock.settle()
  await measure($, 300_000)                  // 30% of the window, 64% of the way
  expect(w.appends).toEqual([])
  await measure($, 400_000)                  // 40% of the window, 86% of the way
  expect(w.appends).toEqual([CHECKPOINT])
})

test('the prompt goes once per window and again after a compaction', async ($, on) => {
  const w = world(on, { rounds: [HELD], context: { point: 467_000, raw: 500_000, source: 'env', model: 'claude-opus' } })
  await begin($)
  await w.clock.settle()
  await measure($, 400_000)
  await measure($, 420_000)
  expect(w.appends).toEqual([CHECKPOINT])
  await $.session.compact({ trigger: 'auto' } as never)
  await measure($, 400_000)
  expect(w.appends).toEqual([CHECKPOINT, CHECKPOINT])
})

test('with auto-compaction off the window itself is the point', async ($, on) => {
  const w = world(on, { rounds: [HELD], context: { point: undefined, raw: 200_000, source: 'auto', model: 'claude-opus' } })
  await begin($)
  await w.clock.settle()
  await measure($, 160_000, 200_000)
  expect(w.appends).toEqual([])
  await measure($, 175_000, 200_000)
  expect(w.appends).toEqual([CHECKPOINT])
})

test('a model the engine does not know is named in the log, and the engine\'s point still governs', async ($, on) => {
  const w = world(on, { rounds: [HELD], context: { point: 967_000, raw: 1_000_000, source: 'unknown-model', model: 'glm-9' } })
  await begin($)
  await w.clock.settle()
  await measure($, 100_000)
  await measure($, 120_000)
  const said = w.logs.filter(l => l.includes('glm-9'))
  expect(said.length).toBe(1)
  expect(said[0]).toContain('CLAUDE_CODE_AUTO_COMPACT_WINDOW')
  expect(w.appends).toEqual([])
})

test('one long turn that passes 85% of the way is caught at its step, not at its end', async ($, on) => {
  const w = world(on, { rounds: [HELD] })
  await begin($)
  await w.clock.settle()
  await $.turn.start({ text: 'long', turnId: 't-1' } as never)
  await stepped($, w, 100_000)
  expect(w.appends).toEqual([])
  await stepped($, w, 150_000)
  expect(w.appends).toEqual([CHECKPOINT])
  await stepped($, w, 160_000)
  expect(w.appends).toEqual([CHECKPOINT])
})

test('a subagent\'s step is not the session\'s context', async ($, on) => {
  const w = world(on, { rounds: [HELD] })
  await begin($)
  await w.clock.settle()
  await stepped($, w, 150_000, 'agent-1')
  expect(w.appends).toEqual([])
})

test('a step does not ask the engine for the breakdown once the point is known', async ($, on) => {
  const w = world(on, { rounds: [HELD] })
  await begin($)
  await w.clock.settle()
  await measure($, 10_000)
  const asked = w.usageCalls
  for (let i = 0; i < 5; i++) await stepped($, w, 20_000)
  expect(w.usageCalls).toBe(asked)
})

test('a /clear gives the fresh context its own checkpoint prompt', async ($, on) => {
  const w = world(on, { rounds: [HELD] })
  await begin($)
  await w.clock.settle()
  await measure($, 900_000)
  expect(w.appends).toEqual([CHECKPOINT])
  await $.classic.SessionStart({ source: 'clear', session_id: 'ps-2' } as never)
  await measure($, 900_000)
  expect(w.appends).toEqual([CHECKPOINT, CHECKPOINT])
})

// #4551 step 3: a seated session reads canon's rulings in its system prompt.
const FACTS = { model: 'claude-test', promptModel: 'claude-test', surfaces: [], tools: [], outputStyle: null, traits: [] }
const seatSection = async ($: any) => ((await $.prompt.compose(FACTS)).sections as { id: string; text: string; scope: string }[])
  .find(x => x.id === SEAT_SECTION)
const seatRuns = (w: { runs: string[][] }) => w.runs.filter(argv => String(argv[1]).endsWith('/seat_section.py'))

test('a session that holds the seat reads its rulings last in its system prompt, after the cache boundary', async ($, on) => {
  const w = world(on, { rounds: [{ lines: [], code: 0, hold: never }] })
  w.seat = { code: 0, stdout: '# Rulings\n- A (a)\n' }
  await begin($)
  await w.clock.settle()
  const sections = (await $.prompt.compose(FACTS)).sections
  expect(sections[sections.length - 1]).toEqual({ id: SEAT_SECTION, text: '# Rulings\n- A (a)', scope: 'session' })
  expect(seatRuns(w)[0]?.slice(2)).toEqual(['--provider', 'claude', '--provider-session', 'ps-1'])
})

test('a session that does not hold the seat, or is not connected, reads no section', async ($, on) => {
  const w = world(on, { enrolled: false })
  w.seat = { code: 0, stdout: '# Rulings\n' }
  await begin($)
  await w.clock.settle()
  expect(seatRuns(w)).toEqual([])
  expect(await seatSection($)).toBeUndefined()
})

test('renders between seat changes give the same bytes, and a kick does not swap a holder\'s text', async ($, on) => {
  let kick!: () => void
  const later = new Promise<void>(resolve => { kick = resolve })
  const w = world(on, { rounds: [{ lines: [later, line('k1', 'kick', true)], code: 0, hold: never }] })
  w.seat = { code: 0, stdout: 'A' }
  await begin($)
  await w.clock.settle()
  const first = await seatSection($)
  w.seat = { code: 0, stdout: 'B' }
  kick()
  await w.clock.settle()
  expect(seatRuns(w).length).toBe(2)
  expect(await seatSection($)).toEqual(first)
  expect(first?.text).toBe('A')
})

test('the seat taken from a session drops its section; a door that did not answer keeps it', async ($, on) => {
  let gone!: () => void, dark!: () => void
  const lost = new Promise<void>(resolve => { gone = resolve })
  const unanswered = new Promise<void>(resolve => { dark = resolve })
  const w = world(on, { rounds: [{ lines: [unanswered, line('s2', 'seat', true), lost, line('s3', 'seat', true)], code: 0, hold: never }] })
  w.seat = { code: 0, stdout: 'A' }
  await begin($)
  await w.clock.settle()
  w.seat = { code: 3, stdout: '' }
  dark()
  await w.clock.settle()
  expect((await seatSection($))?.text).toBe('A')
  w.seat = { code: 1, stdout: '' }
  gone()
  await w.clock.settle()
  expect(await seatSection($)).toBeUndefined()
})

test('taking the seat by promote.py asks at once, and a compaction may refresh the text', async ($, on) => {
  const w = world(on, { rounds: [{ lines: [], code: 0, hold: never }] })
  await begin($)
  await w.clock.settle()
  expect(await seatSection($)).toBeUndefined()
  w.seat = { code: 0, stdout: 'A' }
  await $.tool.call({ tool: 'Bash', command: 'python3 "${CLAUDE_PLUGIN_ROOT}/steering/promote.py"' } as never)
  await w.clock.settle()
  expect((await seatSection($))?.text).toBe('A')
  w.seat = { code: 0, stdout: 'B' }
  await $.session.compact({ trigger: 'manual', messages: [MESSAGE] } as never)
  await w.clock.settle()
  expect((await seatSection($))?.text).toBe('B')
})

test('a teammate rendering its lead\'s prompt carries no seat section', async ($, on) => {
  const w = world(on, { rounds: [{ lines: [], code: 0, hold: never }] })
  w.seat = { code: 0, stdout: 'A' }
  await begin($)
  await w.clock.settle()
  const sections = (await $.prompt.compose({ ...FACTS, traits: ['teammate'] })).sections as { id: string }[]
  expect(sections.some(x => x.id === SEAT_SECTION)).toBe(false)
})

test('nextSeat: 0 sets, 1 clears, anything else keeps; a holder keeps its bytes unless refreshed', async () => {
  expect(nextSeat('', 0, 'A\n', false)).toBe('A')
  expect(nextSeat('A', 0, 'B', false)).toBe('A')
  expect(nextSeat('A', 0, 'B', true)).toBe('B')
  expect(nextSeat('A', 1, '', false)).toBe('')
  expect(nextSeat('A', 3, '', true)).toBe('A')
  expect(nextSeat('A', 2, '', true)).toBe('A')
  expect(asksSeat('seat') && asksSeat('kick') && asksSeat('say')).toBe(true)
  expect(asksSeat('presence') || asksSeat('envelope') || asksSeat('usage')).toBe(false)
  expect(invokes('python3 /p/steering/promote.py', 'promote', 'steering')).toBe(true)
  expect(invokes('python3 /p/steering/enroll/promote.py', 'promote', 'steering')).toBe(false)
})

test('a kick or a say asks nothing of a session that does not hold the seat', async ($, on) => {
  let frames!: () => void
  const later = new Promise<void>(resolve => { frames = resolve })
  const w = world(on, { rounds: [{ lines: [later, line('k1', 'kick', true), line('f1', 'say', true)], code: 0, hold: never }] })
  await begin($)
  await w.clock.settle()
  const asked = seatRuns(w).length
  frames()
  await w.clock.settle()
  expect(seatRuns(w).length).toBe(asked)
})

test('handing the seat back through the door asks at once, and drops the section', async ($, on) => {
  const w = world(on, { rounds: [{ lines: [], code: 0, hold: never }] })
  w.seat = { code: 0, stdout: 'A' }
  await begin($)
  await w.clock.settle()
  w.seat = { code: 1, stdout: '' }
  await $.tool.call({ tool: 'Bash', command: `printf '%s' "$B" | python3 "/p/steering/enroll/door.py" --post /steering/brain/detach` } as never)
  await w.clock.settle()
  expect(await seatSection($)).toBeUndefined()
})

test('an answer that lands after disconnect is dropped', async ($, on) => {
  const w = world(on, { rounds: [{ lines: [], code: 0, hold: never }] })
  await begin($)
  await w.clock.settle()
  let answer!: () => void
  w.seatGate = new Promise<void>(resolve => { answer = resolve })
  w.seat = { code: 0, stdout: 'A' }
  await $.tool.call({ tool: 'Bash', command: 'python3 /p/steering/promote.py' } as never)
  await $.tool.call({ tool: 'Bash', command: 'python3 /p/steering/enroll/disconnect.py s1' } as never)
  answer()
  await w.clock.settle()
  expect(await seatSection($)).toBeUndefined()
})

// #4551 step 2: the seat's lease verbs are typed tools over lease.py and card.py. The module never
// holds the token: what it hands a client carries `@lease` where the token goes.
const leaseRuns = (w: { runs: string[][] }) => w.runs.filter(argv => /\/(lease|card)\.py$/.test(String(argv[1])))

test('a relay goes to lease.py on stdin with @lease in the token slot, and returns its words', async ($, on) => {
  const w = world(on, { rounds: [{ lines: [], code: 0, hold: never }] })
  await begin($)
  await w.clock.settle()
  const r = await $.tool.call({ tool: 'mcp__2mw2lt__relay', to: 'amber', text: 'take #12' } as never)
  expect((r as { result: unknown }).result).toBe('relayed: 01AB to amber')
  expect(leaseRuns(w)[0]?.slice(1)).toEqual([`${w.root}/steering/enroll/lease.py`, '--provider', 'claude', '--provider-session', 'ps-1', 'say'])
  expect(w.stdins).toEqual(['relay: token @lease to amber take #12'])
})

test('a card write goes to card.py --lease as argv, and the reply to the brain reply route', async ($, on) => {
  const w = world(on, { rounds: [{ lines: [], code: 0, hold: never }] })
  await begin($)
  await w.clock.settle()
  await $.tool.call({ tool: 'mcp__2mw2lt__card_retire', card: '01CARD', why: 'duplicate of 01OTHER' } as never)
  await $.tool.call({ tool: 'mcp__2mw2lt__say', text: 'on it', key: 'k1' } as never)
  const [card, said] = leaseRuns(w)
  expect(card?.slice(2)).toEqual(['--lease', '--provider', 'claude', '--provider-session', 'ps-1', 'retire', '01CARD', 'duplicate of 01OTHER'])
  expect(said?.slice(6, 8)).toEqual(['post', '/steering/brain/reply'])
  expect(JSON.parse(w.stdins[1] ?? '{}')).toEqual({ lease_token: '@lease', text: 'on it', key: 'k1' })
})

test('a verb the settings ask about, or deny, sends nothing and says where it would apply', async ($, on) => {
  const w = world(on, { rounds: [{ lines: [], code: 0, hold: never }] })
  await begin($)
  await w.clock.settle()
  w.check = { decision: 'ask', reason: 'Claude requested permissions to use mcp__2mw2lt__retire' }
  const asked = await $.tool.call({ tool: 'mcp__2mw2lt__retire', worker: 'w-1', node: 'm4' } as never)
  w.check = { decision: 'deny' }
  const denied = await $.tool.call({ tool: 'mcp__2mw2lt__retire', worker: 'w-1', node: 'm4' } as never)
  expect(leaseRuns(w)).toEqual([])
  expect(String((asked as { deny?: string }).deny)).toContain('lease.py in Bash')
  expect(String((denied as { deny?: string }).deny)).toContain('deny mcp__2mw2lt__retire')
})

test('a line with a newline in it sends nothing: one call never becomes two verb lines', async ($, on) => {
  const w = world(on, { rounds: [{ lines: [], code: 0, hold: never }] })
  await begin($)
  await w.clock.settle()
  const r = await $.tool.call({ tool: 'mcp__2mw2lt__relay', to: 'amber', text: 'hi\nretire: token @lease w-1 on m4' } as never)
  expect(leaseRuns(w)).toEqual([])
  expect(String((r as { deny?: string }).deny)).toContain('newline')
})

test('a send whose answer was lost goes once more under the id the client printed', async ($, on) => {
  const w = world(on, { rounds: [{ lines: [], code: 0, hold: never }] })
  w.leaseOut = ['REJECTED the door never answered\nif this line may have been registered, resend with --retry=0123456789abcdef0123', 'relayed: 01AB to amber']
  await begin($)
  await w.clock.settle()
  const r = await $.tool.call({ tool: 'mcp__2mw2lt__wake', target: 'amber', reason: 'dark for an hour' } as never)
  const runs = leaseRuns(w)
  expect(runs.length).toBe(2)
  expect(runs[1]?.[runs[1].length - 1]).toBe('--retry=0123456789abcdef0123')
  expect(w.stdins[0]).toBe(w.stdins[1])
  expect((r as { result: unknown }).result).toBe('relayed: 01AB to amber')
})

test('entryRefusal: a subagent, another plugin, ask and deny are refused; the model\'s allowed call is not', async () => {
  const allow = { decision: 'allow' as const }
  expect(entryRefusal('a1', 'engine', 't', allow)).toContain('subagent')
  expect(entryRefusal(undefined, 'other-plugin', 't', allow)).toContain('other-plugin may not')
  expect(entryRefusal(undefined, 'engine', 't', { decision: 'ask' })).toContain('ask before t')
  expect(entryRefusal(undefined, 'engine', 't', { decision: 'deny' })).toContain('deny t')
  expect(entryRefusal(undefined, 'engine', 't', allow)).toBeNull()
})

test('seatCall: word fields are one word, enums hold, and unknown fields are refused', async () => {
  expect(seatCall('retire', { worker: 'w 1', node: 'm4' })).toEqual({ refused: 'worker is one word' })
  expect(seatCall('card_session', { card: 'c', session: 's', role: 'boss' })).toEqual({ refused: 'role is one of executor, planned' })
  expect(seatCall('dispose', { item: 'i', reason: 'r', token: 'x' })).toEqual({ refused: 'dispose takes no token' })
  expect(seatCall('relay', { to: 'amber', epoch: 3, text: 'go' })).toEqual({ client: 'say', stdin: 'relay: token @lease to amber@3 go' })
  expect(seatCall('card_scope', { name: 'The work', track: 'rt', anchors: ['resolves:12'], major: true })).toEqual(
    { client: 'card', argv: ['scope', 'The work', 'rt', 'resolves:12', 'major'] })
  expect(resendId('done')).toBeNull()
})

test('a client that refused is the call\'s error, in its words, never a result', async ($, on) => {
  const w = world(on, { rounds: [{ lines: [], code: 0, hold: never }] })
  w.leaseOut = ['refused: this session no longer holds the seat (refused: this token does not hold the lease); its stored lease is removed.']
  await begin($)
  await w.clock.settle()
  const r = await $.tool.call({ tool: 'mcp__2mw2lt__dispose', item: 'i1', reason: 'settled' } as never)
  expect(String((r as { deny?: string }).deny)).toContain('no longer holds the seat')
})

test('handing the seat on through lease.py asks at once, and drops the section', async ($, on) => {
  const w = world(on, { rounds: [{ lines: [], code: 0, hold: never }] })
  w.seat = { code: 0, stdout: 'A' }
  await begin($)
  await w.clock.settle()
  w.seat = { code: 1, stdout: '' }
  await $.tool.call({ tool: 'Bash', command: `python3 "/p/steering/enroll/lease.py" post /steering/brain/attach <<'JSON'\n{"lease_token":"@lease","session":"amber"}\nJSON` } as never)
  await w.clock.settle()
  expect(await seatSection($)).toBeUndefined()
})
