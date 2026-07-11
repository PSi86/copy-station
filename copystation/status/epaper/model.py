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
    "transcoding": "Transcoding",
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


def usage_text(used: int, capacity: int) -> str:
    """``used / capacity`` for a gauge row (``--`` when the size is unknown)."""
    if capacity <= 0:
        return "--"
    return f"{fmt_bytes(used)} / {fmt_bytes(capacity)}"


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
class DeviceView:
    """A detected volume as the panel shows it (name + role + fill gauge).

    Used before a transfer assigns source/target roles -- during detecting the
    snapshot's ``source``/``target`` are still empty, but the candidate already
    appears in ``devices`` with its capacity/free, so the panel renders these.
    """

    name: str
    role: str
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
    devices: tuple[DeviceView, ...]
    device_count: int
    speed_text: str
    eta_text: str
    error_text: str
    version: str
    ap_active: bool = False
    auto_transcode_active: bool = False
    transcode_active: bool = False
    transcode_name: str = ""
    transcode_encoder: str = ""
    transcode_size_text: str = ""
    transcode_fps_text: str = ""
    elapsed_text: str = ""
    # Queue aggregate (only meaningful for a multi-file batch): "i/n" position and
    # the current file's own percent (shown as text, since for a batch the main bar
    # tracks the WHOLE queue instead of the single file).
    transcode_queue_text: str = ""
    transcode_file_text: str = ""

    def storage_line(self, storage: StorageView) -> str:
        """``used / capacity`` for a storage row (``--`` when absent)."""
        return usage_text(storage.used, storage.capacity) if storage.present else "--"


def _storage_view(raw: dict[str, Any] | None) -> StorageView:
    raw = raw or {}
    return StorageView(
        label=str(raw.get("label", "") or ""),
        used=int(raw.get("used", 0) or 0),
        capacity=int(raw.get("capacity", 0) or 0),
    )


def _device_view(raw: dict[str, Any]) -> DeviceView:
    capacity = int(raw.get("capacity", 0) or 0)
    free = int(raw.get("free", 0) or 0)
    return DeviceView(
        name=str(raw.get("name") or raw.get("node") or "device"),
        role=str(raw.get("role", "") or ""),
        used=max(0, capacity - free),
        capacity=capacity,
    )


def build_view(snapshot: dict[str, Any], version: str = "") -> ViewModel:
    """Project a StationState snapshot dict into a :class:`ViewModel`."""
    phase = str(snapshot.get("phase", "ready"))
    transcode = snapshot.get("transcode") or {}
    tr_active = bool(transcode.get("active")) and phase == "transcoding"

    queue = (transcode.get("queue") or {}) if tr_active else {}
    q_count = int(queue.get("count", 0) or 0)
    q_index = int(queue.get("index", 0) or 0)
    # A real batch (>1 file): show the WHOLE queue on the main bar (it only grows,
    # so it partial-refreshes cleanly between files) and the current file's own
    # progress as text. A single file keeps the per-file bar as before.
    batch = tr_active and q_count > 1
    file_percent = int(round(float(transcode.get("percent", 0.0) or 0.0)))

    if tr_active:
        percent = int(round(float(queue.get("percent", 0.0) or 0.0))) if batch \
            else file_percent
    else:
        percent = int(round(float(snapshot.get("percent", 0.0) or 0.0)))
    show_progress = tr_active or phase in ("copying", "success")
    speed = snapshot.get("speed_bytes")
    devices = tuple(_device_view(d) for d in (snapshot.get("devices", []) or []))
    return ViewModel(
        status_text=_STATUS_TEXT.get(phase, phase.title() or "Ready"),
        phase=phase,
        percent=max(0, min(100, percent)),
        progress_fraction=max(0.0, min(1.0, percent / 100.0)),
        show_progress=show_progress,
        source=_storage_view(snapshot.get("source")),
        target=_storage_view(snapshot.get("target")),
        devices=devices,
        device_count=len(devices),
        speed_text=f"{fmt_bytes(speed)}/s" if speed else "",
        # For a batch the footer ETA is the whole queue's remaining time (Σ),
        # otherwise the single running job's / copy's ETA.
        eta_text=fmt_duration(queue.get("eta_seconds")) if batch
        else (fmt_duration(transcode.get("eta_seconds")) if tr_active
              else fmt_duration(snapshot.get("eta_seconds"))),
        error_text=str(snapshot.get("error", "") or ""),
        version=version,
        ap_active=bool(snapshot.get("wifi_ap", False)),
        auto_transcode_active=bool(snapshot.get("auto_transcode", False)),
        transcode_active=tr_active,
        transcode_name=str(transcode.get("name", "") or ""),
        transcode_encoder=str(transcode.get("encoder", "") or ""),
        transcode_size_text=fmt_bytes(transcode.get("input_size")) if (tr_active and transcode.get("input_size")) else "",
        transcode_fps_text=(f"{round(transcode['fps'])} fps" if (tr_active and transcode.get("fps")) else ""),
        elapsed_text=fmt_duration(transcode.get("elapsed_seconds")) if tr_active else "",
        transcode_queue_text=(f"{q_index}/{q_count}" if batch else ""),
        transcode_file_text=(f"file {file_percent}%" if batch else ""),
    )
