"""0.2.4: "Check again" on the setup line re-reads the tokens and runs the rights check anew.

Konstantin (2026-11-12): "Forgebrücke has a button after setting the environment variables, if
they're not getting picked up, that with a click of a button they get picked up. [...] And we also
need this in Coolifybrücke."

The page's capabilities route shares one check for 2 minutes (`CAPABILITIES_TTL_S`). A token saved
in the settings form changes the cache key, so it is used on the next ask anyway. A fix outside
Hermes (a proxy's allow-list, Coolify's API switch, a token's rights in Coolify) does not, so the
page kept showing the old result for up to 2 minutes. `GET /capabilities?refresh=true` is the
button's route: it checks again whatever the cache holds.
"""

from __future__ import annotations

import importlib.util
import pathlib
import sys
import types

ROOT = pathlib.Path(__file__).resolve().parents[1]


class Gate:
    """Something in front of Coolify that answers a plain-text 403 until the user opens it."""

    def __init__(self, kit, behind):
        self.kit, self.behind, self.closed = kit, behind, True

    def __call__(self, method, url, headers, body, timeout):
        if self.closed:
            return self.kit.client.HttpResponse(403, b"Forbidden", "")
        return self.behind(method, url, headers, body, timeout)


class Clock:
    def __init__(self):
        self.now = 1_800_000_000.0

    def __call__(self):
        return self.now


def _clocked_cache(kit, tmp_path):
    """The capabilities cache for tmp_path, on a clock the test steps."""
    clock = Clock()
    snap = importlib.import_module(kit.plugin.__name__ + ".snapshot")
    kit.plugin._CAPABILITIES_CACHES[str(tmp_path)] = snap.SnapshotCache(ttl_s=kit.plugin.CAPABILITIES_TTL_S, clock=clock)
    return clock


def test_check_again_uses_a_token_saved_later_and_checks_coolify_anew(kit, tmp_path):
    saved: dict = {}  # the profile's .env as the settings form writes it
    gate = Gate(kit, kit.FakeCoolify({"tok-saved-later": "teamwide_read_deploy"}))
    settings = kit.settings(check_updates=False)
    clock = _clocked_cache(kit, tmp_path)

    def ask(**kw):
        return kit.plugin.api_capabilities(settings, saved.get, transport=gate, data_dir=tmp_path, **kw)

    status, body = ask(refresh=True)
    assert status == 400 and body["error"] == "not_configured"  # no token yet

    saved["COOLIFYBRUECKE_TOKEN"] = "tok-saved-later"  # saved in the settings form, page still open
    status, body = ask(refresh=True)
    assert status == 200 and body["cache"]["hit"] is None
    assert "could not be reached" in body["setup"]["line"]

    gate.closed = False  # the user fixes the allow-list
    clock.now += 30
    status, body = ask()  # the page's own poll: the shared check, still the old answer
    assert status == 200 and body["cache"]["hit"]
    assert "could not be reached" in body["setup"]["line"]

    status, body = ask(refresh=True)  # the click
    assert status == 200 and body["cache"]["hit"] is None
    assert body["instance"]["reachable"] is True
    assert "could not be reached" not in body["setup"]["line"]
    assert body["checked_at"]

    status, again = ask()  # the next poll keeps the new answer
    assert again["cache"]["hit"] and again["instance"]["reachable"] is True


def test_check_again_reuses_a_check_younger_than_the_floor(kit, tmp_path):
    """R150-4: one check is about 50 Coolify requests against 200 per minute per user, so a click
    right after another check (a second window, a double click) answers from it. The agent's tool
    keeps refresh=True as "check now"."""
    settings = kit.settings(check_updates=False)
    token = {"COOLIFYBRUECKE_TOKEN": "tok-floor"}.get
    clock = _clocked_cache(kit, tmp_path)
    fake = kit.FakeCoolify({"tok-floor": "teamwide_read_deploy"})

    def click():
        status, body = kit.plugin.api_capabilities(settings, token, transport=fake, data_dir=tmp_path, refresh=True)
        assert status == 200
        return body["cache"]

    assert click()["hit"] is None
    clock.now += 4
    second = click()
    assert second["hit"] == "memory" and second["age_s"] == 4.0
    tool = kit.plugin.capabilities_report(settings, token, transport=fake, data_dir=tmp_path, refresh=True)
    assert tool["cache"]["hit"] is None
    clock.now += kit.plugin.CAPABILITIES_REFRESH_FLOOR_S
    assert click()["hit"] is None


def _fake_fastapi(monkeypatch):
    fastapi = types.ModuleType("fastapi")
    responses = types.ModuleType("fastapi.responses")

    class APIRouter:
        def __init__(self):
            self.routes = {}

        def _add(self, key):
            def wrap(fn):
                self.routes[key] = fn
                return fn
            return wrap

        def get(self, path):
            return self._add(path)

        def post(self, path):
            return self._add("POST " + path)

        def put(self, path):
            return self._add("PUT " + path)

    class JSONResponse:
        def __init__(self, content, status_code=200):
            self.content, self.status_code = content, status_code

    fastapi.APIRouter = APIRouter
    fastapi.Query = lambda default=None, **kw: default
    fastapi.Body = lambda default=None, **kw: default
    responses.JSONResponse = JSONResponse
    monkeypatch.setitem(sys.modules, "fastapi", fastapi)
    monkeypatch.setitem(sys.modules, "fastapi.responses", responses)


def test_the_route_passes_refresh_through(monkeypatch):
    _fake_fastapi(monkeypatch)
    spec = importlib.util.spec_from_file_location("coolifybruecke_api_check_again", ROOT / "dashboard" / "plugin_api.py")
    assert spec is not None and spec.loader is not None
    api = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(api)
    seen = []
    fake = types.SimpleNamespace(api_capabilities=lambda get_setting, get_secret, **kw: (seen.append(kw), (200, {"ok": True}))[1])
    monkeypatch.setattr(api, "_plugin", lambda: fake)
    assert api.capabilities().status_code == 200
    assert api.capabilities(refresh=True).status_code == 200
    assert seen == [{"refresh": False}, {"refresh": True}]
