import pytest

from copystation.status.epaper.presets import (
    EpaperConfigError,
    display_size,
    resolve_panel,
)


def test_preset_fills_controller_and_resolution():
    panel = resolve_panel({"model": "waveshare-1.54"})
    assert panel["controller"] == "ssd1681"
    assert (panel["width"], panel["height"]) == (200, 200)
    assert panel["rotation"] == 0


def test_2_9_preset_defaults_to_landscape_rotation():
    panel = resolve_panel({"model": "waveshare-2.9"})
    assert panel["controller"] == "ssd1680"
    assert (panel["width"], panel["height"]) == (128, 296)
    assert panel["rotation"] == 90
    # WeAct 2.9" is the same SSD1680 panel.
    weact = resolve_panel({"model": "weact-2.9"})
    assert (weact["controller"], weact["width"], weact["height"]) == ("ssd1680", 128, 296)


def test_explicit_value_overrides_preset():
    panel = resolve_panel({"model": "waveshare-1.54", "width": 250, "rotation": 180})
    assert panel["width"] == 250          # explicit wins
    assert panel["height"] == 200         # still from the preset
    assert panel["rotation"] == 180


def test_explicit_controller_without_model():
    panel = resolve_panel({"controller": "ssd1680", "width": 296, "height": 128})
    assert panel["controller"] == "ssd1680"
    assert panel["rotation"] == 0         # defaults to 0 when neither set


def test_under_specified_raises():
    with pytest.raises(EpaperConfigError):
        resolve_panel({})                 # no model and no controller/size


def test_unknown_model_raises():
    with pytest.raises(EpaperConfigError):
        resolve_panel({"model": "nope"})


def test_bad_rotation_raises():
    with pytest.raises(EpaperConfigError):
        resolve_panel({"model": "waveshare-1.54", "rotation": 45})


def test_display_size_swaps_on_quarter_turns():
    assert display_size(200, 200, 0) == (200, 200)
    assert display_size(200, 200, 180) == (200, 200)
    assert display_size(128, 296, 90) == (296, 128)
    assert display_size(128, 296, 270) == (296, 128)
