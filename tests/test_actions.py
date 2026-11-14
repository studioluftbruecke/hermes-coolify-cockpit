"""Behaviour: the ActionSpec registry, the rights and view checks, `POST /actions`, and the
agent lane (approval hook, handler, unattended refusal), against a synthetic instance. No network.

The instance has one project `shop` with a `staging` environment (inside the scoped token's view)
and a `production` environment (visible only to team-wide tokens).
"""

import importlib
import importlib.util
import json
import pathlib
import re
import sys
import urllib.parse

import pytest

BASE = "https://coolify.example.test"
BOGUS = "hermes-permission-probe-does-not-exist"
DENIED = {"message": "Missing required permissions: deploy"}
SCOPED = {"message": "This API token is limited to selected projects, environments, or resources, so it "
                     "cannot use this endpoint."}

APPS = {
    "app-stg": {"uuid": "app-stg", "name": "shop-web-staging", "environment_id": 11, "status": "running:healthy",
                "fqdn": "https://staging.shop.example.test",
                "destination": {"server": {"name": "app-1"}}},
    "app-prod": {"uuid": "app-prod", "name": "shop-web", "environment_id": 12, "status": "running:healthy",
                 "fqdn": "https://shop.example.test", "destination": {"server": {"name": "app-1"}}},
}
SERVICES = {"svc-stg": {"uuid": "svc-stg", "name": "umami-staging", "environment_id": 11, "status": "running:healthy",
                        "server": {"name": "app-2"}}}
DATABASES = {"db-stg": {"uuid": "db-stg", "name": "pg-staging", "environment_id": 11, "status": "running:healthy",
                        "destination": {"server": {"name": "app-2"}}}}
DEPLOYMENTS = {
    "dep-run": {"deployment_uuid": "dep-run", "application_uuid": "app-stg", "application_name": "shop-web-staging",
                "status": "in_progress", "server_name": "app-1"},
    "dep-done": {"deployment_uuid": "dep-done", "application_uuid": "app-stg", "application_name": "shop-web-staging",
                 "status": "finished", "server_name": "app-1"},
    "dep-prod": {"deployment_uuid": "dep-prod", "application_uuid": "app-prod", "application_name": "shop-web",
                 "status": "in_progress", "server_name": "app-1"},
}

# token -> (scoped, holds deploy)
TOKENS = {
    "tok-act": (True, True),          # a scoped API token: read + deploy
    "tok-act-nodeploy": (True, False),  # scoped read only
    "tok-read": (False, False),       # overview: team-wide read
    "tok-read-deploy": (False, True),  # overview that also holds deploy
    "tok-full": (False, True),        # single team-wide token with deploy
}

ACTION_ANSWERS = {
    "/api/v1/deploy": {"deployments": [{"message": "Application shop-web-staging deployment queued.",
                                        "resource_uuid": "app-stg", "deployment_uuid": "dep-new"}]},
    "/api/v1/applications/app-stg/restart": {"message": "Restart request queued.", "deployment_uuid": "dep-rst"},
    "/api/v1/applications/app-stg/start": {"message": "Deployment request queued.", "deployment_uuid": "dep-sta"},
    "/api/v1/applications/app-stg/stop": {"message": "Application stopping request queued."},
    "/api/v1/applications/app-stg/rollback": {"message": "Rollback queued.", "deployment_uuid": "dep-rb"},
    "/api/v1/deployments/dep-run/cancel": {"message": "Deployment cancelled successfully.",
                                           "deployment_uuid": "dep-run", "status": "cancelled-by-user"},
}


class Instance:
    def __init__(self):
        self.requests = []  # (method, path, query dict, json body, token)

    def __call__(self, method, url, headers, body, timeout):
        client = sys.modules[_plugin().__name__ + ".coolify_client"]
        token = headers.get("Authorization", "")[len("Bearer "):]
        parsed = urllib.parse.urlsplit(url)
        query = dict(urllib.parse.parse_qsl(parsed.query))
        self.requests.append((method, parsed.path, query, json.loads(body) if body else None, token))
        scoped, deploy = TOKENS[token]

        def answer(status, payload):
            return client.HttpResponse(status, json.dumps(payload).encode(), "application/json")

        def visible(row):
            return not scoped or row.get("environment_id") == 11

        path = parsed.path[len("/api/v1"):]
        if method == "GET":
            lists = {"/applications": APPS, "/services": SERVICES, "/databases": DATABASES}
            if path in lists:
                return answer(200, [r for r in lists[path].values() if visible(r)])
            if path == "/projects":
                return answer(200, [{"uuid": "proj-shop", "name": "shop"}])
            if path == "/projects/proj-shop":
                envs = [{"id": 11, "uuid": "env-stg", "name": "staging"}]
                if not scoped:
                    envs.append({"id": 12, "uuid": "env-prod", "name": "production"})
                return answer(200, {"uuid": "proj-shop", "name": "shop", "environments": envs})
            if path == "/servers":
                return answer(403, SCOPED) if scoped else answer(200, [])
            for prefix, table in (("/applications/", APPS), ("/services/", SERVICES), ("/databases/", DATABASES)):
                uuid = path[len(prefix):]
                if path.startswith(prefix) and uuid in table and visible(table[uuid]):
                    return answer(200, table[uuid])
            if path.startswith("/deployments/"):
                row = DEPLOYMENTS.get(path[len("/deployments/"):])
                if row and visible(APPS[row["application_uuid"]]):
                    return answer(200, row)
            return answer(404, {"message": "Not found."})
        # Non-GET: the right is checked before the lookup (measured against Coolify 4.6).
        if not deploy:
            return answer(403, DENIED)
        if path == "/deploy" and query.get("uuid") == BOGUS:
            return answer(404, {"message": "No resources found."})
        if parsed.path in ACTION_ANSWERS:
            return answer(200, ACTION_ANSWERS[parsed.path])
        if re.match(r"^/api/v1/(applications|services|databases)/[^/]+/(restart|start|stop)$", parsed.path):
            return answer(200, {"message": "Request queued."})
        return answer(404, {"message": "Not found."})

    def actions_sent(self):
        """Every request that could change something: non-GETs other than the made-up-UUID probe."""
        return [r for r in self.requests if r[0] != "GET" and r[2].get("uuid") != BOGUS]


_LOADED = {}


def _plugin():
    if "pkg" not in _LOADED:
        root = pathlib.Path(__file__).resolve().parents[1]
        name = "coolifybruecke_m4_under_test"
        spec = importlib.util.spec_from_file_location(name, root / "__init__.py",
                                                      submodule_search_locations=[str(root)])
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
        importlib.import_module(name + ".actions")
        _LOADED["pkg"] = module
    return _LOADED["pkg"]


@pytest.fixture
def pkg():
    plugin = _plugin()
    acts = sys.modules[plugin.__name__ + ".actions"]
    acts.ABILITY_CACHE._seen.clear()
    acts.VIEW_CACHE._views.clear()
    return plugin


def acts():
    return sys.modules[_plugin().__name__ + ".actions"]


def settings(**extra):
    values = {"COOLIFYBRUECKE_URL": BASE, **extra}
    return lambda key, default=None: values.get(key, default)


def secrets(action="tok-act", read="tok-read"):
    return {"COOLIFYBRUECKE_TOKEN": action, "COOLIFYBRUECKE_READ_TOKEN": read}.get


# --- the registry and the requests it builds ---------------------------------------------------

EXPECTED = [
    # kind, target, params, (path, query, json body)
    ("deploy", "shop-web-staging", None, ("/api/v1/deploy", {"uuid": "app-stg"}, {})),
    ("force_deploy", "app-stg", None, ("/api/v1/deploy", {"uuid": "app-stg", "force": "true"}, {})),
    ("restart", "shop-web-staging", None, ("/api/v1/applications/app-stg/restart", {}, {})),
    ("start", "app-stg", None, ("/api/v1/applications/app-stg/start", {}, {})),
    ("stop", "shop-web-staging", None, ("/api/v1/applications/app-stg/stop", {"docker_cleanup": "false"}, {})),
    ("cancel", "dep-run", None, ("/api/v1/deployments/dep-run/cancel", {}, {})),
    ("rollback", "app-stg", {"commit": "4f2a9c1"}, ("/api/v1/applications/app-stg/rollback", {}, {"commit": "4f2a9c1"})),
    ("restart", "umami-staging", None, ("/api/v1/services/svc-stg/restart", {}, {})),
    ("stop", "umami-staging", None, ("/api/v1/services/svc-stg/stop", {"docker_cleanup": "false"}, {})),
    ("start", "pg-staging", None, ("/api/v1/databases/db-stg/start", {}, {})),
    ("stop", "pg-staging", None, ("/api/v1/databases/db-stg/stop", {"docker_cleanup": "false"}, {})),
    ("deploy", "umami-staging", None, ("/api/v1/deploy", {"uuid": "svc-stg"}, {})),
]


def test_registry_has_the_seven_actions_and_stop_pins_docker_cleanup(pkg):
    assert [s.kind for s in acts().ACTION_SPECS] == ["deploy", "force_deploy", "restart", "start", "stop",
                                                     "cancel", "rollback"]
    assert all(s.ability.value == "deploy" and s.method == "POST" for s in acts().ACTION_SPECS)
    assert acts().SPECS["stop"].fixed_params == {"docker_cleanup": "false"}


@pytest.mark.parametrize("kind,target,params,expected", EXPECTED)
def test_each_spec_sends_exactly_its_request_with_the_action_token(pkg, kind, target, params, expected):
    fake = Instance()
    status, body = pkg.api_action(settings(), secrets(), {"kind": kind, "target": target, "params": params or {}},
                                  transport=fake)
    assert status == 200 and body["ok"] and body["sent"], body
    sent = fake.actions_sent()
    assert [(m, p, q, b) for m, p, q, b, _ in sent] == [("POST",) + expected]
    assert {t for *_, t in fake.requests} == {"tok-act"}, "the read-only token is never used on the action lane"


def test_deployment_uuid_comes_back_where_coolify_creates_one(pkg):
    for kind, target, uuid in (("deploy", "app-stg", "dep-new"), ("restart", "app-stg", "dep-rst"),
                               ("cancel", "dep-run", "dep-run"), ("stop", "app-stg", None)):
        _, body = pkg.api_action(settings(), secrets(), {"kind": kind, "target": target}, transport=Instance())
        assert body.get("deployment_uuid") == uuid, (kind, body)


@pytest.mark.parametrize("target", ["shop-web-staging", "umami-staging", "pg-staging"])
def test_stop_never_omits_docker_cleanup_false(pkg, target):
    fake = Instance()
    pkg.api_action(settings(), secrets(), {"kind": "stop", "target": target}, transport=fake)
    (sent,) = fake.actions_sent()
    assert sent[2].get("docker_cleanup") == "false"


def test_stop_refuses_a_docker_cleanup_parameter_and_sends_nothing(pkg):
    fake = Instance()
    status, body = pkg.api_action(settings(), secrets(),
                                  {"kind": "stop", "target": "app-stg", "params": {"docker_cleanup": "true"}},
                                  transport=fake)
    assert status == 400 and body["error"] == "bad_params" and not body["sent"]
    assert fake.actions_sent() == []


def test_fixed_parameters_are_applied_last(pkg):
    a = acts()
    target = a.Target("application", "app-stg", "x")
    request = a.build_request(a.SPECS["stop"], target, {})
    assert request.params == {"docker_cleanup": "false"}
    request = a.build_request(a.SPECS["force_deploy"], target, {})
    assert request.params == {"uuid": "app-stg", "force": "true"}


def test_dry_run_plans_without_sending_and_names_the_target(pkg):
    fake = Instance()
    status, body = pkg.api_action(settings(), secrets(), {"kind": "restart", "target": "shop-web-staging",
                                                          "dry_run": True}, transport=fake)
    assert status == 200 and body["dry_run"] and not body["sent"]
    assert body["confirm_text"] == ("restart application 'shop-web-staging' (project shop, environment staging, "
                                    "server app-1)")
    assert body["token"]["role"] == "api" and "tok-act" not in json.dumps(body)
    assert fake.actions_sent() == []


# --- refusals ------------------------------------------------------------------------------------

def _refused(pkg, request, code, *, action="tok-act", read="tok-read"):
    fake = Instance()
    status, body = pkg.api_action(settings(), secrets(action, read), request, transport=fake)
    assert body["error"] == code and body["sent"] is False and not body["ok"], body
    assert fake.actions_sent() == [], "a refused action sends nothing that can change anything"
    return status, body, fake


def test_refused_without_the_deploy_right(pkg):
    status, body, _ = _refused(pkg, {"kind": "restart", "target": "app-stg"}, "no_deploy_right",
                               action="tok-act-nodeploy")
    assert status == 403 and "deploy" in body["message"]


def test_overview_token_is_never_used_for_actions_even_when_it_holds_deploy(pkg):
    _, body, fake = _refused(pkg, {"kind": "restart", "target": "app-stg"}, "no_deploy_right",
                             action="tok-act-nodeploy", read="tok-read-deploy")
    assert "never use the read-only token" in body["message"]
    assert [r for r in fake.requests if r[4] == "tok-read-deploy"] == [], "the read-only token got a request"


@pytest.mark.parametrize("target", ["shop-web", "app-prod"])
def test_refused_for_a_resource_outside_the_action_tokens_view(pkg, target):
    status, body, fake = _refused(pkg, {"kind": "restart", "target": target}, "outside_action_view")
    assert status == 403
    assert not any("app-prod" in r[1] for r in fake.requests), "nothing is sent about the hidden resource"


def test_refused_to_cancel_a_deployment_outside_the_view_or_already_finished(pkg):
    _refused(pkg, {"kind": "cancel", "target": "dep-prod"}, "outside_action_view")
    status, body, _ = _refused(pkg, {"kind": "cancel", "target": "dep-done"}, "not_cancellable")
    assert status == 409 and "finished" in body["message"]


@pytest.mark.parametrize("target", ["abc/../other", "../other"])
def test_a_malformed_deployment_id_is_refused_before_any_transport(pkg, target):
    status, body, fake = _refused(pkg, {"kind": "cancel", "target": target}, "bad_params")
    assert status == 400 and "deployment UUID" in body["message"]
    assert fake.requests == []


def test_refused_for_unknown_kind_wrong_kind_and_missing_commit(pkg):
    _refused(pkg, {"kind": "delete", "target": "app-stg"}, "unknown_kind")
    _refused(pkg, {"kind": "force_deploy", "target": "umami-staging"}, "wrong_kind")
    _refused(pkg, {"kind": "rollback", "target": "app-stg"}, "bad_params")
    _refused(pkg, {"kind": "rollback", "target": "app-stg", "params": {"commit": "x; rm -rf /"}}, "bad_params")


def test_a_lone_team_wide_token_acts_as_the_single_token(pkg):
    fake = Instance()
    status, body = pkg.api_action(settings(), secrets(action=None, read="tok-full"),
                                  {"kind": "restart", "target": "shop-web"}, transport=fake)
    assert status == 200 and body["token"]["role"] == "single"
    assert [r[1] for r in fake.actions_sent()] == ["/api/v1/applications/app-prod/restart"]


# --- the agent lane: approval hook, handler, unattended runs -------------------------------------

class Hermes:
    """What Hermes does with a plugin tool call: run the pre_tool_call hook; on `approve` ask the
    human (here: `answer`), and call the handler only when approved (tools/approval.py,
    hermes_cli/plugins.py `_resolve_block_from_details`)."""

    def __init__(self, pkg, run, fake, *, answer=True, action="tok-act", read="tok-read", get_setting=None):
        self.tickets = pkg.ApprovalTickets()
        self.settings = get_setting or settings()
        self.gate = pkg.make_action_gate(self.settings, secrets(action, read), lambda: run, self.tickets,
                                         transport=fake)
        self.pkg, self.run, self.fake, self.answer = pkg, run, fake, answer
        self.secrets = secrets(action, read)
        self.asked = []

    def handler(self, args, session_id="s1"):
        return json.loads(json.dumps(self.pkg.run_approved_action(self.settings, self.secrets, args, session_id,
                                                                  self.run, self.tickets, transport=self.fake)))

    def call(self, args, session_id="s1"):
        directive = self.gate(tool_name="coolify_action", args=args, session_id=session_id, tool_call_id="c1")
        if directive and directive["action"] == "block":
            return {"blocked": directive["message"]}
        if directive and directive["action"] == "approve":
            self.asked.append(directive)
            if not self.answer:
                return {"blocked": "BLOCKED: User denied."}
        return self.handler(args, session_id)


def attended():
    return _plugin().RunContext(platform="desktop")


def test_gate_ignores_other_tools(pkg):
    hermes = Hermes(pkg, attended(), Instance())
    assert hermes.gate(tool_name="terminal", args={"command": "ls"}) is None


def test_approved_call_runs_once_and_the_prompt_names_action_resource_environment_project_server(pkg):
    fake = Instance()
    hermes = Hermes(pkg, attended(), fake)
    result = hermes.call({"kind": "restart", "target": "shop-web-staging"})
    (asked,) = hermes.asked
    for word in ("restart", "shop-web-staging", "environment staging", "project shop", "server app-1",
                 "/api/v1/applications/app-stg/restart".replace("/api/v1", "")):
        assert word in asked["message"], (word, asked["message"])
    assert asked["rule_key"] == "coolify_action:restart:app-stg"
    assert result["ok"] and result["deployment_uuid"] == "dep-rst"
    assert len(fake.actions_sent()) == 1


def test_denied_approval_sends_nothing_and_revokes_the_ticket(pkg):
    fake = Instance()
    hermes = Hermes(pkg, attended(), fake, answer=False)
    args = {"kind": "stop", "target": "shop-web-staging"}
    result = hermes.call(args)
    assert "denied" in result["blocked"] and len(hermes.asked) == 1
    assert fake.actions_sent() == []
    # Hermes fires post_approval_response with the plugin rule's pattern key and the choice.
    observer = pkg.make_approval_observer(hermes.tickets)
    observer(pattern_key="plugin_rule:" + hermes.asked[0]["rule_key"], choice="deny", surface="cli")
    assert hermes.handler(args)["error"] == "not_approved", "a denied call's ticket must not run later"
    assert fake.actions_sent() == []


def test_an_approved_choice_keeps_the_ticket(pkg):
    fake = Instance()
    hermes = Hermes(pkg, attended(), fake)
    args = {"kind": "restart", "target": "app-stg"}
    directive = hermes.gate(tool_name="coolify_action", args=args, session_id="s1")
    pkg.make_approval_observer(hermes.tickets)(pattern_key="plugin_rule:" + directive["rule_key"], choice="once")
    assert hermes.handler(args)["ok"]


def test_handler_without_the_hook_refuses_and_sends_nothing(pkg):
    fake = Instance()
    hermes = Hermes(pkg, attended(), fake)
    result = hermes.handler({"kind": "restart", "target": "app-stg"})
    assert result["error"] == "not_approved" and fake.requests == []


def test_a_ticket_is_one_use_and_bound_to_the_session(pkg):
    fake = Instance()
    hermes = Hermes(pkg, attended(), fake)
    args = {"kind": "restart", "target": "app-stg"}
    hermes.gate(tool_name="coolify_action", args=args, session_id="s1")
    assert hermes.handler(args, session_id="other")["error"] == "not_approved"
    assert hermes.handler(args, session_id="s1")["ok"]
    assert hermes.handler(args, session_id="s1")["error"] == "not_approved"
    assert len(fake.actions_sent()) == 1


@pytest.mark.parametrize("run,word", [
    ({"cron": True}, "cron job"),
    ({"single_query": True}, "single-query"),
    ({"platform": "webhook"}, "webhook"),
    ({"platform": "msgraph_webhook"}, "msgraph_webhook"),
    ({"platform": "api_server"}, "api_server"),
    ({"platform": "cli", "host_blind": True}, "plugin host"),
])
def test_unattended_runs_refuse_without_any_request(pkg, run, word):
    fake = Instance()
    hermes = Hermes(pkg, pkg.RunContext(**run), fake)
    result = hermes.call({"kind": "deploy", "target": "shop-web-staging"})
    assert "unattended" in result["blocked"] and word in result["blocked"]
    assert hermes.asked == [], "no approval prompt that nobody can answer"
    assert fake.requests == [], "no request to Coolify at all"
    # The handler refuses on its own as well (a hook that failed is skipped by Hermes).
    assert hermes.handler({"kind": "deploy", "target": "shop-web-staging"})["error"] == "unattended"
    assert fake.requests == []


def test_gate_refusals_need_no_approval(pkg):
    fake = Instance()
    hermes = Hermes(pkg, attended(), fake, action="tok-act-nodeploy", read="tok-read-deploy")
    result = hermes.call({"kind": "restart", "target": "app-stg"})
    assert "no_deploy_right" in result["blocked"] and hermes.asked == []
    assert fake.actions_sent() == []
    hermes = Hermes(pkg, attended(), Instance())
    assert "outside_action_view" in hermes.call({"kind": "restart", "target": "shop-web"})["blocked"]


def test_agent_actions_setting_off_refuses(pkg):
    fake = Instance()
    tickets = pkg.ApprovalTickets()
    gate = pkg.make_action_gate(settings(agent_actions=False), secrets(), attended, tickets, transport=fake)
    directive = gate(tool_name="coolify_action", args={"kind": "restart", "target": "app-stg"})
    assert directive["action"] == "block" and "agent_actions" in directive["message"]
    assert fake.requests == []


def test_gate_fails_closed_on_an_unexpected_error(pkg):
    def broken(method, url, headers, body, timeout):
        raise RuntimeError("boom")

    tickets = pkg.ApprovalTickets()
    gate = pkg.make_action_gate(settings(), secrets(), attended, tickets, transport=broken)
    directive = gate(tool_name="coolify_action", args={"kind": "restart", "target": "app-stg"}, session_id="s1")
    assert directive["action"] == "block"
    assert tickets.take(tickets.key("s1", {"kind": "restart", "target": "app-stg"})) is None


def test_registered_hook_and_tool(pkg):
    class Ctx:
        def __init__(self):
            self.hooks, self.tools = [], []

        def get_config(self, key, default=None):
            return default

        def register_tool(self, **kw):
            self.tools.append(kw["name"])

        def register_hook(self, name, callback):
            self.hooks.append(name)

        def current_cron_execution(self):
            return object()

        def session_surface(self):
            return "desktop"

    ctx = Ctx()
    pkg.register(ctx)
    assert "coolify_action" in ctx.tools and ctx.hooks == ["pre_tool_call", "post_approval_response"]
    assert pkg.run_context_from(ctx).unattended_reason() == "this is a cron job"


class _SurfaceCtx:
    """A ctx as the plugin host forwards it: the caller's surface, no cron, nothing else."""

    def __init__(self, surface):
        self.surface = surface

    def current_cron_execution(self):
        return None

    def session_surface(self):
        return self.surface


@pytest.mark.parametrize("surface", ["cli", "oneshot", ""])
def test_plugin_host_on_a_cli_surface_fails_closed(pkg, monkeypatch, surface):
    # In a separate plugin host the turn's single-query flag is not visible, and `hermes -z`
    # auto-approves Hermes' gate, so an approve directive would act unattended.
    monkeypatch.setenv(pkg.HOST_PROCESS_ENV, "1")
    run = pkg.run_context_from(_SurfaceCtx(surface))
    assert run.host_blind and "plugin host" in run.unattended_reason()
    fake = Instance()
    hermes = Hermes(pkg, run, fake)
    result = hermes.call({"kind": "restart", "target": "app-stg"})
    assert "unattended" in result["blocked"] and "in_process" in result["blocked"]
    assert hermes.asked == [] and fake.requests == []


@pytest.mark.parametrize("surface", ["desktop", "tui", "telegram"])
def test_plugin_host_keeps_approval_where_a_person_is(pkg, monkeypatch, surface):
    monkeypatch.setenv(pkg.HOST_PROCESS_ENV, "1")
    assert pkg.run_context_from(_SurfaceCtx(surface)).unattended_reason() is None


def test_in_process_cli_still_asks(pkg, monkeypatch):
    monkeypatch.delenv(pkg.HOST_PROCESS_ENV, raising=False)
    assert pkg.run_context_from(_SurfaceCtx("cli")).unattended_reason() is None


def test_an_action_never_resolves_part_of_a_name(pkg):
    _, body, _ = _refused(pkg, {"kind": "restart", "target": "shop-web-stag"}, "outside_action_view")
    assert "shop-web-staging" in body["message"], "it suggests the exact name instead"


# --- protected environments ------------------------------------------------------

def test_agent_is_refused_on_production_by_default_and_his_click_still_works(pkg):
    fake = Instance()
    hermes = Hermes(pkg, attended(), fake, action="tok-full", read=None)
    result = hermes.call({"kind": "restart", "target": "shop-web"})
    assert "production is a protected environment" in result["blocked"] and hermes.asked == []
    assert fake.actions_sent() == []
    # the handler alone (a hook that let it through) refuses too
    assert hermes.handler({"kind": "restart", "target": "shop-web"})["error"] in ("protected", "not_approved")
    status, body = pkg.api_action(settings(), secrets("tok-full", None), {"kind": "restart", "target": "shop-web"},
                                  transport=fake)
    assert status == 200 and body["sent"]  # the cockpit's own button is never refused here


def test_switch_on_asks_each_time_with_a_one_time_key_naming_the_environment(pkg):
    fake = Instance()
    on = settings(agent_on_protected=True)
    hermes = Hermes(pkg, attended(), fake, action="tok-full", read=None, get_setting=on)
    first = hermes.call({"kind": "restart", "target": "shop-web"})
    second = hermes.call({"kind": "restart", "target": "shop-web"})
    assert first["ok"] and second["ok"] and len(fake.actions_sent()) == 2
    keys = [a["rule_key"] for a in hermes.asked]
    assert len(keys) == 2 and keys[0] != keys[1]  # "for this session" and "always" never carry over
    assert all(k.startswith("coolify_action:restart:app-prod:protected:") for k in keys)
    assert hermes.asked[0]["message"].startswith("PROTECTED ENVIRONMENT PRODUCTION.")


def test_staging_marked_protected_is_refused_and_unprotected_production_is_asked_normally(pkg):
    fake = Instance()
    marked = settings(protected_environments="staging, Production")
    hermes = Hermes(pkg, attended(), fake, get_setting=marked)
    assert "staging is a protected environment" in hermes.call({"kind": "restart", "target": "app-stg"})["blocked"]
    free = settings(protected_environments="staging")  # production is no longer named
    hermes = Hermes(pkg, attended(), fake, action="tok-full", read=None, get_setting=free)
    assert hermes.call({"kind": "restart", "target": "shop-web"})["ok"]
    assert hermes.asked[0]["rule_key"] == "coolify_action:restart:app-prod"


def test_unknown_environment_counts_as_protected(pkg):
    assert pkg.is_protected(settings(), None) and pkg.is_protected(settings(), "")
    assert pkg.is_protected(settings(), "PRODUCTION") and not pkg.is_protected(settings(), "staging")
    assert pkg.protected_environments(settings(protected_environments=None)) == ["production"]
