"""Behaviour: a cleared "Protected environments" field keeps production protected. Letting the agent
act there is the separate switch, so an emptied text field never opens it by accident.
"""

import pytest


@pytest.mark.parametrize("raw", ["", "   ", " , ", []])
def test_an_empty_setting_protects_the_default(plugin, raw):
    values = {plugin.PROTECTED_SETTING: raw}
    get_setting = lambda key, default=None: values.get(key, default)  # noqa: E731
    assert plugin.protected_environments(get_setting) == ["production"]
    assert plugin.is_protected(get_setting, "production")


def test_a_named_list_replaces_the_default(plugin):
    values = {plugin.PROTECTED_SETTING: "shop-live, Staging"}
    get_setting = lambda key, default=None: values.get(key, default)  # noqa: E731
    assert plugin.protected_environments(get_setting) == ["shop-live", "staging"]
    assert not plugin.is_protected(get_setting, "production")
