"""Behaviour: inside a plugin host the page's routes see a token saved after the host started
within 2 seconds (at once on "Check again"), and one scope build serves a page load's reads, not
one per secret per request. The routes read secrets only through
Hermes' scope builder and never touch the profile's `.env` themselves. Only the plugin's own keys
are kept between requests.
"""

from __future__ import annotations

import importlib.util
import pathlib
import sys
import time
import types

ROOT = pathlib.Path(__file__).resolve().parents[1]


def _fake_fastapi(monkeypatch):
    fastapi = types.ModuleType("fastapi")
    responses = types.ModuleType("fastapi.responses")

    class APIRouter:
        def _add(self, *_):
            return lambda fn: fn

        get = post = put = _add

    setattr(fastapi, "APIRouter", APIRouter)
    setattr(fastapi, "Query", lambda default=None, **kw: default)
    setattr(fastapi, "Body", lambda default=None, **kw: default)
    setattr(responses, "JSONResponse", object)
    monkeypatch.setitem(sys.modules, "fastapi", fastapi)
    monkeypatch.setitem(sys.modules, "fastapi.responses", responses)


def _routes_module(monkeypatch, home, builds):
    _fake_fastapi(monkeypatch)

    def build_profile_secret_scope(hermes_home):
        builds.append(str(hermes_home))
        values = {}
        for line in (pathlib.Path(hermes_home) / ".env").read_text().splitlines():
            key, _, value = line.partition("=")
            values[key] = value
        return values

    scope = types.ModuleType("agent.secret_scope")
    setattr(scope, "build_profile_secret_scope", build_profile_secret_scope)
    setattr(scope, "get_secret", lambda name: None)
    constants = types.ModuleType("hermes_constants")
    setattr(constants, "get_hermes_home", lambda: home)
    if "agent" not in sys.modules:
        monkeypatch.setitem(sys.modules, "agent", types.ModuleType("agent"))
    monkeypatch.setitem(sys.modules, "agent.secret_scope", scope)
    monkeypatch.setitem(sys.modules, "hermes_constants", constants)
    monkeypatch.setenv("HERMES_PLUGIN_HOST_PROCESS", "1")
    spec = importlib.util.spec_from_file_location("coolifybruecke_api_secret_scope", ROOT / "dashboard" / "plugin_api.py")
    assert spec is not None and spec.loader is not None
    api = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(api)
    return api


def test_a_page_loads_reads_share_one_scope_build(monkeypatch, tmp_path):
    (tmp_path / ".env").write_text("COOLIFYBRUECKE_TOKEN=first\nOTHER_PLUGIN_TOKEN=not-ours\n")
    builds: list = []
    api = _routes_module(monkeypatch, tmp_path, builds)
    clock = [1000.0]
    monkeypatch.setattr(time, "monotonic", lambda: clock[0])

    for _ in range(6):  # six routes of one page load, two secrets each, within a second
        assert api._get_secret("COOLIFYBRUECKE_TOKEN") == "first"
        assert api._get_secret("COOLIFYBRUECKE_READ_TOKEN") is None
        clock[0] += 0.15
    assert len(builds) == 1


def test_a_saved_or_removed_token_shows_within_two_seconds(monkeypatch, tmp_path):
    env = tmp_path / ".env"
    env.write_text("COOLIFYBRUECKE_TOKEN=first\n")
    api = _routes_module(monkeypatch, tmp_path, [])
    clock = [1000.0]
    monkeypatch.setattr(time, "monotonic", lambda: clock[0])
    assert api._get_secret("COOLIFYBRUECKE_TOKEN") == "first"

    env.write_text("COOLIFYBRUECKE_TOKEN=second-saved-later\n")
    clock[0] += 2.0
    assert api._get_secret("COOLIFYBRUECKE_TOKEN") == "second-saved-later"

    env.write_text("")
    clock[0] += 2.0
    assert api._get_secret("COOLIFYBRUECKE_TOKEN") is None


def test_the_routes_never_touch_the_profiles_env_file_themselves(monkeypatch, tmp_path):
    (tmp_path / ".env").write_text("COOLIFYBRUECKE_TOKEN=first\n")
    builds: list = []
    api = _routes_module(monkeypatch, tmp_path, builds)
    touched = []
    for name in ("stat", "lstat", "exists", "is_file", "open", "read_text", "read_bytes"):
        real = getattr(pathlib.Path, name)

        def spy(self, *args, _real=real, _name=name, **kwargs):
            if self.name == ".env" and not builds_in_progress:
                touched.append(_name)
            return _real(self, *args, **kwargs)

        monkeypatch.setattr(pathlib.Path, name, spy)
    builds_in_progress: list = []
    scope = sys.modules["agent.secret_scope"]
    real_build = scope.build_profile_secret_scope

    def tracked_build(home):
        builds_in_progress.append(1)
        try:
            return real_build(home)
        finally:
            builds_in_progress.pop()

    monkeypatch.setattr(scope, "build_profile_secret_scope", tracked_build)

    assert api._get_secret("COOLIFYBRUECKE_TOKEN") == "first"
    assert api._get_secret("COOLIFYBRUECKE_TOKEN") == "first"
    assert touched == [], "only Hermes' scope builder reads the profile's secrets"


def test_only_the_plugins_own_keys_are_kept(monkeypatch, tmp_path):
    (tmp_path / ".env").write_text("COOLIFYBRUECKE_TOKEN=first\nOTHER_PLUGIN_TOKEN=not-ours\n")
    api = _routes_module(monkeypatch, tmp_path, [])
    api._get_secret("COOLIFYBRUECKE_TOKEN")
    kept = repr(vars(api))
    assert "not-ours" not in kept


def test_check_again_reads_the_secrets_anew(monkeypatch, tmp_path):
    (tmp_path / ".env").write_text("COOLIFYBRUECKE_TOKEN=first\n")
    builds: list = []
    api = _routes_module(monkeypatch, tmp_path, builds)
    monkeypatch.setattr(api, "_plugin", lambda: types.SimpleNamespace(
        api_capabilities=lambda get_setting, get_secret, **kw: (200, {"token": get_secret("COOLIFYBRUECKE_TOKEN")})))
    monkeypatch.setattr(api, "_reply", lambda result: result)
    api.capabilities(refresh=False)
    api.capabilities(refresh=False)
    assert len(builds) == 1
    api.capabilities(refresh=True)
    assert len(builds) == 2
