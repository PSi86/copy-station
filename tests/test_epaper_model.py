from copystation.status.epaper.model import (
    build_view,
    fmt_bytes,
    fmt_duration,
)

_COPYING = {
    "phase": "copying",
    "percent": 67.0,
    "source": {"label": "DJI O4", "capacity": 32_000_000_000, "used": 12_000_000_000},
    "target": {"label": "SDXC", "capacity": 256_000_000_000, "used": 121_000_000_000},
    "devices": [{"node": "/dev/sda"}, {"node": "/dev/sdb"}],
    "speed_bytes": 18_000_000,
    "eta_seconds": 42,
    "error": "",
}


def test_build_view_copying():
    v = build_view(_COPYING, version="0.1.0")
    assert v.status_text == "Copying"
    assert v.percent == 67
    assert abs(v.progress_fraction - 0.67) < 1e-9
    assert v.show_progress is True
    assert abs(v.source.fraction - 0.375) < 1e-3
    assert v.source.present and v.target.present
    assert v.device_count == 2
    assert v.speed_text.endswith("/s") and "MB" in v.speed_text
    assert v.eta_text == "0:42"
    assert v.version == "0.1.0"


def test_build_view_ready_hides_progress():
    v = build_view({"phase": "ready", "percent": 0.0}, version="x")
    assert v.status_text == "Ready"
    assert v.show_progress is False
    assert v.source.present is False  # no storage yet


def test_build_view_error_carries_message():
    v = build_view({"phase": "error", "error": "Target was disconnected"})
    assert v.status_text == "Error"
    assert v.error_text == "Target was disconnected"


def test_percent_clamped():
    assert build_view({"phase": "copying", "percent": 150}).percent == 100
    assert build_view({"phase": "copying", "percent": -5}).percent == 0


def test_fmt_helpers():
    assert fmt_bytes(0) == "0 B"
    assert fmt_bytes(None) == "--"
    assert fmt_bytes(1536) == "1.5 KB"
    assert fmt_duration(None) == "--"
    assert fmt_duration(42) == "0:42"
    assert fmt_duration(3661) == "1:01:01"
