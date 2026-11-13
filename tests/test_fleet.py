"""the failing rule, the window edge, the Signal registry and the env check (fleet.py,
envcheck.py). Rows are recorded shapes (MCP list_deployments, REST history, env rows) with
synthetic values."""

import importlib
import importlib.util
import json
import pathlib
import sys
import types

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]


def _load():
    name = "coolifybruecke_fleet_under_test"
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, ROOT / "__init__.py", submodule_search_locations=[str(ROOT)])
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
    pkg = sys.modules[name]
    return types.SimpleNamespace(pkg=pkg, fleet=importlib.import_module(name + ".fleet"),
                                 env=importlib.import_module(name + ".envcheck"),
                                 client=importlib.import_module(name + ".coolify_client"))


@pytest.fixture
def m():
    return _load()


def row(uuid, app, status, created, finished=None, commit="abc1234def5678", **flags):
    return {"deployment_uuid": uuid, "application_uuid": app, "status": status, "created_at": created,
            "finished_at": finished or created, "commit": commit, "commit_message": "msg", **flags}


def complete(m):
    return m.fleet.ListWindow(True)


# --- the failing rule --------------------------------------------------------------------

def test_newest_settled_failed_and_nothing_running_is_failing(m):
    rows = [row("d1", "a", "finished", "2026-11-01T10:00:00Z"), row("d2", "a", "failed", "2026-11-02T10:00:00Z"),
            row("d3", "a", "failed", "2026-11-03T10:00:00Z")]
    r = m.fleet.fleet_row("a", rows, finished=complete(m), failed=complete(m))
    assert r.state == "failing" and r.failing_since == "2026-11-02T10:00:00Z"
    assert r.last_good["uuid"] == "d1" and r.latest["uuid"] == "d3"


def test_a_later_success_clears_it_and_marks_the_failure_superseded(m):
    rows = [row("d1", "a", "failed", "2026-11-01T10:00:00Z"), row("d2", "a", "finished", "2026-11-02T10:00:00Z")]
    r = m.fleet.fleet_row("a", rows, finished=complete(m), failed=complete(m))
    assert r.state == "ok" and r.failing_since is None
    assert m.fleet.superseded(rows) == {"d1"}


def test_a_newer_running_deployment_means_deploying_not_failing(m):
    rows = [row("d1", "a", "failed", "2026-11-01T10:00:00Z"), row("d2", "a", "in_progress", "2026-11-02T10:00:00Z")]
    assert m.fleet.fleet_row("a", rows, finished=complete(m), failed=complete(m)).state == "deploying"


def test_a_cancelled_deployment_counts_as_nothing(m):
    rows = [row("d1", "a", "failed", "2026-11-01T10:00:00Z"),
            row("d2", "a", "cancelled-by-user", "2026-11-02T10:00:00Z")]
    assert m.fleet.fleet_row("a", rows, finished=complete(m), failed=complete(m)).state == "failing"


def test_no_rows_is_never_deployed_only_when_both_lists_are_complete(m):
    cut = m.fleet.ListWindow(False, "2026-11-03T00:00:00Z")
    assert m.fleet.fleet_row("a", [], finished=complete(m), failed=complete(m)).state == "never_deployed"
    assert m.fleet.fleet_row("a", [], finished=cut, failed=complete(m)).state == "unknown"


def test_window_edge_needs_one_history_read_and_the_history_decides(m):
    cut = m.fleet.ListWindow(False, "2026-11-03T00:00:00Z")  # the finished list reaches back to 3 November
    old_failure = [row("d1", "a", "failed", "2026-10-20T10:00:00Z")]
    assert m.fleet.needs_history(old_failure, cut, complete(m)) is True
    recent_failure = [row("d1", "a", "failed", "2026-11-04T10:00:00Z")]
    assert m.fleet.needs_history(recent_failure, cut, complete(m)) is False
    history = [row("d0", "a", "finished", "2026-10-25T10:00:00Z"), old_failure[0]]
    r = m.fleet.fleet_row("a", old_failure, finished=cut, failed=complete(m), history=history)
    assert r.state == "ok" and r.checked == "history"


def test_window_of_reads_the_pagination(m):
    rows = [{"created_at": "2026-11-03T00:00:00Z"}, {"created_at": "2026-11-09T00:00:00Z"}]
    assert m.fleet.window_of({"_pagination": {"total": 255}}, rows) == m.fleet.ListWindow(False, "2026-11-03T00:00:00Z")
    assert m.fleet.window_of({"_pagination": {"total": 2}}, rows).complete is True


def test_trigger_from_flags(m):
    assert [m.fleet.trigger_of(f) for f in ({"is_webhook": True}, {"is_api": True}, {"is_mcp": True}, {})] == [
        "webhook", "api", "mcp", "manual"]


# --- signals ----------------------------------------------------------------------------------

def snapshot(**extra):
    base = {
        "resources": [
            {"uuid": "a", "kind": "application", "name": "shop", "environment": "production", "status": "running:healthy",
             "git_branch": "main"},
            {"uuid": "b", "kind": "application", "name": "shop-staging", "environment": "staging", "status": "running:unhealthy"},
            {"uuid": "c", "kind": "service", "name": "wiki", "environment": "production", "status": "exited"},
            {"uuid": "d", "kind": "application", "name": "blog", "environment": "production", "status": "exited:unhealthy"},
        ],
        "servers": [
            {"uuid": "s1", "name": "web-1", "reachable": True, "usable": True,
             "metrics": {"current": {"disk_used_percent": 87.2, "memory_used_bytes": 7.6, "memory_total_bytes": 8.0}}},
            {"uuid": "s2", "name": "web-2", "reachable": False, "usable": True, "metrics": None},
        ],
        "fleet": {
            "a": {"state": "failing", "failing_since": "2026-11-09T10:00:00Z",
                  "last_good": {"uuid": "g1", "commit": "1111111aaaaaa", "finished_at": "2026-11-01T10:00:00Z"}},
            "b": {"state": "ok", "last_good": {"uuid": "g2", "commit": "2222222bbbbbb", "finished_at": "2026-11-05T10:00:00Z"}},
            "d": {"state": "ok", "last_good": {"uuid": "g3", "commit": "HEAD", "finished_at": "2026-11-05T10:00:00Z"}},
        },
    }
    base.update(extra)
    return base


def test_signals_find_each_kind_and_sort_worst_first(m):
    view = m.fleet.View(snapshot(), env_changes={"b": "2026-11-06T10:00:00Z"},
                        version={"update_available": True, "latest_version": "4.8.0", "coolify_version": "4.7.1"},
                        branches={"b": {"head": "3333333cccccc", "source": "forgebruecke"}})
    items = m.fleet.attention(view)
    got = [(a.signal, a.resource_uuid or a.server_uuid) for a in items]
    assert got == [("failing", "a"), ("unhealthy", "b"), ("server_down", "s2"), ("server_pressure", "s1"),
                   ("env_changed", "b"), ("behind_branch", "b"), ("update_available", None)]
    failing = items[0]
    assert failing.fix == "rollback" and failing.detail == "last good 1111111"
    assert items[3].title == "web-1 at disk 87 % and memory 95 %"
    assert m.fleet.summary(items)[0] == {"signal": "failing", "label": "failing", "severity": "bad", "count": 1}


def test_a_stopped_resource_is_not_unhealthy(m):
    assert [m.fleet.is_unhealthy(s) for s in ("exited", "exited:unhealthy", "running:unhealthy", "degraded:unhealthy",
                                              "restarting", "running:healthy")] == [False, False, True, True, True, False]


def test_thresholds_are_settings(m):
    view = m.fleet.View(snapshot(), disk_warn=90, memory_warn=96)
    assert not [a for a in m.fleet.attention(view) if a.signal == "server_pressure"]


def test_failing_without_a_known_good_commit_suggests_a_deploy(m):
    snap = snapshot()
    snap["fleet"]["a"]["last_good"] = {"uuid": "g1", "commit": "HEAD"}
    assert m.fleet.attention(m.fleet.View(snap))[0].fix == "deploy"


def test_behind_is_quiet_while_failing_deploying_or_equal(m):
    snap = snapshot()
    branches = {"a": {"head": "9999999"}, "b": {"head": "2222222bbbbbb"}, "d": {"head": "4444444"}}
    assert not [a for a in m.fleet.attention(m.fleet.View(snap, branches=branches)) if a.signal == "behind_branch"]
    view = m.fleet.View(snap, branches={"b": {"head": "5555555eeeeee", "commits": 3}})
    behind = [a for a in m.fleet.attention(view) if a.signal == "behind_branch"]
    assert behind[0].title == "shop-staging (staging): its branch is 3 commits ahead"


# --- the env check ----------------------------------------------------------------------------

class Envs:
    """A transport answering GET /applications/{uuid}/envs from a dict; counts requests."""

    def __init__(self, m, answers, status=200):
        self.m, self.answers, self.status, self.paths = m, answers, status, []

    def __call__(self, method, url, headers, body, timeout):
        path = url.split("/api/v1", 1)[1]
        self.paths.append(path)
        uuid = path.split("/")[2]
        payload = self.answers.get(uuid, [])
        status = self.status if not isinstance(self.status, dict) else self.status.get(uuid, 200)
        if status == 429:
            return self.m.client.HttpResponse(429, b'{"message": "Too Many Attempts."}', headers={"Retry-After": "30"})
        return self.m.client.HttpResponse(status, json.dumps(payload).encode())


def env_rows(*stamps):
    return [{"key": f"K{i}", "updated_at": s, "is_shown_once": False} for i, s in enumerate(stamps)]


def test_changed_after_compares_with_the_last_good_deploy(m):
    assert m.env.changed_after(env_rows("2026-11-01T10:00:00.000000Z", "2026-11-06T09:00:00.000000Z"),
                               "2026-11-05T10:00:00.000000Z") == "2026-11-06T09:00:00.000000Z"
    assert m.env.changed_after(env_rows("2026-11-01T10:00:00.000000Z"), "2026-11-05T10:00:00.000000Z") is None
    assert m.env.changed_after(env_rows("2026-11-06T10:00:00Z"), None) is None


def test_env_refresh_reads_due_apps_once_and_keeps_answers(m, tmp_path):
    fleet = snapshot()["fleet"]
    transport = Envs(m, {"b": env_rows("2026-11-06T10:00:00Z"), "a": env_rows("2026-10-01T10:00:00Z")})
    client = m.client.CoolifyClient("https://coolify.example.test", "tok", transport=transport)
    path = tmp_path / "env-check.json"
    first = m.env.refresh(client, fleet, path=path, key="k", now=1000.0)
    assert first == {"b": "2026-11-06T10:00:00Z"} and len(transport.paths) == 3
    assert "tok" not in path.read_text() and "K0" not in path.read_text()  # timestamps only, no names or values
    again = m.env.refresh(client, fleet, path=path, key="k", now=1100.0)
    assert again == first and len(transport.paths) == 3  # within ENV_TTL_S: no read
    fleet["b"]["last_good"] = {"uuid": "g9", "finished_at": "2026-11-07T10:00:00Z"}  # it deployed again
    after = m.env.refresh(client, fleet, path=path, key="k", now=1200.0)
    assert after == {} and transport.paths[-1] == "/applications/b/envs"


def test_env_refresh_stops_on_429_and_keeps_the_last_answers(m, tmp_path):
    fleet = snapshot()["fleet"]
    path = tmp_path / "env-check.json"
    ok = m.client.CoolifyClient("https://c.test", "tok", transport=Envs(m, {"b": env_rows("2026-11-06T10:00:00Z")}))
    m.env.refresh(ok, fleet, path=path, key="k", now=1000.0)
    transport = Envs(m, {}, status=429)
    limited = m.client.CoolifyClient("https://c.test", "tok", transport=transport)
    assert m.env.refresh(limited, fleet, path=path, key="k", now=5000.0) == {"b": "2026-11-06T10:00:00Z"}
    assert len(transport.paths) == 1  # the round ended at the first 429


def test_env_refresh_skips_deploying_apps_and_caps_reads(m):
    fleet = {f"x{i}": {"state": "ok", "last_good": {"uuid": f"g{i}", "finished_at": "2026-11-01T00:00:00Z"}}
             for i in range(10)}
    fleet["x0"]["state"] = "deploying"
    due = m.env.due(fleet, {}, 0.0)
    assert "x0" not in due and len(due) == 9
    transport = Envs(m, {})
    client = m.client.CoolifyClient("https://c.test", "tok", transport=transport)
    m.env.refresh(client, fleet, path=None, key="k", now=0.0)
    assert len(transport.paths) == m.env.ENV_READS


# --- server ranges and the disk estimate (servers.py) --------------------------------

def _srv():
    return importlib.import_module("coolifybruecke_fleet_under_test.servers")


def week(start_pct, per_day, days=7, step_h=0.5, t0=1_794_000_000):
    n = int(days * 24 / step_h)
    return [[(t0 + i * step_h * 3600) * 1000, start_pct + per_day * i * step_h / 24] for i in range(n + 1)]


def test_disk_estimate_only_when_it_fills_within_30_days(m):
    srv = _srv()
    rising = week(70, 2.0)            # 70 % to 84 % over 7 days: about 8 days left
    got = srv.disk_forecast(rising)
    assert got and 7.5 <= got["days"] <= 8.5 and abs(got["slope_per_day"] - 2.0) < 0.01
    assert srv.disk_forecast(week(40, 0.2)) is None      # 300 days: no number
    assert srv.disk_forecast(week(60, -1.0)) is None     # shrinking
    assert srv.disk_forecast(week(60, 0.0)) is None      # flat
    assert srv.disk_forecast(rising[:5]) is None         # too few points
    assert srv.disk_forecast(week(70, 2.0, days=0.5)) is None  # under a day of data


def test_a_range_is_read_once_per_ttl_and_tied_to_the_instance_key(m, tmp_path):
    srv = _srv()
    calls = []

    def read(uuid, minutes):
        calls.append((uuid, minutes))
        return {"series": {"cpu_percent": [[1, 2.0]]}, "bucket_seconds": 300, "minutes": minutes, "current": {}}, None

    path = tmp_path / srv.METRICS_FILE
    first = srv.read_range(read, "s1", "6h", path=path, key="k", now=1000.0)
    again = srv.read_range(read, "s1", "6h", path=path, key="k", now=1200.0)
    assert (first["cache"], again["cache"]) == ("miss", "hit") and calls == [("s1", 360)]
    srv.read_range(read, "s1", "6h", path=path, key="k", now=1000.0 + srv.RANGE_TTL_S["6h"] + 1)
    srv.read_range(read, "s1", "6h", path=path, key="other-token", now=1400.0)
    assert len(calls) == 3
    with pytest.raises(ValueError):
        srv.read_range(read, "s1", "30m", path=path, key="k")


def test_range_payload_reads_7d_for_the_estimate_and_skips_servers_without_metrics(m, tmp_path):
    srv = _srv()
    minutes_read = []

    def read(uuid, minutes):
        minutes_read.append(minutes)
        series = {"disk_used_percent": week(70, 2.0)} if minutes == 10080 else {"cpu_percent": [[1, 3.0]]}
        return {"series": series, "bucket_seconds": 1800 if minutes == 10080 else 300, "minutes": minutes}, None

    servers = [{"uuid": "s1", "metrics": {"current": {"disk_used_percent": 84.0}}},
               {"uuid": "s2", "metrics_enabled": False}]
    out = srv.range_payload(read, servers, "24h", path=tmp_path / "m.json", key="k", now=1000.0)
    assert sorted(minutes_read) == [1440, 10080] and list(out["servers"]) == ["s1"]
    assert 7.5 <= out["servers"]["s1"]["disk_full"]["days"] <= 8.5
    out = srv.range_payload(read, servers, "1h", path=tmp_path / "m.json", key="k", now=1010.0)
    assert minutes_read.count(10080) == 1  # the 7 d read is on the slow cadence
