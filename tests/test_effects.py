from copystation.status import Event
from copystation.status.effects import (
    DETECT_FLASHES,
    DETECT_OFF,
    DETECT_ON,
    EMPTY_HOLD_SECONDS,
    TransientQueue,
    effect_phase,
)

PERIOD = DETECT_ON + DETECT_OFF


def test_detect_lit_then_dark_within_a_flash():
    lit, done = effect_phase(Event.DEVICE_DETECTED, 0.0)
    assert lit and not done
    lit, done = effect_phase(Event.DEVICE_DETECTED, DETECT_ON + 0.001)
    assert not lit and not done


def test_detect_has_exactly_four_lit_phases():
    # The middle of each flash's on-window is lit; there is no fifth flash.
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
