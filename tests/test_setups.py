"""Behaviour: every token setup from PRODUCT.md's 'Setups and what they unlock' table, plus an
unreachable instance and a rejected token, gives the right available / not-available lists."""

import json

import pytest

ALL = ["connection", "resources", "team_view", "deployments", "servers", "server_stats", "actions",
       "live_logs", "update_notice"]


def ids(entries):
    return [e.split(":", 1)[0] for e in entries]


def missing(out):
    return {e["feature"].split(":", 1)[0]: e for e in out["not_available"]}


def without(*names):
    return [f for f in ALL if f not in names]


def test_recommended_pair_unlocks_everything_but_live_logs(kit):
    out, _ = kit.report(action="scoped_read_deploy", read="teamwide_read")
    assert ids(out["available"]) == without("live_logs")
    assert list(missing(out)) == ["live_logs"]
    assert "paste failed logs" in missing(out)["live_logs"]["unlock"]
    assert "read:sensitive` to the action token's scope" in missing(out)["live_logs"]["unlock"]
    assert out["routing"] == {"reads": "overview", "servers": "overview", "logs": None, "actions": "action"}
    action, overview = out["tokens"]
    assert (action["role"], action["scope"], action["rights"]) == ("action", "scoped", ["read", "deploy"])
    assert action["sees"]["applications"] == 8
    assert action["sees"]["environments_by_name"] == {"staging": 5}
    assert action["sees"]["servers"].startswith("refused")
    assert (overview["role"], overview["scope"], overview["rights"]) == ("overview", "team-wide", ["read"])
    assert overview["sees"]["servers"] == 2
    assert out["warnings"] == []


def test_single_scoped_token_names_server_stats_and_partial_view(kit):
    out, _ = kit.report(action="scoped_read_deploy")
    assert ids(out["available"]) == ["connection", "resources", "deployments", "actions", "update_notice"]
    gone = missing(out)
    assert list(gone) == ["team_view", "servers", "server_stats", "live_logs"]
    assert gone["server_stats"]["reason"] == "No server stats (scoped tokens cannot read servers)"
    assert gone["team_view"]["reason"] == "Only the 5 project(s) inside the token's scope are visible"
    assert "COOLIFY_COCKPIT_READ_TOKEN" in gone["servers"]["unlock"]
    assert "the single token's scope" in gone["live_logs"]["unlock"]
    assert out["tokens"][0]["role"] == "single"
    assert out["routing"]["actions"] == "single"


def test_single_teamwide_read_token_is_a_read_only_cockpit(kit):
    out, _ = kit.report(read="teamwide_read")
    assert ids(out["available"]) == without("actions", "live_logs")
    gone = missing(out)
    assert gone["actions"]["reason"] == "No actions (the single token lacks `deploy`)"
    assert "deploy" in gone["actions"]["unlock"]
    assert gone["live_logs"]["reason"] == "No live logs (no token holds `read:sensitive`)"
    assert out["tokens"][0]["setting"] == "COOLIFY_COCKPIT_READ_TOKEN"
    assert out["instance"]["metrics_enabled_servers"] == "2/2"


def test_single_full_access_token_unlocks_everything_and_says_what_that_means(kit):
    out, _ = kit.report(action="teamwide_full")
    assert ids(out["available"]) == ALL
    assert out["not_available"] == []
    text = " ".join(out["warnings"])
    assert "production included" in text
    assert "read:sensitive" in text and "reach the model" in text
    assert out["routing"] == {"reads": "single", "servers": "single", "logs": "single", "actions": "single"}


def test_write_and_delete_rights_are_reported_not_used(kit):
    out, _ = kit.report(action="teamwide_read_deploy_write_delete")
    assert ids(out["available"]) == without("live_logs")
    assert out["tokens"][0]["rights"] == ["read", "deploy", "write", "delete"]
    assert any("write and delete" in w and "does not use these rights" in w for w in out["warnings"])


def test_unreachable_instance_unlocks_nothing_and_says_why(kit):
    out, fake = kit.report(action="scoped_read_deploy", read="teamwide_read", unreachable=True)
    assert out["available"] == []
    assert ids([e["feature"] for e in out["not_available"]]) == ALL
    assert all("did not answer" in e["reason"] for e in out["not_available"])
    assert "COOLIFY_COCKPIT_URL" in out["not_available"][0]["unlock"]
    assert out["instance"]["reachable"] is False
    assert {t["status"] for t in out["tokens"]} == {"unreachable"}
    assert len(fake.requests) == 2  # one per token, then it stops


def test_rejected_token_unlocks_nothing_and_says_which_setting(kit):
    out, fake = kit.report(action="rejected")
    assert out["available"] == []
    assert all("rejected (401" in e["reason"] for e in out["not_available"])
    assert "COOLIFY_COCKPIT_TOKEN" in out["not_available"][0]["unlock"]
    assert out["tokens"][0]["status"] == "unauthorized"
    assert any("401" in w for w in out["warnings"])
    assert [r[0] for r in fake.requests if r[3]] == ["GET"]  # stops after the first 401


def test_rejected_overview_token_leaves_the_action_token_working(kit):
    out, _ = kit.report(action="scoped_read_deploy", read="rejected")
    assert "actions" in ids(out["available"])
    gone = missing(out)
    assert gone["servers"]["reason"].endswith("the overview token was rejected (401 Unauthenticated)")


def test_actions_never_fall_back_to_the_overview_token(kit):
    out, _ = kit.report(action="scoped_read_deploy", read="teamwide_read_deploy")
    assert out["routing"]["actions"] == "action"
    out, _ = kit.report(action="teamwide_read", read="teamwide_read_deploy")
    gone = missing(out)
    assert "actions" in gone
    assert "never use the overview token" in gone["actions"]["reason"]
    assert out["routing"]["actions"] is None
    assert any("overview token also holds deploy" in w for w in out["warnings"])


def test_update_notice_compares_with_the_release_feed(kit):
    out, _ = kit.report(action="scoped_read_deploy")
    assert out["instance"]["coolify_version"] == "4.6.0"
    assert out["instance"]["latest_version"] == "4.7.1"
    assert out["instance"]["update_available"] is True


def test_update_check_off_sends_nothing_to_the_feed(kit):
    out, fake = kit.report(action="scoped_read_deploy", check_updates=False)
    assert missing(out)["update_notice"]["reason"].startswith("The update check is switched off")
    assert not [r for r in fake.requests if "cdn.coollabs.io" in r[1]]
    assert out["instance"]["update_check"] == "off"


@pytest.mark.parametrize("url,secrets,expected", [
    ("", {"COOLIFY_COCKPIT_TOKEN": "x"}, "No Coolify URL"),
    ("ftp://coolify.example.test", {"COOLIFY_COCKPIT_TOKEN": "x"}, "http"),
    ("https://coolify.example.test", {}, "No Coolify token"),
])
def test_missing_settings_explain_what_to_set(kit, url, secrets, expected):
    out = kit.plugin.capabilities_report(kit.settings(url), secrets.get, transport=kit.FakeCoolify({}))
    assert out["ok"] is False and out["error"] == "not_configured"
    assert expected in out["message"]


def test_same_token_in_both_fields_is_one_single_token(kit):
    connection = kit.caps.connection_from_settings(kit.BASE_URL + "/api/v1/", "same", "same")
    assert [s.role.value for s in connection.slots] == ["single"]
    assert connection.base_url == kit.BASE_URL
    assert connection.notes


def test_output_is_capped_and_carries_no_token(kit):
    out, _ = kit.report(action="scoped_read_deploy", read="teamwide_read")
    text = json.dumps(out)
    assert len(text) <= kit.caps.MAX_OUTPUT_CHARS
    assert "tok-action" not in text and "tok-read" not in text


def test_handler_returns_json_and_never_raises(kit):
    def broken_setting(key, default=None):
        raise RuntimeError("config unreadable")

    handler = kit.plugin.make_capabilities_handler(broken_setting, lambda name: None)
    out = json.loads(handler({}))
    assert out == {"ok": False, "error": "internal", "message": "RuntimeError: config unreadable"}
