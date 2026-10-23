// What the module reads and writes as text: hold.py's frame lines, the frame recording, the
// fixed rows it may append, and the version floor. Nothing here touches `$`.

export type Line = { id: string | null; kind: string; wakes: boolean; inform?: boolean; hold?: string; session?: string; why?: string; retry?: boolean; remedy?: string }

export const POINTER = 'Call mcp__2mw2lt__frames.'
export const CHECKPOINT = 'Compaction is near. Write your checkpoint with mcp__2mw2lt__checkpoint now.'
export const FLOOR = [2, 1, 289] as const

// What a hold that no retry outlives tells the session in a turn of its own (#6308): the refusal
// and its remedy, and the retire that may stand behind it, so the session exits or reconnects
// instead of going deaf until somebody types into it. The refusal a plugin hold re-arms for stays
// the module's alone (#5641).
export const refusalTurn = (why?: string, remedy?: string) =>
  `2mw2lt: this session's steering stream was refused for good${why ? `: ${why}` : ''}. ` +
  `If the seat retired this session, post any checkpoint due, run /2mw2lt:disconnect, and end the session; ` +
  `otherwise ${remedy || 'run /2mw2lt:connect'}.`

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
// prints; return the pending ones verbatim and in order, with each envelope's ulid for ack.py and
// each say's id for hold.py --read-say, which settles it as this tool returns it (#7136). A
// `bulletin` frame's text is collected separately: it reaches the model as one appended message
// at a turn's start, never as the `frames` tool's result.
export function pick(recording: string, pending: readonly string[]): { text: string; envelopes: string[]; envelopeIds: string[]; says: string[]; bulletins: string[]; found: string[]; read: { directives: string[]; seatGeneration?: number; seatFrameId?: string } } {
  const want = new Set(pending)
  const out: string[] = []
  const envelopes: string[] = []
  const envelopeIds: string[] = []
  const says: string[] = []
  const bulletins: string[] = []
  const found: string[] = []
  const directives: string[] = []
  let seatGeneration: number | undefined
  let seatFrameId: string | undefined
  for (const raw of recording.split('\n')) {
    if (!raw.startsWith('data: ')) continue
    let f: { kind?: unknown; ulid?: unknown; id?: unknown; directive?: unknown; generation?: unknown; text?: unknown }
    try { f = JSON.parse(raw.slice(6)) } catch { continue }
    if (!f || typeof f.id !== 'string' || !want.has(f.id)) continue
    want.delete(f.id)
    found.push(f.id)
    out.push(raw.slice(6))
    if (f.kind === 'envelope' && typeof f.ulid === 'string') { envelopes.push(f.ulid); envelopeIds.push(f.id); directives.push(f.ulid) }
    // A seat relay of a member's request carries its own directive id and stays the seat read's,
    // as it always was; a plain say settles under its frame id, which is its directive row's, when
    // this tool returns it (#7136).
    if (f.kind === 'say' && typeof f.directive === 'string') directives.push(f.directive)
    if (f.kind === 'say' && typeof f.directive !== 'string') says.push(f.id)
    if (f.kind === 'bulletin' && typeof f.text === 'string') bulletins.push(f.text)
    if (f.kind === 'seat' && typeof f.generation === 'number' && Number.isSafeInteger(f.generation)) {
      seatGeneration = f.generation
      seatFrameId = f.id
    }
  }
  return { text: out.join('\n'), envelopes, envelopeIds, says, bulletins, found, read: { directives, ...(seatGeneration !== undefined ? { seatGeneration, seatFrameId } : {}) } }
}

// The bulletin changes one turn appends stay within the same 1024-byte budget a change frame is
// rendered under (the bulletin design §5): whole lines in order while they fit beside the
// overflow line they may need, counted in UTF-8 bytes as Go's capLines counts them.
export const BULLETIN_TURN_MAX = 1024
const utf8 = new TextEncoder()
export function capBulletin(texts: readonly string[], max = BULLETIN_TURN_MAX): string {
  const rows = texts.join('\n').split('\n')
  for (let n = rows.length; n >= 0; n--) {
    const parts = rows.slice(0, n)
    const rest = rows.length - n
    if (rest > 0) parts.push(`+${rest} more: bulletin.py read`)
    const out = parts.join('\n')
    if (utf8.encode(out).length <= max) return out
  }
  return ''
}

// The section's first line names the head it was rendered at ("Bulletin r42: …"): the revision a
// fresh section holds every older change frame against.
export function sectionRevision(section: string): number | undefined {
  const m = /^Bulletin r(\d+):/.exec(section)
  return m ? Number(m[1]) : undefined
}

// How many ids of frames the model has read are kept to recognise a replay by.
export const READ_KEPT = 256

// Whether a frame that wakes the session earns a pointer: only one the `frames` tool can hand over
// (it finds frames by id) and that the session has neither been pointed at nor read. The stream
// reopens and the daemon replays frames it holds, so the same id arrives again and again.
export function owed(id: string | null, pending: readonly string[], read: readonly string[]): id is string {
  return id !== null && !pending.includes(id) && !read.includes(id)
}

export function pointing(pending: readonly string[], inform: readonly string[]): string[] {
  return pending.filter(id => !inform.includes(id))
}

export function quiet(n: number): string | undefined {
  return n > 0 ? `2mw2lt · ${n} quiet updates` : undefined
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

// The enrolment lifecycle verbs (#3342): a subagent presents its parent's credential — the harness
// hands its provider session id down — so its connect would mint nothing of its own and its
// disconnect would end the parent's enrolment. Which verb a command runs, or None: announce rides
// verb.py, and the other verb.py subcommands move the board, not the enrolment.
export function lifecycle(command: string): string | null {
  for (const name of ['connect', 'disconnect', 'checkpoint', 'ack', 'say'])
    if (invokes(command, name)) return name
  if (invokes(command, 'verb') && /\bverb\.py"?\s+announce\b/.test(command)) return 'announce'
  return null
}

// The seat's section (#4551 step 3): the knowledge's standing rulings, which seat_section.py composes for a
// session that holds the seat. It is a `session` section: after the boundary shared across
// organizations, and still in this session's own cached prefix, so it changes only when the seat does,
// or where the module may refresh it (`refresh`): the text from 0, nothing from 1, and what the
// session already had from anything else, a door that did not answer included.
export const SEAT_SECTION = '2mw2lt:seat'
export const BULLETIN_SECTION = '2mw2lt:bulletin'
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
export type SeatCall = { client: 'say'; stdin: string } | { client: 'reply'; stdin: string } | { client: 'post'; path: string; stdin: string } | { client: 'card'; argv: string[] }
type Field = { type: 'string' | 'integer' | 'boolean' | 'array'; description: string; word?: boolean; lines?: boolean; enum?: string[] }
type SeatTool = { description: string; fields: Record<string, Field>; required: string[]; oneOf?: string[]; refuse?: (a: Record<string, unknown>) => string | null; build: (a: Record<string, unknown>) => SeatCall }

const str = (a: Record<string, unknown>, k: string) => String(a[k] ?? '')
const word = (description: string): Field => ({ type: 'string', description, word: true })
const text = (description: string): Field => ({ type: 'string', description })
// A field that keeps its lines: the client reads stdin whole as one verb, so a newline is text.
const paragraphs = (description: string): Field => ({ type: 'string', description, lines: true })

export const SEAT_TOOLS: Record<string, SeatTool> = {
  relay: {
    description: 'The seat, or a goal\'s steward for a session its launches started: relay a directive to a session. The daemon frames the seat\'s as on the owner\'s behalf, and a steward\'s as its own, within the seat\'s bounds.',
    fields: { to: word('The session to direct.'), epoch: { type: 'integer', description: 'The session\'s enrolment epoch, when it must be that one.' }, checkpoint: word('A checkpoint\'s id, when the session takes over from it: the daemon hands it the checkpoint\'s text after the directive, so do not paste the text.'), text: paragraphs('The directive, naming the card. Paragraphs and lists keep their lines, and the session reads it as markdown.') },
    required: ['to', 'text'],
    build: a => ({ client: 'say', stdin: `relay: token @lease to ${str(a, 'to')}${a.epoch !== undefined ? `@${str(a, 'epoch')}` : ''}${a.checkpoint !== undefined ? ` checkpoint ${str(a, 'checkpoint')}` : ''} ${str(a, 'text')}` }),
  },
  dispose: {
    description: 'Seat only: close one of the brain\'s Needs You rows, with a reason the owner can read.',
    fields: { item: word('The row\'s id.'), reason: text('Why it is closed.') },
    required: ['item', 'reason'],
    build: a => ({ client: 'say', stdin: `dispose: token @lease ${str(a, 'item')} ${str(a, 'reason')}` }),
  },
  'out-of-band': {
    description: 'Seat only: record on one of the owner\'s Needs You rows the answer the owner gave somewhere else (#6325). The row closes as the owner\'s answer relayed by you — a ruling on their behalf, theirs to acknowledge or correct, never a seat dismissal — and the session that raised it is told, so it does not wait on a row nobody will answer. An ask is refused: the owner answers it in the desk.',
    fields: { item: word('The row\'s id.'), channel: text('Where the owner gave the answer, and when: such as "the owner\'s chat, 2026-10-13T13:20Z".'), words: text('The owner\'s decision as they gave it.') },
    required: ['item', 'channel', 'words'],
    build: a => ({ client: 'say', stdin: `out-of-band: token @lease ${str(a, 'item')} via ${str(a, 'channel')}: ${str(a, 'words')}` }),
  },
  retire: {
    description: 'The seat, or a goal\'s steward for a session its launches started: retire a session, ending its process and its enrolment. Refused while it has unpublished work. A Go workspace takes the reason; the incumbent takes the node.',
    fields: { worker: word('The worker\'s session.'), node: word('The machine it runs on, on the incumbent.'), reason: text('Why, on a Go workspace.') },
    required: ['worker'],
    oneOf: ['node', 'reason'],
    build: a => ({ client: 'say', stdin: a.node !== undefined
      ? `retire: token @lease ${str(a, 'worker')} on ${str(a, 'node')}`
      : `retire: token @lease ${str(a, 'worker')} because ${str(a, 'reason')}` }),
  },
  wake: {
    description: 'The seat, or a goal\'s steward for a session its launches started: wake a session that has gone dark.',
    fields: { target: word('The session to wake.'), reason: text('Why.') },
    required: ['target', 'reason'],
    build: a => ({ client: 'say', stdin: `wake: token @lease ${str(a, 'target')} because ${str(a, 'reason')}` }),
  },
  rehome: {
    description: 'Seat only: move a stopped Claude Code session to another account on the same machine, keeping its conversation. Name the account as for a launch, by its number under its vendor (Anthropic when vendor is not given), and the model and effort it resumes on, in that vendor\'s own ids; without them it resumes on the launcher\'s defaults. compact runs /compact on the account it leaves first; without_checkpoint, only with compact, skips the checkpoint that compact otherwise requires.',
    fields: { session: word('The session to move.'), vendor: word('The account\'s vendor, such as zai; Anthropic when not given.'),
              account: word('The account to move it to: the team\'s number for it, or its id.'),
              model: word('The model it resumes on, in its vendor\'s own ids, such as glm-5.3.'),
              effort: { type: 'string', enum: ['low', 'medium', 'high', 'xhigh', 'max'], description: 'The effort it resumes at.' },
              compact: { type: 'boolean', description: 'Compact the session before it moves.' },
              without_checkpoint: { type: 'boolean', description: 'Compact without a current checkpoint.' }, reason: text('Why.') },
    required: ['session', 'account', 'reason'],
    refuse: a => a.without_checkpoint === true && a.compact !== true ? 'without_checkpoint is only with compact' : null,
    build: a => ({ client: 'say', stdin: `rehome: token @lease ${str(a, 'session')} to${a.vendor !== undefined ? ` vendor ${str(a, 'vendor')}` : ''} ${str(a, 'account')}${
      a.model !== undefined ? ` model ${str(a, 'model')}` : ''}${a.effort !== undefined ? ` effort ${str(a, 'effort')}` : ''}${a.compact === true
      ? (a.without_checkpoint === true ? ' compact without checkpoint' : ' compact') : ''} because ${str(a, 'reason')}` }),
  },
  park: {
    description: 'The seat, or a goal\'s steward for a session its launches started: park an idle Claude Code session: its process stops and frees its worker slot, its enrolment, cards and queued messages stay, and a wake resumes it with its conversation. Refused unless idle. The park stands an hour unless until says otherwise; then you are told it is over.',
    fields: { session: word('The session to park.'), until: word('When the park ends: a time (RFC 3339) or a duration such as 45m or 2h.'), reason: text('What needs its slot.') },
    required: ['session', 'reason'],
    build: a => ({ client: 'say', stdin: `park: token @lease ${str(a, 'session')}${a.until !== undefined ? ` until ${str(a, 'until')}` : ''} because ${str(a, 'reason')}` }),
  },
  keep: {
    description: 'The seat, or a goal\'s steward for a session its launches started: keep a finished session idle on purpose, because work in its territory is expected soon. Name the plan as the reason; the keep stands half an hour unless until says otherwise, and work placed on the session ends it.',
    fields: { session: word('The session to keep.'), until: word('When the keep ends: a time (RFC 3339) or a duration such as 45m or 2h.'), reason: text('The work expected in its territory, and when.') },
    required: ['session', 'reason'],
    build: a => ({ client: 'say', stdin: `keep: token @lease ${str(a, 'session')}${a.until !== undefined ? ` until ${str(a, 'until')}` : ''} because ${str(a, 'reason')}` }),
  },
  unkeep: {
    description: 'The seat, or a goal\'s steward for a session its launches started: clear the keep on a session.',
    fields: { session: word('The kept session.') },
    required: ['session'],
    build: a => ({ client: 'say', stdin: `unkeep: token @lease ${str(a, 'session')}` }),
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
              major: { type: 'boolean', description: 'Declare the work major.' }, card: word('The card id to scope under, to retry one.'),
              criticality: { type: 'string', description: 'Triage: how much breaks if the work is wrong.', enum: ['critical', 'major', 'minor', 'trivial'] },
              size: { type: 'string', description: 'Triage: how large the work is.', enum: ['S', 'M', 'L', 'XL'] } },
    required: ['name', 'track'],
    build: a => ({ client: 'card', argv: ['scope', ...(a.card !== undefined ? ['--card', str(a, 'card')] : []), str(a, 'name'), str(a, 'track'),
                                         ...((a.anchors as unknown[] | undefined) ?? []).map(String), ...(a.major === true ? ['major'] : []),
                                         ...(a.criticality !== undefined || a.size !== undefined ? [`triage:${str(a, 'criticality')},${str(a, 'size')}`] : [])] }),
  },
  card_session: {
    description: 'Seat only: name a session as a card\'s executor or planned one.',
    fields: { card: word('The card.'), session: word('The session.'), role: { type: 'string', description: 'Its role.', enum: ['executor', 'planned'] } },
    required: ['card', 'session', 'role'],
    build: a => ({ client: 'card', argv: ['session', str(a, 'card'), str(a, 'session'), str(a, 'role')] }),
  },
  answer: {
    description: 'Seat only: answer a snapshot or a regroup by its episode once you have reviewed it: acted, saying what you did, or considered, saying why nothing needed you. This is how a snapshot is answered, never by speaking to the owner.',
    fields: { episode: word('The episode the signal carries.'), verdict: { type: 'string', description: 'What you did with it.', enum: ['acted', 'considered'] },
              because: text('What you did, or why nothing needed you.') },
    required: ['episode', 'verdict', 'because'],
    build: a => ({ client: 'say', stdin: `answer: token @lease ${str(a, 'episode')} ${str(a, 'verdict')} because ${str(a, 'because')}` }),
  },
  say: {
    description: 'Seat only: speak to the owner on the Direct line, as the brain: first, again, or about a say they sent. Each call is one message. Never to answer a frame: reading one acknowledged it, and a snapshot is answered with the answer tool.',
    fields: { text: text('What you say.'), request: word('The id of the owner\'s say this is about, if it is about one.') },
    required: ['text'],
    build: a => ({ client: 'reply',
                   stdin: JSON.stringify({ lease_token: '@lease', text: str(a, 'text'), ...(a.request !== undefined ? { request: str(a, 'request') } : {}) }) }),
  },
}

export function seatSchema(name: string) {
  const t = SEAT_TOOLS[name]!
  const properties = Object.fromEntries(Object.entries(t.fields).map(([k, f]) =>
    [k, { type: f.type, description: f.description, ...(f.enum ? { enum: f.enum } : {}), ...(f.type === 'array' ? { items: { type: 'string' } } : {}) }]))
  return { type: 'object', properties, required: t.required, additionalProperties: false }
}

// What one call sends, or why it is refused. A control character could end the verb line and start
// another, and a word field with a space would shift every field after it. A field that keeps its
// lines is the verb's last, which lease.py reads to the end of stdin, so only a newline and a tab pass.
const CONTROL = /[\u0000-\u001f\u007f]/
const CONTROL_BUT_LINES = /[\u0000-\u0008\u000b-\u001f\u007f]/
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
      if (f.lines ? CONTROL_BUT_LINES.test(s) : CONTROL.test(s)) return { refused: f.lines ? `${k} carries a control character other than a newline or a tab` : `${k} carries a control character or a newline` }
      if ((f.word || f.type === 'array') && (/\s/.test(s) || s === '')) return { refused: `${k} is one word` }
    }
    if (f.enum && !f.enum.includes(String(v))) return { refused: `${k} is one of ${f.enum.join(', ')}` }
    if (f.type === 'integer' && !Number.isInteger(v)) return { refused: `${k} is a whole number` }
  }
  const why = t.refuse?.(args)
  if (why) return { refused: why }
  return t.build(args)
}

// lease.py prints this as its last line when a send may have been registered: the same line goes
// again under the id, and the door answers it from its record.
export function resendId(stdout: string): string | null {
  const m = /resend with --retry=([A-Za-z0-9-]+)\s*$/.exec(stdout)
  return m ? m[1]! : null
}

// Why a seat tool's call, or the checkpoint tool's, is refused at its entry, or null to send it. A subagent's call, another
// plugin's, and one the session's permission settings do not allow outright are refused: the
// plugin answers above the permission system and cannot show its dialog (doc 175 §8).
export type Entry = { own: string; verbs: string; client: string }
export const SEAT_ENTRY: Entry = { own: "a seat verb is the brain's own", verbs: "the seat's verbs", client: 'lease.py' }
export const CHECKPOINT_ENTRY: Entry = { own: "a checkpoint is the session's own", verbs: 'the checkpoint tool', client: 'checkpoint.py' }
export function entryRefusal(agentId: string | undefined, origin: string, tool: string,
                             check: { decision: 'allow' | 'ask' | 'deny'; reason?: string }, entry: Entry = SEAT_ENTRY): string | null {
  if (agentId !== undefined) return `refused: ${entry.own}, never a subagent's.`
  if (origin !== 'engine') return `refused: ${origin} may not call ${entry.verbs}.`
  if (check.decision === 'allow') return null
  return `refused: ${check.reason ?? `this session's permission settings ${check.decision === 'ask' ? 'ask before' : 'deny'} ${tool}`}. `
    + `The plugin cannot show the permission dialog; run the same verb through ${entry.client} in Bash, where it applies.`
}

// The checkpoint tool (#6324): checkpoint.py's arguments, the text for its stdin and the lessons
// for its --learned-file, or why the call is refused. checkpoint.py judges the text and the lessons.
export const CHECKPOINT_BOUNDARIES = ['spec-settled', 'unit-done', 'context', 'handover', 'exit']
export const CHECKPOINT_TOOL = {
  description: "Hand this session's checkpoint to the platform, which scans and keeps its text (/2mw2lt:checkpoint says what goes in it). "
    + 'With mirror, the same text is posted as a comment on that pull request or issue once the platform has kept it.',
  inputSchema: {
    type: 'object',
    properties: {
      boundary: { type: 'string', enum: CHECKPOINT_BOUNDARIES, description: 'The boundary this checkpoint is written at.' },
      text: { type: 'string', maxLength: 8192, description: 'The note, as /2mw2lt:checkpoint lays it out: at most 8192 bytes.' },
      learned: { type: 'array', description: 'At most eight lessons: each one rule and the mistake it prevents, scoped to repository paths or "*".',
                 items: { type: 'object', properties: { text: { type: 'string' }, scope: { type: 'array', items: { type: 'string' } } },
                          required: ['text', 'scope'], additionalProperties: false } },
      mirror: { type: 'string', description: 'A GitHub pull request or issue URL to post the note on.' },
    },
    required: ['boundary', 'text'],
    additionalProperties: false,
  },
}
export function checkpointCall(args: Record<string, unknown>): { boundary: string; text: string; learned?: string; mirror?: string } | { refused: string } {
  for (const k of Object.keys(args)) if (!['boundary', 'text', 'learned', 'mirror'].includes(k)) return { refused: `checkpoint takes no ${k}` }
  const { boundary, text, learned, mirror } = args
  if (typeof boundary !== 'string' || !CHECKPOINT_BOUNDARIES.includes(boundary)) return { refused: `boundary is one of ${CHECKPOINT_BOUNDARIES.join(', ')}` }
  if (typeof text !== 'string' || text === '') return { refused: 'text is required' }
  if (learned !== undefined && !Array.isArray(learned)) return { refused: 'learned is a list' }
  if (mirror !== undefined && (typeof mirror !== 'string' || mirror === '')) return { refused: 'mirror is a URL' }
  return { boundary, text, ...(learned !== undefined ? { learned: JSON.stringify(learned) } : {}), ...(mirror !== undefined ? { mirror } : {}) }
}
