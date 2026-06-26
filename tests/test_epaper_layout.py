import pytest

pytest.importorskip("PIL")

from copystation.status.epaper.layout import render, render_stopped  # noqa: E402
from copystation.status.epaper.model import (  # noqa: E402
    DeviceView,
    StorageView,
    ViewModel,
    build_view,
)


def _vm(percent):
    return ViewModel(
        status_text="Copying",
        phase="copying",
        percent=percent,
        progress_fraction=percent / 100.0,
        show_progress=True,
        source=StorageView(label="DJI", used=12, capacity=32),
        target=StorageView(label="SD", used=120, capacity=256),
        devices=(),
        device_count=2,
        speed_text="18 MB/s",
        eta_text="0:42",
        error_text="",
        version="0.1.0",
    )


def _black_pixels(img):
    # mode "1": histogram bucket 0 = black pixels, bucket 255 = white.
    return img.histogram()[0]


def test_portrait_render_size_and_mode():
    img = render(_vm(50), 200, 200)
    assert img.size == (200, 200)
    assert img.mode == "1"
    assert _black_pixels(img) > 0  # something was drawn


def test_landscape_render_size():
    img = render(_vm(50), 296, 128)
    assert img.size == (296, 128)
    assert img.mode == "1"


def test_progress_bar_grows_with_percent():
    # Same view apart from the progress -> a fuller bar means more black pixels.
    low = _black_pixels(render(_vm(10), 200, 200))
    high = _black_pixels(render(_vm(90), 200, 200))
    assert high > low


_DETECTING_SNAPSHOT = {
    "phase": "detecting",
    "source": {"capacity": 0, "used": 0},
    "target": {"capacity": 0, "used": 0},
    "devices": [
        {
            "name": "MassStorageClass",
            "node": "/dev/sdb1",
            "capacity": 127843434496,
            "free": 121482903552,
            "role": "candidate",
        }
    ],
}


def test_detecting_renders_the_candidate_device():
    # Regression: while detecting, source/target are still empty, so the panel
    # must render the detected device from the devices list (not a blank frame).
    view = build_view(_DETECTING_SNAPSHOT, "0.1.0")
    with_device = _black_pixels(render(view, 296, 128))

    empty = build_view({"phase": "detecting", "devices": []}, "0.1.0")
    without = _black_pixels(render(empty, 296, 128))

    assert with_device > without  # the device row adds visible content


def test_detecting_device_renders_on_square_panel():
    view = build_view(_DETECTING_SNAPSHOT, "0.1.0")
    img = render(view, 200, 200)
    assert img.size == (200, 200)
    assert _black_pixels(img) > 0


def test_stopped_frame():
    img = render_stopped("0.1.0", 200, 200)
    assert img.size == (200, 200)
    assert img.mode == "1"
    assert _black_pixels(img) > 0  # title/border drawn
