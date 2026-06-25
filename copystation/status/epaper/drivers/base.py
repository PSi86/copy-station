"""Transport primitives shared by all e-paper controller drivers.

HARDWARE-DEPENDENT. An e-paper panel is driven over SPI (command/data bytes)
plus a handful of GPIO lines: DC (command vs data), RST (reset), BUSY (an input
the controller drives while it refreshes) and optionally PWR (panel power enable)
and a GPIO chip-select. The SPI and GPIO handles are injected, so a driver's
command sequence can be unit-tested with fakes -- no real panel needed.

Concrete controllers (``ssd168x``, ``ssd1677``) subclass :class:`EpaperDriver`
and implement ``init`` / ``display_full`` / ``display_partial`` / ``clear`` /
``sleep`` on top of these primitives.
"""

from __future__ import annotations

import time

# spidev caps a single transfer (commonly 4096 bytes); a full frame is larger,
# so frame data is sent in chunks below this.
_SPI_CHUNK = 2048


class EpaperDriver:
    def __init__(
        self,
        *,
        width: int,
        height: int,
        spi,
        gpio_out,
        gpio_in,
        dc: int,
        rst: int,
        busy: int,
        pwr: int | None = None,
        cs: int | None = None,
        busy_timeout: float = 30.0,
    ) -> None:
        self.width = int(width)
        self.height = int(height)
        self.bytes_per_row = (self.width + 7) // 8
        self.buffer_size = self.bytes_per_row * self.height

        self._spi = spi
        self._out = gpio_out
        self._in = gpio_in
        self._dc = int(dc)
        self._rst = int(rst)
        self._busy = int(busy)
        self._pwr = None if pwr is None else int(pwr)
        self._cs = None if cs is None else int(cs)
        self._busy_timeout = busy_timeout

    # ----- transport primitives ------------------------------------------------

    @staticmethod
    def _sleep_ms(ms: float) -> None:
        time.sleep(ms / 1000.0)

    def reset(self) -> None:
        """Power the panel (if a PWR pin is wired) and pulse RST."""
        if self._pwr is not None:
            self._out.set(self._pwr, True)
            self._sleep_ms(10)
        self._out.set(self._rst, True)
        self._sleep_ms(20)
        self._out.set(self._rst, False)
        self._sleep_ms(5)
        self._out.set(self._rst, True)
        self._sleep_ms(20)

    def _select(self, low: bool) -> None:
        if self._cs is not None:
            self._out.set(self._cs, not low)  # CS is active-low

    def command(self, value: int) -> None:
        self._out.set(self._dc, False)  # DC low = command
        self._select(True)
        self._spi.xfer2([value & 0xFF])
        self._select(False)

    def data(self, values) -> None:
        self._out.set(self._dc, True)  # DC high = data
        buf = list(values)
        for i in range(0, len(buf), _SPI_CHUNK):
            self._select(True)
            self._spi.xfer2(buf[i : i + _SPI_CHUNK])
            self._select(False)

    def wait_busy(self) -> None:
        """Block until the controller releases BUSY (or the timeout lapses)."""
        deadline = time.monotonic() + self._busy_timeout
        while self._in.get(self._busy):  # get() already honours busy polarity
            if time.monotonic() > deadline:
                return
            self._sleep_ms(10)

    def power_off_panel(self) -> None:
        if self._pwr is not None:
            self._out.set(self._pwr, False)

    def close(self) -> None:
        try:
            self._spi.close()
        except Exception:  # pragma: no cover - best effort
            pass
        for handle in (self._out, self._in):
            try:
                handle.release()
            except Exception:  # pragma: no cover
                pass

    # ----- panel operations (subclasses implement) -----------------------------

    def init(self) -> None:  # pragma: no cover - interface
        raise NotImplementedError

    def display_full(self, buf) -> None:  # pragma: no cover - interface
        raise NotImplementedError

    def display_partial(self, buf) -> None:  # pragma: no cover - interface
        raise NotImplementedError

    def clear(self) -> None:  # pragma: no cover - interface
        raise NotImplementedError

    def sleep(self) -> None:  # pragma: no cover - interface
        raise NotImplementedError
