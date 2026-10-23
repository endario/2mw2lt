import type { On } from 'claude-code'
import { mock } from 'claude-code/testing'

// The behaviour tests fake the engine beneath the plugin: the session, the clock, the files,
// host commands and hold.py's child. Kit gap (2.1.290): a plugin's own `$.session.append` never
// reaches a test's hook beneath it ("no implementation for session.append"), whatever the test
// registers, so a row is observed through the debug line the module writes as it appends.

// A round is one child: its lines in order (a promise in among them holds it there), then its
// exit code, or a throw.
export type Round = { lines: (string | Promise<void> | { stderr: string })[]; code: number | null | 'throw'; hold?: Promise<void>; holdMs?: number }

export const TOOL = 'mcp__2mw2lt__frames'
export const FRAME_PATH = '/ws/.claude/steering-frames/s1.ndjson'
export const CLAIM_PATH = '/ws/.claude/steering-holders/ps-1.json'
export const ROOT = '/ws/checkout'
export const line = (id: string | null, kind: string, wakes: boolean) => JSON.stringify({ id, kind, wakes }) + '\n'
// The machine agent the workspace records, and what its surface poll answers, in turn; a poll past
// the last answer is held, as the agent holds one with nothing to say. An answer may itself be held.
export const AGENT = { port: '9991', keyFile: '/keys/9991.key', key: 'agent-key' }
export type Poll = { watch: boolean; cursor?: string; commands?: unknown[] } | { status: number; text: string }
// What the agent's frames route answers one of the hold's polls with (#7838), in turn; a poll past
// the last answer is held, and 'throw' is one the agent did not answer at all. An answer may be
// held. What a DELETE is answered with needs no scripting: 204, or a hang while deleteHangs.
export type HoldPoll = { status: number; text: string; hold?: Promise<void> } | 'throw'

export function world(on: On, { version = '2.1.290', rounds = [] as Round[], recording = '', keep = 'Keep verbatim: steering session s1; branch b; checkpoint https://github.com/example-org/example-repo/pull/1.\n',
                         agent = false, polls = [] as (Poll | Promise<Poll>)[], posts = [] as (number | Promise<number>)[], holdPolls = [] as (HoldPoll | Promise<HoldPoll>)[], deleteHangs = false, holdRoute = false, enrolled = true, submitRejects = false, submitDrops = false, storesSubmits = false, ackFails = false, readFails = false, engineState = false,
                         // The engine's own answer to `$.session.usage({ breakdown })`: the window it measures
                         // against, the token count it auto-compacts at, and who settled the window.
                         context = { point: 167_000 as number | undefined, raw: 200_000, source: 'auto', model: 'claude-test' } } = {}) {
  const w = {
    clock: mock.clock(on, { now: 1_000_000 }),
    ackFails,
    readFails,
    // The engine's refusal after the person's Esc: a submit resolves `drop` until they type (#7827).
    submitDrops,
    root: '',
    writes: [] as { path: string; text: string }[],
    tools: [] as string[],
    runs: [] as string[][],
    cwds: [] as (string | undefined)[],
    spawns: [] as string[][],
    // The environment each child was given over the host process's own, runs and spawns alike.
    envs: [] as (Record<string, string> | undefined)[],
    // The session id the host process carries in its environment when it differs from its own
    // (#7069); a lease client then refuses as the Python one does, unless the child is told otherwise.
    inherited: undefined as string | undefined,
    claimsAtSpawn: [] as number[],
    submits: [] as string[],
    // Releases a submit held for its row to be stored (storesSubmits).
    stored: undefined as (() => void) | undefined,
    // Whether each submit asked to be the user's, unframed.
    asUser: [] as boolean[],
    appends: [] as string[],
    logs: [] as string[],
    statuses: [] as (string | undefined)[],
    psession: 'ps-1',
    claimFails: false,
    compacts: [] as { trigger: string; instructions?: string }[],
    recording,
    enrolled,
    // What connect.py last wrote to the session's connect record; empty while it has written none.
    connected: '',
    bashThrows: false,
    // What a Bash call answers: a command an abort moved to the background carries its task's id.
    bash: { stdout: '', stderr: '', interrupted: false } as Record<string, unknown>,
    // Whether a process the module runs is aborted, as a call's is under `$.turn.abort` (#4823), and
    // how many were.
    runAborted: false,
    abortedRuns: 0,
    // The turns the module aborted, and the background tasks it stopped; a TaskStop the engine
    // refuses answers taskStopDeny.
    aborts: [] as string[],
    taskStops: [] as string[],
    taskStopDeny: undefined as string | undefined,
    sessionLookupCode: 0,
    retainedName: '',
    holdNextLookup: undefined as Promise<void> | undefined,
    readGate: undefined as Promise<void> | undefined,
    state: new Map<string, unknown>(),
    // The next read of this key is answered with what it held when asked, once `release` runs.
    holdNextGet: undefined as { key: string; release: Promise<void> } | undefined,
    context,
    usageCalls: 0,
    stepUsage: 0,
    // The chunks each step streams, in turn; past the last, a step streams none.
    steps: [] as unknown[][],
    // What seat_section.py answers: 0 with the rulings, 1 for a session that does not hold the seat.
    seat: { code: 1, stdout: '' },
    // What bulletin.py section answers: 0 with the snapshot, 1 for an empty bulletin, 3 for a door
    // that cannot tell.
    bulletin: { code: 1, stdout: '' },
    // Held until released: a seat_section.py run still out.
    seatGate: undefined as Promise<void> | undefined,
    // Held until released: a lease or acknowledgement client still out.
    wireGate: undefined as Promise<void> | undefined,
    wireStarted: undefined as (() => void) | undefined,
    // What each lease client was handed on stdin, and what it answers in turn.
    stdins: [] as (string | undefined)[],
    leaseOut: [] as string[],
    // What checkpoint.py answers each run, in turn; past the last, the checkpoint is kept.
    checkpointOut: [] as { code: number; stdout: string; stderr: string }[],
    check: { decision: 'allow' } as { decision: 'allow' | 'ask' | 'deny'; reason?: string },
    polls,
    // What the agent answers each events post, in turn (202 past the last); a promise holds it.
    posts,
    // What it answers each of the hold's polls, in turn; and whether a let-go is never answered.
    holdPolls,
    deleteHangs,
    // Whether this agent serves the frames route at all (#7838); without it a frames GET answers
    // the plain 404 of an agent older than the route, and the hold arm falls to the child.
    holdRoute,
    // The key the agent's key file holds; unset, the file is not there yet.
    agentKey: agent ? AGENT.key : undefined as string | undefined,
    // Every request the module sent the agent, polls and posts alike.
    fetches: [] as { url: string; method: string; headers: Record<string, string>; body?: string }[],
  }
  const versions = new Map<string, number>()
  if (!engineState) on('state.get', async ($, e) => {
    const value = w.state.get(e.key)
    const version = versions.get(e.key) ?? 0
    const held = w.holdNextGet
    if (held?.key === e.key) { w.holdNextGet = undefined; await held.release }
    return { value: { value, version } } as never
  })
  // A set whose ifVersion is not the key's version writes nothing, as the engine's does.
  if (!engineState) on('state.set', async ($, e) => {
    const version = versions.get(e.key) ?? 0
    if (e.ifVersion !== undefined && e.ifVersion !== version) return { value: { isSet: false, version } } as never
    w.state.set(e.key, e.value)
    versions.set(e.key, version + 1)
    return { value: { isSet: true, version: version + 1 } } as never
  })
  on('session.start', async ($, e) => ({ cwd: e.cwd }))
  on('session.version', async () => ({ value: { version } }))
  on('session.root', async () => ({ value: ROOT }))
  on('session.id', async () => ({ value: w.psession }))
  on('turn.start', async ($, e) => ({ turnId: e.turnId }))
  on('turn.complete', async ($, e) => ({ text: e.answer ?? e.text }))
  on('fs.read', async ($, e) => {
    if (e.path.endsWith('/.claude-plugin/plugin.json')) {
      w.root = e.path.slice(0, -'/.claude-plugin/plugin.json'.length)
      return { value: '{"version":"0.8.99"}' }
    }
    if (e.path === FRAME_PATH) { if (w.readGate) await w.readGate; return { value: w.recording } }
    if (e.path === CLAIM_PATH.replace(/\.json$/, '.connect') && w.connected) return { value: w.connected }
    if (agent && e.path === '/ws/.env') return { value: `OTHER=1\nSTEERING_AGENT_PORT=${AGENT.port}\nSTEERING_AGENT_KEY_FILE=${AGENT.keyFile}\n` }
    if (agent && e.path === AGENT.keyFile && w.agentKey) return { value: `${w.agentKey}\n` }
    throw new Error('ENOENT')
  })
  mock.env(on, { HOME: '/u/owner' })
  on('http.fetch', async ($, e) => {
    const method = e.init?.method ?? 'GET'
    w.fetches.push({ url: e.url, method, headers: { ...e.init?.headers }, body: e.init?.body })
    if (method === 'POST') {
      const status = await (w.posts.shift() ?? 202)
      return { value: { status, ok: status >= 200 && status < 300, headers: {}, text: status < 300 ? '{}' : '{"refused":"no"}' } }
    }
    // The hold's polls are the frames route's, answered from their own queue; a let-go is answered
    // at once, or hung while deleteHangs, to prove the module's own cleanup outlives it.
    if (e.url.includes('/frames')) {
      if (!w.holdRoute) return { value: { status: 404, ok: false, headers: {}, text: '404 page not found\n' } }
      if (method === 'DELETE') {
        if (w.deleteHangs) await never
        return { value: { status: 204, ok: true, headers: {}, text: '' } }
      }
      const held = w.holdPolls.shift()
      if (!held) await never
      if (held === 'throw') throw new Error('fetch failed: connection refused')
      const answer = await held
      if (answer.hold) await answer.hold
      return { value: { status: answer.status, ok: answer.status >= 200 && answer.status < 300, headers: {}, text: answer.text } }
    }
    const held = w.polls.shift()
    if (!held) await never
    const answer = await held!
    const r = 'status' in answer ? answer : { status: 200, text: JSON.stringify({ commands: [], cursor: '', ...answer }) }
    return { value: { status: r.status, ok: r.status >= 200 && r.status < 300, headers: {}, text: r.text } }
  })
  on('fs.write', async ($, e) => { w.writes.push(e); return { value: undefined } })
  on('tool.register', async ($, e) => { w.tools.push(e.name); return { value: { tool: `mcp__2mw2lt__${e.name}` } } })
  on('process.run', async ($, e) => {
    if (w.runAborted) {
      w.abortedRuns++
      throw new Error(`2mw2lt: $.process.run(${e.argv[0]}) aborted, the call interrupted`)
    }
    w.runs.push([...e.argv])
    w.cwds.push(e.init?.cwd)
    w.envs.push(e.init?.env)
    if (/\/(lease|card)\.py$/.test(String(e.argv[1]))) {
      w.wireStarted?.()
      if (w.wireGate) await w.wireGate
      w.stdins.push(e.init?.stdin)
      const own = e.init?.env?.CLAUDE_CODE_SESSION_ID ?? w.inherited
      const named = e.argv[e.argv.indexOf('--provider-session') + 1]
      if (own && own !== named) {
        return { value: { exitCode: 1, stdout: '', stderr: `refused: this harness is session ${own}, not ${named}: a session speaks only as itself`, isStdoutTruncated: false, isStderrTruncated: false } }
      }
      const out = w.leaseOut.shift() ?? 'relayed: 01AB to amber'
      return { value: { exitCode: /^(REJECTED|refused)/.test(out) ? 1 : 0, stdout: out, stderr: '', isStdoutTruncated: false, isStderrTruncated: false } }
    }
    if (e.argv[0] === 'mktemp') {
      return { value: { exitCode: 0, stdout: '/tmp/tmp.lessons\n', stderr: '', isStdoutTruncated: false, isStderrTruncated: false } }
    }
    if (String(e.argv[1]).endsWith('/checkpoint.py')) {
      w.stdins.push(e.init?.stdin)
      const out = w.checkpointOut.shift() ?? { code: 0, stdout: 'checkpointed: 7d1c0f2e-0000-4000-8000-000000000001 context\n', stderr: 'id 0123456789abcdef0123\n' }
      return { value: { exitCode: out.code, stdout: out.stdout, stderr: out.stderr, isStdoutTruncated: false, isStderrTruncated: false } }
    }
    if (e.argv.some(a => a.endsWith('/ack.py'))) {
      w.wireStarted?.()
      if (w.wireGate) await w.wireGate
    }
    if (String(e.argv[1]).endsWith('/seat_section.py')) {
      if (w.seatGate) await w.seatGate
      return { value: { exitCode: w.seat.code, stdout: w.seat.stdout, stderr: '', isStdoutTruncated: false, isStderrTruncated: false } }
    }
    if (String(e.argv[1]).endsWith('/bulletin.py')) {
      return { value: { exitCode: w.bulletin.code, stdout: w.bulletin.stdout, stderr: '', isStdoutTruncated: false, isStderrTruncated: false } }
    }
    const out = e.argv.includes('--session-of') ? (w.enrolled ? 's1\n' : e.argv[4] === undefined ? w.retainedName : '')
      : e.argv.includes('--claim-path') ? (w.claimFails ? '' : `/ws/.claude/steering-holders/${e.argv[3]}.json\n`)
      : e.argv.includes('--frame-path') ? `${FRAME_PATH}\n`
      : e.argv.includes('--keep') ? keep : ''
    if (e.argv.includes('--session-of') && w.holdNextLookup) {
      const gate = w.holdNextLookup
      w.holdNextLookup = undefined
      await gate
    }
    const exitCode = e.argv.includes('--session-of') ? w.sessionLookupCode
      : (e.argv.includes('--claim-path') && w.claimFails) || (e.argv.some(a => a.endsWith('/ack.py')) && w.ackFails)
        || (e.argv.includes('--read-say') && w.readFails) ? 1 : 0
    return { value: { exitCode, stdout: out, stderr: '', isStdoutTruncated: false, isStderrTruncated: false } }
  })
  on('process.spawn', async function* ($, e) {
    w.spawns.push([...e.argv])
    w.envs.push(e.env)
    w.claimsAtSpawn.push(w.writes.filter(write => JSON.parse(write.text).at > 0).length)
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
    w.asUser.push((e.origin as { asUser?: boolean } | undefined)?.asUser === true)
    if (submitRejects) throw new Error('the prompt was refused')
    if (w.submitDrops) return { drop: 'the session was stopped and nobody has sent input since' } as never
    // A submit 2.1.306 takes stores its row, passing session.append, before it resolves; a test
    // that stores the row itself holds the submit until it has.
    if (storesSubmits) await new Promise<void>(r => { w.stored = r })
    return { text: e.text }
  })
  on('tool.call', { tool: 'Bash' }, (async () => {
    if (w.bashThrows) throw new Error('the engine broke')
    return { result: w.bash }
  }) as never)
  on('turn.abort', async ($, e) => { w.aborts.push(e.turnId); return { value: undefined } })
  on('tool.call', { tool: 'TaskStop' }, (async ($: unknown, e: { task_id: string }) => {
    if (w.taskStopDeny) return { deny: w.taskStopDeny }
    w.taskStops.push(e.task_id)
    return { result: { message: `Successfully stopped task: ${e.task_id}`, task_id: e.task_id, task_type: 'local_bash' } }
  }) as never)
  on('ui.status', async ($, e) => {
    w.statuses.push(e.text)
    return { value: undefined }
  })
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
  on('turn.step', async function* ($, e) { for (const c of w.steps.shift() ?? []) yield c as never; return { turnId: e.turnId, index: e.index, answer: '', toolUses: [], stopReason: 'tool_use', usage: { model: 'claude-test', input_tokens: 1, output_tokens: 1, cache_read_input_tokens: w.stepUsage - 1, cache_creation_input_tokens: 0 } } as never })
  on('session.end', async ($, e) => ({ sessionId: e.sessionId }))
  on('classic.SessionStart', async () => ({}) as never)
  on('prompt.compose', async () => ({ sections: [{ id: 'intro', text: 'engine', scope: 'shared' as const }] }))
  on('tool.check', async () => w.check as never)
  return w
}

export const MESSAGE = { role: 'user' as const, text: 'hello', toolUses: [] }
export const never = new Promise<void>(() => {})
export const begin = async ($: any) => $.session.start({ cwd: '/ws/checkout', surface: 'terminal', isInteractive: true })
