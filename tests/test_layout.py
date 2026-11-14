"""Behaviour: the WidgetType registry, layout validation, the three presets, the versioned
store (compare-and-set), the `coolify_cockpit` tool and the layout routes. No network: the tool
and the routes only edit a file in a temp dir (the availability route uses the fake instance)."""

import importlib
import importlib.util
import json
import threading
import urllib.request

import pytest


@pytest.fixture
def lay(plugin):
    return importlib.import_module(plugin.__name__ + ".layout")


@pytest.fixture
def store(lay, tmp_path):
    return lay.LayoutStore(tmp_path / "layout.json")


def tool(plugin, store, **args):
    return json.loads(plugin.make_cockpit_handler(lambda: store)(args))


def errors_of(lay, fn, *args):
    with pytest.raises(lay.LayoutError) as caught:
        fn(*args)
    return " | ".join(caught.value.errors)


# --- the registry --------------------------------------------------------------------------------

def test_one_widget_type_per_product_widget_each_tied_to_a_feature_rule(plugin, lay):
    caps = importlib.import_module(plugin.__name__ + ".capabilities")
    # Since 0.1.0: connection, diagnosis, update_notice and backups left; summary is new
    assert list(lay.TYPES) == ["summary", "deployments", "resources", "servers", "quick_actions", "notebook", "chat"]
    rule_ids = {r.id for r in caps.FEATURE_RULES}
    for wtype in lay.WIDGET_TYPES:
        assert wtype.feature is None or wtype.feature in rule_ids, wtype.type
        assert set(wtype.enhanced_by) <= rule_ids
        assert wtype.data and wtype.default_size in lay.SIZES
    assert lay.TYPES["quick_actions"].feature == "actions"
    assert lay.TYPES["servers"].feature == "servers" and "server_stats" in lay.TYPES["servers"].enhanced_by
    assert lay.TYPES["notebook"].feature is None  # needs nothing from a token
    json.dumps(lay.catalog())  # the page receives it as JSON


def test_annotate_says_why_a_widget_is_not_available(lay):
    layout = lay.validate_layout({"widgets": [
        {"id": "s", "type": "servers", "props": {}}, {"id": "q", "type": "quick_actions", "props": {}},
        {"id": "n", "type": "notebook", "props": {}}, {"id": "d", "type": "deployments", "props": {}}]})
    features = [
        {"id": "servers", "available": False, "reason": "No server data (scoped tokens cannot read servers)",
         "unlock": "add a team-wide read-only token"},
        {"id": "server_stats", "available": False, "reason": "No server stats", "unlock": "x"},
        {"id": "actions", "available": True}, {"id": "deployments", "available": True},
        {"id": "live_logs", "available": False, "reason": "No live logs", "unlock": "paste"}]
    rows = {r["id"]: r for r in lay.annotate(layout, features)}
    assert rows["s"]["available"] is False and "scoped tokens" in rows["s"]["reason"]
    assert rows["s"]["unlock"] == "add a team-wide read-only token"
    assert rows["q"]["available"] is True and rows["n"]["available"] is True
    assert rows["d"]["available"] is True and "limits" not in rows["d"]  # 0.1.0: no live-logs line on the card


# --- validation ----------------------------------------------------------------------------------

def test_unknown_type_is_rejected_with_the_known_types(lay):
    msg = errors_of(lay, lay.validate_widget, {"id": "x", "type": "terminal", "props": {}})
    assert "unknown widget type 'terminal'" in msg and "quick_actions" in msg


def test_unknown_and_mistyped_props_are_rejected_clearly(lay):
    msg = errors_of(lay, lay.validate_widget,
                    {"id": "r", "type": "resources", "size": "xl",
                     "props": {"environment": 3, "colour": "red", "status": "sleeping"}})
    assert "unknown prop(s) colour; known: project, environment" in msg
    assert "props.environment must be text" in msg
    assert "props.status must be one of any, running, stopped, unhealthy, attention" in msg
    assert "size must be one of s, m, l" in msg


def test_ids_title_and_layout_fields_are_checked(lay):
    assert "id must be" in errors_of(lay, lay.validate_widget, {"id": "Bad Id", "type": "chat"})
    assert "title must be" in errors_of(lay, lay.validate_widget, {"id": "c", "type": "chat", "title": "x" * 81})
    msg = errors_of(lay, lay.validate_layout, {"widgets": [{"id": "a", "type": "chat"}, {"id": "a", "type": "notebook"}],
                                               "theme": "dark"})
    assert "unknown field(s) theme" in msg and "'a' is used twice" in msg
    assert "schema" in errors_of(lay, lay.validate_layout, {"schema": 1, "widgets": []})
    assert "deploy_desk" in errors_of(lay, lay.validate_layout, {"preset": "fancy", "widgets": []})


def test_ints_and_lists_have_bounds(lay):
    msg = errors_of(lay, lay.validate_widget, {"id": "d", "type": "deployments", "props": {"limit": 500}})
    assert "limit must be between 1 and 50" in msg
    msg = errors_of(lay, lay.validate_widget, {"id": "c", "type": "chat", "props": {"suggestions": ["q"] * 7}})
    assert "at most 6" in msg
    widget = lay.validate_widget({"id": "d", "type": "deployments", "props": {"limit": 5, "environment": " staging "}})
    assert widget.props == {"limit": 5, "environment": "staging"} and widget.size == "l"


# --- quick actions: kinds come from the ActionSpec registry ----------------------------------------

def test_quick_action_kinds_are_checked_against_the_action_registry(plugin, lay):
    acts = importlib.import_module(plugin.__name__ + ".actions")
    resource_kinds = [k for k, s in acts.SPECS.items() if s.target == "resource"]
    assert list(lay._RESOURCE_ACTION_KINDS) == resource_kinds

    def check(button):
        return lay.validate_widget({"id": "q", "type": "quick_actions", "props": {"buttons": [button]}})

    ok = check({"kind": "restart", "target": "shop-web-staging"})
    assert ok.props["buttons"] == [{"kind": "restart", "target": "shop-web-staging"}]
    assert check({"kind": "rollback", "target": "shop", "params": {"commit": "abc123"}}).props["buttons"][0]["params"]
    assert "unknown action 'delete'; known: deploy, force_deploy" in errors_of(
        lay, check, {"kind": "delete", "target": "shop"})
    assert "cancel acts on one deployment" in errors_of(lay, check, {"kind": "cancel", "target": "dep-1"})
    assert "Roll back needs `commit`" in errors_of(lay, check, {"kind": "rollback", "target": "shop"})
    assert "params has unknown field(s) docker_cleanup" in errors_of(
        lay, check, {"kind": "stop", "target": "shop", "params": {"docker_cleanup": "true"}})
    assert "needs target" in errors_of(lay, check, {"kind": "restart"})
    assert "unknown field(s) when" in errors_of(lay, check, {"kind": "restart", "target": "x", "when": "now"})


def test_row_actions_take_parameterless_resource_actions_only(lay):
    def rows(kinds):
        return lay.validate_widget({"id": "r", "type": "resources", "props": {"row_actions": kinds}})

    assert rows(["deploy", "restart", "stop"]).props["row_actions"] == ["deploy", "restart", "stop"]
    assert rows(["deploy", "restart", "stop", "start"]).props["row_actions"] == ["deploy", "restart", "stop", "start"]
    assert "rollback needs commit per click" in errors_of(lay, rows, ["rollback"])
    assert "unknown action 'nuke'" in errors_of(lay, rows, ["nuke"])


def test_row_actions_defaults_carry_start_and_stop(lay):
    # SS-2: a new layout gets all four small buttons (the cap is 6). A stored layout keeps whatever
    # it saved (D13): nothing injects the default behind the user's back.
    assert lay.TYPES["resources"].props_schema["row_actions"].default == ["deploy", "restart", "stop", "start"]
    assert lay.validate_widget({"id": "r", "type": "resources", "props": {}}).props == {}
    apps = [w for w in lay.preset_widgets("overview", None) if w.type == "resources"][0]
    assert apps.props["row_actions"] == ["deploy", "restart", "stop", "start"]


# --- presets -------------------------------------------------------------------------------------

@pytest.mark.parametrize("name,params,types", [
    ("overview", None, ["summary", "deployments", "resources", "servers"]),
    ("deploy_desk", {"environment": "staging"}, ["resources", "deployments"]),
    ("deploy_desk", {"environment": "staging", "project": "shop",
                     "buttons": [{"kind": "restart", "target": "shop-web-staging"}]},
     ["quick_actions", "resources", "deployments"]),
])
def test_each_preset_round_trips_through_the_file(lay, tmp_path, name, params, types):
    store = lay.LayoutStore(tmp_path / "layout.json")
    built = lay.place_preset(store.load(), name, params, "replace")
    assert [w.type for w in built.widgets] == types
    saved = store.save(built, 0, by="pane")
    again = lay.LayoutStore(tmp_path / "layout.json").load()
    assert again.to_dict() == saved.to_dict()
    assert again.preset == name and again.version == 1
    assert lay.validate_layout(again.to_dict()).to_dict()["widgets"] == saved.to_dict()["widgets"]


def test_deploy_desk_scopes_every_widget_to_the_environment(lay):
    widgets = lay.preset_widgets("deploy_desk", {"environment": "staging"})
    assert all(w.props.get("environment") == "staging" for w in widgets)
    assert widgets[0].props["row_actions"] == ["deploy", "restart", "stop", "start"]
    narrowed = lay.preset_widgets("deploy_desk", {"environment": "staging", "row_actions": ["deploy"]})
    assert narrowed[0].props["row_actions"] == ["deploy"]


def test_preset_params_are_validated(lay):
    assert "takes no param(s) environment" in errors_of(lay, lay.preset_widgets, "overview", {"environment": "x"})
    assert "Unknown preset 'fancy'" in errors_of(lay, lay.preset_widgets, "fancy", None)
    assert "unknown action 'delete'" in errors_of(lay, lay.preset_widgets, "deploy_desk",
                                                 {"buttons": [{"kind": "delete", "target": "x"}]})


def test_preset_on_top_keeps_the_current_widgets_and_renames_clashes(lay):
    current = lay.default_layout()
    first = lay.place_preset(current, "deploy_desk", {"environment": "staging"}, "top")
    twice = lay.place_preset(first, "deploy_desk", {"environment": "staging"}, "top")
    assert [w.id for w in first.widgets][:2] == ["deploy-apps", "deploy-deployments"]
    assert [w.id for w in first.widgets][2:] == ["summary", "deployments", "apps", "servers"]
    assert [w.id for w in twice.widgets][:2] == ["deploy-apps-2", "deploy-deployments-2"] and first.preset == "overview"


def test_a_0011_layout_opens_on_the_new_default_without_a_warning_and_saves_as_schema_2(lay, store):
    # A file 0.0.11 wrote (schema 1, a staging deploy desk on top).
    old = {"schema": 1, "version": 7, "preset": "overview", "updated_at": "2026-11-09T20:00:00Z", "updated_by": "pane",
           "widgets": [{"id": "deploy-resources", "type": "resources", "title": "Deploy desk: staging", "size": "l",
                        "props": {"environment": "staging", "row_actions": ["deploy", "restart"]}},
                       {"id": "servers", "type": "servers", "size": "l", "props": {}},
                       {"id": "connection", "type": "connection", "size": "l", "props": {}}]}
    store.path.write_text(json.dumps(old))
    layout = store.load()
    assert [w.type for w in layout.widgets] == ["summary", "deployments", "resources", "servers"]
    assert layout.warnings == [] and layout.version == 7  # the stored version: the next save does not conflict
    saved = store.update(lambda cur: lay.move_widget(cur, "servers", 0)[0], 7, by="pane")
    on_disk = json.loads(store.path.read_text())
    assert on_disk["schema"] == 2 and on_disk["version"] == 8 and on_disk["widgets"][0]["id"] == "servers"
    assert saved.version == 8


def test_no_file_means_the_overview_preset_at_version_zero(store):
    layout = store.load()
    assert (layout.version, layout.preset) == (0, "overview")
    assert not store.path.exists()


# --- storage: compare-and-set ----------------------------------------------------------------------

def test_a_save_based_on_an_older_version_gets_a_conflict_with_the_current_layout(lay, store):
    pane_base = store.load()                                   # the pane loaded version 0
    agent = store.update(lambda cur: lay.add_widget(cur, {"type": "chat"}, 0)[0], by="agent")
    assert agent.version == 1
    pane_edit = lay.remove_widget(pane_base, "servers")[0]
    with pytest.raises(lay.LayoutConflict) as caught:
        store.save(pane_edit, pane_base.version, by="pane")
    conflict = caught.value.to_dict()
    assert conflict["error"] == "conflict" and conflict["current_version"] == 1
    assert conflict["layout"]["widgets"][0]["type"] == "chat"   # the newer layout comes back
    assert store.load().widgets[0].type == "chat"               # and was not overwritten
    assert store.save(pane_edit, 1, by="pane").version == 2      # rebased on the current version: saved


def test_the_agent_with_an_expected_version_also_loses_cleanly(lay, store):
    store.save(lay.default_layout(), 0, by="pane")
    store.save(lay.default_layout(), 1, by="pane")
    with pytest.raises(lay.LayoutConflict):
        store.update(lambda cur: cur, 1, by="agent")
    with pytest.raises(lay.LayoutConflict):
        store.save(lay.default_layout(), None, by="pane")       # "start over" never hits a live file
    assert store.load().version == 2 and store.load().updated_by == "pane"


def test_concurrent_writers_in_separate_handles_never_lose_an_edit(lay, tmp_path):
    path = tmp_path / "layout.json"
    lay.LayoutStore(path).save(lay.Layout([]), 0, by="pane")

    def writer(n):
        own = lay.LayoutStore(path)  # its own lock fd, as another process would have
        for i in range(5):
            own.update(lambda cur, n=n, i=i: lay.add_widget(cur, {"id": f"w{n}-{i}", "type": "chat"})[0], by="agent")

    threads = [threading.Thread(target=writer, args=(n,)) for n in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    final = lay.LayoutStore(path).load()
    assert final.version == 31 and len(final.widgets) == 30


def test_the_layout_file_survives_a_reload(plugin, lay, store):
    store.update(lambda cur: lay.add_widget(cur, {"type": "quick_actions", "props": {
        "buttons": [{"kind": "restart", "target": "shop-web-staging"}]}}, 0)[0], by="agent")
    on_disk = json.loads(store.path.read_text())
    assert on_disk["version"] == 1 and on_disk["schema"] == 2 and on_disk["updated_by"] == "agent"
    reloaded = importlib.reload(lay)
    again = reloaded.LayoutStore(store.path).load()
    assert again.widgets[0].props["buttons"] == [{"kind": "restart", "target": "shop-web-staging"}]
    assert again.version == 1 and len(again.widgets) == 5


def test_a_hand_edited_bad_widget_is_skipped_with_a_warning(lay, store):
    store.save(lay.default_layout(), 0, by="pane")
    raw = json.loads(store.path.read_text())
    raw["widgets"].append({"id": "t", "type": "terminal", "props": {}})
    store.path.write_text(json.dumps(raw))
    layout = store.load()
    assert len(layout.widgets) == 4 and "terminal" in layout.warnings[0]


def test_an_unreadable_file_is_reported_and_kept_aside_on_start_over(lay, store):
    store.path.write_text("{not json")
    with pytest.raises(lay.LayoutError) as caught:
        store.load()
    assert caught.value.code == "unreadable"
    saved = store.save(lay.default_layout(), None, by="pane")
    assert saved.version == 1
    assert list(store.path.parent.glob("layout.json.unreadable-*"))


# --- the coolify_cockpit tool ----------------------------------------------------------------------

@pytest.fixture
def no_network(monkeypatch):
    def refuse(*a, **k):
        raise AssertionError("coolify_cockpit must not make a network request")

    monkeypatch.setattr(urllib.request, "urlopen", refuse)


def test_tool_get_shows_layout_types_and_presets(plugin, store, no_network):
    out = tool(plugin, store, operation="get")
    assert out["ok"] and out["layout"]["version"] == 0
    assert out["layout"]["widgets"][0] == "0. summary summary (l)"
    assert out["layout"]["widgets"][-1] == "3. servers servers (l)"
    assert any(line.startswith("quick_actions: Quick actions needs feature actions") for line in out["widget_types"])
    assert any(p.startswith("deploy_desk:") for p in out["presets"])
    assert len(json.dumps(out)) < 4000  # compact for the model


def test_tool_operations_edit_only_the_layout(plugin, store, no_network):
    out = tool(plugin, store, operation="apply_preset", preset="deploy_desk", placement="top",
               params={"environment": "staging",
                       "buttons": [{"kind": "restart", "target": "shop-web-staging"}]})
    assert out["ok"] and out["layout"]["version"] == 1
    assert out["layout"]["widgets"][0].startswith("0. deploy-actions quick_actions (s)")
    assert "buttons=[restart shop-web-staging]" in out["layout"]["widgets"][0]
    assert "No Coolify action was run" in out["note"]

    out = tool(plugin, store, operation="add_widget", widget={"type": "chat", "props": {"suggestions": ["why?"]}},
               position=1)
    assert out["changed"] == "added chat (chat) at position 1"
    out = tool(plugin, store, operation="update_widget", widget_id="deploy-apps",
               changes={"size": "m", "props": {"row_actions": ["deploy"], "project": None}})
    assert "deploy-apps resources (m)" in " ".join(out["layout"]["widgets"])
    out = tool(plugin, store, operation="move_widget", widget_id="chat", position=-1)
    assert out["layout"]["widgets"][-1].startswith("7. chat chat")
    out = tool(plugin, store, operation="remove_widget", widget_id="chat")
    assert out["changed"] == "removed chat (chat)" and out["layout"]["version"] == 5
    assert store.load().version == 5


def test_tool_rejects_bad_input_and_saves_nothing(plugin, store, no_network):
    bad = tool(plugin, store, operation="add_widget", widget={"type": "terminal"})
    assert bad["ok"] is False and bad["error"] == "invalid" and "known: summary" in bad["message"]
    assert bad["note"] == "Nothing was saved."
    bad = tool(plugin, store, operation="add_widget",
               widget={"type": "quick_actions", "props": {"buttons": [{"kind": "delete", "target": "x"}]}})
    assert "unknown action 'delete'" in bad["message"]
    bad = tool(plugin, store, operation="remove_widget", widget_id="nope")
    assert bad["error"] == "not_found" and "summary" in bad["message"]
    bad = tool(plugin, store, operation="explode")
    assert "Unknown operation" in bad["message"]
    assert not store.path.exists()  # nothing was written


def test_tool_with_a_stale_expected_version_gets_the_current_layout(plugin, lay, store, no_network):
    store.save(lay.default_layout(), 0, by="pane")
    out = tool(plugin, store, operation="remove_widget", widget_id="servers", expected_version=0)
    assert out["ok"] is False and out["error"] == "conflict" and out["current_version"] == 1
    assert len(out["layout"]["widgets"]) == 4
    assert tool(plugin, store, operation="remove_widget", widget_id="servers", expected_version=1)["ok"]


# --- the layout routes -----------------------------------------------------------------------------

def test_routes_put_and_preset_use_compare_and_set(plugin, store):
    status, body = plugin.api_layout_get(store)
    assert status == 200 and body["layout"]["version"] == 0 and len(body["widget_types"]) == 7
    layout = body["layout"]
    layout["widgets"] = layout["widgets"][:2]
    status, body = plugin.api_layout_put(store, {"expected_version": 0, "layout": layout})
    assert status == 200 and body["layout"]["version"] == 1 and body["layout"]["updated_by"] == "pane"
    status, body = plugin.api_layout_put(store, {"expected_version": 0, "layout": layout})
    assert status == 409 and body["current_version"] == 1
    status, body = plugin.api_layout_put(store, {"layout": layout})
    assert status == 400 and "expected_version" in body["message"]
    status, body = plugin.api_layout_put(store, {"expected_version": 1, "layout": {"widgets": [{"type": "x"}]}})
    assert status == 400 and "unknown widget type" in body["message"]
    status, body = plugin.api_layout_preset(store, {"preset": "deploy_desk", "expected_version": 1})
    assert status == 200 and body["layout"]["preset"] == "deploy_desk" and body["layout"]["version"] == 2
    status, body = plugin.api_layout_preset(store, {"preset": "nope"})
    assert status == 400


def test_route_availability_joins_the_setup_feature_lists(plugin, store, kit):
    fake = kit.FakeCoolify({"tok-action": "scoped_read_deploy"})
    secrets = {"COOLIFYBRUECKE_TOKEN": "tok-action"}.get
    status, body = plugin.api_layout_get(store, availability=True, get_setting=kit.settings(),
                                         get_secret=secrets, transport=fake)
    assert status == 200
    rows = {r["id"]: r for r in body["availability"]}
    assert rows["servers"]["available"] is False
    assert rows["servers"]["reason"] == "No server data (scoped tokens cannot read servers)"
    assert rows["deployments"]["available"] is True and rows["apps"]["available"] is True
    for method, url, body, _ in fake.requests:  # reads, the made-up-UUID probes and MCP initialize only
        assert method == "GET" or "hermes-permission-probe-does-not-exist" in url \
            or (url.endswith("/mcp") and body.get("method") == "initialize"), (method, url)


def test_route_availability_without_a_setup_says_why(plugin, store, kit):
    status, body = plugin.api_layout_get(store, availability=True, get_setting=lambda k, d=None: d,
                                         get_secret=lambda n: None)
    assert status == 200 and body["availability_error"]["error"] == "not_configured"
    assert {r["available"] for r in body["availability"] if r["feature"]} == {False}


def test_dashboard_layout_routes_reach_the_store(monkeypatch, plugin, store):
    # A stand-in for FastAPI (the test venv has none), as in test_snapshot.
    import pathlib
    import sys
    import types
    fastapi = types.ModuleType("fastapi")
    responses = types.ModuleType("fastapi.responses")

    class APIRouter:
        def __init__(self):
            self.routes = {}

        def _add(self, prefix, path):
            def wrap(fn):
                self.routes[prefix + path] = fn
                return fn
            return wrap

        def get(self, path):
            return self._add("", path)

        def post(self, path):
            return self._add("POST ", path)

        def put(self, path):
            return self._add("PUT ", path)

    class JSONResponse:
        def __init__(self, content, status_code=200):
            self.content, self.status_code = content, status_code

    fastapi.APIRouter, fastapi.Query, fastapi.Body = APIRouter, (lambda d=None, **k: d), (lambda d=None, **k: d)
    responses.JSONResponse = JSONResponse
    monkeypatch.setitem(sys.modules, "fastapi", fastapi)
    monkeypatch.setitem(sys.modules, "fastapi.responses", responses)
    root = pathlib.Path(plugin.__file__).resolve().parent
    spec = importlib.util.spec_from_file_location("coolifybruecke_layout_api_under_test",
                                                  root / "dashboard" / "plugin_api.py")
    api = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(api)
    monkeypatch.setattr(api, "_plugin", lambda: plugin)
    monkeypatch.setattr(plugin, "layout_store", lambda path="default": store)
    assert api.layout_get(availability=False).status_code == 200
    put = api.layout_put({"expected_version": 0, "layout": {"widgets": [{"id": "n", "type": "notebook"}]}})
    assert put.status_code == 200 and put.content["layout"]["version"] == 1
    assert api.layout_put({"expected_version": 0, "layout": {"widgets": []}}).status_code == 409
    preset = api.layout_preset({"preset": "deploy_desk", "params": {"environment": "staging"}, "placement": "top"})
    assert preset.status_code == 200 and preset.content["layout"]["version"] == 2


# --- the tool text names every widget type with its label (Mac session: 11 calls for one add) --------

class RecordingCtx:
    def __init__(self):
        self.tools = {}

    def register_tool(self, **kwargs):
        self.tools[kwargs["name"]] = kwargs


def test_the_registered_tool_text_names_every_widget_type_with_its_label(plugin, lay):
    ctx = RecordingCtx()
    plugin._register_layout(ctx)
    registered = ctx.tools["coolify_cockpit"]
    text = registered["description"]
    assert registered["schema"]["description"] == text
    for t in lay.WIDGET_TYPES:
        assert f'{t.type} ("{t.label}")' in text, t.type
    assert 'chat ("Cockpit chat")' in text and 'notebook ("Instance notebook")' in text
    assert "{widget_types}" not in text
    assert "coolifybruecke:coolifybruecke" in text
    assert "position 0 for the top" in text
    assert len(text) < 3000


def test_a_new_widget_type_row_reaches_the_tool_text_without_a_second_list(plugin, lay, monkeypatch):
    extra = lay.WidgetType("status_page", "Status page", "A test row.", (), None, {})
    monkeypatch.setattr(lay, "WIDGET_TYPES", lay.WIDGET_TYPES + (extra,))
    assert 'status_page ("Status page")' in plugin.cockpit_tool_schema()["description"]
