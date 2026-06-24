import threading

from copystation.status import Event, State
from copystation.status.ws2812_backend import (
    MAX_LEDS,
    Ws2812Backend,
    _DETECT_COLOR,
    _EMPTY_COLOR,
    _ERROR_COLOR,
    _FILL_COLOR,
    _IDLE_COLOR,
    _OFF,
    encode_pixels,
    leds_for,
)


def test_leds_for_bounds():
    assert leds_for(0.0, 10) == 0
    assert leds_for(-0.5, 10) == 0
    assert leds_for(1.0, 10) == 10
    assert leds_for(2.0, 10) == 10


def test_leds_for_scales_with_count():
    # The progress bar spans exactly the configured number of LEDs.
    assert leds_for(1.0, 1) == 1
    assert leds_for(0.5, 1) == 1   # 0.5 -> rounds up to the single LED
    assert leds_for(0.5, 4) == 2
    assert leds_for(0.45, 10) == 5  # 4.5 -> rounds up (int(x + 0.5))
    assert leds_for(0.95, 10) == 10


def test_max_leds_is_ten():
    assert MAX_LEDS == 10


def _decode(out: list[int]) -> list[tuple[int, int, int]]:
    """Reverse ``encode_pixels``: byte stream -> list of (R, G, B) tuples.

    Unpacks the bytes to a bitstream, reads it in groups of three SPI bits
    (110 -> 1, 100 -> 0), regroups the recovered data bits into GRB bytes and
    re-orders them to RGB.
    """
    bits: list[int] = []
    for byte in out:
        for i in range(7, -1, -1):
            bits.append((byte >> i) & 1)

    data: list[int] = []
    for i in range(0, len(bits), 3):
        triple = tuple(bits[i : i + 3])
        assert triple in {(1, 1, 0), (1, 0, 0)}, triple
        data.append(triple[1])

    pixels: list[tuple[int, int, int]] = []
    for i in range(0, len(data), 24):
        byte_bits = data[i : i + 24]
        vals = []
        for j in range(0, 24, 8):
            value = 0
            for bit in byte_bits[j : j + 8]:
                value = (value << 1) | bit
            vals.append(value)
        g, r, b = vals  # encoder emits GRB order
        pixels.append((r, g, b))
    return pixels


def test_encode_length_per_led():
    # 3 colour bytes * 8 bits * 3 SPI bits = 72 bits = 9 bytes per LED.
    assert len(encode_pixels([(0, 0, 0)])) == 9
    assert len(encode_pixels([(1, 2, 3)] * 5)) == 9 * 5


def test_encode_round_trips_colours():
    pixels = [(255, 0, 0), (0, 128, 0), (0, 0, 64), (10, 20, 30)]
    assert _decode(encode_pixels(pixels)) == pixels


def test_encode_off_pixel_round_trips():
    assert _decode(encode_pixels([(0, 0, 0)])) == [(0, 0, 0)]


def test_effect_pixels_cover_all_leds():
    # One-shot effects use the whole strip (bypass __init__: no spidev needed).
    b = Ws2812Backend.__new__(Ws2812Backend)
    b._led_count = 3
    assert b._effect_pixels(Event.DEVICE_DETECTED, True) == [_DETECT_COLOR] * 3
    assert b._effect_pixels(Event.DEVICE_DETECTED, False) == [_OFF] * 3
    # 'source empty' is a steady hold -> always lit while it plays.
    assert b._effect_pixels(Event.SOURCE_EMPTY, True) == [_EMPTY_COLOR] * 3
    # Detection green and empty-source blue must be visually different.
    assert _DETECT_COLOR != _EMPTY_COLOR


def test_fill_gauge_is_white_and_at_least_one_led():
    b = Ws2812Backend.__new__(Ws2812Backend)
    b._led_count = 10
    # Half full -> five white LEDs, the rest off.
    assert b._fill_pixels(0.5) == [_FILL_COLOR] * 5 + [_OFF] * 5
    # Empty volume still lights one LED so "detected" reads.
    assert b._fill_pixels(0.0) == [_FILL_COLOR] + [_OFF] * 9
    # Full volume lights the whole strip.
    assert b._fill_pixels(1.0) == [_FILL_COLOR] * 10
    # White is equal-channel and clearly not the green detection colour.
    assert _FILL_COLOR[0] == _FILL_COLOR[1] == _FILL_COLOR[2]
    assert _FILL_COLOR != _DETECT_COLOR


def test_error_is_bright_red_with_its_own_rendering():
    # ERROR is all-LEDs-blink-red, so it must NOT also be a single idle colour.
    assert State.ERROR not in _IDLE_COLOR
    assert _ERROR_COLOR[0] > 0 and _ERROR_COLOR[1] == 0 and _ERROR_COLOR[2] == 0


def test_set_fill_tracks_sticky_flag():
    b = Ws2812Backend.__new__(Ws2812Backend)
    b._lock = threading.Lock()
    b.set_fill(0.5, sticky=True)
    assert (b._fill, b._fill_sticky, b._fill_shown_at) == (0.5, True, None)
    b.set_fill(0.3)  # default: a brief (non-sticky) readout again
    assert b._fill_sticky is False
