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
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable, Optional

from .config import Config
from .state import StatusHub
from .status import Event, State
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
    has_media: bool = True  # DCIM folder contains at least one file


def select_roles(
    probes: list[Probe],
    min_bytes: int,
    require_source_smaller: bool = True,
) -> tuple[Probe, Probe]:
    """Pick (source, target) from probed partitions -- order independent.

    Policy:
    * Partitions below ``min_bytes`` are ignored entirely.
    * Source = the smallest partition that has a NON-EMPTY DCIM folder (and
      matches the optional USB VID/PID allowlist). A device whose DCIM folder is
      empty is never a source -- there is nothing to copy.
    * Target = the largest of the remaining partitions.
    * Unless disabled, the source must be strictly smaller than the target, so
      the larger device is never used as source even if it also carries DCIM.

    Raises ``NoSourceError`` / ``NoTargetError`` / ``InvalidLayoutError``.
    """
    eligible = [p for p in probes if p.capacity >= min_bytes]

    source_candidates = [
        p for p in eligible if p.has_dcim and p.matched_source and p.has_media
    ]
    if not source_candidates:
        raise NoSourceError("No source (non-empty DCIM) found among eligible partitions")
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


def has_empty_source(eligible: list[Probe]) -> bool:
    """True when a source is connected but there is nothing to copy.

    That is: at least one eligible volume is source-shaped (carries a DCIM folder
    and matches the optional VID/PID allowlist), and *every* such volume has an
    empty DCIM. Used both to decide the "empty source" status signal and to skip
    starting a transfer.
    """
    source_shaped = [p for p in eligible if p.has_dcim and p.matched_source]
    return bool(source_shaped) and not any(p.has_media for p in source_shaped)


def device_views(
    probes: list[Probe],
    min_bytes: int,
    source: Optional[Probe] = None,
    target: Optional[Probe] = None,
) -> list[dict]:
    """Per-device summary for the web UI.

    When ``source``/``target`` are given (a decision was made by
    ``select_roles``), each volume is labelled with its *actual* role. Without a
    decision -- e.g. while only one volume is present -- eligible volumes are
    shown as ``"candidate"`` rather than guessed, because the smaller/larger
    split is only known once two volumes are present.

    ``role``: ``"target"`` | ``"empty"`` (source-shaped but its DCIM folder is
    empty -> nothing to copy) | ``"source"`` | ``"candidate"`` (eligible,
    undecided) | ``"unused"`` (eligible but not chosen, e.g. a third volume) |
    ``"ignored"`` (below ``min_bytes``).

    Role precedence matters: ``target`` is checked BEFORE ``empty`` (a device used
    as the target must never be flagged ``empty``, even if its own DCIM is empty),
    and ``empty`` is checked BEFORE ``source`` (so once a source's DCIM has been
    cleared after a successful copy it flips from ``source`` to ``empty``).
    """
    src_node = source.device_node if source else None
    tgt_node = target.device_node if target else None
    views: list[dict] = []
    for p in probes:
        eligible = p.capacity >= min_bytes
        if not eligible:
            role = "ignored"
        elif p.device_node == tgt_node:
            role = "target"  # the chosen target -- emptiness is irrelevant here
        elif p.has_dcim and p.matched_source and not p.has_media:
            role = "empty"  # a source with an empty DCIM -- nothing to copy
        elif p.device_node == src_node:
            role = "source"
        elif src_node or tgt_node:
            role = "unused"  # eligible, but a different volume was chosen
        else:
            role = "candidate"  # no decision yet (waiting for a second volume)
        views.append(
            {
                "name": p.name,
                "node": p.device_node,
                "capacity": p.capacity,
                "free": p.free,
                "has_dcim": p.has_dcim,
                "eligible": eligible,
                "role": role,
            }
        )
    return views


def _dcim_has_media(media_dir: Path) -> bool:
    """True if the DCIM folder contains at least one regular file (any depth).

    Early-exits on the first file, so it is cheap even for a full card.
    """
    try:
        for entry in media_dir.rglob("*"):
            if entry.is_file() and not entry.is_symlink():
                return True
    except OSError:  # pragma: no cover - defensive (e.g. media vanished)
        return False
    return False


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
        # ``_armed`` guards against re-running a transfer for a device set that
        # was already handled. It is cleared when a transfer starts and re-armed
        # once fewer than two eligible volumes remain (i.e. one was removed).
        self._armed = True
        # For the action log: which eligible volumes were present last time, and
        # their names (kept so a removal can still be named after it is gone).
        self._prev_nodes: set[str] = set()
        self._node_names: dict[str, str] = {}
        if self._root_dev:
            _LOG.info("Root device (locked): %s", self._root_dev)

    # ----- public loop ---------------------------------------------------------

    def run(self) -> None:
        """Event-driven main loop.

        Reacts to USB add/remove events. It does NOT assume that source and
        target are inserted at the same moment: it keeps re-evaluating the set of
        present volumes and only starts a transfer once at least two eligible
        volumes are available. While fewer are present it stays in DETECTING and
        publishes the detected devices, so the web UI shows what was recognised.
        """
        monitor = self._pyudev.Monitor.from_netlink(self._context)
        monitor.filter_by(subsystem="block")

        self._armed = True
        self._publish_ready()
        _LOG.info("Ready -- waiting for devices ...")
        # Devices may already be plugged in when the daemon starts.
        self._evaluate()

        while True:
            self._wait_for_change(monitor)
            self._settle(monitor)
            self._evaluate()

    # ----- internal helpers ----------------------------------------------------

    def _wait_for_change(self, monitor) -> None:
        """Block until any block add/remove event arrives."""
        for device in iter(monitor.poll, None):
            if device.action in ("add", "remove"):
                return

    def _settle(self, monitor) -> None:
        """Adaptive debounce: return once the bus is quiet, capped at settle_seconds.

        A freshly inserted device enumerates its volumes in a short burst. Rather
        than always waiting the full ``settle_seconds``, we proceed as soon as no
        further event has arrived for ``settle_quiet_seconds``. This is faster in
        the common case without being less reliable: any volume that appears after
        we proceed simply triggers another evaluation. ``settle_seconds`` stays the
        hard upper bound for slow enumerations.
        """
        cap = float(self._config.get("settle_seconds", 2.0))
        quiet = float(self._config.get("settle_quiet_seconds", 1.0))
        start = time.monotonic()
        while True:
            remaining = cap - (time.monotonic() - start)
            if remaining <= 0:
                return
            # poll() returns the next event, or None when the timeout elapses
            # with the bus quiet -> settled.
            if monitor.poll(timeout=min(quiet, remaining)) is None:
                return

    def _publish_ready(self) -> None:
        """Back to the idle state: clear phase, devices and storage figures."""
        self._hub.reset_to_ready()
        self._hub.set_devices([])

    def _log_device_changes(self, eligible: list[Probe]) -> tuple[bool, bool]:
        """Emit 'detected'/'removed' events for the eligible-volume set.

        Returns ``(any_added, any_removed)`` so the caller can decide whether to
        log follow-up messages (e.g. 'waiting for another device').
        """
        names = {p.device_node: p.name for p in eligible}
        current = set(names)
        added = current - self._prev_nodes
        removed = self._prev_nodes - current
        self._node_names.update(names)
        for node in sorted(added):
            self._hub.log_event(f"Storage device detected: {names[node]}")
            # A quick green blink per recognised volume -- unmistakable on the LEDs.
            self._hub.signal(Event.DEVICE_DETECTED)
        for node in sorted(removed):
            self._hub.log_event(f"Device removed: {self._node_names.pop(node, node)}")
        self._prev_nodes = current
        return bool(added), bool(removed)

    def _evaluate(self) -> None:
        """Probe all present volumes; wait, or transfer once two are eligible."""
        ident = self._config.get("identify", {})
        min_bytes = int(ident.get("min_partition_gb", 6)) * 1024**3
        require_smaller = bool(ident.get("require_source_smaller_than_target", True))
        mount_base = Path(self._config.get("mount_base", "/run/copystation/mnt"))

        probes: list[Probe] = []
        try:
            for dev in self._current_partitions():
                probe = self._probe_device(dev, mount_base)
                if probe is not None:
                    probes.append(probe)

            eligible = [p for p in probes if p.capacity >= min_bytes]
            added, removed = self._log_device_changes(eligible)

            # A source is connected but its DCIM is empty -> a steady blue "nothing
            # to copy" hold. Fire only on a change, so it does not repeat endlessly.
            if added and has_empty_source(eligible):
                self._hub.signal(Event.SOURCE_EMPTY)

            if not eligible:
                self._armed = True
                self._hub.set_devices(device_views(probes, min_bytes))
                self._publish_ready()
                if removed:
                    self._hub.log_event("Ready -- waiting for devices")
                _LOG.info("Ready -- waiting for devices ...")
                return

            if len(eligible) < 2:
                # Not enough to decide yet -- keep waiting and re-arm.
                self._armed = True
                self._hub.set_devices(device_views(probes, min_bytes))
                self._hub.set_phase(State.DETECTING)
                if added:
                    self._hub.log_event("Waiting for another device ...")
                _LOG.info("Detecting -- %d eligible volume(s), need 2 ...", len(eligible))
                return

            # A source is connected but its DCIM is empty -- nothing to copy. Don't
            # start a transfer and don't error; show it as "empty" and wait.
            if has_empty_source(eligible):
                self._armed = True
                self._hub.set_devices(device_views(probes, min_bytes))
                self._hub.set_phase(State.DETECTING)
                if added:
                    self._hub.log_event(
                        "Source connected but its DCIM folder is empty -- nothing to copy"
                    )
                _LOG.info("Source DCIM is empty -- nothing to copy.")
                return

            # Two or more eligible volumes -- decide roles for real.
            try:
                source, target = select_roles(probes, min_bytes, require_smaller)
            except TransferError:
                self._hub.set_devices(device_views(probes, min_bytes))
                raise
            self._hub.set_devices(device_views(probes, min_bytes, source, target))

            if not self._armed:
                # This set was already handled; wait until a device is removed.
                return

            self._armed = False
            _LOG.info(
                "Source = %s (%s, %d B), Target = %s (%d B)",
                source.device_node, source.name, source.capacity,
                target.device_node, target.capacity,
            )
            self._hub.log_event(
                f"Roles assigned: source = {source.name}, target = {target.name}"
            )

            def _refresh_devices() -> None:
                # Re-measure the mounts and republish, so the web view tracks the
                # filling target during the copy and the emptied source afterwards.
                refreshed = [self._restat(p) for p in probes]
                self._hub.set_devices(device_views(refreshed, min_bytes, source, target))

            self._transfer(
                source_root=source.mountpoint,
                target_root=target.mountpoint,
                source_name=source.name,
                source_device=source.device_node,
                target_device=target.device_node,
                hub=self._hub,
                config=self._config,
                on_devices_refresh=_refresh_devices,
            )
            self._hub.log_event("Ready to remove devices")
        except TransferError as exc:
            self._armed = False
            _LOG.error("Transfer failed: %s", exc)
            self._hub.log_event(f"Copy failed: {exc}", level="error")
            self._hub.set_error(str(exc))
        except Exception as exc:  # pragma: no cover - defensive
            self._armed = False
            _LOG.exception("Unexpected error: %s", exc)
            self._hub.log_event(f"Unexpected error: {exc}", level="error")
            self._hub.set_error(str(exc))
        finally:
            subprocess.run(["sync"], check=False)
            for probe in probes:
                self._umount(probe.mountpoint)

    def _is_candidate(self, device) -> bool:
        """True if the device is a mountable USB volume (not root).

        Accepts two shapes:
        * a USB *partition* (``sda1``, ``sdc1`` ...), and
        * a USB *whole disk* that carries a filesystem directly, with no
          partition table (``sdc`` with no ``sdc1``). The DJI O4 Air Unit
          exposes its storage this way ("superfloppy"), so without this it would
          never be detected. Disks that DO have a partition table are skipped
          here -- their partitions are handled individually.
        """
        if device.get("ID_BUS") != "usb":
            return False
        devtype = device.get("DEVTYPE")
        if devtype == "partition":
            pass
        elif devtype == "disk":
            if device.get("ID_PART_TABLE_TYPE"):
                return False  # partitioned -> its partitions are the candidates
            if not device.get("ID_FS_TYPE"):
                return False  # no directly-mountable filesystem
        else:
            return False
        base = _strip_partition(device.sys_name)
        if self._root_dev and base == self._root_dev:
            return False
        return True

    def _current_partitions(self) -> list:
        """All currently present, eligible USB volumes (partitions or disks)."""
        result = []
        for device in self._context.list_devices(subsystem="block"):
            if self._is_candidate(device):
                result.append(device)
        return result

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

        media_dir = mountpoint / self._config.media_dirname
        has_dcim = media_dir.is_dir()
        return Probe(
            sys_name=dev.sys_name,
            device_node=dev.device_node,
            mountpoint=mountpoint,
            has_dcim=has_dcim,
            matched_source=self._matches_source(dev),
            capacity=capacity,
            free=free,
            name=self._volume_name(dev),
            has_media=has_dcim and _dcim_has_media(media_dir),
        )

    def _restat(self, probe: Probe) -> Probe:
        """Re-measure a mounted probe (free space + DCIM contents) in place.

        Used to refresh the web view live during/after a copy: the target fills
        up, and once the source DCIM has been cleared it becomes empty. Returns
        the probe unchanged if the mount is gone.
        """
        try:
            stat = os.statvfs(probe.mountpoint)
        except OSError:
            return probe
        media_dir = probe.mountpoint / self._config.media_dirname
        has_dcim = media_dir.is_dir()
        return replace(
            probe,
            capacity=stat.f_frsize * stat.f_blocks,
            free=stat.f_frsize * stat.f_bavail,
            has_dcim=has_dcim,
            has_media=has_dcim and _dcim_has_media(media_dir),
        )

    @staticmethod
    def _usb_ids(device) -> tuple[str, str]:
        """(vid, pid) of a device, lowercased.

        A whole-disk node (e.g. the O4's ``sdc``) sometimes lacks
        ``ID_VENDOR_ID``/``ID_MODEL_ID``; in that case fall back to its USB
        ancestor, which always carries them. Returns ``("", "")`` if unknown.
        """
        vid = (device.get("ID_VENDOR_ID") or "").lower()
        pid = (device.get("ID_MODEL_ID") or "").lower()
        if vid and pid:
            return vid, pid
        find_parent = getattr(device, "find_parent", None)
        if callable(find_parent):
            try:
                parent = find_parent("usb", "usb_device")
            except Exception:  # pragma: no cover - defensive
                parent = None
            if parent is not None:
                vid = vid or (parent.get("ID_VENDOR_ID") or "").lower()
                pid = pid or (parent.get("ID_MODEL_ID") or "").lower()
        return vid, pid

    def _matches_source(self, device) -> bool:
        """Optional hardening via USB VID/PID allowlist (otherwise always True)."""
        ident = self._config.get("identify", {})
        vids = [v.lower() for v in ident.get("source_usb_vendor_ids", [])]
        pids = [p.lower() for p in ident.get("source_usb_product_ids", [])]
        if not vids and not pids:
            return True
        vid, pid = self._usb_ids(device)
        if vids and vid not in vids:
            return False
        if pids and pid not in pids:
            return False
        return True

    def _volume_name(self, device) -> str:
        """Human-friendly volume name.

        Resolution order (best first):
        1. a user-configured name matched by USB VID/PID (``identify
           .device_labels``) -- this is how an O4 becomes "O4 Lite"/"O4 Pro"
           without hardcoding, since the USB product string is only a serial,
        2. the filesystem label (e.g. a card the user named),
        3. the USB model / vendor string,
        4. a generic fallback.
        """
        configured = self._configured_label(device)
        if configured:
            return configured
        for key in ("ID_FS_LABEL", "ID_MODEL", "ID_VENDOR"):
            value = device.get(key)
            if value:
                return value
        return "camera"

    def _configured_label(self, device) -> Optional[str]:
        """Look up a friendly name from ``identify.device_labels`` by VID/PID."""
        mapping = self._config.get("identify", {}).get("device_labels", [])
        if not mapping:
            return None
        vid, pid = self._usb_ids(device)
        for entry in mapping:
            evid = str(entry.get("vid", "")).lower()
            epid = str(entry.get("pid", "")).lower()
            if not (evid or epid):
                continue
            if evid and evid != vid:
                continue
            if epid and epid != pid:
                continue
            return entry.get("name")
        return None

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
