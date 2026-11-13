"""Server metrics per range and the disk estimate.

The snapshot keeps its 1 h series (one MCP call per server, reused for a minute). The cards' other
ranges come from `GET /servers/metrics?range=6h|24h|7d`, read once per server and range and kept
for RANGE_TTL_S in `server-metrics.json` in the plugin data dir, so the page's minute poll never
grows. Coolify serves 5 minute buckets up to 24 h and 30 minute buckets for 7 d (measured:
`feedback-0.0.11/probe_metrics_range.txt`). MCP calls sit outside Coolify's rate limiter.

The disk estimate ("full in about N d") is a least-squares slope over the 7 d disk series, read on
the slow cadence (the 7 d entry, 30 minutes) and shown only when the server would fill within
FORECAST_MAX_DAYS. It is an estimate; the label says "about".
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any, Optional

RANGES: dict[str, int] = {"1h": 60, "6h": 360, "24h": 1440, "7d": 10080}
RANGE_TTL_S: dict[str, float] = {"1h": 60.0, "6h": 300.0, "24h": 600.0, "7d": 1800.0}
FORECAST_MAX_DAYS = 30.0
FORECAST_MIN_POINTS = 12
METRICS_FILE = "server-metrics.json"
_LOCK = threading.Lock()


def disk_forecast(series: Any, current_percent: Any = None) -> Optional[dict]:
    """{"days": float, "slope_per_day": float} when the disk fills within FORECAST_MAX_DAYS, else None.

    `series` is Coolify's [[epoch_ms, percent], ...]. A flat or falling disk, too few points or a
    span under a day give None: no number the user has to interpret."""
    points = [(float(p[0]) / 1000.0, float(p[1])) for p in (series or [])
              if isinstance(p, (list, tuple)) and len(p) == 2 and isinstance(p[1], (int, float))]
    if len(points) < FORECAST_MIN_POINTS or points[-1][0] - points[0][0] < 86400:
        return None
    n = len(points)
    mean_t = sum(t for t, _ in points) / n
    mean_v = sum(v for _, v in points) / n
    var = sum((t - mean_t) ** 2 for t, _ in points)
    if var <= 0:
        return None
    slope_s = sum((t - mean_t) * (v - mean_v) for t, v in points) / var
    slope_day = slope_s * 86400
    now_v = float(current_percent) if isinstance(current_percent, (int, float)) else points[-1][1]
    if slope_day <= 0.01 or now_v >= 100:
        return None
    days = (100.0 - now_v) / slope_day
    if days > FORECAST_MAX_DAYS:
        return None
    return {"days": round(days, 1), "slope_per_day": round(slope_day, 3)}


def _load(path: Optional[Path]) -> dict:
    if path is None or not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _save(path: Optional[Path], data: dict) -> None:
    if path is None:
        return
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data, separators=(",", ":")), encoding="utf-8")
    os.replace(tmp, path)


def read_range(read: Any, server_uuid: str, range_id: str, *, path: Optional[Path], key: str,
               now: Optional[float] = None) -> dict:
    """One server's metrics for one range: from the file while younger than its TTL, else one read.

    `read(server_uuid, minutes)` returns (metrics dict, None) or (None, why not), the snapshot's
    `_metrics` shape. `key` ties the file to the instance and token (the snapshot cache key)."""
    if range_id not in RANGES:
        raise ValueError(f"range must be one of {', '.join(RANGES)}")
    now = time.time() if now is None else now
    slot = f"{server_uuid}:{range_id}"
    with _LOCK:
        stored = _load(path)
        if stored.get("key") != key:
            stored = {"key": key, "entries": {}}
        entry = stored.get("entries", {}).get(slot)
    if isinstance(entry, dict) and -1.0 <= now - float(entry.get("fetched_epoch") or 0) <= RANGE_TTL_S[range_id]:
        return {**entry, "cache": "hit"}
    metrics, why = read(server_uuid, RANGES[range_id])
    if metrics is None:
        return {"error": why or "no metrics", "range": range_id}
    entry = {"range": range_id, "minutes": metrics.get("minutes"), "bucket_seconds": metrics.get("bucket_seconds"),
             "series": metrics.get("series") or {}, "current": metrics.get("current") or {},
             "fetched_epoch": round(now, 1)}
    with _LOCK:
        stored = _load(path)
        if stored.get("key") != key:
            stored = {"key": key, "entries": {}}
        entries = stored.setdefault("entries", {})
        entries[slot] = entry
        # keep the file small: drop entries of servers or ranges older than a day
        for name in [k for k, v in entries.items() if now - float((v or {}).get("fetched_epoch") or 0) > 86400]:
            entries.pop(name, None)
        _save(path, stored)
    return {**entry, "cache": "miss"}


def range_payload(read: Any, servers: list[dict], range_id: str, *, path: Optional[Path], key: str,
                  now: Optional[float] = None) -> dict:
    """What the cards render for a range: per server its series, plus the disk estimate from 7 d."""
    out: dict[str, Any] = {"range": range_id, "minutes": RANGES[range_id], "servers": {}}
    for server in servers:
        uuid = server.get("uuid")
        if not uuid or server.get("metrics_enabled") is False:
            continue
        got = read_range(read, uuid, range_id, path=path, key=key, now=now)
        week = got if range_id == "7d" else read_range(read, uuid, "7d", path=path, key=key, now=now)
        current = ((server.get("metrics") or {}).get("current") or {}).get("disk_used_percent")
        forecast = disk_forecast(((week.get("series") or {}).get("disk_used_percent")), current) \
            if "error" not in week else None
        out["servers"][uuid] = {"series": got.get("series") or {}, "bucket_seconds": got.get("bucket_seconds"),
                                "error": got.get("error"), "cache": got.get("cache"), "disk_full": forecast}
    return out
