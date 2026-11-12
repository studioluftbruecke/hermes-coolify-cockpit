"""Alerts with Desktop closed (PLAN-v0.1 M6, T2, probe M0d).

A plugin cannot create a scheduled job or deliver a message from a thread. Hermes' cron can run a
script with no model turn and deliver its stdout as written; empty stdout delivers nothing. So the
alert path is:

    cron job (no agent, every N minutes)
      -> shim script `<home>/scripts/coolifybruecke-alerts.py` (written by `alerts on`)
        -> `hermes coolifybruecke tick`  (a Hermes CLI process: holds ctx, reads the secrets)
          -> the same Attention list the page and the agent read
          -> `diff()` against `alerts-state.json`: new items and recovered ones, printed once

    AlertState { key: base URL, open: {attention key: {signal, severity, title, since, first_seen, missed}},
                 pending: {attention key: {seen}}, checked }

Rules: an item is announced once, when it first appears. A "Recovered" line follows when it is
gone, except while its app is deploying or failing: a redeploy of a failing app is not a recovery
yet, and a branch that is ahead stays ahead while the deploy of it fails (the signal itself is
quiet then, so the page shows one problem per app). A run
with nothing new prints nothing. Coolify unreachable or a rejected token is an item of its own
(`coolify_unreachable`); a rate-limited run changes nothing and prints nothing.

The morning digest is an automation blueprint instead (one model turn per morning): the same list,
written by the agent from `coolify_overview`.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional

STATE_FILE = "alerts-state.json"
SCRIPT_NAME = "coolifybruecke-alerts.py"
OLD_SCRIPT_NAME = "coolify-cockpit-alerts.py"
JOB_NAME = "Coolifybrücke alerts"
DEFAULT_EVERY = "5m"
MARK = {"bad": "✗", "warn": "!", "info": "·"}
FIX_WORD = {"deploy": "Deploy", "rollback": "Roll back", "restart": "Restart"}
_LOCK = threading.Lock()

SHIM = '''#!/usr/bin/env python3
"""Coolifybrücke alerts: written by `hermes coolifybruecke alerts on`; run by a Hermes cron job
with no model turn. It asks the plugin for what is new since its last run and prints it; Hermes
delivers the output as written, and an empty output delivers nothing. Remove it with
`hermes coolifybruecke alerts off`."""
import os
import shutil
import subprocess
import sys

hermes = shutil.which("hermes") or "hermes"
run = subprocess.run([hermes, "coolifybruecke", "tick"], capture_output=True, text=True, timeout=240,
                     env=dict(os.environ))
sys.stdout.write(run.stdout)
if run.returncode != 0:
    tail = (run.stderr or "").strip().splitlines()[-3:]
    sys.stderr.write("coolifybruecke tick failed: " + " | ".join(tail) + "\\n")
    sys.exit(run.returncode)
'''


def load_state(path: Optional[Path], key: str) -> Optional[dict]:
    """The stored state for this instance (its base URL), or None before the first run."""
    if path is None:
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("key") != key or not isinstance(data.get("open"), dict):
        return None
    return data


def save_state(path: Optional[Path], state: Mapping[str, Any]) -> None:
    if path is None:
        return
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    with _LOCK:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(json.dumps(dict(state)), encoding="utf-8")
            os.replace(tmp, path)
        except OSError:
            try:
                tmp.unlink()
            except OSError:
                pass


def diff(items: Iterable[Mapping[str, Any]], state: Optional[Mapping[str, Any]], *, key: str,
         held: Iterable[str] = (), confirm: Optional[Mapping[str, int]] = None,
         now: Optional[float] = None) -> tuple[list[dict], list[dict], dict]:
    """(new items, recovered items, next state). `items` are Attention dicts carrying `key`;
    `held` names the app uuids deploying or failing now, whose other open items stay open.
    `confirm` gives a signal the number of runs in a row it must be seen before it is announced,
    and missed before it recovers (a status that flickers, measured: `running:unhealthy` on and off
    between two runs a few minutes apart); 1 when unset."""
    now = time.time() if now is None else now
    confirm = confirm or {}
    needs = lambda item: max(1, int(confirm.get(str(item.get("signal")), 1)))  # noqa: E731
    current = {str(a["key"]): dict(a) for a in items if a.get("key")}
    before: dict[str, dict] = dict((state or {}).get("open") or {})
    pending_before: dict[str, dict] = dict((state or {}).get("pending") or {})
    busy = set(held)
    new, recovered = [], []
    kept: dict[str, dict] = {}
    pending: dict[str, dict] = {}
    for k, a in current.items():
        if k in before:
            kept[k] = {**before[k], "missed": 0}
            continue
        seen = int((pending_before.get(k) or {}).get("seen") or 0) + 1
        if seen < needs(a):
            pending[k] = {"seen": seen, "signal": a.get("signal")}
            continue
        new.append(a)
        kept[k] = {"signal": a.get("signal"), "severity": a.get("severity"), "title": a.get("title"),
                   "since": a.get("since"), "resource_uuid": a.get("resource_uuid"),
                   "first_seen": round(now, 1), "missed": 0}
    for k, old in before.items():
        if k in current:
            continue
        if old.get("resource_uuid") and old["resource_uuid"] in busy:
            kept[k] = old  # still open: its app is deploying or failing, the outcome decides
            continue
        missed = int(old.get("missed") or 0) + 1
        if missed < needs(old):
            kept[k] = {**old, "missed": missed}
        else:
            recovered.append(old)
    return new, recovered, {"key": key, "open": kept, "pending": pending, "checked": round(now, 1)}


def render(new: list[Mapping[str, Any]], recovered: list[Mapping[str, Any]], *, host: str) -> str:
    """The text the job delivers; empty when there is nothing to say."""
    if not new and not recovered:
        return ""
    lines = [f"Coolifybrücke · {host}"]
    for a in new:
        fix = f" → {FIX_WORD.get(str(a['fix']), str(a['fix']))}" if a.get("fix") else ""
        lines.append(f"{MARK.get(str(a.get('severity')), '·')} {a.get('title')}{fix}")
    for a in recovered:
        lines.append(f"✓ Recovered: {a.get('title')}")
    return "\n".join(lines) + "\n"


def unreachable_item(kind: str, message: str) -> dict:
    title = "Coolify rejected the API token" if kind == "unauthorized" else "Coolify is not reachable"
    return {"key": "coolify_unreachable:-", "signal": "coolify_unreachable", "severity": "bad",
            "title": title, "detail": message[:200]}


# --- the job --------------------------------------------------------------------------------------

def write_shim(home: Path) -> Path:
    scripts = home / "scripts"
    scripts.mkdir(parents=True, exist_ok=True)
    path = scripts / SCRIPT_NAME
    tmp = scripts / f".{SCRIPT_NAME}.{os.getpid()}.tmp"
    tmp.write_text(SHIM, encoding="utf-8")
    os.replace(tmp, path)
    return path


_ANSI = re.compile(r"\x1b\[[0-9;]*m")
_JOB_HEAD = re.compile(r"^  (\S+) \[(\w+)\]\s*$")
_JOB_ROW = re.compile(r"^    ([A-Z][A-Za-z ]*?):\s+(.*?)\s*$")
_ROW_KEYS = {"Name": "name", "Schedule": "schedule_display", "Deliver": "deliver", "Script": "script"}


def parse_cron_list(text: str) -> list[dict]:
    """The jobs in `hermes cron list --all` output, with the keys our_jobs() and describe() read.

    The plugin goes through Hermes' public CLI, not its scheduler module, so this text is the
    contract: a job is a `  <id> [<state>]` line followed by indented `Label: value` rows."""
    jobs: list[dict] = []
    for line in _ANSI.sub("", text or "").splitlines():
        head = _JOB_HEAD.match(line)
        if head:
            state = head.group(2)
            jobs.append({"id": head.group(1), "state": state, "enabled": state == "active"})
            continue
        row = _JOB_ROW.match(line)
        if row and jobs and row.group(1) in _ROW_KEYS:
            jobs[-1][_ROW_KEYS[row.group(1)]] = row.group(2)
    return jobs


def parse_created_job(text: str) -> dict:
    """The job `hermes cron create` reports (`Created job: <id>`, `Schedule: ...`); {} when none."""
    clean = _ANSI.sub("", text or "")
    created = re.search(r"^Created job: (\S+)\s*$", clean, re.M)
    if not created:
        return {}
    schedule = re.search(r"^\s+Schedule: (.+?)\s*$", clean, re.M)
    return {"id": created.group(1), "schedule_display": schedule.group(1) if schedule else None}


def our_jobs(jobs: Iterable[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    """The cron jobs that run the shim (by script, so a renamed job still counts), 0.1.0's included."""
    return [j for j in jobs if str(j.get("script") or "").rstrip("/").endswith((SCRIPT_NAME, OLD_SCRIPT_NAME))]


def remove_old_shim(home: Path) -> bool:
    """Delete 0.1.0's shim script. True when it existed."""
    old = home / "scripts" / OLD_SCRIPT_NAME
    if not old.exists():
        return False
    old.unlink()
    return True


def describe(job: Optional[Mapping[str, Any]], state: Optional[Mapping[str, Any]], now: Optional[float] = None) -> dict:
    """What the settings page and `alerts status` show."""
    now = time.time() if now is None else now
    out: dict[str, Any] = {"on": bool(job and job.get("enabled", True)), "job_id": (job or {}).get("id"),
                           "schedule": (job or {}).get("schedule_display") or ((job or {}).get("schedule") or {}).get("display"),
                           "deliver": (job or {}).get("deliver"), "open": len(((state or {}).get("open") or {}))}
    checked = (state or {}).get("checked")
    if isinstance(checked, (int, float)):
        out["last_check_age_s"] = max(0, round(now - float(checked)))
    return out
