"""The `behind_branch` signal's reads: is an application's branch ahead of what it runs?
(PLAN-v0.1 -> A10, probe M0c.)

Coolify does not know the branch head: every application stores `git_commit_sha: HEAD`. The
deployed commit is known (the last finished deployment's `commit`). The head comes from the git
host, through the first source that can answer, in this order:

    1. forgebruecke  Forgebrücke's `forgejo_get kind=branch`, when it is enabled in the same
                     profile and the repository sits on its Forgejo host. Only a process that
                     holds the plugin `ctx` can call it (M0c: the tick command and the agent's
                     tools; a page route cannot), so routes leave these apps to them.
    2. git_token     The optional "Git host token" against its own host's API (Forgejo/Gitea or
                     GitHub). The only source that also counts the commits between the two.
    3. public        git's smart HTTP (`info/refs?service=git-upload-pack`) without a credential:
                     answers a public repository, refuses a private one (M0c: 200 and 401).

A token is only ever sent to the host it belongs to: Forgebrücke uses its own, and the git host
token goes to `git_host` (or github.com for a GitHub token when that setting is empty). The answers
live in `branch-heads.json` in the plugin data dir, keyed like the env check, shared by the
routes, the agent's tools and the tick. Repo names, branch names and shas only.

    BranchHead { head, commits?, source, checked, repo, branch, deployed }
"""

from __future__ import annotations

import contextvars
import json
import os
import re
import threading
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Optional

BRANCH_FILE = "branch-heads.json"
BRANCH_TTL_S = 600.0          # a head is read again after 10 minutes
CLOSED_TTL_S = 6 * 3600.0     # a repo no source could read is asked again after 6 hours
BRANCH_READS = 8              # at most this many reads per round, oldest first
TIMEOUT_S = 8.0
_LOCK = threading.Lock()

Transport = Callable[[str, str, Mapping[str, str], Optional[bytes], float], Any]
Dispatch = Callable[[str, dict], Any]

_SCP = re.compile(r"^(?:[\w.-]+@)?(?P<host>[\w.-]+):(?P<path>[^/].*)$")
_BARE = re.compile(r"^[\w.-]+/[\w.-]+$")
_SHA = re.compile(r"^[0-9a-f]{40}$")
GITHUB_TOKEN_PREFIXES = ("ghp_", "github_pat_", "gho_", "ghs_", "ghu_")


@dataclass(frozen=True)
class Repo:
    host: str      # lowercase, no port
    path: str      # owner/repo, no .git

    @property
    def https(self) -> str:
        return f"https://{self.host}/{self.path}.git"


def parse_repo(raw: Any) -> Optional[Repo]:
    """Coolify's `git_repository` in its four measured forms: `git@host:owner/repo.git`,
    `ssh://git@host:port/owner/repo.git`, `https://host/owner/repo(.git)`, and a bare
    `owner/repo` (Coolify's form for a GitHub source)."""
    text = str(raw or "").strip()
    if not text:
        return None
    host, path = "", ""
    if "://" in text:
        parsed = urllib.parse.urlparse(text)
        host, path = parsed.hostname or "", parsed.path
    elif _BARE.match(text):
        host, path = "github.com", text
    else:
        m = _SCP.match(text)
        if m:
            host, path = m.group("host"), m.group("path")
            first, _, rest = path.partition("/")
            if first.isdigit() and rest:  # `git@host:2222/owner/repo.git`: Coolify keeps the ssh port there
                path = rest
    path = path.strip("/")
    if path.endswith(".git"):
        path = path[:-4]
    if not host or path.count("/") != 1 or not all(path.split("/")):
        return None
    return Repo(host.lower(), path)


def host_of(url: Any) -> Optional[str]:
    text = str(url or "").strip()
    if not text:
        return None
    parsed = urllib.parse.urlparse(text if "://" in text else f"https://{text}")
    return (parsed.hostname or "").lower() or None


def token_host(token: Optional[str], git_host: Any) -> Optional[str]:
    """Where the git host token may go: the `git_host` setting, else github.com for a token that
    looks like GitHub's, else nowhere."""
    if not token:
        return None
    host = host_of(git_host)
    if host:
        return host
    return "github.com" if token.startswith(GITHUB_TOKEN_PREFIXES) else None


@dataclass
class Sources:
    """What this process can ask. `dispatch` is set only where the plugin ctx exists."""
    forgebruecke_host: Optional[str] = None     # Forgebrücke's host when it is enabled here
    dispatch: Optional[Dispatch] = None
    git_token: Optional[str] = None
    git_host: Optional[str] = None              # token_host(...)
    transport: Optional[Transport] = None

    def plan(self, repo: Repo) -> Optional[str]:
        """The source this process uses for `repo`, or None to leave it (Forgebrücke's apps in a
        process without ctx: the tick or an agent turn reads them)."""
        if self.forgebruecke_host and repo.host == self.forgebruecke_host:
            return "forgebruecke" if self.dispatch is not None else None
        if self.git_token and self.git_host and repo.host == self.git_host:
            return "git_token"
        return "public"


# --- the three reads ----------------------------------------------------------------------------

class ReadError(Exception):
    def __init__(self, kind: str, message: str = ""):
        super().__init__(message or kind)
        self.kind = kind   # closed (401/403/404: private or gone) | failed (anything else)


def _http(transport: Transport, url: str, headers: Mapping[str, str]) -> tuple[int, bytes]:
    try:
        response = transport("GET", url, {"User-Agent": "hermes-coolifybruecke", **headers}, None, TIMEOUT_S)
    except OSError as error:
        raise ReadError("failed", f"no answer: {error}") from error
    return int(response.status), bytes(response.body or b"")


def _closed_or_failed(status: int) -> ReadError:
    return ReadError("closed" if status in (401, 403, 404) else "failed", f"HTTP {status}")


def head_from_refs(body: bytes, branch: str) -> Optional[str]:
    """The sha of `refs/heads/<branch>` in a smart-HTTP ref advertisement (pkt-lines)."""
    want = f"refs/heads/{branch}".encode()
    for line in body.split(b"\n"):
        line = line.split(b"\0", 1)[0]
        parts = line.strip().split(b" ")
        if len(parts) == 2 and parts[1] == want:
            sha = parts[0][-40:].decode("ascii", "replace")
            if _SHA.match(sha):
                return sha
    return None


def read_public(transport: Transport, repo: Repo, branch: str) -> dict:
    url = f"{repo.https}/info/refs?service=git-upload-pack"
    status, body = _http(transport, url, {})
    if status != 200:
        raise _closed_or_failed(status)
    sha = head_from_refs(body, branch)
    if sha is None:
        raise ReadError("closed", "branch not found")
    return {"head": sha}


def _api_base(host: str) -> str:
    return "https://api.github.com" if host == "github.com" else f"https://{host}/api/v1"


def read_with_token(transport: Transport, token: str, repo: Repo, branch: str, deployed: str) -> dict:
    """Branch head through the host's API, and the commit count from the deployed commit to it
    (GitHub `ahead_by`, Forgejo/Gitea `total_commits`) when the compare call answers."""
    base = _api_base(repo.host)
    auth = {"Authorization": f"token {token}", "Accept": "application/json"}
    quoted = urllib.parse.quote(branch, safe="")
    status, body = _http(transport, f"{base}/repos/{repo.path}/branches/{quoted}", auth)
    if status != 200:
        raise _closed_or_failed(status)
    try:
        commit = (json.loads(body.decode("utf-8", "replace")) or {}).get("commit") or {}
    except ValueError as error:
        raise ReadError("failed", "not JSON") from error
    sha = str(commit.get("id") or commit.get("sha") or "")
    if not _SHA.match(sha):
        raise ReadError("failed", "no sha in the branch answer")
    out: dict[str, Any] = {"head": sha}
    if _SHA.match(deployed or "") and deployed != sha:
        status, body = _http(transport, f"{base}/repos/{repo.path}/compare/{deployed}...{sha}", auth)
        if status == 200:
            try:
                data = json.loads(body.decode("utf-8", "replace")) or {}
            except ValueError:
                data = {}
            count = data.get("ahead_by", data.get("total_commits"))
            if isinstance(count, int) and count >= 0:
                out["commits"] = count
    elif deployed == sha:
        out["commits"] = 0
    return out


def read_forgebruecke(dispatch: Dispatch, repo: Repo, branch: str) -> dict:
    raw = dispatch("forgejo_get", {"kind": "branch", "repo": repo.path, "ref": branch})
    try:
        body = json.loads(raw) if isinstance(raw, (str, bytes)) else raw
    except ValueError as error:
        raise ReadError("failed", "Forgebrücke answered no JSON") from error
    if not isinstance(body, dict) or not body.get("ok"):
        error = str((body or {}).get("error") or "") if isinstance(body, dict) else ""
        raise ReadError("closed" if error in ("not_found", "forbidden", "no_access") else "failed",
                        f"Forgebrücke: {error or 'no answer'}")
    sha = str(((body.get("branch") or {}).get("sha")) or "")
    if not _SHA.match(sha):
        raise ReadError("failed", "Forgebrücke returned no sha")
    return {"head": sha}


# --- the cache and one round --------------------------------------------------------------------

def _load(path: Optional[Path], key: str) -> dict[str, dict]:
    if path is None:
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    apps = data.get("apps") if isinstance(data, dict) and data.get("key") == key else None
    return {str(k): v for k, v in (apps or {}).items() if isinstance(v, dict)}


def _save(path: Optional[Path], key: str, apps: Mapping[str, dict]) -> None:
    if path is None:
        return
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps({"key": key, "apps": dict(apps)}), encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        try:
            tmp.unlink()
        except OSError:
            pass


@dataclass(frozen=True)
class Target:
    uuid: str
    repo: Repo
    branch: str
    deployed: str     # full sha of the last finished deployment


def targets(resources: list[Mapping], fleet: Mapping[str, Mapping]) -> list[Target]:
    """Applications with a parseable repository, a branch and a deployed full sha, not deploying."""
    out = []
    for r in resources:
        uuid = str(r.get("uuid") or "")
        row = fleet.get(uuid) or {}
        if r.get("kind") != "application" or row.get("state") == "deploying":
            continue
        repo, branch = parse_repo(r.get("git_repository")), str(r.get("git_branch") or "").strip()
        deployed = str(((row.get("last_good") or {}).get("commit")) or "")
        if repo and branch and _SHA.match(deployed):
            out.append(Target(uuid, repo, branch, deployed))
    return out


def due(items: list[Target], known: Mapping[str, dict], now: float) -> list[Target]:
    """Targets to read this round: no answer, a stale one, or the deployed commit moved since."""
    out = []
    for t in items:
        entry = known.get(t.uuid)
        ttl = CLOSED_TTL_S if (entry or {}).get("closed") else BRANCH_TTL_S
        if (entry is None or entry.get("deployed") != t.deployed or entry.get("branch") != t.branch
                or not -1.0 <= now - float(entry.get("checked") or 0) <= ttl):
            out.append(t)
    return sorted(out, key=lambda t: float((known.get(t.uuid) or {}).get("checked") or 0))


def _read(sources: Sources, source: str, t: Target) -> dict:
    if source == "forgebruecke":
        assert sources.dispatch is not None
        return read_forgebruecke(sources.dispatch, t.repo, t.branch)
    if sources.transport is None:
        raise ReadError("failed", "no transport")
    if source == "git_token":
        assert sources.git_token is not None
        return read_with_token(sources.transport, sources.git_token, t.repo, t.branch, t.deployed)
    return read_public(sources.transport, t.repo, t.branch)


def _after_failure(previous: Optional[dict], base: dict) -> dict:
    """A read that got no verdict (a 5xx, a timeout): keep the last answer for the same deployed
    commit, and wait BRANCH_TTL_S before asking again (measured: a host answering 503 on every
    round would otherwise be asked on every page poll)."""
    if previous and previous.get("head") and previous.get("deployed") == base["deployed"]:
        return {**previous, "checked": base["checked"]}
    return {**base, "failed": True}


def refresh(sources: Optional[Sources], resources: list[Mapping], fleet: Mapping[str, Mapping], *,
            path: Optional[Path], key: str, now: Optional[float] = None, reads: int = BRANCH_READS) -> dict[str, dict]:
    """Read the due targets this process can ask (at most `reads`), store the answers, and return
    {app uuid: {head, commits?, source}} for every app whose stored answer is for the commit it runs
    now. Never raises for a read: a failed read keeps the last answer and waits BRANCH_TTL_S, a closed
    repo is asked again after CLOSED_TTL_S."""
    now = time.time() if now is None else now
    items = targets(resources, fleet)
    with _LOCK:
        known = _load(path, key)
    work = []
    if sources is not None:
        for t in due(items, known, now):
            source = sources.plan(t.repo)
            if source is not None:
                work.append((t, source))
        work = work[:reads]

    def one(job: tuple[Target, str]) -> tuple[str, dict]:
        t, source = job
        base = {"checked": round(now, 1), "repo": t.repo.path, "host": t.repo.host, "branch": t.branch,
                "deployed": t.deployed, "source": source}
        try:
            return t.uuid, {**base, **_read(sources, source, t)}  # type: ignore[arg-type]
        except ReadError as error:
            if error.kind == "closed":
                return t.uuid, {**base, "closed": True}
            return t.uuid, _after_failure(known.get(t.uuid), base)
        except Exception:  # a source's own bug must never break the snapshot
            return t.uuid, _after_failure(known.get(t.uuid), base)

    if work:
        # Each read runs in a copy of the caller's context: inside an agent turn Hermes keeps the
        # profile's secret scope in a ContextVar, and a bare worker thread would not see it, so
        # Forgebrücke's tool answered without its token (measured: 0 of 23 reads in a turn).
        contexts = [contextvars.copy_context() for _ in work]
        with ThreadPoolExecutor(max_workers=min(4, len(work))) as pool:
            fresh = dict(pool.map(lambda pair: pair[0].run(one, pair[1]), zip(contexts, work)))
        if fresh:
            with _LOCK:
                current = {u for u in (t.uuid for t in items)}
                merged = {**_load(path, key), **fresh}
                merged = {u: e for u, e in merged.items() if u in current or u in fleet}
                _save(path, key, merged)
            known = merged
    out = {}
    by_uuid = {t.uuid: t for t in items}
    for uuid, entry in known.items():
        t = by_uuid.get(uuid)
        if t is None or entry.get("closed") or not entry.get("head"):
            continue
        if entry.get("deployed") != t.deployed or entry.get("branch") != t.branch:
            continue
        info = {"head": str(entry["head"]), "source": entry.get("source")}
        if isinstance(entry.get("commits"), int):
            info["commits"] = entry["commits"]
        out[uuid] = info
    return out
