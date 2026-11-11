"""Coolify's API rate limit: the 429 mapping, no retry loop, and the request budget of the page.

Coolify 4.6.0 throttles `/api/*` at API_RATE_LIMIT requests per minute (200 by default) per Coolify
user, so both cockpit tokens of one user share one budget. Before 0.0.10 a page load spent ~129 of
it (two separate capabilities checks of ~49 requests each plus a 30-request snapshot), and a 429
came back as `bad_request` (experiments/coolify-cockpit/m9-ratelimit). These tests pin the mapping
and the caches that keep one open page far below the limit.
"""

import importlib
import importlib.util
import json
import pathlib
import sys
import threading
import time
import urllib.parse

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
BASE = "https://coolify.example.test"
BOGUS = "hermes-permission-probe-does-not-exist"
SCOPED = {"message": "This token is limited to selected environments and cannot read servers."}


def load(name):
    """One plugin load = one process (the gateway or the profile's plugin host)."""
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, ROOT / "__init__.py", submodule_search_locations=[str(ROOT)])
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    for sub in ("capabilities", "snapshot", "actions", "coolify_client", "layout"):
        importlib.import_module(f"{name}.{sub}")
    return module


def sub(module, name):
    return sys.modules[f"{module.__name__}.{name}"]


APPS = [
    {"uuid": "app-stg", "name": "shop-staging", "environment_id": 11, "status": "running:healthy"},
    {"uuid": "app-prod", "name": "shop", "environment_id": 12, "status": "running:healthy"},
]


class Coolify:
    """Both tokens of one user. `limited(method, path)` decides which requests get a 429."""

    def __init__(self, module, limited=None, retry_after="37"):
        self.client = sub(module, "coolify_client")
        self.limited = limited or (lambda method, path: False)
        self.retry_after = retry_after
        self.requests = []  # (method, path, token)
        self.apps = [dict(a) for a in APPS]
        self.envs = [{"id": 11, "name": "staging"}, {"id": 12, "name": "production"}]
        self._lock = threading.Lock()

    def answer(self, status, payload, headers=None):
        return self.client.HttpResponse(status, json.dumps(payload).encode(), "application/json", headers or {})

    def __call__(self, method, url, headers, body, timeout):
        token = headers.get("Authorization", "")[len("Bearer "):]
        parsed = urllib.parse.urlsplit(url)
        path = parsed.path[len("/api/v1"):] if parsed.path.startswith("/api/v1") else parsed.path
        query = dict(urllib.parse.parse_qsl(parsed.query))
        with self._lock:
            self.requests.append((method, path, token))
        if path == "/mcp":
            call = json.loads(body)
            name = (call.get("params") or {}).get("name")
            text = json.dumps({"data": []}) if name == "list_deployments" else json.dumps({"current": {"cpu_percent": 1}})
            result = {"serverInfo": {"name": "coolify"}} if call["method"] == "initialize" else \
                {"content": [{"type": "text", "text": text}]}
            return self.answer(200, {"jsonrpc": "2.0", "id": call["id"], "result": result})
        if self.limited(method, path):
            headers = {"Retry-After": self.retry_after} if self.retry_after is not None else {}
            return self.answer(429, {"message": "Too Many Attempts."}, headers)
        scoped = token == "tok-act"
        rights = {"read", "deploy"} if scoped else {"read"}
        if BOGUS in path or query.get("uuid") == BOGUS:
            need = {"GET": "read", "POST": "deploy", "PATCH": "write", "DELETE": "delete"}[method]
            need = "read:sensitive" if path.endswith("/logs") else need
            return self.answer(404, {"message": "Not found."}) if need in rights else \
                self.answer(403, {"message": f"Missing required permissions: {need}"})
        apps = [a for a in self.apps if not scoped or a["environment_id"] == 11]
        if method != "GET":
            return self.answer(200, {"message": "Restart request queued.", "deployment_uuid": "dep-new"})
        if path == "/version":
            return self.answer(200, "4.6.0")
        if path == "/servers":
            return self.answer(403, SCOPED) if scoped else self.answer(200, [])
        if path == "/projects":
            return self.answer(200, [{"uuid": "proj-shop", "name": "shop"}])
        if path == "/projects/proj-shop":
            envs = [e for e in self.envs if not scoped or e["id"] == 11]
            return self.answer(200, {"uuid": "proj-shop", "name": "shop", "environments": envs})
        if path == "/applications":
            return self.answer(200, apps)
        if path in ("/services", "/databases", "/deployments"):
            return self.answer(200, [])
        if path.startswith("/deployments/applications/"):
            return self.answer(200, {"count": 0, "deployments": []})
        if path.startswith("/applications/"):
            found = [a for a in apps if a["uuid"] == path.split("/")[2]]
            return self.answer(200, {**found[0], "destination": {"server": {"name": "app-1"}}}) if found else \
                self.answer(404, {"message": "Not found."})
        return self.answer(404, {"message": "Not found."})

    def count(self, prefix=""):
        return sum(1 for m, p, _ in self.requests if p != "/mcp" and f"{m} {p}".startswith(prefix))


def settings(key, default=None):
    return {"COOLIFY_COCKPIT_URL": BASE, "check_updates": False}.get(key, default)


secrets = {"COOLIFY_COCKPIT_TOKEN": "tok-act", "COOLIFY_COCKPIT_READ_TOKEN": "tok-read"}.get


@pytest.fixture
def host(tmp_path, monkeypatch):
    """The profile's plugin host (serves the page's routes) with its plugin data dir."""
    module = load("coolify_cockpit_rl_host")
    monkeypatch.setattr(module, "_default_data_dir", lambda: tmp_path)
    monkeypatch.setattr(module, "_default_cache_file", lambda: tmp_path / "snapshot-cache.json")
    module._CAPABILITIES_CACHES.clear()
    sub(module, "snapshot").SNAPSHOT_CACHE._memory.clear()
    sub(module, "actions").ABILITY_CACHE._seen.clear()
    sub(module, "actions").VIEW_CACHE._views.clear()
    return module


# --- the 429 mapping -----------------------------------------------------------------------------

def test_a_429_is_rate_limited_with_its_wait_and_names_the_shared_budget(host):
    fake = Coolify(host, limited=lambda m, p: True)
    client_mod = sub(host, "coolify_client")
    with pytest.raises(client_mod.CoolifyError) as caught:
        client_mod.CoolifyClient(BASE, "tok-act", transport=fake).get("/applications")
    error = caught.value
    assert error.kind is client_mod.ErrorKind.RATE_LIMITED and error.status == 429 and error.retry_after == 37
    assert str(error).startswith("rate_limited: Coolify is limiting API requests")
    assert "try again in 37 s" in error.message and str(error).endswith("(GET /applications)")
    assert "API_RATE_LIMIT" in error.message and "200 by default" in error.message
    assert "per Coolify user, shared by all of that user's tokens" in error.message


def test_a_429_without_retry_after_says_a_minute_and_other_4xx_stay_bad_request(host):
    client_mod = sub(host, "coolify_client")
    error = client_mod.error_for_status(429, {"message": "Too Many Attempts."}, "GET", "/servers")
    assert error.kind is client_mod.ErrorKind.RATE_LIMITED and error.retry_after is None
    assert "try again in a minute" in error.message
    assert client_mod.error_for_status(422, {"message": "bad"}, "POST", "/x").kind is client_mod.ErrorKind.BAD_REQUEST


def test_the_client_never_retries_a_429(host, monkeypatch):
    """One request per call: no sleep, no second attempt, even when Retry-After is short."""
    slept = []
    monkeypatch.setattr(time, "sleep", lambda s: slept.append(s))
    fake = Coolify(host, limited=lambda m, p: True, retry_after="1")
    client_mod = sub(host, "coolify_client")
    client = client_mod.CoolifyClient(BASE, "tok-read", transport=fake)
    for _ in range(3):
        with pytest.raises(client_mod.CoolifyError):
            client.get("/version")
    assert fake.count() == 3 and slept == []


def test_a_rate_limited_capabilities_check_stops_and_claims_no_right(host, monkeypatch):
    """Each token stops at its first 429 (one request each); the answer is rate_limited, never a
    report that counts the 429 as a held right (0.0.9 mapped it to bad_request = held)."""
    slept = []
    monkeypatch.setattr(time, "sleep", lambda s: slept.append(s))
    fake = Coolify(host, limited=lambda m, p: True)
    out = host.capabilities_report(settings, secrets, transport=fake, data_dir=None)
    assert out["ok"] is False and out["error"] == "rate_limited" and out["retry_after_s"] == 37
    assert fake.count() == 2 and slept == []
    only_deploy = Coolify(host, limited=lambda m, p: m == "POST" and p == "/deploy")
    out = host.capabilities_report(settings, secrets, transport=only_deploy, data_dir=None)
    assert out["error"] == "rate_limited" and "tokens" not in out


def test_routes_answer_429_with_retry_after(host):
    fake = Coolify(host, limited=lambda m, p: True)
    status, body = host.api_snapshot(settings, secrets, transport=fake, cache=sub(host, "snapshot").SnapshotCache())
    assert (status, body["error"], body["retry_after_s"]) == (429, "rate_limited", 37)
    status, body = host.api_capabilities(settings, secrets, transport=fake)
    assert (status, body["error"]) == (429, "rate_limited")
    status, body = host.api_action(settings, secrets, {"kind": "restart", "target": "shop-staging", "dry_run": True},
                                   transport=fake)
    assert (status, body["error"], body["retry_after_s"], body["sent"]) == (429, "rate_limited", 37, False)
    status, body = host.api_action_catalog(settings, secrets, transport=fake)
    assert (status, body["error"]) == (429, "rate_limited")  # a wait, not "the token lacks deploy"


def test_a_snapshot_that_met_a_429_is_not_kept(host):
    """A swallowed read (a project detail) that got a 429 fails the build, so no snapshot with
    missing environment names is cached."""
    snap_mod = sub(host, "snapshot")
    cache = snap_mod.SnapshotCache()
    fake = Coolify(host, limited=lambda m, p: p == "/projects/proj-shop")
    status, body = host.api_snapshot(settings, secrets, transport=fake, cache=cache, cache_file=None)
    assert status == 429 and body["error"] == "rate_limited"
    assert cache._memory == {}
    fake.limited = lambda m, p: False
    status, body = host.api_snapshot(settings, secrets, transport=fake, cache=cache, cache_file=None)
    assert status == 200 and body["cache"]["hit"] is None and len(body["resources"]) == 2


# --- one capabilities check for every caller -----------------------------------------------------

def test_the_capabilities_check_is_shared_by_the_tool_and_the_page_in_another_process(host, tmp_path, monkeypatch):
    gateway = load("coolify_cockpit_rl_gateway")
    monkeypatch.setattr(gateway, "_default_data_dir", lambda: tmp_path)
    gateway._CAPABILITIES_CACHES.clear()
    fake = Coolify(host)
    tool = gateway.capabilities_report(settings, secrets, transport=fake)  # the agent's tool (gateway)
    one_check = fake.count()
    assert tool["ok"] and tool["cache"]["hit"] is None and one_check >= 20
    assert (tmp_path / "capabilities-cache.json").exists()
    status, page = host.api_capabilities(settings, secrets, transport=fake)  # the page (plugin host)
    assert status == 200 and page["cache"]["hit"] == "file" and fake.count() == one_check
    status, layout = host.api_layout_get(host.layout_store(tmp_path / "layout.json"), availability=True,
                                         get_setting=settings, get_secret=secrets, transport=fake)
    assert status == 200 and "availability_error" not in layout and fake.count() == one_check
    assert page["tokens"] == tool["tokens"]
    fresh = gateway.capabilities_report(settings, secrets, transport=fake, refresh=True)
    assert fresh["cache"]["hit"] is None and fake.count() == 2 * one_check


def test_the_page_loads_two_capability_asks_at_once_and_coolify_sees_one_check(host, tmp_path):
    fake = Coolify(host)
    results = []
    store = host.layout_store(tmp_path / "layout.json")
    threads = [threading.Thread(target=lambda: results.append(host.api_capabilities(settings, secrets, transport=fake))),
               threading.Thread(target=lambda: results.append(host.api_layout_get(
                   store, availability=True, get_setting=settings, get_secret=secrets, transport=fake)))]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    probes = [r for r in fake.requests if BOGUS in r[1] or r[0] in ("PATCH", "DELETE")]
    assert [s for s, _ in results] == [200, 200]
    assert len(probes) == 8  # 4 made-up-UUID probe paths x 2 tokens (deploy is a POST with ?uuid=)
    assert sum(1 for m, p, _ in fake.requests if m == "POST" and p == "/deploy") == 2


def test_the_capabilities_check_expires(host, tmp_path):
    fake = Coolify(host)
    host.capabilities_report(settings, secrets, transport=fake)
    first = fake.count()
    cache = host._capabilities_cache(tmp_path)
    cache._clock = lambda: time.time() + host.CAPABILITIES_TTL_S + 1
    out = host.capabilities_report(settings, secrets, transport=fake)
    assert out["cache"]["hit"] is None and fake.count() == 2 * first


# --- a click costs few requests --------------------------------------------------------------------

def test_the_confirm_reuses_the_dry_runs_view_of_the_action_token(host):
    fake = Coolify(host)
    request = {"kind": "restart", "target": "shop-staging"}
    status, plan = host.api_action(settings, secrets, {**request, "dry_run": True}, transport=fake)
    assert status == 200 and plan["target"]["environment"] == "staging" and plan["target"]["server"] == "app-1"
    reads = fake.count("GET")
    status, sent = host.api_action(settings, secrets, request, transport=fake)
    assert status == 200 and sent["ok"] and sent["sent"]
    assert fake.count("GET") == reads  # no second GET /applications, /projects or server lookup
    assert [r for r in fake.requests if r[0] == "POST" and BOGUS not in r[1]][-1][1] == "/applications/app-stg/restart"


def test_the_dry_run_takes_environment_names_from_the_stored_snapshot(host):
    fake = Coolify(host)
    status, _ = host.api_snapshot(settings, secrets, transport=fake)  # the page's snapshot, stored in the data dir
    assert status == 200
    before = len(fake.requests)
    status, plan = host.api_action(settings, secrets, {"kind": "restart", "target": "app-stg", "dry_run": True},
                                   transport=fake)
    asked = [f"{m} {p}" for m, p, _ in fake.requests[before:]]
    assert status == 200 and plan["target"]["project"] == "shop" and plan["target"]["environment"] == "staging"
    assert not any(a.startswith("GET /projects") for a in asked)
    assert asked.count("GET /applications") == 1  # the API token's own list still decides the view


def test_a_resource_outside_the_view_is_still_refused_with_a_cached_view(host):
    fake = Coolify(host)
    host.api_action(settings, secrets, {"kind": "restart", "target": "shop-staging", "dry_run": True}, transport=fake)
    status, body = host.api_action(settings, secrets, {"kind": "restart", "target": "app-prod"}, transport=fake)
    assert status == 403 and body["error"] == "outside_action_view"


# --- the snapshot and the followed deployment ---------------------------------------------------------

def test_a_rebuild_reuses_environment_names_until_an_unknown_id_appears(host, tmp_path):
    snap_mod = sub(host, "snapshot")
    now = [time.time()]
    cache = snap_mod.SnapshotCache(clock=lambda: now[0])
    fake = Coolify(host)
    host.api_snapshot(settings, secrets, transport=fake, cache=cache, cache_file=tmp_path / "s.json")
    assert fake.count("GET /projects") == 2  # the list and one detail
    now[0] += snap_mod.CACHE_TTL_S + 1
    status, body = host.api_snapshot(settings, secrets, transport=fake, cache=cache, cache_file=tmp_path / "s.json")
    assert status == 200 and body["stats"]["environments"] == "reused" and fake.count("GET /projects") == 2
    assert {r["environment"] for r in body["resources"]} == {"staging", "production"}
    fake.envs.append({"id": 13, "name": "preview"})  # a new environment, and an app in it
    fake.apps.append({"uuid": "app-prev", "name": "shop-pr-7", "environment_id": 13, "status": "running"})
    now[0] += snap_mod.CACHE_TTL_S + 1
    status, body = host.api_snapshot(settings, secrets, transport=fake, cache=cache, cache_file=tmp_path / "s.json")
    assert body["stats"]["environments"] == "read" and fake.count("GET /projects") == 4
    assert next(r for r in body["resources"] if r["uuid"] == "app-prev")["environment"] == "preview"


def test_following_a_deployment_does_not_rebuild_the_snapshot(host, tmp_path):
    snap_mod = sub(host, "snapshot")
    now = [time.time()]
    cache = snap_mod.SnapshotCache(clock=lambda: now[0])
    fake = Coolify(host)
    setup = {"transport": fake, "cache": cache, "cache_file": tmp_path / "s.json", "data_dir": None}
    host.api_snapshot(settings, secrets, **setup)
    built = fake.count()
    for _ in range(6):  # 30 s of 5 s polls, the snapshot long expired
        now[0] += 5
        status, body = host.api_resource_deployments(settings, secrets, "app-stg", take=10, **setup)
        assert status == 200 and body["resource"]["name"] == "shop-staging"
    assert fake.count() == built + 6 and fake.count("GET /deployments/applications/app-stg") == 6
    status, body = host.api_resource_deployments(settings, secrets, "not-there", take=10, **setup)
    assert status == 404 and fake.count("GET /servers") == 4  # an unknown uuid builds a snapshot once
