"""A 403 that Coolify's rights check never wrote is not a missing right.

Measured on a real setup: a reverse proxy with an IP allow-list in front of Coolify answered every
request, `/api/health` included, with `403` and the plain-text body `Forbidden`. The cockpit read
each of those as "the token lacks this right" and told the user to give the token `read`, which
was already there. Coolify itself refuses the same way before any token check when its API is
switched off or the caller is outside its allowed IPs (app/Http/Middleware/ApiAllowed.php,
ApiIpAllowed.php). Each of these means: Coolify's API cannot be reached from this machine.
"""

import json

import pytest

PROXY = (403, "Forbidden")
API_OFF = (403, {"success": False, "message": "API is disabled."})
NOT_ALLOWED = (403, {"success": False, "message": "You are not allowed to access the API."})


def _gate(kit, answer):
    status, payload = answer
    seen = []

    def transport(method, url, headers, body, timeout):
        seen.append((method, url))
        raw = payload.encode() if isinstance(payload, str) else json.dumps(payload).encode()
        return kit.client.HttpResponse(status, raw, "" if isinstance(payload, str) else "application/json")

    return transport, seen


def _report(kit, answer):
    transport, seen = _gate(kit, answer)
    secrets = {"COOLIFYBRUECKE_TOKEN": "tok-gate-" + str(answer[1])[:12], "COOLIFYBRUECKE_READ_TOKEN": None}
    return kit.plugin.capabilities_report(kit.settings(), secrets.get, transport=transport), seen


@pytest.mark.parametrize("answer,words", [
    (PROXY, "proxy or firewall"),
    (API_OFF, "API is switched off"),
    (NOT_ALLOWED, "allowed IPs"),
])
def test_a_gate_403_is_unreachable_not_a_missing_right(kit, answer, words):
    status, payload = answer
    with pytest.raises(kit.client.CoolifyError) as caught:
        kit.client.CoolifyClient(kit.BASE_URL, "tok", transport=_gate(kit, answer)[0]).get("/applications/x")
    assert caught.value.kind.value == "unreachable"
    assert caught.value.status == 403
    assert caught.value.ability is None
    assert words in caught.value.message


@pytest.mark.parametrize("answer,words", [
    (PROXY, "proxy or firewall"),
    (API_OFF, "API is switched off"),
    (NOT_ALLOWED, "allowed IPs"),
])
def test_capabilities_behind_a_gate_say_why_and_never_blame_the_token(kit, answer, words):
    out, seen = _report(kit, answer)
    assert out["available"] == []
    assert out["instance"]["reachable"] is False
    assert words in out["instance"]["error"]
    assert out["tokens"][0]["status"] == "unreachable"
    reasons = " ".join(e["reason"] for e in out["not_available"])
    assert "No token holds" not in reasons and "lacks" not in reasons
    assert all(words in e["reason"] for e in out["not_available"])
    assert "could not be reached" in out["setup"]["line"]
    assert len(seen) == 1  # the first gate answer stops the probes


def test_a_json_403_from_coolify_without_a_named_right_stays_forbidden(kit):
    """Laravel's own refusal ("This action is unauthorized.") comes from Coolify: not a gate."""
    answer = (403, {"message": "This action is unauthorized."})
    with pytest.raises(kit.client.CoolifyError) as caught:
        kit.client.CoolifyClient(kit.BASE_URL, "tok", transport=_gate(kit, answer)[0]).get("/applications/x")
    assert caught.value.kind.value == "forbidden"
