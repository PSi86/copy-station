"""Panel presets and configuration resolution for the e-paper backend.

A user normally selects a panel with a single ``model:`` word; the preset fills
in the controller and the controller-native resolution (and a sensible default
content rotation). Every field can still be overridden explicitly in the config.

``width``/``height`` are the *controller-native* dimensions from the datasheet
(what the SSD168x RAM windows are sized against), not the visually displayed
orientation. ``rotation`` then orients the content for the viewer -- e.g. the
2.9" panel is native 128x296 (portrait) and shown landscape via ``rotation: 90``.
"""

from __future__ import annotations

from typing import Any

# model -> the fields the preset contributes. Keys omitted here fall back to the
# DEFAULTS in config.py (or to an explicit user value, which always wins).
PRESETS: dict[str, dict[str, Any]] = {
    # Waveshare 1.54" V2 -- 200x200, SSD1681 (primary target). Square, so the
    # default content rotation is 0.
    "waveshare-1.54": {"controller": "ssd1681", "width": 200, "height": 200, "rotation": 0},
    # Waveshare 2.9" V2 -- native 128x296, SSD1680. Shown landscape by default.
    "waveshare-2.9": {"controller": "ssd1680", "width": 128, "height": 296, "rotation": 90},
    # WeAct Studio 2.9" black/white -- same SSD1680 panel as the Waveshare 2.9".
    "weact-2.9": {"controller": "ssd1680", "width": 128, "height": 296, "rotation": 90},
    # WeAct Studio 3.7" black/white -- 280x480, SSD1677. ROADMAP: the ssd1677
    # driver is not implemented yet, so this preset is reserved, not functional.
    "weact-3.7": {"controller": "ssd1677", "width": 280, "height": 480, "rotation": 90},
}

# Fields a preset is allowed to fill (everything else comes straight from cfg).
_PRESET_FIELDS = ("controller", "width", "height", "rotation")


class EpaperConfigError(ValueError):
    """Raised when the e-paper configuration cannot be resolved to a panel."""


def resolve_panel(cfg: dict) -> dict:
    """Return a fully-resolved panel config dict.

    Resolution order for the preset-derived fields (controller/width/height/
    rotation): an explicit, non-None ``cfg`` value wins; otherwise the value from
    the named ``model`` preset; otherwise (rotation only) 0.

    Raises :class:`EpaperConfigError` if the panel is under-specified (no model
    and no explicit controller/width/height) or the model is unknown.
    """
    model = cfg.get("model")
    preset: dict[str, Any] = {}
    if model is not None:
        if model not in PRESETS:
            known = ", ".join(sorted(PRESETS))
            raise EpaperConfigError(
                f"Unknown e-paper model '{model}'. Known models: {known}."
            )
        preset = PRESETS[model]

    resolved: dict[str, Any] = dict(cfg)
    for field in _PRESET_FIELDS:
        explicit = cfg.get(field)
        if explicit is not None:
            resolved[field] = explicit
        elif field in preset:
            resolved[field] = preset[field]
        elif field == "rotation":
            resolved[field] = 0
        else:
            resolved[field] = None

    missing = [f for f in ("controller", "width", "height") if resolved.get(f) is None]
    if missing:
        raise EpaperConfigError(
            "E-paper panel under-specified: set 'model' to a preset "
            f"({', '.join(sorted(PRESETS))}) or give explicit "
            f"{', '.join(missing)}."
        )

    if resolved["rotation"] not in (0, 90, 180, 270):
        raise EpaperConfigError(
            f"epaper.rotation must be 0/90/180/270, got {resolved['rotation']!r}"
        )
    return resolved


def display_size(width: int, height: int, rotation: int) -> tuple[int, int]:
    """Viewer-facing (w, h) after applying ``rotation`` to a native panel size."""
    return (height, width) if rotation in (90, 270) else (width, height)
