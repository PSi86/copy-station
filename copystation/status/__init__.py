"""Status indication of the copy station.

The indication is encapsulated behind a thin abstraction, so the daemon only
calls ``set_state(State.X)`` and knows nothing about the concrete hardware.
Concrete backends (LED, buzzer, WS2812) live in their own modules and are only
imported when the configuration actually requires them -- this way the service
runs on the dev machine without ``libgpiod`` using just the log backend.
"""

from __future__ import annotations

import enum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..config import Config


class State(enum.Enum):
    """Steady operating states that are signalled to the outside."""

    READY = "ready"
    DETECTING = "detecting"
    COPYING = "copying"
    ERROR = "error"
    SUCCESS = "success"  # short confirmation pulse after a successful transfer


class Event(enum.Enum):
    """One-shot moments worth a brief, unmistakable signal.

    Unlike a :class:`State` (a steady phase the indicator keeps showing), an
    ``Event`` plays a transient animation once and then hands the indicator back
    to the current state. See ``status.effects`` for the shared timing.
    """

    DEVICE_DETECTED = "device_detected"  # a new eligible volume was recognised
    SOURCE_EMPTY = "source_empty"        # a source is connected but has nothing to copy
    SERVICE_STARTED = "service_started"  # the daemon just started -> a brief boot sweep


class StatusIndicator:
    """Base class / no-op backend.

    Concrete backends override ``set_state`` and optionally ``signal`` / ``close``.
    """

    def set_state(self, state: State) -> None:  # pragma: no cover - no-op
        pass

    def set_progress(self, fraction: float) -> None:  # pragma: no cover - no-op
        """Report copy progress (0.0..1.0). Only progress-aware backends use it."""
        pass

    def set_fill(self, fraction: float, sticky: bool = False) -> None:  # pragma: no cover - no-op
        """Report the detected device's fill level (0.0..1.0), shown as a gauge
        while detecting. ``sticky`` keeps the gauge up until the state changes
        (used just before a copy, so it stays visible until the copy bar takes
        over); otherwise it is a brief readout. Only bar-style backends use it."""
        pass

    def signal(self, event: Event) -> None:  # pragma: no cover - no-op
        """Fire a one-shot transient effect. Backends that support animation
        override this; the rest safely ignore it."""
        pass

    def close(self) -> None:  # pragma: no cover - no-op
        pass


class CompositeIndicator(StatusIndicator):
    """Forwards every state to multiple backends.

    A failure of a single backend must not stop operation -- the data transfer
    is more important than the indication.
    """

    def __init__(self, backends: list[StatusIndicator]):
        self._backends = backends

    def set_state(self, state: State) -> None:
        for backend in self._backends:
            try:
                backend.set_state(state)
            except Exception:  # pragma: no cover - indication must never crash
                pass

    def set_progress(self, fraction: float) -> None:
        for backend in self._backends:
            try:
                backend.set_progress(fraction)
            except Exception:  # pragma: no cover - indication must never crash
                pass

    def set_fill(self, fraction: float, sticky: bool = False) -> None:
        for backend in self._backends:
            try:
                backend.set_fill(fraction, sticky)
            except Exception:  # pragma: no cover - indication must never crash
                pass

    def signal(self, event: Event) -> None:
        for backend in self._backends:
            try:
                backend.signal(event)
            except Exception:  # pragma: no cover - indication must never crash
                pass

    def close(self) -> None:
        for backend in self._backends:
            try:
                backend.close()
            except Exception:  # pragma: no cover
                pass


def build_indicator(config: "Config") -> StatusIndicator:
    """Build a (composite) status backend from the configuration.

    If initialising a hardware backend fails (e.g. ``libgpiod`` missing on the
    dev machine), it is skipped instead of preventing startup. If no backend
    remains, a no-op is used.
    """
    status_cfg = config.get("status", {})
    names = status_cfg.get("backends", ["log"])
    backends: list[StatusIndicator] = []

    for name in names:
        try:
            backends.append(_create_backend(name, status_cfg))
        except Exception as exc:
            # Intentionally only warn; the log backend itself should always work.
            import logging

            logging.getLogger("copystation.status").warning(
                "Status backend '%s' could not be initialised: %s",
                name,
                exc,
            )

    if not backends:
        return StatusIndicator()
    if len(backends) == 1:
        return backends[0]
    return CompositeIndicator(backends)


def _create_backend(name: str, status_cfg: dict) -> StatusIndicator:
    if name == "log":
        from .log_backend import LogBackend

        return LogBackend()
    if name == "led":
        from .led_backend import LedBackend

        return LedBackend(status_cfg.get("led", {}))
    if name == "buzzer":
        from .buzzer_backend import BuzzerBackend

        return BuzzerBackend(status_cfg.get("buzzer", {}))
    if name == "ws2812":
        from .ws2812_backend import Ws2812Backend

        return Ws2812Backend(status_cfg.get("ws2812", {}))
    if name == "grove_led_bar":
        from .grove_led_bar import GroveLedBarBackend

        return GroveLedBarBackend(status_cfg.get("grove_led_bar", {}))
    raise ValueError(f"Unknown status backend: {name}")
