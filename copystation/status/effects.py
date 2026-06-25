"""One-shot transient status effects (signalled, not steady states).

Steady ``State`` values (READY/DETECTING/...) describe what the station *is*
doing and are rendered continuously. Some moments instead deserve a brief,
unmistakable *signal*: a volume was just recognised, or a connected source has
nothing to copy. Those are modelled as :class:`Event` signals that play once and
then hand the indicator back to whatever steady state is current.

The timing here is medium-independent, so every backend animates the same way:

* :func:`effect_phase` says whether the indicator should be lit this instant and
  whether the effect is over -- a pure function (like ``leds_for``), so it is
  trivially testable.
* :class:`TransientQueue` sequences queued effects for a backend's render
  thread.

Each backend maps "lit" to its own medium (an LED colour, a bar of segments, a
single GPIO line), so the *vocabulary* stays consistent across hardware:

* ``DEVICE_DETECTED``  -- two quick flashes, the moment a volume is recognised.
* ``SOURCE_EMPTY``     -- a steady several-second hold, "nothing to copy".
* ``SERVICE_STARTED``  -- a one-pass wipe up the bar when the daemon starts.
"""

from __future__ import annotations

import threading
from collections import deque
from typing import Optional, Tuple

from . import Event

# Device-detected: two quick flashes -- a short, unmistakable "recognised", after
# which the steady state takes over with the device's fill gauge.
DETECT_FLASHES = 2
DETECT_ON = 0.12   # s lit per flash
DETECT_OFF = 0.12  # s dark per flash

# Empty source: hold steady long enough to be unmistakable ("nothing to copy").
EMPTY_HOLD_SECONDS = 5.0

# Service started: a quick one-pass "wipe" up the bar, so a (re)start of the
# daemon is visible at a glance before it settles into READY.
STARTUP_SWEEP_SECONDS = 0.7

# The post-detection fill gauge is a brief readout: show it for this long, then
# let the bar rest (off) until the next event.
FILL_GAUGE_SECONDS = 3.0

# How often a render thread should re-sample an effect (crisp enough for the
# flash above without busy-spinning).
EFFECT_TICK_SECONDS = 0.02

# Cap on pending effects: a burst of detections must not back the indicator up
# for minutes. Signals beyond the cap are dropped.
MAX_QUEUED_EFFECTS = 6


def effect_phase(event: Event, elapsed: float) -> Tuple[bool, bool]:
    """Return ``(lit, done)`` for a one-shot effect ``elapsed`` seconds in.

    ``lit``  -- whether the indicator should show the effect's colour right now.
    ``done`` -- whether the effect has finished (the caller drops it then).

    Medium-independent: the backend decides what "lit" looks like (a colour, a
    set of segments, a GPIO line).
    """
    if event is Event.DEVICE_DETECTED:
        period = DETECT_ON + DETECT_OFF
        if elapsed >= period * DETECT_FLASHES:
            return False, True
        return (elapsed % period) < DETECT_ON, False
    if event is Event.SOURCE_EMPTY:
        if elapsed >= EMPTY_HOLD_SECONDS:
            return False, True
        return True, False
    if event is Event.SERVICE_STARTED:
        # Always "lit" while it runs; the backend draws the growing wipe itself
        # from ``elapsed`` (see :func:`startup_sweep_count`).
        return True, elapsed >= STARTUP_SWEEP_SECONDS
    return False, True  # unknown -> nothing to play


def startup_sweep_count(elapsed: float, count: int) -> int:
    """LEDs/segments lit so far in the one-pass startup wipe (grows 1 -> count)."""
    if elapsed <= 0.0:
        return 1
    frac = min(1.0, elapsed / STARTUP_SWEEP_SECONDS)
    return max(1, min(count, round(frac * count)))


def fill_gauge_visible(elapsed: float) -> bool:
    """True while the post-detection fill gauge should still be shown.

    ``elapsed`` is seconds since the gauge first appeared (after the detection
    blink). Once it lapses the bar rests until the next event.
    """
    return elapsed < FILL_GAUGE_SECONDS


class TransientQueue:
    """Thread-safe FIFO of one-shot effects with a single active one.

    A backend's render thread calls :meth:`current` each tick to learn the
    active effect and how long it has been running, and :meth:`finish` once an
    effect ends. Producers call :meth:`push` from any thread.
    """

    def __init__(self, max_queued: int = MAX_QUEUED_EFFECTS) -> None:
        self._queue: "deque[Event]" = deque()
        self._active: Optional[Tuple[Event, float]] = None
        self._max = max_queued
        self._lock = threading.Lock()

    def push(self, event: Event) -> None:
        with self._lock:
            if len(self._queue) < self._max:
                self._queue.append(event)

    def current(self, now: float) -> Optional[Tuple[Event, float]]:
        """Activate the next queued effect if idle; return ``(event, elapsed)``.

        ``now`` is a monotonic timestamp. Returns ``None`` when there is nothing
        to show.
        """
        with self._lock:
            if self._active is None and self._queue:
                self._active = (self._queue.popleft(), now)
            if self._active is None:
                return None
            event, start = self._active
            return event, now - start

    def finish(self) -> None:
        """Drop the active effect so the next :meth:`current` picks the next one."""
        with self._lock:
            self._active = None

    def __len__(self) -> int:  # pragma: no cover - convenience
        with self._lock:
            return len(self._queue) + (1 if self._active is not None else 0)
