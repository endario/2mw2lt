import type { Register, EngineInterface, Timer, HookStream, ProcessSpawnChunk, ProcessSpawnResult } from 'claude-code'
import { lines, atLeast, pick, invokes, answerable, tokensOf, due, POINTER, CHECKPOINT, CONNECT_AGAIN } from './frames'
import type { Routine } from './frames'

// The plugin's hooks module (#3872): it holds a connected Claude session's steering stream through
// hold.py's `--wake plugin` mode, wakes the session with a fixed pointer, and hands the frames to
// the model only as the `frames` tool's result. Every wire action is the Python client's; this
// module owns timing and delivery. It is an enhancement: with it absent, connect finds no fresh
// holder claim and the session keeps the model-run recipe.

const BUSY = { plugin: '2mw2lt', key: 'busy' } as const
const TURN_AT = { plugin: '2mw2lt', key: 'turnAt' } as const
const PENDING = { plugin: '2mw2lt', key: 'pending' } as const
const POINTED = { plugin: '2mw2lt', key: 'pointed' } as const
const APPENDED = { plugin: '2mw2lt', key: 'appended' } as const
const WARNED = { plugin: '2mw2lt', key: 'warned' } as const
const POINT = { plugin: '2mw2lt', key: 'point' } as const
const SESSION = { plugin: '2mw2lt', key: 'session' } as const
const CONNECTED = { plugin: '2mw2lt', key: 'connected' } as const

const TOOL = 'mcp__2mw2lt__frames'
const REFRESH_MS = 60_000
const REPOINT_MS = 1500
const STDERR_TAIL = 2000

// Always this plugin version's own clients, never the launcher: it resolves the recorded install,
// which may be an older hold.py that refuses `--wake plugin`.
const script = ($: EngineInterface, name: string) => `${$.plugin.root}/steering/enroll/${name}`

// Every row the module writes goes through here, and its text is one of the fixed texts in
// frames.ts: a frame's own text reaches the model only as the `frames` tool's result. A refused
// append (a plugin above, or a run no plugin may shape) is logged, and the hold goes on.
async function append($: EngineInterface, text: string) {
  $.ui.log(`2mw2lt: appends "${text}"`, { to: 'debug' })
  try {
    await $.session.append({ message: { type: 'user', content: [{ type: 'text', text }] } })
  } catch (err) {
    $.ui.log(`2mw2lt: the append was refused: ${String(err)}`, { to: 'debug' })
  }
}

async function moduleVersion($: EngineInterface): Promise<string> {
  try {
    const manifest = JSON.parse(String(await $.fs.read(`${$.plugin.root}/.claude-plugin/plugin.json`)))
    return typeof manifest.version === 'string' ? manifest.version : ''
  } catch { return '' }
}

// hold.py names where a provider session's claim lives; the path is asked once per id, so the
// release at `session.end` spends none of its 1.5 s on a child.
async function claimPath($: EngineInterface, id: string): Promise<string> {
  if (claimed?.id === id) return claimed.path
  const r = await $.process.run(['python3', script($, 'hold.py'), '--claim-path', id], { cwd: root })
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
  await $.fs.write(path, JSON.stringify({ provider_session: id, module: version, at }))
  return true
}

// What the summariser keeps: rebrief.py's line, or the session alone when it gives none.
async function keepLine($: EngineInterface, session: string): Promise<string> {
  const r = await $.process.run(['python3', script($, 'rebrief.py'), '--keep', session], { cwd: root }).catch(() => undefined)
  return (r?.exitCode === 0 && r.stdout.trim()) || `Keep verbatim: steering session ${session}.`
}

// PENDING and POINTED are read and then written by the hold loop and by the `frames` tool, each
// across awaits; every such change runs in turn through this one chain.
let chain: Promise<unknown> = Promise.resolve()
function locked<T>(work: () => Promise<T>): Promise<T> {
  const run = chain.then(work, work)
  chain = run.catch(() => undefined)
  return run
}

// Called with the chain held. A pointer the session will not take as a prompt is appended
// instead, so the frame still reaches the model at its next request.
async function point($: EngineInterface) {
  const { value: pointed = false } = await $.state.get(POINTED)
  if (pointed) return
  await $.state.set(POINTED, true)
  await $.state.set(APPENDED, await tell($, POINTER))
}

// A pointer appended into a running turn is read only if that turn makes another request, and a
// turn about to end does not: the row stays unread, POINTED stays set, and no pointer is ever sent
// again. So a turn that ended with its pointer unanswered gets one as a prompt of its own,
// once the session is idle.
async function repoint($: EngineInterface) {
  await locked(async () => {
    if (!(await $.state.get(APPENDED)).value) return
    if ((await $.state.get(BUSY)).value) return            // the next turn's end asks again
    await unpoint($)
    // Nothing is owed a pointer once the module no longer holds the stream.
    if (!yielded && (await $.state.get(SESSION)).value && ((await $.state.get(PENDING)).value ?? []).length) await point($)
  })
}

// The pointer is no longer outstanding: the model read the frames, or the session starts over.
async function unpoint($: EngineInterface) {
  await $.state.set(POINTED, false)
  await $.state.set(APPENDED, false)
}

// A fixed text the model must read: a turn of its own on an idle session, a row in the running
// turn on a busy one. A prompt the session will not take is appended instead, to be read at its
// next request. Whether it went into a running turn.
async function tell($: EngineInterface, text: string): Promise<boolean> {
  const { value: busy = false } = await $.state.get(BUSY)
  if (busy) await append($, text)
  else void $.prompt.submit({ text }).catch(() => append($, text))
  return busy
}

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
  await locked(async () => {
    if (!(await owed())) return
    const point = (fresh ? undefined : (await $.state.get(POINT)).value) ?? await compactionPoint($)
    if (!due(tokens, point)) return
    await $.state.set(WARNED, true)
    await append($, CHECKPOINT)
  })
}

async function sessionOf($: EngineInterface): Promise<string> {
  const r = await $.process.run(['python3', script($, 'hold.py'), '--session-of', await $.session.id()], { cwd: root })
  return r.exitCode === 0 ? r.stdout.trim() : ''
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
// Set when a hold is refused after a connect: connecting did not repair it, so the module hands the
// stream back to the model-run recipe for the rest of this process rather than loop on it.
let yielded = false
let child: HookStream<ProcessSpawnChunk, ProcessSpawnResult> | undefined
// The model whose window the engine did not recognise, once named in the log.
let named = ''

function stop() {
  generation++
  void child?.return(undefined as never).catch(() => undefined)
  child = undefined
}

// A routine kick (#3995): the daemon counts it answered by any evidence of the session later than
// the kick, which a turn's end gives and an idle session's does not, so the answer is posted here,
// with no model turn. Not while the running turn is older than the kick's interval: that kick goes
// unanswered and counts as it always has.
async function answerKick($: EngineInterface, ps: string, session: string, kick: Routine, gen: number) {
  const { value: busy = false } = await $.state.get(BUSY)
  const { value: began } = await $.state.get(TURN_AT)
  if (!answerable(busy, began, await $.clock.now(), kick.interval)) {
    $.ui.log(`2mw2lt: the kick at ${kick.at} goes unanswered: a turn has run past ${kick.interval} s`, { to: 'debug' })
    return
  }
  if (gen !== generation) return
  const r = await $.process.run(
    ['python3', script($, 'hold.py'), '--answer-kick', kick.at, '--provider', 'claude', '--provider-session', ps, session],
    { cwd: root })
  if (r.exitCode !== 0) $.ui.log(`2mw2lt: answering the kick at ${kick.at} exited ${r.exitCode}: ${r.stderr.trim()}`, { to: 'debug' })
}

// One child, read to its end: its exit code, and whether a frame it gave woke the session.
async function holdOnce($: EngineInterface, ps: string, session: string, gen: number) {
  const held = $.process.spawn({
    argv: ['python3', script($, 'hold.py'), '--wake', 'plugin', '--provider', 'claude', '--provider-session', ps, session],
    cwd: root,
  })
  child = held
  let buffer = ''
  let said = ''                                // the end of what it wrote to stderr: why it stopped
  let woke = false
  for await (const { stream, text } of held) {
    if (gen !== generation) return { code: null, woke, said }   // leaving the loop kills the child
    if (stream !== 'stdout') { said = (said + text).slice(-STDERR_TAIL); continue }
    const { complete, rest } = lines(buffer + text)
    buffer = rest
    for (const l of complete) {
      if (l.routine && !l.wakes) {
        void answerKick($, ps, session, l.routine, gen).catch(err => $.ui.log(`2mw2lt: answering a kick broke: ${String(err)}`, { to: 'debug' }))
        continue
      }
      if (!l.wakes || l.kind === 'closed') continue
      woke = true
      await locked(async () => {
        const { value: pending = [] } = await $.state.get(PENDING)
        if (l.id) await $.state.set(PENDING, [...pending, l.id])
        await point($)
      })
    }
  }
  if (gen !== generation) return { code: null, woke, said }
  return { code: (await held.result).code, woke, said }
}

async function holdLoop($: EngineInterface, session: string, gen: number) {
  const ps = await $.session.id()
  let wait = 1000
  while (gen === generation) {
    const began = await $.clock.now()
    let code: number | null = null
    try {
      const ended = await holdOnce($, ps, session, gen)
      code = ended.code
      if (ended.woke) wait = 1000
      if (code !== 0) $.ui.log(`2mw2lt: the hold exited ${code}: ${ended.said.trim()}`, { to: 'debug' })
    } catch (err) {                            // a child that broke is held again, as one that exited
      $.ui.log(`2mw2lt: the hold broke: ${String(err)}`, { to: 'debug' })
    }
    if (gen !== generation) return
    // 3: the stream was revoked. 1: the hold was refused. Neither is retried; connecting again
    // is the recipe's repair. A refusal before this process ever connected adds no row: the
    // recipe is about to connect anyway. Any other end (4: its recording failed) is held again.
    const connected = (await $.state.get(CONNECTED)).value
    if (code === 1 && connected) {
      yielded = true
      refresh?.cancel()
      await claim($, false).catch(() => undefined)   // connect now prints today's reply: arm a hold
    }
    if (code === 3 || (code === 1 && connected)) {
      await tell($, CONNECT_AGAIN)
    }
    if (code === 1 || code === 3) return
    if ((await $.clock.now()) - began > 60_000) wait = 1000   // it held: this is no crash loop
    await new Promise<void>(resolve => { $.clock.after(wait, resolve) })
    wait = Math.min(wait * 2, 60_000)
  }
}

async function start($: EngineInterface, session: string) {
  stop()
  const gen = generation
  await $.state.set(SESSION, session)
  void holdLoop($, session, gen).catch(err => $.ui.log(`2mw2lt: the hold loop ended: ${String(err)}`, { to: 'debug' }))
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
    if (await claim($, true)) refresh = $.clock.every(REFRESH_MS, () => { if (!yielded) void claim($, true).catch(() => undefined) })
    await $.tool.register({
      name: 'frames',
      description: 'Read the steering frames waiting for this session, each with its daemon-written from: line. Acknowledges the envelopes it returns.',
      inputSchema: { type: 'object', properties: {}, additionalProperties: false },
    })
    const session = await sessionOf($)       // a resume or reload of a session already connected
    if (session) await start($, session)
    return started
  })

  on('tool.describe', { tool: TOOL }, async ($, e, next) => ({ ...(await next(e)), isDeferred: false }))

  on('tool.call', { tool: TOOL }, async $ => {
    const { value: session = '' } = await $.state.get(SESSION)
    const pending = await locked(async () => (await $.state.get(PENDING)).value ?? [])
    let recording = ''
    if (session && pending.length) {
      const at = await $.process.run(['python3', script($, 'hold.py'), '--frame-path', session], { cwd: root })
      if (at.exitCode === 0) recording = String(await $.fs.read(at.stdout.trim()).catch(() => ''))
    }
    const { text, envelopes, found } = pick(recording, pending)
    // Answering the tool is the ack: "acked" keeps meaning the model has the envelope.
    for (const ulid of envelopes) {
      const acked = await $.process.run(['python3', script($, 'ack.py'), session, ulid], { cwd: root })
      if (acked.exitCode !== 0) $.ui.log(`2mw2lt: ack.py ${ulid} exited ${acked.exitCode}: ${acked.stderr.trim()}`, { to: 'debug' })
    }
    // Only what was returned leaves PENDING: an id the recording does not hold yet stays, and does
    // not wake the session again. A frame that arrived while this ran gets its own pointer.
    await locked(async () => {
      const { value: now = [] } = await $.state.get(PENDING)
      const left = now.filter(id => !found.includes(id))
      await $.state.set(PENDING, left)
      await unpoint($)
      if (left.some(id => !pending.includes(id))) await point($)
    })
    return { result: text || 'No steering frame is waiting.' }
  })

  on('tool.call', { tool: 'Bash' }, async ($, e, next) => {
    const result = await next(e)
    // The completed call is a trigger only; its output is never parsed (design D3).
    if (!root || result.deny !== undefined || result.isError) return result
    if (!yielded && invokes(e.command, 'connect')) {
      const session = await sessionOf($)
      if (session) {
        await $.state.set(CONNECTED, true)
        await start($, session)
      }
    } else if (invokes(e.command, 'disconnect')) {
      // The session chose to leave: hold nothing, and ask nothing of it. The claim stays, since
      // the module is still live for a later connect.
      stop()
      await $.state.set(CONNECTED, false)
      await $.state.set(SESSION, '')
    }
    return result
  }).catch(($, e, next) => next(e))

  on('turn.start', async ($, e, next) => {
    // A subagent's turn starts inside ours, and the run is the outer turn's.
    if (!(await $.state.get(BUSY)).value) await $.state.set(TURN_AT, await $.clock.now())
    await $.state.set(BUSY, true)
    return next(e)
  })

  on('turn.complete', async ($, e, next) => {
    if (e.agentId === undefined) {                                 // a subagent's turn ends inside ours
      await $.state.set(BUSY, false)
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
  // so each of the main thread's requests is measured as it is answered.
  on('turn.step', async function* ($, e, next) {
    const r = yield* next(e)
    if (e.agentId === undefined && r.usage) {
      await checkpointIfDue($, tokensOf(r.usage), false).catch(err => $.ui.log(`2mw2lt: the checkpoint check broke: ${String(err)}`, { to: 'debug' }))
    }
    return r
  })

  on('session.compact', async ($, e, next) => {
    if (!root || e.trigger === 'precompute' || e.agentId !== undefined) return next(e)
    const { value: session = '' } = await $.state.get(SESSION)
    await locked(() => $.state.set(WARNED, false))   // ordered with a check still in flight
    if (!session) return next(e)
    const keep = await keepLine($, session)
    return next({ ...e, instructions: [e.instructions, keep].filter(Boolean).join('\n') })
  }).catch(($, e, next) => next(e))

  // /clear ends this session id without a `session.start` for the next: the hold goes with the old
  // id, and the new id gets its claim here. rebrief.py sends the model to connect again.
  on('classic.SessionStart', async ($, e, next) => {
    if (root && e.source === 'clear') {
      await locked(async () => {
        await $.state.set(PENDING, [])
        await $.state.set(WARNED, false)             // a fresh context is owed its own prompt
        await unpoint($)
      })
      if (!yielded) await claim($, true, e.session_id)
    }
    return next(e)
  }).catch(($, e, next) => next(e))

  // Stop the loop before anything else: the engine ends the child before this runs, and a loop
  // left going would spawn another mid-shutdown. All of it shares a 1.5 s bound.
  on('session.end', async ($, e, next) => {
    stop()
    await $.state.set(APPENDED, false)   // a turn ending now has nothing to be pointed again for
    if (e.reason !== 'clear') refresh?.cancel()
    if (root) await claim($, false, e.sessionId)
    return next(e)
  })
}
