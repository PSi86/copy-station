"""Single source of truth shared by the daemon, the web interface and the LEDs.

``StationState`` is a thread-safe container for everything the UI and the status
display need: the current phase, copy progress, timing and the storage figures of
both mass storages. It is written by exactly one thread (the device-watcher /
transfer thread) and read by the web server and the LED-bar render thread.

``StatusHub`` is a thin facade the daemon talks to so call sites stay clean: each
mutating call updates the ``StationState`` *and* forwards the relevant bit to the
status indicators (LEDs/buzzer/log).
"""

from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Optional

from .status import State, StatusIndicator

# How many recent action-log entries to keep for the web UI.
MAX_EVENTS = 200


@dataclass
class StorageInfo:
    """Capacity figures of one mass storage, in bytes."""

    label: str = ""
    capacity: int = 0
    used: int = 0
    free: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "capacity": self.capacity,
            "used": self.used,
            "free": self.free,
        }


class StationState:
    """Thread-safe snapshot of what the station is doing right now."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._phase: State = State.READY
        self._progress: float = 0.0  # 0.0 .. 1.0
        self._bytes_done: int = 0
        self._bytes_total: int = 0
        self._started_monotonic: Optional[float] = None
        self._finished_monotonic: Optional[float] = None
        self._transfer_name: str = ""
        self._error: str = ""
        self._source = StorageInfo()
        self._target = StorageInfo()
        self._devices: list[dict[str, Any]] = []
        self._events: deque[dict[str, Any]] = deque(maxlen=MAX_EVENTS)
        self._event_seq = 0

    # ----- mutators (single writer) --------------------------------------------

    def set_phase(self, phase: State) -> None:
        with self._lock:
            self._phase = phase

    def set_error(self, message: str) -> None:
        with self._lock:
            self._error = message
            self._phase = State.ERROR

    def begin_transfer(self, name: str, bytes_total: int) -> None:
        with self._lock:
            self._transfer_name = name
            self._bytes_total = bytes_total
            self._bytes_done = 0
            self._progress = 0.0
            self._error = ""
            self._started_monotonic = time.monotonic()
            self._finished_monotonic = None
            self._phase = State.COPYING

    def update_progress(self, bytes_done: int) -> None:
        with self._lock:
            self._bytes_done = bytes_done
            if self._bytes_total > 0:
                self._progress = min(1.0, bytes_done / self._bytes_total)

    def finish_transfer(self) -> None:
        with self._lock:
            self._progress = 1.0
            self._bytes_done = self._bytes_total
            self._finished_monotonic = time.monotonic()

    def set_storage(self, source: StorageInfo, target: StorageInfo) -> None:
        with self._lock:
            self._source = source
            self._target = target

    def set_devices(self, devices: list[dict[str, Any]]) -> None:
        """Replace the list of currently detected volumes (web UI display)."""
        with self._lock:
            self._devices = list(devices)

    def log_event(self, message: str, level: str = "info") -> None:
        """Append a timestamped entry to the action log (kept across cycles)."""
        with self._lock:
            self._event_seq += 1
            self._events.append(
                {
                    "seq": self._event_seq,
                    "time": time.time(),
                    "level": level,
                    "message": message,
                }
            )

    def reset_to_ready(self) -> None:
        with self._lock:
            self._phase = State.READY
            self._progress = 0.0
            self._bytes_done = 0
            self._bytes_total = 0
            self._started_monotonic = None
            self._finished_monotonic = None
            self._transfer_name = ""
            self._error = ""
            self._source = StorageInfo()
            self._target = StorageInfo()
            self._devices = []

    # ----- read access ---------------------------------------------------------

    @property
    def phase(self) -> State:
        with self._lock:
            return self._phase

    @property
    def progress(self) -> float:
        with self._lock:
            return self._progress

    def _elapsed_locked(self) -> Optional[float]:
        if self._started_monotonic is None:
            return None
        end = self._finished_monotonic or time.monotonic()
        return max(0.0, end - self._started_monotonic)

    def snapshot(self) -> dict[str, Any]:
        """Plain dict for JSON serialisation (computed fields included)."""
        with self._lock:
            elapsed = self._elapsed_locked()
            eta: Optional[float] = None
            speed: Optional[float] = None
            if (
                self._phase is State.COPYING
                and elapsed
                and self._bytes_done > 0
            ):
                speed = self._bytes_done / elapsed
                if speed > 0 and self._bytes_total > 0:
                    eta = max(0.0, (self._bytes_total - self._bytes_done) / speed)
            return {
                "phase": self._phase.value,
                "percent": round(self._progress * 100, 1),
                "bytes_done": self._bytes_done,
                "bytes_total": self._bytes_total,
                "elapsed_seconds": round(elapsed, 1) if elapsed is not None else None,
                "eta_seconds": round(eta, 1) if eta is not None else None,
                "speed_bytes": round(speed) if speed is not None else None,
                "transfer_name": self._transfer_name,
                "error": self._error if self._phase is State.ERROR else "",
                "source": self._source.as_dict(),
                "target": self._target.as_dict(),
                "devices": list(self._devices),
                "events": list(reversed(self._events)),  # newest first
            }


class StatusHub:
    """Facade combining the shared state and the status indicators.

    The daemon mutates the station through this single object; it keeps the
    ``StationState`` (read by web + LED bar) and the indicators (LEDs/buzzer/log)
    in sync.
    """

    def __init__(self, state: StationState, indicator: StatusIndicator) -> None:
        self._state = state
        self._indicator = indicator

    @property
    def state(self) -> StationState:
        return self._state

    def set_phase(self, phase: State) -> None:
        self._state.set_phase(phase)
        self._indicator.set_state(phase)

    def set_error(self, message: str) -> None:
        self._state.set_error(message)
        self._indicator.set_state(State.ERROR)

    def begin_transfer(self, name: str, bytes_total: int) -> None:
        self._state.begin_transfer(name, bytes_total)
        self._indicator.set_state(State.COPYING)
        self._indicator.set_progress(0.0)

    def update_progress(self, bytes_done: int) -> None:
        self._state.update_progress(bytes_done)
        self._indicator.set_progress(self._state.progress)

    def finish_transfer(self) -> None:
        self._state.finish_transfer()
        self._indicator.set_progress(1.0)

    def set_storage(self, source: StorageInfo, target: StorageInfo) -> None:
        self._state.set_storage(source, target)

    def set_devices(self, devices: list[dict]) -> None:
        self._state.set_devices(devices)

    def log_event(self, message: str, level: str = "info") -> None:
        self._state.log_event(message, level)

    def reset_to_ready(self) -> None:
        self._state.reset_to_ready()
        self._indicator.set_state(State.READY)

    def close(self) -> None:
        self._indicator.close()
