// What the module reads and writes as text: hold.py's frame lines, the frame recording, the
// fixed rows it may append, and the version floor. Nothing here touches `$`.

// `routine` rides a kick the daemon says moved nothing: the module answers it without a model turn.
export type Routine = { at: string; interval: number }
export type Line = { id: string | null; kind: string; wakes: boolean; routine?: Routine }

export const POINTER = 'A steering frame is waiting. Call mcp__2mw2lt__frames to read it.'
export const CHECKPOINT = 'Compaction is near. Write your checkpoint (/2mw2lt:checkpoint) now.'
export const CONNECT_AGAIN = 'The steering stream was refused. Run /2mw2lt:connect again.'
export const FLOOR = [2, 1, 289] as const

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
export function pick(recording: string, pending: readonly string[]): { text: string; envelopes: string[]; found: string[] } {
  const want = new Set(pending)
  const out: string[] = []
  const envelopes: string[] = []
  const found: string[] = []
  for (const raw of recording.split('\n')) {
    if (!raw.startsWith('data: ')) continue
    let f: { kind?: unknown; ulid?: unknown; id?: unknown }
    try { f = JSON.parse(raw.slice(6)) } catch { continue }
    if (!f || typeof f.id !== 'string' || !want.has(f.id)) continue
    want.delete(f.id)
    found.push(f.id)
    out.push(raw.slice(6))
    if (f.kind === 'envelope' && typeof f.ulid === 'string') envelopes.push(f.ulid)
  }
  return { text: out.join('\n'), envelopes, found }
}

// That a Bash command runs the named client: python3 (or python) on a path ending
// steering/enroll/<name>.py, and not for its help. A read of the file (cat, grep) is not a run.
export function invokes(command: string, name: string): boolean {
  // The path is a bare word, or a double-quoted argument that may hold spaces and a quoted
  // command substitution of its own ("$(dirname "$x")/steering/enroll/connect.py").
  const path = `(?:"[^\\n]*?steering/enroll/${name}\\.py"|[^\\s";&|]*steering/enroll/${name}\\.py)`
  const run = new RegExp(`(?:^|[\\s;&|(])python3?\\s+${path}(?=[\\s;&|)]|$)([^;&|\\n]*)`, 'g')
  for (const m of command.matchAll(run)) if (!/(^|\s)(--help|-h)(\s|$)/.test(m[1] ?? '')) return true
  return false
}
