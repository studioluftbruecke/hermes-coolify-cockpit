"""0.2.1: Coolifybrücke's sidebar row and its Sidebar section, run from the real desktop/plugin.js.

tests/test_sidebar_row.py proves the shared module on its own. This file loads the plugin's own
Desktop half in node, with the three imports (`@hermes/plugin-sdk`, `react`, `react/jsx-runtime`)
swapped for small stubs, calls its `register(ctx)` against a fake ctx whose `register` replaces an
entry by id (as Desktop's registry does), and checks what this plugin wires up:

- one sidebar row, id `nav`, labelled "Coolifybrücke", codicon `dashboard`, path `/coolifybruecke`,
  at 54 with nothing stored (D3), at the stored position, and none when stored hidden;
- the palette row "Open Coolifybrücke" stays registered while the row is hidden, so the hidden hint
  names a command that exists;
- the settings page "Coolifybrücke" renders the Sidebar section bound to that same row: the switch
  hides the row and stores the choice, the field moves it, the hint names the palette row;
- the chat tab title stays "Cockpit" (brand rename D5).

Run from the plugin folder: python3 -m pytest tests -q --import-mode=importlib
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
    "host": "{ navigate: p => { globalThis.__navigated.push(p) }, state: {} }",
    "SIDEBAR_NAV_AREA": "'sidebar.nav'",
    "ROUTES_AREA": "'routes'",
    "PALETTE_AREA": "'palette'",
    "STATUSBAR_AREAS": "{ left: 'statusbar.left', right: 'statusbar.right' }",
    "jsx": "(type, props, key) => ({ type, props, key })",
    "jsxs": "(type, props, key) => ({ type, props, key })",
}

RUN = r"""
globalThis.__navigated = []
const { default: plugin } = await import('./plugin.mjs')

function fakeCtx(stored) {
  const rows = []
  const pages = []
  const store = new Map(stored === undefined ? [] : [['sidebar', JSON.stringify(stored)]])
  return {
    rows, pages,
    stored: () => (store.has('sidebar') ? JSON.parse(store.get('sidebar')) : undefined),
    storage: { get: (k, f) => (store.has(k) ? JSON.parse(store.get(k)) : f), set: (k, v) => store.set(k, JSON.stringify(v)) },
    register(c) {
      const i = rows.findIndex(r => r.id === c.id)
      if (i >= 0) rows.splice(i, 1)
      rows.push(c)
      return () => { const j = rows.findIndex(r => r.id === c.id); if (j >= 0) rows.splice(j, 1) }
    },
    registerSettingsPage(p) { pages.push(p); return () => {} },
  }
}
const nav = ctx => ctx.rows.filter(r => r.area === 'sidebar.nav')
const view = ctx => nav(ctx).map(r => `${r.id}@${r.order}:${r.data.label}:${r.data.codicon}:${r.data.path}`).join(' ')
const texts = n => (n == null || typeof n !== 'object' ? (typeof n === 'string' ? [n] : []) : Array.isArray(n) ? n.flatMap(texts) : texts(n.props && n.props.children))
const find = (n, test) => (n == null || typeof n !== 'object' ? [] : Array.isArray(n) ? n.flatMap(c => find(c, test)) : [...(test(n) ? [n] : []), ...find(n.props && n.props.children, test)])
const out = {}
let ctx

ctx = fakeCtx(); plugin.register(ctx)
out.default_row = view(ctx) === 'nav@54:Coolifybrücke:dashboard:/coolifybruecke'
out.one_nav_row = nav(ctx).length === 1 && ctx.stored() === undefined

ctx = fakeCtx({ show: true, position: 12 }); plugin.register(ctx)
out.stored_position = view(ctx) === 'nav@12:Coolifybrücke:dashboard:/coolifybruecke'

ctx = fakeCtx({ show: false, position: 54 }); plugin.register(ctx)
const palette = ctx.rows.find(r => r.area === 'palette')
palette.data.run()
out.hidden_keeps_palette = nav(ctx).length === 0 && palette.data.label === 'Open Coolifybrücke'
  && globalThis.__navigated.at(-1) === '/coolifybruecke'

// The settings page: the Sidebar section is bound to the row register() made.
ctx = fakeCtx(); plugin.register(ctx)
const page = ctx.pages.find(p => p.id === 'settings')
out.settings_entry = Boolean(page) && page.title === 'Coolifybrücke' && page.icon === 'dashboard'
const pageEl = page.render()
const settingsTree = pageEl.type(pageEl.props)
const sections = find(settingsTree, n => n.props && n.props.row && n.props.sdk)
out.one_section = sections.length === 1 && sections[0].props.sdk.SIDEBAR_NAV_AREA === 'sidebar.nav'
const sectionEl = sections[0]
const render = () => sectionEl.type(sectionEl.props)
let section = render()
const shown = texts(section)
out.section_texts = shown.join('|') === 'Sidebar|Show in the sidebar|Position|Lower numbers sit higher. Kanban is 50.'
  && section.props['data-sidebar-section'] === 'Coolifybrücke'
find(section, n => n.type === 'Switch')[0].props.onCheckedChange(false)
section = render()
const hint = texts(section).find(t => t.startsWith('Open it from the command palette'))
const paletteLabel = ctx.rows.find(r => r.area === 'palette').data.label
out.switch_hides = nav(ctx).length === 0 && ctx.stored().show === false && find(section, n => n.type === 'Switch')[0].props.checked === false
out.hint_names_palette_row = hint === `Open it from the command palette: ${paletteLabel}.` && paletteLabel === 'Open Coolifybrücke'
const field = find(section, n => n.type === 'Input')[0]
const target = { value: '3' }
field.props.onKeyDown({ key: 'Enter', target })
find(render(), n => n.type === 'Switch')[0].props.onCheckedChange(true)
out.field_moves = view(ctx) === 'nav@3:Coolifybrücke:dashboard:/coolifybruecke' && JSON.stringify(ctx.stored()) === '{"show":true,"position":3}'

process.stdout.write('\nRESULT ' + JSON.stringify(out) + '\n')
"""

CASES = ["default_row", "one_nav_row", "stored_position", "hidden_keeps_palette", "settings_entry", "one_section",
         "section_texts", "switch_hides", "hint_names_palette_row", "field_moves"]


def _imports(js: str, module: str) -> list[str]:
    found = re.search(r"import\s*\{([^}]*)\}\s*from\s*'" + re.escape(module) + "'", js)
    assert found, f"desktop/plugin.js imports nothing from {module}"
    return [name.strip() for name in found.group(1).split(",") if name.strip()]


def _stub(names: list[str]) -> str:
    lines = []
    for name in names:
        value = SPECIAL.get(name)
        if value is None:
            # Components are compared by name (the tree is never mounted); hooks are never called
            # outside a component, and register() renders nothing.
            value = repr(name) if name[0].isupper() else "() => { throw new Error('not stubbed: " + name + "') }"
        lines.append(f"export const {name} = {value}")
    return "\n".join(lines) + "\n"


@pytest.fixture(scope="module")
def results(tmp_path_factory):
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not on PATH")
    js = PLUGIN_JS.read_text(encoding="utf-8")
    work = tmp_path_factory.mktemp("coolify-sidebar")
    for module, local in MODULES.items():
        (work / local[2:]).write_text(_stub(_imports(js, module)), encoding="utf-8")
        js = js.replace(f"from '{module}'", f"from '{local}'")
    (work / "plugin.mjs").write_text(js, encoding="utf-8")
    (work / "run.mjs").write_text(RUN, encoding="utf-8")
    done = subprocess.run([node, str(work / "run.mjs")], capture_output=True, text=True, timeout=60, cwd=work)
    assert done.returncode == 0, done.stderr
    lines = [line for line in done.stdout.splitlines() if line.startswith("RESULT ")]
    assert len(lines) == 1, done.stdout  # the plugin logs registration to the console too
    return json.loads(lines[0][len("RESULT "):])


@pytest.mark.parametrize("case", CASES)
def test_sidebar_row_and_section(results, case):
    assert results.get(case) is True, f"{case}: {results}"


def test_chat_tab_title_stays_cockpit():
    assert re.search(r"^const SESSION_TITLE = 'Cockpit'$", PLUGIN_JS.read_text(encoding="utf-8"), re.M)
