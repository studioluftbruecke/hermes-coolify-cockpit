// node desktop_write_path.mjs <case>: one case per process (the page keeps module state).
// Mounts the real desktop/plugin.js on desktop_page_harness.mjs and prints one line
// RESULT {"ok": bool, ...details}. The Python test runs every case and asserts ok.
import * as h from './harness.mjs'
import plugin from './plugin.mjs'

const APPS = [
  { uuid: 'app-a', name: 'app-a', kind: 'application', project: 'Demo', environment: 'staging', status: 'running:healthy' },
  { uuid: 'app-b', name: 'app-b', kind: 'application', project: 'Demo', environment: 'staging', status: 'running:healthy' }
]
const iso = ago => new Date(Date.now() - ago).toISOString()
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
      { kind: 'cancel', label: 'Cancel deployment', target_kinds: ['application'] }
    ],
    action_token: { role: 'api', setting: 'COOLIFYBRUECKE_TOKEN', deploy: true }
  },
  snapshot: opts => ({
    ok: true, taken_at: iso(2000), servers: [], resources: APPS, fleet: {},
    deployments: { running: opts.running ? [{ uuid: 'dep-run', resource_uuid: 'app-a', status: 'in_progress' }] : [] },
    summary: [{ signal: 'env_changed', count: 2, label: 'env changed', severity: 'warn' }],
    attention: APPS.map(a => ({ signal: 'env_changed', resource_uuid: a.uuid, severity: 'warn', label: 'env changed' })),
    latest_deployments: [
      opts.running
        ? { uuid: 'dep-run', resource_uuid: 'app-a', resource_name: 'app-a', status: 'in_progress', created_at: iso(60000), url: opts.url }
        : { uuid: 'dep-1', resource_uuid: 'app-a', resource_name: 'app-a', status: 'finished', created_at: iso(60000), finished_at: iso(30000), url: opts.url }
    ]
  }),
  layout: {
    ok: true,
    layout: { version: 1, updated_by: 'pane', widgets: [
      { id: 'summary', type: 'summary', size: 'l', props: {} },
      { id: 'apps', type: 'resources', size: 'l', props: { row_actions: ['deploy'], pairs: false } },
      { id: 'deployments', type: 'deployments', size: 'l', props: {} }
    ] },
    widget_types: [], presets: []
  }
}

const plan = (kind, uuid) => {
  if (kind === 'cancel') {
    return { ok: true, dry_run: true, sent: false, kind, label: 'Cancel deployment', target: { kind: 'deployment', uuid, resource_uuid: 'app-a', name: 'app-a', project: 'Demo', environment: 'staging', server: 's1' },
      request: { method: 'POST', path: '/deployments/' + uuid + '/cancel', params: {} }, token: { setting: 'COOLIFYBRUECKE_TOKEN', fingerprint: 'abcd1234' }, confirm_text: 'cancel deployment ' + uuid }
  }
  const app = APPS.find(a => a.uuid === uuid) || APPS[0]
  return { ok: true, dry_run: true, sent: false, kind, label: 'Deploy', target: { kind: 'application', uuid: app.uuid, name: app.name, project: 'Demo', environment: 'staging', server: 's1' },
    request: { method: 'GET', path: '/deploy', params: { uuid: app.uuid } }, token: { setting: 'COOLIFYBRUECKE_TOKEN', fingerprint: 'abcd1234' }, confirm_text: 'deploy application ' + app.name }
}

// ctx.rest rejects an HTTP error the way Electron does: the status and the JSON body in the message.
const fail = (status, body) => {
  throw new Error('Error invoking remote method: ' + status + ': ' + JSON.stringify(body))
}

function boot(opts = {}) {
  const calls = []
  const live = []
  const opened = []
  const handler = (path, init) => {
    const method = (init && init.method) || 'GET'
    const body = (init && init.body) || {}
    const profile = h.host.state.profile.get()
    calls.push({ method, path, profile, dry_run: Boolean(body.dry_run), target: body.target })
    const p = path.split('?')[0]
    if (opts.route) {
      const out = opts.route({ method, path, p, body, profile })
      if (out !== undefined) return out
    }
    if (p === '/setup') return FIX.setup
    if (p === '/capabilities') return FIX.capabilities
    if (p === '/snapshot') return FIX.snapshot({ url: opts.url || 'https://coolify.example.test/project/x/deployment/dep-1', running: opts.running })
    if (p === '/actions' && method === 'GET') return FIX.actions
    if (p === '/actions' && method === 'POST') {
      if (body.dry_run) return plan(body.kind, body.target)
      live.push(body)
      if (opts.liveFail) {
        const failure = opts.liveFail(body, live)
        if (failure) fail(failure.status, failure.body)
      }
      if (body.kind === 'cancel') return { ok: true, sent: true, kind: body.kind, message: 'cancelled' }
      return { ok: true, sent: true, kind: body.kind, deployment_uuid: 'dep-' + body.target + '-' + live.length, message: 'queued' }
    }
    if (p === '/layout' && path.includes('availability')) return { ok: true, availability: [] }
    if (p === '/layout') return FIX.layout
    if (p.startsWith('/resources/')) return { ok: true, deployments: [{ uuid: 'none-yet', status: 'queued' }] }
    if (p === '/alerts') return { ok: true, on: false }
    if (p === '/notes') return { ok: true, text: '', entries: 0 }
    return fail(404, { detail: 'no fixture for ' + path })
  }
  const contributions = []
  const ctx = {
    rest: async (path, init) => handler(path, init),
    os: { openExternal: async url => (opened.push(url), opts.openExternal !== undefined ? opts.openExternal : true) },
    storage: { get: (k, f) => f, set: () => {}, remove: () => {} },
    register: c => (contributions.push(c), () => {}),
    registerSettingsPage: c => (contributions.push({ ...c, area: 'settings' }), () => {})
  }
  plugin.register(ctx)
  const ids = opts.mount || ['page']
  const parts = ids.map(id => contributions.find(c => c.id === id))
  h.mount(h.jsx('div', { children: parts.map((c, i) => h.jsx('section', { 'data-part': ids[i], children: (c.render || c.data.render)() }, ids[i])) }))
  return { calls, live, opened }
}

const outsideDialog = label => h.buttons(label).find(b => !h.inDialog(b))
const confirmButton = label => h.buttons(label).find(b => h.inDialog(b))
const dialogOpen = () => h.all(n => n.props.role === 'dialog').length > 0
const liveSends = calls => calls.filter(c => c.method === 'POST' && c.path === '/actions' && !c.dry_run)
const dryRuns = calls => calls.filter(c => c.method === 'POST' && c.path === '/actions' && c.dry_run)

async function openDeploy() {
  await h.settle()
  const deploy = outsideDialog('Deploy')
  if (!deploy) throw new Error('no Deploy button on the page: ' + h.pageText().slice(0, 300))
  h.click(deploy)
  await h.settle()
  const confirm = confirmButton('Deploy app-a')
  if (!confirm || confirm.props.disabled) throw new Error('the confirm never became ready: ' + h.pageText().slice(0, 400))
  return confirm
}

async function openBulk() {
  await h.settle()
  const bulk = outsideDialog('Redeploy 2 apps with changed env')
  if (!bulk) throw new Error('no bulk button: ' + h.pageText().slice(0, 300))
  h.click(bulk)
  await h.settle()
  const confirm = confirmButton('Redeploy 2 apps')
  if (!confirm || confirm.props.disabled) throw new Error('the bulk confirm never became ready')
  return confirm
}

const cases = {
  // Enter on a focused Cancel closes the dialog and sends nothing.
  async enter_on_cancel() {
    const app = boot()
    await openDeploy()
    const cancel = confirmButton('Cancel')
    h.press(cancel, 'Enter')
    await h.settle()
    const sent = liveSends(app.calls).length
    return { ok: sent === 0 && !dialogOpen(), live_sends: sent, dialog_open: dialogOpen() }
  },

  // The keyboard path still works: focus moves to the confirm once the check passed, Enter sends once.
  async enter_on_confirm() {
    const app = boot()
    const confirm = await openDeploy()
    const focusedConfirm = h.focusedNode() === confirm
    h.press(h.focusedNode(), 'Enter')
    await h.settle()
    const sent = liveSends(app.calls).length
    return { ok: focusedConfirm && sent === 1, focused_confirm: focusedConfirm, live_sends: sent }
  },

  // Bulk redeploy: app-b fails after app-a was queued. app-a is followed on the page, the confirm
  // is disarmed, and Check again plans only what was not sent.
  async bulk_partial() {
    const app = boot({ liveFail: body => (body.target === 'app-b' ? { status: 502, body: { ok: false, error: 'upstream', message: 'Coolify answered 502', sent: true } } : null) })
    const confirm = await openBulk()
    h.click(confirm)
    await h.settle()
    const shown = h.pageText()
    const tracked = /Deploy app-a/.test(shown)
    const armed = h.buttons(/^Redeploy/).some(b => h.inDialog(b) && !b.props.disabled)
    for (const b of h.buttons(/^Redeploy/).filter(h.inDialog)) h.click(b)
    await h.settle()
    const aAfterClick = app.live.filter(b => b.target === 'app-a').length
    const dryBefore = dryRuns(app.calls).length
    const again = confirmButton('Check again')
    if (again) h.click(again)
    await h.settle()
    const replanned = dryRuns(app.calls).slice(dryBefore).map(c => c.target)
    const rest = h.buttons(/^Redeploy 1 app$/).find(b => h.inDialog(b) && !b.props.disabled)
    if (rest) h.click(rest)
    await h.settle()
    const aTotal = app.live.filter(b => b.target === 'app-a').length
    const bTotal = app.live.filter(b => b.target === 'app-b').length
    const ok = tracked && !armed && aAfterClick === 1 && Boolean(again) && JSON.stringify(replanned) === '["app-b"]' && Boolean(rest) && aTotal === 1 && bTotal === 2
    return { ok, tracked_after_partial: tracked, confirm_armed_after_failure: armed, app_a_sends_after_click: aAfterClick, check_again_shown: Boolean(again), replanned, app_a_sends: aTotal, app_b_sends: bTotal, error_shown: /Coolify answered 502/.test(shown) }
  },

  // One app, 502 with sent: true (the request left and may have landed): the confirm is disarmed,
  // the dialog says it may have run, and only a new dry run arms it again.
  async single_502_sent() {
    const app = boot({ liveFail: (body, live) => (live.length === 1 ? { status: 502, body: { ok: false, error: 'upstream', message: 'Coolify answered 502', sent: true } } : null) })
    const confirm = await openDeploy()
    h.click(confirm)
    await h.settle()
    const shown = h.pageText()
    const disarmed = h.buttons('Deploy app-a').filter(h.inDialog).every(b => b.props.disabled)
    h.press(h.focusedNode() || confirm, 'Enter')
    for (const b of h.buttons('Deploy app-a').filter(h.inDialog)) h.click(b)
    await h.settle()
    const afterRetry = liveSends(app.calls).length
    const dryBefore = dryRuns(app.calls).length
    const again = confirmButton('Check again')
    if (again) h.click(again)
    await h.settle()
    const rearmed = confirmButton('Deploy app-a')
    const newDryRun = dryRuns(app.calls).length - dryBefore
    if (rearmed && !rearmed.props.disabled) h.click(rearmed)
    await h.settle()
    const total = liveSends(app.calls).length
    const ok = disarmed && afterRetry === 1 && /may have run/.test(shown) && Boolean(again) && newDryRun === 1 && total === 2
    return { ok, disarmed, sends_after_retry_clicks: afterRetry, says_may_have_run: /may have run/.test(shown), check_again_shown: Boolean(again), new_dry_runs: newDryRun, total_sends: total }
  },

  // One app, 502 with sent: false (a login proxy redirected it; nothing reached Coolify): the
  // dialog says it did not run, never that it may have, and Check again plans a new send.
  async single_502_not_sent() {
    const redirect = 'the Coolify address answered with a redirect to https://sso.example.net/login (HTTP 302); the request was not repeated there and the token was not sent there.'
    const app = boot({ liveFail: (body, live) => (live.length === 1 ? { status: 502, body: { ok: false, error: 'unreachable', message: redirect, sent: false } } : null) })
    const confirm = await openDeploy()
    h.click(confirm)
    await h.settle()
    const shown = h.pageText()
    const mayHaveRun = /may have run/.test(shown)
    const didNotRun = /app-a did not reach Coolify/.test(shown)
    const again = Boolean(confirmButton('Check again'))
    const ok = !mayHaveRun && didNotRun && again && /not repeated there/.test(shown) && liveSends(app.calls).length === 1
    return { ok, says_may_have_run: mayHaveRun, says_did_not_reach: didNotRun, check_again_shown: again }
  },

  // One app, refused before sending (the token was revoked since the dry run): the dialog says the request
  // was not sent, never that it did not reach Coolify, which would contradict Coolify's own 401 above it.
  async single_401_not_sent() {
    const app = boot({ liveFail: (body, live) => (live.length === 1 ? { status: 401, body: { ok: false, error: 'unauthorized', message: 'Coolify rejected the API token (401).', sent: false } } : null) })
    const confirm = await openDeploy()
    h.click(confirm)
    await h.settle()
    const shown = h.pageText()
    const notSent = /app-a was not sent/.test(shown)
    const didNotReach = /did not reach Coolify/.test(shown)
    const ok = notSent && !didNotReach && !/may have run/.test(shown) && liveSends(app.calls).length === 1
    return { ok, says_not_sent: notSent, says_did_not_reach: didNotReach }
  },

  // A deployment URL from Coolify with a script scheme is not a link and never reaches an opener.
  async link_scheme() {
    const app = boot({ url: 'javascript:globalThis.__ran=1' })
    await h.settle()
    const anchors = h.all(n => n.type === 'a').map(n => String(n.props.href))
    for (const a of h.all(n => n.type === 'a')) h.click(a)
    await h.settle()
    const scripted = anchors.filter(u => /^javascript:/i.test(u))
    const ok = scripted.length === 0 && h.windowOpens.length === 0 && !app.opened.some(u => /^javascript:/i.test(u)) && h.nativeOpens.length === 0
    return { ok, anchors, window_open: h.windowOpens, os_open: app.opened }
  },

  // An https link opens through Desktop's openExternal; where Desktop cannot open it (resolves
  // false, as in the web build) the page does not fall back to window.open, and a short notice
  // names the address to open in a browser.
  async link_no_bridge() {
    const url = 'https://coolify.example.test/project/x/deployment/dep-1'
    const app = boot({ url, openExternal: false })
    await h.settle()
    const link = h.all(n => n.type === 'a' && n.props.href === url)[0]
    if (link) h.click(link)
    await h.settle()
    const notices = h.hostCalls.filter(c => c.name === 'notify').map(c => c.args[0] && c.args[0].message)
    const named = notices.some(m => String(m).includes(url))
    const ok = Boolean(link) && JSON.stringify(app.opened) === JSON.stringify([url]) && h.windowOpens.length === 0 && named
    return { ok, link: Boolean(link), os_open: app.opened, window_open: h.windowOpens, notices }
  },

  // Where Desktop opens the link, nothing else is said.
  async link_opens_quietly() {
    const url = 'https://coolify.example.test/project/x/deployment/dep-1'
    const app = boot({ url })
    await h.settle()
    const link = h.all(n => n.type === 'a' && n.props.href === url)[0]
    if (link) h.click(link)
    await h.settle()
    const notices = h.hostCalls.filter(c => c.name === 'notify')
    const ok = Boolean(link) && JSON.stringify(app.opened) === JSON.stringify([url]) && notices.length === 0 && h.windowOpens.length === 0
    return { ok, os_open: app.opened, notices: notices.length }
  },

  // The status-bar chip alone, with this profile's Hermes half off: GET /setup backs off instead of
  // polling every 4 s (with a retry each) for as long as Desktop runs.
  async chip_backend_off() {
    const app = boot({ mount: ['chip'], route: ({ p }) => (p === '/setup' ? fail(404, { detail: "Plugin 'coolifybruecke' is not enabled in this profile" }) : undefined) })
    await h.advance(10 * 60 * 1000)
    const setup = app.calls.filter(c => c.path === '/setup').length
    const other = app.calls.filter(c => c.path !== '/setup').length
    return { ok: setup <= 6 && other === 0, setup_calls_in_10_min: setup, other_calls: other, chip_text: h.pageText() }
  },

  // The chip alone while the plugin is not set up: once a minute, not every 4 s.
  async chip_needed_idle() {
    const app = boot({ mount: ['chip'], route: ({ p }) => (p === '/setup' ? { ok: true, configured: false, steps: [] } : undefined) })
    await h.advance(10 * 60 * 1000)
    const setup = app.calls.filter(c => c.path === '/setup').length
    return { ok: setup <= 11, setup_calls_in_10_min: setup }
  },

  // With the page open, a value saved in the settings form still shows within seconds.
  async page_needed_fast() {
    let configured = false
    const app = boot({ mount: ['page', 'chip'], route: ({ p }) => (p === '/setup' ? { ok: true, configured, steps: [] } : undefined) })
    await h.advance(1000)
    configured = true
    const before = app.calls.filter(c => c.path === '/setup').length
    await h.advance(5000)
    const after = app.calls.filter(c => c.path === '/setup').length
    const snapshot = app.calls.some(c => c.path === '/snapshot')
    return { ok: after > before && snapshot, setup_calls_in_5_s: after - before, snapshot_loaded: snapshot }
  },

  // The settings page while the Hermes half is off notices it being switched on within seconds.
  async settings_off_fast() {
    let off = true
    const app = boot({ mount: ['settings', 'chip'], route: ({ p }) => (p === '/setup' && off ? fail(404, { detail: "Plugin 'coolifybruecke' is not enabled in this profile" }) : undefined) })
    await h.advance(1000)
    off = false
    const before = app.calls.filter(c => c.path === '/setup').length
    await h.advance(5000)
    const after = app.calls.filter(c => c.path === '/setup').length
    return { ok: after > before, setup_calls_in_5_s: after - before }
  },

  // A followed deployment and an open confirm belong to the profile they started in. After a switch
  // the other profile neither shows them nor polls the first profile's resource; switching back
  // shows the followed row again.
  async profile_switch() {
    const app = boot()
    const confirm = await openDeploy()
    h.click(confirm)
    await h.advance(1000)
    const followedHere = /Deploy app-a/.test(h.pageText())
    h.click(outsideDialog('Deploy'))
    await h.settle()
    const confirmOpenBefore = dialogOpen()
    h.host.state.profile.set('other')
    await h.advance(60 * 1000)
    const inOther = app.calls.filter(c => c.profile === 'other')
    const polledOld = inOther.filter(c => c.path.startsWith('/resources/app-a')).length
    const dryRunsInOther = dryRuns(inOther).length
    const shownInOther = /Deploy app-a/.test(h.pageText())
    const confirmInOther = dialogOpen()
    h.host.state.profile.set('default')
    await h.advance(1000)
    const shownBack = /Deploy app-a/.test(h.pageText())
    const ok = followedHere && confirmOpenBefore && polledOld === 0 && dryRunsInOther === 0 && !shownInOther && !confirmInOther && shownBack
    return { ok, followed_in_default: followedHere, polls_of_app_a_in_other: polledOld, dry_runs_in_other: dryRunsInOther, row_shown_in_other: shownInOther, confirm_shown_in_other: confirmInOther, row_shown_back_in_default: shownBack }
  },

  // The page names a version skew between its two halves once, and stays quiet when GET /setup
  // names the same version or none (a Hermes half older than the field).
  async version_note() {
    let version = '0.0.1'
    boot({ mount: ['page'], route: ({ p }) => (p === '/setup' ? { ok: true, configured: true, steps: [], ...(version === null ? {} : { version }) } : undefined) })
    await h.settle()
    const notes = () => h.all(n => n.props['data-cc-version'] !== undefined)
    const skew = notes().map(h.text)
    version = plugin.version
    await h.advance(61 * 1000)
    const same = notes().length
    version = null
    await h.advance(61 * 1000)
    const missing = notes().length
    const ok = typeof plugin.version === 'string' && skew.length === 1 && skew[0].includes(plugin.version) && skew[0].includes('0.0.1') && same === 0 && missing === 0
    return { ok, page_version: plugin.version, skew_note: skew, notes_when_same: same, notes_without_field: missing }
  },

  // Cancelling a deployment refreshes the open history of its application at once.
  async cancel_refreshes_history() {
    const app = boot({ running: true })
    await h.settle()
    const name = h.all(n => n.type === 'button' && n.props['aria-expanded'] !== undefined && h.text(n) === 'app-a')[0]
    if (!name) throw new Error('no app-a row: ' + h.pageText().slice(0, 300))
    h.click(name)
    await h.advance(10 * 1000)
    const history = () => app.calls.filter(c => c.path.startsWith('/resources/app-a/deployments')).length
    const opened = history()
    h.click(outsideDialog('Cancel'))
    await h.settle()
    const confirm = confirmButton('Cancel deployment app-a')
    if (!confirm || confirm.props.disabled) throw new Error('no cancel confirm: ' + h.pageText().slice(0, 400))
    h.click(confirm)
    await h.settle()
    const after = history() - opened
    const sent = liveSends(app.calls).map(c => c.target)
    return { ok: opened >= 1 && JSON.stringify(sent) === '["dep-run"]' && after >= 1, history_reads_when_opened: opened, history_reads_after_cancel: after, sent }
  },
}

const name = process.argv[2]
if (!cases[name]) {
  console.log('cases:', Object.keys(cases).join(' '))
  process.exit(2)
}
console.info = () => {}
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
