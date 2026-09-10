"""Entry point, transfer orchestration and main loop.

Two modes of operation:

* ``--simulate <source> <target>`` runs ONE transfer with two local folders
  (source contains a DCIM folder, target is the "SD root"). This lets the whole
  core logic be checked on the dev machine without hardware/udev.

* without ``--simulate`` the event-driven daemon runs (Linux/Cubie only): it
  listens via pyudev for USB mass storage, detects source (DCIM) and target,
  transfers, verifies and clears the source.
"""

from __future__ import annotations

import argparse
import logging
import os
import shutil
import sys
import time
from pathlib import Path

from .config import Config, load_config
from .media import recording_files
from .naming import next_transfer_dir
from .settings_store import SettingsStore
from .state import StationState, StatusHub, StorageInfo
from .status import Event, State, build_indicator
from .transfer import (
    SourceVanishedError,
    TransferError,
    check_free_space,
    cleanup_source,
    copy_tree,
    delete_files,
    total_size,
    verify,
    volume_alive,
)

_LOG = logging.getLogger("copystation")


def storage_info(path: Path, label: str = "") -> StorageInfo:
    """Capacity/used/free of the filesystem holding ``path`` (best effort)."""
    try:
        usage = shutil.disk_usage(path)
        return StorageInfo(
            label=label,
            capacity=usage.total,
            used=usage.total - usage.free,
            free=usage.free,
        )
    except OSError:
        return StorageInfo(label=label)


def _device_abort_check(
    source_device: str | None,
    target_device: str | None,
    source_root: Path | None = None,
    target_root: Path | None = None,
):
    """Abort callback that fires if the source OR target volume goes away.

    Returns a labelled reason string so the failure says which side was unplugged,
    or None while both are still present. None if there is nothing to watch.

    Each side is checked with :func:`volume_alive` (device node + the kernel's
    backing-disk capacity), which catches both a whole device being unplugged and
    a card pulled from a reader whose node lingers -- passively, without writing
    to either volume.
    """
    watched = [(d, m, label) for d, m, label in
               ((source_device, source_root, "Source"),
                (target_device, target_root, "Target")) if d or m is not None]
    if not watched:
        return None

    def _check():
        for node, mount, label in watched:
            if not volume_alive(node, mount):
                return f"{label} disconnected. Nothing was deleted -- reconnect and retry."
        return None

    return _check


def perform_transfer(
    source_root: Path,
    target_root: Path,
    source_name: str | None,
    hub: StatusHub,
    config: Config,
    source_device: str | None = None,
    target_device: str | None = None,
    target_name: str | None = None,
    on_devices_refresh=None,
    required: int | None = None,
    root_media: bool = False,
) -> Path:
    """Perform a complete transfer.

    The order is safety critical: copy -> verify -> ONLY THEN clear the source.
    Any error before successful verification leaves the source untouched.
    Returns the created target folder.

    ``root_media`` marks a source that records to the root of its storage (see
    ``identify.root_media_sources``) instead of into ``media_dirname``. Its
    recordings are individual files there (:func:`~copystation.media.recording_files`),
    and that one list is copied, verified and then deleted -- everything else on
    the source stays, and no folder is ever removed.

    ``source_device`` / ``target_device`` (e.g. ``/dev/sdc``) let the copy abort
    promptly if either is unplugged mid-transfer, rather than waiting for the I/O
    timeout. ``on_devices_refresh`` (optional) is called to re-measure the devices
    for the web view: throttled during the copy (target filling up) and once after
    the source has been cleared (source now empty). ``required`` is the source
    media size in bytes; pass it to skip a re-scan when the caller already
    measured it (the daemon does this during the pre-copy gauge hold so the copy
    bar appears promptly).
    """
    source_root = Path(source_root)
    target_root = Path(target_root)
    if root_media:
        media_dir = source_root
        files = recording_files(source_root)
        if not files:
            raise TransferError("No recordings on the source.")
    else:
        media_dir = source_root / config.media_dirname
        files = None
        if not media_dir.is_dir():
            raise TransferError(f"No {config.media_dirname} folder on the source.")

    src_label = source_name or "source"
    tgt_label = target_name or "target"
    hub.set_storage(storage_info(source_root, src_label), storage_info(target_root, tgt_label))

    if required is None:
        required = total_size(media_dir, files)
    check_free_space(target_root, required)

    dest = next_transfer_dir(target_root, source_name)
    _LOG.info("Copying %s -> %s (%d bytes)", media_dir, dest, required)
    hub.log_event(f"Copy started: {dest.name}")
    hub.begin_transfer(dest.name, required)
    # Abort the copy quickly if the source or target volume disappears (unplugged
    # device OR a stale mount, so a vanished target can't go unnoticed).
    abort_check = _device_abort_check(
        source_device, target_device, source_root, target_root
    )

    # Progress handler that also refreshes the device view, throttled to ~1 s so
    # the panel tracks the filling target without re-stat'ing on every line.
    last_refresh = [0.0]

    def _on_progress(done: int) -> None:
        hub.update_progress(done)
        if on_devices_refresh is not None:
            now = time.monotonic()
            if now - last_refresh[0] >= 1.0:
                last_refresh[0] = now
                on_devices_refresh()

    copy_tree(media_dir, dest, on_progress=_on_progress, abort_check=abort_check,
              files=files)
    # Safety net: if the target vanished during the copy but rsync still
    # "finished" (writes buffered in the page cache, never flushed to a gone
    # device), do NOT verify/clear -- the data never reached the target.
    if abort_check is not None:
        reason = abort_check()
        if reason:
            raise SourceVanishedError(reason)
    hub.finish_transfer()
    if on_devices_refresh is not None:
        on_devices_refresh()  # final figures: target now holds the full copy

    _LOG.info("Verifying transfer ...")
    hub.log_event("Verifying ...")
    verify(media_dir, dest, files=files)

    hub.log_event("Clearing source ...")
    if files is None:
        keep = config.get("cleanup", {}).get("keep_dcim_folder", True)
        _LOG.info("Verification ok -- clearing source (keep_folder=%s)", keep)
        cleanup_source(media_dir, keep_folder=keep)
    else:
        _LOG.info("Verification ok -- deleting the %d copied recording file(s)", len(files))
        delete_files(media_dir, files)
    if on_devices_refresh is not None:
        on_devices_refresh()  # source DCIM now empty -> "empty" flag + freed space

    hub.log_event(f"Copy complete: {dest.name}")
    hub.set_phase(State.SUCCESS)
    # Refresh storage figures after the copy (free space changed).
    hub.set_storage(storage_info(source_root, src_label), storage_info(target_root, tgt_label))
    _LOG.info("Transfer complete: %s", dest)
    return dest


def _maybe_start_web(hub: StatusHub, config: Config, features=None,
                     ap_control=None) -> bool:
    """Start the web interface if enabled. Returns True if it was started.

    ``features`` is an optional prebuilt ``(browse, transcode, preview)`` tuple so
    the daemon can share the transcode manager with the device watcher (for
    auto-transcode); when omitted the features are built here (simulation path).
    ``ap_control`` is the WiFi-AP controller backing the interface's AP switch
    (``None`` -> the frontend hides it).

    True also covers "started, but still waiting for its address to appear" --
    that wait happens in the web server's own thread, so the daemon walks straight
    on to the device watcher and the station copies regardless of the network.
    """
    web_cfg = config.get("web", {})
    if not web_cfg.get("enabled"):
        return False
    try:
        from .web import start_web_server

        browse, transcode, preview = features if features is not None \
            else _build_web_features(hub, config)
        start_web_server(
            hub.state,
            web_cfg.get("host", "0.0.0.0"),
            int(web_cfg.get("port", 8080)),
            config=config,
            browse=browse,
            transcode=transcode,
            preview=preview,
            wifi_ap=ap_control,
        )
        return True
    except Exception as exc:
        _LOG.warning("Web interface could not be started: %s", exc)
        return False


def _build_ap_controller(config: Config, hub: StatusHub = None,
                         ap_settings=None, portal=None):
    """The runtime AP switch shared by the web interface and the user button.

    ``None`` when the AP is not configured at all (no password -> it can never
    come up), so the web interface hides the switch instead of offering a control
    that cannot work.
    """
    ap_cfg = config.get("wifi_ap", {}) or {}
    if not str(ap_cfg.get("password", "") or ""):
        return None
    try:
        from .wifi_ap import ApController

        return ApController(config=config, hub=hub, settings=ap_settings, portal=portal)
    except Exception as exc:  # pragma: no cover - defensive
        _LOG.warning("WiFi AP control unavailable: %s", exc)
        return None


def _user_settings_file(config: Config) -> str:
    """Path of the single overlay holding all runtime-mutable user settings."""
    return str(config.get("user_settings_file", "/var/lib/copystation/user-settings.json"))


def _build_web_features(hub: StatusHub, config: Config, user_settings=None):
    """Construct the optional file-browser and transcode managers.

    Each is best-effort: a missing dependency (pyudev, ffmpeg) or a disabled
    feature yields ``None``, and the web app simply hides that panel. Never lets
    an optional feature stop the (status) web interface from coming up.

    ``user_settings`` is the shared :class:`SettingsStore`; the transcode manager
    is given its ``transcode`` section so it shares the single overlay file with
    the WiFi AP. When omitted (the simulation path) a private store is opened.

    Returns ``(browse, transcode, preview)`` where ``browse`` is passed to the web
    app only to expose the file endpoints -- it is ``None`` unless the file browser
    itself is enabled, even when transcoding (which needs its own mount access)
    is on. ``preview`` is the in-browser player backend.
    """
    files_enabled = (config.get("web", {}) or {}).get("files", {}).get("enabled", True)
    transcode_enabled = bool((config.get("transcode", {}) or {}).get("enabled"))

    if user_settings is None:
        user_settings = SettingsStore(_user_settings_file(config))

    browse = None
    if files_enabled or transcode_enabled:
        try:
            from .mounts import BrowseManager

            browse = BrowseManager(config)
        except Exception as exc:  # pragma: no cover - defensive
            _LOG.warning("File browser/mounts unavailable: %s", exc)

    transcode = None
    if transcode_enabled and browse is not None:
        try:
            from .transcode import TranscodeManager

            transcode = TranscodeManager(config, hub, browse,
                                         settings=user_settings.section("transcode"))
        except Exception as exc:  # pragma: no cover - defensive
            _LOG.warning("Transcoding unavailable: %s", exc)

    # In-browser preview/playback needs the file browser and ffmpeg; enabled with
    # the browser unless explicitly turned off (preview.enabled: false).
    preview = None
    preview_enabled = (config.get("preview", {}) or {}).get("enabled", True)
    if files_enabled and browse is not None and preview_enabled:
        try:
            from .preview import PreviewManager

            preview = PreviewManager(config, browse, hub.state)
        except Exception as exc:  # pragma: no cover - defensive
            _LOG.warning("Video preview unavailable: %s", exc)

    # Only expose the file endpoints when the browser itself is enabled.
    browse_out = browse if files_enabled else None
    return browse_out, transcode, (preview if files_enabled else None)


def run_simulation(args: argparse.Namespace, config: Config) -> int:
    """Run a transfer with local folders (development/test)."""
    state = StationState()
    hub = StatusHub(state, build_indicator(config, state=state))
    web_running = _maybe_start_web(hub, config)

    source = Path(args.source)
    target = Path(args.target)
    target.mkdir(parents=True, exist_ok=True)

    rc = 0
    hub.set_phase(State.DETECTING)
    try:
        dest = perform_transfer(
            source_root=source,
            target_root=target,
            source_name=args.source_name,
            hub=hub,
            config=config,
        )
        print(f"OK: {dest}")
    except TransferError as exc:
        hub.set_error(str(exc))
        _LOG.error("Transfer failed: %s", exc)
        rc = 1

    if web_running:
        # Keep the process (and the web UI) alive so the result can be inspected.
        _LOG.info("Web interface running; press Ctrl+C to stop.")
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:  # pragma: no cover
            pass
    else:
        hub.set_phase(State.READY)

    hub.close()
    return rc


def _effective_ap_enabled(config: Config, ap_settings) -> bool:
    """Whether the AP should be up: the persisted overlay wins over the config.

    A runtime toggle (web/button) writes ``enabled`` to the ``wifi_ap`` section of
    the shared user-settings overlay, so it survives a restart independent of
    ``wifi_ap.enabled`` in config.yaml; until a toggle happens the config applies.
    """
    if ap_settings.has("enabled"):
        return bool(ap_settings.get("enabled"))
    return bool((config.get("wifi_ap", {}) or {}).get("enabled"))


def _apply_wifi_ap_state(config: Config, want_up: bool) -> bool:
    """Bring the AP to ``want_up`` at startup; return whether it is up afterwards.

    When it should be OFF, a stale autoconnect profile that raised the AP is
    brought down, so a persisted 'off' really means off. Best-effort.
    """
    ap_cfg = config.get("wifi_ap", {}) or {}
    if want_up:
        try:
            from .wifi_ap import start_ap

            return bool(start_ap(ap_cfg))
        except Exception as exc:  # pragma: no cover - defensive
            _LOG.warning("WiFi AP could not be started: %s", exc)
            return False
    try:
        from .wifi_ap import down, forget, is_active

        if is_active(ap_cfg):
            _LOG.info("WiFi AP is active but persisted off -- bringing it down")
            down(ap_cfg)
        else:
            # Down already, but a leftover profile may still be on disk with
            # autoconnect enabled -- and that is what raises the AP at the NEXT
            # boot, before this reconcile can run. Drop it now so "persisted
            # off" also means "off during boot", not just afterwards.
            forget(ap_cfg)
    except Exception as exc:  # pragma: no cover - defensive
        _LOG.warning("WiFi AP reconcile failed: %s", exc)
    return False


def _maybe_prepare_captive_portal(config: Config):
    """Set up the optional captive portal (DNS hijack + port-80 redirect).

    Returns a *not yet listening* :class:`CaptivePortal` or ``None``. The DNS
    drop-in is written here -- before the AP is raised, so NetworkManager's shared
    dnsmasq reads it -- and is removed again when the feature is disabled so stale
    config never lingers. The redirect server itself binds the AP address, which
    only exists once the AP is up, so it is started later via ``portal.sync()``
    and follows every AP on/off from then on. Best-effort: any failure is logged
    and the AP still works, just without the auto-redirect.
    """
    ap_cfg = config.get("wifi_ap", {}) or {}
    try:
        from .captive_portal import remove_dnsmasq_hijack, write_dnsmasq_hijack, CaptivePortal
    except Exception as exc:  # pragma: no cover - defensive
        _LOG.warning("Captive portal module unavailable: %s", exc)
        return None

    if not ap_cfg.get("captive_portal"):
        remove_dnsmasq_hijack()  # clean up if it was enabled before
        return None

    web_cfg = config.get("web", {}) or {}
    if not web_cfg.get("enabled"):
        # Only a problem if the AP can actually be raised and joined: the portal
        # ships enabled, so for a station running without the web interface this
        # is a note, not a misconfiguration to shout about on every start.
        log = _LOG.warning if _ap_usable(config) else _LOG.info
        log("captive_portal is set but web.enabled is false -- no page to redirect to.")
        remove_dnsmasq_hijack()
        return None

    try:
        ip = str(ap_cfg.get("ipv4_address", "10.42.0.1/24")).split("/")[0]
        write_dnsmasq_hijack(ip)
        return CaptivePortal(ip, int(web_cfg.get("port", 8080)),
                             int(ap_cfg.get("captive_portal_port", 80)))
    except Exception as exc:
        _LOG.warning("Captive portal could not be prepared: %s", exc)
        return None


def _wifi_ap_bound_to_button(config: Config) -> bool:
    """True if any enabled user button binds the ``wifi_ap`` toggle action."""
    for entry in (config.get("buttons") or {}).values():
        if not (entry or {}).get("enabled"):
            continue
        actions = (entry or {}).get("actions") or {}
        if any(v == "wifi_ap" for v in actions.values()):
            return True
    return False


def _ap_usable(config: Config, ap_enabled: bool = False) -> bool:
    """Whether anything could actually raise the AP: config, overlay or a button.

    ``ap_enabled`` is the effective startup state (config + persisted overlay).
    Used to decide whether an AP-related misconfiguration is worth a warning: on a
    station that never raises an access point it is just noise.
    """
    return bool(ap_enabled
                or (config.get("wifi_ap", {}) or {}).get("enabled")
                or _wifi_ap_bound_to_button(config))


def web_bind_covers(host: str, address: str) -> bool:
    """Whether a server bound to ``host`` also answers on ``address``.

    A wildcard bind (the default ``0.0.0.0``) covers every interface, including
    ones that come and go; a concrete address covers only itself.
    """
    host = str(host or "").strip()
    return host in ("", "0.0.0.0", "::", "*") or host == str(address).strip()


def _check_ap_web_reachability(config: Config, web_up: bool,
                               ap_enabled: bool = False) -> None:
    """Warn about the classic 'AP up but web UI refused' misconfigurations.

    Two independent ways to end up with an access point nobody can use:

    * ``web.enabled`` is off -- nothing listens at all, so ``http://<ap-ip>:<port>/``
      is refused. The AP and web are independent flags, so enabling the AP (or
      binding it to a button) while leaving the web interface off is a common
      setup mistake.
    * ``web.host`` is a concrete address (typically the LAN IP) instead of
      ``0.0.0.0`` -- then the interface listens on that ONE address and is refused
      over the AP, whose address is a different one.

    The reachable URL is only logged when the bind actually covers the AP address,
    so the journal never advertises a URL that is refused.
    """
    ap_usable = _ap_usable(config, ap_enabled)
    web_cfg = config.get("web", {}) or {}
    port = web_cfg.get("port", 8080)
    ap_ip = str((config.get("wifi_ap", {}) or {}).get("ipv4_address", "10.42.0.1/24")).split("/")[0]
    host = str(web_cfg.get("host", "0.0.0.0"))
    covers_ap = web_bind_covers(host, ap_ip)

    if ap_usable and not web_cfg.get("enabled"):
        _LOG.warning(
            "WiFi AP is configured but web.enabled is false -- the AP has no web UI "
            "to serve, so http://<ap-ip>:%s/ will be refused. Set web.enabled: true "
            "to reach the interface over the access point.",
            port,
        )
    elif ap_usable and not covers_ap:
        _LOG.warning(
            "WiFi AP is configured but web.host is %r -- the web interface listens "
            "on that address ONLY, so http://%s:%s/ will be refused over the AP. "
            "Set web.host: 0.0.0.0 to serve every interface.",
            host, ap_ip, port,
        )
    if web_up and covers_ap:
        _LOG.info("Web interface over the AP: http://%s:%s/", ap_ip, port)


def _maybe_start_buttons(config: Config, hub: StatusHub, transcode=None,
                         ap_settings=None, portal=None) -> list:
    """Start the optional GPIO user buttons. Returns them (possibly empty).

    ``hub`` is passed through so a ``wifi_ap`` / ``auto_transcode`` button action
    can update the display status and fire its WS2812 blink code; ``transcode`` is
    the manager the ``auto_transcode`` action toggles; ``ap_settings`` is the store
    the ``wifi_ap`` action persists the AP on/off state to and ``portal`` the
    captive portal that follows the AP up and down.
    """
    try:
        from .buttons import build_buttons

        buttons = build_buttons(config, hub=hub, transcode=transcode,
                                ap_settings=ap_settings, portal=portal)
        for button in buttons:
            button.start()
            _LOG.info("User button %s active", button.name)
        return buttons
    except Exception as exc:
        _LOG.warning("User buttons could not be started: %s", exc)
        return []


def run_daemon(config: Config) -> int:
    """Event-driven daemon (Linux/Cubie only).

    ``systemctl stop`` terminates this process with SIGTERM (default action); the
    LEDs are switched off afterwards by the unit's ExecStopPost (`leds-off`),
    which is reliable regardless of how the process exits. An interactive Ctrl-C
    raises KeyboardInterrupt and shuts down cleanly here.
    """
    # Lazy import: devices needs pyudev, which is not available on Windows.
    from .devices import DeviceWatcher
    from .pins import log_gpio_conflicts

    # Before any hardware is opened: name GPIO lines that two configured features
    # would both request -- the second one only ever gets a bare EBUSY.
    log_gpio_conflicts(config)

    state = StationState()
    hub = StatusHub(state, build_indicator(config, state=state))
    hub.set_phase(State.READY)
    hub.signal(Event.SERVICE_STARTED)  # a brief boot wipe so a (re)start is visible
    # One overlay file holds ALL runtime-mutable settings (auto-transcode, default
    # preset, WiFi AP state); features get their own section, so there is a single
    # file and no cross-writer races.
    user_settings = SettingsStore(_user_settings_file(config))
    # Build the optional file-browser / transcode / preview managers once, so the
    # SAME transcode manager backs both the web UI and the device watcher's
    # auto-transcode (which needs it even when the web UI is off).
    browse, transcode, preview = _build_web_features(hub, config, user_settings)
    # The captive-portal DNS drop-in is written BEFORE the AP is raised, so
    # NetworkManager's dnsmasq reads it; the redirect server binds the AP address
    # and is therefore started later, when the AP is actually up.
    ap_settings = user_settings.section("wifi_ap")
    portal = _maybe_prepare_captive_portal(config)
    # One controller for every runtime AP switch (web interface, user button,
    # `wifi-ap` CLI): same feedback, same persisted state, portal kept in step.
    ap_control = _build_ap_controller(config, hub, ap_settings, portal)
    # Start the web server first so it is already listening (on 0.0.0.0, all
    # interfaces) before the slower AP bring-up.
    web_up = _maybe_start_web(hub, config, (browse, transcode, preview), ap_control)
    # WiFi AP: the persisted overlay state (from a web/button toggle) wins over
    # wifi_ap.enabled in config.yaml, so a runtime toggle survives a restart. Apply
    # it (bringing a stale-up AP down when it should be off) and reflect the badge.
    ap_enabled = _effective_ap_enabled(config, ap_settings)
    ap_up = _apply_wifi_ap_state(config, ap_enabled)
    hub.set_ap_active(ap_up)
    if portal is not None:
        portal.sync(ap_up)
    _check_ap_web_reachability(config, web_up, ap_enabled)
    buttons = _maybe_start_buttons(config, hub, transcode, ap_settings, portal)

    watcher = DeviceWatcher(
        config=config, hub=hub, transfer=perform_transfer, transcode=transcode
    )
    try:
        watcher.run()
    except KeyboardInterrupt:  # pragma: no cover
        _LOG.info("Shutting down on request ...")
    finally:
        for button in buttons:
            button.close()
        if portal is not None:
            portal.stop()
        hub.close()  # switches every LED off
    return 0


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="copystation", description=__doc__)
    parser.add_argument("-c", "--config", help="Path to config.yaml")
    parser.add_argument("-v", "--verbose", action="store_true")

    sub = parser.add_subparsers(dest="mode")
    sim = sub.add_parser("simulate", help="Run one transfer with local folders")
    sim.add_argument("source", help="Source folder (contains DCIM)")
    sim.add_argument("target", help="Target folder (SD root)")
    sim.add_argument("--source-name", default="SIM", help="Plain-text source name")
    sub.add_parser(
        "leds-off",
        help="Switch the status LEDs off and exit (used by the systemd ExecStopPost)",
    )
    ap_parser = sub.add_parser(
        "wifi-ap", help="Switch the WLAN access point on/off from the shell"
    )
    ap_parser.add_argument("action", choices=["on", "off", "toggle", "status"])

    args = parser.parse_args(argv)
    _setup_logging(args.verbose)
    config = load_config(args.config)

    if args.mode == "simulate":
        return run_simulation(args, config)
    if args.mode == "leds-off":
        return run_leds_off(config)
    if args.mode == "wifi-ap":
        return run_wifi_ap(config, args.action)
    return run_daemon(config)


def run_wifi_ap(config: Config, action: str) -> int:
    """Switch the WLAN access point from the shell and exit.

    The rescue path for a station with no user button: it needs neither a running
    daemon nor a reachable web interface, only nmcli. The chosen state is written
    to the same user-settings overlay the button and the web switch use, so it
    survives a restart (the overlay wins over ``wifi_ap.enabled``).

    Note: with the daemon running this changes the AP behind its back -- the
    display badge and the captive portal only catch up on the next restart. Use
    the web switch or the button when either is available.
    """
    from .wifi_ap import ApController, is_active

    ap_cfg = config.get("wifi_ap", {}) or {}
    ap_settings = SettingsStore(_user_settings_file(config)).section("wifi_ap")

    if action == "status":
        active = is_active(ap_cfg)
        persisted = ap_settings.get("enabled") if ap_settings.has("enabled") else None
        print(f"WiFi AP: {'up' if active else 'down'} "
              f"(connection {ap_cfg.get('con_name', 'copystation-ap')!r}, "
              f"persisted state: {persisted if persisted is not None else 'from config'})")
        return 0

    control = ApController(config=config, settings=ap_settings)
    actual = control.flip() if action == "toggle" else control.apply(action == "on")
    print(f"WiFi AP is now {'up' if actual else 'down'}")
    # `on` that did not come up is a failure the caller should see (bad password,
    # no NetworkManager, no Wi-Fi device) -- the reason is in the log above.
    return 0 if (actual or action != "on") else 1


def run_leds_off(config: Config) -> int:
    """Drive the configured status LEDs off and exit.

    Run by the systemd ``ExecStopPost`` after the service has stopped, in a fresh
    process: it re-opens the hardware (free again once the daemon exited) and
    sends a single "all off" frame. Decoupling the blackout from the daemon's own
    shutdown makes it reliable regardless of how the daemon process ended (clean
    exit, SIGTERM, or SIGKILL).
    """
    names = config.get("status", {}).get("backends", ["log"])
    _LOG.info("leds-off: switching status backends off: %s", ", ".join(names))
    # start=False: open the hardware without the render loop, so close() just sends
    # one OFF frame -- no brief flash of the idle colour.
    indicator = build_indicator(config, start=False)
    indicator.close()  # close() forces an "all off" frame and releases the hardware
    _LOG.info("leds-off: done")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
