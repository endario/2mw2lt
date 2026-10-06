import type { On } from 'claude-code'
import { test, expect, mock } from 'claude-code/testing'
import { lines, atLeast, pick, invokes, POINTER, CHECKPOINT, CONNECT_AGAIN } from '../frames'

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
                         enrolled = true, submitRejects = false } = {}) {
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
    const out = e.argv.includes('--session-of') ? (w.enrolled ? 's1\n' : '')
      : e.argv.includes('--claim-path') ? (w.claimFails ? '' : `/ws/.claude/steering-holders/${e.argv[3]}.json\n`)
      : e.argv.includes('--frame-path') ? `${FRAME_PATH}\n`
      : e.argv.includes('--keep') ? keep : ''
    const exitCode = e.argv.includes('--claim-path') && w.claimFails ? 1 : 0
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
  on('session.end', async ($, e) => ({ sessionId: e.sessionId }))
  on('classic.SessionStart', async () => ({}) as never)
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
  for (const argv of w.runs) expect([`${w.root}/steering/enroll/hold.py`, `${w.root}/steering/enroll/ack.py`]).toContain(argv[1])
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
  await $.session.measure({ context: { window: 200_000, percent: 90 }, rateLimits: [], changed: ['context'] } as never)
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
  await $.session.measure({ context: { window: 200_000, percent: 90 }, rateLimits: [], changed: ['context'] } as never)
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
