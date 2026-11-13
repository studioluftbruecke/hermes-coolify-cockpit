"""What the Desktop page relies on from the routes: deployment links, the version handshake, and
type checks on the page's write routes."""

from __future__ import annotations

import importlib
import importlib.util
import re
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
_LOADED: dict = {}


def _plugin():
    if "pkg" not in _LOADED:
        name = "coolifybruecke_route_contract_under_test"
        spec = importlib.util.spec_from_file_location(name, ROOT / "__init__.py", submodule_search_locations=[str(ROOT)])
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
        _LOADED["pkg"] = module
        _LOADED["snapshot"] = importlib.import_module(name + ".snapshot")
        _LOADED["layout"] = importlib.import_module(name + ".layout")
    return _LOADED["pkg"]


@pytest.mark.parametrize("raw, expected", [
    ("/project/p/deployment/d", "https://c.example/project/p/deployment/d"),
    ("https://c.example/project/p/deployment/d", "https://c.example/project/p/deployment/d"),
    ("HTTP://c.example/x", "HTTP://c.example/x"),
    ("javascript:alert(1)", None),
    ("JavaScript:alert(1)", None),
    ("data:text/html,x", None),
    ("", None),
    (None, None),
])
def test_deployment_links_are_instance_paths_or_web_urls(raw, expected):
    _plugin()
    assert _LOADED["snapshot"]._deployment_url("https://c.example", raw) == expected


def _api(monkeypatch):
    fastapi = types.ModuleType("fastapi")
    responses = types.ModuleType("fastapi.responses")

    class APIRouter:
        def __init__(self):
            self.routes = {}

        def get(self, path):
            return lambda fn: self.routes.setdefault(path, fn)

        def post(self, path):
            return lambda fn: self.routes.setdefault("POST " + path, fn)

        def put(self, path):
            return lambda fn: self.routes.setdefault("PUT " + path, fn)

    fastapi.APIRouter = APIRouter
    fastapi.Query = lambda default=None, **kw: default
    fastapi.Body = lambda default=None, **kw: default
    responses.JSONResponse = lambda content, status_code=200: (status_code, content)
    monkeypatch.setitem(sys.modules, "fastapi", fastapi)
    monkeypatch.setitem(sys.modules, "fastapi.responses", responses)
    spec = importlib.util.spec_from_file_location("coolifybruecke_api_route_contract", ROOT / "dashboard" / "plugin_api.py")
    api = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(api)
    return api


def test_setup_names_the_manifest_version(monkeypatch):
    api = _api(monkeypatch)
    fake = types.SimpleNamespace(api_setup=lambda get_setting, get_secret: (200, {"ok": True, "configured": False, "steps": []}))
    monkeypatch.setattr(api, "_plugin", lambda: fake)
    status, body = api.setup()
    manifest = re.search(r'^version:\s*"?([^"\s]+)"?', (ROOT / "plugin.yaml").read_text(encoding="utf-8"), re.M).group(1)
    assert status == 200 and body["version"] == manifest and body["configured"] is False


def test_the_route_list_names_every_route(monkeypatch):
    api = _api(monkeypatch)
    doc = api.__doc__ or ""
    for key in api.router.routes:
        method, _, path = key.rpartition(" ")
        assert re.search(rf"^\s+{method or 'GET'} {re.escape(path)}\b", doc, re.M), f"{method or 'GET'} {path} missing from the docstring"


@pytest.mark.parametrize("version", ["a", {}, True, 1.5, [1]])
def test_layout_routes_refuse_a_version_that_is_not_a_whole_number(tmp_path, version):
    plugin = _plugin()
    store = _LOADED["layout"].LayoutStore(tmp_path / "layout.json")
    layout = plugin.api_layout_get(store)[1]["layout"]
    status, body = plugin.api_layout_put(store, {"expected_version": version, "layout": layout})
    assert status == 400 and body["error"] == "invalid"
    status, body = plugin.api_layout_preset(store, {"preset": "overview", "expected_version": version})
    assert status == 400 and body["error"] == "invalid"


@pytest.mark.parametrize("request_body", [{"text": 5}, {"text": ["a"]}, {"text": {"a": 1}}, {"text": "ok", "topic": ["a"]}, {"text": "ok", "topic": 3}])
def test_notes_refuse_text_that_is_not_a_string(tmp_path, request_body):
    status, body = _plugin().api_notes_append(request_body, data_dir=tmp_path)
    assert status == 400 and body["error"] == "invalid"
    assert not list(tmp_path.rglob("*.md")) or "a: 1" not in "".join(p.read_text() for p in tmp_path.rglob("*.md"))
