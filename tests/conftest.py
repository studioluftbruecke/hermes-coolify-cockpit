"""Shared test helpers: load the plugin package without Hermes, and replay recorded responses.

The fixtures under `fixtures/` carry Coolify 4.6.0 response shapes with synthetic values. A
`FakeCoolify` answers by token (from the Authorization header) and records every request, so tests
can assert what was sent as well as what came back.
"""

import importlib.util
import json
import pathlib
import sys
import urllib.error
import urllib.parse

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
FIXTURES = pathlib.Path(__file__).resolve().parent / "fixtures"
BASE_URL = "https://coolify.example.test"


def load_plugin():
    name = "coolify_cockpit_under_test"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(
        name, ROOT / "__init__.py", submodule_search_locations=[str(ROOT)])
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def fixture(*parts):
    return json.loads(FIXTURES.joinpath(*parts).read_text(encoding="utf-8"))


class FakeCoolify:
    """Transport that replays fixtures. `tokens` maps a placeholder token string to a fixture name."""

    def __init__(self, tokens, *, unreachable=False):
        self.instance = fixture("instance.json")
        self.tokens = {tok: fixture("tokens", f"{name}.json") for tok, name in tokens.items()}
        self.unreachable = unreachable
        self.requests = []  # (method, url, json body or None, token or None)

    def __call__(self, method, url, headers, body, timeout):
        client = importlib.import_module(load_plugin().__name__ + ".coolify_client")
        auth = headers.get("Authorization", "")
        token = auth[len("Bearer "):] if auth.startswith("Bearer ") else None
        self.requests.append((method, url, json.loads(body) if body else None, token))
        if self.unreachable:
            raise urllib.error.URLError("[Errno -2] Name or service not known")
        if token is None:
            status, payload = self.instance["public_routes"][f"{method} {url}"]
            return self._response(client, status, payload)
        assert url.startswith(BASE_URL), url
        parsed = urllib.parse.urlsplit(url)
        key = f"{method} {parsed.path}" + (f"?{parsed.query}" if parsed.query else "")
        data = self.tokens[token]
        if key in data["routes"]:
            status, payload = data["routes"][key]
        elif data.get("authenticated") and key in self.instance["authenticated_routes"]:
            status, payload = self.instance["authenticated_routes"][key]
        elif "default" in data:
            status, payload = data["default"]
        else:
            raise AssertionError(f"unexpected request {key}")
        return self._response(client, status, payload)

    @staticmethod
    def _response(client, status, payload):
        raw = payload.encode() if isinstance(payload, str) else json.dumps(payload).encode()
        return client.HttpResponse(status, raw, "application/json")


@pytest.fixture(autouse=True)
def own_hermes_home(tmp_path_factory, monkeypatch):
    """A fresh profile home per test. With Hermes importable (check.sh puts its source on
    PYTHONPATH), the plugin keeps its capability check and notebook in
    `<HERMES_HOME>/plugin-data/coolify-cockpit/`, so a shared home lets one test's cached check
    answer the next test's report. Kept out of `tmp_path`, which some tests list. The caller's
    session platform is cleared too: an agent shell sets HERMES_SESSION_PLATFORM, which the run
    context falls back to when a test's session has no surface."""
    home = tmp_path_factory.mktemp("hermes-home")
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.delenv("HERMES_SESSION_PLATFORM", raising=False)
    return home


@pytest.fixture
def plugin():
    return load_plugin()


class Kit:
    """What the tests need, handed over as one fixture (importlib mode has no conftest imports)."""

    BASE_URL = BASE_URL
    FakeCoolify = FakeCoolify
    fixture = staticmethod(fixture)

    def __init__(self):
        self.plugin = load_plugin()
        self.caps = importlib.import_module(self.plugin.__name__ + ".capabilities")
        self.client = importlib.import_module(self.plugin.__name__ + ".coolify_client")

    def settings(self, url=BASE_URL, check_updates=True):
        values = {"COOLIFY_COCKPIT_URL": url, "check_updates": check_updates}
        return lambda key, default=None: values.get(key, default)

    def report(self, action=None, read=None, *, check_updates=True, unreachable=False, url=BASE_URL):
        """Run the tool's report for a setup: `action`/`read` name token fixtures (or None).

        Each fixture gets its own token string: the capability check is cached per token
        fingerprint, so two setups sharing a string would share one check."""
        tokens = {}
        if action:
            tokens[f"tok-action-{action}"] = action
        if read:
            tokens[f"tok-read-{read}"] = read
        fake = FakeCoolify(tokens, unreachable=unreachable)
        secrets = {"COOLIFY_COCKPIT_TOKEN": f"tok-action-{action}" if action else None,
                   "COOLIFY_COCKPIT_READ_TOKEN": f"tok-read-{read}" if read else None}
        out = self.plugin.capabilities_report(self.settings(url, check_updates), secrets.get, transport=fake)
        return out, fake


@pytest.fixture
def kit():
    return Kit()
