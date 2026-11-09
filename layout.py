"""The cockpit layout: `Layout`, `Widget`, the `WidgetType` registry, presets, validation, storage.

PRODUCT.md -> Customization model: presets, then edit mode, then the agent, all on one layout.
The Desktop page and the agent's `coolify_cockpit` tool edit the same file, `layout.json` in the
plugin's data directory, so everything here is data the page renders, never code.

    Layout     { schema: 1, version, preset, widgets: [Widget], updated_at, updated_by }
    Widget     { id, type, title?, size: s|m|l, props }       ordered; the page lays out by order + size
    WidgetType { type, label, data, feature, enhanced_by, props_schema, default_size }

Every widget type is one `WidgetType` row: the props it accepts (each with a type, a default and a
description), the data it reads, and the `FeatureRule` id (capabilities.py) it depends on, so the
page can show "not available, because ..." with the same reason `coolify_capabilities` gives.
Adding a widget type means adding a row here and its renderer in the Desktop half.

Concurrent edits (the pane and the agent): every save is a compare-and-set on `version`. A writer
says which version its change is based on; if the file moved on in between, the save is refused
with a `LayoutConflict` that carries the current layout, so the newer edit is never overwritten
silently. Saves hold an exclusive lock on a sibling lock file, because the agent's tool and the
pane's routes run in different processes.

Nothing here talks to Coolify. Quick-action buttons name an `ActionSpec` kind and a resource; the
kind and its parameters are checked against the ActionSpec registry (actions.py), and the target
is resolved only when someone clicks, by the action route with the action token's own view.
"""

from __future__ import annotations

import copy
import datetime as _dt
import json
import os
import re
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping, Optional

try:
    from .actions import SPECS as ACTION_SPECS_BY_KIND, ActionRefused, check_params
    from .capabilities import FEATURE_RULES
except ImportError:  # loaded as a top-level module (tests, scripts)
    from actions import SPECS as ACTION_SPECS_BY_KIND, ActionRefused, check_params  # type: ignore
    from capabilities import FEATURE_RULES  # type: ignore

SCHEMA = 1
SIZES = ("s", "m", "l")
MAX_WIDGETS = 40
MAX_TITLE = 80
MAX_TEXT = 120
LAYOUT_FILE = "layout.json"
_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")
FEATURE_IDS = frozenset(rule.id for rule in FEATURE_RULES)
RESOURCE_KINDS = ("application", "service", "database")


class LayoutError(ValueError):
    """A layout, widget or request that does not validate. `errors` lists every problem found."""

    def __init__(self, errors: list[str], code: str = "invalid"):
        self.errors = list(errors)
        self.code = code
        super().__init__("; ".join(self.errors))

    def to_dict(self) -> dict[str, Any]:
        return {"ok": False, "error": self.code, "message": str(self), "errors": self.errors}


class LayoutConflict(Exception):
    """The layout changed since the version the writer based its change on."""

    def __init__(self, expected: Any, current: "Layout"):
        self.expected, self.current = expected, current
        super().__init__(f"The layout changed since version {expected}: it is at version {current.version} "
                         f"now (last change by {current.updated_by or 'unknown'} at {current.updated_at or '?'}). "
                         "Reload it and apply the change again.")

    def to_dict(self) -> dict[str, Any]:
        return {"ok": False, "error": "conflict", "message": str(self), "expected_version": self.expected,
                "current_version": self.current.version, "layout": self.current.to_dict()}


# --- props schema --------------------------------------------------------------------------------

@dataclass(frozen=True)
class Prop:
    """One widget prop. `kind`: string | bool | int | enum | list | object. A list holds `item` values;
    an object holds `fields`. `choices` on a string documents the values a registry check accepts."""

    kind: str
    description: str
    default: Any = None
    choices: tuple[str, ...] = ()
    minimum: Optional[int] = None
    maximum: Optional[int] = None
    item: Optional["Prop"] = None
    max_items: int = 20
    fields: Mapping[str, "Prop"] = field(default_factory=dict)   # for item kind "object"
    required_fields: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"kind": self.kind, "description": self.description}
        if self.default is not None:
            out["default"] = self.default
        if self.choices:
            out["choices"] = list(self.choices)
        if self.minimum is not None:
            out["minimum"] = self.minimum
        if self.maximum is not None:
            out["maximum"] = self.maximum
        if self.item is not None:
            out["item"] = self.item.to_dict()
            out["max_items"] = self.max_items
        if self.fields:
            out["fields"] = {name: spec.to_dict() for name, spec in self.fields.items()}
            out["required_fields"] = list(self.required_fields)
        return out


def _check_value(spec: Prop, value: Any, where: str, errors: list[str]) -> Any:
    """Coerce and check one value; append a message per problem; return the clean value."""
    if spec.kind == "string":
        if not isinstance(value, str):
            errors.append(f"{where} must be text")
            return None
        value = value.strip()
        if len(value) > MAX_TEXT:
            errors.append(f"{where} is longer than {MAX_TEXT} characters")
        return value
    if spec.kind == "bool":
        if not isinstance(value, bool):
            errors.append(f"{where} must be true or false")
        return value
    if spec.kind == "int":
        if isinstance(value, bool) or not isinstance(value, int):
            errors.append(f"{where} must be a whole number")
            return None
        if spec.minimum is not None and value < spec.minimum or spec.maximum is not None and value > spec.maximum:
            errors.append(f"{where} must be between {spec.minimum} and {spec.maximum}")
        return value
    if spec.kind == "enum":
        if value not in spec.choices:
            errors.append(f"{where} must be one of {', '.join(spec.choices)} (got {value!r})")
        return value
    if spec.kind == "object":
        if not isinstance(value, dict):
            errors.append(f"{where} must be an object with {', '.join(spec.fields)}")
            return None
        unknown = sorted(set(value) - set(spec.fields))
        if unknown:
            errors.append(f"{where} has unknown field(s) {', '.join(unknown)} (known: {', '.join(spec.fields)})")
        missing = [name for name in spec.required_fields if value.get(name) in (None, "")]
        if missing:
            errors.append(f"{where} needs {', '.join(missing)}")
        return {name: _check_value(spec.fields[name], v, f"{where}.{name}", errors)
                for name, v in value.items() if name in spec.fields and v is not None}
    if spec.kind == "list":
        if not isinstance(value, list):
            errors.append(f"{where} must be a list")
            return None
        if len(value) > spec.max_items:
            errors.append(f"{where} holds {len(value)} items; at most {spec.max_items}")
        assert spec.item is not None
        return [_check_value(spec.item, v, f"{where}[{i}]", errors) for i, v in enumerate(value)]
    raise AssertionError(f"unknown prop kind {spec.kind}")


# --- the WidgetType registry ---------------------------------------------------------------------

@dataclass(frozen=True)
class WidgetType:
    type: str
    label: str
    description: str
    data: tuple[str, ...]                 # what the page reads to render it
    feature: Optional[str]                # FeatureRule id it depends on; None: needs nothing from a token
    props_schema: Mapping[str, Prop]
    default_size: str = "m"
    enhanced_by: tuple[str, ...] = ()     # FeatureRule ids that add to it when available
    check: Optional[Callable[[dict, list[str], str], None]] = None  # extra checks across props

    def to_dict(self) -> dict[str, Any]:
        return {"type": self.type, "label": self.label, "description": self.description,
                "data": list(self.data), "feature": self.feature, "enhanced_by": list(self.enhanced_by),
                "default_size": self.default_size,
                "props_schema": {name: spec.to_dict() for name, spec in self.props_schema.items()}}


def _project() -> Prop:
    return Prop("string", "Only this project (name, any case; part of the name is enough).")


def _environment() -> Prop:
    return Prop("string", "Only this environment, e.g. staging (name, any case).")


_RESOURCE_ACTION_KINDS = tuple(k for k, s in ACTION_SPECS_BY_KIND.items() if s.target == "resource")
_ROW_ACTION_KINDS = tuple(k for k in _RESOURCE_ACTION_KINDS if not ACTION_SPECS_BY_KIND[k].body_params)


def _check_action_kind(kind: Any, where: str, errors: list[str], *, row: bool = False) -> bool:
    """Action kinds come from the ActionSpec registry; quick actions take resource actions only."""
    spec = ACTION_SPECS_BY_KIND.get(kind) if isinstance(kind, str) else None
    if spec is None:
        errors.append(f"{where}: unknown action {kind!r}; known: {', '.join(ACTION_SPECS_BY_KIND)}")
        return False
    if spec.target != "resource":
        errors.append(f"{where}: {kind} acts on one deployment, not a resource, so it cannot be pinned; "
                      "it appears on the deployments widget's running rows")
        return False
    if row and spec.body_params:
        errors.append(f"{where}: {kind} needs {', '.join(spec.body_params)} per click, so it cannot be a row "
                      "action; pin it as a quick-action button with params instead")
        return False
    return True


def _check_resources(props: dict, errors: list[str], where: str) -> None:
    for i, kind in enumerate(props.get("row_actions") or []):
        _check_action_kind(kind, f"{where}.row_actions[{i}]", errors, row=True)


def _check_quick_actions(props: dict, errors: list[str], where: str) -> None:
    for i, button in enumerate(props.get("buttons") or []):
        if not isinstance(button, dict):
            continue
        spot = f"{where}.buttons[{i}]"
        if not _check_action_kind(button.get("kind"), spot + ".kind", errors):
            continue
        spec = ACTION_SPECS_BY_KIND[button["kind"]]
        try:
            check_params(spec, button.get("params") or {})
        except ActionRefused as refused:
            errors.append(f"{spot}.params: {refused.message}")


_ACTION_BUTTON = Prop(
    "object", "One pinned action: kind (from the ActionSpec registry), target (a resource's exact name or "
    "uuid; resolved at click time inside the action token's view), optional params and label.",
    fields={"kind": Prop("string", "Action kind (checked against the ActionSpec registry).",
                         choices=_RESOURCE_ACTION_KINDS),
            "target": Prop("string", "Exact resource name or uuid."),
            "params": Prop("object", "rollback only: {commit}.",
                           fields={"commit": Prop("string", "Image tag or commit to roll back to.")}),
            "label": Prop("string", "Button text; default: the action's label and the target.")},
    required_fields=("kind", "target"))

WIDGET_TYPES: tuple[WidgetType, ...] = (
    WidgetType(
        "connection", "Connection and rights",
        "Reachability, version, each token's rights and scope, and the setup's available / not available lists.",
        ("capabilities",), "connection",
        {"show_warnings": Prop("bool", "Show the setup warnings.", True),
         "show_projects": Prop("bool", "List the projects each token sees.", False)},
        default_size="m"),
    WidgetType(
        "servers", "Servers",
        "Per server: reachable, usable, CPU, RAM, disk, load, network, a small history chart.",
        ("snapshot.servers",), "servers",
        {"servers": Prop("list", "Only these servers (names); empty: all.", item=Prop("string", "Server name.")),
         "metrics": Prop("list", "Which metrics to show; empty: all.",
                         item=Prop("enum", "Metric.", choices=("cpu", "ram", "disk", "load", "network")),
                         max_items=5),
         "history": Prop("bool", "Show the history chart.", True)},
        default_size="l", enhanced_by=("server_stats",)),
    WidgetType(
        "resources", "Resources",
        "Applications, services and databases with status, domain, project and environment; optional row actions.",
        ("snapshot.resources", "actions"), "resources",
        {"project": _project(), "environment": _environment(),
         "kinds": Prop("list", "Only these kinds; empty: all.",
                       item=Prop("enum", "Resource kind.", choices=RESOURCE_KINDS), max_items=3),
         "name_contains": Prop("string", "Only resources whose name contains this."),
         "status": Prop("enum", "Only resources in this state.", "any",
                        choices=("any", "running", "stopped", "unhealthy")),
         "row_actions": Prop("list", "Action buttons on every row (ActionSpec kinds without parameters); shown "
                                     "only where the action token holds deploy and sees the resource.",
                             item=Prop("string", "Action kind.", choices=_ROW_ACTION_KINDS), max_items=6),
         "show_env_keys": Prop("bool", "Show environment variable names (never values).", False)},
        default_size="l", enhanced_by=("team_view", "actions"), check=_check_resources),
    WidgetType(
        "deployments", "Deployments",
        "Running deployments and recent failures, linked into Coolify; history per application on demand.",
        ("snapshot.deployments",), "deployments",
        {"project": _project(), "environment": _environment(),
         "show": Prop("enum", "Which deployments.", "all", choices=("all", "running", "failed")),
         "limit": Prop("int", "Rows at most.", 10, minimum=1, maximum=50)},
        default_size="m", enhanced_by=("actions",)),
    WidgetType(
        "quick_actions", "Quick actions",
        "Pinned action buttons, each naming an action and a resource. Every click confirms and names the target.",
        ("actions", "snapshot.resources"), "actions",
        {"buttons": Prop("list", "The pinned buttons, in order.", item=_ACTION_BUTTON, max_items=12)},
        default_size="s", check=_check_quick_actions),
    WidgetType(
        "diagnosis", "Diagnosis",
        "Failed deployments with a diagnosis card (class, evidence lines, likely fix); a paste box when no "
        "token can read logs.",
        ("snapshot.deployments.recent_failed", "diagnose"), "deployments",
        {"project": _project(), "environment": _environment(),
         "paste_box": Prop("bool", "Show the paste box for a log.", True)},
        default_size="m", enhanced_by=("live_logs",)),
    WidgetType(
        "update_notice", "Update notice",
        "\"Coolify 4.x.y is available\" with a link to the instance.",
        ("capabilities.instance",), "update_notice",
        {"include_helpers": Prop("bool", "Also show Sentinel and helper versions.", True)},
        default_size="s"),
    WidgetType(
        "notebook", "Instance notebook",
        "COOLIFY.md: what you and the agent learned about this instance.",
        ("notes",), None,
        {"max_entries": Prop("int", "Newest entries shown.", 20, minimum=1, maximum=200),
         "editable": Prop("bool", "Allow editing in the widget.", True)},
        default_size="m"),
    WidgetType(
        "backups", "Backups",
        "Backup schedules per database and the last run's status.",
        ("mcp.backups",), "resources",
        {"project": _project(), "environment": _environment(),
         "database": Prop("string", "Only databases whose name contains this.")},
        default_size="m"),
    WidgetType(
        "chat", "Cockpit chat",
        "Ask about what you are looking at; the selection rides along as @coolify: context. Runs as a normal "
        "Hermes chat on your provider.",
        ("session",), None,
        {"suggestions": Prop("list", "Suggested questions shown under the input.",
                             item=Prop("string", "A question."), max_items=6)},
        default_size="m"),
)

TYPES: dict[str, WidgetType] = {t.type: t for t in WIDGET_TYPES}
assert all(t.feature is None or t.feature in FEATURE_IDS for t in WIDGET_TYPES)
assert all(f in FEATURE_IDS for t in WIDGET_TYPES for f in t.enhanced_by)


# --- Widget and Layout ---------------------------------------------------------------------------

@dataclass
class Widget:
    id: str
    type: str
    size: str
    props: dict[str, Any] = field(default_factory=dict)
    title: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"id": self.id, "type": self.type}
        if self.title:
            out["title"] = self.title
        out["size"] = self.size
        out["props"] = copy.deepcopy(self.props)
        return out


@dataclass
class Layout:
    widgets: list[Widget]
    preset: Optional[str] = None
    version: int = 0
    updated_at: Optional[str] = None
    updated_by: Optional[str] = None
    warnings: list[str] = field(default_factory=list)   # not stored; what loading had to skip

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"schema": SCHEMA, "version": self.version, "preset": self.preset,
                               "widgets": [w.to_dict() for w in self.widgets],
                               "updated_at": self.updated_at, "updated_by": self.updated_by}
        if self.warnings:
            out["warnings"] = list(self.warnings)
        return out

    def find(self, widget_id: Any) -> int:
        for index, widget in enumerate(self.widgets):
            if widget.id == widget_id:
                return index
        known = ", ".join(w.id for w in self.widgets) or "none"
        raise LayoutError([f"No widget with id {widget_id!r} (widgets: {known})"], code="not_found")


def validate_widget(raw: Any, where: str = "widget") -> Widget:
    """One widget from untrusted input (pane, agent, hand-edited file). Unknown types and props fail."""
    if not isinstance(raw, dict):
        raise LayoutError([f"{where} must be an object with type, id, size, props"])
    errors: list[str] = []
    unknown_keys = sorted(set(raw) - {"id", "type", "title", "size", "props"})
    if unknown_keys:
        errors.append(f"{where} has unknown field(s) {', '.join(unknown_keys)} (known: id, type, title, size, props)")
    kind = raw.get("type")
    wtype = TYPES.get(kind) if isinstance(kind, str) else None
    if wtype is None:
        raise LayoutError(errors + [f"{where}: unknown widget type {kind!r}; known: {', '.join(TYPES)}"])
    wid = raw.get("id")
    if not isinstance(wid, str) or not _ID.match(wid):
        errors.append(f"{where}.id must be 1-40 characters of a-z, 0-9 and '-' (got {wid!r})")
    size = raw.get("size", wtype.default_size)
    if size not in SIZES:
        errors.append(f"{where}.size must be one of {', '.join(SIZES)} (got {size!r})")
    title = raw.get("title")
    if title is not None and (not isinstance(title, str) or len(title.strip()) > MAX_TITLE):
        errors.append(f"{where}.title must be text of at most {MAX_TITLE} characters")
    props_in = raw.get("props") or {}
    props: dict[str, Any] = {}
    if not isinstance(props_in, dict):
        errors.append(f"{where}.props must be an object")
    else:
        unknown = sorted(set(props_in) - set(wtype.props_schema))
        if unknown:
            known = ", ".join(wtype.props_schema) or "none"
            errors.append(f"{where} ({kind}) has unknown prop(s) {', '.join(unknown)}; known: {known}")
        for name, value in props_in.items():
            if name in wtype.props_schema and value is not None:
                props[name] = _check_value(wtype.props_schema[name], value, f"{where}.props.{name}", errors)
        if wtype.check is not None:
            wtype.check(props, errors, f"{where}.props")
    if errors:
        raise LayoutError(errors)
    return Widget(wid, kind, size, props, title.strip() if isinstance(title, str) and title.strip() else None)


def validate_layout(raw: Any) -> Layout:
    """A whole layout from untrusted input. Version and timestamps are the store's, not the caller's."""
    if not isinstance(raw, dict):
        raise LayoutError(["layout must be an object with widgets"])
    errors: list[str] = []
    unknown = sorted(set(raw) - {"schema", "version", "preset", "widgets", "updated_at", "updated_by", "warnings"})
    if unknown:
        errors.append(f"layout has unknown field(s) {', '.join(unknown)}")
    if raw.get("schema", SCHEMA) != SCHEMA:
        errors.append(f"layout schema {raw.get('schema')!r} is not supported (this plugin reads schema {SCHEMA})")
    preset = raw.get("preset")
    if preset is not None and preset not in PRESETS:
        errors.append(f"layout.preset must be one of {', '.join(PRESETS)} or null (got {preset!r})")
    items = raw.get("widgets")
    if not isinstance(items, list):
        raise LayoutError(errors + ["layout.widgets must be a list"])
    if len(items) > MAX_WIDGETS:
        errors.append(f"layout holds {len(items)} widgets; at most {MAX_WIDGETS}")
    widgets: list[Widget] = []
    for index, item in enumerate(items):
        try:
            widgets.append(validate_widget(item, f"widgets[{index}]"))
        except LayoutError as error:
            errors.extend(error.errors)
    seen: set[str] = set()
    for widget in widgets:
        if widget.id in seen:
            errors.append(f"widget id {widget.id!r} is used twice")
        seen.add(widget.id)
    if errors:
        raise LayoutError(errors)
    return Layout(widgets, preset)


# --- presets -------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Preset:
    name: str
    label: str
    description: str
    params: Mapping[str, Prop]
    build: Callable[[dict], list[dict]]

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "label": self.label, "description": self.description,
                "params": {name: spec.to_dict() for name, spec in self.params.items()}}


def _overview(_: dict) -> list[dict]:
    # Konstantin's own layout from the 2026-11-09 Mac test ("Please use my current layout as the
    # default one"), with the connection widget last: setup lives on top only until it is done.
    return [{"id": "deploy-resources", "type": "resources", "title": "Deploy desk: staging", "size": "l",
             "props": {"environment": "staging", "row_actions": ["deploy", "restart"]}},
            {"id": "servers", "type": "servers", "size": "l", "props": {}},
            {"id": "deployments", "type": "deployments", "size": "l", "props": {"show": "all"}},
            {"id": "resources", "type": "resources", "size": "l", "props": {}},
            {"id": "connection", "type": "connection", "size": "l", "props": {}}]


def _deploy_desk(params: dict) -> list[dict]:
    env = params.get("environment") or "staging"
    where = {"environment": env, **({"project": params["project"]} if params.get("project") else {})}
    widgets: list[dict] = []
    if params.get("buttons"):
        widgets.append({"id": "deploy-actions", "type": "quick_actions", "title": f"Quick actions: {env}",
                        "size": "s", "props": {"buttons": params["buttons"]}})
    widgets += [
        {"id": "deploy-resources", "type": "resources", "title": f"Deploy desk: {env}", "size": "l",
         "props": {**where, "row_actions": list(params.get("row_actions") or ["deploy", "restart"])}},
        {"id": "deploy-deployments", "type": "deployments", "title": f"Deployments: {env}", "size": "m",
         "props": {**where, "show": "all", "limit": 10}},
        {"id": "deploy-diagnosis", "type": "diagnosis", "title": f"Diagnosis: {env}", "size": "m", "props": where},
    ]
    return widgets


def _server_watch(_: dict) -> list[dict]:
    return [{"id": "watch-servers", "type": "servers", "size": "l", "props": {"history": True}},
            {"id": "watch-update", "type": "update_notice", "size": "s", "props": {}},
            {"id": "watch-notebook", "type": "notebook", "size": "m", "props": {}}]


PRESET_LIST: tuple[Preset, ...] = (
    Preset("overview", "Overview", "Default: deploy desk for staging, servers, deployments, resources, connection.",
           {}, _overview),
    Preset("deploy_desk", "Deploy desk",
           "Resources of one environment with pinned actions, its recent deployments and diagnosis; optional "
           "quick-action buttons on top.",
           {"environment": Prop("string", "The environment, e.g. staging.", "staging"),
            "project": Prop("string", "Only this project."),
            "row_actions": Prop("list", "Row actions on the resources widget.", ["deploy", "restart"],
                                item=Prop("string", "Action kind.", choices=_ROW_ACTION_KINDS), max_items=6),
            "buttons": Prop("list", "Quick-action buttons pinned above the desk.", item=_ACTION_BUTTON,
                            max_items=12)},
           _deploy_desk),
    Preset("server_watch", "Server watch", "Servers with history, the update notice and the notebook.", {},
           _server_watch),
)
PRESETS: dict[str, Preset] = {p.name: p for p in PRESET_LIST}
DEFAULT_PRESET = "overview"
PLACEMENTS = ("replace", "top", "bottom")


def preset_widgets(name: Any, params: Any = None) -> list[Widget]:
    preset = PRESETS.get(name) if isinstance(name, str) else None
    if preset is None:
        raise LayoutError([f"Unknown preset {name!r}; known: {', '.join(PRESETS)}"])
    params = params or {}
    if not isinstance(params, dict):
        raise LayoutError(["preset params must be an object"])
    errors: list[str] = []
    unknown = sorted(set(params) - set(preset.params))
    if unknown:
        known = ", ".join(preset.params) or "none"
        errors.append(f"preset {name} takes no param(s) {', '.join(unknown)}; known: {known}")
    clean = {k: _check_value(preset.params[k], v, f"params.{k}", errors)
             for k, v in params.items() if k in preset.params and v is not None}
    if errors:
        raise LayoutError(errors)
    return validate_layout({"widgets": preset.build(clean)}).widgets


def default_layout() -> Layout:
    return Layout(preset_widgets(DEFAULT_PRESET), DEFAULT_PRESET)


def unique_id(base: str, taken: set[str]) -> str:
    base = re.sub(r"[^a-z0-9-]+", "-", base.lower()).strip("-")[:36] or "widget"
    if base not in taken:
        return base
    n = 2
    while f"{base}-{n}" in taken:
        n += 1
    return f"{base}-{n}"


def place_preset(current: Layout, name: str, params: Any, placement: str = "replace") -> Layout:
    """`replace`: the preset becomes the layout. `top` / `bottom`: its widgets join the current ones."""
    if placement not in PLACEMENTS:
        raise LayoutError([f"placement must be one of {', '.join(PLACEMENTS)} (got {placement!r})"])
    widgets = preset_widgets(name, params)
    if placement == "replace":
        return Layout(widgets, name)
    taken = {w.id for w in current.widgets}
    for widget in widgets:
        widget.id = unique_id(widget.id, taken)
        taken.add(widget.id)
    merged = widgets + current.widgets if placement == "top" else current.widgets + widgets
    if len(merged) > MAX_WIDGETS:
        raise LayoutError([f"the layout would hold {len(merged)} widgets; at most {MAX_WIDGETS}"])
    return Layout(merged, current.preset)


# --- edits (pure: a Layout in, a new Layout out) ---------------------------------------------------

def _copy(layout: Layout) -> Layout:
    return Layout([Widget(w.id, w.type, w.size, copy.deepcopy(w.props), w.title) for w in layout.widgets],
                  layout.preset, layout.version, layout.updated_at, layout.updated_by)


def _position(value: Any, length: int) -> int:
    if value is None:
        return length
    if isinstance(value, bool) or not isinstance(value, int):
        raise LayoutError([f"position must be a whole number, 0 for the top (got {value!r})"])
    if value < 0:
        value = length + 1 + value  # -1: the bottom
    return max(0, min(length, value))


def add_widget(layout: Layout, raw: Any, position: Any = None) -> tuple[Layout, Widget]:
    if not isinstance(raw, dict):
        raise LayoutError(["widget must be an object with at least type"])
    out = _copy(layout)
    if len(out.widgets) >= MAX_WIDGETS:
        raise LayoutError([f"the layout already holds {MAX_WIDGETS} widgets"])
    raw = dict(raw)
    taken = {w.id for w in out.widgets}
    if raw.get("id") in taken:
        raise LayoutError([f"widget id {raw['id']!r} is already used; pick another or omit id"])
    if not raw.get("id") and isinstance(raw.get("type"), str):
        raw["id"] = unique_id(str(raw["type"]).replace("_", "-"), taken)
    widget = validate_widget(raw)
    out.widgets.insert(_position(position, len(out.widgets)), widget)
    return out, widget


def remove_widget(layout: Layout, widget_id: Any) -> tuple[Layout, Widget]:
    out = _copy(layout)
    return out, out.widgets.pop(out.find(widget_id))


def update_widget(layout: Layout, widget_id: Any, changes: Any) -> tuple[Layout, Widget]:
    """Change title, size or props. Props merge into the current ones; a prop set to null is removed."""
    if not isinstance(changes, dict) or not changes:
        raise LayoutError(["changes must be an object with title, size and/or props"])
    unknown = sorted(set(changes) - {"title", "size", "props"})
    if unknown:
        raise LayoutError([f"changes has unknown field(s) {', '.join(unknown)}; a widget's type and id cannot "
                           "change (remove it and add a new one)"])
    out = _copy(layout)
    index = out.find(widget_id)
    current = out.widgets[index].to_dict()
    if "title" in changes:
        current["title"] = changes["title"]
        if changes["title"] in (None, ""):
            current.pop("title", None)
    if "size" in changes:
        current["size"] = changes["size"]
    if "props" in changes:
        if not isinstance(changes["props"], dict):
            raise LayoutError(["changes.props must be an object"])
        merged = {**current["props"], **changes["props"]}
        current["props"] = {k: v for k, v in merged.items() if v is not None}
    out.widgets[index] = validate_widget(current, f"widget {widget_id}")
    return out, out.widgets[index]


def move_widget(layout: Layout, widget_id: Any, position: Any) -> tuple[Layout, Widget]:
    out = _copy(layout)
    widget = out.widgets.pop(out.find(widget_id))
    if position is None:
        raise LayoutError(["position is needed: 0 for the top, -1 for the bottom"])
    out.widgets.insert(_position(position, len(out.widgets)), widget)
    return out, widget


# --- availability: join a layout with the setup's feature lists -----------------------------------

def annotate(layout: Layout, features: list[dict]) -> list[dict]:
    """Per widget: available or not and why, from `Feature` rows ({id, available, reason?, unlock?})."""
    by_id = {f.get("id"): f for f in features if isinstance(f, dict)}
    out = []
    for widget in layout.widgets:
        wtype = TYPES[widget.type]
        row: dict[str, Any] = {"id": widget.id, "type": widget.type, "feature": wtype.feature}
        rule = by_id.get(wtype.feature) if wtype.feature else None
        if wtype.feature is None:
            row["available"] = True
        elif rule is None:
            row.update(available=False, reason="The setup was not checked")
        else:
            row["available"] = bool(rule.get("available"))
            if not row["available"]:
                row.update(reason=rule.get("reason"), unlock=rule.get("unlock"))
        limits = [f"{f}: {by_id[f].get('reason')}" for f in wtype.enhanced_by
                  if f in by_id and not by_id[f].get("available")]
        if limits:
            row["limits"] = limits
        out.append(row)
    return out


# --- storage: layout.json with a version counter and compare-and-set ------------------------------

def _now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0).isoformat()


class LayoutStore:
    """`layout.json` in the plugin data dir. No file yet: the Overview preset at version 0."""

    def __init__(self, path: Optional[Path], *, lock_timeout_s: float = 10.0):
        self.path = Path(path) if path is not None else None
        self.lock_timeout_s = lock_timeout_s
        self._thread_lock = threading.Lock()

    @contextmanager
    def _locked(self) -> Iterator[None]:
        """One writer at a time across threads and processes (fcntl lock on a sibling file)."""
        if self.path is None:
            with self._thread_lock:
                yield
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = self.path.with_name(self.path.name + ".lock")
        with self._thread_lock:
            fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
            try:
                _acquire(fd, self.lock_timeout_s)
                yield
            finally:
                _release(fd)
                os.close(fd)

    def load(self) -> Layout:
        if self.path is None or not self.path.exists():
            return default_layout()
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            raise LayoutError([f"{self.path.name} cannot be read ({type(error).__name__}); fix the file, or "
                               "save a new layout with expected_version null to start over"], code="unreadable")
        if not isinstance(raw, dict) or not isinstance(raw.get("widgets"), list):
            raise LayoutError([f"{self.path.name} holds no layout; save a new one with expected_version null "
                               "to start over"], code="unreadable")
        version = raw.get("version") if isinstance(raw.get("version"), int) else 0
        warnings: list[str] = []
        widgets: list[Widget] = []
        for index, item in enumerate(raw["widgets"]):
            try:
                widget = validate_widget(item, f"widgets[{index}]")
            except LayoutError as error:  # a hand edit or an older plugin: skip, say so, keep the rest
                warnings.append(f"skipped: {error}")
                continue
            if any(w.id == widget.id for w in widgets):
                warnings.append(f"skipped: widget id {widget.id!r} is used twice")
                continue
            widgets.append(widget)
        preset = raw.get("preset") if raw.get("preset") in PRESETS else None
        return Layout(widgets, preset, version, raw.get("updated_at"), raw.get("updated_by"), warnings)

    def _current_version(self) -> Optional[int]:
        """The stored version, or None when the file is missing or unreadable."""
        if self.path is None or not self.path.exists():
            return 0
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return raw.get("version") if isinstance(raw, dict) and isinstance(raw.get("version"), int) else None

    def save(self, layout: Layout, expected_version: Any, *, by: str) -> Layout:
        """Compare-and-set: write `layout` as version+1 only if the file is still at `expected_version`.

        `expected_version` None means "start over" and is accepted only when the file is missing or
        unreadable (so a newer layout cannot be overwritten by accident either).
        """
        if self.path is None:
            raise LayoutError(["No plugin data directory, so the layout cannot be saved"], code="no_storage")
        with self._locked():
            current = self._current_version()
            stale = (expected_version is None and current not in (None, 0)) or \
                    (expected_version is not None and (isinstance(expected_version, bool)
                                                        or expected_version != current))
            if stale:
                raise LayoutConflict(expected_version, self.load())
            if current is None:  # unreadable: keep the bad file beside the new one
                self.path.replace(self.path.with_name(f"{self.path.name}.unreadable-{int(time.time())}"))
            saved = Layout(layout.widgets, layout.preset, (current or 0) + 1, _now(), by)
            self._write(saved)
            return saved

    def update(self, change: Callable[[Layout], Layout], expected_version: Any = None, *, by: str) -> Layout:
        """Read-change-write under the lock. With `expected_version`, a newer file is a conflict;
        without it the change applies to the newest layout (it is an edit, not a replacement)."""
        if self.path is None:
            raise LayoutError(["No plugin data directory, so the layout cannot be saved"], code="no_storage")
        with self._locked():
            current = self.load()
            if expected_version is not None and (isinstance(expected_version, bool)
                                                 or expected_version != current.version):
                raise LayoutConflict(expected_version, current)
            changed = change(current)
            saved = Layout(changed.widgets, changed.preset, current.version + 1, _now(), by)
            self._write(saved)
            return saved

    def _write(self, layout: Layout) -> None:
        assert self.path is not None
        tmp = self.path.with_name(f".{self.path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
        try:
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(layout.to_dict(), handle, indent=2, ensure_ascii=False)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, self.path)
        finally:
            if tmp.exists():
                tmp.unlink()


try:
    import fcntl as _fcntl
except ImportError:  # not POSIX: threads are still serialized, other processes are not
    _fcntl = None  # type: ignore


def _acquire(fd: int, timeout_s: float) -> None:
    deadline = time.monotonic() + timeout_s
    while True:
        try:
            if _fcntl is not None:
                _fcntl.flock(fd, _fcntl.LOCK_EX | _fcntl.LOCK_NB)
            return
        except BlockingIOError:
            if time.monotonic() > deadline:
                raise LayoutError(["Another writer holds the layout lock; try again"], code="busy") from None
            time.sleep(0.02)


def _release(fd: int) -> None:
    if _fcntl is not None:
        _fcntl.flock(fd, _fcntl.LOCK_UN)


# --- the catalog for the page and the agent -------------------------------------------------------

def catalog() -> dict[str, Any]:
    return {"widget_types": [t.to_dict() for t in WIDGET_TYPES], "presets": [p.to_dict() for p in PRESET_LIST],
            "sizes": list(SIZES), "max_widgets": MAX_WIDGETS, "placements": list(PLACEMENTS)}


def _short(value: Any) -> str:
    if isinstance(value, list):
        return "[" + ", ".join(_short(v) for v in value) + "]"
    if isinstance(value, dict):
        if "kind" in value and "target" in value:
            extra = f" {value['params']}" if value.get("params") else ""
            return f"{value['kind']} {value['target']}{extra}"
        return "{" + ", ".join(f"{k}={_short(v)}" for k, v in value.items()) + "}"
    return str(value).lower() if isinstance(value, bool) else str(value)


def widget_line(position: int, widget: Widget) -> str:
    props = " ".join(f"{k}={_short(v)}" for k, v in widget.props.items())
    title = f' "{widget.title}"' if widget.title else ""
    return f"{position}. {widget.id} {widget.type} ({widget.size}){title}" + (f" {props}" if props else "")


def compact(layout: Layout) -> dict[str, Any]:
    """The model-facing view: one line per widget, top to bottom."""
    out: dict[str, Any] = {"version": layout.version, "preset": layout.preset,
                           "widgets": [widget_line(i, w) for i, w in enumerate(layout.widgets)]}
    if layout.updated_by:
        out["last_change"] = f"{layout.updated_by} at {layout.updated_at}"
    if layout.warnings:
        out["warnings"] = layout.warnings
    return out


def type_lines() -> list[str]:
    """One line per widget type with its props, for the agent."""
    lines = []
    for t in WIDGET_TYPES:
        props = ", ".join(f"{n}:{p.kind}" + (f"[{'|'.join(p.choices)}]" if p.choices else "")
                          for n, p in t.props_schema.items())
        needs = f" needs feature {t.feature}" if t.feature else ""
        lines.append(f"{t.type}: {t.label}{needs}; props {props or 'none'}")
    return lines
