from copystation.status import Event
from copystation.status.effects import (
    AP_FLASHES,
    DETECT_FLASHES,
    DETECT_OFF,
    DETECT_ON,
    EMPTY_HOLD_SECONDS,
    FILL_GAUGE_SECONDS,
    STARTUP_SWEEP_SECONDS,
    TransientQueue,
    effect_phase,
    fill_gauge_visible,
    startup_sweep_count,
)

PERIOD = DETECT_ON + DETECT_OFF


def test_detect_lit_then_dark_within_a_flash():
    lit, done = effect_phase(Event.DEVICE_DETECTED, 0.0)
    assert lit and not done
    lit, done = effect_phase(Event.DEVICE_DETECTED, DETECT_ON + 0.001)
    assert not lit and not done


def test_detect_blinks_exactly_twice():
    assert DETECT_FLASHES == 2  # "only blink green twice", then the fill gauge
    # The middle of each flash's on-window is lit; there is no extra flash.
    for k in range(DETECT_FLASHES):
        lit, done = effect_phase(Event.DEVICE_DETECTED, k * PERIOD + DETECT_ON / 2)
        assert lit and not done, k
    # Just before the end it is still running, at/after the end it is done.
    assert effect_phase(Event.DEVICE_DETECTED, PERIOD * DETECT_FLASHES - 0.001)[1] is False
    assert effect_phase(Event.DEVICE_DETECTED, PERIOD * DETECT_FLASHES)[1] is True


def test_source_empty_holds_lit_then_finishes():
    assert effect_phase(Event.SOURCE_EMPTY, 0.0) == (True, False)
    assert effect_phase(Event.SOURCE_EMPTY, EMPTY_HOLD_SECONDS - 0.01) == (True, False)
    assert effect_phase(Event.SOURCE_EMPTY, EMPTY_HOLD_SECONDS) == (False, True)


def test_ap_and_auto_transcode_toggles_blink_three_times():
    assert AP_FLASHES == 3
    for event in (Event.AP_ENABLED, Event.AP_DISABLED,
                  Event.AUTO_TRANSCODE_ENABLED, Event.AUTO_TRANSCODE_DISABLED):
        # Lit in the middle of each of the three flashes.
        for k in range(AP_FLASHES):
            lit, done = effect_phase(event, k * PERIOD + DETECT_ON / 2)
            assert lit and not done, (event, k)
        # Runs up to AP_FLASHES periods, then it is done.
        assert effect_phase(event, PERIOD * AP_FLASHES - 0.001)[1] is False
        assert effect_phase(event, PERIOD * AP_FLASHES)[1] is True


def test_fill_gauge_shows_for_three_seconds_then_hides():
    assert FILL_GAUGE_SECONDS == 3.0
    assert fill_gauge_visible(0.0) is True
    assert fill_gauge_visible(FILL_GAUGE_SECONDS - 0.01) is True
    assert fill_gauge_visible(FILL_GAUGE_SECONDS) is False
    assert fill_gauge_visible(FILL_GAUGE_SECONDS + 5) is False


def test_startup_sweep_runs_once_and_grows():
    # Lit throughout, finishes after the sweep duration.
    assert effect_phase(Event.SERVICE_STARTED, 0.0) == (True, False)
    assert effect_phase(Event.SERVICE_STARTED, STARTUP_SWEEP_SECONDS) == (True, True)
    # The wipe grows from one LED to the full count, monotonically, clamped.
    assert startup_sweep_count(0.0, 10) == 1
    assert startup_sweep_count(STARTUP_SWEEP_SECONDS / 2, 10) == 5
    assert startup_sweep_count(STARTUP_SWEEP_SECONDS, 10) == 10
    assert startup_sweep_count(STARTUP_SWEEP_SECONDS * 5, 10) == 10  # clamped


def test_queue_is_fifo_and_tracks_elapsed():
    q = TransientQueue()
    q.push(Event.DEVICE_DETECTED)
    q.push(Event.SOURCE_EMPTY)

    assert q.current(100.0) == (Event.DEVICE_DETECTED, 0.0)
    # Same active effect, elapsed grows with the clock.
    assert q.current(100.5) == (Event.DEVICE_DETECTED, 0.5)

    q.finish()
    assert q.current(101.0) == (Event.SOURCE_EMPTY, 0.0)
    q.finish()
    assert q.current(102.0) is None


def test_queue_caps_backlog():
    q = TransientQueue(max_queued=3)
    for _ in range(10):
        q.push(Event.DEVICE_DETECTED)

    drained, now = 0, 0.0
    while q.current(now) is not None:
        q.finish()
        drained += 1
        now += 1.0
    assert drained == 3
