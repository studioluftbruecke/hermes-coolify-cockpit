"""migrate.py: 0.1.0's data folder `plugin-data/coolify-cockpit/` carried over to the new name."""

import importlib
import pathlib
import re

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]


@pytest.fixture
def migrate(plugin):
    return importlib.import_module(plugin.__name__ + ".migrate")


def hermes_data_dir(root):
    """Hermes' `plugin_data_dir`: returns the folder and creates it on every call."""
    def plugin_data_dir(name):
        path = root / name
        path.mkdir(parents=True, exist_ok=True)
        return path
    return plugin_data_dir


def old_folder(root, files):
    old = root / "coolify-cockpit"
    old.mkdir(parents=True)
    for name, text in files.items():
        (old / name).write_text(text, encoding="utf-8")
    return old


def snapshot(folder):
    return {p.name: p.read_bytes() for p in folder.iterdir() if p.is_file()}


def test_old_folder_only_copies_user_files_and_leaves_locks_and_caches(migrate, tmp_path):
    old = old_folder(tmp_path, {"layout.json": "{\"v\": 3}", "COOLIFY.md": "# notes\n", "alerts-state.json": "{}",
                                "layout.json.lock": "", "snapshot-cache.json": "{}", "capabilities-cache.json": "{}"})
    (old / "sub").mkdir()
    before = snapshot(old)
    new = migrate.data_dir(hermes_data_dir(tmp_path), "coolifybruecke")
    assert sorted(p.name for p in new.iterdir()) == ["COOLIFY.md", "alerts-state.json", "layout.json"]
    assert (new / "layout.json").read_text() == "{\"v\": 3}" and (new / "COOLIFY.md").read_text() == "# notes\n"
    assert snapshot(old) == before


def test_both_folders_the_new_file_wins_and_the_old_stays(migrate, tmp_path):
    old = old_folder(tmp_path, {"layout.json": "old", "COOLIFY.md": "old notes"})
    new = tmp_path / "coolifybruecke"
    new.mkdir()
    (new / "layout.json").write_text("new", encoding="utf-8")
    before = snapshot(old)
    assert migrate.data_dir(hermes_data_dir(tmp_path), "coolifybruecke") == new
    assert (new / "layout.json").read_text() == "new" and (new / "COOLIFY.md").read_text() == "old notes"
    assert snapshot(old) == before


def test_new_folder_created_empty_before_the_first_call_still_gets_the_files(plugin, own_hermes_home):
    pytest.importorskip("plugins.plugin_storage")
    data = own_hermes_home / "plugin-data"
    old_folder(data, {"layout.json": "old", "COOLIFY.md": "old notes"})
    (data / "coolifybruecke").mkdir()
    assert plugin._default_layout_file() == data / "coolifybruecke" / "layout.json"
    assert sorted(p.name for p in (data / "coolifybruecke").iterdir()) == ["COOLIFY.md", "layout.json"]


def test_the_copy_is_removed_by_0_3_0(migrate):
    version = re.search(r'(?m)^version:\s*"?([0-9.]+)', (ROOT / "plugin.yaml").read_text(encoding="utf-8")).group(1)
    as_tuple = lambda v: tuple(int(x) for x in v.split("."))  # noqa: E731
    assert as_tuple(version) < as_tuple(migrate.REMOVE_IN), "delete migrate.py, its test and its call in __init__.py"
