"""0.1.0 M6: the alert job (alerts.py) and the tick behind it (__init__.tick_report, the CLI).

The tick is driven through the same fake Coolify the snapshot tests use, so the Attention list it
diffs is the one the page and the agent read.
"""

import importlib
import importlib.util
import json
import pathlib
import sys
import types

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import test_snapshot as ts  # noqa: E402  (the fake Coolify and its fixture)


@pytest.fixture
def m():
    pkg = ts._plugin()
    return types.SimpleNamespace(pkg=pkg, alerts=importlib.import_module(pkg.__name__ + ".alerts"))


def item(key, title, severity="bad", fix=None, uuid=None):
    signal = key.split(":", 1)[0]
    out = {"key": key, "signal": signal, "severity": severity, "title": title}
    if fix:
        out["fix"] = fix
    if uuid:
        out["resource_uuid"] = uuid
    return out


# --- diff and render ----------------------------------------------------------------------------

def test_an_item_is_announced_once_and_recovers_once(m):
    a = m.alerts
    failing = item("failing:app-1", "shop (staging) failed its last deploy", fix="rollback", uuid="app-1")
    new, rec, state = a.diff([failing], None, key="k", now=10.0)
    assert [x["key"] for x in new] == ["failing:app-1"] and rec == []
    assert a.render(new, rec, host="coolify.example.test") == (
        "Coolifybrücke · coolify.example.test\n✗ shop (staging) failed its last deploy → Roll back\n")
    new, rec, state = a.diff([failing], state, key="k", now=20.0)
    assert new == [] and rec == [] and a.render(new, rec, host="h") == ""  # nothing new prints nothing
    new, rec, state = a.diff([], state, key="k", now=30.0)
    assert new == [] and [x["title"] for x in rec] == ["shop (staging) failed its last deploy"]
    assert a.render(new, rec, host="h").splitlines()[1] == "✓ Recovered: shop (staging) failed its last deploy"
    assert state["open"] == {}


def test_an_item_stays_open_while_its_app_deploys_or_fails(m):
    a = m.alerts
    failing = item("failing:app-1", "shop failed", uuid="app-1")
    _, _, state = a.diff([failing], None, key="k")
    new, rec, state = a.diff([], state, key="k", held=["app-1"])  # a redeploy is running
    assert new == [] and rec == [] and "failing:app-1" in state["open"]
    new, rec, state = a.diff([], state, key="k", held=[])
    assert [x["title"] for x in rec] == ["shop failed"]
    ahead = item("behind_branch:app-1", "shop: main is ahead", severity="warn", uuid="app-1")
    _, _, state = a.diff([ahead], state, key="k")
    new, rec, state = a.diff([failing], state, key="k", held=["app-1"])  # its deploy failed: still ahead
    assert [x["key"] for x in new] == ["failing:app-1"] and rec == []
    new, rec, state = a.diff([], state, key="k", held=[])  # the head deployed: both clear
    assert sorted(x["title"] for x in rec) == ["shop failed", "shop: main is ahead"]


def test_a_flickering_status_needs_two_runs_to_alert_and_two_to_recover(m):
    a = m.alerts
    sick = item("unhealthy:app-2", "agent (production) is running:unhealthy", fix="restart", uuid="app-2")
    confirm = {"unhealthy": 2}
    new, rec, state = a.diff([sick], None, key="k", confirm=confirm)
    assert new == [] and state["open"] == {} and state["pending"]["unhealthy:app-2"]["seen"] == 1
    new, rec, state = a.diff([], state, key="k", confirm=confirm)  # it flickered back: never announced
    assert new == rec == [] and state["pending"] == {}
    _, _, state = a.diff([sick], state, key="k", confirm=confirm)
    new, rec, state = a.diff([sick], state, key="k", confirm=confirm)
    assert [x["key"] for x in new] == ["unhealthy:app-2"]
    new, rec, state = a.diff([], state, key="k", confirm=confirm)
    assert rec == [] and state["open"]["unhealthy:app-2"]["missed"] == 1
    new, rec, state = a.diff([sick], state, key="k", confirm=confirm)  # back again: no second alert
    assert new == rec == [] and state["open"]["unhealthy:app-2"]["missed"] == 0
    _, _, state = a.diff([], state, key="k", confirm=confirm)
    new, rec, state = a.diff([], state, key="k", confirm=confirm)
    assert [x["title"] for x in rec] == ["agent (production) is running:unhealthy"]


def test_another_instance_starts_a_fresh_state(m, tmp_path):
    a = m.alerts
    path = tmp_path / a.STATE_FILE
    a.save_state(path, {"key": "k1", "open": {"x:-": {"title": "t"}}, "checked": 1})
    assert a.load_state(path, "k1")["open"] == {"x:-": {"title": "t"}}
    assert a.load_state(path, "k2") is None


# --- the tick, end to end on the fake instance --------------------------------------------------

def test_tick_announces_then_stays_quiet_then_recovers(m, tmp_path):
    envs = "GET /api/v1/applications/app-shop-web/envs"
    ts.FIXTURE["teamwide"]["rest"][envs] = [200, [{"key": "K", "updated_at": "2026-11-06T08:00:00.000000Z"}]]
    try:
        inst = ts.Instance({"tok-action": "scoped", "tok-read": "teamwide"})
        kw = dict(transport=inst, cache_file=None, data_dir=tmp_path)
        snap_mod = sys.modules[m.pkg.__name__ + ".snapshot"]
        text, facts = m.pkg.tick_report(ts.settings(), ts.secrets("tok-action", "tok-read"),
                                        cache=snap_mod.SnapshotCache(), **kw)
        assert facts["first_run"] is True and facts["new"] == 1
        assert text.splitlines() == ["Coolifybrücke · coolify.example.test",
                                     "! shop-web (production): env changed after its last deploy → Deploy"]
        text, facts = m.pkg.tick_report(ts.settings(), ts.secrets("tok-action", "tok-read"),
                                        cache=snap_mod.SnapshotCache(), **kw)
        assert text == "" and facts["new"] == 0 and facts["open"] == 1
    finally:
        del ts.FIXTURE["teamwide"]["rest"][envs]
    # the env row is gone (stored answer expired and re-read): recovered
    env_file = tmp_path / "env-check.json"
    env_file.unlink()
    text, facts = m.pkg.tick_report(ts.settings(), ts.secrets("tok-action", "tok-read"),
                                    cache=snap_mod.SnapshotCache(), **kw)
    assert text.splitlines()[1:] == ["✓ Recovered: shop-web (production): env changed after its last deploy"]
    assert json.loads((tmp_path / m.alerts.STATE_FILE).read_text())["open"] == {}


def test_tick_peek_leaves_the_state_alone(m, tmp_path):
    inst = ts.Instance({"tok-read": "teamwide"})
    snap_mod = sys.modules[m.pkg.__name__ + ".snapshot"]
    m.pkg.tick_report(ts.settings(), ts.secrets("tok-read", None), peek=True, transport=inst,
                      cache=snap_mod.SnapshotCache(), cache_file=None, data_dir=tmp_path)
    assert not (tmp_path / m.alerts.STATE_FILE).exists()


def test_tick_on_an_unreachable_coolify_says_so_once_and_keeps_open_items(m, tmp_path):
    a = m.alerts
    path = tmp_path / a.STATE_FILE
    snap_mod = sys.modules[m.pkg.__name__ + ".snapshot"]
    caps = sys.modules[m.pkg.__name__ + ".capabilities"]
    key = caps.connection_from_settings(ts.BASE, "tok-read", None).base_url
    a.save_state(path, {"key": key, "open": {"failing:app-1": {"title": "shop failed", "resource_uuid": "app-1"}},
                        "checked": 1})
    down = ts.Instance({"tok-read": "teamwide"}, unreachable=True)
    kw = dict(transport=down, cache_file=None, data_dir=tmp_path)
    text, facts = m.pkg.tick_report(ts.settings(), ts.secrets("tok-read", None), cache=snap_mod.SnapshotCache(), **kw)
    assert text.splitlines()[1:] == ["✗ Coolify is not reachable"]
    assert set(json.loads(path.read_text())["open"]) == {"failing:app-1", "coolify_unreachable:-"}
    text, _ = m.pkg.tick_report(ts.settings(), ts.secrets("tok-read", None), cache=snap_mod.SnapshotCache(), **kw)
    assert text == ""


def test_tick_without_settings_prints_nothing(m, tmp_path):
    text, facts = m.pkg.tick_report(lambda k, d=None: d, lambda n: None, data_dir=tmp_path)
    assert text == "" and facts["ok"] is False and facts["error"] == "not_configured"


# --- the job: shim, on, off, status -------------------------------------------------------------

class FakeCron:
    def __init__(self):
        self.jobs = []

    def list_jobs(self, include_disabled=False):
        return list(self.jobs)

    def create_job(self, prompt, schedule, **kw):
        job = {"id": f"job{len(self.jobs) + 1}", "prompt": prompt, "schedule_display": f"every {schedule}",
               "enabled": True, **kw}
        self.jobs.append(job)
        return job

    def remove_job(self, job_id):
        self.jobs = [j for j in self.jobs if j["id"] != job_id]
        return True


def test_alerts_on_is_idempotent_and_off_removes_job_and_script(m, tmp_path, monkeypatch):
    cron = FakeCron()
    cron.jobs.append({"id": "other", "script": "backup.py", "enabled": True})
    monkeypatch.setattr(m.pkg, "_cron_jobs", lambda: cron)
    out = m.pkg.alerts_on("5m", "local", home=tmp_path)
    shim = tmp_path / "scripts" / m.alerts.SCRIPT_NAME
    assert shim.exists() and "coolifybruecke\", \"tick\"" in shim.read_text()
    assert out["replaced"] == [] and cron.jobs[-1]["no_agent"] is True and cron.jobs[-1]["script"] == shim.name
    out = m.pkg.alerts_on("15m", "telegram", home=tmp_path)
    assert out["replaced"] == ["job2"] and [j["id"] for j in cron.jobs] == ["other", "job2"]
    assert cron.jobs[-1]["deliver"] == "telegram"
    status = m.pkg.alerts_status(tmp_path, jobs=cron.jobs)
    assert status["on"] is True and status["jobs"] == 1 and status["deliver"] == "telegram"
    off = m.pkg.alerts_off(home=tmp_path)
    assert off == {"removed_jobs": ["job2"], "removed_script": True} and [j["id"] for j in cron.jobs] == ["other"]
    assert m.pkg.alerts_status(tmp_path, jobs=cron.jobs)["on"] is False


def test_alerts_on_replaces_the_0_1_0_job_and_deletes_its_shim(m, tmp_path, monkeypatch):
    cron = FakeCron()
    cron.jobs.append({"id": "old", "name": "Coolify Cockpit alerts", "script": m.alerts.OLD_SCRIPT_NAME, "enabled": True})
    old_shim = tmp_path / "scripts" / m.alerts.OLD_SCRIPT_NAME
    old_shim.parent.mkdir()
    old_shim.write_text("#!/usr/bin/env python3\n")
    monkeypatch.setattr(m.pkg, "_cron_jobs", lambda: cron)
    out = m.pkg.alerts_on("5m", "local", home=tmp_path)
    assert out["replaced"] == ["old"] and [j["name"] for j in cron.jobs] == ["Coolifybrücke alerts"]
    assert not old_shim.exists() and sorted(p.name for p in old_shim.parent.iterdir()) == ["coolifybruecke-alerts.py"]


def test_the_shim_runs_the_tick_and_passes_its_output_through(m, tmp_path):
    shim = m.alerts.write_shim(tmp_path)
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    hermes = fake_bin / "hermes"
    hermes.write_text("#!/bin/sh\n[ \"$1 $2\" = \"coolifybruecke tick\" ] && echo 'Coolifybrücke · h' && exit 0\nexit 3\n")
    hermes.chmod(0o755)
    import os
    import subprocess
    run = subprocess.run([sys.executable, str(shim)], capture_output=True, text=True,
                         env={**os.environ, "PATH": f"{fake_bin}:{os.environ.get('PATH', '')}"})
    assert run.returncode == 0 and run.stdout == "Coolifybrücke · h\n"


def test_cli_parser_and_handler(m, tmp_path, monkeypatch):
    import argparse
    parser = argparse.ArgumentParser()
    m.pkg._cli_setup(parser)
    args = parser.parse_args(["alerts", "on", "--every", "10m"])
    assert (args.cockpit_command, args.alerts_command, args.every, args.deliver) == ("alerts", "on", "10m", "local")
    args = parser.parse_args(["tick", "--peek"])
    seen = {}
    monkeypatch.setattr(m.pkg, "tick_report", lambda gs, gx, peek=False: (seen.setdefault("peek", peek) and "", {"ok": True}))
    assert m.pkg.make_cli_handler(lambda k, d=None: d, lambda n: None)(args) == 0 and seen == {"peek": True}


def test_alerts_route_reads_without_coolify(m, tmp_path, monkeypatch):
    monkeypatch.setattr(m.pkg, "_cron_jobs", lambda: FakeCron())
    status, body = m.pkg.api_alerts(data_dir=tmp_path)
    assert status == 200 and body["ok"] is True and body["on"] is False and body["open"] == 0
