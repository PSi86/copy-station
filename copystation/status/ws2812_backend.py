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
* Detecting (``DETECTING``): all LEDs form a STEADY white gauge of the detected
  device's fill level (``set_fill``) -- same bar idea as the copy progress, but
  white and not blinking. At least one LED is lit so "detected" reads even for a
  near-empty volume. The gauge is a brief readout: it shows for ~3 s after a
  device is detected, then a slow MAGENTA pulse marks "detecting, waiting"
  (deliberately far from the green idle) -- unless the gauge is ``sticky`` (set
  just before a copy), when it stays up until the copy bar takes over.
* Error (``ERROR``): ALL LEDs blink red -- impossible to miss, e.g. when a device
  is pulled mid-copy.
* Ready: the first LED is lit steady green. Success = a short green blink on it.

On top of those steady states, one-shot :class:`Event` signals overlay a brief
animation (see ``status.effects``) and then hand the strip back to the current
state:

* ``DEVICE_DETECTED``: all LEDs flash bright green twice -- an unmistakable "a
  volume was recognised", after which the white fill gauge above takes over.
* ``SOURCE_EMPTY``: all LEDs hold solid blue for a few seconds -- "a source is
  connected but there is nothing to copy". (Distinct from the copy colour, which
  is a *partial, blinking* progress bar rather than a solid hold.)
* ``SERVICE_STARTED``: a quick cyan wipe up the strip when the daemon starts.

On shutdown ``close()`` switches every LED off.

Without ``spidev`` / matching hardware the constructor raises -- the factory
caller then skips the backend.
"""

from __future__ import annotations

import threading
import time

from . import Event, State, StatusIndicator
from .effects import (
    EFFECT_TICK_SECONDS,
    TransientQueue,
    effect_phase,
    fill_gauge_visible,
    startup_sweep_count,
)

# Number of LEDs the feature supports at most.
MAX_LEDS = 10

# Raw zero bytes wrapped around every frame to drive MOSI actively low. 256 bytes
# is ~850 us at 2.4 MHz, well past the WS2812 reset time (>50 us; ~280 us on a
# WS2812B). Sent BEFORE the data it absorbs the SPI start-of-transfer glitch that
# otherwise corrupts the FIRST LED (a slightly-off idle colour, and the first LED
# not going fully dark on OFF); sent AFTER the data it is the reset that latches
# the frame so it is displayed. Both matter and neither depends on the board's
# (uncontrolled) idle line level, unlike a plain sleep.
_RESET_BYTES = 256

# (R, G, B) of an unlit pixel.
_OFF = (0, 0, 0)

# Idle status colour shown on the first LED, per phase. DETECTING (white fill
# gauge) and ERROR (all LEDs blink red) have their own rendering, so only READY
# is a single steady colour here.
_IDLE_COLOR: dict[State, tuple[int, int, int]] = {
    State.READY: (0, 60, 0),   # green
}

# Error: ALL LEDs blink red -- impossible to miss (e.g. a device pulled mid-copy).
_ERROR_COLOR = (90, 0, 0)  # bright red

# Service started: a cyan wipe up the strip -- a colour used nowhere else, so a
# (re)start is unmistakable.
_STARTUP_COLOR = (0, 50, 50)  # cyan

# Colour of the progress bar during a copy.
_COPY_COLOR = (0, 0, 60)  # blue

# Colour of the fill gauge shown while detecting -- white, unmistakably different
# from the green "ready" colour and the blue copy bar.
_FILL_COLOR = (50, 50, 50)  # white

# Colour of the "detecting, waiting" indicator shown after the fill gauge -- a
# magenta pulse, deliberately far from the green idle so the two never look alike.
_DETECTING_COLOR = (50, 0, 50)  # magenta

# Confirmations are a touch brighter (~90) so they read as a deliberate event,
# not a resting colour.
_SUCCESS_COLOR = (0, 90, 0)   # bright green -- transfer done
_DETECT_COLOR = (0, 90, 0)    # bright green -- one-shot "device detected" flash
_EMPTY_COLOR = (0, 0, 90)     # bright blue  -- one-shot "source empty" hold


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
    def __init__(self, cfg: dict, start: bool = True) -> None:
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
        self._fill = 0.0
        self._fill_sticky = False                 # keep the gauge up until COPYING
        self._fill_shown_at: float | None = None  # when the gauge first appeared
        self._last_pixels: list[tuple[int, int, int]] | None = None
        self._transients = TransientQueue()

        self._stop = threading.Event()
        # ``start=False`` opens the hardware without the render loop -- used by the
        # `leds-off` command, which then close()s to send a single OFF frame
        # without first flashing the idle colour.
        self._thread: threading.Thread | None = None
        if start:
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

    def set_fill(self, fraction: float, sticky: bool = False) -> None:
        with self._lock:
            self._fill = fraction
            self._fill_sticky = sticky
            # Restart the brief gauge window at the next detecting render.
            self._fill_shown_at = None

    def signal(self, event: Event) -> None:
        # Momentary effects are queued; the render loop plays them in order and
        # then resumes the steady state.
        self._transients.push(event)

    def close(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=1.0)
        try:
            # _render wraps the OFF frame in leading+trailing low resets, so it is
            # both glitch-free (first LED really goes dark) and latched (displayed).
            self._last_pixels = None  # force the OFF frame past the de-dup
            self._render([_OFF] * self._led_count)
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
            # A queued one-shot effect (detection blink / empty-source hold) takes
            # over the strip until it finishes, then the steady state resumes.
            if self._play_transient():
                blink_on = True
                continue

            now = time.monotonic()
            with self._lock:
                phase = self._phase
                progress = self._progress
                fill = self._fill
                sticky = self._fill_sticky
                fill_elapsed = 0.0
                if phase is State.DETECTING:
                    if self._fill_shown_at is None:
                        self._fill_shown_at = now
                    fill_elapsed = now - self._fill_shown_at

            if phase is State.COPYING:
                # At least one LED so the very start of the copy (0 %) is not a
                # dark strip that reads as a pause before the bar grows.
                count = max(1, leds_for(progress, self._led_count))
                pixels = self._bar(count, _COPY_COLOR) if blink_on else self._all_off()
                self._render(pixels)
                blink_on = not blink_on
                time.sleep(0.05)  # 10 Hz toggle
            elif phase is State.DETECTING:
                if sticky or fill_gauge_visible(fill_elapsed):
                    # White fill gauge: a brief readout, or held until the copy bar
                    # takes over when sticky (a copy is imminent) -- no gap.
                    self._render(self._fill_pixels(fill))
                    blink_on = True
                    time.sleep(0.05)
                else:
                    # Gauge done, still waiting -> a slow magenta pulse, clearly
                    # distinct from the green idle (not just a dark strip).
                    self._render(self._single(_DETECTING_COLOR) if blink_on else self._all_off())
                    blink_on = not blink_on
                    time.sleep(0.4)  # ~1.25 Hz
            elif phase is State.ERROR:
                # All LEDs blink red until the situation is cleared (devices removed).
                self._render([_ERROR_COLOR] * self._led_count if blink_on else self._all_off())
                blink_on = not blink_on
                time.sleep(0.25)  # ~2 Hz alarm blink (distinct from the 10 Hz copy)
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

    def _play_transient(self) -> bool:
        """Render the active one-shot effect, if any.

        Drains finished effects and renders the first live one for a single tick.
        Returns True while an effect is playing (the caller then skips the
        steady-state rendering), False when the queue is empty.
        """
        now = time.monotonic()
        while True:
            cur = self._transients.current(now)
            if cur is None:
                return False
            event, elapsed = cur
            lit, done = effect_phase(event, elapsed)
            if done:
                self._transients.finish()
                continue  # try the next queued effect immediately
            self._render(self._effect_pixels(event, lit, elapsed))
            time.sleep(EFFECT_TICK_SECONDS)
            return True

    def _fill_pixels(self, fill: float) -> list[tuple[int, int, int]]:
        """Steady white fill gauge; at least one LED lit so a near-empty volume
        still reads as 'detected'."""
        count = max(1, leds_for(fill, self._led_count))
        return self._bar(count, _FILL_COLOR)

    def _effect_pixels(self, event: Event, lit: bool, elapsed: float) -> list[tuple[int, int, int]]:
        """Pixels for a one-shot effect (green flash / blue hold / startup wipe)."""
        if event is Event.DEVICE_DETECTED:
            return [_DETECT_COLOR if lit else _OFF] * self._led_count
        if event is Event.SOURCE_EMPTY:
            return [_EMPTY_COLOR if lit else _OFF] * self._led_count
        if event is Event.SERVICE_STARTED:
            return self._bar(startup_sweep_count(elapsed, self._led_count), _STARTUP_COLOR)
        return [_OFF] * self._led_count

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
        # Wrap the data in a low period before and after: the leading low absorbs
        # the SPI start-of-transfer glitch that corrupts the first LED, the
        # trailing low is the reset that latches the frame. See ``_RESET_BYTES``.
        reset = [0] * _RESET_BYTES
        self._spi.xfer2(reset + encode_pixels(pixels) + reset)
