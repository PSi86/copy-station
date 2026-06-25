"""Driver for the SSD1677 controller -- WeAct 3.7" (280x480). ROADMAP.

SSD1677 shares the SSD168x command model but uses 16-bit gate parameters and a
slightly different init, and cannot be validated without the panel. The class is
reserved so the preset and factory resolve cleanly; it raises until implemented.
"""

from __future__ import annotations

from .base import EpaperDriver


class Ssd1677Driver(EpaperDriver):
    def init(self) -> None:
        raise NotImplementedError(
            "The SSD1677 driver (WeAct 3.7\") is on the roadmap but not "
            "implemented yet. Use a waveshare-1.54/2.9 or weact-2.9 panel."
        )
