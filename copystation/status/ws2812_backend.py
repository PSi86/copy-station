"""Status via an addressable RGB LED (WS2812 / Neopixel).

HARDWARE-DEPENDENT / yet to be validated: WS2812 has a strict timing protocol.
On the Allwinner A733 it is usually generated via SPI MOSI (each WS2812 bit
encoded as 3 SPI bits). Whether SPI on the Cubie A7S can be used for this and at
which ``/dev/spidev*`` it sits must be checked on the hardware.

This backend is intentionally kept minimal and only active when "ws2812" is
listed in ``status.backends``. Without matching hardware/library the constructor
raises -- the factory caller then skips the backend.
"""

from __future__ import annotations

from . import State, StatusIndicator

# Colour (R, G, B) per state.
_COLORS: dict[State, tuple[int, int, int]] = {
    State.READY: (0, 40, 0),       # green
    State.DETECTING: (40, 30, 0),  # yellow
    State.COPYING: (0, 0, 60),     # blue
    State.SUCCESS: (0, 80, 0),     # bright green
    State.ERROR: (80, 0, 0),       # red
}


class Ws2812Backend(StatusIndicator):
    def __init__(self, cfg: dict) -> None:
        # spidev is only meaningfully present on the Cubie.
        import spidev  # type: ignore

        self._spi = spidev.SpiDev()
        bus, device = self._parse_device(cfg.get("device", "/dev/spidev0.0"))
        self._spi.open(bus, device)
        # 3 SPI bits per WS2812 bit -> ~2.4 MHz SPI gives ~800 kHz data rate.
        self._spi.max_speed_hz = 2_400_000
        self._led_count = int(cfg.get("led_count", 1))

    @staticmethod
    def _parse_device(path: str) -> tuple[int, int]:
        # "/dev/spidev0.0" -> (0, 0)
        tail = path.rsplit("spidev", 1)[-1]
        bus_str, dev_str = tail.split(".")
        return int(bus_str), int(dev_str)

    def set_state(self, state: State) -> None:
        color = _COLORS.get(state, (0, 0, 0))
        self._spi.xfer2(self._encode(color))

    def _encode(self, color: tuple[int, int, int]) -> list[int]:
        # WS2812 expects GRB order, MSB first. Each data bit is encoded as 3 SPI
        # bits: 1 -> 110, 0 -> 100.
        r, g, b = color
        bits: list[int] = []
        for byte in (g, r, b):
            for i in range(7, -1, -1):
                bits.extend((1, 1, 0) if (byte >> i) & 1 else (1, 0, 0))
        bitstream = bits * self._led_count
        # Pack the bitstream into bytes.
        out: list[int] = []
        for i in range(0, len(bitstream) - 7, 8):
            value = 0
            for bit in bitstream[i : i + 8]:
                value = (value << 1) | bit
            out.append(value)
        return out

    def close(self) -> None:
        try:
            # Off (all LEDs black).
            self._spi.xfer2(self._encode((0, 0, 0)))
            self._spi.close()
        except Exception:  # pragma: no cover
            pass
