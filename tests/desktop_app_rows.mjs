// node desktop_app_rows.mjs <case>: one case per process (the page keeps module state).
// Mounts the real desktop/plugin.js on desktop_page_harness.mjs and prints one line
// RESULT {"ok": bool, ...details}. The Python test runs every case and asserts ok.
// F17: an Apps row shows Stop on a resource that runs, Start on one that is stopped, neither on an
// unknown state, and no Start while a deployment of that stopped app is in flight (SS-0 measured:
// start on a running app queues a full redeploy, and status stays stale while a deployment runs).
import fs from 'node:fs'
import * as h from './harness.mjs'
import plugin, { statusClass } from './plugin.mjs'

const iso = ago => new Date(Date.now() - ago).toISOString()

const app = (name, status) => ({
  uuid: 'app-' + name, name, kind: 'application', project: 'Demo', environment: 'staging', status
})

const FIX = {
  setup: { ok: true, configured: true, steps: [] },
  capabilities: {
    ok: true, checked_at: iso(1000), instance: { url: 'https://coolify.example.test', reachable: true, coolify_version: '4.0.0' },
    tokens: [{ role: 'api', scope: 'team-wide', status: 'ok', rights: ['read', 'deploy'], setting: 'COOLIFYBRUECKE_TOKEN', fingerprint: 'abcd1234', sees: {} }],
    protected: { environments: ['production'], agent_allowed: false }, available: [], not_available: [], warnings: [], setup: { line: 'Ready', tone: 'good' }
  },
  actions: {
    ok: true,
    actions: [
      { kind: 'deploy', label: 'Deploy', target_kinds: ['application'] },
      { kind: 'restart', label: 'Restart', target_kinds: ['application'] },
      { kind: 'start', label: 'Start', target_kinds: ['application'] },
      { kind: 'stop', label: 'Stop', target_kinds: ['application'] }
    ],
    action_token: { role: 'api', setting: 'COOLIFYBRUECKE_TOKEN', deploy: true }
  }
}

function boot(opts) {
  const resources = opts.resources || []
  const snapshot = { ok: true, taken_at: iso(2000), servers: [], resources, fleet: opts.fleet || {}, deployments: { running: [] }, summary: [], attention: [], latest_deployments: [] }
  const layout = {
    ok: true,
    layout: { version: 1, updated_by: 'pane', widgets: [{ id: 'apps', type: 'resources', size: 'l', props: { row_actions: opts.rowActions || ['deploy', 'restart', 'stop', 'start'], pairs: false } }] },
    widget_types: [], presets: []
  }
  const ctx = {
    rest: async (path, init) => {
      const method = (init && init.method) || 'GET'
      const p = path.split('?')[0]
      if (p === '/setup') return FIX.setup
      if (p === '/capabilities') return FIX.capabilities
      if (p === '/snapshot') return snapshot
      if (p === '/actions' && method === 'GET') return FIX.actions
      if (p === '/layout' && path.includes('availability')) return { ok: true, availability: [] }
      if (p === '/layout') return layout
      if (p === '/alerts') return { ok: true, on: false }
      if (p === '/notes') return { ok: true, text: '', entries: 0 }
      return { ok: false, error: 'no fixture for ' + p }
    },
    storage: { get: (k, f) => f, set: () => {}, remove: () => {} },
    register: c => (contributions.push(c), () => {}),
    registerSettingsPage: c => (contributions.push({ ...c, area: 'settings' }), () => {})
  }
  const contributions = []
  plugin.register(ctx)
  const parts = ['page'].map(id => contributions.find(c => c.id === id))
  h.mount(h.jsx('div', { children: parts.map(c => h.jsx('section', { children: c.render() })) }))
}

const rowOf = uuid => h.all(n => n.type === 'div' && n.props['data-cc-app'] === uuid)[0]
const LABELS = ['Deploy', 'Restart', 'Start', 'Stop']
const rowButtons = uuid => {
  const row = rowOf(uuid)
  if (!row) return null
  const found = h.all(n => n.type === 'button' && LABELS.includes(h.text(n).trim()), [row]).map(n => h.text(n).trim())
  return LABELS.filter(l => found.includes(l))
}
const rowTone = uuid => {
  const row = rowOf(uuid)
  const dot = row && h.all(n => n.props['data-dot'] !== undefined, [row])[0]
  return dot ? dot.props['data-tone'] : null
}

// The real state measured on the instance (fixture recorded during SS-3, the same app at two
// times, so one state per process): the stopped staging row shows Start, the running one Stop.
const liveState = (key, want) => {
  const state = JSON.parse(fs.readFileSync(new URL('./ss3-live-state.json', import.meta.url), 'utf8'))
  boot({ resources: [state[key].resource], fleet: { [state[key].resource.uuid]: state[key].fleet } })
  return h.settle().then(() => {
    const buttons = rowButtons(state[key].resource.uuid)
    return { ok: JSON.stringify(buttons) === JSON.stringify(want), buttons, want }
  })
}

// SS-0's measured vocabulary (brain coolifybruecke-ss0-statuses.md) plus the statuses Coolify
// documents that this instance never showed. Unknown means neither button.
const VOCAB = [
  ['running:healthy', 'running'], ['running:unknown', 'running'], ['running:unhealthy', 'running'],
  ['running', 'running'], ['exited:unhealthy', 'stopped'], ['exited', 'stopped'], ['exited:3', 'stopped'],
  ['stopped', 'stopped'], ['restarting', 'unknown'], ['starting', 'unknown'], ['created', 'unknown'],
  ['degraded', 'unknown'], ['', 'unknown'], [undefined, 'unknown']
]

const cases = {
  vocabulary() {
    const wrong = VOCAB.filter(([status, want]) => statusClass(status) !== want)
    return Promise.resolve({ ok: wrong.length === 0, wrong, checked: VOCAB.length })
  },
  buttons_running() {
    boot({ resources: [app('run', 'running:healthy'), app('runu', 'running:unknown')] })
    return h.settle().then(() => {
      const a = rowButtons('app-run')
      const b = rowButtons('app-runu')
      return { ok: JSON.stringify(a) === JSON.stringify(['Deploy', 'Restart', 'Stop']) && JSON.stringify(b) === JSON.stringify(a), running: a, unknown_health: b }
    })
  },
  buttons_stopped() {
    boot({ resources: [app('off', 'exited:unhealthy'), app('off2', 'exited')] })
    return h.settle().then(() => {
      const a = rowButtons('app-off')
      const b = rowButtons('app-off2')
      return { ok: JSON.stringify(a) === JSON.stringify(['Deploy', 'Restart', 'Start']) && JSON.stringify(b) === JSON.stringify(a), stopped: a }
    })
  },
  buttons_unknown() {
    boot({ resources: [app('new', 'created'), app('blank', undefined)] })
    return h.settle().then(() => {
      const a = rowButtons('app-new')
      const b = rowButtons('app-blank')
      return { ok: JSON.stringify(a) === JSON.stringify(['Deploy', 'Restart']) && JSON.stringify(b) === JSON.stringify(a), unknown: a }
    })
  },
  start_hidden_while_deploying() {
    boot({ resources: [app('off', 'exited:unhealthy')], fleet: { 'app-off': { state: 'deploying' } } })
    return h.settle().then(() => {
      const a = rowButtons('app-off')
      return { ok: JSON.stringify(a) === JSON.stringify(['Deploy', 'Restart']), buttons: a }
    })
  },
  live_state_stopped() {
    return liveState('stopped', ['Deploy', 'Restart', 'Start'])
  },
  live_state_running() {
    return liveState('running', ['Deploy', 'Restart', 'Stop'])
  },
  tones() {
    boot({ resources: [app('run', 'running:healthy'), app('sick', 'running:unhealthy'), app('off', 'exited:unhealthy'), app('new', 'created')] })
    return h.settle().then(() => {
      const tones = { run: rowTone('app-run'), sick: rowTone('app-sick'), off: rowTone('app-off'), fresh: rowTone('app-new') }
      const ok = tones.run === 'good' && tones.sick === 'warn' && tones.off === 'bad' && tones.fresh === 'muted'
      return { ok, tones }
    })
  }
}

const wanted = process.argv[2]
if (!cases[wanted]) {
  console.error('no case ' + wanted)
  process.exit(1)
}
cases[wanted]().then(value => {
  console.log('RESULT ' + JSON.stringify(value))
}, error => {
  console.error(error && error.stack || error)
  process.exit(1)
})
