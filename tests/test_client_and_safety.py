"""Behaviour: the probe never sends a write to anything but the made-up UUID, and the client maps
every Coolify answer to one typed error kind."""

import json
import socket
import urllib.error

import pytest

SETUPS = [
    ("scoped_read_deploy", "teamwide_read"),
    ("scoped_read_deploy", None),
    (None, "teamwide_read"),
    ("teamwide_full", None),
    ("teamwide_read_deploy_write_delete", None),
    ("teamwide_read", "teamwide_read_deploy"),
    ("rejected", None),
]


@pytest.mark.parametrize("action,read", SETUPS)
def test_no_non_get_request_reaches_anything_but_the_bogus_uuid(kit, action, read):
    _, fake = kit.report(action=action, read=read)
    writes = [(m, url, body) for m, url, body, _ in fake.requests if m != "GET"]
    assert writes or action == "rejected", "the probe should send its write checks"
    for method, url, body in writes:
        if url == kit.BASE_URL + "/mcp":
            assert method == "POST" and body["method"] == "initialize"
        else:
            assert kit.caps.BOGUS_UUID in url, (method, url)


def test_probe_sends_exactly_the_five_documented_checks(kit):
    _, fake = kit.report(action="teamwide_read")
    bogus = [(m, url.replace(kit.BASE_URL, "")) for m, url, _, _ in fake.requests if kit.caps.BOGUS_UUID in url]
    b = kit.caps.BOGUS_UUID
    assert bogus == [("GET", f"/api/v1/applications/{b}"), ("GET", f"/api/v1/applications/{b}/logs"),
                     ("POST", f"/api/v1/deploy?uuid={b}"), ("PATCH", f"/api/v1/applications/{b}"),
                     ("DELETE", f"/api/v1/applications/{b}")]


def _client(kit, status, payload):
    def transport(method, url, headers, body, timeout):
        raw = payload.encode() if isinstance(payload, str) else json.dumps(payload).encode()
        return kit.client.HttpResponse(status, raw, "application/json")

    return kit.client.CoolifyClient(kit.BASE_URL, "tok", transport=transport)


@pytest.mark.parametrize("status,payload,kind,ability", [
    (401, {"message": "Unauthenticated."}, "unauthorized", None),
    (403, {"message": "Missing required permissions: deploy"}, "forbidden", "deploy"),
    (403, {"message": "Missing required permissions: read:sensitive"}, "forbidden", "read:sensitive"),
    (403, {"message": "This API token is limited to selected projects, environments, or resources, so it "
                      "cannot use this endpoint."}, "scoped_refused", None),
    (404, {"message": "Application not found."}, "not_found", None),
    (422, {"message": "Validation failed."}, "bad_request", None),
    (502, "<html>Bad Gateway</html>", "server", None),
])
def test_client_maps_each_answer_to_one_error_kind(kit, status, payload, kind, ability):
    with pytest.raises(kit.client.CoolifyError) as caught:
        _client(kit, status, payload).get("/applications/x")
    assert caught.value.kind.value == kind
    assert caught.value.ability == ability
    assert caught.value.status == status


def test_client_maps_no_answer_to_unreachable_without_the_token(kit):
    import urllib.error

    def transport(method, url, headers, body, timeout):
        raise urllib.error.URLError("timed out")

    client = kit.client.CoolifyClient(kit.BASE_URL, "secret-token-value", transport=transport)
    with pytest.raises(kit.client.CoolifyError) as caught:
        client.get("/version")
    assert caught.value.kind.value == "unreachable"
    assert "secret-token-value" not in str(caught.value) + repr(client)


@pytest.mark.parametrize("error,sent", [
    (urllib.error.URLError(socket.gaierror(-2, "Name or service not known")), False),
    (urllib.error.URLError(ConnectionRefusedError(111, "Connection refused")), False),
    (urllib.error.URLError("timed out"), True),
    (urllib.error.URLError(ConnectionResetError(104, "Connection reset by peer")), True),
    (urllib.error.URLError(TimeoutError("timed out")), True),
], ids=["dns", "refused", "generic", "reset", "timeout"])
def test_only_proven_pre_send_failures_are_unsent(kit, error, sent):
    def transport(method, url, headers, body, timeout):
        raise error

    client = kit.client.CoolifyClient(kit.BASE_URL, "tok", transport=transport)
    with pytest.raises(kit.client.CoolifyError) as caught:
        client.post("/deploy")
    assert caught.value.kind.value == "unreachable"
    assert caught.value.sent is sent


def test_client_reads_plain_text_version_and_mcp_json(kit):
    assert _client(kit, 200, "4.6.0").get("/version") == "4.6.0"
    result = _client(kit, 200, {"jsonrpc": "2.0", "id": 1, "result": {"serverInfo": {"name": "Coolify"}}})
    assert result.mcp_call("initialize") == {"serverInfo": {"name": "Coolify"}}
    sse = "event: message\ndata: " + json.dumps({"jsonrpc": "2.0", "id": 1, "result": {"ok": True}})
    assert _client(kit, 200, sse).mcp_call("tools/list") == {"ok": True}
