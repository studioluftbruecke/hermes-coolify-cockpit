"""The Connection card's states and "Check again", run from the real desktop/plugin.js in node.

The three imports are swapped for stubs as in test_desktop_sidebar.py. `useQuery` answers from a
table per query key, so each test sets one state of `/setup` and `/capabilities`; the component
tree is expanded by calling each function component with those stubs.

- A click while a plain `/capabilities` fetch is in flight still sends
  `/capabilities?refresh=true`. The query client here keeps React Query 5's two rules that decide
  it: `fetchQuery` on a key with a fetch in flight returns that fetch's promise, and
  `cancelQueries` drops the fetch in flight. Set QUERY_CORE to the path of
  `@tanstack/query-core` to run the same case against the real library.
- The settings card while the Hermes half is off, while `/setup` loads, and when it failed
  with a non-404 shows that state, not the empty card ("Coolify ?").
- A stored but invalid URL names the URL error.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PLUGIN_JS = ROOT / "desktop" / "plugin.js"
MODULES = {"@hermes/plugin-sdk": "./sdk.mjs", "react": "./react.mjs", "react/jsx-runtime": "./jsx.mjs"}

SPECIAL = {
    "atom": """v => {
  const ls = new Set()
  return { get: () => v, set: n => { v = n; ls.forEach(l => l(v)) }, listen: l => (ls.add(l), () => ls.delete(l)) }
}""",
    "useValue": "a => (a && typeof a.get === 'function' ? a.get() : a)",
    "host": "{ navigate: p => { globalThis.__navigated.push(p) }, state: { profile: { get: () => 'p1' } } }",
    "useQuery": "opts => globalThis.__useQuery(opts)",
    "useQueryClient": "() => globalThis.__client",
    "SIDEBAR_NAV_AREA": "'sidebar.nav'",
    "ROUTES_AREA": "'routes'",
    "PALETTE_AREA": "'palette'",
    "STATUSBAR_AREAS": "{ left: 'statusbar.left', right: 'statusbar.right' }",
    "jsx": "(type, props, key) => ({ type, props, key })",
    "jsxs": "(type, props, key) => ({ type, props, key })",
}
REACT = {
    "useState": "v => [typeof v === 'function' ? v() : v, () => {}]",
    "useEffect": "() => {}",
    "useMemo": "f => f()",
    "useRef": "v => ({ current: v })",
    "Component": "class { constructor(props) { this.props = props; this.state = {} } }",
}

RUN = r"""
globalThis.__navigated = []
const sent = []
let settings
const delay = { '/setup': 5, '/capabilities': 60, '/capabilities?refresh=true': 5 }
const answers = {
  '/setup': { ok: true, configured: true, steps: [] },
  '/capabilities': { ok: true, from: 'shared check' },
  '/capabilities?refresh=true': { ok: true, from: 'fresh check' },
}
const ctx = {
  register: () => () => {},
  registerSettingsPage: p => { settings = p; return () => {} },
  storage: { get: (k, f) => f, set: () => {}, remove: () => {} },
  os: { openExternal: async () => true },
  rest: path => { sent.push(path); return new Promise(r => setTimeout(() => r(answers[path]), delay[path] || 1)) },
}
const { default: plugin } = await import('./plugin.mjs')
plugin.register(ctx)

// React Query 5: a fetch in flight is shared per key; cancelQueries drops it (prefix match).
class FakeClient {
  constructor() { this.inflight = new Map() }
  fetchQuery({ queryKey, queryFn }) {
    const k = JSON.stringify(queryKey)
    if (this.inflight.has(k)) return this.inflight.get(k)
    const p = queryFn().finally(() => { if (this.inflight.get(k) === p) this.inflight.delete(k) })
    this.inflight.set(k, p)
    return p
  }
  async cancelQueries({ queryKey }) {
    const prefix = JSON.stringify(queryKey).slice(0, -1)
    for (const k of [...this.inflight.keys()]) if (k === prefix + ']' || k.startsWith(prefix + ',')) this.inflight.delete(k)
  }
  invalidateQueries() { return Promise.resolve() }
}
async function makeClient() {
  if (!process.env.QUERY_CORE) return { client: new FakeClient(), library: 'fake' }
  const { QueryClient } = await import(process.env.QUERY_CORE)
  return { client: new QueryClient(), library: 'query-core' }
}

// Render: call every function component with the stubs, keep host elements.
const expand = n => {
  if (n == null || typeof n !== 'object') return n
  if (Array.isArray(n)) return n.map(expand)
  if (typeof n.type === 'function') return expand(n.type(n.props || {}))
  const children = n.props && n.props.children
  return children === undefined ? n : { ...n, props: { ...n.props, children: expand(children) } }
}
const texts = n => (n == null || typeof n !== 'object' ? (typeof n === 'string' ? [n] : []) : Array.isArray(n) ? n.flatMap(texts) : texts(n.props && n.props.children))
const find = (n, test) => (n == null || typeof n !== 'object' ? [] : Array.isArray(n) ? n.flatMap(c => find(c, test)) : [...(test(n) ? [n] : []), ...find(n.props && n.props.children, test)])

// The settings page's own card, located in the page tree (not an export).
const pageTree = (el => el.type(el.props))(settings.render())
const widgetEl = find(pageTree, n => typeof n.type === 'function' && n.type.name === 'ConnectionWidget')[0]

const idle = { isLoading: false, error: null, data: undefined }
function card(setup, caps) {
  globalThis.__useQuery = opts => (opts.queryKey.at(-1) === 'setup' ? setup : opts.enabled === false ? idle : caps)
  const tree = expand(widgetEl)
  return { tree, text: texts(tree).join('|') }
}
const err = (status, message) => Object.assign(new Error(message), { status, body: null })
const step = (id, extra) => ({ id, set: false, ...extra })
const out = {}

// The settings card's off, loading and failed setup states.
let c = card({ isLoading: false, data: undefined, error: err(404, "plugin 'coolifybruecke' is not enabled in this profile") }, idle)
out.off = { ok: c.text.includes('Turn on the plugin in this profile.') && c.text.includes('Switch on Coolifybrücke in Capabilities, Plugins.') && !c.text.includes('Coolify ?'), text: c.text }
c = card({ isLoading: true, data: undefined, error: null }, idle)
out.setup_loading = { ok: find(c.tree, n => n.props && n.props['data-cc-setup-result'] === 'loading').length === 1 && !c.text.includes('Coolify ?'), text: c.text }
c = card({ isLoading: false, data: undefined, error: err(500, 'Internal Server Error') }, idle)
out.setup_failed = { ok: c.text.includes('Setup: Internal Server Error') && c.text.includes('Check again') && !c.text.includes('Coolify ?'), text: c.text }

// A stored but invalid URL is named, and each not-set-up line keeps its exact wording.
const needed = steps => card({ isLoading: false, error: null, data: { ok: true, configured: false, steps } }, idle).text
const line = t => t.split('|').find(s => s.startsWith('Not set up')) || t
out.url_error = { ok: false, text: line(needed([step('url', { value: 'coolify.example', error: 'the Coolify URL must start with http:// or https:// and name a host' }), step('action_token', { set: true }), step('read_token')])) }
out.url_error.ok = out.url_error.text === 'Not set up: the Coolify URL must start with http:// or https:// and name a host. Fix it in the plugin settings.'
out.url_error_no_token = { text: line(needed([step('url', { value: 'x', error: 'the Coolify URL must start with http:// or https:// and name a host' }), step('action_token'), step('read_token')])) }
out.url_error_no_token.ok = out.url_error_no_token.text === 'Not set up: the Coolify URL must start with http:// or https:// and name a host. Fix it and add a token in the plugin settings.'
out.no_token = { text: line(needed([step('url', { set: true, value: 'https://c.example' }), step('action_token'), step('read_token')])) }
out.no_token.ok = out.no_token.text === 'Not set up: add a token in the plugin settings.'
out.nothing = { text: line(needed([step('url'), step('action_token'), step('read_token')])) }
out.nothing.ok = out.nothing.text === 'Not set up: add the Coolify URL and a token in the plugin settings.'

// "Check again" while the page's plain fetch is in flight.
const { client, library } = await makeClient()
globalThis.__client = client
c = card({ isLoading: false, error: null, data: { ok: true, configured: true, steps: [] } }, { isLoading: false, error: null, data: { setup: { line: 'x', tone: 'good' } } })
const button = find(c.tree, n => n.type === 'Button' && texts(n).includes('Check again'))[0]
const key = ['coolifybruecke', 'p1', 'capabilities']
const plain = client.fetchQuery({ queryKey: key, queryFn: () => ctx.rest('/capabilities'), staleTime: 0 }).catch(e => 'cancelled')
await button.props.onClick()
await plain
await new Promise(r => setTimeout(r, 100))
out.inflight_click = { ok: sent.includes('/capabilities?refresh=true'), sent, library }

process.stdout.write('\nRESULT ' + JSON.stringify(out) + '\n')
"""

CASES = ["off", "setup_loading", "setup_failed", "url_error", "url_error_no_token", "no_token", "nothing", "inflight_click"]


def _imports(js: str, module: str) -> list[str]:
    found = re.search(r"import\s*\{([^}]*)\}\s*from\s*'" + re.escape(module) + "'", js)
    assert found, f"desktop/plugin.js imports nothing from {module}"
    return [name.strip() for name in found.group(1).split(",") if name.strip()]


def _stub(names: list[str], special: dict) -> str:
    lines = []
    for name in names:
        value = special.get(name)
        if value is None:
            value = repr(name) if name[0].isupper() else "() => { throw new Error('not stubbed: " + name + "') }"
        lines.append(f"export const {name} = {value}")
    return "\n".join(lines) + "\n"


@pytest.fixture(scope="module")
def results(tmp_path_factory):
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not on PATH")
    js = PLUGIN_JS.read_text(encoding="utf-8")
    work = tmp_path_factory.mktemp("coolify-connection")
    for module, local in MODULES.items():
        special = REACT if module == "react" else SPECIAL
        (work / local[2:]).write_text(_stub(_imports(js, module), special), encoding="utf-8")
        js = js.replace(f"from '{module}'", f"from '{local}'")
    (work / "plugin.mjs").write_text(js, encoding="utf-8")
    (work / "run.mjs").write_text(RUN, encoding="utf-8")
    done = subprocess.run([node, str(work / "run.mjs")], capture_output=True, text=True, timeout=60, cwd=work)
    assert done.returncode == 0, done.stderr
    lines = [line for line in done.stdout.splitlines() if line.startswith("RESULT ")]
    assert len(lines) == 1, done.stdout
    print("RESULTS", lines[0][len("RESULT "):])  # shown with pytest -s
    return json.loads(lines[0][len("RESULT "):])


@pytest.mark.parametrize("case", CASES)
def test_connection_card(results, case):
    assert results[case]["ok"] is True, f"{case}: {results[case]}"
