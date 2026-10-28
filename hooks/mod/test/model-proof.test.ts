import { test, expect } from 'claude-code/testing'
import { world, begin, never } from './world'

const HELD = { lines: [], code: 0 as const, hold: never }
const DIR = '/proof/launch-1'
const FROM = 'console user alice, member of the team'
const env = (deadline = '1001000') => ({
  STEERING_MODEL_PROOF: DIR,
  STEERING_MODEL_PROOF_LAUNCH: 'launch-1',
  STEERING_MODEL_PROOF_DEADLINE: deadline,
})

type World = ReturnType<typeof world>
const postedTo = (w: World, route: string) => w.fetches.filter(f => f.method === 'POST' && f.url.endsWith(`/steering/surface/${route}`)).map(f => JSON.parse(f.body!))
const accept = (w: World) => w.files.set(`${DIR}/accepted.json`, JSON.stringify({ launch: 'launch-1', model: 'claude-test' }))

function commands(on: any) {
  const e = { runs: [] as { command: string; args: string }[], answers: [] as ((r: unknown) => void)[] }
  on('command.list', async () => ({ value: [] }) as never)
  on('command.run', async (_$: unknown, run: { command: string; args: string }) => {
    e.runs.push({ command: run.command, args: run.args })
    return new Promise(resolve => { e.answers.push(resolve) })
  })
  return e
}

const composeDefaults = {
  name: 'compose-defaults', tier: 'prepend' as const,
  register(on: any) {
    on('prompt.compose', async ($: any, e: Record<string, unknown>, next: any) => {
      if (e.model === undefined) return $.prompt.compose({
        model: 'claude-test', promptModel: 'claude-test', surfaces: ['terminal'],
        tools: [], outputStyle: null, traits: [],
      })
      return next(e)
    })
  },
}

async function proofStart($: any, on: any, deadline?: string) {
  let second!: (p: unknown) => void
  const w = world(on, {
    agent: true, rounds: [HELD], proofEnv: env(deadline),
    polls: [{ watch: true, cursor: 'c1' }, new Promise(r => { second = r })],
  })
  const e = commands(on)
  await begin($)
  await w.clock.settle()
  return { w, e, second }
}

test('restricted startup publishes effective compose evidence before restoring the hold', { plugins: [composeDefaults] }, async ($, on) => {
  const { w } = await proofStart($, on)
  const path = `${DIR}/prompt-compose.json`
  expect(JSON.parse(w.files.get(path)!)).toEqual({ launch: 'launch-1', session: 'ps-1', model: 'claude-test' })
  const published = w.actions.indexOf(`write:${path}`)
  const restored = w.actions.findIndex(a => a.includes('--session-of'))
  expect(published).toBeGreaterThanOrEqual(0)
  expect(restored).toBeGreaterThan(published)
  expect(w.files.has(`${DIR}/accepted.json`)).toBe(false)
  expect(w.runs.some(argv => argv[1] === '-c' && argv[4] === path)).toBe(true)
  expect(w.writes.some(write => write.path === path)).toBe(false)
})

test('an unrestricted startup does not invoke the proof compose channel', async ($, on) => {
  const w = world(on, { agent: true, rounds: [HELD], polls: [{ watch: false, cursor: 'c1' }] })
  commands(on)
  await begin($)
  await w.clock.settle()
  expect(w.composes).toEqual([])
})

test('restricted resume holds idle prompt and command ingress until acceptance', { plugins: [composeDefaults] }, async ($, on) => {
  const { w, e, second } = await proofStart($, on)
  second({ watch: true, cursor: 'c2', commands: [
    { kind: 'prompt', body: { id: 'prompt-1', text: 'list the files', from: FROM } },
    { kind: 'command', body: { id: 'command-1', name: 'compact', args: '', from: FROM } },
  ] })
  await w.clock.settle()
  expect(w.submits).toEqual([])
  expect(e.runs).toEqual([])
  expect(w.appends).toEqual([])

  accept(w)
  await w.clock.advance(25)
  await w.clock.settle()
  expect(w.submits).toEqual(['list the files'])
  expect(e.runs).toEqual([{ command: 'compact', args: '' }])
  expect(w.appends).toEqual([])
})

test('restricted resume holds direct composer ingress until acceptance', { plugins: [composeDefaults] }, async ($, on) => {
  const { w } = await proofStart($, on)
  const typed = $.prompt.submit({ text: 'typed at the composer' })
  await w.clock.settle()
  expect(w.submits).toEqual([])

  accept(w)
  await w.clock.advance(25)
  await typed
  expect(w.submits).toEqual(['typed at the composer'])
})

test('restricted resume holds busy append ingress until acceptance', { plugins: [composeDefaults] }, async ($, on) => {
  const { w, second } = await proofStart($, on)
  await $.turn.start({ turnId: 'busy', text: 'working' })
  second({ watch: true, cursor: 'c2', commands: [
    { kind: 'prompt', body: { id: 'prompt-busy', text: 'join the running turn', from: FROM } },
  ] })
  await w.clock.settle()
  expect(w.appends).toEqual([])

  accept(w)
  await w.clock.advance(25)
  await w.clock.settle()
  expect(w.appends).toEqual(['join the running turn'])
  await $.turn.complete({ turnId: 'busy', text: '', reason: 'answer' } as never)
})

test('proof timeout refuses prompt, command and busy append without an append fallback', { plugins: [composeDefaults] }, async ($, on) => {
  let second!: (p: unknown) => void
  let third!: (p: unknown) => void
  const w = world(on, {
    agent: true, rounds: [HELD], proofEnv: env('1000000'),
    polls: [{ watch: true, cursor: 'c1' }, new Promise(r => { second = r }), new Promise(r => { third = r })],
  })
  const e = commands(on)
  await begin($)
  await w.clock.settle()
  second({ watch: true, cursor: 'c2', commands: [
    { kind: 'prompt', body: { id: 'prompt-idle', text: 'must not fall back', from: FROM } },
    { kind: 'command', body: { id: 'command-timeout', name: 'compact', args: '', from: FROM } },
  ] })
  await w.clock.settle()
  await $.turn.start({ turnId: 'busy-timeout', text: 'working' })
  third({ watch: true, cursor: 'c3', commands: [
    { kind: 'prompt', body: { id: 'prompt-busy-timeout', text: 'must not append', from: FROM } },
  ] })
  await w.clock.settle()

  expect(w.submits).toEqual([])
  expect(e.runs).toEqual([])
  expect(w.appends).toEqual([])
  const landed = postedTo(w, 'landed')
  expect(landed.map(r => r.id)).toEqual(['prompt-idle', 'prompt-busy-timeout'])
  for (const receipt of landed) expect(receipt.refused).toContain('deadline')
  const ran = postedTo(w, 'ran')
  expect(ran.length).toBe(1)
  expect(ran[0].id).toBe('command-timeout')
  expect(ran[0].refused).toContain('deadline')

  accept(w)
  await w.clock.advance(25)
  await w.clock.settle()
  expect(w.submits).toEqual([])
  expect(e.runs).toEqual([])
  expect(w.appends).toEqual([])
  await $.turn.complete({ turnId: 'busy-timeout', text: '', reason: 'answer' } as never)
})
