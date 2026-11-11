"""Carry the data folder of Coolify Cockpit 0.1.0 over to the new name. Nothing else.

0.1.0 kept its files in `<profile>/plugin-data/coolify-cockpit/`. 0.2.0 reads
`<profile>/plugin-data/coolifybruecke/`. Every path to the data folder goes through `data_dir`, so
the copy runs in whichever process asks first: the gateway, the plugin host child (where
`register()` never runs) or a CLI command.

Per file, not per folder: Hermes creates the new folder on the first `plugin_data_dir` call, so
"copy when the new folder is missing" would never fire. A file already in the new folder wins. Lock
files and caches are not copied; they rebuild by themselves. The old folder is never changed.
"""

from __future__ import annotations

import os
import shutil
import threading
from pathlib import Path
from typing import Callable

OLD_NAME = "coolify-cockpit"
REMOVE_IN = "0.3.0"

_DONE: set[Path] = set()
_LOCK = threading.Lock()


def _carried(name: str) -> bool:
    return not (name.endswith(".lock") or name.endswith("-cache.json"))


def carry_over(old: Path, new: Path) -> list[str]:
    """Copy each regular file of `old` that `new` lacks. Returns the names copied."""
    try:
        sources = sorted(p for p in old.iterdir() if p.is_file() and not p.is_symlink() and _carried(p.name))
    except OSError:
        return []
    copied = []
    for source in sources:
        target = new / source.name
        if target.exists():
            continue
        tmp = new / f".{source.name}.{os.getpid()}.{threading.get_ident()}.migrate.tmp"
        try:
            shutil.copy2(source, tmp)
            os.replace(tmp, target)
            copied.append(source.name)
        except OSError:
            try:
                tmp.unlink()
            except OSError:
                pass
    return copied


def data_dir(plugin_data_dir: Callable[[str], Path], name: str) -> Path:
    """`plugin_data_dir(name)`, with 0.1.0's files carried over once per folder and process."""
    new = plugin_data_dir(name)
    with _LOCK:
        if new not in _DONE:
            carry_over(new.parent / OLD_NAME, new)
            _DONE.add(new)
    return new
