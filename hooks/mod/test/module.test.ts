import type { On } from 'claude-code'
import { register } from '../register'
import { test, expect } from 'claude-code/testing'
import { lines, atLeast, pick, invokes, lifecycle, nextSeat, asksSeat, seatCall, entryRefusal, framesEntry, obsoleteSeatRefusal, resendId, capBulletin, sectionRevision, POINTER, CHECKPOINT, SEAT_SECTION, BULLETIN_SECTION } from '../frames'
import { world, line, begin, never, MESSAGE, TOOL, FRAME_PATH, CLAIM_PATH, ROOT, AGENT } from './world'
import type { HoldPoll } from './world'

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
  const { text, envelopes, says } = pick(RECORDING, ['s9', 'e1', 'f1'])
  expect(text).toBe([SAY, ENVELOPE, SEAT].join('\n'))
  expect(envelopes).toEqual(['01JENV'])
  expect(says).toEqual(['f1'])                       // a say without a directive settles under its frame id
  expect(pick(RECORDING, ['s9', 'e1', 'gone']).found).toEqual(['e1', 's9'])
})

test('a frame of any kind is picked by its id, and only an envelope is acked', async () => {
  const { text, envelopes } = pick(RECORDING, ['s9'])
  expect(text).toBe(SEAT)
  expect(envelopes).toEqual([])
  expect(pick(RECORDING, ['01JENV']).text).toBe('')
  expect(pick(RECORDING, ['nothing-recorded']).text).toBe('')
})

test('pick records only returned frames as foreground semantic identities', async () => {
  const request = 'e20adafc-2cda-48c9-bae2-7bae3d4bfcbd'
  const directive = 'edaed9f7-42bd-479a-baa9-ddc2d4fdffba'
  const ordinary = 'bb48a465-45e5-4e38-85c8-2223890d4f47'
  const envelope = '86340b22-765e-4b5c-93b2-ddc8653052b2'
  const raw = [
    JSON.stringify({ kind: 'say', id: request, directive, text: 'answer me' }),
    JSON.stringify({ kind: 'say', id: ordinary, text: 'ordinary' }),
    JSON.stringify({ kind: 'envelope', id: 'envelope-frame', ulid: envelope }),
    JSON.stringify({ kind: 'seat', id: 'seat:4', generation: 4 }),
    JSON.stringify({ kind: 'unknown', id: 'unknown-frame' }),
  ].map(frame => `data: ${frame}`).join('\n')
  const got = pick(raw, [request, ordinary, 'envelope-frame', 'seat:4', 'unknown-frame'])
  expect(got.read).toEqual({ directives: [directive, envelope], seatGeneration: 4, seatFrameId: 'seat:4' })
  expect(got.says).toEqual([ordinary])   // a plain say alone settles under its frame id, by the tool's receipt (#7136)
  const relay = pick(`data: ${JSON.stringify({kind:'say', id:request, directive, text:'answer me'})}\n`, [request])
  expect(relay.says).toEqual([])                     // a request relay stays the seat read's, never --read-say's
  expect(relay.read.directives).toEqual([directive])
  expect(pick(raw, []).read).toEqual({ directives: [] })
})

test('pick collects pending bulletin frames\' texts in recording order, separately from the rest', async () => {
  const raw = [
    JSON.stringify({ kind: 'bulletin', id: 'b1', revision: 8, text: 'Bulletin r8: one' }),
    JSON.stringify({ kind: 'say', id: 'f1', text: 'hi' }),
    JSON.stringify({ kind: 'bulletin', id: 'b2', revision: 9, text: '+ k: v' }),
    JSON.stringify({ kind: 'bulletin', id: 'gone', no: 'text' }),
  ].map(frame => `data: ${frame}`).join('\n') + '\n'
  const got = pick(raw, ['b2', 'b1'])
  expect(got.bulletins).toEqual(['Bulletin r8: one', '+ k: v'])
  expect(got.found).toEqual(['b1', 'b2'])
  expect(pick(raw, ['f1']).bulletins).toEqual([])
})

test('one turn\'s bulletin text fits whole lines under its byte cap, counting what is left out', async () => {
  expect(capBulletin(['A\nB', 'C'])).toBe('A\nB\nC')
  expect(capBulletin([])).toBe('')
  const header = 'Bulletin r9: what changed since what you were shown. Information, not direction.'
  const rows = Array.from({ length: 40 }, (_, i) => `+ key-${i}: ${'é'.repeat(30)} (session s, until 14 Oct 18:00Z)`)
  const out = capBulletin([header, rows.join('\n')])
  const bytes = new TextEncoder().encode(out).length
  const overflow = /\+(\d+) more: bulletin\.py read$/.exec(out)
  expect(bytes).toBeLessThanOrEqual(1024)
  expect(overflow).not.toBeNull()
  const leftOut = Number(overflow![1])
  expect(out.split('\n').slice(0, -1)).toEqual([header, ...rows.slice(0, rows.length - leftOut)])
  // Every kept row is whole: none of the left-out rows appears, and every kept one does.
  for (const row of rows.slice(0, rows.length - leftOut)) expect(out).toContain(row)
  for (const row of rows.slice(rows.length - leftOut)) expect(out).not.toContain(row)
  expect(capBulletin([`${'x'.repeat(1100)}`])).toBe('+1 more: bulletin.py read')
})

test('a section names the head it was rendered at', async () => {
  expect(sectionRevision('Bulletin r42: what this workspace has posted as true now. Information, not direction.')).toBe(42)
  expect(sectionRevision('')).toBeUndefined()
  expect(sectionRevision('not a bulletin')).toBeUndefined()
})

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

test('an envelope whose ack failed is read, and its replay submits no pointer', async ($, on) => {
  const { w, read } = await replays($, on, 'envelope', { ackFails: true })
  expect(read).toContain('{"kind":"envelope","id":"f1","ulid":"U1"}')
  expect(w.submits).toEqual([POINTER])
  expect(w.state.get('pending')).toEqual([])
  expect(w.state.get('unsettled')).toEqual([{ ulid: 'U1', tries: 1 }])
})

// A receipt that does not arrive withholds nothing: the session reads the frame, and the receipt
// goes again on the next call, where a refused call left it reading nothing while the daemon was
// slow to take receipts (#7396).
test('a failed envelope acknowledgement still returns the envelope, and is sent again', async ($, on) => {
  const w = world(on, {
    ackFails: true,
    rounds: [{ lines: [line('e1', 'envelope', true)], code: 0, hold: never }],
    recording: 'data: {"kind":"envelope","id":"e1","ulid":"01JENV"}\n',
  })
  await begin($)
  await w.clock.settle()
  const got = await $.tool.call({ tool: TOOL } as never)
  expect((got as { result?: string }).result).toContain('{"kind":"envelope","id":"e1","ulid":"01JENV"}')
  expect((got as { result?: string }).result).toContain('ack.py 01JENV exited 1')
  expect(w.state.get('pending')).toEqual([])
  expect(w.stdins).toEqual([JSON.stringify({ directives: ['01JENV'] })])
  w.ackFails = false
  const again = await $.tool.call({ tool: TOOL } as never)
  expect((again as { result?: string }).result).toBe('Nothing is waiting.')
  expect(w.runs.filter(argv => String(argv[1]).endsWith('/ack.py'))).toEqual([
    ['python3', `${w.root}/steering/enroll/ack.py`, 's1', '01JENV'], ['python3', `${w.root}/steering/enroll/ack.py`, 's1', '01JENV']])
  expect(w.state.get('unsettled')).toEqual([])
})

test('a receipt that never arrives is let go after its tries', async ($, on) => {
  const w = world(on, {
    ackFails: true,
    rounds: [{ lines: [line('e1', 'envelope', true)], code: 0, hold: never }],
    recording: 'data: {"kind":"envelope","id":"e1","ulid":"01JENV"}\n',
  })
  await begin($)
  await w.clock.settle()
  for (let i = 0; i < 4; i++) await $.tool.call({ tool: TOOL } as never)
  expect(w.runs.filter(argv => String(argv[1]).endsWith('/ack.py')).length).toBe(3)
  expect(w.state.get('unsettled')).toEqual([])
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

test('a kick wakes the session', async ($, on) => {
  const w = world(on, { rounds: [{ lines: [line('k1', 'kick', true)], code: 0, hold: never }] })
  await begin($)
  await w.clock.settle()
  expect(w.submits).toEqual([POINTER])
})

test('envelopes are acked only after the frames tool, once each, and the tool returns them verbatim', async ($, on) => {
  const ENV = '{"kind":"envelope","id":"e1","ulid":"01JENV","text":"from: the seat, session brain, on the owner\'s behalf\\nship it"}'
  const SAID = '{"kind":"say","id":"f1","from":"session peer","text":"from: session peer\\nhello"}'
  const w = world(on, {
    rounds: [{ lines: [line('e1', 'envelope', true), line('f1', 'say', true)], code: 0, hold: never }],
    recording: `data: ${ENV}\ndata: ${SAID}\n`,
    // The world carries its agent (#8530): without one the surface poll retries from the 250ms
    // backoff to its 5s cap, and each retry pays real kit IPC — the .env read that ENOENTs and
    // three env.gets — so the 600s advance below fires it ~120 times, ~500 round-trips whose wall
    // clock under a loaded machine took this test past the kit's 5000ms. With the agent the one
    // poll holds, as it does against a real one, and the advance pays only the claim refresh's
    // own 60s cadence. A test that means to advance minutes of a world with no agent pays the
    // same per-retry round-trips and wants its own answer to this.
    agent: true,
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

test('the main frames tool proves exactly its returned native frames', async ($, on) => {
  const request = 'e20adafc-2cda-48c9-bae2-7bae3d4bfcbd'
  const directive = 'edaed9f7-42bd-479a-baa9-ddc2d4fdffba'
  const ordinary = 'bb48a465-45e5-4e38-85c8-2223890d4f47'
  const w = world(on, {
    rounds: [{ lines: [line(request, 'say', true), line(ordinary, 'say', true), line('seat:4', 'seat', true)], code: 0, hold: never }],
    recording: [
      { kind: 'say', id: request, directive, text: 'answer me' },
      { kind: 'say', id: ordinary, text: 'ordinary' },
      { kind: 'seat', id: 'seat:4', generation: 4 },
    ].map(frame => `data: ${JSON.stringify(frame)}`).join('\n') + '\n',
  })
  await begin($)
  await w.clock.settle()
  await $.tool.call({ tool: TOOL } as never)
  expect(w.stdins).toContain(JSON.stringify({ directives: [directive], seat_generation: 4 }))
  expect(w.runs).toContainEqual(['python3', `${w.root}/steering/enroll/hold.py`, '--read-say', 's1', ordinary])
})

// A session resumed from inside another session's environment inherits its CLAUDE_CODE_SESSION_ID:
// the engine sets the session's own for the Bash tool, not for a module's children (#7069).
test('a session resumed inside another session\'s environment reads its own frames', async ($, on) => {
  const SAID = { kind: 'say', id: 'bb48a465-45e5-4e38-85c8-2223890d4f47', directive: 'edaed9f7-42bd-479a-baa9-ddc2d4fdffba', text: 'answer me' }
  const w = world(on, {
    rounds: [{ lines: [line(SAID.id, 'say', true)], code: 0, hold: never }],
    recording: `data: ${JSON.stringify(SAID)}\n`,
  })
  w.inherited = 'ps-seat'
  await begin($)
  await w.clock.settle()
  const answered = await $.tool.call({ tool: TOOL } as never)
  expect((answered as { result?: unknown }).result).toBe(JSON.stringify(SAID))
  expect(w.stdins).toContain(JSON.stringify({ directives: [SAID.directive] }))
  expect(w.envs.length).toBeGreaterThan(0)
  expect(w.envs.filter(env => env?.CLAUDE_CODE_SESSION_ID !== w.psession)).toEqual([])
})

test('a stale seat frame is retired without discarding the ordinary frame beside it', async ($, on) => {
  const ordinary = 'bb48a465-45e5-4e38-85c8-2223890d4f47'
  const w = world(on, {
    rounds: [{ lines: [line('seat:4', 'seat', true), line(ordinary, 'say', true)], code: 0, hold: never }],
    recording: [
      { kind: 'seat', id: 'seat:4', generation: 4 },
      { kind: 'say', id: ordinary, text: 'ordinary' },
    ].map(frame => `data: ${JSON.stringify(frame)}`).join('\n') + '\n',
  })
  w.leaseOut.push('refused: this session does not hold the seat (refused (409): seat-generation-stale)')
  await begin($)
  await w.clock.settle()
  const first = await $.tool.call({ tool: TOOL } as never)
  expect((first as { deny?: string }).deny).toContain('seat-generation-stale')
  expect(w.state.get('pending')).toEqual([ordinary])
  expect(w.state.get('read')).toEqual(['seat:4'])
  expect(w.submits).toEqual([POINTER])
  expect(w.appends).toEqual([POINTER])
  const second = await $.tool.call({ tool: TOOL } as never)
  expect((second as { result?: string }).result).toBe(JSON.stringify({ kind: 'say', id: ordinary, text: 'ordinary' }))
  expect(w.stdins).toEqual([JSON.stringify({ directives: [], seat_generation: 4 })])
  expect(w.runs).toContainEqual(['python3', `${w.root}/steering/enroll/hold.py`, '--read-say', 's1', ordinary])
  expect(w.state.get('pending')).toEqual([])
})

test('a lapsed seat frame is retired without claiming a replacement seat', async ($, on) => {
  const ordinary = 'bb48a465-45e5-4e38-85c8-2223890d4f47'
  const w = world(on, {
    rounds: [{ lines: [line('seat:4', 'seat', true), line(ordinary, 'say', true)], code: 0, hold: never }],
    recording: [
      { kind: 'seat', id: 'seat:4', generation: 4 },
      { kind: 'say', id: ordinary, text: 'ordinary' },
    ].map(frame => `data: ${JSON.stringify(frame)}`).join('\n') + '\n',
  })
  w.leaseOut.push('refused: refused (409): seat-lapsed')
  await begin($)
  await w.clock.settle()
  const first = await $.tool.call({ tool: TOOL } as never)
  expect((first as { deny?: string }).deny).toContain('seat-lapsed')
  expect(w.state.get('pending')).toEqual([ordinary])
  const second = await $.tool.call({ tool: TOOL } as never)
  expect((second as { result?: string }).result).toBe(JSON.stringify({ kind: 'say', id: ordinary, text: 'ordinary' }))
  expect(w.stdins).toEqual([JSON.stringify({ directives: [], seat_generation: 4 })])
  expect(w.runs).toContainEqual(['python3', `${w.root}/steering/enroll/hold.py`, '--read-say', 's1', ordinary])
})

test('a failed foreground proof keeps returned frames pending', async ($, on) => {
  const id = 'bb48a465-45e5-4e38-85c8-2223890d4f47'
  const directive = 'edaed9f7-42bd-479a-baa9-ddc2d4fdffba'
  const w = world(on, {
    rounds: [{ lines: [line(id, 'say', true)], code: 0, hold: never }],
    recording: `data: ${JSON.stringify({ kind: 'say', id, directive, text: 'answer me' })}\n`,
  })
  w.leaseOut.push('refused: seat-generation-stale')
  await begin($)
  await w.clock.settle()
  const got = await $.tool.call({ tool: TOOL } as never)
  expect((got as { deny?: string }).deny).toContain('seat-generation-stale')
  expect(w.state.get('pending')).toEqual([id])
})

// A plain say settles by the tool's own receipt under its frame id, not the seat read (#7136):
// a refused receipt still returns the say, and the receipt alone goes again.
test('a refused say receipt still returns the say, and is sent again', async ($, on) => {
  const id = 'bb48a465-45e5-4e38-85c8-2223890d4f47'
  const w = world(on, {
    readFails: true,
    rounds: [{ lines: [line(id, 'say', true)], code: 0, hold: never }],
    recording: `data: ${JSON.stringify({ kind: 'say', id, text: 'ordinary' })}\n`,
  })
  await begin($)
  await w.clock.settle()
  const got = await $.tool.call({ tool: TOOL } as never)
  expect((got as { result?: string }).result).toContain('ordinary')
  expect((got as { result?: string }).result).toContain(`hold.py --read-say ${id} exited 1`)
  expect(w.state.get('pending')).toEqual([])
  w.readFails = false
  const again = await $.tool.call({ tool: TOOL } as never)
  expect((again as { result?: string }).result).toBe('Nothing is waiting.')
  expect(w.runs.filter(argv => argv.includes('--read-say'))).toEqual([
    ['python3', `${w.root}/steering/enroll/hold.py`, '--read-say', 's1', id], ['python3', `${w.root}/steering/enroll/hold.py`, '--read-say', 's1', id]])
  expect(w.state.get('unsettled')).toEqual([])
})

async function refusalLeavesRoomForTheNextPointer($: any, on: On, kind: 'say' | 'envelope') {
  const id = kind === 'say' ? 'bb48a465-45e5-4e38-85c8-2223890d4f47' : 'e1'
  const next = 'cc48a465-45e5-4e38-85c8-2223890d4f47'
  let arrive!: () => void
  const w = world(on, {
    ackFails: kind === 'envelope',
    rounds: [{ lines: [line(id, kind, true), new Promise<void>(resolve => { arrive = resolve }), line(next, 'say', true)], code: 0, hold: never }],
    recording: kind === 'envelope'
      ? `data: ${JSON.stringify({ kind, id, ulid: '86340b22-765e-4b5c-93b2-ddc8653052b2' })}\n`
      : `data: ${JSON.stringify({ kind: 'say', id, directive: 'edaed9f7-42bd-479a-baa9-ddc2d4fdffba', text: 'answer me' })}\n`,
  })
  if (kind === 'say') w.leaseOut.push('refused: seat-generation-stale')
  await begin($)
  await w.clock.settle()
  const result = await $.tool.call({ tool: TOOL } as never)
  arrive()
  await w.clock.settle()
  return { w, result }
}

test('a proof refusal clears its outstanding pointer for a new arrival', async ($, on) => {
  const { w, result } = await refusalLeavesRoomForTheNextPointer($, on, 'say')
  expect((result as { deny?: string }).deny).toContain('foreground frame read')
  expect(w.state.get('pending')).toEqual(['bb48a465-45e5-4e38-85c8-2223890d4f47', 'cc48a465-45e5-4e38-85c8-2223890d4f47'])
  expect(w.submits).toEqual([POINTER, POINTER])
})

test('a failed acknowledgement clears its outstanding pointer for a new arrival', async ($, on) => {
  const { w, result } = await refusalLeavesRoomForTheNextPointer($, on, 'envelope')
  expect((result as { result?: string }).result).toContain('was not reported')
  expect(w.state.get('pending')).toEqual(['cc48a465-45e5-4e38-85c8-2223890d4f47'])
  expect(w.submits).toEqual([POINTER, POINTER])
})

async function arrivalDuringRefusal($: any, on: On, kind: 'say' | 'envelope') {
  const id = kind === 'say' ? 'bb48a465-45e5-4e38-85c8-2223890d4f47' : 'e1'
  const next = 'cc48a465-45e5-4e38-85c8-2223890d4f47'
  let arrive!: () => void
  let release!: () => void
  let started!: () => void
  const clientStarted = new Promise<void>(resolve => { started = resolve })
  const w = world(on, {
    ackFails: kind === 'envelope',
    rounds: [{ lines: [line(id, kind, true), new Promise<void>(resolve => { arrive = resolve }), line(next, 'say', true)], code: 0, hold: never }],
    recording: kind === 'envelope'
      ? `data: ${JSON.stringify({ kind, id, ulid: '86340b22-765e-4b5c-93b2-ddc8653052b2' })}\n`
      : `data: ${JSON.stringify({ kind: 'say', id, directive: 'edaed9f7-42bd-479a-baa9-ddc2d4fdffba', text: 'answer me' })}\n`,
  })
  if (kind === 'say') w.leaseOut.push('refused: unavailable')
  w.wireGate = new Promise<void>(resolve => { release = resolve })
  w.wireStarted = started
  await begin($)
  await w.clock.settle()
  const reading = $.tool.call({ tool: TOOL } as never)
  await clientStarted
  arrive()
  await w.clock.settle()
  release()
  const result = await reading
  await w.clock.settle()
  return { w, result, id, next }
}

test('a proof refusal re-wakes for an arrival during its client call', async ($, on) => {
  const { w, result, id, next } = await arrivalDuringRefusal($, on, 'say')
  expect((result as { deny?: string }).deny).toContain('foreground frame read')
  expect(w.state.get('pending')).toEqual([id, next])
  expect(w.submits).toEqual([POINTER])
  expect(w.appends).toEqual([POINTER])
})

test('a failed acknowledgement re-wakes for an arrival during its client call', async ($, on) => {
  const { w, result, next } = await arrivalDuringRefusal($, on, 'envelope')
  expect((result as { result?: string }).result).toContain('was not reported')
  expect(w.state.get('pending')).toEqual([next])
  expect(w.submits).toEqual([POINTER])
  expect(w.appends).toEqual([POINTER])
})

test('a failed proof is never preceded by an envelope acknowledgement', async ($, on) => {
  const w = world(on, {
    rounds: [{ lines: [line('e1', 'envelope', true)], code: 0, hold: never }],
    recording: `data: ${JSON.stringify({ kind: 'envelope', id: 'e1', ulid: '86340b22-765e-4b5c-93b2-ddc8653052b2' })}\n`,
  })
  w.leaseOut.push('refused: seat-generation-stale')
  await begin($)
  await w.clock.settle()
  const result = await $.tool.call({ tool: TOOL } as never)
  expect((result as { deny?: string }).deny).toContain('foreground frame read')
  expect(w.runs.filter(argv => String(argv[1]).endsWith('/ack.py'))).toEqual([])
})

test('a subagent frames call does not consume the main loop\'s pending frames', async ($, on) => {
  const id = 'bb48a465-45e5-4e38-85c8-2223890d4f47'
  const w = world(on, {
    rounds: [{ lines: [line(id, 'say', true)], code: 0, hold: never }],
    recording: `data: ${JSON.stringify({ kind: 'say', id, text: 'ordinary' })}\n`,
  })
  await begin($)
  await w.clock.settle()
  const got = await $.tool.call({ tool: TOOL, agentId: 'subagent-1' } as never)
  expect((got as { result?: string }).result).toBe('Nothing is waiting.')
  expect(w.state.get('pending')).toEqual([id])
  expect(w.runs.filter(argv => argv.some(arg => String(arg).endsWith('/ack.py') || String(arg).endsWith('/lease.py')))).toEqual([])
})

test('the actual frames tool hook leaves pending frames untouched for another plugin', async ($, on) => {
  const id = 'bb48a465-45e5-4e38-85c8-2223890d4f47'
  const w = world(on)
  const recording = `data: ${JSON.stringify({ kind: 'say', id, text: 'ordinary' })}\n`
  w.state.set('session', 's1')
  w.state.set('pending', [id])
  let frames: ((...args: any[]) => Promise<unknown>) | undefined
  register(((event: string, matcher: { tool?: string }, handler: unknown) => {
    if (event === 'tool.call' && matcher.tool === TOOL) frames = handler as typeof frames
    return { catch: () => undefined }
  }) as never)
  const engine = {
    plugin: { root: '/plugin' },
    session: { id: async () => 'ps-1' },
    state: {
      get: async (ref: { key: string }) => ({ value: w.state.get(ref.key) }),
      set: async (ref: { key: string }, value: unknown) => { w.state.set(ref.key, value) },
    },
    fs: { read: async () => recording },
    process: {
      run: async (argv: string[]) => {
        w.runs.push([...argv])
        return { exitCode: 0, stdout: argv.includes('--frame-path') ? `${FRAME_PATH}\n` : 'read: frames', stderr: '' }
      },
    },
    ui: { log: () => undefined },
  }
  const got = await frames!(engine, { tool: TOOL }, { origin: { plugin: 'other-plugin' } }) as { result?: string }
  expect(got.result).toBe('Nothing is waiting.')
  expect(w.state.get('pending')).toEqual([id])
  expect(w.runs).toEqual([])
})

test('only the engine main loop may consume the frames tool', async () => {
  expect(framesEntry(undefined, 'engine')).toBe(true)
  expect(framesEntry('subagent-1', 'engine')).toBe(false)
  expect(framesEntry(undefined, 'other-plugin')).toBe(false)
})

test('only explicit stale or lapsed seat refusals retire a seat notice', async () => {
  expect(obsoleteSeatRefusal('refused: this session does not hold the seat (refused (409): seat-generation-stale)')).toBe(true)
  expect(obsoleteSeatRefusal('refused: refused (409): seat-lapsed [generation] request abc')).toBe(true)
  expect(obsoleteSeatRefusal('refused: unavailable: seat-lapsed')).toBe(false)
  expect(obsoleteSeatRefusal('refused: this session does not hold the seat (seat-lapsed)')).toBe(false)
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
  for (const argv of w.runs) expect([`${w.root}/steering/enroll/hold.py`, `${w.root}/steering/enroll/ack.py`, `${w.root}/steering/enroll/seat_section.py`, `${w.root}/steering/enroll/bulletin.py`, `${w.root}/steering/enroll/lease.py`, `${w.root}/steering/enroll/card.py`]).toContain(argv[1])
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

// The plugin holds every session's stream, the seat's included; a session is never asked to arm or
// reconnect one (owner ruling, 2026-10-11, #5641). Today a session told its stream was down armed
// its own hold, which revoked the plugin's, and the plugin then asked it to connect again.
test('a revoked stream is held again by the plugin, and the session is asked nothing', async ($, on) => {
  const w = world(on, { rounds: [{ lines: [line(null, 'closed', true)], code: 3 }, { lines: [], code: 0, hold: never }] })
  await begin($)
  await w.clock.settle()
  await w.clock.advance(120_000)
  expect(w.spawns.length).toBe(2)
  expect(w.submits).toEqual([])
  expect(w.appends).toEqual([])
})

// Measured 2026-10-13 (#6308): a retired session's hold was refused for good, the module stopped
// it and said nothing, and the session went deaf — it could neither reconnect nor exit, and the
// owner found out only by typing into it. A refusal no retry outlives now starts one turn that
// says the refusal, the remedy, and the retire that may stand behind it.
test('a hold refused for good starts one turn saying the refusal, the remedy and the exit path', async ($, on) => {
  const refused = JSON.stringify({ id: null, kind: 'refused', wakes: false,
    why: 'refused 401: session-token-locator-read.', retry: false,
    remedy: 'run /2mw2lt:connect, which the plugin\'s hold follows' }) + '\n'
  const w = world(on, { rounds: [{ lines: [refused], code: 1 }, { lines: [], code: 0, hold: never }] })
  await begin($)
  await w.clock.settle()
  expect(w.spawns.length).toBe(1)
  expect(w.submits.length).toBe(1)
  expect(w.submits[0]).toContain('refused 401: session-token-locator-read')
  expect(w.submits[0]).toContain('/2mw2lt:disconnect')
  expect(w.submits[0]).toContain('run /2mw2lt:connect, which the plugin\'s hold follows')
  await w.clock.advance(120_000)
  expect(w.spawns.length).toBe(1)                          // the loop stopped; the turn is not repeated
  expect(w.submits.length).toBe(1)
})

test('a contested stream keeps backing off however long each hold lasted, so two holders do not trade it every second', async ($, on) => {
  const w = world(on, { rounds: [{ lines: [], code: 3, holdMs: 120_000 }, { lines: [], code: 3, holdMs: 120_000 }, { lines: [], code: 0, hold: never }] })
  await begin($)
  await w.clock.settle()
  await w.clock.advance(120_000)
  await w.clock.advance(1_000)
  expect(w.spawns.length).toBe(2)
  await w.clock.advance(120_000)
  await w.clock.advance(1_500)
  expect(w.spawns.length).toBe(2)                       // a hold that lasted earns no reset here
  await w.clock.advance(1_000)
  expect(w.spawns.length).toBe(3)
})

test('a hold that ends badly says why in the debug log, and one that broke its recording is held again', async ($, on) => {
  const w = world(on, { enrolled: false, rounds: [
    { lines: [{ stderr: 'cannot record stream frames at /ws/x: disk full\n' }], code: 4 },
    { lines: [{ stderr: 'refused: no fresh holder claim for ps-1; the plugin\'s module starts this hold\n' }], code: 1 },
    { lines: [], code: 0, hold: never },
  ] })
  await begin($)
  await w.clock.settle()
  w.enrolled = true
  await $.tool.call({ tool: 'Bash', command: 'python3 /p/steering/enroll/connect.py --current' } as never)
  await w.clock.settle()
  await w.clock.advance(5_000)
  expect(w.spawns.length).toBe(3)
  expect(w.logs.some(l => /exited 4/.test(l) && l.includes('disk full'))).toBe(true)
  expect(w.logs.some(l => /exited 1/.test(l) && l.includes('no fresh holder claim'))).toBe(true)
})

// Measured 2026-10-13: a session ran connect as `python3 $E/connect.py`, which `invokes` cannot see,
// and held no stream for its whole life while connect said the plugin held it.
test('a connect the command does not name still starts the hold, from the record connect wrote', async ($, on) => {
  const w = world(on, { enrolled: false, rounds: [{ lines: [], code: 0, hold: never }] })
  await begin($)
  await w.clock.settle()
  expect(w.spawns.length).toBe(0)
  w.enrolled = true
  w.connected = '1791854530.9'
  await $.tool.call({ tool: 'Bash', command: 'python3 $E/connect.py --current' } as never)
  await w.clock.settle()
  expect(w.spawns.length).toBe(1)
  await $.tool.call({ tool: 'Bash', command: 'ls' } as never)
  await w.clock.settle()
  expect(w.spawns.length).toBe(1)                       // one record asks for one start
})

test('a connect run where no command of this session shows it starts the hold within a refresh', async ($, on) => {
  const w = world(on, { enrolled: false, rounds: [{ lines: [], code: 0, hold: never }] })
  await begin($)
  await w.clock.settle()
  w.enrolled = true
  w.connected = '1791854530.9'
  await w.clock.advance(60_000)
  await w.clock.settle()
  expect(w.spawns.length).toBe(1)
})

test('a connect whose enrolment lookup failed keeps its record for the next refresh', async ($, on) => {
  const w = world(on, { enrolled: false, rounds: [{ lines: [], code: 0, hold: never }] })
  await begin($)
  await w.clock.settle()
  w.enrolled = true
  w.sessionLookupCode = 1
  w.connected = '1791854530.9'
  await $.tool.call({ tool: 'Bash', command: 'python3 $E/connect.py --current' } as never)
  await w.clock.settle()
  expect(w.spawns.length).toBe(0)
  w.sessionLookupCode = 0
  await w.clock.advance(60_000)
  await w.clock.settle()
  expect(w.spawns.length).toBe(1)
})

test('a connect record from before the session started asks for no second start', async ($, on) => {
  const w = world(on, { rounds: [{ lines: [], code: 0, hold: never }] })
  w.connected = '1791854530.9'
  await begin($)
  await w.clock.settle()
  await w.clock.advance(60_000)
  await $.tool.call({ tool: 'Bash', command: 'ls' } as never)
  await w.clock.settle()
  expect(w.spawns.length).toBe(1)
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
  expect(w.writes.length).toBe(3)                       // claim, the retrying record, release: the refresh no longer writes it live
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
  expect(JSON.parse(w.writes.filter(x => x.path.endsWith('/ps-2.json'))[0]?.text ?? '{}').hold).toBeUndefined()   // no stale holding carried to the new id
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
  expect(w.appends).toEqual([POINTER, CHECKPOINT])
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

test('lifecycle names the enrolment verb a command runs, or none', () => {
  expect(lifecycle('python3 /p/steering/enroll/connect.py --current')).toBe('connect')
  expect(lifecycle('python3 "/p/steering/enroll/disconnect.py" s1')).toBe('disconnect')
  expect(lifecycle('python3 /p/steering/enroll/checkpoint.py s1 handover https://github.com/example-org/example-repo/pull/1')).toBe('checkpoint')
  expect(lifecycle('python3 /p/steering/enroll/ack.py s1 01JENV')).toBe('ack')
  expect(lifecycle('python3 /p/steering/enroll/say.py --to peer s1 hello')).toBe('say')
  expect(lifecycle('python3 /p/steering/enroll/verb.py announce s1 as acct on b doing "w"')).toBe('announce')
  expect(lifecycle('python3 "$ws/my plugins/steering/enroll/verb.py" announce s1 x')).toBe('announce')
  // The board's verbs and a read of a client are not the enrolment lifecycle.
  expect(lifecycle('python3 /p/steering/enroll/verb.py wait s1 for checks 1')).toBe(null)
  expect(lifecycle('python3 /p/steering/enroll/verb.py done s1')).toBe(null)
  expect(lifecycle('python3 /p/steering/enroll/taking.py s1 branch')).toBe(null)
  expect(lifecycle('cat /p/steering/enroll/say.py')).toBe(null)
  expect(lifecycle('grep -n say /p/steering/enroll/say.py')).toBe(null)
  expect(lifecycle('python3 /p/steering/enroll/connect.py --help')).toBe(null)
  expect(lifecycle('python3 /p/steering/enroll/say.py -h')).toBe(null)
  expect(lifecycle('printf hi')).toBe(null)
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

// The engine's own state, not the stand-in: a dispatch reads what it held at its first read until
// it writes (#7516). The frames call and the hold loop are two dispatches, so a frame the loop adds
// while a call runs must survive the call's write-back with a pointer of its own, and a frame a
// call returned must not come back with the loop's next one.
test('under the engine state, a frame that arrives mid-call is pointed and returned once', async ($, on) => {
  let second: () => void = () => {}
  let third: () => void = () => {}
  const w = world(on, {
    engineState: true,
    rounds: [{ lines: [line('f1', 'say', true), new Promise<void>(r => { second = r }), line('f2', 'say', true),
      new Promise<void>(r => { third = r }), line('f3', 'say', true)], code: 0, hold: never }],
    recording: ['f1', 'f2', 'f3'].map(id => `data: {"kind":"say","id":"${id}"}\n`).join(''),
  })
  await begin($)
  await w.clock.settle()
  await $.turn.start({ text: 'the pointer', turnId: 't-1' } as never)
  let open: () => void = () => {}
  w.readGate = new Promise<void>(r => { open = r })
  const reading = $.tool.call({ tool: TOOL } as never)
  await w.clock.settle()
  second()
  await w.clock.settle()
  open()
  expect(await reading).toEqual({ result: '{"kind":"say","id":"f1"}' })
  await w.clock.settle()
  expect(w.appends).toEqual([POINTER])                // f2 is owed a pointer of its own
  third()
  await w.clock.settle()
  expect(await $.tool.call({ tool: TOOL } as never)).toEqual({ result: '{"kind":"say","id":"f2"}\n{"kind":"say","id":"f3"}' })
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
  w.enrolled = false
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
  expect((early as { result: unknown }).result).toBe('Nothing is waiting.')
  expect((later as { result: unknown }).result).toBe('{"kind":"say","id":"f1","text":"hi"}')
  expect(w.submits).toEqual([POINTER])          // a frame still missing does not wake the session again
})

// A subagent's command is its own: a connect or a disconnect it runs never stops, restarts or clears the
// parent's hold, which only the parent's own loop controls (#5688).
test('a subagent\'s connect or disconnect leaves the parent\'s hold, session and seat as they were', async ($, on) => {
  const w = world(on, { rounds: [{ lines: [], code: 0, hold: never }, { lines: [], code: 0, hold: never }] })
  await begin($)
  await w.clock.settle()
  expect(w.spawns.length).toBe(1)
  w.state.set('seat', 'the rulings')
  await $.tool.call({ tool: 'Bash', command: 'python3 "/p/steering/enroll/disconnect.py" s1', agentId: 'sub-1' } as never)
  await $.tool.call({ tool: 'Bash', command: 'python3 /p/steering/enroll/connect.py --current', agentId: 'sub-1' } as never)
  await w.clock.settle()
  await w.clock.advance(120_000)
  expect(w.spawns.length).toBe(1)                       // neither stopped nor restarted
  expect(w.state.get('session')).toBe('s1')
  expect(w.state.get('seat')).toBe('the rulings')
})

// A subagent presents its parent's credential: its provider session id is inherited, so its
// lifecycle verbs are refused before they run — the seat tools' own refusal's shape (#3342).
test('a subagent\'s lifecycle verbs are refused, and the parent\'s enrolment never learns of them', async ($, on) => {
  const w = world(on, { rounds: [{ lines: [], code: 0, hold: never }] })
  await begin($)
  await w.clock.settle()
  for (const [command, verb] of [
    ['python3 /p/steering/enroll/connect.py --current', 'connect'],
    ['python3 "/p/steering/enroll/disconnect.py" s1', 'disconnect'],
    ['python3 /p/steering/enroll/checkpoint.py s1 handover https://github.com/example-org/example-repo/pull/1', 'checkpoint'],
    ['python3 /p/steering/enroll/ack.py s1 01JENV', 'ack'],
    ['python3 /p/steering/enroll/say.py --to peer s1 hello', 'say'],
    ['python3 /p/steering/enroll/verb.py announce s1 as acct on b doing "w"', 'announce'],
  ] as const) {
    const got = await $.tool.call({ tool: 'Bash', command, agentId: 'sub-1' } as never)
    expect(String((got as { deny?: string }).deny)).toContain(`refused: the enrolment lifecycle is the session's own, never a subagent's (${verb}`)
  }
  // A read, a help and the session's own loop are not runs to refuse: the verb is refused by who
  // runs it, not by what it is.
  for (const command of ['cat /p/steering/enroll/connect.py', 'python3 /p/steering/enroll/connect.py --help']) {
    const got = await $.tool.call({ tool: 'Bash', command } as never)
    expect((got as { deny?: string }).deny).toBeUndefined()
  }
  const own = await $.tool.call({ tool: 'Bash', command: 'python3 /p/steering/enroll/say.py s1 hello' } as never)
  expect((own as { deny?: string }).deny).toBeUndefined()
  expect(w.spawns.length).toBe(1)                       // the parent's hold was never stopped or restarted
})

// Only the guard's own evaluation is fail-closed: a chain that breaks past it is the engine's
// error, however the harness answers it, never a refusal blaming the guard (#6208 round 1).
test('an error past the lifecycle guard propagates, and a refusal still refuses', async ($, on) => {
  const w = world(on, { rounds: [{ lines: [], code: 0, hold: never }] })
  w.bashThrows = true
  await begin($)
  await w.clock.settle()
  const denied = await $.tool.call({ tool: 'Bash', command: 'python3 /p/steering/enroll/connect.py --current', agentId: 'sub-1' } as never)
  expect(String((denied as { deny?: string }).deny)).toContain("the enrolment lifecycle is the session's own")
  let refused = false
  let broke = false
  try {
    const got = await $.tool.call({ tool: 'Bash', command: 'python3 /p/steering/enroll/say.py s1 hello', agentId: 'sub-1' } as never)
    refused = String((got as { deny?: string }).deny ?? '') !== ''
  } catch {
    broke = true
  }
  expect(refused || broke).toBe(true)
})

for (const [name, command] of [
  ['a refused disconnect masked by a successful shell command', 'python3 /p/steering/enroll/disconnect.py other-session; printf "finished"'],
  ['a disconnect sent as text to another pane', 'tmux send-keys -t %2 \'cd /work && python3 /p/steering/enroll/disconnect.py other-session\' Enter'],
]) test(`${name} keeps this session reachable`, async ($, on) => {
  let arrive!: () => void
  const w = world(on, { rounds: [{ lines: [new Promise<void>(resolve => { arrive = resolve }), line('after', 'say', true)], code: 0, hold: never }] })
  await begin($)
  await w.clock.settle()
  w.state.set('seat', 'the rulings')
  await $.tool.call({ tool: 'Bash', command } as never)
  expect(w.state.get('session')).toBe('s1')
  expect(w.state.get('seat')).toBe('the rulings')
  arrive()
  await w.clock.settle()
  expect(w.submits).toEqual([POINTER])
  expect(w.spawns.length).toBe(1)
})

// A detached name's record can outlive it beside the one its next connect wrote (#5661): the
// lookup hands the module whichever record sorted first, and the hold keeps the enrolment Go
// admits. The admitted name is the session's from then on — its frames, its next arm — so the
// stream it holds and the name it answers by cannot drift apart.
test('a hold admitted under a reconciled name makes that name the session\'s', async ($, on) => {
  const held = JSON.stringify({ id: null, kind: 'held', wakes: false, hold: 'h7', session: 's2' }) + '\n'
  const w = world(on, {
    rounds: [{ lines: [held, line('e1', 'envelope', true)], code: 0 }, { lines: [], code: 0, hold: never }],
    recording: 'data: {"kind":"envelope","id":"e1","ulid":"01JHELD"}\n',
  })
  await begin($)
  await w.clock.settle()
  expect(w.state.get('session')).toBe('s2')
  await $.tool.call({ tool: TOOL } as never)
  await w.clock.settle()
  expect(w.runs).toContainEqual(['python3', `${w.root}/steering/enroll/hold.py`, '--frame-path', 's2'])
  await w.clock.advance(2_000)
  expect(w.spawns[1]?.slice(-1)).toEqual(['s2'])
})

test('a failed enrolment lookup after disconnect text does not mean this session left', async ($, on) => {
  let arrive!: () => void
  const w = world(on, { rounds: [{ lines: [new Promise<void>(resolve => { arrive = resolve }), line('after', 'say', true)], code: 0, hold: never }] })
  await begin($)
  await w.clock.settle()
  w.state.set('seat', 'the rulings')
  w.enrolled = false
  w.sessionLookupCode = 1
  await $.tool.call({ tool: 'Bash', command: 'python3 /p/steering/enroll/disconnect.py other-session; printf "finished"' } as never)
  expect(w.state.get('session')).toBe('s1')
  expect(w.state.get('seat')).toBe('the rulings')
  arrive()
  await w.clock.settle()
  expect(w.submits).toEqual([POINTER])
})

test('disconnect ends the held enrolment even when an older name remains stored', async ($, on) => {
  const w = world(on, { rounds: [{ lines: [], code: 0, hold: never }] })
  await begin($)
  await w.clock.settle()
  w.state.set('seat', 'the rulings')
  w.enrolled = false
  w.retainedName = 'old-name\n'
  await $.tool.call({ tool: 'Bash', command: 'python3 /p/steering/enroll/disconnect.py s1' } as never)
  expect(w.state.get('session')).toBe('')
  expect(w.state.get('seat')).toBe('')
  expect(w.runs).toContainEqual(['python3', `${w.root}/steering/enroll/hold.py`, '--session-of', 'ps-1', 's1'])
})

test('a disconnect lookup that lands after reconnect does not stop the new hold', async ($, on) => {
  const w = world(on, { rounds: [{ lines: [], code: 0, hold: never }, { lines: [], code: 0, hold: never }] })
  await begin($)
  await w.clock.settle()
  let release!: () => void
  w.enrolled = false
  w.holdNextLookup = new Promise<void>(resolve => { release = resolve })
  const leaving = $.tool.call({ tool: 'Bash', command: 'python3 /p/steering/enroll/disconnect.py s1' } as never)
  await w.clock.settle()
  w.enrolled = true
  await $.tool.call({ tool: 'Bash', command: 'python3 /p/steering/enroll/connect.py --current' } as never)
  await w.clock.settle()
  release()
  await leaving
  expect(w.state.get('session')).toBe('s1')
  expect(w.spawns.length).toBe(2)
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
  w.enrolled = false
  await $.tool.call({ tool: 'Bash', command: 'python3 "/p/steering/enroll/disconnect.py" s1' } as never)
  end()
  await w.clock.settle()
  await w.clock.advance(120_000)
  expect(w.appends).toEqual([])
  expect(w.spawns.length).toBe(1)
  expect(w.state.get('session')).toBe('')
  expect(w.writes.map(x => JSON.parse(x.text).at).every(at => at > 0)).toBe(true)   // the claim stays live
})

test('a replacement hold re-points a frame whose submitted turn never started', async ($, on) => {
  const w = world(on, { rounds: [
    { lines: [line('f1', 'say', true)], code: 0 },
    { lines: [line(null, 'held', false)], code: 0, hold: never },
  ] })
  await begin($)
  await w.clock.settle()
  expect(w.submits).toEqual([POINTER])
  await w.clock.advance(1_000)
  expect(w.spawns.length).toBe(2)
  expect(w.submits).toEqual([POINTER, POINTER])
})

test('a sleep-expired claim is refreshed before the replacement hold delivers its replay', async ($, on) => {
  const w = world(on, { recording: RECORDING, rounds: [
    { lines: [], code: 4 },
    { lines: [line('e1', 'envelope', true)], code: 0, hold: never },
  ] })
  await begin($)
  await w.clock.settle()
  await w.clock.advance(1_000)
  expect(w.spawns.length).toBe(2)
  expect(w.claimsAtSpawn).toEqual([1, 3])   // the retrying record, then the refresh before the spawn
  expect(w.submits).toEqual([POINTER])
  expect(w.state.get('pending')).toEqual(['e1'])
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

test('a pointer dropped after the person stopped the session is appended, and offered again once they speak', async ($, on) => {
  const w = world(on, { submitDrops: true, rounds: [{ lines: [line('f1', 'say', true), line('f2', 'say', true)], code: 0, hold: never }] })
  await begin($)
  await w.clock.settle()
  expect(w.submits).toEqual([POINTER])
  expect(w.appends).toEqual([POINTER])
  expect(w.logs.some(l => l.includes('the session was stopped and nobody has sent input since'))).toBe(true)
  w.submitDrops = false
  await $.turn.start({ text: 'the person typed', turnId: 't-1' } as never)   // the model ignores the row
  await $.turn.complete({ turnId: 't-1', text: 'done' } as never)
  await w.clock.advance(2_000)
  expect(w.submits).toEqual([POINTER, POINTER])
})

// The session reads the plugin's state from its claim (#5641): holding only once coordination
// admitted the hold, retrying with the refusal that stopped it, or stopped with the remedy.
const lastHold = (w: { writes: { path: string; text: string }[] }) =>
  JSON.parse([...w.writes].reverse().find(x => x.path === CLAIM_PATH)?.text ?? '{}').hold

test('a hold coordination admitted is recorded as holding, with its id and since when', async ($, on) => {
  const w = world(on, { rounds: [{ lines: [JSON.stringify({ id: null, kind: 'held', wakes: false, hold: 'h1' }) + '\n'], code: 0, hold: never }] })
  await begin($)
  await w.clock.settle()
  expect(lastHold(w)).toEqual({ state: 'holding', hold: 'h1', since: 1000 })
})

test('a refusal a retry can outlive is recorded as retrying, with the attempt, the refusal and the next try', async ($, on) => {
  const w = world(on, { rounds: [
    { lines: [JSON.stringify({ id: null, kind: 'refused', wakes: false, why: 'refused 403: incarnation-unobserved', retry: true }) + '\n'], code: 1 },
    { lines: [], code: 0, hold: never },
  ] })
  await begin($)
  await w.clock.settle()
  expect(lastHold(w)).toEqual({ state: 'retrying', attempt: 1, refusal: 'refused 403: incarnation-unobserved', at: 1000, next_at: 1001 })
  await w.clock.advance(1_000)
  expect(w.spawns.length).toBe(2)
})

test('a refusal no retry outlives stops the loop, records its remedy and tells the session once', async ($, on) => {
  const w = world(on, { rounds: [
    { lines: [JSON.stringify({ id: null, kind: 'refused', wakes: false, why: 'refused 403: session-not-enrolled', retry: false, remedy: 'run /2mw2lt:connect' }) + '\n'], code: 1 },
    { lines: [], code: 0, hold: never },
  ] })
  await begin($)
  await w.clock.settle()
  await w.clock.advance(300_000)
  expect(w.spawns.length).toBe(1)
  expect(lastHold(w)).toEqual({ state: 'stopped', refusal: 'refused 403: session-not-enrolled', remedy: 'run /2mw2lt:connect', at: 1000 })
  expect(w.submits.length).toBe(1)
  expect(w.submits[0]).toContain('refused 403: session-not-enrolled')
  expect(w.submits[0]).toContain('run /2mw2lt:connect')
})

test('a hold refused after a connect is held again by the plugin, its claim kept live, and the session is asked nothing', async ($, on) => {
  const w = world(on, { enrolled: false, rounds: [
    { lines: [{ stderr: 'refused 403: a Claude stream must declare event\n' }], code: 1 },
    { lines: [], code: 1 },
    { lines: [], code: 0, hold: never },
  ] })
  await begin($)
  await w.clock.settle()
  w.enrolled = true
  await $.tool.call({ tool: 'Bash', command: 'python3 /p/steering/enroll/connect.py --current' } as never)
  await w.clock.settle()
  await w.clock.advance(300_000)
  expect(w.spawns.length).toBe(3)
  expect(w.submits).toEqual([])
  expect(w.appends).toEqual([])
  expect(JSON.parse(w.writes[w.writes.length - 1]?.text ?? '{}').at).toBeGreaterThan(0)   // connect still finds the plugin holding
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

// #4551 step 3: a seated session reads the knowledge's rulings in its system prompt.
const FACTS = { model: 'claude-test', promptModel: 'claude-test', surfaces: [], tools: [], outputStyle: null, traits: [] }
const seatSection = async ($: any) => ((await $.prompt.compose(FACTS)).sections as { id: string; text: string; scope: string }[])
  .find(x => x.id === SEAT_SECTION)
const seatRuns = (w: { runs: string[][] }) => w.runs.filter(argv => String(argv[1]).endsWith('/seat_section.py'))
const bulletinSection = async ($: any) => ((await $.prompt.compose(FACTS)).sections as { id: string; text: string; scope: string }[])
  .find(x => x.id === BULLETIN_SECTION)
const bulletinRuns = (w: { runs: string[][] }) => w.runs.filter(argv => String(argv[1]).endsWith('/bulletin.py'))

// #6473 unit 6: every connected session keeps the bulletin in its system prompt, moved only where
// the conversation's cache is spent anyway.
test('a session that holds no seat still reads the bulletin, after the seat section', async ($, on) => {
  const w = world(on, { rounds: [{ lines: [], code: 0, hold: never }] })
  w.seat = { code: 1, stdout: '' }
  w.bulletin = { code: 0, stdout: 'Bulletin r1: a line\n' }
  await begin($)
  await w.clock.settle()
  const sections = (await $.prompt.compose(FACTS)).sections as { id: string }[]
  expect(sections[sections.length - 1]).toEqual({ id: BULLETIN_SECTION, text: 'Bulletin r1: a line', scope: 'session' })
  expect(bulletinRuns(w)[0]?.slice(2)).toEqual(['--provider', 'claude', '--provider-session', 'ps-1', 'section'])
})

test('the bulletin moves on compaction and not on an ordinary turn', async ($, on) => {
  const w = world(on, { rounds: [{ lines: [], code: 0, hold: never }] })
  w.bulletin = { code: 0, stdout: 'A\n' }
  await begin($)
  await w.clock.settle()
  expect((await bulletinSection($))?.text).toBe('A')
  await $.turn.complete({ turnId: 't-1', text: 'done' } as never)
  await w.clock.settle()
  expect(bulletinRuns(w).length).toBe(1)                      // an ordinary turn asks nothing
  w.bulletin = { code: 0, stdout: 'B\n' }
  await $.session.compact({ trigger: 'manual', messages: [MESSAGE] } as never)
  await w.clock.settle()
  expect((await bulletinSection($))?.text).toBe('B')
})

test('a door that cannot tell keeps the bulletin it had; an empty bulletin clears it', async ($, on) => {
  const w = world(on, { rounds: [{ lines: [], code: 0, hold: never }] })
  w.bulletin = { code: 0, stdout: 'A\n' }
  await begin($)
  await w.clock.settle()
  w.bulletin = { code: 3, stdout: '' }
  await $.session.compact({ trigger: 'manual', messages: [MESSAGE] } as never)
  await w.clock.settle()
  expect((await bulletinSection($))?.text).toBe('A')
  w.bulletin = { code: 1, stdout: '' }
  await $.session.compact({ trigger: 'manual', messages: [MESSAGE] } as never)
  await w.clock.settle()
  expect(await bulletinSection($)).toBeUndefined()
})

test('a teammate rendering its lead\'s prompt carries no bulletin section', async ($, on) => {
  const w = world(on, { rounds: [{ lines: [], code: 0, hold: never }] })
  w.bulletin = { code: 0, stdout: 'A\n' }
  await begin($)
  await w.clock.settle()
  const sections = (await $.prompt.compose({ ...FACTS, traits: ['teammate'] })).sections as { id: string }[]
  expect(sections.some(x => x.id === BULLETIN_SECTION)).toBe(false)
})

// #6473 unit 7b: the changes a `bulletin` frame carries reach the model as one message appended
// into the turn that next starts, never as a wake, and never twice.
test('a bulletin frame wakes nothing and is appended, whole, at the next turn', async ($, on) => {
  const w = world(on, {
    rounds: [{ lines: [line('b1', 'bulletin', false), line('b2', 'bulletin', false)], code: 0, hold: never }],
    recording: 'data: {"kind":"bulletin","id":"b1","revision":8,"text":"Bulletin r8: one"}\n'
      + 'data: {"kind":"bulletin","id":"b2","revision":9,"text":"+ k: v (session s)"}\n',
  })
  await begin($)
  await w.clock.settle()
  expect(w.state.get('bulletins')).toEqual(['b1', 'b2'])          // collected, not pointed at
  expect(w.submits).toEqual([])                                   // an idle session is spent nothing
  expect(w.appends).toEqual([])
  await $.turn.start({ text: 'the person typed', turnId: 't-1' } as never)
  await w.clock.settle()
  expect(w.appends).toEqual(['Bulletin r8: one\n+ k: v (session s)'])   // two frames, one turn
  expect(w.state.get('bulletins')).toEqual([])
  expect(w.state.get('read')).toEqual(['b1', 'b2'])
  await $.turn.start({ text: 'again', turnId: 't-2' } as never)
  await w.clock.settle()
  expect(w.appends.length).toBe(1)                                // told once
  const again = $.tool.call({ tool: TOOL } as never)              // and the frames tool shows nothing
  expect(String((await again as { result: unknown }).result)).toBe('Nothing is waiting.')
})

test('a compaction drops the bulletin frames the fresh section holds and keeps the newer one', async ($, on) => {
  const w = world(on, {
    rounds: [{ lines: [line('b1', 'bulletin', false), line('b2', 'bulletin', false)], code: 0, hold: never }],
    recording: 'data: {"kind":"bulletin","id":"b1","revision":5,"text":"+ old: gone"}\n'
      + 'data: {"kind":"bulletin","id":"b2","revision":9,"text":"+ new: come"}\n',
  })
  await begin($)
  await w.clock.settle()
  expect(w.state.get('bulletins')).toEqual(['b1', 'b2'])          // an empty section holds nothing
  w.bulletin = { code: 0, stdout: 'Bulletin r7: fresh\n' }
  await $.session.compact({ trigger: 'manual', messages: [MESSAGE] } as never)
  await w.clock.settle()
  expect(w.state.get('bulletins')).toEqual(['b2'])                // r5 is held by the r7 section; r9 is not
  await $.turn.start({ text: 'the person typed', turnId: 't-1' } as never)
  await w.clock.settle()
  expect(w.appends).toEqual(['+ new: come'])
})

test('a process whose last render was a teammate\'s runs no bulletin refresh, so marks nothing shown', async ($, on) => {
  const w = world(on, { rounds: [{ lines: [], code: 0, hold: never }] })
  w.bulletin = { code: 0, stdout: 'A\n' }
  await begin($)
  await w.clock.settle()
  const before = bulletinRuns(w).length
  await $.prompt.compose({ ...FACTS, traits: ['teammate'] })
  w.bulletin = { code: 0, stdout: 'B\n' }
  await $.session.compact({ trigger: 'manual', messages: [MESSAGE] } as never)
  await w.clock.settle()
  expect(bulletinRuns(w).length).toBe(before)                     // no run, so no mark
  expect((await bulletinSection($))?.text).toBe('A')              // and the bytes it had
  await $.prompt.compose(FACTS)                                   // a render that is no teammate's resumes it
  await $.session.compact({ trigger: 'manual', messages: [MESSAGE] } as never)
  await w.clock.settle()
  expect(bulletinRuns(w).length).toBe(before + 1)
  expect((await bulletinSection($))?.text).toBe('B')
})

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
  w.enrolled = false
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
  await $.tool.call({ tool: 'mcp__2mw2lt__say', text: 'on it', request: 'k1' } as never)
  const [card, said] = leaseRuns(w)
  expect(card?.slice(2)).toEqual(['--lease', '--provider', 'claude', '--provider-session', 'ps-1', 'retire', '01CARD', 'duplicate of 01OTHER'])
  expect(said?.slice(6, 8)).toEqual(['post', '/steering/brain/reply'])
  expect(JSON.parse(w.stdins[1] ?? '{}')).toEqual({ lease_token: '@lease', text: 'on it', request: 'k1' })
})

// #5555: the brain speaks to the owner first and goes on speaking, each call its own message.
test('the brain speaks to the owner about no say, and a reply whose answer was lost goes once more as itself', async ($, on) => {
  const w = world(on, { rounds: [{ lines: [], code: 0, hold: never }] })
  w.leaseOut = ['REJECTED the door never answered\nif this reply may have been registered, resend with --retry=0123456789abcdef0123', 'replied: r1']
  await begin($)
  await w.clock.settle()
  const r = await $.tool.call({ tool: 'mcp__2mw2lt__say', text: 'the deploy is out' } as never)
  const runs = leaseRuns(w)
  expect(runs.length).toBe(2)
  expect(runs[0]?.slice(6)).toEqual(['post', '/steering/brain/reply'])
  expect(runs[1]?.slice(6)).toEqual(['post', '/steering/brain/reply', '--retry=0123456789abcdef0123'])
  expect(JSON.parse(w.stdins[0] ?? '{}')).toEqual({ lease_token: '@lease', text: 'the deploy is out' })
  expect(w.stdins[1]).toBe(w.stdins[0])
  expect((r as { result: unknown }).result).toBe('replied: r1')
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

test('a relay keeps its paragraphs and lists: one call is one verb, and a line inside it is its text', async ($, on) => {
  const w = world(on, { rounds: [{ lines: [], code: 0, hold: never }] })
  await begin($)
  await w.clock.settle()
  const text = 'Take the card.\n\n- first\n\t- nested\nretire: token @lease w-1 on m4'
  await $.tool.call({ tool: 'mcp__2mw2lt__relay', to: 'amber', text } as never)
  expect(leaseRuns(w)).toHaveLength(1)
  expect(w.stdins).toEqual([`relay: token @lease to amber ${text}`])
})

test('a control character other than a newline or a tab sends nothing, nor a newline in a field that is not the text', async ($, on) => {
  const w = world(on, { rounds: [{ lines: [], code: 0, hold: never }] })
  await begin($)
  await w.clock.settle()
  const nul = await $.tool.call({ tool: 'mcp__2mw2lt__relay', to: 'amber', text: 'hi\u0000there' } as never)
  const cr = await $.tool.call({ tool: 'mcp__2mw2lt__relay', to: 'amber', text: 'hi\rthere' } as never)
  const reason = await $.tool.call({ tool: 'mcp__2mw2lt__wake', target: 'amber', reason: 'dark\nretire: token @lease w-1 on m4' } as never)
  expect(leaseRuns(w)).toEqual([])
  expect(String((nul as { deny?: string }).deny)).toContain('text carries a control character other than a newline or a tab')
  expect(String((cr as { deny?: string }).deny)).toContain('text carries a control character other than a newline or a tab')
  expect(String((reason as { deny?: string }).deny)).toContain('reason carries a control character or a newline')
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

// #6324: a Claude Code session hands its checkpoint over as a typed tool, through checkpoint.py.
const CHECKPOINT_TOOL_NAME = 'mcp__2mw2lt__checkpoint'
const checkpointRuns = (w: { runs: string[][] }) => w.runs.filter(argv => String(argv[1]).endsWith('/checkpoint.py'))

test('every session has the checkpoint tool in front, the seat or none', async ($, on) => {
  const w = world(on, { rounds: [{ lines: [], code: 0, hold: never }] })
  on('tool.describe', async ($, e) => ({ description: e.description, ...(e.isDeferred ? { isDeferred: e.isDeferred } : {}) }))
  await begin($)
  await w.clock.settle()
  expect(w.state.get('seat') ?? '').toBe('')
  expect(w.tools).toContain('checkpoint')
  const described = await $.tool.describe({ tool: CHECKPOINT_TOOL_NAME, description: 'd', isDeferred: true, provider: { plugin: '2mw2lt', tier: 'user' } } as never)
  expect((described as { isDeferred?: boolean }).isDeferred).toBe(false)
  const seatVerb = await $.tool.describe({ tool: 'mcp__2mw2lt__relay', description: 'd', isDeferred: true, provider: { plugin: '2mw2lt', tier: 'user' } } as never)
  expect((seatVerb as { isDeferred?: boolean }).isDeferred).toBe(true)
})

test('a subagent\'s checkpoint is refused before checkpoint.py runs: the checkpoint is the session\'s own', async ($, on) => {
  const w = world(on, { rounds: [{ lines: [], code: 0, hold: never }] })
  await begin($)
  await w.clock.settle()
  const r = await $.tool.call({ tool: CHECKPOINT_TOOL_NAME, agentId: 'subagent-1', boundary: 'context', text: 'note' } as never)
  expect(String((r as { deny?: string }).deny)).toContain('never a subagent\'s')
  expect(checkpointRuns(w)).toEqual([])
})

test('the checkpoint\'s text reaches checkpoint.py byte for byte on stdin, the lessons through a file it removes after', async ($, on) => {
  const w = world(on, { rounds: [{ lines: [], code: 0, hold: never }] })
  await begin($)
  await w.clock.settle()
  const text = '## Checkpoint\n\nBoundary: `context` — session s1.\n\n**Next.**\n1. `rm -rf "$x"`; ünïcode, trailing spaces   \n\n'
  const learned = [{ text: 'Read the plan first.', scope: ['steering/'] }]
  const r = await $.tool.call({ tool: CHECKPOINT_TOOL_NAME, boundary: 'context', text, learned, mirror: 'https://github.com/example-org/example-repo/pull/1' } as never)
  expect((r as { result?: string }).result).toBe('checkpointed: 7d1c0f2e-0000-4000-8000-000000000001 context\nid 0123456789abcdef0123')
  expect(checkpointRuns(w)).toEqual([['python3', `${w.root}/steering/enroll/checkpoint.py`, 's1', 'context',
    '--learned-file', '/tmp/tmp.lessons', '--mirror', 'https://github.com/example-org/example-repo/pull/1']])
  expect(w.stdins).toEqual([text])
  expect(w.writes).toContainEqual({ path: '/tmp/tmp.lessons', text: JSON.stringify(learned) })
  const at = (argv: string[]) => w.runs.findIndex(run => run.join(' ') === argv.join(' '))
  expect(at(['rm', '-f', '/tmp/tmp.lessons'])).toBeGreaterThan(at(checkpointRuns(w)[0]!))
})

test('a checkpoint whose answer was lost goes once more under its id, with the same text, and no more', async ($, on) => {
  const w = world(on, { rounds: [{ lines: [], code: 0, hold: never }] })
  const lost = { code: 1, stdout: 'unavailable: the door did not answer\nif it may have been recorded, resend with --retry=0123456789abcdef0123\n', stderr: 'id 0123456789abcdef0123\n' }
  w.checkpointOut = [lost, lost]
  await begin($)
  await w.clock.settle()
  const r = await $.tool.call({ tool: CHECKPOINT_TOOL_NAME, boundary: 'handover', text: 'the note' } as never)
  const runs = checkpointRuns(w)
  expect(runs.length).toBe(2)
  expect(runs[0]?.slice(2)).toEqual(['s1', 'handover'])
  expect(runs[1]?.slice(2)).toEqual(['s1', 'handover', '--retry=0123456789abcdef0123'])
  expect(w.stdins).toEqual(['the note', 'the note'])
  expect(String((r as { deny?: string }).deny)).toContain('resend with --retry=0123456789abcdef0123')
})

test('a checkpoint from a session not connected, or at no known boundary, runs nothing', async ($, on) => {
  const w = world(on, { rounds: [{ lines: [], code: 0, hold: never }] })
  await begin($)
  await w.clock.settle()
  const odd = await $.tool.call({ tool: CHECKPOINT_TOOL_NAME, boundary: '--retry=x', text: 'note' } as never)
  expect(String((odd as { deny?: string }).deny)).toContain('boundary is one of')
  w.enrolled = false
  const unconnected = await $.tool.call({ tool: CHECKPOINT_TOOL_NAME, boundary: 'exit', text: 'note' } as never)
  expect(String((unconnected as { deny?: string }).deny)).toContain('/2mw2lt:connect')
  expect(checkpointRuns(w)).toEqual([])
})

test('seatCall: word fields are one word, enums hold, and unknown fields are refused', async () => {
  expect(seatCall('retire', { worker: 'w 1', node: 'm4' })).toEqual({ refused: 'worker is one word' })
  expect(seatCall('retire', { worker: 'teal', reason: 'the card landed' })).toEqual({ client: 'say', stdin: 'retire: token @lease teal because the card landed' })
  expect(seatCall('retire', { worker: 'w-1', node: 'm4' })).toEqual({ client: 'say', stdin: 'retire: token @lease w-1 on m4' })
  expect(seatCall('retire', { worker: 'teal' })).toEqual({ refused: 'retire takes one of node or reason' })
  expect(seatCall('retire', { worker: 'teal', node: 'm4', reason: 'done' })).toEqual({ refused: 'retire takes one of node or reason' })
  expect(seatCall('card_session', { card: 'c', session: 's', role: 'boss' })).toEqual({ refused: 'role is one of executor, planned' })
  expect(seatCall('dispose', { item: 'i', reason: 'r', token: 'x' })).toEqual({ refused: 'dispose takes no token' })
  expect(seatCall('relay', { to: 'amber', epoch: 3, text: 'go' })).toEqual({ client: 'say', stdin: 'relay: token @lease to amber@3 go' })
  expect(seatCall('relay', { to: 'amber', checkpoint: '0b5f3c1e-4a2d-4c8e-9f10-2a3b4c5d6e7f', text: 'take over' }))
    .toEqual({ client: 'say', stdin: 'relay: token @lease to amber checkpoint 0b5f3c1e-4a2d-4c8e-9f10-2a3b4c5d6e7f take over' })
  expect(seatCall('card_scope', { name: 'The work', track: 'rt', anchors: ['resolves:12'], major: true })).toEqual(
    { client: 'card', argv: ['scope', 'The work', 'rt', 'resolves:12', 'major'] })
  expect(seatCall('card_scope', { name: 'The work', track: 'rt', criticality: 'major', size: 'L' })).toEqual(
    { client: 'card', argv: ['scope', 'The work', 'rt', 'triage:major,L'] })
  expect(resendId('done')).toBeNull()
})

// A snapshot is answered by its episode, never by speaking to the owner (the owner's ruling, 2026-10-22).
test('seatCall: a snapshot is answered by its episode, acted or considered, with why', async () => {
  expect(seatCall('answer', { episode: '0b5f3c1e-4a2d-4c8e-9f10-2a3b4c5d6e7f', verdict: 'considered', because: 'nothing moved' }))
    .toEqual({ client: 'say', stdin: 'answer: token @lease 0b5f3c1e-4a2d-4c8e-9f10-2a3b4c5d6e7f considered because nothing moved' })
  expect(seatCall('answer', { episode: 'e1', verdict: 'noop', because: 'x' })).toEqual({ refused: 'verdict is one of acted, considered' })
  expect(seatCall('answer', { episode: 'e1', verdict: 'acted' })).toEqual({ refused: 'because is required' })
})

test('seatCall: an out-of-band answer names the row, where it was given, and the words', async () => {
  expect(seatCall('out-of-band', { item: 'i1', channel: "the owner's chat, 13:20Z", words: 'ship it Friday' }))
    .toEqual({ client: 'say', stdin: `out-of-band: token @lease i1 via the owner's chat, 13:20Z: ship it Friday` })
  expect(seatCall('out-of-band', { item: 'i1', channel: 'chat' })).toEqual({ refused: 'words is required' })
  expect(seatCall('out-of-band', { item: 'i1', words: 'ship it Friday' })).toEqual({ refused: 'channel is required' })
  expect(seatCall('out-of-band', { channel: 'chat', words: 'ship it Friday' })).toEqual({ refused: 'item is required' })
})

test('seatCall: a park names its until only when given, and needs a reason', async () => {
  expect(seatCall('park', { session: 'teal', reason: 'a critic needs its slot' }))
    .toEqual({ client: 'say', stdin: 'park: token @lease teal because a critic needs its slot' })
  expect(seatCall('park', { session: 'teal', until: '2h', reason: 'a critic needs its slot' }))
    .toEqual({ client: 'say', stdin: 'park: token @lease teal until 2h because a critic needs its slot' })
  expect(seatCall('park', { session: 'teal' })).toEqual({ refused: 'reason is required' })
})

test('seatCall: a rehome names the account, and compacts first only when asked', async () => {
  const line = (a: Record<string, unknown>) => seatCall('rehome', { session: 'teal', account: '3', reason: 'account 2 is spent', ...a })
  expect(line({})).toEqual({ client: 'say', stdin: 'rehome: token @lease teal to 3 because account 2 is spent' })
  expect(line({ compact: true })).toEqual({ client: 'say', stdin: 'rehome: token @lease teal to 3 compact because account 2 is spent' })
  expect(line({ compact: true, without_checkpoint: true })).toEqual(
    { client: 'say', stdin: 'rehome: token @lease teal to 3 compact without checkpoint because account 2 is spent' })
  expect(line({ compact: false, without_checkpoint: true })).toEqual({ refused: 'without_checkpoint is only with compact' })
  expect(line({ account: 'acct 3' })).toEqual({ refused: 'account is one word' })
  expect(seatCall('rehome', { session: 'teal', reason: 'r' })).toEqual({ refused: 'account is required' })
})

test('seatCall: a rehome into another vendor names it, and the model and effort the session resumes on', async () => {
  const line = (a: Record<string, unknown>) => seatCall('rehome', { session: 'teal', account: '4', reason: 'build on GLM', ...a })
  expect(line({ vendor: 'zai', model: 'glm-5.3-flash', effort: 'high' })).toEqual(
    { client: 'say', stdin: 'rehome: token @lease teal to vendor zai 4 model glm-5.3-flash effort high because build on GLM' })
  expect(line({ vendor: 'zai', model: 'glm-5.3', compact: true })).toEqual(
    { client: 'say', stdin: 'rehome: token @lease teal to vendor zai 4 model glm-5.3 compact because build on GLM' })
  expect(line({ effort: 'extreme' })).toEqual({ refused: 'effort is one of low, medium, high, xhigh, max' })
})

test('seatCall: a keep carries its reason, and its until only when given; an unkeep names the session alone', async () => {
  expect(seatCall('keep', { session: 'teal', reason: 'the review on #5536' }))
    .toEqual({ client: 'say', stdin: 'keep: token @lease teal because the review on #5536' })
  expect(seatCall('keep', { session: 'teal', until: '45m', reason: 'the review on #5536' }))
    .toEqual({ client: 'say', stdin: 'keep: token @lease teal until 45m because the review on #5536' })
  expect(seatCall('keep', { session: 'teal', until: '45m' })).toEqual({ refused: 'reason is required' })
  expect(seatCall('keep', { session: 'teal', until: 'in an hour', reason: 'r' })).toEqual({ refused: 'until is one word' })
  expect(seatCall('unkeep', { session: 'teal' })).toEqual({ client: 'say', stdin: 'unkeep: token @lease teal' })
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

// The hold as polls on the agent's frames route (#7838): no child holds it where the agent serves
// the route, each poll carries the agent's key and the harness session, and the lines the agent
// answers are handled as the child's were.
const poll = (status: number, body: unknown): HoldPoll => ({ status, text: typeof body === 'string' ? body : JSON.stringify(body) })
const admitted = (id = 'h-1', session = 's1', frames: unknown[] = []) =>
  poll(200, { held: { hold_id: id, session, provider_session: 'ps-1', runtime: 'claude:n:r', frames: FRAME_PATH }, frames })
const framesOf = (...lines: unknown[]) => poll(200, { frames: lines })
// Only the hold's own requests: the live view's channel polls the same agent beside it.
const framesFetches = (w: { fetches: { url: string; method: string; headers: Record<string, string>; body?: string }[] }) =>
  w.fetches.filter(f => f.url.includes('/frames'))
const holdsLetGo = (w: Parameters<typeof framesFetches>[0]) => framesFetches(w).filter(f => f.method === 'DELETE').map(f => f.url.split('/frames')[1])

test('a workspace naming an agent is held as polls: the first admits, and a waking frame points', async ($, on) => {
  const w = world(on, { agent: true, holdRoute: true, holdPolls: [admitted(), framesOf({ id: 'u1', kind: 'usage', wakes: false }, { id: 'f1', kind: 'say', wakes: true })] })
  await begin($)
  await w.clock.settle()
  expect(w.spawns).toEqual([])
  expect(w.submits).toEqual([POINTER])
  const mine = framesFetches(w)
  expect(mine.length).toBe(3)                            // admitted, the frames, and the poll out now
  expect(mine[0]?.url).toBe(`http://127.0.0.1:${AGENT.port}/steering/session/s1/frames`)
  expect(mine[1]?.url).toBe(`http://127.0.0.1:${AGENT.port}/steering/session/s1/frames?hold=h-1`)
  expect(mine[0]?.headers).toEqual({ 'X-Steering-Agent-Key': AGENT.key, 'X-Steering-Workspace': '/ws', 'X-Steering-Provider': 'claude',
    'X-Steering-Provider-Session': 'ps-1', 'X-Steering-Wake': 'plugin',
    // The configuration the engine moved its home to, and the session's project: where the agent
    // reads its registry of live sessions, and what a hand-over's targeting names.
    'X-Steering-Config-Dir': encodeURIComponent('/u/owner/.claude'), 'X-Steering-Project': encodeURIComponent(ROOT) })
  const claim = JSON.parse(w.writes.filter(x => x.path === CLAIM_PATH).at(-1)!.text)
  expect(claim.hold.state).toBe('holding')
  expect(claim.hold.hold).toBe('h-1')
  // The frames tool reads the recording where the agent said it is, asking hold.py for no path.
  w.recording = 'data: {"kind":"say","id":"f1","from":"session peer","text":"hello"}\n'
  const r = await $.tool.call({ tool: TOOL }) as { result: string }
  expect(r.result).toContain('"id":"f1"')
  expect(w.runs.filter(r => r.includes('--frame-path'))).toEqual([])
})

test('an agent that serves no frames route hands the arm to hold.py, and the next arm probes it again', async ($, on) => {
  const w = world(on, { agent: true, holdRoute: true, holdPolls: [poll(404, '404 page not found\n'), poll(404, '404 page not found\n')],
    rounds: [{ lines: [line('f1', 'say', true)], code: 0 }, { lines: [], code: 0, hold: never }] })
  await begin($)
  await w.clock.settle()
  expect(w.spawns.length).toBe(1)
  expect(w.submits).toEqual([POINTER])
  await w.clock.advance(1_000)
  expect(framesFetches(w).length).toBe(2)                // each arm probes the route first
  expect(w.spawns.length).toBe(2)
})

test('a poll refused for good stops the loop and tells the session; a curable one holds again', async ($, on) => {
  const w = world(on, { agent: true, holdRoute: true, holdPolls: [
    poll(403, { refused: 'refused 403: uplink-stale.', retry: true }),
    poll(403, { refused: 'refused 403: session-retired. The agent serves [/ws].', retry: false, remedy: 'run /2mw2lt:connect, which the plugin\'s hold follows' })] })
  await begin($)
  await w.clock.settle()
  expect(framesFetches(w).length).toBe(1)
  let claim = JSON.parse(w.writes.filter(x => x.path === CLAIM_PATH).at(-1)!.text)
  expect(claim.hold.state).toBe('retrying')
  expect(claim.hold.refusal).toBe('refused 403: uplink-stale.')
  await w.clock.advance(1_000)
  expect(framesFetches(w).length).toBe(2)
  expect(w.submits.length).toBe(1)
  expect(w.submits[0]).toContain('refused 403: session-retired')
  expect(w.submits[0]).toContain('run /2mw2lt:connect, which the plugin\'s hold follows')
  claim = JSON.parse(w.writes.filter(x => x.path === CLAIM_PATH).at(-1)!.text)
  expect(claim.hold.state).toBe('stopped')
  await w.clock.advance(120_000)
  expect(framesFetches(w).length).toBe(2)                // the loop stopped
})

test('an agent serving another workspace is a refusal for good, as hold.py reads a 404 naming what it serves', async ($, on) => {
  const w = world(on, { agent: true, holdRoute: true, holdPolls: [poll(404, { refused: 'this agent does not serve /ws', serves: ['/elsewhere'] })] })
  await begin($)
  await w.clock.settle()
  expect(w.submits.length).toBe(1)
  expect(w.submits[0]).toContain('refused 404: this agent does not serve /ws')
  await w.clock.advance(120_000)
  expect(framesFetches(w).length).toBe(1)
})

test('a closed stream ends the arm and the next poll admits anew; a revoked one backs off like a child exiting 3', async ($, on) => {
  const w = world(on, { agent: true, holdRoute: true, holdPolls: [admitted(), framesOf({ id: null, kind: 'closed', wakes: false, why: 'uplink' }),
    admitted('h-2'), framesOf({ id: null, kind: 'closed', wakes: true, why: 'revoked' }), admitted('h-3')] })
  await begin($)
  await w.clock.settle()
  await w.clock.advance(1_000)
  await w.clock.advance(2_000)
  expect(framesFetches(w).map(f => f.url.split('/frames')[1])).toEqual(['', '?hold=h-1', '', '?hold=h-2', '', '?hold=h-3'])
  expect(w.submits).toEqual([])                          // a closed frame is no pointer
  const claim = JSON.parse(w.writes.filter(x => x.path === CLAIM_PATH).at(-1)!.text)
  expect(claim.hold.hold).toBe('h-3')
})

test('a poll the agent did not answer is held again after the backoff', async ($, on) => {
  const w = world(on, { agent: true, holdRoute: true, holdPolls: ['throw', admitted()] })
  await begin($)
  await w.clock.settle()
  expect(w.logs.some(l => l.startsWith('2mw2lt: the hold broke: '))).toBe(true)
  await w.clock.advance(1_000)
  expect(framesFetches(w).length).toBe(3)
})

test('an agent answering 503 is held again after the backoff, and the agent it resolved is kept', async ($, on) => {
  const w = world(on, { agent: true, holdRoute: true, holdPolls: [poll(503, { refused: 'no uplink' }), admitted()] })
  await begin($)
  await w.clock.settle()
  expect(w.logs.some(l => l.includes('the hold exited 2: the agent answered 503'))).toBe(true)
  await w.clock.advance(1_000)
  expect(framesFetches(w).length).toBe(3)
})

test('the admitted session is published at once, so the frames tool receipts under the name coordination admits', async ($, on) => {
  const w = world(on, { agent: true, holdRoute: true, holdPolls: [admitted('h-1', 's1-kept'), framesOf({ id: 'f1', kind: 'say', wakes: true })] })
  await begin($)
  await w.clock.settle()
  w.recording = 'data: {"kind":"say","id":"f1","from":"session peer","text":"hello"}\n'
  const r = await $.tool.call({ tool: TOOL }) as { result: string }
  expect(r.result).toContain('"id":"f1"')
  // The receipt names the admitted session, not the one the module first asked for (#5661).
  expect(w.runs.some(argv => argv.includes('--read-say') && argv.includes('s1-kept') && argv.includes('f1'))).toBe(true)
})

test("the session's end finishes its local cleanup whatever the agent's pace", async ($, on) => {
  const w = world(on, { agent: true, holdRoute: true, holdPolls: [admitted()], deleteHangs: true })
  await begin($)
  await w.clock.settle()
  await $.session.end({ sessionId: 'ps-1', reason: 'exit' } as never)
  const claim = JSON.parse(w.writes.filter(x => x.path === CLAIM_PATH).at(-1)!.text)
  expect(claim.at).toBe(0)                        // released, though the let-go still hangs
  expect(holdsLetGo(w).length).toBe(1)
})

test('the session ending, a disconnect, or a connect that starts over lets the agent go of the hold', async ($, on) => {
  // The first arm's third poll is the one out when connect starts over; the fresh arm's admits h-2.
  const hung: HoldPoll = { status: 0, text: '', hold: never }
  const w = world(on, { agent: true, holdRoute: true, holdPolls: [admitted(), framesOf({ id: 'f1', kind: 'say', wakes: true }), hung, admitted('h-2')] })
  await begin($)
  await w.clock.settle()
  expect(holdsLetGo(w)).toEqual([])
  await $.tool.call({ tool: 'Bash', command: 'python3 /p/steering/enroll/connect.py --current' } as never)
  await w.clock.settle()
  expect(holdsLetGo(w)).toEqual(['?hold=h-1'])                          // the superseded arm's hold
  expect(framesFetches(w).filter(f => f.method !== 'DELETE').length).toBe(5)   // a fresh admit, and the poll out now
  await $.session.end({ sessionId: 'ps-1', reason: 'exit' } as never)
  await w.clock.settle()
  expect(holdsLetGo(w)).toEqual(['?hold=h-1', '?hold=h-2'])
  const claim = JSON.parse(w.writes.filter(x => x.path === CLAIM_PATH).at(-1)!.text)
  expect(claim.at).toBe(0)
})
