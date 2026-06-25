"""E-paper controller drivers and the hardware-opening factory.

``open_driver`` opens the real SPI device and GPIO lines from a resolved panel
config and returns the matching :class:`EpaperDriver`. The backend can be handed
an alternative factory for tests, so nothing here needs real hardware to test.
"""

from __future__ import annotations

from .base import EpaperDriver
from .ssd168x import Ssd168xDriver
from .ssd1677 import Ssd1677Driver

# SSD1681 (1.54") shares the SSD1680 command set -> the same parameterised class.
DRIVER_CLASSES: dict[str, type[EpaperDriver]] = {
    "ssd1680": Ssd168xDriver,
    "ssd1681": Ssd168xDriver,
    "ssd1677": Ssd1677Driver,
}


def _parse_spidev(path: str) -> tuple[int, int]:
    """``/dev/spidev0.0`` -> ``(0, 0)`` (same convention as the ws2812 backend)."""
    tail = str(path).rsplit("spidev", 1)[-1]
    bus_str, dev_str = tail.split(".")
    return int(bus_str), int(dev_str)


def open_driver(panel: dict) -> EpaperDriver:
    """Open SPI + GPIO from a resolved panel config and build the driver."""
    controller = str(panel["controller"]).lower()
    cls = DRIVER_CLASSES.get(controller)
    if cls is None:
        known = ", ".join(sorted(DRIVER_CLASSES))
        raise ValueError(
            f"Unknown e-paper controller '{controller}'. Known: {known}."
        )

    import spidev  # only present on the target boards

    from ...gpio import open_input_lines, open_output_lines

    bus, dev = _parse_spidev(panel.get("device", "/dev/spidev0.0"))
    spi = spidev.SpiDev()
    spi.open(bus, dev)
    spi.max_speed_hz = int(panel.get("spi_speed_hz", 4_000_000))
    try:
        spi.mode = 0
    except Exception:  # pragma: no cover - some stubs lack a settable mode
        pass

    chip = panel.get("gpiochip", "gpiochip0")
    dc = int(panel["dc"])
    rst = int(panel["rst"])
    busy = int(panel["busy"])
    pwr = panel.get("pwr")
    cs = panel.get("cs")

    out_offsets = [dc, rst]
    if pwr is not None:
        out_offsets.append(int(pwr))
    if cs is not None:
        out_offsets.append(int(cs))
    gpio_out = open_output_lines(chip, out_offsets, "copystation-epaper")

    busy_active_high = bool(panel.get("busy_active_high", True))
    gpio_in = open_input_lines(
        chip, [busy], "copystation-epaper", active_low=not busy_active_high
    )

    return cls(
        width=int(panel["width"]),
        height=int(panel["height"]),
        spi=spi,
        gpio_out=gpio_out,
        gpio_in=gpio_in,
        dc=dc,
        rst=rst,
        busy=busy,
        pwr=None if pwr is None else int(pwr),
        cs=None if cs is None else int(cs),
    )
