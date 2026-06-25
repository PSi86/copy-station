from copystation.status import Event, State
from copystation.status.led_backend import _PATTERN, LedBackend


def test_success_is_distinct_from_ready():
    # Both use the green 'ready' LED, but SUCCESS blinks so it is not mistaken
    # for plain idle (READY is steady).
    ready = _PATTERN[State.READY]
    success = _PATTERN[State.SUCCESS]
    assert ready[:3] == success[:3]      # same LED lit
    assert ready[3] is False             # READY steady
    assert success[3] is True            # SUCCESS blinks


def test_effect_lines_mapping():
    # Device detected -> flash the green 'ready' LED only.
    assert LedBackend._effect_lines(Event.DEVICE_DETECTED, True) == (True, False, False)
    assert LedBackend._effect_lines(Event.DEVICE_DETECTED, False) == (False, False, False)
    # Source empty -> all three LEDs (a pattern no steady state uses).
    assert LedBackend._effect_lines(Event.SOURCE_EMPTY, True) == (True, True, True)
