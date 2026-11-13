"""Behaviour: the settings card's alert status poll does not start a `hermes cron list` child on
every call, and a change made through `alerts on` / `off` shows at once.
"""

import json
import os
import stat
import sys

CRON_LIST = """\
┌─────────────────────────────────────────────────────────────────────────┐
│                         Scheduled Jobs                                  │
└─────────────────────────────────────────────────────────────────────────┘
"""


def _fake_hermes(tmp_path, monkeypatch):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    record = tmp_path / "calls.jsonl"
    fake = bindir / "hermes"
    fake.write_text(f"""#!{sys.executable}
import json, sys
with open({str(record)!r}, "a") as f:
    f.write(json.dumps(sys.argv[1:]) + "\\n")
if sys.argv[1:3] == ["cron", "create"]:
    print("Created job: job123")
    print("  Schedule: every 5m")
elif sys.argv[1:3] == ["cron", "list"]:
    print({CRON_LIST!r})
""")
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{bindir}:{os.environ['PATH']}")
    return lambda: [json.loads(line) for line in record.read_text().splitlines()] if record.exists() else []


def test_repeated_status_polls_start_one_cron_child(plugin, tmp_path, monkeypatch):
    calls = _fake_hermes(tmp_path, monkeypatch)
    data = tmp_path / "data"
    data.mkdir()
    for _ in range(3):
        status, body = plugin.api_alerts(data_dir=data)
        assert status == 200 and body["ok"]
    lists = [c for c in calls() if c[:2] == ["cron", "list"]]
    assert len(lists) == 1, lists


def test_alerts_off_shows_in_the_next_status_poll(plugin, tmp_path, monkeypatch):
    calls = _fake_hermes(tmp_path, monkeypatch)
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    plugin.api_alerts()
    plugin.alerts_off(home=home)
    plugin.api_alerts()
    lists = [c for c in calls() if c[:2] == ["cron", "list"]]
    assert len(lists) == 3, "status, off's own list, and a fresh status after the change"
