"""Behaviour over a real socket: a web page answering 200 (a login proxy in front of Coolify) is an
error, never Coolify's answer. Before, it read as every right held, the HTML was quoted as the
Coolify version, the overview said nothing needs the user, and the alert job announced recoveries.
"""

import importlib
import json

HTML = b"<!doctype html><html><head><title>Sign in</title></head><body><form>login</form></body></html>"
TOKEN = "31|" + "K" * 40


def _settings(url):
    values = {"COOLIFYBRUECKE_URL": url, "check_updates": False}
    return lambda key, default=None: values.get(key, default)


def _secrets(token=TOKEN):
    return {"COOLIFYBRUECKE_TOKEN": token}.get


def _login_page(method, path, headers):
    return 200, {"Content-Type": "text/html"}, HTML


def test_a_login_page_claims_no_right_and_is_named_as_a_web_page(plugin, local_http):
    server = local_http["coolify"]
    server.route = _login_page
    report = plugin.capabilities_report(_settings(server.url), _secrets(), data_dir=None)
    text = json.dumps(report)
    assert "<!doctype" not in text.lower(), "the page is never quoted to the model"
    for token in report.get("tokens") or []:
        assert not token.get("abilities"), f"no right from a web page: {token.get('abilities')}"
    assert "web page" in text, text[:500]


def test_the_overview_over_a_login_page_is_an_outage_not_an_all_clear(plugin, local_http):
    server = local_http["coolify"]
    server.route = _login_page
    text = plugin.overview_text(_settings(server.url), _secrets(), data_dir=None, cache_file=None)
    assert "<!doctype" not in text.lower()
    assert "NEEDS YOU: nothing" not in text
    assert "web page" in text


def test_the_version_route_still_answers_a_plain_version_string(plugin, local_http):
    client_mod = importlib.import_module(plugin.__name__ + ".coolify_client")
    server = local_http["coolify"]
    server.route = lambda m, p, h: (200, {"Content-Type": "text/html; charset=UTF-8"}, b"4.0.0-beta.420.6")
    assert client_mod.CoolifyClient(server.url, TOKEN).get("/version") == "4.0.0-beta.420.6"


def test_the_alert_job_never_announces_a_recovery_from_a_login_page(plugin, kit, local_http, tmp_path):
    server = local_http["coolify"]
    fake = kit.FakeCoolify({TOKEN: "teamwide_read_deploy"})
    proxy = {"on": False}

    def route(method, path, headers):
        if proxy["on"]:
            return _login_page(method, path, headers)
        response = fake(method, kit.BASE_URL + path, headers, None, 15.0)
        body = response.body
        if method == "GET" and path == "/api/v1/applications":
            rows = json.loads(body)
            rows[1]["status"] = "running:unhealthy"
            body = json.dumps(rows).encode()
        return response.status, {"Content-Type": "application/json"}, body

    server.route = route
    gs, sec = _settings(server.url), _secrets()
    facts = {}
    for i in range(3):
        _, facts = plugin.tick_report(gs, sec, data_dir=tmp_path, cache_file=None, git_transport=None,
                                      now=1_000_000 + i * 300)
    assert facts.get("open"), "the unhealthy app is open before the proxy takes over"
    proxy["on"] = True
    recovered = 0
    for i in range(3, 6):
        text, facts = plugin.tick_report(gs, sec, data_dir=tmp_path, cache_file=None, git_transport=None,
                                         now=1_000_000 + i * 300)
        recovered += facts.get("recovered") or 0
        assert "Recovered" not in text, text
    assert recovered == 0
