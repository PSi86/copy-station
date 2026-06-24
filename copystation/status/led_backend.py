"""Status via individual GPIO LEDs through libgpiod.

Expects three optional lines (``ready``, ``busy``, ``error``) on a gpiochip.
Which gpiochip and which line offsets are correct must be determined on the
Cubie with ``gpiodetect`` / ``gpioinfo`` and entered into config.yaml -- see
``status.led`` in config.example.yaml.

Blinking states are driven by a background thread.
"""

from __future__ import annotations

import threading
import time

from . import Event, State, StatusIndicator
from .effects import EFFECT_TICK_SECONDS, TransientQueue, effect_phase

# Mapping state -> (ready_led, busy_led, error_led, blink?).
# True = on, False = off. blink toggles the "on" LEDs periodically.
#
# Every state is deliberately distinct: READY is the only steady single LED;
# SUCCESS reuses the green 'ready' LED but *blinks* it, so a finished copy is not
# mistaken for plain idle; DETECTING (ready+busy) and COPYING (busy only) differ
# by the ready LED.
_PATTERN: dict[State, tuple[bool, bool, bool, bool]] = {
    State.READY: (True, False, False, False),
    State.DETECTING: (True, True, False, True),
    State.COPYING: (False, True, False, True),
    State.SUCCESS: (True, False, False, True),   # green, blinking (vs. READY steady)
    State.ERROR: (False, False, True, True),
}


class LedBackend(StatusIndicator):
    def __init__(self, cfg: dict) -> None:
        from .gpio import open_output_lines

        chip = cfg.get("gpiochip", "gpiochip0")
        lines = cfg.get("lines", {})

        # role -> line offset, for the roles that are actually configured.
        self._role_offset: dict[str, int] = {
            role: int(lines[role])
            for role in ("ready", "busy", "error")
            if lines.get(role) is not None
        }
        self._gpio = open_output_lines(
            chip, list(self._role_offset.values()), "copystation"
        )

        self._lock = threading.Lock()
        self._current = State.READY
        self._transients = TransientQueue()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def set_state(self, state: State) -> None:
        with self._lock:
            self._current = state

    def signal(self, event: Event) -> None:
        self._transients.push(event)

    def _apply(self, ready: bool, busy: bool, error: bool) -> None:
        mapping = {"ready": ready, "busy": busy, "error": error}
        for role, offset in self._role_offset.items():
            self._gpio.set(offset, mapping[role])

    def _run(self) -> None:
        phase = True
        while not self._stop.is_set():
            # A queued one-shot effect takes over the LEDs until it finishes.
            if self._play_transient():
                phase = True
                continue

            with self._lock:
                ready, busy, error, blink = _PATTERN.get(
                    self._current, _PATTERN[State.READY]
                )
            if blink and not phase:
                self._apply(False, False, False)
            else:
                self._apply(ready, busy, error)
            phase = not phase
            time.sleep(0.4)

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
            ready, busy, error = self._effect_lines(event, lit)
            self._apply(ready, busy, error)
            time.sleep(EFFECT_TICK_SECONDS)
            return True

    @staticmethod
    def _effect_lines(event: Event, lit: bool) -> tuple[bool, bool, bool]:
        """Map a one-shot effect to (ready, busy, error) for the discrete LEDs.

        No blue LED exists here, so 'source empty' lights all three at once -- a
        pattern no steady state uses -- while 'device detected' flashes the green
        ready LED.
        """
        if event is Event.DEVICE_DETECTED:
            return (lit, False, False)        # flash the green 'ready' LED
        if event is Event.SOURCE_EMPTY:
            return (lit, lit, lit)            # all three -> distinct "attention" hold
        if event is Event.SERVICE_STARTED:
            return (lit, lit, lit)            # all three briefly at startup
        return (False, False, False)

    def close(self) -> None:
        self._stop.set()
        self._thread.join(timeout=1.0)
        self._gpio.release()
