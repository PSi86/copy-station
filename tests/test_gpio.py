import enum
from types import SimpleNamespace

import pytest

from copystation.status.gpio import (
    _V1OutputLines,
    _V2OutputLines,
    _chip_name,
    _chip_path,
    _select_impl,
    open_output_lines,
)


# ----- fake libgpiod v1 module -------------------------------------------------


class FakeV1Line:
    def __init__(self):
        self.values = []
        self.requested = None
        self.released = False

    def request(self, consumer, type):
        self.requested = (consumer, type)

    def set_value(self, value):
        self.values.append(value)

    def release(self):
        self.released = True


class FakeV1Chip:
    def __init__(self, name):
        self.name = name
        self.lines = {}
        self.closed = False

    def get_line(self, offset):
        line = FakeV1Line()
        self.lines[offset] = line
        return line

    def close(self):
        self.closed = True


class FakeV1Module:
    LINE_REQ_DIR_OUT = "out"

    def __init__(self):
        self.chips = []

    def Chip(self, name):
        chip = FakeV1Chip(name)
        self.chips.append(chip)
        return chip


# ----- fake libgpiod v2 module -------------------------------------------------


class Direction(enum.Enum):
    OUTPUT = 1


class Value(enum.Enum):
    INACTIVE = 0
    ACTIVE = 1


class FakeLineSettings:
    def __init__(self, direction=None, output_value=None):
        self.direction = direction
        self.output_value = output_value


class FakeV2Request:
    def __init__(self):
        self.sets = []
        self.released = False

    def set_value(self, offset, value):
        self.sets.append((offset, value))

    def release(self):
        self.released = True


class FakeV2Module:
    LineSettings = FakeLineSettings
    line = SimpleNamespace(Direction=Direction, Value=Value)

    def __init__(self):
        self.request = None
        self.request_args = None

    def request_lines(self, path, consumer, config):
        self.request = FakeV2Request()
        self.request_args = (path, consumer, config)
        return self.request


# ----- chip normalisation ------------------------------------------------------


def test_chip_name_and_path_normalisation():
    assert _chip_name("gpiochip0") == "gpiochip0"
    assert _chip_name("/dev/gpiochip4") == "gpiochip4"
    assert _chip_name("0") == "gpiochip0"
    assert _chip_path("gpiochip0") == "/dev/gpiochip0"
    assert _chip_path("/dev/gpiochip4") == "/dev/gpiochip4"
    assert _chip_path("0") == "/dev/gpiochip0"


# ----- version dispatch --------------------------------------------------------


def test_select_impl_picks_v1_and_v2():
    assert _select_impl(FakeV1Module()) is _V1OutputLines
    assert _select_impl(FakeV2Module()) is _V2OutputLines


def test_select_impl_rejects_unknown():
    with pytest.raises(RuntimeError):
        _select_impl(SimpleNamespace())


# ----- v1 routing --------------------------------------------------------------


def test_v1_open_set_release():
    mod = FakeV1Module()
    lines = open_output_lines("0", [17, 18], "copystation", gpiod_module=mod)

    chip = mod.chips[0]
    assert chip.name == "gpiochip0"  # "0" normalised to a bare name for v1
    assert set(chip.lines) == {17, 18}
    assert chip.lines[17].requested == ("copystation", "out")

    lines.set(17, True)
    lines.set(18, False)
    assert chip.lines[17].values == [1]
    assert chip.lines[18].values == [0]

    lines.release()
    assert chip.lines[17].released and chip.lines[18].released
    assert chip.closed


# ----- v2 routing --------------------------------------------------------------


def test_v2_open_set_release():
    mod = FakeV2Module()
    lines = open_output_lines("gpiochip0", [5], "copystation", gpiod_module=mod)

    path, consumer, config = mod.request_args
    assert path == "/dev/gpiochip0"  # normalised to a device path for v2
    assert consumer == "copystation"
    assert isinstance(config[5], FakeLineSettings)
    assert config[5].direction is Direction.OUTPUT
    assert config[5].output_value is Value.INACTIVE

    lines.set(5, True)
    lines.set(5, False)
    assert mod.request.sets == [(5, Value.ACTIVE), (5, Value.INACTIVE)]

    lines.release()
    assert mod.request.released
