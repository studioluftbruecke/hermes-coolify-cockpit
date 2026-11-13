"""Behaviour: whether a person is there to approve a Coolify action. Unattended runs are refused
before any Coolify request; when Hermes cannot say whether the turn is a one-shot run, the plugin
refuses too (fail closed), because Hermes' own gate approves one-shot runs by itself.
"""

import pytest


class Ctx:
    def __init__(self, surface="cli", cron=False):
        self.surface, self.cron = surface, cron

    def current_cron_execution(self):
        return object() if self.cron else None

    def session_surface(self):
        return self.surface


def _settings(key, default=None):
    return {"COOLIFYBRUECKE_URL": "https://coolify.example.test"}.get(key, default)


def _verdict(plugin, ctx):
    return plugin.action_precheck(_settings, plugin.run_context_from(ctx))


UNKNOWN = "does not say whether a person is present"


@pytest.mark.parametrize("surface", ["cli", "desktop", "tui", "telegram"])
def test_attended_when_hermes_says_the_turn_is_not_a_one_shot(plugin, session_finite, monkeypatch, surface):
    monkeypatch.delenv(plugin.HOST_PROCESS_ENV, raising=False)
    session_finite(False)
    assert _verdict(plugin, Ctx(surface)) is None


@pytest.mark.parametrize("surface", ["cli", "desktop"])
def test_a_one_shot_turn_is_refused(plugin, session_finite, surface):
    session_finite(True)
    refused = _verdict(plugin, Ctx(surface))
    assert refused and refused["error"] == "unattended"


@pytest.mark.parametrize("answer", [None, RuntimeError("moved")])
@pytest.mark.parametrize("surface", ["cli", "desktop"])
def test_when_hermes_cannot_say_the_turn_counts_as_unattended(plugin, session_finite, monkeypatch, answer, surface):
    monkeypatch.delenv(plugin.HOST_PROCESS_ENV, raising=False)
    session_finite(answer)
    refused = _verdict(plugin, Ctx(surface))
    assert refused and refused["error"] == "unattended"
    assert UNKNOWN in refused["message"] and "update the plugin" in refused["message"]


def test_the_hook_blocks_with_no_coolify_request_when_hermes_cannot_say(plugin, kit, session_finite):
    session_finite(None)
    fake = kit.FakeCoolify({})
    secrets = {"COOLIFYBRUECKE_TOKEN": "tok"}.get
    gate = plugin.make_action_gate(_settings, secrets, lambda: plugin.run_context_from(Ctx("cli")),
                                   plugin.ApprovalTickets(), transport=fake)
    decision = gate(tool_name="coolify_action", args={"kind": "restart", "target": "x"}, session_id="s1")
    assert decision["action"] == "block" and UNKNOWN in decision["message"]
    assert fake.requests == []


@pytest.mark.parametrize("env,value", [("HERMES_CRON_SESSION", "1"), ("HERMES_SINGLE_QUERY_SESSION", "1")])
def test_cron_and_single_query_env_are_unattended(plugin, monkeypatch, env, value):
    monkeypatch.setenv(env, value)
    refused = _verdict(plugin, Ctx("desktop"))
    assert refused and refused["error"] == "unattended"


@pytest.mark.parametrize("platform", ["webhook", "msgraph_webhook", "api_server"])
def test_programmatic_platforms_are_unattended(plugin, platform):
    refused = _verdict(plugin, Ctx(platform))
    assert refused and refused["error"] == "unattended"


def test_a_cron_execution_from_ctx_is_unattended(plugin):
    refused = _verdict(plugin, Ctx("desktop", cron=True))
    assert refused and refused["error"] == "unattended"
