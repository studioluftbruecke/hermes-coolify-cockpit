// node desktop_statusbar.mjs <case>: one case per process (the plugin keeps module state).
// Registers the real desktop/plugin.js on desktop_page_harness.mjs against a ctx whose register()
// returns a real disposer, so the live set of status-bar contributions can be read, and prints one
// line RESULT {"ok": bool, ...details}. The Python test runs every case and asserts ok.
import * as h from './harness.mjs'
import plugin from './plugin.mjs'

const logs = []
console.info = (...args) => logs.push(args.map(String).join(' '))

const fail = (status, body) => {
  throw new Error('Error invoking remote method: ' + status + ': ' + JSON.stringify(body))
}

const SNAPSHOT = { ok: true, taken_at: new Date().toISOString(), servers: [{ name: 's1', reachable: true }], resources: [], fleet: {}, deployments: { running: [] }, summary: [], attention: [], latest_deployments: [] }
const CAPS = { ok: true, instance: { reachable: true }, tokens: [], available: [], not_available: [], warnings: [], setup: { line: 'Ready', tone: 'good' } }

// setup: what GET /setup answers now (a function, so a case can change it between reads).
function boot({ stored = {}, setup = () => ({ ok: true, configured: true, steps: [] }), snapshot = () => SNAPSHOT } = {}) {
  const calls = []
  const live = new Set()
  const store = new Map(Object.entries(stored))
  const timers = []
  const listeners = []
  const ctx = {
    rest: async path => {
      const p = path.split('?')[0]
      calls.push(p)
      if (p === '/setup') return setup()
      if (p === '/snapshot') return snapshot()
      if (p === '/capabilities') return CAPS
      return fail(404, { detail: 'no fixture for ' + path })
    },
    storage: { get: (k, f) => (store.has(k) ? store.get(k) : f), set: (k, v) => store.set(k, v) },
    register: c => (live.add(c), () => live.delete(c)),
    registerSettingsPage: () => () => {},
    setInterval: (fn, ms) => (timers.push({ fn, ms }), () => {}),
    addEventListener: (target, type, fn) => (listeners.push({ target, type, fn }), () => {}),
    onDispose: () => {}
  }
  plugin.register(ctx)
  const bar = () => [...live].filter(c => c.area === 'statusBar.right')
  const tick = async () => {
    for (const t of timers) await t.fn()
    await h.settle()
  }
  const focus = async () => {
    for (const l of listeners.filter(l => l.type === 'focus')) await l.fn()
    await h.settle()
  }
  // Mount what the status bar would render now: each item's own render, beside a core item.
  const mountBar = () => h.mount(h.jsx('div', { children: [h.jsx('span', { children: 'core item' }, 'core'), ...bar().map(c => h.jsx('section', { children: c.data.render() }, c.data.id))] }))
  return { ctx, calls, live, store, timers, listeners, bar, tick, focus, mountBar }
}

const shape = c => ({ id: c.id, area: c.area, order: c.order, top_render: typeof c.render, data_id: c.data && c.data.id, toggle_label: c.data && c.data.toggleLabel, data_render: c.data && typeof c.data.render })
const WANT = JSON.stringify({ id: 'chip', area: 'statusBar.right', order: 70, top_render: 'undefined', data_id: 'coolifybruecke.chip', toggle_label: 'Coolifybrücke', data_render: 'function' })

const cases = {
  // Nothing stored, a backend that says nothing (older than the setting): the chip is one data item
  // with the stable id and the brand as toggleLabel, rendered through its own boundary, and being
  // shown costs no extra GET /setup at register.
  async shown_by_default() {
    const app = boot()
    const items = app.bar().map(shape)
    const setupAtRegister = app.calls.filter(p => p === '/setup').length
    app.mountBar()
    await h.settle()
    const text = h.pageText()
    return { ok: items.length === 1 && JSON.stringify(items[0]) === WANT && setupAtRegister === 0 && text.includes('Coolify') && app.bar().length === 1,
      items, setup_at_register: setupAtRegister, text }
  },

  // The chip's own poll reads show_in_statusbar false: the item is disposed (nothing in the bar) and
  // the answer stored. While hidden, the 60 s read and window focus ask GET /setup; true registers
  // the item once with the same id, and further reads while shown ask nothing.
  async switch_off_then_on() {
    let show = false
    const app = boot({ setup: () => ({ ok: true, configured: true, steps: [], show_in_statusbar: show }) })
    app.mountBar()
    await h.settle()
    const off = { items: app.bar().length, stored: app.store.get('show_in_statusbar') }
    const polls = app.timers.map(t => t.ms)
    const before = app.calls.filter(p => p === '/setup').length
    await app.tick()
    const hiddenRead = app.calls.filter(p => p === '/setup').length - before
    show = true
    await app.focus()
    const on = { items: app.bar().map(shape), stored: app.store.get('show_in_statusbar') }
    const shownBefore = app.calls.filter(p => p === '/setup').length
    await app.tick(); await app.focus(); await app.tick()
    const shownReads = app.calls.filter(p => p === '/setup').length - shownBefore
    const ok = off.items === 0 && off.stored === false && JSON.stringify(polls) === '[60000]' && hiddenRead === 1 &&
      on.items.length === 1 && JSON.stringify(on.items[0]) === WANT && on.stored === true && shownReads === 0 && app.bar().length === 1
    return { ok, off, polls, hidden_read: hiddenRead, on, shown_reads: shownReads }
  },

  // A Desktop that stored "off" registers nothing at start (no flash) and asks GET /setup once; a
  // failed read keeps it hidden; the next answer true shows it.
  async stored_off_no_flash() {
    let answer = () => fail(500, { detail: 'down' })
    const app = boot({ stored: { show_in_statusbar: false }, setup: () => answer() })
    const atRegister = app.bar().length
    await h.settle()
    const afterFailed = app.bar().length
    const reads = app.calls.filter(p => p === '/setup').length
    answer = () => ({ ok: true, configured: true, steps: [], show_in_statusbar: true })
    await app.tick()
    const items = app.bar().map(shape)
    return { ok: atRegister === 0 && afterFailed === 0 && reads === 1 && items.length === 1 && JSON.stringify(items[0]) === WANT,
      at_register: atRegister, after_failed_read: afterFailed, reads_at_start: reads, items }
  },

  // The chip throws while rendering (a backend answering servers as a number): its own boundary
  // catches it, the chip renders nothing, the bar's other items stay, and the failure is logged.
  async boundary_catches() {
    const app = boot({ snapshot: () => ({ ...SNAPSHOT, servers: 5 }) })
    app.mountBar()
    let threw = null
    try {
      await h.settle()
      await h.settle()
    } catch (error) {
      threw = String(error)
    }
    const text = h.pageText()
    const logged = logs.some(l => l.includes('the status-bar chip failed') && l.includes('TypeError'))
    return { ok: threw === null && text === 'core item' && logged, threw, text, logged }
  }
}

const name = process.argv[2]
if (!cases[name]) {
  console.log('cases:', Object.keys(cases).join(' '))
  process.exit(2)
}
cases[name]().then(
  out => {
    process.stdout.write('\nRESULT ' + JSON.stringify(out) + '\n')
    process.exit(0)
  },
  error => {
    process.stdout.write('\nRESULT ' + JSON.stringify({ ok: false, error: String(error && error.stack) }) + '\n')
    process.exit(0)
  }
)
