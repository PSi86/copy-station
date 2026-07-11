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

from .status import Event, State, StatusIndicator

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
        # Serialises the two *writers* that must never touch the same removable
        # volume at once: a copy evaluation (DeviceWatcher._evaluate) and a video
        # transcode job. Both run as threads in the daemon process, so an
        # in-process lock is enough -- no IPC. Read-only web browsing does NOT
        # take this lock (a read-only mount is safe next to a live writer).
        # Distinct from ``_lock`` (which only guards the snapshot fields below).
        self.operation_lock = threading.Lock()
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
        # Whether the WLAN access point is currently up. Independent of the copy
        # cycle (survives reset_to_ready), so the display keeps showing it.
        self._ap_active = False
        # Whether auto-transcode is currently enabled (a setting, not a phase).
        # Mirrored from the TranscodeManager so the e-paper can show a badge and a
        # button toggle reflects instantly. Survives reset_to_ready.
        self._auto_transcode = False
        # Current video transcode (overrides the copy status while active).
        self._transcode: dict[str, Any] = {"active": False}

    # ----- mutators (single writer) --------------------------------------------

    def set_phase(self, phase: State) -> None:
        with self._lock:
            self._phase = phase

    def set_error(self, message: str) -> None:
        with self._lock:
            self._error = message
            self._phase = State.ERROR
            # Freeze the elapsed clock at the failure point: without this the
            # snapshot keeps counting (end = now) as if the copy were still live.
            if self._started_monotonic is not None and self._finished_monotonic is None:
                self._finished_monotonic = time.monotonic()

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

    def set_ap_active(self, active: bool) -> None:
        """Record whether the WLAN access point is up (shown on display + web)."""
        with self._lock:
            self._ap_active = bool(active)

    def set_auto_transcode(self, active: bool) -> None:
        """Record whether auto-transcode is enabled (e-paper badge + web)."""
        with self._lock:
            self._auto_transcode = bool(active)

    def begin_transcode(self, name: str) -> None:
        """Mark a transcode as running (phase TRANSCODING overrides everything)."""
        with self._lock:
            self._transcode = {
                "active": True,
                "name": name,
                "percent": 0.0,
                "encoder": "",
                "hw": False,
                "input_size": 0,
                "fps": None,
                "started": time.monotonic(),
            }
            self._phase = State.TRANSCODING

    def update_transcode(self, fraction: float, encoder: str = "", hw: bool = False) -> None:
        with self._lock:
            if self._transcode.get("active"):
                self._transcode["percent"] = min(100.0, max(0.0, fraction * 100.0))
                if encoder:
                    self._transcode["encoder"] = encoder
                    self._transcode["hw"] = hw

    def set_transcode_meta(self, input_size: Optional[int] = None, fps: Optional[float] = None) -> None:
        """Record the input size / live fps of the running transcode."""
        with self._lock:
            if self._transcode.get("active"):
                if input_size is not None:
                    self._transcode["input_size"] = int(input_size)
                if fps is not None:
                    self._transcode["fps"] = float(fps)

    def set_transcode_queue(self, pending: int, index: int, count: int,
                            eta_seconds: Optional[float], percent: float) -> None:
        """Record the transcode queue aggregate (pending count, position, total
        remaining time, overall %) so the e-paper panel can show the whole batch,
        not just the current file. No-op unless a transcode is active."""
        with self._lock:
            if self._transcode.get("active"):
                self._transcode["queue_pending"] = int(pending)
                self._transcode["queue_index"] = int(index)
                self._transcode["queue_count"] = int(count)
                self._transcode["queue_eta_seconds"] = eta_seconds
                self._transcode["queue_percent"] = float(percent)

    def finish_transcode(self) -> None:
        with self._lock:
            self._transcode = {"active": False}

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
    def ap_active(self) -> bool:
        """Last known WLAN AP state (used to flip it instantly on a button press)."""
        with self._lock:
            return self._ap_active

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
                "wifi_ap": self._ap_active,
                "auto_transcode": self._auto_transcode,
                "transcode": self._transcode_snapshot_locked(),
                "events": list(reversed(self._events)),  # newest first
            }

    def _transcode_snapshot_locked(self) -> dict[str, Any]:
        """Transcode block for the snapshot (elapsed/ETA from percent + wall clock)."""
        tr = self._transcode
        if not tr.get("active"):
            return {"active": False}
        percent = float(tr.get("percent", 0.0))
        started = tr.get("started")
        elapsed = (time.monotonic() - started) if started is not None else None
        eta = None
        if elapsed is not None and percent > 0:
            eta = max(0.0, elapsed * (100.0 - percent) / percent)
        return {
            "active": True,
            "name": tr.get("name", ""),
            "percent": round(percent, 1),
            "encoder": tr.get("encoder", ""),
            "hw": bool(tr.get("hw", False)),
            "input_size": int(tr.get("input_size", 0) or 0),
            "fps": tr.get("fps"),
            "elapsed_seconds": round(elapsed, 1) if elapsed is not None else None,
            "eta_seconds": round(eta, 1) if eta is not None else None,
            "queue": {
                "pending": int(tr.get("queue_pending", 0) or 0),
                "index": int(tr.get("queue_index", 0) or 0),
                "count": int(tr.get("queue_count", 0) or 0),
                "eta_seconds": tr.get("queue_eta_seconds"),
                "percent": round(float(tr.get("queue_percent", 0.0) or 0.0), 1),
            },
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

    def signal(self, event: Event) -> None:
        """Fire a one-shot status effect (e.g. a detection blink). Momentary --
        it is not part of the persisted snapshot, only the live indicators."""
        self._indicator.signal(event)

    def set_fill(self, fraction: float, sticky: bool = False) -> None:
        """Feed the detected device's fill level to the indicators (shown as a
        gauge while detecting). ``sticky`` keeps the gauge up until the state
        changes. Indicator-only: the web UI has its own per-device storage
        figures, so this never touches the snapshot."""
        self._indicator.set_fill(fraction, sticky)

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

    def set_ap_active(self, active: bool) -> None:
        self._state.set_ap_active(active)

    def begin_transcode(self, name: str) -> None:
        """Enter the TRANSCODING phase: overrides the copy status on every backend."""
        self._state.begin_transcode(name)
        self._indicator.set_state(State.TRANSCODING)
        self._indicator.set_progress(0.0)

    def set_transcode_progress(self, fraction: float, encoder: str = "", hw: bool = False) -> None:
        self._state.update_transcode(fraction, encoder, hw)
        self._indicator.set_progress(fraction)

    def finish_transcode(self, restore_phase: State) -> None:
        """Leave the transcode phase and restore whatever phase was showing before."""
        self._state.finish_transcode()
        self.set_phase(restore_phase)

    def fail_transcode(self, message: str) -> None:
        """End a failed transcode by showing the error on every backend."""
        self._state.finish_transcode()
        self.set_error(message)

    def log_event(self, message: str, level: str = "info") -> None:
        self._state.log_event(message, level)

    def reset_to_ready(self) -> None:
        self._state.reset_to_ready()
        self._indicator.set_state(State.READY)

    def close(self) -> None:
        self._indicator.close()
