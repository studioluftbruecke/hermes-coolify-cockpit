"""The `env_changed` signal's reads: has an env variable changed since an application's last
finished deployment? (Measured on a 26-application instance: 1 flagged.)

One `GET /applications/{uuid}/envs` per application tells it: every env row carries `updated_at`,
and a read token sees no values (measured). Two signals that do not work were measured too and
are not used: `config_hash` against the deployment's `configuration_hash` (0 of 26 differ) and the
application's own `updated_at` (26 of 26 newer; it moves on status checks).

Cost and cadence: one call per application, read again after ENV_TTL_S or as soon as the
application has a newer finished deployment, at most ENV_READS per round, oldest first. These
reads use their own client outside the snapshot's rate watch, so a 429 here never blanks the
dashboard: the round stops and the last known answers stay. The answers live in `env-check.json`
in the plugin data dir, shared by the page's routes and the agent's tools. Names and timestamps
only, never a value.
"""

from __future__ import annotations

import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Mapping, Optional

try:
    from .coolify_client import CoolifyClient, CoolifyError, ErrorKind
    from .fleet import parse_stamp
except ImportError:  # loaded as a top-level module (tests, scripts)
    from coolify_client import CoolifyClient, CoolifyError, ErrorKind  # type: ignore
    from fleet import parse_stamp  # type: ignore

ENV_FILE = "env-check.json"
ENV_TTL_S = 600.0
ENV_READS = 6
_LOCK = threading.Lock()


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


def changed_after(envs: Any, finished_at: Optional[str]) -> Optional[str]:
    """The newest env row's `updated_at` when it is later than `finished_at`, else None."""
    done = parse_stamp(finished_at)
    if done is None or not isinstance(envs, list):
        return None
    newest = None
    for row in envs:
        stamp = parse_stamp(row.get("updated_at")) if isinstance(row, dict) else None
        if stamp is not None and stamp > done and (newest is None or stamp > newest[0]):
            newest = (stamp, str(row.get("updated_at")))
    return newest[1] if newest else None


def due(fleet: Mapping[str, Mapping], known: Mapping[str, dict], now: float) -> list[str]:
    """Applications to read this round: no answer yet, an answer older than ENV_TTL_S, or a newer
    finished deployment since the answer. Only apps with a known last good deployment count."""
    out = []
    for uuid, row in fleet.items():
        good = (row or {}).get("last_good") or {}
        if not good.get("finished_at") or (row or {}).get("state") == "deploying":
            continue
        entry = known.get(uuid)
        if entry is None or entry.get("good") != good.get("uuid") or not -1.0 <= now - float(entry.get("checked") or 0) <= ENV_TTL_S:
            out.append(uuid)
    return sorted(out, key=lambda u: float((known.get(u) or {}).get("checked") or 0))


def refresh(client: Optional[CoolifyClient], fleet: Mapping[str, Mapping], *, path: Optional[Path], key: str,
            now: Optional[float] = None, reads: int = ENV_READS) -> dict[str, str]:
    """Read the due applications (at most `reads`) and return {app uuid: changed_at} for every
    application whose stored answer says its env changed after its last good deployment."""
    now = time.time() if now is None else now
    with _LOCK:
        known = _load(path, key)
    todo = due(fleet, known, now)[:reads] if client is not None else []
    if todo:
        stop = threading.Event()

        def one(uuid: str) -> tuple[str, Optional[dict]]:
            if stop.is_set():
                return uuid, None
            good = (fleet.get(uuid) or {}).get("last_good") or {}
            try:
                assert client is not None
                envs = client.get(f"/applications/{uuid}/envs")
            except CoolifyError as error:
                if error.kind is ErrorKind.RATE_LIMITED:
                    stop.set()
                return uuid, None
            return uuid, {"checked": round(now, 1), "good": good.get("uuid"),
                          "changed_at": changed_after(envs, good.get("finished_at"))}

        # The first read goes alone: a 429 there ends the round before the others are sent.
        results = [one(todo[0])]
        if len(todo) > 1 and not stop.is_set():
            with ThreadPoolExecutor(max_workers=min(6, len(todo) - 1)) as pool:
                results += list(pool.map(one, todo[1:]))
        fresh = {u: e for u, e in results if e is not None}
        if fresh:
            with _LOCK:
                merged = {**_load(path, key), **fresh}
                merged = {u: e for u, e in merged.items() if u in fleet}
                _save(path, key, merged)
            known = merged
    out = {}
    for uuid, entry in known.items():
        good = ((fleet.get(uuid) or {}).get("last_good") or {}).get("uuid")
        if entry.get("changed_at") and entry.get("good") == good:
            out[uuid] = str(entry["changed_at"])
    return out
