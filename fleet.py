"""What needs the user: one `FleetRow` per application and the `Signal` registry.

The snapshot reads the team's newest deployments in two MCP calls (`list_deployments` with
`status=finished` and `status=failed`, 100 rows each) plus the running queue. From those rows this
module derives, per application:

    FleetRow  { resource_uuid, latest, last_good, running, state, failing_since,
                env_changed_at?, behind? }
    state     ok | failing | deploying | never_deployed | unknown

The failing rule: an application is failing when its newest settled deployment
(finished or failed) failed and no newer deployment is running. A cancelled deployment counts as
nothing (it is in neither list). When a list is cut (more rows than one page), a row older than the
other list's oldest row could hide a newer opposite result; such an application gets one REST
history read before it counts (`needs_history`), and the verdict is kept until it deploys again.

Every count the page, the status-bar chip, the agent and the alert job show comes from one list of
`Attention` items, built by the `Signal` registry below. Nothing counts failures on its own.

    Signal    { id, label, severity: bad|warn|info, needs, find(view) -> [Attention] }
    Attention { signal, severity, resource_uuid? | server_uuid?, title, since, fix? }

Pure: no requests here. The snapshot passes rows in; `signals_for` takes the extra facts (env
check, version, branch heads, thresholds) as plain dicts.
"""

from __future__ import annotations

import datetime as _dt
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Iterable, Mapping, Optional

SETTLED = ("finished", "failed")
RUNNING = ("queued", "in_progress")
STATES = ("ok", "failing", "deploying", "never_deployed", "unknown")
DISK_WARN_DEFAULT = 85
MEMORY_WARN_DEFAULT = 90


def _at(row: Optional[Mapping]) -> str:
    return str((row or {}).get("created_at") or "")


def trigger_of(row: Mapping) -> str:
    """webhook | api | mcp | manual, from Coolify's flags (measured over 178 rows)."""
    if row.get("is_webhook"):
        return "webhook"
    if row.get("is_api"):
        return "api"
    if row.get("is_mcp"):
        return "mcp"
    return "manual"


@dataclass
class ListWindow:
    """How far back one MCP list reaches: complete, or cut at its oldest row."""
    complete: bool
    oldest: Optional[str] = None

    def covers(self, stamp: str) -> bool:
        return self.complete or (self.oldest is not None and stamp >= self.oldest)


def window_of(payload: Any, rows: list[Mapping]) -> ListWindow:
    page = payload.get("_pagination") if isinstance(payload, Mapping) else None
    total = page.get("total") if isinstance(page, Mapping) else None
    complete = isinstance(total, int) and total <= len(rows)
    return ListWindow(complete, min((_at(r) for r in rows), default=None))


@dataclass
class FleetRow:
    resource_uuid: str
    state: str
    latest: Optional[dict] = None       # newest deployment of any status we know
    last_good: Optional[dict] = None    # newest finished deployment
    running: Optional[dict] = None      # a queued or running deployment
    failing_since: Optional[str] = None
    env_changed_at: Optional[str] = None
    behind: Optional[dict] = None       # {head, commits?, source}
    checked: str = "lists"              # lists | history (the window edge was read)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _short(row: Optional[Mapping]) -> Optional[dict]:
    if not row:
        return None
    keep = ("deployment_uuid", "uuid", "status", "commit", "commit_message", "created_at", "finished_at",
            "server_name")
    out = {k: row.get(k) for k in keep if row.get(k) is not None}
    out["uuid"] = str(row.get("deployment_uuid") or row.get("uuid") or "")
    out.pop("deployment_uuid", None)
    out["trigger"] = trigger_of(row)
    if out.get("commit_message"):
        out["commit_message"] = str(out["commit_message"]).splitlines()[0][:120]
    return out


def needs_history(rows: list[Mapping], finished: ListWindow, failed: ListWindow) -> bool:
    """True when the app's newest settled row could be hidden by a cut list (the window edge)."""
    settled = [r for r in rows if r.get("status") in SETTLED]
    if not settled:
        return False
    newest = max(settled, key=_at)
    other = finished if newest.get("status") == "failed" else failed
    return not other.covers(_at(newest))


def fleet_row(uuid: str, rows: list[Mapping], *, finished: ListWindow, failed: ListWindow,
              history: Optional[list[Mapping]] = None) -> FleetRow:
    """One application's row from its deployment rows (any order). `history` is a REST history
    read for an app at the window edge; it replaces the list rows for the settled verdict."""
    rows = sorted(rows, key=_at, reverse=True)
    running = next((r for r in rows if r.get("status") in RUNNING), None)
    source = sorted(history, key=_at, reverse=True) if history is not None else rows
    settled = [r for r in source if r.get("status") in SETTLED]
    finished_rows = [r for r in settled if r.get("status") == "finished"]
    last_good = finished_rows[0] if finished_rows else None
    row = FleetRow(uuid, "unknown", _short(rows[0] if rows else None), _short(last_good), _short(running),
                   checked="history" if history is not None else "lists")
    if running is not None:
        row.state = "deploying"
    elif settled and settled[0].get("status") == "failed":
        row.state = "failing"
    elif settled:
        row.state = "ok"
    elif finished.complete and failed.complete:
        row.state = "never_deployed"
    if settled and settled[0].get("status") == "failed":
        streak = [r for r in settled if not last_good or _at(r) > _at(last_good)]
        row.failing_since = _at(streak[-1]) if streak else _at(settled[0])
    return row


def build_fleet(app_uuids: Iterable[str], rows: list[Mapping], *, finished: ListWindow, failed: ListWindow,
                histories: Optional[Mapping[str, list]] = None) -> dict[str, FleetRow]:
    by_app: dict[str, list[Mapping]] = {}
    for r in rows:
        if r.get("application_uuid"):
            by_app.setdefault(str(r["application_uuid"]), []).append(r)
    histories = histories or {}
    return {uuid: fleet_row(uuid, by_app.get(uuid, []), finished=finished, failed=failed,
                            history=histories.get(uuid))
            for uuid in app_uuids}


def superseded(rows: list[Mapping]) -> set[str]:
    """Failed deployments a later finished deployment of the same application replaced."""
    newest_good: dict[str, str] = {}
    for r in rows:
        if r.get("status") == "finished" and r.get("application_uuid"):
            app = str(r["application_uuid"])
            newest_good[app] = max(newest_good.get(app, ""), _at(r))
    return {str(r.get("deployment_uuid") or r.get("uuid")) for r in rows
            if r.get("status") == "failed" and _at(r) < newest_good.get(str(r.get("application_uuid")), "")}


# --- signals ----------------------------------------------------------------------------------

@dataclass
class Attention:
    signal: str
    severity: str           # bad | warn | info
    title: str
    resource_uuid: Optional[str] = None
    server_uuid: Optional[str] = None
    since: Optional[str] = None
    fix: Optional[str] = None   # an ActionSpec kind
    detail: Optional[str] = None
    badge: Optional[str] = None  # the app row's short word; the signal's label when unset

    def to_dict(self) -> dict[str, Any]:
        return {k: v for k, v in asdict(self).items() if v is not None}

    @property
    def key(self) -> str:
        """Stable identity for the alert job: signal plus the thing it is about."""
        return f"{self.signal}:{self.resource_uuid or self.server_uuid or '-'}"


@dataclass
class View:
    """What the signals read: the snapshot dict plus facts gathered outside it."""
    snapshot: Mapping[str, Any]
    env_changes: Mapping[str, str] = field(default_factory=dict)      # app uuid -> changed_at
    version: Mapping[str, Any] = field(default_factory=dict)          # version-check.json fields
    branches: Mapping[str, Mapping] = field(default_factory=dict)     # app uuid -> {head, commits?, source}
    disk_warn: float = DISK_WARN_DEFAULT
    memory_warn: float = MEMORY_WARN_DEFAULT

    @property
    def resources(self) -> dict[str, Mapping]:
        return {str(r.get("uuid")): r for r in self.snapshot.get("resources") or []}

    @property
    def fleet(self) -> Mapping[str, Mapping]:
        return self.snapshot.get("fleet") or {}


@dataclass(frozen=True)
class Signal:
    id: str
    label: str              # the chip's word: "1 failing", "2 env changed"
    severity: str
    needs: str              # the FeatureRule id it depends on (capabilities.py)
    find: Callable[[View], list[Attention]]
    description: str = ""
    confirm: int = 1        # alert job: runs in a row before it is announced, and before it recovers


def _name(view: View, uuid: str) -> str:
    return str((view.resources.get(uuid) or {}).get("name") or uuid)


def _where(view: View, uuid: str) -> str:
    r = view.resources.get(uuid) or {}
    env = r.get("environment")
    return f" ({env})" if env else ""


def _failing(view: View) -> list[Attention]:
    out = []
    for uuid, row in view.fleet.items():
        if row.get("state") != "failing":
            continue
        good = row.get("last_good") or {}
        out.append(Attention("failing", "bad", f"{_name(view, uuid)}{_where(view, uuid)} failed its last deploy",
                             resource_uuid=uuid, since=row.get("failing_since"),
                             fix="rollback" if good.get("commit") and good.get("commit") != "HEAD" else "deploy",
                             detail=f"last good {str(good.get('commit') or '')[:7]}" if good.get("commit") else None))
    return out


def is_unhealthy(status: Any) -> bool:
    """Running but failing its health check, degraded, or restarting. A stopped resource
    (`exited`, `exited:unhealthy`) is not an attention item: it is often stopped on purpose."""
    s = str(status or "").lower()
    return (s.startswith("running") and "unhealthy" in s) or s.startswith("degraded") or s.startswith("restarting")


def _unhealthy(view: View) -> list[Attention]:
    out = []
    for r in view.snapshot.get("resources") or []:
        if is_unhealthy(r.get("status")):
            uuid = str(r.get("uuid"))
            out.append(Attention("unhealthy", "bad", f"{r.get('name')}{_where(view, uuid)} is {r.get('status')}",
                                 resource_uuid=uuid, fix="restart"))
    return out


def _server_down(view: View) -> list[Attention]:
    out = []
    for s in view.snapshot.get("servers") or []:
        if s.get("reachable") is False or s.get("usable") is False:
            what = "not reachable" if s.get("reachable") is False else "not usable"
            out.append(Attention("server_down", "bad", f"Server {s.get('name')} is {what}",
                                 server_uuid=str(s.get("uuid"))))
    return out


def _pressure(view: View) -> list[Attention]:
    out = []
    for s in view.snapshot.get("servers") or []:
        current = ((s.get("metrics") or {}).get("current")) or {}
        disk = current.get("disk_used_percent")
        used, total = current.get("memory_used_bytes"), current.get("memory_total_bytes")
        parts = []
        if isinstance(disk, (int, float)) and disk >= view.disk_warn:
            parts.append(f"disk {disk:.0f} %")
        if isinstance(used, (int, float)) and isinstance(total, (int, float)) and total:
            mem = used / total * 100
            if mem >= view.memory_warn:
                parts.append(f"memory {mem:.0f} %")
        if parts:
            out.append(Attention("server_pressure", "warn", f"{s.get('name')} at {' and '.join(parts)}",
                                 server_uuid=str(s.get("uuid"))))
    return out


def _env_changed(view: View) -> list[Attention]:
    out = []
    for uuid, changed in sorted(view.env_changes.items()):
        if uuid in view.fleet and (view.fleet[uuid] or {}).get("state") != "deploying":
            out.append(Attention("env_changed", "warn", f"{_name(view, uuid)}{_where(view, uuid)}: env changed after its last deploy",
                                 resource_uuid=uuid, since=changed, fix="deploy"))
    return out


def _update(view: View) -> list[Attention]:
    v = view.version or {}
    if v.get("update_available") is True and v.get("latest_version"):
        return [Attention("update_available", "info", f"Coolify {v['latest_version']} is out",
                          detail=f"this instance runs {v.get('coolify_version')}")]
    return []


def _behind(view: View) -> list[Attention]:
    out = []
    for uuid, info in sorted(view.branches.items()):
        row = view.fleet.get(uuid) or {}
        deployed = str(((row.get("last_good") or {}).get("commit")) or "")
        head = str(info.get("head") or "")
        if not head or len(deployed) < 7 or deployed == "HEAD" or row.get("state") in ("deploying", "failing"):
            continue
        if head.startswith(deployed[:12]) or deployed.startswith(head[:12]):
            continue
        commits = info.get("commits")
        if commits == 0:  # the head is behind what runs (a rollback or a force push): nothing to deploy
            continue
        branch = (view.resources.get(uuid) or {}).get("git_branch") or "its branch"
        ahead = f"{commits} commit{'s' if commits != 1 else ''} ahead" if isinstance(commits, int) else "ahead"
        out.append(Attention("behind_branch", "warn", f"{_name(view, uuid)}{_where(view, uuid)}: {branch} is {ahead}",
                             resource_uuid=uuid, fix="deploy", detail=f"head {head[:7]}, deployed {deployed[:7]}",
                             badge=f"{commits} ahead" if isinstance(commits, int) else "ahead"))
    return out


SIGNALS: tuple[Signal, ...] = (
    Signal("failing", "failing", "bad", "deployments", _failing,
           "The newest finished-or-failed deployment failed and none is running."),
    Signal("unhealthy", "unhealthy", "bad", "resources", _unhealthy,
           "Running but failing its health check, degraded or restarting.", confirm=2),
    Signal("server_down", "server down", "bad", "servers", _server_down, "A server is not reachable or not usable."),
    Signal("server_pressure", "server pressure", "warn", "server_stats", _pressure,
           "Disk or memory at or over the warning level.", confirm=2),
    Signal("env_changed", "env changed", "warn", "resources", _env_changed,
           "An env variable changed after the last finished deployment."),
    Signal("behind_branch", "branch ahead", "warn", "deployments", _behind,
           "The branch head differs from the deployed commit."),
    Signal("update_available", "update", "info", "update_notice", _update, "A newer Coolify release exists."),
)
SIGNALS_BY_ID = {s.id: s for s in SIGNALS}
SEVERITY_ORDER = {"bad": 0, "warn": 1, "info": 2}


def attention(view: View) -> list[Attention]:
    """Every signal's items, worst first, then by signal order."""
    order = {s.id: i for i, s in enumerate(SIGNALS)}
    items = [item for s in SIGNALS for item in s.find(view)]
    return sorted(items, key=lambda a: (SEVERITY_ORDER.get(a.severity, 9), order[a.signal], a.title.lower()))


def summary(items: list[Attention]) -> list[dict[str, Any]]:
    """Counts per signal, in registry order: what the chip row shows."""
    out = []
    for s in SIGNALS:
        n = sum(1 for a in items if a.signal == s.id)
        if n:
            out.append({"signal": s.id, "label": s.label, "severity": s.severity, "count": n})
    return out


def parse_stamp(value: Any) -> Optional[_dt.datetime]:
    try:
        return _dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
