"""Audible status signals via a piezo buzzer (GPIO through libgpiod).

Only state transitions with a signal character are sounded:
- SUCCESS: a short double beep (transfer done)
- ERROR:   three long beeps

The beeps run in a background thread so ``set_state`` does not block. ``line``
(line offset) must be set in config.yaml.
"""

from __future__ import annotations

import threading

from . import State, StatusIndicator

# Beep pattern per state as a list of (on_seconds, pause_seconds).
_BEEPS: dict[State, list[tuple[float, float]]] = {
    State.SUCCESS: [(0.08, 0.08), (0.08, 0.0)],
    State.ERROR: [(0.4, 0.2), (0.4, 0.2), (0.4, 0.0)],
}


class BuzzerBackend(StatusIndicator):
    def __init__(self, cfg: dict) -> None:
        import gpiod  # lazy: only present on the Cubie

        offset = cfg.get("line")
        if offset is None:
            raise ValueError("buzzer.line is not configured")

        chip_name = cfg.get("gpiochip", "gpiochip0")
        self._chip = gpiod.Chip(chip_name)
        self._line = self._chip.get_line(int(offset))
        self._line.request(consumer="copystation", type=gpiod.LINE_REQ_DIR_OUT)
        self._line.set_value(0)

        self._last: State | None = None
        self._lock = threading.Lock()

    def set_state(self, state: State) -> None:
        pattern = _BEEPS.get(state)
        if pattern is None or state is self._last:
            self._last = state
            return
        self._last = state
        threading.Thread(target=self._play, args=(pattern,), daemon=True).start()

    def _play(self, pattern: list[tuple[float, float]]) -> None:
        import time

        with self._lock:
            for on, pause in pattern:
                self._line.set_value(1)
                time.sleep(on)
                self._line.set_value(0)
                if pause:
                    time.sleep(pause)

    def close(self) -> None:
        try:
            self._line.set_value(0)
            self._line.release()
        except Exception:  # pragma: no cover
            pass
