"""The single user-settings overlay: sections, load/prune/save robustness."""

import json

from copystation.settings_store import USER_SETTINGS_SCHEMA, SettingsStore

# A small schema used by most tests (two sections, distinct keys).
SCHEMA = {"transcode": ("auto_transcode", "default_preset"), "wifi_ap": ("enabled",)}


def _read(path):
    return json.loads(path.read_text())


def test_sections_are_independent_and_persist(tmp_path):
    path = tmp_path / "user.json"
    store = SettingsStore(str(path), SCHEMA)
    tc = store.section("transcode")
    ap = store.section("wifi_ap")

    assert tc.has("auto_transcode") is False
    assert tc.get("auto_transcode", "fallback") == "fallback"

    tc.update(auto_transcode=True)
    ap.update(enabled=True)
    # Both sections live in ONE file, each under its own key.
    assert _read(path) == {"transcode": {"auto_transcode": True}, "wifi_ap": {"enabled": True}}
    assert tc.get("auto_transcode") is True and ap.get("enabled") is True

    # Updating one section never disturbs the other.
    tc.update(default_preset="720p-h264")
    assert _read(path) == {
        "transcode": {"auto_transcode": True, "default_preset": "720p-h264"},
        "wifi_ap": {"enabled": True},
    }


def test_update_ignores_unknown_keys(tmp_path):
    path = tmp_path / "user.json"
    store = SettingsStore(str(path), SCHEMA)
    store.section("wifi_ap").update(enabled=True, bogus="x")  # bogus not persisted
    assert _read(path) == {"wifi_ap": {"enabled": True}}


def test_unknown_section_raises(tmp_path):
    store = SettingsStore(str(tmp_path / "user.json"), SCHEMA)
    try:
        store.section("does_not_exist")
    except KeyError:
        pass
    else:  # pragma: no cover
        raise AssertionError("expected KeyError for an unknown section")


def test_load_prunes_unknown_sections_and_keys_and_rewrites(tmp_path):
    # A file from another version with a since-renamed key AND a whole removed
    # section must load robustly: both are dropped and the file cleaned up.
    path = tmp_path / "user.json"
    path.write_text(json.dumps({
        "transcode": {"auto_transcode": True, "old_key": 1},   # stale key
        "wifi_ap": {"enabled": False},
        "removed_feature": {"whatever": 2},                    # stale whole section
    }))
    store = SettingsStore(str(path), SCHEMA)
    assert store.as_dict() == {"transcode": {"auto_transcode": True},
                               "wifi_ap": {"enabled": False}}
    # The overlay file itself was rewritten without the stale entries...
    assert _read(path) == {"transcode": {"auto_transcode": True},
                           "wifi_ap": {"enabled": False}}
    # ...and reloading stays clean (no churn).
    store.reload()
    assert "removed_feature" not in store.as_dict()


def test_all_keys_valid_leaves_file_untouched(tmp_path):
    path = tmp_path / "user.json"
    original = {"transcode": {"auto_transcode": False, "default_preset": "1080p-h264"}}
    path.write_text(json.dumps(original))
    mtime = path.stat().st_mtime_ns
    SettingsStore(str(path), SCHEMA)
    # No unknown sections/keys -> no rewrite (the file is left exactly as-is).
    assert path.stat().st_mtime_ns == mtime
    assert _read(path) == original


def test_missing_corrupt_or_nonobject_file_reads_empty(tmp_path):
    assert SettingsStore(str(tmp_path / "nope.json"), SCHEMA).as_dict() == {}

    corrupt = tmp_path / "bad.json"
    corrupt.write_text("{ this is not json")
    assert SettingsStore(str(corrupt), SCHEMA).as_dict() == {}

    non_object = tmp_path / "list.json"
    non_object.write_text("[1, 2, 3]")
    assert SettingsStore(str(non_object), SCHEMA).as_dict() == {}


def test_default_schema_covers_both_features():
    # The shipped schema declares exactly the runtime sections used across the app.
    assert set(USER_SETTINGS_SCHEMA) == {"transcode", "wifi_ap"}
    assert set(USER_SETTINGS_SCHEMA["transcode"]) == {"auto_transcode", "default_preset"}
    assert set(USER_SETTINGS_SCHEMA["wifi_ap"]) == {"enabled"}
