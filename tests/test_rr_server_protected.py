"""Behaviour: an empty or invalid "Protected environments" field keeps production protected.
Letting the agent act there is the separate switch, so a bad value never opens it by accident.
"""

import pytest


def _setting(plugin, raw):
    values = {plugin.PROTECTED_SETTING: raw}
    return lambda key, default=None: values.get(key, default)  # noqa: E731


@pytest.mark.parametrize("raw", [None, "", "   ", " , ", [], ()])
def test_an_empty_setting_protects_the_default(plugin, raw):
    get_setting = _setting(plugin, raw)
    assert plugin.protected_environments(get_setting) == ["production"]
    assert plugin.is_protected(get_setting, "production")


@pytest.mark.parametrize("raw", [False, True, 0, 1, "false", "0", "off", "no", "NO"])
def test_a_boolean_or_false_like_setting_protects_the_default(plugin, raw):
    get_setting = _setting(plugin, raw)
    assert plugin.protected_environments(get_setting) == ["production"]
    assert plugin.is_protected(get_setting, "production")


@pytest.mark.parametrize("raw", [
    "shop-live, Staging",
    ["Shop-Live", " staging ", "shop-live"],
    ("shop-live", "Staging"),
])
def test_a_named_list_replaces_the_default(plugin, raw):
    get_setting = _setting(plugin, raw)
    assert plugin.protected_environments(get_setting) == ["shop-live", "staging"]
    assert not plugin.is_protected(get_setting, "production")


def test_an_invalid_setting_warns_through_the_capabilities_report(plugin, kit):
    values = {"COOLIFYBRUECKE_URL": kit.BASE_URL, "check_updates": False,
              plugin.PROTECTED_SETTING: "false"}
    fake = kit.FakeCoolify({"tok": "teamwide_read"})
    secrets = {"COOLIFYBRUECKE_TOKEN": "tok"}.get
    get_setting = lambda key, default=None: values.get(key, default)  # noqa: E731

    out = plugin.capabilities_report(get_setting, secrets, transport=fake, data_dir=None)

    assert out["protected"]["environments"] == ["production"]
    assert any("protected_environments" in warning and "production" in warning
               for warning in out["warnings"])
