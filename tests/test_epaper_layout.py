import pytest

pytest.importorskip("PIL")

from copystation.status.epaper.layout import render, render_stopped  # noqa: E402
from copystation.status.epaper.model import StorageView, ViewModel  # noqa: E402


def _vm(percent):
    return ViewModel(
        status_text="Copying",
        phase="copying",
        percent=percent,
        progress_fraction=percent / 100.0,
        show_progress=True,
        source=StorageView(label="DJI", used=12, capacity=32),
        target=StorageView(label="SD", used=120, capacity=256),
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


def test_stopped_frame():
    img = render_stopped("0.1.0", 200, 200)
    assert img.size == (200, 200)
    assert img.mode == "1"
    assert _black_pixels(img) > 0  # title/border drawn
