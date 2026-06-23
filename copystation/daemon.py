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
from .naming import next_transfer_dir
from .state import StationState, StatusHub, StorageInfo
from .status import State, build_indicator
from .transfer import (
    TransferError,
    check_free_space,
    cleanup_source,
    copy_tree,
    total_size,
    verify,
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


def perform_transfer(
    source_root: Path,
    target_root: Path,
    source_name: str | None,
    hub: StatusHub,
    config: Config,
    source_device: str | None = None,
) -> Path:
    """Perform a complete transfer.

    The order is safety critical: copy -> verify -> ONLY THEN clear the source.
    Any error before successful verification leaves the source untouched.
    Returns the created target folder.

    ``source_device`` (e.g. ``/dev/sdc``) lets the copy abort promptly if the
    source is unplugged mid-transfer, rather than waiting for the I/O timeout.
    """
    source_root = Path(source_root)
    target_root = Path(target_root)
    media_dir = source_root / config.media_dirname

    if not media_dir.is_dir():
        raise TransferError(
            f"No '{config.media_dirname}' folder on the source: {media_dir}"
        )

    src_label = source_name or "source"
    hub.set_storage(storage_info(source_root, src_label), storage_info(target_root, "target"))

    required = total_size(media_dir)
    check_free_space(target_root, required)

    dest = next_transfer_dir(target_root, source_name)
    _LOG.info("Copying %s -> %s (%d bytes)", media_dir, dest, required)
    hub.log_event(f"Copy started: {dest.name}")
    hub.begin_transfer(dest.name, required)
    # Abort the copy quickly if the source device node disappears (unplugged).
    abort_check = None
    if source_device:
        abort_check = lambda: not os.path.exists(source_device)  # noqa: E731
    copy_tree(media_dir, dest, on_progress=hub.update_progress, abort_check=abort_check)
    hub.finish_transfer()

    _LOG.info("Verifying transfer ...")
    hub.log_event("Verifying transferred data ...")
    verify(media_dir, dest)

    keep = config.get("cleanup", {}).get("keep_dcim_folder", True)
    _LOG.info("Verification ok -- clearing source (keep_folder=%s)", keep)
    hub.log_event("Clearing source data ...")
    cleanup_source(media_dir, keep_folder=keep)

    hub.log_event(f"Copy complete: {dest.name}")
    hub.set_phase(State.SUCCESS)
    # Refresh storage figures after the copy (free space changed).
    hub.set_storage(storage_info(source_root, src_label), storage_info(target_root, "target"))
    _LOG.info("Transfer complete: %s", dest)
    return dest


def _maybe_start_web(state: StationState, config: Config) -> bool:
    """Start the web interface if enabled. Returns True if it was started."""
    web_cfg = config.get("web", {})
    if not web_cfg.get("enabled"):
        return False
    try:
        from .web import start_web_server

        start_web_server(state, web_cfg.get("host", "0.0.0.0"), int(web_cfg.get("port", 8080)))
        return True
    except Exception as exc:
        _LOG.warning("Web interface could not be started: %s", exc)
        return False


def run_simulation(args: argparse.Namespace, config: Config) -> int:
    """Run a transfer with local folders (development/test)."""
    state = StationState()
    hub = StatusHub(state, build_indicator(config))
    web_running = _maybe_start_web(state, config)

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


def _maybe_start_shutdown_button(config: Config):
    """Start the optional GPIO shutdown button. Returns it, or None."""
    try:
        from .power import build_shutdown_button

        button = build_shutdown_button(config)
        if button is not None:
            button.start()
            _LOG.info("Shutdown button active")
        return button
    except Exception as exc:
        _LOG.warning("Shutdown button could not be started: %s", exc)
        return None


def run_daemon(config: Config) -> int:
    """Event-driven daemon (Linux/Cubie only)."""
    # Lazy import: devices needs pyudev, which is not available on Windows.
    from .devices import DeviceWatcher

    state = StationState()
    hub = StatusHub(state, build_indicator(config))
    hub.set_phase(State.READY)
    _maybe_start_web(state, config)
    button = _maybe_start_shutdown_button(config)

    watcher = DeviceWatcher(config=config, hub=hub, transfer=perform_transfer)
    try:
        watcher.run()
    except KeyboardInterrupt:  # pragma: no cover
        _LOG.info("Shutting down on request ...")
    finally:
        if button is not None:
            button.close()
        hub.close()
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

    args = parser.parse_args(argv)
    _setup_logging(args.verbose)
    config = load_config(args.config)

    if args.mode == "simulate":
        return run_simulation(args, config)
    return run_daemon(config)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
