"""Copy rules (PLAN-v0.1 -> Copy rules) that a static read of the shipped files can hold.

The rendered-page length test lives in the off-device harness (experiments); these checks keep the
manifest, the Desktop half's own description and the settings entry honest on every commit.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MANIFEST_LIMIT = 160


def _manifest_description() -> str:
    text = (ROOT / "plugin.yaml").read_text(encoding="utf-8")
    found = re.search(r'^description:\s*"(.*)"\s*$', text, re.M)
    assert found, "plugin.yaml has no one-line quoted description"
    return found.group(1)


def _plugin_js() -> str:
    return (ROOT / "desktop" / "plugin.js").read_text(encoding="utf-8")


def test_manifest_description_is_short():
    description = _manifest_description()
    assert 40 <= len(description) <= MANIFEST_LIMIT, len(description)


def test_desktop_description_is_short():
    found = re.search(r"^  description: '([^']*)',$", _plugin_js(), re.M)
    assert found, "desktop/plugin.js default export has no description"
    assert len(found.group(1)) <= MANIFEST_LIMIT, len(found.group(1))


def test_settings_entry_has_the_sidebar_name_and_icon():
    # 0.2.1: the row comes from the shared sidebar-row module (tests/test_sidebar_row.py checks the
    # copy and the call); the settings entry carries the same brand and codicon.
    js = _plugin_js()
    nav = re.search(r"startSidebarRow\(ctx, \{ brand: '([^']+)', path: PATH, codicon: '([a-z-]+)'", js)
    assert nav, "sidebar row not found"
    page = re.search(r"ctx\.registerSettingsPage\(\{ id: '[a-z-]+', title: NAV_LABEL, icon: '([a-z-]+)'", js)
    assert page, "the settings entry must be registered with the sidebar's NAV_LABEL"
    assert page.group(1) == nav.group(2), (page.group(1), nav.group(2))
    assert re.search(r"^const NAV_LABEL = 'Coolifybrücke'$", js, re.M)
    assert nav.group(1) == "Coolifybrücke"


# --- 0.1.0 M8: no sentence over 120 characters in the shipped copy ------------------------------
#
# The off-device render checks the rendered default page (experiments/.../harness, run.cjs). These
# two checks hold the same limit on every commit over the strings that copy is made of: the Desktop
# half's string literals (class names, log lines and the chat drafts `*_PROMPT` excluded) and the
# registries the page shows (attention signals, feature rules).

SENTENCE_LIMIT = 120
_SENTENCE = re.compile(r"(?<=[.!?])\s+")


def _long(texts):
    return [(where, s) for where, text in texts for s in _SENTENCE.split(text) if len(s) > SENTENCE_LIMIT]


def test_no_sentence_over_120_characters_in_the_desktop_strings():
    texts = []
    for number, line in enumerate(_plugin_js().splitlines(), 1):
        stripped = line.lstrip()
        if stripped.startswith("//") or "className" in line or "_PROMPT =" in line or "log(" in line:
            continue
        texts += [(number, lit) for lit in re.findall(r"'((?:[^'\\]|\\.)*)'", line) if " " in lit]
    assert _long(texts) == []


def test_no_sentence_over_120_characters_in_the_registries():
    import importlib
    import importlib.util
    import sys
    name = "coolifybruecke_copy_under_test"
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, ROOT / "__init__.py", submodule_search_locations=[str(ROOT)])
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
    fleet = importlib.import_module(name + ".fleet")
    caps = importlib.import_module(name + ".capabilities")
    texts = [(s.id, s.label) for s in fleet.SIGNALS] + [(s.id, s.description) for s in fleet.SIGNALS]
    for rule in caps.FEATURE_RULES:
        texts += [(rule.id, t) for t in (rule.label, rule.reason_if_missing, rule.unlock_hint,
                                          rule.reason_if_scoped, rule.unlock_if_scoped) if t]
    assert _long(texts) == []
