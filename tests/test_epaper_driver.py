from copystation.status.epaper.drivers.ssd168x import (
    _UPDATE_FULL,
    _UPDATE_PARTIAL,
    Ssd168xDriver,
)


class _FakeOut:
    def __init__(self):
        self.log = []

    def set(self, offset, high):
        self.log.append((offset, bool(high)))

    def release(self):
        pass


class _FakeIn:
    """BUSY always reads not-busy, so wait_busy returns immediately."""

    def get(self, offset):
        return False

    def release(self):
        pass


class _FakeSpi:
    max_speed_hz = 0

    def __init__(self):
        self.xfers = []  # each entry is a list of bytes

    def xfer2(self, data):
        self.xfers.append(list(data))

    def close(self):
        pass


def _driver(width=200, height=200):
    spi, out, inp = _FakeSpi(), _FakeOut(), _FakeIn()
    drv = Ssd168xDriver(
        width=width,
        height=height,
        spi=spi,
        gpio_out=out,
        gpio_in=inp,
        dc=25,
        rst=17,
        busy=24,
    )
    return drv, spi, out


def _has(payloads, single_byte):
    return [single_byte] in payloads


def _followed_by(payloads, cmd, data_byte):
    for i, p in enumerate(payloads):
        if p == [cmd] and i + 1 < len(payloads) and payloads[i + 1] == [data_byte]:
            return True
    return False


def test_buffer_geometry():
    drv, _, _ = _driver(200, 200)
    assert drv.bytes_per_row == 25
    assert drv.buffer_size == 25 * 200


def test_init_sends_core_commands_and_pulses_reset():
    drv, spi, out = _driver()
    drv.init()
    assert _has(spi.xfers, 0x12)  # software reset
    assert _has(spi.xfers, 0x01)  # driver output control
    assert _has(spi.xfers, 0x11)  # data entry mode
    assert _has(spi.xfers, 0x44) and _has(spi.xfers, 0x45)  # RAM window
    # RST was driven low then high during reset.
    assert (17, False) in out.log and (17, True) in out.log


def test_full_refresh_uses_full_update_byte():
    drv, spi, _ = _driver()
    drv.display_full([0xFF] * drv.buffer_size)
    assert _has(spi.xfers, 0x24)  # write current RAM
    assert _has(spi.xfers, 0x26)  # seed the previous RAM
    assert _followed_by(spi.xfers, 0x22, _UPDATE_FULL)
    assert _has(spi.xfers, 0x20)  # master activation


def test_partial_refresh_uses_partial_update_byte():
    drv, spi, _ = _driver()
    drv.display_full([0xFF] * drv.buffer_size)  # establishes a base frame
    spi.xfers.clear()
    drv.display_partial([0x00] * drv.buffer_size)
    assert _followed_by(spi.xfers, 0x22, _UPDATE_PARTIAL)
    assert _UPDATE_PARTIAL != _UPDATE_FULL


def test_data_is_chunked_for_large_frames():
    drv, spi, _ = _driver()
    spi.xfers.clear()
    drv.data([0xAB] * 5000)  # > the 2048-byte SPI chunk
    # The 5000-byte payload must be split across multiple xfers.
    big = [x for x in spi.xfers if len(x) > 1]
    assert len(big) >= 3
    assert sum(len(x) for x in big) == 5000


def test_sleep_sends_deep_sleep():
    drv, spi, _ = _driver()
    drv.sleep()
    assert _followed_by(spi.xfers, 0x10, 0x01)
