import type { Args, ResultOf } from 'claude-code'

// The live view's events (#4823, design §5): the main conversation's rows, turns and compactions,
// numbered for the machine agent. Pure: register.ts owns every call on `$` (the engine allows `$`
// only at its own call sites), and this holds the shapes and the numbering.

type Append = Args<'session.append'>

export type Block = { type: 'text' | 'thinking' | 'tool-use' | 'tool-result'; id?: string; text?: string; tool?: string; input?: unknown; error?: true }
// `replaces` is the key of the streamed block a response row takes the place of (#8282).
export type Row = { id?: string; role: 'user' | 'assistant' | 'tool-result' | 'system'; origin: string; act?: string; replaces?: string; blocks: Block[] }
export type Turn = { state: 'started' | 'completed'; reason?: 'answered' | 'interrupted' | 'error' }
export type Ask = { id: string; kind: 'permission' | 'question'; tool?: string; options?: string[] }
export type Settled = { ask: string; by: string; answer?: unknown }
// What one streamed block grew by since the last send. It leaves as written: the block's row is scanned.
export type Delta = { key: string; block: 'text' | 'thinking'; text: string; offset: number; stopped?: true }
export type Payload = { kind: 'row' | 'compaction'; row: Row } | { kind: 'turn'; turn: Turn }
  | { kind: 'ask'; ask: Ask } | { kind: 'ask-settled'; settled: Settled } | { kind: 'delta'; delta: Delta }
// One of the agent's commands on the poll: a kind and that kind's body. `emit` asks the module to
// number an event the agent saw, `prompt` to land words from the desk, `interrupt` to stop its turn,
// `command` to run a slash command.
export type Command = { kind?: unknown; body?: unknown }
// What a desk command did (#8432): dispatched as its own turn, waiting for the busy turn to end, or
// run as a local command with what it resolved to; or why it did not run.
export type Ran = { landed: 'new-turn' | 'queued' | 'completed'; why?: string } | { landed: 'ran'; result?: string } | { refused: string }
// Where a prompt landed, as the agent's receipt says it (design §8.2): submitted as a new turn,
// appended to the running one, or appended for the next request with why it started none.
export type Landing = { landed: 'new-turn' | 'running-turn' | 'queued'; why?: string } | { refused: string }
// What an interrupt did, as the agent's receipt says it: the turn stopped, the background tasks
// the stop left running ended or not with why, whether the turn's end came too late to see them
// all, and whether the model was told; or why it did not.
export type Stopping = { interrupted: true; stopped: string[]; unstopped: string[]; unseen: boolean; told: boolean } | { refused: string }
export type Event = { v: 1; session: string; seq: number } & (Payload | { kind: 'gap'; gap: { from: number; to: number } })

export const DEFAULT_PORT = '9990'

// Who wrote a user row, as history reads the stored origin (machine/internal/surface/claude's
// originOf): the person at the terminal or on Remote Control, or with no stamp the ingress could
// name, which Claude stores as no origin; a plugin; and the engine otherwise.
function originOf(o: Append['origin'], meta: boolean): string {
  switch (o.kind) {
    case 'composer': case 'bridge': case 'sdk': return 'person'
    case 'unclassified': return meta ? 'engine' : 'person'
    case 'plugin': return 'plugin'
    default: return 'engine'
  }
}

const str = (v: unknown) => typeof v === 'string' ? v : undefined

function textOf(content: unknown): string {
  if (typeof content === 'string') return content
  if (!Array.isArray(content)) return ''
  return content.filter(b => b?.type === 'text' && typeof b.text === 'string').map(b => b.text as string).join('\n')
}

// An empty text is left out, as the agent's encoding leaves it out of what it sends on.
const spoken = (text: string) => text ? { text } : {}

// A block the event has no kind for (an image, a document, redacted thinking) is left out.
export function blocksOf(content: readonly { type: string; [field: string]: unknown }[]): Block[] {
  const out: Block[] = []
  for (const b of content) {
    if (b.type === 'text' && typeof b.text === 'string') out.push({ type: 'text', ...spoken(b.text) })
    else if (b.type === 'thinking' && typeof b.thinking === 'string' && b.thinking.trim() !== '') out.push({ type: 'thinking', text: b.thinking })
    else if (b.type === 'tool_use') out.push({ type: 'tool-use', id: str(b.id), tool: str(b.name), input: b.input })
    else if (b.type === 'tool_result') out.push({ type: 'tool-result', id: str(b.tool_use_id), ...spoken(textOf(b.content)), ...(b.is_error === true ? { error: true as const } : {}) })
  }
  return out
}

// The name this plugin's rows are stored under: its own rows that open with a from: line are that
// speaker's words.
const OWN = '2mw2lt'
const FROM_LINE = /^from: ([^\n]+)\n\n/
const COMMAND_TAG = /<(command-message|command-name|command-args)>([\s\S]*?)<\/(command-message|command-name|command-args)>/g
const LOCAL_OUTPUT = /^\s*<local-command-(?:stdout|stderr)>([\s\S]*)<\/local-command-(?:stdout|stderr)>\s*$/

// The command line a slash command's stored record was typed as, `/name args`, or undefined for text
// that is not wholly such a record.
export function commandLine(text: string): string | undefined {
  let name = '', args = '', rest = '', at = 0
  for (const m of text.matchAll(COMMAND_TAG)) {
    if (m[1] !== m[3]) return undefined
    rest += text.slice(at, m.index)
    at = m.index + m[0].length
    if (m[1] === 'command-name') name = m[2]!
    if (m[1] === 'command-args') args = m[2]!
  }
  rest += text.slice(at)
  if (!name || rest.trim()) return undefined
  return args.trim() ? `${name} ${args.trim()}` : name
}

// A user row's words as the view draws them: the speaker a from: line names on this plugin's own
// rows, the line itself not drawn though the model read it; a slash command as its command line;
// and what a local command printed, which is the harness's.
function said(row: Row, own: boolean) {
  row.blocks.forEach((b, i) => {
    if (b.type !== 'text' || b.text === undefined) return
    const from = FROM_LINE.exec(b.text), line = commandLine(b.text), printed = LOCAL_OUTPUT.exec(b.text)
    if (own && i === 0 && from) { row.origin = from[1]!; b.text = b.text.slice(from[0].length) }
    else if (line !== undefined) b.text = line
    else if (printed) { row.origin = 'engine'; b.text = printed[1]! }
  })
}

// Whether the view draws anything of a row: a call, its result (drawn under the call), a thought, or
// words that are not blank.
const drawn = (r: Row) => r.blocks.some(b => b.type !== 'text' || !!b.text?.trim())

type Stored = { uuid: string; message: Append['message'] }

// The event a row the main conversation stored makes, or undefined for one the view does not show:
// a subagent's, a message queue record (no row of the conversation), an attachment (the harness's
// context, an output style or a hook's result, not something said), a notice, the compaction's
// boundary, and one the view would draw nothing of. History maps the transcript's lines by the same
// rules (machine/internal/surface/claude/history.go), so a reload draws what the live view drew;
// test/rows-slice.ts is one recorded session both are held to. `act` is the desk's act that landed it.
export function eventOf(e: Pick<Append, 'door' | 'origin' | 'agentId'>, stored: Stored, act?: string): Payload | undefined {
  const m = stored.message
  if (e.agentId !== undefined || e.door === 'queue' || (m.type !== 'user' && m.type !== 'assistant')) return undefined
  const blocks = blocksOf(m.content)
  const row: Row = { id: stored.uuid, role: 'assistant', origin: 'model', ...(act ? { act } : {}), blocks }
  let kind: 'row' | 'compaction' = 'row'
  if (m.type === 'user') {
    if (e.door === 'compaction') { kind = 'compaction'; row.role = 'system'; row.origin = 'engine' }
    else if (blocks.length && blocks.every(b => b.type === 'tool-result')) { row.role = 'tool-result'; row.origin = 'engine' }
    else {
      row.role = 'user'
      row.origin = originOf(e.origin, m.isMeta === true)
      said(row, ours(e.origin))
    }
  }
  return drawn(row) ? { kind, row } : undefined
}

// Whether a row's origin is this plugin's.
export const ours = (o: { kind: string }) => o.kind === 'plugin' && 'name' in o && o.name === OWN

// Whether a row stored under door and origin is one this module appended itself: it emits those
// where it makes them, with the act that landed them, and its hook passes them by.
export const ownNote = (e: Pick<Append, 'door' | 'origin'>) => e.door === 'note' && ours(e.origin)

// The origin and door this module's own appends are stored under.
export const OWN_NOTE = { door: 'note', origin: { kind: 'plugin', name: OWN } } as const

// The event an `emit` asks for: an ask or its settlement, which the agent sees in the session's
// hooks and the module numbers among its own rows, so the session's events keep one numbering.
// Anything else is why it was not taken.
export function emitted(body: unknown): Payload | string {
  const b = (typeof body === 'object' && body !== null ? body : {}) as { kind?: unknown; ask?: unknown; settled?: unknown }
  if (b.kind === 'ask' && typeof b.ask === 'object' && b.ask !== null) return { kind: 'ask', ask: b.ask as Ask }
  if (b.kind === 'ask-settled' && typeof b.settled === 'object' && b.settled !== null) return { kind: 'ask-settled', settled: b.settled as Settled }
  return `an emit of ${String(JSON.stringify(b.kind ?? null)).slice(0, 40)}, which is no ask or settlement`
}

// The prompt a `prompt` asks the module to land: its act's id, the text the model reads, which opens
// with its from: line, and who said it, as its row's origin. Without words or a speaker, why not,
// and the id when there was one to report it under.
export function prompted(body: unknown): { id: string; text: string; from: string } | { id?: string; refused: string } {
  const b = (typeof body === 'object' && body !== null ? body : {}) as { id?: unknown; text?: unknown; from?: unknown }
  const id = typeof b.id === 'string' && b.id ? b.id : undefined
  if (id && typeof b.text === 'string' && b.text.trim() && typeof b.from === 'string' && b.from) return { id, text: b.text, from: b.from }
  return { ...(id ? { id } : {}), refused: 'the agent\'s prompt carried no words or no speaker' }
}

// The interrupt an `interrupt` asks the module to carry out: its act's id, and who asked, as the
// from: line names them. Without a speaker, why not, and the id when there was one.
export function interrupting(body: unknown): { id: string; from: string } | { id?: string; refused: string } {
  const b = (typeof body === 'object' && body !== null ? body : {}) as { id?: unknown; from?: unknown }
  const id = typeof b.id === 'string' && b.id ? b.id : undefined
  if (id && typeof b.from === 'string' && b.from) return { id, from: b.from }
  return { ...(id ? { id } : {}), refused: 'the agent\'s interrupt named nobody who asked for it' }
}

// What the model reads after a stop from the desk, under the from: line naming who stopped it: a
// plugin's abort leaves no interruption marker, and a model never told picks the work up again at
// its next turn (spike A).
export function interruptNote(from: string, stopped: number, unstopped: number, unseen = false): string {
  const ended = stopped === 1 ? ' The command it was running was ended.'
    : stopped > 1 ? ` The ${stopped} commands it was running were ended.` : ''
  const left = unstopped === 1 ? ' A command it was running could not be ended and may still be running.'
    : unstopped > 1 ? ` ${unstopped} commands it was running could not be ended and may still be running.` : ''
  const unknown = unseen ? ' A command it was running may not have been ended.' : ''
  return `from: ${from}\n\nYour turn was interrupted from the desk.${ended}${left}${unknown} Do not carry on with it; wait for what you are asked next.`
}

// The first text a stored row holds: the words a submitted prompt was stored with.
export function firstText(content: readonly { type: string; [field: string]: unknown }[]): string {
  const b = content.find(b => b.type === 'text' && typeof b.text === 'string')
  return b ? b.text as string : ''
}

// The turn as it stands, told to a watch that begins between a turn's start and end: running, or
// idle, which the view reads from a completed turn that carries no reason.
export const turnNow = (busy: boolean): Payload => ({ kind: 'turn', turn: { state: busy ? 'started' : 'completed' } })

// The event has no refusal: a turn the model refused ended without its answer, as an error's did.
export function turnReason(reason: Args<'turn.complete'>['reason']): NonNullable<Turn['reason']> {
  return reason === 'answer' ? 'answered' : reason === 'aborted' ? 'interrupted' : 'error'
}

// The agent serving a workspace, from the record `local_workspace.agent_port` and `agent_key_file`
// read: the port the environment names, else the workspace's `.env`; the key file that record names
// for that port, else the agent's own under the config root.
export function agentOf(record: string, env: { port?: string; xdg?: string; home?: string }): { port: string; keyFiles: string[] } {
  const named = (name: string) => record.split('\n').map(l => l.trim()).find(l => l.startsWith(`${name}=`))?.slice(name.length + 1) ?? ''
  const served = named('STEERING_AGENT_PORT')
  const port = env.port || served || DEFAULT_PORT
  const keyFile = named('STEERING_AGENT_KEY_FILE')
  if (keyFile && served && Number(served) === Number(port)) return { port, keyFiles: [keyFile] }
  const file = `2mw2lt/agents/${Number(port) || port}.key`
  return { port, keyFiles: [...(env.xdg?.startsWith('/') ? [`${env.xdg}/${file}`] : []), ...(env.home ? [`${env.home}/.config/${file}`] : [])] }
}

// The workspace a header names: its common root, percent-encoded as `local_workspace.path_header` does.
export const workspaceHeader = (root: string) => encodeURIComponent(root).replace(/%2F/g, '/')

type Streamed = { index: number; block: Delta['block']; grown: string; replaced: boolean; offset: number; unseen: boolean; stopped: boolean }
const utf8 = new TextEncoder()

// A step's text and thinking as the session writes them (#8282, design §4), gathered by block
// between sends. A chunk carries no id its stored row also carries, so a row is matched to its block
// by order: the harness stores one response row per block, at the block's end and in block order.
export class Streaming {
  private step = ''
  private blocks: Streamed[] = []

  // A step begins: a block of the step before is no row's to replace from now on.
  begin(turnId: string, index: number) {
    this.step = `${turnId}/${index}`
    this.blocks = []
  }

  // A piece of a block, and whether anything now waits to be sent. An empty piece still opens its
  // block, whose row must find it in its place.
  take(c: { kind: 'text' | 'thinking'; index: number; text: string }, watched = true): boolean {
    if (!this.step) return false
    let b = this.blocks.find(b => b.index === c.index)
    if (!b) this.blocks.push(b = { index: c.index, block: c.kind, grown: '', replaced: false, offset: 0, unseen: false, stopped: false })
    if (!watched) { b.unseen = true; b.grown = '' }
    if (!b.replaced && !b.stopped && !b.unseen && b.block === c.kind) b.grown += c.text
    return this.blocks.some(b => b.grown)
  }

  // A final flush also ends emission for blocks still waiting on their rows.
  send(stopped = false): Payload[] {
    const out: Payload[] = []
    for (const b of [...this.blocks].sort((x, y) => x.index - y.index)) {
      if (b.replaced || b.unseen || b.stopped || (!b.grown && !stopped)) continue
      out.push({ kind: 'delta', delta: { key: `${this.step}/${b.index}`, block: b.block, text: b.grown, offset: b.offset, ...(stopped ? { stopped: true } : {}) } })
      b.offset += utf8.encode(b.grown).length
      b.stopped = stopped
      b.grown = ''
    }
    return out
  }

  // The key a response row replaces: the oldest streamed block of the step no row has replaced. None
  // when no block is open or that block is of another kind than the row's one block: the row is then
  // drawn as any row is, and the block stays open, so a mismatch costs a block drawn twice until the
  // turn's end, never a row put in the wrong place. What the block grew by and was not sent goes with
  // it: the row carries the whole block.
  replaces(content: readonly { type: string }[]): string | undefined {
    let open: Streamed | undefined
    for (const b of this.blocks) if (!b.replaced && (!open || b.index < open.index)) open = b
    if (!open || content.length !== 1 || content[0]!.type !== open.block) return undefined
    open.replaced = true
    open.grown = ''
    return `${this.step}/${open.index}`
  }

  // The turn ended: what no row replaced is let go.
  end() {
    this.step = ''
    this.blocks = []
  }
}

// The numbering: one seq per event, rising for the session's life, and a gap wherever the reader
// cannot see what happened, so it re-reads history rather than show a view with a hole in it.
// Events are numbered as they are taken to be sent, so a loss learned from the batch before is
// always the next batch's opening gap.
export class Outbox {
  // The last seq minted; undefined until the one kept across a reload is read back.
  seq: number | undefined
  // Per session, the last seq before a hole: its next batch opens with a gap from it.
  private owed = new Map<string, number>()
  // A reload's hole, owed to whichever session is taken first.
  private reloaded: number | undefined
  private queue: { session: string; payload: Payload }[] = []

  // The seq kept before this process: a reload, so what the last one had queued is gone.
  resume(kept: number) {
    if (this.seq !== undefined) return
    this.seq = kept
    if (kept > 0) this.reloaded = kept
  }

  // Whether this event opens a batch, which the caller then sends after its window.
  add(session: string, payload: Payload): boolean {
    if (this.seq === undefined) return false
    this.queue.push({ session, payload })
    return this.queue.length === 1
  }

  get pending(): boolean {
    return this.queue.length > 0
  }

  // The leading run of one session's events, numbered, its gap first: the agent takes a batch
  // that names one session, and the rest waits for the next send.
  take(): Event[] {
    const head = this.queue[0]
    if (!head || this.seq === undefined) return []
    const session = head.session
    let n = 0
    while (n < this.queue.length && this.queue[n]!.session === session) n++
    const out: Event[] = []
    let from = this.owed.get(session)
    if (this.reloaded !== undefined) {
      from = Math.min(from ?? this.reloaded, this.reloaded)
      this.reloaded = undefined
    }
    if (from !== undefined) {
      const to = ++this.seq
      out.push({ v: 1, session, seq: to, kind: 'gap', gap: { from, to } })
      this.owed.delete(session)
    }
    for (const { payload } of this.queue.splice(0, n)) out.push({ v: 1, session, seq: ++this.seq, ...payload })
    return out
  }

  // A batch the agent did not take: the hole runs from before it, or from where its own opening
  // gap's did, so an earlier loss is never narrowed.
  failed(batch: readonly Event[]) {
    const first = batch[0]
    if (first) this.lost(first.session, first.kind === 'gap' ? first.gap.from : first.seq - 1)
  }

  // Events never made while the view was lost.
  lost(session: string, from: number) {
    this.owed.set(session, Math.min(this.owed.get(session) ?? from, from))
  }
}

// A slash command as the agent keeps it (#8432 decision 3), classed here, the one place that knows
// Claude: `prompt` expands into the model's input, `session` acts on the session's own context,
// `spend` changes what it costs, and `control` is never run from the desk. `$.command.list()` gives
// no argument hint, so `hint` is empty.
export type Class = 'prompt' | 'session' | 'spend' | 'control'
export type SlashCommand = { name: string; description: string; hint: string; source: string; class: Class }

// Claude's built-ins mix local commands with bundled prompt skills (U0), so each is classed by name.
// One this map does not name is left out: nothing unclassified reaches the desk.
export const BUILTIN_CLASS: Readonly<Record<string, Class>> = {
  // Bundled skills: they expand into the model's input.
  'code-review': 'prompt', simplify: 'prompt', 'security-review': 'prompt', init: 'prompt', verify: 'prompt',
  run: 'prompt', 'run-skill-generator': 'prompt', 'deep-research': 'prompt', batch: 'prompt', loop: 'prompt',
  dataviz: 'prompt', 'artifact-design': 'prompt', 'artifact-diagramming': 'prompt', 'claude-api': 'prompt',
  'workflow-authoring': 'prompt', 'plugin-authoring': 'prompt',
  // The session's own context, no wider reach.
  compact: 'session', context: 'session', review: 'session', usage: 'session', status: 'session', rename: 'session',
  // What the session costs.
  model: 'spend', effort: 'spend', fast: 'spend', advisor: 'spend',
  // What the session may do, where it runs, or whether it does.
  config: 'control', permissions: 'control', hooks: 'control', plugin: 'control', 'reload-plugins': 'control',
  'reload-skills': 'control', mcp: 'control', login: 'control', logout: 'control', exit: 'control', sandbox: 'control',
  'add-dir': 'control', cd: 'control', memory: 'control', plan: 'control', 'auto-mode-setup': 'control',
  'update-config': 'control', 'fewer-permission-prompts': 'control', doctor: 'control', debug: 'control',
  clear: 'control', resume: 'control', fork: 'control', branch: 'control', rewind: 'control', background: 'control',
}

// The list the agent is told: every command not built in is a prompt (a skill, a markdown command,
// an MCP prompt) unless a built-in holds its name, and a built-in is classed by name, or left out.
export function classified(list: readonly { name: string; description: string; source: string }[]): SlashCommand[] {
  const out: SlashCommand[] = []
  for (const c of list) {
    const named = Object.prototype.hasOwnProperty.call(BUILTIN_CLASS, c.name) ? BUILTIN_CLASS[c.name] : undefined
    // A name a built-in holds keeps the built-in's class whoever lists it: a repo's skill named `exit`
    // must not read as a prompt while the engine may run the built-in under that name.
    const cls = c.source === 'builtin' || named ? named : 'prompt'
    if (cls) out.push({ name: c.name, description: c.description, hint: '', source: c.source, class: cls })
  }
  return out
}

// The command a `command` asks the module to run: its act's id, its name and args, and who ran it,
// as the from: line names them. Without a name or a speaker, why not, and the id when there was one.
export function running(body: unknown): { id: string; name: string; args: string; from: string } | { id?: string; refused: string } {
  const b = (typeof body === 'object' && body !== null ? body : {}) as { id?: unknown; name?: unknown; args?: unknown; from?: unknown }
  const id = typeof b.id === 'string' && b.id ? b.id : undefined
  if (id && typeof b.name === 'string' && b.name && typeof b.from === 'string' && b.from && (b.args === undefined || typeof b.args === 'string')) {
    return { id, name: b.name, args: b.args ?? '', from: b.from }
  }
  return { ...(id ? { id } : {}), refused: 'the agent\'s command carried no name or no speaker' }
}

// What the model reads first in a desk command's turn: who ran it, since the command's own text
// cannot carry a from: line (the engine reads the whole text as the command line).
export const commandNote = (from: string, name: string, args: string) =>
  `from: ${from}\n\nThe next command, /${args.trim() ? `${name} ${args.trim()}` : name}, is theirs.`

// The most of a local command's result the agent is handed.
export const RESULT_MAX = 4096

// What a local command resolved to, as the desk's receipt shows it: its output and the notes it left
// the model, cut at RESULT_MAX, never inside a surrogate pair; undefined when it left neither.
export function ranResult(r: { text?: unknown; context?: unknown }): string | undefined {
  const parts = [typeof r.text === 'string' ? r.text : '', ...(Array.isArray(r.context) ? r.context.filter(c => typeof c === 'string') : [])]
  let out = parts.filter(p => p.trim()).join('\n\n')
  if (!out) return undefined
  if (out.length > RESULT_MAX) {
    const end = /[\uD800-\uDBFF]/.test(out[RESULT_MAX - 1]!) ? RESULT_MAX - 1 : RESULT_MAX
    out = out.slice(0, end)
  }
  return out
}
