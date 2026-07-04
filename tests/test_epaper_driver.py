import sys
import types

import pytest

from copystation.status.epaper.drivers.ssd168x import (
    _UPDATE_FULL,
    _UPDATE_PARTIAL,
    _UPDATE_PARTIAL_LUT,
    Ssd1680Driver,
    Ssd1681Driver,
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


def _payload_after(payloads, cmd):
    for i, p in enumerate(payloads):
        if p == [cmd] and i + 1 < len(payloads):
            return payloads[i + 1]
    return None


def test_ssd1681_keeps_source_mode_bit_clear():
    # With B7 of 0x21 set, the SSD1681 only accepts RAM X windows up to 192 px
    # and silently discards wider frames (seen on the Waveshare 1.54" V2.1 as a
    # never-changing panel). The 1681 driver must keep byte B at 0x00.
    spi, out, inp = _FakeSpi(), _FakeOut(), _FakeIn()
    drv = Ssd1681Driver(width=200, height=200, spi=spi, gpio_out=out,
                        gpio_in=inp, dc=25, rst=17, busy=24)
    drv.init()
    assert _payload_after(spi.xfers, 0x21) == [0x00, 0x00]


def _ssd1681(width=200, height=200):
    spi, out, inp = _FakeSpi(), _FakeOut(), _FakeIn()
    drv = Ssd1681Driver(width=width, height=height, spi=spi, gpio_out=out,
                        gpio_in=inp, dc=25, rst=17, busy=24)
    return drv, spi


def test_ssd1681_partial_loads_vendor_lut_and_uses_0xcf():
    # The 1.54" partial path follows the vendor flow: first partial after a
    # full frame loads the dedicated LUT (0x32), enables the ping-pong (0x37),
    # switches the border and displays with 0xCF instead of the OTP 0xFF.
    drv, spi = _ssd1681()
    drv.init()
    drv.display_full([0xFF] * drv.buffer_size)
    spi.xfers.clear()
    drv.display_partial([0x00] * drv.buffer_size)
    assert _has(spi.xfers, 0x32)
    assert _has(spi.xfers, 0x37)
    assert _followed_by(spi.xfers, 0x22, _UPDATE_PARTIAL_LUT)
    assert not _followed_by(spi.xfers, 0x22, _UPDATE_PARTIAL)


def test_ssd1681_second_partial_skips_lut_and_previous_ram():
    # Once in partial mode the ping-pong maintains the previous frame: no LUT
    # reload and no 0x26 re-seed on subsequent partials.
    drv, spi = _ssd1681()
    drv.init()
    drv.display_full([0xFF] * drv.buffer_size)
    drv.display_partial([0x00] * drv.buffer_size)
    spi.xfers.clear()
    drv.display_partial([0xAA] * drv.buffer_size)
    assert not _has(spi.xfers, 0x32)
    assert [0x26] not in spi.xfers
    assert _followed_by(spi.xfers, 0x22, _UPDATE_PARTIAL_LUT)


def test_ssd1681_full_after_partial_reinitialises():
    # A full refresh after partials must leave partial mode: init() re-runs
    # (SWRESET restores LUT/border/voltage state) before the 0xF7 update.
    drv, spi = _ssd1681()
    drv.init()
    drv.display_full([0xFF] * drv.buffer_size)
    drv.display_partial([0x00] * drv.buffer_size)
    spi.xfers.clear()
    drv.display_full([0xFF] * drv.buffer_size)
    assert _has(spi.xfers, 0x12)  # SWRESET from the re-init
    assert _followed_by(spi.xfers, 0x22, _UPDATE_FULL)


def test_ssd1680_partial_keeps_otp_path():
    # The SSD1680 panels stay on the OTP mode-2 waveform with 0x26 seeding.
    spi, out, inp = _FakeSpi(), _FakeOut(), _FakeIn()
    drv = Ssd1680Driver(width=128, height=296, spi=spi, gpio_out=out,
                        gpio_in=inp, dc=25, rst=17, busy=24)
    drv.init()
    drv.display_full([0xFF] * drv.buffer_size)
    spi.xfers.clear()
    drv.display_partial([0x00] * drv.buffer_size)
    assert _has(spi.xfers, 0x26)
    assert not _has(spi.xfers, 0x32)
    assert _followed_by(spi.xfers, 0x22, _UPDATE_PARTIAL)


def test_ssd1680_uses_s8_source_mode():
    # The SSD1680 panels (2.9"/2.13") use the S8.. source window, matching the
    # vendor reference code.
    spi, out, inp = _FakeSpi(), _FakeOut(), _FakeIn()
    drv = Ssd1680Driver(width=128, height=296, spi=spi, gpio_out=out,
                        gpio_in=inp, dc=25, rst=17, busy=24)
    drv.init()
    assert _payload_after(spi.xfers, 0x21) == [0x00, 0x80]


def test_sleep_sends_deep_sleep():
    drv, spi, _ = _driver()
    drv.sleep()
    assert _followed_by(spi.xfers, 0x10, 0x01)


def test_open_driver_wraps_gpio_errno_517_with_actionable_hint(monkeypatch):
    # A bare "[Errno 517]" from libgpiod must become an actionable message that
    # names the offending line/role and explains offsets-vs-pin-numbers.
    fake = types.ModuleType("spidev")

    class _Spi:
        max_speed_hz = 0
        mode = 0

        def open(self, *a):
            pass

        def close(self):
            pass

    fake.SpiDev = _Spi
    monkeypatch.setitem(sys.modules, "spidev", fake)

    import copystation.status.gpio as gpio

    def _boom(*a, **k):
        raise OSError(517, "Unknown error 517")

    monkeypatch.setattr(gpio, "open_output_lines", _boom)

    from copystation.status.epaper.drivers import open_driver

    panel = {
        "controller": "ssd1680", "width": 128, "height": 296,
        "device": "/dev/spidev1.0", "gpiochip": "gpiochip0",
        "dc": 26, "rst": 18, "busy": 16,
    }
    with pytest.raises(RuntimeError) as excinfo:
        open_driver(panel)
    msg = str(excinfo.value)
    assert "517" in msg
    assert "dc" in msg and "rst" in msg            # the offending roles are named
    assert "offset" in msg.lower()                  # the offsets-vs-pins hint fires
