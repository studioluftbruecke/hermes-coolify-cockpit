"""Behaviour: a token that cannot go into an HTTP header is refused by name, and no result ever
quotes it. A token pasted across a line wrap (or a quoted `.env` value holding `\\n`) used to reach
http.client, whose error quotes the whole header, and the handlers printed that error to the model.
"""

import importlib
import json

TAIL = "Zq9Zq9Zq9SECRETTAIL"
BROKEN = "7|" + "A1b2C3d4E5f6G7h8I9j0" * 2 + "\n" + TAIL


def _settings(url):
    values = {"COOLIFYBRUECKE_URL": url, "check_updates": False}
    return lambda key, default=None: values.get(key, default)


def _secrets(token=BROKEN):
    return {"COOLIFYBRUECKE_TOKEN": token}.get


def _surfaces(plugin, url):
    gs, sec = _settings(url), _secrets()
    return {
        "coolify_capabilities": lambda: plugin.make_capabilities_handler(gs, sec)({}),
        "coolify_overview": lambda: plugin.overview_text(gs, sec, data_dir=None, cache_file=None),
        "GET /capabilities": lambda: json.dumps(plugin.api_capabilities(gs, sec, data_dir=None)),
        "GET /snapshot": lambda: json.dumps(plugin.api_snapshot(gs, sec, cache_file=None)),
        "POST /actions": lambda: json.dumps(plugin.api_action(gs, sec, {"kind": "restart", "target": "x"})),
        "GET /actions": lambda: json.dumps(plugin.api_action_catalog(gs, sec)),
        "@coolify:overview": lambda: plugin.expand_context_reference("overview", gs, sec, cache_file=None),
    }


def test_a_token_with_a_line_break_is_refused_by_name_on_every_surface(plugin, local_http):
    server = local_http["coolify"]
    for name, call in _surfaces(plugin, server.url).items():
        text = call()
        assert TAIL not in text, f"{name} quoted the token"
        assert "COOLIFYBRUECKE_TOKEN" in text, f"{name} does not name the setting to fix: {text[:300]}"
    assert server.requests == [], "nothing is sent with a token that cannot be a header"


def test_the_client_never_quotes_a_header_it_could_not_send(plugin, local_http):
    client_mod = importlib.import_module(plugin.__name__ + ".coolify_client")
    client = client_mod.CoolifyClient(local_http["coolify"].url, BROKEN)
    try:
        client.get("/version")
        raised = None
    except client_mod.CoolifyError as error:
        raised = error
    assert raised is not None
    assert TAIL not in str(raised) and TAIL not in raised.message


def test_a_git_host_token_with_a_line_break_is_never_quoted(plugin, local_http):
    branches = importlib.import_module(plugin.__name__ + ".branches")
    client_mod = importlib.import_module(plugin.__name__ + ".coolify_client")
    try:
        branches._http(client_mod.urllib_transport, local_http["coolify"].url + "/x",
                       {"Authorization": "token ghp_abc\n" + TAIL})
        raised = None
    except branches.ReadError as error:
        raised = error
    assert raised is not None and TAIL not in str(raised)


def test_an_unexpected_error_reports_its_type_but_never_its_text(plugin):
    def secret_raising(name):
        raise RuntimeError("leaked " + TAIL)

    gs = _settings("https://coolify.example.test")
    status, body = plugin.api_action(gs, secret_raising, {"kind": "restart", "target": "x"})
    assert TAIL not in json.dumps(body)
    assert "RuntimeError" in body["message"]
    tool = plugin.make_capabilities_handler(gs, secret_raising)({})
    assert TAIL not in tool and "RuntimeError" in tool
