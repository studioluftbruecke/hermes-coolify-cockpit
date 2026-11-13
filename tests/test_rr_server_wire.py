"""Behaviour over a real socket: the plugin's own urllib transport against a local HTTP server.

A Coolify behind a login proxy answers API calls with a redirect to the login host, or with the
login page itself. The token must stay with the Coolify address, and a page that is not Coolify's
API must never be read as Coolify's answer.
"""

import importlib
import json

TOKEN = "7|" + "A1b2C3d4E5f6G7h8I9j0" * 2 + "zz"


def _settings(url, **extra):
    values = {"COOLIFYBRUECKE_URL": url, "check_updates": False, **extra}
    return lambda key, default=None: values.get(key, default)


def _secrets(token=TOKEN):
    values = {"COOLIFYBRUECKE_TOKEN": token}
    return values.get


def _coolify_like(kit, token=TOKEN, fixture="teamwide_read_deploy"):
    """A route for LocalHttp that answers like the recorded Coolify for `token`."""
    fake = kit.FakeCoolify({token: fixture})

    def route(method, path, headers):
        response = fake(method, kit.BASE_URL + path, headers, None, 15.0)
        return response.status, {"Content-Type": "application/json"}, response.body

    return route


def _token_seen(server):
    return [r for r in server.requests if TOKEN in (r[2].get("Authorization") or "")]


def _redirect_to(target):
    return lambda method, path, headers: (302, {"Location": target + "/login?rd=" + path}, b"")


# --- a redirect never carries the token to another host -----------------------------------------

def test_a_redirect_on_every_read_keeps_the_token_home_and_says_where_it_pointed(plugin, local_http):
    coolify, elsewhere = local_http["coolify"], local_http["elsewhere"]
    target = f"http://localhost:{elsewhere.port}"
    coolify.route = _redirect_to(target)
    elsewhere.route = lambda m, p, h: (200, {"Content-Type": "text/html"}, b"<!doctype html><p>Sign in</p>")

    report = plugin.capabilities_report(_settings(coolify.url), _secrets(), data_dir=None)

    assert coolify.requests, "the configured address was asked"
    assert elsewhere.requests == [], "nothing may follow the redirect, with or without the token"
    text = json.dumps(report)
    assert f"localhost:{elsewhere.port}" in text, "the answer names where the redirect pointed"
    assert "redirect" in text.lower()
    held = [t.get("abilities") for t in report.get("tokens") or []]
    assert not any(held), f"no right may be claimed from a redirect: {held}"


def test_a_redirected_restart_is_reported_as_not_done_and_the_token_stays_home(plugin, kit, local_http):
    coolify, elsewhere = local_http["coolify"], local_http["elsewhere"]
    answer = _coolify_like(kit)
    moved = f"http://localhost:{elsewhere.port}/moved"

    def route(method, path, headers):
        if method == "POST" and path.startswith("/api/v1/applications/app-shop-staging/restart"):
            return 301, {"Location": moved + path}, b""
        return answer(method, path, headers)

    coolify.route = route
    status, result = plugin.api_action(_settings(coolify.url), _secrets(),
                                       {"kind": "restart", "target": "shop-staging"})

    assert elsewhere.requests == []
    assert status != 200 and result["ok"] is False, result
    assert f"localhost:{elsewhere.port}" in result["message"]
    posts = [r for r in coolify.requests if r[0] == "POST" and "/restart" in r[1]]
    assert len(posts) == 1, "the write went out once, to the configured address only"


def test_the_git_host_token_is_not_sent_on_after_a_redirect(plugin, local_http):
    branches = importlib.import_module(plugin.__name__ + ".branches")
    client = importlib.import_module(plugin.__name__ + ".coolify_client")
    git, elsewhere = local_http["coolify"], local_http["elsewhere"]
    git.route = _redirect_to(f"http://localhost:{elsewhere.port}")
    secret = "ghp_" + "x" * 36

    try:
        branches._http(client.urllib_transport, git.url + "/api/v1/repos/o/r/branches/main",
                       {"Authorization": f"token {secret}"})
        raised = None
    except branches.ReadError as error:
        raised = error

    assert elsewhere.requests == []
    assert raised is not None and raised.kind == "failed"
    assert f"localhost:{elsewhere.port}" in str(raised)
