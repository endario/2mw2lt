import type { Register, EngineInterface, Timer, HookStream, ProcessSpawnChunk, ProcessSpawnResult } from 'claude-code'
import { lines, atLeast, pick, invokes, lifecycle, tokensOf, due, owed, pointing, quiet, framesEntry, obsoleteSeatRefusal, nextSeat, asksSeat, seatCall, seatSchema, resendId, entryRefusal, checkpointCall, CHECKPOINT_ENTRY, CHECKPOINT_TOOL, refusalTurn, capBulletin, sectionRevision, READ_KEPT, POINTER, CHECKPOINT, SEAT_SECTION, BULLETIN_SECTION, SEAT_TOOLS, TIMER_TOOLS, timerCall, timerSchema, TIMER_ENTRY } from './frames'
import type { Line, SeatCall } from './frames'
import * as surface from './surface'
import type { CommandReceipt } from './types'

// The plugin's hooks module (#3872): it holds a connected Claude session's steering stream, wakes
// the session with a fixed pointer, and hands the frames to the model only as the `frames` tool's
// result. The stream is held as polls on the agent's frames route (#7838), which hands the module
// hold.py's frame lines and records the frames itself; where the agent is older than that route, or
// the workspace names no agent, hold.py's `--wake plugin` child holds it as before. Every other wire
// action is the Python client's; this module owns timing and delivery. It is an enhancement: with
// it absent, connect finds no fresh holder claim and the session keeps the model-run recipe.

const BUSY = { plugin: '2mw2lt', key: 'busy' } as const
const PENDING = { plugin: '2mw2lt', key: 'pending' } as const
const INFORM = { plugin: '2mw2lt', key: 'inform' } as const
const READ = { plugin: '2mw2lt', key: 'read' } as const
const POINTED = { plugin: '2mw2lt', key: 'pointed' } as const
const APPENDED = { plugin: '2mw2lt', key: 'appended' } as const
const WARNED = { plugin: '2mw2lt', key: 'warned' } as const
const POINT = { plugin: '2mw2lt', key: 'point' } as const
const SESSION = { plugin: '2mw2lt', key: 'session' } as const
const SEAT = { plugin: '2mw2lt', key: 'seat' } as const
const BULLETIN = { plugin: '2mw2lt', key: 'bulletin' } as const
const BULLETINS = { plugin: '2mw2lt', key: 'bulletins' } as const
const UNSETTLED = { plugin: '2mw2lt', key: 'unsettled' } as const
const TURN = { plugin: '2mw2lt', key: 'turn' } as const

const TOOL = 'mcp__2mw2lt__frames'
const CHECKPOINT_CALL = 'mcp__2mw2lt__checkpoint'
const SEAT_TOOL = new RegExp(`^mcp__2mw2lt__(${Object.keys(SEAT_TOOLS).join('|')})$`)
const TIMER_TOOL = new RegExp(`^mcp__2mw2lt__(${Object.keys(TIMER_TOOLS).join('|')})$`)
const REFRESH_MS = 60_000
const REPOINT_MS = 1500
const STDERR_TAIL = 2000
// How many frames calls a receipt that did not arrive is sent again on before it is let go: the
// daemon keeps an unread frame and wakes the session for it, so a lost one is never silent.
const RECEIPT_TRIES = 3

// Always this plugin version's own clients, never the launcher: it resolves the recorded install,
// which may be an older hold.py that refuses `--wake plugin`.
const script = ($: EngineInterface, name: string) => `${$.plugin.root}/steering/enroll/${name}`

// Every row the module writes goes through here, and its text is one of the fixed texts in
// frames.ts, a desk prompt's words under the from: line naming who said them, or the note of a
// stop or a command from the desk under that line: a frame's own text reaches the model only as the `frames`
// tool's result. Its row is shown here, with the desk's act that landed it, since the module's hook
// passes its own appends by. A refused append (a plugin above, or a run no plugin may shape) is
// logged, the hold goes on, and the refusal is returned.
async function append($: EngineInterface, text: string, act?: string): Promise<string | undefined> {
  $.ui.log(`2mw2lt: appends "${text}"`, { to: 'debug' })
  try {
    const stored = await $.session.append({ message: { type: 'user', content: [{ type: 'text', text }] } })
    const payload = stored.uuid === undefined ? undefined : surface.eventOf(surface.OWN_NOTE, stored, act)
    if (payload) show($, payload)
    return undefined
  } catch (err) {
    $.ui.log(`2mw2lt: the append was refused: ${String(err)}`, { to: 'debug' })
    return String(err)
  }
}

async function moduleVersion($: EngineInterface): Promise<string> {
  try {
    const manifest = JSON.parse(String(await $.fs.read(`${$.plugin.root}/.claude-plugin/plugin.json`)))
    return typeof manifest.version === 'string' ? manifest.version : ''
  } catch { return '' }
}

// Every child runs as this session: a process resumed from inside another session's environment
// inherits that session's CLAUDE_CODE_SESSION_ID, which the engine replaces for the Bash tool and
// not for a module's children (#7069).
const own = (ps: string) => ({ CLAUDE_CODE_SESSION_ID: ps })

async function runOwn($: EngineInterface, argv: string[], stdin?: string) {
  return $.process.run(argv, { cwd: root, env: own(await $.session.id()), ...(stdin !== undefined ? { stdin } : {}) })
}

// hold.py names where a provider session's claim lives; the path is asked once per id, so the
// release at `session.end` spends none of its 1.5 s on a child.
async function claimPath($: EngineInterface, id: string): Promise<string> {
  if (claimed?.id === id) return claimed.path
  const r = await runOwn($, ['python3', script($, 'hold.py'), '--claim-path', id])
  const path = r.exitCode === 0 ? r.stdout.trim() : ''
  if (path) claimed = { id, path }
  return path
}

// `$.fs` has no delete: a released claim is written with `at: 0`, which no reader takes as fresh.
async function claim($: EngineInterface, live: boolean, ps?: string): Promise<boolean> {
  const id = ps ?? await $.session.id()
  const path = await claimPath($, id)
  if (!path) return false
  const at = live ? (await $.clock.now()) / 1000 : 0
  await $.fs.write(path, JSON.stringify({ provider_session: id, module: version, at, ...(hold ? { hold } : {}) }))
  return true
}

// connect.py records when it bound this session (holder.connect_path): the hold starts from that
// record, read after each command and on each refresh, since a command that names connect through a
// variable or an alias never matches `invokes`. A record newer than the last start, else 0.
let connectedAt = 0
async function connected($: EngineInterface): Promise<number> {
  const path = await claimPath($, await $.session.id())
  if (!path) return 0
  let at = 0
  try { at = Number(String(await $.fs.read(path.replace(/\.json$/, '.connect')))) } catch { return 0 }
  return at > connectedAt ? at : 0
}

// Start the hold for a connect, and only then spend its record: a lookup that failed leaves it
// for the next command or refresh.
async function startConnected($: EngineInterface, at: number) {
  const session = await sessionOf($)
  if (!session) return
  if (at) connectedAt = Math.max(connectedAt, at)
  await start($, session)
}

// What the summariser keeps: rebrief.py's line, or the session alone when it gives none.
async function keepLine($: EngineInterface, session: string): Promise<string> {
  const r = await runOwn($, ['python3', script($, 'rebrief.py'), '--keep', session]).catch(() => undefined)
  return (r?.exitCode === 0 && r.stdout.trim()) || `Keep verbatim: steering session ${session}.`
}

// PENDING and POINTED are read and then written by the hold loop and by the `frames` tool, each
// across awaits; every such change runs in turn through this one chain. The engine answers a
// dispatch's $.state.get from what it held at that dispatch's first read, renewed only by that
// dispatch's own writes, so each section first renews it: a set whose ifVersion cannot match writes
// nothing and renews it, as update() reads again after losing a race. Without it a frames call
// wrote back a pending list without the frame the hold loop had added meanwhile (#7516).
let chain: Promise<unknown> = Promise.resolve()
function locked<T>($: EngineInterface, work: () => Promise<T>): Promise<T> {
  const section = async () => {
    await $.state.set(PENDING, [], { ifVersion: Number.MAX_SAFE_INTEGER })
    return work()
  }
  const run = chain.then(section, section)
  chain = run.catch(() => undefined)
  return run
}

// Called with the chain held. A pointer the session will not take as a prompt is appended
// instead, so the frame still reaches the model at its next request, and the turn that request
// belongs to is owed a pointer again if it ends unread.
async function point($: EngineInterface) {
  const { value: pointed = false } = await $.state.get(POINTED)
  if (pointed) return
  const { value: pending = [] } = await $.state.get(PENDING)
  const { value: inform = [] } = await $.state.get(INFORM)
  $.ui.log(`2mw2lt: pointer covers ${inform.filter(id => pending.includes(id)).length} inform`, { to: 'debug' })
  await $.state.set(POINTED, true)
  const appended = () => locked($, async () => {
    if ((await $.state.get(POINTED)).value) await $.state.set(APPENDED, true)
  })
  await $.state.set(APPENDED, await tell($, POINTER, appended))
}

// A pointer appended into a running turn is read only if that turn makes another request, and a
// turn about to end does not: the row stays unread, POINTED stays set, and no pointer is ever sent
// again. So a turn that ended with its pointer unanswered gets one as a prompt of its own,
// once the session is idle.
async function repoint($: EngineInterface) {
  await locked($, async () => {
    const appended = (await $.state.get(APPENDED)).value
    const pointed = (await $.state.get(POINTED)).value
    if (!appended && !pointed) return
    if ((await $.state.get(BUSY)).value) return            // the next turn's end asks again
    await unpoint($)
    // Nothing is owed a pointer once the module no longer holds the stream.
    if ((await $.state.get(SESSION)).value && (await pointingNow($, (await $.state.get(PENDING)).value ?? [])).length) await point($)
  })
}

// Called with the chain held, so returned frames cannot overwrite a newly arrived inform.
async function pointingNow($: EngineInterface, pending: readonly string[]): Promise<string[]> {
  const { value: inform = [] } = await $.state.get(INFORM)
  const held = inform.filter(id => pending.includes(id))
  if (held.length !== inform.length) await $.state.set(INFORM, held)
  $.ui.status(quiet(held.length))
  return pointing(pending, held)
}

// The pointer is no longer outstanding: the model read the frames, or the session starts over.
async function unpoint($: EngineInterface) {
  await $.state.set(POINTED, false)
  await $.state.set(APPENDED, false)
}

// A frame arriving while a client call is outstanding was added to PENDING while its prior pointer
// still stood. A refusal keeps the original batch untouched, but re-points that genuinely new row.
async function refuseFrames($: EngineInterface, pending: readonly string[]) {
  await locked($, async () => {
    await unpoint($)
    const { value: now = [] } = await $.state.get(PENDING)
    if ((await pointingNow($, now)).some(id => !pending.includes(id))) await point($)
  })
}

// A returned seat notice identifies one generation. When that exact generation has gone stale,
// remembering its id as read prevents its replay from blocking the ordinary frames beside it.
async function retireStaleSeatFrame($: EngineInterface, seatFrameId: string) {
  await locked($, async () => {
    const { value: now = [] } = await $.state.get(PENDING)
    const left = now.filter(id => id !== seatFrameId)
    await $.state.set(PENDING, left)
    const { value: read = [] } = await $.state.get(READ)
    if (!read.includes(seatFrameId)) await $.state.set(READ, [...read, seatFrameId].slice(-READ_KEPT))
    await unpoint($)
    if ((await pointingNow($, left)).length) await point($)
  })
}

// A fixed text the model must read: a turn of its own on an idle session, a row in the running
// turn on a busy one. A prompt the session will not take is appended instead, to be read at its
// next request, and `appended` runs. Whether it went into a running turn.
async function tell($: EngineInterface, text: string, appended?: () => Promise<void>): Promise<boolean> {
  const { value: busy = false } = await $.state.get(BUSY)
  if (busy) {
    await append($, text)
    return true
  }
  // Never awaited: a hook the turn waits on hangs awaiting its own submit.
  void submit($, text)
    .then(l => 'landed' in l && l.landed === 'new-turn' ? undefined : appended?.())
    .catch(() => undefined)
  return false
}

// Words submitted as a turn of their own, and where they landed. The engine refuses by rejecting, or
// by resolving `drop`, as every submit does after the person's Esc until they type again (#7827):
// the words are then appended, to be read at the session's next request, and why is said. A desk
// prompt's words (its act) are submitted as the user's, so the model reads them from their from:
// line rather than under the engine's frame of a plugin's message (spike A); the terminal still
// labels the turn the plugin's. The submitted row is shown by the module's hook as it is stored,
// before the submit resolves, under the act its words were submitted with.
async function submit($: EngineInterface, text: string, act?: string): Promise<surface.Landing> {
  let why: string
  const waiting = act === undefined ? undefined : { text, act }
  if (waiting) submitted.push(waiting)
  try {
    const r = await $.prompt.submit(act === undefined ? { text } : { text, asUser: true })
    if (!(r && 'drop' in r)) return { landed: 'new-turn' }
    why = String(r.drop)
  } catch (err) {
    why = String(err)
  } finally {
    // Its row is stored before the submit settles, so words still waiting now were never stored.
    if (waiting && submitted.includes(waiting)) submitted.splice(submitted.indexOf(waiting), 1)
  }
  $.ui.log(`2mw2lt: the prompt was refused: ${why}`, { to: 'debug' })
  const failed = await append($, text, act)
  return failed === undefined ? { landed: 'queued', why } : { refused: `the session took it neither as a prompt (${why}) nor as a row (${failed})` }
}

// The desk prompts submitted and not yet stored, oldest first: a submit names no row, so the row the
// hook sees stored with the same words is the one their act landed.
const submitted: { text: string; act: string }[] = []

// The act that submitted text, taken from those waiting.
function actOf(text: string): string | undefined {
  const at = submitted.findIndex(s => s.text === text)
  return at < 0 ? undefined : submitted.splice(at, 1)[0]!.act
}

// A desk prompt (#4823 unit 2.2), landed as the module's own pointer is — submitted on an idle
// session, appended to a running turn — and its landing reported to the agent, whose receipt
// settles the act. A prompt the agent could not hand over whole is refused, never guessed at.
async function land($: EngineInterface, session: string, body: unknown) {
  const p = surface.prompted(body)
  let landing: surface.Landing
  if ('refused' in p) landing = { refused: p.refused }
  else if ((await $.state.get(BUSY)).value) {
    const failed = await append($, p.text, p.id)
    landing = failed === undefined ? { landed: 'running-turn' } : { refused: `the running turn did not take it as a row (${failed})` }
  } else landing = await submit($, p.text, p.id)
  if (!p.id) {
    $.ui.log(`2mw2lt: the agent's prompt was not taken, and names no id to say so under: ${'refused' in landing ? landing.refused : ''}`, { to: 'debug' })
    return
  }
  await report($, 'landed', session, p.id, landing)
}

// What the module did with one of the agent's commands, told to the agent's route for its kind,
// whose receipt settles the desk's act.
async function report($: EngineInterface, route: string, session: string, id: string, outcome: object) {
  try {
    const at = agent
    if (!at) throw new Error('no agent to report to')
    const r = await $.http.fetch(`${at.base}/steering/surface/${route}`,
      { method: 'POST', headers: agentHeaders(at), body: JSON.stringify({ session, producer: await $.session.id(), id, ...outcome }) })
    if (!r.ok) throw new Error(`the agent answered ${r.status}: ${r.text.slice(0, 200)}`)
    return true
  } catch (err) {
    $.ui.log(`2mw2lt: the desk's act ${id} was not reported ${route}: ${String(err)}`, { to: 'debug' })
    return false
  }
}

// Unlike live-view batches, command receipts survive a failed send and a module reload.
const COMMAND_RECEIPTS = { plugin: '2mw2lt', key: 'commandReceipts' } as const
let commandSending: Promise<void> | undefined
let commandRetry: Timer | undefined

async function reportCommand($: EngineInterface, session: string, id: string, outcome: surface.Ran) {
  await locked($, async () => {
    const { value: pending = [] } = await $.state.get(COMMAND_RECEIPTS)
    await $.state.set(COMMAND_RECEIPTS, [...pending, { session, id, outcome }])
  })
  await sendCommandReceipts($)
}

function sendCommandReceipts($: EngineInterface): Promise<void> {
  if (commandSending) return commandSending
  commandSending = (async () => {
    const failed = new Set<string>()
    while (true) {
      const receipt: CommandReceipt | undefined = await locked($, async () =>
        (await $.state.get(COMMAND_RECEIPTS)).value?.find(r => !failed.has(r.id)))
      if (!receipt) return
      const workspace = workspaceOf()
      if (!agent && workspace) agent = await agentOf($, workspace)
      if (!await report($, 'ran', receipt.session, receipt.id, receipt.outcome)) {
        failed.add(receipt.id)
        continue
      }
      await locked($, async () => {
        const { value: pending = [] } = await $.state.get(COMMAND_RECEIPTS)
        const at = pending.findIndex(r => r.id === receipt.id)
        await $.state.set(COMMAND_RECEIPTS, [...pending.slice(0, at), ...pending.slice(at + 1)])
      })
    }
  })().finally(async () => {
    commandSending = undefined
    const pending = await locked($, async () => (await $.state.get(COMMAND_RECEIPTS)).value?.length)
    if (pending && !commandRetry) commandRetry = $.clock.after(RETRY_CAP_MS, () => {
      commandRetry = undefined
      void sendCommandReceipts($)
    })
  })
  return commandSending
}

// The turn a desk interrupt is stopping: the background tasks its abort left running, gathered as
// their calls return, and its end, by which every call of it has.
let stopping: { turn: string; moved: string[]; ended: () => void } | undefined
// How long an interrupt waits for the aborted turn's end: measured at about 100 ms (#4823).
const STOP_WAIT_MS = 3000

// A desk interrupt (#4823 unit 2.3, design §6.1), reported to the agent, whose receipt settles the act.
async function interrupt($: EngineInterface, session: string, body: unknown) {
  const p = surface.interrupting(body)
  const { value: turn = '' } = await $.state.get(TURN)
  const outcome: surface.Stopping = 'refused' in p ? { refused: p.refused }
    : !turn ? { refused: 'no turn of the session is running' }
    : stopping ? { refused: 'another interrupt is stopping the turn' }
    : await stopTurn($, turn, p.from)
  if (!p.id) {
    $.ui.log(`2mw2lt: the agent's interrupt was not taken, and names no id to say so under: ${'refused' in outcome ? outcome.refused : ''}`, { to: 'debug' })
    return
  }
  await report($, 'stopped', session, p.id, outcome)
}

// The running turn aborted; each command the abort moved to the background ended, as the person's
// Esc ends it and the abort does not; and the model told, since an abort leaves it no marker. A
// command the model ran in the background on purpose goes on, as it does past Esc.
async function stopTurn($: EngineInterface, turn: string, from: string): Promise<surface.Stopping> {
  const s = { turn, moved: [] as string[], ended: () => {} }
  let over = false
  const ended = new Promise<void>(resolve => { s.ended = () => { over = true; resolve() } })
  stopping = s
  try {
    await $.turn.abort({ turnId: turn })
    await Promise.race([ended, $.clock.sleep(STOP_WAIT_MS)])
  } catch (err) {
    return { refused: `the engine did not abort the turn: ${String(err)}` }
  } finally {
    if (stopping === s) stopping = undefined
  }
  const stopped: string[] = []
  const unstopped: string[] = []
  // A call still returning past the wait is not seen, so what it moved is not known to be ended.
  const unseen = !over
  for (const id of s.moved) {
    const why = await $.tool.call({ tool: 'TaskStop', task_id: id })
      .then(r => r.deny ?? (r.isError ? r.text ?? 'it failed' : undefined), err => String(err))
    if (why === undefined) stopped.push(id)
    else unstopped.push(`${id}: ${why}`)
  }
  const failed = await append($, surface.interruptNote(from, stopped.length, unstopped.length, unseen))
  return { interrupted: true, stopped, unstopped, unseen, told: failed === undefined }
}

// The desk's commands running (#8432), oldest first. Each holds its note until the engine dispatches
// its command, which this module's prompt.submit hook sees under its own origin: a note appended
// before the run is read by a busy session's running turn instead (U0, finding 3).
type Running = { act: string; session: string; name: string; note: string; fired: boolean }
const runs: Running[] = []
// The acts whose command was dispatched and whose echo row is not stored yet, oldest first.
const echoing: { act: string; session: string; name: string }[] = []
const ECHOING_KEPT = 8
// The act whose turn's rows are stored now: from its command's echo row to that turn's end.
let stamping: { act: string; session: string } | undefined
// Each command's class, as the agent was last told.
let classes = new Map<string, surface.Class>()

// A desk command (#8432), run through the engine's own command route and reported to the agent,
// whose receipt settles the act. A busy session queues the run until its turn ends, so that is
// reported at once. A prompt command resolves at dispatch, before its turn; a local one when it has
// run, with what it resolved to (U0, finding 1).
async function runCommand($: EngineInterface, session: string, body: unknown) {
  const r = surface.running(body)
  if ('refused' in r) {
    if (r.id) await reportCommand($, session, r.id, { refused: r.refused })
    else $.ui.log(`2mw2lt: the agent's command was not taken, and names no id to say so under: ${r.refused}`, { to: 'debug' })
    return
  }
  const run: Running = { act: r.id, session, name: r.name, note: surface.commandNote(r.from, r.name, r.args), fired: false }
  runs.push(run)
  const busy = (await $.state.get(BUSY)).value === true
  if (busy) await reportCommand($, session, r.id, { landed: 'queued', why: 'the session is running a turn; the command runs when it ends' })
  let ran: surface.Ran
  try {
    const out = await $.command.run({ command: r.name, args: r.args })
    if (run.fired || classes.get(r.name) === 'prompt') ran = { landed: 'new-turn' }
    else {
      const result = surface.ranResult(out)
      ran = { landed: 'ran', ...(result === undefined ? {} : { result }) }
    }
  } catch (err) {
    ran = { refused: `the engine refused /${r.name}: ${String(err)}` }
  } finally {
    runs.splice(runs.indexOf(run), 1)       // a note its dispatch did not append is never read
  }
  await reportCommand($, session, r.id, ran)
}

// The live view's channel to this machine's agent (#4823, design §6.1): `$.http.fetch` alone, one
// ordered send queue out. While nobody watches, nothing is made or
// sent; rows leave whole, and the agent redacts them.
const SURFACE_SEQ = { plugin: '2mw2lt', key: 'surfaceSeq' } as const
const BATCH_MS = 5
const RETRY_MS = 250
const RETRY_CAP_MS = 5000
const outbox = new surface.Outbox()
// A burst of thinking sent per chunk fell behind (spike A): a step's text and thinking leave at most
// this often, and at the step's end.
const STREAM_MS = 50
const streaming = new surface.Streaming()
let watching = false
// Whether a turn runs, as the turn hooks last said, before their awaits: a watch beginning reads it
// without one, so a turn starting meanwhile is not told over. Unknown until one says, after a reload.
let busyNow: boolean | undefined
let watched = ''                     // the session the channel follows: what its events are sent as
let sending: Promise<void> = Promise.resolve()
let agent: { base: string; key: string; workspace: string } | undefined
let following = 0
let surfaceSaid = ''
// The command list the agent was last told, as sent: told again when it changes. Owed once a poll
// stands, after a follow or a failed poll, since the agent may be a new one that keeps none.
let listSent = ''
let listOwed = true
let listing = false
let listSaid = ''

// A refusal on the channel is logged once until it changes or the channel recovers.
function surfaceRefused($: EngineInterface, why: string) {
  if (why === surfaceSaid) return
  surfaceSaid = why
  $.ui.log(`2mw2lt: ${why}`, { to: 'debug' })
}

async function agentOf($: EngineInterface, workspace: string) {
  const record = await $.fs.read(`${workspace}/.env`).then(String, () => '')
  const env = async (read: () => Promise<string | undefined>) => read().catch(() => undefined)
  const home = await env(() => $.env.get('HOME')) ?? ''
  const { port, keyFiles } = surface.agentOf(record, {
    port: await env(() => $.env.get('STEERING_AGENT_PORT')),
    xdg: await env(() => $.env.get('XDG_CONFIG_HOME')),
    home,
  })
  for (const file of keyFiles) {
    const key = await $.fs.read(file).then(t => String(t).trim(), () => '')
    // The hold's poll rides this agent too (#7838), so it carries what its frames route asks
    // beyond the key: the harness's configuration directory, under which the agent reads the
    // session's own registry, and the project this session runs in, which a hand-over names.
    if (key) return { base: `http://127.0.0.1:${port}`, key, workspace,
      config: await env(() => $.env.get('CLAUDE_CONFIG_DIR')) || (home ? `${home}/.claude` : ''),
      cwd: await $.session.root() }
  }
  return undefined
}

const agentHeaders = (a: { key: string; workspace: string }) =>
  ({ 'content-type': 'application/json', 'X-Steering-Agent-Key': a.key, 'X-Steering-Workspace': surface.workspaceHeader(a.workspace) })

function show($: EngineInterface, payload: surface.Payload) {
  if (!watching || !outbox.add(watched, payload)) return
  $.clock.after(BATCH_MS, () => { sendBatch($) })
}

// One batch at a time, in order, until the queue is empty. A batch the agent did not take is gone,
// and that session's next batch opens with a gap over it, an earlier gap's range included.
function sendBatch($: EngineInterface) {
  sending = sending.then(async () => {
    for (let batch = outbox.take(); batch.length; batch = outbox.take()) {
      const first = batch[0]!
      try {
        await $.state.set(SURFACE_SEQ, batch[batch.length - 1]!.seq)
        const at = agent
        if (!at) throw new Error('no agent to send to')
        // The producer is this conversation, whose state keeps the seq: another conversation of the
        // session's name numbers afresh, and the door tells the two numberings apart by it.
        const producer = await $.session.id()
        const r = await $.http.fetch(`${at.base}/steering/surface/events`,
          { method: 'POST', headers: agentHeaders(at), body: JSON.stringify({ session: first.session, producer, events: batch }) })
        if (!r.ok) throw new Error(`the agent answered ${r.status}: ${r.text.slice(0, 200)}`)
      } catch (err) {
        surfaceRefused($, `the live view's events were not taken: ${String(err)}`)
        outbox.failed(batch)
      }
    }
  }).catch(err => { surfaceRefused($, `the live view's send broke: ${String(err)}`) })
}

// Asks the agent whether anyone watches this session, until unfollowed.
async function pollSurface($: EngineInterface, workspace: string, session: string, gen: number) {
  if (outbox.seq === undefined) outbox.resume((await $.state.get(SURFACE_SEQ)).value ?? 0)
  let cursor = ''
  let wait = RETRY_MS
  while (gen === following) {
    try {
      // Resolved into a local and installed only while this poll still stands, so a follower made
      // obsolete while it asked installs nothing over a newer one's agent.
      const resolved = await agentOf($, workspace)
      if (gen !== following) return
      agent ??= resolved
      // An agent not yet started is retried as one that stopped: its key appears when it starts.
      if (!agent) throw new Error(`no key for the agent serving ${workspace}`)
      void sendCommandReceipts($)
      // The producer, as the events name it: the agent advertises this session's rows only while
      // the conversation its hold pins is the one polling.
      const producer = await $.session.id()
      const r = await $.http.fetch(`${agent.base}/steering/surface/poll?session=${encodeURIComponent(session)}&producer=${encodeURIComponent(producer)}&after=${encodeURIComponent(cursor)}`,
        { headers: agentHeaders(agent) })
      if (gen !== following) return
      if (!r.ok) throw new Error(`the agent answered ${r.status}: ${r.text.slice(0, 200)}`)
      const answer = JSON.parse(r.text) as { watch?: unknown; cursor?: unknown; commands?: unknown }
      const was = watching
      watching = answer.watch === true
      // A watch beginning has no word of the turn until the next one starts or ends: it is told
      // the turn as it stands.
      if (watching && !was) {
        const stored = busyNow ?? (await $.state.get(BUSY)).value === true
        show($, surface.turnNow(busyNow ?? stored))
      }
      if (typeof answer.cursor === 'string') cursor = answer.cursor
      if (listOwed) {
        listOwed = false
        void tellCommands($, session)
      }
      for (const c of Array.isArray(answer.commands) ? answer.commands as surface.Command[] : []) command($, session, c)
      wait = RETRY_MS
      surfaceSaid = ''
    } catch (err) {
      if (gen !== following) return
      surfaceRefused($, `the live view's poll failed: ${String(err)}`)
      listOwed = true
      if (watching && outbox.seq !== undefined) outbox.lost(session, outbox.seq)   // rows go unseen until it answers
      watching = false
      agent = undefined                  // read again: a restarted agent may hold a new key
      await new Promise<void>(resolve => { $.clock.after(wait, resolve) })
      wait = Math.min(wait * 2, RETRY_CAP_MS)
    }
  }
}

// The session's slash commands, classed, told the agent when they differ from what it was last told.
// A refusal is said once until it changes, and the next turn's end tries again.
async function tellCommands($: EngineInterface, session: string) {
  if (listing || !session) return
  listing = true
  try {
    const list = surface.classified(await $.command.list())
    classes = new Map(list.map(c => [c.name, c.class]))
    const text = JSON.stringify(list)
    if (text === listSent) return
    const at = agent
    if (!at) throw new Error('no agent to tell')
    const r = await $.http.fetch(`${at.base}/steering/surface/commands`,
      { method: 'POST', headers: agentHeaders(at), body: JSON.stringify({ session, producer: await $.session.id(), commands: list }) })
    if (!r.ok) throw new Error(`the agent answered ${r.status}: ${r.text.slice(0, 200)}`)
    listSent = text
    listSaid = ''
  } catch (err) {
    listSent = ''
    const why = `the session's commands were not told the agent: ${String(err)}`
    if (why !== listSaid) $.ui.log(`2mw2lt: ${why}`, { to: 'debug' })
    listSaid = why
  } finally {
    listing = false
  }
}

// One of the agent's commands, by its kind. A kind this module does not know is a newer agent's: it
// is ignored, and said.
function command($: EngineInterface, session: string, c: surface.Command) {
  switch (c?.kind) {
    case 'prompt':
      void land($, session, c.body).catch(err => $.ui.log(`2mw2lt: the desk's prompt broke: ${String(err)}`, { to: 'debug' }))
      return
    case 'interrupt':
      void interrupt($, session, c.body).catch(err => $.ui.log(`2mw2lt: the desk's interrupt broke: ${String(err)}`, { to: 'debug' }))
      return
    case 'command':
      void runCommand($, session, c.body).catch(err => $.ui.log(`2mw2lt: the desk's command broke: ${String(err)}`, { to: 'debug' }))
      return
    case 'emit': {
      const payload = surface.emitted(c.body)
      if (typeof payload === 'string') $.ui.log(`2mw2lt: the agent's emit was not taken: ${payload}`, { to: 'debug' })
      else show($, payload)
      return
    }
    default:
      $.ui.log(`2mw2lt: ignored the agent's command of kind ${String(JSON.stringify(c?.kind ?? null)).slice(0, 40)}, which this module does not know`, { to: 'debug' })
  }
}

// Every change of the session's identity goes through here or `unfollow`: the old name's watch says
// nothing about the new one, so nothing is shown until the agent answers for the new name, from an
// empty cursor.
function follow($: EngineInterface, workspace: string, session: string) {
  const gen = ++following
  watching = false
  watched = session
  listSent = ''
  listOwed = true
  void pollSurface($, workspace, session, gen).catch(err => $.ui.log(`2mw2lt: the live view's poll ended: ${String(err)}`, { to: 'debug' }))
}

function unfollow() {
  following++
  watching = false
  watched = ''
}

// The session is known by another name from now on, or by none. If its rows were being shown, what
// it makes before the new name is watched goes unseen, so the new name's first batch opens with a gap.
function rename($: EngineInterface, session: string) {
  if (!watched || session === watched) return
  if (!session || !workspaceOf()) { unfollow(); return }
  if (watching && outbox.seq !== undefined) outbox.lost(session, outbox.seq)
  follow($, workspaceOf()!, session)
}

// Every write of the session's name goes through here, so the live view moves with it at once.
async function setSession($: EngineInterface, session: string) {
  await $.state.set(SESSION, session)
  rename($, session)
}

// The live view follows the workspace the claim is in: the common root the agent serves.
const workspaceOf = () => claimed?.path.match(/^(.*)\/\.claude\/steering-holders\/[^/]+$/)?.[1]

// The token count the engine auto-compacts at, read from its own breakdown; the window where it
// has auto-compaction off. A model it does not know gets the engine's default window, and the log
// says so, since nothing here can tell what that model's real limit is.
async function compactionPoint($: EngineInterface): Promise<number | undefined> {
  const { context } = await $.session.usage({ breakdown: 'summary' })
  const b = context.breakdown
  if (!b) return undefined
  if (b.autocompactSource === 'unknown-model' && named !== b.model) {
    named = b.model
    $.ui.log(`2mw2lt: the engine does not know the context window of ${b.model}; the checkpoint prompt follows its assumed auto-compact point of ${b.autoCompactThreshold ?? b.rawMaxTokens} tokens. Set CLAUDE_CODE_AUTO_COMPACT_WINDOW to the model's real window.`, { to: 'debug' })
  }
  const point = b.autoCompactThreshold ?? b.rawMaxTokens
  await $.state.set(POINT, point)
  return point
}

// Once per window, to a connected session, when it is 85% of the way to compaction. The point is
// read afresh at a turn's end, where a setting or the model may have moved it, and from the last
// reading at a step, which is one model request and must not cost a breakdown.
async function checkpointIfDue($: EngineInterface, tokens: number | undefined, fresh: boolean) {
  // A step is one model request: an unconnected or already-warned session leaves before the lock.
  const owed = async () => (await $.state.get(WARNED)).value !== true && !!(await $.state.get(SESSION)).value
  if (!root || tokens === undefined || !(await owed())) return
  await locked($, async () => {
    if (!(await owed())) return
    const point = (fresh ? undefined : (await $.state.get(POINT)).value) ?? await compactionPoint($)
    if (!due(tokens, point)) return
    await $.state.set(WARNED, true)
    await append($, CHECKPOINT)
  })
}

// Whether this session holds the seat, and the rulings it then reads in its system prompt. Only a
// connected session asks: seat_section.py reads on its enrolment. `refresh` lets the text itself
// change; without it a holder keeps the bytes it had, and only taking or losing the seat moves them.
// Asks run one after another, so an older answer never lands over a newer one, and an answer that
// arrives after the session disconnected is dropped.
let seatChain: Promise<unknown> = Promise.resolve()
function refreshSeat($: EngineInterface, refresh: boolean): Promise<void> {
  const run = seatChain.then(async () => {
    if (!root || !(await $.state.get(SESSION)).value) return
    // The bulletin reads beside the seat's own run, not after it: the two write independent keys,
    // and a door that is down should cost the chain one retry budget, not two.
    const beside = refresh ? refreshBulletin($) : undefined
    const r = await runOwn($,
      ['python3', script($, 'seat_section.py'), '--provider', 'claude', '--provider-session', await $.session.id()])
    if (r.exitCode !== 0 && r.exitCode !== 1) $.ui.log(`2mw2lt: seat_section.py exited ${r.exitCode}: ${r.stderr.trim()}`, { to: 'debug' })
    if (!(await $.state.get(SESSION)).value) return
    const { value: was = '' } = await $.state.get(SEAT)
    const next = nextSeat(was, r.exitCode, r.stdout, refresh)
    if (next !== was) await $.state.set(SEAT, next)
    if (beside) await beside
  })
  seatChain = run.catch(() => undefined)
  return run
}

async function refreshBulletin($: EngineInterface): Promise<void> {
  // A connected teammate renders no section, so its refresh must not mark one shown either: the
  // mark would keep the changes from every process on this enrolment (plan unit 7b).
  if (teammate) return
  const r = await runOwn($,
    ['python3', script($, 'bulletin.py'), '--provider', 'claude', '--provider-session', await $.session.id(), 'section'])
  if (r.exitCode !== 0 && r.exitCode !== 1) $.ui.log(`2mw2lt: bulletin.py section exited ${r.exitCode}: ${r.stderr.trim()}`, { to: 'debug' })
  // An answer that arrives after the session disconnected is dropped, as the seat's is; and any
  // exit but 0 and 1 keeps the bytes it had — a door that cannot tell, or a child that broke, is
  // never a reason to drop the lines.
  if (!(await $.state.get(SESSION)).value) return
  if (r.exitCode !== 0 && r.exitCode !== 1) return
  const { value: was = '' } = await $.state.get(BULLETIN)
  const next = r.exitCode === 0 ? r.stdout.trim() : ''
  if (next !== was) await $.state.set(BULLETIN, next)
  // The fresh section holds every change older than the head it was rendered at: appending them
  // again would bill the same changes twice over a render that already spent the cache.
  const rendered = r.exitCode === 0 ? sectionRevision(next) : undefined
  if (rendered !== undefined) await dropHeldBulletins($, rendered)
}

// The revision each pending bulletin frame was rendered at, read from the recording the frames
// tool reads. A frame the recording no longer holds answers nothing, so its caller keeps it.
async function bulletinRevisions($: EngineInterface): Promise<Map<string, number>> {
  const out = new Map<string, number>()
  const { value: session = '' } = await $.state.get(SESSION)
  if (!root || !session) return out
  const at = await framePath($, session)
  if (!at) return out
  const recording = String(await $.fs.read(at).catch(() => ''))
  for (const raw of recording.split('\n')) {
    if (!raw.startsWith('data: ')) continue
    let f: { kind?: unknown; id?: unknown; revision?: unknown }
    try { f = JSON.parse(raw.slice(6)) } catch { continue }
    if (f && f.kind === 'bulletin' && typeof f.id === 'string' && typeof f.revision === 'number' && Number.isSafeInteger(f.revision)) {
      out.set(f.id, f.revision)
    }
  }
  return out
}

async function dropHeldBulletins($: EngineInterface, rendered: number) {
  await locked($, async () => {
    const { value: ids = [] } = await $.state.get(BULLETINS)
    if (!ids.length) return
    const revisions = await bulletinRevisions($)
    const left = ids.filter(id => (revisions.get(id) ?? Number.POSITIVE_INFINITY) > rendered)
    if (left.length === ids.length) return
    await $.state.set(BULLETINS, left)
  })
}

const askSeat = ($: EngineInterface, refresh: boolean) =>
  void refreshSeat($, refresh).catch(err => $.ui.log(`2mw2lt: the seat check broke: ${String(err)}`, { to: 'debug' }))

// The bulletin's changes reach the model as one message appended into the turn that just started
// (plan unit 7b): an idle session is spent nothing, and a working one reads them at its next
// turn. What the recording still holds is read; an id it has lost stays pending, as a waking
// frame's does.
async function tellBulletins($: EngineInterface) {
  if (!root) return
  const { value: ids = [] } = await $.state.get(BULLETINS)
  if (!ids.length) return
  const { value: session = '' } = await $.state.get(SESSION)
  let recording = ''
  if (session) {
    const at = await framePath($, session)
    if (at) recording = String(await $.fs.read(at).catch(() => ''))
  }
  const { bulletins, found } = pick(recording, ids)
  if (bulletins.length) await append($, capBulletin(bulletins))
  await locked($, async () => {
    const { value: now = [] } = await $.state.get(BULLETINS)
    const left = now.filter(id => !found.includes(id))
    await $.state.set(BULLETINS, left)
    const { value: read = [] } = await $.state.get(READ)
    const added = found.filter(id => !read.includes(id))
    if (added.length) await $.state.set(READ, [...read, ...added].slice(-READ_KEPT))
  })
}

// A kick or a say asks only a holder: it is how the daemon tells one the seat was taken. A session
// that does not hold the seat is seated by a seat frame or promote.py, never by either.
async function askHolder($: EngineInterface) {
  if ((await $.state.get(SEAT)).value) askSeat($, false)
}

// One seat verb, through its Python client. A send whose
// answer was lost goes once more under the id the client printed. A client that did not answer
// with success is the call's error, in its own words.
async function runSeatCall($: EngineInterface, call: SeatCall): Promise<{ result: string } | { deny: string }> {
  const speaker = ['--provider', 'claude', '--provider-session', await $.session.id()]
  if (call.client === 'card') return answer(await runOwn($, ['python3', script($, 'card.py'), '--lease', ...speaker, ...call.argv]))
  const argv = ['python3', script($, 'lease.py'), ...speaker,
    ...(call.client === 'say' ? ['say'] : call.client === 'reply' ? ['post', '/steering/brain/reply'] : ['post', call.path])]
  return answer(call.client === 'post' ? await runOwn($, argv, call.stdin) : await resending($, argv, call.stdin))
}

const answer = (r: { exitCode: number; stdout: string; stderr: string }): { result: string } | { deny: string } => {
  const words = [r.stdout.trim(), r.stderr.trim()].filter(Boolean).join('\n') || `the client exited ${r.exitCode} and said nothing`
  return r.exitCode === 0 ? { result: words } : { deny: words }
}

async function resending($: EngineInterface, argv: string[], stdin?: string) {
  const r = await runOwn($, argv, stdin)
  const again = resendId(r.stdout)
  return again ? runOwn($, [...argv, `--retry=${again}`], stdin) : r
}

// The checkpoint tool's call, through checkpoint.py: the text on stdin as the model gave it, and
// the lessons through a file of their own, since stdin is the text's. `$.fs` has no delete, so the
// file is made and removed by the host's own mktemp and rm.
async function runCheckpoint($: EngineInterface, args: Record<string, unknown>): Promise<{ result: string } | { deny: string }> {
  const call = checkpointCall(args)
  if ('refused' in call) return { deny: `refused: ${call.refused}` }
  const session = await sessionOf($)
  if (!session) return { deny: 'refused: this session is not connected to steering: run /2mw2lt:connect first.' }
  let learned = ''
  if (call.learned !== undefined) {
    const made = await runOwn($, ['mktemp'])
    learned = made.exitCode === 0 ? made.stdout.trim() : ''
    if (!learned) return { deny: `refused: no file could be made for the lessons: mktemp exited ${made.exitCode}: ${made.stderr.trim()}` }
  }
  try {
    if (learned) {
      const why = await $.fs.write(learned, call.learned!).then(() => null, (err: unknown) => String(err))
      if (why !== null) {
        $.ui.log(`2mw2lt: the checkpoint's lessons file could not be written: ${why}`, { to: 'debug' })
        return { deny: 'refused: the lessons could not be written to a file for checkpoint.py; nothing was sent.' }
      }
    }
    return answer(await resending($, ['python3', script($, 'checkpoint.py'), session, call.boundary,
      ...(learned ? ['--learned-file', learned] : []), ...(call.mirror !== undefined ? ['--mirror', call.mirror] : [])], call.text))
  } finally {
    if (learned) await runOwn($, ['rm', '-f', learned]).catch(() => undefined)
  }
}

async function sessionOf($: EngineInterface, session?: string): Promise<string | null> {
  const r = await runOwn($, ['python3', script($, 'hold.py'), '--session-of', await $.session.id(), ...(session ? [session] : [])])
  if (r.exitCode !== 0) {
    $.ui.log(`2mw2lt: the enrolment lookup exited ${r.exitCode}: ${r.stderr.trim()}`, { to: 'debug' })
    return null
  }
  return r.stdout.trim()
}

// Module variables start over on a reload, and so does everything they hold: the reload kills
// the child and drops the timers, and `session.start` runs again. Facts that must survive a
// reload are in `$.state`. `root` is the session's root, where every client runs so that Python
// resolves the workspace itself; it is set only above the version floor.
let root = ''
let claimed: { id: string; path: string } | undefined
let version = ''
let generation = 0
let refresh: Timer | undefined
let child: HookStream<ProcessSpawnChunk, ProcessSpawnResult> | undefined
// The plugin's own account of the hold, written into its claim for the session to read (#5641):
// holding only once coordination admitted it, retrying with the last refusal, or stopped.
let hold: Record<string, unknown> | undefined
// The model whose window the engine did not recognise, once named in the log.
let named = ''
// Whether the last prompt composition this process rendered was a teammate's. A connected
// teammate renders no bulletin section, so its refresh must not mark one shown either: the mark
// would keep the changes from every process on this enrolment (plan unit 7b). Unknown until the
// first render, and a module variable because the trait reaches only `prompt.compose`.
let teammate: boolean | undefined
// The hold the agent admitted to this module's polls, to let go of when the module stops holding.
let holdId = ''
let holdName = ''
// Where the agent records the frames it hands this module, which the `frames` tool reads: named
// by the agent with the hold, else asked of hold.py for each read.
let framesPath = ''

// What one arm of the hold ended with: the child's exit code, or the poll's own; whether a frame
// woke the session; the end of what was said about it; the refusal, if one ended it; and the
// session coordination admitted where the enrolment reconciled a retained name (#5661).
type Ended = { code: number | null; woke: boolean; said: string; refused?: Line; session?: string }

// The poll's own codes, where the child's are hold.py's: a hold the agent could not serve just
// then is held again as a child that exited is.
const POLL_UNSERVED = 2

// Stop the arm that holds, and answer the hold it held at the agent, for the caller to let go of.
function stop(): string {
  generation++
  unfollow()
  void child?.return(undefined as never).catch(() => undefined)
  child = undefined
  const held = holdId
  holdId = ''
  return held
}

async function framePath($: EngineInterface, session: string): Promise<string> {
  if (framesPath) return framesPath
  const at = await runOwn($, ['python3', script($, 'hold.py'), '--frame-path', session]).catch(() => undefined)
  return at?.exitCode === 0 ? at.stdout.trim() : ''
}

// The hold's poll rides the agent the live view's channel resolved (#7838), whose facts carry what
// the frames route asks beyond the key and workspace: the harness's configuration directory, where
// the agent reads the session's own registry, and the project this session runs in, which a
// hand-over's targeting names. The agent has no other way to know either.
const framesRoute = (a: NonNullable<typeof agent>, session: string, held: string) =>
  `${a.base}/steering/session/${encodeURIComponent(session)}/frames${held ? `?hold=${encodeURIComponent(held)}` : ''}`

const holdHeaders = (a: NonNullable<typeof agent>, ps: string) => ({
  'X-Steering-Agent-Key': a.key, 'X-Steering-Workspace': surface.workspaceHeader(a.workspace),
  'X-Steering-Provider': 'claude', 'X-Steering-Provider-Session': ps, 'X-Steering-Wake': 'plugin',
  ...(a.config ? { 'X-Steering-Config-Dir': encodeURIComponent(a.config) } : {}),
  ...(a.cwd ? { 'X-Steering-Project': encodeURIComponent(a.cwd) } : {}),
})

// Let the agent go of a hold this module no longer polls: at the session's end, a disconnect, or a
// start that supersedes the arm. Without it the agent keeps the hold its grace long.
async function letGo($: EngineInterface, session: string, held: string) {
  const a = agent
  if (a && held) await releaseHold($, a, session, held)
}

async function releaseHold($: EngineInterface, a: NonNullable<typeof agent>, session: string, held: string) {
  try {
    const r = await $.http.fetch(framesRoute(a, session, held), { method: 'DELETE', headers: holdHeaders(a, await $.session.id()) })
    if (r.status !== 204) $.ui.log(`2mw2lt: letting go of hold ${held} was answered ${r.status}`, { to: 'debug' })
  } catch (err) {
    $.ui.log(`2mw2lt: the hold ${held} was not let go: ${String(err)}`, { to: 'debug' })
  }
}

// One frame line as hold.py prints it, from the child or from the agent's poll (#7838). Answers
// the session coordination admitted where a `held` line names another, else nothing.
async function onLine($: EngineInterface, l: Line, ps: string, session: string, arm: { woke: boolean; refused?: Line }): Promise<string | undefined> {
  if (l.kind === 'held') {
    hold = { state: 'holding', hold: l.hold ?? null, since: (await $.clock.now()) / 1000 }
    await claim($, true, ps).catch(() => undefined)
    // A hold replacement can follow a pointer submission that never became a turn: the
    // engine then emits no `turn.complete`, so the normal repoint timer never runs. Reconcile
    // the pending pointer when the replacement is admitted; a busy turn still defers to its
    // completion, while an idle session gets the one prompt it missed.
    await repoint($)
    if (l.session && l.session !== session) await setSession($, l.session)   // admitted under this name: it is the session's from now
    return l.session && l.session !== session ? l.session : undefined
  }
  if (l.kind === 'refused') { arm.refused = l; return }
  if (l.kind === 'seat') askSeat($, true)
  else if (asksSeat(l.kind)) await askHolder($)
  if (l.kind === 'bulletin' && l.id) {
    // Recorded for the turn that next starts; it wakes nobody, so no pointer either.
    const fid = l.id
    await locked($, async () => {
      const { value: ids = [] } = await $.state.get(BULLETINS)
      const { value: read = [] } = await $.state.get(READ)
      if (!ids.includes(fid) && !read.includes(fid)) await $.state.set(BULLETINS, [...ids, fid])
    })
    return
  }
  if (!l.wakes || l.kind === 'closed') return
  arm.woke = true
  await locked($, async () => {
    const { value: pending = [] } = await $.state.get(PENDING)
    const { value: read = [] } = await $.state.get(READ)
    if (!owed(l.id, pending, read)) {
      $.ui.log(`2mw2lt: a ${l.kind} frame ${l.id ? `${l.id} is already pending or read` : 'carries no id'}; no pointer`, { to: 'debug' })
      return
    }
    await $.state.set(PENDING, [...pending, l.id])
    if (l.inform) {
      const { value: inform = [] } = await $.state.get(INFORM)
      await $.state.set(INFORM, [...inform, l.id])
      await pointingNow($, [...pending, l.id])
      return
    }
    await point($)
  })
  return
}

// One arm of the hold: polls on the agent where the workspace names one and it serves the frames
// route, else hold.py's child. The agent is resolved with the live view's channel's own resolver,
// and again after an arm that could not reach it: a restarted agent may hold a new key.
async function holdOnce($: EngineInterface, ps: string, session: string, gen: number): Promise<Ended> {
  const workspace = workspaceOf()
  if (workspace) {
    // Resolved into a local and installed only past the generation check, so an arm made obsolete
    // while it asked installs nothing over a newer arm's agent.
    const resolved = await agentOf($, workspace)
    if (gen !== generation) return { code: null, woke: false, said: '' }
    agent ??= resolved
    if (agent) {
      const polled = await holdPolled($, ps, session, gen)
      if (polled) return polled
      $.ui.log('2mw2lt: the agent serves no frames route; hold.py holds this arm', { to: 'debug' })
    }
  }
  return holdChild($, ps, session, gen)
}

// The hold as polls (#7838): the first admits it at the door and names it back, with where the
// agent records the frames; each after answers the frames that came, as hold.py's lines, waiting
// for the first up to the agent's keepalive, within the engine's own 30 s on a fetch. A `closed`
// frame ends the arm as it ends a child: revoked is 3, and the rest hold again. A refusal of the
// hold is hold.py's `refused` line, and ends the arm with 1. Nothing when the agent serves no such
// route, for the child to hold this arm.
async function holdPolled($: EngineInterface, ps: string, session: string, gen: number): Promise<Ended | undefined> {
  const a = agent!
  const arm: { woke: boolean; refused?: Line } = { woke: false }
  let admitted: string | undefined
  let held = ''
  while (gen === generation) {
    const r = await $.http.fetch(framesRoute(a, admitted ?? session, held), { headers: holdHeaders(a, ps) })
    let body: { held?: Record<string, unknown>; frames?: Line[]; refused?: unknown; retry?: unknown; remedy?: unknown; serves?: unknown } = {}
    try { body = JSON.parse(r.text) } catch { /* not the agent's answer */ }
    if (gen !== generation) {
      // Superseded while the poll was out: what it answered stays in the agent's recording, and
      // the door replays what went unread. Whoever stopped the arm lets its hold go, and an
      // admission this late answer carries is this arm's own to release, no one else's.
      const late = body.held
      if (late && typeof late.hold_id === 'string') {
        const theirs = typeof late.session === 'string' ? late.session : session
        void releaseHold($, a, theirs, late.hold_id)
      }
      return { code: null, woke: arm.woke, said: '', refused: arm.refused, session: admitted }
    }
    if (r.status === 404 && typeof body.refused !== 'string') return undefined     // no such route: an older agent
    if (r.status !== 200) {
      // An agent serving other workspaces than this session's is the enrolment's diagnosis, not a
      // moment's (#1073), as hold.py reads it; the door's refusals come decided by the agent.
      const elsewhere = r.status === 404 && Array.isArray(body.serves) && body.serves.length > 0
      if (typeof body.retry === 'boolean' || elsewhere) {
        const retry = typeof body.retry === 'boolean' ? body.retry : false
        arm.refused = { id: null, kind: 'refused', wakes: false, why: `${elsewhere ? `refused ${r.status}: ` : ''}${String(body.refused)}`, retry,
          ...(typeof body.remedy === 'string' ? { remedy: body.remedy } : retry ? {} : { remedy: 'run /2mw2lt:connect, which the plugin\'s hold follows' }) }
        return { code: 1, woke: arm.woke, said: arm.refused.why ?? '', refused: arm.refused, session: admitted }
      }
      return { code: POLL_UNSERVED, woke: arm.woke, said: `the agent answered ${r.status}: ${r.text.slice(0, STDERR_TAIL)}`, refused: arm.refused, session: admitted }
    }
    if (body.held) {
      held = String(body.held.hold_id ?? '')
      holdId = held
      holdName = String(body.held.session ?? session)
      if (typeof body.held.frames === 'string') framesPath = body.held.frames
      const renamed = await onLine($, { id: null, kind: 'held', wakes: false, hold: held, session: holdName }, ps, session, arm)
      if (renamed) admitted = renamed
    }
    for (const l of body.frames ?? []) {
      await onLine($, l, ps, session, arm)
      if (l.kind === 'closed') {
        holdId = ''
        return { code: l.why === 'revoked' ? 3 : 0, woke: arm.woke, said: `the stream closed: ${l.why ?? ''}`, refused: arm.refused, session: admitted }
      }
    }
  }
  return { code: null, woke: arm.woke, said: '', refused: arm.refused, session: admitted }
}

// One child, read to its end: its exit code, and whether a frame it gave woke the session. The
// child names the session coordination admitted where the enrolment reconciled a retained name
// (#5661), so the loop answers by the admitted name from the next arm on.
async function holdChild($: EngineInterface, ps: string, session: string, gen: number): Promise<Ended> {
  const held = $.process.spawn({
    argv: ['python3', script($, 'hold.py'), '--wake', 'plugin', '--provider', 'claude', '--provider-session', ps, session],
    cwd: root,
    env: own(ps),
  })
  child = held
  let buffer = ''
  let said = ''                                // the end of what it wrote to stderr: why it stopped
  const arm: { woke: boolean; refused?: Line } = { woke: false }
  let admitted: string | undefined
  for await (const { stream, text } of held) {
    if (gen !== generation) return { code: null, woke: arm.woke, said, refused: arm.refused, session: admitted }   // leaving the loop kills the child
    if (stream !== 'stdout') { said = (said + text).slice(-STDERR_TAIL); continue }
    const { complete, rest } = lines(buffer + text)
    buffer = rest
    for (const l of complete) {
      const renamed = await onLine($, l, ps, session, arm)
      if (renamed) admitted = renamed
    }
  }
  if (gen !== generation) return { code: null, woke: arm.woke, said, refused: arm.refused, session: admitted }
  return { code: (await held.result).code, woke: arm.woke, said, refused: arm.refused, session: admitted }
}

async function holdLoop($: EngineInterface, session: string, gen: number) {
  const ps = await $.session.id()
  let wait = 1000
  let retry = false
  let attempt = 0
  let named = session
  while (gen === generation) {
    const began = await $.clock.now()
    let code: number | null = null
    let refused: Line | undefined
    let admitted: string | undefined
    try {
      // Sleep can expire the claim before its periodic refresh runs.
      if (retry) await claim($, true, ps)
      if (gen !== generation) return
      retry = true
      const ended = await holdOnce($, ps, named, gen)
      code = ended.code
      refused = ended.refused
      admitted = ended.session
      if (ended.woke) wait = 1000
      if (code !== 0) $.ui.log(`2mw2lt: the hold exited ${code}: ${ended.said.trim()}`, { to: 'debug' })
    } catch (err) {                            // a child that broke is held again, as one that exited
      $.ui.log(`2mw2lt: the hold broke: ${String(err)}`, { to: 'debug' })
      // A poll the agent did not answer: the agent may have moved, so it is asked of again. Only
      // the arm's own loop clears what it owns, never a superseded one's.
      if (gen === generation) {
        agent = undefined
        holdId = ''
      }
    }
    if (gen !== generation) return
    if (admitted && admitted !== named) {
      // Coordination admitted the hold under a reconciled name: the frames and the next arm
      // follow it (#5661). Past the generation check, a superseded loop writes nothing.
      named = admitted
      await setSession($, named)
    }
    // The plugin holds every session's stream, the seat's included, and never hands that to the
    // session (owner ruling, #5641): a revoked (3) or refused (1) hold is held again like any
    // other end, and the session is asked nothing. Those two keep backing off however long the
    // hold lasted. A refusal hold.py names as one no retry outlives stops the loop — and is the
    // one exception (#6308): nothing else will ever reach the session, so it is told, once, below.
    const now = await $.clock.now()
    if (refused && refused.retry === false) {
      hold = { state: 'stopped', refusal: refused.why ?? `the hold exited ${code}`, remedy: refused.remedy ?? '', at: now / 1000 }
      await claim($, true, ps).catch(() => undefined)
      // The stopped claim alone reaches nobody: a turn of its own says the refusal, so the session
      // reconnects or exits (#6308). This loop is not re-armed, so the turn comes once.
      await tell($, refusalTurn(refused.why, refused.remedy))
      return
    }
    if (code !== 1 && code !== 3 && now - began > 60_000) { wait = 1000; attempt = 0 }   // it held: this is no crash loop
    attempt++
    hold = { state: 'retrying', attempt, at: now / 1000, next_at: (now + wait) / 1000,
      refusal: refused?.why ?? (code === 3 ? 'another hold took this stream' : `the hold ended (${code ?? 'broke'})`) }
    await claim($, true, ps).catch(() => undefined)
    await new Promise<void>(resolve => { $.clock.after(wait, resolve) })
    wait = Math.min(wait * 2, 60_000)
  }
}

async function start($: EngineInterface, session: string) {
  void letGo($, holdName, stop())
  const gen = generation
  hold = { state: 'starting' }
  // The recording is asked of again for this start: it is the session's.
  framesPath = ''
  await setSession($, session)
  askSeat($, true)
  void holdLoop($, session, gen).catch(err => $.ui.log(`2mw2lt: the hold loop ended: ${String(err)}`, { to: 'debug' }))
  const workspace = workspaceOf()
  if (workspace) follow($, workspace, session)
}

export const register: Register = on => {
  on('session.start', async ($, e, next) => {
    const started = await next(e)
    if (!atLeast((await $.session.version()).version)) return started
    root = await $.session.root()
    version = await moduleVersion($)
    refresh?.cancel()
    // The root is fixed for the session: where no claim path is named (a project that is no
    // workspace), asking again every minute would only fail again.
    if (await claim($, true)) {
      connectedAt = Math.max(connectedAt, await connected($))   // a record from before this start asks for no second
      refresh = $.clock.every(REFRESH_MS, () => {
        void claim($, true).then(async () => {
          const at = await connected($)
          if (at) await startConnected($, at)
        }).catch(() => undefined)
      })
    }
    await $.tool.register({
      name: 'frames',
      description: 'Read the frames waiting for this session, each with its daemon-written from: line. Reading is the acknowledgement: what it returns is acknowledged, so a frame that needs nothing gets no reply and no further call.',
      inputSchema: { type: 'object', properties: {}, additionalProperties: false },
    })
    await $.tool.register({ name: 'checkpoint', ...CHECKPOINT_TOOL })
    for (const name of Object.keys(SEAT_TOOLS)) {
      await $.tool.register({ name, description: SEAT_TOOLS[name]!.description, inputSchema: seatSchema(name) })
    }
    for (const name of Object.keys(TIMER_TOOLS)) {
      await $.tool.register({ name, description: TIMER_TOOLS[name]!.description, inputSchema: timerSchema(name) })
    }
    const session = await sessionOf($)       // a resume or reload of a session already connected
    if (session) await start($, session)
    return started
  })

  on('tool.describe', { tool: TOOL }, async ($, e, next) => ({ ...(await next(e)), isDeferred: false }))
  on('tool.describe', { tool: CHECKPOINT_CALL }, async ($, e, next) => ({ ...(await next(e)), isDeferred: false }))
  // The timer tools are every session's own — the seat's or a worker's — so they stand in front
  // for all, unlike the seat's verbs, which wait behind ToolSearch for a holder.
  on('tool.describe', { tool: TIMER_TOOL }, async ($, e, next) => ({ ...(await next(e)), isDeferred: false }))

  // The seat's verbs stay behind ToolSearch for a session that does not hold the seat; a holder
  // has them in its list. They move when the seat section does, so the prompt changes once.
  on('tool.describe', { tool: SEAT_TOOL }, async ($, e, next) => {
    const described = await next(e)
    return (await $.state.get(SEAT)).value ? { ...described, isDeferred: false } : described
  })

  // A plugin's tool is answered above the permission system (doc 175 §8): so this hook asks it.
  // These checks guard the tool's entry; the lease itself is the stored one any process under
  // this provider session could use through lease.py.
  on('tool.call', { tool: SEAT_TOOL }, async ($, e, next) => {
    const { tool, tool_use_id: _id, agentId, ...args } = e as unknown as Record<string, unknown> & { tool: string; agentId?: string }
    // Asked only of the model's own main-loop call: the other refusals need no question put.
    const checked = agentId === undefined && next.origin.plugin === 'engine'
      ? await $.tool.check({ tool, input: args }) : { decision: 'deny' as const }
    const refused = entryRefusal(agentId, next.origin.plugin, tool, checked)
    if (refused) return { deny: refused }
    const call = seatCall(tool.replace(/^mcp__2mw2lt__/, ''), args)
    if ('refused' in call) return { deny: `refused: ${call.refused}` }
    return runSeatCall($, call)
  })

  on('tool.call', { tool: CHECKPOINT_CALL }, async ($, e, next) => {
    const { tool, tool_use_id: _id, agentId, ...args } = e as unknown as Record<string, unknown> & { tool: string; agentId?: string }
    const checked = agentId === undefined && next.origin.plugin === 'engine'
      ? await $.tool.check({ tool, input: args }) : { decision: 'deny' as const }
    const refused = entryRefusal(agentId, next.origin.plugin, tool, checked, CHECKPOINT_ENTRY)
    if (refused) return { deny: refused }
    return runCheckpoint($, args)
  })

  // A timer verb goes on the session's own enrolment through timer.py — never lease.py, which a
  // worker must not borrow. A mutation whose answer was lost goes once more under the id the
  // client printed, as the seat verbs do.
  on('tool.call', { tool: TIMER_TOOL }, async ($, e, next) => {
    const { tool, tool_use_id: _id, agentId, ...args } = e as unknown as Record<string, unknown> & { tool: string; agentId?: string }
    const checked = agentId === undefined && next.origin.plugin === 'engine'
      ? await $.tool.check({ tool, input: args }) : { decision: 'deny' as const }
    const refused = entryRefusal(agentId, next.origin.plugin, tool, checked, TIMER_ENTRY)
    if (refused) return { deny: refused }
    const call = timerCall(tool.replace(/^mcp__2mw2lt__/, ''), args)
    if ('refused' in call) return { deny: `refused: ${call.refused}` }
    const session = await sessionOf($)
    if (!session) return { deny: 'refused: this session is not connected to steering: run /2mw2lt:connect first.' }
    const argv = ['python3', script($, 'timer.py'), session, ...call.argv]
    return answer(call.stdin !== undefined ? await resending($, argv, call.stdin) : await runOwn($, argv))
  })

  on('tool.call', { tool: TOOL }, async ($, e, next) => {
    const { agentId } = e as unknown as { agentId?: string }
    if (!framesEntry(agentId, next.origin.plugin)) return { result: 'Nothing is waiting.' }
    const { value: session = '' } = await $.state.get(SESSION)
    const pending = await locked($, async () => (await $.state.get(PENDING)).value ?? [])
    let recording = ''
    if (session && pending.length) {
      const at = await framePath($, session)
      if (at) recording = String(await $.fs.read(at).catch(() => ''))
    }
    const { text, envelopes, says, found, read } = pick(recording, pending)
    if (read.directives.length || read.seatGeneration !== undefined) {
      const proof = await runSeatCall($, { client: 'post', path: '/steering/seat/read',
        stdin: JSON.stringify({ directives: read.directives, ...(read.seatGeneration !== undefined ? { seat_generation: read.seatGeneration } : {}) }) })
      if ('deny' in proof) {
        if (read.seatFrameId && obsoleteSeatRefusal(proof.deny)) await retireStaleSeatFrame($, read.seatFrameId)
        else await refuseFrames($, pending)
        return { deny: `refused: foreground frame read: ${proof.deny}` }
      }
    }
    // Prove the selected envelope before its receipt lets the daemon stop replaying it: a refused
    // proof then remains recoverable even if this local recording later trims it. A say settles
    // when this tool returns it, not when the hold recorded it (#7136): the recording is the
    // plugin's hands, and a say still unsettled is what the platform wakes the session for.
    // A receipt that does not arrive withholds nothing: the frames are returned, and the receipt
    // is sent again on the next call. Refusing the call over it left the session reading nothing
    // while the daemon was slow to take receipts (#7396).
    const { value: owing = [] } = await $.state.get(UNSETTLED)
    const receipts = [...owing.filter(r => !envelopes.includes(r.ulid ?? '') && !says.includes(r.say ?? '')),
      ...envelopes.map(ulid => ({ ulid, tries: 0 })), ...says.map(say => ({ say, tries: 0 }))]
    const ackFailures: string[] = []
    const unsettled: typeof owing = []
    for (const r of receipts) {
      const run = r.ulid !== undefined
        ? await runOwn($, ['python3', script($, 'ack.py'), session, r.ulid])
        : await runOwn($, ['python3', script($, 'hold.py'), '--read-say', session, r.say ?? ''])
      if (run.exitCode === 0) continue
      const what = r.ulid !== undefined ? `ack.py ${r.ulid}` : `hold.py --read-say ${r.say}`
      ackFailures.push(`${what} exited ${run.exitCode}${run.stderr.trim() ? `: ${run.stderr.trim()}` : ''}`)
      $.ui.log(`2mw2lt: ${what} exited ${run.exitCode}: ${run.stderr.trim()}`, { to: 'debug' })
      if (r.tries + 1 < RECEIPT_TRIES) unsettled.push({ ...r, tries: r.tries + 1 })
    }
    if (owing.length || unsettled.length) await $.state.set(UNSETTLED, unsettled)
    // Only what was returned leaves PENDING: an id the recording does not hold yet stays, and does
    // not wake the session again. A frame that arrived while this ran gets its own pointer.
    await locked($, async () => {
      const { value: now = [] } = await $.state.get(PENDING)
      const readNow = found
      const left = now.filter(id => !readNow.includes(id))
      await $.state.set(PENDING, left)
      const { value: read = [] } = await $.state.get(READ)
      await $.state.set(READ, [...read, ...readNow].slice(-READ_KEPT))
      await unpoint($)
      if ((await pointingNow($, left)).some(id => !pending.includes(id))) await point($)
    })
    const unreported = ackFailures.length ? `\n2mw2lt: the read was not reported, and is sent again on the next frames call: ${ackFailures.join('; ')}` : ''
    return { result: (text || 'Nothing is waiting.') + unreported }
  })

  // A subagent presents its parent's credential: the harness hands its provider session id down,
  // so its connect would mint nothing of its own and its disconnect would end the parent's
  // enrolment (#3342). The lifecycle verbs are refused to a subagent loop outright, as the seat
  // tools are (plans/3872 backlog §1.3). Only the guard's own evaluation is fail-closed: an error
  // past it propagates, as the observer's does, and is never mistaken for a refusal.
  on('tool.call', { tool: 'Bash' }, async ($, e, next) => {
    const { agentId } = e as unknown as { agentId?: string }
    if (agentId === undefined || !root) return next(e)
    let verb: string | null
    try {
      verb = lifecycle(e.command)
    } catch {
      return { deny: 'refused: the subagent lifecycle guard could not be evaluated, so the call is refused rather than passed (#3342)' }
    }
    if (!verb) return next(e)
    return { deny: `refused: the enrolment lifecycle is the session's own, never a subagent's (${verb} would act on its parent's credential). Ask the session to run it` }
  })

  on('tool.call', { tool: 'Bash' }, async ($, e, next) => {
    const result = await next(e)
    const { agentId } = e as unknown as { agentId?: string }
    const moved = result.deny === undefined && !result.isError ? result.result : undefined
    const aborted = agentId === undefined && moved?.backgroundedByTurnAbort === true
    if (aborted && moved?.backgroundTaskId) stopping?.moved.push(moved.backgroundTaskId)
    // The completed call is a trigger only; its output is never parsed (design D3). A command an
    // abort moved to the background has not completed, and the work below is aborted with it,
    // whose throw hid the task from every hook and the model (#4823). A subagent's command
    // is its own, never the session's: only the parent's loop moves its hold (#5688).
    if (!root || agentId !== undefined || result.deny !== undefined || result.isError || aborted) return result
    const at = await connected($)
    if (at || invokes(e.command, 'connect')) {
      await startConnected($, at)
    } else if (invokes(e.command, 'promote', 'steering') || ((invokes(e.command, 'door') || invokes(e.command, 'lease')) && /\/steering\/brain\/(attach|detach)\b/.test(e.command))) {
      // Taking the seat, handing it on or handing it back: no frame tells the session that left it.
      askSeat($, true)
    } else if (invokes(e.command, 'disconnect')) {
      const gen = generation
      const { value: session = '' } = await $.state.get(SESSION)
      if (!session || await sessionOf($, session) !== '' || gen !== generation) return result
      // The claim stays, since the module is still live for a later connect.
      await letGo($, holdName, stop())
      hold = { state: 'stopped', refusal: 'this session disconnected', remedy: 'run /2mw2lt:connect', at: (await $.clock.now()) / 1000 }
      await claim($, true).catch(() => undefined)
      await setSession($, '')
      await $.state.set(SEAT, '')
      await $.state.set(BULLETIN, '')
      await $.state.set(BULLETINS, [])
    }
    return result
  }).catch(($, e, next) => next(e))

  on('turn.start', async ($, e, next) => {
    if (e.agentId === undefined) show($, { kind: 'turn', turn: { state: 'started' } })
    // A subagent's turn starts inside ours, and the run is the outer turn's.
    busyNow = true
    await $.state.set(BUSY, true)
    if (e.agentId === undefined) await $.state.set(TURN, e.turnId)
    if (e.agentId === undefined) await tellBulletins($).catch(err => $.ui.log(`2mw2lt: the bulletin turn broke: ${String(err)}`, { to: 'debug' }))
    return next(e)
  })

  on('turn.complete', async ($, e, next) => {
    if (e.agentId === undefined) {                                 // a subagent's turn ends inside ours
      const command = stamping
      stamping = undefined
      streaming.end()
      show($, { kind: 'turn', turn: { state: 'completed', reason: surface.turnReason(e.reason) } })
      busyNow = false
      await $.state.set(BUSY, false)
      await $.state.set(TURN, '')
      if (command) void reportCommand($, command.session, command.act, e.reason === 'answer'
        ? { landed: 'completed' } : { refused: `the command's turn ended: ${e.reason}` })
      echoing.length = 0                                           // an echo that never came is not the next run's
      if (stopping?.turn === e.turnId) stopping.ended()
      // A plugin reloaded in the turn changes the list.
      void tellCommands($, watched)
      // A turn dead on an API error (a 429) did not read its pointer, and sending it into the
      // same refusal would only be refused again. The frames stay pending, and the next one to
      // arrive is pointed afresh, which a pointer left outstanding would have blocked for good.
      if (e.reason === 'error') {
        await locked($, () => unpoint($))
        return next(e)
      }
      if ((await $.state.get(APPENDED)).value) $.clock.after(REPOINT_MS, () => { void repoint($).catch(() => undefined) })
    }
    return next(e)
  })

  on('session.measure', async ($, e, next) => {
    const out = await next(e)
    await checkpointIfDue($, e.context.tokens, true).catch(err => $.ui.log(`2mw2lt: the checkpoint check broke: ${String(err)}`, { to: 'debug' }))
    return out
  })

  // One long turn can run from well short of the point to compaction with no turn's end between,
  // so each of the main thread's requests is measured as it is answered. While the session is
  // watched, the main thread's text and thinking stream to the live view (#8282): each chunk is
  // passed on as it came, and nothing the mirror does reaches the turn. A piece is taken before it
  // is passed on, so its block is open before the engine can store the block's row. Offsets let a
  // watch begun mid-step join later blocks without treating a suffix as a beginning.
  on('turn.step', async function* ($, e, next) {
    if (e.agentId === undefined) streaming.begin(e.turnId, e.index)
    let timer: Timer | undefined
    const send = (stopped = false) => {
      timer = undefined
      if (e.agentId !== undefined || !watching) return
      try { for (const p of streaming.send(stopped)) show($, p) }
      catch (err) { $.ui.log(`2mw2lt: the live view's stream broke: ${String(err)}`, { to: 'debug' }) }
    }
    const stream = next(e)
    try {
      for await (const c of stream) {
        if (e.agentId === undefined && (c.kind === 'text' || c.kind === 'thinking')) {
          try { if (streaming.take(c, watching) && !timer) timer = $.clock.after(STREAM_MS, send) }
          catch (err) { $.ui.log(`2mw2lt: the live view's stream broke: ${String(err)}`, { to: 'debug' }) }
        }
        yield c
      }
    } finally {
      timer?.cancel()
      send(true)
    }
    const r = await stream.result
    if (e.agentId === undefined && r.usage) {
      await checkpointIfDue($, tokensOf(r.usage), false).catch(err => $.ui.log(`2mw2lt: the checkpoint check broke: ${String(err)}`, { to: 'debug' }))
    }
    return r
  })

  on('session.compact', async ($, e, next) => {
    if (!root || e.trigger === 'precompute' || e.agentId !== undefined) return next(e)
    const { value: session = '' } = await $.state.get(SESSION)
    await locked($, () => $.state.set(WARNED, false))   // ordered with a check still in flight
    if (!session) return next(e)
    // The conversation's cache is spent here anyway: the new text is in place before the next request.
    await refreshSeat($, true).catch(err => $.ui.log(`2mw2lt: the seat check broke: ${String(err)}`, { to: 'debug' }))
    const keep = await keepLine($, session)
    const compacted = await next({ ...e, instructions: [e.instructions, keep].filter(Boolean).join('\n') })
    return compacted
  }).catch(($, e, next) => next(e))

  // Every row the main conversation keeps, as stored, for the live view, its compaction's summary
  // and the desk prompts this module submits among them; the module's own appends are shown where
  // they are made. A submitted prompt's act is taken whether or not anyone watches.
  on('session.append', async ($, e, next) => {
    const stored = await next(e)
    if (stored.uuid === undefined || surface.ownNote(e)) return stored
    // A response row takes its streamed block's place, matched whether or not it is shown, so the
    // rows after it find theirs.
    const replaces = e.door === 'response' && e.agentId === undefined ? streaming.replaces(stored.message.content) : undefined
    let act = e.door === 'prompt' && surface.ours(e.origin) ? actOf(surface.firstText(stored.message.content)) : undefined
    // A desk command's echo row begins its turn's rows, which carry its act until the turn ends: the
    // answer's rows are the model's, and only that turn tells them apart (U0, finding 2).
    if (e.agentId === undefined) {
      const line = e.door === 'command' && surface.ours(e.origin) ? surface.commandLine(surface.firstText(stored.message.content)) : undefined
      const at = line === undefined ? -1 : echoing.findIndex(c => `/${c.name}` === line.split(' ')[0])
      if (at >= 0) stamping = echoing.splice(at, 1)[0]!
      // A person's own prompt, typed while the turn runs, is theirs: never the desk speaker's.
      if (act === undefined && e.origin.kind !== 'composer' && e.origin.kind !== 'bridge' && e.origin.kind !== 'sdk') act = stamping?.act
    }
    const payload = watching ? surface.eventOf(e, stored, act) : undefined
    if (payload?.kind === 'row' && replaces) payload.row.replaces = replaces
    if (payload) show($, payload)
    return stored
  }).catch(($, e, next) => next(e))

  // A desk command's note joins its own turn: appended as the engine dispatches the command, under
  // this module's origin, never before the run, which a busy session's running turn would read.
  on('prompt.submit', async ($, e, next) => {
    const run = e.command && surface.ours(e.origin) ? runs.find(r => !r.fired && r.name === e.command!.name) : undefined
    if (run) {
      run.fired = true
      echoing.push({ act: run.act, session: run.session, name: run.name })
      if (echoing.length > ECHOING_KEPT) echoing.shift()
      await append($, run.note, run.act)
    }
    return next(e)
  }).catch(($, e, next) => next(e))

  // /clear ends this session id without a `session.start` for the next: the hold goes with the old
  // id, and the new id gets its claim here. rebrief.py sends the model to connect again.
  on('classic.SessionStart', async ($, e, next) => {
    if (root && e.source === 'clear') {
      await locked($, async () => {
        await $.state.set(PENDING, [])
        await $.state.set(INFORM, [])
        $.ui.status(undefined)
        await $.state.set(READ, [])                  // a fresh context has read nothing
        await $.state.set(WARNED, false)             // a fresh context is owed its own prompt
        await unpoint($)
      })
      hold = undefined                             // the new id holds nothing until it connects
      await claim($, true, e.session_id)
      askSeat($, true)
    }
    return next(e)
  }).catch(($, e, next) => next(e))

  // The seat's rulings, last in the system prompt, after the cache boundary. A teammate renders its
  // lead's prompt and holds no seat.
  on('prompt.compose', async ($, e, next) => {
    const composed = await next(e)
    teammate = e.traits.includes('teammate')
    if (teammate) return composed
    const sections = [...composed.sections]
    let added = false
    const { value: seat = '' } = await $.state.get(SEAT)
    if (seat) { sections.push({ id: SEAT_SECTION, text: seat, scope: 'session' as const }); added = true }
    const { value: bulletin = '' } = await $.state.get(BULLETIN)
    if (bulletin) { sections.push({ id: BULLETIN_SECTION, text: bulletin, scope: 'session' as const }); added = true }
    return added ? { sections } : composed
  }).catch(($, e, next) => next(e))

  // Stop the loop before anything else: the engine ends the child before this runs, and a loop
  // left going would spawn another mid-shutdown. All of it shares a 1.5 s bound, so the local
  // cleanup runs first and the let-go at the agent last: a door the agent waits on cannot cost
  // the claim its release, and a let-go that misses the bound falls to the agent's grace.
  on('session.end', async ($, e, next) => {
    const held = stop()
    await $.state.set(APPENDED, false)   // a turn ending now has nothing to be pointed again for
    if (e.reason !== 'clear') refresh?.cancel()
    if (root) await claim($, false, e.sessionId)
    // Not awaited: the hook's own bound ends here, and a let-go that outlives it falls to the
    // agent's grace, which is what it is for.
    void letGo($, holdName, held)
    return next(e)
  })
}
