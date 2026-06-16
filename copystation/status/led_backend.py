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

from . import State, StatusIndicator

# Mapping state -> (ready_led, busy_led, error_led, blink?).
# True = on, False = off. blink toggles the "on" LEDs periodically.
_PATTERN: dict[State, tuple[bool, bool, bool, bool]] = {
    State.READY: (True, False, False, False),
    State.DETECTING: (True, True, False, True),
    State.COPYING: (False, True, False, True),
    State.SUCCESS: (True, False, False, False),
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
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def set_state(self, state: State) -> None:
        with self._lock:
            self._current = state

    def _apply(self, ready: bool, busy: bool, error: bool) -> None:
        mapping = {"ready": ready, "busy": busy, "error": error}
        for role, offset in self._role_offset.items():
            self._gpio.set(offset, mapping[role])

    def _run(self) -> None:
        phase = True
        while not self._stop.is_set():
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

    def close(self) -> None:
        self._stop.set()
        self._thread.join(timeout=1.0)
        self._gpio.release()
