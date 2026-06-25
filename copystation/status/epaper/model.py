"""The view model: a pure projection of a StationState snapshot for the screen.

The e-paper backend reads ``StationState.snapshot()`` (the same dict the web UI
polls) and turns it into a small, immutable :class:`ViewModel` describing exactly
what the panel shows. Keeping this a pure function makes the layout and the
full-vs-partial policy trivially testable without any hardware or live state.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# phase value (StationState) -> the word shown on the panel.
_STATUS_TEXT = {
    "ready": "Ready",
    "detecting": "Detecting",
    "copying": "Copying",
    "error": "Error",
    "success": "Done",
}


def fmt_bytes(n: int | float | None) -> str:
    """Human-readable size, e.g. ``12 GB`` / ``1.4 TB`` (mirrors the web UI)."""
    if n is None:
        return "--"
    units = ["B", "KB", "MB", "GB", "TB"]
    value = float(n)
    i = 0
    while value >= 1024 and i < len(units) - 1:
        value /= 1024
        i += 1
    digits = 0 if (value >= 10 or i == 0) else 1
    return f"{value:.{digits}f} {units[i]}"


def fmt_duration(seconds: float | None) -> str:
    """``m:ss`` (or ``h:mm:ss``) for an elapsed/ETA time, ``--`` if unknown."""
    if seconds is None:
        return "--"
    s = int(round(seconds))
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return f"{h}:{m:02d}:{sec:02d}" if h > 0 else f"{m}:{sec:02d}"


@dataclass(frozen=True)
class StorageView:
    """One mass storage's figures, as the screen needs them."""

    label: str
    used: int
    capacity: int

    @property
    def fraction(self) -> float:
        return (self.used / self.capacity) if self.capacity > 0 else 0.0

    @property
    def present(self) -> bool:
        return self.capacity > 0


@dataclass(frozen=True)
class ViewModel:
    """Everything the panel renders, derived from one snapshot."""

    status_text: str
    phase: str
    percent: int
    progress_fraction: float
    show_progress: bool
    source: StorageView
    target: StorageView
    device_count: int
    speed_text: str
    eta_text: str
    error_text: str
    version: str

    def storage_line(self, storage: StorageView) -> str:
        """``used / capacity`` for a storage row (``--`` when absent)."""
        if not storage.present:
            return "--"
        return f"{fmt_bytes(storage.used)} / {fmt_bytes(storage.capacity)}"


def _storage_view(raw: dict[str, Any] | None) -> StorageView:
    raw = raw or {}
    return StorageView(
        label=str(raw.get("label", "") or ""),
        used=int(raw.get("used", 0) or 0),
        capacity=int(raw.get("capacity", 0) or 0),
    )


def build_view(snapshot: dict[str, Any], version: str = "") -> ViewModel:
    """Project a StationState snapshot dict into a :class:`ViewModel`."""
    phase = str(snapshot.get("phase", "ready"))
    percent = int(round(float(snapshot.get("percent", 0.0) or 0.0)))
    show_progress = phase in ("copying", "success")
    speed = snapshot.get("speed_bytes")
    return ViewModel(
        status_text=_STATUS_TEXT.get(phase, phase.title() or "Ready"),
        phase=phase,
        percent=max(0, min(100, percent)),
        progress_fraction=max(0.0, min(1.0, percent / 100.0)),
        show_progress=show_progress,
        source=_storage_view(snapshot.get("source")),
        target=_storage_view(snapshot.get("target")),
        device_count=len(snapshot.get("devices", []) or []),
        speed_text=f"{fmt_bytes(speed)}/s" if speed else "",
        eta_text=fmt_duration(snapshot.get("eta_seconds")),
        error_text=str(snapshot.get("error", "") or ""),
        version=version,
    )
