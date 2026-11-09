"""The instance notebook `COOLIFY.md`, and the cockpit's own entries for changes it observes.

The notebook holds what neither Hermes memory nor a skill holds: facts about this one Coolify
instance ("app X needs NPM_TOKEN as a build variable", "upgraded 4.6.0 -> 4.7.0"). Three authors
write it: the user (the cockpit's widget), the agent (`coolify_notes append`) and the cockpit itself
when it observes a change (`observe`). It is read on demand (`coolify_notes read`, `@coolify:notes`)
and never injected into every turn.

File layout (plain Markdown, one dated section per entry, newest last):

    # COOLIFY.md
    <one paragraph saying what this file is>

    ## 2026-11-08 14:03 UTC · agent
    The entry text.

Every write is atomic: the new file is written next to the old one and moved over it, under an
exclusive lock, so a reader never sees half an entry and two writers (the agent's tool in one
process, the pane's route in another) never lose each other's entry. Entry text passes through
the same secret redaction as deployment logs before it is stored.

Observed changes compare what a capabilities check or a fresh snapshot saw with the last seen state
in `observed-state.json` (same data dir). Each change writes exactly one entry: the Coolify version,
a server appearing or disappearing, a token's rights or scope. What a run could not see (servers
through a scoped token, a token that was refused) is unknown, not \"gone\", and changes nothing.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
import os
import re
import threading
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterator, Optional

try:
    from .redact import redact
except ImportError:  # loaded as a top-level module (tests, scripts)
    from redact import redact  # type: ignore

NOTES_FILE = "COOLIFY.md"
STATE_FILE = "observed-state.json"
LOCK_FILE = ".COOLIFY.md.lock"

AUTHORS = ("you", "agent", "cockpit")
MAX_ENTRY_CHARS = 4000
MAX_FILE_BYTES = 1_000_000
READ_DEFAULT_CHARS = 6000
READ_MAX_CHARS = 40_000

TITLE = (
    "# COOLIFY.md\n\n"
    "Notes about this Coolify instance, written by you (the cockpit's notebook widget), the agent "
    "(`coolify_notes append`) and the cockpit itself when it sees a change (version, servers, token "
    "rights). Hermes reads it only on demand (`coolify_notes read`, `@coolify:notes`). Newest entry "
    "last.\n"
)

_ENTRY_HEAD = re.compile(r"(?m)^## (?P<when>\d{4}-\d{2}-\d{2} \d{2}:\d{2}) UTC · (?P<author>[a-z]+)(?: · (?P<topic>[^\n]*))?$")

Clock = Callable[[], _dt.datetime]


def _utcnow() -> _dt.datetime:
    return _dt.datetime.now(_dt.timezone.utc)


class NotesError(ValueError):
    """An append the notebook refuses (empty, too long, file too large); the message says why."""


# --- locking and atomic writes -------------------------------------------------------------------

_THREAD_LOCK = threading.Lock()


@contextmanager
def _locked(data_dir: Path) -> Iterator[None]:
    """One writer at a time across threads and processes (flock where the OS has it)."""
    data_dir.mkdir(parents=True, exist_ok=True)
    with _THREAD_LOCK:
        try:
            import fcntl
        except ImportError:  # no flock on this OS: threads are still serialized
            yield
            return
        fd = os.open(data_dir / LOCK_FILE, os.O_WRONLY | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)


def _atomic_write(path: Path, text: str) -> None:
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


# --- the notebook --------------------------------------------------------------------------------

@dataclass
class Entry:
    when: str      # "2026-11-08 14:03"
    author: str    # you | agent | cockpit
    topic: str     # "" or a short label (version, server, token)
    text: str
    masked: int = 0

    def to_markdown(self) -> str:
        topic = f" · {self.topic}" if self.topic else ""
        return f"## {self.when} UTC · {self.author}{topic}\n{self.text}\n"

    def to_dict(self) -> dict[str, Any]:
        return {"when": self.when + " UTC", "author": self.author, "topic": self.topic or None,
                "text": self.text, "masked_secrets": self.masked}


def _clean_text(text: Any) -> tuple[str, int]:
    """Strip, redact secrets, and keep entries from forging another entry's heading."""
    body = str(text if text is not None else "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if not body:
        raise NotesError("The note is empty. Pass the text to write.")
    if len(body) > MAX_ENTRY_CHARS:
        raise NotesError(f"The note has {len(body)} characters; one entry holds at most {MAX_ENTRY_CHARS}. "
                         "Shorten it or split it into several entries.")
    body, masked = redact(body)
    body = re.sub(r"(?m)^##(?=\s)", "###", body)  # a level-2 heading would read as a new entry
    return body, masked


def _clean_topic(topic: Any) -> str:
    words = re.sub(r"[^\w .:/-]", "", str(topic or "")).strip()
    return words[:40]


class Notebook:
    """`COOLIFY.md` in the plugin's data dir. Pure file logic; the data dir comes in from outside."""

    def __init__(self, data_dir: Path, clock: Clock = _utcnow):
        self.data_dir = Path(data_dir)
        self.path = self.data_dir / NOTES_FILE
        self._clock = clock

    def _read_raw(self) -> str:
        try:
            return self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return ""

    def append(self, text: Any, *, author: str = "agent", topic: str = "") -> Entry:
        """Add one dated entry at the end, atomically. Returns the entry as stored."""
        if author not in AUTHORS:
            raise NotesError(f"Unknown author {author!r}; one of {', '.join(AUTHORS)}.")
        body, masked = _clean_text(text)
        entry = Entry(self._clock().astimezone(_dt.timezone.utc).strftime("%Y-%m-%d %H:%M"), author,
                      _clean_topic(topic), body, masked)
        with _locked(self.data_dir):
            self._write_entries([entry])
        return entry

    def _write_entries(self, entries: list[Entry]) -> None:
        """Append entries to the file; the caller holds the lock."""
        current = self._read_raw()
        if len(current.encode("utf-8")) > MAX_FILE_BYTES:
            raise NotesError(f"{NOTES_FILE} is larger than {MAX_FILE_BYTES // 1000} KB. Trim old entries "
                             "first (it is a plain Markdown file in the plugin's data folder).")
        text = current if current.strip() else TITLE
        for entry in entries:
            text = text.rstrip("\n") + "\n\n" + entry.to_markdown()
        _atomic_write(self.path, text)

    def entries(self) -> list[Entry]:
        raw = self._read_raw()
        heads = list(_ENTRY_HEAD.finditer(raw))
        out = []
        for i, head in enumerate(heads):
            end = heads[i + 1].start() if i + 1 < len(heads) else len(raw)
            out.append(Entry(head["when"], head["author"], head["topic"] or "", raw[head.end():end].strip()))
        return out

    def read(self, max_chars: Optional[int] = READ_DEFAULT_CHARS) -> dict[str, Any]:
        """The notebook text, newest entries kept when it is longer than `max_chars`."""
        raw = self._read_raw()
        cap = READ_DEFAULT_CHARS if max_chars is None else max(500, min(int(max_chars), READ_MAX_CHARS))
        heads = list(_ENTRY_HEAD.finditer(raw))
        total = len(heads)
        if not raw.strip():
            return {"exists": False, "entries": 0, "chars": 0, "truncated": False,
                    "text": "COOLIFY.md is empty: no notes about this instance yet."}
        if len(raw) <= cap:
            return {"exists": True, "entries": total, "chars": len(raw), "truncated": False, "text": raw.rstrip()}
        starts = [h.start() for h in heads]
        title = raw[: starts[0]].rstrip() if starts else ""
        kept_from = len(raw)
        for start in reversed(starts):  # newest entries first, as many as fit
            if len(raw) - start + len(title) + 120 > cap:
                break
            kept_from = start
        shown = sum(1 for s in starts if s >= kept_from)
        if shown == 0:  # even the newest entry is too long: cut it, say so
            body = raw[starts[-1]:] if starts else raw
            text = body[: cap - 80].rstrip() + "\n... (entry cut at the size cap)"
            shown = 1 if starts else 0
        else:
            text = raw[kept_from:].rstrip()
        hidden = total - shown
        note = (f"({hidden} older entr{'y' if hidden == 1 else 'ies'} not shown; read again with a larger "
                f"max_chars, up to {READ_MAX_CHARS})")
        return {"exists": True, "entries": total, "chars": len(raw), "truncated": True, "shown_entries": shown,
                "text": "\n\n".join(p for p in (title, note, text) if p)}


# --- observed changes ----------------------------------------------------------------------------

@dataclass
class TokenSeen:
    fingerprint: str
    rights: list[str]
    scope: str  # scoped | team-wide | unknown

    def to_dict(self) -> dict[str, Any]:
        return {"fingerprint": self.fingerprint, "rights": sorted(self.rights), "scope": self.scope}


@dataclass
class Observation:
    """What one run saw. None in a field means \"not seen this time\", never \"gone\"."""

    instance_key: str
    coolify_version: Optional[str] = None
    latest_version: Optional[str] = None
    update_available: Optional[bool] = None
    servers: Optional[dict[str, str]] = None          # uuid -> name, only from a team-wide read
    tokens: dict[str, TokenSeen] = field(default_factory=dict)  # setting name -> what it holds
    source: str = ""                                  # capabilities | snapshot


def instance_key(base_url: str) -> str:
    return hashlib.sha256(str(base_url).rstrip("/").encode("utf-8")).hexdigest()[:16]


def _load_state(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"schema": 1, "instances": {}}
    if not isinstance(data, dict) or not isinstance(data.get("instances"), dict):
        return {"schema": 1, "instances": {}}
    return data


def _version_text(obs: Observation, before: str) -> str:
    text = f"Coolify version changed: {before} → {obs.coolify_version}."
    if obs.update_available is True and obs.latest_version:
        text += f" Coolify {obs.latest_version} is available as an update."
    elif obs.update_available is False and obs.latest_version:
        text += f" {obs.coolify_version} is the latest release."
    return text + " Check the release notes for changed API rights or settings."


def changes(previous: dict[str, Any], obs: Observation) -> tuple[list[tuple[str, str]], dict[str, Any]]:
    """(entries as (topic, text), the new stored state). Pure: no file access."""
    state = {k: v for k, v in previous.items()} if isinstance(previous, dict) else {}
    found: list[tuple[str, str]] = []

    if obs.coolify_version:
        before = state.get("coolify_version")
        if before and before != obs.coolify_version:
            found.append(("version", _version_text(obs, before)))
        state["coolify_version"] = obs.coolify_version
    if obs.latest_version:
        state["latest_version"] = obs.latest_version

    if obs.servers is not None:
        before_servers = state.get("servers")
        if isinstance(before_servers, dict):
            for uuid, name in sorted(obs.servers.items(), key=lambda kv: kv[1]):
                if uuid not in before_servers:
                    found.append(("server", f"New server visible: {name} ({uuid})."))
            for uuid, name in sorted(before_servers.items(), key=lambda kv: str(kv[1])):
                if uuid not in obs.servers:
                    found.append(("server", f"Server no longer listed: {name} ({uuid}). It was removed in "
                                            "Coolify, or the reading token no longer sees it."))
        state["servers"] = dict(obs.servers)

    raw_tokens = state.get("tokens")
    tokens_before: dict[str, Any] = dict(raw_tokens) if isinstance(raw_tokens, dict) else {}
    tokens_now: dict[str, Any] = dict(tokens_before)
    for setting, seen in sorted(obs.tokens.items()):
        old = tokens_before.get(setting)
        now = seen.to_dict()
        if isinstance(old, dict) and (sorted(old.get("rights") or []) != now["rights"]
                                      or old.get("scope") != now["scope"]):
            parts = []
            gained = sorted(set(now["rights"]) - set(old.get("rights") or []))
            lost = sorted(set(old.get("rights") or []) - set(now["rights"]))
            if gained:
                parts.append("gained " + ", ".join(gained))
            if lost:
                parts.append("lost " + ", ".join(lost))
            if old.get("scope") != now["scope"]:
                parts.append(f"scope {old.get('scope')} → {now['scope']}")
            other = " (a different token is stored there now)" if old.get("fingerprint") != now["fingerprint"] else ""
            found.append(("token", f"The token in {setting}{other}: " + "; ".join(parts) + ". Rights now: "
                                   + (", ".join(now["rights"]) or "none") + f"; {now['scope']}."))
        tokens_now[setting] = now
    state["tokens"] = tokens_now
    return found, state


def observe(data_dir: Optional[Path], obs: Observation, *, clock: Clock = _utcnow) -> list[Entry]:
    """Compare with the last seen state, write one notebook entry per change, store the new state.

    The first observation of an instance only records the state. Read, compare, write and append
    run under the notebook's lock, so two processes observing the same change write one entry.
    """
    if data_dir is None:
        return []
    data_dir = Path(data_dir)
    book = Notebook(data_dir, clock)
    with _locked(data_dir):
        state_path = data_dir / STATE_FILE
        stored = _load_state(state_path)
        previous = stored["instances"].get(obs.instance_key)
        found, new_state = changes(previous or {}, obs)
        new_state["seen_at"] = clock().astimezone(_dt.timezone.utc).replace(microsecond=0).isoformat()
        stamp = clock().astimezone(_dt.timezone.utc).strftime("%Y-%m-%d %H:%M")
        entries = [Entry(stamp, "cockpit", topic, text) for topic, text in found]  # own text: no secrets
        if entries:
            book._write_entries(entries)
        stored["instances"][obs.instance_key] = new_state
        _atomic_write(state_path, json.dumps(stored, indent=1, sort_keys=True))
    return entries
