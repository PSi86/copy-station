#!/usr/bin/env python3
"""Render the documentation previews of the e-paper status layout.

Produces the PNGs under ``docs/images/`` that the README embeds, using the real
rendering pipeline (``status.epaper.layout``), so the documentation always
matches the code. Re-run after changing the layout:

    python scripts/render_epaper_previews.py

Needs Pillow (``pip install Pillow`` on the dev machine; ``python3-pil`` on the
device). The images are committed so the README renders without running this.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from PIL import Image  # noqa: E402

from copystation import __version__  # noqa: E402
from copystation.status.epaper.layout import render, render_stopped  # noqa: E402
from copystation.status.epaper.model import build_view  # noqa: E402
from copystation.status.epaper.presets import display_size, resolve_panel  # noqa: E402

OUT_DIR = REPO_ROOT / "docs" / "images"

# Scale the chunky 1-bit frames up with nearest-neighbour so the pixels stay
# crisp (true to how the panel looks) instead of being blurred by the browser.
SCALE = 3

_SOURCE = {"label": "DJI O4", "capacity": 32_000_000_000, "used": 12_000_000_000}
_TARGET = {"label": "SDXC", "capacity": 256_000_000_000, "used": 121_000_000_000}

# (filename phase token) -> snapshot dict, mirroring StationState.snapshot().
STATES = {
    "copying": {
        "phase": "copying", "percent": 67.0,
        "source": _SOURCE, "target": _TARGET, "devices": [{}, {}],
        "speed_bytes": 18_000_000, "eta_seconds": 42, "error": "",
    },
    "detecting": {
        "phase": "detecting", "percent": 0.0,
        # While detecting, source/target are still empty -- the panel shows the
        # detected volume(s) from the devices list.
        "devices": [
            {"name": "O4 Lite", "node": "/dev/sdb1", "role": "candidate",
             "capacity": 32_000_000_000, "free": 20_000_000_000},
        ],
    },
    "error": {
        "phase": "error",
        "error": "Target device was disconnected during the copy.",
        "source": _SOURCE, "target": _TARGET, "devices": [{}, {}],
    },
}

# (filename token, model, [phases]) -- which panels/states to render.
JOBS = [
    ("1.54", "waveshare-1.54", ["copying", "detecting", "error", "stopped"]),
    ("2.9", "waveshare-2.9", ["copying"]),
    ("2.13", "waveshare-2.13", ["copying"]),
]


def _render(model: str, phase: str):
    panel = resolve_panel({"model": model})
    w, h = display_size(panel["width"], panel["height"], panel["rotation"])
    if phase == "stopped":
        return render_stopped(__version__, w, h)
    return render(build_view(STATES[phase], __version__), w, h)


def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    for token, model, phases in JOBS:
        for phase in phases:
            img = _render(model, phase)
            img = img.resize((img.width * SCALE, img.height * SCALE), Image.NEAREST)
            path = OUT_DIR / f"epaper-{token}-{phase}.png"
            img.save(path)
            print(f"wrote {path.relative_to(REPO_ROOT)} ({img.width}x{img.height})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
