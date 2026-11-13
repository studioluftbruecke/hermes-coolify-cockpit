"""Behaviour over a real socket: rights are read only from Coolify's own answers.

Coolify checks a token's right before it looks a resource up, so a made-up id answers 404 when
the right is held and 403 naming the right when it is not. Something in front of Coolify that
answers success with an empty body, 204 or `{}` on every path says nothing about rights: the
check names the address as not Coolify's API and claims no right, for the token report, for the
action's own deploy check and for the log reads.
"""

import importlib
import itertools
import json

import pytest

_SERIAL = itertools.count(1)

EMPTY_ANSWERS = {
    "200-empty": (200, {"Content-Type": "text/html"}, b""),
    "204": (204, {}, b""),
    "200-empty-object": (200, {"Content-Type": "application/json"}, b"{}"),
}


def _settings(url):
    values = {"COOLIFYBRUECKE_URL": url, "check_updates": False}
    return lambda key, default=None: values.get(key, default)


@pytest.fixture
def token():
    """A token string per test: the rights check is cached per token."""
    return f"9|{next(_SERIAL):04d}" + "Z1x2C3v4B5n6M7a8S9d0" * 2


def _secrets(token):
    return lambda name, default=None: {"COOLIFYBRUECKE_TOKEN": token}.get(name, default)


def _coolify_like(kit, fixture, token):
    fake = kit.FakeCoolify({token: fixture})

    def route(method, path, headers):
        try:
            response = fake(method, kit.BASE_URL + path, headers, None, 15.0)
        except AssertionError:  # a read the fixture does not record
            return 404, {"Content-Type": "application/json"}, b'{"message":"Not found."}'
        return response.status, {"Content-Type": "application/json"}, response.body

    return route


def _held(report):
    return [right for t in report.get("tokens") or [] for right in t.get("rights") or []]


@pytest.mark.parametrize("answer", list(EMPTY_ANSWERS), ids=list(EMPTY_ANSWERS))
def test_an_empty_success_on_every_path_claims_no_right(plugin, local_http, answer, token):
    server = local_http["coolify"]
    server.route = lambda method, path, headers: EMPTY_ANSWERS[answer]

    report = plugin.capabilities_report(_settings(server.url), _secrets(token), data_dir=None)

    assert _held(report) == [], report["tokens"]
    token = report["tokens"][0]
    assert token["status"] != "ok", token
    assert "not Coolify's API" in json.dumps(report), report


@pytest.mark.parametrize("answer", list(EMPTY_ANSWERS), ids=list(EMPTY_ANSWERS))
def test_an_action_behind_an_empty_success_is_refused_before_its_request(plugin, local_http, answer, token):
    server = local_http["coolify"]
    server.route = lambda method, path, headers: EMPTY_ANSWERS[answer]

    status, body = plugin.api_action(_settings(server.url), _secrets(token), {"kind": "restart", "target": "shop-staging"})

    assert body["ok"] is False and body["sent"] is False, body
    assert "not Coolify's API" in body["message"], body
    assert not [r for r in server.requests if r[1].endswith("/restart")]


def test_log_reads_claim_no_right_from_an_empty_success(plugin, local_http, token):
    server = local_http["coolify"]
    server.route = lambda method, path, headers: EMPTY_ANSWERS["200-empty-object"]
    diagnose = importlib.import_module(plugin.__name__ + ".diagnose")
    connection = plugin._connection(_settings(server.url), _secrets(token))

    logs, answer = diagnose.fetch_deployment_log(connection, "dep-1")

    assert logs is None, answer
    assert not any(t.get("read_sensitive") for t in answer.get("tokens") or []), answer


# --- the everyday setups still read their rights ----------------------------------------------

@pytest.mark.parametrize("fixture, rights", [
    ("teamwide_read", ["read"]),
    ("teamwide_read_deploy", ["read", "deploy"]),
    ("scoped_read_deploy", ["read", "deploy"]),
    ("teamwide_read_deploy_write_delete", ["read", "deploy", "write", "delete"]),
    ("teamwide_full", ["read", "read:sensitive", "deploy"]),
])
def test_coolifys_own_answers_still_give_each_tokens_rights(plugin, kit, local_http, fixture, rights, token):
    server = local_http["coolify"]
    server.route = _coolify_like(kit, fixture, token)

    report = plugin.capabilities_report(_settings(server.url), _secrets(token), data_dir=None)

    assert report["tokens"][0]["status"] == "ok", report["tokens"]
    assert sorted(_held(report)) == sorted(rights)


def test_a_restart_with_coolifys_own_answers_still_goes_out_once(plugin, kit, local_http, token):
    server = local_http["coolify"]
    coolify = _coolify_like(kit, "teamwide_read_deploy", token)

    def route(method, path, headers):
        if method == "POST" and path.startswith("/api/v1/applications/app-shop-staging/restart"):
            return local_http["json"](200, {"message": "Restart request queued.", "deployment_uuid": "dep-9"})
        return coolify(method, path, headers)

    server.route = route
    status, body = plugin.api_action(_settings(server.url), _secrets(token), {"kind": "restart", "target": "shop-staging"})

    assert status == 200 and body["ok"] is True and body["deployment_uuid"] == "dep-9", body
