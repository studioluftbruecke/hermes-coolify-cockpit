"""Behaviour over a real socket: a failed action says whether its request may have reached Coolify.

`sent: false` means nothing reached Coolify (no connection, a redirect or a page from something in
front of it, a token refused before a byte left), so a new send repeats nothing. `sent: true` on a
failure means Coolify, or the connection after the request left, gave no clean answer: the action
may have run, and the page says so before the user sends again.
"""

import dataclasses
import importlib
import socket
import threading

import pytest

TOKEN = "8|" + "Q1w2E3r4T5y6U7i8O9p0" * 2 + "xy"
RESTART = "/api/v1/applications/app-shop-staging/restart"


def _settings(url):
    values = {"COOLIFYBRUECKE_URL": url, "check_updates": False}
    return lambda key, default=None: values.get(key, default)


def _secrets(name, default=None):
    return {"COOLIFYBRUECKE_TOKEN": TOKEN}.get(name, default)


def _coolify_like(kit):
    fake = kit.FakeCoolify({TOKEN: "teamwide_read_deploy"})

    def route(method, path, headers):
        try:
            response = fake(method, kit.BASE_URL + path, headers, None, 15.0)
        except AssertionError:  # a read the fixture does not record (the app's server name)
            return 404, {"Content-Type": "application/json"}, b'{"message":"Not found."}'
        return response.status, {"Content-Type": "application/json"}, response.body

    return route


def _restart_answers(kit, server, answer):
    coolify = _coolify_like(kit)

    def route(method, path, headers):
        if method == "POST" and path.startswith(RESTART):
            return answer
        return coolify(method, path, headers)

    server.route = route


def _restart(plugin, server):
    status, result = plugin.api_action(_settings(server.url), _secrets, {"kind": "restart", "target": "shop-staging"})
    posts = [r for r in server.requests if r[0] == "POST" and r[1].startswith(RESTART)]
    return status, result, posts


# --- the everyday cases stay as they were ----------------------------------------------------

def test_a_restart_coolify_accepts_is_sent_and_ok(plugin, kit, local_http):
    server = local_http["coolify"]
    _restart_answers(kit, server, local_http["json"](200, {"message": "Restart request queued.",
                                                           "deployment_uuid": "dep-1"}))
    status, result, posts = _restart(plugin, server)
    assert (status, result["ok"], result["sent"], len(posts)) == (200, True, True, 1), result


@pytest.mark.parametrize("answer", [
    (500, {"Content-Type": "application/json"}, b'{"message":"Server Error"}'),
    (502, {"Content-Type": "text/html"}, b"<html>Bad Gateway</html>"),
    (404, {"Content-Type": "application/json"}, b'{"message":"Application not found."}'),
], ids=["coolify-500", "gateway-502", "coolify-404"])
def test_an_answer_after_the_request_left_says_it_may_have_run(plugin, kit, local_http, answer):
    server = local_http["coolify"]
    _restart_answers(kit, server, answer)
    status, result, posts = _restart(plugin, server)
    assert status == 502 and result["ok"] is False and result["sent"] is True, result
    assert len(posts) == 1


# --- a request that never reached Coolify says so ---------------------------------------------

@pytest.mark.parametrize("answer", [
    (302, {"Location": "https://sso.example.net/login"}, b""),
    (200, {"Content-Type": "text/html"}, b"<!doctype html><title>Sign in</title><form>login</form>"),
    (403, {"Content-Type": "text/plain"}, b"Forbidden"),
], ids=["login-redirect", "login-page", "proxy-403"])
def test_an_answer_from_something_in_front_of_coolify_is_not_sent(plugin, kit, local_http, answer):
    server = local_http["coolify"]
    _restart_answers(kit, server, answer)
    status, result, posts = _restart(plugin, server)
    assert status == 502 and result["ok"] is False, result
    assert result["sent"] is False, result
    assert len(posts) == 1, "one request, to the configured address"


def _planned(plugin, kit, local_http):
    server = local_http["coolify"]
    server.route = _coolify_like(kit)
    return plugin.plan_action(_settings(server.url), _secrets, {"kind": "restart", "target": "shop-staging"})


def _execute(plugin, act, **kwargs):
    return importlib.import_module(plugin.__name__ + ".actions").execute(act, **kwargs)


def test_a_refused_connection_is_not_sent(plugin, kit, local_http):
    act = _planned(plugin, kit, local_http)
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        closed_port = probe.getsockname()[1]
    result = _execute(plugin, dataclasses.replace(act, base_url=f"http://127.0.0.1:{closed_port}"))
    assert result["ok"] is False and result["error"] == "unreachable", result
    assert result["sent"] is False, result


def test_a_token_refused_before_sending_is_not_sent(plugin, kit, local_http):
    act = _planned(plugin, kit, local_http)
    broken = dataclasses.replace(act, slot=dataclasses.replace(act.slot, token="8|abc\ndef"))
    before = len(local_http["coolify"].requests)
    result = _execute(plugin, broken)
    assert result["ok"] is False and result["sent"] is False, result
    assert "def" not in result["message"]
    assert len(local_http["coolify"].requests) == before


class _SilentAfterRequest:
    """Reads one request, then either closes without an answer or keeps the connection silent."""

    def __init__(self, close):
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(1)
        self.port = self.sock.getsockname()[1]
        self.received = []
        self.release = threading.Event()
        self.close = close
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self):
        conn, _ = self.sock.accept()
        with conn:
            data = b""
            while b"\r\n\r\n" not in data:
                data += conn.recv(4096)
            self.received.append(data.split(b"\r\n", 1)[0].decode())
            if not self.close:
                self.release.wait(5)


@pytest.mark.parametrize("close", [True, False], ids=["closed-without-answer", "no-answer-in-time"])
def test_a_request_that_left_without_an_answer_may_have_run(plugin, kit, local_http, close):
    act = _planned(plugin, kit, local_http)
    silent = _SilentAfterRequest(close)
    try:
        result = _execute(plugin, dataclasses.replace(act, base_url=f"http://127.0.0.1:{silent.port}"), timeout=0.5)
    finally:
        silent.release.set()
        silent.sock.close()
    assert silent.received and silent.received[0].startswith("POST " + RESTART), silent.received
    assert result["ok"] is False and result["error"] == "unreachable", result
    assert result["sent"] is True, result
