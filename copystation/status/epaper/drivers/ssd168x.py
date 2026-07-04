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

The one hardware-validated difference between the two controllers is byte B of
"display update control 1" (0x21): the SSD1680 reference code sets B7 (source
window starts at S8), but on the SSD1681 that bit shrinks the usable source
window to S8..S199 = 192 px -- the chip then SILENTLY DISCARDS every RAM write
whose X window is wider than 24 bytes, which shows as a never-changing frame
(confirmed on the Waveshare 1.54" V2.1 panel). Hence the per-controller
subclasses below.
"""

from __future__ import annotations

from .base import EpaperDriver

# Display update control 2 (0x22) operands: which engines to run on 0x20.
_UPDATE_FULL = 0xF7         # load temperature + full waveform + display
_UPDATE_PARTIAL = 0xFF      # display mode 2 with the OTP waveform (SSD1680)
_UPDATE_PARTIAL_LUT = 0xCF  # display mode 2 with a host-loaded LUT (SSD1681)

# Partial-refresh waveform for the 1.54" V2 panel, verbatim from Waveshare's
# MIT-licensed reference driver (epd1in54_V2.py, WF_PARTIAL_1IN54_0). The OTP
# mode-2 waveform works too, but leaves standing ghosts of erased content; this
# LUT actively decays them with every further partial update (validated on the
# V2.1 panel). Layout: 153 LUT bytes for 0x32, then gate voltage (0x03), source
# voltages (0x04, 3 bytes), dummy/gate line rate (0x3F) and VCOM (0x2C).
_WF_PARTIAL_1IN54 = (
    0x00, 0x40, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
    0x80, 0x80, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
    0x40, 0x40, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
    0x00, 0x80, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
    0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
    0x0F, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x01, 0x01, 0x00, 0x00, 0x00,
    0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
    0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
    0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
    0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
    0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
    0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
    0x22, 0x22, 0x22, 0x22, 0x22, 0x22, 0x00, 0x00, 0x00, 0x02, 0x17, 0x41,
    0xB0, 0x32, 0x28,
)

# Write register for display option (0x37): byte 5 = 0x40 enables the RAM
# ping-pong for display mode 2 -- after each partial update the controller
# copies the new frame into the "previous" RAM itself, so partials only need
# to write 0x24.
_PINGPONG_0X37 = (0x00, 0x00, 0x00, 0x00, 0x00, 0x40, 0x00, 0x00, 0x00, 0x00)


class Ssd168xDriver(EpaperDriver):
    # Byte B of display update control 1 (0x21). B7 selects the S8.. source
    # window; it must stay CLEAR on the SSD1681 (see the module docstring).
    _update_ctrl1_b = 0x00

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
        self.data([0x00, self._update_ctrl1_b])

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


class Ssd1680Driver(Ssd168xDriver):
    """SSD1680 (2.9"/2.13" panels): S8.. source mode, per the reference code."""

    _update_ctrl1_b = 0x80


class Ssd1681Driver(Ssd168xDriver):
    """SSD1681 (1.54", 200x200): all 200 sources -- B7 of 0x21 must stay clear.

    Partial refreshes follow the vendor flow instead of the OTP mode-2 waveform:
    on the first partial after a full frame the driver loads the dedicated
    partial LUT, enables the RAM ping-pong and switches the border, then each
    partial only writes the new frame and displays with ``0xCF``. A subsequent
    full refresh re-runs ``init`` (hardware reset) to restore the full-refresh
    state. Hardware-validated on the Waveshare 1.54" V2.1: the OTP path leaves
    standing ghosts, the vendor LUT decays them with every further update.
    """

    _update_ctrl1_b = 0x00

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self._partial_mode = False

    def init(self) -> None:
        self._partial_mode = False
        super().init()

    def _enter_partial(self) -> None:
        """Load the partial LUT and switch the panel into partial-update mode.

        Mirrors Waveshare's ``init(isPartial=1)``: reset, host LUT (0x32 plus
        the voltage/VCOM tail registers), ping-pong enable, partial border,
        power-on. The reset reverts entry mode and RAM window to POR, so both
        are re-established to this driver's conventions afterwards.
        """
        lut = _WF_PARTIAL_1IN54
        self.reset()
        self.wait_busy()
        self.command(0x32)  # LUT register
        self.data(lut[:153])
        self.wait_busy()
        self.command(0x3F)  # dummy line / gate time options from the LUT tail
        self.data([lut[153]])
        self.command(0x03)  # gate driving voltage
        self.data([lut[154]])
        self.command(0x04)  # source driving voltages
        self.data(lut[155:158])
        self.command(0x2C)  # VCOM
        self.data([lut[158]])
        self.command(0x37)
        self.data(_PINGPONG_0X37)
        self.command(0x3C)  # border waveform for partial mode
        self.data([0x80])
        self.command(0x22)  # power on (enable clock + analog)
        self.data([0xC0])
        self.command(0x20)
        self.wait_busy()
        self.command(0x11)  # data entry mode: X increment, Y increment
        self.data([0x03])
        self._set_window(0, 0, self.width - 1, self.height - 1)
        self._partial_mode = True

    def display_full(self, buf) -> None:
        if self._partial_mode:
            # Leave partial mode: the LUT, border and voltages must go back to
            # the full-refresh state, which init() restores via hardware reset.
            self.init()
        super().display_full(buf)

    def display_partial(self, buf) -> None:
        buf = list(buf)
        if not self._partial_mode:
            self._enter_partial()
        # The base frame is already in RAM 0x26 (display_full writes both RAMs)
        # and the ping-pong keeps it current -- only the new frame is written.
        self._set_cursor(0, 0)
        self.command(0x24)
        self.data(buf)
        self._refresh(_UPDATE_PARTIAL_LUT)
        self._last_buf = buf
