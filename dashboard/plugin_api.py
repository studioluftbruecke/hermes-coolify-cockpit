"""Backend routes for the Coolifybrücke pane, mounted at `/api/plugins/coolifybruecke/`.

    GET /setup                            which settings are filled in (no Coolify request, no token value)
    GET /capabilities                     the token setup report (same as coolify_capabilities),
                                          one check shared by every caller for 2 minutes
    GET /snapshot[?refresh=true]          the Snapshot (servers, resources, deployments), 10 s cache
    GET /resources/{uuid}/deployments     deployment history of one application (?take=N, max 50)
    GET /actions                          the ActionSpec registry and whether the API token holds deploy
    POST /actions                         {kind, target, params?, dry_run?}: one lifecycle action
    GET /layout[?availability=true]       the cockpit layout, widget types and presets (+ per-widget availability)
    PUT /layout                           {expected_version, layout}: save the pane's layout (compare-and-set)
    POST /layout/preset                   {preset, params?, placement?, expected_version?}: apply a preset
    GET /notes[?max_chars=N]              the instance notebook COOLIFY.md (newest entries kept)
    POST /notes                           {text, topic?}: append one dated entry as "you"
    POST /diagnose                        {log_text?, deployment_uuid?}: a diagnosis card for the widget
    GET /alerts                           the alert job: on or off, cadence, delivery, last check (no Coolify request)

The handlers are the plugin's own `api_*` functions; this file only adapts them to FastAPI. Hermes
loads it as a standalone module, so the plugin folder is loaded here as a package by path. Settings
come from the profile config (`plugins.entries.coolifybruecke.settings`), secrets from Hermes'
secret scope (`get_secret`). Every route but `POST /actions` only reads from Coolify; that one is
the user's own click (the pane confirms it first) and runs with the API token only, after the
same rights and view checks the agent's tool runs.
Coolify's API rate limit (200 requests per minute per Coolify user by default) answers 429
`{"error": "rate_limited", "retry_after_s": N}` on every route that reads Coolify.
The layout routes never call Coolify (except `?availability=true`, the shared capabilities check); they
edit `layout.json` in the plugin data dir, the same file the agent's `coolify_cockpit` edits. A save
based on an older version answers 409 with the current layout.
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, Body, Query
from fastapi.responses import JSONResponse

PLUGIN = "coolifybruecke"
_HOST_PROCESS_ENV = "HERMES_PLUGIN_HOST_PROCESS"  # set by Hermes in a plugin host process
_PACKAGE = "hermes_coolifybruecke_dashboard"

router = APIRouter()


def _plugin():
    module = sys.modules.get(_PACKAGE)
    if module is not None:
        return module
    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location(_PACKAGE, root / "__init__.py",
                                                  submodule_search_locations=[str(root)])
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load the {PLUGIN} package")
    module = importlib.util.module_from_spec(spec)
    sys.modules[_PACKAGE] = module
    spec.loader.exec_module(module)
    return module


def _get_setting(key: str, default: Any = None) -> Any:
    from hermes_cli.config import load_config_readonly

    entries = ((load_config_readonly() or {}).get("plugins") or {}).get("entries") or {}
    settings = (entries.get(PLUGIN) or {}).get("settings") or {}
    return settings.get(key, default)


def _get_secret(name: str) -> Optional[str]:
    """The profile's current value. In a secondary profile these routes run in its plugin host
    (`HERMES_PLUGIN_HOST_PROCESS=1`), whose environment is the profile's `.env` as it was when the
    host started, so a token saved, changed or removed in the settings form afterwards stayed
    invisible until Hermes restarted (measured, `experiments/coolify-cockpit/onboarding/`). There the
    plugin's own keys come from Hermes' scope builder (the one each gateway turn uses), which reads
    the profile's secrets as they are now, and a key missing there is missing, as in a gateway turn
    for that profile. Everywhere else the request already runs in a fresh profile scope."""
    from agent.secret_scope import get_secret

    if os.environ.get(_HOST_PROCESS_ENV) == "1" and name.startswith("COOLIFYBRUECKE_"):
        try:
            from agent.secret_scope import build_profile_secret_scope
            from hermes_constants import get_hermes_home

            current = build_profile_secret_scope(get_hermes_home())
        except Exception:
            current = None
        if current is not None:
            return current.get(name) or None
    return get_secret(name)


def _reply(result: tuple[int, dict]) -> JSONResponse:
    status, body = result
    return JSONResponse(body, status_code=status)


# Sync handlers: FastAPI runs them on a worker thread, so a slow Coolify never blocks the loop.

@router.get("/setup")
def setup() -> JSONResponse:
    return _reply(_plugin().api_setup(_get_setting, _get_secret))


@router.get("/capabilities")
def capabilities() -> JSONResponse:
    return _reply(_plugin().api_capabilities(_get_setting, _get_secret))


@router.get("/snapshot")
def snapshot(refresh: bool = Query(False)) -> JSONResponse:
    return _reply(_plugin().api_snapshot(_get_setting, _get_secret, refresh=refresh))


@router.get("/servers/metrics")
def server_metrics(range: str = Query("1h")) -> JSONResponse:
    return _reply(_plugin().api_server_metrics(_get_setting, _get_secret, range))


@router.get("/resources/{uuid}/deployments")
def resource_deployments(uuid: str, take: int = Query(10, ge=1, le=50)) -> JSONResponse:
    return _reply(_plugin().api_resource_deployments(_get_setting, _get_secret, uuid, take=take))


@router.get("/actions")
def action_catalog() -> JSONResponse:
    return _reply(_plugin().api_action_catalog(_get_setting, _get_secret))


@router.post("/actions")
def action(body: dict = Body(...)) -> JSONResponse:
    return _reply(_plugin().api_action(_get_setting, _get_secret, body))


@router.get("/layout")
def layout_get(availability: bool = Query(False)) -> JSONResponse:
    plugin = _plugin()
    return _reply(plugin.api_layout_get(plugin.layout_store(), availability=availability,
                                        get_setting=_get_setting, get_secret=_get_secret))


@router.put("/layout")
def layout_put(body: dict = Body(...)) -> JSONResponse:
    plugin = _plugin()
    return _reply(plugin.api_layout_put(plugin.layout_store(), body))


@router.post("/layout/preset")
def layout_preset(body: dict = Body(...)) -> JSONResponse:
    plugin = _plugin()
    return _reply(plugin.api_layout_preset(plugin.layout_store(), body))


# M7: the notebook widget. Entries written here carry the author "you".

@router.get("/notes")
def notes(max_chars: int = Query(6000, ge=500, le=40000)) -> JSONResponse:
    return _reply(_plugin().api_notes_read(max_chars))


@router.post("/notes")
def notes_append(body: dict = Body(...)) -> JSONResponse:
    return _reply(_plugin().api_notes_append(body))


# M3: the diagnosis widget. Pasted log text is classified here; a deployment UUID alone is fetched
# only through a token holding read:sensitive (otherwise the answer asks for a paste).

@router.post("/diagnose")
def diagnose(body: dict = Body(...)) -> JSONResponse:
    return _reply(_plugin().api_diagnose(_get_setting, _get_secret, body))


@router.get("/alerts")
def alerts() -> JSONResponse:
    return _reply(_plugin().api_alerts())
