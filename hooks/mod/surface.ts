import type { Args, ResultOf } from 'claude-code'

// The live view's events (#4823, design §5): the main conversation's rows, turns and compactions,
// numbered for the machine agent. Pure: register.ts owns every call on `$` (the engine allows `$`
// only at its own call sites), and this holds the shapes and the numbering.

type Append = Args<'session.append'>

export type Block = { type: 'text' | 'thinking' | 'tool-use' | 'tool-result'; id?: string; text?: string; tool?: string; input?: unknown }
export type Row = { id?: string; role: 'user' | 'assistant' | 'tool-result' | 'system'; origin: string; blocks: Block[] }
export type Turn = { state: 'started' | 'completed'; reason?: 'answered' | 'interrupted' | 'error' }
export type Payload = { kind: 'row' | 'compaction'; row: Row } | { kind: 'turn'; turn: Turn }
export type Event = { v: 1; session: string; seq: number } & (Payload | { kind: 'gap'; gap: { from: number; to: number } })

export const DEFAULT_PORT = '9990'

// Who wrote a row: the person at the terminal or on Remote Control, a plugin, the model, or the
// engine, which a tool's result is too.
export function originOf(o: Append['origin']): string {
  switch (o.kind) {
    case 'composer': case 'bridge': return 'person'
    case 'plugin': return 'plugin'
    case 'model': return 'model'
    default: return 'engine'
  }
}

function roleOf(m: Append['message'], door: Append['door']): Row['role'] {
  if (m.type === 'assistant') return 'assistant'
  if (door === 'tool-result') return 'tool-result'
  return m.type === 'user' ? 'user' : 'system'
}

const str = (v: unknown) => typeof v === 'string' ? v : undefined

function textOf(content: unknown): string {
  if (typeof content === 'string') return content
  if (!Array.isArray(content)) return ''
  return content.filter(b => b?.type === 'text' && typeof b.text === 'string').map(b => b.text as string).join('\n')
}

// A block the event has no kind for (an image, a document, redacted thinking) is left out.
export function blocksOf(content: readonly { type: string; [field: string]: unknown }[], tool?: string): Block[] {
  const out: Block[] = []
  for (const b of content) {
    if (b.type === 'text' && typeof b.text === 'string') out.push({ type: 'text', text: b.text })
    else if (b.type === 'thinking' && typeof b.thinking === 'string') out.push({ type: 'thinking', text: b.thinking })
    else if (b.type === 'tool_use') out.push({ type: 'tool-use', id: str(b.id), tool: str(b.name), input: b.input })
    else if (b.type === 'tool_result') out.push({ type: 'tool-result', id: str(b.tool_use_id), tool, text: textOf(b.content) })
  }
  return out
}

// A row the main conversation stored, or undefined for one the view does not show: a subagent's,
// a message queue record (no row of the conversation), and the module's own, which never come
// back through its own hook and are emitted where they are made.
export function rowOf(e: Append, stored: { uuid: string; message: Append['message'] }): Row | undefined {
  if (e.agentId !== undefined || e.door === 'queue') return undefined
  if (e.origin.kind === 'plugin' && 'name' in e.origin && e.origin.name === '2mw2lt') return undefined
  const tool = e.origin.kind === 'tool' ? e.origin.tool : undefined
  return { id: stored.uuid, role: roleOf(stored.message, e.door), origin: originOf(e.origin), blocks: blocksOf(stored.message.content, tool) }
}

export const ownRow = (text: string, id?: string): Row =>
  ({ ...(id ? { id } : {}), role: 'user', origin: 'plugin', blocks: [{ type: 'text', text }] })

// The compacted conversation opens with its summary.
export function compactionRow(r: ResultOf['session.compact']): Row | undefined {
  const summary = r.skip === undefined ? r.messages[0] : undefined
  if (!summary) return undefined
  return { role: 'system', origin: 'engine', blocks: summary.text ? [{ type: 'text', text: summary.text }] : [] }
}

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
