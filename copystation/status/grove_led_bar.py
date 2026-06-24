"""Status on a Seeed Grove LED Bar v2.0 (MY9221 driver).

The MY9221 is NOT an I2C device -- it uses a proprietary 2-wire serial protocol
(DI = data, DCKI = clock). We bit-bang it over two GPIO lines via libgpiod. The
protocol is timing-uncritical (no minimum clock, edge-based latch), so Python
scheduler jitter delays but never corrupts a frame.

Behaviour:
* During a copy (``COPYING``): light segments ``1..segments_for(progress)`` and
  blink the whole lit pattern at 10 Hz (50 ms on / 50 ms off) to signal activity.
* Detecting (``DETECTING``): a STEADY fill gauge of the detected device
  (``set_fill``) -- segments ``1..segments_for(fill)``, at least one -- shown as a
  brief ~3 s readout, after which the bar rests until the next event.
* Other idle phases: a single steady segment by phase -- Ready = green
  (segment 3), Error = red (segment 1). Success = short green blink on segment 3.

One-shot :class:`Event` signals overlay a brief animation (see ``status.effects``)
and then resume the steady state. The bar is single-colour, so both use the whole
bar: ``DEVICE_DETECTED`` flashes all segments twice ("a volume was recognised");
``SOURCE_EMPTY`` holds all segments steady for a few seconds ("nothing to copy").
Both stay distinct from the partial bars above.

The exact latch timing and the segment-to-channel orientation must be validated
on the hardware (see the plan's open points).
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
)

# Number of segments on the bar.
SEGMENT_COUNT = 10

# Per-segment "on" / "off" 16-bit grayscale values. 0xFFFF is full-on in both
# the 8-bit and 16-bit grayscale modes, 0x0000 is off -- safe for plain on/off.
_ON = 0xFFFF
_OFF = 0x0000

# MY9221 command word: 0x0000 selects the default mode.
_CMD = 0x0000

# Idle phase -> which single segment (1-based) is lit steady. DETECTING is not
# here -- it shows a fill gauge (segments 1..n), not a single segment.
_IDLE_SEGMENT = {
    State.READY: 3,  # green
    State.ERROR: 1,  # red
}


def segments_for(progress: float) -> int:
    """Map a progress fraction (0.0..1.0) to a number of lit segments (0..10)."""
    if progress <= 0.0:
        return 0
    if progress >= 1.0:
        return SEGMENT_COUNT
    return max(0, min(SEGMENT_COUNT, int(progress * SEGMENT_COUNT + 0.5)))


class GroveLedBarBackend(StatusIndicator):
    def __init__(self, cfg: dict) -> None:
        from .gpio import open_output_lines

        chip = cfg.get("gpiochip", "gpiochip0")
        clock_off = cfg.get("clock_line")
        data_off = cfg.get("data_line")
        if clock_off is None or data_off is None:
            raise ValueError("grove_led_bar.clock_line and data_line must be set")

        # If True, segment 1 is the opposite physical end (orientation fix).
        self._reverse = bool(cfg.get("reverse", False))

        self._clock_off = int(clock_off)
        self._data_off = int(data_off)
        self._gpio = open_output_lines(
            chip, [self._clock_off, self._data_off], "copystation"
        )

        self._lock = threading.Lock()
        self._phase = State.READY
        self._progress = 0.0
        self._fill = 0.0
        self._fill_shown_at: float | None = None  # when the gauge first appeared
        self._last_levels: list[int] | None = None
        self._clock_state = 0
        self._transients = TransientQueue()

        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    # ----- StatusIndicator interface -------------------------------------------

    def set_state(self, state: State) -> None:
        with self._lock:
            self._phase = state

    def set_progress(self, fraction: float) -> None:
        with self._lock:
            self._progress = fraction

    def set_fill(self, fraction: float) -> None:
        with self._lock:
            self._fill = fraction
            self._fill_shown_at = None  # restart the brief gauge window

    def signal(self, event: Event) -> None:
        self._transients.push(event)

    def close(self) -> None:
        self._stop.set()
        self._thread.join(timeout=1.0)
        try:
            self._render([_OFF] * SEGMENT_COUNT)
        except Exception:  # pragma: no cover
            pass
        self._gpio.release()

    # ----- render loop ---------------------------------------------------------

    def _run(self) -> None:
        blink_on = True
        while not self._stop.is_set():
            # A queued one-shot effect takes over the bar until it finishes.
            if self._play_transient():
                blink_on = True
                continue

            now = time.monotonic()
            with self._lock:
                phase = self._phase
                progress = self._progress
                fill = self._fill
                fill_elapsed = 0.0
                if phase is State.DETECTING:
                    if self._fill_shown_at is None:
                        self._fill_shown_at = now
                    fill_elapsed = now - self._fill_shown_at

            if phase is State.COPYING:
                count = segments_for(progress)
                levels = self._first_n(count) if blink_on else [_OFF] * SEGMENT_COUNT
                self._render(levels)
                blink_on = not blink_on
                time.sleep(0.05)  # 10 Hz toggle
            elif phase is State.DETECTING:
                # Steady fill gauge (at least one segment), shown only briefly.
                if fill_gauge_visible(fill_elapsed):
                    self._render(self._first_n(max(1, segments_for(fill))))
                else:
                    self._render([_OFF] * SEGMENT_COUNT)
                blink_on = True
                time.sleep(0.05)
            elif phase is State.SUCCESS:
                levels = self._single(3) if blink_on else [_OFF] * SEGMENT_COUNT
                self._render(levels)
                blink_on = not blink_on
                time.sleep(0.1)
            else:
                segment = _IDLE_SEGMENT.get(phase)
                self._render(self._single(segment) if segment else [_OFF] * SEGMENT_COUNT)
                blink_on = True
                time.sleep(0.05)

    def _play_transient(self) -> bool:
        """Render the active one-shot effect, if any. Returns True while playing."""
        now = time.monotonic()
        while True:
            cur = self._transients.current(now)
            if cur is None:
                return False
            event, elapsed = cur
            lit, done = effect_phase(event, elapsed)
            if done:
                self._transients.finish()
                continue
            # Single-colour bar: every one-shot effect uses the whole bar.
            self._render([_ON] * SEGMENT_COUNT if lit else [_OFF] * SEGMENT_COUNT)
            time.sleep(EFFECT_TICK_SECONDS)
            return True

    def _first_n(self, n: int) -> list[int]:
        return [_ON if i < n else _OFF for i in range(SEGMENT_COUNT)]

    def _single(self, segment_1based: int) -> list[int]:
        levels = [_OFF] * SEGMENT_COUNT
        if 1 <= segment_1based <= SEGMENT_COUNT:
            levels[segment_1based - 1] = _ON
        return levels

    # ----- MY9221 bit-bang -----------------------------------------------------

    def _render(self, levels: list[int]) -> None:
        if levels == self._last_levels:
            return
        self._last_levels = list(levels)

        ordered = list(reversed(levels)) if self._reverse else levels
        # MY9221 has 12 channels; the bar uses the first 10, the rest stay off.
        channels = ordered + [_OFF, _OFF]

        self._send16(_CMD)
        for value in channels:
            self._send16(value)
        self._latch()

    def _send16(self, value: int) -> None:
        for i in range(15, -1, -1):
            self._gpio.set(self._data_off, bool((value >> i) & 1))
            self._clock_state ^= 1
            self._gpio.set(self._clock_off, bool(self._clock_state))

    def _latch(self) -> None:
        # Internal-latch sequence: pull data low, then toggle it four times.
        self._gpio.set(self._data_off, False)
        time.sleep(0.0000002)  # ~200 ns
        for _ in range(4):
            self._gpio.set(self._data_off, True)
            self._gpio.set(self._data_off, False)
