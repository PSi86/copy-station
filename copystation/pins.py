"""Static GPIO line map of the configuration (startup sanity check).

Every hardware feature requests its libgpiod lines the moment it is initialised,
and a line has exactly ONE owner: the second request fails with EBUSY. Since the
status backends are opened one after another (in the order of
``status.backends``) and the user buttons after them, such a collision surfaces
as an opaque "Device or resource busy" on whichever feature happened to come
second -- with no hint that the first one is the actual culprit.

This module derives the claimed lines from the *configuration*, before anything
is opened, so the daemon can name both sides of the collision.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from .status.gpio import chip_name

_LOG = logging.getLogger("copystation.pins")

# Status backends that do not claim any GPIO line (log-only, or SPI-driven).
_NO_GPIO_BACKENDS = ("log", "ws2812")


@dataclass(frozen=True)
class Claim:
    """One configured GPIO line and the config key that claims it."""

    chip: str      # normalised chip name, e.g. "gpiochip0"
    offset: int
    owner: str     # config path, e.g. "status.epaper.rst"


def _add(claims: list[Claim], chip, offset, owner: str) -> None:
    """Append a claim, skipping unset (``None``) or non-numeric line values."""
    if offset is None:
        return
    try:
        line = int(offset)
    except (TypeError, ValueError):
        return
    claims.append(Claim(chip=chip_name(chip), offset=line, owner=owner))


def _epaper_pins(cfg: dict) -> dict:
    """The e-paper pins as the backend will request them (preset defaults applied).

    ``pwr`` may come from the panel preset (the 2.13" HATs gate panel power on
    BCM18), so the raw config alone would miss that claim. A config the resolver
    rejects is reported by the backend itself -- fall back to the raw values.
    """
    try:
        from .status.epaper.presets import resolve_panel

        return resolve_panel(cfg)
    except Exception:
        return cfg


def _status_claims(status_cfg: dict) -> list[Claim]:
    claims: list[Claim] = []
    for name in status_cfg.get("backends") or []:
        if name in _NO_GPIO_BACKENDS:
            continue
        cfg = status_cfg.get(name) or {}
        chip = cfg.get("gpiochip", "gpiochip0")
        if name == "led":
            for role, line in (cfg.get("lines") or {}).items():
                _add(claims, chip, line, f"status.led.lines.{role}")
        elif name == "buzzer":
            _add(claims, chip, cfg.get("line"), "status.buzzer.line")
        elif name == "grove_led_bar":
            _add(claims, chip, cfg.get("clock_line"), "status.grove_led_bar.clock_line")
            _add(claims, chip, cfg.get("data_line"), "status.grove_led_bar.data_line")
        elif name == "epaper":
            panel = _epaper_pins(cfg)
            for role in ("dc", "rst", "busy", "pwr", "cs"):
                _add(claims, chip, panel.get(role), f"status.epaper.{role}")
    return claims


def _button_claims(buttons_cfg: dict) -> list[Claim]:
    claims: list[Claim] = []
    for name in sorted(buttons_cfg or {}):
        cfg = (buttons_cfg or {}).get(name) or {}
        if not cfg.get("enabled"):
            continue
        _add(claims, cfg.get("gpiochip", "gpiochip0"), cfg.get("line"),
             f"buttons.{name}.line")
    return claims


def gpio_claims(config) -> list[Claim]:
    """Every GPIO line the *enabled* features of ``config`` will request.

    ``config`` is a :class:`~copystation.config.Config` (any mapping with
    ``get`` works). Disabled backends/buttons and unset (``None``) lines are not
    claimed and therefore never reported.
    """
    claims = _status_claims(config.get("status", {}) or {})
    claims.extend(_button_claims(config.get("buttons") or {}))
    return claims


def find_gpio_conflicts(config) -> list[str]:
    """Human-readable message per GPIO line that more than one feature claims."""
    by_line: dict[tuple[str, int], list[str]] = {}
    for claim in gpio_claims(config):
        by_line.setdefault((claim.chip, claim.offset), []).append(claim.owner)

    messages = []
    for (chip, offset), owners in by_line.items():
        if len(owners) < 2:
            continue
        messages.append(
            f"GPIO line conflict: {chip} line {offset} is claimed by "
            f"{' and '.join(owners)} -- whichever is initialised second fails with "
            "'Device or resource busy' (EBUSY). Move one of them to a free line "
            "(see `gpioinfo`) or drop the feature you do not use."
        )
    return messages


def log_gpio_conflicts(config, log: logging.Logger | None = None) -> list[str]:
    """Warn about every configured GPIO collision; returns the messages."""
    messages = find_gpio_conflicts(config)
    for message in messages:
        (log or _LOG).warning("%s", message)
    return messages
