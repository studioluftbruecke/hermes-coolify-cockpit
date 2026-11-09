"""Coolify lifecycle actions: the ActionSpec registry, the rights check and `execute()`.

Every action is one `ActionSpec` row (PRODUCT.md -> Data shapes). The pane's route, the agent's
tool and (later) the Desktop confirm dialog all read the same row, so the request an action sends,
the right it needs and the text that names its target are defined once.

The order is fixed and every step can refuse before anything is sent to the target:

    plan()     1. the spec exists and the parameters are allowed (unknown ones are refused, so
                  nobody can add `docker_cleanup=true` to a stop)
               2. the action token: the token stored for actions, or the only token. Never the
                  overview token, even when it holds `deploy` (PRODUCT.md -> Which token does what)
               3. that token holds `deploy` (the M1 made-up-UUID probe, cached for a minute)
               4. that token can see the target: the resource is in its own lists, or the
                  deployment answers its GET. A resource outside its view is refused here
               5. the request is built from the row; fixed parameters are applied last
    execute()  sends exactly the planned request with the action token

Reads in steps 3 and 4 go only to the action token, and the only non-GET they send is the
made-up-UUID probe of step 3. The target receives a request only in `execute()`.

Coolify's API rate limit is shared by every token of a user (`coolify_client`), so a click costs
as few requests as the checks allow: the action token's view (its three lists and the server name
of each target) is kept for VIEW_TTL_S like the deploy right, so the confirm after a dry run, or the
handler after the approval hook, re-checks against it without reading again; and the environment
names come from the snapshot's stored map when the caller passes one (names only; which resources
the token can see still comes from the token's own lists). Coolify itself refuses a request
outside the token's scope or rights, whatever the cached view says. A 429 refuses the action as
`rate_limited` with the wait; nothing is retried.
"""

from __future__ import annotations

import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from typing import Any, Mapping, Optional

try:
    from .capabilities import (ABILITY_PROBES, ACTION_SETTING, READ_SETTING, Ability, Connection, Role,
                               TokenSlot)
    from .coolify_client import CoolifyClient, CoolifyError, ErrorKind, Transport, fingerprint, urllib_transport
    from .snapshot import KINDS, _environment_map, _resources
except ImportError:  # loaded as a top-level module (tests, scripts)
    from capabilities import (ABILITY_PROBES, ACTION_SETTING, READ_SETTING, Ability,  # type: ignore
                              Connection, Role, TokenSlot)
    from coolify_client import (CoolifyClient, CoolifyError, ErrorKind, Transport,  # type: ignore
                                fingerprint, urllib_transport)
    from snapshot import KINDS, _environment_map, _resources  # type: ignore

DEFAULT_TIMEOUT = 15.0
ABILITY_TTL_S = 60.0
VIEW_TTL_S = 60.0
CANCELLABLE = ("queued", "in_progress")
_COMMIT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,127}$")


# --- the registry ------------------------------------------------------------------------------

@dataclass(frozen=True)
class ActionSpec:
    kind: str                         # deploy | force_deploy | restart | start | stop | cancel | rollback
    label: str                        # button text
    verb: str                         # "restart", used in texts
    target: str                       # resource | deployment
    method: str
    paths: Mapping[str, str]          # resource kind (or "deployment") -> path, `{uuid}` filled in
    query: Mapping[str, str] = field(default_factory=dict)        # templated query, `{uuid}` filled in
    fixed_params: Mapping[str, str] = field(default_factory=dict)  # always sent, never overridable
    body_params: tuple[str, ...] = ()                              # user params sent in the JSON body
    ability: Ability = Ability.DEPLOY
    returns_deployment: bool = False
    confirm_text: str = ""            # format keys: verb, kind, name, project, environment, server, extra
    note: str = ""

    @property
    def target_kinds(self) -> tuple[str, ...]:
        return tuple(self.paths)


_CONFIRM = "{verb} {kind} '{name}' (project {project}, environment {environment}, server {server}){extra}"

ACTION_SPECS: tuple[ActionSpec, ...] = (
    ActionSpec("deploy", "Deploy", "deploy", "resource", "POST",
               {"application": "/deploy", "service": "/deploy"}, query={"uuid": "{uuid}"},
               returns_deployment=True, confirm_text=_CONFIRM,
               note="Builds and starts the current commit (applications) or starts the service."),
    ActionSpec("force_deploy", "Force rebuild", "force-rebuild and deploy", "resource", "POST",
               {"application": "/deploy"}, query={"uuid": "{uuid}"}, fixed_params={"force": "true"},
               returns_deployment=True, confirm_text=_CONFIRM,
               note="Deploy without the build cache."),
    ActionSpec("restart", "Restart", "restart", "resource", "POST",
               {"application": "/applications/{uuid}/restart", "service": "/services/{uuid}/restart",
                "database": "/databases/{uuid}/restart"},
               returns_deployment=True, confirm_text=_CONFIRM,
               note="Restarts the running containers without a rebuild."),
    ActionSpec("start", "Start", "start", "resource", "POST",
               {"application": "/applications/{uuid}/start", "service": "/services/{uuid}/start",
                "database": "/databases/{uuid}/start"},
               returns_deployment=True, confirm_text=_CONFIRM),
    ActionSpec("stop", "Stop", "stop", "resource", "POST",
               {"application": "/applications/{uuid}/stop", "service": "/services/{uuid}/stop",
                "database": "/databases/{uuid}/stop"},
               fixed_params={"docker_cleanup": "false"}, confirm_text=_CONFIRM,
               note="Always sends docker_cleanup=false: Coolify's default runs a Docker cleanup on the "
                    "whole server."),
    ActionSpec("cancel", "Cancel deployment", "cancel the deployment of", "deployment", "POST",
               {"deployment": "/deployments/{uuid}/cancel"}, confirm_text=_CONFIRM,
               note="Only queued or running deployments can be cancelled."),
    ActionSpec("rollback", "Roll back", "roll back", "resource", "POST",
               {"application": "/applications/{uuid}/rollback"}, body_params=("commit",),
               returns_deployment=True, confirm_text=_CONFIRM,
               note="Queues a deployment of an earlier image; needs `commit` (the image tag)."),
)

SPECS: dict[str, ActionSpec] = {spec.kind: spec for spec in ACTION_SPECS}
assert SPECS["stop"].fixed_params.get("docker_cleanup") == "false"


# --- outcomes ----------------------------------------------------------------------------------

class ActionRefused(Exception):
    """The action was refused before anything was sent to the target. `code` is stable."""

    STATUS = {"not_configured": 400, "unknown_kind": 400, "bad_params": 400, "wrong_kind": 400,
              "not_found": 404, "ambiguous": 400, "no_deploy_right": 403, "outside_action_view": 403,
              "not_cancellable": 409, "unattended": 403, "not_approved": 403, "disabled": 403,
              "unreachable": 502, "unauthorized": 401, "rate_limited": 429, "error": 502}

    def __init__(self, code: str, message: str, retry_after: Optional[int] = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.retry_after = retry_after

    @property
    def status(self) -> int:
        return self.STATUS.get(self.code, 400)

    def to_dict(self) -> dict[str, Any]:
        out = {"ok": False, "error": self.code, "message": self.message, "sent": False}
        if self.retry_after is not None:
            out["retry_after_s"] = self.retry_after
        return out


def _rate_limited(error: CoolifyError) -> ActionRefused:
    return ActionRefused("rate_limited", error.message, error.retry_after)


@dataclass(frozen=True)
class Target:
    kind: str                     # application | service | database | deployment
    uuid: str
    name: str
    project: Optional[str] = None
    environment: Optional[str] = None
    server: Optional[str] = None
    resource_uuid: Optional[str] = None   # for a deployment: its application
    status: Optional[str] = None


@dataclass(frozen=True)
class ActionRequest:
    method: str
    path: str
    params: Mapping[str, str]
    json_body: Optional[Mapping[str, Any]]

    def signature(self) -> tuple:
        return (self.method, self.path, tuple(sorted(self.params.items())),
                tuple(sorted((self.json_body or {}).items())))


@dataclass(frozen=True)
class ActionPlan:
    spec: ActionSpec
    target: Target
    request: ActionRequest
    slot: TokenSlot = field(repr=False)
    base_url: str = ""

    @property
    def token_label(self) -> str:
        return f"{self.slot.role.value} token (sha256 {fingerprint(self.slot.token)})"

    @property
    def confirm_text(self) -> str:
        t = self.target
        extra = ""
        if self.spec.kind == "cancel":
            extra = f", deployment {t.uuid}, status {t.status or '?'}"
        if self.spec.kind == "rollback":
            extra = f", to image {self.request.json_body.get('commit') if self.request.json_body else '?'}"
        if self.spec.kind == "stop":
            extra = ", without Docker cleanup"
        kind = "application" if t.kind == "deployment" else t.kind
        return self.spec.confirm_text.format(verb=self.spec.verb, kind=kind, name=t.name,
                                             project=t.project or "?", environment=t.environment or "?",
                                             server=t.server or "?", extra=extra)

    def approval_text(self) -> str:
        """One line for the Hermes approval prompt: what, which resource, where, which token."""
        sent = f"{self.request.method} {self.request.path}"
        if self.request.params:
            sent += "?" + "&".join(f"{k}={v}" for k, v in self.request.params.items())
        return f"Coolify: {self.confirm_text}. Uses the {self.token_label}; sends {sent}"

    def to_dict(self) -> dict[str, Any]:
        """The plan without the token, for the pane and the tool result."""
        return {"kind": self.spec.kind, "label": self.spec.label, "target": asdict(self.target),
                "request": {"method": self.request.method, "path": self.request.path,
                            "params": dict(self.request.params), "json_body": self.request.json_body},
                "token": {"role": self.slot.role.value, "setting": self.slot.setting,
                          "fingerprint": fingerprint(self.slot.token)},
                "confirm_text": self.confirm_text}


# --- step 2: which token acts --------------------------------------------------------------------

def action_slot(connection: Connection) -> TokenSlot:
    """The action token, or the only token. Never the overview token."""
    for slot in connection.slots:
        if slot.role in (Role.ACTION, Role.SINGLE):
            return slot
    raise ActionRefused("not_configured", f"No action token is set. Actions use only {ACTION_SETTING} "
                                          f"(or a single token); {READ_SETTING} is never used for actions.")


# --- step 3: does that token hold `deploy`? --------------------------------------------------------

_DEPLOY_PROBE = next(p for p in ABILITY_PROBES if p.ability is Ability.DEPLOY)


class AbilityCache:
    """`deploy` held or not, per instance and token fingerprint, for ABILITY_TTL_S."""

    def __init__(self, ttl_s: float = ABILITY_TTL_S, clock=time.monotonic):
        self.ttl_s, self._clock = ttl_s, clock
        self._seen: dict[str, tuple[float, bool]] = {}
        self._lock = threading.Lock()

    def get(self, key: str) -> Optional[bool]:
        with self._lock:
            entry = self._seen.get(key)
            if entry is None or self._clock() - entry[0] > self.ttl_s:
                return None
            return entry[1]

    def put(self, key: str, held: bool) -> None:
        with self._lock:
            self._seen[key] = (self._clock(), held)


ABILITY_CACHE = AbilityCache()


def holds_deploy(client: CoolifyClient, cache: AbilityCache = ABILITY_CACHE) -> bool:
    """The M1 probe for `deploy` alone: a POST against a made-up UUID; 404 = held, 403 = missing."""
    key = f"{client.base_url}|{fingerprint(client._token)}"
    known = cache.get(key)
    if known is not None:
        return known
    try:
        client.request(_DEPLOY_PROBE.method, _DEPLOY_PROBE.path, params=_DEPLOY_PROBE.params, json_body={})
        held = True
    except CoolifyError as error:
        if error.kind in (ErrorKind.NOT_FOUND, ErrorKind.BAD_REQUEST):
            held = True
        elif error.kind is ErrorKind.FORBIDDEN:
            held = False
        elif error.kind is ErrorKind.UNAUTHORIZED:
            raise ActionRefused("unauthorized", "Coolify rejected the action token (401). Store a valid "
                                                f"token as {ACTION_SETTING}.") from None
        elif error.kind is ErrorKind.UNREACHABLE:
            raise ActionRefused("unreachable", f"The Coolify instance did not answer: {error.message}") from None
        elif error.kind is ErrorKind.RATE_LIMITED:
            raise _rate_limited(error) from None
        else:
            raise ActionRefused("error", f"Could not check the action token's rights: {error}") from None
    cache.put(key, held)
    return held


# --- step 4: can that token see the target? -------------------------------------------------------

class ViewCache:
    """The action token's view (its resources, and the server name of each target it resolved),
    per instance and token fingerprint, for VIEW_TTL_S. Memory only: the dry run and the confirm
    run in one process (the plugin host), and so do the approval hook and the tool handler."""

    def __init__(self, ttl_s: float = VIEW_TTL_S, clock=time.monotonic):
        self.ttl_s, self._clock = ttl_s, clock
        self._views: dict[str, tuple[float, list[dict], dict[str, Optional[str]]]] = {}
        self._lock = threading.Lock()

    def get(self, key: str) -> Optional[tuple[list[dict], dict[str, Optional[str]]]]:
        with self._lock:
            entry = self._views.get(key)
            if entry is None or not 0 <= self._clock() - entry[0] <= self.ttl_s:
                return None
            return entry[1], entry[2]

    def put(self, key: str, resources: list[dict]) -> dict[str, Optional[str]]:
        servers: dict[str, Optional[str]] = {}
        with self._lock:
            self._views[key] = (self._clock(), resources, servers)
        return servers


VIEW_CACHE = ViewCache()


def _view_key(client: CoolifyClient) -> str:
    return f"{client.base_url}|{fingerprint(client._token)}"


def action_view(client: CoolifyClient, *, environments: Optional[Mapping[int, tuple]] = None,
                cache: Optional[ViewCache] = None) -> list[dict]:
    """Every application, service and database the action token itself can see, with project and
    environment names. Only GETs. `environments` (id -> (project, environment)) saves the token's
    own project reads when it names every environment the token's resources sit in; `cache` keeps
    the view for VIEW_TTL_S."""
    if cache is not None:
        kept = cache.get(_view_key(client))
        if kept is not None:
            return kept[0]
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = {kind: pool.submit(client.get, f"/{kind}s") for kind in KINDS}
        rows: dict[str, list] = {}
        for kind, future in futures.items():
            try:
                payload = future.result()
            except CoolifyError as error:
                raise _read_refusal(error) from None
            rows[kind] = payload if isinstance(payload, list) else []
    ids = {row["environment_id"] for kind_rows in rows.values() for row in kind_rows
           if isinstance(row, dict) and isinstance(row.get("environment_id"), int)}
    if environments is not None and ids <= set(environments):
        env_map = dict(environments)
    else:
        try:
            env_map = _environment_map(client)
        except CoolifyError as error:
            if error.kind is ErrorKind.RATE_LIMITED:
                raise _rate_limited(error) from None
            env_map = {}
    resources = [asdict(r) for r in _resources(rows, env_map)]
    if cache is not None:
        cache.put(_view_key(client), resources)
    return resources


def _read_refusal(error: CoolifyError) -> ActionRefused:
    if error.kind is ErrorKind.UNAUTHORIZED:
        return ActionRefused("unauthorized", f"Coolify rejected the action token (401). Store a valid token "
                                             f"as {ACTION_SETTING}.")
    if error.kind is ErrorKind.UNREACHABLE:
        return ActionRefused("unreachable", f"The Coolify instance did not answer: {error.message}")
    if error.kind is ErrorKind.FORBIDDEN:
        return ActionRefused("outside_action_view", "The action token cannot read resources (it lacks "
                                                    "`read`), so it cannot see any target.")
    if error.kind is ErrorKind.RATE_LIMITED:
        return _rate_limited(error)
    return ActionRefused("error", f"Could not read the action token's resources: {error}")


def _server_name(client: CoolifyClient, kind: str, uuid: str) -> Optional[str]:
    try:
        row = client.get(f"/{kind}s/{uuid}")
    except CoolifyError:
        return None
    if not isinstance(row, dict):
        return None
    for holder in (row.get("destination"), row):
        server = holder.get("server") if isinstance(holder, dict) else None
        if isinstance(server, dict) and server.get("name"):
            return str(server["name"])
    return None


def _server_name_cached(client: CoolifyClient, kind: str, uuid: str, cache: Optional[ViewCache]) -> Optional[str]:
    kept = cache.get(_view_key(client)) if cache is not None else None
    if kept is not None and uuid in kept[1]:
        return kept[1][uuid]
    name = _server_name(client, kind, uuid)
    if kept is not None:
        kept[1][uuid] = name
    return name


def resolve_resource(client: CoolifyClient, spec: ActionSpec, query: str, *,
                     environments: Optional[Mapping[int, tuple]] = None,
                     cache: Optional[ViewCache] = None) -> Target:
    """Exact uuid or exact name (any case) inside the action token's view. Unlike the read tools,
    an action never picks a resource from part of a name: `shop-web` must not act on
    `shop-web-staging`."""
    resources = action_view(client, environments=environments, cache=cache)
    q = query.strip()
    hits = [r for r in resources if r["uuid"] == q] or [r for r in resources if r["name"].lower() == q.lower()]
    if len(hits) > 1:
        listed = ", ".join(f"{r['name']} ({r['kind']}, {r.get('project')}/{r.get('environment')}, {r['uuid']})"
                           for r in hits[:8])
        raise ActionRefused("ambiguous", f"{len(hits)} resources are named {q!r}: {listed}. Pass the uuid.")
    if not hits:
        near = [r["name"] for r in resources if q.lower() in r["name"].lower()][:5]
        hint = f" Did you mean {', '.join(near)}? Name it exactly or pass its uuid." if near else ""
        raise ActionRefused("outside_action_view",
                            f"No resource named or with the uuid {q!r} among the {len(resources)} the action "
                            "token can see. Actions run only on resources inside the action token's view; "
                            "Coolify's token scope decides that view." + hint)
    found = hits[0]
    if found["kind"] not in spec.target_kinds:
        allowed = " or ".join(spec.target_kinds)
        raise ActionRefused("wrong_kind", f"{spec.label} works on {allowed}s; {found['name']} is a {found['kind']}.")
    return Target(found["kind"], found["uuid"], found["name"], found.get("project"), found.get("environment"),
                  _server_name_cached(client, found["kind"], found["uuid"], cache), status=found.get("status"))


def resolve_deployment(client: CoolifyClient, deployment_uuid: str, *,
                       environments: Optional[Mapping[int, tuple]] = None,
                       cache: Optional[ViewCache] = None) -> Target:
    uuid = (deployment_uuid or "").strip()
    if not uuid:
        raise ActionRefused("bad_params", "Name the deployment to cancel by its uuid.")
    try:
        row = client.get(f"/deployments/{uuid}")
    except CoolifyError as error:
        if error.kind in (ErrorKind.NOT_FOUND, ErrorKind.FORBIDDEN, ErrorKind.SCOPED_REFUSED):
            raise ActionRefused("outside_action_view", f"The action token cannot see deployment {uuid!r} "
                                                       f"({error.kind.value}).") from None
        raise _read_refusal(error) from None
    if not isinstance(row, dict):
        raise ActionRefused("error", "Coolify answered the deployment lookup with something unexpected.")
    app = row.get("application") if isinstance(row.get("application"), dict) else {}
    app_uuid = str(row.get("application_uuid") or app.get("uuid") or "") or None
    name = str(row.get("application_name") or app.get("name") or "?")
    project = environment = None
    if app_uuid:
        match = next((r for r in action_view(client, environments=environments, cache=cache)
                      if r["uuid"] == app_uuid), None)
        if match is None:
            raise ActionRefused("outside_action_view", f"Deployment {uuid!r} belongs to an application "
                                                       "outside the action token's view.")
        name, project, environment = match["name"], match.get("project"), match.get("environment")
    status = str(row.get("status") or "") or None
    if status not in CANCELLABLE:
        raise ActionRefused("not_cancellable", f"Deployment {uuid!r} is {status or 'in an unknown state'}; "
                                               "only queued or running deployments can be cancelled.")
    return Target("deployment", uuid, name, project, environment, str(row.get("server_name") or "") or None,
                  resource_uuid=app_uuid, status=status)


# --- step 1 and 5: the request -----------------------------------------------------------------

def spec_for(kind: Any) -> ActionSpec:
    spec = SPECS.get(str(kind or "").strip().lower())
    if spec is None:
        raise ActionRefused("unknown_kind", f"Unknown action {kind!r}. Known: {', '.join(SPECS)}.")
    return spec


def check_params(spec: ActionSpec, params: Optional[Mapping[str, Any]]) -> dict[str, str]:
    """Only the spec's own body parameters are accepted; everything else is refused."""
    params = dict(params or {})
    unknown = sorted(set(params) - set(spec.body_params))
    if unknown:
        raise ActionRefused("bad_params", f"{spec.label} takes no parameter {', '.join(unknown)}"
                                          + (f" (allowed: {', '.join(spec.body_params)})" if spec.body_params
                                             else "") + ".")
    clean: dict[str, str] = {}
    for name in spec.body_params:
        value = str(params.get(name) or "").strip()
        if not value:
            raise ActionRefused("bad_params", f"{spec.label} needs `{name}`.")
        if name == "commit" and not _COMMIT.match(value):
            raise ActionRefused("bad_params", "`commit` must be an image tag or commit sha.")
        clean[name] = value
    return clean


def build_request(spec: ActionSpec, target: Target, body: Mapping[str, str]) -> ActionRequest:
    path = spec.paths[target.kind].format(uuid=target.uuid)
    query = {k: v.format(uuid=target.uuid) for k, v in spec.query.items()}
    query.update(spec.fixed_params)  # last, so nothing can override a fixed parameter
    return ActionRequest(spec.method, path, query, dict(body) if spec.body_params else None)


def plan(connection: Connection, kind: Any, target: Any, params: Optional[Mapping[str, Any]] = None, *,
         transport: Transport = urllib_transport, timeout: float = DEFAULT_TIMEOUT,
         ability_cache: AbilityCache = ABILITY_CACHE, view_cache: Optional[ViewCache] = VIEW_CACHE,
         environments: Optional[Mapping[int, tuple]] = None) -> ActionPlan:
    """Steps 1 to 5. Raises ActionRefused; sends nothing to the target. `environments` are stored
    environment names (the snapshot's map), `view_cache` keeps the action token's view."""
    spec = spec_for(kind)
    body = check_params(spec, params)
    if not str(target or "").strip():
        raise ActionRefused("bad_params", "Name the target: a resource name or uuid"
                                          + (", or a deployment uuid for cancel." if spec.target == "deployment"
                                             else "."))
    slot = action_slot(connection)
    client = CoolifyClient(connection.base_url, slot.token, timeout=timeout, transport=transport)
    if not holds_deploy(client, ability_cache):
        overview = any(s.role is Role.OVERVIEW for s in connection.slots)
        raise ActionRefused("no_deploy_right", f"The {slot.role.value} token ({slot.setting}) lacks the "
                                               "`deploy` right, so it cannot run actions."
                                               + (" Actions never use the overview token, whatever it holds."
                                                  if overview else "")
                                               + " Give that token `deploy` in Coolify (Keys & Tokens), scoped "
                                                 "to the environments it may touch.")
    if spec.target == "deployment":
        resolved = resolve_deployment(client, str(target), environments=environments, cache=view_cache)
    else:
        resolved = resolve_resource(client, spec, str(target), environments=environments, cache=view_cache)
    request = build_request(spec, resolved, body)
    if spec.kind == "stop" and request.params.get("docker_cleanup") != "false":
        raise AssertionError("stop must send docker_cleanup=false")
    return ActionPlan(spec, resolved, request, slot, connection.base_url)


# --- execute -----------------------------------------------------------------------------------

def _deployment_uuid(payload: Any) -> Optional[str]:
    if not isinstance(payload, dict):
        return None
    if payload.get("deployment_uuid"):
        return str(payload["deployment_uuid"])
    rows = payload.get("deployments")
    if isinstance(rows, list):
        found = [str(r["deployment_uuid"]) for r in rows if isinstance(r, dict) and r.get("deployment_uuid")]
        return found[0] if found else None
    return None


def execute(action: ActionPlan, *, transport: Transport = urllib_transport,
            timeout: float = DEFAULT_TIMEOUT) -> dict[str, Any]:
    """Send the planned request with the action token and return Coolify's answer."""
    if action.slot.role is Role.OVERVIEW:
        raise AssertionError("actions never use the overview token")
    request = action.request
    if action.spec.kind == "stop" and request.params.get("docker_cleanup") != "false":
        raise AssertionError("stop must send docker_cleanup=false")
    client = CoolifyClient(action.base_url, action.slot.token, timeout=timeout, transport=transport)
    started = time.monotonic()
    try:
        payload = client.request(request.method, request.path, params=dict(request.params) or None,
                                 json_body=dict(request.json_body) if request.json_body is not None else {})
    except CoolifyError as error:
        return {"ok": False, "error": error.kind.value, "message": error.message, "status": error.status,
                "sent": True, **action.to_dict()}
    out = {"ok": True, "sent": True, **action.to_dict(), "response": payload,
           "deployment_uuid": _deployment_uuid(payload), "ms": round((time.monotonic() - started) * 1000)}
    if isinstance(payload, dict) and isinstance(payload.get("deployments"), list):
        messages = [str(r.get("message")) for r in payload["deployments"] if isinstance(r, dict) and r.get("message")]
        out["message"] = "; ".join(messages) or None
    elif isinstance(payload, dict):
        out["message"] = payload.get("message")
    return out


def catalog() -> list[dict[str, Any]]:
    """The registry for the pane: which actions exist, on which kinds, with what fixed parameters."""
    return [{"kind": s.kind, "label": s.label, "target": s.target, "target_kinds": list(s.target_kinds),
             "method": s.method, "fixed_params": dict(s.fixed_params), "body_params": list(s.body_params),
             "ability": s.ability.value, "returns_deployment": s.returns_deployment, "note": s.note}
            for s in ACTION_SPECS]
