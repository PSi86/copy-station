"""Load configuration and merge it with defaults.

The configuration is intentionally designed so that the service runs even
without a config.yaml: in that case the defaults apply (status only via the log
backend, source identification purely by the presence of a DCIM folder).
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError:  # pragma: no cover - YAML is optional, defaults suffice
    yaml = None


# Name of the media folder on the source that is copied and then cleared.
DCIM_DIRNAME = "DCIM"

# Base directory under which the daemon mounts candidate partitions at runtime
# (one subfolder per device, e.g. /run/copystation/mnt/sda1).
DEFAULT_MOUNT_BASE = "/run/copystation/mnt"


DEFAULTS: dict[str, Any] = {
    # Base directory for the per-device mountpoints.
    "mount_base": DEFAULT_MOUNT_BASE,
    # Name of the media subfolder on the source.
    "media_dirname": DCIM_DIRNAME,
    # Source identification.
    "identify": {
        # The source is primarily detected by its DCIM folder. Optional
        # hardening via a USB VID/PID allowlist (lowercase strings), e.g. "2ca3".
        "source_usb_vendor_ids": [],
        "source_usb_product_ids": [],
        # Partitions smaller than this are ignored (e.g. boot/EFI partitions).
        "min_partition_gb": 6,
        # Require the source to be smaller than the target, so the larger device
        # is never used as source even if it also carries a DCIM folder.
        "require_source_smaller_than_target": True,
        # Friendly web-UI names matched by USB VID/PID (the O4's USB product
        # string is only a serial). 2ca3:0020 is the DJI O4 Lite (confirmed);
        # adjust/add entries for other models as their VID/PIDs are confirmed.
        "device_labels": [
            {"vid": "2ca3", "pid": "0020", "name": "O4 Lite"},
        ],
    },
    # Cleanup behaviour.
    "cleanup": {
        # True: delete the contents of DCIM, keep the DCIM folder itself.
        # False: remove the whole DCIM folder (the camera recreates it).
        "keep_dcim_folder": True,
    },
    # Status indication. Multiple backends can be combined.
    # Available: "log", "led", "buzzer", "ws2812".
    "status": {
        "backends": ["log"],
        # GPIO pin assignment for the LED backend (gpiochip name + line offsets).
        # Fill in only after running `gpiodetect`/`gpioinfo` on the Cubie.
        "led": {
            "gpiochip": "gpiochip0",
            "lines": {"ready": None, "busy": None, "error": None},
        },
        "buzzer": {
            "gpiochip": "gpiochip0",
            "line": None,
        },
        # Addressable WS2812B / NeoPixel strip (1-10 LEDs), driven over SPI.
        # First LED shows the status colour; during a copy LEDs 1..N form a
        # blinking progress bar.
        "ws2812": {
            "device": "/dev/spidev0.0",
            "led_count": 1,
        },
        # Grove LED Bar v2.0 (MY9221), bit-banged over two GPIO lines.
        # Fill in line offsets after `gpioinfo` on the Cubie.
        "grove_led_bar": {
            "gpiochip": "gpiochip0",
            "clock_line": None,
            "data_line": None,
            # Flip if segment 1 is the wrong physical end of the bar.
            "reverse": False,
        },
    },
    # Optional local web interface (off by default).
    "web": {
        "enabled": False,
        "host": "0.0.0.0",  # all interfaces; robust to interfaces up/down
        "port": 8080,
    },
    # Optional GPIO shutdown button (off by default). Held for hold_seconds it
    # runs a clean `systemctl poweroff` -- the safe way to power the station down.
    "power": {
        "shutdown_button": {
            "enabled": False,
            "gpiochip": "gpiochip0",
            "line": None,          # line offset of the button (BCM number on Pi)
            "active_low": True,    # pressed = pulled to GND (button to GND)
            "bias": "pull_up",     # pull_up | pull_down | disable | as_is
            "hold_seconds": 1.0,   # must be held this long to trigger
            "action": "poweroff",  # poweroff | reboot
        },
    },
    # Hard upper bound (seconds) for how long detection waits after a udev add
    # event before partitions are detected/mounted. The adaptive debounce
    # (settle_quiet_seconds) usually proceeds sooner.
    "settle_seconds": 2.0,
    # Adaptive debounce: proceed this many seconds after the last USB event,
    # capped by settle_seconds.
    "settle_quiet_seconds": 1.0,
}


@dataclass
class Config:
    """Loaded configuration, merged with the defaults."""

    data: dict[str, Any] = field(default_factory=lambda: copy.deepcopy(DEFAULTS))

    def __getitem__(self, key: str) -> Any:
        return self.data[key]

    def get(self, key: str, default: Any = None) -> Any:
        return self.data.get(key, default)

    @property
    def mount_base(self) -> Path:
        return Path(self.data["mount_base"])

    @property
    def media_dirname(self) -> str:
        return self.data["media_dirname"]


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Recursively layer ``override`` on top of ``base`` (nested dicts merge)."""
    result = copy.deepcopy(base)
    for key, value in override.items():
        if (
            key in result
            and isinstance(result[key], dict)
            and isinstance(value, dict)
        ):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def load_config(path: str | Path | None = None) -> Config:
    """Load the configuration.

    If no file exists (or PyYAML is not installed), the defaults are used. An
    existing file is merged over the defaults, so even a partial config.yaml is
    valid.
    """
    if path is None:
        return Config(copy.deepcopy(DEFAULTS))

    path = Path(path)
    if not path.exists():
        return Config(copy.deepcopy(DEFAULTS))

    if yaml is None:  # pragma: no cover - only without PyYAML
        raise RuntimeError(
            "config.yaml present but PyYAML is not installed "
            "(pip install pyyaml)"
        )

    with path.open("r", encoding="utf-8") as fh:
        loaded = yaml.safe_load(fh) or {}

    if not isinstance(loaded, dict):
        raise ValueError(f"Invalid configuration in {path}: expected a mapping")

    return Config(_deep_merge(DEFAULTS, loaded))
