"""Behaviour: route bodies of the wrong type are refused as invalid (400), never stored coerced or
reported as a conflict.
"""

import pytest


@pytest.mark.parametrize("body", [{"text": ["a"]}, {"text": 5}, {"text": {"a": 1}},
                                  {"text": "fine", "topic": ["x"]}, {"text": "fine", "topic": 7}])
def test_a_note_that_is_not_text_is_refused_and_not_stored(plugin, tmp_path, body):
    status, answer = plugin.api_notes_append(body, data_dir=tmp_path)
    assert status == 400 and answer["error"] == "invalid", answer
    _, read = plugin.api_notes_read(data_dir=tmp_path)
    assert read.get("entries", 0) in (0, None) or "fine" not in read.get("text", "")


@pytest.mark.parametrize("version", ["a", {}, True, [1], 1.5])
def test_a_layout_version_that_is_not_a_whole_number_is_invalid(plugin, tmp_path, version):
    layout = plugin._layout()
    store = layout.LayoutStore(tmp_path / "layout.json")
    status, put = plugin.api_layout_put(store, {"expected_version": version, "layout": {"preset": "overview"}})
    assert status == 400 and put["error"] == "invalid", put
    status, preset = plugin.api_layout_preset(store, {"preset": "overview", "expected_version": version})
    assert status == 400 and preset["error"] == "invalid", preset
