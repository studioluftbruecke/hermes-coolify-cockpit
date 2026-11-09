"""Behaviour of M2: the Snapshot, its cache, the read tools and the dashboard routes, against a
synthetic instance (`fixtures/snapshot.json`, Coolify 4.6.0 shapes). No network."""

import importlib
import importlib.util
import json
import pathlib
import sys
import types
import urllib.error
import urllib.parse

import pytest

FIXTURE = json.loads((pathlib.Path(__file__).resolve().parent / "fixtures" / "snapshot.json").read_text())
BASE = "https://coolify.example.test"
READ_TOOLS = {"get_server_metrics", "list_deployments"}


class Instance:
    """Answers by token: `tokens` maps a token string to `teamwide`, `scoped`, `rejected`."""

    def __init__(self, tokens, *, mcp_off=False, unreachable=False):
        self.tokens = tokens
        self.mcp_off = mcp_off
        self.unreachable = unreachable
        self.requests = []  # (method, path or tool, token)

    def __call__(self, method, url, headers, body, timeout):
        client = sys.modules[_plugin().__name__ + ".coolify_client"]
        token = headers.get("Authorization", "")[len("Bearer "):]
        if self.unreachable:
            raise urllib.error.URLError("[Errno -2] Name or service not known")
        parsed = urllib.parse.urlsplit(url)
        if parsed.path == "/mcp":
            call = json.loads(body)
            name = call["params"].get("name")
            self.requests.append((method, "MCP " + str(name), token))
            assert call["method"] == "tools/call" and name in READ_TOOLS, call
            data = FIXTURE.get(self.tokens.get(token), {})
            if self.mcp_off:
                return client.HttpResponse(404, b'{"message": "Not found"}')
            args = call["params"]["arguments"]
            key = f"{name}:{args['server_uuid']}" if name == "get_server_metrics" else name
            if key not in data.get("mcp", {}):
                return client.HttpResponse(400, json.dumps({"jsonrpc": "2.0", "id": call["id"], "error": {
                    "code": -32602, "message": f"Tool [{name}] not found."}}).encode())
            return client.HttpResponse(200, json.dumps({"jsonrpc": "2.0", "id": call["id"],
                                                        "result": data["mcp"][key]}).encode())
        key = f"{method} {parsed.path}" + (f"?{parsed.query}" if parsed.query else "")
        self.requests.append((method, key, token))
        kind = self.tokens.get(token)
        if kind == "rejected" or kind is None:
            return client.HttpResponse(401, b'{"message": "Unauthenticated."}')
        status, payload = FIXTURE[kind]["rest"].get(key, (404, {"message": "Not found."}))
        return client.HttpResponse(status, json.dumps(payload).encode())


_LOADED = {}


def _plugin():
    if "pkg" not in _LOADED:
        root = pathlib.Path(__file__).resolve().parents[1]
        name = "coolify_cockpit_m2_under_test"
        spec = importlib.util.spec_from_file_location(name, root / "__init__.py",
                                                      submodule_search_locations=[str(root)])
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
        importlib.import_module(name + ".snapshot")
        _LOADED["pkg"] = module
    return _LOADED["pkg"]


@pytest.fixture
def m2():
    pkg = _plugin()
    return types.SimpleNamespace(pkg=pkg, snap=sys.modules[pkg.__name__ + ".snapshot"],
                                 caps=sys.modules[pkg.__name__ + ".capabilities"])


def settings(url=BASE):
    values = {"COOLIFY_COCKPIT_URL": url}
    return lambda key, default=None: values.get(key, default)


def secrets(action=None, read=None):
    values = {"COOLIFY_COCKPIT_TOKEN": action, "COOLIFY_COCKPIT_READ_TOKEN": read}
    return values.get


def setup(m2, instance, *, action="tok-action", read="tok-read", cache=None, cache_file=None):
    """kwargs for the pkg entry points: one fresh cache per test unless given."""
    return dict(transport=instance, cache=cache or m2.snap.SnapshotCache(), cache_file=cache_file)


def build(m2, instance, action="tok-action", read="tok-read", **kw):
    conn = m2.caps.connection_from_settings(BASE, action, read)
    return m2.snap.build_snapshot(conn, transport=instance, **kw).to_dict()


# --- the Snapshot --------------------------------------------------------------------------------

def test_recommended_pair_reads_everything_with_the_overview_token(m2):
    inst = Instance({"tok-action": "scoped", "tok-read": "teamwide"})
    snap = build(m2, inst)
    assert snap["source"]["role"] == "overview" and snap["source"]["scope"] == "team-wide"
    assert [(s["name"], bool(s["metrics"]), s["metrics_note"] is None) for s in snap["servers"]] == [
        ("main", True, True), ("edge", False, False)]
    assert snap["servers"][0]["metrics"]["current"]["cpu_percent"] == 13.6
    assert len(snap["servers"][0]["metrics"]["series"]["cpu_percent"]) == 12
    assert "Sentinel metrics are off" in snap["servers"][1]["metrics_note"]
    rows = [(r["kind"], r["name"], r["project"], r["environment"], r["status"]) for r in snap["resources"]]
    assert rows == [
        ("application", "blog", "Blog", "production", "running:unknown"),
        ("service", "analytics", "Blog", "production", "running:healthy"),
        ("application", "shop-web", "Shop", "production", "running:healthy"),
        ("database", "shop-db", "Shop", "production", "running:healthy"),
        ("application", "shop-web-staging", "Shop", "staging", "exited:unhealthy"),
    ]
    assert next(r for r in snap["resources"] if r["kind"] == "service")["fqdn"] == "https://stats.example.test"
    running = snap["deployments"]["running"]
    assert [(d["uuid"], d["resource_uuid"], d["status"]) for d in running] == [
        ("dep-run", "app-shop-staging", "in_progress")]  # REST queue rows name the app only; mapped by name
    failed = snap["deployments"]["recent_failed"]
    assert [d["uuid"] for d in failed] == ["dep-fail-2", "dep-fail-1", "dep-fail-0"]  # newest first
    assert failed[0]["url"].startswith(BASE + "/project/")
    assert snap["limits"] == [] and snap["errors"] == []


def test_snapshot_sends_only_reads_and_counts_every_call(m2):
    inst = Instance({"tok-action": "scoped", "tok-read": "teamwide"})
    snap = build(m2, inst)
    assert {m for m, _, _ in inst.requests} == {"GET", "POST"}
    assert all(m == "GET" or p in ("MCP get_server_metrics", "MCP list_deployments") for m, p, _ in inst.requests)
    stats = snap["stats"]
    assert stats["calls"] == len(inst.requests) == 11
    assert stats["by_call"] == {"GET /applications": 1, "GET /databases": 1, "GET /deployments": 1,
                                "GET /projects": 1, "GET /projects/{uuid}": 2, "GET /servers": 2,
                                "GET /services": 1, "MCP get_server_metrics": 1, "MCP list_deployments": 1}
    # the action token answered only the scope check
    assert [p for _, p, t in inst.requests if t == "tok-action"] == ["GET /api/v1/servers"]


def test_counting_transport_refuses_any_write(m2):
    counter = m2.snap.CountingTransport(lambda *a: None)
    with pytest.raises(AssertionError):
        counter("POST", BASE + "/api/v1/deploy?uuid=x", {}, b"{}", 1)
    with pytest.raises(AssertionError):
        counter("POST", BASE + "/mcp", {}, json.dumps({"method": "tools/call", "params": {"name": "deploy"}}).encode(), 1)


def test_single_scoped_token_names_the_missing_server_data(m2):
    inst = Instance({"tok-action": "scoped"})
    snap = build(m2, inst, read=None)
    assert snap["source"] == {"role": "single", "setting": "COOLIFY_COCKPIT_TOKEN",
                              "fingerprint": snap["source"]["fingerprint"], "scope": "scoped"}
    assert snap["servers"] == []
    assert any("scoped tokens cannot read servers" in x and "COOLIFY_COCKPIT_READ_TOKEN" in x for x in snap["limits"])
    assert [(r["name"], r["environment"]) for r in snap["resources"]] == [("shop-web-staging", "staging")]
    assert [d["uuid"] for d in snap["deployments"]["recent_failed"]] == ["dep-fail-2", "dep-fail-1"]
    assert not any(p == "MCP get_server_metrics" for _, p, _ in inst.requests)


def test_two_team_wide_tokens_read_with_the_overview_token(m2):
    inst = Instance({"tok-action": "teamwide", "tok-read": "teamwide"})
    snap = build(m2, inst)
    assert snap["source"]["role"] == "overview"


def test_two_scoped_tokens_read_with_the_one_that_sees_more(m2):
    wide = dict(FIXTURE["scoped"], rest={**FIXTURE["scoped"]["rest"],
                                         "GET /api/v1/applications": [200, FIXTURE["teamwide"]["rest"]["GET /api/v1/applications"][1]]})
    FIXTURE["scoped_wide"] = wide
    try:
        inst = Instance({"tok-action": "scoped", "tok-read": "scoped_wide"})
        snap = build(m2, inst)
    finally:
        del FIXTURE["scoped_wide"]
    assert snap["source"]["role"] == "overview"
    assert len([r for r in snap["resources"] if r["kind"] == "application"]) == 3
    # the tie-break list is reused, not fetched twice
    assert sum(1 for _, p, t in inst.requests if p == "GET /api/v1/applications" and t == "tok-read") == 1


def test_mcp_off_keeps_the_rest_and_says_what_is_missing(m2):
    inst = Instance({"tok-action": "scoped", "tok-read": "teamwide"}, mcp_off=True)
    snap = build(m2, inst)
    assert snap["deployments"]["recent_failed"] is None
    assert any("need Coolify's MCP server" in x and "coolify_deployments" in x for x in snap["limits"])
    assert snap["servers"][0]["metrics"] is None and "metrics unavailable" in snap["servers"][0]["metrics_note"]
    assert len(snap["resources"]) == 5


def test_no_usable_token_raises_with_the_setting_to_fix(m2):
    inst = Instance({"tok-action": "rejected"})
    with pytest.raises(m2.snap.SnapshotUnavailable, match="COOLIFY_COCKPIT_TOKEN.*401"):
        build(m2, inst, read=None)
    with pytest.raises(m2.snap.SnapshotUnavailable, match="did not answer"):
        build(m2, Instance({}, unreachable=True), read=None)


# --- cache ---------------------------------------------------------------------------------------

class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def test_cache_reuses_a_snapshot_for_ten_seconds_then_rebuilds(m2):
    inst = Instance({"tok-action": "scoped", "tok-read": "teamwide"})
    clock = Clock()
    cache = m2.snap.SnapshotCache(clock=clock)
    conn = m2.caps.connection_from_settings(BASE, "tok-action", "tok-read")
    first = m2.snap.snapshot_dict(conn, transport=inst, cache=cache)
    calls = len(inst.requests)
    clock.now += 9.5
    second = m2.snap.snapshot_dict(conn, transport=inst, cache=cache)
    assert len(inst.requests) == calls and second["cache"] == {"hit": "memory", "age_s": 9.5, "ttl_s": 10.0}
    assert second["taken_at"] == first["taken_at"]
    clock.now += 1.0
    third = m2.snap.snapshot_dict(conn, transport=inst, cache=cache)
    assert third["cache"]["hit"] is None and len(inst.requests) > calls
    assert third["stats"]["hint"] == "used"  # the expired snapshot told it which token reads
    refreshed = m2.snap.snapshot_dict(conn, transport=inst, cache=cache, refresh=True)
    assert refreshed["cache"]["hit"] is None


def test_cache_file_shares_a_snapshot_between_processes(m2, tmp_path):
    inst = Instance({"tok-action": "scoped", "tok-read": "teamwide"})
    clock = Clock()
    path = tmp_path / "snapshot-cache.json"
    conn = m2.caps.connection_from_settings(BASE, "tok-action", "tok-read")
    m2.snap.snapshot_dict(conn, transport=inst, cache=m2.snap.SnapshotCache(clock=clock), cache_file=path)
    calls = len(inst.requests)
    other_process = m2.snap.SnapshotCache(clock=clock)
    clock.now += 3
    again = m2.snap.snapshot_dict(conn, transport=inst, cache=other_process, cache_file=path)
    assert again["cache"]["hit"] == "file" and len(inst.requests) == calls
    stored = path.read_text()
    assert "tok-action" not in stored and "tok-read" not in stored  # fingerprints only
    # another token setup is another key: no reuse
    single = m2.caps.connection_from_settings(BASE, "tok-action", None)
    other = m2.snap.snapshot_dict(single, transport=inst, cache=m2.snap.SnapshotCache(clock=clock), cache_file=path)
    assert other["cache"]["hit"] is None and other["source"]["role"] == "single"


def test_a_rebuild_reuses_server_metrics_for_a_minute(m2):
    inst = Instance({"tok-action": "scoped", "tok-read": "teamwide"})
    first = build(m2, inst)
    inst.requests.clear()
    second = build(m2, inst, hint=m2.snap.hint_from(first))
    assert second["stats"]["metrics_reused"] == 1
    assert not any(p == "MCP get_server_metrics" for _, p, _ in inst.requests)
    assert second["servers"][0]["metrics"] == first["servers"][0]["metrics"]
    first["servers"][0]["metrics"]["fetched_epoch"] -= 61
    inst.requests.clear()
    third = build(m2, inst, hint=m2.snap.hint_from(first))
    assert third["stats"]["metrics_reused"] == 0
    assert sum(1 for _, p, _ in inst.requests if p == "MCP get_server_metrics") == 1
    text = m2.snap.render_overview({**first, "cache": {}})
    assert "(metrics 61 s old)" in text


def test_a_hint_for_another_token_is_dropped_and_the_reads_redone(m2):
    inst = Instance({"tok-action": "scoped", "tok-read": "teamwide"})
    snap = build(m2, inst, hint=m2.snap.ReadHint("action", ()))
    assert snap["stats"]["hint"] == "dropped" and snap["source"]["role"] == "overview"
    assert len(snap["resources"]) == 5 and snap["servers"][0]["metrics"] is not None


# --- tools ---------------------------------------------------------------------------------------

def test_overview_text_answers_failures_and_server_load(m2):
    inst = Instance({"tok-action": "scoped", "tok-read": "teamwide"})
    text = m2.pkg.overview_text(settings(), secrets("tok-action", "tok-read"), **setup(m2, inst))
    assert "read with the overview token, team-wide" in text
    assert "- main: reachable, usable. CPU 14%, RAM 6.0 of 16.0 GiB (38%), disk 40% of 150.0 GiB, load1 0.68" in text
    assert "- edge: reachable, usable. Sentinel metrics are off" in text
    assert "DEPLOYMENTS running or queued: 1" in text
    lines = text.splitlines()
    first = lines.index(next(l for l in lines if l.startswith("Recent failed deployments")))
    assert lines[first + 1] == ('- 2026-11-04 21:30 shop-web-staging [Shop / staging] on main commit def5678 '
                                '"Pin node 20" (deployment dep-fail-2)')
    assert "- shop-web-staging (application, Shop / staging): exited:unhealthy" in text
    assert "  blog app running:unknown https://blog.example.test" in text
    assert "tok-" not in text


def test_overview_filters_by_environment(m2):
    inst = Instance({"tok-action": "scoped", "tok-read": "teamwide"})
    text = m2.pkg.overview_text(settings(), secrets("tok-action", "tok-read"), environment="STAG", **setup(m2, inst))
    assert "(1 of 5 resources)" in text
    assert "shop-web-staging" in text and "  blog app" not in text
    assert "dep-fail-0" not in text  # blog's failure is filtered out with blog


def test_overview_text_stays_under_the_cap_on_a_big_instance(m2):
    snap = build(m2, Instance({"tok-action": "scoped", "tok-read": "teamwide"}))
    snap["resources"] = [dict(snap["resources"][0], uuid=f"u{i}", name=f"app-{i:03d}-with-a-long-name",
                              fqdn=f"https://app-{i}.example.test", status="running:healthy") for i in range(400)]
    text = m2.snap.render_overview(snap)
    assert len(text) <= m2.snap.MAX_OVERVIEW_CHARS
    assert "more resources not listed" in text and "LIMITS" not in text


def test_overview_reports_setup_problems_as_text(m2):
    inst = Instance({"tok-action": "rejected"})
    text = m2.pkg.overview_text(settings(), secrets("tok-action"), **setup(m2, inst))
    assert text.startswith("Coolify overview unavailable: No token can read the instance")
    assert m2.pkg.overview_text(settings(url=""), secrets("tok-action"), **setup(m2, inst)).startswith(
        "Coolify overview unavailable: No Coolify URL set")


def test_deployments_history_by_name(m2):
    inst = Instance({"tok-action": "scoped", "tok-read": "teamwide"})
    text = m2.pkg.deployments_text(settings(), secrets("tok-action", "tok-read"), resource="shop-web-staging",
                                   **setup(m2, inst))
    lines = text.splitlines()
    assert lines[0].startswith("shop-web-staging (application, Shop / staging, status exited:unhealthy")
    assert lines[1] == '- 2026-11-04 21:30 failed, 3m00s commit def5678 "Pin node 20" (deployment dep-fail-2)'
    assert lines[3] == '- 2026-11-03 10:00 finished, 2m30s commit 9999999 "Add banner" (deployment dep-ok-1)'
    assert ("GET", "GET /api/v1/deployments/applications/app-shop-staging?skip=0&take=10", "tok-read") in inst.requests


def test_deployments_name_lookup_is_strict_about_ambiguity(m2):
    inst = Instance({"tok-action": "scoped", "tok-read": "teamwide"})
    text = m2.pkg.deployments_text(settings(), secrets("tok-action", "tok-read"), resource="shop",
                                   **setup(m2, inst))
    assert text.startswith("Coolify deployments unavailable: 3 resources match 'shop'")
    assert "Pass the uuid" in text
    svc = m2.pkg.deployments_text(settings(), secrets("tok-action", "tok-read"), resource="analytics",
                                  **setup(m2, inst))
    assert "applications only" in svc


def test_a_renamed_app_shows_its_current_name_in_deployments(m2):
    rows = json.loads(FIXTURE["teamwide"]["mcp"]["list_deployments"]["content"][0]["text"])
    rows["data"][0] = dict(rows["data"][0], application_name="old-name-before-rename")
    renamed = json.loads(json.dumps(FIXTURE["teamwide"]))
    renamed["mcp"]["list_deployments"]["content"][0]["text"] = json.dumps(rows)
    FIXTURE["renamed"] = renamed
    try:
        snap = build(m2, Instance({"tok-read": "renamed"}), action=None)
    finally:
        del FIXTURE["renamed"]
    assert [d["resource_name"] for d in snap["deployments"]["recent_failed"]] == [
        "shop-web-staging", "shop-web-staging", "blog"]


def test_a_deployment_id_passed_as_resource_shows_that_deployment(m2):
    inst = Instance({"tok-action": "scoped", "tok-read": "teamwide"})
    text = m2.pkg.deployments_text(settings(), secrets("tok-action", "tok-read"), resource="dep-fail-2",
                                   **setup(m2, inst))
    assert text.startswith("Deployment dep-fail-2 of shop-web-staging")


def test_deployment_detail_never_passes_logs_on(m2):
    inst = Instance({"tok-action": "scoped", "tok-read": "teamwide"})
    text = m2.pkg.deployments_text(settings(), secrets("tok-action", "tok-read"), deployment_uuid="dep-fail-2",
                                   **setup(m2, inst))
    assert "Deployment dep-fail-2 of shop-web-staging (app-shop-staging): failed" in text
    assert "flags: force_rebuild=True" in text
    assert "SYNTHETIC LOG LINE" not in text
    assert "Build logs are not included" in text


def test_tool_handlers_return_text_and_never_raise(m2, monkeypatch):
    handler = m2.pkg.make_deployments_handler(settings(), secrets("tok-action", "tok-read"))
    assert handler({}).startswith("Pass `resource`")
    monkeypatch.setattr(m2.pkg, "overview_text", lambda *a, **k: 1 / 0)
    out = json.loads(m2.pkg.make_overview_handler(settings(), secrets("tok-action"))({}))
    assert out["ok"] is False and out["error"] == "internal"


# --- dashboard routes ----------------------------------------------------------------------------

def test_api_snapshot_and_history(m2):
    inst = Instance({"tok-action": "scoped", "tok-read": "teamwide"})
    kw = setup(m2, inst)
    status, body = m2.pkg.api_snapshot(settings(), secrets("tok-action", "tok-read"), **kw)
    assert status == 200 and body["ok"] is True and len(body["resources"]) == 5
    assert set(body) >= {"servers", "resources", "deployments", "taken_at", "source", "limits", "cache"}
    status, body = m2.pkg.api_resource_deployments(settings(), secrets("tok-action", "tok-read"), "app-shop-staging",
                                                   take=2, **kw)
    assert status == 200 and [d["uuid"] for d in body["deployments"]] == ["dep-fail-2", "dep-fail-1"]
    assert body["deployments"][0]["resource_uuid"] == "app-shop-staging"
    status, body = m2.pkg.api_resource_deployments(settings(), secrets("tok-action", "tok-read"), "nope", **kw)
    assert status == 404 and body["error"] == "not_found"
    status, body = m2.pkg.api_resource_deployments(settings(), secrets("tok-action", "tok-read"), "svc-analytics", **kw)
    assert status == 200 and body["deployments"] == [] and "applications only" in body["note"]


def test_api_errors_carry_a_status_and_a_reason(m2):
    inst = Instance({"tok-action": "rejected"})
    status, body = m2.pkg.api_snapshot(settings(), secrets("tok-action"), **setup(m2, inst))
    assert status == 502 and body["error"] == "unavailable"
    assert "plugin settings" in body["hint"]  # a rejected token: check it in the settings
    status, body = m2.pkg.api_snapshot(settings(url=""), secrets("tok-action"), **setup(m2, inst))
    assert status == 400 and body["error"] == "not_configured" and "hint" not in body  # the URL is not a secret
    status, body = m2.pkg.api_snapshot(settings(), secrets(), **setup(m2, inst))
    assert status == 400 and "plugin settings" in body["hint"]  # no token yet: the settings
    status, body = m2.pkg.api_capabilities(settings(url=""), secrets("tok-action"))
    assert status == 400 and body["error"] == "not_configured"


def _fake_fastapi(monkeypatch):
    fastapi = types.ModuleType("fastapi")
    responses = types.ModuleType("fastapi.responses")

    class APIRouter:
        def __init__(self):
            self.routes = {}

        def get(self, path):
            def wrap(fn):
                self.routes[path] = fn
                return fn
            return wrap

        def post(self, path):
            def wrap(fn):
                self.routes["POST " + path] = fn
                return fn
            return wrap

        def put(self, path):
            def wrap(fn):
                self.routes["PUT " + path] = fn
                return fn
            return wrap

    class JSONResponse:
        def __init__(self, content, status_code=200):
            self.content, self.status_code = content, status_code

    fastapi.APIRouter = APIRouter
    fastapi.Query = lambda default=None, **kw: default
    fastapi.Body = lambda default=None, **kw: default
    responses.JSONResponse = JSONResponse
    monkeypatch.setitem(sys.modules, "fastapi", fastapi)
    monkeypatch.setitem(sys.modules, "fastapi.responses", responses)


def test_plugin_api_mounts_the_routes(monkeypatch):
    _fake_fastapi(monkeypatch)
    root = pathlib.Path(__file__).resolve().parents[1]
    manifest = json.loads((root / "dashboard" / "manifest.json").read_text())
    assert manifest["name"] == "coolify-cockpit" and manifest["api"] == "plugin_api.py"
    spec = importlib.util.spec_from_file_location("coolify_cockpit_api_under_test", root / "dashboard" / "plugin_api.py")
    api = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(api)
    assert sorted(api.router.routes) == ["/actions", "/capabilities", "/layout", "/notes",
                                         "/resources/{uuid}/deployments", "/setup", "/snapshot", "POST /actions",
                                         "POST /diagnose", "POST /layout/preset", "POST /notes", "PUT /layout"]
    monkeypatch.setattr(api, "_get_setting", lambda key, default=None: default)  # no URL configured
    monkeypatch.setattr(api, "_get_secret", lambda name: None)
    for call in (api.capabilities, lambda: api.snapshot(refresh=False),
                 lambda: api.resource_deployments("x", take=10)):
        response = call()
        assert response.status_code == 400 and response.content["error"] == "not_configured"


# --- 0.0.11: the page's setup state and fresh secrets in the plugin host -------------------------

def test_setup_names_what_is_missing_and_never_a_token_value(m2):
    status, body = m2.pkg.api_setup(settings(url=""), secrets())
    assert status == 200 and body["configured"] is False
    assert [(s["id"], s["set"], s["required"]) for s in body["steps"]] == [
        ("url", False, True), ("action_token", False, True), ("read_token", False, False)]
    status, body = m2.pkg.api_setup(settings(), secrets(read="tok-read"))
    assert body["configured"] is True  # one token of either kind is a working setup
    assert [(s["id"], s["set"], s["required"]) for s in body["steps"]][1:] == [
        ("action_token", False, False), ("read_token", True, False)]
    assert "tok-read" not in json.dumps(body)
    status, body = m2.pkg.api_setup(settings(url="not a url"), secrets("tok-action"))
    assert body["configured"] is False and body["steps"][0]["set"] is False and body["steps"][0]["error"]


def test_routes_read_a_token_saved_after_the_plugin_host_started(monkeypatch):
    _fake_fastapi(monkeypatch)
    profile_now = {"COOLIFY_COCKPIT_TOKEN": "saved-later"}
    launch_env = {"COOLIFY_COCKPIT_READ_TOKEN": "removed-since", "OTHER": "x"}
    scope = types.ModuleType("agent.secret_scope")
    scope.build_profile_secret_scope = lambda home: dict(profile_now)
    scope.get_secret = lambda name: launch_env.get(name)
    consts = types.ModuleType("hermes_constants")
    consts.get_hermes_home = lambda: pathlib.Path("/profile")
    monkeypatch.setitem(sys.modules, "agent", types.ModuleType("agent"))
    monkeypatch.setitem(sys.modules, "agent.secret_scope", scope)
    monkeypatch.setitem(sys.modules, "hermes_constants", consts)
    root = pathlib.Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location("coolify_cockpit_api_secrets", root / "dashboard" / "plugin_api.py")
    api = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(api)

    monkeypatch.setenv("HERMES_PLUGIN_HOST_PROCESS", "1")  # in a profile's plugin host
    assert api._get_secret("COOLIFY_COCKPIT_TOKEN") == "saved-later"
    assert api._get_secret("COOLIFY_COCKPIT_READ_TOKEN") is None  # removed from .env: gone at once
    assert api._get_secret("OTHER") == "x"  # other names keep Hermes' own lookup
    profile_now["COOLIFY_COCKPIT_TOKEN"] = "rolled"
    assert api._get_secret("COOLIFY_COCKPIT_TOKEN") == "rolled"

    monkeypatch.delenv("HERMES_PLUGIN_HOST_PROCESS")  # in process: the request's own fresh scope
    assert api._get_secret("COOLIFY_COCKPIT_READ_TOKEN") == "removed-since"
