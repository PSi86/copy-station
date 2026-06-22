"""Status via an addressable WS2812B / NeoPixel strip (1-10 LEDs).

HARDWARE-DEPENDENT: WS2812B has a strict ~800 kHz one-wire protocol. We generate
it via the SPI MOSI line, encoding each WS2812 data bit as three SPI bits
(1 -> 110, 0 -> 100) at ~2.4 MHz. Which ``/dev/spidev*`` the strip sits on
depends on the board and must be checked on the hardware.

Behaviour mirrors the Grove LED Bar, adapted to a colour strip. The key
difference: the bar dedicates separate segments to the idle colours, whereas a
WS2812 pixel can show any colour, so a single LED suffices for the status:

* During a copy (``COPYING``): light LEDs ``1..leds_for(progress)`` in the copy
  colour and blink the whole lit pattern at 10 Hz (50 ms on / 50 ms off) to
  signal activity.
* Idle: only the first LED is lit steady in the status colour -- Ready = green,
  Detecting = yellow, Error = red. Success = a short green blink on the first
  LED. The remaining LEDs stay dark until a transfer needs them for the
  progress bar.

Without ``spidev`` / matching hardware the constructor raises -- the factory
caller then skips the backend.
"""

from __future__ import annotations

import threading
import time

from . import State, StatusIndicator

# Number of LEDs the feature supports at most.
MAX_LEDS = 10

# (R, G, B) of an unlit pixel.
_OFF = (0, 0, 0)

# Idle status colour shown on the first LED, per phase.
_IDLE_COLOR: dict[State, tuple[int, int, int]] = {
    State.READY: (0, 40, 0),       # green
    State.DETECTING: (40, 30, 0),  # yellow
    State.ERROR: (80, 0, 0),       # red
}

# Colour of the progress bar during a copy.
_COPY_COLOR = (0, 0, 60)  # blue

# Colour of the success confirmation blink.
_SUCCESS_COLOR = (0, 80, 0)  # bright green


def leds_for(progress: float, led_count: int) -> int:
    """Map a progress fraction (0.0..1.0) to a number of lit LEDs (0..led_count)."""
    if progress <= 0.0:
        return 0
    if progress >= 1.0:
        return led_count
    return max(0, min(led_count, int(progress * led_count + 0.5)))


def encode_pixels(pixels: list[tuple[int, int, int]]) -> list[int]:
    """Encode per-LED (R, G, B) colours into the SPI byte stream for a WS2812.

    WS2812 expects GRB order, MSB first. Each data bit is encoded as three SPI
    bits: 1 -> 110, 0 -> 100. The resulting bitstream is packed into whole bytes
    (9 bytes per LED), so its length is always a multiple of 8.
    """
    bits: list[int] = []
    for r, g, b in pixels:
        for byte in (g, r, b):
            for i in range(7, -1, -1):
                bits.extend((1, 1, 0) if (byte >> i) & 1 else (1, 0, 0))
    out: list[int] = []
    for i in range(0, len(bits), 8):
        value = 0
        for bit in bits[i : i + 8]:
            value = (value << 1) | bit
        out.append(value)
    return out


class Ws2812Backend(StatusIndicator):
    def __init__(self, cfg: dict) -> None:
        # spidev is only meaningfully present on the target boards.
        import spidev  # type: ignore

        led_count = int(cfg.get("led_count", 1))
        if not 1 <= led_count <= MAX_LEDS:
            raise ValueError(
                f"ws2812.led_count must be between 1 and {MAX_LEDS}, got {led_count}"
            )
        self._led_count = led_count

        self._spi = spidev.SpiDev()
        bus, device = self._parse_device(cfg.get("device", "/dev/spidev0.0"))
        self._spi.open(bus, device)
        # 3 SPI bits per WS2812 bit -> ~2.4 MHz SPI gives ~800 kHz data rate.
        self._spi.max_speed_hz = 2_400_000

        self._lock = threading.Lock()
        self._phase = State.READY
        self._progress = 0.0
        self._last_pixels: list[tuple[int, int, int]] | None = None

        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    @staticmethod
    def _parse_device(path: str) -> tuple[int, int]:
        # "/dev/spidev0.0" -> (0, 0)
        tail = path.rsplit("spidev", 1)[-1]
        bus_str, dev_str = tail.split(".")
        return int(bus_str), int(dev_str)

    # ----- StatusIndicator interface -------------------------------------------

    def set_state(self, state: State) -> None:
        with self._lock:
            self._phase = state

    def set_progress(self, fraction: float) -> None:
        with self._lock:
            self._progress = fraction

    def close(self) -> None:
        self._stop.set()
        self._thread.join(timeout=1.0)
        try:
            self._render([_OFF] * self._led_count)  # all LEDs off
        except Exception:  # pragma: no cover
            pass
        try:
            self._spi.close()
        except Exception:  # pragma: no cover
            pass

    # ----- render loop ---------------------------------------------------------

    def _run(self) -> None:
        blink_on = True
        while not self._stop.is_set():
            with self._lock:
                phase = self._phase
                progress = self._progress

            if phase is State.COPYING:
                count = leds_for(progress, self._led_count)
                pixels = self._bar(count, _COPY_COLOR) if blink_on else self._all_off()
                self._render(pixels)
                blink_on = not blink_on
                time.sleep(0.05)  # 10 Hz toggle
            elif phase is State.SUCCESS:
                pixels = self._single(_SUCCESS_COLOR) if blink_on else self._all_off()
                self._render(pixels)
                blink_on = not blink_on
                time.sleep(0.1)
            else:
                color = _IDLE_COLOR.get(phase)
                self._render(self._single(color) if color else self._all_off())
                blink_on = True
                time.sleep(0.05)

    def _all_off(self) -> list[tuple[int, int, int]]:
        return [_OFF] * self._led_count

    def _bar(self, n: int, color: tuple[int, int, int]) -> list[tuple[int, int, int]]:
        return [color if i < n else _OFF for i in range(self._led_count)]

    def _single(self, color: tuple[int, int, int]) -> list[tuple[int, int, int]]:
        pixels = self._all_off()
        pixels[0] = color
        return pixels

    # ----- output --------------------------------------------------------------

    def _render(self, pixels: list[tuple[int, int, int]]) -> None:
        if pixels == self._last_pixels:
            return
        self._last_pixels = list(pixels)
        self._spi.xfer2(encode_pixels(pixels))
