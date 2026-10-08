// What the module reads and writes as text: hold.py's frame lines, the frame recording, the
// fixed rows it may append, and the version floor. Nothing here touches `$`.

// `routine` rides a kick the daemon says moved nothing: the module answers it without a model turn.
export type Routine = { at: string; interval: number }
export type Line = { id: string | null; kind: string; wakes: boolean; routine?: Routine }

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

// Whether a routine kick may be answered at `now`: not while the turn running has been going longer
// than the kick's own interval, which is the silence doc 70 §3 counts. A turn whose start was never
// seen (a reload mid-turn) is not known to be that long.
export function answerable(busy: boolean, turnStart: number | undefined, now: number, intervalS: number): boolean {
  return !busy || turnStart === undefined || now - turnStart <= intervalS * 1000
}

// The recording holds `data: {json}` lines. Every frame carries the daemon's `id`, which hold.py
// prints; return the pending ones verbatim and in order, with each envelope's ulid for ack.py.
export function pick(recording: string, pending: readonly string[]): { text: string; envelopes: string[]; envelopeIds: string[]; found: string[] } {
  const want = new Set(pending)
  const out: string[] = []
  const envelopes: string[] = []
  const envelopeIds: string[] = []
  const found: string[] = []
  for (const raw of recording.split('\n')) {
    if (!raw.startsWith('data: ')) continue
    let f: { kind?: unknown; ulid?: unknown; id?: unknown }
    try { f = JSON.parse(raw.slice(6)) } catch { continue }
    if (!f || typeof f.id !== 'string' || !want.has(f.id)) continue
    want.delete(f.id)
    found.push(f.id)
    out.push(raw.slice(6))
    if (f.kind === 'envelope' && typeof f.ulid === 'string') { envelopes.push(f.ulid); envelopeIds.push(f.id) }
  }
  return { text: out.join('\n'), envelopes, envelopeIds, found }
}

// How many ids of frames the model has read are kept to recognise a replay by.
export const READ_KEPT = 256

// Whether a frame that wakes the session earns a pointer: only one the `frames` tool can hand over
// (it finds frames by id) and that the session has neither been pointed at nor read. The stream
// reopens and the daemon replays frames it holds, so the same id arrives again and again.
export function owed(id: string | null, pending: readonly string[], read: readonly string[]): id is string {
  return id !== null && !pending.includes(id) && !read.includes(id)
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

// The seat's section (#4551 step 3): canon's standing rulings, which seat_section.py composes for a
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
