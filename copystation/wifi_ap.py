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

:class:`ApController` is the one place that switches the AP at runtime, whatever
triggered it (user button, web interface, ``wifi-ap`` CLI): it gives the same
feedback, persists the same state and keeps the captive portal in step.
"""

from __future__ import annotations

import ipaddress
import logging
import subprocess
import threading
from typing import Any, Iterable, List, Sequence, Tuple

_LOG = logging.getLogger("copystation.wifi_ap")

# WPA2-PSK needs at least 8 characters; a shorter/empty password is rejected by
# NetworkManager, so we refuse to raise the AP and say why instead.
MIN_PSK_LEN = 8

# Interfaces that never conflict with the AP subnet (loopback is its own world).
_IGNORED_IFACES = ("lo",)


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


def addr_show_cmd() -> List[str]:
    """List the IPv4 addresses of every interface (one line each)."""
    return ["ip", "-4", "-o", "addr", "show"]


def device_status_cmd() -> List[str]:
    """List every NetworkManager device with its type (one line each)."""
    return ["nmcli", "-t", "-f", "DEVICE,TYPE", "device", "status"]


# ----- address conflict detection --------------------------------------------
#
# The AP subnet is served by NetworkManager's `ipv4.method shared`. If it clashes
# with a network the station is ALREADY on (typically the LAN behind eth0), the
# box ends up with two routes into the same subnet and answers LAN hosts over
# Wi-Fi -- an established SSH session over Ethernet dies the moment the AP comes
# up. That is a config mistake we cannot fix, but we can name it precisely
# instead of letting the user hunt a "the network breaks randomly" ghost.


def parse_addr_show(output: str) -> List[Tuple[str, str]]:
    """Parse ``ip -4 -o addr show`` into ``(ifname, cidr)`` pairs.

    Each line looks like ``2: eth0    inet 192.168.1.50/24 brd ... scope global``;
    anything unparseable is skipped rather than raising -- this only feeds a
    warning.
    """
    found: List[Tuple[str, str]] = []
    for line in (output or "").splitlines():
        parts = line.split()
        if len(parts) < 4 or "inet" not in parts:
            continue
        at = parts.index("inet")
        if at < 2 or at + 1 >= len(parts):
            continue
        cidr, ifname = parts[at + 1], parts[1].rstrip(":")
        if "/" in cidr and ifname:
            found.append((ifname, cidr))
    return found


def parse_wifi_ifnames(output: str) -> List[str]:
    """Pick the Wi-Fi device names out of ``nmcli -t -f DEVICE,TYPE device status``."""
    names: List[str] = []
    for line in (output or "").splitlines():
        parts = line.split(":")
        if len(parts) >= 2 and parts[1].strip() == "wifi" and parts[0].strip():
            names.append(parts[0].strip())
    return names


def address_conflicts(
    ap_cidr: str,
    existing: Iterable[Tuple[str, str]],
    skip: Sequence[str] = (),
) -> List[str]:
    """Describe every existing address that collides with the AP subnet.

    Two kinds of collision, both fatal for the existing network:

    * the exact same address is already configured elsewhere (duplicate IP), or
    * the subnets overlap, so the routing table gets a second path into them.

    Returns one human-readable line per conflict (empty = all clear).
    """
    try:
        ap_iface = ipaddress.ip_interface(str(ap_cidr).strip())
    except ValueError:
        return []
    skipped = set(_IGNORED_IFACES) | {s for s in skip if s}
    messages: List[str] = []
    for ifname, cidr in existing:
        if ifname in skipped:
            continue
        try:
            other = ipaddress.ip_interface(cidr)
        except ValueError:
            continue
        if other.ip == ap_iface.ip:
            messages.append(
                f"{ifname} already carries {other.ip} -- the AP would be a DUPLICATE address"
            )
        elif other.network.overlaps(ap_iface.network):
            messages.append(
                f"{ifname} is on {other.network} which OVERLAPS the AP subnet "
                f"{ap_iface.network}"
            )
    return messages


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


def local_addresses() -> List[Tuple[str, str]]:
    """Current ``(ifname, cidr)`` pairs of this machine (empty if ``ip`` fails)."""
    try:
        return parse_addr_show(_run(addr_show_cmd(), check=True).stdout)
    except (OSError, subprocess.CalledProcessError):
        return []


def ap_ifnames(cfg: Any) -> List[str]:
    """The interfaces the AP may occupy itself -- never a conflict worth warning about.

    The configured ``ifname`` when there is one, otherwise every Wi-Fi device,
    because that is what NetworkManager picks the AP interface from. Raising the
    AP takes that device over regardless, so an address already sitting on it is
    either our own AP from a moment ago or a client connection that ends anyway
    -- neither is the LAN-breaking clash this check exists for.
    """
    configured = str(_cfg(cfg, "ifname", "") or "").strip()
    if configured:
        return [configured]
    try:
        return parse_wifi_ifnames(_run(device_status_cmd(), check=True).stdout)
    except (OSError, subprocess.CalledProcessError):
        return []


def log_address_conflicts(cfg: Any) -> List[str]:
    """Warn if the AP subnet collides with a network this station is already on.

    Called just before the AP is raised. The AP's own interface is skipped: the
    profile has just been deleted and re-created, but NetworkManager drops the
    old address asynchronously, so re-raising an AP that is already up would
    otherwise report its own address as a duplicate on every daemon restart.
    Purely advisory: the AP is still brought up (the user may know exactly what
    they are doing), but the journal now names both sides.
    """
    ap_cidr = str(_cfg(cfg, "ipv4_address", "") or "")
    if not ap_cidr:
        return []
    conflicts = address_conflicts(ap_cidr, local_addresses(), skip=ap_ifnames(cfg))
    for message in conflicts:
        _LOG.warning(
            "WiFi AP address conflict: %s. Existing connections over that "
            "interface (SSH included) will break while the AP is up -- pick a "
            "wifi_ap.ipv4_address in a subnet you do not otherwise use.",
            message,
        )
    return conflicts


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


def forget(cfg: Any) -> None:
    """Remove the profile so NetworkManager cannot auto-activate it at boot.

    The profile carries ``autoconnect yes``, which is what keeps a *running* AP
    alive across an interface flap -- but a profile left behind after the AP is
    switched off is auto-activated by NetworkManager on the next boot, seconds
    into it and long before this daemon starts. The AP would then broadcast, run
    WPA2 and hand out DHCP leases for ~20 s before the startup reconcile takes it
    down again, despite being persisted off. Deleting costs nothing: every
    bring-up goes through :func:`ensure_profile`, which builds the profile from
    the config from scratch anyway.
    """
    try:
        _run(delete_cmd(cfg), check=False)  # ignore 'unknown connection'
    except OSError as exc:  # pragma: no cover - defensive
        _LOG.warning("WiFi AP profile could not be removed: %s", exc)


# nmcli's exit code for "connection, device, or access point does not exist".
# Since switching the AP off deletes the profile, switching it off *again* --
# or at startup when it is already down -- finds nothing to bring down. That is
# the desired end state, not a failure worth a warning.
_NMCLI_NOT_FOUND = 10


def down(cfg: Any) -> bool:
    """Bring the AP down and drop its profile (see :func:`forget`).

    Idempotent: with no profile left there is nothing to bring down, which nmcli
    reports as an error but is exactly the state we want.
    """
    ok = True
    try:
        _run(down_cmd(cfg), check=True)
    except subprocess.CalledProcessError as exc:
        if exc.returncode == _NMCLI_NOT_FOUND:
            _LOG.debug("WiFi AP already down (no '%s' profile)", con_name(cfg))
        else:
            _LOG.warning("WiFi AP could not be brought down: %s", exc)
            ok = False
    except OSError as exc:
        _LOG.warning("WiFi AP could not be brought down: %s", exc)
        ok = False
    # Even when the 'down' failed: an orphaned profile is exactly what would
    # raise the AP behind our back at the next boot.
    forget(cfg)
    return ok


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
    log_address_conflicts(cfg)  # advisory: names an AP subnet that breaks the LAN
    ok = up(cfg)
    if ok:
        _LOG.info(
            "WiFi AP '%s' up (SSID %r, %s)",
            con_name(cfg), _cfg(cfg, "ssid", "Copy_Station"),
            _cfg(cfg, "ipv4_address", "shared"),
        )
    return ok


def set_active(cfg: Any, active: bool) -> bool:
    """Bring the AP up (``active=True``) or down; return the resulting state.

    Unlike :func:`toggle` this takes the desired direction as an argument, so the
    caller can decide the target (e.g. by flipping a cached state) and show the
    indication *before* this slow nmcli call runs. Returns whether the AP is up
    afterwards (``False`` if a requested bring-up failed).
    """
    if active:
        _LOG.info("WiFi AP: bringing '%s' up", con_name(cfg))
        return bool(start_ap(cfg))
    _LOG.info("WiFi AP: bringing '%s' down", con_name(cfg))
    down(cfg)
    return False


def toggle(cfg: Any) -> bool:
    """Flip the AP and return whether it is **active afterwards**.

    Down if it was active, otherwise ensure the profile and bring it up. The
    return value (True = now up, False = now down) lets the caller show the right
    indication. Bound to a user button via the ``wifi_ap`` action keyword.
    """
    return set_active(cfg, not is_active(cfg))


class ApController:
    """Switch the AP at runtime -- one behaviour for every trigger.

    A user button, the web interface and the ``wifi-ap`` CLI all go through here,
    so each of them gives the same feedback, writes the same persisted state and
    keeps the captive portal in step. Without a controller the "no button
    attached" case is a dead end: the AP can then only be changed by editing the
    config and restarting.

    The indication is updated *first*, before the (slow, several-second) nmcli
    call, so the display badge and the WS2812 blink code react the instant the
    press/click is recognised; if the bring-up then fails, display and persisted
    state are corrected. ``settings`` is the ``wifi_ap`` section of the shared
    user-settings overlay (the persisted state wins over ``wifi_ap.enabled`` on
    the next start), ``portal`` an optional :class:`~copystation.captive_portal.
    CaptivePortal` that follows the AP up and down.
    """

    def __init__(self, config: Any = None, hub: Any = None,
                 settings: Any = None, portal: Any = None) -> None:
        self._config = config
        self._hub = hub
        self._settings = settings
        self._portal = portal
        # Triggers live on different threads (button poll loop, web threadpool):
        # serialise them so two switches never interleave their nmcli calls.
        self._lock = threading.Lock()

    @property
    def cfg(self) -> Any:
        return (self._config.get("wifi_ap") if self._config is not None else None) or {}

    def known_active(self) -> bool:
        """Last known AP state -- from memory when possible, else ask nmcli."""
        if self._hub is not None:
            return bool(self._hub.state.ap_active)
        return is_active(self.cfg)

    def apply(self, active: bool) -> bool:
        """Bring the AP to ``active``; return whether it is up afterwards."""
        active = bool(active)
        with self._lock:
            if self._hub is None:
                actual = set_active(self.cfg, active)
                self._persist(actual)
                self._sync_portal(actual)
                return actual

            from .status import Event

            # Instant feedback (display + LED) + persist before the slow network op.
            self._hub.set_ap_active(active)
            self._hub.signal(Event.AP_ENABLED if active else Event.AP_DISABLED)
            self._persist(active)
            actual = set_active(self.cfg, active)
            if actual != active:
                # The bring-up failed (e.g. no valid password): correct the display
                # and the persisted state.
                self._hub.set_ap_active(actual)
                self._persist(actual)
            self._sync_portal(actual)
            return actual

    def flip(self) -> bool:
        """Toggle the AP; return whether it is up afterwards."""
        if self._hub is None:
            # No cached state to flip -- let nmcli decide the direction.
            with self._lock:
                actual = toggle(self.cfg)
                self._persist(actual)
                self._sync_portal(actual)
                return actual
        return self.apply(not self.known_active())

    def _persist(self, active: bool) -> None:
        if self._settings is not None:
            self._settings.update(enabled=bool(active))

    def _sync_portal(self, active: bool) -> None:
        if self._portal is None:
            return
        try:
            self._portal.sync(active)
        except Exception as exc:  # pragma: no cover - best effort
            _LOG.warning("Captive portal could not follow the AP state: %s", exc)
