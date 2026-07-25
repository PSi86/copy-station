import logging

from copystation.config import load_config
from copystation.pins import find_gpio_conflicts, gpio_claims, log_gpio_conflicts


def _config(status=None, buttons=None) -> dict:
    return {"status": status or {}, "buttons": buttons or {}}


def test_defaults_have_no_conflicts():
    # The defaults carry pin suggestions for every backend, but only `log` is
    # enabled -- an unused pin must never be reported.
    assert find_gpio_conflicts(load_config(None)) == []


def test_only_enabled_backends_claim_lines():
    config = _config(
        {
            "backends": ["log", "epaper"],
            "grove_led_bar": {"clock_line": 18, "data_line": 17},
            "epaper": {"model": "waveshare-1.54", "dc": 25, "rst": 17, "busy": 24},
        }
    )
    owners = {claim.owner for claim in gpio_claims(config)}
    assert owners == {"status.epaper.dc", "status.epaper.rst", "status.epaper.busy"}
    assert find_gpio_conflicts(config) == []


def test_grove_and_epaper_sharing_a_line_is_reported():
    # The collision the Pi example used to invite: Grove DI and the panel RST
    # both on BCM17.
    config = _config(
        {
            "backends": ["log", "grove_led_bar", "epaper"],
            "grove_led_bar": {"clock_line": 6, "data_line": 17},
            "epaper": {"model": "waveshare-1.54", "dc": 25, "rst": 17, "busy": 24},
        }
    )
    messages = find_gpio_conflicts(config)
    assert len(messages) == 1
    assert "gpiochip0 line 17" in messages[0]
    assert "status.grove_led_bar.data_line" in messages[0]
    assert "status.epaper.rst" in messages[0]


def test_preset_supplied_power_pin_is_claimed():
    # `pwr` is not in the config at all -- it comes from the 2.13" HAT preset and
    # is still requested, so the check must see it.
    config = _config(
        {
            "backends": ["epaper", "grove_led_bar"],
            "epaper": {"model": "waveshare-2.13", "dc": 25, "rst": 17, "busy": 24},
            "grove_led_bar": {"clock_line": 18, "data_line": 5},
        }
    )
    messages = find_gpio_conflicts(config)
    assert len(messages) == 1
    assert "line 18" in messages[0] and "status.epaper.pwr" in messages[0]


def test_button_against_status_backend():
    config = _config(
        {"backends": ["led"], "led": {"lines": {"ready": 27, "busy": 22, "error": 23}}},
        {"userbutton_1": {"enabled": True, "line": 22}},
    )
    messages = find_gpio_conflicts(config)
    assert len(messages) == 1
    assert "buttons.userbutton_1.line" in messages[0]
    assert "status.led.lines.busy" in messages[0]


def test_disabled_button_claims_nothing():
    config = _config(
        {"backends": ["buzzer"], "buzzer": {"line": 12}},
        {"userbutton_1": {"enabled": False, "line": 12}},
    )
    assert find_gpio_conflicts(config) == []


def test_chip_spelling_is_normalised_but_different_chips_do_not_collide():
    same = _config(
        {
            "backends": ["buzzer", "grove_led_bar"],
            "buzzer": {"gpiochip": "/dev/gpiochip0", "line": 12},
            "grove_led_bar": {"gpiochip": "0", "clock_line": 12, "data_line": 5},
        }
    )
    assert len(find_gpio_conflicts(same)) == 1

    other = _config(
        {
            "backends": ["buzzer", "grove_led_bar"],
            "buzzer": {"gpiochip": "gpiochip1", "line": 12},
            "grove_led_bar": {"gpiochip": "gpiochip0", "clock_line": 12, "data_line": 5},
        }
    )
    assert find_gpio_conflicts(other) == []


def test_unresolvable_epaper_config_still_yields_its_pins():
    # An unknown model makes the resolver raise; the pins are still claimed (the
    # broken model itself is reported by the backend).
    config = _config(
        {
            "backends": ["epaper", "buzzer"],
            "epaper": {"model": "nope-1.0", "dc": 25, "rst": 17, "busy": 24},
            "buzzer": {"line": 24},
        }
    )
    messages = find_gpio_conflicts(config)
    assert len(messages) == 1
    assert "line 24" in messages[0]


def test_log_gpio_conflicts_warns(caplog):
    config = _config(
        {
            "backends": ["grove_led_bar", "buzzer"],
            "grove_led_bar": {"clock_line": 6, "data_line": 5},
            "buzzer": {"line": 6},
        }
    )
    with caplog.at_level(logging.WARNING, logger="copystation.pins"):
        messages = log_gpio_conflicts(config)
    assert len(messages) == 1
    assert "GPIO line conflict" in caplog.text
