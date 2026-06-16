from copystation.status.grove_led_bar import SEGMENT_COUNT, segments_for


def test_segments_for_bounds():
    assert segments_for(0.0) == 0
    assert segments_for(-0.5) == 0
    assert segments_for(1.0) == SEGMENT_COUNT
    assert segments_for(2.0) == SEGMENT_COUNT


def test_segments_for_rounding():
    assert segments_for(0.05) == 1   # 0.5 -> rounds up
    assert segments_for(0.44) == 4
    assert segments_for(0.45) == 5   # 4.5 -> rounds up (int(x+0.5))
    assert segments_for(0.5) == 5
    assert segments_for(0.95) == 10  # 9.5 -> 10
