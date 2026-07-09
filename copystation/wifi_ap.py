"""Optional WLAN access point via NetworkManager (nmcli).

Lets the station host its own network in the field so the web interface is
reachable without an existing LAN. The daemon calls :func:`start_ap` on startup
when ``wifi_ap.enabled`` is set: it (re)creates a NetworkManager connection
profile from the config and brings it up. ``ipv4.method shared`` makes
NetworkManager run DHCP + NAT on the AP subnet automatically, so a client that
associates gets an address and can reach ``http://<ipv4_address>:<web.port>/``.

The command *builders* are pure functions returning an argument list (no shell),
so they are unit-testable without NetworkManager; the thin runners below execute
them with ``subprocess``. Everything is best-effort: a failure is logged and the
rest of the daemon keeps running.
"""

from __future__ import annotations

import logging
import subprocess
from typing import Any, List

_LOG = logging.getLogger("copystation.wifi_ap")

# WPA2-PSK needs at least 8 characters; a shorter/empty password is rejected by
# NetworkManager, so we refuse to raise the AP and say why instead.
MIN_PSK_LEN = 8


def _cfg(cfg: Any, key: str, default: Any = None) -> Any:
    return (cfg or {}).get(key, default)


def con_name(cfg: Any) -> str:
    return str(_cfg(cfg, "con_name", "copystation-ap"))


def delete_cmd(cfg: Any) -> List[str]:
    """Remove the profile (idempotent -- ignore 'unknown connection')."""
    return ["nmcli", "connection", "delete", con_name(cfg)]


def add_cmd(cfg: Any) -> List[str]:
    """Full ``nmcli connection add`` for the hotspot profile.

    Built from scratch each time (after a delete) so the profile always matches
    the current config -- no drift between an old profile and new settings.
    """
    ssid = str(_cfg(cfg, "ssid", "Copy_Station"))
    ifname = str(_cfg(cfg, "ifname", "") or "*")
    autoconnect = "yes" if _cfg(cfg, "autoconnect", True) else "no"
    cmd = [
        "nmcli", "connection", "add", "type", "wifi",
        "con-name", con_name(cfg),
        "ifname", ifname,
        "ssid", ssid,
        "autoconnect", autoconnect,
        "802-11-wireless.mode", "ap",
        "ipv4.method", "shared",
    ]
    band = str(_cfg(cfg, "band", "") or "")
    if band:
        cmd += ["802-11-wireless.band", band]
    channel = _cfg(cfg, "channel")
    if channel:
        cmd += ["802-11-wireless.channel", str(channel)]
    ipv4 = str(_cfg(cfg, "ipv4_address", "") or "")
    if ipv4:
        cmd += ["ipv4.addresses", ipv4]
    password = str(_cfg(cfg, "password", "") or "")
    if password:
        cmd += ["wifi-sec.key-mgmt", "wpa-psk", "wifi-sec.psk", password]
    return cmd


def up_cmd(cfg: Any) -> List[str]:
    return ["nmcli", "connection", "up", con_name(cfg)]


def down_cmd(cfg: Any) -> List[str]:
    return ["nmcli", "connection", "down", con_name(cfg)]


def active_cmd() -> List[str]:
    return ["nmcli", "-t", "-f", "NAME", "connection", "show", "--active"]


# ----- runners ---------------------------------------------------------------


def _run(cmd: List[str], check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, check=check)


def _valid_psk(cfg: Any) -> bool:
    """A usable AP needs a WPA2 password of at least 8 characters."""
    password = str(_cfg(cfg, "password", "") or "")
    if len(password) < MIN_PSK_LEN:
        _LOG.warning(
            "WiFi AP not raised: set wifi_ap.password to at least %d characters "
            "(WPA2 requirement).",
            MIN_PSK_LEN,
        )
        return False
    return True


def ensure_profile(cfg: Any) -> bool:
    """(Re)create the NetworkManager hotspot profile from the config."""
    _run(delete_cmd(cfg), check=False)  # ignore 'unknown connection'
    try:
        _run(add_cmd(cfg), check=True)
    except (OSError, subprocess.CalledProcessError) as exc:
        _LOG.warning("WiFi AP profile could not be created: %s", exc)
        return False
    return True


def up(cfg: Any) -> bool:
    try:
        _run(up_cmd(cfg), check=True)
    except (OSError, subprocess.CalledProcessError) as exc:
        _LOG.warning("WiFi AP could not be brought up: %s", exc)
        return False
    return True


def down(cfg: Any) -> bool:
    try:
        _run(down_cmd(cfg), check=True)
    except (OSError, subprocess.CalledProcessError) as exc:
        _LOG.warning("WiFi AP could not be brought down: %s", exc)
        return False
    return True


def is_active(cfg: Any) -> bool:
    try:
        out = _run(active_cmd(), check=True).stdout
    except (OSError, subprocess.CalledProcessError):
        return False
    return con_name(cfg) in out.splitlines()


def start_ap(cfg: Any) -> bool:
    """Ensure the profile exists and bring the AP up. Called by the daemon."""
    if not _valid_psk(cfg):
        return False
    if not ensure_profile(cfg):
        return False
    ok = up(cfg)
    if ok:
        _LOG.info(
            "WiFi AP '%s' up (SSID %r, %s)",
            con_name(cfg), _cfg(cfg, "ssid", "Copy_Station"),
            _cfg(cfg, "ipv4_address", "shared"),
        )
    return ok


def toggle(cfg: Any) -> bool:
    """Flip the AP and return whether it is **active afterwards**.

    Down if it was active, otherwise ensure the profile and bring it up. The
    return value (True = now up, False = now down) lets the caller show the right
    indication. Bound to a user button via the ``wifi_ap`` action keyword.
    """
    if is_active(cfg):
        _LOG.info("WiFi AP toggle: bringing '%s' down", con_name(cfg))
        down(cfg)
        return False
    _LOG.info("WiFi AP toggle: bringing '%s' up", con_name(cfg))
    return bool(start_ap(cfg))
