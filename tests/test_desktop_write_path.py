"""The cockpit page's write path, run from the real desktop/plugin.js in node.

desktop_page_harness.mjs stands in for react, react/jsx-runtime and @hermes/plugin-sdk (hooks with
per-instance state, bubbling key and click events with the browser's Enter activation, React
Query's per-key cache, retry and refetchInterval on a virtual clock). desktop_write_path.mjs mounts
the page against a recording ctx.rest and drives it as a user would. Each case runs in its own node
process, because the page keeps module state.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
HERE = Path(__file__).resolve().parent
PLUGIN_JS = ROOT / "desktop" / "plugin.js"
IMPORTS = ("@hermes/plugin-sdk", "react", "react/jsx-runtime")

CASES = {
    "enter_on_cancel": "Enter on a focused Cancel closes the dialog and sends nothing",
    "enter_on_confirm": "Enter on the focused confirm button sends exactly once",
    "bulk_partial": "a partial bulk failure follows what was sent and re-plans only the rest",
    "single_502_sent": "a send that may have landed disarms the confirm until a new dry run",
    "single_502_not_sent": "a send that never reached Coolify says it did not run, never that it may have",
    "single_401_not_sent": "a send refused before it left says it was not sent, not that it did not reach Coolify",
    "link_scheme": "a javascript: deployment URL is no link and reaches no opener",
    "link_no_bridge": "where Desktop cannot open a link, a notice names the address; never window.open",
    "link_opens_quietly": "an https link opens through ctx.os.openExternal with no notice",
    "chip_backend_off": "the chip alone backs off GET /setup while the Hermes half is off",
    "chip_needed_idle": "the chip alone asks GET /setup once a minute while setup is missing",
    "page_needed_fast": "with the page open, a saved setting shows within seconds",
    "settings_off_fast": "with the settings page open, switching the Hermes half on shows within seconds",
    "profile_switch": "followed actions and an open confirm stay with their profile",
    "version_note": "the page names a version skew between its halves, and only then",
    "cancel_refreshes_history": "cancelling a deployment refreshes its application's open history",
}


def test_page_version_matches_manifest():
    manifest = (ROOT / "plugin.yaml").read_text(encoding="utf-8")
    wanted = re.search(r'^version:\s*"?([^"\s]+)"?', manifest, re.M)
    found = re.search(r"^const VERSION = '([^']+)'", PLUGIN_JS.read_text(encoding="utf-8"), re.M)
    assert wanted and found, "plugin.yaml version or desktop/plugin.js VERSION not found"
    assert found.group(1) == wanted.group(1), "desktop/plugin.js VERSION must equal plugin.yaml version"


@pytest.fixture(scope="module")
def page_dir(tmp_path_factory):
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not on PATH")
    js = PLUGIN_JS.read_text(encoding="utf-8")
    for module in IMPORTS:
        assert f"from '{module}'" in js, f"desktop/plugin.js no longer imports {module}"
        js = js.replace(f"from '{module}'", "from './harness.mjs'")
    work = tmp_path_factory.mktemp("coolify-write-path")
    (work / "plugin.mjs").write_text(js, encoding="utf-8")
    shutil.copy(HERE / "desktop_page_harness.mjs", work / "harness.mjs")
    shutil.copy(HERE / "desktop_write_path.mjs", work / "cases.mjs")
    return node, work


def run_case(page_dir, case: str) -> dict:
    node, work = page_dir
    done = subprocess.run([node, "cases.mjs", case], capture_output=True, text=True, timeout=60, cwd=work)
    assert done.returncode == 0, done.stdout + done.stderr
    lines = [line for line in done.stdout.splitlines() if line.startswith("RESULT ")]
    assert len(lines) == 1, done.stdout + done.stderr
    return json.loads(lines[0][len("RESULT "):])


@pytest.mark.parametrize("case", list(CASES))
def test_write_path(page_dir, case):
    result = run_case(page_dir, case)
    assert result["ok"] is True, f"{CASES[case]}: {result}"
