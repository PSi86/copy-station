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
import subprocess
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable, Optional

from . import volumes
from .config import Config
from .state import StatusHub, StorageInfo
from .status import Event, State
from .status.effects import FILL_GAUGE_SECONDS
from .transcode import is_video_file
from .transfer import TransferError, total_size, volume_alive

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
    has_media: bool = True   # media folder holds at least one real file
    is_empty: bool = False   # effectively blank: no real file anywhere on the medium
    no_media: bool = False   # nothing to copy, but the medium carries other data
    has_label: bool = False  # a user-configured label (identify.device_labels) matched


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


def has_source(eligible: list[Probe]) -> bool:
    """True when at least one eligible volume can serve as the source.

    A source is a volume that carries a non-empty media folder and passes the
    optional VID/PID allowlist. When this is False there is simply nothing to
    copy yet -- no card looks like a source, or every source-shaped card is
    empty. That is a *wait* condition, not an error: the source may still be
    plugged in. Two blank cards (no DCIM at all) land here too, so they wait
    quietly instead of raising ``NoSourceError``.
    """
    return any(p.has_dcim and p.matched_source and p.has_media for p in eligible)


def has_empty_source(eligible: list[Probe]) -> bool:
    """True when a source is connected but there is nothing to copy.

    That is: at least one eligible volume is source-shaped (carries a DCIM folder
    and matches the optional VID/PID allowlist), and *every* such volume has an
    empty DCIM. Used both to decide the "empty source" status signal and to skip
    starting a transfer.
    """
    source_shaped = [p for p in eligible if p.has_dcim and p.matched_source]
    return bool(source_shaped) and not any(p.has_media for p in source_shaped)


def _used_fraction(p: Probe) -> float:
    """Fraction of a volume that is in use (0.0..1.0), clamped."""
    if p.capacity <= 0:
        return 0.0
    used = max(0, p.capacity - p.free)
    return min(1.0, used / p.capacity)


def fill_fraction_for_display(eligible: list[Probe]) -> Optional[float]:
    """The fill level to show on the LED gauge while detecting.

    Prefers a source-shaped volume (the camera, whose contents will be copied --
    the most informative "how full" number); otherwise the fullest eligible
    volume. ``None`` when there is nothing to show.
    """
    if not eligible:
        return None
    source_shaped = [p for p in eligible if p.has_dcim and p.matched_source]
    pool = source_shaped or eligible
    return max(_used_fraction(p) for p in pool)


def _display_rank(p: Probe, min_bytes: int) -> int:
    """Display priority of a probe -- lower sorts first (shown higher up).

    The panel shows only the top few device rows (the rest collapse into
    "+N more"), so the most promising volumes must come first:

    * 0 -- a user-configured label matched (``identify.device_labels``): an
      explicitly recognised device, the strongest signal of interest.
    * 1 -- looks like a real source: a non-empty media folder that passes the
      optional VID/PID allowlist.
    * 2 -- any other eligible volume.
    * 3 -- ignored (below ``min_bytes``): least interesting, shown last.
    """
    if p.capacity < min_bytes:
        return 3
    if p.has_label:
        return 0
    if p.has_dcim and p.matched_source and p.has_media:
        return 1
    return 2


def order_for_display(probes: list[Probe], min_bytes: int) -> list[Probe]:
    """Sort probes for the panel/web display: best candidates first.

    Primary key is :func:`_display_rank` (configured label > source-shaped >
    other > ignored); within a rank, larger volumes come first. Pure and
    order-independent so the same set always renders the same way.
    """
    return sorted(probes, key=lambda p: (_display_rank(p, min_bytes), -p.capacity))


def device_views(
    probes: list[Probe],
    min_bytes: int,
    source: Optional[Probe] = None,
    target: Optional[Probe] = None,
) -> list[dict]:
    """Per-device summary for the web UI and e-paper panel.

    The result is ordered by :func:`order_for_display` (best candidates first),
    so a panel that only shows a few rows surfaces the most promising volumes.

    When ``source``/``target`` are given (a decision was made by
    ``select_roles``), each volume is labelled with its *actual* role. Without a
    decision -- e.g. while only one volume is present -- eligible volumes are
    shown as ``"candidate"`` rather than guessed, because the smaller/larger
    split is only known once two volumes are present.

    ``role``: ``"target"`` | ``"no_media"`` (nothing to copy, but the medium
    carries other data -> never used as source) | ``"empty"`` (effectively blank
    medium -> never used as source) | ``"source"`` | ``"candidate"`` (eligible,
    has media, undecided) | ``"unused"`` (eligible but not chosen, e.g. a third
    volume) | ``"ignored"`` (below ``min_bytes``).

    Role precedence matters: ``target`` is checked BEFORE ``no_media``/``empty``
    (a device used as the target must never be flagged either way, even if it
    carries no media), ``no_media`` takes priority over ``empty`` (they are
    disjoint by construction, see :func:`_content_flags`, so ``empty`` really
    means blank), and both are checked BEFORE ``source`` (so once a source has
    been cleared after a successful copy it flips from ``source`` to ``empty``
    -- or to ``no_media`` if the card held unrelated files all along).
    """
    src_node = source.device_node if source else None
    tgt_node = target.device_node if target else None
    views: list[dict] = []
    for p in order_for_display(probes, min_bytes):
        eligible = p.capacity >= min_bytes
        if not eligible:
            role = "ignored"
        elif p.device_node == tgt_node:
            role = "target"  # the chosen target -- emptiness is irrelevant here
        elif p.no_media:
            role = "no_media"  # carries data, but nothing to copy
        elif p.is_empty:
            role = "empty"  # effectively blank medium
        elif p.device_node == src_node:
            role = "source"
        elif src_node or tgt_node:
            role = "unused"  # eligible, but a different volume was chosen
        else:
            role = "candidate"  # eligible, has media, no decision yet
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


# Filesystem bookkeeping that is NOT real content -- a card carrying only these
# still counts as empty. Hidden entries (names starting with ".") are junk too.
_JUNK_NAMES = frozenset({
    "system volume information", "$recycle.bin", "found.000", "lost.dir",
    "thumbs.db", ".ds_store", ".trashes", ".spotlight-v100", ".fseventsd",
    ".android_secure", "._.trashes", "desktop.ini",
})


def _is_junk(name: str) -> bool:
    """True for hidden entries and known filesystem bookkeeping (not real data)."""
    n = name.lower()
    return n.startswith(".") or n in _JUNK_NAMES


def _has_real_file(root: Path) -> bool:
    """True if ``root`` contains at least one real (non-junk) file, any depth.

    Hidden files and filesystem bookkeeping (.DS_Store, Thumbs.db, System Volume
    Information, ...) are ignored, so a card the OS sprinkled junk onto still
    reads as blank. Directories alone (however many) do not count as content.
    Early-exits on the first hit, so a well-filled medium answers immediately.
    """
    try:
        for entry in root.rglob("*"):
            if not entry.is_file() or entry.is_symlink():
                continue
            rel = entry.relative_to(root)
            if not any(_is_junk(part) for part in rel.parts):
                return True
    except OSError:  # pragma: no cover - defensive (e.g. media vanished)
        return False
    return False


def _content_flags(mountpoint: Path, media_dir: Path, has_dcim: bool) -> tuple[bool, bool, bool]:
    """``(has_media, is_empty, no_media)`` for a mounted volume.

    * ``has_media`` -- the media folder holds at least one real file: a source.
    * ``is_empty`` -- nothing to copy AND the whole medium is effectively blank
      (no real file anywhere; junk and bare folders ignored).
    * ``no_media`` -- nothing to copy, but the medium DOES carry real data
      (a data stick without a media folder, or a card whose media folder is
      empty next to unrelated files). Kept separate from ``is_empty`` so the
      UIs never call a well-filled volume "empty".

    Exactly one of ``is_empty``/``no_media`` is set when there is nothing to
    copy; both are False for a volume with media.
    """
    has_media = has_dcim and _has_real_file(media_dir)
    has_content = has_media or _has_real_file(mountpoint)
    return has_media, not has_content, (not has_media and has_content)


class DeviceWatcher:
    """Listens for USB block devices and orchestrates transfers."""

    def __init__(
        self,
        config: Config,
        hub: StatusHub,
        transfer: TransferFn,
        transcode=None,
    ) -> None:
        import pyudev  # lazy: only present on the Cubie

        self._pyudev = pyudev
        self._config = config
        self._hub = hub
        self._transfer = transfer
        # Optional TranscodeManager: when auto-transcode is enabled it queues a
        # transcode of the just-copied files after a successful copy.
        self._transcode = transcode
        self._context = pyudev.Context()
        self._root_dev = volumes.root_source_device()
        # ``_armed`` guards against re-running a transfer for a device set that
        # was already handled. It is cleared when a transfer starts and re-armed
        # once fewer than two eligible volumes remain (i.e. one was removed).
        self._armed = True
        # ``_errored`` latches the error indication after a failed/aborted copy so
        # the red stays up until the user clears it by unplugging the device(s) --
        # otherwise the very USB-remove event that caused the abort would at once
        # re-evaluate back to DETECTING and the error would never be seen.
        self._errored = False
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
        self._errored = False
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
        """Block until a block event that can change which volumes are available.

        ``add``/``remove`` cover a volume appearing/disappearing. A pulled card in
        a reader that does not report a clean removal does NOT emit ``remove`` at
        once (the node lingers); instead the kernel emits a ``change`` carrying
        ``DISK_MEDIA_CHANGE`` -- so we react to that too, otherwise such a removal
        would go unnoticed until the (much later) ``remove``. Other ``change``
        events (benign property updates) are ignored so the post-copy 100 % view
        is not disturbed until a device is actually touched. This is the set of
        events needed for reliable detection across USB topologies / card readers.

        A clean ``systemctl stop`` terminates the process here (SIGTERM); the
        LEDs are switched off afterwards by the unit's ExecStopPost. Ctrl-C raises
        KeyboardInterrupt, which the daemon catches to shut down cleanly.
        """
        for device in iter(monitor.poll, None):
            if device.action in ("add", "remove"):
                return
            if device.action == "change" and device.get("DISK_MEDIA_CHANGE"):
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

    def _clear_role_storage(self) -> None:
        """Drop the source/target storage panes once the roles are stale.

        After a copy the SUCCESS view keeps the final figures. When a device is
        then removed, the phase falls back to DETECTING -- without this the UIs
        (e-paper prefers the source/target rows over the detected-volume rows,
        the web UI shows the storage cards) would keep rendering the finished
        copy's panes next to the new phase.
        """
        self._hub.set_storage(StorageInfo(), StorageInfo())

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
            self._hub.log_event(f"Detected: {names[node]}")
            # A quick green blink per recognised volume -- unmistakable on the LEDs.
            self._hub.signal(Event.DEVICE_DETECTED)
        for node in sorted(removed):
            self._hub.log_event(f"Removed: {self._node_names.pop(node, node)}")
        self._prev_nodes = current
        return bool(added), bool(removed)

    def _evaluate(self) -> None:
        """Probe all present volumes; wait, or transfer once two are eligible.

        Serialised with video transcode jobs through the shared ``operation_lock``
        so the daemon never mounts/writes a removable volume while a transcode is
        writing one (and vice versa). Both run as threads in this process, so the
        in-process lock is sufficient. A transcode in flight simply defers the
        next evaluation until it finishes.
        """
        with self._hub.state.operation_lock:
            self._evaluate_impl()

    def _evaluate_impl(self) -> None:
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

            # While latched in error, keep showing the red alarm until the user
            # clears it by unplugging everything. Any device still present holds
            # the error; an empty bus resets it (handled by the READY block below).
            if self._errored:
                if eligible:
                    self._hub.set_devices(device_views(probes, min_bytes))
                    return
                self._errored = False

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

            # Feed the detected device's fill level to the LED gauge shown while
            # detecting (the web UI keeps its own per-device storage figures).
            fill = fill_fraction_for_display(eligible) or 0.0
            self._hub.set_fill(fill)

            if len(eligible) < 2:
                # Not enough to decide yet -- keep waiting and re-arm.
                self._armed = True
                self._hub.set_devices(device_views(probes, min_bytes))
                self._clear_role_storage()
                self._hub.set_phase(State.DETECTING)
                if added:
                    self._hub.log_event("Waiting for another device ...")
                _LOG.info("Detecting -- %d eligible volume(s), need 2 ...", len(eligible))
                return

            # No usable source yet -- either a source-shaped card is present but
            # its DCIM is empty, or none of the present volumes looks like a
            # source at all (e.g. two completely blank cards). Neither is an
            # error: the source may simply not be attached yet. Show what is
            # present and keep waiting -- no transfer, no red alarm, so there is
            # no "unplug everything to reset" dance.
            if not has_source(eligible):
                self._armed = True
                self._hub.set_devices(device_views(probes, min_bytes))
                self._clear_role_storage()
                self._hub.set_phase(State.DETECTING)
                if added:
                    if has_empty_source(eligible):
                        self._hub.log_event("Source DCIM empty -- nothing to copy")
                    else:
                        self._hub.log_event("No source detected -- waiting")
                _LOG.info("No usable source present -- waiting.")
                return

            # Two or more eligible volumes -- decide roles for real.
            try:
                source, target = select_roles(probes, min_bytes, require_smaller)
            except TransferError:
                self._hub.set_devices(device_views(probes, min_bytes))
                raise

            if not self._armed:
                # This set was already handled (a copy ran). Show the raw detected
                # volumes -- candidates/empty, like ready->detecting -- so stale
                # source/target labels don't linger, and wait for a removal to
                # re-arm. The phase is left as-is (a SUCCESS 100 % view persists).
                self._hub.set_devices(device_views(probes, min_bytes))
                return

            # Roles are shown only when a copy is actually imminent.
            self._hub.set_devices(device_views(probes, min_bytes, source, target))
            self._armed = False
            _LOG.info(
                "Source = %s (%s, %d B), Target = %s (%d B)",
                source.device_node, source.name, source.capacity,
                target.device_node, target.capacity,
            )
            self._hub.log_event(f"Source: {source.name}  Target: {target.name}")

            # Let the source's fill gauge have its moment before the copy bar takes
            # over: keep it up (sticky) and hold in DETECTING for the gauge
            # duration, then start the copy. Sticky => the gauge stays visible
            # until COPYING replaces it, so there is no gap before the copy. The
            # (possibly slow) size scan happens INSIDE the hold so the copy bar
            # appears promptly when the gauge time is up, not after an extra pause.
            self._hub.set_fill(fill, sticky=True)
            self._hub.set_phase(State.DETECTING)
            media_dir = source.mountpoint / self._config.media_dirname
            required = self._hold_before_copy(source, target, media_dir)
            if required is None:
                self._armed = True
                self._hub.log_event("Device removed before copy")
                _LOG.info("A device disappeared during the pre-copy hold.")
                return

            def _refresh_devices() -> None:
                # Re-measure the mounts and republish, so the web view tracks the
                # filling target during the copy and the emptied source afterwards.
                refreshed = [self._restat(p) for p in probes]
                self._hub.set_devices(device_views(refreshed, min_bytes, source, target))

            dest = self._transfer(
                source_root=source.mountpoint,
                target_root=target.mountpoint,
                source_name=source.name,
                source_device=source.device_node,
                target_device=target.device_node,
                target_name=target.name,
                hub=self._hub,
                config=self._config,
                on_devices_refresh=_refresh_devices,
                required=required,  # measured during the hold -> no re-scan delay
            )
            self._hub.log_event("Ready to remove devices")
            # Auto-transcode: queue the just-copied video files (which now live on
            # the TARGET) while the target is still mounted here. The finally below
            # then unmounts BOTH cards and releases the operation lock, so the
            # transcode worker only starts encoding once the source is released --
            # it can be pulled while the batch runs.
            self._maybe_queue_auto_transcode(target, dest)
        except TransferError as exc:
            self._armed = False
            self._errored = True
            _LOG.error("Transfer failed: %s", exc)
            self._hub.log_event(f"Copy failed: {exc}", level="error")
            self._hub.set_error(str(exc))
        except Exception as exc:  # pragma: no cover - defensive
            self._armed = False
            self._errored = True
            _LOG.exception("Unexpected error: %s", exc)
            self._hub.log_event(f"Unexpected error: {exc}", level="error")
            self._hub.set_error(str(exc))
        finally:
            subprocess.run(["sync"], check=False)
            for probe in probes:
                self._umount(probe.mountpoint)

    def _maybe_queue_auto_transcode(self, target: Probe, dest: Path) -> None:
        """Queue an auto-transcode of the just-copied video files, if enabled.

        The copied files live under ``dest`` on the target volume; each video
        file becomes one transcode job (output onto the same target's
        ``Transcoded/`` folder), using the persisted default preset. Enqueuing
        happens here (target still mounted) but the transcode worker only starts
        once the caller's ``finally`` has unmounted the cards and released the
        operation lock -- so the source is safely released first. Best-effort: any
        failure is logged and never affects the completed copy.
        """
        tc = self._transcode
        if tc is None or not getattr(tc, "auto_transcode", False):
            return
        preset = tc.default_preset
        if not preset:
            _LOG.warning("Auto-transcode enabled but no preset configured -- skipping.")
            return
        try:
            rels = []
            for path in sorted(Path(dest).rglob("*")):
                if not path.is_file() or path.is_symlink():
                    continue
                if not is_video_file(path.name):
                    continue
                try:
                    rels.append(path.relative_to(target.mountpoint).as_posix())
                except ValueError:  # pragma: no cover - defensive
                    continue
            if not rels:
                self._hub.log_event("Auto-transcode: no video files to convert")
                return
            tc.submit_auto(target.sys_name, rels, mount_root=target.mountpoint,
                           output_device=target.sys_name, preset_id=preset)
            self._hub.log_event(
                f"Auto-transcode: queued {len(rels)} file(s) [{preset}]"
            )
        except Exception as exc:  # pragma: no cover - never break a good copy
            _LOG.warning("Auto-transcode could not be queued: %s", exc)
            self._hub.log_event(f"Auto-transcode skipped: {exc}", level="error")

    def _hold_before_copy(self, source: Probe, target: Probe, media_dir: Path):
        """Stay in DETECTING for the fill-gauge duration before copying.

        The render thread shows the source's fill gauge during this hold, so the
        user sees how full the source is before the copy bar takes over. The
        source size scan runs INSIDE the window (it counts towards the hold), so
        the copy bar appears promptly afterwards instead of after an extra scan.
        Polls for device removal so a card pulled here does not start a doomed
        copy. Returns the source media size in bytes, or None if a device
        disappeared.
        """
        deadline = time.monotonic() + FILL_GAUGE_SECONDS
        try:
            required = total_size(media_dir)
        except OSError:
            return None
        while time.monotonic() < deadline:
            if not self._both_present(source, target):
                return None
            time.sleep(0.1)
        return required if self._both_present(source, target) else None

    @staticmethod
    def _both_present(source: Probe, target: Probe) -> bool:
        # Node existence AND the kernel's backing-disk capacity, so a pulled card
        # whose node lingers is caught during the pre-copy hold too -- passively.
        return (
            volume_alive(source.device_node, source.mountpoint)
            and volume_alive(target.device_node, target.mountpoint)
        )

    def _is_candidate(self, device) -> bool:
        """True if the device is a mountable USB volume (not root).

        Thin wrapper around :func:`copystation.volumes.is_usb_volume`, which is
        the shared source of truth used by the web file browser too.
        """
        return volumes.is_usb_volume(device, self._root_dev)

    def _current_partitions(self) -> list:
        """All currently present, eligible *and live* USB volumes.

        ``volume_alive`` drops a node whose backing disk the kernel has zeroed --
        a card pulled from a reader leaves the node lingering for several seconds,
        and without this filter it would still be listed (and re-shown with its
        old role) until the late ``remove``.
        """
        result = []
        for device in self._context.list_devices(subsystem="block"):
            if self._is_candidate(device) and volume_alive(device.device_node):
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
        has_media, is_empty, no_media = _content_flags(mountpoint, media_dir, has_dcim)
        return Probe(
            sys_name=dev.sys_name,
            device_node=dev.device_node,
            mountpoint=mountpoint,
            has_dcim=has_dcim,
            matched_source=self._matches_source(dev),
            capacity=capacity,
            free=free,
            name=self._volume_name(dev),
            has_media=has_media,
            is_empty=is_empty,
            no_media=no_media,
            has_label=self._configured_label(dev) is not None,
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
        has_media, is_empty, no_media = _content_flags(probe.mountpoint, media_dir, has_dcim)
        return replace(
            probe,
            capacity=stat.f_frsize * stat.f_blocks,
            free=stat.f_frsize * stat.f_bavail,
            has_dcim=has_dcim,
            has_media=has_media,
            is_empty=is_empty,
            no_media=no_media,
        )

    @staticmethod
    def _usb_ids(device) -> tuple[str, str]:
        """(vid, pid) of a device, lowercased. See :func:`volumes.usb_ids`."""
        return volumes.usb_ids(device)

    def _matches_source(self, device) -> bool:
        """Optional hardening via USB VID/PID allowlist (otherwise always True)."""
        ident = self._config.get("identify", {})
        vids = [v.lower() for v in ident.get("source_usb_vendor_ids", [])]
        pids = [p.lower() for p in ident.get("source_usb_product_ids", [])]
        if not vids and not pids:
            return True
        vid, pid = volumes.usb_ids(device)
        if vids and vid not in vids:
            return False
        if pids and pid not in pids:
            return False
        return True

    def _volume_name(self, device) -> str:
        """Human-friendly volume name. See :func:`volumes.volume_name`."""
        return volumes.volume_name(device, self._config)

    def _configured_label(self, device) -> Optional[str]:
        """Friendly name from ``identify.device_labels``. See :func:`volumes.configured_label`."""
        return volumes.configured_label(device, self._config)

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
