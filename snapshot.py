"""The cockpit's read model: one Snapshot of servers, resources and deployments, cached 10 s.

A Snapshot answers "what is going on?" in one read. It follows the M1 routing rule: every call
goes to the token with the widest view (team-wide before scoped; among equals the one that sees
more applications; the read-only token before the API token). Nothing here writes: REST calls are
GETs, and MCP calls are `tools/call` of the read tools in `READ_MCP_TOOLS` only.

Calls for a cold snapshot (one team-wide reader, P servers with metrics on, J projects):

    GET /servers per token (scope signal and server list)
    GET /applications, /services, /databases, /deployments (the running queue), /projects
    GET /projects/{uuid} per project (environment names; resources carry only an environment id)
    MCP get_server_metrics per server with Sentinel metrics on
    MCP list_deployments status=finished and status=failed (the team's newest 100 of each)

All of them run in parallel. A rebuild that follows an expired snapshot starts the metrics calls
before the scope check, with the token that read last time, reuses server metrics younger
than 60 s and the environment names younger than ENVIRONMENTS_TTL_S (`ReadHint`). Environment ids
never change meaning, so the names are read again only when that time is up or a resource points
at an id the map does not hold (a new environment). On Konstantin's instance (23 projects) that
takes a rebuild from 30 rate-limited REST calls to 6. Per-application deployment history is not
part of the snapshot; it is fetched on demand for one resource (`deployment_history`), except for an
application at the window edge of the two lists (`fleet.needs_history`, at most EDGE_READS per build,
kept until that application deploys again).

0.1.0: the two lists give every application's `FleetRow` (`fleet.py`: failing, deploying, last good)
and the newest deployments of every environment (`latest_deployments`, successes included). The
`Attention` list is built on top by `fleet.attention` when the snapshot is served.

Coolify's API rate limit (`coolify_client`) counts REST calls only, per Coolify user. A build that
met a 429 anywhere raises `rate_limited` and is not cached: its swallowed reads would show as
facts (an empty resource list).

The cache has two layers with one key (instance URL and token fingerprints): memory, and a JSON
file in the plugin's data dir. The file layer lets the agent's tool and the Desktop pane's routes
share one snapshot when Hermes runs them in different processes. It holds what the snapshot
holds (names, statuses, domains, metrics), never a token.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

try:
    from . import fleet as fleet_mod
    from .capabilities import (OVERVIEW_HINT, Connection, Role, TokenSlot)
    from .coolify_client import (CoolifyClient, CoolifyError, ErrorKind, HttpResponse, LimitWatch,
                                 Transport, fingerprint, urllib_transport)
except ImportError:  # loaded as a top-level module (tests, scripts)
    import fleet as fleet_mod  # type: ignore
    from capabilities import OVERVIEW_HINT, Connection, Role, TokenSlot  # type: ignore
    from coolify_client import (CoolifyClient, CoolifyError, ErrorKind, HttpResponse,  # type: ignore
                                LimitWatch, Transport, fingerprint, urllib_transport)

CACHE_TTL_S = 10.0
LIST_ROWS = 100       # rows per MCP list_deployments call (Coolify's page maximum)
LATEST_KEEP = 40      # latest_deployments kept in the snapshot
EDGE_READS = 5        # REST history reads per build for applications at the window edge
GOOD_TAKE = 20        # M5: history rows read to find a failing app's last good deploy (Roll back)
FALLBACK_READS = 10   # without MCP: REST history reads per build, each app at most every FALLBACK_TTL_S
FALLBACK_TTL_S = 300.0
HISTORY_DEFAULT = 10
HISTORY_MAX = 50
METRICS_MINUTES = 60
METRICS_TTL_S = 60.0  # server metrics are reused this long (the pane refreshes them once a minute)
ENVIRONMENTS_TTL_S = 600.0  # environment names are reused this long unless an unknown id shows up
PROJECT_WORKERS = 8
READ_MCP_TOOLS = frozenset({"get_server_metrics", "list_deployments"})

KINDS = ("application", "service", "database")


# --- data shapes (PRODUCT.md -> Data shapes) ---------------------------------------------------

@dataclass
class Deployment:
    uuid: str
    resource_uuid: Optional[str]
    resource_name: Optional[str]
    status: Optional[str]
    commit: Optional[str]
    commit_message: Optional[str]
    created_at: Optional[str]
    finished_at: Optional[str]
    server_name: Optional[str]
    url: Optional[str]
    trigger: Optional[str] = None       # webhook | api | mcp | manual
    project: Optional[str] = None
    environment: Optional[str] = None
    superseded: bool = False            # a failure a later success of the same app replaced


@dataclass
class ServerState:
    uuid: str
    name: str
    reachable: Optional[bool]
    usable: Optional[bool]
    metrics_enabled: Optional[bool]
    metrics: Optional[dict] = None   # {"current": {...}, "series": {name: [[ms, value], ...]}}
    metrics_note: Optional[str] = None  # why metrics are missing


@dataclass
class Resource:
    uuid: str
    kind: str  # application | service | database
    name: str
    project: Optional[str]
    environment: Optional[str]
    status: Optional[str]
    fqdn: Optional[str] = None
    last_deployment: Optional[dict] = None  # not filled in the snapshot; see deployment_history
    git_repository: Optional[str] = None    # applications: as Coolify stores it (ssh, https or owner/repo)
    git_branch: Optional[str] = None


@dataclass
class Snapshot:
    servers: list[ServerState]
    resources: list[Resource]
    deployments: dict[str, Optional[list[Deployment]]]  # running
    taken_at: str
    source: dict[str, Any]       # which token read: role, setting, fingerprint, scoped
    limits: list[str] = field(default_factory=list)   # what this setup cannot show, and why
    errors: list[str] = field(default_factory=list)   # calls that failed this time
    stats: dict[str, Any] = field(default_factory=dict)
    environments: dict[str, list] = field(default_factory=dict)  # environment id -> [project, environment]
    environments_epoch: Optional[float] = None                   # when those names were read
    latest_deployments: Optional[list[Deployment]] = None        # every environment, newest first; None: unknown
    fleet: dict[str, dict] = field(default_factory=dict)         # application uuid -> FleetRow
    histories: dict[str, dict] = field(default_factory=dict)     # app uuid -> {key, epoch, rows}: REST reads kept

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class SnapshotUnavailable(Exception):
    """No configured token could read the instance; the message says what to fix."""


# --- call accounting -----------------------------------------------------------------------------

_UUIDISH = re.compile(r"^[A-Za-z0-9_-]{6,}$")
_FIXED_SEGMENTS = {"api", "v1", "servers", "applications", "services", "databases", "deployments",
                   "projects", "environments", "version", "resources", "mcp"}


def _path_label(method: str, url: str, body: Optional[bytes]) -> str:
    if url.endswith("/mcp"):
        try:
            payload = json.loads(body or b"{}")
            return "MCP " + str(payload.get("params", {}).get("name") or payload.get("method"))
        except ValueError:
            return "MCP ?"
    path = url.split("://", 1)[-1].split("/", 1)[-1].split("?", 1)[0]
    parts = [p if p in _FIXED_SEGMENTS or not _UUIDISH.match(p) else "{uuid}" for p in path.split("/")]
    label = "/" + "/".join(parts)
    return f"{method} {label.replace('/api/v1', '', 1)}"


class CountingTransport:
    """Wraps a transport, counts calls per label and refuses anything but reads."""

    def __init__(self, inner: Transport):
        self._inner = inner
        self._lock = threading.Lock()
        self.calls: dict[str, int] = {}
        self.slowest: dict[str, int] = {}

    def __call__(self, method: str, url: str, headers, body, timeout) -> HttpResponse:
        label = _path_label(method, url, body)
        if method != "GET":
            tool = label[4:] if label.startswith("MCP ") else None
            if tool not in READ_MCP_TOOLS:
                raise AssertionError(f"the snapshot sends reads only, refused: {label}")
        with self._lock:
            self.calls[label] = self.calls.get(label, 0) + 1
        started = time.monotonic()
        try:
            return self._inner(method, url, headers, body, timeout)
        finally:
            ms = round((time.monotonic() - started) * 1000)
            with self._lock:
                self.slowest[label] = max(ms, self.slowest.get(label, 0))

    @property
    def total(self) -> int:
        return sum(self.calls.values())

    def summary(self) -> dict[str, Any]:
        rest = sum(n for k, n in self.calls.items() if not k.startswith("MCP "))
        return {"calls": self.total, "rest": rest, "mcp": self.total - rest,
                "by_call": dict(sorted(self.calls.items())), "slowest_ms": dict(sorted(self.slowest.items()))}


# --- small helpers -----------------------------------------------------------------------------

def _now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0).isoformat()


def _str(value: Any) -> Optional[str]:
    return None if value is None or value == "" else str(value)


def mcp_tool(client: CoolifyClient, name: str, arguments: dict[str, Any]) -> Any:
    """Call one read tool of Coolify's MCP server and return its parsed JSON payload."""
    if name not in READ_MCP_TOOLS:
        raise ValueError(f"{name} is not a read tool the cockpit uses")
    result = client.mcp_call("tools/call", {"name": name, "arguments": arguments})
    content = (result or {}).get("content") if isinstance(result, dict) else None
    text = content[0].get("text", "") if isinstance(content, list) and content else ""
    if isinstance(result, dict) and result.get("isError"):
        raise CoolifyError(ErrorKind.SERVER, str(text)[:300] or "MCP tool error", method="POST",
                           path=f"/mcp {name}")
    try:
        return json.loads(text)
    except ValueError:
        raise CoolifyError(ErrorKind.SERVER, f"MCP {name} did not answer JSON", method="POST",
                           path=f"/mcp {name}") from None


def _deployment_url(base_url: str, raw: Any) -> Optional[str]:
    url = _str(raw)
    if url and url.startswith("/"):
        return base_url + url
    return url


def to_deployment(row: dict, base_url: str, uuid_by_name: dict[str, str]) -> Deployment:
    name = _str(row.get("application_name"))
    return Deployment(
        uuid=str(row.get("deployment_uuid") or row.get("uuid") or ""),
        resource_uuid=_str(row.get("application_uuid")) or (uuid_by_name.get(name) if name else None),
        resource_name=name,
        status=_str(row.get("status")),
        commit=_str(row.get("commit")),
        commit_message=_str(row.get("commit_message")),
        created_at=_str(row.get("created_at")),
        finished_at=_str(row.get("finished_at")),
        server_name=_str(row.get("server_name")),
        url=_deployment_url(base_url, row.get("deployment_url")),
        trigger=fleet_mod.trigger_of(row),
    )


# --- choosing the reader (M1 routing rule: reads go to the widest view) --------------------------

@dataclass
class _View:
    slot: TokenSlot
    client: CoolifyClient
    state: str  # ok | unauthorized | unreachable | no_read | error
    scoped: Optional[bool] = None
    servers: Optional[list] = None
    detail: str = ""
    applications: Optional[list] = None  # fetched while breaking a tie, reused afterwards


def _look(slot: TokenSlot, base_url: str, transport: Transport, timeout: float) -> _View:
    client = CoolifyClient(base_url, slot.token, timeout=timeout, transport=transport)
    try:
        servers = client.get("/servers")
        return _View(slot, client, "ok", scoped=False,
                     servers=[s for s in servers if isinstance(s, dict)] if isinstance(servers, list) else [])
    except CoolifyError as error:
        if error.kind is ErrorKind.SCOPED_REFUSED:
            return _View(slot, client, "ok", scoped=True)
        if error.kind is ErrorKind.UNAUTHORIZED:
            return _View(slot, client, "unauthorized", detail=error.message)
        if error.kind is ErrorKind.UNREACHABLE:
            return _View(slot, client, "unreachable", detail=error.message)
        if error.kind is ErrorKind.RATE_LIMITED:
            raise error  # no token can read until the limit resets; say that, not "no reader"
        if error.kind is ErrorKind.FORBIDDEN and error.ability == "read":
            return _View(slot, client, "no_read", detail=error.message)
        return _View(slot, client, "ok", scoped=None, detail=str(error))


def _role_rank(view: _View) -> int:
    return 0 if view.slot.role is Role.ACTION else 1


def choose_reader(views: list[_View]) -> Optional[_View]:
    usable = [v for v in views if v.state == "ok"]
    if not usable:
        return None
    team = [v for v in usable if v.scoped is False]
    if team:
        return max(team, key=_role_rank)
    if len(usable) == 1:
        return usable[0]

    def count(view: _View) -> int:
        try:
            apps = view.client.get("/applications")
        except CoolifyError:
            return -1
        view.applications = apps if isinstance(apps, list) else []
        return len(view.applications)

    with ThreadPoolExecutor(max_workers=len(usable)) as pool:
        counts = list(pool.map(count, usable))
    return max(zip(usable, counts), key=lambda pair: (pair[1], _role_rank(pair[0])))[0]


def _no_reader_message(views: list[_View]) -> str:
    if all(v.state == "unreachable" for v in views):
        return (f"Coolify could not be reached ({views[0].detail}). Check COOLIFYBRUECKE_URL, "
                "that the instance is up, Coolify's allowed IPs (Settings → Advanced → API), and any "
                "proxy or firewall in front of Coolify.")
    parts = []
    for v in views:
        what = {"unauthorized": "was rejected (401); store a valid token",
                "no_read": "lacks the `read` right",
                "unreachable": f"could not reach Coolify ({v.detail})"}.get(v.state, f"failed ({v.detail})")
        parts.append(f"the {v.slot.role.word} ({v.slot.setting}) {what}")
    return "No token can read the instance: " + "; ".join(parts) + "."


# --- building ----------------------------------------------------------------------------------

def _environment_map(client: CoolifyClient) -> dict[int, tuple[str, str]]:
    """environment id -> (project name, environment name), from /projects and one detail each."""
    projects = client.get("/projects")
    projects = [p for p in projects if isinstance(p, dict)] if isinstance(projects, list) else []

    def detail(project: dict) -> list[tuple[int, tuple[str, str]]]:
        name = str(project.get("name", ""))
        try:
            data = client.get(f"/projects/{project.get('uuid')}")
        except CoolifyError:
            return []
        envs = data.get("environments") if isinstance(data, dict) else None
        return [(int(e["id"]), (name, str(e.get("name", "")))) for e in envs or []
                if isinstance(e, dict) and isinstance(e.get("id"), int)]

    with ThreadPoolExecutor(max_workers=PROJECT_WORKERS) as pool:
        pairs = [pair for chunk in pool.map(detail, projects) for pair in chunk]
    return dict(pairs)


def _fqdn(kind: str, row: dict) -> Optional[str]:
    if kind == "service":
        domains = [str(a.get("fqdn")) for a in row.get("applications") or []
                   if isinstance(a, dict) and a.get("fqdn")]
        return ",".join(domains) or None
    return _str(row.get("fqdn"))


def _resources(rows_by_kind: dict[str, list], envs: dict[int, tuple[str, str]]) -> list[Resource]:
    out: list[Resource] = []
    for kind in KINDS:
        for row in rows_by_kind.get(kind) or []:
            if not isinstance(row, dict):
                continue
            project, environment = envs.get(row.get("environment_id"), (None, None))
            app = kind == "application"
            out.append(Resource(uuid=str(row.get("uuid", "")), kind=kind, name=str(row.get("name", "")),
                                project=project, environment=environment, status=_str(row.get("status")),
                                fqdn=_fqdn(kind, row),
                                git_repository=_str(row.get("git_repository")) if app else None,
                                git_branch=_str(row.get("git_branch")) if app else None))
    out.sort(key=lambda r: ((r.project or "~").lower(), (r.environment or "").lower(), r.kind, r.name.lower()))
    return out


def _metrics(client: CoolifyClient, server_uuid: str, minutes: int = METRICS_MINUTES
             ) -> tuple[Optional[dict], Optional[str]]:
    """(metrics, None) or (None, why not) for one server."""
    try:
        data = mcp_tool(client, "get_server_metrics", {"server_uuid": server_uuid, "minutes": int(minutes)})
    except CoolifyError as error:
        return None, f"metrics unavailable ({error.kind.value}: {error.message[:120]})"
    data = data.get("data", data) if isinstance(data, dict) else {}
    current = data.get("current") if isinstance(data.get("current"), dict) else None
    if not current:
        return None, "Sentinel sent no current metrics"
    series = data.get("series") if isinstance(data.get("series"), dict) else {}
    return {"current": current, "series": series, "online": data.get("online"),
            "bucket_seconds": data.get("bucket_seconds"), "minutes": data.get("minutes"),
            "fetched_epoch": round(time.time(), 1)}, None


@dataclass(frozen=True)
class ReadHint:
    """What the previous snapshot learned: which token read, which servers have metrics on, and
    the metrics it read.

    Coolify's metrics call is the slowest read (1.5 to 3 s when Coolify was idle, measured), so a
    rebuild starts it at once for the servers it already knows, in parallel with the scope check,
    and reuses metrics younger than METRICS_TTL_S instead of reading them again (the server widgets
    refresh once a minute). Starting every read early was measured too and was slower: Coolify
    answers more slowly under the extra parallel load. If the scope check picks another token,
    the early metrics are dropped and read again with the right one.
    """
    role: str
    metric_servers: tuple[str, ...] = ()
    metrics: dict = field(default_factory=dict)  # server uuid -> metrics dict with fetched_epoch
    environments: dict = field(default_factory=dict)  # environment id (int) -> (project, environment)
    environments_epoch: Optional[float] = None
    histories: dict = field(default_factory=dict)  # app uuid -> {key, epoch, rows}


def hint_from(snapshot: Optional[dict]) -> Optional[ReadHint]:
    if not isinstance(snapshot, dict) or not isinstance(snapshot.get("source"), dict):
        return None
    servers = [s for s in snapshot.get("servers") or [] if isinstance(s, dict) and s.get("metrics_enabled")]
    metrics = {str(s.get("uuid")): s["metrics"] for s in servers if isinstance(s.get("metrics"), dict)}
    envs: dict[int, tuple[str, str]] = {}
    for key, pair in (snapshot.get("environments") or {}).items():
        if str(key).isdigit() and isinstance(pair, (list, tuple)) and len(pair) == 2:
            envs[int(key)] = (str(pair[0]), str(pair[1]))
    epoch = snapshot.get("environments_epoch")
    histories = snapshot.get("histories") if isinstance(snapshot.get("histories"), dict) else {}
    return ReadHint(str(snapshot["source"].get("role")), tuple(str(s.get("uuid")) for s in servers), metrics,
                    envs, float(epoch) if isinstance(epoch, (int, float)) else None, dict(histories))


def fresh_environments(hint: Optional[ReadHint], now: Optional[float] = None) -> Optional[dict]:
    """The hint's environment names while younger than ENVIRONMENTS_TTL_S, else None."""
    if hint is None or not hint.environments or hint.environments_epoch is None:
        return None
    now = time.time() if now is None else now
    return dict(hint.environments) if -1.0 <= now - hint.environments_epoch <= ENVIRONMENTS_TTL_S else None


def environment_ids(rows_by_kind: dict[str, list]) -> set[int]:
    return {row["environment_id"] for rows in rows_by_kind.values() for row in rows or []
            if isinstance(row, dict) and isinstance(row.get("environment_id"), int)}


class _Metrics:
    """Server metrics for one build: reused when fresh, else one MCP call per server, started early."""

    def __init__(self, pool: ThreadPoolExecutor, client: CoolifyClient, known: Optional[dict] = None):
        now = time.time()
        self._pool, self.client = pool, client
        self._known = {uuid: m for uuid, m in (known or {}).items()
                       if isinstance(m, dict) and -1.0 <= now - float(m.get("fetched_epoch") or 0) <= METRICS_TTL_S}
        self._pending: dict[str, Any] = {}
        self.reused = 0

    def start(self, uuids) -> "_Metrics":
        for uuid in uuids:
            if uuid not in self._known and uuid not in self._pending:
                self._pending[uuid] = self._pool.submit(_metrics, self.client, uuid)
        return self

    def get(self, uuid: str) -> tuple[Optional[dict], Optional[str]]:
        if uuid in self._known:
            self.reused += 1
            return self._known[uuid], None
        return self.start([uuid])._pending[uuid].result()


def _reads(pool: ThreadPoolExecutor, client: CoolifyClient, list_rows: int,
           applications: Optional[list], read_environments: bool = True) -> dict[str, Any]:
    get = client.get
    rows = max(1, min(100, list_rows))
    jobs: dict[str, Callable[[], Any]] = {
        "applications": (lambda: applications) if applications is not None else (lambda: get("/applications")),
        "services": lambda: get("/services"),
        "databases": lambda: get("/databases"),
        "running": lambda: get("/deployments"),
        "finished": lambda: mcp_tool(client, "list_deployments", {"status": "finished", "per_page": rows}),
        "failed": lambda: mcp_tool(client, "list_deployments", {"status": "failed", "per_page": rows}),
    }
    if read_environments:
        jobs["environments"] = lambda: _environment_map(client)
    return {name: pool.submit(job) for name, job in jobs.items()}


def _history_rows(client: CoolifyClient, uuids: list[str], failures: dict, take: int = 5
                  ) -> dict[str, list[dict]]:
    """REST history (newest `take`) for a few applications, in parallel. A failure is recorded under
    `history:<uuid>`; a 429 is re-raised by the build's LimitWatch."""
    if not uuids:
        return {}

    def one(uuid: str) -> tuple[str, Optional[list[dict]]]:
        try:
            payload = client.get(f"/deployments/applications/{uuid}", {"skip": 0, "take": take})
        except CoolifyError as error:
            failures[f"history:{uuid}"] = error
            return uuid, None
        rows = payload.get("deployments") if isinstance(payload, dict) else payload
        keep = ("deployment_uuid", "status", "commit", "commit_message", "created_at", "finished_at",
                "server_name", "is_webhook", "is_api", "is_mcp")
        return uuid, [{k: r.get(k) for k in keep} for r in rows or [] if isinstance(r, dict)]

    with ThreadPoolExecutor(max_workers=min(8, len(uuids))) as pool:
        return {uuid: rows for uuid, rows in pool.map(one, uuids) if rows is not None}


def build_snapshot(connection: Connection, *, transport: Transport = urllib_transport,
                   timeout: float = 15.0, list_rows: int = LIST_ROWS,
                   hint: Optional[ReadHint] = None) -> Snapshot:
    watch = LimitWatch(transport)
    counter = CountingTransport(watch)
    started = time.monotonic()
    known_envs = fresh_environments(hint)
    hinted_slot = next((s for s in connection.slots if hint and s.role.value == hint.role), None)
    with ThreadPoolExecutor(max_workers=16) as pool:
        early: Optional[_Metrics] = None
        if hint is not None and hinted_slot is not None:
            early = _Metrics(pool, CoolifyClient(connection.base_url, hinted_slot.token, timeout=timeout,
                                                 transport=counter), hint.metrics).start(hint.metric_servers)
        views = list(pool.map(lambda s: _look(s, connection.base_url, counter, timeout), connection.slots))
        reader = choose_reader(views)
        if reader is None:
            raise SnapshotUnavailable(_no_reader_message(views))
        futures = _reads(pool, reader.client, list_rows, reader.applications, known_envs is None)
        metrics = early if early is not None and reader.slot is hinted_slot else _Metrics(pool, reader.client)
        servers: list[ServerState] = []
        for row in reader.servers or []:
            settings = row.get("settings") if isinstance(row.get("settings"), dict) else {}
            servers.append(ServerState(str(row.get("uuid", "")), str(row.get("name", "")), row.get("is_reachable"),
                                       row.get("is_usable"), settings.get("is_metrics_enabled")))
        metrics.start(s.uuid for s in servers if s.metrics_enabled)
        for server in servers:
            if server.metrics_enabled:
                server.metrics, server.metrics_note = metrics.get(server.uuid)
            elif server.metrics_enabled is False:
                server.metrics_note = "Sentinel metrics are off on this server (Coolify: server → Sentinel → Metrics)"
        results: dict[str, Any] = {}
        failures: dict[str, CoolifyError] = {}
        for name, future in futures.items():
            try:
                results[name] = future.result()
            except CoolifyError as error:
                failures[name] = error

    rows = {kind: results.get(plural) if isinstance(results.get(plural), list) else []
            for kind, plural in zip(KINDS, ("applications", "services", "databases"))}
    envs_epoch = hint.environments_epoch if known_envs is not None and hint is not None else None
    if known_envs is not None and environment_ids(rows) - set(known_envs):
        known_envs = None  # a resource in an environment the stored names do not know: read them again
        try:
            results["environments"] = _environment_map(reader.client)
        except CoolifyError as error:
            failures["environments"] = error
    if known_envs is not None:
        results["environments"] = known_envs
    else:
        envs_epoch = time.time() if "environments" not in failures else None
    watch.raise_if_limited()

    limits: list[str] = []
    errors: list[str] = []
    if reader.scoped:
        limits.append("No server data or stats: scoped tokens cannot read servers. To unlock: " + OVERVIEW_HINT)
    elif reader.scoped is None:
        limits.append(f"No server data: GET /servers failed ({reader.detail})")
    for name, error in failures.items():
        if name not in ("finished", "failed") and not name.startswith("history:"):
            errors.append(f"{name}: {error}")
    env_map = results.get("environments") or {}
    resources = _resources(rows, env_map)
    uuid_by_name = {r.name: r.uuid for r in resources if r.kind == "application"}

    name_by_uuid = {r.uuid: r.name for r in resources if r.kind == "application"}

    def current_name(d: Deployment) -> Deployment:
        # A deployment row carries the app's name at deploy time; a renamed app shows its name now.
        d.resource_name = name_by_uuid.get(d.resource_uuid or "", d.resource_name)
        return d

    running_rows = [r for r in (results.get("running") if isinstance(results.get("running"), list) else [])
                    if isinstance(r, dict)]
    for r in running_rows:  # the queue's rows may name the app only
        if not r.get("application_uuid") and uuid_by_name.get(str(r.get("application_name"))):
            r["application_uuid"] = uuid_by_name[str(r.get("application_name"))]
    running = [current_name(to_deployment(r, connection.base_url, uuid_by_name)) for r in running_rows]

    listed: dict[str, list[dict]] = {}
    windows: dict[str, Any] = {}
    for status in ("finished", "failed"):
        if status in failures:
            continue
        payload = results.get(status)
        rows_listed = [r for r in (payload.get("data") if isinstance(payload, dict) else None) or [] if isinstance(r, dict)]
        listed[status] = rows_listed
        windows[status] = fleet_mod.window_of(payload, rows_listed)
    app_uuids = [r.uuid for r in resources if r.kind == "application"]
    histories: dict[str, dict] = {}
    history_reads = 0
    latest: Optional[list[Deployment]] = None
    fleet: dict[str, dict] = {}
    known = dict(hint.histories) if hint is not None else {}
    if len(listed) == 2:
        rows_all = listed["finished"] + listed["failed"] + running_rows
        by_app: dict[str, list[dict]] = {}
        for r in rows_all:
            if r.get("application_uuid"):
                by_app.setdefault(str(r["application_uuid"]), []).append(r)
        edge = [u for u in app_uuids
                if fleet_mod.needs_history(by_app.get(u, []), windows["finished"], windows["failed"])]
        for uuid in edge:  # kept until the app deploys again: the newest list row is the key
            key = max((str(r.get("deployment_uuid") or r.get("uuid")) + "@" + str(r.get("created_at"))
                       for r in by_app.get(uuid, [])), default="")
            kept = known.get(uuid)
            if isinstance(kept, dict) and kept.get("key") == key and isinstance(kept.get("rows"), list):
                histories[uuid] = kept
        to_read = [u for u in edge if u not in histories][:EDGE_READS]
        read = _history_rows(reader.client, to_read, failures)
        history_reads += len(to_read)
        for uuid, rows_read in read.items():
            key = max((str(r.get("deployment_uuid") or r.get("uuid")) + "@" + str(r.get("created_at"))
                       for r in by_app.get(uuid, [])), default="")
            histories[uuid] = {"key": key, "epoch": round(time.time(), 1), "rows": rows_read}
        fleet = {u: row.to_dict() for u, row in fleet_mod.build_fleet(
            app_uuids, rows_all, finished=windows["finished"], failed=windows["failed"],
            histories={u: h["rows"] for u, h in histories.items() if u in edge}).items()}
        for uuid in edge:
            if uuid not in histories:
                fleet[uuid]["state"] = "unknown"  # past the window and not read yet this build
        # M5, Roll back to last good: a failing app whose last finished deploy is older than the
        # finished list gets one history read (GOOD_TAKE rows), kept until it deploys again.
        lost = [u for u, row in fleet.items() if row.get("state") == "failing" and not row.get("last_good")]
        goods: dict[str, dict] = {}
        for uuid in lost:
            key = "good:" + max((str(r.get("deployment_uuid") or r.get("uuid")) + "@" + str(r.get("created_at"))
                                 for r in by_app.get(uuid, [])), default="")
            kept = known.get("good:" + uuid)
            if isinstance(kept, dict) and kept.get("key") == key and isinstance(kept.get("rows"), list):
                goods[uuid] = kept
        to_read = [u for u in lost if u not in goods][:max(0, EDGE_READS - len(to_read))]
        for uuid, rows_read in _history_rows(reader.client, to_read, failures, take=GOOD_TAKE).items():
            key = "good:" + max((str(r.get("deployment_uuid") or r.get("uuid")) + "@" + str(r.get("created_at"))
                                 for r in by_app.get(uuid, [])), default="")
            goods[uuid] = {"key": key, "epoch": round(time.time(), 1), "rows": rows_read}
        history_reads += len(to_read)
        for uuid, kept in goods.items():
            histories["good:" + uuid] = kept
            done = sorted((r for r in kept["rows"] if r.get("status") == "finished"),
                          key=lambda r: str(r.get("created_at") or ""), reverse=True)
            if done:
                fleet[uuid]["last_good"] = fleet_mod._short(done[0])
        gone = fleet_mod.superseded(rows_all)
        seen: set[str] = set()
        latest = []
        for r in sorted(rows_all, key=lambda x: str(x.get("created_at") or ""), reverse=True):
            d = current_name(to_deployment(r, connection.base_url, uuid_by_name))
            if not d.uuid or d.uuid in seen:
                continue
            seen.add(d.uuid)
            d.superseded = d.uuid in gone
            latest.append(d)
            if len(latest) >= LATEST_KEEP:
                break
    else:
        # Without Coolify's MCP server: REST history per application on a slow cadence.
        error = failures.get("finished") or failures.get("failed")
        if error is not None and error.kind is ErrorKind.RATE_LIMITED:
            raise error
        limits.append("Failing apps and the latest deploys need Coolify's MCP server (Coolify: Settings, "
                      f"Advanced, API and MCP); it answered {error.kind.value if error else '?'}. The cockpit "
                      f"reads each app's history instead, every {int(FALLBACK_TTL_S // 60)} minutes.")
        now = time.time()
        for uuid in app_uuids:
            kept = known.get(uuid)
            if isinstance(kept, dict) and isinstance(kept.get("rows"), list) and \
                    0 <= now - float(kept.get("epoch") or 0) <= FALLBACK_TTL_S:
                histories[uuid] = kept
        stale = sorted((u for u in app_uuids if u not in histories),
                       key=lambda u: float((known.get(u) or {}).get("epoch") or 0))[:FALLBACK_READS]
        history_reads += len(stale)
        for uuid, rows_read in _history_rows(reader.client, stale, failures).items():
            histories[uuid] = {"key": "rest", "epoch": round(now, 1), "rows": rows_read}
        complete = fleet_mod.ListWindow(True)
        for uuid in app_uuids:
            if uuid in histories:
                fleet[uuid] = fleet_mod.fleet_row(uuid, [dict(r, application_uuid=uuid) for r in histories[uuid]["rows"]]
                                                  + [r for r in running_rows if r.get("application_uuid") == uuid],
                                                  finished=complete, failed=complete).to_dict()
            else:
                fleet[uuid] = fleet_mod.FleetRow(uuid, "unknown").to_dict()
    watch.raise_if_limited()
    by_res = {r.uuid: r for r in resources}
    for d in running + (latest or []):
        where = by_res.get(d.resource_uuid or "")
        if where is not None:
            d.project, d.environment = where.project, where.environment

    stats = counter.summary()
    stats["duration_ms"] = round((time.monotonic() - started) * 1000)
    stats["hint"] = "none" if hint is None else ("used" if metrics is early else "dropped")
    stats["metrics_reused"] = metrics.reused
    source = {"role": reader.slot.role.value, "setting": reader.slot.setting,
              "fingerprint": fingerprint(reader.slot.token),
              "scope": {True: "scoped", False: "team-wide", None: "unknown"}[reader.scoped]}
    skipped = [f"the {v.slot.role.word} ({v.slot.setting}): {v.state}" for v in views
               if v.state != "ok"]
    if skipped:
        errors.append("not used: " + "; ".join(skipped))
    stats["environments"] = "reused" if known_envs is not None else "read"
    stats["history_reads"] = history_reads
    return Snapshot(servers, resources, {"running": running}, _now(),
                    source, limits, errors, stats,
                    {str(k): list(v) for k, v in env_map.items()}, envs_epoch,
                    latest, fleet, histories)


# --- cache -------------------------------------------------------------------------------------

def cache_key(connection: Connection) -> str:
    material = connection.base_url + "|" + ",".join(f"{s.role.value}:{fingerprint(s.token)}"
                                                    for s in connection.slots)
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:16]


class SnapshotCache:
    """10 s cache in memory plus an optional JSON file, so separate processes share a snapshot."""

    def __init__(self, ttl_s: float = CACHE_TTL_S, clock: Callable[[], float] = time.time):
        self.ttl_s = ttl_s
        self._clock = clock
        self._memory: dict[str, tuple[float, dict]] = {}
        self._lock = threading.Lock()

    def _read_file(self, path: Optional[Path], key: str) -> Optional[tuple[float, dict]]:
        if path is None:
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(payload, dict) or payload.get("key") != key:
            return None
        stamp, snap = payload.get("stamp"), payload.get("snapshot")
        return (float(stamp), snap) if isinstance(stamp, (int, float)) and isinstance(snap, dict) else None

    @staticmethod
    def _write_file(path: Optional[Path], key: str, stamp: float, snap: dict) -> None:
        if path is None:
            return
        tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
        try:
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump({"key": key, "stamp": stamp, "snapshot": snap}, handle)
            os.replace(tmp, path)
        except OSError:
            try:
                tmp.unlink()
            except OSError:
                pass

    def _fresh(self, entry: Optional[tuple[float, dict]], now: float) -> bool:
        return entry is not None and 0 <= now - entry[0] <= self.ttl_s

    def peek(self, key: str, *, path: Optional[Path] = None) -> Optional[tuple[float, dict]]:
        """The newest stored (stamp, value) of any age, from memory or the file; never builds."""
        with self._lock:
            memory = self._memory.get(key)
            stored = self._read_file(path, key)
        if memory is None or (stored is not None and stored[0] > memory[0]):
            return stored
        return memory

    def get_or_build(self, key: str, build: Callable[[Optional[dict]], dict], *, path: Optional[Path] = None,
                     refresh: bool = False) -> tuple[dict, dict]:
        """Return (snapshot dict, cache info). One build at a time per process.

        `build` receives the newest stored snapshot of any age (or None), so it can start its
        reads with what that snapshot learned (`ReadHint`).
        """
        with self._lock:
            now = self._clock()
            memory = self._memory.get(key)
            stored = memory if self._fresh(memory, now) else (self._read_file(path, key) or memory)
            if not refresh and stored is not None and self._fresh(stored, now):
                self._memory[key] = stored
                layer = "memory" if stored is memory else "file"
                return stored[1], {"hit": layer, "age_s": round(now - stored[0], 1), "ttl_s": self.ttl_s}
            snap = build(stored[1] if stored is not None else None)
            stamp = self._clock()
            self._memory[key] = (stamp, snap)
            self._write_file(path, key, stamp, snap)
            return snap, {"hit": None, "age_s": 0.0, "ttl_s": self.ttl_s}


SNAPSHOT_CACHE = SnapshotCache()


def snapshot_dict(connection: Connection, *, transport: Transport = urllib_transport,
                  cache: SnapshotCache = SNAPSHOT_CACHE, cache_file: Optional[Path] = None,
                  refresh: bool = False) -> dict:
    """The cached Snapshot as a dict, with a `cache` field saying whether it was reused."""
    snap, info = cache.get_or_build(
        cache_key(connection),
        lambda last: build_snapshot(connection, transport=transport, hint=hint_from(last)).to_dict(),
        path=cache_file, refresh=refresh)
    return {**snap, "cache": info}


# --- one resource: lookup and deployment history (on demand, not part of the snapshot) ----------

class ResourceLookupError(ValueError):
    """The resource name or uuid matched nothing, or more than one resource."""


def find_resource(resources: list[dict], query: str) -> dict:
    """Exact uuid, then exact name (any case), then a unique part of a name."""
    q = (query or "").strip()
    if not q:
        raise ResourceLookupError("Name a resource (its name or uuid).")
    hits = [r for r in resources if r["uuid"] == q]
    if not hits:
        hits = [r for r in resources if r["name"].lower() == q.lower()]
    if not hits:
        hits = [r for r in resources if q.lower() in r["name"].lower()]
    if len(hits) == 1:
        return hits[0]
    if not hits:
        raise ResourceLookupError(f"No resource matches {q!r} among the {len(resources)} this token can see.")
    listed = ", ".join(f"{r['name']} ({r['kind']}, {r.get('project')}/{r.get('environment')}, {r['uuid']})"
                       for r in hits[:8])
    more = f" and {len(hits) - 8} more" if len(hits) > 8 else ""
    raise ResourceLookupError(f"{len(hits)} resources match {q!r}: {listed}{more}. Pass the uuid.")


def reader_client(connection: Connection, role: str, *, transport: Transport = urllib_transport,
                  timeout: float = 15.0) -> CoolifyClient:
    """The client for the token that read the snapshot (its role is in `source.role`)."""
    slot = next((s for s in connection.slots if s.role.value == role), connection.slots[0])
    return CoolifyClient(connection.base_url, slot.token, timeout=timeout, transport=transport)


def deployment_history(client: CoolifyClient, resource: dict, limit: int = HISTORY_DEFAULT) -> list[Deployment]:
    take = max(1, min(HISTORY_MAX, int(limit or HISTORY_DEFAULT)))
    payload = client.get(f"/deployments/applications/{resource['uuid']}", {"skip": 0, "take": take})
    rows = payload.get("deployments") if isinstance(payload, dict) else payload
    found = [to_deployment(r, client.base_url, {resource["name"]: resource["uuid"]})
             for r in rows or [] if isinstance(r, dict)]
    for d in found:
        d.resource_uuid = d.resource_uuid or resource["uuid"]
    found.sort(key=lambda d: d.created_at or "", reverse=True)
    return found[:take]


# Fields of GET /deployments/{uuid} worth showing; logs are never passed on here (a token with
# read:sensitive would return them, and log text goes through the M6 redaction path only).
DETAIL_FLAGS = ("force_rebuild", "is_webhook", "is_api", "rollback", "restart_only", "pull_request_id",
                "only_this_server")


def deployment_detail(client: CoolifyClient, deployment_uuid: str) -> dict[str, Any]:
    row = client.get(f"/deployments/{deployment_uuid}")
    if not isinstance(row, dict):
        raise CoolifyError(ErrorKind.SERVER, "unexpected deployment answer", path=f"/deployments/{deployment_uuid}")
    app = row.get("application") if isinstance(row.get("application"), dict) else {}
    if app.get("uuid") and not row.get("application_uuid"):
        row = {**row, "application_uuid": app.get("uuid")}
    out = asdict(to_deployment(row, client.base_url, {}))
    out["flags"] = {k: row[k] for k in DETAIL_FLAGS if row.get(k) not in (None, False, 0, "")}
    out["logs_included"] = False
    return out


# --- model-facing text -------------------------------------------------------------------------

MAX_OVERVIEW_CHARS = 6000
MAX_DEPLOYMENTS_CHARS = 4000
RUNNING_OK = ("running:healthy", "running:unknown", "running")


def _gib(value: Any) -> str:
    try:
        return f"{float(value) / 1024 ** 3:.1f}"
    except (TypeError, ValueError):
        return "?"


def _rate(value: Any) -> str:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return "?"
    return f"{v / 1024 / 1024:.1f} MB/s" if v >= 1024 * 1024 else f"{v / 1024:.1f} KB/s"


def _pct(value: Any) -> str:
    try:
        return f"{float(value):.0f}%"
    except (TypeError, ValueError):
        return "?"


def _when(stamp: Optional[str]) -> str:
    return (stamp or "?")[:16].replace("T", " ")


def _duration(created: Optional[str], finished: Optional[str]) -> str:
    try:
        a = _dt.datetime.fromisoformat(str(created).replace("Z", "+00:00"))
        b = _dt.datetime.fromisoformat(str(finished).replace("Z", "+00:00"))
    except ValueError:
        return ""
    seconds = int((b - a).total_seconds())
    return f", {seconds // 60}m{seconds % 60:02d}s" if seconds >= 0 else ""


def _server_line(s: dict) -> str:
    state = ", ".join(w for w, v in (("reachable", s.get("reachable")), ("usable", s.get("usable"))) if v) \
        or "not reachable"
    m = (s.get("metrics") or {}).get("current")
    if m:
        mem_total = m.get("memory_total_bytes") or 0
        mem_pct = f" ({float(m.get('memory_used_bytes') or 0) / mem_total * 100:.0f}%)" if mem_total else ""
        stats = (f"CPU {_pct(m.get('cpu_percent'))}, RAM {_gib(m.get('memory_used_bytes'))} of "
                 f"{_gib(mem_total)} GiB{mem_pct}, disk {_pct(m.get('disk_used_percent'))} of "
                 f"{_gib(m.get('disk_total_bytes'))} GiB, load1 {m.get('load1')}, "
                 f"net in {_rate(m.get('network_rx_bytes_per_sec'))} out {_rate(m.get('network_tx_bytes_per_sec'))}")
        age = time.time() - float(s["metrics"].get("fetched_epoch") or time.time())
        if age >= 15:
            stats += f" (metrics {age:.0f} s old)"
    else:
        stats = s.get("metrics_note") or "no metrics"
    return f"- {s.get('name')}: {state}. {stats}"


def _where(by_uuid: dict[str, dict], uuid: Optional[str]) -> str:
    r = by_uuid.get(uuid or "")
    return f" [{r.get('project') or '?'} / {r.get('environment') or '?'}]" if r else ""


def _deployment_line(d: dict, by_uuid: dict[str, dict]) -> str:
    message = (d.get("commit_message") or "").splitlines()[0][:70] if d.get("commit_message") else ""
    commit = (d.get("commit") or "")[:7]
    parts = [f"- {_when(d.get('created_at'))} {d.get('resource_name') or '?'}{_where(by_uuid, d.get('resource_uuid'))}",
             f"on {d['server_name']}" if d.get("server_name") else "",
             f"commit {commit}" if commit and commit != "HEAD" else (commit or ""),
             f'"{message}"' if message else "",
             f"(deployment {d.get('uuid')})"]
    return " ".join(p for p in parts if p)


def _matches(r: dict, project: Optional[str], environment: Optional[str]) -> bool:
    return ((not project or project.lower() in (r.get("project") or "").lower())
            and (not environment or environment.lower() in (r.get("environment") or "").lower()))


def _attention_line(a: dict, by_uuid: dict[str, dict]) -> str:
    fix = f" [fix: {a['fix']}]" if a.get("fix") else ""
    word = "at" if a.get("signal") == "env_changed" else "since"
    since = f" {word} {_when(a.get('since'))} UTC" if a.get("since") else ""
    detail = f" ({a['detail']})" if a.get("detail") else ""
    target = a.get("resource_uuid") or a.get("server_uuid")
    return f"- {a.get('severity')}: {a.get('title')}{detail}{since}{fix}" + (f" (uuid {target})" if target else "")


LATEST_IN_OVERVIEW = 8


def render_overview(snap: dict, *, project: Optional[str] = None, environment: Optional[str] = None,
                    max_chars: int = MAX_OVERVIEW_CHARS, attention: Optional[list] = None) -> str:
    """Compact text for the model, never longer than `max_chars`. Opens with what needs the user."""
    resources = snap.get("resources") or []
    by_uuid = {r["uuid"]: r for r in resources}
    filtered = bool(project or environment)
    shown = [r for r in resources if _matches(r, project, environment)]
    shown_uuids = {r["uuid"] for r in shown}
    cache = snap.get("cache") or {}
    stats = snap.get("stats") or {}
    source = snap.get("source") or {}
    head = [f"Coolify snapshot taken {_when(snap.get('taken_at'))} UTC "
            f"({'cached, ' + str(cache.get('age_s')) + ' s old' if cache.get('hit') else 'fresh'}; "
            f"read with the {Role(source.get('role')).word if source.get('role') in {r.value for r in Role} else 'token'}, "
            f"{source.get('scope')}; "
            f"{stats.get('calls')} calls in {(stats.get('duration_ms') or 0) / 1000:.1f} s)"]
    if filtered:
        head.append(f"Filter: project~{project or '*'} environment~{environment or '*'} "
                    f"({len(shown)} of {len(resources)} resources)")

    items = [a for a in (attention if attention is not None else snap.get("attention") or [])
             if not filtered or not a.get("resource_uuid") or a.get("resource_uuid") in shown_uuids]
    if items:
        lines = head + [f"NEEDS YOU ({len(items)}), worst first:"] + [_attention_line(a, by_uuid) for a in items]
    else:
        lines = head + ["NEEDS YOU: nothing. No app is failing, no server is down."]

    servers = snap.get("servers") or []
    lines += [f"SERVERS ({len(servers)})"] + ([_server_line(s) for s in servers] or ["- none readable"])

    deps = snap.get("deployments") or {}
    running = [d for d in deps.get("running") or [] if not filtered or d.get("resource_uuid") in shown_uuids]
    lines.append(f"DEPLOYMENTS running or queued: {len(running) or 'none'}")
    lines += [_deployment_line(d, by_uuid) + f" status {d.get('status')}" for d in running]
    latest = snap.get("latest_deployments")
    if latest is None:
        lines.append("Latest deploys: unknown (see LIMITS)")
    else:
        latest = [d for d in latest if d.get("status") in ("finished", "failed")
                  and (not filtered or d.get("resource_uuid") in shown_uuids)][:LATEST_IN_OVERVIEW]
        lines.append(f"LATEST DEPLOYS, newest first, all environments (times UTC):" if latest
                     else "Latest deploys: none in the window")
        lines += [_deployment_line(d, by_uuid) + f" {d.get('status')}"
                  + (" (fixed since by a later deploy)" if d.get("superseded") else "")
                  + (f" via {d['trigger']}" if d.get("trigger") else "") for d in latest]

    counts = {k: sum(1 for r in shown if r["kind"] == k) for k in KINDS}
    lines.append(f"RESOURCES: {counts['application']} applications, {counts['service']} services, "
                 f"{counts['database']} databases")
    down = [r for r in shown if (r.get("status") or "") not in RUNNING_OK]
    if down:
        lines.append(f"Not running or unhealthy ({len(down)}):")
        lines += [f"- {r['name']} ({r['kind']}, {r.get('project')} / {r.get('environment')}): {r.get('status')}"
                  for r in down]
    tail = []
    if snap.get("limits"):
        tail += ["LIMITS"] + [f"- {x}" for x in snap["limits"]]
    if snap.get("errors"):
        tail += ["ERRORS"] + [f"- {x}" for x in snap["errors"]]

    groups: dict[tuple, list[dict]] = {}
    for r in shown:
        groups.setdefault((r.get("project") or "?", r.get("environment") or "?"), []).append(r)
    listing = ["BY PROJECT (name kind status domain)"]
    for (proj, env), items in groups.items():
        listing.append(f"{proj} / {env}:")
        for r in items:
            domain = (r.get("fqdn") or "").split(",")[0]
            listing.append(f"  {r['name']} {r['kind'][:3]} {r.get('status') or '?'}" + (f" {domain}" if domain else ""))

    def size(parts: list[str]) -> int:
        return sum(len(p) + 1 for p in parts)

    budget = max_chars - size(lines) - size(tail) - 120
    kept: list[str] = []
    for line in listing:
        if size(kept) + len(line) + 1 > budget:
            hidden = sum(1 for x in listing[len(kept):] if x.startswith("  "))
            kept.append(f"... {hidden} more resources not listed; call coolify_overview with project= "
                        "or environment= to narrow")
            break
        kept.append(line)
    text = "\n".join(lines + kept + tail)
    if len(text) > max_chars:  # header sections alone overflow: cut hard, say so
        text = text[: max_chars - 40].rsplit("\n", 1)[0] + "\n... output cut at the size cap"
    return text


def render_history(resource: dict, deployments: list[Deployment], *, max_chars: int = MAX_DEPLOYMENTS_CHARS) -> str:
    head = (f"{resource['name']} ({resource['kind']}, {resource.get('project')} / {resource.get('environment')}, "
            f"status {resource.get('status')}, uuid {resource['uuid']}): ")
    if not deployments:
        return head + "no deployments recorded."
    lines = [head + f"last {len(deployments)} deployments, newest first (times UTC):"]
    for d in deployments:
        message = (d.commit_message or "").splitlines()[0][:70] if d.commit_message else ""
        lines.append(f"- {_when(d.created_at)} {d.status}{_duration(d.created_at, d.finished_at)}"
                     f" commit {(d.commit or '?')[:7]}" + (f' "{message}"' if message else "")
                     + f" (deployment {d.uuid})")
    if deployments[0].url:
        lines.append(f"Newest deployment in Coolify: {deployments[0].url}")
    text = "\n".join(lines)
    return text if len(text) <= max_chars else text[: max_chars - 30].rsplit("\n", 1)[0] + "\n... cut at the size cap"


def render_detail(detail: dict) -> str:
    message = (detail.get("commit_message") or "").strip()
    lines = [f"Deployment {detail['uuid']} of {detail.get('resource_name')} ({detail.get('resource_uuid')}): "
             f"{detail.get('status')}",
             f"created {_when(detail.get('created_at'))} UTC, finished {_when(detail.get('finished_at'))} UTC"
             f"{_duration(detail.get('created_at'), detail.get('finished_at'))}",
             f"server {detail.get('server_name')}, commit {detail.get('commit')}"]
    if message:
        lines.append("commit message: " + message[:300])
    if detail.get("flags"):
        lines.append("flags: " + ", ".join(f"{k}={v}" for k, v in detail["flags"].items()))
    if detail.get("url"):
        lines.append("open in Coolify: " + detail["url"])
    lines.append("Build logs are not included here (they need the read:sensitive right); "
                 "the user can paste the log for a diagnosis.")
    return "\n".join(lines)
