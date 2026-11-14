"""The Apps rows' Start and Stop buttons, run from the real desktop/plugin.js in node.

desktop_page_harness.mjs stands in for react, react/jsx-runtime and @hermes/plugin-sdk.
desktop_app_rows.mjs mounts the cockpit page against fixed route answers and asserts which buttons
each measured status class shows (milestone SS-1; the vocabulary comes from the SS-0 measurement on
the live instance). Each case runs in its own node process, because the page keeps module state.
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
    "vocabulary": "the classifier sorts every measured status into running, stopped or unknown",
    "buttons_running": "a running row shows Stop and no Start",
    "buttons_stopped": "a stopped row shows Start and no Stop",
    "buttons_unknown": "a row of unknown state shows neither Stop nor Start",
    "start_hidden_while_deploying": "a stopped row mid-deployment shows no Start",
    "live_state_stopped": "against the real measured state, the stopped staging row shows Start and no Stop",
    "live_state_running": "against the real measured state, the running staging row shows Stop and no Start",
    "tones": "a stopped row's dot shows stopped, an unhealthy running row's a warning",
}


@pytest.fixture(scope="module")
def page_dir(tmp_path_factory):
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not on PATH")
    js = PLUGIN_JS.read_text(encoding="utf-8")
    for module in IMPORTS:
        assert f"from '{module}'" in js, f"desktop/plugin.js no longer imports {module}"
        js = js.replace(f"from '{module}'", "from './harness.mjs'")
    work = tmp_path_factory.mktemp("coolify-app-rows")
    (work / "plugin.mjs").write_text(js, encoding="utf-8")
    shutil.copy(HERE / "desktop_page_harness.mjs", work / "harness.mjs")
    shutil.copy(HERE / "desktop_app_rows.mjs", work / "cases.mjs")
    shutil.copy(HERE / "fixtures" / "ss3-live-state.json", work / "ss3-live-state.json")
    return node, work


def run_case(page_dir, case: str) -> dict:
    node, work = page_dir
    done = subprocess.run([node, "cases.mjs", case], capture_output=True, text=True, timeout=60, cwd=work)
    assert done.returncode == 0, done.stdout + done.stderr
    lines = [line for line in done.stdout.splitlines() if line.startswith("RESULT ")]
    assert len(lines) == 1, done.stdout + done.stderr
    return json.loads(lines[0][len("RESULT "):])


@pytest.mark.parametrize("case", list(CASES))
def test_app_rows(page_dir, case):
    result = run_case(page_dir, case)
    assert result["ok"] is True, f"{CASES[case]}: {result}"
