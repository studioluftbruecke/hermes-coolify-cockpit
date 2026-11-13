"""Behaviour: a deployment id from the agent or an `@coolify:` reference never becomes part of
another API path: anything that is not id-shaped is refused before a request.
"""

import pytest

TOKEN = "tok-read-uuid"


def _settings(key, default=None):
    return {"COOLIFYBRUECKE_URL": "https://coolify.example.test", "check_updates": False}.get(key, default)


class Recording:
    """FakeCoolify for the token's routes; an empty list for any other read."""

    def __init__(self, kit):
        self.fake = kit.FakeCoolify({TOKEN: "teamwide_read_deploy"})
        self.client = kit.client
        self.requests = self.fake.requests

    def __call__(self, method, url, headers, body, timeout):
        try:
            return self.fake(method, url, headers, body, timeout)
        except AssertionError:
            return self.client.HttpResponse(200, b"[]", "application/json")


@pytest.mark.parametrize("bad", ["../applications/app-shop-staging", "dep?take=1000", "dep/../../servers", "a b"])
def test_a_deployment_id_that_is_not_id_shaped_sends_nothing(plugin, kit, bad):
    fake = Recording(kit)
    secrets = {"COOLIFYBRUECKE_TOKEN": TOKEN}.get
    texts = [plugin.deployments_text(_settings, secrets, deployment_uuid=bad, transport=fake, cache_file=None),
             plugin.expand_context_reference(f"deployment/{bad}", _settings, secrets, transport=fake, cache_file=None)]
    asked = [url for _, url, _, _ in fake.requests if "/deployments/" in url and "/deployments/applications/" not in url]
    assert asked == [], asked
    for text in texts:
        assert "not a deployment id" in text, text[-300:]
