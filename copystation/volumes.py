"""Reusable USB mass-storage volume enumeration and naming.

Single source of truth for "which block devices are browsable USB volumes,
excluding the OS/root device". Shared by the device watcher (source/target
detection, :mod:`copystation.devices`) and the web file browser
(:mod:`copystation.web`).

pyudev and ``findmnt`` are Linux-only, so those calls are lazy/guarded -- the
pure helper (:func:`strip_partition`) imports fine on the Windows dev machine.
The rich functions all take a plain pyudev ``device`` plus (where needed) the
loaded :class:`~copystation.config.Config`, so they carry no watcher state.
"""

from __future__ import annotations

import logging
import re
import subprocess
from pathlib import Path
from typing import Optional

_LOG = logging.getLogger("copystation.volumes")


def strip_partition(devname: str) -> str:
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


def root_source_device() -> Optional[str]:
    """Determine the kernel name (e.g. 'mmcblk0') of the device that holds '/'.

    This device must never be used as source/target or exposed for browsing.
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
    return strip_partition(name)


def kernel_partitions(device) -> list[str]:
    """Names of the partitions the kernel created on a whole-disk ``device``.

    The kernel marks each partition it created with a ``partition`` attribute in
    the disk's sysfs directory (``.../block/sda/sda1/partition``); no other
    subdirectory of a disk carries one. Empty when there are none, or when the
    device has no sysfs path to look at.
    """
    sys_path = getattr(device, "sys_path", None)
    if not sys_path:
        return []
    try:
        return sorted(entry.parent.name for entry in Path(sys_path).glob("*/partition"))
    except OSError:  # pragma: no cover - defensive (device vanished mid-scan)
        return []


def is_usb_volume(device, root_dev: Optional[str]) -> bool:
    """True if ``device`` is a mountable USB volume (and not the root device).

    Accepts two shapes:
    * a USB *partition* (``sda1``, ``sdc1`` ...), and
    * a USB *whole disk* that carries a filesystem directly and has no
      partitions (``sdc`` with no ``sdc1``). DJI O4 and Walksnail air units
      expose their storage this way ("superfloppy"), so without this they would
      never be detected. Disks the kernel split into partitions are skipped
      here -- their partitions are handled individually.

    Whether a disk is partitioned is decided by the partitions the kernel
    actually created, not by udev's ``ID_PART_TABLE_TYPE``: libblkid before 2.39
    (Debian 12 / Raspberry Pi OS Bookworm) takes the 55AA signature of an exFAT
    boot sector for an empty DOS partition table and reports ``dos`` for exactly
    these superfloppies. The kernel creates no partition for them, so the whole
    disk is the only node there is to mount.
    """
    if device.get("ID_BUS") != "usb":
        return False
    devtype = device.get("DEVTYPE")
    if devtype == "partition":
        pass
    elif devtype == "disk":
        if not device.get("ID_FS_TYPE"):
            return False  # no directly-mountable filesystem
        if kernel_partitions(device):
            return False  # partitioned -> its partitions are the candidates
        if device.get("ID_PART_TABLE_TYPE"):
            _LOG.debug(
                "%s: udev reports a %r partition table but the kernel created no "
                "partition -- using the whole disk (%s)",
                device.sys_name, device.get("ID_PART_TABLE_TYPE"),
                device.get("ID_FS_TYPE"),
            )
    else:
        return False
    base = strip_partition(device.sys_name)
    if root_dev and base == root_dev:
        return False
    return True


def usb_ids(device) -> tuple[str, str]:
    """(vid, pid) of a device, lowercased.

    A whole-disk node (e.g. the O4's ``sdc``) sometimes lacks
    ``ID_VENDOR_ID``/``ID_MODEL_ID``; in that case fall back to its USB
    ancestor, which always carries them. Returns ``("", "")`` if unknown.
    """
    vid = (device.get("ID_VENDOR_ID") or "").lower()
    pid = (device.get("ID_MODEL_ID") or "").lower()
    if vid and pid:
        return vid, pid
    parent = _usb_parent(device)
    if parent is not None:
        vid = vid or (parent.get("ID_VENDOR_ID") or "").lower()
        pid = pid or (parent.get("ID_MODEL_ID") or "").lower()
    return vid, pid


def _usb_parent(device):
    """The USB device node a block device hangs off, or None."""
    find_parent = getattr(device, "find_parent", None)
    if not callable(find_parent):
        return None
    try:
        return find_parent("usb", "usb_device")
    except Exception:  # pragma: no cover - defensive
        return None


def _read_sysfs(sys_path: str, attribute: str) -> str:
    try:
        return (Path(sys_path) / attribute).read_text(errors="replace").strip()
    except OSError:
        return ""


def usb_strings(device) -> tuple[str, str]:
    """(manufacturer, product) descriptor strings of the USB device behind ``device``.

    What the kernel logs as ``Manufacturer:``/``Product:`` on connect, read from
    the USB device's sysfs. The block node's own ``ID_VENDOR``/``ID_MODEL`` are
    no substitute: for USB mass storage they come from the SCSI inquiry, which
    reads ``Linux`` / ``File-Stor Gadget`` for any device built on the Linux
    gadget framework. ``("", "")`` if unknown.
    """
    parent = _usb_parent(device)
    sys_path = getattr(parent, "sys_path", None) if parent is not None else None
    if not sys_path:
        return "", ""
    return _read_sysfs(sys_path, "manufacturer"), _read_sysfs(sys_path, "product")


def root_media_profile(device, config) -> Optional[dict]:
    """The ``identify.root_media_sources`` entry ``device`` matches, or None.

    Such a device records to the root of its storage instead of into a DCIM
    folder (the Walksnail air unit), so a match decides that its recordings are
    copied from -- and afterwards deleted from -- the root. Every criterion an
    entry names (``vid``, ``pid``, ``manufacturer``, ``product``) must match;
    strings compare case-insensitively. An entry that names none matches
    nothing, so a half-filled entry can never turn every device into one.
    """
    entries = (config.get("identify", {}) or {}).get("root_media_sources") or []
    if not entries:
        return None
    vid, pid = usb_ids(device)
    manufacturer, product = usb_strings(device)
    actual = {"vid": vid, "pid": pid,
              "manufacturer": manufacturer.lower(), "product": product.lower()}
    for entry in entries:
        wanted = {key: str(entry[key]).strip().lower() for key in actual if entry.get(key)}
        if wanted and all(actual[key] == value for key, value in wanted.items()):
            return entry
    return None


def configured_label(device, config) -> Optional[str]:
    """Look up a friendly name from ``identify.device_labels`` by USB VID/PID."""
    mapping = config.get("identify", {}).get("device_labels", [])
    if not mapping:
        return None
    vid, pid = usb_ids(device)
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


def volume_name(device, config) -> str:
    """Human-friendly volume name.

    Resolution order (best first):
    1. a user-configured name matched by USB VID/PID (``identify.device_labels``)
       -- this is how an O4 becomes "O4 Lite"/"O4 Pro" without hardcoding, since
       the USB product string is only a serial,
    2. the name of a matched ``identify.root_media_sources`` entry (a Walksnail
       only reports the generic ``Linux`` / ``File-Stor Gadget``),
    3. the filesystem label (e.g. a card the user named),
    4. the USB model / vendor string,
    5. a generic fallback.
    """
    configured = configured_label(device, config)
    if configured:
        return configured
    profile = root_media_profile(device, config)
    if profile and profile.get("name"):
        return str(profile["name"])
    for key in ("ID_FS_LABEL", "ID_MODEL", "ID_VENDOR"):
        value = device.get(key)
        if value:
            return value
    return "camera"


def list_usb_volumes(config) -> list[dict]:
    """All present, live USB volumes with the OS/root device excluded.

    For the web file browser: returns a list of ``{sys_name, device_node, name}``
    for every attached USB mass-storage volume that is *not* the board's own
    OS card. Linux-only (needs pyudev); the caller catches import errors.

    A node whose backing disk the kernel has already zeroed (a card pulled from a
    reader whose node lingers) is dropped via :func:`~copystation.transfer.volume_alive`,
    so a stale device never shows up as browsable.
    """
    import pyudev  # lazy: only present on the device

    from .transfer import volume_alive

    context = pyudev.Context()
    root_dev = root_source_device()
    volumes: list[dict] = []
    for device in context.list_devices(subsystem="block"):
        if not is_usb_volume(device, root_dev):
            continue
        node = device.device_node
        if not volume_alive(node):
            continue
        volumes.append(
            {
                "sys_name": device.sys_name,
                "device_node": node,
                "name": volume_name(device, config),
            }
        )
    return volumes
