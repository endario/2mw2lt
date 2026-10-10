// What the module reads and writes as text: hold.py's frame lines, the frame recording, the
// fixed rows it may append, and the version floor. Nothing here touches `$`.

export type Line = { id: string | null; kind: string; wakes: boolean }

export const POINTER = 'A steering frame is waiting. Call mcp__2mw2lt__frames to read it.'
export const CHECKPOINT = 'Compaction is near. Write your checkpoint (/2mw2lt:checkpoint) now.'
export const CONNECT_AGAIN = 'The steering stream was refused. Run /2mw2lt:connect again.'
export const FLOOR = [2, 1, 289] as const

// The checkpoint prompt goes this far along the way to the engine's auto-compact point, which its
// settings, the account and the model decide. It is not a share of the window: a session set to
// compact at 500k on a 1M model compacts at 47% of it. The window stands in only with auto-compaction off.
export const CHECKPOINT_AT = 0.85

// What a response was answered over: uncached, cache-written and cache-read input together, the
// figure the status line reports as the context's tokens.
export function tokensOf(u: { input_tokens?: number; cache_read_input_tokens?: number; cache_creation_input_tokens?: number }): number {
  return (u.input_tokens ?? 0) + (u.cache_read_input_tokens ?? 0) + (u.cache_creation_input_tokens ?? 0)
}

export function due(tokens: number | undefined, point: number | undefined): boolean {
  return tokens !== undefined && point !== undefined && point > 0 && tokens >= point * CHECKPOINT_AT
}

export function lines(buffer: string): { complete: Line[]; rest: string } {
  const parts = buffer.split('\n')
  const rest = parts.pop() ?? ''
  const complete: Line[] = []
  for (const raw of parts) {
    try {
      const l = JSON.parse(raw)
      if (l && typeof l.kind === 'string') complete.push(l)
    } catch { /* not a frame line */ }
  }
  return { complete, rest }
}

export function atLeast(version: string, floor: readonly number[] = FLOOR): boolean {
  const v = version.split(/[.\s-]/).slice(0, 3).map(Number)
  for (let i = 0; i < 3; i++) {
    const have = v[i] ?? 0, want = floor[i] ?? 0
    if (have !== want) return have > want
  }
  return true
}

// The recording holds `data: {json}` lines. Every frame carries the daemon's `id`, which hold.py
// prints; return the pending ones verbatim and in order, with each envelope's ulid for ack.py.
export function pick(recording: string, pending: readonly string[]): { text: string; envelopes: string[]; envelopeIds: string[]; found: string[]; read: { directives: string[]; seatGeneration?: number; seatFrameId?: string } } {
  const want = new Set(pending)
  const out: string[] = []
  const envelopes: string[] = []
  const envelopeIds: string[] = []
  const found: string[] = []
  const directives: string[] = []
  let seatGeneration: number | undefined
  let seatFrameId: string | undefined
  for (const raw of recording.split('\n')) {
    if (!raw.startsWith('data: ')) continue
    let f: { kind?: unknown; ulid?: unknown; id?: unknown; directive?: unknown; generation?: unknown }
    try { f = JSON.parse(raw.slice(6)) } catch { continue }
    if (!f || typeof f.id !== 'string' || !want.has(f.id)) continue
    want.delete(f.id)
    found.push(f.id)
    out.push(raw.slice(6))
    if (f.kind === 'envelope' && typeof f.ulid === 'string') { envelopes.push(f.ulid); envelopeIds.push(f.id); directives.push(f.ulid) }
    if (f.kind === 'say') directives.push(typeof f.directive === 'string' ? f.directive : f.id)
    if (f.kind === 'seat' && typeof f.generation === 'number' && Number.isSafeInteger(f.generation)) {
      seatGeneration = f.generation
      seatFrameId = f.id
    }
  }
  return { text: out.join('\n'), envelopes, envelopeIds, found, read: { directives, ...(seatGeneration !== undefined ? { seatGeneration, seatFrameId } : {}) } }
}

// How many ids of frames the model has read are kept to recognise a replay by.
export const READ_KEPT = 256

// Whether a frame that wakes the session earns a pointer: only one the `frames` tool can hand over
// (it finds frames by id) and that the session has neither been pointed at nor read. The stream
// reopens and the daemon replays frames it holds, so the same id arrives again and again.
export function owed(id: string | null, pending: readonly string[], read: readonly string[]): id is string {
  return id !== null && !pending.includes(id) && !read.includes(id)
}

export function framesEntry(agentId: string | undefined, origin: string): boolean {
  return agentId === undefined && origin === 'engine'
}

// lease.py preserves a superseded generation inside its holder wrapper, but reports an expired one
// as the Refused form itself. Only those native forms make their returned seat notice obsolete.
export function obsoleteSeatRefusal(deny: string): boolean {
  return /^(?:refused: this session does not hold the seat \(refused \(\d+\): seat-generation-stale(?: \[[^\]\r\n]+\])?(?: request [^\s)\r\n]+)?\)|refused: refused \(\d+\): seat-lapsed(?: \[[^\]\r\n]+\])?(?: request [^\s\r\n]+)?)$/.test(deny)
}

// That a Bash command runs the named client: python3 (or python) on a path ending
// <dir>/<name>.py, steering/enroll by default, and not for its help. A read of the file (cat, grep)
// is not a run.
export function invokes(command: string, name: string, dir = 'steering/enroll'): boolean {
  // The path is a bare word, or a double-quoted argument that may hold spaces and a quoted
  // command substitution of its own ("$(dirname "$x")/steering/enroll/connect.py").
  const path = `(?:"[^\\n]*?${dir}/${name}\\.py"|[^\\s";&|]*${dir}/${name}\\.py)`
  const run = new RegExp(`(?:^|[\\s;&|(])python3?\\s+${path}(?=[\\s;&|)]|$)([^;&|\\n]*)`, 'g')
  for (const m of command.matchAll(run)) if (!/(^|\s)(--help|-h)(\s|$)/.test(m[1] ?? '')) return true
  return false
}

// The seat's section (#4551 step 3): the knowledge's standing rulings, which seat_section.py composes for a
// session that holds the seat. It is a `session` section: after the boundary shared across
// organizations, and still in this session's own cached prefix, so it changes only when the seat does,
// or where the module may refresh it (`refresh`): the text from 0, nothing from 1, and what the
// session already had from anything else, a door that did not answer included.
export const SEAT_SECTION = '2mw2lt:seat'
export function nextSeat(was: string, code: number, stdout: string, refresh: boolean): string {
  if (code === 1) return ''
  if (code !== 0) return was
  return refresh || !was ? stdout.trim() : was
}

// The hold lines that may mean the seat changed hands: the seat frame itself, and for a holder a
// kick (only the holder is kicked) or a say (how the daemon tells a holder the seat was taken).
export function asksSeat(kind: string): boolean {
  return kind === 'seat' || kind === 'kick' || kind === 'say'
}

// The seat's lease verbs as typed tools (#4551 step 2). Each is a thin wrapper over the Python
// client: a `say` line or a body for lease.py, or card.py's argv, with `@lease` where the token
// goes. The module never holds the token; the client fills it from the store.
export type SeatCall = { client: 'say'; stdin: string } | { client: 'post'; path: string; stdin: string } | { client: 'card'; argv: string[] }
type Field = { type: 'string' | 'integer' | 'boolean' | 'array'; description: string; word?: boolean; enum?: string[] }
type SeatTool = { description: string; fields: Record<string, Field>; required: string[]; oneOf?: string[]; build: (a: Record<string, unknown>) => SeatCall }

const str = (a: Record<string, unknown>, k: string) => String(a[k] ?? '')
const word = (description: string): Field => ({ type: 'string', description, word: true })
const text = (description: string): Field => ({ type: 'string', description })

export const SEAT_TOOLS: Record<string, SeatTool> = {
  relay: {
    description: 'Seat only: relay a directive to a session. The daemon frames it as on the owner\'s behalf.',
    fields: { to: word('The session to direct.'), epoch: { type: 'integer', description: 'The session\'s enrolment epoch, when it must be that one.' }, text: text('The directive, naming the card.') },
    required: ['to', 'text'],
    build: a => ({ client: 'say', stdin: `relay: token @lease to ${str(a, 'to')}${a.epoch !== undefined ? `@${str(a, 'epoch')}` : ''} ${str(a, 'text')}` }),
  },
  dispose: {
    description: 'Seat only: close one of the brain\'s Needs You rows, with a reason the owner can read.',
    fields: { item: word('The row\'s id.'), reason: text('Why it is closed.') },
    required: ['item', 'reason'],
    build: a => ({ client: 'say', stdin: `dispose: token @lease ${str(a, 'item')} ${str(a, 'reason')}` }),
  },
  retire: {
    description: 'Seat only: retire a session, ending its process and its enrolment. Refused while it has unpublished work. A Go workspace takes the reason; the incumbent takes the node.',
    fields: { worker: word('The worker\'s session.'), node: word('The machine it runs on, on the incumbent.'), reason: text('Why, on a Go workspace.') },
    required: ['worker'],
    oneOf: ['node', 'reason'],
    build: a => ({ client: 'say', stdin: a.node !== undefined
      ? `retire: token @lease ${str(a, 'worker')} on ${str(a, 'node')}`
      : `retire: token @lease ${str(a, 'worker')} because ${str(a, 'reason')}` }),
  },
  wake: {
    description: 'Seat only: wake a session that has gone dark.',
    fields: { target: word('The session to wake.'), reason: text('Why.') },
    required: ['target', 'reason'],
    build: a => ({ client: 'say', stdin: `wake: token @lease ${str(a, 'target')} because ${str(a, 'reason')}` }),
  },
  card_reclassify: {
    description: 'Seat only: correct a card\'s track, significance, state, priority or major mark.',
    fields: { card: word('The card.'), field: { type: 'string', description: 'What to correct.', enum: ['track', 'significance', 'state', 'priority', 'major'] },
              value: word('The new value; for track a lane id or off-track.'), why: text('Why.') },
    required: ['card', 'field', 'value', 'why'],
    build: a => ({ client: 'card', argv: ['reclassify', str(a, 'card'), str(a, 'field'), str(a, 'value'), str(a, 'why')] }),
  },
  card_retire: {
    description: 'Seat only: retire a card that no longer describes real work.',
    fields: { card: word('The card.'), why: text('Why.') },
    required: ['card', 'why'],
    build: a => ({ client: 'card', argv: ['retire', str(a, 'card'), str(a, 'why')] }),
  },
  card_scope: {
    description: 'Seat only: make placed work a card.',
    fields: { name: text('The card\'s name.'), track: word('Its lane.'), anchors: { type: 'array', description: 'Anchors, as resolves:<n> or advances:<n>.' },
              major: { type: 'boolean', description: 'Declare the work major.' }, card: word('The card id to scope under, to retry one.') },
    required: ['name', 'track'],
    build: a => ({ client: 'card', argv: ['scope', ...(a.card !== undefined ? ['--card', str(a, 'card')] : []), str(a, 'name'), str(a, 'track'),
                                         ...((a.anchors as unknown[] | undefined) ?? []).map(String), ...(a.major === true ? ['major'] : [])] }),
  },
  card_session: {
    description: 'Seat only: name a session as a card\'s executor or planned one.',
    fields: { card: word('The card.'), session: word('The session.'), role: { type: 'string', description: 'Its role.', enum: ['executor', 'planned'] } },
    required: ['card', 'session', 'role'],
    build: a => ({ client: 'card', argv: ['session', str(a, 'card'), str(a, 'session'), str(a, 'role')] }),
  },
  say: {
    description: 'Seat only: reply to a say, as the brain.',
    fields: { text: text('The reply.'), key: word('The say\'s id, when replying to one.') },
    required: ['text'],
    build: a => ({ client: 'post', path: '/steering/brain/reply',
                   stdin: JSON.stringify({ lease_token: '@lease', text: str(a, 'text'), ...(a.key !== undefined ? { key: str(a, 'key') } : {}) }) }),
  },
}

export function seatSchema(name: string) {
  const t = SEAT_TOOLS[name]!
  const properties = Object.fromEntries(Object.entries(t.fields).map(([k, f]) =>
    [k, { type: f.type, description: f.description, ...(f.enum ? { enum: f.enum } : {}), ...(f.type === 'array' ? { items: { type: 'string' } } : {}) }]))
  return { type: 'object', properties, required: t.required, additionalProperties: false }
}

// What one call sends, or why it is refused. A control character could end the verb line and start
// another, and a word field with a space would shift every field after it.
const CONTROL = /[\u0000-\u001f\u007f]/
export function seatCall(name: string, args: Record<string, unknown>): SeatCall | { refused: string } {
  const t = SEAT_TOOLS[name]
  if (!t) return { refused: `no seat tool ${name}` }
  for (const k of t.required) if (args[k] === undefined || args[k] === '') return { refused: `${k} is required` }
  if (t.oneOf && t.oneOf.filter(k => args[k] !== undefined && args[k] !== '').length !== 1) return { refused: `${name} takes one of ${t.oneOf.join(' or ')}` }
  for (const [k, v] of Object.entries(args)) {
    const f = t.fields[k]
    if (!f) return { refused: `${name} takes no ${k}` }
    const values = Array.isArray(v) ? v : [v]
    for (const x of values) {
      const s = String(x)
      if (CONTROL.test(s)) return { refused: `${k} carries a control character or a newline` }
      if ((f.word || f.type === 'array') && (/\s/.test(s) || s === '')) return { refused: `${k} is one word` }
    }
    if (f.enum && !f.enum.includes(String(v))) return { refused: `${k} is one of ${f.enum.join(', ')}` }
    if (f.type === 'integer' && !Number.isInteger(v)) return { refused: `${k} is a whole number` }
  }
  return t.build(args)
}

// lease.py prints this as its last line when a send may have been registered: the same line goes
// again under the id, and the door answers it from its record.
export function resendId(stdout: string): string | null {
  const m = /resend with --retry=([A-Za-z0-9-]+)\s*$/.exec(stdout)
  return m ? m[1]! : null
}

// Why a seat tool's call is refused at its entry, or null to send it. A subagent's call, another
// plugin's, and one the session's permission settings do not allow outright are refused: the
// plugin answers above the permission system and cannot show its dialog (doc 175 §8).
export function entryRefusal(agentId: string | undefined, origin: string, tool: string,
                             check: { decision: 'allow' | 'ask' | 'deny'; reason?: string }): string | null {
  if (agentId !== undefined) return "refused: a seat verb is the brain's own, never a subagent's."
  if (origin !== 'engine') return `refused: ${origin} may not call the seat's verbs.`
  if (check.decision === 'allow') return null
  return `refused: ${check.reason ?? `this session's permission settings ${check.decision === 'ask' ? 'ask before' : 'deny'} ${tool}`}. `
    + 'The plugin cannot show the permission dialog; run the same verb through lease.py in Bash, where it applies.'
}
