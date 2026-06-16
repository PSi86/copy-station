"""Device detection, mounting and role identification (Linux/Cubie only).

Task: from the mass-storage devices attached to the USB hub, determine the
SOURCE (camera, recognised by its DCIM folder) and the TARGET (SD card), mount
both, trigger the transfer and then unmount cleanly.

Important safety rules:
* The Cubie's own boot/root device (the microSD inside the Cubie) is strictly
  excluded.
* Only block devices attached via USB are considered.
* Re-arm (next run) only happens after the current devices have been physically
  removed -- this prevents an endless copy loop.
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from .config import Config
from .state import StatusHub
from .status import State
from .transfer import TransferError

_LOG = logging.getLogger("copystation.devices")

# Signature of the transfer function injected by the daemon.
TransferFn = Callable[..., Path]


class NoSourceError(TransferError):
    """No eligible partition carries a DCIM folder."""


class NoTargetError(TransferError):
    """No eligible partition is available as the target."""


class InvalidLayoutError(TransferError):
    """The source is not smaller than the target -- refuse to copy."""


@dataclass
class Probe:
    """A mounted candidate partition with everything role selection needs.

    Pure data -- carries no pyudev/mount dependency, so ``select_roles`` (which
    consumes a list of these) is unit-testable on the dev machine.
    """

    sys_name: str
    device_node: str
    mountpoint: Path
    has_dcim: bool
    matched_source: bool
    capacity: int
    free: int
    name: str


def select_roles(
    probes: list[Probe],
    min_bytes: int,
    require_source_smaller: bool = True,
) -> tuple[Probe, Probe]:
    """Pick (source, target) from probed partitions -- order independent.

    Policy:
    * Partitions below ``min_bytes`` are ignored entirely.
    * Source = the smallest partition that has a DCIM folder (and matches the
      optional USB VID/PID allowlist).
    * Target = the largest of the remaining partitions.
    * Unless disabled, the source must be strictly smaller than the target, so
      the larger device is never used as source even if it also carries DCIM.

    Raises ``NoSourceError`` / ``NoTargetError`` / ``InvalidLayoutError``.
    """
    eligible = [p for p in probes if p.capacity >= min_bytes]

    source_candidates = [p for p in eligible if p.has_dcim and p.matched_source]
    if not source_candidates:
        raise NoSourceError("No source (DCIM) found among eligible partitions")
    source = min(source_candidates, key=lambda p: p.capacity)

    target_candidates = [p for p in eligible if p is not source]
    if not target_candidates:
        raise NoTargetError("No target device found")
    target = max(target_candidates, key=lambda p: p.capacity)

    if require_source_smaller and source.capacity >= target.capacity:
        raise InvalidLayoutError(
            f"Source ({source.capacity} B) is not smaller than target "
            f"({target.capacity} B) -- refusing to copy"
        )
    return source, target


def _root_source_device() -> Optional[str]:
    """Determine the kernel name (e.g. 'mmcblk0') of the device that holds '/'.

    This device must never be used as source/target.
    """
    try:
        out = subprocess.run(
            ["findmnt", "-n", "-o", "SOURCE", "/"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return None
    # e.g. /dev/mmcblk0p2 -> base device mmcblk0
    name = Path(out).name
    return _strip_partition(name)


def _strip_partition(devname: str) -> str:
    """Reduce a partition name to its base device.

    sda1 -> sda ; mmcblk0p2 -> mmcblk0 ; nvme0n1p1 -> nvme0n1
    """
    # Devices whose base name ends in a digit use a 'p' before the partition
    # number (mmcblk0p2, nvme0n1p1, loop0p1); plain disks use a bare digit (sda1).
    m = re.match(r"^(.*\d)p(\d+)$", devname)
    if m:
        return m.group(1)
    m = re.match(r"^([a-zA-Z]+)\d+$", devname)
    if m:
        return m.group(1)
    return devname


class DeviceWatcher:
    """Listens for USB block devices and orchestrates transfers."""

    def __init__(
        self,
        config: Config,
        hub: StatusHub,
        transfer: TransferFn,
    ) -> None:
        import pyudev  # lazy: only present on the Cubie

        self._pyudev = pyudev
        self._config = config
        self._hub = hub
        self._transfer = transfer
        self._context = pyudev.Context()
        self._root_dev = _root_source_device()
        if self._root_dev:
            _LOG.info("Root device (locked): %s", self._root_dev)

    # ----- public loop ---------------------------------------------------------

    def run(self) -> None:
        """Main loop: wait for devices, run transfer, re-arm."""
        monitor = self._pyudev.Monitor.from_netlink(self._context)
        monitor.filter_by(subsystem="block")

        while True:
            self._hub.reset_to_ready()
            _LOG.info("Ready -- waiting for devices ...")

            # Wait for the first relevant add event.
            self._wait_for_add(monitor)
            self._hub.set_phase(State.DETECTING)

            # Settle time so all partitions appear.
            time.sleep(float(self._config.get("settle_seconds", 2.0)))

            try:
                self._handle_cycle()
            except TransferError as exc:
                _LOG.error("Transfer failed: %s", exc)
                self._hub.set_error(str(exc))
            except Exception as exc:  # pragma: no cover - defensive
                _LOG.exception("Unexpected error: %s", exc)
                self._hub.set_error(str(exc))

            # Re-arm: wait until the devices are physically removed.
            self._wait_for_removal()

    # ----- internal helpers ----------------------------------------------------

    def _wait_for_add(self, monitor) -> None:
        for device in iter(monitor.poll, None):
            if device.action == "add" and self._is_candidate(device):
                return

    def _is_candidate(self, device) -> bool:
        """True if the device is a mountable USB partition (not root)."""
        if device.get("DEVTYPE") != "partition":
            return False
        if device.get("ID_BUS") != "usb":
            return False
        base = _strip_partition(device.sys_name)
        if self._root_dev and base == self._root_dev:
            return False
        return True

    def _current_partitions(self) -> list:
        """All currently present, eligible USB partitions."""
        result = []
        for device in self._context.list_devices(subsystem="block", DEVTYPE="partition"):
            if self._is_candidate(device):
                result.append(device)
        return result

    def _handle_cycle(self) -> None:
        partitions = self._current_partitions()
        if not partitions:
            raise TransferError("No USB partitions found")

        ident = self._config.get("identify", {})
        min_bytes = int(ident.get("min_partition_gb", 6)) * 1024**3
        require_smaller = bool(ident.get("require_source_smaller_than_target", True))
        mount_base = Path(self._config.get("mount_base", "/run/copystation/mnt"))

        probes: list[Probe] = []
        try:
            # Mount every candidate at its own unique mountpoint and probe it.
            # Order does not matter: roles are chosen afterwards by select_roles.
            for dev in partitions:
                probe = self._probe_device(dev, mount_base)
                if probe is not None:
                    probes.append(probe)

            source, target = select_roles(probes, min_bytes, require_smaller)
            _LOG.info(
                "Source = %s (%s, %d B), Target = %s (%d B)",
                source.device_node, source.name, source.capacity,
                target.device_node, target.capacity,
            )

            self._transfer(
                source_root=source.mountpoint,
                target_root=target.mountpoint,
                source_name=source.name,
                hub=self._hub,
                config=self._config,
            )
        finally:
            subprocess.run(["sync"], check=False)
            for probe in probes:
                self._umount(probe.mountpoint)

    def _probe_device(self, dev, mount_base: Path) -> Optional[Probe]:
        """Mount one candidate and measure it. Returns None if it cannot mount."""
        mountpoint = mount_base / dev.sys_name
        try:
            self._mount(dev.device_node, mountpoint)
        except (OSError, subprocess.CalledProcessError) as exc:
            _LOG.warning("Skipping %s: mount failed (%s)", dev.device_node, exc)
            return None

        try:
            stat = os.statvfs(mountpoint)
            capacity = stat.f_frsize * stat.f_blocks
            free = stat.f_frsize * stat.f_bavail
        except OSError as exc:
            _LOG.warning("Skipping %s: statvfs failed (%s)", dev.device_node, exc)
            self._umount(mountpoint)
            return None

        media = self._config.media_dirname
        return Probe(
            sys_name=dev.sys_name,
            device_node=dev.device_node,
            mountpoint=mountpoint,
            has_dcim=(mountpoint / media).is_dir(),
            matched_source=self._matches_source(dev),
            capacity=capacity,
            free=free,
            name=self._source_name(dev, mountpoint),
        )

    def _matches_source(self, device) -> bool:
        """Optional hardening via USB VID/PID allowlist (otherwise always True)."""
        ident = self._config.get("identify", {})
        vids = [v.lower() for v in ident.get("source_usb_vendor_ids", [])]
        pids = [p.lower() for p in ident.get("source_usb_product_ids", [])]
        if not vids and not pids:
            return True
        vid = (device.get("ID_VENDOR_ID") or "").lower()
        pid = (device.get("ID_MODEL_ID") or "").lower()
        if vids and vid not in vids:
            return False
        if pids and pid not in pids:
            return False
        return True

    @staticmethod
    def _source_name(device, mountpoint: Path) -> str:
        """Plain-text source name (volume label, else USB model/vendor)."""
        for key in ("ID_FS_LABEL", "ID_MODEL", "ID_VENDOR"):
            value = device.get(key)
            if value:
                return value
        return "camera"

    def _mount(self, device_node: str, mountpoint: Path) -> Path:
        mountpoint.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            ["mount", device_node, str(mountpoint)],
            capture_output=True,
            text=True,
            check=True,
        )
        return mountpoint

    @staticmethod
    def _umount(mountpoint: Path) -> None:
        subprocess.run(["umount", str(mountpoint)], capture_output=True, check=False)

    def _wait_for_removal(self) -> None:
        """Block until no eligible USB partitions remain."""
        _LOG.info("Waiting for devices to be removed ...")
        while self._current_partitions():
            time.sleep(1.0)
