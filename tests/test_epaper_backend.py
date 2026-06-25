import copy

import pytest

pytest.importorskip("PIL")

from copystation.config import DEFAULTS, Config  # noqa: E402
from copystation.state import StationState  # noqa: E402
from copystation.status import StatusIndicator, build_indicator  # noqa: E402
from copystation.status.epaper import EpaperBackend  # noqa: E402


class _FakeDriver:
    def __init__(self, panel):
        self.width = int(panel["width"])
        self.height = int(panel["height"])
        self.bytes_per_row = (self.width + 7) // 8
        self.buffer_size = self.bytes_per_row * self.height
        self.calls = []
        self.full = []
        self.partial = []

    def init(self):
        self.calls.append("init")

    def display_full(self, buf):
        self.calls.append("full")
        self.full.append(list(buf))

    def display_partial(self, buf):
        self.calls.append("partial")
        self.partial.append(list(buf))

    def clear(self):
        self.calls.append("clear")

    def sleep(self):
        self.calls.append("sleep")

    def power_off_panel(self):
        self.calls.append("pwroff")

    def close(self):
        self.calls.append("close")


def _cfg():
    c = copy.deepcopy(DEFAULTS["status"]["epaper"])
    c["model"] = "waveshare-1.54"
    c["partial_min_interval"] = 0.0   # no throttle in the test
    c["full_refresh_every"] = 3
    return c


def _backend(state):
    return EpaperBackend(
        _cfg(), state=state, start=False, driver_factory=_FakeDriver
    )


def test_first_tick_is_full_then_partials_track_progress():
    state = StationState()
    be = _backend(state)
    drv = be._driver

    be._tick()  # first frame ever -> FULL
    assert drv.calls.count("full") == 1
    assert len(drv.full[0]) == drv.buffer_size  # packed to the native RAM size

    state.begin_transfer("t", 100)  # ready -> copying: phase change -> FULL
    be._tick()
    assert drv.calls.count("full") == 2

    state.update_progress(20)
    be._tick()  # bar grew, no throttle -> PARTIAL
    state.update_progress(40)
    be._tick()  # PARTIAL
    assert drv.calls.count("partial") >= 2
    assert all(len(b) == drv.buffer_size for b in drv.partial)


def test_rotated_panel_packs_to_native_buffer_size():
    # waveshare-2.9 is native 128x296 shown landscape (rotation 90). The packed
    # frame must match the controller's native RAM size regardless of rotation.
    cfg = copy.deepcopy(DEFAULTS["status"]["epaper"])
    cfg["model"] = "waveshare-2.9"
    state = StationState()
    state.begin_transfer("t", 100)
    be = EpaperBackend(cfg, state=state, start=False, driver_factory=_FakeDriver)
    be._tick()  # FULL
    expected = (128 // 8) * 296  # bytes_per_row * height = 4736
    assert len(be._driver.full[0]) == expected


def test_device_removed_forces_full(monkeypatch):
    state = StationState()
    be = _backend(state)
    drv = be._driver
    state.set_devices([{"node": "a"}, {"node": "b"}])
    be._tick()  # first -> FULL
    state.set_devices([{"node": "a"}, {"node": "b"}])
    be._tick()  # no change -> SKIP
    fulls_before = drv.calls.count("full")
    state.set_devices([{"node": "a"}])  # one disappeared
    be._tick()
    assert drv.calls.count("full") == fulls_before + 1


def test_close_draws_stop_frame_and_sleeps():
    be = EpaperBackend(_cfg(), state=None, start=False, driver_factory=_FakeDriver)
    drv = be._driver
    be.close()
    assert "init" in drv.calls          # initialised lazily for the off frame
    assert "full" in drv.calls          # the stop frame is a full refresh
    assert drv.calls.index("full") < drv.calls.index("sleep")
    assert "close" in drv.calls
    assert len(drv.full[-1]) == drv.buffer_size


def test_build_indicator_skips_underspecified_epaper():
    cfg = Config()
    cfg.data["status"] = {"backends": ["epaper"], "epaper": {}}  # no model/controller
    indicator = build_indicator(cfg)  # must not raise
    assert isinstance(indicator, StatusIndicator)
