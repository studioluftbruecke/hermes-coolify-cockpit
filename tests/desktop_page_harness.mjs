// A small stand-in for react, react/jsx-runtime and @hermes/plugin-sdk, enough to mount the real
// desktop/plugin.js in plain node and drive it like a user: click, press keys, switch profiles,
// let time pass. The test copies plugin.js next to this file and points its three imports here.
//
// What it keeps from the real libraries, because the page's behaviour depends on it:
// - hooks keep their state per component instance across renders, effects run after a render
//   and clean up when their deps change or the component leaves the tree;
// - a keydown bubbles from its target up through every ancestor's handler, and Enter on a
//   focused, enabled button clicks it unless a handler called preventDefault (the browser's own
//   activation); a disabled button gets no click;
// - React Query's rules the page relies on: one entry per query key shared by every caller, a
//   fetch in flight is shared, `retry` is asked after each failure, `enabled: false` never
//   fetches, `refetchInterval` (a number or a function of the query) decides the next poll.
// Polling runs on a virtual clock (`advance(ms)`), so a test can count requests over minutes.
// Layout, focus traps, portals and styling are not reproduced.

// The page uses window's timers; window.open is recorded, the page must never call it.
export const windowOpens = []
globalThis.window = { setTimeout, clearTimeout, setInterval, clearInterval, open: (...args) => (windowOpens.push(args), null) }

// ── react ────────────────────────────────────────────────────────────────────────────────────
const instances = new Map()
let visited = new Set()
let current = null
let hookIndex = 0
let effects = []
let rootElement = null
let renderQueued = false
let renders = 0

export function jsx(type, props, key) {
  return { type, props: props || {}, key: key === undefined ? null : key }
}
export const jsxs = jsx

function slot(init) {
  const inst = current
  const i = hookIndex++
  if (!(i in inst.hooks)) inst.hooks[i] = init()
  return inst.hooks[i]
}

const changed = (a, b) => !a || !b || a.length !== b.length || a.some((v, i) => !Object.is(v, b[i]))

export function useState(init) {
  const s = slot(() => ({ value: typeof init === 'function' ? init() : init }))
  if (!s.set) {
    s.set = next => {
      const value = typeof next === 'function' ? next(s.value) : next
      if (!Object.is(value, s.value)) {
        s.value = value
        schedule()
      }
    }
  }
  return [s.value, s.set]
}

export function useRef(value) {
  return slot(() => ({ current: value }))
}

export function useMemo(fn, deps) {
  const s = slot(() => ({ deps: null }))
  if (s.deps === null || changed(s.deps, deps)) {
    s.value = fn()
    s.deps = deps
  }
  return s.value
}

export function useEffect(fn, deps) {
  const s = slot(() => ({ deps: null, effect: true }))
  if (s.deps === null || deps === undefined || changed(s.deps, deps)) {
    s.deps = deps || []
    effects.push(() => {
      if (s.cleanup) s.cleanup()
      const out = fn()
      s.cleanup = typeof out === 'function' ? out : null
    })
  }
}

let focused = null
export const focusedNode = () => (focused && hosts.get(focused.hid) === focused ? focused : null)
const hosts = new Map()
let seenHosts = new Set()

function expand(node, path, parent) {
  if (node == null || typeof node === 'boolean') return []
  if (typeof node === 'string' || typeof node === 'number') return [String(node)]
  if (Array.isArray(node)) return node.flatMap((child, i) => expand(child, path + '/' + (child && child.key != null ? 'k:' + child.key : i), parent))
  if (typeof node.type === 'function') {
    const id = path + ':' + (node.type.name || 'anonymous') + (node.key != null ? '#' + node.key : '')
    let inst = instances.get(id)
    if (!inst) instances.set(id, (inst = { hooks: [] }))
    visited.add(id)
    const saved = [current, hookIndex]
    current = inst
    hookIndex = 0
    let out
    try {
      out = node.type(node.props)
    } finally {
      ;[current, hookIndex] = saved
    }
    return expand(out, id, parent)
  }
  // A host node keeps its identity across renders, as a DOM node does, so a focused button or a
  // node a test holds sees its current props.
  const hid = path + '>' + node.type
  let host = hosts.get(hid)
  if (!host || host.type !== node.type) {
    host = { type: node.type, hid, focus: () => (focused = host) }
    hosts.set(hid, host)
  }
  seenHosts.add(hid)
  Object.assign(host, { props: node.props, parent })
  if (node.props.ref && typeof node.props.ref === 'object') node.props.ref.current = host
  host.children = expand(node.props.children, hid, host)
  return [host]
}

let tree = []
function render() {
  renders += 1
  visited = new Set()
  effects = []
  observed = new Map()
  seenHosts = new Set()
  tree = expand(rootElement, 'root', null)
  for (const hid of [...hosts.keys()]) if (!seenHosts.has(hid)) hosts.delete(hid)
  for (const [id, inst] of instances) {
    if (visited.has(id)) continue
    for (const h of inst.hooks) if (h && h.cleanup) h.cleanup()
    instances.delete(id)
  }
  const run = effects
  effects = []
  for (const fn of run) fn()
}

function schedule() {
  if (renderQueued || !rootElement) return
  renderQueued = true
  queueMicrotask(() => {
    renderQueued = false
    render()
  })
}

export function mount(element) {
  rootElement = element
  render()
}

// ── nanostores ───────────────────────────────────────────────────────────────────────────────
export function atom(value) {
  const listeners = new Set()
  return {
    get: () => value,
    set: next => {
      if (Object.is(next, value)) return
      value = next
      listeners.forEach(l => l(value))
      schedule()
    },
    listen: l => (listeners.add(l), () => listeners.delete(l)),
    subscribe: l => (listeners.add(l), l(value), () => listeners.delete(l))
  }
}
export const useValue = a => (a && typeof a.get === 'function' ? a.get() : a)

// ── react query ──────────────────────────────────────────────────────────────────────────────
export const clock = { now: 0 }
const entries = new Map()
let observed = new Map()
let inflight = 0

function entryFor(queryKey) {
  const k = JSON.stringify(queryKey)
  let e = entries.get(k)
  if (!e) {
    e = { k, queryKey, state: { data: undefined, error: null, errorUpdateCount: 0, dataUpdatedAt: 0 }, fetching: null, fetched: false, lastFetchAt: 0, opts: null }
    entries.set(k, e)
  }
  return e
}

function fetchEntry(e, opts) {
  if (e.fetching) return e.fetching
  const o = opts || e.opts
  inflight += 1
  schedule()
  e.fetching = (async () => {
    let failures = 0
    for (;;) {
      try {
        const data = await o.queryFn()
        e.state = { ...e.state, data, error: null, dataUpdatedAt: clock.now }
        break
      } catch (error) {
        const r = o.retry
        const again = typeof r === 'function' ? r(failures, error) : typeof r === 'number' ? failures < r : r !== false
        failures += 1
        if (!again) {
          e.state = { ...e.state, error, errorUpdateCount: e.state.errorUpdateCount + 1 }
          break
        }
      }
    }
  })().finally(() => {
    e.fetching = null
    e.fetched = true
    e.lastFetchAt = clock.now
    inflight -= 1
    schedule()
  })
  return e.fetching
}

export function useQuery(opts) {
  const e = entryFor(opts.queryKey)
  e.opts = opts
  const enabled = opts.enabled !== false
  if (enabled) {
    const list = observed.get(e.k) || []
    list.push(opts)
    observed.set(e.k, list)
    if (!e.fetched && !e.fetching) void fetchEntry(e, opts)
  }
  const { data, error } = e.state
  const fetching = Boolean(e.fetching)
  return {
    data,
    error,
    isFetching: fetching,
    isLoading: enabled && data === undefined && (fetching || !e.fetched),
    refetch: () => fetchEntry(e, opts)
  }
}

const startsWith = (key, prefix) => prefix.every((v, i) => JSON.stringify(v) === JSON.stringify(key[i]))

const client = {
  invalidateQueries: async ({ queryKey } = {}) => {
    const hits = [...entries.values()].filter(e => !queryKey || startsWith(e.queryKey, queryKey))
    for (const e of hits) e.fetched = false
    await Promise.all(hits.filter(e => observed.has(e.k)).map(e => fetchEntry(e)))
    schedule()
  },
  cancelQueries: async () => {},
  setQueryData: (queryKey, updater) => {
    const e = entryFor(queryKey)
    e.state = { ...e.state, data: typeof updater === 'function' ? updater(e.state.data) : updater, error: null }
    e.fetched = true
    schedule()
  },
  getQueryData: queryKey => entryFor(queryKey).state.data,
  fetchQuery: ({ queryKey, queryFn }) => {
    const e = entryFor(queryKey)
    return fetchEntry(e, { ...(e.opts || {}), queryFn, retry: false }).then(() => {
      if (e.state.error) throw e.state.error
      return e.state.data
    })
  }
}
export const useQueryClient = () => client

function interval(opts, e) {
  const r = opts.refetchInterval
  const v = typeof r === 'function' ? r({ state: e.state, queryKey: e.queryKey }) : r
  return typeof v === 'number' && v > 0 ? v : null
}

/** Let `ms` of virtual time pass: every observed query polls on its own refetchInterval. */
export async function advance(ms) {
  const end = clock.now + ms
  for (;;) {
    await settle()
    let next = null
    for (const [k, list] of observed) {
      const e = entries.get(k)
      const every = list.map(o => interval(o, e)).filter(v => v !== null)
      if (!every.length) continue
      const due = e.lastFetchAt + Math.min(...every)
      if (due <= end && (!next || due < next.due)) next = { e, due }
    }
    if (!next) break
    clock.now = Math.max(clock.now, next.due)
    await fetchEntry(next.e)
  }
  clock.now = end
  await settle()
}

/** Wait until no render is queued and no fetch is in flight. */
export async function settle() {
  for (let quiet = 0, n = 0; quiet < 3 && n < 500; n += 1) {
    await new Promise(r => setTimeout(r, 0))
    quiet = renderQueued || inflight ? 0 : quiet + 1
  }
}

// ── @hermes/plugin-sdk ───────────────────────────────────────────────────────────────────────
const UI_ONLY = ['variant', 'size', 'spinning', 'tone', 'sizing', 'onCheckedChange', 'checked', 'onOpenAutoFocus', 'onOpenChange', 'open', 'className']
const strip = props => Object.fromEntries(Object.entries(props).filter(([k]) => !UI_ONLY.includes(k)))
const pass = tag => props => jsx(tag, strip(props))

export const Button = props => jsx('button', { type: 'button', ...strip(props) })
export const Badge = pass('span')
export const Codicon = props => jsx('i', { 'data-icon': props.name })
export const StatusDot = () => jsx('i', { 'data-dot': '' })
export const Skeleton = pass('div')
export const LogView = pass('pre')
export const Input = pass('input')
export const Textarea = pass('textarea')
export const Switch = props => jsx('button', { role: 'switch', 'aria-checked': Boolean(props.checked), onClick: () => props.onCheckedChange && props.onCheckedChange(!props.checked) })
export const SegmentedControl = ({ options, onChange }) => jsx('div', { children: (options || []).map(o => jsx('button', { type: 'button', onClick: () => onChange(o.id), children: o.label }, o.id)) })
export const EmptyState = ({ title, description }) => jsx('div', { children: [title, ' ', description] })
export const ErrorState = ({ title, description }) => jsx('div', { children: [title, ' ', description] })
export function Dialog({ open, children }) {
  return open ? jsx('div', { 'data-dialog': '', children }) : null
}
export function DialogContent(props) {
  useEffect(() => {
    if (props.onOpenAutoFocus) props.onOpenAutoFocus({ preventDefault() {} })
  }, [])
  return jsx('div', { role: 'dialog', ...strip(props) })
}
export const DialogHeader = pass('div')
export const DialogFooter = pass('div')
export const DialogTitle = pass('h2')
export const DialogDescription = pass('p')
export function ConfirmDialog({ open, title, description, children, confirmLabel, onConfirm, onClose }) {
  if (!open) return null
  return jsx('div', { role: 'dialog', children: [jsx('h2', { children: title }), jsx('p', { children: description }), children,
    jsx('button', { type: 'button', onClick: onClose, children: 'Cancel' }),
    jsx('button', { type: 'button', onClick: () => onConfirm(), children: confirmLabel })] })
}

export const ROUTES_AREA = 'routes'
export const SIDEBAR_NAV_AREA = 'sidebar.nav'
export const PALETTE_AREA = 'palette'
export const STATUSBAR_AREAS = { left: 'statusBar.left', right: 'statusBar.right' }

export const hostCalls = []
const record = name => (...args) => {
  hostCalls.push({ name, args })
  return Promise.resolve(true)
}
export const host = {
  state: { profile: atom('default'), connectionId: atom('local') },
  navigate: record('navigate'),
  request: record('request'),
  openSession: record('openSession'),
  newChat: record('newChat'),
  notify: record('notify'),
  composer: { setDraft: record('setDraft') }
}

// ── driving the page ─────────────────────────────────────────────────────────────────────────
export const text = n => (typeof n === 'string' ? n : n ? n.children.map(text).join('') : '')
export const pageText = () => tree.map(text).join('')

export function all(test, nodes = tree) {
  return nodes.flatMap(n => (typeof n === 'string' ? [] : [...(test(n) ? [n] : []), ...all(test, n.children)]))
}

export const inDialog = n => {
  for (let p = n; p; p = p.parent) if (p.props && p.props.role === 'dialog') return true
  return false
}

export function buttons(label) {
  return all(n => n.type === 'button' && (label instanceof RegExp ? label.test(text(n).trim()) : text(n).trim() === label))
}

export const nativeOpens = []

const HANDLER = { click: 'onClick', keydown: 'onKeyDown' }

/** Dispatch a DOM-like event at `node`: bubbles to the root, then the browser's default action. */
export function fire(node, type, init) {
  if (type === 'click' && node.type === 'button' && node.props.disabled) return null
  const event = {
    type,
    key: init && init.key,
    target: node,
    currentTarget: null,
    defaultPrevented: false,
    stopped: false,
    preventDefault() {
      this.defaultPrevented = true
    },
    stopPropagation() {
      this.stopped = true
    }
  }
  for (let n = node; n && !event.stopped; n = n.parent) {
    const handler = n.props && n.props[HANDLER[type]]
    if (typeof handler === 'function') {
      event.currentTarget = n
      handler(event)
    }
  }
  if (!event.defaultPrevented) {
    if (type === 'keydown' && event.key === 'Enter' && node.type === 'button') fire(node, 'click')
    if (type === 'click' && node.type === 'a') nativeOpens.push(node.props.href)
  }
  return event
}

export const click = node => fire(node, 'click')
export const press = (node, key) => fire(node, 'keydown', { key })
export const renderCount = () => renders
