"""Driver for the SSD1680 / SSD1681 e-paper controllers.

These two controllers share a command set; they differ only in the values
derived from the panel's width/height, so one parameterised driver covers both:

* SSD1681 -- Waveshare 1.54" (200x200).
* SSD1680 -- Waveshare 2.9" and WeAct 2.9" (native 128x296).

The init/refresh sequence follows the SSD168x datasheet and Waveshare's V2
reference flow. The controllers carry built-in (OTP) waveforms, so no LUT upload
is needed for a full refresh; partial refresh reuses the previous frame as the
base and refreshes with the partial update mode.

The exact update-control bytes (0x22 values), the border waveform and BUSY
polarity should be confirmed on each panel -- see the plan's hardware-validation
points. The RAM is 1 bit/pixel, 1 = white, packed MSB-first per row.
"""

from __future__ import annotations

from .base import EpaperDriver

# Display update control 2 (0x22) operands: which engines to run on 0x20.
_UPDATE_FULL = 0xF7     # load temperature + full waveform + display
_UPDATE_PARTIAL = 0xFF  # display using the partial update mode


class Ssd168xDriver(EpaperDriver):
    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        # The previously displayed frame, used as the base for partial updates.
        self._last_buf: list[int] | None = None

    def init(self) -> None:
        self.reset()
        self.wait_busy()
        self.command(0x12)  # software reset
        self.wait_busy()

        self.command(0x01)  # driver output control
        last_gate = self.height - 1
        self.data([last_gate & 0xFF, (last_gate >> 8) & 0xFF, 0x00])

        self.command(0x11)  # data entry mode: X increment, Y increment
        self.data([0x03])

        self._set_window(0, 0, self.width - 1, self.height - 1)

        self.command(0x3C)  # border waveform
        self.data([0x05])
        self.command(0x18)  # temperature sensor: use the internal one
        self.data([0x80])
        self.command(0x21)  # display update control 1
        self.data([0x00, 0x80])

        self._set_cursor(0, 0)
        self.wait_busy()

    def _set_window(self, x0: int, y0: int, x1: int, y1: int) -> None:
        self.command(0x44)  # RAM X start/end (byte addresses)
        self.data([(x0 >> 3) & 0xFF, (x1 >> 3) & 0xFF])
        self.command(0x45)  # RAM Y start/end
        self.data([y0 & 0xFF, (y0 >> 8) & 0xFF, y1 & 0xFF, (y1 >> 8) & 0xFF])

    def _set_cursor(self, x: int, y: int) -> None:
        self.command(0x4E)  # RAM X address counter
        self.data([(x >> 3) & 0xFF])
        self.command(0x4F)  # RAM Y address counter
        self.data([y & 0xFF, (y >> 8) & 0xFF])

    def _refresh(self, mode: int) -> None:
        self.command(0x22)
        self.data([mode])
        self.command(0x20)  # master activation
        self.wait_busy()

    def display_full(self, buf) -> None:
        buf = list(buf)
        self._set_cursor(0, 0)
        self.command(0x24)  # write B/W RAM (current frame)
        self.data(buf)
        self._set_cursor(0, 0)
        self.command(0x26)  # write the "previous" RAM so partials have a base
        self.data(buf)
        self._refresh(_UPDATE_FULL)
        self._last_buf = buf

    def display_partial(self, buf) -> None:
        buf = list(buf)
        # Seed the previous frame so the controller can compute the delta.
        if self._last_buf is not None:
            self._set_cursor(0, 0)
            self.command(0x26)
            self.data(self._last_buf)
        self._set_cursor(0, 0)
        self.command(0x24)  # new frame
        self.data(buf)
        self._refresh(_UPDATE_PARTIAL)
        self._last_buf = buf

    def clear(self) -> None:
        self.display_full([0xFF] * self.buffer_size)  # 0xFF = all white

    def sleep(self) -> None:
        self.command(0x10)  # deep sleep mode
        self.data([0x01])
