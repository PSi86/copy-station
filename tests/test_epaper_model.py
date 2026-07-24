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


def test_build_view_reads_ap_status():
    assert build_view({"phase": "ready", "wifi_ap": True}).ap_active is True
    assert build_view({"phase": "ready", "wifi_ap": False}).ap_active is False
    assert build_view({"phase": "ready"}).ap_active is False  # absent -> off


def test_build_view_extracts_detected_devices():
    v = build_view(
        {
            "phase": "detecting",
            "devices": [
                {"name": "MassStorageClass", "node": "/dev/sdb1",
                 "capacity": 128_000_000_000, "free": 121_000_000_000,
                 "role": "candidate"},
            ],
        }
    )
    assert v.device_count == 1
    dev = v.devices[0]
    assert dev.name == "MassStorageClass"
    assert dev.role == "candidate"
    assert dev.capacity == 128_000_000_000
    assert dev.used == 7_000_000_000          # capacity - free
    assert 0.0 < dev.fraction < 0.1 and dev.present


def test_build_view_device_falls_back_to_node_name():
    v = build_view({"phase": "detecting", "devices": [{"node": "/dev/sdc"}]})
    assert v.devices[0].name == "/dev/sdc"
    assert v.devices[0].present is False       # unknown capacity


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


# --------------------------------------------------------------------------- #
# Transcode queue (multi-file batch)
# --------------------------------------------------------------------------- #

def _transcoding(queue, percent=40.0, eta=30.0):
    return {
        "phase": "transcoding",
        "transcode": {
            "active": True, "name": "clip.mp4", "percent": percent,
            "encoder": "cpu", "hw": False, "input_size": 0, "fps": None,
            "elapsed_seconds": 12.0, "eta_seconds": eta, "queue": queue,
        },
    }


def test_build_view_batch_shows_queue_and_uses_overall_bar():
    v = build_view(_transcoding(
        {"pending": 3, "index": 2, "count": 5, "eta_seconds": 300.0, "percent": 55.0,
         "elapsed_seconds": 240.0}))
    assert v.transcode_active is True
    assert v.transcode_queue_text == "2/5"       # position within the batch
    assert v.transcode_file_text == "file 40%"   # the current file's own progress
    assert v.percent == 55                        # main bar = whole queue
    assert v.eta_text == fmt_duration(300.0)      # footer ETA = total remaining
    assert v.elapsed_text == fmt_duration(240.0)  # elapsed = whole queue too (Σ)


def test_build_view_single_file_transcode_unchanged():
    v = build_view(_transcoding(
        {"pending": 1, "index": 1, "count": 1, "eta_seconds": 30.0, "percent": 40.0}))
    assert v.transcode_queue_text == ""    # not a batch -> no "i/n"
    assert v.transcode_file_text == ""
    assert v.percent == 40                 # per-file bar, as before
    assert v.eta_text == fmt_duration(30.0)
    assert v.elapsed_text == fmt_duration(12.0)   # single file -> its own elapsed


def test_build_view_transcode_without_queue_block():
    # An active transcode snapshot that predates the queue field must still render.
    v = build_view({"phase": "transcoding",
                    "transcode": {"active": True, "name": "clip.mp4", "percent": 20.0}})
    assert v.transcode_active is True
    assert v.transcode_queue_text == "" and v.percent == 20


# --------------------------------------------------------------------------- #
# Auto-transcode badge
# --------------------------------------------------------------------------- #

def test_build_view_auto_transcode_badge_flag():
    assert build_view({"phase": "ready", "auto_transcode": True}).auto_transcode_active is True
    assert build_view({"phase": "ready", "auto_transcode": False}).auto_transcode_active is False
    assert build_view({"phase": "ready"}).auto_transcode_active is False  # absent -> off
