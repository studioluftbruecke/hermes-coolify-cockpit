"""What a Coolify token setup can do: version, update, per-token rights and scope, and the feature
lists the setup unlocks.

Rights are detected, never configured. Coolify checks a token's right before it looks the
resource up, so a call against a made-up UUID answers 404 when the token holds the right and 403
naming the right when it does not. Only that made-up UUID ever receives a non-GET request here.

Every feature the setup can or cannot use is one `FeatureRule` row; the setup feedback, the agent
tool and (later) the Desktop widgets read the same rows.
"""

from __future__ import annotations

import datetime as _dt
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Mapping, Optional
from urllib.parse import urlsplit

try:
    from .coolify_client import (CoolifyClient, CoolifyError, ErrorKind, LimitWatch, Transport,
                                 fetch_public_json, fingerprint, header_safe, normalize_base_url,
                                 urllib_transport)
except ImportError:  # loaded as a top-level module (tests, scripts)
    from coolify_client import (CoolifyClient, CoolifyError, ErrorKind, LimitWatch,  # type: ignore
                                Transport, fetch_public_json, fingerprint, header_safe,
                                normalize_base_url, urllib_transport)

BOGUS_UUID = "hermes-permission-probe-does-not-exist"
RELEASES_URL = "https://cdn.coollabs.io/coolify/versions.json"
MIN_SCOPED_VERSION = (4, 6, 0)
MAX_PROJECT_DETAILS = 60


# --- domain ------------------------------------------------------------------------------------

class Ability(str, Enum):
    READ = "read"
    READ_SENSITIVE = "read:sensitive"
    DEPLOY = "deploy"
    WRITE = "write"
    DELETE = "delete"


class Role(str, Enum):
    ACTION = "api"          # COOLIFYBRUECKE_TOKEN when a read-only token is also set
    OVERVIEW = "read_only"  # COOLIFYBRUECKE_READ_TOKEN when an API token is also set
    SINGLE = "single"       # the only token set, in either field

    @property
    def word(self) -> str:
        """How every text names this token: "API token", "read-only token", or just "token"."""
        return {"api": "API token", "read_only": "read-only token"}.get(self.value, "token")


class Lane(str, Enum):
    READ = "read"      # any token; the one with the widest view answers
    ACTION = "action"  # only the API token (or the only token), never the read-only token


class TokenStatus(str, Enum):
    OK = "ok"
    UNAUTHORIZED = "unauthorized"
    UNREACHABLE = "unreachable"
    ERROR = "error"


@dataclass(frozen=True)
class AbilityProbe:
    ability: Ability
    method: str
    path: str
    params: Optional[Mapping[str, str]] = None


# Every path names BOGUS_UUID; these answers were measured against Coolify 4.6.
ABILITY_PROBES: tuple[AbilityProbe, ...] = (
    AbilityProbe(Ability.READ, "GET", f"/applications/{BOGUS_UUID}"),
    AbilityProbe(Ability.READ_SENSITIVE, "GET", f"/applications/{BOGUS_UUID}/logs"),
    AbilityProbe(Ability.DEPLOY, "POST", "/deploy", {"uuid": BOGUS_UUID}),
    AbilityProbe(Ability.WRITE, "PATCH", f"/applications/{BOGUS_UUID}"),
    AbilityProbe(Ability.DELETE, "DELETE", f"/applications/{BOGUS_UUID}"),
)


@dataclass(frozen=True)
class TokenSlot:
    role: Role
    token: str = field(repr=False)
    setting: str  # where the user stored it, for messages


@dataclass(frozen=True)
class Connection:
    base_url: str
    slots: tuple[TokenSlot, ...]
    notes: tuple[str, ...] = ()


class ConfigError(ValueError):
    pass


URL_SETTING = "COOLIFYBRUECKE_URL"
ACTION_SETTING = "COOLIFYBRUECKE_TOKEN"
READ_SETTING = "COOLIFYBRUECKE_READ_TOKEN"


def connection_from_settings(url: Optional[str], action_token: Optional[str],
                             read_token: Optional[str]) -> Connection:
    """Any combination of one or two tokens is a valid setup; a lone token is the only token."""
    if not (url or "").strip():
        raise ConfigError(f"No Coolify URL set. Set {URL_SETTING} in the plugin settings "
                          "(the address you open Coolify at, e.g. https://coolify.example.com).")
    try:
        base = normalize_base_url(url or "")
    except ValueError as error:
        raise ConfigError(f"{URL_SETTING}: {error}") from None
    action = (action_token or "").strip()
    read = (read_token or "").strip()
    for setting, value in ((ACTION_SETTING, action), (READ_SETTING, read)):
        if value and not header_safe(value):
            found = "a space or line break" if re.search(r"\s", value) else "a character no token has"
            raise ConfigError(f"{setting} contains {found}, so it cannot be sent. Store the token again "
                              "as one line in the plugin settings.")
    if not action and not read:
        raise ConfigError(f"No Coolify token set. Store an API token as {ACTION_SETTING} in the "
                          f"plugin settings; optionally a team-wide read-only token as {READ_SETTING}.")
    notes = []
    if base.startswith("http://") and urlsplit(base).hostname not in {"localhost", "127.0.0.1", "::1"}:
        notes.append(f"{URL_SETTING} uses plain HTTP; tokens are sent without encryption. Use HTTPS outside localhost.")
    if action and read and action == read:
        slots = (TokenSlot(Role.SINGLE, action, ACTION_SETTING),)
        notes.append(f"{ACTION_SETTING} and {READ_SETTING} hold the same token; it is used as the only token.")
    elif action and read:
        slots = (TokenSlot(Role.ACTION, action, ACTION_SETTING), TokenSlot(Role.OVERVIEW, read, READ_SETTING))
    elif action:
        slots = (TokenSlot(Role.SINGLE, action, ACTION_SETTING),)
    else:
        slots = (TokenSlot(Role.SINGLE, read, READ_SETTING),)
    return Connection(base, slots, tuple(notes))


@dataclass
class EnvironmentView:
    uuid: str
    name: str


@dataclass
class ProjectView:
    uuid: str
    name: str
    environments: list[EnvironmentView] = field(default_factory=list)


@dataclass
class ServerView:
    uuid: str
    name: str
    reachable: Optional[bool]
    usable: Optional[bool]
    metrics_enabled: Optional[bool]


@dataclass
class Sees:
    projects: list[ProjectView] = field(default_factory=list)
    applications: Optional[int] = None
    services: Optional[int] = None
    databases: Optional[int] = None
    servers: Optional[int] = None  # None: refused (scoped) or not readable
    projects_truncated: bool = False


@dataclass
class TokenInfo:
    role: Role
    setting: str
    fingerprint: str
    status: TokenStatus = TokenStatus.OK
    scoped: Optional[bool] = None
    abilities: dict[Ability, Optional[bool]] = field(default_factory=dict)
    sees: Sees = field(default_factory=Sees)
    servers: list[ServerView] = field(default_factory=list)
    error: Optional[str] = None
    notes: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.status is TokenStatus.OK

    def holds(self, ability: Ability) -> bool:
        return self.ok and self.abilities.get(ability) is True

    @property
    def label(self) -> str:
        return f"{self.role.word} ({self.setting}, sha256 {self.fingerprint})"


@dataclass
class InstanceFacts:
    reachable: bool
    unreachable_reason: Optional[str] = None
    coolify_version: Optional[str] = None
    latest_version: Optional[str] = None
    update_available: Optional[bool] = None
    update_check: str = "off"  # on | off | failed
    mcp_enabled: Optional[bool] = None
    mcp_detail: Optional[str] = None
    metrics_enabled_by_server: dict[str, bool] = field(default_factory=dict)


@dataclass(frozen=True)
class Needs:
    abilities: tuple[Ability, ...] = ()
    lane: Lane = Lane.READ
    unscoped: bool = False
    mcp: bool = False
    metrics: bool = False
    update_check: bool = False


@dataclass(frozen=True)
class FeatureRule:
    id: str
    label: str
    needs: Needs
    reason_if_missing: str
    unlock_hint: str
    reason_if_scoped: str = ""
    unlock_if_scoped: str = ""


@dataclass
class Feature:
    id: str
    label: str
    available: bool
    reason: Optional[str] = None
    unlock: Optional[str] = None
    via: Optional[Role] = None


@dataclass
class Capabilities:
    base_url: str
    instance: InstanceFacts
    tokens: list[TokenInfo]
    features: list[Feature]
    routing: dict[str, Optional[str]]
    warnings: list[str]
    checked_at: str

    @property
    def available(self) -> list[Feature]:
        return [f for f in self.features if f.available]

    @property
    def not_available(self) -> list[Feature]:
        return [f for f in self.features if not f.available]


# --- the FeatureRule registry ------------------------------------------------------------------

OVERVIEW_HINT = (f"add a team-wide read-only token ({READ_SETTING}), or use a "
                 "token without a scope")

FEATURE_RULES: tuple[FeatureRule, ...] = (
    FeatureRule(
        "connection", "Connection, version and token rights", Needs(),
        "No token was accepted by the instance",
        f"store a valid API token as {ACTION_SETTING} (Coolify: Keys & Tokens → API tokens)"),
    FeatureRule(
        "resources", "Applications, services and databases with status",
        Needs(abilities=(Ability.READ,)),
        "No token holds `read`", "give the token the `read` right"),
    FeatureRule(
        "team_view", "Every project of the team",
        Needs(abilities=(Ability.READ,), unscoped=True),
        "No token holds `read`", "give the token the `read` right",
        "Only the {visible_projects} project(s) inside the token's scope are visible", OVERVIEW_HINT),
    FeatureRule(
        "deployments", "Deployment queue and history",
        Needs(abilities=(Ability.READ,)),
        "No token holds `read`", "give the token the `read` right"),
    FeatureRule(
        "servers", "Server list and health",
        Needs(abilities=(Ability.READ,), unscoped=True),
        "No token holds `read`", "give the token the `read` right",
        "No server data (scoped tokens cannot read servers)", OVERVIEW_HINT),
    FeatureRule(
        "server_stats", "Server stats: CPU, RAM, disk, load, network",
        Needs(abilities=(Ability.READ,), unscoped=True, mcp=True, metrics=True),
        "No token holds `read`", "give the token the `read` right",
        "No server stats (scoped tokens cannot read servers)", OVERVIEW_HINT),
    FeatureRule(
        "actions", "Deploy, restart, start, stop, cancel, rollback",
        Needs(abilities=(Ability.DEPLOY,), lane=Lane.ACTION),
        "No actions (the {action_token} lacks `deploy`)",
        f"give the {{action_token}} the `deploy` right, or store a token that has it as {ACTION_SETTING}"),
    FeatureRule(
        "live_logs", "Live deployment logs (diagnosis without pasting)",
        Needs(abilities=(Ability.READ_SENSITIVE,)),
        "No live logs (no token holds `read:sensitive`)",
        "paste failed logs into the chat for a diagnosis, or add `read:sensitive` to the "
        "{action_token}'s scope"),
    FeatureRule(
        "update_notice", "Update notice for new Coolify releases",
        Needs(update_check=True),
        "No token was accepted by the instance",
        f"store a valid API token as {ACTION_SETTING}"),
)


# --- probing -----------------------------------------------------------------------------------

NOT_COOLIFY_NOTE = ("the address answered success for an application that does not exist, where Coolify answers "
                    "404, so it is not Coolify's API answering (a proxy or another service?); set the Coolify URL "
                    "to the address its API answers at")


def probe_right(client: CoolifyClient, probe: AbilityProbe) -> bool:
    """One right probe: held = 404 (or another 4xx), missing = 403 naming the right.

    Any other answer raises its CoolifyError, since it says nothing about the right. A success for
    the made-up UUID is no answer Coolify gives (an empty body, 204 and `{}` included), so it
    raises as unreachable instead of counting as held.
    """
    assert BOGUS_UUID in probe.path or BOGUS_UUID in (probe.params or {}).values()
    try:
        client.request(probe.method, probe.path, params=probe.params,
                       json_body=None if probe.method in ("GET", "DELETE") else {})
    except CoolifyError as error:
        if error.kind in (ErrorKind.NOT_FOUND, ErrorKind.BAD_REQUEST):
            return True
        if error.kind is ErrorKind.FORBIDDEN:
            return False
        raise
    raise CoolifyError(ErrorKind.UNREACHABLE, NOT_COOLIFY_NOTE, method=probe.method, path=probe.path, sent=False)


def probe_abilities(client: CoolifyClient) -> tuple[dict[Ability, Optional[bool]], Optional[CoolifyError]]:
    """Every right probe in turn (`probe_right`); a right with no clear answer is None.

    Stops at the first 401, unreachable or 429 answer and returns it, since no later probe can
    succeed (and each one more would spend from the rate limit that just ran out).
    """
    found: dict[Ability, Optional[bool]] = {}
    for probe in ABILITY_PROBES:
        try:
            found[probe.ability] = probe_right(client, probe)
        except CoolifyError as error:
            if error.kind in (ErrorKind.UNAUTHORIZED, ErrorKind.UNREACHABLE, ErrorKind.RATE_LIMITED):
                return found, error
            found[probe.ability] = None
    return found, None


def _project_details(client: CoolifyClient, projects: list[dict]) -> list[ProjectView]:
    def one(project: dict) -> ProjectView:
        view = ProjectView(str(project.get("uuid", "")), str(project.get("name", "")))
        try:
            detail = client.get(f"/projects/{view.uuid}")
        except CoolifyError:
            return view
        environments = detail.get("environments") if isinstance(detail, dict) else None
        for env in environments if isinstance(environments, list) else []:
            if isinstance(env, dict):
                view.environments.append(EnvironmentView(str(env.get("uuid", "")), str(env.get("name", ""))))
        return view

    with ThreadPoolExecutor(max_workers=8) as pool:
        return list(pool.map(one, projects))


def _count(client: CoolifyClient, path: str) -> Optional[int]:
    try:
        payload = client.get(path)
    except CoolifyError:
        return None
    return len(payload) if isinstance(payload, list) else None


def inspect_token(base_url: str, slot: TokenSlot, transport: Transport, timeout: float) -> TokenInfo:
    client = CoolifyClient(base_url, slot.token, timeout=timeout, transport=transport)
    info = TokenInfo(slot.role, slot.setting, fingerprint(slot.token))
    abilities, fatal = probe_abilities(client)
    info.abilities = {a: abilities.get(a) for a in Ability}
    if fatal is not None:
        info.status = {ErrorKind.UNAUTHORIZED: TokenStatus.UNAUTHORIZED,
                       ErrorKind.UNREACHABLE: TokenStatus.UNREACHABLE}.get(fatal.kind, TokenStatus.ERROR)
        info.error = fatal.message
        return info
    unknown = [a.value for a, held in info.abilities.items() if held is None]
    if unknown:
        info.notes.append("could not determine: " + ", ".join(unknown))
    if not info.holds(Ability.READ):
        return info

    # Scoped or team-wide: GET /servers answers 403 "limited to selected ..." for a scoped token.
    try:
        servers = client.get("/servers")
        info.scoped = False
        for server in servers if isinstance(servers, list) else []:
            settings = server.get("settings") if isinstance(server.get("settings"), dict) else {}
            info.servers.append(ServerView(
                str(server.get("uuid", "")), str(server.get("name", "")),
                server.get("is_reachable"), server.get("is_usable"), settings.get("is_metrics_enabled")))
        info.sees.servers = len(info.servers)
    except CoolifyError as error:
        if error.kind is ErrorKind.SCOPED_REFUSED:
            info.scoped = True
        else:
            info.notes.append(f"GET /servers: {error.kind.value}")

    projects: list[dict] = []
    try:
        listed = client.get("/projects")
        projects = [p for p in listed if isinstance(p, dict)] if isinstance(listed, list) else []
    except CoolifyError as error:
        info.notes.append(f"GET /projects: {error.kind.value}")
    if len(projects) > MAX_PROJECT_DETAILS:
        info.sees.projects_truncated = True
    info.sees.projects = _project_details(client, projects[:MAX_PROJECT_DETAILS])
    info.sees.applications = _count(client, "/applications")
    info.sees.services = _count(client, "/services")
    info.sees.databases = _count(client, "/databases")
    return info


def _version_key(version: str) -> tuple[int, ...]:
    core = str(version).strip().lstrip("v").split("-")[0]
    return tuple(int(part) if part.isdigit() else 0 for part in core.split("."))


def instance_facts(base_url: str, slots: tuple[TokenSlot, ...], tokens: list[TokenInfo], *,
                   transport: Transport, timeout: float, check_updates: bool,
                   first_error: Optional[CoolifyError],
                   release_feed: Optional[Callable[[], Any]] = None) -> InstanceFacts:
    if first_error is not None and first_error.kind is ErrorKind.UNREACHABLE:
        return InstanceFacts(reachable=False, unreachable_reason=first_error.message)
    facts = InstanceFacts(reachable=True)
    by_role = {t.role: t for t in tokens}
    clients = [CoolifyClient(base_url, s.token, timeout=timeout, transport=transport)
               for s in slots if by_role[s.role].ok]

    for client in clients:
        try:
            facts.coolify_version = str(client.get("/version")).strip().strip('"')
            break
        except CoolifyError:
            continue

    if check_updates:
        try:
            feed = release_feed() if release_feed is not None else \
                fetch_public_json(RELEASES_URL, timeout=timeout, transport=transport)
            facts.latest_version = str(feed["stable"]["coolify"]["version"])
            facts.update_check = "on"
            if facts.coolify_version:
                facts.update_available = _version_key(facts.latest_version) > _version_key(facts.coolify_version)
        except (CoolifyError, KeyError, TypeError):
            facts.update_check = "failed"

    # MCP is one switch per instance; ask with the token that answers reads.
    reader = route_reads(tokens)
    if reader is not None:
        slot = next(s for s in slots if s.role is reader.role)
        client = CoolifyClient(base_url, slot.token, timeout=timeout, transport=transport)
        try:
            result = client.mcp_call("initialize", {
                "protocolVersion": "2025-11-25", "capabilities": {},
                "clientInfo": {"name": "hermes-coolifybruecke", "version": "0"}})
            info = (result or {}).get("serverInfo") if isinstance(result, dict) else None
            facts.mcp_enabled = bool(info)
            facts.mcp_detail = f"{info.get('name', '')} {info.get('version', '')}".strip() if info else None
        except CoolifyError as error:
            facts.mcp_enabled = None if error.kind is ErrorKind.UNREACHABLE else False
            facts.mcp_detail = str(error)

    servers_from = route_servers(tokens)
    if servers_from is not None:
        facts.metrics_enabled_by_server = {s.uuid: bool(s.metrics_enabled) for s in servers_from.servers}
    return facts


# --- routing -----------------------------------------------------------------------------------

def _view_width(token: TokenInfo) -> tuple:
    sees = token.sees
    return (token.scoped is False, len(sees.projects), sees.applications or 0,
            token.role is not Role.ACTION)


def route_reads(tokens: list[TokenInfo], ability: Ability = Ability.READ) -> Optional[TokenInfo]:
    """Reads go to the token with the widest view that holds the right."""
    holders = [t for t in tokens if t.holds(ability)]
    return max(holders, key=_view_width) if holders else None


def route_servers(tokens: list[TokenInfo]) -> Optional[TokenInfo]:
    holders = [t for t in tokens if t.holds(Ability.READ) and t.scoped is False]
    return max(holders, key=_view_width) if holders else None


def route_actions(tokens: list[TokenInfo]) -> Optional[TokenInfo]:
    """Actions go only to the API token (or the only token), never to the read-only token."""
    return next((t for t in tokens if t.role in (Role.ACTION, Role.SINGLE) and t.ok), None)


def routing_table(tokens: list[TokenInfo]) -> dict[str, Optional[str]]:
    def role(token: Optional[TokenInfo]) -> Optional[str]:
        return token.role.value if token is not None else None

    action = route_actions(tokens)
    return {
        "reads": role(route_reads(tokens)),
        "servers": role(route_servers(tokens)),
        "logs": role(route_reads(tokens, Ability.READ_SENSITIVE)),
        "actions": role(action) if action is not None and action.holds(Ability.DEPLOY) else None,
    }


# --- evaluation --------------------------------------------------------------------------------

def evaluate(rule: FeatureRule, tokens: list[TokenInfo], facts: InstanceFacts) -> Feature:
    def missing(reason: str, unlock: str) -> Feature:
        return Feature(rule.id, rule.label, False, reason, unlock)

    # Rule texts name the token the user would change: "the API token" or "the token".
    action = route_actions(tokens) or next((t for t in tokens if t.role is not Role.OVERVIEW), None)
    words = {"action_token": "API token" if action is None or action.role is Role.SINGLE else action.role.word,
             "visible_projects": max((len(t.sees.projects) for t in tokens if t.ok), default=0)}

    if not facts.reachable:
        return missing(f"Coolify could not be reached ({facts.unreachable_reason})",
                       f"check {URL_SETTING}, that the instance is up, Coolify's allowed IPs "
                       "(Settings → Advanced → API), and any proxy or firewall in front of Coolify")
    needs = rule.needs
    in_lane = [t for t in tokens if t.ok and (needs.lane is Lane.READ or t.role is not Role.OVERVIEW)]
    if not in_lane:
        rejected = [t for t in tokens if t.status is TokenStatus.UNAUTHORIZED
                    and (needs.lane is Lane.READ or t.role is not Role.OVERVIEW)]
        if rejected:
            return missing(f"The {rejected[0].role.word} was rejected (401 Unauthenticated)",
                           f"store a valid token in {rejected[0].setting}; Coolify shows a token "
                           "only once, so create a new one if it was lost")
        return missing(rule.reason_if_missing.format(**words), rule.unlock_hint.format(**words))

    holders = [t for t in in_lane if all(t.holds(a) for a in needs.abilities)]
    if not holders:
        reason = rule.reason_if_missing.format(**words)
        if needs.lane is Lane.ACTION and any(t.role is Role.OVERVIEW and all(t.holds(a) for a in needs.abilities)
                                             for t in tokens):
            reason += "; the read-only token holds it, but actions never use the read-only token"
        return missing(reason, rule.unlock_hint.format(**words))
    if needs.unscoped:
        holders = [t for t in holders if t.scoped is False]
        if not holders:
            reason = rule.reason_if_scoped.format(**words)
            rejected = [t for t in tokens if t.status is TokenStatus.UNAUTHORIZED]
            if rejected:
                reason += f"; the {rejected[0].role.word} was rejected (401 Unauthenticated)"
            return missing(reason, rule.unlock_if_scoped.format(**words))
    if needs.mcp and facts.mcp_enabled is not True:
        return missing("Coolify's MCP server is off or did not answer",
                       "in Coolify: Settings → Advanced → API and MCP, switch MCP on")
    if needs.metrics and not any(facts.metrics_enabled_by_server.values()):
        return missing("Sentinel metrics are off on every server",
                       "in Coolify: open the server → Sentinel, enable Metrics")
    if needs.update_check:
        if facts.update_check == "off":
            return missing("The update check is switched off (setting check_updates)",
                           "set check_updates to true in the plugin settings")
        if facts.update_check == "failed" or not facts.coolify_version:
            return missing("Coolify's release feed did not answer", "try again later")
    via = max(holders, key=_view_width) if needs.lane is Lane.READ else holders[0]
    return Feature(rule.id, rule.label, True, via=via.role)


def setup_warnings(tokens: list[TokenInfo], facts: InstanceFacts, notes: tuple[str, ...]) -> list[str]:
    warnings = list(notes)
    for token in tokens:
        if token.status is TokenStatus.UNAUTHORIZED:
            warnings.append(f"The {token.label} was rejected by Coolify (401). Store a valid token.")
        if not token.ok:
            continue
        held = {a for a, v in token.abilities.items() if v}
        if token.role is not Role.OVERVIEW and Ability.DEPLOY in held and token.scoped is False:
            # The setup guide recommends exactly this token; the warning says what guards it instead
            # (protected environments).
            warnings.append(f"The {token.role.word} can deploy, restart and stop every resource of "
                            "the team, production included. The agent stays off the protected "
                            "environments unless you allow it in the settings.")
        if held & {Ability.WRITE, Ability.DELETE}:
            rights = " and ".join(a.value for a in (Ability.WRITE, Ability.DELETE) if a in held)
            warnings.append(f"The {token.role.word} holds {rights}: it can change or delete "
                            "resources. The cockpit does not use these rights, but anything that holds "
                            "the token can.")
        if Ability.READ_SENSITIVE in held:
            where = "for the whole team" if token.scoped is False else "inside its scope"
            warnings.append(f"The {token.role.word} holds read:sensitive: logs and environment "
                            f"values are readable {where}, so they can reach the model (the cockpit "
                            "redacts known secret patterns; still disclosed).")
        if token.role is Role.OVERVIEW and held - {Ability.READ}:
            extra = ", ".join(a.value for a in Ability if a in held and a is not Ability.READ)
            warnings.append(f"The read-only token also holds {extra}. The cockpit uses it only for reads; "
                            "a read-only token is enough.")
        if any(v is None for v in token.abilities.values()):
            warnings.append(f"Some rights of the {token.role.word} could not be determined "
                            f"({'; '.join(token.notes)}).")
    if facts.coolify_version and _version_key(facts.coolify_version) < MIN_SCOPED_VERSION:
        warnings.append(f"Coolify {facts.coolify_version} is older than 4.6.0; scoped tokens and some "
                        "checks need 4.6.0 or newer.")
    if facts.update_check == "failed":
        warnings.append("The update check could not reach Coolify's release feed.")
    return warnings


def _now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0).isoformat()


def build_capabilities(connection: Connection, *, check_updates: bool = True,
                       transport: Transport = urllib_transport, timeout: float = 15.0,
                       release_feed: Optional[Callable[[], Any]] = None) -> Capabilities:
    """Probe every token of the setup and evaluate every FeatureRule against the result.
    `release_feed` answers like Coolify's release feed (`cached_release_feed` reuses a recent read);
    without it the feed is read once per call.

    Raises `CoolifyError(rate_limited)` when any request of the check got a 429: the reads it
    swallows (counts, project details) would otherwise turn into wrong facts."""
    watch = LimitWatch(transport)
    transport = watch
    with ThreadPoolExecutor(max_workers=max(1, len(connection.slots))) as pool:
        tokens = list(pool.map(lambda s: inspect_token(connection.base_url, s, transport, timeout),
                               connection.slots))
    watch.raise_if_limited()  # before instance_facts spends more of the budget that ran out
    first_error = None
    if all(t.status is TokenStatus.UNREACHABLE for t in tokens):
        first_error = CoolifyError(ErrorKind.UNREACHABLE, tokens[0].error or "no answer")
    facts = instance_facts(connection.base_url, connection.slots, tokens, transport=transport,
                           timeout=timeout, check_updates=check_updates, first_error=first_error,
                           release_feed=release_feed)
    watch.raise_if_limited()
    features = [evaluate(rule, tokens, facts) for rule in FEATURE_RULES]
    warnings = setup_warnings(tokens, facts, connection.notes) if facts.reachable else list(connection.notes)
    return Capabilities(connection.base_url, facts, tokens, features, routing_table(tokens), warnings, _now())


# --- model-facing rendering --------------------------------------------------------------------

MAX_PROJECTS_SHOWN = 12
MAX_OUTPUT_CHARS = 9000


def _token_summary(token: TokenInfo, show_projects: bool) -> dict[str, Any]:
    out: dict[str, Any] = {"role": token.role.value, "setting": token.setting,
                           "fingerprint": token.fingerprint, "status": token.status.value}
    if not token.ok:
        out["error"] = token.error
        return out
    out["scope"] = {True: "scoped", False: "team-wide", None: "unknown"}[token.scoped]
    out["rights"] = [a.value for a, v in token.abilities.items() if v is True]
    out["lacks"] = [a.value for a, v in token.abilities.items() if v is False]
    if any(v is None for v in token.abilities.values()):
        out["unknown"] = [a.value for a, v in token.abilities.items() if v is None]
    if token.holds(Ability.READ):
        sees = token.sees
        env_names: dict[str, int] = {}
        for project in sees.projects:
            for env in project.environments:
                env_names[env.name] = env_names.get(env.name, 0) + 1
        out["sees"] = {
            "projects": len(sees.projects), "environments_by_name": env_names,
            "applications": sees.applications, "services": sees.services, "databases": sees.databases,
            "servers": sees.servers if sees.servers is not None else
            ("refused: scoped tokens cannot read servers" if token.scoped else "not readable"),
        }
        if show_projects:
            shown = [f"{p.name} [{', '.join(e.name for e in p.environments)}]"
                     for p in sees.projects[:MAX_PROJECTS_SHOWN]]
            hidden = len(sees.projects) - len(shown)
            if hidden > 0 or sees.projects_truncated:
                shown.append(f"... {hidden} more" if hidden > 0 else "... list truncated")
            out["projects"] = shown
    if token.notes:
        out["notes"] = token.notes
    return out


def to_model_dict(caps: Capabilities, *, show_projects: bool = True) -> dict[str, Any]:
    facts = caps.instance
    instance: dict[str, Any] = {"url": caps.base_url, "reachable": facts.reachable}
    if facts.reachable:
        metrics = facts.metrics_enabled_by_server
        instance.update({
            "coolify_version": facts.coolify_version, "latest_version": facts.latest_version,
            "update_available": facts.update_available, "update_check": facts.update_check,
            "mcp_enabled": facts.mcp_enabled,
            "metrics_enabled_servers": f"{sum(metrics.values())}/{len(metrics)}" if metrics else None,
        })
    else:
        instance["error"] = facts.unreachable_reason
    tokens = [_token_summary(t, show_projects) for t in caps.tokens]
    return {
        "ok": True,
        "setup": result_line({"instance": instance, "tokens": tokens}),
        "instance": instance,
        "tokens": tokens,
        "routing": caps.routing,
        "available": [f"{f.id}: {f.label}" + (f" (via the {f.via.word})" if f.via else "")
                      for f in caps.available],
        "not_available": [{"feature": f"{f.id}: {f.label}", "reason": f.reason, "unlock": f.unlock}
                          for f in caps.not_available],
        "warnings": caps.warnings,
        "checked_at": caps.checked_at,
    }


def render_for_model(caps: Capabilities) -> dict[str, Any]:
    """The tool result: compact, and under MAX_OUTPUT_CHARS once serialized."""
    import json

    out = to_model_dict(caps)
    out["update_notice"] = update_notice(version_status_from(caps.instance), caps.base_url)
    if len(json.dumps(out)) > MAX_OUTPUT_CHARS:
        out = {**to_model_dict(caps, show_projects=False), "update_notice": out["update_notice"]}
    return out


# --- the update notice, shared by coolify_capabilities and coolify_overview -----------------
#
# One line for the model: the running version and, when Coolify publishes a newer one, that an update
# exists with the instance link (the upgrade itself stays a click in Coolify; the plugin never upgrades). The overview keeps the last answer in `version-check.json` in the plugin data dir: the
# instance's version at most every VERSION_TTL_S, Coolify's release feed at most every FEED_TTL_S.

VERSION_TTL_S = 600.0
FEED_TTL_S = 6 * 3600.0
VERSION_FILE = "version-check.json"


def version_status_from(facts: InstanceFacts) -> dict[str, Any]:
    return {"coolify_version": facts.coolify_version, "latest_version": facts.latest_version,
            "update_available": facts.update_available, "update_check": facts.update_check}


def update_notice(status: Optional[Mapping[str, Any]], base_url: str) -> Optional[str]:
    """One line about the running version and an available update, or None when nothing is known."""
    status = status or {}
    current, latest = status.get("coolify_version"), status.get("latest_version")
    if not current:
        return None
    if status.get("update_available") is True and latest:
        return (f"Update available: Coolify {latest} is out; this instance runs {current}. Update from "
                f"Coolify itself when it suits you ({base_url}).")
    if status.get("update_check") == "off":
        return f"Coolify {current} (the update check is switched off: setting check_updates)."
    if status.get("update_check") == "failed" or not latest:
        return f"Coolify {current} (Coolify's release feed did not answer, so updates are unknown)."
    return f"Coolify {current}, the latest release."


def _read_version_file(path: Optional[Any], key: str) -> dict[str, Any]:
    if path is None:
        return {}
    import json
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) and data.get("key") == key else {}


def _write_version_file(path: Optional[Any], data: dict[str, Any]) -> None:
    if path is None:
        return
    import json
    import os
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(data), encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        try:
            tmp.unlink()
        except OSError:
            pass


def remember_version(path: Optional[Any], base_url: str, status: Mapping[str, Any], *,
                     now: Optional[float] = None) -> None:
    """Store what a capabilities check learned, so the overview reuses it."""
    import time
    stamp = time.time() if now is None else now
    data = {"key": base_url, **{k: status.get(k) for k in ("coolify_version", "latest_version",
                                                         "update_available", "update_check")},
            "version_epoch": stamp}
    if status.get("update_check") == "on":
        # A feed answer that came from this file (cached_release_feed) keeps its own read time.
        before = _read_version_file(path, base_url)
        kept = float(before.get("feed_epoch") or 0)
        fresh = before.get("latest_version") == status.get("latest_version") and 0 <= stamp - kept <= FEED_TTL_S
        data["feed_epoch"] = kept if fresh else stamp
    _write_version_file(path, data)


def cached_release_feed(path: Optional[Any], base_url: str, *, transport: Transport = urllib_transport,
                        timeout: float = 10.0, now: Optional[float] = None) -> Callable[[], Any]:
    """A `release_feed` for build_capabilities: the latest version from `version-check.json` while
    its feed read is younger than FEED_TTL_S, otherwise one GET to the feed. The overview, the tool
    and the page's checks then share one feed read per FEED_TTL_S."""
    def feed() -> Any:
        import time
        stamp = time.time() if now is None else now
        data = _read_version_file(path, base_url)
        if data.get("latest_version") and 0 <= stamp - float(data.get("feed_epoch") or 0) <= FEED_TTL_S:
            return {"stable": {"coolify": {"version": data["latest_version"]}}}
        return fetch_public_json(RELEASES_URL, timeout=timeout, transport=transport)

    return feed


def cached_version_status(client: CoolifyClient, *, check_updates: bool, path: Optional[Any] = None,
                          transport: Transport = urllib_transport, timeout: float = 10.0,
                          now: Optional[float] = None) -> dict[str, Any]:
    """The instance version and update state for the overview, read again only when stale."""
    import time
    stamp = time.time() if now is None else now
    data = _read_version_file(path, client.base_url) or {"key": client.base_url}
    if not data.get("coolify_version") or not 0 <= stamp - float(data.get("version_epoch") or 0) <= VERSION_TTL_S:
        try:
            data["coolify_version"] = str(client.get("/version")).strip().strip('"')
            data["version_epoch"] = stamp
        except CoolifyError:
            pass
    if not check_updates:
        data["update_check"], data["latest_version"], data["update_available"] = "off", None, None
    else:
        if not data.get("latest_version") or not 0 <= stamp - float(data.get("feed_epoch") or 0) <= FEED_TTL_S:
            try:
                feed = fetch_public_json(RELEASES_URL, timeout=timeout, transport=transport)
                data["latest_version"] = str(feed["stable"]["coolify"]["version"])
                data["feed_epoch"], data["update_check"] = stamp, "on"
            except (CoolifyError, KeyError, TypeError):
                data["update_check"] = "failed" if not data.get("latest_version") else data.get("update_check", "on")
        if data.get("latest_version") and data.get("coolify_version"):
            data["update_check"] = data.get("update_check") or "on"
            data["update_available"] = _version_key(data["latest_version"]) > _version_key(data["coolify_version"])
    _write_version_file(path, data)
    return {k: data.get(k) for k in ("coolify_version", "latest_version", "update_available", "update_check")}


# --- the setup's result line ------------------------------------------------------------
#
# After a token is saved, one line replaces the setup explanation (the result of the token check):
#   "Team-wide · read, deploy · 28 apps · 2 servers"
#   "Limited to 5 projects · read, deploy · no servers"   + the button "Add a read-only token"
# A token holding write or delete adds one amber chip "can change and delete"; the cockpit never
# calls those rights. Built from the model dict `to_model_dict` returns, so the page, the settings
# page and the tests read one function.

WARN_CHIP = "can change and delete"
SHOWN_RIGHTS = ("read", "deploy", "read:sensitive")


def _plural(n: Any, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


def result_line(model: Mapping[str, Any]) -> dict[str, Any]:
    tokens = list(model.get("tokens") or [])
    main = next((t for t in tokens if t.get("role") in (Role.ACTION.value, Role.SINGLE.value)), None)
    read = next((t for t in tokens if t.get("role") == Role.OVERVIEW.value), None)
    instance = model.get("instance") or {}
    out: dict[str, Any] = {"line": "", "tone": "good", "limited": False, "add_read_token": False,
                           "rights": [], "chip": None, "chip_detail": None}
    if not instance.get("reachable", True):
        return {**out, "line": "Coolify could not be reached from here", "tone": "bad"}
    if main is None:
        main = read
    if main is None:
        return {**out, "line": "No token set", "tone": "bad"}
    if main.get("status") != "ok":
        word = "Rejected by Coolify (401)" if main.get("status") == "unauthorized" else "Token check failed"
        return {**out, "line": f"{word} · check the token", "tone": "bad"}
    rights = [r for r in SHOWN_RIGHTS if r in (main.get("rights") or [])]
    sees = main.get("sees") or {}
    limited = main.get("scope") == "scoped"
    if limited:
        head = f"Limited to {_plural(sees.get('projects') or 0, 'project')}"
        servers = "no servers"
    else:
        head = "Team-wide" if main.get("scope") == "team-wide" else "Scope unknown"
        servers = _plural(sees["servers"], "server") if isinstance(sees.get("servers"), int) else "no servers"
    parts = [head, ", ".join(rights) or "no rights", _plural(sees.get("applications") or 0, "app"), servers]
    if limited and read is not None and read.get("status") == "ok" and read.get("scope") == "team-wide":
        parts[-1] = "read-only token adds servers"
    out.update(line=" · ".join(parts), limited=limited, rights=rights,
               add_read_token=limited and read is None,
               tone="warn" if "deploy" not in rights or limited else "good")
    held = set(main.get("rights") or [])
    if held & {"write", "delete"}:
        both = " and ".join(r for r in ("write", "delete") if r in held)
        out["chip"] = WARN_CHIP
        out["chip_detail"] = (f"This token holds {both}. The cockpit never uses them, but anything that holds the "
                              "token can.")
    return out
