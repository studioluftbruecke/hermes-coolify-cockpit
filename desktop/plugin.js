// Coolifybrücke: the Desktop half. One page (/coolifybruecke) that renders the saved layout as a
// grid of widgets, a status-bar chip, a sidebar row and a palette command.
//
// Everything the page shows comes from this plugin's own backend routes (dashboard/plugin_api.py)
// through ctx.rest; the layout lives in the plugin data dir on the Hermes backend, so the agent's
// coolify_cockpit tool and this page edit the same file (every save is a compare-and-set on its
// version; a 409 means someone else saved first). Actions are the user's own clicks: each one
// opens a confirm dialog naming action, resource, environment and server, built from the route's
// dry run, and runs only the API token. The cockpit chat is a normal Hermes session opened
// beside the page with a drafted question; nothing is ever sent without the user pressing send.
//
// Plain ESM, loaded uncompiled: jsx() calls, imports only @hermes/plugin-sdk, react and
// react/jsx-runtime, theme variables only, no eval.

import {
  atom,
  Badge,
  Button,
  Codicon,
  ConfirmDialog,
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  EmptyState,
  ErrorState,
  host,
  Input,
  LogView,
  PALETTE_AREA,
  ROUTES_AREA,
  SegmentedControl,
  SIDEBAR_NAV_AREA,
  Skeleton,
  STATUSBAR_AREAS,
  StatusDot,
  Switch,
  Textarea,
  useQuery,
  useQueryClient,
  useValue
} from '@hermes/plugin-sdk'
import { useEffect, useMemo, useRef, useState } from 'react'
import { jsx, jsxs } from 'react/jsx-runtime'

const ID = 'coolifybruecke'
const PATH = '/coolifybruecke'
const TAG = '[' + ID + ']'
const SESSION_TITLE = 'Cockpit'
const NAV_LABEL = 'Coolifybrücke'
const SETTINGS_PAGE = '/settings?tab=plugins&agent=' + ID
const PLUGINS_PAGE = '/capabilities?tab=plugins'
const TOKEN_HINT = 'A token you change in the plugin settings is used at once.'
const TERMINAL = new Set(['finished', 'failed', 'cancelled', 'cancelled-by-user', 'error'])

let pluginCtx = null

// Page-wide state shared by widgets that do not sit in one React subtree.
const $pending = atom(null) // the action awaiting confirmation
const $tracked = atom([]) // actions sent from this page, followed until their deployment ends
const $diagnose = atom(null) // the deployment the diagnosis panel looks at
const $chips = atom([]) // @coolify: context chips for the chat widget
const $banner = atom(null) // { tone, title, text } shown at the top of the page

function log(step, value) {
  try {
    console.info(TAG, step, value === undefined ? '' : value)
  } catch {
    /* no console */
  }
}

// ── small JSX helpers ────────────────────────────────────────────────────────────────────────────

function el(type, props, ...children) {
  const { key, ...rest } = props || {}
  const kids = children.filter(c => c !== undefined)
  if (kids.length === 0) return jsx(type, rest, key)
  if (kids.length === 1) return jsx(type, { ...rest, children: kids[0] }, key)
  return jsxs(type, { ...rest, children: kids }, key)
}

const T = {
  primary: 'var(--ui-text-primary)',
  secondary: 'var(--ui-text-secondary)',
  tertiary: 'var(--ui-text-tertiary)',
  quaternary: 'var(--ui-text-quaternary)',
  stroke: 'var(--ui-stroke-secondary)',
  strokeSoft: 'var(--ui-stroke-tertiary)',
  accent: 'var(--ui-accent)',
  red: 'var(--ui-red)',
  green: 'var(--ui-green)',
  yellow: 'var(--ui-yellow)',
  fill: 'var(--ui-bg-quaternary)'
}

const tint = (color, pct) => 'color-mix(in srgb, ' + color + ' ' + pct + '%, transparent)'

const S = {
  row: (gap = 8, extra) => ({ display: 'flex', alignItems: 'center', gap, minWidth: 0, ...extra }),
  col: (gap = 8, extra) => ({ display: 'flex', flexDirection: 'column', gap, minWidth: 0, ...extra }),
  small: { fontSize: '0.6875rem', lineHeight: '1rem', color: T.tertiary },
  label: { fontSize: '0.6875rem', lineHeight: '1rem', color: T.quaternary },
  text: { fontSize: '0.75rem', lineHeight: '1.125rem', color: T.secondary },
  strong: { fontSize: '0.75rem', lineHeight: '1.125rem', color: T.primary, fontWeight: 500 },
  mono: { fontFamily: 'var(--dt-font-mono, ui-monospace, monospace)', fontVariantNumeric: 'tabular-nums' },
  ellipsis: { overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', minWidth: 0 },
  hair: { borderTop: '1px solid ' + T.strokeSoft },
  box: { border: '1px solid ' + T.strokeSoft, borderRadius: 6, padding: '8px 10px' }
}

// Server messages quote rights in Markdown backticks; the page shows them as plain text.
const plain = text => String(text == null ? '' : text).replace(/`/g, '')

function Small({ children, style }) {
  return el('span', { style: { ...S.small, ...style } }, children)
}

function Note({ tone, icon, children, action }) {
  const color = tone === 'bad' ? T.red : tone === 'warn' ? T.yellow : tone === 'good' ? T.green : T.tertiary
  return el(
    'div',
    {
      role: tone === 'bad' ? 'alert' : undefined,
      style: {
        ...S.row(8, { alignItems: 'flex-start' }),
        borderRadius: 6,
        padding: '6px 10px',
        background: tone ? tint(color, 9) : tint(T.secondary, 5),
        border: '1px solid ' + (tone ? tint(color, 28) : T.strokeSoft)
      }
    },
    el(Codicon, { name: icon || (tone === 'bad' ? 'error' : tone === 'warn' ? 'warning' : 'info'), size: '0.8rem', style: { color, marginTop: 2 } }),
    el('div', { style: { ...S.text, flex: 1, minWidth: 0, whiteSpace: 'pre-wrap', overflowWrap: 'anywhere' } }, children),
    action || null
  )
}

function IconButton({ icon, label, onClick, disabled }) {
  return el(Button, { 'aria-label': label, title: label, disabled, onClick, size: 'icon-xs', variant: 'ghost' },
    el(Codicon, { name: icon, size: '0.8rem' }))
}

function openExternal(href) {
  const os = pluginCtx && pluginCtx.os
  Promise.resolve(os && typeof os.openExternal === 'function' ? os.openExternal(href) : false)
    .then(ok => {
      if (!ok) window.open(href, '_blank', 'noopener')
    })
    .catch(() => window.open(href, '_blank', 'noopener'))
}

function ExternalLink({ href, children }) {
  if (!href) return children
  return el(
    'a',
    {
      href,
      onClick: event => {
        event.preventDefault()
        openExternal(href)
      },
      rel: 'noreferrer',
      style: { color: T.accent, textDecoration: 'none' },
      target: '_blank',
      title: href
    },
    children
  )
}

// ── formatting ───────────────────────────────────────────────────────────────────────────────────

function ago(iso) {
  const t = iso ? Date.parse(iso) : NaN
  if (!Number.isFinite(t)) return ''
  const s = Math.max(0, Math.round((Date.now() - t) / 1000))
  if (s < 45) return s + ' s ago'
  if (s < 3600) return Math.round(s / 60) + ' min ago'
  if (s < 86400) return Math.round(s / 3600) + ' h ago'
  return Math.round(s / 86400) + ' d ago'
}

function duration(start, end) {
  const a = Date.parse(start || '')
  const b = Date.parse(end || '')
  if (!Number.isFinite(a) || !Number.isFinite(b)) return ''
  const s = Math.max(0, Math.round((b - a) / 1000))
  return s < 120 ? s + ' s' : Math.round(s / 60) + ' min'
}

function bytes(n) {
  if (typeof n !== 'number' || !Number.isFinite(n)) return '?'
  const gib = n / 1024 ** 3
  return gib >= 10 ? Math.round(gib) + ' GiB' : gib.toFixed(1) + ' GiB'
}

function rate(n) {
  if (typeof n !== 'number' || !Number.isFinite(n)) return '?'
  if (n > 1024 * 1024) return (n / 1024 / 1024).toFixed(1) + ' MB/s'
  if (n > 1024) return (n / 1024).toFixed(1) + ' kB/s'
  return Math.round(n) + ' B/s'
}

function pct(n) {
  return typeof n === 'number' && Number.isFinite(n) ? n.toFixed(n < 10 ? 1 : 0) + ' %' : '?'
}

function shortSha(commit) {
  return commit && /^[0-9a-f]{12,}$/i.test(commit) ? commit.slice(0, 7) : commit || ''
}

function firstLine(text) {
  return String(text || '').split('\n')[0]
}

function hostOf(url) {
  try {
    return new URL(url).host
  } catch {
    return url || ''
  }
}

function resourceTone(status) {
  const s = String(status || '')
  if (s.includes('unhealthy')) return 'warn'
  if (s.startsWith('running')) return 'good'
  if (s.startsWith('exited') || s.startsWith('stopped') || s.includes('degraded')) return 'bad'
  return 'muted'
}

function deploymentTone(status) {
  const s = String(status || '')
  if (s === 'finished') return 'good'
  if (s === 'failed' || s === 'error') return 'bad'
  if (s === 'in_progress' || s === 'queued') return 'warn'
  return 'muted'
}

function matches(value, wanted) {
  if (!wanted) return true
  return String(value || '').toLowerCase().includes(String(wanted).toLowerCase())
}

// ── backend ──────────────────────────────────────────────────────────────────────────────────────

// ctx.rest rejects on HTTP >= 400 with "<status>: <body>" somewhere in the message (Electron adds
// its own prefix). Recover the status and the JSON body so a 409 can hand back the current layout.
function restError(error) {
  const message = error && error.message ? String(error.message) : String(error)
  const found = message.match(/\b([1-5]\d\d): ([\s\S]*)$/)
  let status = found ? Number(found[1]) : error && error.statusCode ? Number(error.statusCode) : 0
  let body = null
  const raw = found ? found[2] : message
  const start = raw.indexOf('{')
  if (start >= 0) {
    try {
      body = JSON.parse(raw.slice(start))
    } catch {
      body = null
    }
  }
  if (body && body.detail && !body.message) body.message = typeof body.detail === 'string' ? body.detail : JSON.stringify(body.detail)
  const limited = isRateLimited({ status, body })
  const out = new Error(limited ? rateLimitText(body) : (body && body.message) || raw || 'Request failed')
  out.status = status
  out.body = body
  out.rateLimited = limited
  return out
}

// Coolify's API rate limit (API_RATE_LIMIT, 200 requests per minute per Coolify user by default, shared
// by all of that user's tokens) answers 429; every route passes it on as { error: 'rate_limited',
// retry_after_s }. Asking again at once only meets the same limit, so no query retries it.
function isRateLimited(error) {
  return Boolean(error && (error.status === 429 || (error.body && error.body.error === 'rate_limited')))
}

function rateLimitText(body) {
  const wait = body && typeof body.retry_after_s === 'number' ? body.retry_after_s + ' s' : 'a minute'
  return 'Coolify is limiting API requests; try again in ' + wait + '.'
}

const RATE_LIMIT_DETAIL =
  "Coolify allows API_RATE_LIMIT requests per minute (200 by default) per Coolify user, shared by all of that user's tokens. Nothing was sent."

const retryOnce = (failures, error) => failures < 1 && !isRateLimited(error)

async function rest(path, opts) {
  if (!pluginCtx || typeof pluginCtx.rest !== 'function') throw new Error('ctx.rest is not available in this Desktop build')
  try {
    return await pluginCtx.rest(path, { timeoutMs: 60000, ...(opts || {}) })
  } catch (error) {
    const out = restError(error)
    log('rest ' + ((opts && opts.method) || 'GET') + ' ' + path + ' failed', { status: out.status, message: out.message })
    throw out
  }
}

function useProfile() {
  return useValue(host.state.profile) || ''
}

const qk = (profile, ...rest) => [ID, profile, ...rest]

function fastPoll(query) {
  const running = (((query.state.data || {}).deployments || {}).running || []).length > 0
  const followed = $tracked.get().some(t => !t.done)
  return running || followed
}

// The settings, without a Coolify request (GET /setup). Polled every few seconds while something is
// missing, so a value saved in the settings form shows here without a reload.
function useSetup() {
  const profile = useProfile()
  return useQuery({
    queryKey: qk(profile, 'setup'),
    queryFn: () => rest('/setup'),
    refetchInterval: query => (query.state.data && query.state.data.configured ? 60000 : 4000),
    staleTime: 2000,
    retry: retryOnce
  })
}

// 'off': the Hermes half is not enabled in this profile. 'needed': URL or token missing. 'ready':
// configured, or a backend older than GET /setup (its 404 names no plugin). null: still asking.
function setupState(setup) {
  if (setup.error) {
    if (setup.error.status === 404 && /not enabled/i.test(setup.error.message || '')) return 'off'
    return setup.error.status === 404 ? 'ready' : null
  }
  if (!setup.data) return null
  return setup.data.configured ? 'ready' : 'needed'
}

function useSnapshot() {
  const profile = useProfile()
  const ready = setupState(useSetup()) === 'ready'
  return useQuery({
    enabled: ready,
    queryKey: qk(profile, 'snapshot'),
    queryFn: () => rest('/snapshot'),
    refetchInterval: query => (fastPoll(query) ? 15000 : 60000),
    staleTime: 10000,
    retry: retryOnce
  })
}

function useCapabilities() {
  const profile = useProfile()
  const ready = setupState(useSetup()) === 'ready'
  return useQuery({
    enabled: ready,
    queryKey: qk(profile, 'capabilities'),
    queryFn: () => rest('/capabilities'),
    refetchInterval: 300000,
    staleTime: 240000,
    retry: retryOnce
  })
}

function useLayout() {
  const profile = useProfile()
  return useQuery({
    queryKey: qk(profile, 'layout'),
    queryFn: () => rest('/layout'),
    refetchInterval: 15000,
    staleTime: 5000,
    retry: retryOnce
  })
}

// Per-widget availability (one capabilities check on the backend), refreshed when the layout's
// version changes so a widget the agent adds is checked too.
function useAvailability(version) {
  const profile = useProfile()
  const ready = setupState(useSetup()) === 'ready'
  return useQuery({
    queryKey: qk(profile, 'availability', version),
    queryFn: () => rest('/layout?availability=true'),
    enabled: ready && typeof version === 'number',
    refetchInterval: 300000,
    staleTime: 240000,
    retry: retryOnce
  })
}

function useActionCatalog() {
  const profile = useProfile()
  const ready = setupState(useSetup()) === 'ready'
  return useQuery({
    enabled: ready,
    queryKey: qk(profile, 'actions'),
    queryFn: () => rest('/actions'),
    refetchInterval: 300000,
    staleTime: 240000,
    retry: retryOnce
  })
}

function useNotes(maxChars) {
  const profile = useProfile()
  return useQuery({
    queryKey: qk(profile, 'notes'),
    queryFn: () => rest('/notes?max_chars=' + (maxChars || 12000)),
    refetchInterval: 60000,
    staleTime: 20000,
    retry: retryOnce
  })
}

function useHistory(resourceUuid, fast) {
  const profile = useProfile()
  return useQuery({
    queryKey: qk(profile, 'history', resourceUuid),
    queryFn: () => rest('/resources/' + encodeURIComponent(resourceUuid) + '/deployments?take=10'),
    enabled: Boolean(resourceUuid),
    refetchInterval: fast ? 5000 : false,
    staleTime: 5000,
    retry: retryOnce
  })
}

// ── rights: which buttons may appear ─────────────────────────────────────────────────────────────

function parseScope(token) {
  // The capabilities report lists what a scoped token sees as "Project [env, env]".
  if (!token) return null
  if (token.scope !== 'scoped') return { all: true }
  const projects = Array.isArray(token.projects) ? token.projects : []
  if (projects.some(p => /^\.\.\. \d+ more$/.test(String(p)))) return { all: true, truncated: true }
  const map = new Map()
  for (const entry of projects) {
    const found = String(entry).match(/^(.*) \[(.*)\]$/)
    if (found) map.set(found[1].toLowerCase(), found[2].split(',').map(s => s.trim().toLowerCase()))
  }
  return { all: false, map }
}

function useRights() {
  const catalog = useActionCatalog()
  const caps = useCapabilities()
  return useMemo(() => {
    const body = catalog.data || {}
    const specs = new Map((body.actions || []).map(spec => [spec.kind, spec]))
    const tokens = (caps.data && caps.data.tokens) || []
    const actionToken = tokens.find(t => t.role === 'api' || t.role === 'single') || null
    const scope = parseScope(actionToken)
    const canAct = Boolean(body.action_token && body.action_token.deploy === true)
    const reason = canAct
      ? null
      : (body.unavailable && body.unavailable.message) ||
        (body.action_token ? 'The API token lacks deploy, so the cockpit shows no action buttons.' : null)
    const inScope = resource => {
      if (!canAct || !resource || !scope) return false
      if (scope.all) return true
      const envs = scope.map.get(String(resource.project || '').toLowerCase())
      return Boolean(envs && envs.includes(String(resource.environment || '').toLowerCase()))
    }
    const allows = (kind, resource) => {
      const spec = specs.get(kind)
      return Boolean(spec && inScope(resource) && (spec.target_kinds || []).includes(resource.kind))
    }
    return { canAct, reason, specs, inScope, allows, loaded: Boolean(catalog.data && caps.data), token: actionToken }
  }, [catalog.data, caps.data])
}

// ── actions: confirm, send, follow ───────────────────────────────────────────────────────────────

function requestAction(kind, target, extra) {
  $pending.set({ kind, target, ...(extra || {}), at: Date.now() })
}

function ActionButton({ kind, label, target, resource, deployment, params, rights, variant }) {
  const spec = rights.specs.get(kind)
  return el(
    Button,
    {
      onClick: () => requestAction(kind, target, { params, resourceName: resource && resource.name, resourceUuid: resource && resource.uuid, deployment }),
      size: 'xs',
      title: spec && spec.note ? spec.note : undefined,
      variant: variant || (kind === 'stop' ? 'outline' : 'secondary')
    },
    label || (spec ? spec.label : kind)
  )
}

function PlanLine({ name, value }) {
  return el(
    'div',
    { style: { display: 'grid', gridTemplateColumns: '6.5rem 1fr', gap: 8 } },
    el('span', { style: S.label }, name),
    el('span', { style: { ...S.text, color: T.primary, overflowWrap: 'anywhere' } }, value)
  )
}

// The confirm dialog is built from the SDK's Dialog parts, not ConfirmDialog: ConfirmDialog keeps its
// confirm button enabled whatever the body says, so a failed check could still be clicked, and its
// thrown error then showed the check's message a second time. Here the check's error shows once, the
// confirm button stays disabled until the dry run succeeded, and "Check again" re-runs the dry run
// (POST /actions plans and checks again on the server in any case).
function ActionConfirm() {
  const pending = useValue($pending)
  const profile = useProfile()
  const client = useQueryClient()
  const catalog = useActionCatalog() // the page's cached catalog: names the action while the check runs
  const [sendState, setSendState] = useState('idle') // idle | sending | sent
  const [sendError, setSendError] = useState(null)
  const confirmRef = useRef(null)
  const cancelRef = useRef(null)
  const closeTimer = useRef(null)
  const caps = useCapabilities()
  // M5 "Redeploy what changed": one confirm for several apps. Each target gets its own dry run (the
  // server plans and checks each, as for one click); the dialog lists them all, one click sends all.
  const plan = useQuery({
    queryKey: qk(profile, 'plan', pending && pending.at),
    queryFn: async () => {
      if (pending.bulk) {
        const plans = await Promise.all(pending.bulk.map(b => rest('/actions', { method: 'POST', body: { kind: pending.kind, target: b.uuid, dry_run: true } })))
        return { bulk: plans, label: plans[0] && plans[0].label, confirm_text: 'deploy ' + plans.map(p => p.target.name).join(' and '), target: {} }
      }
      return rest('/actions', { method: 'POST', body: { kind: pending.kind, target: pending.target, params: pending.params, dry_run: true } })
    },
    enabled: Boolean(pending),
    retry: false,
    staleTime: Infinity
  })
  const checking = plan.isFetching
  const ready = Boolean(plan.data) && !plan.error && !checking
  const checkFailed = Boolean(plan.error) && !checking
  useEffect(() => {
    setSendState('idle')
    setSendError(null)
  }, [pending && pending.at])
  useEffect(() => {
    if (ready && confirmRef.current) confirmRef.current.focus()
  }, [ready])
  useEffect(() => () => closeTimer.current && window.clearTimeout(closeTimer.current), [])
  if (!pending) return null
  const data = plan.data
  const target = (data && data.target) || {}
  const spec = ((catalog.data && catalog.data.actions) || []).find(a => a.kind === pending.kind)
  const kindLabel = (data && data.label) || (spec && spec.label) || pending.kind
  const name = (data && data.bulk ? pending.resourceName : target.name) || pending.resourceName || pending.target
  const busy = sendState !== 'idle'
  const close = () => {
    if (sendState !== 'sending') $pending.set(null)
  }
  const sendBulk = async () => {
    const sent = []
    for (const p of data.bulk) {
      const result = await rest('/actions', { method: 'POST', body: { kind: pending.kind, target: p.target.uuid } })
      sent.push({
        id: pending.at + '-' + p.target.uuid,
        kind: result.kind || pending.kind,
        label: kindLabel,
        resourceName: p.target.name,
        resourceUuid: p.target.uuid,
        deploymentUuid: result.deployment_uuid || null,
        message: result.message || '',
        sentAt: new Date().toISOString(),
        mine: true,
        done: !result.deployment_uuid
      })
    }
    log('bulk sent', { kind: pending.kind, count: sent.length })
    $tracked.set(sent.concat($tracked.get()).slice(0, 8))
    void client.invalidateQueries({ queryKey: qk(profile, 'snapshot') })
  }
  const send = async () => {
    if (!ready || busy) return
    setSendError(null)
    setSendState('sending')
    try {
      if (data.bulk) {
        await sendBulk()
        setSendState('sent')
        closeTimer.current = window.setTimeout(() => {
          closeTimer.current = null
          $pending.set(null)
        }, 600)
        return
      }
      const result = await rest('/actions', { method: 'POST', body: { kind: pending.kind, target: pending.target, params: pending.params } })
      log('action sent', { kind: result.kind, deployment: result.deployment_uuid, ok: result.ok })
      $tracked.set(
        [
          {
            id: pending.at,
            kind: result.kind || pending.kind,
            label: kindLabel,
            resourceName: name,
            resourceUuid: target.kind === 'deployment' ? target.resource_uuid : target.uuid || pending.resourceUuid,
            deploymentUuid: result.deployment_uuid || null,
            message: result.message || '',
            sentAt: new Date().toISOString(),
            mine: true,
            done: !result.deployment_uuid
          }
        ].concat($tracked.get().slice(0, 4))
      )
      void client.invalidateQueries({ queryKey: qk(profile, 'snapshot') })
      if (target.uuid) void client.invalidateQueries({ queryKey: qk(profile, 'history', target.uuid) })
      setSendState('sent')
      closeTimer.current = window.setTimeout(() => {
        closeTimer.current = null
        $pending.set(null)
      }, 600)
    } catch (error) {
      setSendState('idle')
      setSendError(error)
    }
  }
  const failure = error =>
    el(
      Note,
      { tone: 'bad' },
      error.message,
      error.rateLimited
        ? el('div', { style: { ...S.small, marginTop: 4 } }, RATE_LIMIT_DETAIL)
        : error.body && error.body.hint
          ? el('div', { style: { ...S.small, marginTop: 4 } }, error.body.hint)
          : undefined
    )
  const protectedEnv = env => isProtected(caps, env)
  const body = checking
    ? el('div', { style: S.col(6) }, el(Skeleton, { style: { height: 14, width: '70%' } }), el(Skeleton, { style: { height: 14, width: '50%' } }))
    : checkFailed
      ? failure(plan.error)
      : data && data.bulk
      ? el(
          'div',
          { 'data-cc-bulk': data.bulk.length, style: S.col(6) },
          ...data.bulk.map(p =>
            el(
              'div',
              { key: p.target.uuid, style: S.row(8) },
              el(StatusDot, { tone: protectedEnv(p.target.environment) ? 'bad' : 'muted' }),
              el('span', { style: S.strong }, p.target.name),
              el(Badge, { size: 'xs', variant: protectedEnv(p.target.environment) ? 'destructive' : 'outline' }, p.target.environment || 'unknown'),
              el(Small, {}, p.target.server || '')
            )
          ),
          data.bulk.some(p => protectedEnv(p.target.environment)) ? el(Note, { tone: 'bad' }, 'This includes a protected environment.') : null
        )
      : el(
          'div',
          { style: S.col(6) },
          el(PlanLine, { name: 'Action', value: kindLabel }),
          el(PlanLine, { name: target.kind === 'deployment' ? 'Deployment' : 'Resource', value: name + (target.kind && target.kind !== 'deployment' ? ' (' + target.kind + ')' : '') }),
          target.project ? el(PlanLine, { name: 'Project', value: target.project }) : null,
          el(PlanLine, { name: 'Environment', value: target.environment || 'unknown' }),
          el(PlanLine, { name: 'Server', value: target.server || 'unknown' }),
          data && data.token ? el(PlanLine, { name: 'Token', value: data.token.setting + ' (sha256 ' + data.token.fingerprint + ')' }) : null,
          data && data.request ? el(PlanLine, { name: 'Request', value: el('span', { style: S.mono }, data.request.method + ' ' + data.request.path + paramText(data.request.params)) }) : null,
          pending.kind === 'stop'
            ? el(Note, { tone: 'warn' }, 'Stop takes the app offline until you start it again. The cockpit always sends docker_cleanup=false.')
            : null,
          protectedEnv(target.environment) ? el(Note, { tone: 'bad' }, 'This is ' + target.environment + '.') : null
        )
  const description = ready
    ? 'Coolify will ' + data.confirm_text + '.'
    : checkFailed
      ? 'The check did not finish, so nothing can be sent yet.'
      : 'Checking what this click would do.'
  const confirmLabel = sendState === 'sending' ? 'Sending' : sendState === 'sent' ? 'Sent' : data && data.bulk ? 'Redeploy ' + data.bulk.length + ' apps' : kindLabel + ' ' + (name.length > 28 ? 'now' : name)
  return el(
    Dialog,
    { onOpenChange: open => !open && close(), open: true },
    el(
      DialogContent,
      {
        className: 'max-w-md',
        'data-cockpit': 'action-confirm',
        onKeyDown: event => {
          if (event.key === 'Enter' && ready && !busy) {
            event.preventDefault()
            void send()
          }
        },
        onOpenAutoFocus: event => {
          // Keep focus inside the dialog (Enter on the page's button would open a second one);
          // the confirm button takes it once the check has passed.
          event.preventDefault()
          if (cancelRef.current) cancelRef.current.focus()
        }
      },
      el(
        DialogHeader,
        {},
        el(DialogTitle, {}, data && data.bulk ? 'Redeploy ' + name : kindLabel + ': ' + name),
        el(DialogDescription, {}, description)
      ),
      body,
      sendError ? failure(sendError) : null,
      el(
        DialogFooter,
        {},
        el(Button, { disabled: sendState === 'sending', onClick: close, ref: cancelRef, variant: 'ghost' }, 'Cancel'),
        checkFailed ? el(Button, { onClick: () => void plan.refetch(), variant: 'secondary' }, 'Check again') : null,
        el(
          Button,
          {
            'aria-disabled': !ready || busy,
            disabled: !ready || busy,
            onClick: () => void send(),
            ref: confirmRef,
            title: ready ? undefined : checkFailed ? 'The check failed; check again first.' : 'Still checking.',
            variant: pending.kind === 'stop' || pending.kind === 'cancel' ? 'destructive' : 'default'
          },
          sendState === 'sending' ? el(Codicon, { name: 'loading', size: '0.8rem', spinning: true }) : sendState === 'sent' ? el(Codicon, { name: 'check', size: '0.8rem' }) : null,
          confirmLabel
        )
      )
    )
  )
}

function paramText(params) {
  const entries = Object.entries(params || {})
  return entries.length ? '?' + entries.map(([k, v]) => k + '=' + v).join('&') : ''
}

function TrackedAction({ item }) {
  const fast = !item.done
  const history = useHistory(item.deploymentUuid ? item.resourceUuid : null, fast)
  const row = ((history.data && history.data.deployments) || []).find(d => d.uuid === item.deploymentUuid)
  const status = row ? row.status : item.deploymentUuid ? 'queued' : 'sent'
  useEffect(() => {
    if (row && TERMINAL.has(row.status) && !item.done) {
      $tracked.set($tracked.get().map(t => (t.id === item.id ? { ...t, done: true, status: row.status } : t)))
      log('deployment ended', { uuid: row.uuid, status: row.status })
      // M5 follow-through: a deploy he started that fails opens its diagnosis without a click.
      if (row.status === 'failed' && item.mine) $diagnose.set({ ...row, resource_name: item.resourceName, resource_uuid: item.resourceUuid, at: Date.now(), followed: true })
    }
  }, [row && row.status])
  const tone = item.deploymentUuid ? deploymentTone(status) : 'good'
  return el(
    'div',
    { style: { ...S.row(8), ...S.box, padding: '6px 10px' } },
    el(StatusDot, { tone }),
    el(
      'div',
      { style: { ...S.col(0), flex: 1 } },
      el('span', { style: S.strong }, item.label + ' ' + item.resourceName),
      el(
        Small,
        {},
        (item.deploymentUuid ? 'Deployment ' + item.deploymentUuid + ': ' + status.replace('_', ' ') : item.message || 'Sent') +
          (row && row.finished_at ? ', ' + duration(row.created_at, row.finished_at) : '') +
          ' (sent ' + ago(item.sentAt) + ')'
      )
    ),
    row && row.url ? el(ExternalLink, { href: row.url }, el(Small, { style: { color: T.accent } }, 'Open in Coolify')) : null,
    !item.done ? el(Codicon, { name: 'loading', size: '0.8rem', spinning: true, style: { color: T.tertiary } }) : null,
    el(IconButton, { icon: 'close', label: 'Dismiss', onClick: () => $tracked.set($tracked.get().filter(t => t.id !== item.id)) })
  )
}

function ActionTracker() {
  const tracked = useValue($tracked)
  if (!tracked.length) return null
  return el('div', { style: S.col(6) }, ...tracked.map(item => el(TrackedAction, { item, key: item.id })))
}

// ── cockpit chat: a normal Hermes session beside the page ───────────────────────────────────────

const sleep = ms => new Promise(resolve => window.setTimeout(resolve, ms))

function readAtom(name) {
  try {
    const a = host.state && host.state[name]
    return a && typeof a.get === 'function' ? a.get() : undefined
  } catch {
    return undefined
  }
}

async function fillDraft(address, text) {
  const composer = host.composer
  if (!composer || typeof composer.setDraft !== 'function') return false
  for (let attempt = 1; attempt <= 8; attempt += 1) {
    try {
      if (await composer.setDraft(address, text)) {
        log('draft filled', { address, attempt })
        return true
      }
    } catch (error) {
      log('setDraft failed', error && error.message)
    }
    await sleep(400)
  }
  return false
}

async function createSession(profile) {
  const created = await host.request('session.create', { ...(profile ? { profile } : {}), title: SESSION_TITLE, follow_profile_config: true })
  const runtime = created && created.session_id
  const stored = created && created.stored_session_id
  if (!stored) throw new Error('session.create returned no stored_session_id')
  // session.create is lazy; naming it materializes the stored row so it can be opened.
  if (runtime) await host.request('session.title', { session_id: runtime, title: SESSION_TITLE })
  log('cockpit session created', { stored })
  return stored
}

// Opens (or reuses) the "Cockpit" chat as a tab beside the page and drafts `text` into it.
// Never submits. Falls back to a plain new chat with the draft when session control fails.
async function openCockpitChat(text) {
  const profile = readAtom('profile') || readAtom('focusedSessionProfile') || ''
  const storageKey = 'session:' + profile
  const storage = pluginCtx && pluginCtx.storage
  let stored = storage ? storage.get(storageKey, null) : null
  const open = async id => host.openSession(id, { profile: profile || undefined, intent: 'tab', awaitHydration: true, tabTitle: SESSION_TITLE })
  try {
    if (typeof host.openSession !== 'function' || typeof host.request !== 'function') throw new Error('no session control in this Desktop build')
    if (stored) {
      try {
        await open(stored)
      } catch (error) {
        log('stored cockpit session did not open, creating a new one', error && error.message)
        stored = null
      }
    }
    if (!stored) {
      stored = await createSession(profile)
      if (storage) storage.set(storageKey, stored)
      await open(stored)
    }
    if (await fillDraft(stored, text)) return { mode: 'tab' }
    throw new Error('the chat tab did not accept the draft')
  } catch (error) {
    log('session control failed, falling back to a new chat', error && error.message)
    if (typeof host.newChat === 'function') host.newChat(profile || null)
    await sleep(500)
    const ok = (await fillDraft('new', text)) || (await fillDraft(null, text))
    if (!ok && typeof host.notify === 'function') {
      host.notify({ kind: 'warning', message: 'Opened a new chat; paste this question: ' + text })
    }
    return { mode: 'new-chat', ok }
  }
}

function addChip(chip) {
  const list = $chips.get().filter(c => c.ref !== chip.ref)
  $chips.set([chip].concat(list).slice(0, 6))
}

function refFor(resource) {
  return /\s/.test(resource.name || '') ? resource.uuid : resource.name
}

function ask(kind, item) {
  let chip
  let question
  if (kind === 'deployment') {
    chip = { ref: '@coolify:deployment/' + item.uuid, label: (item.resource_name || 'deployment') + ' ' + (item.status || '') }
    question =
      item.status === 'failed'
        ? 'Why did this deployment of ' + (item.resource_name || 'the app') + ' fail? Use its state and tell me the likely cause and the fix.'
        : 'How is this deployment of ' + (item.resource_name || 'the app') + ' going?'
  } else if (kind === 'resource') {
    chip = { ref: '@coolify:' + refFor(item), label: item.name }
    question = 'What is the state of ' + item.name + ', and is anything wrong with it?'
  } else if (kind === 'server') {
    chip = { ref: '@coolify:server/' + item.uuid, label: 'server ' + item.name }
    question = 'Is server ' + item.name + ' healthy, or is it overloaded?'
  } else {
    chip = { ref: '@coolify:overview', label: 'overview' }
    question = 'Give me a short status of my Coolify instance.'
  }
  addChip(chip)
  return openCockpitChat(question + ' ' + chip.ref + ' ')
}

function AskButton({ kind, item, label }) {
  const [busy, setBusy] = useState(false)
  return el(
    Button,
    {
      disabled: busy,
      onClick: () => {
        setBusy(true)
        ask(kind, item)
          .catch(error => log('ask failed', error && error.message))
          .finally(() => setBusy(false))
      },
      size: 'xs',
      title: 'Open the Cockpit chat beside this page with this ' + kind + ' as context. Nothing is sent until you press send.',
      variant: 'ghost'
    },
    el(Codicon, { name: 'comment-discussion', size: '0.75rem' }),
    label || 'Ask'
  )
}

// ── widget frame and the not-available body ─────────────────────────────────────────────────────

function Card({ title, subtitle, right, children, editing, style }) {
  return el(
    'section',
    {
      style: {
        ...S.col(0),
        border: '1px solid ' + (editing ? tint(T.accent, 45) : T.stroke),
        borderRadius: 8,
        minHeight: 0,
        overflow: 'hidden',
        ...style
      }
    },
    el(
      'header',
      { style: { ...S.row(8), padding: '8px 12px', borderBottom: '1px solid ' + T.strokeSoft, minHeight: 36 } },
      el('div', { style: { ...S.col(0), flex: 1 } }, el('h2', { style: { ...S.strong, ...S.ellipsis, margin: 0, fontSize: '0.75rem' } }, title), subtitle ? el(Small, { style: S.ellipsis }, subtitle) : null),
      right || null
    ),
    el('div', { style: { ...S.col(8), padding: '10px 12px', minHeight: 0 } }, children)
  )
}

function NotAvailable({ row }) {
  return el(
    'div',
    { style: S.col(6) },
    el(Note, { tone: 'warn', icon: 'lock' }, 'Not available with this token setup: ' + plain(row.reason || 'the setup was not checked') + '.'),
    row.unlock ? el(Small, {}, 'To unlock it: ' + plain(row.unlock) + '.') : null
  )
}

function QueryError({ error, what }) {
  const hint = error && error.body && error.body.hint
  const notFound = error && error.status === 404 && !(error.body && error.body.error)
  return el(
    'div',
    { style: S.col(6) },
    el(Note, { tone: 'bad' }, (what ? what + ': ' : '') + (notFound ? 'the cockpit backend is not enabled on this profile (Hermes: plugins enable coolifybruecke).' : plain(error.message))),
    hint ? el(Small, {}, plain(hint)) : null
  )
}

function Loading({ rows }) {
  return el('div', { style: S.col(6) }, ...Array.from({ length: rows || 3 }, (_, i) => el(Skeleton, { key: i, style: { height: 16, width: 90 - i * 12 + '%' } })))
}

// ── connection & rights ──────────────────────────────────────────────────────────────────────────

const RIGHTS = ['read', 'read:sensitive', 'deploy', 'write', 'delete']

function TokenBlock({ token, showProjects }) {
  const held = new Set(token.rights || [])
  const sees = token.sees || {}
  const roleName = token.role === 'api' ? 'API token' : token.role === 'read_only' ? 'Read-only token' : 'Token'
  const tone = token.status === 'ok' ? 'good' : token.status === 'unauthorized' ? 'bad' : 'warn'
  return el(
    'div',
    { style: { ...S.col(6), ...S.box } },
    el(
      'div',
      { style: S.row(8) },
      el(StatusDot, { tone }),
      el('span', { style: S.strong }, roleName),
      el(Small, { style: S.mono }, token.setting + ' · sha256 ' + token.fingerprint),
      el('span', { style: { flex: 1 } }),
      el(Badge, { variant: token.scope === 'scoped' ? 'default' : 'muted' }, token.scope || token.status)
    ),
    token.status !== 'ok' ? el(Note, { tone: 'bad' }, token.error || 'Coolify refused this token (' + token.status + ').') : null,
    el(
      'div',
      { style: { ...S.row(4), flexWrap: 'wrap' } },
      ...RIGHTS.map(right =>
        el(Badge, { key: right, variant: held.has(right) ? (right === 'write' || right === 'delete' ? 'warn' : 'success') : 'outline', style: held.has(right) ? undefined : { textDecoration: 'line-through', opacity: 0.7 } }, right)
      )
    ),
    el(
      Small,
      {},
      'Sees ' +
        [
          sees.projects != null ? sees.projects + ' projects' : null,
          sees.applications != null ? sees.applications + ' apps' : null,
          sees.services != null ? sees.services + ' services' : null,
          sees.databases != null ? sees.databases + ' databases' : null,
          typeof sees.servers === 'number' ? sees.servers + ' servers' : sees.servers ? 'no servers (' + String(sees.servers).replace(/^refused: /, '') + ')' : null
        ]
          .filter(Boolean)
          .join(', ')
    ),
    showProjects && Array.isArray(token.projects) && token.projects.length
      ? el(Small, { style: { color: T.secondary } }, token.projects.join(' · '))
      : null
  )
}

function featureName(text) {
  const s = String(text || '')
  const i = s.indexOf(': ')
  return i > 0 ? { id: s.slice(0, i), label: s.slice(i + 2) } : { id: s, label: s }
}

// The one line a saved setup shows (capabilities.result_line): scope, rights, what it reaches.
// A limited token offers the read-only token; write or delete adds one amber chip.
function SetupResult({ setup, compact }) {
  const profile = useProfile()
  if (!setup || !setup.line) return null
  const tone = setup.tone === 'bad' ? T.red : setup.tone === 'warn' ? T.yellow : T.green
  return el(
    'div',
    { 'data-cc-setup-result': setup.tone, style: { ...S.row(8), flexWrap: 'wrap' } },
    el(StatusDot, { tone: setup.tone === 'bad' ? 'bad' : setup.tone === 'warn' ? 'warn' : 'good' }),
    el('span', { style: { ...S.strong, color: compact ? T.primary : tone === T.green ? T.primary : tone } }, setup.line),
    setup.chip ? el('span', { title: setup.chip_detail || '' }, el(Badge, { variant: 'warn' }, setup.chip)) : null,
    setup.add_read_token
      ? el(Button, { onClick: () => host.navigate(settingsHref(profile)), size: 'xs', variant: 'outline' }, el(Codicon, { name: 'add', size: '0.75rem' }), 'Add a read-only token')
      : null
  )
}

function ProtectedLine({ protectedEnvs }) {
  if (!protectedEnvs) return null
  const names = protectedEnvs.environments || []
  return el(
    Small,
    {},
    el(Codicon, { name: 'lock', size: '0.7rem' }), ' ',
    names.length ? 'Protected: ' + names.join(', ') + '. ' : 'No protected environments. ',
    protectedEnvs.agent_allowed ? 'The agent may act there; each action asks you.' : 'The agent may not act there; your own clicks work.'
  )
}

function ConnectionWidget({ widget }) {
  const caps = useCapabilities()
  const props = widget.props || {}
  if (caps.isLoading) return el(Loading, { rows: 4 })
  if (caps.error) return el(QueryError, { error: caps.error, what: 'Capabilities' })
  const data = caps.data || {}
  const inst = data.instance || {}
  return el(
    'div',
    { style: S.col(10) },
    el(SetupResult, { setup: data.setup }),
    el(ProtectedLine, { protectedEnvs: data.protected }),
    el(
      'div',
      { style: S.row(8) },
      el(StatusDot, { tone: inst.reachable ? 'good' : 'bad' }),
      el(ExternalLink, { href: inst.url }, el('span', { style: S.strong }, hostOf(inst.url))),
      el(Small, {}, 'Coolify ' + (inst.coolify_version || '?')),
      inst.update_available ? el(Badge, { variant: 'warn' }, 'update ' + inst.latest_version) : null,
      el('span', { style: { flex: 1 } }),
      el(Small, {}, 'MCP ' + (inst.mcp_enabled ? 'on' : 'off') + (inst.metrics_enabled_servers ? ', metrics ' + inst.metrics_enabled_servers : ''))
    ),
    ...(data.tokens || []).map(token => el(TokenBlock, { key: token.role + token.fingerprint, token, showProjects: props.show_projects })),
    el(
      'div',
      { style: { display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(220px, 1fr))', gap: 10 } },
      el(
        'div',
        { style: S.col(4) },
        el('span', { style: S.label }, 'Available with this setup'),
        el(
          'div',
          { style: { ...S.row(4), flexWrap: 'wrap' } },
          ...(data.available || []).map(text => {
            const f = featureName(text)
            const via = f.label.match(/^(.*) \((via the [a-z-]+ token)\)$/)
            return el(
              'span',
              { key: f.id, title: f.label, style: { ...S.row(4), borderRadius: 4, padding: '2px 6px', background: tint(T.green, 10), color: T.primary, fontSize: '0.6875rem' } },
              el(Codicon, { name: 'check', size: '0.7rem', style: { color: T.green } }),
              (via ? via[1] : f.label).replace(/: .*$/, '')
            )
          })
        )
      ),
      el(
        'div',
        { style: S.col(4) },
        el('span', { style: S.label }, 'Not available'),
        (data.not_available || []).length === 0 ? el(Small, {}, 'Nothing: this setup unlocks every feature.') : null,
        ...(data.not_available || []).map(row => {
          const f = featureName(row.feature)
          return el(
            'div',
            { key: f.id, style: S.col(2) },
            el('div', { style: S.row(6, { alignItems: 'flex-start' }) }, el(Codicon, { name: 'circle-slash', size: '0.75rem', style: { color: T.tertiary, marginTop: 2 } }), el('span', { style: S.text }, f.label)),
            el(Small, { style: { paddingLeft: 18 } }, plain(row.reason) + (row.unlock ? '. Unlock: ' + plain(row.unlock) + '.' : ''))
          )
        })
      )
    ),
    props.show_warnings !== false && (data.warnings || []).length
      ? el('div', { style: S.col(4) }, ...data.warnings.map((w, i) => el(Note, { key: i, tone: 'warn' }, plain(w))))
      : null,
    data.hint ? el(Note, { tone: 'warn' }, data.hint) : null,
    el(Small, { style: { color: T.quaternary } }, 'Checked ' + ago(data.checked_at) + '. ' + TOKEN_HINT)
  )
}

// ── 0.1.0 shared: the attention data (fleet.py) ─────────────────────────────────────────────────
//
// The backend's Attention list is the only place a count comes from. Every block below reads
// snap.attention, snap.summary and snap.fleet; nothing counts on its own.

const $focus = atom(null) // the attention signal the summary chips filter the Apps list by
const $openApp = atom(null) // { uuid, at }: the app row whose history is open (Diagnose, follow-through)

function signalCount(data, signal) {
  const row = ((data && data.summary) || []).find(s => s.signal === signal)
  return row ? row.count : 0
}

function attentionFor(data, uuid) {
  return ((data && data.attention) || []).filter(a => a.resource_uuid === uuid)
}

function protectedSet(caps) {
  const envs = ((caps && caps.data && caps.data.protected) || {}).environments
  return new Set((envs || ['production']).map(e => String(e).toLowerCase()))
}

function isProtected(caps, environment) {
  return protectedSet(caps).has(String(environment || '').toLowerCase())
}

function joinResources(deployments, resources) {
  const byUuid = new Map((resources || []).map(r => [r.uuid, r]))
  return (deployments || []).map(d => ({ ...d, resource: byUuid.get(d.resource_uuid) || null }))
}

function deploymentFilter(props) {
  return d =>
    (!props.project || (d.resource && matches(d.resource.project, props.project))) &&
    (!props.environment || String((d.resource && d.resource.environment) || d.environment || '').toLowerCase() === String(props.environment).toLowerCase())
}

const SIGNAL_TONE = { bad: 'bad', warn: 'warn', info: 'muted' }
const BADGE_VARIANT = { bad: 'destructive', warn: 'warn', info: 'outline' }

// ── summary ──────────────────────────────────────────────────────────────────────────────────────

function lastDeployAgo(data) {
  const done = ((data && data.latest_deployments) || []).find(d => d.status === 'finished')
  return done ? ago(done.finished_at || done.created_at) : null
}

function SummaryWidget({ widget }) {
  const snap = useSnapshot()
  const rights = useRights()
  const focus = useValue($focus)
  const props = widget.props || {}
  if (snap.isLoading) return el(Loading, { rows: 1 })
  if (snap.error) return el(QueryError, { error: snap.error, what: 'Snapshot' })
  const data = snap.data || {}
  const only = new Set(props.signals || [])
  const rows = (data.summary || []).filter(s => !only.size || only.has(s.signal))
  const running = ((data.deployments || {}).running || []).length
  const apps = (data.resources || []).filter(r => r.kind === 'application').length
  const changed = (data.attention || []).filter(a => a.signal === 'env_changed' && a.resource_uuid)
  const resources = new Map((data.resources || []).map(r => [r.uuid, r]))
  const redeployable = changed.map(a => resources.get(a.resource_uuid)).filter(r => r && rights.allows('deploy', r))
  if (!rows.length && !running) {
    const last = lastDeployAgo(data)
    return el(
      'div',
      { 'data-cc-summary': 'clear', style: { ...S.row(8), flexWrap: 'wrap' } },
      el(StatusDot, { tone: 'good' }),
      el('span', { style: { ...S.text, color: T.primary } }, apps + ' apps healthy' + (last ? ', last deploy ' + last : ''))
    )
  }
  return el(
    'div',
    { 'data-cc-summary': 'attention', style: { ...S.row(8), flexWrap: 'wrap' } },
    ...rows.map(s =>
      el(
        Button,
        {
          key: s.signal,
          'aria-pressed': focus === s.signal,
          onClick: () => $focus.set(focus === s.signal ? null : s.signal),
          size: 'xs',
          title: focus === s.signal ? 'Show all apps' : 'Show only these in Apps',
          variant: focus === s.signal ? 'default' : 'chip'
        },
        el(StatusDot, { tone: SIGNAL_TONE[s.severity] || 'muted' }),
        el('b', {}, String(s.count)),
        ' ' + s.label
      )
    ),
    running ? el('span', { style: { ...S.row(4), ...S.text } }, el(StatusDot, { tone: 'warn' }), el('b', {}, String(running)), ' deploying') : null,
    focus ? el(Button, { onClick: () => $focus.set(null), size: 'xs', variant: 'ghost' }, 'Show all') : null,
    el('span', { style: { flex: 1 } }),
    props.redeploy_changed !== false && redeployable.length
      ? el(
          Button,
          {
            onClick: () =>
              requestAction('deploy', redeployable[0].uuid, {
                bulk: redeployable.map(r => ({ uuid: r.uuid, name: r.name, environment: r.environment })),
                resourceName: redeployable.length + (redeployable.length === 1 ? ' app' : ' apps') + ' with changed env'
              }),
            size: 'xs',
            variant: 'secondary'
          },
          'Redeploy ' + redeployable.length + (redeployable.length === 1 ? ' app' : ' apps') + ' with changed env'
        )
      : null
  )
}

// ── latest deploys ───────────────────────────────────────────────────────────────────────────────

function lastGoodText(row) {
  const good = row && row.last_good
  if (!good || !good.commit || good.commit === 'HEAD') return null
  return shortSha(good.commit) + ', last good' + (good.finished_at ? ' ' + ago(good.finished_at) : '')
}

function RollbackButton({ resource, row, rights }) {
  const good = row && row.last_good
  if (!resource || !good || !good.commit || good.commit === 'HEAD' || !rights.allows('rollback', resource)) return null
  return el(ActionButton, {
    kind: 'rollback',
    label: 'Roll back to ' + shortSha(good.commit),
    params: { commit: good.commit },
    resource,
    rights,
    target: resource.uuid,
    variant: 'secondary'
  })
}

function LatestRow({ d, rights, fleet, open, onDiagnose }) {
  const r = d.resource
  const row = fleet[d.resource_uuid]
  const failedNow = d.status === 'failed' && !d.superseded
  const running = !TERMINAL.has(String(d.status || ''))
  const word = d.superseded ? 'failed, fixed since' : String(d.status || '').replace('_', ' ')
  const canCancel = running && r && rights.allows('cancel', r)
  return el(
    'div',
    { 'data-cc-deploy': d.uuid, style: { ...S.col(6), padding: '6px 0', ...S.hair, opacity: d.superseded ? 0.55 : 1 } },
    el(
      'div',
      { style: { ...S.row(8), flexWrap: 'wrap' } },
      el(StatusDot, { tone: deploymentTone(d.status), title: word }),
      el('span', { style: { ...S.strong, ...S.ellipsis, maxWidth: 220 } }, d.resource_name || 'unknown'),
      el(Badge, { size: 'xs', variant: 'outline' }, d.environment || (r && r.environment) || '?'),
      el(Small, { style: { color: failedNow ? T.red : T.tertiary } }, word),
      el(Small, { style: { ...S.ellipsis, flex: 1, minWidth: 120 } }, firstLine(d.commit_message) || shortSha(d.commit) || ''),
      d.trigger ? el(Small, {}, d.trigger) : null,
      d.finished_at ? el(Small, { style: S.mono }, duration(d.created_at, d.finished_at)) : null,
      el(Small, { style: { ...S.mono, minWidth: 64, textAlign: 'right' } }, ago(d.created_at)),
      canCancel ? el(ActionButton, { kind: 'cancel', target: d.uuid, resource: r, deployment: d.uuid, rights, label: 'Cancel', variant: 'outline' }) : null,
      failedNow ? el(RollbackButton, { resource: r, row, rights }) : null,
      failedNow ? el(Button, { 'aria-expanded': open, onClick: onDiagnose, size: 'xs', variant: open ? 'default' : 'secondary' }, el(Codicon, { name: 'debug', size: '0.75rem' }), 'Diagnose') : null,
      d.url ? el(ExternalLink, { href: d.url }, el(Codicon, { name: 'link-external', size: '0.7rem' })) : null
    ),
    open ? el('div', { style: { paddingLeft: 16 } }, el(DiagnosisPanel, { deployment: d, pasteBox: true })) : null
  )
}

function DeploymentsWidget({ widget }) {
  const snap = useSnapshot()
  const rights = useRights()
  const selected = useValue($diagnose)
  const props = widget.props || {}
  if (snap.isLoading) return el(Loading, { rows: 4 })
  if (snap.error) return el(QueryError, { error: snap.error, what: 'Snapshot' })
  const data = snap.data || {}
  const keep = deploymentFilter(props)
  const show = props.show || 'all'
  const fleet = data.fleet || {}
  let rows = joinResources(data.latest_deployments || [], data.resources).filter(keep)
  if (show === 'failed') rows = rows.filter(d => d.status === 'failed')
  if (show === 'running') rows = rows.filter(d => !TERMINAL.has(String(d.status || '')))
  rows = rows.slice(0, props.limit || 8)
  if (data.latest_deployments == null) {
    return el(Small, {}, ((data.limits || []).find(t => /MCP/.test(t)) || 'No deployment list in this snapshot.'))
  }
  if (!rows.length) return el(Small, {}, 'No deploys to show.')
  return el(
    'div',
    { style: { ...S.col(0), maxHeight: widget.size === 'l' ? 560 : 380, overflowY: 'auto' } },
    ...rows.map(d =>
      el(LatestRow, {
        key: d.uuid,
        d,
        fleet,
        open: Boolean(selected && selected.uuid === d.uuid),
        onDiagnose: () => $diagnose.set(selected && selected.uuid === d.uuid ? null : { ...d, at: Date.now() }),
        rights
      })
    )
  )
}

// ── apps ─────────────────────────────────────────────────────────────────────────────────────────

function statusMatches(status, filter, attention) {
  const s = String(status || '')
  if (!filter || filter === 'any') return true
  if (filter === 'running') return s.startsWith('running')
  if (filter === 'unhealthy') return s.includes('unhealthy') && !s.startsWith('exited')
  if (filter === 'stopped') return s.startsWith('exited') || s.startsWith('stopped')
  if (filter === 'attention') return attention.length > 0
  return true
}

function HistoryRows({ resource }) {
  const history = useHistory(resource.uuid, false)
  if (history.isLoading) return el(Loading, { rows: 2 })
  if (history.error) return el(QueryError, { error: history.error })
  const rows = (history.data && history.data.deployments) || []
  if (!rows.length) return el(Small, {}, history.data && history.data.note ? history.data.note : 'No deployments recorded.')
  return el(
    'div',
    { style: S.col(4) },
    ...rows.slice(0, 6).map(d =>
      el(
        'div',
        { key: d.uuid, style: S.row(8) },
        el(StatusDot, { tone: deploymentTone(d.status) }),
        el(Small, { style: { ...S.mono, color: T.secondary } }, shortSha(d.commit) || '?'),
        el(Small, { style: { ...S.ellipsis, flex: 1 } }, firstLine(d.commit_message) || d.status),
        el(Small, {}, ago(d.created_at) + (d.finished_at ? ', ' + duration(d.created_at, d.finished_at) : '')),
        d.status === 'failed' ? el(Button, { onClick: () => $diagnose.set({ ...d, resource_name: resource.name, at: Date.now() }), size: 'xs', variant: 'ghost' }, 'Diagnose') : null,
        d.url ? el(ExternalLink, { href: d.url }, el(Codicon, { name: 'link-external', size: '0.7rem' })) : null
      )
    )
  )
}

function SignalBadges({ items }) {
  return el(
    'span',
    { style: { ...S.row(4), flexWrap: 'wrap' } },
    ...items.map(a => el('span', { key: a.signal, 'data-cc-badge': a.signal }, el(Badge, { size: 'xs', title: a.title, variant: BADGE_VARIANT[a.severity] || 'outline' }, a.badge || a.label || a.signal)))
  )
}

function AppRow({ resource, row, items, rowActions, rights, locked, showEnvKeys }) {
  const open = useValue($openApp)
  const isOpen = Boolean(open && open.uuid === resource.uuid)
  const failing = row && row.state === 'failing'
  const running = row && row.state === 'deploying'
  const actions = (rowActions || []).filter(kind => rights.allows(kind, resource))
  const deployed = row && row.last_good // what runs: the last finished deploy (none known: no commit shown)
  return el(
    'div',
    { 'data-cc-app': resource.uuid, style: { ...S.col(6), padding: '6px 0', ...S.hair } },
    el(
      'div',
      { style: { ...S.row(8), flexWrap: 'wrap' } },
      el(StatusDot, { tone: failing ? 'bad' : running ? 'warn' : resourceTone(resource.status), title: (row && row.state) || resource.status }),
      el(
        'button',
        {
          'aria-expanded': isOpen,
          onClick: () => $openApp.set(isOpen ? null : { uuid: resource.uuid, at: Date.now() }),
          style: { ...S.strong, ...S.ellipsis, maxWidth: 260, background: 'none', border: 'none', padding: 0, cursor: 'pointer', textAlign: 'left' },
          title: resource.kind === 'application' ? 'Deployment history' : resource.kind,
          type: 'button'
        },
        resource.name
      ),
      resource.kind !== 'application' ? el(Badge, { size: 'xs', variant: 'outline' }, resource.kind) : null,
      locked ? el(Codicon, { name: 'lock', size: '0.7rem', title: 'Protected: the agent may not act here', style: { color: T.tertiary } }) : null,
      el(SignalBadges, { items }),
      el('span', { style: { flex: 1 } }),
      deployed && deployed.commit && deployed.commit !== 'HEAD' ? el(Small, { style: S.mono, title: 'Deployed commit' }, shortSha(deployed.commit)) : null,
      deployed ? el(Small, { style: { ...S.mono, minWidth: 64, textAlign: 'right' } }, ago(deployed.finished_at || deployed.created_at)) : null,
      failing ? el(RollbackButton, { resource, row, rights }) : null,
      ...actions.map(kind => el(ActionButton, { key: kind, kind, target: resource.uuid, resource, rights })),
      el(AskButton, { kind: 'resource', item: resource })
    ),
    isOpen && resource.kind === 'application' ? el('div', { style: { paddingLeft: 16 } }, el(HistoryRows, { resource })) : null,
    isOpen && showEnvKeys ? el(Small, { style: { paddingLeft: 16 } }, 'Env key names: ask the agent ("which env keys does ' + resource.name + ' have?").') : null
  )
}

// Apps of one repository in two environments (staging and production, say): a pipeline pair line.
function pairsOf(apps) {
  const byRepo = new Map()
  for (const r of apps) {
    if (r.kind !== 'application' || !r.git_repository) continue
    const key = String(r.git_repository).toLowerCase()
    byRepo.set(key, (byRepo.get(key) || []).concat([r]))
  }
  // A pair is one app per environment (a monorepo with two apps in one environment is not a
  // pipeline), and both sides with a known deployed commit (no "?" on the page).
  return Array.from(byRepo.values()).filter(list => {
    const envs = list.map(r => r.environment)
    return envs.length >= 2 && new Set(envs).size === envs.length
  })
}

function PairLine({ apps, fleet, rights, caps }) {
  const order = apps.slice().sort((a, b) => isProtected(caps, a.environment) - isProtected(caps, b.environment) || String(a.environment).localeCompare(String(b.environment)))
  return el(
    'div',
    { 'data-cc-pair': order.map(r => r.uuid).join(','), style: { ...S.row(10), flexWrap: 'wrap', padding: '4px 0', ...S.hair } },
    el('span', { style: { ...S.strong, minWidth: 160 } }, String(order[0].git_repository).replace(/^.*[/:]([^/]+\/[^/]+?)(\.git)?$/, '$1')),
    ...order.map((r, i) => {
      const row = fleet[r.uuid] || {}
      const d = row.last_good || row.latest
      return el(
        'span',
        { key: r.uuid, style: { ...S.row(6) } },
        i ? el(Codicon, { name: 'arrow-right', size: '0.7rem', style: { color: T.tertiary } }) : null,
        el(StatusDot, { tone: row.state === 'failing' ? 'bad' : row.state === 'deploying' ? 'warn' : resourceTone(r.status) }),
        el(Small, {}, r.environment + ' ' + (d && d.commit && d.commit !== 'HEAD' ? shortSha(d.commit) : '?') + (d ? ' ' + ago(d.finished_at || d.created_at) : '')),
        rights.allows('deploy', r) ? el(ActionButton, { kind: 'deploy', target: r.uuid, resource: r, rights, variant: 'ghost' }) : null
      )
    })
  )
}

function ResourcesWidget({ widget }) {
  const snap = useSnapshot()
  const caps = useCapabilities()
  const rights = useRights()
  const focus = useValue($focus)
  const props = widget.props || {}
  const [query, setQuery] = useState('')
  const [env, setEnv] = useState('')
  if (snap.isLoading) return el(Loading, { rows: 5 })
  if (snap.error) return el(QueryError, { error: snap.error, what: 'Snapshot' })
  const data = snap.data || {}
  const fleet = data.fleet || {}
  const all = (data.resources || []).filter(
    r =>
      matches(r.project, props.project) &&
      (!props.environment || String(r.environment || '').toLowerCase() === String(props.environment).toLowerCase()) &&
      (!props.kinds || !props.kinds.length || props.kinds.includes(r.kind)) &&
      matches(r.name, props.name_contains)
  )
  const items = new Map(all.map(r => [r.uuid, attentionFor(data, r.uuid)]))
  const envs = Array.from(new Set(all.map(r => r.environment).filter(Boolean))).sort((a, b) => isProtected(caps, b) - isProtected(caps, a) || a.localeCompare(b))
  const shown = all.filter(
    r =>
      statusMatches(r.status, props.status, items.get(r.uuid)) &&
      (!focus || items.get(r.uuid).some(a => a.signal === focus)) &&
      (!env || r.environment === env) &&
      (!query || matches(r.name + ' ' + r.project + ' ' + (r.fqdn || ''), query))
  )
  const rank = r => (items.get(r.uuid).some(a => a.severity === 'bad') ? 0 : items.get(r.uuid).length ? 1 : 2)
  const sorted = shown.slice().sort((a, b) => rank(a) - rank(b) || String(a.name).localeCompare(String(b.name)))
  const apps = sorted.filter(r => r.kind === 'application')
  const others = sorted.filter(r => r.kind !== 'application')
  const groups = (props.environment ? [props.environment] : envs).map(e => ({ env: e, list: apps.filter(r => r.environment === e) })).filter(g => g.list.length)
  const known = r => Boolean(fleet[r.uuid] && fleet[r.uuid].last_good && fleet[r.uuid].last_good.commit && fleet[r.uuid].last_good.commit !== 'HEAD')
  const pairs = props.pairs !== false && !props.environment && !focus ? pairsOf(all).filter(list => list.every(known)) : []
  const row = r => el(AppRow, { key: r.uuid, resource: r, row: fleet[r.uuid], items: items.get(r.uuid), rowActions: props.row_actions, rights, locked: isProtected(caps, r.environment), showEnvKeys: props.show_env_keys })
  return el(
    'div',
    { style: S.col(8) },
    el(
      'div',
      { style: { ...S.row(8), flexWrap: 'wrap' } },
      el(Input, { 'aria-label': 'Filter apps', onChange: e => setQuery(e.target.value), placeholder: 'Filter by name or project', style: { width: 200 }, value: query }),
      !props.environment && envs.length > 1
        ? el(SegmentedControl, { onChange: id => setEnv(id === '*' ? '' : id), options: [{ id: '*', label: 'All' }].concat(envs.map(e => ({ id: e, label: e }))), sizing: 'content', value: env || '*' })
        : null,
      focus ? el(Button, { onClick: () => $focus.set(null), size: 'xs', variant: 'ghost' }, el(Codicon, { name: 'close', size: '0.7rem' }), 'only ' + focus.replace('_', ' ')) : null,
      el('span', { style: { flex: 1 } }),
      el(Small, {}, shown.length + ' of ' + all.length)
    ),
    (props.row_actions || []).length && !rights.canAct && rights.loaded ? el(Small, {}, 'No action buttons: ' + (rights.reason || 'needs `deploy`') + '.') : null,
    pairs.length && !query && !env
      ? el('div', { style: S.col(0) }, el('span', { style: { ...S.label, padding: '4px 0' } }, 'Pipelines'), ...pairs.map(list => el(PairLine, { key: list.map(r => r.uuid).join(), apps: list, caps, fleet, rights })))
      : null,
    shown.length
      ? el(
          'div',
          { style: { ...S.col(0), maxHeight: widget.size === 'l' ? 640 : 380, overflowY: 'auto' } },
          ...groups.flatMap(g => [
            el('div', { key: 'g-' + g.env, style: { ...S.label, padding: '8px 0 2px' } }, g.env + (isProtected(caps, g.env) ? ' · protected' : '') + ' · ' + g.list.length),
            ...g.list.map(row)
          ]),
          others.length ? el('div', { key: 'g-other', style: { ...S.label, padding: '8px 0 2px' } }, 'Services and databases · ' + others.length) : null,
          ...others.map(row)
        )
      : el(EmptyState, { title: 'Nothing matches', description: all.length ? 'Clear the filters to see all ' + all.length + '.' : 'This widget filters on ' + [props.project, props.environment].filter(Boolean).join(' / ') + '.' })
  )
}

// ── servers (A15: his 0.0.11 cards, with range, deploy ticks, hover, peak, disk estimate) ────────

const RANGES = [
  { id: '1h', label: '1 h', minutes: 60 },
  { id: '6h', label: '6 h', minutes: 360 },
  { id: '24h', label: '24 h', minutes: 1440 },
  { id: '7d', label: '7 d', minutes: 10080 }
]
const $range = atom(null) // the range the page's switch picked; null: the widget's prop

function useServerRange(rangeId) {
  const profile = useProfile()
  const ready = setupState(useSetup()) === 'ready'
  return useQuery({
    enabled: ready,
    queryKey: qk(profile, 'server-metrics', rangeId),
    queryFn: () => rest('/servers/metrics?range=' + rangeId),
    refetchInterval: rangeId === '1h' ? 60000 : 300000,
    staleTime: rangeId === '1h' ? 30000 : 240000,
    retry: retryOnce
  })
}

const METRICS = {
  cpu: { label: 'CPU', series: ['cpu_percent'], domain: () => [0, 100], fmt: v => pct(v), peak: true },
  ram: { label: 'RAM', series: ['memory_used_bytes'], domain: c => (c.memory_total_bytes ? [0, c.memory_total_bytes] : null), fmt: (v, _, c) => bytes(v) + ' of ' + bytes(c.memory_total_bytes) },
  disk: { label: 'Disk', series: ['disk_used_percent'], domain: () => [0, 100], fmt: (v, _, c) => pct(v) + ' of ' + bytes(c.disk_total_bytes), limit: true },
  load: { label: 'Load', series: ['load1'], domain: () => null, fmt: v => (typeof v === 'number' ? v.toFixed(2) : '?'), peak: true },
  network: { label: 'Network in / out', series: ['network_rx_bytes_per_sec', 'network_tx_bytes_per_sec'], domain: () => null, fmt: (v, w) => rate(v) + ' / ' + rate(w) }
}

function pointsOf(list) {
  return (list || []).filter(p => Array.isArray(p) && typeof p[1] === 'number' && Number.isFinite(p[1]))
}

function Chart({ spec, series, current, minutes, ticks, cursor, onCursor, limit }) {
  const lines = spec.series.map(name => pointsOf(series[name]))
  const first = lines[0]
  if (first.length < 2) return el(Small, {}, 'no history')
  const w = 300
  const h = 40
  const end = first[first.length - 1][0]
  const start = end - minutes * 60000
  const values = lines.flat().map(p => p[1])
  let [min, max] = spec.domain(current) || [0, Math.max(...values) * 1.15 || 1]
  if (max - min <= 0) max = min + 1
  const x = t => ((t - start) / (end - start)) * w
  const y = v => h - 2 - ((Math.min(max, Math.max(min, v)) - min) / (max - min)) * (h - 5)
  const hot = limit != null && first[first.length - 1][1] >= limit
  const color = hot ? T.yellow : T.accent
  const poly = pts => pts.map(p => x(p[0]).toFixed(1) + ',' + y(p[1]).toFixed(1)).join(' ')
  return el(
    'svg',
    {
      'aria-label': spec.label + ', last ' + RANGES.find(r => r.minutes === minutes).label,
      onMouseLeave: () => onCursor(null),
      onMouseMove: e => {
        const box = e.currentTarget.getBoundingClientRect()
        onCursor(start + Math.min(1, Math.max(0, (e.clientX - box.left) / box.width)) * (end - start))
      },
      preserveAspectRatio: 'none',
      role: 'img',
      style: { width: '100%', height: h, display: 'block', cursor: 'crosshair' },
      viewBox: '0 0 ' + w + ' ' + h
    },
    limit != null ? el('line', { x1: 0, x2: w, y1: y(limit), y2: y(limit), style: { stroke: T.yellow, strokeDasharray: '2 4', opacity: 0.7, vectorEffect: 'non-scaling-stroke' } }) : null,
    el('polygon', { points: '0,' + h + ' ' + poly(first) + ' ' + w + ',' + h, style: { fill: tint(color, 14), stroke: 'none' } }),
    ...lines.map((pts, k) =>
      pts.length > 1 ? el('polyline', { key: k, points: poly(pts), style: { fill: 'none', stroke: color, strokeWidth: 1.5, vectorEffect: 'non-scaling-stroke', strokeDasharray: k ? '3 3' : undefined, opacity: k ? 0.8 : 1 } }) : null
    ),
    ...ticks.filter(t => t.at >= start && t.at <= end).map(t =>
      el('line', { key: t.uuid, x1: x(t.at), x2: x(t.at), y1: 0, y2: 9, style: { stroke: T.primary, strokeWidth: 2, opacity: 0.75, vectorEffect: 'non-scaling-stroke' } }, el('title', {}, 'Deploy: ' + t.name + ', ' + ago(new Date(t.at).toISOString())))
    ),
    cursor != null ? el('line', { x1: x(cursor), x2: x(cursor), y1: 0, y2: h, style: { stroke: T.primary, opacity: 0.6, vectorEffect: 'non-scaling-stroke' } }) : null
  )
}

function valueAt(list, at) {
  const pts = pointsOf(list)
  if (!pts.length) return null
  if (at == null) return pts[pts.length - 1][1]
  let best = pts[0]
  for (const p of pts) if (Math.abs(p[0] - at) < Math.abs(best[0] - at)) best = p
  return best[1]
}

function ServerCard({ server, metrics, history, range, ranged, ticks, diskWarn }) {
  const [cursor, setCursor] = useState(null)
  const m = server.metrics
  const current = (m && m.current) || {}
  const extra = ranged && ranged.servers ? ranged.servers[server.uuid] : null
  const series = (range === '1h' ? m && m.series : extra && extra.series) || (m && m.series) || {}
  const minutes = RANGES.find(r => r.id === range).minutes
  const tone = !server.reachable ? 'bad' : !server.usable ? 'warn' : current.disk_used_percent >= diskWarn ? 'warn' : 'good'
  const full = extra && extra.disk_full
  const inRange = ticks.filter(t => t.at >= Date.now() - minutes * 60000).length
  return el(
    'div',
    { 'data-cc-server': server.uuid, style: { ...S.col(8), ...S.box } },
    el(
      'div',
      { style: { ...S.row(8), flexWrap: 'wrap' } },
      el(StatusDot, { tone }),
      el('span', { style: S.strong }, server.name),
      el(Small, {}, server.reachable ? (server.usable ? 'reachable' : 'reachable, not usable') : 'unreachable'),
      full ? el(Badge, { variant: 'warn', title: 'From the last 7 days of disk use; an estimate' }, 'disk full in about ' + Math.max(1, Math.round(full.days)) + ' d') : null,
      el(Small, {}, cursor != null ? ago(new Date(cursor).toISOString()) : inRange ? inRange + (inRange === 1 ? ' deploy' : ' deploys') + ' in this range' : ''),
      el('span', { style: { flex: 1 } }),
      el(AskButton, { kind: 'server', item: server })
    ),
    m
      ? el(
          'div',
          { style: { display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(150px, 1fr))', gap: 10 } },
          ...metrics.map(id => {
            const spec = METRICS[id]
            const now = spec.series.map(name => (cursor == null ? current[name] : valueAt(series[name], cursor)))
            const peak = spec.peak ? Math.max(...pointsOf(series[spec.series[0]]).map(p => p[1])) : null
            const text = now[0] == null ? '?' : spec.fmt(now[0], now[1], current)
            return el(
              'div',
              { key: id, style: S.col(2) },
              el('span', { style: S.label }, spec.label),
              el(
                'span',
                { style: { ...S.text, ...S.mono, color: T.primary, ...S.ellipsis }, title: text },
                text,
                peak != null && Number.isFinite(peak) && cursor == null ? el('span', { style: { color: T.tertiary } }, '  peak ' + spec.fmt(peak, null, current)) : null
              ),
              history ? el(Chart, { spec, series, current, minutes, ticks, cursor, onCursor: setCursor, limit: spec.limit ? diskWarn : null }) : null
            )
          })
        )
      : el(
          Small,
          {},
          server.metrics_enabled === false
            ? 'No metrics: Sentinel is off for this server (Coolify: Servers, ' + server.name + ', Sentinel).'
            : 'No metrics in this snapshot' + (server.metrics_error ? ': ' + server.metrics_error : '.')
        )
  )
}

function ServersWidget({ widget }) {
  const profile = useProfile()
  const snap = useSnapshot()
  const caps = useCapabilities()
  const picked = useValue($range)
  const props = widget.props || {}
  const range = picked || props.range || '1h'
  const ranged = useServerRange(range)
  if (snap.isLoading) return el(Loading, { rows: 4 })
  if (snap.error) return el(QueryError, { error: snap.error, what: 'Snapshot' })
  const wanted = (props.servers || []).map(s => s.toLowerCase())
  const servers = (snap.data.servers || []).filter(s => !wanted.length || wanted.includes(String(s.name).toLowerCase()))
  const metrics = props.metrics && props.metrics.length ? props.metrics : ['cpu', 'ram', 'disk', 'load', 'network']
  const missing = ((caps.data && caps.data.not_available) || []).find(row => /^servers?(_stats)?:/.test(row.feature || ''))
  const diskWarn = (snap.data.thresholds && snap.data.thresholds.disk_warn_percent) || 85
  if (!servers.length) {
    return el(
      'div',
      { style: S.col(6) },
      el(Small, {}, missing ? 'Needs a team-wide token.' : 'The reading token returned no servers.'),
      missing ? el(Button, { onClick: () => host.navigate(settingsHref(profile)), size: 'xs', variant: 'outline' }, 'Add a read-only token') : null
    )
  }
  const deploys = (snap.data.latest_deployments || []).concat(((snap.data.deployments || {}).running) || [])
  const ticksFor = server =>
    deploys.filter(d => d.server_name === server.name && d.created_at).map(d => ({ uuid: d.uuid, name: d.resource_name || 'deploy', at: Date.parse(d.created_at) }))
  return el(
    'div',
    { style: S.col(8) },
    el(
      'div',
      { style: { ...S.row(8), flexWrap: 'wrap' } },
      el(Small, {}, 'Hover a chart to read a moment; ticks are deploys.'),
      el('span', { style: { flex: 1 } }),
      ranged.isFetching && range !== '1h' ? el(Codicon, { name: 'refresh', size: '0.75rem', spinning: true }) : null,
      el(SegmentedControl, { onChange: id => $range.set(id), options: RANGES.map(r => ({ id: r.id, label: r.label })), sizing: 'content', value: range })
    ),
    ...servers.map(server =>
      el(ServerCard, { key: server.uuid, server, metrics, history: props.history !== false, range, ranged: ranged.data, ticks: ticksFor(server), diskWarn })
    )
  )
}

// ── quick actions ────────────────────────────────────────────────────────────────────────────────

function QuickActionsWidget({ widget }) {
  const snap = useSnapshot()
  const rights = useRights()
  const buttons = (widget.props && widget.props.buttons) || []
  if (!buttons.length) return el(EmptyState, { title: 'No pinned actions', description: 'Edit the layout to pin one, or ask the agent: "add a restart button for my staging app".' })
  if (rights.loaded && !rights.canAct) return el(Note, { tone: 'warn', icon: 'lock' }, rights.reason || 'Needs `deploy`.')
  const resources = (snap.data && snap.data.resources) || []
  return el(
    'div',
    { style: S.col(6) },
    ...buttons.map((button, i) => {
      const r = resources.find(x => x.uuid === button.target) || resources.find(x => String(x.name).toLowerCase() === String(button.target).toLowerCase())
      const spec = rights.specs.get(button.kind)
      const label = button.label || (spec ? spec.label : button.kind) + ' ' + button.target
      const allowed = r ? rights.allows(button.kind, r) : false
      return el(
        'div',
        { key: i, style: S.row(8) },
        r ? el(StatusDot, { tone: resourceTone(r.status), title: r.status }) : el(StatusDot, { tone: 'muted' }),
        el('div', { style: { ...S.col(0), flex: 1 } }, el('span', { style: { ...S.strong, ...S.ellipsis } }, label), el(Small, { style: S.ellipsis }, r ? r.project + ' / ' + r.environment + ' · ' + r.status : snap.isLoading ? 'loading' : 'not in the snapshot: ' + button.target)),
        allowed || (!r && !snap.isLoading)
          ? el(ActionButton, { kind: button.kind, target: r ? r.uuid : button.target, resource: r || { name: button.target }, params: button.params, rights, label: spec ? spec.label : button.kind, variant: 'default' })
          : el(Small, {}, rights.loaded ? 'outside the API token' : '')
      )
    })
  )
}

// ── diagnosis ────────────────────────────────────────────────────────────────────────────────────

function DiagnosisCard({ card, deployment }) {
  const evidence = new Set((card.evidence || []).map(e => e.line))
  const lines = card.excerpt && card.excerpt.length ? card.excerpt : card.evidence || []
  return el(
    'div',
    { style: { ...S.col(8), ...S.box } },
    el(
      'div',
      { style: { ...S.row(8), flexWrap: 'wrap' } },
      el('span', { style: S.strong }, card.title),
      el(Badge, { variant: card.class === 'unknown' ? 'muted' : 'destructive' }, card.class),
      el(Badge, { variant: card.confidence === 'high' ? 'success' : card.confidence === 'medium' ? 'warn' : 'muted' }, card.confidence + ' confidence'),
      el('span', { style: { flex: 1 } }),
      el(Small, {}, card.source === 'api' ? 'log read from Coolify' : 'pasted log')
    ),
    card.explain ? el('p', { style: { ...S.text, margin: 0 } }, card.explain) : null,
    el('div', { style: S.col(2) }, el('span', { style: S.label }, 'Likely fix'), el('p', { style: { ...S.text, color: T.primary, margin: 0 } }, card.likely_fix)),
    lines.length
      ? el(
          LogView,
          { style: { maxHeight: 220 } },
          ...lines.map(e =>
            el(
              'div',
              { key: e.line, style: evidence.has(e.line) ? { color: T.primary, background: tint(T.red, 12), margin: '0 -10px', padding: '0 10px' } : undefined },
              el('span', { style: { color: T.quaternary, userSelect: 'none' } }, String(e.line).padStart(4, ' ') + '  '),
              e.text
            )
          )
        )
      : null,
    el(
      'div',
      { style: S.row(8) },
      card.docs ? el(ExternalLink, { href: card.docs }, el(Small, { style: { color: T.accent } }, 'Coolify docs')) : null,
      card.redactions ? el(Small, {}, card.redactions + ' secret' + (card.redactions === 1 ? '' : 's') + ' masked') : null,
      el('span', { style: { flex: 1 } }),
      el(
        Button,
        {
          onClick: () =>
            openCockpitChat(
              'The cockpit diagnosed ' + (deployment ? 'the failed deployment of ' + (deployment.resource_name || 'my app') : 'a pasted deployment log') + ' as "' + card.title + '" (' + card.class + ', ' + card.confidence + ' confidence). Help me fix it.' +
                (deployment ? ' @coolify:deployment/' + deployment.uuid + ' ' : ' ')
            ),
          size: 'xs',
          variant: 'secondary'
        },
        el(Codicon, { name: 'comment-discussion', size: '0.75rem' }),
        'Ask Hermes to fix'
      )
    )
  )
}

function DiagnosisPanel({ deployment, pasteBox }) {
  const [state, setState] = useState({ busy: false, result: null, error: null })
  const [text, setText] = useState('')
  const run = async body => {
    setState({ busy: true, result: null, error: null })
    try {
      const result = await rest('/diagnose', { method: 'POST', body })
      setState({ busy: false, result, error: null })
    } catch (error) {
      setState({ busy: false, result: error.body || null, error })
    }
  }
  useEffect(() => {
    setText('')
    if (deployment) void run({ deployment_uuid: deployment.uuid })
    else setState({ busy: false, result: null, error: null })
  }, [deployment && deployment.uuid, deployment && deployment.at])
  const result = state.result
  const askPaste = !deployment || (result && result.paste_instead)
  return el(
    'div',
    { style: S.col(8) },
    deployment
      ? el(
          'div',
          { style: S.row(8) },
          el(StatusDot, { tone: 'bad' }),
          el('span', { style: S.strong }, deployment.resource_name || 'deployment'),
          el(Small, { style: S.mono }, deployment.uuid),
          el(Small, {}, ago(deployment.created_at)),
          el('span', { style: { flex: 1 } }),
          deployment.url ? el(ExternalLink, { href: deployment.url }, el(Small, { style: { color: T.accent } }, 'Open log in Coolify')) : null
        )
      : null,
    state.busy ? el(Loading, { rows: 3 }) : null,
    result && result.ok && result.diagnosis ? el(DiagnosisCard, { card: result.diagnosis, deployment }) : null,
    result && result.paste_instead && deployment ? el(Note, { icon: 'lock' }, 'No configured token may read build logs (read:sensitive), so paste the log here. In Coolify: open the deployment, switch on "Show Debug Logs", copy the whole log.') : null,
    state.error && !(result && result.paste_instead) ? el(QueryError, { error: state.error, what: 'Diagnosis' }) : null,
    askPaste && pasteBox !== false
      ? el(
          'div',
          { style: S.col(6) },
          el(Textarea, { 'aria-label': 'Deployment log', onChange: e => setText(e.target.value), placeholder: 'Paste a failed deployment log', rows: 5, style: { ...S.mono, fontSize: '0.6875rem' }, value: text }),
          el(
            'div',
            { style: S.row(8) },
            el(Small, { style: { flex: 1 } }, 'The log goes to this page only; secrets are masked before the card is built.'),
            el(Button, { disabled: !text.trim() || state.busy, onClick: () => run({ log_text: text, ...(deployment ? { deployment_uuid: deployment.uuid } : {}) }), size: 'xs' }, 'Diagnose log')
          )
        )
      : null
  )
}

// ── notebook ─────────────────────────────────────────────────────────────────────────────────────

function parseNotes(text) {
  const parts = String(text || '').split(/\n(?=## )/)
  const entries = []
  for (const part of parts) {
    const found = part.match(/^## (.+)\n([\s\S]*)$/)
    if (found) entries.push({ head: found[1].trim(), body: found[2].trim() })
  }
  return entries
}

function NotebookWidget({ widget }) {
  const notes = useNotes()
  const profile = useProfile()
  const client = useQueryClient()
  const props = widget.props || {}
  const [text, setText] = useState('')
  const [topic, setTopic] = useState('')
  const [state, setState] = useState({ busy: false, error: null })
  if (notes.isLoading) return el(Loading, { rows: 4 })
  if (notes.error) return el(QueryError, { error: notes.error, what: 'Notebook' })
  const entries = parseNotes(notes.data.text).reverse().slice(0, props.max_entries || 20)
  const append = async () => {
    setState({ busy: true, error: null })
    try {
      await rest('/notes', { method: 'POST', body: { text, ...(topic.trim() ? { topic: topic.trim() } : {}) } })
      setText('')
      setTopic('')
      setState({ busy: false, error: null })
      await client.invalidateQueries({ queryKey: qk(profile, 'notes') })
    } catch (error) {
      setState({ busy: false, error })
    }
  }
  return el(
    'div',
    { style: S.col(8) },
    entries.length
      ? el(
          'div',
          { style: { ...S.col(8), maxHeight: 360, overflowY: 'auto' } },
          ...entries.map((entry, i) =>
            el(
              'article',
              { key: i, style: { ...S.col(2), paddingBottom: 6, borderBottom: i < entries.length - 1 ? '1px solid ' + T.strokeSoft : 'none' } },
              el(Small, { style: { color: T.secondary } }, entry.head),
              el('div', { style: { ...S.text, whiteSpace: 'pre-wrap', overflowWrap: 'anywhere' } }, entry.body.replace(/\*\*/g, ''))
            )
          )
        )
      : el(EmptyState, { title: 'The notebook is empty', description: 'Write what you learned about this instance; the agent reads it on demand with @coolify:notes.' }),
    props.editable !== false
      ? el(
          'div',
          { style: S.col(6) },
          el(Textarea, { 'aria-label': 'New note', onChange: e => setText(e.target.value), placeholder: 'Add a note, e.g. "app X needs NPM_TOKEN as a build variable"', rows: 2, value: text }),
          el(
            'div',
            { style: S.row(8) },
            el(Input, { 'aria-label': 'Topic', onChange: e => setTopic(e.target.value), placeholder: 'Topic (optional)', style: { width: 160 }, value: topic }),
            el(Small, { style: { flex: 1 } }, notes.data.entries + ' entries in COOLIFY.md'),
            el(Button, { disabled: !text.trim() || state.busy, onClick: append, size: 'xs' }, state.busy ? 'Saving' : 'Add note')
          ),
          state.error ? el(QueryError, { error: state.error }) : null
        )
      : null
  )
}

// ── cockpit chat ─────────────────────────────────────────────────────────────────────────────────

const DEFAULT_SUGGESTIONS = ['Why did the last staging deploy fail?', 'Is any server overloaded?', 'What changed on this instance this week?']

function ChatWidget({ widget }) {
  const chips = useValue($chips)
  const [text, setText] = useState('')
  const [state, setState] = useState({ busy: false, result: null })
  const suggestions = (widget.props && widget.props.suggestions && widget.props.suggestions.length ? widget.props.suggestions : DEFAULT_SUGGESTIONS).slice(0, 6)
  const send = async question => {
    const q = (question || text).trim()
    if (!q) return
    setState({ busy: true, result: null })
    const refs = chips.map(c => c.ref).join(' ')
    const result = await openCockpitChat(q + (refs ? ' ' + refs : '') + ' ')
    setState({ busy: false, result })
    if (!question) setText('')
  }
  const quick = [
    { ref: '@coolify:overview', label: 'overview' },
    { ref: '@coolify:notes', label: 'notebook' }
  ].filter(q => !chips.some(c => c.ref === q.ref))
  return el(
    'div',
    { style: S.col(8) },
    el(
      'div',
      { style: { ...S.row(6), flexWrap: 'wrap' } },
      el(Small, {}, 'Context:'),
      chips.length ? null : el(Small, { style: { color: T.quaternary } }, 'none yet; Ask buttons add it'),
      ...chips.map(chip =>
        el(
          'span',
          { key: chip.ref, style: { ...S.row(4), borderRadius: 4, padding: '1px 4px 1px 6px', background: tint(T.accent, 12), color: T.primary, fontSize: '0.6875rem' }, title: chip.ref },
          el('span', { style: S.mono }, chip.ref.replace(/^@coolify:/, '@')),
          el(IconButton, { icon: 'close', label: 'Remove ' + chip.ref, onClick: () => $chips.set($chips.get().filter(c => c.ref !== chip.ref)) })
        )
      ),
      ...quick.map(q => el(Button, { key: q.ref, onClick: () => addChip(q), size: 'xs', variant: 'ghost' }, el(Codicon, { name: 'add', size: '0.7rem' }), q.label))
    ),
    el(Textarea, {
      'aria-label': 'Ask about this Coolify instance',
      onChange: e => setText(e.target.value),
      onKeyDown: e => {
        if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) {
          e.preventDefault()
          void send()
        }
      },
      placeholder: 'Ask about what you see. Opens the Cockpit chat beside this page with your question drafted; you press send.',
      rows: 2,
      value: text
    }),
    el(
      'div',
      { style: { ...S.row(6), flexWrap: 'wrap' } },
      ...suggestions.map((s, i) => el(Button, { key: i, onClick: () => send(s), size: 'xs', variant: 'chip' }, s)),
      el('span', { style: { flex: 1 } }),
      el(Button, { disabled: !text.trim() || state.busy, onClick: () => send(), size: 'xs' }, el(Codicon, { name: 'comment-discussion', size: '0.75rem' }), 'Draft in chat')
    ),
    state.result
      ? el(Small, {}, state.result.mode === 'tab' ? 'Drafted in the Cockpit chat beside this page. Review it and press send.' : state.result.ok ? 'Opened a new chat with the draft (session control was not available).' : 'Could not fill the draft; the chat is open, paste your question there.')
      : el(Small, { style: { color: T.quaternary } }, 'Runs as a normal Hermes chat on your provider and model; actions there still ask for your approval.')
  )
}

// ── widget registry (JS side of layout.py's WidgetType rows) ────────────────────────────────────

const RENDERERS = {
  summary: SummaryWidget,
  deployments: DeploymentsWidget,
  resources: ResourcesWidget,
  servers: ServersWidget,
  quick_actions: QuickActionsWidget,
  notebook: NotebookWidget,
  chat: ChatWidget
}

const ICONS = {
  summary: 'pulse',
  deployments: 'rocket',
  resources: 'package',
  servers: 'server',
  quick_actions: 'zap',
  notebook: 'notebook',
  chat: 'comment-discussion'
}

// ── edit mode: props editor ──────────────────────────────────────────────────────────────────────

function Field({ name, spec, children }) {
  return el('label', { style: S.col(3) }, el('span', { style: S.label }, name + (spec && spec.description ? ': ' + spec.description : '')), children)
}

function Toggles({ choices, value, onChange }) {
  const set = new Set(value || [])
  return el(
    'div',
    { style: { ...S.row(4), flexWrap: 'wrap' } },
    ...choices.map(choice =>
      el(
        Button,
        {
          'aria-pressed': set.has(choice),
          key: choice,
          onClick: () => {
            const next = new Set(set)
            if (next.has(choice)) next.delete(choice)
            else next.add(choice)
            onChange(choices.filter(c => next.has(c)))
          },
          size: 'xs',
          variant: set.has(choice) ? 'default' : 'chip'
        },
        choice
      )
    )
  )
}

function ButtonsEditor({ value, onChange, kinds }) {
  const rows = Array.isArray(value) ? value : []
  const update = (i, patch) => onChange(rows.map((row, j) => (j === i ? { ...row, ...patch } : row)))
  return el(
    'div',
    { style: S.col(6) },
    ...rows.map((row, i) =>
      el(
        'div',
        { key: i, style: { ...S.row(6), flexWrap: 'wrap' } },
        el(SegmentedControl, { onChange: kind => update(i, { kind }), options: kinds.map(k => ({ id: k, label: k.replace('_', ' ') })), sizing: 'content', value: row.kind || kinds[0] }),
        el(Input, { 'aria-label': 'Target', onChange: e => update(i, { target: e.target.value }), placeholder: 'resource name or uuid', style: { width: 200 }, value: row.target || '' }),
        el(Input, { 'aria-label': 'Label', onChange: e => update(i, { label: e.target.value || undefined }), placeholder: 'label (optional)', style: { width: 160 }, value: row.label || '' }),
        row.kind === 'rollback' ? el(Input, { 'aria-label': 'Commit', onChange: e => update(i, { params: { commit: e.target.value } }), placeholder: 'image tag / commit', style: { width: 140 }, value: (row.params && row.params.commit) || '' }) : null,
        el(IconButton, { icon: 'trash', label: 'Remove button', onClick: () => onChange(rows.filter((_, j) => j !== i)) })
      )
    ),
    el(Button, { onClick: () => onChange(rows.concat([{ kind: 'restart', target: '' }])), size: 'xs', variant: 'ghost' }, el(Codicon, { name: 'add', size: '0.7rem' }), 'Add button')
  )
}

function PropInput({ name, spec, value, onChange }) {
  if (spec.kind === 'bool') {
    const checked = value === undefined ? Boolean(spec.default) : Boolean(value)
    return el('label', { style: S.row(8) }, el(Switch, { checked, onCheckedChange: v => onChange(v), size: 'xs' }), el('span', { style: S.text }, name + ': ' + spec.description))
  }
  if (spec.kind === 'enum') {
    return el(Field, { name, spec }, el(SegmentedControl, { onChange: v => onChange(v), options: spec.choices.map(c => ({ id: c, label: c })), sizing: 'content', value: value === undefined ? spec.default || spec.choices[0] : value }))
  }
  if (spec.kind === 'int') {
    return el(Field, { name, spec }, el(Input, { max: spec.maximum, min: spec.minimum, onChange: e => onChange(e.target.value === '' ? undefined : Number(e.target.value)), placeholder: spec.default != null ? String(spec.default) : '', style: { width: 100 }, type: 'number', value: value === undefined ? '' : value }))
  }
  if (spec.kind === 'list' && spec.item && spec.item.kind === 'object') {
    const kinds = (spec.item.fields && spec.item.fields.kind && spec.item.fields.kind.choices) || ['restart']
    return el(Field, { name, spec }, el(ButtonsEditor, { kinds, onChange: v => onChange(v.length ? v : undefined), value }))
  }
  if (spec.kind === 'list' && spec.item && spec.item.choices) {
    return el(Field, { name, spec }, el(Toggles, { choices: spec.item.choices, onChange: v => onChange(v.length ? v : undefined), value }))
  }
  if (spec.kind === 'list') {
    return el(Field, { name, spec }, el(Textarea, { onChange: e => { const v = e.target.value.split('\n').map(s => s.trim()).filter(Boolean); onChange(v.length ? v : undefined) }, placeholder: 'one per line', rows: 2, value: (value || []).join('\n') }))
  }
  return el(Field, { name, spec }, el(Input, { onChange: e => onChange(e.target.value === '' ? undefined : e.target.value), style: { maxWidth: 320 }, value: value || '' }))
}

function PropsEditor({ widget, type, onChange }) {
  const schema = (type && type.props_schema) || {}
  const set = (name, v) => {
    const props = { ...(widget.props || {}) }
    if (v === undefined) delete props[name]
    else props[name] = v
    onChange({ props })
  }
  return el(
    'div',
    { style: { ...S.col(10), padding: '10px 12px', borderBottom: '1px solid ' + T.strokeSoft, background: tint(T.accent, 4) } },
    el(Field, { name: 'title', spec: { description: 'shown in the widget header' } }, el(Input, { onChange: e => onChange({ title: e.target.value || undefined }), placeholder: type ? type.label : widget.type, style: { maxWidth: 320 }, value: widget.title || '' })),
    ...Object.entries(schema).map(([name, spec]) => el(PropInput, { key: name, name, spec, onChange: v => set(name, v), value: (widget.props || {})[name] }))
  )
}

// ── the page ─────────────────────────────────────────────────────────────────────────────────────

const SIZES = [
  { id: 's', label: 'S' },
  { id: 'm', label: 'M' },
  { id: 'l', label: 'L' }
]

function useWidth(ref) {
  const [width, setWidth] = useState(1200)
  useEffect(() => {
    const node = ref.current
    if (!node || typeof ResizeObserver !== 'function') return undefined
    const observer = new ResizeObserver(entries => setWidth(Math.round(entries[0].contentRect.width)))
    observer.observe(node)
    return () => observer.disconnect()
  }, [ref])
  return width
}

function span(size, columns) {
  if (columns === 1) return 1
  if (columns === 6) return size === 's' ? 3 : 6
  return size === 's' ? 4 : size === 'm' ? 6 : 12
}

function WidgetFrame({ widget, type, avail, editing, index, count, edit }) {
  const [propsOpen, setPropsOpen] = useState(false)
  const Renderer = RENDERERS[widget.type]
  const title = widget.title || (type ? type.label : widget.type)
  const blocked = avail && avail.available === false
  const right = editing
    ? el(
        'div',
        { style: S.row(2) },
        el(SegmentedControl, { onChange: size => edit.update(index, { size }), options: SIZES, value: widget.size || 'm' }),
        el(IconButton, { disabled: index === 0, icon: 'arrow-up', label: 'Move up', onClick: () => edit.move(index, -1) }),
        el(IconButton, { disabled: index === count - 1, icon: 'arrow-down', label: 'Move down', onClick: () => edit.move(index, 1) }),
        el(IconButton, { icon: 'settings-gear', label: propsOpen ? 'Close settings' : 'Widget settings', onClick: () => setPropsOpen(!propsOpen) }),
        el(IconButton, { icon: 'trash', label: 'Remove widget', onClick: () => edit.remove(index) })
      )
    : el(Codicon, { name: ICONS[widget.type] || 'symbol-misc', size: '0.8rem', style: { color: T.quaternary } })
  return el(
    'div',
    { 'data-widget': widget.type, 'data-widget-id': widget.id, style: { gridColumn: 'span ' + edit.span(widget.size), minWidth: 0 } },
    el(
      Card,
      { editing, right, subtitle: editing ? (widget.id === widget.type.replace(/_/g, '-') ? widget.type : widget.id + ' · ' + widget.type) : null, title },
      editing && propsOpen ? el('div', { style: { margin: '-10px -12px 0' } }, el(PropsEditor, { onChange: patch => edit.update(index, patch), type, widget })) : null,
      !Renderer
        ? el(Note, { tone: 'warn' }, 'This cockpit does not know the widget type "' + widget.type + '". Update the plugin, or remove the widget.')
        : blocked
          ? el(NotAvailable, { row: avail })
          : el(Renderer, { widget }),
      !blocked && avail && avail.limits && avail.limits.length
        ? el(Small, { style: { color: T.quaternary } }, 'Limited: ' + avail.limits.map(l => plain(l.replace(/^[a-z_]+: /, ''))).join('; '))
        : null
    )
  )
}

function newWidgetId(type, widgets) {
  const base = type.replace(/_/g, '-')
  const taken = new Set(widgets.map(w => w.id))
  if (!taken.has(base)) return base
  let n = 2
  while (taken.has(base + '-' + n)) n += 1
  return base + '-' + n
}

function PresetDialog({ preset, baseVersion, dirty, onClose, onApplied }) {
  const [env, setEnv] = useState('staging')
  const [placement, setPlacement] = useState('replace')
  if (!preset) return null
  const apply = async () => {
    const params = preset.name === 'deploy_desk' ? { environment: env || 'staging' } : {}
    try {
      const out = await rest('/layout/preset', { method: 'POST', body: { preset: preset.name, params, placement, expected_version: baseVersion } })
      onApplied(out.layout, null)
    } catch (error) {
      if (error.status === 409 && error.body && error.body.layout) {
        onApplied(error.body.layout, error.body)
        return
      }
      throw error
    }
  }
  return el(ConfirmDialog, {
    children: el(
      'div',
      { style: S.col(10) },
      el(Small, {}, preset.description),
      preset.name === 'deploy_desk' ? el(Field, { name: 'environment', spec: { description: 'the environment the desk shows' } }, el(Input, { onChange: e => setEnv(e.target.value), style: { width: 180 }, value: env })) : null,
      el(Field, { name: 'placement', spec: { description: 'replace the layout, or add the preset above or below it' } }, el(SegmentedControl, { onChange: setPlacement, options: [{ id: 'replace', label: 'Replace' }, { id: 'top', label: 'Add on top' }, { id: 'bottom', label: 'Add below' }], sizing: 'content', value: placement })),
      dirty ? el(Note, { tone: 'warn' }, 'Your unsaved edits are dropped; the preset is saved at once.') : null
    ),
    confirmLabel: 'Apply preset',
    description: 'Saves a new layout version that the agent sees too.',
    onClose,
    onConfirm: apply,
    open: true,
    title: 'Apply the ' + preset.label + ' preset'
  })
}

function EditToolbar({ catalog, draft, setDraft, dirty, saving, onSave, onCancel, onPreset }) {
  const types = (catalog && catalog.widget_types) || []
  const presets = (catalog && catalog.presets) || []
  return el(
    'div',
    { style: { ...S.col(8), ...S.box, borderColor: tint(T.accent, 45), background: tint(T.accent, 4) } },
    el(
      'div',
      { style: S.row(8) },
      el(Codicon, { name: 'edit', size: '0.85rem', style: { color: T.accent } }),
      el('span', { style: S.strong }, 'Editing the layout'),
      el(Small, {}, 'version ' + draft.base_version + (dirty ? ', unsaved changes' : '')),
      el('span', { style: { flex: 1 } }),
      el(Button, { onClick: onCancel, size: 'xs', variant: 'ghost' }, dirty ? 'Discard' : 'Done'),
      el(Button, { disabled: !dirty || saving, onClick: onSave, size: 'xs' }, saving ? 'Saving' : 'Save layout')
    ),
    el(
      'div',
      { style: { ...S.row(4), flexWrap: 'wrap' } },
      el(Small, { style: { width: 64 } }, 'Add'),
      ...types.map(type =>
        el(
          Button,
          {
            key: type.type,
            onClick: () => setDraft({ ...draft, widgets: draft.widgets.concat([{ id: newWidgetId(type.type, draft.widgets), type: type.type, size: type.default_size || 'm', props: {} }]) }),
            size: 'xs',
            title: type.description,
            variant: 'chip'
          },
          el(Codicon, { name: ICONS[type.type] || 'add', size: '0.7rem' }),
          type.label
        )
      )
    ),
    el(
      'div',
      { style: { ...S.row(4), flexWrap: 'wrap' } },
      el(Small, { style: { width: 64 } }, 'Presets'),
      ...presets.map(p => el(Button, { key: p.name, onClick: () => onPreset(p), size: 'xs', title: p.description, variant: 'outline' }, p.label))
    ),
    el(Small, { style: { color: T.quaternary } }, 'The agent edits the same layout: try "add a notebook widget to my cockpit".')
  )
}

function Banner() {
  const banner = useValue($banner)
  if (!banner) return null
  return el(Note, { tone: banner.tone, action: el(IconButton, { icon: 'close', label: 'Dismiss', onClick: () => $banner.set(null) }) }, el('strong', {}, banner.title), banner.text ? '\n' + banner.text : '')
}

function StandaloneDiagnosis({ hasWidget }) {
  const selected = useValue($diagnose)
  if (!selected || hasWidget) return null
  return el(
    Card,
    { right: el(IconButton, { icon: 'close', label: 'Close diagnosis', onClick: () => $diagnose.set(null) }), title: 'Diagnosis' },
    el(DiagnosisPanel, { deployment: selected, pasteBox: true })
  )
}

// The first route call after the server's plugin host starts can take several seconds (7.7 s
// measured; later calls 0.2-0.3 s). Past COLD_START_MS without a first answer, say so instead of
// showing skeletons that look stuck.
const COLD_START_MS = 2500
const COLD_START_TEXT = 'Starting the cockpit backend on the server; the first load can take a few seconds.'

function useColdStart(waiting) {
  const [late, setLate] = useState(false)
  useEffect(() => {
    if (!waiting) {
      setLate(false)
      return undefined
    }
    const timer = setTimeout(() => setLate(true), COLD_START_MS)
    return () => clearTimeout(timer)
  }, [waiting])
  return waiting && late
}

function CockpitPage() {
  const profile = useProfile()
  const client = useQueryClient()
  const setup = useSetup()
  const setupNow = setupState(setup)
  const [checking, setChecking] = useState(false)
  const layoutQ = useLayout()
  const snap = useSnapshot()
  const caps = useCapabilities()
  const saved = layoutQ.data && layoutQ.data.layout
  const avail = useAvailability(saved ? saved.version : undefined)
  const [editing, setEditing] = useState(false)
  const [draft, setDraft] = useState(null)
  const [saving, setSaving] = useState(false)
  const [preset, setPreset] = useState(null)
  const [refreshing, setRefreshing] = useState(false)
  const gridRef = useRef(null)
  const width = useWidth(gridRef)
  const columns = width >= 1040 ? 12 : width >= 640 ? 6 : 1

  // An agent edit while the page is open shows on the next layout poll; say so once.
  const lastVersion = useRef(null)
  useEffect(() => {
    if (!saved) return
    if (lastVersion.current != null && saved.version !== lastVersion.current && saved.updated_by && saved.updated_by !== 'pane' && !editing) {
      $banner.set({ tone: null, title: 'Layout updated by ' + saved.updated_by, text: 'Version ' + saved.version + ' at ' + (saved.updated_at || 'just now') + '.' })
    }
    lastVersion.current = saved.version
  }, [saved && saved.version])

  const startEdit = () => {
    setDraft({ base_version: saved.version, preset: saved.preset, widgets: saved.widgets.map(w => ({ ...w, props: { ...(w.props || {}) } })) })
    setEditing(true)
  }
  const stopEdit = () => {
    setEditing(false)
    setDraft(null)
  }
  const dirty = Boolean(draft && saved && JSON.stringify(draft.widgets) !== JSON.stringify(saved.widgets))
  const applyServerLayout = (layout, conflict) => {
    client.setQueryData(qk(profile, 'layout'), old => ({ ...(old || {}), layout }))
    if (conflict) {
      $banner.set({
        tone: 'warn',
        title: 'The layout changed while you were editing',
        text: 'Version ' + layout.version + ' was saved by ' + (layout.updated_by || 'someone else') + (layout.updated_at ? ' ' + ago(layout.updated_at) : '') + ' while you edited version ' + (conflict.expected_version != null ? conflict.expected_version : '?') + '. The page reloaded it; your edits were not saved. Make them again and save.'
      })
      log('layout conflict', { expected: conflict.expected_version, current: conflict.current_version })
    }
    setDraft({ base_version: layout.version, preset: layout.preset, widgets: layout.widgets.map(w => ({ ...w, props: { ...(w.props || {}) } })) })
  }
  const save = async () => {
    setSaving(true)
    try {
      const out = await rest('/layout', { method: 'PUT', body: { expected_version: draft.base_version, layout: { preset: draft.preset, widgets: draft.widgets } } })
      client.setQueryData(qk(profile, 'layout'), old => ({ ...(old || {}), layout: out.layout }))
      lastVersion.current = out.layout.version
      $banner.set({ tone: 'good', title: 'Layout saved as version ' + out.layout.version, text: 'The agent sees the same layout (coolify_cockpit).' })
      stopEdit()
    } catch (error) {
      if (error.status === 409 && error.body && error.body.layout) {
        applyServerLayout(error.body.layout, error.body)
        await client.invalidateQueries({ queryKey: qk(profile, 'layout') })
      } else {
        const list = error.body && error.body.errors ? '\n' + error.body.errors.join('\n') : ''
        $banner.set({ tone: 'bad', title: 'The layout was not saved', text: plain(error.message) + list })
      }
    } finally {
      setSaving(false)
    }
  }
  const edit = {
    span: size => span(size, columns),
    update: (i, patch) => setDraft(d => ({ ...d, widgets: d.widgets.map((w, j) => (j === i ? { ...w, ...patch } : w)) })),
    move: (i, delta) =>
      setDraft(d => {
        const widgets = d.widgets.slice()
        const [w] = widgets.splice(i, 1)
        widgets.splice(Math.max(0, Math.min(widgets.length, i + delta)), 0, w)
        return { ...d, widgets }
      }),
    remove: i => setDraft(d => ({ ...d, widgets: d.widgets.filter((_, j) => j !== i) }))
  }
  const refresh = async () => {
    setRefreshing(true)
    try {
      const fresh = await rest('/snapshot?refresh=true')
      client.setQueryData(qk(profile, 'snapshot'), fresh)
      await client.invalidateQueries({ queryKey: qk(profile, 'capabilities') })
      await client.invalidateQueries({ queryKey: qk(profile, 'layout') })
    } catch (error) {
      $banner.set({ tone: 'bad', title: 'Refresh failed', text: error.message })
    } finally {
      setRefreshing(false)
    }
  }

  const widgets = editing && draft ? draft.widgets : (saved && saved.widgets) || []
  const availById = new Map(((avail.data && avail.data.availability) || []).map(row => [row.id, row]))
  const types = new Map(((layoutQ.data && layoutQ.data.widget_types) || []).map(t => [t.type, t]))
  const inst = (caps.data && caps.data.instance) || {}
  const diagnosing = useValue($diagnose)
  const shownDeploy = Boolean(diagnosing && ((snap.data && snap.data.latest_deployments) || []).slice(0, 8).some(d => d.uuid === diagnosing.uuid))
  const running = (((snap.data || {}).deployments || {}).running || []).length
  const tracked = useValue($tracked)
  const fast = running > 0 || tracked.some(t => !t.done)
  const anyError = setup.error || layoutQ.error || snap.error || caps.error
  const coldStart = useColdStart(!anyError && (setup.isLoading || layoutQ.isLoading || (setupNow === 'ready' && (snap.isLoading || caps.isLoading))))
  const onboarding = setupNow === 'off' || setupNow === 'needed'
  const checkSetup = async () => {
    setChecking(true)
    try {
      await setup.refetch()
    } finally {
      setChecking(false)
    }
  }

  return el(
    'div',
    { style: { height: '100%', overflow: 'auto' } },
    el(
      'div',
      { ref: gridRef, style: { ...S.col(12), maxWidth: 1480, margin: '0 auto', padding: '14px 18px 32px', fontSize: '0.75rem', color: T.secondary } },
      el(
        'div',
        { style: { ...S.row(10), flexWrap: 'wrap' } },
        el('h1', { style: { margin: 0, fontSize: '0.9375rem', fontWeight: 600, color: T.primary } }, 'Coolifybrücke'),
        inst.url ? el(ExternalLink, { href: inst.url }, el(Small, { style: { color: T.accent } }, hostOf(inst.url))) : null,
        inst.coolify_version ? el(Small, {}, 'v' + inst.coolify_version) : null,
        // A14: the header carries no counts; the summary row is the only place a count shows.
        caps.data && caps.data.setup && caps.data.setup.line
          ? el(IconButton, { icon: caps.data.setup.tone === 'good' ? 'key' : 'warning', label: caps.data.setup.line + '. Open settings.', onClick: () => host.navigate(settingsHref(profile)) })
          : null,
        el('span', { style: { flex: 1 } }),
        onboarding
          ? null
          : el(
              Button,
              {
                disabled: refreshing,
                onClick: refresh,
                size: 'xs',
                title: snap.data ? 'Updated ' + ago(snap.data.taken_at) + '. Refreshes every ' + (fast ? '15' : '60') + ' s.' : undefined,
                variant: 'ghost'
              },
              el(Codicon, { name: 'refresh', size: '0.75rem', spinning: refreshing }),
              'Refresh'
            ),
        onboarding ? null : el(AskButton, { kind: 'overview', item: {}, label: 'Ask' }),
        editing || onboarding ? null : el(Button, { disabled: !saved, onClick: startEdit, size: 'xs', variant: 'secondary' }, el(Codicon, { name: 'edit', size: '0.75rem' }), 'Edit')
      ),
      el(Banner, {}),
      coldStart ? el('div', { 'data-cc-notice': 'cold-start', role: 'status' }, el(Note, {}, COLD_START_TEXT)) : null,
      onboarding ? el(SetupPanel, { checking, data: setup.data, onCheck: checkSetup, state: setupNow }) : null,
      onboarding ? null : el(ActionTracker, {}),
      editing && draft
        ? el(EditToolbar, { catalog: layoutQ.data, dirty, draft, onCancel: stopEdit, onPreset: setPreset, onSave: save, saving, setDraft })
        : null,
      onboarding ? null : el(StandaloneDiagnosis, { hasWidget: widgets.some(w => w.type === 'deployments') && shownDeploy }),
      onboarding || setupNow === null
        ? null
        : layoutQ.isLoading
        ? el(Loading, { rows: 6 })
        : layoutQ.error
          ? el(ErrorState, { description: layoutQ.error.message + (layoutQ.error.body && layoutQ.error.body.hint ? ' ' + layoutQ.error.body.hint : ''), title: 'The cockpit layout did not load' })
          : widgets.length === 0
            ? el(EmptyState, { title: 'No widgets', description: 'Edit the layout to add widgets or apply a preset, or ask the agent to arrange the cockpit.' })
            : el(
                'div',
                { style: { display: 'grid', gridTemplateColumns: 'repeat(' + columns + ', minmax(0, 1fr))', gridAutoFlow: editing ? 'row' : 'row dense', gap: 12, alignItems: 'start' } },
                ...widgets.map((widget, index) =>
                  el(WidgetFrame, { avail: availById.get(widget.id), count: widgets.length, edit, editing, index, key: widget.id, type: types.get(widget.type), widget })
                )
              ),
      !onboarding && saved && saved.warnings && saved.warnings.length ? el(Note, { tone: 'warn' }, 'Some widgets in layout.json were skipped:\n' + saved.warnings.join('\n')) : null,
    ),
    el(ActionConfirm, {}),
    preset
      ? el(PresetDialog, {
          baseVersion: draft ? draft.base_version : saved && saved.version,
          dirty,
          onApplied: (layout, conflict) => {
            applyServerLayout(layout, conflict)
            if (!conflict) $banner.set({ tone: 'good', title: 'Preset applied as version ' + layout.version, text: null })
            setPreset(null)
          },
          onClose: () => setPreset(null),
          preset
        })
      : null
  )
}

// ── onboarding ───────────────────────────────────────────────────────────────────────────────────
// Shown instead of the widgets until the cockpit has a URL and a token. Hermes' own settings form
// holds the inputs (a plugin page cannot store a secret); each step links there, and the panel
// notices a saved value within a few seconds.

function settingsHref(profile) {
  return SETTINGS_PAGE + (profile ? '&profile=' + encodeURIComponent(profile) : '')
}

function SetupStep({ n, done, optional, title, status, children, action }) {
  const color = done ? T.green : T.tertiary
  return el(
    'div',
    { 'data-cc-step': String(n), style: { ...S.row(12, { alignItems: 'flex-start' }), ...S.box, padding: '12px 14px' } },
    el(
      'div',
      {
        style: {
          flex: '0 0 auto', width: 22, height: 22, borderRadius: 11, display: 'grid', placeItems: 'center',
          border: '1px solid ' + (done ? tint(T.green, 50) : T.stroke), background: done ? tint(T.green, 14) : 'transparent',
          color, fontSize: '0.6875rem', fontWeight: 600
        }
      },
      done ? el(Codicon, { name: 'check', size: '0.75rem' }) : String(n)
    ),
    el(
      'div',
      { style: { ...S.col(6), flex: 1 } },
      el(
        'div',
        { style: { ...S.row(8), flexWrap: 'wrap' } },
        el('span', { style: S.strong }, title),
        optional ? el(Badge, { variant: 'outline' }, 'optional') : el(Badge, { variant: done ? 'success' : 'warn' }, done ? 'done' : 'required'),
        status ? el(Small, { style: { ...S.mono, color: done ? T.green : T.tertiary } }, status) : null
      ),
      children,
      action ? el('div', { style: { ...S.row(8), flexWrap: 'wrap', paddingTop: 2 } }, ...[].concat(action)) : null
    )
  )
}

// 0.1.0 (PLAN-v0.1, Tokens and setup): two steps, each one line and one button. The detail sits
// behind "Why?". The optional read-only token never shows here: the result line offers it when the
// saved token is limited to some projects.
function SetupPanel({ state, data, onCheck, checking }) {
  const profile = useProfile()
  const [why, setWhy] = useState(false)
  const steps = new Map(((data && data.steps) || []).map(step => [step.id, step]))
  const url = steps.get('url') || {}
  const token = steps.get('action_token') || {}
  const base = url.set && url.value ? String(url.value).replace(/\/+$/, '') : ''
  const openSettings = label => el(Button, { onClick: () => host.navigate(settingsHref(profile)), size: 'xs', variant: 'default' }, el(Codicon, { name: 'settings-gear', size: '0.75rem' }), label)
  const tokenPage = base ? el(Button, { onClick: () => openExternal(base + '/security/api-tokens'), size: 'xs', variant: 'outline' }, el(Codicon, { name: 'link-external', size: '0.75rem' }), 'Open API tokens in Coolify') : null
  const off = state === 'off'
  let n = 0
  return el(
    'section',
    { 'data-cc-setup': state, style: { ...S.col(10), maxWidth: 760 } },
    el('h2', { style: { margin: 0, fontSize: '0.875rem', fontWeight: 600, color: T.primary } }, 'Set up the cockpit'),
    off
      ? el(SetupStep, {
          n: ++n, title: 'Turn on the plugin in this profile',
          action: el(Button, { onClick: () => host.navigate(PLUGINS_PAGE), size: 'xs', variant: 'default' }, el(Codicon, { name: 'extensions', size: '0.75rem' }), 'Open Plugins')
        },
        el('span', { style: S.text }, 'Switch on coolifybruecke in Capabilities, Plugins.'))
      : null,
    el(SetupStep, {
        n: ++n, done: Boolean(url.set), title: 'Coolify URL',
        status: url.set ? url.value : url.error ? plain(url.error) : null,
        action: off ? null : openSettings(url.set ? 'Change' : 'Enter URL')
      },
      el('span', { style: S.text }, 'The address you open Coolify at.')),
    el(SetupStep, {
        n: ++n, done: Boolean(token.set), title: 'API token',
        status: token.set ? 'set' : null,
        action: off ? null : [tokenPage, openSettings(token.set ? 'Change' : 'Paste token')].filter(Boolean)
      },
      el('div', { style: { ...S.row(6), flexWrap: 'wrap' } },
        el('span', { style: S.text }, 'Create one with'),
        el(Badge, { variant: 'outline' }, 'read'),
        el(Badge, { variant: 'outline' }, 'deploy'),
        el('span', { style: S.text }, 'on all projects.'),
        el('button', { type: 'button', onClick: () => setWhy(!why), style: { background: 'none', border: 'none', padding: 0, cursor: 'pointer', color: T.tertiary, fontSize: '0.75rem', textDecoration: 'underline' } }, why ? 'Hide' : 'Why?')),
      why ? el(Small, {}, 'Read shows your apps, deploys and servers. Deploy adds the Deploy and Restart buttons. Each agent action still asks you, and the agent stays off protected environments. The token is stored in this profile; the cockpit shows only its fingerprint. If API access is off: Coolify, Settings, Advanced.') : null),
    el(
      'div',
      { style: { ...S.row(10), flexWrap: 'wrap' } },
      el(Button, { disabled: checking, onClick: onCheck, size: 'xs', variant: 'secondary' }, el(Codicon, { name: 'refresh', size: '0.75rem', spinning: checking }), 'Check again')
    )
  )
}

// ── status-bar chip ──────────────────────────────────────────────────────────────────────────────

// Settings, Plugins, Coolifybrücke: the connection, the token's rights and scope, and what the
// setup unlocks. The settings form itself sits under this entry (Desktop folds it in).
function SettingsPage() {
  return el(
    'div',
    { style: S.col(12) },
    el(Card, { title: 'Connection and token' }, el(ConnectionWidget, { widget: { props: { show_warnings: true, show_projects: false } } })),
    el(AlertsCard, {}),
    el(TwoPlaceNote, {})
  )
}

// Desktop loads this file from the computer it runs on, never from a remote Hermes, so an update on the
// server leaves this half behind. host.state.connectionId is 'local' for this computer's own Hermes
// (Desktop's LOCAL_CONNECTION_ID, never given to a remote source); any other value, null or a Desktop
// without the field shows the line, because a missed update costs more than one line.
const NO_CONNECTION = atom(null)
const TWO_PLACE_TIP = 'No Hermes on this computer: Capabilities → Plugins → Install from Git with this plugin\'s repository, Force reinstall.'

function TwoPlaceNote() {
  const id = useValue((host.state && host.state.connectionId) || NO_CONNECTION)
  if (id === 'local') return null
  return el('div', { 'data-cc-two-place': '', title: TWO_PLACE_TIP, style: S.row(6, S.small) },
    el(Codicon, { name: 'info', size: '0.75rem' }),
    el('span', null, 'Hermes on another computer? Update this plugin on both. Here: ',
      el('code', null, 'hermes plugins update ' + ID), ', then Rescan.'))
}

// 0.1.0 M6: alerts run as a Hermes cron job without a model turn (GET /alerts reads it back). The
// page cannot create a job; the button drafts the request to Hermes, which creates it in that chat.
function useAlerts() {
  const profile = useProfile()
  return useQuery({ queryKey: qk(profile, 'alerts'), queryFn: () => rest('/alerts'), refetchInterval: 60000, staleTime: 20000, retry: retryOnce })
}

function alertsLine(a) {
  if (!a || !a.on) return 'Off. Hermes can check every few minutes and tell you what is new.'
  const age = typeof a.last_check_age_s === 'number' ? 'checked ' + (a.last_check_age_s < 90 ? 'just now' : Math.round(a.last_check_age_s / 60) + ' min ago') : 'not run yet'
  return 'On, ' + (a.schedule || '?') + ', to ' + (a.deliver || 'local') + ' · ' + age + ' · ' + (a.open || 0) + ' open'
}

const ALERTS_ON_PROMPT = 'Turn on Coolifybrücke alerts: run `hermes coolifybruecke alerts on`, or create the no-agent cron job the coolifybruecke skill describes so the alerts land in this chat.'
const ALERTS_OFF_PROMPT = 'Turn off Coolifybrücke alerts (`hermes coolifybruecke alerts off`).'

function AlertsCard() {
  const q = useAlerts()
  const a = q.data
  const [busy, setBusy] = useState(false)
  const draft = async text => {
    setBusy(true)
    try {
      await openCockpitChat(text)
    } finally {
      setBusy(false)
    }
  }
  return el(
    Card,
    { title: 'Alerts' },
    el(
      'div',
      { 'data-cc-alerts': a && a.on ? 'on' : 'off', style: { ...S.row(10), flexWrap: 'wrap' } },
      el(StatusDot, { tone: a && a.on ? 'good' : 'muted' }),
      el('span', { style: { ...S.text, flex: 1 } }, q.error ? 'Could not read the alert job.' : alertsLine(a)),
      el(Button, { disabled: busy || !a, onClick: () => draft(a && a.on ? ALERTS_OFF_PROMPT : ALERTS_ON_PROMPT), size: 'xs', variant: a && a.on ? 'outline' : 'default' },
        el(Codicon, { name: 'comment-discussion', size: '0.75rem' }), a && a.on ? 'Ask Hermes to turn off' : 'Ask Hermes to turn on')
    )
  )
}

function CockpitChip() {
  const setup = useSetup()
  const snap = useSnapshot()
  const caps = useCapabilities()
  const now = setupState(setup)
  if (now === 'off') return null // backend off on this profile
  if (snap.error && snap.error.status === 404 && !(snap.error.body && snap.error.body.error)) return null
  if (now === 'needed') {
    return el(
      'button',
      {
        'aria-label': 'Coolifybrücke is not set up yet. Open the cockpit to set it up.',
        className: 'inline-flex h-full items-center gap-1 px-1.5 text-[0.6875rem] tabular-nums text-(--ui-text-tertiary) transition-colors hover:bg-(--chrome-action-hover) hover:text-foreground',
        onClick: () => host.navigate(PATH),
        style: { background: 'transparent', border: 'none', cursor: 'pointer' },
        title: 'Coolifybrücke is not set up yet. Open the cockpit to set it up.',
        type: 'button'
      },
      el(StatusDot, { tone: 'muted' }),
      el('span', {}, 'Coolify: setup needed')
    )
  }
  const data = snap.data || {}
  const servers = data.servers || []
  const failed = signalCount(data, 'failing')
  const running = ((data.deployments || {}).running || []).length
  const update = Boolean(caps.data && caps.data.instance && caps.data.instance.update_available)
  const tone = snap.error ? 'bad' : !snap.data ? 'muted' : servers.some(s => !s.reachable) ? 'bad' : failed ? 'warn' : 'good'
  const title = snap.error
    ? 'Coolifybrücke: ' + snap.error.message
    : 'Coolify: ' + servers.filter(s => s.reachable).length + '/' + servers.length + ' servers reachable, ' + running + ' deploying, ' + failed + ' failing' + (update ? ', update available' : '') + '. Open the cockpit.'
  return el(
    'button',
    {
      'aria-label': title,
      className: 'inline-flex h-full items-center gap-1 px-1.5 text-[0.6875rem] tabular-nums text-(--ui-text-tertiary) transition-colors hover:bg-(--chrome-action-hover) hover:text-foreground',
      onClick: () => host.navigate(PATH),
      style: { background: 'transparent', border: 'none', cursor: 'pointer' },
      title,
      type: 'button'
    },
    el(StatusDot, { tone }),
    el('span', {}, 'Coolify'),
    running ? el('span', { style: { color: T.yellow } }, running + ' deploying') : null,
    failed ? el('span', { style: { color: T.red } }, failed + ' failing') : null,
    update ? el(Codicon, { name: 'arrow-circle-up', size: '0.7rem', style: { color: T.yellow }, title: 'Coolify update available' }) : null
  )
}

export default {
  id: ID,
  name: 'Coolifybrücke',
  description: 'What needs you on your Coolify instance: failing apps, latest deploys, servers. Deploy and restart behind a confirm.',
  defaultEnabled: false,
  register(ctx) {
    pluginCtx = ctx
    ctx.register({ id: 'page', area: ROUTES_AREA, data: { path: PATH }, render: () => jsx(CockpitPage, {}) })
    ctx.register({ id: 'nav', area: SIDEBAR_NAV_AREA, order: 55, data: { codicon: 'dashboard', label: NAV_LABEL, path: PATH } })
    ctx.register({ id: 'chip', area: STATUSBAR_AREAS.right, order: 70, render: () => jsx(CockpitChip, {}) })
    // Settings, Plugins: the same name and icon as the sidebar row. Desktop folds the plugin's
    // settings form (plugin.yaml config_schema) under this entry as its "Agent settings" page.
    if (typeof ctx.registerSettingsPage === 'function') {
      ctx.registerSettingsPage({ id: 'settings', title: NAV_LABEL, icon: 'dashboard', order: 55, render: () => jsx(SettingsPage, {}) })
    }
    ctx.register({
      id: 'open',
      area: PALETTE_AREA,
      data: { id: 'coolifybruecke.open', label: 'Open Coolifybrücke', keywords: ['coolify', 'cockpit', 'deploy', 'servers'], run: () => host.navigate(PATH) }
    })
    log('registered', { path: PATH })
  }
}
