"""The status-bar chip's show switch, run from the real desktop/plugin.js in node.

desktop_page_harness.mjs stands in for react, react/jsx-runtime and @hermes/plugin-sdk (class
components and error boundaries included). desktop_statusbar.mjs registers the plugin against a ctx
whose register() returns a real disposer and checks:

- the chip is a status-bar data item with the stable id `coolifybruecke.chip` and toggleLabel
  "Coolifybrücke" (so Desktop's right-click menu lists it), at order 70, with no top-level render;
- show_in_statusbar false disposes it and nothing is registered; true registers it again once;
- a stored "off" registers nothing at start, and a failed read changes nothing;
- the chip's own error boundary catches a chip that throws, and the bar's other items stay.

Each case runs in its own node process, because the plugin keeps module state.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
HERE = Path(__file__).resolve().parent
PLUGIN_JS = ROOT / "desktop" / "plugin.js"
IMPORTS = ("@hermes/plugin-sdk", "react", "react/jsx-runtime")

CASES = {
    "shown_by_default": "the chip is one data item with the stable id and the brand as toggleLabel",
    "switch_off_then_on": "the setting off disposes the chip, on registers it again once",
    "stored_off_no_flash": "a stored off shows nothing at start; a failed read changes nothing",
    "boundary_catches": "the chip's own error boundary catches a throwing chip",
}


def test_manifest_declares_the_switch():
    manifest = (ROOT / "plugin.yaml").read_text(encoding="utf-8")
    block = manifest.split("  show_in_statusbar:\n", 1)
    assert len(block) == 2, "plugin.yaml config_schema has no show_in_statusbar"
    lines = block[1].split("\n")[:4]
    assert lines[:3] == ["    type: boolean", "    default: true", '    label: "Show in status bar"'], lines


@pytest.fixture(scope="module")
def bar_dir(tmp_path_factory):
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not on PATH")
    js = PLUGIN_JS.read_text(encoding="utf-8")
    for module in IMPORTS:
        assert f"from '{module}'" in js, f"desktop/plugin.js no longer imports {module}"
        js = js.replace(f"from '{module}'", "from './harness.mjs'")
    work = tmp_path_factory.mktemp("coolify-statusbar")
    (work / "plugin.mjs").write_text(js, encoding="utf-8")
    shutil.copy(HERE / "desktop_page_harness.mjs", work / "harness.mjs")
    shutil.copy(HERE / "desktop_statusbar.mjs", work / "cases.mjs")
    return node, work


@pytest.mark.parametrize("case", list(CASES))
def test_statusbar(bar_dir, case):
    node, work = bar_dir
    done = subprocess.run([node, "cases.mjs", case], capture_output=True, text=True, timeout=60, cwd=work)
    assert done.returncode == 0, done.stdout + done.stderr
    lines = [line for line in done.stdout.splitlines() if line.startswith("RESULT ")]
    assert len(lines) == 1, done.stdout + done.stderr
    result = json.loads(lines[0][len("RESULT "):])
    assert result["ok"] is True, f"{CASES[case]}: {result}"
